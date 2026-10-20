"""Causality and attribution checks for the pre-registered positioning cell."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from audit_smart_source import compare_series
from run_smart_vs_crowd_lab import _shuffle_columns, residualize_on_funding


def test_daily_residual_removes_cross_sectional_funding_rank() -> None:
    days = pd.date_range("2025-01-01", periods=4)
    names = list("ABCDEFGH")
    funding = pd.DataFrame([np.arange(8), np.arange(8)[::-1], np.arange(8), np.arange(8)],
                           index=days, columns=names, dtype=float)
    noise = np.array([4.0, -3.0, 1.0, 5.0, -2.0, 4.0, -1.0, -5.0])
    disagreement = 0.4 * funding + pd.DataFrame([noise] * 4, index=days, columns=names)
    residual = residualize_on_funding(disagreement, funding)
    assert residual.notna().all().all()
    assert residual.std(axis=1).gt(0).all()
    for day in days:
        x = funding.loc[day].rank(pct=True).to_numpy()
        y = residual.loc[day].to_numpy()
        assert float(np.dot(x - x.mean(), y)) == pytest.approx(0.0, abs=1e-12)


def test_column_permutation_preserves_each_signal_history() -> None:
    rng = np.random.default_rng(0)
    signal = pd.DataFrame({"A": [1, 2, 3], "B": [10, 20, 30], "C": [100, 200, 300]})
    shuffled = _shuffle_columns(signal, rng)
    assert shuffled.columns.tolist() == signal.columns.tolist()
    assert sorted(tuple(shuffled[col]) for col in shuffled) == sorted(tuple(signal[col]) for col in signal)


def test_future_metrics_cannot_change_earlier_smoothed_disagreement() -> None:
    days = pd.date_range("2025-01-01", periods=35)
    top = pd.DataFrame({"A": np.linspace(1.1, 2.0, 35)}, index=days)
    crowd = pd.DataFrame({"A": np.linspace(0.8, 1.2, 35)}, index=days)
    before = (np.log(top) - np.log(crowd)).rolling(30, min_periods=30).mean()
    changed = top.copy()
    changed.iloc[-1, 0] *= 5
    after = (np.log(changed) - np.log(crowd)).rolling(30, min_periods=30).mean()
    pd.testing.assert_frame_equal(before.iloc[:-1], after.iloc[:-1])


def test_archive_end_of_day_matches_next_rest_timestamp() -> None:
    archive = pd.Series([1.25, 1.50], index=pd.to_datetime(["2026-10-14", "2026-10-15"]))
    rest = [{"timestamp": int(pd.Timestamp("2026-10-15", tz="UTC").timestamp() * 1000),
             "longShortRatio": "1.25"},
            {"timestamp": int(pd.Timestamp("2026-10-16", tz="UTC").timestamp() * 1000),
             "longShortRatio": "1.50"}]
    assert compare_series(archive, rest, "longShortRatio")["bad"] == 0
    assert compare_series(archive, rest, "longShortRatio")["overlap"] == 2
