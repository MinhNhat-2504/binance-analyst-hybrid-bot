"""Ablation: does NEW INFORMATION (funding + cross-sectional) lift the signal?

The harness closed every other door. Clean features carry ~+4.94bps of SHORT excess vs
null (p=0.19) against a ~12bps achievable cost floor; horizon, geometry, maker execution
and threshold tuning were all measured and none clears cost. The only variable left is the
information itself.

This is a controlled ablation, not a new backtest flavour:

  config A  baseline   - the 131 clean price/volume features
  config B  +new-info  - same rows, same folds, plus funding-rate and cross-sectional ranks

Identical rows and folds by construction, so the ONLY difference is the added columns.
Each config gets its own permutation null. The readout is the change in excess-vs-null:

  excess(B) - excess(A)  >> 0   -> new information helps; pursue harder (more sources,
                                   held-out symbols, then revisit cost floor)
  excess(B) ~= excess(A)        -> positioning/relative data does not rescue this signal
                                   either; the honest conclusion is stop, not tune.
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
    build_features, feature_columns, label_universe, load_universe,
    p_value_vs_null, permutation_null, purged_walk_forward, run_side, uniqueness_weights,
)
from honest.funding import add_cross_sectional, fetch_funding, funding_features, merge_funding

CORE_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
                "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "NEARUSDT", "ADAUSDT"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", default="SHORT", choices=["LONG", "SHORT"])
    ap.add_argument("--symbols", nargs="+", default=CORE_SYMBOLS)
    ap.add_argument("--days", type=int, default=540)
    ap.add_argument("--folds", type=int, default=8)
    ap.add_argument("--n-perm", type=int, default=20)
    ap.add_argument("--out", default="signal_v2_ablation.json")
    args = ap.parse_args()

    print("=" * 78)
    print(f"SIGNAL V2 ABLATION  side={args.side}  symbols={len(args.symbols)}  "
          f"days={args.days}  folds={args.folds}  perms={args.n_perm}")
    print("=" * 78)

    print("\n1. bars + clean features")
    raw = load_universe(args.symbols, "15m", args.days, verbose=False)
    feats = {}
    for sym, d in raw.items():
        f = build_features(d, sym)
        if len(f) > 200:
            feats[sym] = f
    print(f"   {len(feats)} symbols, {sum(len(f) for f in feats.values()):,} bars")

    print("\n2. funding features (settled-only, asof-backward merge)")
    for sym in list(feats):
        fr = fetch_funding(sym, args.days + 60)
        if fr.empty:
            print(f"   WARN no funding for {sym}; dropping symbol to keep configs comparable")
            del feats[sym]
            continue
        feats[sym] = merge_funding(feats[sym], funding_features(fr))
        print(f"   {sym:10s} {len(fr):5,d} settlements")

    print("\n3. labels")
    df = label_universe(feats)
    df = df.dropna(subset=["ret_long_net", "ret_short_net"]).reset_index(drop=True)

    print("4. cross-sectional ranks")
    df = add_cross_sectional(df)

    fr_cols = [c for c in df.columns if c.startswith(("FR_", "XS_"))]
    # Equalise rows across configs: both run on exactly the bars where new info exists.
    df = df.dropna(subset=fr_cols).reset_index(drop=True)
    print(f"   {len(df):,} bars after new-info warm-up "
          f"[{df['Open time'].min():%Y-%m-%d} -> {df['Open time'].max():%Y-%m-%d}]")

    w = uniqueness_weights(df)
    folds = purged_walk_forward(df, n_folds=args.folds, embargo_hours=4.0)
    all_cols = feature_columns(df)
    base_cols = [c for c in all_cols if not c.startswith(("FR_", "XS_"))]
    print(f"   folds={len(folds)}  baseline features={len(base_cols)}  "
          f"+new-info={len(all_cols)} (added {len(all_cols) - len(base_cols)})")

    results = {}
    for name, cols in [("baseline", base_cols), ("new_info", all_cols)]:
        print("\n" + "=" * 78)
        print(f"CONFIG {name}  ({len(cols)} features)")
        print("=" * 78)
        r = run_side(df, folds, cols, args.side, w)
        if r.n_trades == 0:
            print("   no trades")
            results[name] = {"n_trades": 0}
            continue
        print(f"\n   POOLED: n={r.n_trades:,} net={r.net_bps:+.2f}bps PF={r.pf:.3f} "
              f"CI=[{r.ci_lo_bps:+.2f},{r.ci_hi_bps:+.2f}] n_eff={r.n_eff:,.0f}")
        print(f"   null ({args.n_perm} perms):")
        nl = permutation_null(df, folds, cols, args.side, w, n_perm=args.n_perm, verbose=False)
        p = p_value_vs_null(r.net_bps, nl) if nl["n"] else float("nan")
        excess = r.net_bps - nl["bps_mean"] if nl["n"] else float("nan")
        print(f"   null mean={nl['bps_mean']:+.2f}  excess={excess:+.2f}bps  p={p:.3f}")
        results[name] = {
            "n_trades": r.n_trades, "net_bps": r.net_bps, "pf": r.pf,
            "ci_lo_bps": r.ci_lo_bps, "ci_hi_bps": r.ci_hi_bps, "n_eff": r.n_eff,
            "null_mean_bps": nl.get("bps_mean"), "excess_bps": excess, "p_value": p,
            "per_fold": [{k: (v.isoformat() if isinstance(v, pd.Timestamp) else v)
                          for k, v in f.items()} for f in r.per_fold],
        }

    print("\n" + "=" * 78)
    print("ABLATION READOUT")
    print("=" * 78)
    a, b = results.get("baseline", {}), results.get("new_info", {})
    if a.get("n_trades") and b.get("n_trades"):
        d_excess = b["excess_bps"] - a["excess_bps"]
        d_net = b["net_bps"] - a["net_bps"]
        print(f"   excess vs null : {a['excess_bps']:+.2f} -> {b['excess_bps']:+.2f}bps  "
              f"(delta {d_excess:+.2f})")
        print(f"   net            : {a['net_bps']:+.2f} -> {b['net_bps']:+.2f}bps  "
              f"(delta {d_net:+.2f})")
        if b["net_bps"] > 0 and b.get("p_value", 1) < 0.05:
            print("   New information lifts the signal past zero AND its null.")
            print("   Next: verify on held-out symbols before any excitement.")
        elif d_excess > 3:
            print("   New information adds real signal but not yet past the cost floor.")
            print("   Worth pursuing: more sources (OI, liquidations), interactions.")
        else:
            print("   New information does not materially lift the signal.")
            print("   Positioning/relative data does not rescue this feature stack either.")

    payload = {"generated_utc": datetime.now(timezone.utc).isoformat(),
               "config": vars(args), "n_bars": len(df),
               "n_features": {"baseline": len(base_cols), "new_info": len(all_cols)},
               "results": results}
    Path(args.out).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\n   report -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
