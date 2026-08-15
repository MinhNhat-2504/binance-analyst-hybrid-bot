from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from clean_research.intraday_carry import build_8h_panel, make_8h_weights
from run_8h_carry_oos import _assert_trailing_data_complete


def test_8h_panel_is_causal_and_weights_are_delta_neutral() -> None:
    opens = pd.date_range("2026-01-01", periods=40, freq="8h")
    bars, funding = {}, {}
    for index in range(11):
        symbol = f"X{index:02d}USDT"
        prices = 100 + index + np.arange(len(opens)) * (0.1 + index / 1000)
        bars[symbol] = pd.DataFrame({
            "Open time": opens,
            "Close time": opens + pd.Timedelta(hours=8) - pd.Timedelta(milliseconds=1),
            "Open": prices,
            "Close": prices + 0.05,
            "Volume": np.full(len(opens), 1_000.0),
            "Quote Asset": prices * 1_000.0,
        })
        funding[symbol] = pd.DataFrame({
            "fundingTime": opens + pd.Timedelta(hours=8),
            "fundingRate": [0.00001 * (index + 1)] * len(opens),
        })
    panel = build_8h_panel(bars, funding)
    assert not panel.empty
    assert (panel["entry_time"] > panel["decision_time"]).all()
    assert (panel["exit_time"] - panel["entry_time"] == pd.Timedelta(hours=8)).all()
    assert (panel["holding_funding_observations"] >= 1).all()
    weighted = make_8h_weights(panel, min_symbols=10)
    active = weighted.groupby("decision_time")["weight"].apply(lambda x: x.abs().sum())
    assert not active[active > 0].empty
    assert np.allclose(active[active > 0], 1.0)
    net = weighted.groupby("decision_time")["weight"].sum()
    assert (net.abs() < 1e-12).all()


def test_8h_zombie_volume_and_stale_funding_cannot_receive_weight() -> None:
    opens = pd.date_range("2026-02-01", periods=32, freq="8h")
    bars, funding = {}, {}
    for index in range(11):
        symbol = f"Z{index:02d}USDT"
        volume = np.full(len(opens), 1_000.0)
        if index == 0:
            volume[-8:] = 0.0
        bars[symbol] = pd.DataFrame({"Open time": opens, "Close time": opens + pd.Timedelta(hours=8) - pd.Timedelta(milliseconds=1), "Open": 100 + index + np.arange(len(opens)), "Close": 101 + index + np.arange(len(opens)), "Volume": volume, "Quote Asset": volume * 100})
        funding_times = opens + pd.Timedelta(hours=8)
        if index == 1:
            funding_times = funding_times[:-8]
        funding[symbol] = pd.DataFrame({"fundingTime": funding_times, "fundingRate": np.full(len(funding_times), 0.0001 * (index + 1))})
    weighted = make_8h_weights(build_8h_panel(bars, funding), min_symbols=10)
    tail = weighted[weighted["decision_time"] == weighted["decision_time"].max()].set_index("symbol")
    assert tail.loc["Z00USDT", "weight"] == 0.0
    assert tail.loc["Z01USDT", "weight"] == 0.0


def test_8h_funding_jitter_is_snapped_and_gapped_symbol_is_dropped() -> None:
    opens = pd.date_range("2026-03-01", periods=30, freq="8h")
    bars, funding = {}, {}
    jitter = pd.to_timedelta([(-1 if i % 2 else 1) for i in range(len(opens))], unit="ms")
    for index in range(11):
        symbol = f"J{index:02d}USDT"
        symbol_opens = opens.delete(12) if index == 10 else opens
        bars[symbol] = pd.DataFrame({"Open time": symbol_opens, "Close time": symbol_opens + pd.Timedelta(hours=8) - pd.Timedelta(milliseconds=1), "Open": np.full(len(symbol_opens), 100 + index), "Close": np.full(len(symbol_opens), 100 + index), "Volume": np.full(len(symbol_opens), 1_000), "Quote Asset": np.full(len(symbol_opens), 100_000)})
        funding[symbol] = pd.DataFrame({"fundingTime": opens + pd.Timedelta(hours=8) + jitter, "fundingRate": np.full(len(opens), 0.0001 * (index + 1))})
    panel = build_8h_panel(bars, funding)
    assert "J10USDT" not in set(panel["symbol"])
    assert (panel["holding_funding_observations"] == 1).all()
    weighted = make_8h_weights(panel, min_symbols=10)
    assert (weighted.groupby("decision_time")["weight"].apply(lambda x: x.abs().sum()) > 0).any()


def test_8h_runner_refuses_silently_stale_funding_tail() -> None:
    now = pd.Timestamp("2026-08-15T12:00:00Z")
    bars = {"A": pd.DataFrame({"Close time": [pd.Timestamp("2026-08-15 07:59:59.999")]})}
    stale = {"A": pd.DataFrame({"fundingTime": [pd.Timestamp("2026-08-01 08:00:00")], "fundingRate": [0.0]})}
    with pytest.raises(RuntimeError, match="silently trim"):
        _assert_trailing_data_complete(bars, stale, now=now)
    fresh = {"A": pd.DataFrame({"fundingTime": [pd.Timestamp("2026-08-15 07:59:59.999")], "fundingRate": [0.0]})}
    payload = _assert_trailing_data_complete(bars, fresh, now=now)
    assert payload["trailing_funding_symbols_stale"] == 0
