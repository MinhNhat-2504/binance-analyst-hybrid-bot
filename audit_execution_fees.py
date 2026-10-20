"""Read-only Binance testnet fee audit for COMPLETE execution runs.

Matches exchange userTrades by (symbol, orderId) to the local execution audit.
Recovery, dry-run and incomplete runs are excluded. No credentials or raw trades
are written to the report.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from execution.binance_futures import DEMO_TESTNET_BASE_URL, FuturesREST
from honest.data import _get

ROOT = Path(__file__).resolve().parent
AUDIT = ROOT / ".execution" / "testnet_execution.sqlite3"
OUT = ROOT / "reports" / "execution_fee_audit.json"


def windows(days: list[date]) -> list[tuple[date, date]]:
    """Group sparse fill dates into Binance's <=7-day userTrades windows."""
    unique = sorted(set(days))
    if not unique:
        return []
    groups = []
    start = end = unique[0]
    for day in unique[1:]:
        if (day - start).days <= 6:
            end = day
        else:
            groups.append((start, end))
            start = end = day
    groups.append((start, end))
    return groups


def _ms(day: date, *, end: bool) -> int:
    moment = datetime.combine(day + (timedelta(days=1) if end else timedelta()),
                              time.min, tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000) - (1 if end else 0)


def audit_rows() -> dict[tuple[str, int], tuple[str, date]]:
    conn = sqlite3.connect(f"file:{AUDIT.as_posix()}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT l.symbol,l.order_id,r.run_id,substr(r.started_utc,1,10) "
        "FROM execution_legs l JOIN execution_runs r ON r.run_id=l.run_id "
        "WHERE r.environment='testnet' AND r.dry_run=0 AND r.status='COMPLETE' "
        "AND l.status='FILLED' AND l.order_id!='' AND l.reason!='emergency_flatten'"
    ).fetchall()
    conn.close()
    return {(symbol, int(order_id)): (run_id, date.fromisoformat(day))
            for symbol, order_id, run_id, day in rows}


def shortfall_vs_paper_open(orders: dict[tuple[str, int], tuple[str, date]]) -> dict:
    """All matched COMPLETE legs, valued against the same daily open paper books."""
    conn = sqlite3.connect(f"file:{AUDIT.as_posix()}?mode=ro", uri=True)
    legs = conn.execute(
        "SELECT l.symbol,l.order_id,l.side,CAST(l.quantity AS REAL),l.avg_fill_price,"
        "l.fill_slippage_bps,substr(r.started_utc,1,10) "
        "FROM execution_legs l JOIN execution_runs r ON r.run_id=l.run_id "
        "WHERE r.environment='testnet' AND r.dry_run=0 AND r.status='COMPLETE' "
        "AND l.status='FILLED' AND l.order_id!='' AND l.reason!='emergency_flatten'"
    ).fetchall()
    conn.close()
    dates_by_symbol: dict[str, list[date]] = defaultdict(list)
    for symbol, _, _, _, _, _, day in legs:
        dates_by_symbol[symbol].append(date.fromisoformat(day))
    opens: dict[str, dict[date, float]] = {}
    errors = []
    for symbol, days in dates_by_symbol.items():
        first, last = min(days), max(days)
        try:
            bars = _get("https://fapi.binance.com/fapi/v1/klines?"
                        f"symbol={symbol}&interval=1d&startTime={_ms(first, end=False)}"
                        f"&endTime={_ms(last, end=True)}&limit=1500")
            opens[symbol] = {datetime.fromtimestamp(int(bar[0]) / 1000,
                                                       tz=timezone.utc).date(): float(bar[1])
                             for bar in bars}
        except Exception as exc:
            errors.append(f"{symbol}:{type(exc).__name__}")
            opens[symbol] = {}
    totals = {"covered_orders": 0, "missing_open_orders": 0, "notional_usdt": 0.0,
              "signed_shortfall_usdt": 0.0, "submit_mid_slippage_usdt": 0.0,
              "submit_mid_coverage": 0}
    by_run: dict[str, dict] = defaultdict(lambda: {"orders": 0, "notional_usdt": 0.0,
                                                    "signed_shortfall_usdt": 0.0})
    for symbol, order_id, side, quantity, avg_fill, mid_slip, day in legs:
        if (symbol, int(order_id)) not in orders:
            continue
        day_open = opens[symbol].get(date.fromisoformat(day))
        if not day_open or not avg_fill:
            totals["missing_open_orders"] += 1
            continue
        notional = float(quantity) * float(avg_fill)
        shortfall_bps = (1 if side == "BUY" else -1) * (float(avg_fill) / day_open - 1) * 10_000
        totals["covered_orders"] += 1
        totals["notional_usdt"] += notional
        totals["signed_shortfall_usdt"] += notional * shortfall_bps / 10_000
        run_row = by_run[orders[(symbol, int(order_id))][0]]
        run_row["orders"] += 1
        run_row["notional_usdt"] += notional
        run_row["signed_shortfall_usdt"] += notional * shortfall_bps / 10_000
        if mid_slip is not None:
            totals["submit_mid_coverage"] += 1
            totals["submit_mid_slippage_usdt"] += notional * float(mid_slip) / 10_000
    n = totals["notional_usdt"]
    totals["shortfall_bps_vs_paper_open"] = 10_000 * totals["signed_shortfall_usdt"] / n if n else None
    totals["submit_mid_slippage_bps"] = 10_000 * totals["submit_mid_slippage_usdt"] / n if n else None
    totals["errors"] = errors
    for row in by_run.values():
        row["shortfall_bps_vs_paper_open"] = 10_000 * row["signed_shortfall_usdt"] / row["notional_usdt"]
    totals["by_run"] = dict(by_run)
    return totals


