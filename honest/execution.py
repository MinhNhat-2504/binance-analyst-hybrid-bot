"""Maker-fill simulation with adverse selection.

The harness says the model has ~9.7bps of selection skill against a 13bps round trip.
Maker execution (0.02% vs 0.05% on Binance USDT-M) would cut the toll to ~6bps and flip
the sign. That arithmetic is trivially right and dangerously incomplete: it assumes you
get filled.

You do not. A resting limit order only fills when price comes to it, which means you
systematically miss the trades that ran your way immediately (the best ones) and fill the
trades that ticked against you first (the worst ones). That is adverse selection, and it
is invisible to any backtest that assumes entry at the close.

This module measures it, by simulating fills against the actual next-bar range instead of
assuming them:

    taker : fill at Close[i], pay taker fee + slippage. Always fills.
    maker : rest at Close[i] +/- offset. Fill only if the next `wait_bars` of price action
            actually reach the price. Otherwise the trade never happens.

The comparison that matters is not maker-fee vs taker-fee. It is:

    taker net over ALL signals   vs   maker net over the SUBSET that filled

Counting only filled makers against all takers is the error this module exists to prevent.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# Binance USDT-M futures, VIP0.
MAKER_FEE = 0.0002
TAKER_FEE = 0.0005


@dataclass
class FillResult:
    n_signals: int
    n_filled: int
    fill_rate: float
    net_bps_filled: float          # mean net over trades that actually filled
    net_bps_all_signals: float     # missed trades counted as 0, i.e. capital idle
    net_bps_missed_would_be: float # what the misses would have paid as takers
    adverse_selection_bps: float   # missed minus filled: >0 means misses were the winners
    pf: float
    total_ret: float


def _exit_path(highs, lows, closes, i, entry, sl_pct, tp_pct, time_limit, is_long):
    """Shared exit sim. Stop assumed to fill first on ambiguous bars."""
    sign = 1.0 if is_long else -1.0
    stop = entry * (1 - sign * sl_pct)
    target = entry * (1 + sign * tp_pct)
    n = len(closes)
    last = min(i + time_limit, n - 1)
    exit_px = closes[last]
    for j in range(i + 1, last + 1):
        if is_long:
            if lows[j] <= stop:
                return stop
            if highs[j] >= target:
                return target
        else:
            if highs[j] >= stop:
                return stop
            if lows[j] <= target:
                return target
    return exit_px


def simulate_fills(df: pd.DataFrame, signal_idx: np.ndarray, side: str,
                   sl_mult: float = 1.0, tp_mult: float = 2.0, time_limit: int = 12,
                   mode: str = "maker", offset_bps: float = 1.0, wait_bars: int = 2,
                   slippage_bps: float = 5.0, exit_mode: str = "taker") -> FillResult:
    """Simulate entry fills for `signal_idx` rows of a single-symbol frame.

    offset_bps: how far passive the resting order sits from Close[i]. Zero would sit at the
      touch and is not reliably maker; 1bp approximates resting behind the spread.
    wait_bars: how long the order rests before being cancelled. Longer waits raise fill
      rate and worsen adverse selection - the tradeoff this whole module exists to expose.
    exit_mode: exits are assumed taker by default. A stop that must fill is a market order;
      pretending otherwise is how backtests invent money.
    """
    highs = df["High"].to_numpy(float)
    lows = df["Low"].to_numpy(float)
    closes = df["Close"].to_numpy(float)
    atr = df["ATR_Pct"].to_numpy(float)
    n = len(closes)
    is_long = side.upper() == "LONG"
    sign = 1.0 if is_long else -1.0

    entry_fee = MAKER_FEE if mode == "maker" else TAKER_FEE
    exit_fee = MAKER_FEE if exit_mode == "maker" else TAKER_FEE
    entry_slip = 0.0 if mode == "maker" else slippage_bps / 1e4
    exit_slip = slippage_bps / 1e4

    filled, missed_would_be = [], []

    for i in signal_idx:
        if i + time_limit >= n or not np.isfinite(atr[i]) or atr[i] <= 0:
            continue
        sl_pct, tp_pct = atr[i] * sl_mult, atr[i] * tp_mult
        ref = closes[i]

        if mode == "taker":
            entry = ref * (1 + sign * entry_slip)
            exit_px = _exit_path(highs, lows, closes, i, entry, sl_pct, tp_pct, time_limit, is_long)
            r = sign * (exit_px * (1 - sign * exit_slip) - entry) / entry - entry_fee - exit_fee
            filled.append(r)
            continue

        # Maker: rest passively. A long rests BELOW the close and needs price to dip to it;
        # a short rests ABOVE and needs a tick up.
        limit = ref * (1 - sign * offset_bps / 1e4)
        fill_bar = -1
        for j in range(i + 1, min(i + 1 + wait_bars, n)):
            touched = lows[j] <= limit if is_long else highs[j] >= limit
            if touched:
                fill_bar = j
                break

        if fill_bar < 0:
            # Never filled. Record what a TAKER would have earned on this same signal: the
            # gap between these and the fills IS the adverse selection. This must use taker
            # ENTRY slippage, exactly like the taker baseline - crediting the missed trade a
            # slippage-free entry would inflate the adverse-selection figure by ~5bps.
            taker_slip = slippage_bps / 1e4
            entry_t = ref * (1 + sign * taker_slip)
            exit_t = _exit_path(highs, lows, closes, i, entry_t, sl_pct, tp_pct, time_limit, is_long)
            missed_would_be.append(
                sign * (exit_t * (1 - sign * exit_slip) - entry_t) / entry_t - TAKER_FEE - exit_fee
            )
            continue

        if fill_bar + time_limit >= n:
            continue
        # Resolve the fill bar's OWN remaining range before scanning later bars. The order
        # is entered intrabar at `limit`, so a fill bar that also reaches the stop is a
        # same-bar stop-out - omitting it (scanning only fill_bar+1) silently drops losses,
        # and since sl<tp those omissions are net favourable. Stop assumed first on ambiguity.
        stop = limit * (1 - sign * sl_pct)
        target = limit * (1 + sign * tp_pct)
        hi, lo = highs[fill_bar], lows[fill_bar]
        if is_long:
            stop_hit, tp_hit = lo <= stop, hi >= target
        else:
            stop_hit, tp_hit = hi >= stop, lo <= target
        if stop_hit:
            exit_px = stop
        elif tp_hit:
            exit_px = target
        else:
            exit_px = _exit_path(highs, lows, closes, fill_bar, limit, sl_pct, tp_pct, time_limit, is_long)
        r = sign * (exit_px * (1 - sign * exit_slip) - limit) / limit - entry_fee - exit_fee
        filled.append(r)

    f = np.array(filled)
    m = np.array(missed_would_be)
    n_sig = len(f) + len(m)
    if n_sig == 0 or len(f) == 0:
        nan = float("nan")
        return FillResult(n_sig, len(f), 0.0, nan, nan, nan, nan, nan, 0.0)

    gains, losses = f[f > 0].sum(), -f[f < 0].sum()
    return FillResult(
        n_signals=n_sig,
        n_filled=len(f),
        fill_rate=len(f) / n_sig,
        net_bps_filled=float(f.mean() * 1e4),
        net_bps_all_signals=float(f.sum() / n_sig * 1e4),
        net_bps_missed_would_be=float(m.mean() * 1e4) if len(m) else float("nan"),
        adverse_selection_bps=float(m.mean() * 1e4 - f.mean() * 1e4) if len(m) else float("nan"),
        pf=float(gains / losses) if losses > 0 else float("inf"),
        total_ret=float(f.sum()),
    )


def compare_execution(frames: dict[str, pd.DataFrame], signals: dict[str, np.ndarray], side: str,
                      wait_grid=(1, 2, 4, 8), offset_bps: float = 1.0, **kw) -> pd.DataFrame:
    """Taker baseline vs maker at several rest durations, pooled across symbols."""
    rows = []

    tk = [simulate_fills(frames[s], sig, side, mode="taker", **kw)
          for s, sig in signals.items() if len(sig)]
    tk = [r for r in tk if r.n_filled]
    if tk:
        tot = sum(r.total_ret for r in tk)
        nf = sum(r.n_filled for r in tk)
        rows.append(dict(mode="taker", wait_bars=0, n_signals=sum(r.n_signals for r in tk),
                         n_filled=nf, fill_rate=1.0, net_bps_filled=tot / nf * 1e4,
                         net_bps_all_signals=tot / nf * 1e4, adverse_selection_bps=np.nan))

    for wait in wait_grid:
        rs = [simulate_fills(frames[s], sig, side, mode="maker", offset_bps=offset_bps,
                             wait_bars=wait, **kw)
              for s, sig in signals.items() if len(sig)]
        rs = [r for r in rs if r.n_filled]
        if not rs:
            continue
        tot = sum(r.total_ret for r in rs)
        nf = sum(r.n_filled for r in rs)
        ns = sum(r.n_signals for r in rs)
        adv = [r.adverse_selection_bps for r in rs if np.isfinite(r.adverse_selection_bps)]
        rows.append(dict(mode="maker", wait_bars=wait, n_signals=ns, n_filled=nf,
                         fill_rate=nf / ns, net_bps_filled=tot / nf * 1e4,
                         net_bps_all_signals=tot / ns * 1e4,
                         adverse_selection_bps=float(np.mean(adv)) if adv else np.nan))

    return pd.DataFrame(rows)
