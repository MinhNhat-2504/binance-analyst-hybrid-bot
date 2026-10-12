"""Tracking error: does the executed book earn what the paper record says it should?

GO_LIVE_CHECKLIST's stop rule is "live diverges from paper by >1%/week for 3 weeks". This
is the instrument for it, rehearsed on testnet first.

    paper  : carry_paper_ledger.csv   pnl of fill_day D (open D -> open D+1, mark-to-market,
                                      net of the modelled cost) x budget
    actual : account equity           totalMarginBalance read before the run of D+1 minus
                                      the same read before the run of D. Mark-to-market too:
                                      unrealized PnL, funding and commission are all in it.

Both sides are mark-to-market over the same ~24h with the same book. The first version of
this file compared paper to the exchange's REALIZED income per UTC day, which is not the
same kind of number: realized PnL lands on the day a position is closed, so a name held
nine days dumped nine days of gains into one row. On 2026-10-12 that method reported
+4.35% of budget of divergence over nine days and single days off by $66 on a $2,000 book;
the equity method on the same days shows the book within a few dollars of paper.

Only CLEAN intervals count as execution tracking:
    the run on day D ended COMPLETE (so the book held during the interval is paper's book),
    and the next equity read is 22-26 hours later (so nothing was skipped in between).
A refused or missed day leaves a stale book - real money, but a missed-day effect that
status.py reports, not an execution divergence. Those intervals are listed, not summed.

The equity series is the one the drawdown guard already records on every run
(.execution/equity_hwm_<env>.json), so this needs no network and no key.

Caveat: a deposit, withdrawal or demo-balance reset between two reads shows up as PnL.
An interval off by more than 5% of budget is flagged rather than trusted.

    python track_paper_vs_testnet.py                 # testnet
    python track_paper_vs_testnet.py --env live      # once live exists
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from execution.contracts import frozen_ceiling  # noqa: E402

LEDGER = ROOT / "carry_paper_ledger.csv"
OUT = ROOT / "tracking_log.csv"
COLUMNS = ("day", "paper_usd", "actual_usd", "diff_usd", "cum_diff_usd", "cum_diff_pct_of_budget")
CLEAN_HOURS = (22.0, 26.0)
SUSPECT_FRACTION_OF_BUDGET = 0.05


def _paths(env: str, root: Path) -> tuple[Path, Path]:
    return (root / ".execution" / f"equity_hwm_{env}.json",
            root / ("carry_testnet_log.csv" if env == "testnet" else "carry_live_log.csv"))


def load_equity(path: Path) -> list[tuple[datetime, float]]:
    if not path.exists():
        return []
    history = json.loads(path.read_text(encoding="utf-8")).get("history") or []
    return sorted((datetime.fromisoformat(r["utc"]), float(r["equity"])) for r in history)


def load_run_status(path: Path) -> dict[str, str]:
    """UTC day -> status of that day's run; COMPLETE wins if a day has more than one row."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for r in csv.DictReader(path.open(encoding="utf-8")):
        day = (r.get("utc") or "")[:10]
        if out.get(day) != "COMPLETE":
            out[day] = r.get("status", "")
    return out


def load_paper(path: Path, budget: float) -> dict[str, float]:
    if not path.exists():
        return {}
    return {r["fill_day"]: float(r["pnl"]) * budget for r in csv.DictReader(path.open(encoding="utf-8"))}


def intervals(equity: list[tuple[datetime, float]], status: dict[str, str], paper: dict[str, float],
              budget: float) -> list[dict]:
    rows = []
    for (ta, ea), (tb, eb) in zip(equity, equity[1:]):
        day = ta.date().isoformat()
        if day not in paper:
            continue
        hours = (tb - ta).total_seconds() / 3600.0
        actual = eb - ea
        diff = actual - paper[day]
        why = ""
        if status.get(day) != "COMPLETE":
            why = f"run {status.get(day) or 'absent'}"
        elif not CLEAN_HOURS[0] <= hours <= CLEAN_HOURS[1]:
            why = f"{hours:.0f}h to the next read"
        elif abs(diff) > SUSPECT_FRACTION_OF_BUDGET * budget:
            why = "off by >5% of budget (transfer or balance reset?)"
        rows.append({"day": day, "hours": round(hours, 1), "paper_usd": round(paper[day], 2),
                     "actual_usd": round(actual, 2), "diff_usd": round(diff, 2), "clean": not why, "why": why})
    return rows


def cumulative(clean: list[dict], budget: float) -> list[dict]:
    out, cum = [], 0.0
    for r in clean:
        cum += r["diff_usd"]
        out.append({"day": r["day"], "paper_usd": r["paper_usd"], "actual_usd": r["actual_usd"],
                    "diff_usd": r["diff_usd"], "cum_diff_usd": round(cum, 2),
                    "cum_diff_pct_of_budget": round(cum / budget * 100, 3)})
    return out


def write_log(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", default="testnet", choices=["testnet", "live"])
    args = ap.parse_args(argv)
    budget = float(frozen_ceiling(args.env))
    if budget <= 0:
        print(f"{args.env}: ceiling is 0 - nothing to track.")
        return 0
    equity_path, log_path = _paths(args.env, ROOT)
    rows = intervals(load_equity(equity_path), load_run_status(log_path), load_paper(LEDGER, budget), budget)
    if not rows:
        print("No overlapping days yet between the equity record and the paper ledger.")
        return 0

    print(f"{'day':10s} {'hours':>5s} {'paper$':>9s} {'actual$':>9s} {'diff$':>8s} {'bps':>7s}")
    for r in rows:
        tail = "" if r["clean"] else f"   not counted: {r['why']}"
        print(f"{r['day']:10s} {r['hours']:5.1f} {r['paper_usd']:+9.2f} {r['actual_usd']:+9.2f} "
              f"{r['diff_usd']:+8.2f} {r['diff_usd'] / budget * 1e4:+7.1f}{tail}")
    clean = [r for r in rows if r["clean"]]
    log = cumulative(clean, budget)
    write_log(log, OUT)
    if not clean:
        print("\nNo clean interval yet (needs two COMPLETE runs on consecutive days).")
        return 0
    total = log[-1]["cum_diff_usd"]
    per_day_bps = total / len(clean) / budget * 1e4
    print(f"\n{len(clean)} clean day(s) of {len(rows)}. cumulative divergence {total:+.2f}$ = "
          f"{total / budget * 100:+.2f}% of budget, {per_day_bps:+.1f} bps/day "
          f"(~{per_day_bps * 7 / 100:+.2f}%/week if it persisted; stop rule: > 1%/week for 3 weeks)")
    print("note: testnet fills come from a demo book; treat the sign and order of magnitude as real.")
    print(f"-> {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
