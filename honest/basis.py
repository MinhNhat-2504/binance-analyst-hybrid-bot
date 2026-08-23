"""Spot-perp basis carry (cash-and-carry) on daily bars.

The trade: long the SPOT coin, short the PERP of the same coin, same notional. Price risk
cancels (spot_ret - perp_ret is the change in basis, a mean-reverting spread). What is
left is the funding the short perp RECEIVES whenever funding is positive - an observed
cash flow, not a forecast. This is carry in the literal sense, and it is the natural
complement to CARRY-7d, whose PnL we measured to be ~80% price leg.

Where this strategy usually dies - and what this module refuses to hide:

  * Four legs per round trip, and spot is the expensive half. Binance spot VIP0 taker is
    10bps, perp taker 5bps. Entering AND exiting = 2x(10+5) = 30bps before slippage.
    Costs are charged on turnover of each leg, not assumed away.
  * Capital is not 1:1 with notional. The spot leg ties up 100% of notional; the perp leg
    needs margin. Returns are reported per unit NOTIONAL (comparable to the carry lab)
    and the capital multiplier is stated so nobody mistakes notional yield for ROE.
  * One-sided by construction. Only long-spot/short-perp when trailing funding is
    positive; there is no reverse trade (shorting spot needs margin borrow = cost).
    Negative-funding names are simply not held. Cash earns 0.
  * Spot and perp tickers differ for the 1000X contracts (1000PEPEUSDT perp vs PEPEUSDT
    spot, price x1000). Returns are scale-free so the basis CHANGE is right either way;
    the basis LEVEL is only reported, never traded on.

Lookahead discipline is the carry lab's: weights decided at close t apply to day t+1,
funding of day t+1 accrues to the weights held during t+1.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from .data import CACHE_DIR, KLINE_COLS, NUMERIC_COLS, _get, fetch_klines
from .funding import fetch_funding

SPOT_API = "https://api.binance.com/api/v3/klines"

# Spot VIP0 taker 10bps, perp taker 5bps, plus 2bps slippage each. Charged per unit of
# |delta weight| on EACH leg, i.e. every rebalance pays spot+perp on the changed notional.
SPOT_COST_PER_LEG = 0.0010 + 0.0002
PERP_COST_PER_LEG = 0.0005 + 0.0002
COST_PER_UNIT_TURNOVER = SPOT_COST_PER_LEG + PERP_COST_PER_LEG   # 19bps per unit |dW|
TRADING_DAYS = 365

# Perp -> (spot symbol, price multiplier). Anything not listed maps to itself, x1.
SPOT_MAP = {
    "1000PEPEUSDT": ("PEPEUSDT", 1000.0),
    "1000SHIBUSDT": ("SHIBUSDT", 1000.0),
    "1000LUNCUSDT": ("LUNCUSDT", 1000.0),
    "1000BONKUSDT": ("BONKUSDT", 1000.0),
    "1000FLOKIUSDT": ("FLOKIUSDT", 1000.0),
}


def fetch_spot_klines(symbol: str, interval: str = "1d", days_back: int = 600,
                      use_cache: bool = True) -> pd.DataFrame:
    """Spot klines, same shape as fetch_klines. Separate cache key."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache = CACHE_DIR / f"SPOT_{symbol}_{interval}_{days_back}d.parquet"
    if use_cache and cache.exists():
        return pd.read_parquet(cache)
    step = 1440 * 60_000
    now_ms = int(time.time() * 1000)
    start = now_ms - days_back * 24 * 60 * 60_000
    rows: list[list] = []
    while start < now_ms:
        batch = _get(f"{SPOT_API}?symbol={symbol}&interval={interval}&startTime={start}&limit=1000")
        if not batch:
            break
        rows.extend(batch)
        nxt = batch[-1][0] + step
        if nxt <= start:
            break
        start = nxt
        time.sleep(0.2)
    if not rows:
        return pd.DataFrame(columns=KLINE_COLS)
    df = pd.DataFrame(rows, columns=KLINE_COLS)
    df = df.drop_duplicates(subset="Open time").sort_values("Open time").reset_index(drop=True)
    df["Open time"] = pd.to_datetime(df["Open time"], unit="ms")
    df["Close time"] = pd.to_datetime(df["Close time"], unit="ms")
    for col in NUMERIC_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.iloc[:-1].reset_index(drop=True)   # drop forming bar
    if use_cache:
        df.to_parquet(cache, index=False)
    return df


