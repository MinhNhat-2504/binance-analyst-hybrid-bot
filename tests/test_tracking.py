"""track_paper_vs_testnet.py: mark-to-market equity against mark-to-market paper, clean days only.

The first version compared paper to realized income per day and reported 4.35% of budget of
divergence in nine days on a book that was in fact within a few dollars of paper. These
tests pin the replacement: what counts as a clean interval, and that a stale-book day is
listed but never summed into the number the go-live stop rule reads.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone

import track_paper_vs_testnet as t

BUDGET = 2000.0


def _eq(*rows):
    return [(datetime.fromisoformat(u).replace(tzinfo=timezone.utc), e) for u, e in rows]


def test_clean_consecutive_days_are_compared_mark_to_market():
    equity = _eq(("2026-10-07T00:21:35", 5103.68), ("2026-10-08T00:22:35", 5142.64), ("2026-10-09T00:21:40", 5165.12))
    status = {"2026-10-07": "COMPLETE", "2026-10-08": "COMPLETE"}
    paper = {"2026-10-07": 37.88, "2026-10-08": 26.88}
    rows = t.intervals(equity, status, paper, BUDGET)
    assert [r["clean"] for r in rows] == [True, True]
    assert rows[0]["actual_usd"] == 38.96 and rows[0]["diff_usd"] == 1.08
    assert rows[1]["actual_usd"] == 22.48 and rows[1]["diff_usd"] == -4.4
    log = t.cumulative(rows, BUDGET)
    assert log[-1]["cum_diff_usd"] == -3.32
    assert log[-1]["cum_diff_pct_of_budget"] == round(-3.32 / BUDGET * 100, 3)


def test_refused_day_and_multi_day_gap_are_listed_but_not_counted():
    equity = _eq(("2026-09-28T00:21:39", 5010.02), ("2026-10-05T00:21:40", 5116.48),
                 ("2026-10-06T02:44:02", 5096.06), ("2026-10-07T00:21:35", 5103.68))
    status = {"2026-09-28": "PLAN_REFUSED", "2026-10-05": "COMPLETE", "2026-10-06": "PLAN_REFUSED"}
    paper = {"2026-09-28": -17.42, "2026-10-05": -17.76, "2026-10-06": 4.32}
    rows = t.intervals(equity, status, paper, BUDGET)
    assert [r["clean"] for r in rows] == [False, False, False]
    assert "PLAN_REFUSED" in rows[0]["why"]            # stale book for a week: +$123 that is not execution error
    assert "to the next read" in rows[1]["why"]        # COMPLETE, but the next read came 26.4h later
    assert "PLAN_REFUSED" in rows[2]["why"]
    assert t.cumulative([r for r in rows if r["clean"]], BUDGET) == []


def test_a_balance_jump_is_flagged_instead_of_trusted():
    equity = _eq(("2026-10-07T00:21:00", 5000.0), ("2026-10-08T00:21:00", 8000.0))
    rows = t.intervals(equity, {"2026-10-07": "COMPLETE"}, {"2026-10-07": 10.0}, BUDGET)
    assert rows[0]["clean"] is False and "5% of budget" in rows[0]["why"]


def test_complete_wins_when_a_day_has_two_log_rows(tmp_path):
    p = tmp_path / "log.csv"
    p.write_text("utc,target_id,status\n2026-10-07T00:20:00+00:00,a,PLAN_REFUSED\n"
                 "2026-10-07T03:00:00+00:00,a,COMPLETE\n2026-10-08T00:20:00+00:00,b,COMPLETE\n"
                 "2026-10-08T05:00:00+00:00,b,MISSED_WINDOW\n", encoding="utf-8")
    assert t.load_run_status(p) == {"2026-10-07": "COMPLETE", "2026-10-08": "COMPLETE"}


def test_log_has_the_columns_the_gate_reads(tmp_path):
    rows = t.cumulative([{"day": "2026-10-07", "paper_usd": 37.88, "actual_usd": 38.96, "diff_usd": 1.08}], BUDGET)
    out = tmp_path / "tracking_log.csv"
    t.write_log(rows, out)
    got = list(csv.DictReader(out.open(encoding="utf-8")))
    assert list(got[0]) == list(t.COLUMNS)
    import gate_report as gr
    weeks, budget = gr.weekly_tracking(got)
    assert round(budget) == 2000 and weeks[0]["days"] == 1 and weeks[0]["breach"] is False


def test_equity_loader_reads_the_drawdown_guard_file(tmp_path):
    p = tmp_path / "equity_hwm_testnet.json"
    p.write_text(json.dumps({"hwm": 1, "history": [{"utc": "2026-10-08T00:22:35+00:00", "equity": 5142.64},
                                                    {"utc": "2026-10-07T00:21:35+00:00", "equity": 5103.68}]}), encoding="utf-8")
    eq = t.load_equity(p)
    assert [e for _, e in eq] == [5103.68, 5142.64], "sorted by time"
    assert t.load_equity(tmp_path / "absent.json") == []
