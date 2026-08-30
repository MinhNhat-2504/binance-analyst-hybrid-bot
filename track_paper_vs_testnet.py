"""Tracking error: does the executed book earn what the paper record says it should?

GO_LIVE_CHECKLIST's stop rule is "live diverges from paper by >1%/week for 3 weeks". This
is the instrument for it, rehearsed on testnet first.

    paper  : carry_paper_ledger.csv  daily pnl (fraction of a 1.0-gross book) x budget
    actual : Binance income history  REALIZED_PNL + FUNDING_FEE + COMMISSION per UTC day

Only days where BOTH exist are compared (a missed testnet day is not tracking error, it
is a missed day - status.py reports those separately). Prints per-day and cumulative
divergence and appends tracking_log.csv. Read-only on the exchange.

    python track_paper_vs_testnet.py                 # testnet
    python track_paper_vs_testnet.py --env live      # once live exists
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from execution.binance_futures import FuturesREST  # noqa: E402
from execution.contracts import frozen_ceiling  # noqa: E402

LEDGER = ROOT / "carry_paper_ledger.csv"
OUT = ROOT / "tracking_log.csv"
START = date(2026, 8, 25)


def income_by_day(client: FuturesREST, start: date) -> dict[str, float]:
    start_ms = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp() * 1000)
    totals: dict[str, float] = defaultdict(float)
    cursor = start_ms
    for _ in range(50):
        rows = client.signed("GET", "/fapi/v1/income", {"startTime": cursor, "limit": 1000})
        if not rows:
            break
        for r in rows:
            if r.get("incomeType") in ("REALIZED_PNL", "FUNDING_FEE", "COMMISSION"):
                day = datetime.fromtimestamp(int(r["time"]) / 1000, tz=timezone.utc).date().isoformat()
                totals[day] += float(r["income"])
        last = max(int(r["time"]) for r in rows)
        if len(rows) < 1000 or last <= cursor:
            break
        cursor = last + 1
        time.sleep(0.2)
    return dict(totals)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="testnet", choices=["testnet", "live"])
    args = ap.parse_args()
    budget = frozen_ceiling(args.env)
    if budget <= 0:
        print(f"{args.env}: ceiling is 0 - nothing to track."); return 0

    paper: dict[str, float] = {}
    if LEDGER.exists():
        for r in csv.DictReader(LEDGER.open(encoding="utf-8")):
            paper[r["fill_day"]] = float(r["pnl"]) * budget
    client = FuturesREST.from_env(args.env, required=True)
    actual = income_by_day(client, START)

    # Only days on which the executor actually rebalanced (COMPLETE run) count as
    # execution tracking. A missed day still earns funding on the stale book - real money,
    # but a MISSED-DAY effect, reported by status.py, not an execution divergence.
    import sqlite3
    db = ROOT / ".execution" / ("testnet_execution.sqlite3" if args.env == "testnet" else "live_execution.sqlite3")
    complete = set()
    if db.exists():
        c = sqlite3.connect(db)
        complete = {r[0] for r in c.execute("SELECT substr(started_utc,1,10) FROM execution_runs WHERE dry_run=0 AND status='COMPLETE'")}
    days = sorted(set(paper) & set(actual) & complete)
    days = [d for d in days if date.fromisoformat(d) >= START]
    if not days:
        print("No overlapping days yet between paper ledger and exchange income."); return 0

    cum_p = cum_a = 0.0
    rows = []
    print(f"{'day':10s} {'paper$':>9s} {'actual$':>9s} {'diff$':>8s} {'cum diff$':>10s} {'cum diff %':>10s}")
    for d in days:
        p, a = paper[d], actual[d]
        cum_p += p; cum_a += a
        diff = a - p; cum = cum_a - cum_p
        rows.append({"day": d, "paper_usd": round(p, 2), "actual_usd": round(a, 2), "diff_usd": round(diff, 2),
                     "cum_diff_usd": round(cum, 2), "cum_diff_pct_of_budget": round(cum / budget * 100, 3)})
        print(f"{d:10s} {p:+9.2f} {a:+9.2f} {diff:+8.2f} {cum:+10.2f} {cum / budget * 100:+9.2f}%")
    weeks = max(len(days) / 7, 1e-9)
    print(f"\n{len(days)} days compared. cumulative divergence {cum_a - cum_p:+.2f}$ = "
          f"{(cum_a - cum_p) / budget * 100:+.2f}% of budget  (~{(cum_a - cum_p) / budget * 100 / weeks:+.2f}%/week; stop rule: > 1%/week for 3 weeks)")
    print("note: testnet fills come from a demo book; treat the SIGN and ORDER OF MAGNITUDE as real, not the third decimal.")
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)
    print(f"-> {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
