"""Vol-targeting overlay for CARRY-7d: does scaling gross by realised vol add anything?

This is a PORTFOLIO layer, not a signal change. The rule (rank by 7d funding, 20% tails)
is untouched; only the gross exposure moves:

    scale_t = clip(target_vol / realised_vol_{t-1}, floor, cap)        realised over 20d
    W_t     = scale_t * W_carry_t

Pre-registered grid: target_vol in {10%, 15%} annualised, lookback 20d, floor 0.25x, cap
2.0x. Four numbers per cell, two universes. No other knobs.

THE HONEST COMPARISON. Vol-targeting changes two things at once: average gross (often > 1
because carry vol is low) and the TIMING of gross. A higher Sharpe could come from either.
So every cell is compared against CONSTANT leverage equal to the cell's own mean scale -
same average gross, no timing. The difference is what vol-targeting actually buys.
Rescaling also costs money (every scale change is turnover on every position); costs are
charged exactly as in the lab, so the overlay pays its own way or shows that it does not.

Also reported: maxDD, worst-day, and Sharpe on both halves - the point of vol-targeting is
the tails, not the mean, and a tail improvement that only shows up in one half is noise.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from honest.daily import _xs_weights, build_panel, evaluate
from run_carry_holdout import HOLDOUT_UNIVERSE
from run_daily_lab import UNIVERSE as DISCOVERY

TARGETS = (0.10, 0.15)
LOOKBACK, FLOOR, CAP = 20, 0.25, 2.0


def carry_W(fday: pd.DataFrame) -> pd.DataFrame:
    return _xs_weights(fday.rolling(7).sum(), q=0.2, direction=-1)


def unit_daily(W, px, fday) -> pd.Series:
    """Daily return of the UNSCALED carry book, used only to estimate realised vol."""
    return evaluate(W, px, fday)["daily"].reindex(W.index).fillna(0.0)


def stats(res: dict, daily: pd.Series) -> dict:
    half = len(daily) // 2
    def sh(s): return float(s.mean() / s.std(ddof=1) * np.sqrt(365)) if len(s) > 2 and s.std(ddof=1) > 0 else float("nan")
    return {
        "sharpe": res["sharpe"], "ann_ret_pct": res["ann_ret_pct"], "ann_vol_pct": res["ann_vol_pct"],
        "max_dd_pct": res["max_dd_pct"], "worst_day_bps": float(daily.min() * 1e4),
        "sharpe_h1": sh(daily.iloc[:half]), "sharpe_h2": sh(daily.iloc[half:]),
        "avg_turnover": res["avg_daily_turnover"],
    }


def run_universe(tag, symbols, days):
    print("\n" + "=" * 84 + f"\nUNIVERSE: {tag}\n" + "=" * 84)
    px, fday = build_panel(symbols, days, verbose=False)
    W = carry_W(fday)
    base_res = evaluate(W, px, fday); base_daily = base_res.pop("daily")
    base = stats(base_res, base_daily)
    print(f"  {'CARRY-7d (1x)':22s} sharpe={base['sharpe']:+5.2f} ann={base['ann_ret_pct']:+6.1f}% vol={base['ann_vol_pct']:5.1f}% "
          f"dd={base['max_dd_pct']:6.1f}% worst={base['worst_day_bps']:+7.0f}bps h1/h2={base['sharpe_h1']:+.2f}/{base['sharpe_h2']:+.2f}")

    # realised vol of the unit book, LAGGED one day so scale_t uses information <= t-1
    rv = unit_daily(W, px, fday).rolling(LOOKBACK).std(ddof=1) * np.sqrt(365)
    out = {"base": base, "cells": {}}
    for tv in TARGETS:
        scale = (tv / rv).clip(lower=FLOOR, upper=CAP).shift(1).fillna(1.0)
        Wv = W.mul(scale, axis=0)
        res = evaluate(Wv, px, fday); d = res.pop("daily"); vt = stats(res, d)
        mean_scale = float(scale[W.abs().sum(axis=1) > 0].mean())
        # constant-leverage control: same average gross, no timing
        Wc = W * mean_scale
        resc = evaluate(Wc, px, fday); dc = resc.pop("daily"); cl = stats(resc, dc)
        name = f"VT-{int(tv * 100)}%"
        out["cells"][name] = {"vol_target": vt, "const_leverage_same_gross": cl, "mean_scale": mean_scale,
                              "scale_p5": float(scale.quantile(0.05)), "scale_p95": float(scale.quantile(0.95))}
        print(f"  {name + ' vol-target':22s} sharpe={vt['sharpe']:+5.2f} ann={vt['ann_ret_pct']:+6.1f}% vol={vt['ann_vol_pct']:5.1f}% "
              f"dd={vt['max_dd_pct']:6.1f}% worst={vt['worst_day_bps']:+7.0f}bps h1/h2={vt['sharpe_h1']:+.2f}/{vt['sharpe_h2']:+.2f} "
              f"turn={vt['avg_turnover']:.2f}  [mean scale {mean_scale:.2f}x, p5-p95 {scale.quantile(0.05):.2f}-{scale.quantile(0.95):.2f}]")
        print(f"  {'   vs const ' + f'{mean_scale:.2f}x':22s} sharpe={cl['sharpe']:+5.2f} ann={cl['ann_ret_pct']:+6.1f}% vol={cl['ann_vol_pct']:5.1f}% "
              f"dd={cl['max_dd_pct']:6.1f}% worst={cl['worst_day_bps']:+7.0f}bps h1/h2={cl['sharpe_h1']:+.2f}/{cl['sharpe_h2']:+.2f} "
              f"turn={cl['avg_turnover']:.2f}   <- same average gross, no timing")
        print(f"  {'   timing value':22s} dSharpe={vt['sharpe'] - cl['sharpe']:+.2f}  dMaxDD={vt['max_dd_pct'] - cl['max_dd_pct']:+.1f}pp  "
              f"dWorstDay={vt['worst_day_bps'] - cl['worst_day_bps']:+.0f}bps  dTurnoverCost~{(vt['avg_turnover'] - cl['avg_turnover']) * 0.0010 * 365 * 100:+.2f}%/yr")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=600)
    ap.add_argument("--out", default="carry_voltarget_report.json")
    args = ap.parse_args()
    print("=" * 84 + f"\nCARRY-7d VOL-TARGET OVERLAY  targets={TARGETS} lookback={LOOKBACK}d floor={FLOOR} cap={CAP}\n" + "=" * 84)
    rep = {"generated_utc": datetime.now(timezone.utc).isoformat(), "grid": {"targets": TARGETS, "lookback": LOOKBACK, "floor": FLOOR, "cap": CAP},
           "discovery": run_universe("DISCOVERY (42)", DISCOVERY, args.days),
           "holdout": run_universe("HOLD-OUT (33)", HOLDOUT_UNIVERSE, args.days)}

    print("\n" + "=" * 84 + "\nREADING\n" + "=" * 84)
    print("  An overlay is worth adopting only if, in BOTH universes, timing value is positive on Sharpe")
    print("  AND it cuts maxDD / worst-day, AND the halves agree in sign. Leverage alone is not skill.")
    verdict = {}
    for name in [f"VT-{int(t * 100)}%" for t in TARGETS]:
        ok = True
        for uni in ("discovery", "holdout"):
            c = rep[uni]["cells"][name]; vt, cl = c["vol_target"], c["const_leverage_same_gross"]
            ok &= (vt["sharpe"] > cl["sharpe"]) and (vt["max_dd_pct"] > cl["max_dd_pct"]) \
                  and np.sign(vt["sharpe_h1"] - cl["sharpe_h1"]) == np.sign(vt["sharpe_h2"] - cl["sharpe_h2"]) >= 0
        verdict[name] = "ADOPTABLE (as portfolio layer for paper-v2, NOT for the locked v1)" if ok else "no consistent timing value"
        print(f"  {name}: {verdict[name]}")
    rep["verdict"] = verdict
    Path(args.out).write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    print(f"\n  report -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
