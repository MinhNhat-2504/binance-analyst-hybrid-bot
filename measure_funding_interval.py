"""How much of CARRY-7d rides on the symbols that settle funding every 4 hours?

Binance moves a contract from 8-hour to 4-hour funding when its funding is extreme -
i.e. exactly when it is most crowded and most squeeze-prone. Six discovery names are on
4h today (ENA, JTO, ORDI, PYTH, TAO, TIA) and PYTH/TAO/ORDI sit in the short leg most
days of the paper ledger. The paper executor's funding-boundary split assumes three
settlements a day; these have six. Not a bug in the signal, but a KNOWN source of
paper-vs-live divergence that has never been measured. This measures it.

Two parts, two different locks:

  --ledger   (any time)   From carry_paper_ledger.csv: how often each 4h name is in the
                          book, on which side, and what share of book-days they are.
                          Descriptive; reads no prices, evaluates no strategy.
  --backtest (>= 2026-10-01, alongside cell 1 of the pre-registration)
                          CARRY-7d on the cached 600-day panel with the 4h names removed
                          from the universe, versus the full universe: Sharpe, return,
                          funding share, worst day. A robustness check on the running
                          strategy, not a new family; it consumes no research slot but
                          the pre-registration schedules it with cell 1, so it waits.

The 4h list is read live from /fapi/v1/fundingInfo (public, keyless) and cached with the
report, because Binance changes it; the list in the docstring is what was true on
2026-09-25.

    python measure_funding_interval.py --ledger
    python measure_funding_interval.py --backtest        # after 2026-10-01
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from honest.data import _get  # noqa: E402

FUNDING_INFO = "https://fapi.binance.com/fapi/v1/fundingInfo"
LEDGER = ROOT / "carry_paper_ledger.csv"
REPORT = ROOT / "reports" / "funding_interval_report.json"
EARLIEST_BACKTEST = date(2026, 10, 1)


def four_hour_symbols(universe: set[str], fetch=_get) -> dict[str, int]:
    """symbol -> fundingIntervalHours for every universe name whose interval is not 8."""
    rows = fetch(FUNDING_INFO) or []
    return {r["symbol"]: int(r.get("fundingIntervalHours", 8)) for r in rows
            if r.get("symbol") in universe and int(r.get("fundingIntervalHours", 8)) != 8}


def ledger_exposure(ledger_path: Path, fast: dict[str, int]) -> dict:
    rows = list(csv.DictReader(ledger_path.open(encoding="utf-8"))) if ledger_path.exists() else []
    if not rows:
        return {"days": 0, "note": "no ledger"}
    per: dict[str, Counter] = {s: Counter() for s in fast}
    book_slots = 0
    fast_slots = 0
    days_with_fast = 0
    for r in rows:
        longs = [s for s in (r.get("longs") or "").split(",") if s]
        shorts = [s for s in (r.get("shorts") or "").split(",") if s]
        book_slots += len(longs) + len(shorts)
        hit = False
        for s in longs:
            if s in per:
                per[s]["long"] += 1; fast_slots += 1; hit = True
        for s in shorts:
            if s in per:
                per[s]["short"] += 1; fast_slots += 1; hit = True
        days_with_fast += hit
    n = len(rows)
    return {
        "days": n,
        "days_with_any_4h_name_pct": round(100.0 * days_with_fast / n, 1),
        "share_of_book_slots_pct": round(100.0 * fast_slots / book_slots, 1) if book_slots else None,
        "per_symbol": {s: {"interval_h": fast[s], "days_long": c["long"], "days_short": c["short"],
                           "in_book_pct": round(100.0 * (c["long"] + c["short"]) / n, 1)}
                       for s, c in sorted(per.items())},
    }


def backtest_exclusion(symbols: list[str], fast: dict[str, int], days: int = 600) -> dict:
    from honest.daily import _xs_weights, build_panel, evaluate
    out = {}
    for tag, uni in (("full", symbols), ("without_4h", [s for s in symbols if s not in fast])):
        px, fday = build_panel(uni, days, verbose=False)
        W = _xs_weights(fday.rolling(7).sum(), q=0.2, direction=-1)
        r = evaluate(W, px, fday)
        out[tag] = {"n_symbols": int(px.shape[1]), "n_days": r["n_days"], "sharpe": round(r["sharpe"], 3),
                    "ann_ret_pct": round(r["ann_ret_pct"], 2), "max_dd_pct": round(r["max_dd_pct"], 2),
                    "funding_share_pct": round(r["funding_share_pct"], 1) if r["funding_share_pct"] == r["funding_share_pct"] else None,
                    "worst_day_bps": round(float(r["daily"].min() * 1e4), 1),
                    "avg_daily_turnover": round(r["avg_daily_turnover"], 4)}
    out["delta_sharpe_without_4h"] = round(out["without_4h"]["sharpe"] - out["full"]["sharpe"], 3)
    out["delta_ann_ret_pct_without_4h"] = round(out["without_4h"]["ann_ret_pct"] - out["full"]["ann_ret_pct"], 2)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ledger", action="store_true")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--today", default=None, help="tests only")
    args = ap.parse_args(argv)
    if not (args.ledger or args.backtest):
        ap.print_help(); return 0

    from run_carry_holdout import HOLDOUT_UNIVERSE
    from run_daily_lab import UNIVERSE
    universe = set(UNIVERSE) | set(HOLDOUT_UNIVERSE)
    fast = four_hour_symbols(universe)
    report: dict = {"as_of": str(date.today()), "four_hour_symbols": fast,
                    "in_discovery": sorted(s for s in fast if s in UNIVERSE),
                    "in_holdout": sorted(s for s in fast if s in HOLDOUT_UNIVERSE)}
    print(f"4h-funding names in universe: {len(fast)}  discovery {report['in_discovery']}  hold-out {report['in_holdout']}")

    if args.ledger:
        report["ledger"] = ledger_exposure(LEDGER, {s: h for s, h in fast.items() if s in UNIVERSE})
        led = report["ledger"]
        print(f"paper ledger: {led['days']} days; a 4h name in the book on {led.get('days_with_any_4h_name_pct')}% of days; "
              f"{led.get('share_of_book_slots_pct')}% of all book slots")
        for s, v in led.get("per_symbol", {}).items():
            print(f"  {s:12s} {v['interval_h']}h  long {v['days_long']:3d}  short {v['days_short']:3d}  in book {v['in_book_pct']:5.1f}%")

    if args.backtest:
        today = date.fromisoformat(args.today) if args.today else date.today()
        if today < EARLIEST_BACKTEST:
            print(f"REFUSING backtest part: scheduled with cell 1 from {EARLIEST_BACKTEST} (RESEARCH_PREREG_Q4_2026.md).")
            return 3
        report["backtest"] = backtest_exclusion(list(UNIVERSE), fast)
        b = report["backtest"]
        print(f"backtest  full: sharpe {b['full']['sharpe']:+.2f} ann {b['full']['ann_ret_pct']:+.1f}%  |  "
              f"without 4h names: sharpe {b['without_4h']['sharpe']:+.2f} ann {b['without_4h']['ann_ret_pct']:+.1f}%  "
              f"(delta sharpe {b['delta_sharpe_without_4h']:+.2f})")

    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"-> {REPORT.relative_to(ROOT) if REPORT.is_relative_to(ROOT) else REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
