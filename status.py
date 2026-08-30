"""One command, whole picture. For the operator who should not have to open five files.

    python status.py

Paper: day count, equity, drawdown, gate distance.
Testnet: COMPLETE count, last run, days missed since the rehearsal started (machine off),
         open markers that block the next run.
Canary: last row and whether it is stale.
Fills:  execution-quality summary if any fills exist.
Never touches the network. Exit 0 always - it informs, it does not judge.
"""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

TESTNET_START = date(2026, 8, 25)      # first day the $2000 ceiling was in force


def _paper():
    led = ROOT / "carry_paper_ledger.csv"
    cfg = json.loads((ROOT / "carry_paper_config_v1.json").read_text(encoding="utf-8"))
    gate = cfg["go_live_gate"]
    if not led.exists():
        return "  paper: no ledger yet"
    rows = list(csv.DictReader(led.open(encoding="utf-8")))
    pnl = [float(r["pnl"]) for r in rows]
    eq, peak, dd = 1.0, 1.0, 0.0
    for p in pnl:
        eq *= 1 + p; peak = max(peak, eq); dd = min(dd, eq / peak - 1)
    n = len(pnl)
    import statistics as st
    sh = (st.mean(pnl) / st.stdev(pnl) * (365 ** 0.5)) if n > 2 and st.stdev(pnl) > 0 else float("nan")
    last = rows[-1]["fill_day"]
    lag = (date.today() - date.fromisoformat(last)).days
    return (f"  paper   day {n}/{gate['min_paper_days']} (target {gate['recommended_paper_days']})  "
            f"equity {eq:.4f} ({(eq - 1) * 100:+.2f}%)  sharpe {sh:+.2f}  maxDD {dd * 100:.1f}%  "
            f"last booked {last} ({lag}d ago{' - OK, needs next open' if lag <= 2 else ' - CHECK paper task'})")


def _testnet():
    db = ROOT / ".execution" / "testnet_execution.sqlite3"
    if not db.exists():
        return "  testnet: no audit DB yet"
    c = sqlite3.connect(db)
    runs = c.execute("SELECT substr(started_utc,1,10), status FROM execution_runs WHERE dry_run=0 ORDER BY started_utc").fetchall()
    complete_days = {d for d, s in runs if s == "COMPLETE"}
    other = [(d, s) for d, s in runs if s != "COMPLETE" and date.fromisoformat(d) >= TESTNET_START]
    today = date.today()
    expected = [(TESTNET_START + timedelta(days=i)) for i in range((today - TESTNET_START).days)]
    missed = [d.isoformat() for d in expected if d.isoformat() not in complete_days
              and d.isoformat() not in {x[0] for x in other}]
    last = runs[-1][0] if runs else "never"
    line = (f"  testnet COMPLETE {len(complete_days)}/20 needed  last run {last}  "
            f"missed (machine off?) {len(missed)}: {', '.join(missed[-5:]) or '-'}")
    if other:
        line += f"\n           non-COMPLETE runs since {TESTNET_START}: " + ", ".join(f"{d}:{s}" for d, s in other)
    return line


def _markers():
    ex = ROOT / ".execution"
    found = [p.name for p in ex.glob("*") if p.name in ("ATTENTION", "canary_ALERT", "testnet_daily.lock")]
    if not found:
        return "  markers none - next scheduled run will proceed"
    return "  MARKERS PRESENT -> " + ", ".join(found) + "   (runs are blocked until resolved; see EXECUTION_RUNBOOK.md)"


def _canary():
    p = ROOT / "canary_log.csv"
    if not p.exists():
        return "  canary: never run"
    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    r = rows[-1]
    age = (date.today() - date.fromisoformat(r["date"])).days
    return (f"  canary  {r['date']} ({age}d ago{' - STALE, weekly task not running?' if age > 8 else ''})  "
            f"Binance {float(r['sharpe_binance_weights']):+.2f} vs Bybit {float(r['sharpe_bybit_weights']):+.2f}  "
            f"funding {float(r['median_funding_ann_pct']):+.1f}%/yr  {('ALERT: ' + r['alerts']) if r['alerts'] else 'clear'}")


def _fills():
    p = ROOT / "execution_quality.csv"
    if not p.exists():
        return "  fills: none analysed yet (run analyze_execution_quality.py after COMPLETE runs)"
    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    s = sorted(float(r["shortfall_bps"]) for r in rows)
    mean = sum(s) / len(s); p90 = s[int(0.9 * (len(s) - 1))]
    return f"  fills   {len(s)} legs  shortfall vs paper open: mean {mean:+.1f}bps  p90 {p90:+.1f}bps  (paper assumes ~10)"


def main() -> int:
    now = datetime.now(timezone.utc)
    print(f"CARRY-7d status  {now:%Y-%m-%d %H:%M} UTC  ({now.astimezone():%H:%M} local)")
    for fn in (_paper, _testnet, _markers, _canary, _fills):
        try:
            print(fn())
        except Exception as exc:  # noqa: BLE001 - a status screen must never crash
            print(f"  {fn.__name__[1:]}: (could not read: {type(exc).__name__}: {exc})")
    print("  next: paper 07:05, testnet 07:20 daily; canary Sun 08:00 - machine must be awake.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
