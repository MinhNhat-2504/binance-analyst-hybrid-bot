"""Daily strategy lab: the pre-registered grid, every cell against its own null.

    python run_daily_lab.py                # full universe, 600d, 200 perms
    python run_daily_lab.py --quick        # 12 symbols, 50 perms

Grid (fixed BEFORE seeing results - adding cells afterwards voids the p-values):

  XS-MOM   cross-sectional momentum, lookback {7, 30, 90}d, long top / short bottom 20%
  TREND    time-series SMA {50, 200} long-flat
  CARRY    funding carry, trailing {3, 7}d funding, short crowded-longs / long crowded-shorts
  BTC-HOLD benchmark: the beta any strategy must beat risk-adjusted

7 strategy cells -> a Bonferroni-honest bar for any single cell is p < 0.05/7 ~= 0.007.
Cells passing 0.05 but not 0.007 are leads for a held-out confirmation, not conclusions.

Deployment gate (pre-committed): a cell is a live-bot candidate only if
  (1) p < 0.007 against its null,           (3) positive after costs at 10bps/leg,
  (2) Sharpe > BTC-hold's Sharpe,           (4) it survives a hold-out re-run on symbols
                                                fetched fresh at decision time.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from honest.daily import (
    COST_PER_LEG, _xs_weights, btc_hold, build_panel, evaluate,
    permutation_null_sharpe, ts_trend,
)

# Established USDT-M perps; the >=400d history filter prunes anything too young.
UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "AVAXUSDT",
    "LINKUSDT", "NEARUSDT", "ADAUSDT", "LTCUSDT", "BCHUSDT", "DOTUSDT", "ATOMUSDT",
    "UNIUSDT", "FILUSDT", "TRXUSDT", "ETCUSDT", "XLMUSDT", "APTUSDT", "ARBUSDT",
    "OPUSDT", "INJUSDT", "SUIUSDT", "TIAUSDT", "SEIUSDT", "RUNEUSDT", "AAVEUSDT",
    "MKRUSDT", "CRVUSDT", "SANDUSDT", "MANAUSDT", "GALAUSDT", "1000PEPEUSDT",
    "1000SHIBUSDT", "WLDUSDT", "TONUSDT", "ENAUSDT", "JTOUSDT", "PYTHUSDT",
    "TAOUSDT", "ORDIUSDT",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=600)
    ap.add_argument("--n-perm", type=int, default=200)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--out", default="daily_lab_report.json")
    args = ap.parse_args()

    symbols = UNIVERSE[:12] if args.quick else UNIVERSE
    n_perm = 50 if args.quick else args.n_perm

    print("=" * 84)
    print(f"DAILY STRATEGY LAB  symbols<={len(symbols)}  days={args.days}  perms={n_perm}  "
          f"cost={COST_PER_LEG * 1e4:.0f}bps/leg")
    print("=" * 84)

    px, fday = build_panel(symbols, args.days, use_cache=not args.no_cache)
    print(f"\n  panel: {px.shape[1]} symbols x {px.shape[0]} days "
          f"[{px.index.min():%Y-%m-%d} -> {px.index.max():%Y-%m-%d}]")
    if px.shape[1] < 10:
        print("  too few symbols for cross-sectional work")
        return 1

    def xs_momentum_from(sig):
        return _xs_weights(sig, q=0.2, direction=1)

    def carry_from(sig):
        return _xs_weights(sig, q=0.2, direction=-1)

    # (name, weight-builder taking the signal, signal matrix | SMA window for TREND)
    CELLS = [
        ("XS-MOM-7d",  xs_momentum_from, px.pct_change(7, fill_method=None)),
        ("XS-MOM-30d", xs_momentum_from, px.pct_change(30, fill_method=None)),
        ("XS-MOM-90d", xs_momentum_from, px.pct_change(90, fill_method=None)),
        ("TREND-50",   None, 50),
        ("TREND-200",  None, 200),
        ("CARRY-3d",   carry_from, fday.rolling(3).sum()),
        ("CARRY-7d",   carry_from, fday.rolling(7).sum()),
    ]

    rows, payload_cells = [], {}
    for name, builder, sig in CELLS:
        if name.startswith("TREND"):
            W = ts_trend(px, sig)
            # Trend's null: shuffle the above-SMA flags within each day. Same construction
            # via a builder over the boolean signal matrix.
            above = (px > px.rolling(sig).mean()).astype(float)

            def trend_builder(s):
                n_active = s.sum(axis=1).replace(0, np.nan)
                return s.div(n_active, axis=0).shift(1).fillna(0.0)

            builder, sig = trend_builder, above
        else:
            W = builder(sig)

        res = evaluate(W, px, fday)
        nl = permutation_null_sharpe(builder, sig, px, fday, res["sharpe"], n_perm=n_perm)
        res.pop("daily")
        res["null_mean_sharpe"] = nl["mean"]
        res["null_p95_sharpe"] = nl["p95"]
        res["p_value"] = nl["p_value"]
        rows.append((name, res))
        payload_cells[name] = res
        print(f"  {name:12s} sharpe={res['sharpe']:+6.2f} ann={res['ann_ret_pct']:+7.1f}% "
              f"dd={res['max_dd_pct']:6.1f}% turn={res['avg_daily_turnover']:.2f} "
              f"| null p95={nl['p95']:+5.2f} p={nl['p_value']:.4f}")

    bench = evaluate(btc_hold(px), px, fday)
    bench.pop("daily")
    payload_cells["BTC-HOLD"] = bench
    print(f"  {'BTC-HOLD':12s} sharpe={bench['sharpe']:+6.2f} ann={bench['ann_ret_pct']:+7.1f}% "
          f"dd={bench['max_dd_pct']:6.1f}%   <- beta benchmark")

    print("\n" + "=" * 84)
    print("VERDICT  (deployment gate: p<0.007 AND sharpe>BTC AND positive net)")
    print("=" * 84)
    bonferroni = 0.05 / len(rows)
    candidates, leads = [], []
    for name, r in rows:
        if not np.isfinite(r["sharpe"]):
            continue
        passing = r["p_value"] < bonferroni and r["sharpe"] > bench["sharpe"] and r["ann_ret_pct"] > 0
        lead = r["p_value"] < 0.05 and r["ann_ret_pct"] > 0
        tag = "CANDIDATE" if passing else ("lead" if lead else "no")
        (candidates if passing else leads if lead else []).append(name)
        print(f"  {name:12s} {tag:9s} p={r['p_value']:.4f} (bar {bonferroni:.4f}) "
              f"sharpe {r['sharpe']:+.2f} vs BTC {bench['sharpe']:+.2f}")

    if candidates:
        print(f"\n  {len(candidates)} cell(s) pass the full gate: {candidates}")
        print("  Next: hold-out confirmation, then 4+ weeks paper, then small live.")
    elif leads:
        print(f"\n  No cell clears Bonferroni; leads worth a hold-out look: {leads}")
    else:
        print("\n  Nothing beats its null. Daily rule-based edge absent in this universe too.")

    Path(args.out).write_text(json.dumps({
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "config": {**vars(args), "n_symbols": int(px.shape[1]), "cost_per_leg": COST_PER_LEG},
        "bonferroni_bar": bonferroni,
        "cells": payload_cells,
        "candidates": candidates,
        "leads": leads,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\n  report -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
