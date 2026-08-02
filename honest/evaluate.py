"""Honest out-of-sample evaluation.

Three rules this module enforces, each answering a way the old pipeline fooled itself:

1. The decision threshold is chosen on an inner validation slice carved out of TRAIN, never
   on test. The old code reported PF at a threshold picked by looking at the same data it
   scored, which guarantees a flattering number even from noise.

2. Confidence intervals use the uniqueness-adjusted effective sample size. With ~92% label
   overlap, 4,000 rows carry roughly 350 independent observations; a CI computed on n=4,000
   is ~3.4x too narrow and will call noise significant.

3. Every headline number is compared against a permutation null: the identical pipeline
   retrained on shuffled labels. Any strategy pipeline with enough knobs produces a
   winning-looking backtest on pure noise. The null says how good "lucky" looks here, and
   a result that does not clear it is not a result.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import xgboost as xgb

from .cv import Fold, assert_no_leakage

XGB_PARAMS = dict(
    n_estimators=300,
    max_depth=4,
    learning_rate=0.05,
    subsample=0.8,
    colsample_bytree=0.8,
    min_child_weight=20,
    reg_lambda=2.0,
    objective="binary:logistic",
    eval_metric="logloss",
    tree_method="hist",
    n_jobs=-1,
)


@dataclass
class SideResult:
    side: str
    n_trades: int = 0
    net_bps: float = 0.0
    pf: float = float("nan")
    win_rate: float = float("nan")
    threshold: float = float("nan")
    total_ret: float = 0.0
    n_eff: float = 0.0
    t_stat: float = float("nan")
    ci_lo_bps: float = float("nan")
    ci_hi_bps: float = float("nan")
    per_fold: list = field(default_factory=list)
    trades: pd.DataFrame | None = None


def _profit_factor(r: np.ndarray) -> float:
    gains = r[r > 0].sum()
    losses = -r[r < 0].sum()
    return float(gains / losses) if losses > 0 else float("inf")


def _pick_threshold(proba: np.ndarray, ret: np.ndarray, w: np.ndarray,
                    grid: np.ndarray, min_trades: int) -> float:
    """Choose the threshold maximising weighted total return on the validation slice.

    Returns nan when no threshold clears min_trades, which propagates as "this side did not
    produce a tradeable signal" rather than silently falling back to a permissive default.
    """
    best_thr, best_score = float("nan"), -np.inf
    for thr in grid:
        m = proba >= thr
        if m.sum() < min_trades:
            continue
        score = float((ret[m] * w[m]).sum())
        if score > best_score:
            best_score, best_thr = score, float(thr)
    return best_thr


def _effective_n(w: np.ndarray) -> float:
    """Number of independent observations = sum of average-uniqueness weights.

    NOT Kish ESS (Sum(w)^2 / Sum(w^2)). Kish measures the DISPERSION of weights and is
    scale-invariant: multiply every uniqueness by 0.1 and Kish is unchanged, so it does not
    see overlap at all. For a mean of overlapping labels, Var(mean) ~= sigma^2 / Sum(w),
    where w_i is bar i's average uniqueness (Lopez de Prado). Sum(w) is the same quantity
    run_honest_harness reports as the headline "independent observations", so the CI and the
    headline now agree instead of contradicting each other by ~6x.
    """
    if len(w) == 0:
        return 0.0
    return float(w.sum())


def run_side(df: pd.DataFrame, folds: list[Fold], feat_cols: list[str], side: str,
             weights: np.ndarray, inner_frac: float = 0.25, min_trades: int = 30,
             thr_grid: np.ndarray | None = None, seed: int = 0,
             verbose: bool = True, check_leakage: bool = True) -> SideResult:
    """Walk-forward one side. Returns pooled OOS stats over all folds."""
    target_col = f"target_{side.lower()}"
    ret_col = f"ret_{side.lower()}_net"
    if thr_grid is None:
        thr_grid = np.arange(0.50, 0.91, 0.02)

    X_all = df[feat_cols].to_numpy(np.float32)
    y_all = df[target_col].to_numpy(np.float32)
    r_all = df[ret_col].to_numpy(np.float64)

    valid = np.isfinite(y_all) & np.isfinite(r_all)
    res = SideResult(side=side)
    picked_rows, picked_thrs = [], []

    for fold in folds:
        if check_leakage:
            assert_no_leakage(df, fold)

        tr = fold.train[valid[fold.train]]
        te = fold.test[valid[fold.test]]
        if len(tr) < 500 or len(te) < 20:
            continue

        # Inner validation is the TAIL of train: the threshold is tuned on the most recent
        # resolved data, mirroring what you would actually know at decision time.
        cut = int(len(tr) * (1 - inner_frac))
        fit_idx, val_idx = tr[:cut], tr[cut:]
        if len(fit_idx) < 300 or len(val_idx) < 50:
            continue

        model = xgb.XGBClassifier(**XGB_PARAMS, random_state=seed)
        model.fit(X_all[fit_idx], y_all[fit_idx], sample_weight=weights[fit_idx])

        thr = _pick_threshold(
            model.predict_proba(X_all[val_idx])[:, 1], r_all[val_idx], weights[val_idx],
            thr_grid, max(10, min_trades // 4),
        )
        if not np.isfinite(thr):
            continue

        p_te = model.predict_proba(X_all[te])[:, 1]
        take = p_te >= thr
        n_take = int(take.sum())
        fold_ret = r_all[te][take]

        res.per_fold.append(dict(
            fold=fold.idx, threshold=thr, n_trades=n_take,
            net_bps=float(fold_ret.mean() * 1e4) if n_take else 0.0,
            total=float(fold_ret.sum()) if n_take else 0.0,
            test_start=fold.test_start, test_end=fold.test_end,
        ))
        if n_take:
            picked_rows.append(te[take])
            picked_thrs.append(thr)
        if verbose:
            b = fold_ret.mean() * 1e4 if n_take else 0.0
            print(f"    fold {fold.idx}: thr={thr:.2f} n={n_take:5d} net={b:+7.2f}bps "
                  f"[{fold.test_start:%m-%d}->{fold.test_end:%m-%d}]")

    if not picked_rows:
        return res

    rows = np.concatenate(picked_rows)
    r = r_all[rows]
    w = weights[rows]

    res.n_trades = len(r)
    res.total_ret = float(r.sum())
    res.net_bps = float(r.mean() * 1e4)
    res.pf = _profit_factor(r)
    res.win_rate = float((r > 0).mean())
    res.threshold = float(np.mean(picked_thrs))
    res.n_eff = _effective_n(w)
    res.trades = df.iloc[rows][["Open time", "symbol", ret_col]].assign(side=side)

    # t-stat and CI on the effective, not nominal, sample size.
    sd = r.std(ddof=1)
    if sd > 0 and res.n_eff > 1:
        se = sd / np.sqrt(res.n_eff)
        res.t_stat = float(r.mean() / se)
        res.ci_lo_bps = float((r.mean() - 1.96 * se) * 1e4)
        res.ci_hi_bps = float((r.mean() + 1.96 * se) * 1e4)

    return res


def permutation_null(df: pd.DataFrame, folds: list[Fold], feat_cols: list[str], side: str,
                     weights: np.ndarray, n_perm: int = 20, seed: int = 0,
                     verbose: bool = True, **kw) -> dict:
    """Rerun the whole pipeline on shuffled labels to map out what luck looks like.

    Shuffling is done per symbol in contiguous blocks the length of the label horizon. A
    plain i.i.d. shuffle would destroy the serial correlation of returns and understate the
    null, making the real result look better than it is.
    """
    rng = np.random.default_rng(seed)
    target_col, ret_col = f"target_{side.lower()}", f"ret_{side.lower()}_net"
    block = 12
    null_bps, null_pf = [], []

    for k in range(n_perm):
        shuf = df.copy()
        for sym, g in df.groupby("symbol", sort=False):
            pos = df.index.get_indexer(g.index)
            r = df[ret_col].to_numpy()[pos]
            n_blocks = int(np.ceil(len(r) / block))
            padded = np.concatenate([r, np.full(n_blocks * block - len(r), np.nan)])
            blocks = padded.reshape(n_blocks, block)
            blocks = blocks[rng.permutation(n_blocks)]
            new_r = blocks.reshape(-1)[:len(r)]
            shuf.iloc[pos, shuf.columns.get_loc(ret_col)] = new_r
            shuf.iloc[pos, shuf.columns.get_loc(target_col)] = (new_r > 0).astype(float)

        r_k = run_side(shuf, folds, feat_cols, side, weights, seed=seed + k + 1,
                       verbose=False, check_leakage=False, **kw)
        if r_k.n_trades > 0:
            null_bps.append(r_k.net_bps)
            null_pf.append(r_k.pf)
        if verbose:
            print(f"    null {k + 1}/{n_perm}: {r_k.net_bps:+7.2f}bps  pf={r_k.pf:.3f}"
                  if r_k.n_trades else f"    null {k + 1}/{n_perm}: no trades")

    return dict(
        n=len(null_bps),
        bps=np.array(null_bps),
        pf=np.array(null_pf),
        bps_mean=float(np.mean(null_bps)) if null_bps else float("nan"),
        bps_std=float(np.std(null_bps)) if null_bps else float("nan"),
        bps_p95=float(np.percentile(null_bps, 95)) if null_bps else float("nan"),
    )


def p_value_vs_null(observed: float, null: dict) -> float:
    """One-sided empirical p: how often does noise beat the real result?

    The +1 numerator/denominator is the standard finite-sample correction; it keeps p from
    ever being reported as an impossible 0.0 from a handful of permutations.
    """
    if null["n"] == 0:
        return float("nan")
    return float((np.sum(null["bps"] >= observed) + 1) / (null["n"] + 1))
