"""Pre-registered Q4 SMART-VS-CROWD experiment; exports no trading targets.

The discovery panel uses the same frozen 600-day price/funding cache as the other
Q4 cells. Archive metrics must cover those dates. A hold-out run is allowed only
for names that passed every discovery gate, and is never opened twice.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from honest.daily import _xs_weights, build_panel, evaluate
from honest.data import CACHE_DIR
from run_daily_lab import UNIVERSE

ROOT = Path(__file__).resolve().parent
DISCOVERY_REPORT = ROOT / "reports" / "smart_vs_crowd_discovery.json"
HOLDOUT_REPORT = ROOT / "reports" / "smart_vs_crowd_holdout.json"
FIRST_DAY = date(2026, 10, 1)
Q = 0.2
COST = 0.001
STRESS = 0.002
N_PERM = 200
SEED = 0


def load_cached_metrics(px: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Read only discovery/hold-out cache files; never silently shrink the universe."""
    top, crowd = {}, {}
    short_days, missing = {}, []
    for symbol in px.columns:
        path = CACHE_DIR / f"METRICS_{symbol}.parquet"
        if not path.exists():
            missing.append(symbol)
            continue
        frame = pd.read_parquet(path)
        required = {"day", "n_rows", "toptrader_position_ratio", "global_account_ratio"}
        if not required <= set(frame.columns):
            raise ValueError(f"{symbol}: cached metrics missing {sorted(required - set(frame.columns))}")
        frame = frame.copy()
        frame["day"] = pd.to_datetime(frame["day"]).dt.normalize()
        frame = frame.drop_duplicates("day", keep="last").set_index("day").reindex(px.index)
        complete = frame["n_rows"].eq(288)
        short_days[symbol] = int((frame["n_rows"].notna() & ~complete).sum())
        for target, field in ((top, "toptrader_position_ratio"),
                              (crowd, "global_account_ratio")):
            values = pd.to_numeric(frame[field], errors="coerce")
            target[symbol] = values.where(complete & values.gt(0))
    if missing:
        raise RuntimeError(f"metrics cache incomplete for {len(missing)} symbols: {', '.join(missing)}")
    top_panel = pd.DataFrame(top, index=px.index).reindex(columns=px.columns)
    crowd_panel = pd.DataFrame(crowd, index=px.index).reindex(columns=px.columns)
    valid = top_panel.notna() & crowd_panel.notna() & px.notna()
    eligible = [symbol for symbol in px if int(valid[symbol].sum()) >= 400]
    excluded = [symbol for symbol in px if symbol not in eligible]
    if len(eligible) < 20:
        raise RuntimeError(f"only {len(eligible)} symbols have >=400 matched tradeable days")
    info = {"eligible_symbols": eligible, "excluded_symbols": excluded,
            "short_archive_days": {k: v for k, v in short_days.items() if v},
            "valid_symbol_days": int(valid[eligible].sum().sum())}
    return top_panel[eligible], crowd_panel[eligible], info


def residualize_on_funding(disagreement: pd.DataFrame, funding7: pd.DataFrame) -> pd.DataFrame:
    """Daily cross-sectional OLS: rank(D) on rank(funding), using only same-day pairs."""
    paired = disagreement.notna() & funding7.notna()
    y = disagreement.where(paired).rank(axis=1, pct=True)
    x = funding7.where(paired).rank(axis=1, pct=True)
    xc = x.sub(x.mean(axis=1), axis=0)
    yc = y.sub(y.mean(axis=1), axis=0)
    beta = (xc * yc).sum(axis=1) / xc.pow(2).sum(axis=1).replace(0, np.nan)
    return yc.sub(xc.mul(beta, axis=0))


def mean_rank_correlation(a: pd.DataFrame, b: pd.DataFrame) -> float:
    paired = a.notna() & b.notna()
    rank_a = a.where(paired).rank(axis=1, pct=True)
    rank_b = b.where(paired).rank(axis=1, pct=True)
    values = rank_a.corrwith(rank_b, axis=1).dropna()
    return float(values.mean()) if len(values) else float("nan")


