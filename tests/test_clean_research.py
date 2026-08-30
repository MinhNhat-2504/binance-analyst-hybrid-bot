from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import json

from clean_research import data as data_module
from clean_research.carry import (
    CarryConfig,
    _max_drawdown,
    carry_daily_components,
    circular_shift_p_value,
    evaluate_carry,
    make_carry_weights,
)
from clean_research.ledger import (
    _decision_id,
    _funding_inside_intervals,
    build_daily_decision_ledger,
)
from clean_research.model import (
    default_model_specs,
    fit_ridge,
    make_prediction_weights,
    walk_forward_folds,
)
from clean_research.schema import FEATURE_SCHEMA_V1, validate_feature_schema
from run_clean_oos import _json_safe


def _daily(symbol_offset: float, n: int = 150) -> pd.DataFrame:
    opens = pd.date_range("2025-01-01", periods=n, freq="1D")
    x = np.arange(n, dtype=float)
    close = 100.0 + symbol_offset + 0.06 * x + np.sin(x / (5.0 + symbol_offset / 10.0))
    open_price = close * (1.0 + 0.001 * np.cos(x / 7.0))
    volume = 1_000.0 + 20.0 * symbol_offset + 5.0 * x
    return pd.DataFrame(
        {
            "Open time": opens,
            "Open": open_price,
            "High": np.maximum(open_price, close) * 1.01,
            "Low": np.minimum(open_price, close) * 0.99,
            "Close": close,
            "Volume": volume,
            "Close time": opens + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1),
            "Quote Asset": volume * close,
            "Taker Buy Base": volume * (0.48 + 0.02 * np.sin(x / 9.0)),
        }
    )


def _funding(n: int = 151, offset: float = 0.0) -> pd.DataFrame:
    days = pd.date_range("2025-01-01", periods=n, freq="1D")
    times = [d + pd.Timedelta(hours=h) for d in days for h in (0, 8, 16)]
    rates = [0.0001 + offset + 0.00001 * np.sin(i / 13.0) for i in range(len(times))]
    return pd.DataFrame({"fundingTime": times, "fundingRate": rates})


def _simple_weighted(n_days: int = 40) -> pd.DataFrame:
    dates = pd.date_range("2025-01-02", periods=n_days, freq="1D")
    rows = []
    for i, date in enumerate(dates):
        for symbol, weight, ret in (("A", 0.5, 0.01), ("B", -0.5, -0.01)):
            rows.append(
                {
                    "decision_time": date - pd.Timedelta(milliseconds=1),
                    "entry_time": date,
                    "exit_time": date + pd.Timedelta(days=1),
                    "symbol": symbol,
                    "price_return": ret + i * 0.00001,
                    "realized_funding": 0.0,
                    "carry_signal": float(i),
                    "funding_observations": 21,
                    "holding_funding_observations": 2,
                    "history_days": 100 + i,
                    "eligible": True,
                    "weight": weight,
                }
            )
    return pd.DataFrame(rows)


def _model_ledger(n_days: int = 260, n_symbols: int = 12) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    for day, entry in enumerate(pd.date_range("2025-01-02", periods=n_days, freq="1D")):
        for symbol_idx in range(n_symbols):
            features = {name: float(rng.normal()) for name in FEATURE_SCHEMA_V1}
            features["xs_funding_7d"] = symbol_idx / (n_symbols - 1) - 0.5
            rows.append(
                {
                    "decision_time": entry - pd.Timedelta(milliseconds=1),
                    "entry_time": entry,
                    "exit_time": entry + pd.Timedelta(days=1),
                    "label_available_at": entry + pd.Timedelta(days=1),
                    "symbol": f"S{symbol_idx:02d}",
                    "price_return": -0.01 * features["xs_funding_7d"] + rng.normal(0, 0.001),
                    "realized_funding": 0.0,
                    **features,
                }
            )
    return pd.DataFrame(rows)


def test_feature_schema_is_exact_ordered_allowlist() -> None:
    assert validate_feature_schema(FEATURE_SCHEMA_V1) == FEATURE_SCHEMA_V1
    with pytest.raises(ValueError, match="canonical"):
        validate_feature_schema(FEATURE_SCHEMA_V1[::-1])
    with pytest.raises(ValueError, match="unknown predictors"):
        validate_feature_schema((*FEATURE_SCHEMA_V1, "Outcome_PnL"))


