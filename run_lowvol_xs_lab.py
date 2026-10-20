"""Pre-registered Q4/2026 LOWVOL-XS experiment; never exports trading targets.

Four fixed cells: rolling log-return volatility or Parkinson range, each with a
30 or 90 day window.  The 90 day BTC beta hedge is part of every cell.  Run the
discovery universe first; hold-out is inaccessible until a discovery nominee
is recorded.  All prices, ranges, and betas are known by decision-day close.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from honest.daily import _xs_weights, build_panel, evaluate
from honest.data import fetch_klines
from run_carry_holdout import HOLDOUT_UNIVERSE
from run_daily_lab import UNIVERSE

ROOT = Path(__file__).resolve().parent
DISCOVERY_REPORT = ROOT / "reports" / "lowvol_xs_discovery.json"
HOLDOUT_REPORT = ROOT / "reports" / "lowvol_xs_holdout.json"
FIRST_DAY = date(2026, 10, 1)
Q = 0.2
COST = 0.001
STRESS = 0.002
N_PERM = 200


def load_ranges(symbols: list[str], px: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    highs, lows = {}, {}
    for symbol in symbols:
        bars = fetch_klines(symbol, "1d", 600, use_cache=True)
        idx = pd.to_datetime(bars["Open time"]).dt.normalize()
        bars = bars.set_index(idx)
        bars = bars[~bars.index.duplicated(keep="last")]
        highs[symbol] = pd.to_numeric(bars["High"], errors="coerce")
        lows[symbol] = pd.to_numeric(bars["Low"], errors="coerce")
    high = pd.DataFrame(highs).reindex_like(px).where(px.notna())
    low = pd.DataFrame(lows).reindex_like(px).where(px.notna())
    if (high.lt(low) & high.notna() & low.notna()).any().any():
        raise ValueError("a daily high is below its low")
    return high, low


def volatility_signals(px: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame) -> dict[str, pd.DataFrame]:
    log_return = np.log(px.where(px > 0)).diff()
    valid_range = (high > 0) & (low > 0) & (high >= low)
    parkinson_daily = (np.log(high.where(valid_range) / low.where(valid_range)) ** 2) / (4 * np.log(2))
    return {
        f"logstd_{window}d": log_return.rolling(window, min_periods=window).std()
        for window in (30, 90)
    } | {
        f"parkinson_{window}d": np.sqrt(parkinson_daily.rolling(window, min_periods=window).mean())
        for window in (30, 90)
    }


def rolling_betas(px: pd.DataFrame, window: int = 90) -> pd.DataFrame:
    if "BTCUSDT" not in px.columns:
        raise ValueError("BTCUSDT is required for the beta hedge")
    ret = px.pct_change(fill_method=None)
    market = ret["BTCUSDT"]
    var = market.rolling(window, min_periods=window).var()
    betas = pd.DataFrame({symbol: ret[symbol].rolling(window, min_periods=window).cov(market) / var
                           for symbol in px.columns}, index=px.index)
    return betas.replace([np.inf, -np.inf], np.nan).clip(lower=0, upper=3)


def lowvol_weights(signal: pd.DataFrame, betas: pd.DataFrame, all_columns: pd.Index) -> pd.DataFrame:
    """Long bottom 20%, short top 20%, then hedge the chosen book's BTC beta."""
    signal = signal.where(betas.reindex_like(signal).notna())
    rank = signal.rank(axis=1, pct=True)
    longs = rank <= Q
    shorts = rank >= 1 - Q
    n_long = longs.sum(axis=1).replace(0, np.nan)
    n_short = shorts.sum(axis=1).replace(0, np.nan)
    raw = longs.div(n_long, axis=0).fillna(0).mul(0.5) - shorts.div(n_short, axis=0).fillna(0).mul(0.5)
    raw = raw.mul((n_long.notna() & n_short.notna()).astype(float), axis=0)
    hedge = -(raw * betas.reindex_like(raw)).sum(axis=1)
    out = raw.reindex(columns=all_columns, fill_value=0.0)
    out["BTCUSDT"] = out["BTCUSDT"] + hedge
    return out.shift(1).fillna(0.0)