def build_basis_panel(symbols: list[str], days: int = 600, min_days: int = 400,
                      use_cache: bool = True, verbose: bool = True):
    """Aligned daily panels: perp close, spot close (scaled to perp units), per-day funding.

    Tradeable symbol-day requires: perp volume > 0, spot volume > 0, inside the funding
    span (same zombie guard as the carry lab, applied to BOTH legs).
    """
    perp_c, spot_c, perp_v, spot_v, fund = {}, {}, {}, {}, {}
    for sym in symbols:
        spot_sym, mult = SPOT_MAP.get(sym, (sym, 1.0))
        try:
            kp = fetch_klines(sym, "1d", days, use_cache)
            ks = fetch_spot_klines(spot_sym, "1d", days, use_cache)
            if len(kp) < min_days or len(ks) < min_days:
                if verbose:
                    print(f"  skip {sym}: perp {len(kp)}d / spot {len(ks)}d < {min_days}d")
                continue
            ip = pd.to_datetime(kp["Open time"]).dt.normalize()
            is_ = pd.to_datetime(ks["Open time"]).dt.normalize()
            pc = kp.set_index(ip)["Close"]; pc = pc[~pc.index.duplicated(keep="last")]
            pv = kp.set_index(ip)["Volume"]; pv = pv[~pv.index.duplicated(keep="last")]
            sc = ks.set_index(is_)["Close"] * mult; sc = sc[~sc.index.duplicated(keep="last")]
            sv = ks.set_index(is_)["Volume"]; sv = sv[~sv.index.duplicated(keep="last")]
            fr = fetch_funding(sym, days + 30, use_cache)
            if fr.empty:
                if verbose:
                    print(f"  skip {sym}: no funding")
                continue
            perp_c[sym], spot_c[sym], perp_v[sym], spot_v[sym] = pc, sc, pv, sv
            fund[sym] = fr.groupby(fr["fundingTime"].dt.normalize())["fundingRate"].sum()
            if verbose:
                print(f"  {sym:14s} perp {len(pc):4d}d  spot {len(sc):4d}d ({spot_sym})  funding {len(fund[sym]):4d}d")
        except Exception as exc:
            if verbose:
                print(f"  FAIL {sym}: {exc}")

    perp = pd.DataFrame(perp_c).sort_index()
    spot = pd.DataFrame(spot_c).reindex(perp.index)
    pv = pd.DataFrame(perp_v).reindex(perp.index)
    sv = pd.DataFrame(spot_v).reindex(perp.index)
    fday = pd.DataFrame(fund).reindex(perp.index)

    tradeable = (pv > 0) & (sv > 0) & spot.notna() & perp.notna()
    for sym, f in fund.items():
        alive = (perp.index >= f.index.min()) & (perp.index <= f.index.max())
        fday.loc[alive, sym] = fday.loc[alive, sym].fillna(0.0)
        tradeable.loc[~alive, sym] = False
    perp = perp.where(tradeable)
    spot = spot.where(tradeable)
    if verbose:
        dead = int((~tradeable & pv.notna()).sum().sum())
        if dead:
            print(f"  tradeability guard: {dead:,} symbol-days excluded")
    return perp, spot, fday


# ---------------------------------------------------------------------------
# Strategy: long spot / short perp on the top-k trailing-funding names, hold H days.
# ---------------------------------------------------------------------------

def basis_weights(fday: pd.DataFrame, tradeable: pd.DataFrame, lookback: int, k: int,
                  hold_days: int, rng: np.random.Generator | None = None) -> pd.DataFrame:
    """Weight matrix (already lagged). Rebalance every `hold_days`; between rebalances the
    book is held. Only names with POSITIVE trailing funding are eligible. Equal weight
    1/k of notional each; fewer than k eligible -> fewer positions, rest in cash.

    rng=None  -> pick the TOP-k eligible by trailing funding (the strategy).
    rng given -> pick k eligible names UNIFORMLY AT RANDOM (the null). Same eligibility,
                 same breadth, same turnover profile, same real funding collected on whoever
                 is held - only "rank by funding" is destroyed. A column permutation would
                 have let the null short negative-funding perps and PAY funding, which the
                 strategy can never do by construction; that is a rigged null, not a test."""
    sig = fday.rolling(lookback).sum().where(tradeable)
    W = pd.DataFrame(0.0, index=fday.index, columns=fday.columns)
    last = None
    for i, day in enumerate(fday.index):
        if i % hold_days == 0:
            row = sig.loc[day]
            eligible = row[(row > 0) & row.notna()]
            if rng is None:
                picks = eligible.sort_values(ascending=False).index[:k]
            else:
                idx = np.array(eligible.index)
                picks = idx[rng.permutation(len(idx))[:k]] if len(idx) else idx
            last = pd.Series(0.0, index=fday.columns)
            if len(picks):
                last[picks] = 1.0 / k      # fixed 1/k so a thin day holds cash, not 100% in 1 name
        W.loc[day] = last if last is not None else 0.0
    return W.shift(1).fillna(0.0)


