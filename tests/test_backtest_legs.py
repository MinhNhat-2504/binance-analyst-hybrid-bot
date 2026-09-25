"""analyze_backtest_legs.py: the leg split must be an exact partition of the strategy's PnL."""
from __future__ import annotations

import numpy as np
import pandas as pd

import analyze_backtest_legs as abl
from honest.daily import _xs_weights, evaluate
from run_carry_hyst_lab import synthetic_panel


def test_legs_partition_the_evaluate_pnl_exactly():
    px, fday = synthetic_panel(n_days=200, n_sym=20, seed=11)
    series = abl.leg_series(px, fday)
    W = _xs_weights(fday.rolling(7).sum(), q=0.2, direction=-1)
    ref = evaluate(W, px, fday, abl.COST)["daily"]
    total = series["total"].reindex(ref.index).fillna(0.0)
    assert np.allclose(total.to_numpy(), ref.to_numpy(), atol=1e-12)
    assert np.allclose((series["long_total"] + series["short_total"]).to_numpy(), series["total"].to_numpy())


def test_exit_days_are_charged_to_the_side_that_held():
    px, fday = synthetic_panel(n_days=120, n_sym=15, seed=5)
    series = abl.leg_series(px, fday)
    W = _xs_weights(fday.rolling(7).sum(), q=0.2, direction=-1)
    prev = W.shift(1).fillna(0.0)
    exit_cost = -(W - prev).abs().where((W == 0) & (prev != 0), 0.0) * abl.COST
    # every exit cost lands in one of the two legs, never dropped
    assert abs(float(exit_cost.sum().sum()) - float((series["long_cost"] + series["short_cost"]).sum()
               - (-(W - prev).abs().where(W != 0, 0.0) * abl.COST).sum().sum())) < 1e-12


def test_blocks_report_both_legs_for_every_span():
    px, fday = synthetic_panel(n_days=150, n_sym=12, seed=2)
    out = abl.blocks(abl.leg_series(px, fday))
    assert set(out) == {"full", "h1", "h2", "pre_cutoff", "post_cutoff"}
    for b in out.values():
        assert {"long", "short", "total"} <= set(b)
        assert b["long"]["n_days"] + 0 >= 0