def _aligned(W: pd.DataFrame, px: pd.DataFrame, fday: pd.DataFrame, start: pd.Timestamp,
             cost: float = COST) -> dict:
    result = evaluate(W, px, fday, cost)
    daily = result["daily"].loc[start:]
    returns = px.pct_change(fill_method=None)
    executable = W.where(returns.notna() & px.shift(1).notna(), 0.0)
    turnover = (executable - executable.shift(1).fillna(0.0)).abs().sum(axis=1)
    sd = daily.std(ddof=1)
    equity = (1 + daily).cumprod()
    return {"daily": daily, "sharpe": float(daily.mean() / sd * np.sqrt(365)) if sd > 0 else float("nan"),
            "ann_ret_pct": float(daily.mean() * 365 * 100),
            "worst_day_bps": float(daily.min() * 1e4),
            "max_dd_pct": float((equity / equity.cummax() - 1).min() * 100),
            "turnover": float(turnover.loc[start:].mean())}


def hac_alpha_t(strategy: pd.Series, carry: pd.Series, lags: int = 10) -> float:
    joined = pd.concat([strategy, carry], axis=1).dropna()
    if len(joined) <= lags + 2:
        return float("nan")
    y, benchmark = joined.iloc[:, 0].to_numpy(float), joined.iloc[:, 1].to_numpy(float)
    X = np.column_stack([np.ones(len(y)), benchmark])
    inverse = np.linalg.pinv(X.T @ X)
    coef = inverse @ X.T @ y
    scores = X * (y - X @ coef)[:, None]
    meat = scores.T @ scores
    for lag in range(1, lags + 1):
        gamma = scores[lag:].T @ scores[:-lag]
        meat += (1 - lag / (lags + 1)) * (gamma + gamma.T)
    variance = float((inverse @ meat @ inverse)[0, 0])
    return float(coef[0] / np.sqrt(variance)) if variance > 0 else float("nan")


def permutation_test(signal: pd.DataFrame, betas: pd.DataFrame, px: pd.DataFrame,
                     fday: pd.DataFrame, start: pd.Timestamp, observed: dict) -> dict:
    rng = np.random.default_rng(0)
    values = signal.to_numpy()
    sharpes, turnovers = [], []
    for _ in range(N_PERM):
        shuffled = pd.DataFrame(values[:, rng.permutation(values.shape[1])], index=signal.index,
                                columns=signal.columns)
        weights = lowvol_weights(shuffled, betas, px.columns)
        result = _aligned(weights, px, fday, start)
        sharpes.append(result["sharpe"])
        turnovers.append(result["turnover"])
    sharpes = np.asarray(sharpes)
    turnovers = np.asarray(turnovers)
    ratio = float(turnovers.mean() / observed["turnover"]) if observed["turnover"] > 0 else float("nan")
    valid = bool(np.isfinite(ratio) and abs(ratio - 1) <= 0.10)
    return {"n": N_PERM, "mean_turnover_ratio": ratio, "turnover_valid": valid,
            "p_value": float((np.sum(sharpes >= observed["sharpe"]) + 1) / (N_PERM + 1)) if valid else None,
            "null_sharpe_p95": float(np.nanpercentile(sharpes, 95))}