def evaluate_basis(W: pd.DataFrame, perp: pd.DataFrame, spot: pd.DataFrame,
                   fday: pd.DataFrame, cost_per_unit_turnover: float = COST_PER_UNIT_TURNOVER,
                   capital_multiplier: float = 1.5) -> dict:
    """Per-unit-NOTIONAL daily returns of the long-spot/short-perp book.

    price leg   = W * (spot_ret - perp_ret)        (basis change; ~0 mean, noise)
    funding leg = W * fday                           (short perp RECEIVES positive funding)
    cost        = |dW| * cost_per_unit_turnover       (spot + perp legs on the changed notional)

    capital_multiplier: capital tied up per unit notional (1.0 spot + margin for the perp
    leg). 1.5 = perp at 2x; 2.0 = perp at 1x. ROE = notional return / multiplier.
    """
    sret = spot.pct_change(fill_method=None)
    pret = perp.pct_change(fill_method=None)
    ok = sret.notna() & pret.notna() & spot.shift(1).notna() & perp.shift(1).notna()
    W = W.where(ok, 0.0)
    price = (W * (sret - pret)).sum(axis=1)
    fund = (W * fday.fillna(0.0)).sum(axis=1)
    turn = (W - W.shift(1).fillna(0.0)).abs().sum(axis=1)
    cost = turn * cost_per_unit_turnover
    daily = (price + fund - cost).fillna(0.0)

    live = W.abs().sum(axis=1) > 0
    if live.any():
        s = live.idxmax()
        daily, price, fund, cost, turn = (x[s:] for x in (daily, price, fund, cost, turn))

    mu, sd = daily.mean(), daily.std(ddof=1)
    eq = (1 + daily).cumprod()
    dd = float((eq / eq.cummax() - 1).min() * 100)
    invested = float((W.abs().sum(axis=1)[live.idxmax():] if live.any() else pd.Series(dtype=float)).mean())
    return {
        "n_days": int(len(daily)),
        "sharpe": float(mu / sd * np.sqrt(TRADING_DAYS)) if sd > 0 else float("nan"),
        "ann_ret_notional_pct": float(mu * TRADING_DAYS * 100),
        "ann_ret_on_capital_pct": float(mu * TRADING_DAYS * 100 / capital_multiplier),
        "ann_vol_pct": float(sd * np.sqrt(TRADING_DAYS) * 100),
        "max_dd_pct": dd,
        "funding_leg_ann_pct": float(fund.mean() * TRADING_DAYS * 100),
        "price_leg_ann_pct": float(price.mean() * TRADING_DAYS * 100),
        "cost_drag_ann_pct": float(cost.mean() * TRADING_DAYS * 100),
        "avg_daily_turnover": float(turn.mean()),
        "avg_invested_fraction": invested,
        "capital_multiplier": capital_multiplier,
        "daily": daily,
    }


def permutation_null_sharpe(builder_with_rng, perp, spot, fday, observed_sharpe: float,
                            n_perm: int = 200, seed: int = 0) -> dict:
    """Random-selection null: k names drawn uniformly from the SAME eligible set at each
    rebalance (see basis_weights). What survives is "carry on random positive-funding
    names"; the gap to the real strategy is the value of ranking by funding."""
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n_perm):
        r = evaluate_basis(builder_with_rng(rng), perp, spot, fday)
        if np.isfinite(r["sharpe"]):
            out.append(r["sharpe"])
    arr = np.array(out)
    return {
        "n": len(arr),
        "mean": float(arr.mean()) if len(arr) else float("nan"),
        "p95": float(np.percentile(arr, 95)) if len(arr) else float("nan"),
        "p_value": float((np.sum(arr >= observed_sharpe) + 1) / (len(arr) + 1)),
    }
