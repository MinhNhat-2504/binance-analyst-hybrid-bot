"""Does maker execution actually rescue the SHORT signal, or does adverse selection eat it?

The full harness found SHORT at -3.22bps net with a permutation null of -12.89bps: roughly
9.7bps of real selection skill, losing to a 13bps taker toll. Maker fees would cut that toll
to ~6bps, which on paper turns -3.22 into +3.78.

That arithmetic silently assumes every resting order fills. This script drops the assumption
and simulates fills against actual next-bar ranges, using the same purged walk-forward models
so the signals are genuinely out-of-sample.

The number to read is `net_bps_all_signals`, not `net_bps_filled`. Missed trades are not
free: they are the trades that ran away from you, and excluding them is precisely how a
maker backtest fabricates an edge that evaporates live.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

from honest import (
    build_features, feature_columns, label_universe, load_universe, purged_walk_forward,
    uniqueness_weights,
)
from honest.evaluate import XGB_PARAMS, _pick_threshold
from honest.execution import compare_execution

CORE_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
                "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "NEARUSDT", "ADAUSDT"]


def collect_oos_signals(df, folds, feat_cols, side, weights, seed=0, inner_frac=0.25):
    """Walk forward and return the OOS rows the model would actually have traded."""
    target_col, ret_col = f"target_{side.lower()}", f"ret_{side.lower()}_net"
    X = df[feat_cols].to_numpy(np.float32)
    y = df[target_col].to_numpy(np.float32)
    r = df[ret_col].to_numpy(np.float64)
    valid = np.isfinite(y) & np.isfinite(r)
    grid = np.arange(0.50, 0.91, 0.02)

    picked = []
    for fold in folds:
        tr = fold.train[valid[fold.train]]
        te = fold.test[valid[fold.test]]
        if len(tr) < 500 or len(te) < 20:
            continue
        cut = int(len(tr) * (1 - inner_frac))
        fit_idx, val_idx = tr[:cut], tr[cut:]
        if len(fit_idx) < 300 or len(val_idx) < 50:
            continue

        model = xgb.XGBClassifier(**XGB_PARAMS, random_state=seed)
        model.fit(X[fit_idx], y[fit_idx], sample_weight=weights[fit_idx])
        thr = _pick_threshold(model.predict_proba(X[val_idx])[:, 1], r[val_idx],
                              weights[val_idx], grid, 10)
        if not np.isfinite(thr):
            continue
        take = model.predict_proba(X[te])[:, 1] >= thr
        if take.any():
            picked.append(te[take])
            print(f"    fold {fold.idx}: thr={thr:.2f} signals={int(take.sum())}")

    return np.concatenate(picked) if picked else np.array([], dtype=int)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", default="SHORT", choices=["LONG", "SHORT"])
    ap.add_argument("--symbols", nargs="+", default=CORE_SYMBOLS)
    ap.add_argument("--days", type=int, default=540)
    ap.add_argument("--folds", type=int, default=8)
    ap.add_argument("--offset-bps", type=float, default=1.0)
    ap.add_argument("--time-limit", type=int, default=12)
    ap.add_argument("--out", default="execution_test_report.json")
    args = ap.parse_args()

    print("=" * 78)
    print(f"EXECUTION TEST  side={args.side}  symbols={len(args.symbols)}  days={args.days}")
    print("=" * 78)

    raw = load_universe(args.symbols, "15m", args.days, verbose=False)
    feats = {s: f for s, f in ((s, build_features(d, s)) for s, d in raw.items()) if len(f) > 200}
    print(f"  {len(feats)} symbols, {sum(len(f) for f in feats.values()):,} bars")

    df = label_universe(feats, sl_mult=1.0, tp_mult=2.0, time_limit=args.time_limit)
    df = df.dropna(subset=["ret_long_net", "ret_short_net"]).reset_index(drop=True)
    w = uniqueness_weights(df)
    folds = purged_walk_forward(df, n_folds=args.folds, embargo_hours=4.0)
    feat_cols = feature_columns(df)
    print(f"  {len(df):,} bars, {len(folds)} folds, {len(feat_cols)} features")

    print(f"\n  collecting out-of-sample {args.side} signals:")
    rows = collect_oos_signals(df, folds, feat_cols, args.side, w)
    if len(rows) == 0:
        print("  no signals - nothing to execute")
        return 1
    print(f"  {len(rows):,} OOS signals total")

    # Map pooled rows back to per-symbol positions, since fills are simulated per symbol.
    sig_df = df.iloc[rows][["symbol", "Open time"]]
    signals = {}
    for sym, g in sig_df.groupby("symbol", sort=False):
        f = feats[sym].reset_index(drop=True)
        pos = f.index[f["Open time"].isin(set(g["Open time"]))].to_numpy()
        if len(pos):
            signals[sym] = pos

    print(f"  mapped across {len(signals)} symbols\n")
    print("=" * 78)
    print("EXECUTION COMPARISON")
    print(f"  maker rests {args.offset_bps}bp passive; exits always taker (a stop must fill)")
    print("=" * 78)

    table = compare_execution(feats, signals, args.side, wait_grid=(1, 2, 4, 8),
                              offset_bps=args.offset_bps, time_limit=args.time_limit)
    if table.empty:
        print("  no fills simulated")
        return 1

    pd.set_option("display.width", 200)
    print()
    print(table.to_string(index=False, float_format=lambda x: f"{x:.3f}"))

    print("\n" + "=" * 78)
    print("READING")
    print("=" * 78)
    tk = table[table["mode"] == "taker"]
    mk = table[table["mode"] == "maker"]
    if not tk.empty and not mk.empty:
        t_all = tk.iloc[0]["net_bps_all_signals"]
        best = mk.loc[mk["net_bps_all_signals"].idxmax()]
        print(f"  taker over all signals      : {t_all:+.2f}bps")
        print(f"  best maker (wait={int(best['wait_bars'])} bars)   : "
              f"{best['net_bps_all_signals']:+.2f}bps over all signals "
              f"({best['net_bps_filled']:+.2f}bps on the {best['fill_rate'] * 100:.0f}% that filled)")
        print(f"  adverse selection           : {best['adverse_selection_bps']:+.2f}bps")
        print("    (positive = the trades you MISSED were better than the ones you got)")
        delta = best["net_bps_all_signals"] - t_all
        print(f"\n  maker - taker               : {delta:+.2f}bps")
        if delta > 0 and best["net_bps_all_signals"] > 0:
            print("  Maker execution both helps AND clears zero. Worth a paper trial.")
        elif delta > 0:
            print("  Maker helps but still loses. The fee saving is real; it is not enough.")
        else:
            print("  Maker does not help: adverse selection exceeds the fee saving.")
            print("  The 13bps->6bps arithmetic was an illusion; fills are not free.")

    payload = {"generated_utc": datetime.now(timezone.utc).isoformat(), "config": vars(args),
               "n_signals": int(len(rows)), "table": table.to_dict("records")}
    Path(args.out).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\n  report -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
