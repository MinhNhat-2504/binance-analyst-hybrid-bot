"""Build one-row-per-decision, next-open executable research ledgers."""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd

from .schema import FEATURE_SCHEMA_V1, FEATURE_SCHEMA_VERSION, validate_feature_schema


IDENTITY_COLUMNS = (
    "decision_id",
    "decision_time",
    "entry_time",
    "exit_time",
    "label_available_at",
    "symbol",
    "horizon_days",
    "execution_lag_bars",
    "feature_schema",
)

OUTCOME_COLUMNS = (
    "entry_price",
    "exit_price",
    "price_return",
    "realized_funding",
    "funding_observations_held",
    "transaction_cost",
    "ret_long_net",
    "ret_short_net",
)


def _rolling_z(values: pd.Series, window: int, min_periods: int) -> pd.Series:
    mean = values.rolling(window, min_periods=min_periods).mean()
    std = values.rolling(window, min_periods=min_periods).std().replace(0, np.nan)
    return (values - mean) / std


def _funding_inside_intervals(
    funding: pd.DataFrame, entries: pd.Series, exits: pd.Series
) -> np.ndarray:
    """Sum settlements strictly after entry and strictly before exit.

    A position entered at the daily open cannot safely be credited/debited the funding
    settlement stamped at that exact instant.  Likewise it exits before a settlement at
    the same timestamp.  Strict endpoints are conservative and deterministic.
    """

    if funding is None or funding.empty:
        return np.zeros(len(entries), dtype=float)
    f = funding.dropna(subset=["fundingTime", "fundingRate"]).sort_values("fundingTime")
    times = pd.to_datetime(f["fundingTime"]).to_numpy("datetime64[ns]")
    rates = pd.to_numeric(f["fundingRate"], errors="coerce").fillna(0).to_numpy(float)
    cumulative = np.concatenate([[0.0], np.cumsum(rates)])
    en = pd.to_datetime(entries).to_numpy("datetime64[ns]")
    ex = pd.to_datetime(exits).to_numpy("datetime64[ns]")
    left = np.searchsorted(times, en, side="right")
    right = np.searchsorted(times, ex, side="left")
    return cumulative[right] - cumulative[left]


def _funding_count_inside_intervals(
    funding: pd.DataFrame, entries: pd.Series, exits: pd.Series
) -> np.ndarray:
    """Count funding observations under the same strict endpoint convention."""

    if funding is None or funding.empty:
        return np.zeros(len(entries), dtype=int)
    f = funding.dropna(subset=["fundingTime", "fundingRate"]).sort_values("fundingTime")
    times = pd.to_datetime(f["fundingTime"]).to_numpy("datetime64[ns]")
    en = pd.to_datetime(entries).to_numpy("datetime64[ns]")
    ex = pd.to_datetime(exits).to_numpy("datetime64[ns]")
    left = np.searchsorted(times, en, side="right")
    right = np.searchsorted(times, ex, side="left")
    return right - left


