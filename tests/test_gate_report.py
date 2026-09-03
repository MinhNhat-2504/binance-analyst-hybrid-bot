"""gate_report.py must compute the day-60 verdict from files alone, and never crash on a missing one."""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

import gate_report as gr

START = date(2026, 8, 3)


def _config(root: Path) -> None:
    (root / "carry_paper_config_v1.json").write_text(json.dumps({
        "paper_start_utc": START.isoformat(), "cost_per_leg": 0.0010,
        "go_live_gate": {"min_paper_days": 60, "recommended_paper_days": 90, "paper_sharpe_min": 0.5,
                         "paper_total_return_min_pct": 0.0, "max_paper_drawdown_pct": -20.0},
    }), encoding="utf-8")


def _ledger(root: Path, pnls: list[float]) -> date:
    with (root / "carry_paper_ledger.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["signal_day", "fill_day", "mark", "n_long", "n_short", "turnover", "pnl", "equity", "shorts", "longs", "run_utc"])
        eq = 1.0
        for i, p in enumerate(pnls):
            eq *= 1 + p
            fill = START + timedelta(days=i)
            w.writerow([(fill - timedelta(days=1)).isoformat(), fill.isoformat(), "open_to_open", 8, 9, 0.3, p, round(eq, 6), "A", "B", "x"])
    return START + timedelta(days=len(pnls) - 1)


def _audit(root: Path, complete_days: int, extra: list[tuple[str, str]] = ()) -> None:
    ex = root / ".execution"
    ex.mkdir(exist_ok=True)
    c = sqlite3.connect(ex / "testnet_execution.sqlite3")
    c.execute("CREATE TABLE execution_runs (run_id TEXT PRIMARY KEY, started_utc TEXT, finished_utc TEXT, target_id TEXT, "
              "environment TEXT, dry_run INTEGER, status TEXT, message TEXT)")
    rows = []
    for i in range(complete_days):
        d = gr.TESTNET_START + timedelta(days=i)
        rows.append((f"dry{i}", f"{d}T00:21:00+00:00", "", "t", "testnet", 1, "DRY_RUN", ""))
        rows.append((f"run{i}", f"{d}T00:21:10+00:00", "", "t", "testnet", 0, "COMPLETE", "n legs"))
    for j, (d, status) in enumerate(extra):
        rows.append((f"x{j}", f"{d}T00:21:10+00:00", "", "t", "testnet", 0, status, ""))
    c.executemany("INSERT INTO execution_runs VALUES (?,?,?,?,?,?,?,?)", rows)
    c.commit(); c.close()


def _tracking(root: Path, days: list[tuple[date, float]], budget: float = 2000.0) -> None:
    with (root / "tracking_log.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["day", "paper_usd", "actual_usd", "diff_usd", "cum_diff_usd", "cum_diff_pct_of_budget"])
        cum = 0.0
        for d, diff in days:
            cum += diff
            w.writerow([d.isoformat(), 1.0, 1.0 + diff, diff, round(cum, 2), round(cum / budget * 100, 3)])


def _quality(root: Path, shortfalls: list[float]) -> None:
    with (root / "execution_quality.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["fill_date", "run_id", "symbol", "side", "reason", "qty", "avg_fill", "day_open", "shortfall_bps", "engine_slippage_bps"])
        for i, s in enumerate(shortfalls):
            w.writerow(["2026-08-25", "r", f"S{i}", "BUY", "rebalance", 1, 1, 1, s, 0])


def _canary(root: Path, rows: list[tuple[date, str]]) -> None:
    with (root / "canary_log.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "window_days", "sharpe_binance_weights", "sharpe_bybit_weights", "gap", "median_funding_ann_pct", "alerts"])
        for d, alert in rows:
            w.writerow([d.isoformat(), 180, 1.5, 1.2, 0.3, 4.0, alert])


def _good_pnls(n: int) -> list[float]:
    # positive drift, modest noise: Sharpe well above 0.5, no drawdown anywhere near -20%
    return [0.004 if i % 3 else -0.002 for i in range(n)]


def _full_green(root: Path, paper_days: int = 65, testnet: int = 21) -> date:
    _config(root)
    last = _ledger(root, _good_pnls(paper_days))
    _audit(root, testnet)
    today = last + timedelta(days=1)
    _tracking(root, [(gr.TESTNET_START + timedelta(days=i), 0.5 * (-1) ** i) for i in range(21)])
    _quality(root, [5.0, -3.0, 12.0, 2.0])
    _canary(root, [(today - timedelta(days=9), ""), (today - timedelta(days=2), "")])
    (root / "carry_paper_incidents.md").write_text("## 2026-08-15 - x\n\nstuff\n\n**Xử lý (2026-08-16):** done\n", encoding="utf-8")
    return today


def test_go_when_everything_passes(tmp_path, capsys):
    today = _full_green(tmp_path)
    rc = gr.main(["--root", str(tmp_path), "--today", today.isoformat()])
    out = capsys.readouterr().out
    assert rc == 0
    assert "VERDICT: GO" in out
    assert "[FAIL" not in out and "[NOT-YET" not in out
    report = (tmp_path / "reports" / "GATE_REPORT.md").read_text(encoding="utf-8")
    assert "## VERDICT: GO" in report
    assert "| PASS | paper Sharpe (ann.) |" in report


def test_no_go_on_drawdown_breach(tmp_path, capsys):
    today = _full_green(tmp_path)
    pnls = _good_pnls(65)
    pnls[30] = -0.25          # one -25% day: max drawdown breaches -20% and cannot un-happen
    _ledger(tmp_path, pnls)
    rc = gr.main(["--root", str(tmp_path), "--today", today.isoformat()])
    out = capsys.readouterr().out
    assert rc == 1
    assert "VERDICT: NO-GO" in out and "paper max drawdown" in out
    res = gr.evaluate(tmp_path, today)
    dd = next(c for c in res.conditions if c.name == "paper max drawdown")
    assert dd.status == gr.FAIL and dd.terminal


def test_not_yet_at_day_27_reports_earliest_date_and_missing_runs(tmp_path, capsys):
    _config(tmp_path)
    last = _ledger(tmp_path, _good_pnls(27))
    _audit(tmp_path, 3)
    today = last + timedelta(days=1)
    rc = gr.main(["--root", str(tmp_path), "--today", today.isoformat()])
    out = capsys.readouterr().out
    assert rc == 2
    assert "VERDICT: NOT-YET" in out
    assert "needs 17 more COMPLETE testnet runs" in out
    res = gr.evaluate(tmp_path, today)
    # day 60 = 33 more fill days after the last one, booked at the next open
    assert res.earliest == last + timedelta(days=34)
    days = next(c for c in res.conditions if c.name == "paper days")
    assert days.status == gr.NOT_YET and days.measured == "27"
    sharpe = next(c for c in res.conditions if c.name == "paper Sharpe (ann.)")
    assert sharpe.status == gr.NOT_YET and "would PASS" in sharpe.note


def test_tracking_breach_three_consecutive_weeks_is_terminal(tmp_path):
    today = _full_green(tmp_path)
    # -0.5% of a 2000 budget per day = -$10; 7 days -> -3.5%/week for 3 adjacent ISO weeks
    monday = gr.TESTNET_START - timedelta(days=gr.TESTNET_START.weekday()) + timedelta(days=7)
    _tracking(tmp_path, [(monday + timedelta(days=i), -10.0) for i in range(21)])
    res = gr.evaluate(tmp_path, today)
    tr = next(c for c in res.conditions if c.name == "tracking error")
    assert tr.status == gr.FAIL and tr.terminal
    assert res.verdict == "NO-GO" and res.exit_code == 1
    assert res.facts["tracking"]["longest"] == 3


def test_tracking_two_breaching_weeks_is_only_a_warning(tmp_path):
    today = _full_green(tmp_path)
    monday = gr.TESTNET_START - timedelta(days=gr.TESTNET_START.weekday()) + timedelta(days=7)
    _tracking(tmp_path, [(monday + timedelta(days=i), -10.0) for i in range(14)])
    res = gr.evaluate(tmp_path, today)
    tr = next(c for c in res.conditions if c.name == "tracking error")
    assert tr.status == gr.PASS and "2 consecutive breaching" in tr.note
    assert res.verdict == "GO"


def test_tracking_breaches_separated_by_a_clean_week_do_not_chain(tmp_path):
    today = _full_green(tmp_path)
    monday = gr.TESTNET_START - timedelta(days=gr.TESTNET_START.weekday()) + timedelta(days=7)
    days = [(monday + timedelta(days=i), -10.0) for i in range(7)]
    days += [(monday + timedelta(days=7 + i), 0.1) for i in range(7)]
    days += [(monday + timedelta(days=14 + i), -10.0) for i in range(14)]
    _tracking(tmp_path, days)
    res = gr.evaluate(tmp_path, today)
    assert res.facts["tracking"]["longest"] == 2
    assert next(c for c in res.conditions if c.name == "tracking error").status == gr.PASS


def test_weekly_tracking_uses_iso_calendar_weeks():
    rows = [{"day": "2026-08-30", "diff_usd": "-5", "cum_diff_usd": "-5", "cum_diff_pct_of_budget": "-0.25"},   # Sunday, W35
            {"day": "2026-08-31", "diff_usd": "-30", "cum_diff_usd": "-35", "cum_diff_pct_of_budget": "-1.75"}]  # Monday, W36
    weeks, budget = gr.weekly_tracking(rows)
    assert budget == pytest.approx(2000.0)
    assert [w["week"] for w in weeks] == ["2026-W35", "2026-W36"]
    assert [w["breach"] for w in weeks] == [False, True]


def test_fill_shortfall_above_cost_assumption_blocks_go(tmp_path):
    today = _full_green(tmp_path)
    _quality(tmp_path, [25.0, 30.0, 12.0])
    res = gr.evaluate(tmp_path, today)
    fills = next(c for c in res.conditions if c.name == "fill shortfall")
    assert fills.status == gr.FAIL and not fills.terminal
    assert res.verdict == "NOT-YET"


def test_canary_alert_and_marker_block_go(tmp_path):
    today = _full_green(tmp_path)
    _canary(tmp_path, [(today - timedelta(days=1), "SIGNAL: Binance edge converging")])
    (tmp_path / ".execution" / "ATTENTION").write_text("{}", encoding="utf-8")
    res = gr.evaluate(tmp_path, today)
    by = {c.name: c for c in res.conditions}
    assert by["signal canary"].status == gr.FAIL
    assert by["no blocking markers"].status == gr.FAIL and "ATTENTION" in by["no blocking markers"].measured
    assert res.verdict == "NOT-YET"


def test_negative_sharpe_at_day_60_is_no_go_but_weak_sharpe_extends_to_90(tmp_path):
    today = _full_green(tmp_path)
    # weak but positive: extend to day 90 (NOT-YET), not NO-GO
    _ledger(tmp_path, [((i * 7919) % 13 - 6) * 0.002 + 0.00015 for i in range(65)])   # Sharpe ~ +0.38
    res = gr.evaluate(tmp_path, today)
    sh = next(c for c in res.conditions if c.name == "paper Sharpe (ann.)")
    assert 0 < gr.paper_stats(gr._read_csv(tmp_path / "carry_paper_ledger.csv"))["sharpe"] < 0.5
    assert sh.status == gr.FAIL and not sh.terminal and "day 90" in sh.note
    assert res.verdict == "NOT-YET"
    # NEGATIVE at day 60 is ALSO only an extension: the pre-registered rule in
    # carry_paper_config_v1.json go_live_gate.note and checklist Part 3 is unconditional
    # ("if any fails at day 60, extend to 90"). The report must not invent a stricter rule.
    _ledger(tmp_path, [((i * 7919) % 13 - 6) * 0.002 - 0.00015 for i in range(65)])   # Sharpe ~ -0.38
    res = gr.evaluate(tmp_path, today)
    sh = next(c for c in res.conditions if c.name == "paper Sharpe (ann.)")
    assert sh.status == gr.FAIL and not sh.terminal and "day 90" in sh.note
    assert res.verdict == "NOT-YET"


def test_missing_everything_degrades_to_not_yet(tmp_path, capsys):
    rc = gr.main(["--root", str(tmp_path), "--today", "2026-09-03"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "VERDICT: NOT-YET" in out
    assert "using built-in defaults" in out
    assert "no ledger" in out and "no audit DB" in out and "no tracking_log.csv" in out
    assert (tmp_path / "reports" / "GATE_REPORT.md").exists()
    assert out.isascii()


def test_exposure_status_in_last_run_fails_cleanly(tmp_path):
    today = _full_green(tmp_path)
    _audit_extra = (tmp_path / ".execution" / "testnet_execution.sqlite3")
    c = sqlite3.connect(_audit_extra)
    c.execute("INSERT INTO execution_runs VALUES ('h','2026-09-30T00:21:10+00:00','','t','testnet',0,'HALTED_MID_BOOK','')")
    c.commit(); c.close()
    res = gr.evaluate(tmp_path, today)
    exp = next(c for c in res.conditions if c.name == "testnet exposure clean")
    assert exp.status == gr.FAIL and "HALTED_MID_BOOK" in exp.measured
    assert res.verdict == "NOT-YET"
