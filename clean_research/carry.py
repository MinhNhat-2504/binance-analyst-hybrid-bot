"""Cross-sectional funding-crowding route with next-open portfolio accounting."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from .ledger import _funding_count_inside_intervals, _funding_inside_intervals


@dataclass(frozen=True)
class CarryConfig:
    lookback_days: int = 7
    tail_fraction: float = 0.20
    cost_per_leg_bps: float = 10.0
    min_history_days: int = 30
    min_funding_observations: int = 18
    min_holding_funding_observations: int = 2
    min_symbols: int = 10
    execution_lag_bars: int = 1

    def __post_init__(self) -> None:
        if self.lookback_days < 1:
            raise ValueError("lookback_days must be positive")
        if not 0.0 < self.tail_fraction < 0.5:
            raise ValueError("tail_fraction must be in (0, 0.5)")
        if self.cost_per_leg_bps < 0:
            raise ValueError("cost_per_leg_bps cannot be negative")
        if self.min_history_days < self.lookback_days:
            raise ValueError("min_history_days cannot be shorter than lookback_days")
        if self.min_funding_observations < 1:
            raise ValueError("min_funding_observations must be positive")
        if self.min_holding_funding_observations < 1:
            raise ValueError("min_holding_funding_observations must be positive")
        if self.min_symbols < 2:
            raise ValueError("min_symbols must be at least two")
        if self.execution_lag_bars < 1:
            raise ValueError("execution_lag_bars must be positive")


@dataclass
class PortfolioMetrics:
    n_days: int
    mean_bps_day: float
    total_return: float
    profit_factor: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    hac_ci_lo_bps: float
    hac_ci_hi_bps: float
    hac_t_stat: float
    # Association null: does the frozen weight path align with outcomes better
    # than circular time shifts?  Costs are held fixed by design, so this p-value
    # is not expected to change when a constant cost assumption changes.
    permutation_p: float
    permutation_n: int
    # Net-profit null: is the mean return after the stated costs above zero under
    # a dependence-preserving circular block bootstrap?
    net_profit_block_p: float
    net_profit_block_n: int
    price_ann_arithmetic: float
    funding_ann_arithmetic: float
    cost_ann_arithmetic: float
    average_gross: float
    average_turnover: float


def _daily_funding_features(
    funding: pd.DataFrame, dates: pd.Series, lookback_days: int
) -> tuple[np.ndarray, np.ndarray]:
    if funding is None or funding.empty:
        return np.zeros(len(dates)), np.zeros(len(dates))
    f = funding.copy()
    f["fundingTime"] = pd.to_datetime(f["fundingTime"])
    f["fundingRate"] = pd.to_numeric(f["fundingRate"], errors="coerce")
    f = f.dropna().sort_values("fundingTime")
    f["date"] = f["fundingTime"].dt.normalize()
    daily = f.groupby("date")["fundingRate"].agg(["sum", "count"])
    calendar = pd.DatetimeIndex(pd.to_datetime(dates).dt.normalize())
    daily = daily.reindex(calendar, fill_value=0.0)
    signal = daily["sum"].rolling(lookback_days, min_periods=lookback_days).sum()
    observations = daily["count"].rolling(lookback_days, min_periods=lookback_days).sum()
    return signal.to_numpy(float), observations.to_numpy(float)


def build_carry_panel(
    bars: dict[str, pd.DataFrame],
    funding: dict[str, pd.DataFrame],
    config: CarryConfig,
) -> pd.DataFrame:
    """Build daily price/funding outcomes and a causal seven-day carry score."""

    frames: list[pd.DataFrame] = []
    for symbol, raw in bars.items():
        d = raw.copy().sort_values("Open time").drop_duplicates("Open time").reset_index(drop=True)
        if len(d) < config.min_history_days + 3:
            continue
        for col in ["Open", "Close", "Quote Asset"]:
            d[col] = pd.to_numeric(d[col], errors="coerce")
        d["Open time"] = pd.to_datetime(d["Open time"])
        d["Close time"] = pd.to_datetime(d["Close time"])
        cadence = d["Open time"].diff().dropna()
        if not cadence.eq(pd.Timedelta(days=1)).all():
            raise ValueError(f"{symbol}: daily bars have gaps; row shifts are unsafe")
        d["decision_time"] = d["Close time"]
        lag = config.execution_lag_bars
        d["entry_time"] = d["Open time"].shift(-lag)
        d["exit_time"] = d["Open time"].shift(-(lag + 1))
        d["entry_price"] = d["Open"].shift(-lag)
        d["exit_price"] = d["Open"].shift(-(lag + 1))
        d["price_return"] = d["exit_price"] / d["entry_price"] - 1.0
        d["realized_funding"] = _funding_inside_intervals(
            funding.get(symbol, pd.DataFrame()), d["entry_time"], d["exit_time"]
        )
        d["holding_funding_observations"] = _funding_count_inside_intervals(
            funding.get(symbol, pd.DataFrame()), d["entry_time"], d["exit_time"]
        )
        d["carry_signal"], d["funding_observations"] = _daily_funding_features(
            funding.get(symbol, pd.DataFrame()), d["Open time"], config.lookback_days
        )
        d["history_days"] = np.arange(1, len(d) + 1)
        d["symbol"] = symbol
        frames.append(
            d[
                [
                    "decision_time",
                    "entry_time",
                    "exit_time",
                    "symbol",
                    "price_return",
                    "realized_funding",
                    "carry_signal",
                    "funding_observations",
                    "holding_funding_observations",
                    "history_days",
                ]
            ]
        )
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True).dropna().sort_values(["decision_time", "symbol"])
    if not (out["entry_time"] > out["decision_time"]).all():
        raise AssertionError("carry route attempted a same-close fill")
    return out.reset_index(drop=True)


def make_carry_weights(panel: pd.DataFrame, config: CarryConfig) -> pd.DataFrame:
    """Long the lowest funding tail and short the highest, with unit gross exposure."""

    rows: list[pd.DataFrame] = []
    for decision_time, group in panel.groupby("decision_time", sort=True):
        group = group.copy()
        group["eligible"] = False
        group["weight"] = 0.0
        eligible = group[
            (group["history_days"] >= config.min_history_days)
            & (group["funding_observations"] >= config.min_funding_observations)
            & (
                group["holding_funding_observations"]
                >= config.min_holding_funding_observations
            )
        ].copy()
        n = len(eligible)
        if n >= config.min_symbols:
            # Preserve the percentile-tail rule that was fixed in the original
            # discovery experiment.  For n=42 it selects 8 low and 9 high names.
            ranks = eligible["carry_signal"].rank(method="average", pct=True)
            long_idx = ranks[ranks <= config.tail_fraction].index
            short_idx = ranks[ranks >= 1.0 - config.tail_fraction].index
            if len(long_idx) and len(short_idx):
                if set(long_idx) & set(short_idx):
                    raise AssertionError("long/short carry tails overlap")
                group.loc[eligible.index, "eligible"] = True
                group.loc[long_idx, "weight"] = 0.5 / len(long_idx)
                group.loc[short_idx, "weight"] = -0.5 / len(short_idx)
        # Keep every outcome row.  Sparse weights plus a complete outcome matrix are
        # required for a valid time-shift null and for zero-weight inactive days.
        rows.append(group)
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True).sort_values(["decision_time", "symbol"])


def _component_matrices(weighted: pd.DataFrame) -> tuple[pd.DatetimeIndex, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    dates = pd.DatetimeIndex(sorted(weighted["entry_time"].unique()))
    symbols = sorted(weighted["symbol"].unique())
    shape = (len(dates), len(symbols))
    date_pos = {d: i for i, d in enumerate(dates)}
    sym_pos = {s: i for i, s in enumerate(symbols)}
    weights = np.zeros(shape)
    price = np.zeros(shape)
    funding = np.zeros(shape)
    for row in weighted.itertuples(index=False):
        i, j = date_pos[pd.Timestamp(row.entry_time)], sym_pos[row.symbol]
        weights[i, j] = float(row.weight)
        price[i, j] = float(row.price_return)
        funding[i, j] = float(row.realized_funding)
    return dates, pd.DataFrame(weights, index=dates, columns=symbols), pd.DataFrame(
        price, index=dates, columns=symbols
    ), pd.DataFrame(funding, index=dates, columns=symbols)


def carry_daily_components(
    weighted: pd.DataFrame, *, liquidate_end: bool = True
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return portfolio components and the aligned weight matrix."""

    dates, weights, prices, funding = _component_matrices(weighted)
    components = _components_from_matrices(
        weights, prices, funding, liquidate_end=liquidate_end
    )
    return components, weights, prices, funding


