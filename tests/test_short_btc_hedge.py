"""The exploratory hedge must use completed data and bounded gross."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from explore_short_btc_hedge import candidate_weights, paired_block_bootstrap


def test_hedge_is_lagged_beta_neutral_and_gross_one() -> None:
    days = pd.date_range("2026-01-01", periods=10)
    names = ["BTCUSDT", "AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"]
    funding = pd.DataFrame({name: [i + 1.0] * len(days) for i, name in enumerate(names)},
                           index=days)
    beta = pd.DataFrame({name: [1.0 if name == "BTCUSDT" else 2.0] * len(days)
                         for name in names}, index=days)
    weights = candidate_weights(funding, beta, funding.columns)
    assert weights.iloc[:7].eq(0).all().all()
    active = weights.iloc[7:]
    assert active.abs().sum(axis=1).to_numpy() == pytest.approx(np.ones(len(active)))
    assert (active * beta.iloc[7:]).sum(axis=1).to_numpy() == pytest.approx(np.zeros(len(active)))
    # DDD has the highest funding and is shorted; BTC is the hedge.
    assert active["DDDUSDT"].lt(0).all()
    assert active["BTCUSDT"].gt(0).all()


def test_future_funding_change_cannot_rewrite_earlier_weights() -> None:
    days = pd.date_range("2026-01-01", periods=12)
    funding = pd.DataFrame({"BTCUSDT": np.arange(12), "AAAUSDT": np.arange(12) + 1,
                            "BBBUSDT": np.arange(12) + 2, "CCCUSDT": np.arange(12) + 3,
                            "DDDUSDT": np.arange(12) + 4}, index=days, dtype=float)
    beta = pd.DataFrame(1.0, index=days, columns=funding.columns)
    original = candidate_weights(funding, beta, funding.columns)
    changed = funding.copy()
    changed.iloc[-1, -1] = -1000
    after = candidate_weights(changed, beta, funding.columns)
    pd.testing.assert_frame_equal(original, after)


def test_paired_bootstrap_is_reproducible_and_retains_pairing() -> None:
    dates = pd.date_range("2025-01-01", periods=80)
    base = pd.Series(np.linspace(-0.01, 0.01, 80), index=dates)
    candidate = base + 0.001
    result = paired_block_bootstrap(candidate, base)
    assert result == paired_block_bootstrap(candidate, base)
    assert result["observed_delta_sharpe"] > 0