def test_funding_interval_uses_strict_endpoints() -> None:
    funding = pd.DataFrame(
        {
            "fundingTime": pd.to_datetime(
                ["2025-01-01 00:00", "2025-01-01 08:00", "2025-01-01 16:00", "2025-01-02 00:00"]
            ),
            "fundingRate": [1.0, 2.0, 3.0, 4.0],
        }
    )
    got = _funding_inside_intervals(
        funding,
        pd.Series(pd.to_datetime(["2025-01-01 00:00"])),
        pd.Series(pd.to_datetime(["2025-01-02 00:00"])),
    )
    assert got.tolist() == [5.0]


def test_daily_ledger_is_unique_and_next_open_causal() -> None:
    symbols = {"AAAUSDT": 0.0, "BBBUSDT": 5.0, "CCCUSDT": 11.0}
    bars = {s: _daily(offset) for s, offset in symbols.items()}
    funding = {s: _funding(offset=offset * 1e-7) for s, offset in symbols.items()}
    ledger = build_daily_decision_ledger(
        bars,
        funding,
        horizon_days=1,
        execution_lag_bars=1,
        min_cross_section=3,
    )
    assert not ledger.empty
    assert ledger["decision_id"].is_unique
    assert (ledger["entry_time"] > ledger["decision_time"]).all()
    assert (ledger["exit_time"] - ledger["entry_time"]).eq(pd.Timedelta(days=1)).all()
    assert (ledger["funding_observations_held"] >= 2).all()
    assert np.allclose(
        ledger["ret_long_net"],
        ledger["price_return"] - ledger["realized_funding"] - ledger["transaction_cost"],
    )


def test_daily_ledger_rejects_missing_funding_and_bar_gaps() -> None:
    bars = {s: _daily(i * 5.0) for i, s in enumerate(("A", "B", "C"))}
    no_funding = {s: pd.DataFrame(columns=["fundingTime", "fundingRate"]) for s in bars}
    assert build_daily_decision_ledger(
        bars, no_funding, horizon_days=1, min_cross_section=3
    ).empty

    broken = {s: frame.copy() for s, frame in bars.items()}
    broken["A"] = broken["A"].drop(index=50).reset_index(drop=True)
    with pytest.raises(ValueError, match="daily bars have gaps"):
        build_daily_decision_ledger(
            broken,
            {s: _funding() for s in broken},
            horizon_days=1,
            min_cross_section=3,
        )


def test_decision_id_normalizes_equivalent_utc_instants() -> None:
    base = {"symbol": "btcusdt", "horizon_days": 1, "execution_lag_bars": 1}
    a = _decision_id(pd.Series({**base, "decision_time": pd.Timestamp("2025-01-01T00:00:00Z")}))
    b = _decision_id(
        pd.Series({**base, "symbol": "BTCUSDT", "decision_time": pd.Timestamp("2025-01-01T07:00:00+07:00")})
    )
    assert a == b


def test_timezone_aware_asof_is_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    daily = _daily(0.0, n=150)
    funding = _funding(n=150)
    monkeypatch.setattr(data_module, "fetch_klines", lambda *args, **kwargs: daily.copy())
    monkeypatch.setattr(data_module, "fetch_funding_rates", lambda *args, **kwargs: funding.copy())
    bars, rates = data_module.load_daily_bundle(
        ["BTCUSDT"], as_of="2025-05-11T00:00:00Z", use_cache=False
    )
    assert bars["BTCUSDT"]["Close time"].max() < pd.Timestamp("2025-05-11 00:00:00")
    assert rates["BTCUSDT"]["fundingTime"].max() <= pd.Timestamp("2025-05-11 00:00:00")


def test_carry_config_and_cost_reject_invalid_values() -> None:
    for q in (0.0, -0.1, 0.5, 0.51):
        with pytest.raises(ValueError, match="tail_fraction"):
            CarryConfig(tail_fraction=q)
    with pytest.raises(ValueError, match="negative"):
        evaluate_carry(_simple_weighted(), cost_per_leg_bps=-1.0)