def evaluate_universe(symbols: list[str], *, cell_names: list[str] | None = None,
                      holdout: bool = False, verbose: bool = True) -> dict:
    px, fday = build_panel(list(dict.fromkeys([*symbols, "BTCUSDT"])), 600, min_days=400,
                           use_cache=True, verbose=False)
    if "BTCUSDT" not in px.columns or len(px) < 400:
        raise RuntimeError("incomplete daily panel or BTC hedge history")
    universe = [symbol for symbol in symbols if symbol in px.columns]
    high, low = load_ranges(universe, px)
    signals = volatility_signals(px[universe], high[universe], low[universe])
    betas = rolling_betas(px)
    carry_signal = fday[universe].rolling(7).sum()
    carry_W = _xs_weights(carry_signal, Q, direction=-1).reindex(columns=px.columns, fill_value=0.0)
    cells = {}
    if cell_names is not None and not set(cell_names) <= set(signals):
        raise ValueError("unknown pre-registered LOWVOL-XS cell")
    for name, signal in signals.items():
        if cell_names is not None and name not in cell_names:
            continue
        W = lowvol_weights(signal, betas, px.columns)
        active = W.abs().sum(axis=1) > 0
        if not active.any():
            raise RuntimeError(f"{name}: no hedgeable portfolios")
        start = active.idxmax()
        own = _aligned(W, px, fday, start)
        stress = _aligned(W, px, fday, start, STRESS)
        # Rebuild the unhedged cross-sectional book, retaining BTC if selected.
        rank = signal.where(betas.reindex_like(signal).notna()).rank(axis=1, pct=True)
        lo, hi = rank <= Q, rank >= 1 - Q
        nl, nh = lo.sum(axis=1).replace(0, np.nan), hi.sum(axis=1).replace(0, np.nan)
        raw_unhedged = lo.div(nl, axis=0).fillna(0).mul(0.5) - hi.div(nh, axis=0).fillna(0).mul(0.5)
        raw_unhedged = raw_unhedged.mul((nl.notna() & nh.notna()).astype(float), axis=0)
        unhedged = raw_unhedged.reindex(columns=px.columns, fill_value=0.0).shift(1).fillna(0.0)
        unhedged_result = _aligned(unhedged, px, fday, start)
        carry = _aligned(carry_W, px, fday, start)
        blend = _aligned(0.7 * carry_W + 0.3 * W, px, fday, start)
        rho = float(own["daily"].corr(carry["daily"]))
        halves = np.array_split(own["daily"].to_numpy(), 2)
        half_returns = [float(np.prod(1 + part) - 1) for part in halves]
        null = permutation_test(signal, betas, px, fday, start, own)
        alpha_t = hac_alpha_t(own["daily"], carry["daily"])
        checks = {
            "sharpe_threshold": own["sharpe"] > (0.40 if holdout else 0.70),
            "null_turnover_valid": null["turnover_valid"],
            "null_p_threshold": null["p_value"] is not None and null["p_value"] < (0.05 if holdout else 0.0125),
            "both_halves_positive": all(value > 0 for value in half_returns),
            "stress_sharpe_gt_0_35": stress["sharpe"] > 0.35,
            "abs_rho_lt_0_40": abs(rho) < 0.40,
            "hac_alpha_t_gt_2": alpha_t > 2.0,
            "blend_sharpe_gain_ge_0_10": blend["sharpe"] - carry["sharpe"] >= 0.10,
            "blend_worst_day_not_worse": blend["worst_day_bps"] >= carry["worst_day_bps"],
            "hedge_sharpe_not_worse": own["sharpe"] >= unhedged_result["sharpe"],
        }
        cells[name] = {
            "n_days": len(own["daily"]), "start": str(start.date()),
            "sharpe": own["sharpe"], "ann_ret_pct": own["ann_ret_pct"],
            "turnover": own["turnover"], "stress_sharpe": stress["sharpe"],
            "unhedged_sharpe": unhedged_result["sharpe"],
            "carry_sharpe_same_days": carry["sharpe"],
            "rho_carry": rho, "hac_alpha_t": alpha_t,
            "half_returns_pct": [100 * value for value in half_returns],
            "blend_sharpe": blend["sharpe"], "blend_worst_day_bps": blend["worst_day_bps"],
            "carry_worst_day_bps": carry["worst_day_bps"],
            "null": null, "checks": checks, "passed": all(checks.values()),
        }
        if verbose:
            print(f"  {name:15s} sharpe {own['sharpe']:+.2f}  ann {own['ann_ret_pct']:+.1f}%  "
                  f"p={null['p_value']}  rho={rho:+.2f}  alpha_t={alpha_t:+.2f}  "
                  f"turnover null {null['mean_turnover_ratio']:.2f}x  -> "
                  f"{'PASS' if cells[name]['passed'] else 'stop'}")
    return {"generated_utc": datetime.now(timezone.utc).isoformat(),
            "universe": "holdout" if holdout else "discovery",
            "n_symbols": len(universe), "n_panel_days": len(px),
            "panel_start": str(px.index.min().date()), "panel_end": str(px.index.max().date()),
            "protocol": "RESEARCH_PREREG_Q4_2026.md cell 2", "n_perm": N_PERM,
            "cells": cells, "nominees": [name for name, row in cells.items() if row["passed"]]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--holdout", action="store_true")
    args = parser.parse_args()
    if date.today() < FIRST_DAY:
        raise RuntimeError(f"LOWVOL-XS is preregistered for {FIRST_DAY} or later")
    if args.holdout:
        if HOLDOUT_REPORT.exists():
            print("REFUSING: hold-out was already opened; no second run")
            return 4
        if not DISCOVERY_REPORT.exists():
            print("REFUSING: discovery report is required before opening hold-out")
            return 4
        discovery = json.loads(DISCOVERY_REPORT.read_text(encoding="utf-8"))
        if not discovery.get("nominees"):
            print("REFUSING: no discovery nominee; hold-out stays closed")
            return 4
    symbols = HOLDOUT_UNIVERSE if args.holdout else UNIVERSE
    result = evaluate_universe(symbols, cell_names=discovery["nominees"] if args.holdout else None,
                               holdout=args.holdout)
    output = HOLDOUT_REPORT if args.holdout else DISCOVERY_REPORT
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    print(f"-> {output}  nominees={result['nominees']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
