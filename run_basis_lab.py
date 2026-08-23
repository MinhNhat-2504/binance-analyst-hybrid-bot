"""Basis-carry lab: pre-registered grid, each cell against its own null, then hold-out.

    python run_basis_lab.py               # discovery 42 + hold-out 33, 200 perms
    python run_basis_lab.py --quick       # 12 symbols, 50 perms

GRID (fixed before results; lookback stays 7d as in CARRY - not a knob here):
    k  in {5, 10}      names held (equal weight 1/k of notional, long spot / short perp)
    H  in {1, 3, 7}    days between rebalances
  -> 6 cells. Bonferroni bar for any single cell: 0.05/6 ~= 0.0083.

BENCHMARKS a cell must beat, not just its null:
    MARKET-CARRY   equal weight across ALL positive-funding names, rebalanced with H=7.
                   This is "basis carry without selection"; if selection does not beat it,
                   the edge is the asset class, not the rule - still useful, but a
                   different (and cheaper) strategy.
    CARRY-7d       the live paper route, for the "is this worth running alongside" question.

COST: 19bps per unit turnover (spot 10+2, perp 5+2), stress 1.5x. Per-unit-NOTIONAL
returns; ROE = notional / capital_multiplier (1.5 = perp leg at 2x).

Deployment gate (pre-committed): p < 0.0083 vs null AND Sharpe > MARKET-CARRY AND positive
after stress cost AND transfers to the disjoint hold-out with p < 0.05 there.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from honest.basis import (
    COST_PER_UNIT_TURNOVER, basis_weights, build_basis_panel, evaluate_basis,
    permutation_null_sharpe,
)
from run_carry_holdout import HOLDOUT_UNIVERSE
from run_daily_lab import UNIVERSE as DISCOVERY

LOOKBACK = 7
GRID = [(k, h) for k in (5, 10) for h in (1, 3, 7)]


def run_universe(tag: str, symbols: list[str], days: int, n_perm: int, verbose_panel: bool):
    print("\n" + "=" * 84 + f"\nUNIVERSE: {tag}\n" + "=" * 84)
    perp, spot, fday = build_basis_panel(symbols, days, verbose=verbose_panel)
    tradeable = perp.notna() & spot.notna()
    print(f"  panel: {perp.shape[1]} symbols x {perp.shape[0]} days "
          f"[{perp.index.min():%Y-%m-%d} -> {perp.index.max():%Y-%m-%d}]")
    if perp.shape[1] < 8:
        print("  too few symbols with both spot and perp")
        return {}

    out = {"n_symbols": int(perp.shape[1]), "cells": {}, "benchmarks": {}}

    # Benchmark: market carry, no selection (all positive-funding names, equal weight, H=7).
    sig = fday.rolling(LOOKBACK).sum().where(tradeable)
    Wm = pd.DataFrame(0.0, index=fday.index, columns=fday.columns)
    last = None
    for i, day in enumerate(fday.index):
        if i % 7 == 0:
            row = sig.loc[day]; pos = row[(row > 0) & row.notna()].index
            last = pd.Series(0.0, index=fday.columns)
            if len(pos):
                last[pos] = 1.0 / len(pos)
        Wm.loc[day] = last if last is not None else 0.0
    Wm = Wm.shift(1).fillna(0.0)
    bm = evaluate_basis(Wm, perp, spot, fday); bm.pop("daily")
    out["benchmarks"]["MARKET-CARRY-H7"] = bm
    print(f"  {'MARKET-CARRY-H7':16s} sharpe={bm['sharpe']:+5.2f} notional={bm['ann_ret_notional_pct']:+6.1f}% "
          f"ROE={bm['ann_ret_on_capital_pct']:+6.1f}% dd={bm['max_dd_pct']:6.1f}% "
          f"fund={bm['funding_leg_ann_pct']:+5.1f}% cost={bm['cost_drag_ann_pct']:5.1f}%   <- no-selection benchmark")

    for k, h in GRID:
        name = f"BASIS-k{k}-H{h}"
        W = basis_weights(fday, tradeable, LOOKBACK, k, h)
        base = evaluate_basis(W, perp, spot, fday)
        stress = evaluate_basis(W, perp, spot, fday, cost_per_unit_turnover=COST_PER_UNIT_TURNOVER * 1.5)
        nl = permutation_null_sharpe(lambda rng, k=k, h=h: basis_weights(fday, tradeable, LOOKBACK, k, h, rng=rng),
                                     perp, spot, fday, base["sharpe"], n_perm=n_perm)
        daily = base.pop("daily"); stress.pop("daily")
        half = len(daily) // 2
        def sh(s): return float(s.mean() / s.std(ddof=1) * np.sqrt(365)) if s.std(ddof=1) > 0 else float("nan")
        cell = {**base, "stress_sharpe": stress["sharpe"], "stress_ann_notional_pct": stress["ann_ret_notional_pct"],
                "sharpe_h1": sh(daily.iloc[:half]), "sharpe_h2": sh(daily.iloc[half:]),
                "null_mean": nl["mean"], "null_p95": nl["p95"], "p_value": nl["p_value"]}
        out["cells"][name] = cell
        print(f"  {name:16s} sharpe={base['sharpe']:+5.2f} (h1 {cell['sharpe_h1']:+5.2f}|h2 {cell['sharpe_h2']:+5.2f}) "
              f"notional={base['ann_ret_notional_pct']:+6.1f}% ROE={base['ann_ret_on_capital_pct']:+6.1f}% "
              f"dd={base['max_dd_pct']:6.1f}% fund={base['funding_leg_ann_pct']:+5.1f}% price={base['price_leg_ann_pct']:+5.1f}% "
              f"cost={base['cost_drag_ann_pct']:5.1f}% turn={base['avg_daily_turnover']:.2f} "
              f"| stress {stress['sharpe']:+5.2f} | null p95={nl['p95']:+5.2f} p={nl['p_value']:.4f}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=600)
    ap.add_argument("--n-perm", type=int, default=200)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default="basis_lab_report.json")
    args = ap.parse_args()
    disc = DISCOVERY[:12] if args.quick else DISCOVERY
    hold = HOLDOUT_UNIVERSE[:10] if args.quick else HOLDOUT_UNIVERSE
    n_perm = 50 if args.quick else args.n_perm

    print("=" * 84)
    print(f"BASIS CARRY LAB  lookback={LOOKBACK}d  grid={GRID}  perms={n_perm}  "
          f"cost={COST_PER_UNIT_TURNOVER * 1e4:.0f}bps/unit turnover (stress x1.5)")
    print("=" * 84)

    report = {"generated_utc": datetime.now(timezone.utc).isoformat(), "config": vars(args),
              "lookback": LOOKBACK, "grid": GRID, "universes": {}}
    report["universes"]["discovery"] = run_universe("DISCOVERY", disc, args.days, n_perm, verbose_panel=True)
    report["universes"]["holdout"] = run_universe("HOLD-OUT (disjoint)", hold, args.days, n_perm, verbose_panel=False)

    print("\n" + "=" * 84 + "\nVERDICT  (gate: p<0.0083 AND sharpe>MARKET-CARRY AND stress>0 AND holdout p<0.05)\n" + "=" * 84)
    print("  NOTE: cash-and-carry capital could instead sit in USDT earning ~5-8%/yr with no execution risk.")
    print("  A cell whose ROE does not clear that is not a strategy, whatever its p-value.")
    bar = 0.05 / len(GRID)
    d, h = report["universes"].get("discovery", {}), report["universes"].get("holdout", {})
    bm_d = d.get("benchmarks", {}).get("MARKET-CARRY-H7", {}).get("sharpe", float("nan"))
    candidates = []
    for name, c in d.get("cells", {}).items():
        hc = h.get("cells", {}).get(name, {})
        passes = (c["p_value"] < bar and c["sharpe"] > bm_d and c["stress_ann_notional_pct"] > 0
                  and hc.get("sharpe", -9) > 0 and hc.get("p_value", 1) < 0.05)
        tag = "CANDIDATE" if passes else ("lead" if c["p_value"] < 0.05 and c["ann_ret_notional_pct"] > 0 else "no")
        if passes:
            candidates.append(name)
        print(f"  {name:16s} {tag:9s} disc p={c['p_value']:.4f} sharpe {c['sharpe']:+.2f} vs market {bm_d:+.2f} "
              f"| holdout sharpe {hc.get('sharpe', float('nan')):+.2f} p={hc.get('p_value', float('nan')):.4f}")
    print()
    if candidates:
        print(f"  {len(candidates)} cell(s) pass the full gate: {candidates}")
        print("  Next: concentration test + paper-v2 config locked by hash, run alongside CARRY-7d.")
    elif bm_d > 0.5:
        print("  No cell beats the gate, but MARKET-CARRY itself is positive: basis carry may be worth running")
        print("  WITHOUT selection (cheaper, simpler). Evaluate that as its own candidate.")
    else:
        print("  Nothing clears. Basis carry at these costs does not pay in this sample.")

    report["candidates"] = candidates
    Path(args.out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\n  report -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
