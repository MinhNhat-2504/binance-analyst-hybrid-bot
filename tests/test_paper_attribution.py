"""analyze_paper_attribution.py: the per-symbol reconstruction must sum to the ledger exactly."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import analyze_paper_attribution as apa

COST = 0.001


def _panels():
    idx = pd.date_range("2026-08-03", periods=5, freq="D")
    cols = ["AAA", "BBB", "CCC", "DDD"]
    opens = pd.DataFrame([[100, 50, 10, 1], [102, 49, 10.5, 1.01], [101, 51, 10.2, 0.99],
                          [103, 50, 10.4, 1.00], [104, 52, 10.1, 1.02]], index=idx, columns=cols, dtype=float)
    fday = pd.DataFrame(0.0003, index=idx, columns=cols)
    fday["CCC"] = -0.0002
    return opens, fday


def _ledger_rows(opens, fday):
    """Book three days exactly the way run_carry_paper.py does, so the test has ground truth."""
    rows, prev_w = [], pd.Series(0.0, index=opens.columns)
    books = [({"AAA": 0.5}, {"CCC": -0.5}), ({"AAA": 0.25, "BBB": 0.25}, {"CCC": -0.5}), ({"BBB": 0.5}, {"DDD": -0.5})]
    for i, (longs, shorts) in enumerate(books):
        fill = opens.index[i]; nxt = opens.index[i + 1]
        w = pd.Series(0.0, index=opens.columns)
        for s, v in longs.items(): w[s] = v
        for s, v in shorts.items(): w[s] = v
        ret = opens.loc[nxt] / opens.loc[fill] - 1
        f = fday.loc[fill]
        pnl = float((w * ret).sum() + (-w * f * 2 / 3).sum() + (-prev_w * f / 3).sum() - (w - prev_w).abs().sum() * COST)
        rows.append({"fill_day": fill.date().isoformat(), "pnl": f"{pnl:.6f}",
                     "longs": ",".join(sorted(longs)), "shorts": ",".join(sorted(shorts))})
        prev_w = w
    return rows


def test_reconstruction_sums_to_the_ledger_and_reports_concentration():
    opens, fday = _panels()
    rows = _ledger_rows(opens, fday)
    long = apa.attribute(rows, opens, fday, COST)
    for r in rows:
        day_total = long.loc[long["fill_day"] == r["fill_day"], "total"].sum()
        assert abs(day_total - float(r["pnl"])) < 1e-6
    summary = apa.summarize(long, len(rows))
    assert summary["n_days"] == 3
    assert abs(summary["by_side"]["long_leg_pct"] + summary["by_side"]["short_leg_pct"] - summary["total_pnl_pct"]) < 2e-3  # each side is rounded to 3 dp
    assert set(summary["by_symbol"]) == {"AAA", "BBB", "CCC", "DDD"}
    c = summary["concentration"]
    assert 0 < c["top1_share_of_gains_pct"] <= 100
    assert c["top3_share_of_gains_pct"] >= c["top1_share_of_gains_pct"]
    assert summary["by_symbol"]["AAA"]["days_long"] == 2 and summary["by_symbol"]["CCC"]["days_short"] == 2
    # cost is charged on the exit too: DDD entered on day 3 and CCC exited on day 3
    assert summary["by_symbol"]["CCC"]["cost"] < 0


def test_a_ledger_that_disagrees_with_the_reconstruction_is_refused():
    opens, fday = _panels()
    rows = _ledger_rows(opens, fday)
    rows[1]["pnl"] = f"{float(rows[1]['pnl']) + 0.001:.6f}"     # someone edited the ledger by 10 bps
    with pytest.raises(RuntimeError, match="reconstruction"):
        apa.attribute(rows, opens, fday, COST)


def test_weights_from_row_are_equal_weight_half_gross_per_side():
    w = apa.weights_from_row({"longs": "AAA,BBB", "shorts": "CCC"}, pd.Index(["AAA", "BBB", "CCC", "DDD"]))
    assert w["AAA"] == w["BBB"] == 0.25 and w["CCC"] == -0.5 and w["DDD"] == 0.0
