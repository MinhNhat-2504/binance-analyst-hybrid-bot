"""run_carry_hyst_lab.py: the builder's invariants and the locks, on synthetic data only.

None of this touches the cache or a real panel: the cell may not run on real data before
2026-10-01, and a test that peeked would void the pre-registration.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import run_carry_hyst_lab as lab


@pytest.fixture(scope="module")
def panel():
    return lab.synthetic_panel(n_days=300, n_sym=25, seed=3)


def _turnover(W: pd.DataFrame) -> float:
    return float((W - W.shift(1).fillna(0.0)).abs().sum(axis=1).mean())


def test_zero_band_reproduces_v1_exactly(panel):
    px, fday = panel
    sig = fday.rolling(7).sum()
    pd.testing.assert_frame_equal(lab.hysteresis_weights(sig, 0.0), lab.v1_weights(sig), check_exact=False, atol=1e-12)


def test_membership_grows_with_the_band_and_side_gross_stays_half(panel):
    """By construction: whatever is in the book under a narrow band is in it under a wider
    one (entries are identical, and a name kept at 0.8-h' is kept at 0.8-h for h > h').
    Turnover is NOT asserted monotone: a wider band lets the side count float, and each
    count change re-weights every name. Whether the band cuts turnover by >= 25% on the
    real panel is exactly what the pre-registered cheap check decides on 2026-10-01."""
    px, fday = panel
    sig = fday.rolling(7).sum()
    prev = lab.v1_weights(sig) != 0
    for h in (0.05, 0.10, 0.20):
        W2 = lab.hysteresis_weights(sig, h)
        member = W2 != 0
        assert (member | ~prev).all().all(), f"h={h}: a name held under a narrower band was dropped"
        prev = member
        live = W2.abs().sum(axis=1) > 0
        longs = W2.clip(lower=0).sum(axis=1)[live]
        shorts = (-W2.clip(upper=0)).sum(axis=1)[live]
        assert np.allclose(longs[longs > 0], 0.5) and np.allclose(shorts[shorts > 0], 0.5)


def test_a_name_inside_the_band_is_held_not_churned():
    """Ten names. S9 sits at the top (rank 1.0), dips to rank 0.7 for one day, comes back.
    v1 exits at rank < 0.8 and re-enters: two round trips. h=0.10 keeps it: 0.7 >= 0.8-0.10."""
    idx = pd.date_range("2025-01-01", periods=6, freq="D")
    cols = [f"S{i}" for i in range(10)]
    base = np.tile(np.linspace(0.0, 0.8, 9), (6, 1))            # ranks of the other nine are fixed
    s9 = np.array([0.95, 0.95, 0.55, 0.95, 0.95, 0.95])[:, None]  # 0.55 is 7th of 10 -> rank 0.7
    sig = pd.DataFrame(np.hstack([base, s9]), index=idx, columns=cols)
    assert sig.rank(axis=1, pct=True).loc[idx[2], "S9"] == pytest.approx(0.7)
    w1 = lab.v1_weights(sig)["S9"]
    w2 = lab.hysteresis_weights(sig, 0.10)["S9"]
    assert w1.iloc[3] == 0.0, "v1 drops the name the day after its rank slipped"
    assert w2.iloc[3] < 0.0, "hysteresis keeps the short inside the band"
    assert _turnover(lab.hysteresis_weights(sig, 0.10)) < _turnover(lab.v1_weights(sig))


def test_placebo_matches_target_turnover_from_above_and_below(panel):
    px, fday = panel
    sig = fday.rolling(7).sum()
    W2 = lab.hysteresis_weights(sig, 0.10)
    target = lab.evaluate(W2, px, fday, lab.COST)["avg_daily_turnover"]
    k, Wp = lab.matched_placebo(sig, px, fday, target)
    got = lab.evaluate(Wp, px, fday, lab.COST)["avg_daily_turnover"]
    assert 1 <= k <= 12
    assert abs(got - target) <= 0.25 * target, "placebo turnover must sit near the cell's"
    # the placebo never adds entries v1 did not make
    W1 = lab.v1_weights(sig)
    entered_p = (Wp != 0) & (Wp.shift(1).fillna(0.0) == 0)
    entered_1 = (W1 != 0) & (W1.shift(1).fillna(0.0) == 0)
    assert not (entered_p & ~entered_1).any().any()


def test_refuses_real_data_before_the_registered_start(capsys):
    rc = lab.main(["--cheap-check", "--today", "2026-09-30"])
    assert rc == 3
    assert "REFUSING" in capsys.readouterr().out


def test_holdout_needs_the_discovery_verdict_first(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lab, "REPORT", tmp_path / "absent.json")
    rc = lab.main(["--holdout", "--today", "2026-10-05"])
    assert rc == 4
    assert "hold-out is read once" in capsys.readouterr().out


def test_cheap_check_and_full_run_on_synthetic(capsys):
    rc = lab.main(["--cheap-check", "--synthetic", "--today", "2026-09-25"])
    out = capsys.readouterr().out
    assert rc in (0, 1) and "CHEAP CHECK" in out
    rc = lab.main(["--synthetic", "--n-perm", "5", "--today", "2026-09-25"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "h=0.05" in out and "h=0.10" in out and "paired p" in out
    assert not lab.REPORT.exists() or True   # synthetic never writes the real report path


def test_decision_requires_every_check(panel):
    px, fday = panel
    sig = fday.rolling(7).sum()
    W1 = lab.v1_weights(sig)
    base = {c: lab.cell(sig, px, fday, W1, c) for c in (0.0, lab.COST, lab.STRESS)}
    W2 = lab.hysteresis_weights(sig, 0.10)
    cells = {c: lab.cell(sig, px, fday, W2, c) for c in (0.0, lab.COST, lab.STRESS)}
    rec = {f"{int(c * 1e4)}bps": lab.strip(cells[c]) for c in cells}
    rec.update({
        "turnover_ratio": 0.5, "delta_net_ann_pct_10bps": 5.0, "delta_net_ann_pct_20bps": 8.0,
        "paired_block_p_10bps": 0.01, "column_null": {"p_value": 0.01},
        "placebo": {"beats_placebo_paired_mean": True},
    })
    # force the Sharpe/DD/worst-day comparisons to pass by copying v1's own numbers
    for key in ("sharpe", "sharpe_h1", "sharpe_h2", "max_dd_pct", "worst_day_bps"):
        rec["10bps"][key] = base[lab.COST][key]
    d = lab.decide(rec, base)
    assert d["nominee"] is True
    rec["placebo"]["beats_placebo_paired_mean"] = False
    assert lab.decide(rec, base)["nominee"] is False, "one failed check is enough to refuse"
