"""Triple-barrier labelling.

Differences from the notebook's apply_triple_barrier_high_low, each fixing a specific
defect the audit found:

1. NO `TBM_Label != 0` filter. The notebook dropped every bar where long and short both
   lost, which uses the future to decide which samples exist. Live, you cannot know whether
   the current bar is one of the survivors, so the model was trained on a universe it can
   never encounter. Here every bar is kept and long/short are labelled independently.

2. The label is NET RETURN AFTER COST, not a win/loss bit. The notebook's label ignored
   fees entirely despite the model being named `fee_aware`. A 5bps winner and a 200bps
   winner were the same label, so the model had no way to learn that most of its "wins"
   do not clear the 13bps round trip.

3. Each row carries `label_horizon_end`, the timestamp the simulated trade actually closed.
   Purging keys off this real end time instead of a fixed row count, which is what made the
   notebook's `purge_bars=24` wrong by ~10x on an interleaved multi-symbol frame.

Intrabar convention: if a bar's range touches both stop and target, the STOP is assumed to
fill first. Without tick data the true order is unknown, so we take the pessimistic branch.
Assuming the target instead would manufacture free profit exactly where it is least safe.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

DEFAULT_COST = 0.0013  # 4bps taker x2 + 5bps slippage, matching the live bot


def _simulate(highs, lows, closes, atr_pct, sl_mult, tp_mult, time_limit, cost, is_long):
    """Path-dependent exit sim for every bar. Returns (net_return, bars_held)."""
    n = len(closes)
    ret = np.full(n, np.nan)
    held = np.full(n, np.nan)
    sign = 1.0 if is_long else -1.0

    for i in range(n - time_limit):
        entry = closes[i]
        if not np.isfinite(entry) or entry <= 0 or not np.isfinite(atr_pct[i]):
            continue
        sl_pct = atr_pct[i] * sl_mult
        tp_pct = atr_pct[i] * tp_mult
        if sl_pct <= 0:
            continue

        stop = entry * (1 - sign * sl_pct)
        target = entry * (1 + sign * tp_pct)

        exit_px = closes[i + time_limit]
        bars = time_limit
        for j in range(1, time_limit + 1):
            k = i + j
            hi, lo = highs[k], lows[k]
            if is_long:
                stop_hit, tp_hit = lo <= stop, hi >= target
            else:
                stop_hit, tp_hit = hi >= stop, lo <= target
            if stop_hit:  # checked first: pessimistic on ambiguous bars
                exit_px, bars = stop, j
                break
            if tp_hit:
                exit_px, bars = target, j
                break

        ret[i] = sign * (exit_px - entry) / entry - cost
        held[i] = bars

    return ret, held


def label_symbol(df: pd.DataFrame, sl_mult: float = 1.0, tp_mult: float = 2.0,
                 time_limit: int = 12, cost: float = DEFAULT_COST,
                 bar_minutes: int = 15) -> pd.DataFrame:
    """Label one symbol's frame. Must be time-sorted and single-symbol."""
    df = df.copy()
    highs = df["High"].to_numpy(float)
    lows = df["Low"].to_numpy(float)
    closes = df["Close"].to_numpy(float)
    atr = df["ATR_Pct"].to_numpy(float)

    rl, bl = _simulate(highs, lows, closes, atr, sl_mult, tp_mult, time_limit, cost, True)
    rs, bs = _simulate(highs, lows, closes, atr, sl_mult, tp_mult, time_limit, cost, False)

    df["ret_long_net"] = rl
    df["ret_short_net"] = rs
    df["exit_bars_long"] = bl
    df["exit_bars_short"] = bs
    df["target_long"] = (rl > 0).astype(float)
    df["target_short"] = (rs > 0).astype(float)
    df.loc[~np.isfinite(rl), "target_long"] = np.nan
    df.loc[~np.isfinite(rs), "target_short"] = np.nan

    # Worst-case close time across both sides: what purging must respect. Read it from the
    # REAL timestamp of the exit bar, not entry_time + bars*bar_minutes. An exchange outage
    # leaves gaps in the 15m grid, so the count-times-bar-width estimate lands earlier than
    # the true exit and the purge would under-embargo across the gap. Positional indexing
    # into the actual Open time is gap-safe. `bar_minutes` is retained only as the fallback
    # spacing for exit indices that run past the end of the frame.
    n = len(df)
    max_bars = np.fmax(np.nan_to_num(bl, nan=time_limit),
                       np.nan_to_num(bs, nan=time_limit)).astype(np.int64)
    rows = np.arange(n, dtype=np.int64)
    exit_idx = np.minimum(rows + max_bars, n - 1)
    open_ns = df["Open time"].to_numpy("datetime64[ns]")
    horizon = open_ns[exit_idx]
    # For rows whose exit index was clamped to the last bar, fall back to the wall-clock
    # estimate so the horizon is never understated at the frame's tail. int64 throughout:
    # bars * minutes * ns/min reaches ~1e14, which overflows int32 (numpy's default on Win).
    clamped = (rows + max_bars) > (n - 1)
    ns_per_min = np.int64(60_000_000_000)
    est = open_ns + (max_bars * np.int64(bar_minutes) * ns_per_min).astype("timedelta64[ns]")
    horizon = np.where(clamped, np.maximum(horizon, est), horizon)
    df["label_horizon_end"] = horizon

    return df


def label_universe(frames: dict[str, pd.DataFrame], **kw) -> pd.DataFrame:
    """Label each symbol independently, then concatenate. Labelling per symbol matters:
    running it on a concatenated frame would let one symbol's bars resolve against the
    next symbol's prices."""
    out = []
    for sym, df in frames.items():
        if len(df) == 0:
            continue
        out.append(label_symbol(df.sort_values("Open time").reset_index(drop=True), **kw))
    if not out:
        return pd.DataFrame()
    return pd.concat(out, ignore_index=True).sort_values("Open time").reset_index(drop=True)


def uniqueness_weights(df: pd.DataFrame, bar_minutes: int = 15) -> np.ndarray:
    """Average uniqueness per Lopez de Prado, computed per symbol.

    Overlapping labels are the reason a 15m frame with a 3h horizon has ~12x fewer
    independent observations than rows. Without this, XGBoost sees the same event a dozen
    times and every metric fitted on those rows is overconfident by roughly sqrt(12).
    """
    w = np.ones(len(df))
    for sym, g in df.groupby("symbol", sort=False):
        starts = g["Open time"].to_numpy("datetime64[m]").astype(np.int64)
        ends = g["label_horizon_end"].to_numpy("datetime64[m]").astype(np.int64)
        order = np.argsort(starts)
        s, e = starts[order], ends[order]

        # Concurrency over a per-symbol minute grid via a difference array.
        t0 = s.min()
        grid_len = int((max(e.max(), s.max()) - t0) // bar_minutes) + 2
        diff = np.zeros(grid_len + 1)
        si = ((s - t0) // bar_minutes).astype(int)
        ei = ((e - t0) // bar_minutes).astype(int)
        np.add.at(diff, si, 1)
        np.add.at(diff, np.minimum(ei + 1, grid_len), -1)
        conc = np.cumsum(diff)[:grid_len]
        conc[conc < 1] = 1

        inv = 1.0 / conc
        cum = np.concatenate([[0.0], np.cumsum(inv)])
        span = np.maximum(ei - si + 1, 1)
        uniq = (cum[np.minimum(ei + 1, grid_len)] - cum[si]) / span

        idx = g.index.to_numpy()[order]
        w[df.index.get_indexer(idx)] = uniq
    return np.clip(w, 1e-6, 1.0)
