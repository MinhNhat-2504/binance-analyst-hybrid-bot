"""Why do honest/ and clean_research/ disagree about CARRY-7d?

honest/  : 592 days, Sharpe 1.78, column-permutation p = 0.005.
clean/   : the same strategy cut into blocks at 2026-04-03; every post-cutoff block has a
           HAC 95% CI on mean bps/day that crosses zero, and the double hold-out shift
           p = 0.18.

Same 42 discovery symbols, same 33 hold-out symbols, same data end (2026-07-31), same
10 bps/leg. So the disagreement is about the STATISTIC and the BLOCK LENGTH, not about the
strategy. This script makes that concrete with numbers rather than an argument:

  1. Rebuild the honest/ daily return series and cut it at the same 2026-04-03 boundary.
     Report mean bps/day and the same HAC CI clean_research uses, per block.
  2. Ask the power question directly: if the true edge equals the full-sample mean, how
     wide is a 120-day CI expected to be, how often does it cross zero, and how many days
     would a block need before its lower bound clears zero 80% of the time.
  3. Put clean_research's own numbers beside honest's block numbers.

Read-only on the cache; no network if the panels are cached. Not a research cell: it
tests the two evaluators against each other, it does not evaluate a new strategy.

    python reconcile_evaluators.py            -> prints, writes reports/evaluator_reconciliation.json
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from clean_research.carry import _hac_mean_ci  # noqa: E402  (the exact CI clean/ reports)
from honest.daily import _xs_weights, build_panel, evaluate  # noqa: E402
from run_carry_holdout import HOLDOUT_UNIVERSE  # noqa: E402
from run_daily_lab import UNIVERSE  # noqa: E402

CUTOFF = pd.Timestamp("2026-04-03")
COST = 0.0010
OUT = ROOT / "reports" / "evaluator_reconciliation.json"
CLEAN_REPORT = ROOT / "reports" / "clean_oos_report.json"


def carry_daily(symbols: list[str], days: int = 600) -> pd.Series:
    px, fday = build_panel(symbols, days, verbose=False)
    W = _xs_weights(fday.rolling(7).sum(), q=0.2, direction=-1)
    return evaluate(W, px, fday, cost_per_leg=COST)["daily"]


def block_stats(daily: pd.Series) -> dict[str, float]:
    x = daily.dropna()
    lo, hi, t = _hac_mean_ci(x * 1e4)
    mu, sd = x.mean(), x.std(ddof=1)
    return {
        "n_days": int(len(x)),
        "mean_bps_day": round(float(mu * 1e4), 2),
        "hac_ci_lo_bps": round(float(lo), 2), "hac_ci_hi_bps": round(float(hi), 2),
        "hac_t": round(float(t), 2),
        "sharpe": round(float(mu / sd * math.sqrt(365)), 2) if sd > 0 else float("nan"),
        "ci_crosses_zero": bool(lo <= 0 <= hi),
    }


def power(mu_bps: float, sd_bps: float, n: int, *, hac_inflation: float) -> dict[str, float]:
    """Expected CI half-width and P(lower bound > 0) for a block of n days.

    hac_inflation is the ratio of the HAC standard error to the iid one, measured on the
    full sample, so autocorrelation is carried into the block prediction.
    """
    se = sd_bps / math.sqrt(n) * hac_inflation
    half = 1.96 * se
    z = mu_bps / se
    # P(estimated lower bound > 0) = P(estimate > 1.96 se) = 1 - Phi(1.96 - mu/se)
    from math import erf, sqrt
    phi = lambda v: 0.5 * (1 + erf(v / sqrt(2)))  # noqa: E731
    return {"n_days": n, "expected_half_width_bps": round(half, 2), "z": round(z, 2),
            "p_lower_bound_positive": round(1 - phi(1.96 - z), 3)}


def days_for_power(mu_bps: float, sd_bps: float, hac_inflation: float, target: float = 0.8) -> int:
    # lower bound > 0 with prob target  <=>  mu/se >= 1.96 + z_target
    z_t = 0.8416  # Phi^-1(0.8)
    se_needed = mu_bps / (1.96 + z_t)
    return int(math.ceil((sd_bps * hac_inflation / se_needed) ** 2))


def clean_numbers() -> dict:
    """clean_research's carry partitions, as it reported them. Missing keys read as None."""
    if not CLEAN_REPORT.exists():
        return {}
    d = json.loads(CLEAN_REPORT.read_text(encoding="utf-8"))
    route = d.get("routes", {}).get("carry_7d", {})
    out = {}
    for section in ("partitions", "historical_partitions", "replay"):
        for name, cells in route.get(section, {}).items():
            cell = cells.get("10bps_per_leg") if isinstance(cells, dict) else None
            if isinstance(cell, dict):
                out[name] = {k: cell.get(k) for k in ("n_days", "mean_bps_day", "hac_ci_lo_bps", "hac_ci_hi_bps", "permutation_p", "sharpe")}
    return out