def _symbol_frame(
    symbol: str,
    daily: pd.DataFrame,
    funding: pd.DataFrame,
    *,
    horizon_days: int,
    execution_lag_bars: int,
    round_trip_cost_bps: float,
) -> pd.DataFrame:
    d = daily.copy().sort_values("Open time").drop_duplicates("Open time").reset_index(drop=True)
    for col in ["Open", "High", "Low", "Close", "Volume", "Taker Buy Base", "Quote Asset"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d["Open time"] = pd.to_datetime(d["Open time"])
    d["Close time"] = pd.to_datetime(d["Close time"])
    cadence = d["Open time"].diff().dropna()
    if not cadence.eq(pd.Timedelta(days=1)).all():
        raise ValueError(f"{symbol}: daily bars have gaps; row shifts are unsafe")

    log_ret = np.log(d["Close"]).diff()
    d["ret_1d"] = d["Close"].pct_change(1)
    for days in [3, 7, 14, 30]:
        d[f"ret_{days}d"] = d["Close"].pct_change(days)
    d["vol_7d"] = log_ret.rolling(7, min_periods=5).std()
    d["vol_30d"] = log_ret.rolling(30, min_periods=20).std()
    d["range_1d"] = (d["High"] - d["Low"]) / d["Close"].replace(0, np.nan)
    d["range_5d"] = d["range_1d"].rolling(5, min_periods=3).mean()

    log_qv = np.log1p(d["Quote Asset"].clip(lower=0))
    d["volume_z_30d"] = _rolling_z(log_qv, 30, 20)
    d["log_quote_volume_30d"] = log_qv.rolling(30, min_periods=20).mean()
    taker_ratio = d["Taker Buy Base"] / d["Volume"].replace(0, np.nan)
    d["taker_imbalance_1d"] = 2.0 * taker_ratio - 1.0
    d["taker_imbalance_3d"] = d["taker_imbalance_1d"].rolling(3, min_periods=3).mean()

    f = funding.copy() if funding is not None else pd.DataFrame(columns=["fundingTime", "fundingRate"])
    if not f.empty:
        f["fundingTime"] = pd.to_datetime(f["fundingTime"])
        f["fundingRate"] = pd.to_numeric(f["fundingRate"], errors="coerce")
        f = f.dropna().sort_values("fundingTime")
        f["date"] = f["fundingTime"].dt.normalize()
        fd = f.groupby("date")["fundingRate"].agg(["last", "sum", "count"]).rename(
            columns={"last": "funding_last", "sum": "funding_sum_1d"}
        )
        calendar = pd.DatetimeIndex(d["Open time"].dt.normalize())
        fd = fd.reindex(calendar)
        fd["funding_mean_3d"] = fd["funding_sum_1d"].rolling(3, min_periods=3).mean()
        fd["funding_sum_7d"] = fd["funding_sum_1d"].rolling(7, min_periods=7).sum()
        fd["funding_z_30"] = _rolling_z(fd["funding_sum_1d"], 30, 20)
        fd = fd.drop(columns=["count"]).reset_index(drop=True)
        for col in [
            "funding_last",
            "funding_sum_1d",
            "funding_mean_3d",
            "funding_sum_7d",
            "funding_z_30",
        ]:
            d[col] = fd[col].to_numpy()
    else:
        for col in [
            "funding_last",
            "funding_sum_1d",
            "funding_mean_3d",
            "funding_sum_7d",
            "funding_z_30",
        ]:
            d[col] = np.nan

    # The decision is made only after the feature day's close.  The earliest executable
    # fill is the following daily open; a one-day holding exits at the next open after it.
    d["decision_time"] = d["Close time"]
    d["entry_time"] = d["Open time"].shift(-execution_lag_bars)
    d["exit_time"] = d["Open time"].shift(-(execution_lag_bars + horizon_days))
    d["label_available_at"] = d["exit_time"]
    d["entry_price"] = d["Open"].shift(-execution_lag_bars)
    d["exit_price"] = d["Open"].shift(-(execution_lag_bars + horizon_days))
    d["price_return"] = d["exit_price"] / d["entry_price"] - 1.0
    d["realized_funding"] = _funding_inside_intervals(f, d["entry_time"], d["exit_time"])
    d["funding_observations_held"] = _funding_count_inside_intervals(
        f, d["entry_time"], d["exit_time"]
    )
    d["transaction_cost"] = float(round_trip_cost_bps) / 10_000.0
    d["ret_long_net"] = d["price_return"] - d["realized_funding"] - d["transaction_cost"]
    d["ret_short_net"] = -d["price_return"] + d["realized_funding"] - d["transaction_cost"]
    d["symbol"] = symbol
    d["horizon_days"] = int(horizon_days)
    d["execution_lag_bars"] = int(execution_lag_bars)
    d["feature_schema"] = FEATURE_SCHEMA_VERSION
    return d


def _add_panel_features(panel: pd.DataFrame) -> pd.DataFrame:
    out = panel.sort_values(["decision_time", "symbol"]).reset_index(drop=True)
    by_time = out.groupby("decision_time", sort=False)
    rank_map = {
        "ret_1d": "xs_ret_1d",
        "ret_3d": "xs_ret_3d",
        "ret_7d": "xs_ret_7d",
        "ret_14d": "xs_ret_14d",
        "vol_30d": "xs_vol_30d",
        "funding_sum_1d": "xs_funding",
        "funding_sum_7d": "xs_funding_7d",
        "log_quote_volume_30d": "xs_liquidity",
        "taker_imbalance_3d": "xs_taker_imbalance",
    }
    for source, target in rank_map.items():
        out[target] = by_time[source].rank(method="average", pct=True) - 0.5

    out["market_ret_1d"] = by_time["ret_1d"].transform("median")
    out["market_ret_3d"] = by_time["ret_3d"].transform("median")
    out["market_breadth_1d"] = by_time["ret_1d"].transform(lambda x: (x > 0).mean())
    out["market_dispersion_1d"] = by_time["ret_1d"].transform(
        lambda x: x.quantile(0.75) - x.quantile(0.25)
    )

    out["beta_60d"] = np.nan
    for _, group in out.groupby("symbol", sort=False):
        idx = group.index
        cov = group["ret_1d"].rolling(60, min_periods=30).cov(group["market_ret_1d"])
        var = group["market_ret_1d"].rolling(60, min_periods=30).var().replace(0, np.nan)
        out.loc[idx, "beta_60d"] = (cov / var).shift(1).to_numpy()
    out["resid_ret_3d"] = out["ret_3d"] - out["beta_60d"] * out["market_ret_3d"]
    out["xs_resid_ret_3d"] = out.groupby("decision_time", sort=False)["resid_ret_3d"].rank(
        method="average", pct=True
    ) - 0.5
    return out


def _decision_id(row: pd.Series) -> str:
    instant = pd.Timestamp(row.decision_time)
    if instant.tzinfo is None:
        instant = instant.tz_localize("UTC")
    else:
        instant = instant.tz_convert("UTC")
    raw = (
        f"{instant.isoformat()}|{str(row.symbol).upper()}|{int(row.horizon_days)}|"
        f"{int(row.execution_lag_bars)}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def build_daily_decision_ledger(
    bars: dict[str, pd.DataFrame],
    funding: dict[str, pd.DataFrame],
    *,
    horizon_days: int,
    execution_lag_bars: int = 1,
    round_trip_cost_bps: float = 15.0,
    min_cross_section: int = 10,
) -> pd.DataFrame:
    """Create a causal supervised ledger from daily bars and settled funding.

    The returned frame contains both predictors and outcomes for research storage, but
    callers must obtain model inputs through ``FEATURE_SCHEMA_V1``.  Every label is unique
    by decision timestamp/symbol/horizon and becomes available no earlier than exit_time.
    """

    if horizon_days < 1:
        raise ValueError("horizon_days must be positive")
    if execution_lag_bars < 1:
        raise ValueError("execution_lag_bars must be positive")
    if round_trip_cost_bps < 0:
        raise ValueError("round_trip_cost_bps cannot be negative")
    frames = [
        _symbol_frame(
            symbol,
            daily,
            funding.get(symbol, pd.DataFrame()),
            horizon_days=horizon_days,
            execution_lag_bars=execution_lag_bars,
            round_trip_cost_bps=round_trip_cost_bps,
        )
        for symbol, daily in bars.items()
        if len(daily) >= 120
    ]
    if not frames:
        return pd.DataFrame()
    out = _add_panel_features(pd.concat(frames, ignore_index=True))
    validate_feature_schema(FEATURE_SCHEMA_V1)
    required = list(FEATURE_SCHEMA_V1) + list(OUTCOME_COLUMNS) + [
        "decision_time",
        "entry_time",
        "exit_time",
        "label_available_at",
    ]
    out = out.dropna(subset=required).copy()
    out = out[out["funding_observations_held"] >= 2 * horizon_days].copy()
    counts = out.groupby("decision_time")["symbol"].transform("nunique")
    out = out[counts >= min_cross_section].copy()
    ordered = list(IDENTITY_COLUMNS) + list(FEATURE_SCHEMA_V1) + list(OUTCOME_COLUMNS)
    if out.empty:
        return pd.DataFrame(columns=ordered)
    out["decision_id"] = out.apply(_decision_id, axis=1)
    if out["decision_id"].duplicated().any():
        dupes = out.loc[out["decision_id"].duplicated(), "decision_id"].head().tolist()
        raise ValueError(f"duplicate decision events: {dupes}")
    if not (out["entry_time"] > out["decision_time"]).all():
        raise AssertionError("entry must be strictly after the feature decision time")
    if not (out["label_available_at"] >= out["exit_time"]).all():
        raise AssertionError("label availability precedes the exit")
    return out[ordered].sort_values(["decision_time", "symbol"]).reset_index(drop=True)
