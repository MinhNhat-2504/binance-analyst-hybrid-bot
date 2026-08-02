"""Daily-scale strategy lab: rule-based portfolios where 12-20bps of cost stops mattering.

Why this exists: the 15m ML stack was measured to carry ~5bps of signal against a ~12bps
cost floor - dead on arrival. At daily scale the same cost is paid over holds of days to
weeks, so cost stops being the binding constraint and the question becomes the honest one:
is there any signal at all?

Everything here is RULE-BASED with no fitted parameters. That kills the training-leakage
class of bugs outright (there is no training), leaving two threats, both handled:

  * Lookahead: every weight matrix is shift(1)-lagged - a position held on day t was
    decided from information available at the close of day t-1.
  * Selection-among-rules: the grid is small, pre-registered in run_daily_lab.py, and every
    cell is reported with its own permutation null. Nothing is cherry-picked.

Accounting conventions (the places daily backtests usually lie):

  * Returns are close-to-close. W.loc[t] applies to ret.loc[t] = close_t/close_{t-1} - 1.
  * Perp positions pay/receive funding no matter the strategy, so funding PnL
    (-W * funding_day, since positive funding means longs pay shorts) is included for
    EVERY strategy, not just the carry one.
  * Costs are charged on turnover: cost_per_leg * sum(|W_t - W_{t-1}|). Missing bars
    (delistings, gaps) force W to 0, so exits are paid for, not teleported.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .data import fetch_klines
from .funding import fetch_funding

# One leg, conservative for a retail-size taker order in a liquid perp: 5bps fee + 5bps
# slippage. A daily-rebalance strategy pays this on every unit of weight it moves.
COST_PER_LEG = 0.0010

TRADING_DAYS = 365  # crypto trades every day


def build_panel(symbols: list[str], days: int = 600, min_days: int = 400,
                use_cache: bool = True, verbose: bool = True):
    """Wide panels aligned on UTC date: close prices and per-day funding-rate sums.

    Symbols with under `min_days` of history are dropped entirely: a symbol that appears
    halfway through the sample would otherwise enter every cross-sectional rank exactly
    when it is newest and most volatile, which is a listing-bias tilt, not a signal.

    ZOMBIE GUARD (audit finding FA-1): Binance keeps returning flat, zero-volume klines
    for perps that were delisted or ticker-migrated (MKRUSDT sat at one price for 326
    days after the SKY migration). A blanket fillna(0.0) on funding then labels those
    corpses "cheap funding" and the carry rule happily longs them - an untradeable
    position with zero variance that deflates portfolio vol and inflates Sharpe. Guard:
    a symbol-day is only tradeable if its bar printed volume AND falls inside the span
    where the perp actually settled funding. Everything else is NaN, which the weight
    mask in evaluate() converts into a forced (and cost-charged) exit.
    """
    closes, vols, fundings = {}, {}, {}
    for sym in symbols:
        try:
            k = fetch_klines(sym, "1d", days, use_cache)
            if len(k) < min_days:
                if verbose:
                    print(f"  skip {sym}: {len(k)}d < {min_days}d")
                continue
            idx = pd.to_datetime(k["Open time"]).dt.normalize()
            s = k.set_index(idx)["Close"]
            v = k.set_index(idx)["Volume"]
            dedup = ~s.index.duplicated(keep="last")
            closes[sym], vols[sym] = s[dedup], v[dedup]

            fr = fetch_funding(sym, days + 30, use_cache)
            if fr.empty:
                if verbose:
                    print(f"  skip {sym}: no funding history")
                closes.pop(sym), vols.pop(sym)
                continue
            day = fr["fundingTime"].dt.normalize()
            fundings[sym] = fr.groupby(day)["fundingRate"].sum()
            if verbose:
                print(f"  {sym:14s} {len(s):4d}d klines, {len(fundings[sym]):4d}d funding")
        except Exception as exc:
            if verbose:
                print(f"  FAIL {sym}: {exc}")

    px = pd.DataFrame(closes).sort_index()
    vol = pd.DataFrame(vols).reindex(px.index)
    px = px.where(vol > 0)                      # zero-volume bar = dead market that day

    fday = pd.DataFrame(fundings).reindex(px.index)
    for sym, f in fundings.items():
        alive = (px.index >= f.index.min()) & (px.index <= f.index.max())
        # Inside the funding span a missing calendar day is a genuine 0-sum day; outside
        # it the perp does not exist, so both price and funding stay NaN (untradeable).
        fday.loc[alive, sym] = fday.loc[alive, sym].fillna(0.0)
        px.loc[~alive, sym] = np.nan

    n_zombie = int((px.isna() & (vol >= 0)).sum().sum())
    if verbose and n_zombie:
        print(f"  zombie guard: {n_zombie:,} symbol-days marked untradeable")
    return px, fday


# ---------------------------------------------------------------------------
# Strategies: signal -> weights. Each returns W ALREADY lagged (shift(1)).
# Splitting signal from weights lets the permutation null shuffle the signal
# through the identical weight constructor.
# ---------------------------------------------------------------------------

def _xs_weights(signal: pd.DataFrame, q: float, direction: int = 1) -> pd.DataFrame:
    """Long top-q / short bottom-q of a cross-sectional signal, 0.5 gross per side.

    direction=-1 flips (long the LOW end), used by carry where high funding is shorted.
    """
    rank = signal.rank(axis=1, pct=True)
    n_top = (rank >= 1 - q).sum(axis=1).replace(0, np.nan)
    n_bot = (rank <= q).sum(axis=1).replace(0, np.nan)
    w_top = (rank >= 1 - q).astype(float).div(n_top, axis=0) * 0.5
    w_bot = (rank <= q).astype(float).div(n_bot, axis=0) * 0.5
    w = (w_top - w_bot) * direction
    return w.shift(1).fillna(0.0)


def xs_momentum(px: pd.DataFrame, lookback: int, q: float = 0.2) -> pd.DataFrame:
    return _xs_weights(px.pct_change(lookback, fill_method=None), q=q, direction=1)


def ts_trend(px: pd.DataFrame, window: int) -> pd.DataFrame:
    """Long-flat: hold symbols above their own SMA, equal-weighted across active names."""
    above = (px > px.rolling(window).mean()).astype(float)
    n_active = above.sum(axis=1).replace(0, np.nan)
    return above.div(n_active, axis=0).shift(1).fillna(0.0)


def funding_carry(fday: pd.DataFrame, lookback: int, q: float = 0.2) -> pd.DataFrame:
    """Short the crowded-long end (they pay you), long the crowded-short end (they pay you).

    The return stream is price PnL + the funding actually received; the signal is trailing
    funding, which persists strongly at multi-day scale. This is a carry trade: the revenue
    leg is observed, not forecast.
    """
    return _xs_weights(fday.rolling(lookback).sum(), q=q, direction=-1)


def btc_hold(px: pd.DataFrame) -> pd.DataFrame:
    w = pd.DataFrame(0.0, index=px.index, columns=px.columns)
    if "BTCUSDT" in w.columns:
        w["BTCUSDT"] = 1.0
    return w.shift(1).fillna(0.0)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate(W: pd.DataFrame, px: pd.DataFrame, fday: pd.DataFrame,
             cost_per_leg: float = COST_PER_LEG) -> dict:
    # fill_method=None (FA-3): the pad default would forward-fill across a dead day and
    # let a position ride through a bar it could not have traded.
    rets = px.pct_change(fill_method=None)
    # A symbol with no price today cannot be held: zero the weight so turnover charges the exit.
    W = W.where(rets.notna() & px.shift(1).notna(), 0.0)

    price_pnl = (W * rets).sum(axis=1)
    funding_pnl = (-W * fday).sum(axis=1)   # positive funding: longs pay shorts
    turnover = (W - W.shift(1).fillna(0.0)).abs().sum(axis=1)
    costs = turnover * cost_per_leg
    daily = (price_pnl + funding_pnl - costs).fillna(0.0)

    # Skip the warm-up zone before the strategy first takes a position.
    live = W.abs().sum(axis=1) > 0
    if live.any():
        daily = daily[live.idxmax():]
        price_pnl, funding_pnl, costs = (s[live.idxmax():] for s in (price_pnl, funding_pnl, costs))

    mu, sd = daily.mean(), daily.std(ddof=1)
    equity = (1 + daily).cumprod()
    peak = equity.cummax()
    return {
        "n_days": int(len(daily)),
        "ann_ret_pct": float(mu * TRADING_DAYS * 100),
        "ann_vol_pct": float(sd * np.sqrt(TRADING_DAYS) * 100),
        "sharpe": float(mu / sd * np.sqrt(TRADING_DAYS)) if sd > 0 else float("nan"),
        "max_dd_pct": float(((equity / peak) - 1).min() * 100),
        "total_ret_pct": float((equity.iloc[-1] - 1) * 100) if len(equity) else 0.0,
        "funding_share_pct": float(funding_pnl.sum() / daily.sum() * 100) if daily.sum() != 0 else float("nan"),
        "avg_daily_turnover": float(turnover.mean()),
        "daily": daily,
    }


def permutation_null_sharpe(builder, signal: pd.DataFrame, px: pd.DataFrame,
                            fday: pd.DataFrame, observed_sharpe: float,
                            n_perm: int = 200, seed: int = 0) -> dict:
    """Column-permutation null: symbol j's whole signal HISTORY is reassigned to symbol k.

    Why columns and not within-day shuffling: a per-day shuffle gives the null portfolio
    brand-new random picks every day, so its turnover (and cost drag) is several times the
    real strategy's - the null then loses on costs, not on skill, and everything "beats"
    it. Permuting whole columns preserves the signal's persistence, turnover, breadth and
    cost profile exactly, and destroys only the signal<->symbol linkage. What survives is
    market structure; the gap to the real strategy is selection skill and nothing else.
    """
    rng = np.random.default_rng(seed)
    vals = signal.to_numpy()
    ncols = vals.shape[1]
    sharpes = []
    for _ in range(n_perm):
        s = pd.DataFrame(vals[:, rng.permutation(ncols)],
                         index=signal.index, columns=signal.columns)
        r = evaluate(builder(s), px, fday)
        if np.isfinite(r["sharpe"]):
            sharpes.append(r["sharpe"])
    arr = np.array(sharpes)
    return {
        "n": len(arr),
        "mean": float(arr.mean()) if len(arr) else float("nan"),
        "p95": float(np.percentile(arr, 95)) if len(arr) else float("nan"),
        "p_value": float((np.sum(arr >= observed_sharpe) + 1) / (len(arr) + 1)),
    }
