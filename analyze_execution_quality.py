"""Paper-vs-executed fill quality: the real cost model, from real fills.

The paper record books every fill at the daily OPEN. The executor fills at ~00:20 UTC
market. The gap between those two prices, signed by side, is the implementation
shortfall the paper record silently assumes away - and the number that decides whether
the live cost assumptions (10bps/leg) were honest.

    python analyze_execution_quality.py                      # testnet audit (default)
    python analyze_execution_quality.py --audit .execution/live_execution.sqlite3

Reads FILLED legs of non-dry-run runs, joins each with that day's daily open from the
public klines, and reports per-leg and aggregate:

    shortfall_bps = side_sign * (avg_fill / day_open - 1) * 1e4     (+ = paid worse than paper)
    slippage_bps  = as recorded by the engine vs its submit-time re-quote

Empty-safe: prints a clear message until COMPLETE runs exist. Appends execution_quality.csv
so the series accumulates run by run.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from honest.data import fetch_klines  # noqa: E402

OUT = ROOT / "execution_quality.csv"
COLS = ("fill_date", "run_id", "symbol", "side", "reason", "qty", "avg_fill", "day_open",
        "shortfall_bps", "engine_slippage_bps")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", default=str(ROOT / ".execution" / "testnet_execution.sqlite3"))
    args = ap.parse_args()
    if not Path(args.audit).exists():
        print("No audit DB yet - nothing to analyze.")
        return 0
    conn = sqlite3.connect(args.audit)
    legs = conn.execute(
        "SELECT r.run_id, substr(r.started_utc,1,10), l.symbol, l.side, l.reason, l.quantity, "
        "l.avg_fill_price, l.fill_slippage_bps "
        "FROM execution_legs l JOIN execution_runs r ON r.run_id=l.run_id "
        "WHERE r.dry_run=0 AND l.status='FILLED' AND l.avg_fill_price > 0 "
        "ORDER BY r.started_utc, l.sequence"
    ).fetchall()
    if not legs:
        print("No filled legs in the audit yet (no COMPLETE/partial runs). Re-run after the "
              "daily rehearsals accumulate - the analysis activates by itself.")
        return 0

    already = set()
    if OUT.exists():
        with OUT.open(encoding="utf-8") as fh:
            already = {(r["run_id"], r["symbol"], r["reason"]) for r in csv.DictReader(fh)}

    opens: dict[str, pd.Series] = {}
    rows = []
    for run_id, day, symbol, side, reason, qty, avg_fill, slip in legs:
        if (run_id, symbol, reason) in already:
            continue
        if symbol not in opens:
            k = fetch_klines(symbol, "1d", 90, use_cache=True)
            idx = pd.to_datetime(k["Open time"]).dt.normalize()
            opens[symbol] = k.set_index(idx)["Open"]
        day_ts = pd.Timestamp(day)
        if day_ts not in opens[symbol].index:
            continue
        day_open = float(opens[symbol].loc[day_ts])
        sign = 1.0 if side == "BUY" else -1.0
        shortfall = sign * (float(avg_fill) / day_open - 1.0) * 1e4
        rows.append(dict(zip(COLS, (day, run_id, symbol, side, reason, qty,
                                    float(avg_fill), day_open, round(shortfall, 2),
                                    slip if slip is not None else ""))))

    if rows:
        new_file = not OUT.exists()
        with OUT.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=COLS)
            if new_file:
                w.writeheader()
            w.writerows(rows)

    df = pd.read_csv(OUT)
    print(f"legs analyzed: {len(df)} across {df.run_id.nunique()} run(s), {df.fill_date.nunique()} day(s)")
    s = df.shortfall_bps.astype(float)
    print(f"shortfall vs paper open:  mean {s.mean():+.1f}bps  median {s.median():+.1f}  p90 {s.quantile(.9):+.1f}  worst {s.max():+.1f}")
    print(f"  (+ = executed worse than the paper assumption; sustained mean >> 10bps means the live cost model must be raised)")
    per_sym = df.groupby("symbol").shortfall_bps.agg(["count", "mean"]).sort_values("mean", ascending=False)
    print("\nworst symbols:")
    print(per_sym.head(5).round(1).to_string())
    print(f"\nappended -> {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