def _sharpe(daily: pd.Series) -> float:
    sd = daily.std(ddof=1)
    return float(daily.mean() / sd * np.sqrt(365)) if sd > 0 else float("nan")


def _daily(signal: pd.DataFrame, px: pd.DataFrame, fday: pd.DataFrame,
           cost: float, start: pd.Timestamp | None = None) -> tuple[pd.Series, float]:
    weights = _xs_weights(signal, Q).reindex(columns=px.columns, fill_value=0.0)
    if start is None:
        active = weights.abs().sum(axis=1).gt(0)
        if not active.any():
            raise RuntimeError("signal never produced a long/short book")
        start = active.idxmax()
    result = evaluate(weights, px, fday, cost)
    daily = result["daily"].reindex(px.loc[start:].index).fillna(0.0)
    returns = px.pct_change(fill_method=None)
    executable = weights.where(returns.notna() & px.shift(1).notna(), 0.0)
    turnover = executable.diff().abs().sum(axis=1).loc[start:].mean()
    return daily, float(turnover)


def _stats(daily: pd.Series, turnover: float) -> dict:
    halves = np.array_split(daily.to_numpy(), 2)
    return {"n_days": len(daily), "start": str(daily.index.min().date()),
            "sharpe": _sharpe(daily), "ann_ret_pct": float(daily.mean() * 365 * 100),
            "total_ret_pct": float(((1 + daily).prod() - 1) * 100),
            "half_returns_pct": [float(((1 + part).prod() - 1) * 100) for part in halves],
            "turnover": turnover}