def main() -> int:
    result: dict = {"cutoff": str(CUTOFF.date()), "cost_per_leg_bps": COST * 1e4, "universes": {}}
    for tag, symbols in (("discovery", UNIVERSE), ("holdout", HOLDOUT_UNIVERSE)):
        daily = carry_daily(symbols)
        pre, post = daily[daily.index < CUTOFF], daily[daily.index >= CUTOFF]
        full = block_stats(daily)
        x = daily.dropna() * 1e4
        iid_se = x.std(ddof=1) / math.sqrt(len(x))
        lo, hi, t = _hac_mean_ci(x)
        hac_se = (hi - lo) / (2 * 1.96)
        inflation = float(hac_se / iid_se) if iid_se > 0 else 1.0
        mu, sd = float(x.mean()), float(x.std(ddof=1))
        blocks = {"full": full, "pre_cutoff": block_stats(pre), "post_cutoff": block_stats(post)}
        pw = {f"block_{n}d": power(mu, sd, n, hac_inflation=inflation) for n in (60, 120, 240, 592)}
        need = days_for_power(mu, sd, inflation)
        result["universes"][tag] = {
            "n_symbols": len(symbols), "blocks": blocks,
            "full_sample_mean_bps": round(mu, 2), "full_sample_sd_bps": round(sd, 2),
            "hac_se_inflation": round(inflation, 3),
            "power_if_true_edge_equals_full_mean": pw,
            "days_needed_for_80pct_power": need,
        }
        print(f"\n== {tag} ({len(symbols)} symbols) ==")
        for name, b in blocks.items():
            print(f"  {name:12s} n={b['n_days']:4d}  mean {b['mean_bps_day']:+7.2f} bps/d  "
                  f"HAC95 [{b['hac_ci_lo_bps']:+7.2f}, {b['hac_ci_hi_bps']:+7.2f}]  t={b['hac_t']:+5.2f}  "
                  f"sharpe {b['sharpe']:+5.2f}  {'CI CROSSES 0' if b['ci_crosses_zero'] else 'CI clear of 0'}")
        print(f"  full-sample mean {mu:+.2f} bps/d, sd {sd:.1f} bps/d, HAC/iid se ratio {inflation:.2f}")
        print("  if the TRUE edge equals that mean:")
        for k, v in pw.items():
            print(f"    {k:11s} expected CI half-width {v['expected_half_width_bps']:5.1f} bps  "
                  f"P(lower bound > 0) = {v['p_lower_bound_positive']:.2f}")
        print(f"  days for the lower bound to clear 0 with 80% power: {need}")

    clean = clean_numbers()
    result["clean_research_reported"] = clean
    if clean:
        print("\n== clean_research/ as reported (10 bps/leg) ==")
        for name, c in clean.items():
            print(f"  {name:28s} n={c.get('n_days')}  mean {c.get('mean_bps_day')}  CI [{c.get('hac_ci_lo_bps')}, {c.get('hac_ci_hi_bps')}]  shift p {c.get('permutation_p')}")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2, default=float), encoding="utf-8")
    print(f"\n-> {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
