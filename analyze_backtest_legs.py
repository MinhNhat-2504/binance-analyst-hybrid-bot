"""Does the short leg of CARRY-7d earn anything over 600 days, or only the long leg?

Prompted by the paper attribution of 2026-09-26: 52 days in, the long leg made +22.6%
and the short leg lost 14.6%. Fifty-two days cannot settle that. The 600-day panels can
say whether the short leg is structurally weak (then half the book pays costs for
nothing and that is the biggest lever left) or whether September is noise (then the leg
stays exactly as it is). Descriptive only: it decomposes the strategy that is already
running; it evaluates no variant and consumes no research slot.

Per universe (discovery 42, hold-out 33), per block (full, first half, second half, pre
and post the 2026-04-03 cutoff): long-leg and short-leg annualised return, price and
funding parts, and each leg's own Sharpe. Costs are split by the leg that traded.

    python analyze_backtest_legs.py            -> reports/backtest_legs.json
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

from honest.daily import _xs_weights, build_panel  # noqa: E402
from run_carry_holdout import HOLDOUT_UNIVERSE  # noqa: E402
from run_daily_lab import UNIVERSE  # noqa: E402

COST = 0.0010
CUTOFF = pd.Timestamp("2026-04-03")
OUT = ROOT / "reports" / "backtest_legs.json"


def leg_series(px: pd.DataFrame, fday: pd.DataFrame) -> dict[str, pd.Series]:
    """Daily contributions split by side, mirroring honest.daily.evaluate's accounting."""
    W = _xs_weights(fday.rolling(7).sum(), q=0.2, direction=-1)
    rets = px.pct_change(fill_method=None)
    W = W.where(rets.notna() & px.shift(1).notna(), 0.0)
    prev = W.shift(1).fillna(0.0)
    side = np.sign(W).where(W != 0, np.sign(prev))          # exit day charged to the side that held
    price = W * rets
    funding = -W * fday
    cost = -(W - prev).abs() * COST
    out = {}
    for name, mask in (("long", side > 0), ("short", side < 0)):
        out[f"{name}_price"] = price.where(mask, 0.0).sum(axis=1)
        out[f"{name}_funding"] = funding.where(mask, 0.0).sum(axis=1)
        out[f"{name}_cost"] = cost.where(mask, 0.0).sum(axis=1)
        out[f"{name}_total"] = out[f"{name}_price"] + out[f"{name}_funding"] + out[f"{name}_cost"]
    out["total"] = out["long_total"] + out["short_total"]
    live = W.abs().sum(axis=1) > 0
    start = live.idxmax()
    return {k: v[start:].fillna(0.0) for k, v in out.items()}


def stats(s: pd.Series) -> dict[str, float]:
    mu, sd = s.mean(), s.std(ddof=1)
    return {"ann_pct": round(float(mu * 365 * 100), 2),
            "sharpe": round(float(mu / sd * math.sqrt(365)), 2) if sd > 0 else float("nan"),
            "n_days": int(len(s))}


def blocks(series: dict[str, pd.Series]) -> dict[str, dict]:
    idx = series["total"].index
    mid = idx[len(idx) // 2]
    spans = {"full": (idx[0], idx[-1]), "h1": (idx[0], mid), "h2": (mid, idx[-1]),
             "pre_cutoff": (idx[0], CUTOFF), "post_cutoff": (CUTOFF, idx[-1])}
    out = {}
    for name, (a, b) in spans.items():
        sl = slice(a, b)
        out[name] = {
            "long": stats(series["long_total"][sl]), "short": stats(series["short_total"][sl]),
            "total": stats(series["total"][sl]),
            "long_price_ann_pct": round(float(series["long_price"][sl].mean() * 365 * 100), 2),
            "long_funding_ann_pct": round(float(series["long_funding"][sl].mean() * 365 * 100), 2),
            "short_price_ann_pct": round(float(series["short_price"][sl].mean() * 365 * 100), 2),
            "short_funding_ann_pct": round(float(series["short_funding"][sl].mean() * 365 * 100), 2),
            "short_share_of_total_pct": round(float(series["short_total"][sl].sum() / series["total"][sl].sum() * 100), 1)
            if series["total"][sl].sum() != 0 else None,
        }
    return out


def main() -> int:
    report = {}
    for tag, symbols in (("discovery", UNIVERSE), ("holdout", HOLDOUT_UNIVERSE)):
        px, fday = build_panel(symbols, 600, verbose=False)
        series = leg_series(px, fday)
        report[tag] = blocks(series)
        print(f"\n== {tag} ({px.shape[1]} symbols) ==")
        print(f"  {'block':12s} {'LONG ann%':>10s} {'sh':>6s} {'SHORT ann%':>11s} {'sh':>6s}   {'short: price/funding':>22s}   {'total ann%':>10s}")
        for name, b in report[tag].items():
            print(f"  {name:12s} {b['long']['ann_pct']:+10.1f} {b['long']['sharpe']:+6.2f} {b['short']['ann_pct']:+11.1f} {b['short']['sharpe']:+6.2f}"
                  f"   {b['short_price_ann_pct']:+9.1f} / {b['short_funding_ann_pct']:+7.1f}   {b['total']['ann_pct']:+10.1f}")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n-> {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
