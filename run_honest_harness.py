"""Answer one question: is there a tradeable edge, measured honestly?

    python run_honest_harness.py                 # default: 10 symbols, 540d, 8 folds
    python run_honest_harness.py --quick         # 4 symbols, 180d, 4 folds, 5 perms
    python run_honest_harness.py --n-perm 50     # tighter null

Reports, per side, the pooled purged out-of-sample net return after the 13bps round trip,
alongside a permutation null built by rerunning the same pipeline on shuffled labels.
The verdict compares the two: an edge must beat what noise achieves on this very pipeline.
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
    DEFAULT_COST, build_features, feature_columns, label_universe, load_universe,
    p_value_vs_null, permutation_null, purged_walk_forward, run_side, uniqueness_weights,
)

CORE_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
                "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "NEARUSDT", "ADAUSDT"]


def hr(title: str = "") -> None:
    print("\n" + "=" * 78)
    if title:
        print(title)
        print("=" * 78)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=CORE_SYMBOLS)
    ap.add_argument("--days", type=int, default=540)
    ap.add_argument("--folds", type=int, default=8)
    ap.add_argument("--embargo-hours", type=float, default=4.0)
    ap.add_argument("--n-perm", type=int, default=20)
    ap.add_argument("--cost", type=float, default=DEFAULT_COST)
    ap.add_argument("--sl-mult", type=float, default=1.0)
    ap.add_argument("--tp-mult", type=float, default=2.0)
    ap.add_argument("--time-limit", type=int, default=12)
    ap.add_argument("--mtf", action="store_true", help="add 4H/1H context features")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--out", default="honest_harness_report.json")
    args = ap.parse_args()

    if args.quick:
        args.symbols = args.symbols[:4]
        args.days, args.folds, args.n_perm = 180, 4, 5

    hr("HONEST HARNESS")
    print(f"symbols={len(args.symbols)}  days={args.days}  folds={args.folds}  "
          f"embargo={args.embargo_hours}h  perms={args.n_perm}")
    print(f"cost={args.cost * 1e4:.0f}bps round trip  sl={args.sl_mult}xATR  tp={args.tp_mult}xATR  "
          f"horizon={args.time_limit} bars ({args.time_limit * 15 / 60:.1f}h)")

    hr("1. DATA")
    raw = load_universe(args.symbols, "15m", args.days, use_cache=not args.no_cache)
    if not raw:
        print("no data fetched")
        return 1

    hr("2. FEATURES")
    feats = {}
    for sym, df in raw.items():
        f = build_features(df, sym, mtf=args.mtf)
        if len(f) > 200:
            feats[sym] = f
    print(f"  {len(feats)} symbols, {sum(len(f) for f in feats.values()):,} bars after warm-up")

    hr("3. LABELS (triple barrier, net of cost, no future-filter)")
    df = label_universe(feats, sl_mult=args.sl_mult, tp_mult=args.tp_mult,
                        time_limit=args.time_limit, cost=args.cost)
    df = df.dropna(subset=["ret_long_net", "ret_short_net"]).reset_index(drop=True)
    print(f"  {len(df):,} labelled bars  "
          f"[{df['Open time'].min():%Y-%m-%d} -> {df['Open time'].max():%Y-%m-%d}]")
    print(f"  base rate: long={df.target_long.mean():.3f}  short={df.target_short.mean():.3f}")
    print(f"  mean net:  long={df.ret_long_net.mean() * 1e4:+.2f}bps  "
          f"short={df.ret_short_net.mean() * 1e4:+.2f}bps   <- coin-flip baseline")

    hr("4. UNIQUENESS")
    w = uniqueness_weights(df)
    n_eff = w.sum()
    print(f"  {len(df):,} rows carry ~{n_eff:,.0f} independent observations "
          f"(mean uniqueness {w.mean():.3f})")
    print(f"  overlap inflates nominal n by {len(df) / max(n_eff, 1):.1f}x")

    hr("5. PURGED WALK-FORWARD")
    folds = purged_walk_forward(df, n_folds=args.folds, embargo_hours=args.embargo_hours)
    for f in folds:
        print(f"  {f}")
    if not folds:
        print("  no usable folds - widen --days")
        return 1

    feat_cols = feature_columns(df)
    print(f"\n  {len(feat_cols)} features")

    results, nulls = {}, {}
    for side in ["LONG", "SHORT"]:
        hr(f"6. {side}")
        r = run_side(df, folds, feat_cols, side, w)
        results[side] = r
        if r.n_trades == 0:
            print("  no trades taken at any fold threshold")
            continue
        print(f"\n  POOLED OOS: n={r.n_trades:,}  net={r.net_bps:+.2f}bps  PF={r.pf:.3f}  "
              f"win={r.win_rate:.3f}")
        print(f"  95% CI (effective n={r.n_eff:,.0f}): "
              f"[{r.ci_lo_bps:+.2f}, {r.ci_hi_bps:+.2f}]bps   t={r.t_stat:+.2f}")

        print(f"\n  permutation null ({args.n_perm} reruns on shuffled labels):")
        nl = permutation_null(df, folds, feat_cols, side, w, n_perm=args.n_perm)
        nulls[side] = nl
        if nl["n"]:
            p = p_value_vs_null(r.net_bps, nl)
            print(f"\n  null: mean={nl['bps_mean']:+.2f}bps  sd={nl['bps_std']:.2f}  "
                  f"p95={nl['bps_p95']:+.2f}bps")
            print(f"  observed {r.net_bps:+.2f}bps  ->  p = {p:.3f}")

    hr("VERDICT")
    payload = {"generated_utc": datetime.now(timezone.utc).isoformat(), "config": vars(args),
               "n_bars": len(df), "n_eff": float(n_eff), "sides": {}}

    any_edge = False
    for side, r in results.items():
        if r.n_trades == 0:
            print(f"  {side:5s}: NO SIGNAL - no threshold produced trades")
            payload["sides"][side] = {"verdict": "no_signal"}
            continue
        nl = nulls.get(side, {"n": 0})
        p = p_value_vs_null(r.net_bps, nl) if nl["n"] else float("nan")
        beats_null = np.isfinite(p) and p < 0.05
        profitable = r.net_bps > 0
        ci_clears = np.isfinite(r.ci_lo_bps) and r.ci_lo_bps > 0

        if profitable and beats_null and ci_clears:
            verdict, any_edge = "EDGE", True
        elif profitable and (beats_null or ci_clears):
            verdict = "WEAK"
        else:
            verdict = "NO EDGE"

        print(f"  {side:5s}: {verdict:8s} net={r.net_bps:+7.2f}bps  PF={r.pf:.3f}  "
              f"p={p:.3f}  CI_low={r.ci_lo_bps:+.2f}bps  n={r.n_trades:,}")
        payload["sides"][side] = {
            "verdict": verdict, "net_bps": r.net_bps, "pf": r.pf, "win_rate": r.win_rate,
            "n_trades": r.n_trades, "n_eff": r.n_eff, "t_stat": r.t_stat,
            "ci_lo_bps": r.ci_lo_bps, "ci_hi_bps": r.ci_hi_bps, "p_value": p,
            "mean_threshold": r.threshold, "per_fold": [
                {k: (v.isoformat() if isinstance(v, pd.Timestamp) else v) for k, v in f.items()}
                for f in r.per_fold
            ],
            "null_mean_bps": nl.get("bps_mean"), "null_p95_bps": nl.get("bps_p95"),
        }

    print()
    if any_edge:
        print("  At least one side clears the null and the cost floor.")
        print("  Next: confirm stability across folds before any sizing work.")
    else:
        print("  No side clears both the null and the cost floor.")
        print("  Adding gates, models, or thresholds on top of this cannot create edge.")
        print("  The signal itself has to change: new features, horizon, or universe.")

    Path(args.out).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\n  report -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