def test_weight_builder_keeps_complete_outcome_matrix() -> None:
    date = pd.Timestamp("2025-02-01")
    panel = pd.DataFrame(
        {
            "decision_time": [date] * 10,
            "entry_time": [date + pd.Timedelta(days=1)] * 10,
            "exit_time": [date + pd.Timedelta(days=2)] * 10,
            "symbol": [f"S{i:02d}" for i in range(10)],
            "price_return": np.linspace(-0.02, 0.02, 10),
            "realized_funding": np.linspace(-0.001, 0.001, 10),
            "carry_signal": np.arange(10, dtype=float),
            "funding_observations": [21] * 10,
            "holding_funding_observations": [2] * 10,
            "history_days": [100] * 10,
        }
    )
    weighted = make_carry_weights(
        panel,
        CarryConfig(min_symbols=10, min_history_days=30, min_funding_observations=18),
    )
    assert len(weighted) == len(panel)
    assert (weighted["weight"] == 0).sum() > 0
    assert weighted["weight"].clip(lower=0).sum() == pytest.approx(0.5)
    assert -weighted["weight"].clip(upper=0).sum() == pytest.approx(0.5)


def test_inactive_days_and_terminal_liquidation_charge_turnover() -> None:
    weighted = _simple_weighted(3)
    weighted.loc[weighted["entry_time"] == pd.Timestamp("2025-01-03"), "weight"] = 0.0
    components, _, _, _ = carry_daily_components(weighted, liquidate_end=True)
    assert components["turnover"].tolist() == pytest.approx([1.0, 1.0, 2.0])


def test_shift_null_enumerates_unique_shifts_only() -> None:
    weighted = _simple_weighted(40)
    components, weights, prices, funding = carry_daily_components(weighted, liquidate_end=True)
    observed_10 = (
        components["price"]
        + components["funding"]
        - components["turnover"] * 10.0 / 10_000.0
    ).mean()
    p, n_null = circular_shift_p_value(
        weights,
        prices,
        funding,
        observed_mean=observed_10,
        cost_per_leg_bps=10.0,
        n_perm=999,
        min_shift=5,
    )
    observed_20 = (
        components["price"]
        + components["funding"]
        - components["turnover"] * 20.0 / 10_000.0
    ).mean()
    p_stress, _ = circular_shift_p_value(
        weights,
        prices,
        funding,
        observed_mean=observed_20,
        cost_per_leg_bps=20.0,
        n_perm=999,
        min_shift=5,
    )
    assert n_null == 31
    assert 0.0 < p <= 1.0
    assert p_stress == p


def test_drawdown_includes_initial_nav() -> None:
    assert _max_drawdown(pd.Series([-0.10, 0.02])) == pytest.approx(-0.10)


def test_walk_forward_training_purges_unavailable_labels() -> None:
    ledger = _model_ledger()
    folds = walk_forward_folds(
        ledger, min_train_days=120, validation_days=50, min_validation_days=30
    )
    assert len(folds) >= 2
    for train, validation, _ in folds:
        assert train["label_available_at"].max() < validation["entry_time"].min()


def test_ridge_training_and_prediction_portfolio_are_complete() -> None:
    ledger = _model_ledger(n_days=200)
    spec = default_model_specs()[0]
    fitted = fit_ridge(ledger.iloc[: 150 * 12], spec)
    validation = ledger.iloc[150 * 12 :].copy()
    predictions = fitted.predict(validation)
    weighted = make_prediction_weights(validation, predictions, min_symbols=10)
    assert len(weighted) == len(validation)
    by_day = weighted.groupby("entry_time")["weight"]
    assert np.allclose(by_day.sum().to_numpy(), 0.0)
    assert np.allclose(by_day.apply(lambda x: x.abs().sum()).to_numpy(), 1.0)


def test_report_payload_is_strict_json() -> None:
    payload = _json_safe({"nan": float("nan"), "inf": np.float64("inf"), "ok": 1.5})
    encoded = json.dumps(payload, allow_nan=False)
    assert json.loads(encoded) == {"nan": None, "inf": None, "ok": 1.5}
