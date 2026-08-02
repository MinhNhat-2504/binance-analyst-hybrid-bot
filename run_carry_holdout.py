"""Hold-out confirmation for the CARRY candidate - the pre-committed gate, step (4).

CARRY-7d passed the in-sample gate (sharpe 1.83, p=0.0050 < Bonferroni 0.0071). This script
attacks it from the directions the grid could not:

  1. DISJOINT UNIVERSE - 30 established perps sharing no symbol with the discovery set.
     Skill that lives in the mechanism (funding persistence) must transfer; skill that
     lives in the particular 42 names must not.
  2. SPLIT-HALF STABILITY - same rule, first vs second half of the sample. A carry premium
     that exists only in one regime is a regime bet, not a strategy.
  3. COST STRESS - 10 vs 20bps/leg. Shorting the most crowded names is where slippage
     lives; the candidate must survive doubled friction.
  4. PnL DECOMPOSITION - funding leg vs price leg. Real carry earns its funding leg and
     bleeds a little on price; a result driven by the PRICE leg of a funding-sorted
     portfolio would be momentum in disguise and should be judged as such.

No new knobs are introduced here: q=0.2 and the {3,7}d lookbacks are carried over verbatim.
Tuning anything on the hold-out would convert it into a second training set.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from honest.daily import (
    _xs_weights, build_panel, evaluate, permutation_null_sharpe,
)

# Disjoint from run_daily_lab.UNIVERSE - no overlap, similar era of listing.
HOLDOUT_UNIVERSE = [
    "ICPUSDT", "HBARUSDT", "ALGOUSDT", "VETUSDT", "EGLDUSDT", "THETAUSDT",
    "XTZUSDT", "NEOUSDT", "IOTAUSDT", "KAVAUSDT", "ZILUSDT", "DYDXUSDT",
    "GMTUSDT", "APEUSDT", "LDOUSDT", "IMXUSDT", "STXUSDT", "FLOWUSDT",
    "CHZUSDT", "SNXUSDT", "COMPUSDT", "YFIUSDT", "SUSHIUSDT", "1INCHUSDT",
    "ENJUSDT", "DASHUSDT", "ZECUSDT", "GRTUSDT", "MINAUSDT", "ROSEUSDT",
    "ARUSDT", "CELOUSDT", "QNTUSDT",
]

MAIN_UNIVERSE_REF = "run_daily_lab.py UNIVERSE (42 symbols)"


def carry_builder(sig: pd.DataFrame) -> pd.DataFrame:
    return _xs_weights(sig, q=0.2, direction=-1)


def decompose(W: pd.DataFrame, px: pd.DataFrame, fday: pd.DataFrame,
              cost_per_leg: float) -> dict:
    """evaluate() plus the funding/price/cost split and split-half sharpes."""
    rets = px.pct_change(fill_method=None)
    W = W.where(rets.notna() & px.shift(1).notna(), 0.0)
    price = (W * rets).sum(axis=1)
    fund = (-W * fday).sum(axis=1)
    turn = (W - W.shift(1).fillna(0.0)).abs().sum(axis=1)
    daily = (price + fund - turn * cost_per_leg).fillna(0.0)

    live = W.abs().sum(axis=1) > 0
    if live.any():
        start = live.idxmax()
        daily, price, fund, turn = (s[start:] for s in (daily, price, fund, turn))

    def sharpe(s):
        return float(s.mean() / s.std(ddof=1) * np.sqrt(365)) if s.std(ddof=1) > 0 else float("nan")

    half = len(daily) // 2
    equity = (1 + daily).cumprod()
    return {
        "n_days": int(len(daily)),
        "sharpe": sharpe(daily),
        "sharpe_h1": sharpe(daily.iloc[:half]),
        "sharpe_h2": sharpe(daily.iloc[half:]),
        "ann_ret_pct": float(daily.mean() * 365 * 100),
        "max_dd_pct": float(((equity / equity.cummax()) - 1).min() * 100),
        "funding_leg_ann_pct": float(fund.mean() * 365 * 100),
        "price_leg_ann_pct": float(price.mean() * 365 * 100),
        "cost_drag_ann_pct": float((turn * cost_per_leg).mean() * 365 * 100),
        "avg_turnover": float(turn.mean()),
    }


def run_universe(tag: str, symbols: list[str], days: int, n_perm: int) -> dict:
    print(f"\n{'=' * 84}\nUNIVERSE: {tag}\n{'=' * 84}")
    px, fday = build_panel(symbols, days, verbose=False)
    print(f"  panel: {px.shape[1]} symbols x {px.shape[0]} days")
    if px.shape[1] < 10:
        print("  too few symbols")
        return {}

    out = {"n_symbols": int(px.shape[1])}
    for lb in (3, 7):
        name = f"CARRY-{lb}d"
        sig = fday.rolling(lb).sum()
        W = carry_builder(sig)
        cells = {}
        for cost in (0.0010, 0.0020):
            d = decompose(W, px, fday, cost)
            key = f"{int(cost * 1e4)}bps"
            cells[key] = d
            if cost == 0.0010:
                nl = permutation_null_sharpe(carry_builder, sig, px, fday,
                                             d["sharpe"], n_perm=n_perm)
                cells[key]["p_value"] = nl["p_value"]
                cells[key]["null_p95"] = nl["p95"]
            print(f"  {name} @{key}: sharpe={d['sharpe']:+5.2f} "
                  f"(h1 {d['sharpe_h1']:+5.2f} | h2 {d['sharpe_h2']:+5.2f}) "
                  f"ann={d['ann_ret_pct']:+6.1f}% dd={d['max_dd_pct']:5.1f}% "
                  f"| legs: fund {d['funding_leg_ann_pct']:+6.1f}% "
                  f"price {d['price_leg_ann_pct']:+6.1f}% cost -{d['cost_drag_ann_pct']:.1f}%"
                  + (f" | p={cells[key]['p_value']:.4f}" if "p_value" in cells[key] else ""))
        out[name] = cells
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=600)
    ap.add_argument("--n-perm", type=int, default=200)
    ap.add_argument("--out", default="carry_holdout_report.json")
    args = ap.parse_args()

    from run_daily_lab import UNIVERSE as MAIN

    overlap = set(MAIN) & set(HOLDOUT_UNIVERSE)
    assert not overlap, f"hold-out contaminated by discovery symbols: {overlap}"

    report = {"generated_utc": datetime.now(timezone.utc).isoformat(),
              "config": vars(args),
              "universes": {}}
    report["universes"]["discovery"] = run_universe(
        f"DISCOVERY ({MAIN_UNIVERSE_REF})", MAIN, args.days, args.n_perm)
    report["universes"]["holdout"] = run_universe(
        f"HOLD-OUT ({len(HOLDOUT_UNIVERSE)} disjoint symbols)",
        HOLDOUT_UNIVERSE, args.days, args.n_perm)

    print(f"\n{'=' * 84}\nREADING\n{'=' * 84}")
    d7 = report["universes"].get("discovery", {}).get("CARRY-7d", {}).get("10bps", {})
    h7 = report["universes"].get("holdout", {}).get("CARRY-7d", {}).get("10bps", {})
    if d7 and h7:
        print(f"  CARRY-7d discovery: sharpe {d7['sharpe']:+.2f} (p={d7.get('p_value', float('nan')):.4f})  "
              f"halves {d7['sharpe_h1']:+.2f}/{d7['sharpe_h2']:+.2f}")
        print(f"  CARRY-7d hold-out : sharpe {h7['sharpe']:+.2f} (p={h7.get('p_value', float('nan')):.4f})  "
              f"halves {h7['sharpe_h1']:+.2f}/{h7['sharpe_h2']:+.2f}")
        transfers = h7["sharpe"] > 0.5 and h7.get("p_value", 1.0) < 0.05
        stable = min(d7["sharpe_h1"], d7["sharpe_h2"], h7["sharpe_h1"], h7["sharpe_h2"]) > 0
        carry_driven = h7["funding_leg_ann_pct"] > 0 and d7["funding_leg_ann_pct"] > 0
        print(f"\n  transfers to disjoint universe : {'YES' if transfers else 'NO'}")
        print(f"  positive in all four halves    : {'YES' if stable else 'NO'}")
        print(f"  funding leg positive both      : {'YES' if carry_driven else 'NO'}")
        if transfers and stable:
            print("\n  Hold-out PASSES. Next gate: adversarial code audit verdict, then paper.")
        else:
            print("\n  Hold-out FAILS or is unstable. The discovery result does not generalise;")
            print("  treat CARRY-7d as regime/universe-specific until proven otherwise.")

    Path(args.out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\n  report -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