def _components_from_matrices(
    weights: pd.DataFrame,
    prices: pd.DataFrame,
    funding: pd.DataFrame,
    *,
    liquidate_end: bool,
) -> pd.DataFrame:
    previous = weights.shift(1, fill_value=0.0)
    turnover = (weights - previous).abs().sum(axis=1)
    if liquidate_end and len(turnover):
        turnover.iloc[-1] += weights.iloc[-1].abs().sum()
    components = pd.DataFrame(index=weights.index)
    components["price"] = (weights * prices).sum(axis=1)
    components["funding"] = (-weights * funding).sum(axis=1)
    components["turnover"] = turnover
    components["gross"] = weights.abs().sum(axis=1)
    return components


def _hac_mean_ci(values: pd.Series, max_lag: int = 7) -> tuple[float, float, float]:
    x = values.dropna().to_numpy(float)
    n = len(x)
    if n < 3:
        return float("nan"), float("nan"), float("nan")
    centered = x - x.mean()
    long_run = float(np.dot(centered, centered) / n)
    for lag in range(1, min(max_lag, n - 1) + 1):
        gamma = float(np.dot(centered[lag:], centered[:-lag]) / n)
        long_run += 2.0 * (1.0 - lag / (max_lag + 1.0)) * gamma
    se = math.sqrt(max(long_run, 0.0) / n)
    if se == 0:
        return x.mean(), x.mean(), float("inf")
    return x.mean() - 1.96 * se, x.mean() + 1.96 * se, x.mean() / se


