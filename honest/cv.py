"""Purged walk-forward splitting.

The notebook used `RollingTradeWalkForward(train_size=500, test_size=100, purge_bars=24)`
over a row-indexed frame holding 10 interleaved symbols. Two things broke:

  * `purge_bars=24` trimmed 24 ROWS. With 10 symbols interleaved by timestamp, one
    timestamp is ~10 rows, so 24 rows is ~36 minutes of real time. The label horizon is
    12 bars x 15m = 3 HOURS. Roughly 70% of the contaminated region stayed in train.
  * `train_size=500` rows is ~50 bars per symbol, about half a day. The saved model is the
    last fold's, so the deployed .pkl learned from half a day of data.

Here everything is expressed in wall-clock time and purging keys off each label's true
close time, so it is correct no matter how many symbols are interleaved.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Fold:
    idx: int
    train: np.ndarray
    test: np.ndarray
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    n_purged: int

    def __repr__(self) -> str:
        return (f"Fold{self.idx}(train={len(self.train):,} [{self.train_start:%Y-%m-%d}->"
                f"{self.train_end:%Y-%m-%d}], test={len(self.test):,} "
                f"[{self.test_start:%Y-%m-%d}->{self.test_end:%Y-%m-%d}], purged={self.n_purged:,})")


def purged_walk_forward(df: pd.DataFrame, n_folds: int = 8, embargo_hours: float = 4.0,
                        min_train_days: float = 30.0, expanding: bool = True,
                        train_days: float | None = None) -> list[Fold]:
    """Sequential time-ordered folds with purge + embargo.

    A train bar is admitted only if its label had already closed `embargo_hours` before the
    test window opens. That single rule subsumes purging: a bar whose outcome is still
    unresolved when the test period begins cannot inform a model that trades that period.

    embargo_hours additionally absorbs serial correlation that outlives the label itself.
    """
    t = pd.to_datetime(df["Open time"])
    end = pd.to_datetime(df["label_horizon_end"])
    embargo = pd.Timedelta(hours=embargo_hours)

    t_min, t_max = t.min(), t.max()
    usable_start = t_min + pd.Timedelta(days=min_train_days)
    if usable_start >= t_max:
        raise ValueError(
            f"Not enough history: {(t_max - t_min).days}d total but min_train_days={min_train_days}"
        )

    edges = pd.date_range(usable_start, t_max, periods=n_folds + 1)
    folds: list[Fold] = []

    for i in range(n_folds):
        t0, t1 = edges[i], edges[i + 1]
        test_mask = (t >= t0) & (t < t1)

        cutoff = t0 - embargo
        train_mask = end < cutoff
        if not expanding and train_days is not None:
            train_mask &= t >= (cutoff - pd.Timedelta(days=train_days))

        # Bars dropped purely because their label was still open at the cutoff.
        n_purged = int(((t < cutoff) & (end >= cutoff)).sum())

        tr = np.flatnonzero(train_mask.to_numpy())
        te = np.flatnonzero(test_mask.to_numpy())
        if len(tr) == 0 or len(te) == 0:
            continue

        folds.append(Fold(
            idx=i, train=tr, test=te,
            train_start=t.iloc[tr].min(), train_end=t.iloc[tr].max(),
            test_start=t.iloc[te].min(), test_end=t.iloc[te].max(),
            n_purged=n_purged,
        ))

    return folds


def assert_no_leakage(df: pd.DataFrame, fold: Fold, embargo_hours: float = 4.0) -> None:
    """Fail loudly if any train label reaches into the test window.

    This is the check the notebook never had. It is cheap, so it runs on every fold.
    """
    end = pd.to_datetime(df["label_horizon_end"])
    t = pd.to_datetime(df["Open time"])
    latest_train_label_end = end.iloc[fold.train].max()
    first_test_bar = t.iloc[fold.test].min()
    if latest_train_label_end >= first_test_bar:
        raise AssertionError(
            f"LEAK in fold {fold.idx}: a train label closes at {latest_train_label_end} "
            f"but the test window opens at {first_test_bar}"
        )
    gap = (first_test_bar - latest_train_label_end).total_seconds() / 3600
    if gap < embargo_hours - 1e-6:
        raise AssertionError(
            f"EMBARGO VIOLATION in fold {fold.idx}: gap {gap:.2f}h < required {embargo_hours}h"
        )
