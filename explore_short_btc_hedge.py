"""One fixed, retrospective SHORT + BTC hedge comparison; no trading targets.

Both historical symbol sets have already been inspected in the leg report, so
this is exploratory evidence only. See RESEARCH_Q1_SHORT_BTC_PROTOCOL.md.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from honest.daily import _xs_weights, build_panel, evaluate
from run_carry_holdout import HOLDOUT_UNIVERSE
from run_daily_lab import UNIVERSE
from run_lowvol_xs_lab import rolling_betas

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "reports" / "short_btc_hedge_exploratory.json"
COST = 0.001
STRESS = 0.002
Q = 0.2
BOOTSTRAPS = 999
BLOCK = 22


def candidate_weights(funding: pd.DataFrame, betas: pd.DataFrame,
                      all_columns: pd.Index) -> pd.DataFrame:
    """Select costly-funding shorts, offset their BTC beta, then match v1 gross."""
    if "BTCUSDT" not in all_columns:
        raise ValueError("BTC hedge requires BTCUSDT prices")
    signal = funding.rolling(7, min_periods=7).sum().where(betas[funding.columns].notna())
    ranks = signal.rank(axis=1, pct=True)
    mask = ranks >= 1 - Q
    count = mask.sum(axis=1).replace(0, np.nan)
    short = mask.div(count, axis=0).fillna(0).mul(-0.5)
    short = short.reindex(columns=all_columns, fill_value=0.0)
    hedge = -(short * betas.reindex_like(short)).sum(axis=1)
    short["BTCUSDT"] = short["BTCUSDT"] + hedge
    gross = short.abs().sum(axis=1).replace(0, np.nan)
    return short.div(gross, axis=0).shift(1).fillna(0.0)


def _stats(daily: pd.Series, turnover: float) -> dict:
    sd = daily.std(ddof=1)
    equity = (1 + daily).cumprod()
    half = len(daily) // 2
    return {
        "n_days": len(daily), "start": str(daily.index.min().date()),
        "sharpe": float(daily.mean() / sd * np.sqrt(365)) if sd > 0 else None,
        "ann_ret_pct": float(daily.mean() * 365 * 100),
        "total_ret_pct": float((equity.iloc[-1] - 1) * 100),
        "max_dd_pct": float((equity / equity.cummax() - 1).min() * 100),
        "half_returns_pct": [float((1 + part).prod() * 100 - 100)
                             for part in (daily.iloc[:half], daily.iloc[half:])],
        "post_2026_04_03_return_pct": float((1 + daily.loc["2026-04-03":]).prod() * 100 - 100),
        "turnover": turnover,
    }


def _aligned(weights: pd.DataFrame, px: pd.DataFrame, fday: pd.DataFrame,
             start: pd.Timestamp, cost: float) -> tuple[pd.Series, float]:
    daily = evaluate(weights, px, fday, cost)["daily"].reindex(px.loc[start:].index).fillna(0.0)
    rets = px.pct_change(fill_method=None)
    actual = weights.where(rets.notna() & px.shift(1).notna(), 0.0)
    turnover = actual.diff().abs().sum(axis=1).loc[start:].mean()
    return daily, float(turnover)


def paired_block_bootstrap(candidate: pd.Series, baseline: pd.Series) -> dict:
    pair = pd.concat([candidate, baseline], axis=1).dropna().to_numpy(dtype=float)
    rng = np.random.default_rng(0)
    n = len(pair)

    def sharpe(x: np.ndarray) -> float:
        sd = x.std(ddof=1)
        return float(x.mean() / sd * np.sqrt(365)) if sd > 0 else float("nan")

    observed = sharpe(pair[:, 0]) - sharpe(pair[:, 1])
    draws = []
    for _ in range(BOOTSTRAPS):
        starts = rng.integers(0, n, size=int(np.ceil(n / BLOCK)))
        indices = np.concatenate([(np.arange(start, start + BLOCK) % n) for start in starts])[:n]
        sample = pair[indices]
        draws.append(sharpe(sample[:, 0]) - sharpe(sample[:, 1]))
    values = np.asarray(draws)
    return {"n": BOOTSTRAPS, "block_days": BLOCK, "observed_delta_sharpe": observed,
            "ci95": np.nanpercentile(values, [2.5, 97.5]).tolist(),
            "fraction_delta_le_zero": float(np.mean(values <= 0))}


def evaluate_universe(symbols: list[str]) -> dict:
    all_names = list(dict.fromkeys([*symbols, "BTCUSDT"]))
    px, fday = build_panel(all_names, 600, min_days=400, use_cache=True, verbose=False)
    if "BTCUSDT" not in px or len(px) < 400:
        raise RuntimeError("BTC hedge or price panel unavailable")
    chosen = [s for s in symbols if s in px]
    betas = rolling_betas(px)
    candidate = candidate_weights(fday[chosen], betas, px.columns)
    baseline = _xs_weights(fday[chosen].rolling(7).sum(), Q, direction=-1).reindex(
        columns=px.columns, fill_value=0.0)
    live = candidate.abs().sum(axis=1).gt(0)
    if not live.any():
        raise RuntimeError("candidate never formed a tradable book")
    start = live.idxmax()
    own, own_turn = _aligned(candidate, px, fday, start, COST)
    old, old_turn = _aligned(baseline, px, fday, start, COST)
    stress, _ = _aligned(candidate, px, fday, start, STRESS)
    executable = candidate.where(px.pct_change(fill_method=None).notna() & px.shift(1).notna(), 0.0)
    beta_at_trade = betas.shift(1).reindex_like(executable)
    ex_ante_beta = (executable * beta_at_trade).sum(axis=1).loc[start:]
    boot = paired_block_bootstrap(own, old)
    return {"symbols_requested": len(symbols), "symbols_used": len(chosen),
            "panel_start": str(px.index.min().date()), "panel_end": str(px.index.max().date()),
            "candidate": _stats(own, own_turn), "v1_same_days": _stats(old, old_turn),
            "candidate_stress_20bps": _stats(stress, own_turn),
            "mean_abs_ex_ante_btc_beta": float(ex_ante_beta.abs().mean()),
            "paired_bootstrap": boot}


def main() -> int:
    if OUT.exists():
        print(f"REFUSING: exploratory comparison already exists at {OUT}")
        return 4
    result = {"generated_utc": datetime.now(timezone.utc).isoformat(),
              "protocol": "RESEARCH_Q1_SHORT_BTC_PROTOCOL.md",
              "status": "EXPLORATORY_ONLY; both historical panels previously inspected",
              "cost_per_leg": COST, "stress_cost_per_leg": STRESS,
              "discovery_history": evaluate_universe(UNIVERSE),
              "disjoint_history_previously_inspected": evaluate_universe(HOLDOUT_UNIVERSE),
              "decision": "NO_V1_CHANGE; prospective shadow evidence required"}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    print(f"-> {OUT}")
    for key in ("discovery_history", "disjoint_history_previously_inspected"):
        row = result[key]
        print(f"{key}: candidate Sharpe {row['candidate']['sharpe']:+.2f}, "
              f"v1 {row['v1_same_days']['sharpe']:+.2f}, "
              f"delta {row['paired_bootstrap']['observed_delta_sharpe']:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