def _shuffle_columns(frame: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    return pd.DataFrame(frame.to_numpy()[:, rng.permutation(frame.shape[1])],
                        index=frame.index, columns=frame.columns)


def _null(signal_builder, px: pd.DataFrame, fday: pd.DataFrame,
          observed_gross: float, observed_net: float, start: pd.Timestamp,
          rng: np.random.Generator) -> dict:
    gross, net, turnovers = [], [], []
    for _ in range(N_PERM):
        signal = signal_builder(rng)
        g, _ = _daily(signal, px, fday, 0.0, start)
        n, turnover = _daily(signal, px, fday, COST, start)
        gross.append(_sharpe(g))
        net.append(_sharpe(n))
        turnovers.append(turnover)
    gross, net = np.asarray(gross), np.asarray(net)
    return {"n": N_PERM,
            "p_gross": float((1 + np.count_nonzero(gross >= observed_gross)) / (N_PERM + 1)),
            "p_net": float((1 + np.count_nonzero(net >= observed_net)) / (N_PERM + 1)),
            "mean_turnover": float(np.mean(turnovers)),
            "net_sharpe_p95": float(np.nanpercentile(net, 95))}


def evaluate_universe(symbols: list[str], *, cell_names: list[str] | None = None,
                      holdout: bool = False, verbose: bool = True) -> dict:
    px, fday = build_panel(symbols, 600, min_days=400, use_cache=True, verbose=False)
    if px.empty or px.index.max().date() >= date.today():
        raise RuntimeError("price panel missing or contains an incomplete UTC day")
    top, crowd, info = load_cached_metrics(px)
    px, fday = px[top.columns], fday[top.columns]
    top, crowd = top.where(px.notna()), crowd.where(px.notna())
    disagreement = np.log(top) - np.log(crowd)
    funding7 = fday.rolling(7, min_periods=7).sum()
    signals = {"D7": disagreement.rolling(7, min_periods=7).mean(),
               "D30": disagreement.rolling(30, min_periods=30).mean(),
               "RESIDUAL": residualize_on_funding(disagreement.rolling(7, min_periods=7).mean(), funding7),
               "CROWD_ONLY": -np.log(crowd).rolling(7, min_periods=7).mean()}
    if cell_names is not None and not set(cell_names) <= {"D7", "D30"}:
        raise ValueError("hold-out may only evaluate discovery's raw D nominees")
    selected = set(cell_names or ("D7", "D30"))
    top_crowd_rho = mean_rank_correlation(top, crowd)
    rng = np.random.default_rng(SEED)
    cells = {}
    carry_signal = funding7
    carry_W = _xs_weights(carry_signal, Q, direction=-1).reindex(columns=px.columns, fill_value=0.0)
    for name, signal in signals.items():
        if holdout and name in {"D7", "D30"} and name not in selected:
            continue
        net, turnover = _daily(signal, px, fday, COST)
        start = net.index.min()
        gross, _ = _daily(signal, px, fday, 0.0, start)
        stress, _ = _daily(signal, px, fday, STRESS, start)
        carry = evaluate(carry_W, px, fday, COST)["daily"].reindex(net.index).fillna(0.0)
        blend_W = (0.5 * carry_W + 0.5 * _xs_weights(signal, Q).reindex(columns=px.columns, fill_value=0.0))
        blend = evaluate(blend_W, px, fday, COST)["daily"].reindex(net.index).fillna(0.0)
        rho_funding = mean_rank_correlation(signal, funding7)
        row = {"net": _stats(net, turnover), "gross_sharpe": _sharpe(gross),
               "stress_ann_ret_pct": float(stress.mean() * 365 * 100),
               "rho_funding_rank": rho_funding, "rho_top_crowd_rank": top_crowd_rho,
               "carry_sharpe_same_days": _sharpe(carry), "blend_sharpe": _sharpe(blend),
               "blend_gain": _sharpe(blend) - _sharpe(carry)}
        cells[name] = row
        if verbose:
            print(f"  {name:10s} gross Sharpe {row['gross_sharpe']:+.2f}  net {row['net']['sharpe']:+.2f} "
                  f"rho funding {rho_funding:+.2f}  blend gain {row['blend_gain']:+.2f}")

    # Pre-registered cheap kills are evaluated before any expensive null or hold-out.
    raw_names = [name for name in ("D7", "D30") if name in cells]
    cheap_ok = {name: bool(cells[name]["gross_sharpe"] >= 0.5
                           and cells[name]["rho_funding_rank"] <= 0.6
                           and top_crowd_rho <= 0.9) for name in raw_names}
    for name in raw_names:
        cells[name]["cheap_checks"] = {
            "gross_sharpe_ge_0_5": cells[name]["gross_sharpe"] >= 0.5,
            "rank_D_funding_le_0_6": cells[name]["rho_funding_rank"] <= 0.6,
            "rank_top_crowd_le_0_9": top_crowd_rho <= 0.9,
        }
    if not any(cheap_ok.values()):
        return _report(px, info, cells, holdout, [], "STOP_CHEAP_GATE")

    for name in [*raw_names, "RESIDUAL", "CROWD_ONLY"]:
        if name not in cells or (name in raw_names and not cheap_ok[name]):
            continue
        observed = cells[name]
        if name == "D7":
            builder = lambda r: _shuffle_columns(signals["D7"], r)
        elif name == "D30":
            builder = lambda r: _shuffle_columns(signals["D30"], r)
        elif name == "RESIDUAL":
            builder = lambda r: residualize_on_funding(_shuffle_columns(signals["D7"], r), funding7)
        else:
            builder = lambda r: _shuffle_columns(signals["CROWD_ONLY"], r)
        observed["null"] = _null(builder, px, fday, observed["gross_sharpe"],
                                 observed["net"]["sharpe"],
                                 pd.Timestamp(observed["net"]["start"]), rng)
        if name in raw_names:
            window = 7 if name == "D7" else 30
            pair_builder = lambda r: (np.log(_shuffle_columns(top, r)) - np.log(crowd)).rolling(
                window, min_periods=window).mean()
            observed["pair_null"] = _null(pair_builder, px, fday,
                                           observed["gross_sharpe"], observed["net"]["sharpe"],
                                           pd.Timestamp(observed["net"]["start"]), rng)

    residual = cells["RESIDUAL"]
    residual_ok = bool("null" in residual and residual["net"]["sharpe"] > (0.5 if holdout else 0.7)
                       and (holdout or residual["null"]["p_net"] < 0.05))
    nominees = []
    for name in raw_names:
        row = cells[name]
        if "null" not in row:
            continue
        checks = {"gross_sharpe_ge_0_5": row["gross_sharpe"] >= 0.5,
                  "rank_D_funding_le_0_6": row["rho_funding_rank"] <= 0.6,
                  "rank_top_crowd_le_0_9": top_crowd_rho <= 0.9,
                  "raw_net_sharpe": row["net"]["sharpe"] > (0.7 if holdout else 1.0),
                  "gross_null_p": holdout or row["null"]["p_gross"] < 0.01,
                  "net_null_p": holdout or row["null"]["p_net"] < 0.05,
                  "pair_null_p": row["pair_null"]["p_net"] < 0.05,
                  "residual_sharpe_and_p": residual_ok,
                  "both_halves_positive": all(v > 0 for v in row["net"]["half_returns_pct"]),
                  "stress_20bps_positive": row["stress_ann_ret_pct"] > 0,
                  "blend_gain_ge_0_20": row["blend_gain"] >= 0.20}
        row["checks"] = checks
        row["passed"] = all(checks.values())
        if row["passed"]:
            nominees.append(name)
    crowd_control = cells["CROWD_ONLY"]
    crowd_control["control_pass"] = bool(
        "null" in crowd_control and crowd_control["net"]["sharpe"] > 1.0
        and crowd_control["null"]["p_gross"] < 0.01
        and crowd_control["null"]["p_net"] < 0.05
        and all(v > 0 for v in crowd_control["net"]["half_returns_pct"])
        and crowd_control["stress_ann_ret_pct"] > 0
        and crowd_control["blend_gain"] >= 0.20)
    decision = ("PASS" if nominees else "FADE_CROWD_ONLY" if crowd_control["control_pass"]
                else "STOP_HOLDOUT" if holdout else "STOP_DISCOVERY")
    return _report(px, info, cells, holdout, nominees, decision)


def _report(px: pd.DataFrame, info: dict, cells: dict, holdout: bool,
            nominees: list[str], decision: str) -> dict:
    return {"generated_utc": datetime.now(timezone.utc).isoformat(),
            "protocol": "RESEARCH_PREREG_Q4_2026.md cell 3",
            "universe": "holdout" if holdout else "discovery",
            "panel_start": str(px.index.min().date()), "panel_end": str(px.index.max().date()),
            "panel_days": len(px), "n_perm": N_PERM, "cost_per_leg": COST,
            "null_interpretation": "p_gross tests zero-cost Sharpe; p_net tests 10bps/leg Sharpe",
            "data": info, "cells": cells, "nominees": nominees, "decision": decision}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--holdout", action="store_true")
    args = parser.parse_args()
    if date.today() < FIRST_DAY:
        raise RuntimeError(f"SMART-VS-CROWD is registered for {FIRST_DAY} or later")
    source_path = ROOT / "reports" / "smart_vs_crowd_source_check.json"
    if not source_path.exists() or not json.loads(source_path.read_text(encoding="utf-8")).get("passes"):
        print("REFUSING: archive/REST source check must pass before opening strategy results")
        return 4
    if args.holdout:
        if HOLDOUT_REPORT.exists() or not DISCOVERY_REPORT.exists():
            print("REFUSING: hold-out already opened or discovery report missing")
            return 4
        discovery = json.loads(DISCOVERY_REPORT.read_text(encoding="utf-8"))
        if not discovery.get("nominees"):
            print("REFUSING: no discovery nominee; hold-out stays closed")
            return 4
        from run_carry_holdout import HOLDOUT_UNIVERSE
        symbols = HOLDOUT_UNIVERSE
        cell_names = discovery["nominees"]
    else:
        symbols, cell_names = UNIVERSE, None
    result = evaluate_universe(symbols, cell_names=cell_names, holdout=args.holdout)
    path = HOLDOUT_REPORT if args.holdout else DISCOVERY_REPORT
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    print(f"-> {path}  decision={result['decision']}  nominees={result['nominees']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
