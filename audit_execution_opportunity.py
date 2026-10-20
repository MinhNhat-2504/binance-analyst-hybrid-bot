"""Read-only execution cost diagnosis and an optimistic maker-fee ceiling.

Uses the COMPLETE-order CSV and fee audit; queries account-specific commission
rates on Binance Demo Futures. A hypothetical all-maker fee saves commissions
only. It does not assume passive orders fill or preserve market prices.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from execution.binance_futures import DEMO_TESTNET_BASE_URL, FuturesREST

ROOT = Path(__file__).resolve().parent
QUALITY = ROOT / "execution_quality.csv"
FEES = ROOT / "reports" / "execution_fee_audit.json"
OUT = ROOT / "reports" / "execution_opportunity.json"


def summarize(rows: list[dict], fees: dict, rates: dict[str, dict]) -> dict:
    if not rows or not fees.get("complete"):
        raise ValueError("full COMPLETE-order quality and fee audits required")
    if len(rows) != fees["matched_order_count"]:
        raise ValueError("quality CSV and fee audit cover different order counts")
    if len({(r["run_id"], r["symbol"], r["order_id"]) for r in rows}) != len(rows):
        raise ValueError("quality CSV has duplicate order IDs")
    runs = {r["run_id"] for r in rows}
    if runs != set(fees["by_run"]):
        raise ValueError("quality CSV and fee audit cover different runs")
    groups: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    maker_usdt = 0.0
    notional = 0.0
    for row in rows:
        symbol = row["symbol"]
        if symbol not in rates:
            raise ValueError(f"commission rate unavailable: {symbol}")
        value = float(row["qty"]) * float(row["avg_fill"])
        slip = float(row["shortfall_bps"])
        mid = float(row["engine_slippage_bps"]) if row["engine_slippage_bps"] else float("nan")
        if value <= 0:
            raise ValueError("nonpositive notional")
        maker_usdt += value * float(rates[symbol]["makerCommissionRate"])
        notional += value
        groups[f"side:{row['side']}"].append((value, slip, mid))
        groups[f"reason:{row['reason']}"].append((value, slip, mid))
        groups[f"symbol:{symbol}"].append((value, slip, mid))
        groups[f"day:{row['fill_date']}"].append((value, slip, mid))

    def stat(values: list[tuple[float, float, float]]) -> dict:
        n = sum(x[0] for x in values)
        mids = [(v, m) for v, _, m in values if m == m]
        return {"orders": len(values), "notional_usdt": n,
                "equal_order_shortfall_bps": sum(x[1] for x in values) / len(values),
                "weighted_shortfall_bps": sum(v * s for v, s, _ in values) / n,
                "weighted_submit_mid_slippage_bps": (
                    sum(v * m for v, m in mids) / sum(v for v, _ in mids) if mids else None)}

    observed_fee_bps = float(fees["totals"]["commission_bps"])
    observed_shortfall_bps = float(fees["price_quality"]["shortfall_bps_vs_paper_open"])
    maker_fee_bps = 10_000 * maker_usdt / notional
    return {
        "orders": len(rows), "runs": len(runs), "notional_usdt": notional,
        "observed_fee_bps": observed_fee_bps,
        "observed_shortfall_bps_vs_paper_open": observed_shortfall_bps,
        "observed_all_in_bps_vs_paper_open": observed_fee_bps + observed_shortfall_bps,
        "hypothetical_all_maker_fee_bps_same_prices": maker_fee_bps,
        "maximum_fee_only_saving_bps_same_prices": observed_fee_bps - maker_fee_bps,
        "hypothetical_all_maker_all_in_bps_same_prices": maker_fee_bps + observed_shortfall_bps,
        "maker_caveat": "Fee-only arithmetic upper bound. No passive fill probability, queue position, "
                        "adverse selection, delay, fallback cost or price improvement is inferred.",
        "by_side": {k[5:]: stat(v) for k, v in groups.items() if k.startswith("side:")},
        "by_reason": {k[7:]: stat(v) for k, v in groups.items() if k.startswith("reason:")},
        "by_day": {k[4:]: stat(v) for k, v in groups.items() if k.startswith("day:")},
        "by_symbol_ge_5_orders": {k[7:]: stat(v) for k, v in groups.items()
                                   if k.startswith("symbol:") and len(v) >= 5},
    }


def main() -> int:
    with QUALITY.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    fees = json.loads(FEES.read_text(encoding="utf-8"))
    if len(rows) != fees["matched_order_count"]:
        raise RuntimeError("fee and fill audits must cover the same COMPLETE runs")
    client = FuturesREST.from_env("testnet")
    if client.base_url != DEMO_TESTNET_BASE_URL:
        raise RuntimeError("commission audit requires the existing Demo Futures account")
    rates = {symbol: client.signed("GET", "/fapi/v1/commissionRate", {"symbol": symbol})
             for symbol in sorted({r["symbol"] for r in rows})}
    result = {"generated_utc": datetime.now(timezone.utc).isoformat(),
              "source": "COMPLETE execution audit, Binance Demo Futures userTrades and commissionRate",
              "summary": summarize(rows, fees, rates),
              "account_commission_rates": {symbol: {
                  "makerCommissionRate": str(data["makerCommissionRate"]),
                  "takerCommissionRate": str(data["takerCommissionRate"])}
                  for symbol, data in rates.items()}}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    s = result["summary"]
    print(f"{s['orders']} orders, observed all-in {s['observed_all_in_bps_vs_paper_open']:.2f}bps; "
          f"all-maker fee-only ceiling {s['maximum_fee_only_saving_bps_same_prices']:.2f}bps")
    print(f"-> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
