"""Cross-exchange lab. Two pre-registered questions, Binance vs Bybit over ~600 days.
(OKX's public funding history only reaches back ~3 months - reported as a sanity check,
never as a gate.)

  A. UNIVERSALITY. The CARRY-7d rule, byte-for-byte (7d funding sum, 20% tails, 0.5 gross
     per side, daily rebalance, 10bps/leg), fed Bybit funding + Bybit prices. Discovery 42
     and hold-out 33, each vs its own column-permutation null (200), halves reported.
     Gate: Sharpe > 0.8 AND p < 0.05 on BOTH universes on Bybit.

  B. CROSS-EXCHANGE FUNDING SPREAD. Same coin, two venues: short the perp where trailing
     7d funding is higher, long where lower. Price risk nets within the coin; PnL is the
     funding differential + perp-perp basis wobble - 2 perp legs at 7bps each.
     Grid: k in {5,10} coins by |spread|, H in {1,7}. 4 cells, Bonferroni 0.0125.
     Null: joint column permutation of (spread, direction) across coins.
     Benchmark: spread on ALL common coins equal-weight (no selection); and the raw mean
     |spread| itself (the ceiling - if the ceiling is below costs, stop reading).
     Gate: p < 0.0125 AND beats the no-selection benchmark AND positive at 1.5x cost AND
     hold-out (disjoint coins) p < 0.05 AND ROE > USDT yield (~6%).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from honest.crossex import build_venue_panel
from honest.daily import COST_PER_LEG, _xs_weights, build_panel, evaluate, permutation_null_sharpe
from run_carry_holdout import HOLDOUT_UNIVERSE
from run_daily_lab import UNIVERSE as DISCOVERY

LOOKBACK = 7
PERP_LEG_COST = 0.0005 + 0.0002       # 7bps per unit |dW| per leg


def carry_builder(sig):
    return _xs_weights(sig, q=0.2, direction=-1)


def _sh(s):
    return float(s.mean() / s.std(ddof=1) * np.sqrt(365)) if len(s) > 2 and s.std(ddof=1) > 0 else float("nan")


# ---------------------------------------------------------------------------
# A. Universality
# ---------------------------------------------------------------------------

def universality(venue: str, tag: str, symbols: list[str], days: int, n_perm: int) -> dict:
    px, fday = (build_panel(symbols, days, verbose=False) if venue == "binance"
                else build_venue_panel(venue, symbols, days, verbose=False))
    if px.shape[1] < 10:
        print(f"  {venue}:{tag}: only {px.shape[1]} symbols - skipped")
        return {}
    sig = fday.rolling(LOOKBACK).sum()
    W = carry_builder(sig)
    res = evaluate(W, px, fday); d = res.pop("daily")
    nl = permutation_null_sharpe(carry_builder, sig, px, fday, res["sharpe"], n_perm=n_perm)
    half = len(d) // 2
    out = {**res, "n_symbols": int(px.shape[1]), "start": str(px.index.min().date()), "end": str(px.index.max().date()),
           "sharpe_h1": _sh(d.iloc[:half]), "sharpe_h2": _sh(d.iloc[half:]),
           "null_mean": nl["mean"], "null_p95": nl["p95"], "p_value": nl["p_value"]}
    print(f"  {venue:8s} {tag:10s} n={px.shape[1]:2d} [{out['start']}->{out['end']}] sharpe={res['sharpe']:+5.2f} "
          f"(h1 {out['sharpe_h1']:+5.2f}|h2 {out['sharpe_h2']:+5.2f}) ann={res['ann_ret_pct']:+6.1f}% dd={res['max_dd_pct']:6.1f}% "
          f"fund={res['funding_share_pct']:5.0f}% | null p95={nl['p95']:+5.2f} p={nl['p_value']:.4f}")
    return out


# ---------------------------------------------------------------------------
# B. Cross-exchange funding spread
# ---------------------------------------------------------------------------

def align(px_a, f_a, px_b, f_b):
    coins = sorted(set(px_a.columns) & set(px_b.columns))
    days = px_a.index.intersection(px_b.index)
    return (px_a.loc[days, coins], f_a.loc[days, coins], px_b.loc[days, coins], f_b.loc[days, coins])


def spread_weights(spread: pd.DataFrame, k: int, hold: int, rng=None):
    """spread = s7_A - s7_B per coin/day. Returns (W_A, W_B) already lagged, where a
    positive entry is LONG that venue's perp and negative is SHORT. Selected coins get
    1/k on each leg: short the higher-funding venue, long the lower."""
    W_A = pd.DataFrame(0.0, index=spread.index, columns=spread.columns)
    W_B = W_A.copy()
    last_a = last_b = None
    sig = spread.abs()
    direction = np.sign(spread)           # +1 => A pays more => short A, long B
    if rng is not None:                   # joint null: reassign coin j's (|spread|, sign) history to coin k
        perm = rng.permutation(spread.shape[1])
        sig = pd.DataFrame(sig.to_numpy()[:, perm], index=sig.index, columns=sig.columns)
        direction = pd.DataFrame(direction.to_numpy()[:, perm], index=direction.index, columns=direction.columns)
    for i, day in enumerate(spread.index):
        if i % hold == 0:
            row = sig.loc[day].dropna()
            picks = row.sort_values(ascending=False).index[:k]
            last_a = pd.Series(0.0, index=spread.columns); last_b = last_a.copy()
            for c in picks:
                dsign = direction.loc[day, c]
                if dsign == 0 or np.isnan(dsign):
                    continue
                last_a[c] = -dsign / k        # A higher -> short A
                last_b[c] = +dsign / k        # ... long B
        W_A.loc[day] = last_a if last_a is not None else 0.0
        W_B.loc[day] = last_b if last_b is not None else 0.0
    return W_A.shift(1).fillna(0.0), W_B.shift(1).fillna(0.0)


def eval_spread(W_A, W_B, px_a, f_a, px_b, f_b, cost=PERP_LEG_COST, capital_multiplier=1.0):
    ra = px_a.pct_change(fill_method=None); rb = px_b.pct_change(fill_method=None)
    ok = ra.notna() & rb.notna() & px_a.shift(1).notna() & px_b.shift(1).notna()
    W_A = W_A.where(ok, 0.0); W_B = W_B.where(ok, 0.0)
    price = (W_A * ra).sum(axis=1) + (W_B * rb).sum(axis=1)
    fund = (-W_A * f_a.fillna(0.0)).sum(axis=1) + (-W_B * f_b.fillna(0.0)).sum(axis=1)   # long pays, short receives
    turn = (W_A - W_A.shift(1).fillna(0.0)).abs().sum(axis=1) + (W_B - W_B.shift(1).fillna(0.0)).abs().sum(axis=1)
    daily = (price + fund - turn * cost).fillna(0.0)
    live = (W_A.abs().sum(axis=1) + W_B.abs().sum(axis=1)) > 0
    if live.any():
        s = live.idxmax(); daily, price, fund, turn = (x[s:] for x in (daily, price, fund, turn))
    eq = (1 + daily).cumprod(); half = len(daily) // 2
    return {"n_days": int(len(daily)), "sharpe": _sh(daily), "ann_ret_notional_pct": float(daily.mean() * 365 * 100),
            "ann_ret_on_capital_pct": float(daily.mean() * 365 * 100 / capital_multiplier),
            "max_dd_pct": float((eq / eq.cummax() - 1).min() * 100),
            "funding_leg_ann_pct": float(fund.mean() * 365 * 100), "price_leg_ann_pct": float(price.mean() * 365 * 100),
            "cost_drag_ann_pct": float((turn * cost).mean() * 365 * 100), "avg_turnover": float(turn.mean()),
            "sharpe_h1": _sh(daily.iloc[:half]), "sharpe_h2": _sh(daily.iloc[half:]), "daily": daily}


def spread_study(tag, symbols, days, n_perm):
    print("\n" + "-" * 84 + f"\n  B. SPREAD  {tag}\n" + "-" * 84)
    px_bin, f_bin = build_panel(symbols, days, verbose=False)
    px_byb, f_byb = build_venue_panel("bybit", symbols, days, verbose=False)
    px_a, f_a, px_b, f_b = align(px_bin, f_bin, px_byb, f_byb)
    print(f"  common coins={px_a.shape[1]} days={px_a.shape[0]} [{px_a.index.min().date()}->{px_a.index.max().date()}]")
    if px_a.shape[1] < 8:
        return {}
    s_a, s_b = f_a.rolling(LOOKBACK).sum(), f_b.rolling(LOOKBACK).sum()
    spread = s_a - s_b
    ceiling = float(spread.abs().mean().mean() / LOOKBACK * 365 * 100)   # mean |daily funding diff| annualised
    print(f"  raw |funding spread| ceiling ~ {ceiling:.1f}%/yr of notional (before ANY cost); "
          f"2-leg round trip = {4 * PERP_LEG_COST * 1e4:.0f}bps")
    out = {"n_coins": int(px_a.shape[1]), "ceiling_ann_pct": ceiling, "cells": {}, "benchmarks": {}}

    # no-selection benchmark: spread on all coins, H=7
    W_A, W_B = spread_weights(spread, k=spread.shape[1], hold=7)
    bm = eval_spread(W_A, W_B, px_a, f_a, px_b, f_b); bm.pop("daily")
    out["benchmarks"]["ALL-COINS-H7"] = bm
    print(f"  {'ALL-COINS-H7':14s} sharpe={bm['sharpe']:+5.2f} notional={bm['ann_ret_notional_pct']:+6.1f}% dd={bm['max_dd_pct']:6.1f}% "
          f"fund={bm['funding_leg_ann_pct']:+5.1f}% price={bm['price_leg_ann_pct']:+5.1f}% cost={bm['cost_drag_ann_pct']:5.1f}%   <- no selection")

    for k in (5, 10):
        for h in (1, 7):
            name = f"SPREAD-k{k}-H{h}"
            W_A, W_B = spread_weights(spread, k, h)
            base = eval_spread(W_A, W_B, px_a, f_a, px_b, f_b); d = base.pop("daily")
            stress = eval_spread(W_A, W_B, px_a, f_a, px_b, f_b, cost=PERP_LEG_COST * 1.5); stress.pop("daily")
            rng = np.random.default_rng(0); nulls = []
            for _ in range(n_perm):
                WA, WB = spread_weights(spread, k, h, rng=rng)
                r = eval_spread(WA, WB, px_a, f_a, px_b, f_b)
                if np.isfinite(r["sharpe"]):
                    nulls.append(r["sharpe"])
            nulls = np.array(nulls)
            p = float((np.sum(nulls >= base["sharpe"]) + 1) / (len(nulls) + 1))
            cell = {**base, "stress_sharpe": stress["sharpe"], "stress_ann_notional_pct": stress["ann_ret_notional_pct"],
                    "null_mean": float(nulls.mean()), "null_p95": float(np.percentile(nulls, 95)), "p_value": p}
            out["cells"][name] = cell
            print(f"  {name:14s} sharpe={base['sharpe']:+5.2f} (h1 {base['sharpe_h1']:+5.2f}|h2 {base['sharpe_h2']:+5.2f}) "
                  f"notional={base['ann_ret_notional_pct']:+6.1f}% dd={base['max_dd_pct']:6.1f}% fund={base['funding_leg_ann_pct']:+5.1f}% "
                  f"price={base['price_leg_ann_pct']:+5.1f}% cost={base['cost_drag_ann_pct']:5.1f}% turn={base['avg_turnover']:.2f} "
                  f"| stress {stress['sharpe']:+5.2f} | null p95={cell['null_p95']:+5.2f} p={p:.4f}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=600)
    ap.add_argument("--n-perm", type=int, default=200)
    ap.add_argument("--out", default="crossex_lab_report.json")
    args = ap.parse_args()

    print("=" * 84 + "\nCROSS-EXCHANGE LAB  (Binance vs Bybit, ~600d; OKX sanity ~90d)\n" + "=" * 84)
    rep = {"generated_utc": datetime.now(timezone.utc).isoformat(), "config": vars(args), "A": {}, "B": {}}

    print("\n" + "-" * 84 + "\n  A. UNIVERSALITY - CARRY-7d rule unchanged, per venue\n" + "-" * 84)
    for venue in ("binance", "bybit"):
        for tag, syms in (("discovery", DISCOVERY), ("holdout", HOLDOUT_UNIVERSE)):
            rep["A"][f"{venue}:{tag}"] = universality(venue, tag, syms, args.days, args.n_perm)
    # OKX: whatever history exists (~90d). Sanity only.
    rep["A"]["okx:discovery~90d"] = universality("okx", "disc~90d", DISCOVERY, 120, 50) if True else {}

    rep["B"]["discovery"] = spread_study("DISCOVERY", DISCOVERY, args.days, args.n_perm)
    rep["B"]["holdout"] = spread_study("HOLD-OUT (disjoint coins)", HOLDOUT_UNIVERSE, args.days, args.n_perm)

    print("\n" + "=" * 84 + "\nVERDICT\n" + "=" * 84)
    a = rep["A"]
    uni_ok = all(a.get(f"bybit:{t}", {}).get("sharpe", -9) > 0.8 and a.get(f"bybit:{t}", {}).get("p_value", 1) < 0.05
                 for t in ("discovery", "holdout"))
    print(f"  A. CARRY-7d on Bybit: {'UNIVERSAL - edge transfers to a different venue' if uni_ok else 'does NOT transfer cleanly (see numbers)'}")
    bD, bH = rep["B"].get("discovery", {}), rep["B"].get("holdout", {})
    cands = []
    for name, c in bD.get("cells", {}).items():
        hc = bH.get("cells", {}).get(name, {})
        bm = bD["benchmarks"]["ALL-COINS-H7"]["sharpe"]
        ok = (c["p_value"] < 0.0125 and c["sharpe"] > bm and c["stress_ann_notional_pct"] > 0
              and hc.get("sharpe", -9) > 0 and hc.get("p_value", 1) < 0.05 and c["ann_ret_on_capital_pct"] > 6)
        if ok:
            cands.append(name)
        print(f"  B. {name:14s} {'CANDIDATE' if ok else 'no':9s} disc sharpe {c['sharpe']:+.2f} p={c['p_value']:.4f} "
              f"ROE {c['ann_ret_on_capital_pct']:+.1f}% | holdout {hc.get('sharpe', float('nan')):+.2f} p={hc.get('p_value', float('nan')):.4f}")
    print(f"\n  spread candidates: {cands or 'none'}")
    rep["verdict"] = {"universality_bybit": uni_ok, "spread_candidates": cands}
    Path(args.out).write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    print(f"\n  report -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
