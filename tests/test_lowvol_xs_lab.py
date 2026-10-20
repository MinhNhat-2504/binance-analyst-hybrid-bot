"""Causal LOWVOL-XS signals and BTC-neutral portfolio accounting."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from run_lowvol_xs_lab import hac_alpha_t, lowvol_weights, volatility_signals


def test_future_price_and_range_shock_cannot_change_an_earlier_signal() -> None:
    index = pd.date_range("2025-01-01", periods=100)
    px = pd.DataFrame({"A": 100 * np.exp(np.cumsum(np.sin(np.arange(100)) * 0.01))}, index=index)
    high, low = px * 1.01, px * 0.99
    before = volatility_signals(px, high, low)
    changed_px, changed_high = px.copy(), high.copy()
    changed_px.iloc[-1, 0] *= 10
    changed_high.iloc[-1, 0] *= 20
    after = volatility_signals(changed_px, changed_high, low)
    for name in before:
        pd.testing.assert_frame_equal(before[name].iloc[:-1], after[name].iloc[:-1])


def test_beta_hedge_uses_yesterdays_decision_and_offsets_selected_book() -> None:
    index = pd.date_range("2025-01-01", periods=3)
    symbols = ["A", "B", "C", "D", "E", "BTCUSDT"]
    signal = pd.DataFrame([[1, 2, 3, 4, 5, 6], [6, 5, 4, 3, 2, 1], [1, 2, 3, 4, 5, 6]],
                          index=index, columns=symbols, dtype=float)
    beta = pd.DataFrame([1.0, 1.2, 1.4, 1.6, 1.8, 1.0], index=symbols).T
    beta = pd.concat([beta] * 3, ignore_index=True).set_axis(index)
    weights = lowvol_weights(signal, beta, pd.Index(symbols))
    assert weights.iloc[0].abs().sum() == 0
    assert weights.loc[index[1], "A"] > 0
    assert weights.loc[index[1], "BTCUSDT"] < 0
    assert float((weights.loc[index[1]] * beta.loc[index[0]]).sum()) == pytest.approx(0.0)
    assert weights.loc[index[2], "BTCUSDT"] > 0  # yesterday's ranking flipped


def test_hac_alpha_detects_an_added_return_stream() -> None:
    rng = np.random.default_rng(8)
    index = pd.date_range("2025-01-01", periods=300)
    benchmark = pd.Series(rng.normal(0, 0.01, len(index)), index=index)
    strategy = 0.5 * benchmark + pd.Series(rng.normal(0.001, 0.001, len(index)), index=index)
    assert hac_alpha_t(strategy, benchmark) > 2