def _profit_factor(values: pd.Series) -> float:
    gains = values[values > 0].sum()
    losses = -values[values < 0].sum()
    return float(gains / losses) if losses > 0 else float("inf")


def _max_drawdown(returns: pd.Series) -> float:
    equity = pd.concat(
        [pd.Series([1.0], index=["initial"]), (1.0 + returns).cumprod()]
    )
    return float((equity / equity.cummax() - 1.0).min()) if len(returns) else float("nan")


def circular_shift_p_value(
    weights: pd.DataFrame,
    price: pd.DataFrame,
    funding: pd.DataFrame,
    observed_mean: float,
    *,
    cost_per_leg_bps: float,
    n_perm: int = 999,
    min_shift: int = 14,
    seed: int = 20260802,
) -> tuple[float, int]:
    """Joint outcome-shift null with the observed strategy cost stream held fixed.

    Price and funding outcomes move together across all symbols, preserving common market
    shocks and serial structure.  Desired weights and their exact entry/rebalance/final
    liquidation costs remain unchanged, so a null draw cannot win or lose merely because
    the circular cut introduced a cheaper boundary transition.
    """

    n = len(weights)
    if n < min_shift * 2 or n_perm <= 0:
        return float("nan"), 0
    w = weights.to_numpy(float)
    pr = price.reindex_like(weights).to_numpy(float)
    fu = funding.reindex_like(weights).to_numpy(float)
    previous = np.vstack([np.zeros((1, w.shape[1])), w[:-1]])
    observed_turnover = np.abs(w - previous).sum(axis=1)
    observed_turnover[-1] += np.abs(w[-1]).sum()
    null = []
    valid_shifts = np.arange(min_shift, n - min_shift + 1)
    # There are only O(n) distinct circular shifts.  Never sample duplicates and
    # pretend they add p-value resolution.
    if n_perm < len(valid_shifts):
        rng = np.random.default_rng(seed)
        shifts = rng.choice(valid_shifts, size=n_perm, replace=False)
    else:
        shifts = valid_shifts
    for shift in shifts:
        shifted_price = np.roll(pr, int(shift), axis=0)
        shifted_funding = np.roll(fu, int(shift), axis=0)
        ret = (w * shifted_price).sum(axis=1) + (-w * shifted_funding).sum(axis=1)
        ret -= observed_turnover * cost_per_leg_bps / 10_000.0
        null.append(float(ret.mean()))
    p_value = float((np.sum(np.asarray(null) >= observed_mean) + 1) / (len(null) + 1))
    return p_value, len(null)