def main() -> int:
    orders = audit_rows()
    if not orders:
        print("No COMPLETE testnet fills to audit")
        return 2
    days_by_symbol: dict[str, list[date]] = defaultdict(list)
    for (symbol, _), (_, day) in orders.items():
        days_by_symbol[symbol].append(day)
    client = FuturesREST.from_env("testnet")
    if client.base_url != DEMO_TESTNET_BASE_URL:
        raise RuntimeError("fee audit expects the existing Binance Demo Futures account")
    matched: dict[tuple[str, int], list[dict]] = defaultdict(list)
    full_windows = []
    errors = []
    for symbol in sorted(days_by_symbol):
        for first, last in windows(days_by_symbol[symbol]):
            try:
                trades = client.signed("GET", "/fapi/v1/userTrades", {
                    "symbol": symbol, "startTime": _ms(first, end=False),
                    "endTime": _ms(last, end=True), "limit": 1000})
                if len(trades) >= 1000:
                    full_windows.append(f"{symbol}:{first}..{last}")
                for trade in trades:
                    key = (symbol, int(trade["orderId"]))
                    if key in orders:
                        matched[key].append(trade)
            except Exception as exc:  # never print signed URLs containing a request signature
                errors.append(f"{symbol}:{first}..{last}:{type(exc).__name__}")
    matched_ids = sorted(matched)
    unmatched = sorted(set(orders) - set(matched))
    by_run: dict[str, dict] = defaultdict(lambda: {"orders": 0, "trades": 0,
                                                   "notional_usdt": 0.0, "commission_usdt": 0.0,
                                                   "maker_notional_usdt": 0.0})
    commission_assets = defaultdict(float)
    for key, trades in matched.items():
        run_id = orders[key][0]
        by_run[run_id]["orders"] += 1
        by_run[run_id]["trades"] += len(trades)
        for trade in trades:
            notional = float(trade.get("quoteQty") or 0)
            if notional <= 0:
                notional = float(trade["qty"]) * float(trade["price"])
            commission = float(trade["commission"])
            asset = str(trade.get("commissionAsset", ""))
            by_run[run_id]["notional_usdt"] += notional
            if trade.get("maker"):
                by_run[run_id]["maker_notional_usdt"] += notional
            commission_assets[asset] += commission
            if asset == "USDT":
                by_run[run_id]["commission_usdt"] += commission
    totals = {key: sum(row[key] for row in by_run.values())
              for key in ("orders", "trades", "notional_usdt", "commission_usdt", "maker_notional_usdt")}
    all_usdt = set(commission_assets) <= {"USDT"}
    totals["commission_bps"] = (10_000 * totals["commission_usdt"] / totals["notional_usdt"]
                                if totals["notional_usdt"] and all_usdt else None)
    totals["maker_notional_share_pct"] = (100 * totals["maker_notional_usdt"] / totals["notional_usdt"]
                                          if totals["notional_usdt"] else None)
    for row in by_run.values():
        row["commission_bps"] = (10_000 * row["commission_usdt"] / row["notional_usdt"]
                                 if row["notional_usdt"] and all_usdt else None)
    price = shortfall_vs_paper_open(orders)
    if price["missing_open_orders"] == 0 and totals["commission_bps"] is not None:
        price["fee_plus_shortfall_bps_vs_paper_open"] = (
            price["shortfall_bps_vs_paper_open"] + totals["commission_bps"])
    else:
        price["fee_plus_shortfall_bps_vs_paper_open"] = None
    for run_id, row in by_run.items():
        price_row = price["by_run"].get(run_id)
        if price_row and row["commission_bps"] is not None:
            row["shortfall_bps_vs_paper_open"] = price_row["shortfall_bps_vs_paper_open"]
            row["fee_plus_shortfall_bps_vs_paper_open"] = (
                row["commission_bps"] + price_row["shortfall_bps_vs_paper_open"])
    report = {"generated_utc": datetime.now(timezone.utc).isoformat(),
              "source": "Binance Demo Futures GET /fapi/v1/userTrades matched to COMPLETE audit orderIds",
              "audit_order_count": len(orders), "matched_order_count": len(matched_ids),
              "unmatched_order_count": len(unmatched), "unmatched_orders": [f"{s}:{o}" for s, o in unmatched],
              "full_windows": full_windows, "errors": errors,
              "commission_by_asset": dict(commission_assets), "totals": totals,
              "price_quality": price,
              "by_run": dict(by_run), "complete": not (unmatched or full_windows or errors)}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(f"matched {len(matched_ids)}/{len(orders)} orders; "
          f"commission {totals['commission_bps']}bps; maker share "
          f"{totals['maker_notional_share_pct']}%; "
          f"fee+paper-open shortfall {price['fee_plus_shortfall_bps_vs_paper_open']}bps; "
          f"complete={report['complete']} -> {OUT}")
    return 0 if report["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
