"""Causal 8-hour funding-crowding panel; a separately registered research cell."""

from __future__ import annotations

import numpy as np
import pandas as pd


FUNDING_GRID_TOLERANCE = pd.Timedelta(seconds=1)


def _snap_funding_to_8h_grid(values: pd.Series) -> pd.Series:
    """Normalize Binance's common +/- millisecond settlement timestamp jitter."""
    raw = pd.to_datetime(values)
    nearest = raw.dt.round("8h")
    return nearest.where((raw - nearest).abs() <= FUNDING_GRID_TOLERANCE, raw)


def _funding_entry_open_exit_closed(funding: pd.DataFrame, entries: pd.Series, exits: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """Sum/count settlements earned over the position interval ``(entry, exit]``.

    A position opened at 08:00 and closed at 16:00 owns the 16:00 settlement, but not
    the one stamped at entry.  This differs deliberately from daily's strict-open rule.
    """
    if funding.empty:
        return np.zeros(len(entries)), np.zeros(len(entries), dtype=int)
    f = funding.dropna(subset=["fundingTime", "fundingRate"]).sort_values("fundingTime")
    times = pd.to_datetime(f["fundingTime"]).to_numpy("datetime64[ns]")
    rates = pd.to_numeric(f["fundingRate"], errors="coerce").fillna(0).to_numpy(float)
    cumulative = np.concatenate([[0.0], np.cumsum(rates)])
    left = np.searchsorted(times, pd.to_datetime(entries).to_numpy("datetime64[ns]"), side="right")
    right = np.searchsorted(times, pd.to_datetime(exits).to_numpy("datetime64[ns]"), side="right")
    return cumulative[right] - cumulative[left], right - left


def build_8h_panel(
    bars: dict[str, pd.DataFrame], funding: dict[str, pd.DataFrame], *, lookback_days: int = 7,
    entry_lag_bars: int = 1,
) -> pd.DataFrame:
    if entry_lag_bars < 1:
        raise ValueError("entry_lag_bars must be positive")
    frames = []
    for symbol, raw in bars.items():
        d = raw.copy().sort_values("Open time").drop_duplicates("Open time").reset_index(drop=True)
        d["Open time"] = pd.to_datetime(d["Open time"])
        d["Close time"] = pd.to_datetime(d["Close time"])
        for col in ("Open", "Close", "Volume", "Quote Asset"):
            if col not in d:
                d[col] = np.nan
            d[col] = pd.to_numeric(d[col], errors="coerce")
        if not d["Open time"].diff().dropna().eq(pd.Timedelta(hours=8)).all():
            # A single broken/delisted symbol cannot kill all 75-symbol research arms.
            # It is excluded entirely; no row-shift is attempted across its gap.
            continue
        f = funding.get(symbol, pd.DataFrame()).copy()
        if f.empty:
            continue
        f["fundingTime"] = _snap_funding_to_8h_grid(f["fundingTime"])
        f["fundingRate"] = pd.to_numeric(f["fundingRate"], errors="coerce")
        f = f.dropna().sort_values("fundingTime").drop_duplicates("fundingTime").set_index("fundingTime")
        rolling_sum = f["fundingRate"].rolling(f"{lookback_days}D", min_periods=18).sum()
        rolling_count = f["fundingRate"].rolling(f"{lookback_days}D", min_periods=18).count()
        d["decision_time"] = d["Close time"]
        d["entry_time"] = d["Open time"].shift(-entry_lag_bars)
        d["exit_time"] = d["Open time"].shift(-(entry_lag_bars + 1))
        d["price_return"] = d["Open"].shift(-(entry_lag_bars + 1)) / d["Open"].shift(-entry_lag_bars) - 1.0
        d["realized_funding"], d["holding_funding_observations"] = _funding_entry_open_exit_closed(f.reset_index(), d["entry_time"], d["exit_time"])
        d["carry_signal"] = rolling_sum.reindex(d["decision_time"], method="ffill").to_numpy()
        d["funding_observations"] = rolling_count.reindex(d["decision_time"], method="ffill").to_numpy()
        last_settlement = f.index.to_series().reindex(d["decision_time"], method="ffill").to_numpy()
        d["funding_is_fresh"] = (d["decision_time"].to_numpy() - last_settlement) <= np.timedelta64(8, "h")
        # Do not forward-fill a delisted/zero-volume perp into a seemingly cheap carry leg.
        d["tradeable"] = (d["Volume"] > 0) & (d["Quote Asset"] > 0) & d["funding_is_fresh"]
        d["holding_tradeable"] = d["tradeable"].shift(-entry_lag_bars) & d["tradeable"].shift(-(entry_lag_bars + 1))
        d["history_bars"] = np.arange(1, len(d) + 1)
        d["symbol"] = symbol
        frames.append(d[["decision_time", "entry_time", "exit_time", "symbol", "price_return", "realized_funding", "carry_signal", "funding_observations", "holding_funding_observations", "history_bars", "tradeable", "holding_tradeable"]])
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).dropna().sort_values(["decision_time", "symbol"]).reset_index(drop=True)


def make_8h_weights(panel: pd.DataFrame, *, tail_fraction: float = 0.2, min_symbols: int = 10) -> pd.DataFrame:
    rows = []
    for _, group in panel.groupby("decision_time", sort=True):
        group = group.copy()
        group["weight"] = 0.0
        eligible = group[(group["history_bars"] >= 21) & (group["funding_observations"] >= 18) & (group["holding_funding_observations"] >= 1) & group["tradeable"] & group["holding_tradeable"]]
        if len(eligible) >= min_symbols:
            ranks = eligible["carry_signal"].rank(method="average", pct=True)
            longs, shorts = ranks[ranks <= tail_fraction].index, ranks[ranks >= 1 - tail_fraction].index
            if len(longs) and len(shorts) and not set(longs) & set(shorts):
                group.loc[longs, "weight"] = 0.5 / len(longs)
                group.loc[shorts, "weight"] = -0.5 / len(shorts)
        rows.append(group)
    return pd.concat(rows, ignore_index=True).sort_values(["decision_time", "symbol"]).reset_index(drop=True) if rows else panel.copy()
