"""Rebuild fill quality for every COMPLETE run from the execution audit.

One CSV row is one exchange order, including split orders. Daily opens are
requested for exact historical dates, so a stale rolling cache cannot omit
old or recent runs. The previous CSV is replaced only when all opens exist.
This script never places orders.
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd

from honest.data import _get

ROOT = Path(__file__).resolve().parent
DEFAULT_AUDIT = ROOT / ".execution" / "testnet_execution.sqlite3"
OUT = ROOT / "execution_quality.csv"
COLS = ("fill_date", "run_id", "order_id", "symbol", "side", "reason", "qty",
        "avg_fill", "day_open", "shortfall_bps", "engine_slippage_bps")


def _ms(day: date) -> int:
    return int(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)


def daily_opens(symbol: str, first: date, last: date) -> dict[date, float]:
    """Fetch a pinned range, in chunks small enough for Binance's kline limit."""
    opens = {}
    cursor = first
    while cursor <= last:
        end = min(last, cursor + timedelta(days=999))
        query = urlencode({"symbol": symbol, "interval": "1d", "startTime": _ms(cursor),
                           "endTime": _ms(end + timedelta(days=1)) - 1, "limit": 1000})
        bars = _get(f"https://fapi.binance.com/fapi/v1/klines?{query}")
        for bar in bars:
            day = datetime.fromtimestamp(int(bar[0]) / 1000, tz=timezone.utc).date()
            opens[day] = float(bar[1])
        cursor = end + timedelta(days=1)
    return opens


def load_legs(audit: Path) -> list[tuple]:
    with sqlite3.connect(f"file:{audit.as_posix()}?mode=ro", uri=True) as conn:
        return conn.execute(
            "SELECT r.run_id,substr(r.started_utc,1,10),l.order_id,l.symbol,l.side,l.reason,"
            "l.quantity,l.avg_fill_price,l.fill_slippage_bps "
            "FROM execution_legs l JOIN execution_runs r ON l.run_id=r.run_id "
            "WHERE r.dry_run=0 AND r.status='COMPLETE' AND l.status='FILLED' "
            "AND l.order_id!='' AND l.reason!='emergency_flatten' AND l.avg_fill_price>0 "
            "ORDER BY r.started_utc,l.sequence"
        ).fetchall()


def build_rows(legs: list[tuple], open_lookup) -> list[dict]:
    """Fail closed if any COMPLETE order cannot be priced."""
    dates_by_symbol: dict[str, list[date]] = defaultdict(list)
    for _, day, _, symbol, *_ in legs:
        dates_by_symbol[symbol].append(date.fromisoformat(day))
    opens = {symbol: open_lookup(symbol, min(days), max(days))
             for symbol, days in dates_by_symbol.items()}
    rows = []
    seen = set()
    for run_id, day, order_id, symbol, side, reason, qty, avg_fill, slip in legs:
        key = (run_id, symbol, str(order_id))
        if key in seen:
            raise ValueError(f"duplicate exchange order in audit: {key}")
        seen.add(key)
        day_open = opens[symbol].get(date.fromisoformat(day))
        if not day_open or day_open <= 0:
            raise RuntimeError(f"missing daily open for {symbol} {day}; keeping previous report")
        signed = (1 if side == "BUY" else -1) * (float(avg_fill) / day_open - 1) * 10_000
        rows.append(dict(zip(COLS, (day, run_id, order_id, symbol, side, reason, qty,
                                    float(avg_fill), day_open, round(signed, 6),
                                    slip if slip is not None else ""))))
    return rows


def write_atomic(rows: list[dict], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8", delete=False,
                                         dir=output.parent, prefix=f".{output.stem}.", suffix=".tmp") as fh:
            temp_path = Path(fh.name)
            writer = csv.DictWriter(fh, fieldnames=COLS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_path, output)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.out is None:
        if args.audit.resolve() != DEFAULT_AUDIT.resolve():
            parser.error("--out is required when --audit is not the default testnet audit")
        args.out = OUT
    if not args.audit.exists():
        print("No audit DB yet - nothing to analyze")
        return 0
    legs = load_legs(args.audit)
    if not legs:
        print("No COMPLETE filled orders yet")
        return 0
    rows = build_rows(legs, daily_opens)
    write_atomic(rows, args.out)
    df = pd.DataFrame(rows)
    shortfall = df.shortfall_bps.astype(float)
    notional = df.qty.astype(float) * df.avg_fill.astype(float)
    weighted = float((shortfall * notional).sum() / notional.sum())
    engine = pd.to_numeric(df.engine_slippage_bps, errors="coerce")
    valid = engine.notna()
    mid = float((engine[valid] * notional[valid]).sum() / notional[valid].sum()) if valid.any() else float("nan")
    print(f"COMPLETE exchange orders: {len(df)} across {df.run_id.nunique()} run(s)")
    print(f"shortfall vs paper open: equal-order mean {shortfall.mean():+.2f}bps; "
          f"notional-weighted {weighted:+.2f}bps")
    print(f"submit-time mid slippage, notional-weighted: {mid:+.2f}bps")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
