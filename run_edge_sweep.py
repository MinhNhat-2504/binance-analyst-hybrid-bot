"""Search the design space for a regime where edge clears cost.

The quick harness run showed the model beating its own permutation null by ~4bps while
paying 13bps to trade. Skill exists; it is just smaller than the toll. That framing has
three exits, and this script tests all of them against the same purged, cost-aware,
null-checked standard:

  --mode cost      Does maker execution (~4bps) rescue it? Taker is a choice, not a law.
  --mode horizon   Does a longer hold grow the move faster than it grows the noise?
                   Cost is fixed per round trip, so a 24h hold amortises it over a move
                   ~sqrt(8)x larger than a 3h hold - IF the signal survives that far out.
  --mode geometry  Does the TP/SL shape matter once the stop is not a hair-trigger?

Each cell reports net bps against its own null. A cell only counts if it clears both.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from honest import (
    build_features, feature_columns, label_universe, load_universe, p_value_vs_null,
    permutation_null, purged_walk_forward, run_side, uniqueness_weights,
)

CORE_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
                "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "NEARUSDT", "ADAUSDT"]

# Binance USDT-M futures, VIP0: 0.02% maker / 0.05% taker.
COST_GRID = {
    "maker_both":  0.0004 + 0.0002,   # 2bps x2 + 2bps slippage - resting both legs
    "maker_entry": 0.0009 + 0.0003,   # maker in, taker out + 3bps
    "taker_both":  0.0010 + 0.0005,   # 5bps x2 + 5bps slippage - what the bot does now
    "current":     0.0013,            # the live bot's own assumption
}

# Which cost assumptions are physically achievable for THIS strategy. The exit is a
# stop-loss, and a stop that must fill is a market (taker) order - you cannot rest it
# passively. So "maker_both" describes a strategy that does not exist: 99.7% of exits in
# the live ledger were stops (SL_OR_TRAIL + TP1_TRAIL_STOP), only 10/4110 were clean
# limit take-profits. A profitable-looking maker_both cell is an accounting fiction, the
# same class of error as the notebook's leakage. Flagging it as EDGE would repeat that
# mistake, so the summary refuses to.
ACHIEVABLE_COST = {"maker_entry", "taker_both", "current"}

HORIZON_GRID = [12, 24, 48, 96, 192]      # 3h, 6h, 12h, 24h, 48h on 15m bars
GEOMETRY_GRID = [(1.0, 2.0), (1.5, 3.0), (2.0, 3.0), (2.0, 4.0), (3.0, 3.0)]


def build_base(symbols, days, mtf, no_cache):
    raw = load_universe(symbols, "15m", days, use_cache=not no_cache, verbose=False)
    print(f"  {len(raw)} symbols fetched")
    feats = {}
    for sym, df in raw.items():
        f = build_features(df, sym, mtf=mtf)
        if len(f) > 200:
            feats[sym] = f
    print(f"  {sum(len(f) for f in feats.values()):,} bars after warm-up")
    return feats


def eval_cell(feats, label_kw, folds_kw, n_perm, seed=0):
    """Label -> purge -> walk-forward -> null, for one parameter cell."""
    df = label_universe(feats, **label_kw)
    df = df.dropna(subset=["ret_long_net", "ret_short_net"]).reset_index(drop=True)
    if len(df) < 5000:
        return None

    w = uniqueness_weights(df)
    try:
        folds = purged_walk_forward(df, **folds_kw)
    except ValueError:
        return None
    if not folds:
        return None

    feat_cols = feature_columns(df)
    out = {"n_bars": len(df), "n_eff": float(w.sum()), "n_folds": len(folds), "sides": {}}

    for side in ["LONG", "SHORT"]:
        r = run_side(df, folds, feat_cols, side, w, seed=seed, verbose=False)
        if r.n_trades == 0:
            out["sides"][side] = {"n_trades": 0}
            continue
        nl = permutation_null(df, folds, feat_cols, side, w, n_perm=n_perm,
                              seed=seed, verbose=False)
        p = p_value_vs_null(r.net_bps, nl) if nl["n"] else float("nan")
        out["sides"][side] = {
            "n_trades": r.n_trades, "net_bps": r.net_bps, "pf": r.pf,
            "win_rate": r.win_rate, "ci_lo_bps": r.ci_lo_bps, "ci_hi_bps": r.ci_hi_bps,
            "t_stat": r.t_stat, "n_eff": r.n_eff, "p_value": p,
            "null_mean_bps": nl.get("bps_mean"),
            "excess_vs_null": r.net_bps - nl["bps_mean"] if nl["n"] else float("nan"),
        }
    return out


def fmt_row(label, side_res):
    if not side_res or side_res.get("n_trades", 0) == 0:
        return f"    {label:22s} no trades"
    s = side_res
    flag = ""
    if s["net_bps"] > 0 and np.isfinite(s["p_value"]) and s["p_value"] < 0.05 and s["ci_lo_bps"] > 0:
        flag = "  <-- EDGE"
    elif s["net_bps"] > 0:
        flag = "  (positive, not significant)"
    return (f"    {label:22s} n={s['n_trades']:5d} net={s['net_bps']:+7.2f}bps "
            f"PF={s['pf']:5.3f} p={s['p_value']:.3f} "
            f"excess={s['excess_vs_null']:+6.2f}bps{flag}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["cost", "horizon", "geometry", "all"], default="all")
    ap.add_argument("--symbols", nargs="+", default=CORE_SYMBOLS)
    ap.add_argument("--days", type=int, default=540)
    ap.add_argument("--folds", type=int, default=6)
    ap.add_argument("--embargo-hours", type=float, default=4.0)
    ap.add_argument("--n-perm", type=int, default=10)
    ap.add_argument("--mtf", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--out", default="edge_sweep_report.json")
    args = ap.parse_args()

    print("=" * 78)
    print(f"EDGE SWEEP  mode={args.mode}  symbols={len(args.symbols)}  days={args.days}  "
          f"perms={args.n_perm}")
    print("=" * 78)
    feats = build_base(args.symbols, args.days, args.mtf, args.no_cache)

    report = {"generated_utc": datetime.now(timezone.utc).isoformat(),
              "config": vars(args), "cells": {}}

    def folds_kw_for(time_limit):
        # Embargo must outlast the label; a 48h hold needs more than a 4h gap.
        return dict(n_folds=args.folds,
                    embargo_hours=max(args.embargo_hours, time_limit * 15 / 60 * 0.5),
                    min_train_days=max(30.0, time_limit * 15 / 60 / 24 * 10))

    if args.mode in ("cost", "all"):
        print("\n" + "=" * 78)
        print("COST SENSITIVITY  (horizon 3h, sl=1.0xATR, tp=2.0xATR)")
        print("  Is the 13bps taker toll the thing standing between skill and profit?")
        print("=" * 78)
        for name, cost in sorted(COST_GRID.items(), key=lambda kv: kv[1]):
            res = eval_cell(feats, dict(sl_mult=1.0, tp_mult=2.0, time_limit=12, cost=cost),
                            folds_kw_for(12), args.n_perm)
            report["cells"][f"cost:{name}"] = res
            print(f"\n  {name} ({cost * 1e4:.0f}bps):")
            if res:
                for side in ["LONG", "SHORT"]:
                    print(fmt_row(side, res["sides"].get(side)))

    if args.mode in ("horizon", "all"):
        print("\n" + "=" * 78)
        print("HORIZON  (achievable cost 12bps = maker entry + taker stop, sl=1.0xATR, tp=2.0xATR)")
        print("  Does a longer hold grow the move faster than it grows the noise?")
        print("  Fixed cost per round trip, so a longer hold amortises it over a ~sqrt(t)")
        print("  larger move - IF the signal survives that far out.")
        print("=" * 78)
        for tl in HORIZON_GRID:
            res = eval_cell(feats, dict(sl_mult=1.0, tp_mult=2.0, time_limit=tl,
                                        cost=COST_GRID["maker_entry"]),
                            folds_kw_for(tl), args.n_perm)
            report["cells"][f"horizon:{tl}"] = res
            print(f"\n  {tl} bars ({tl * 15 / 60:.1f}h):")
            if res:
                for side in ["LONG", "SHORT"]:
                    print(fmt_row(side, res["sides"].get(side)))
            else:
                print("    insufficient data at this horizon")

    if args.mode in ("geometry", "all"):
        print("\n" + "=" * 78)
        print("TP/SL GEOMETRY  (achievable cost 12bps, horizon 6h)")
        print("  The live bot stops at ~0.2xATR. Does a stop wider than the noise help?")
        print("=" * 78)
        for sl, tp in GEOMETRY_GRID:
            res = eval_cell(feats, dict(sl_mult=sl, tp_mult=tp, time_limit=24,
                                        cost=COST_GRID["maker_entry"]),
                            folds_kw_for(24), args.n_perm)
            report["cells"][f"geometry:sl{sl}_tp{tp}"] = res
            print(f"\n  sl={sl}xATR tp={tp}xATR:")
            if res:
                for side in ["LONG", "SHORT"]:
                    print(fmt_row(side, res["sides"].get(side)))

    print("\n" + "=" * 78)
    print("SUMMARY: cells clearing null AND cost")
    print("=" * 78)
    winners, fictional = [], []
    for cell, res in report["cells"].items():
        if not res:
            continue
        # A cost cell named maker_both assumes a passive stop exit, which cannot happen.
        achievable = not (cell.startswith("cost:") and cell.split(":", 1)[1] not in ACHIEVABLE_COST)
        for side, s in res["sides"].items():
            if s.get("n_trades", 0) == 0:
                continue
            if (s["net_bps"] > 0 and np.isfinite(s.get("p_value", np.nan))
                    and s["p_value"] < 0.05 and s["ci_lo_bps"] > 0):
                (winners if achievable else fictional).append((cell, side, s))
    if winners:
        for cell, side, s in sorted(winners, key=lambda x: -x[2]["net_bps"]):
            print(f"  {cell:26s} {side:5s} net={s['net_bps']:+.2f}bps p={s['p_value']:.3f} "
                  f"n={s['n_trades']}")
        print("\n  Treat these as leads, not conclusions: sweeping many cells and keeping")
        print("  the best is itself a way to overfit. Re-run any winner on held-out")
        print("  symbols before believing it.")
    if fictional:
        print("\n  EXCLUDED as physically unachievable (passive stop-loss does not exist):")
        for cell, side, s in fictional:
            print(f"    {cell:24s} {side:5s} net={s['net_bps']:+.2f}bps  <- accounting fiction, not an edge")
    if not winners:
        print("  None. No (cost, horizon, geometry) cell produced edge clearing its null.")
        print("  The features do not carry tradeable information at these horizons.")

    Path(args.out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\n  report -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