def block_bootstrap_net_profit_p_value(
    values: pd.Series,
    *,
    n_boot: int = 999,
    block_length: int = 21,
    seed: int = 20260815,
) -> tuple[float, int]:
    """One-sided p-value for positive *net* mean with serial dependence retained.

    The circular-shift test above answers a signal-association question and holds the
    observed cost path fixed.  This second null answers the distinct economic question:
    whether returns after the declared costs have positive mean.  It resamples centered
    circular blocks, so raising costs lowers the observed net mean and therefore changes
    this p-value even when the association p-value is unchanged.
    """

    x = values.dropna().to_numpy(float)
    n = len(x)
    if n < 3 or n_boot <= 0 or block_length < 1:
        return float("nan"), 0
    block = min(int(block_length), n)
    observed = float(x.mean())
    centered = x - observed
    rng = np.random.default_rng(seed)
    starts_per_draw = math.ceil(n / block)
    offsets = np.arange(block)
    null_means = np.empty(int(n_boot), dtype=float)
    for draw in range(int(n_boot)):
        starts = rng.integers(0, n, size=starts_per_draw)
        indices = ((starts[:, None] + offsets[None, :]) % n).reshape(-1)[:n]
        null_means[draw] = centered[indices].mean()
    p_value = float((np.sum(null_means >= observed) + 1) / (len(null_means) + 1))
    return p_value, len(null_means)


def evaluate_carry(
    weighted: pd.DataFrame,
    *,
    cost_per_leg_bps: float,
    start: pd.Timestamp | str | None = None,
    end: pd.Timestamp | str | None = None,
    n_perm: int = 999,
    periods_per_year: int = 365,
    hac_max_lag: int = 7,
    permutation_min_shift: int = 14,
    require_continuous_active: bool = False,
) -> tuple[PortfolioMetrics, pd.DataFrame]:
    if cost_per_leg_bps < 0:
        raise ValueError("cost_per_leg_bps cannot be negative")
    selected = weighted.copy()
    if start is not None:
        selected = selected[selected["entry_time"] >= pd.Timestamp(start)]
    if end is not None:
        selected = selected[selected["entry_time"] < pd.Timestamp(end)]
    if selected.empty:
        raise ValueError("no carry observations in the requested interval")
    _, weights, price_matrix, funding_matrix = carry_daily_components(
        selected, liquidate_end=False
    )
    active = weights.abs().sum(axis=1) > 0
    if not active.any():
        raise ValueError("carry route has no active portfolio in the requested interval")
    first, last = active[active].index[0], active[active].index[-1]
    if require_continuous_active and not active.loc[first:last].all():
        raise ValueError("inactive portfolio bars inside the evaluation interval; refuse zero-return Sharpe")
    weights = weights.loc[first:last]
    price_matrix = price_matrix.loc[first:last]
    funding_matrix = funding_matrix.loc[first:last]
    components = _components_from_matrices(
        weights, price_matrix, funding_matrix, liquidate_end=True
    )
    components["cost"] = components["turnover"] * cost_per_leg_bps / 10_000.0
    components["net"] = components["price"] + components["funding"] - components["cost"]
    r = components["net"].dropna()
    ci_lo, ci_hi, t_stat = _hac_mean_ci(r, max_lag=hac_max_lag)
    sd = r.std(ddof=1)
    sharpe = float(r.mean() / sd * math.sqrt(periods_per_year)) if sd > 0 else float("nan")
    p, permutation_n = circular_shift_p_value(
        weights,
        price_matrix,
        funding_matrix,
        float(r.mean()),
        cost_per_leg_bps=cost_per_leg_bps,
        n_perm=n_perm,
        min_shift=permutation_min_shift,
    )
    net_p, net_n = block_bootstrap_net_profit_p_value(
        r,
        n_boot=n_perm,
        block_length=max(1, hac_max_lag + 1),
    )
    metrics = PortfolioMetrics(
        n_days=len(r),
        mean_bps_day=float(r.mean() * 10_000),
        total_return=float((1.0 + r).prod() - 1.0),
        profit_factor=_profit_factor(r),
        sharpe=sharpe,
        max_drawdown=_max_drawdown(r),
        win_rate=float((r > 0).mean()),
        hac_ci_lo_bps=float(ci_lo * 10_000),
        hac_ci_hi_bps=float(ci_hi * 10_000),
        hac_t_stat=float(t_stat),
        permutation_p=p,
        permutation_n=permutation_n,
        net_profit_block_p=net_p,
        net_profit_block_n=net_n,
        price_ann_arithmetic=float(components["price"].mean() * periods_per_year),
        funding_ann_arithmetic=float(components["funding"].mean() * periods_per_year),
        cost_ann_arithmetic=float(-components["cost"].mean() * periods_per_year),
        average_gross=float(components["gross"].mean()),
        average_turnover=float(components["turnover"].mean()),
    )
    return metrics, components


def metrics_dict(metrics: PortfolioMetrics) -> dict:
    return asdict(metrics)
