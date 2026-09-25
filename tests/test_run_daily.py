"""run_daily.py / run_scheduler.py / healthcheck.py: the OS-agnostic job chain.

Subprocesses are replaced with fakes; nothing here runs a real stage.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import healthcheck as hc
import run_daily as rd
import run_scheduler as rs


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    (tmp_path / ".execution").mkdir()
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    monkeypatch.setattr(rd.Path, "home", staticmethod(lambda: tmp_path / "home"))
    return tmp_path


def test_chain_continues_after_a_failing_step_and_reports_first_error(repo, monkeypatch):
    calls = []

    def fake_step(label, args, log_name, *, cwd):
        calls.append(label)
        return 5 if label == "testnet" else 0

    monkeypatch.setattr(rd, "run_step", fake_step)
    monkeypatch.setattr(rd, "write_status", lambda *, root: (calls.append("status"), 0)[1])
    rc = rd.run_stage("testnet", root=repo)
    assert calls == ["testnet", "status", "notify"], "a MISSED_WINDOW must not stop status/notify"
    assert rc == 5


def test_canary_marker_follows_the_latest_week(repo, monkeypatch):
    marker = repo / ".execution" / "canary_ALERT"
    monkeypatch.setattr(rd, "run_step", lambda label, args, log_name, *, cwd: 1)
    rd.run_stage("canary", root=repo)
    assert marker.exists()
    monkeypatch.setattr(rd, "run_step", lambda label, args, log_name, *, cwd: 0)
    rd.run_stage("canary", root=repo)
    assert not marker.exists(), "a clear week removes the stale alert"


def test_status_is_written_to_repo_and_copied_to_desktop_when_present(repo, monkeypatch):
    desk = repo / "home" / "Desktop"
    desk.mkdir(parents=True)

    class P:
        returncode = 0
        stdout = "CARRY-7d status fake\n"
        stderr = ""

    monkeypatch.setattr(rd.subprocess, "run", lambda *a, **k: P())
    assert rd.write_status(root=repo) == 0
    assert (repo / "status.txt").read_text(encoding="utf-8") == "CARRY-7d status fake\n"
    assert (desk / rd.DESKTOP_STATUS_NAME).read_text(encoding="utf-8") == "CARRY-7d status fake\n"


def test_status_without_a_desktop_still_writes_the_repo_file(repo, monkeypatch):
    class P:
        returncode = 0
        stdout = "x\n"
        stderr = ""

    monkeypatch.setattr(rd.subprocess, "run", lambda *a, **k: P())
    rd.write_status(root=repo)
    assert (repo / "status.txt").exists()


# --- scheduler -------------------------------------------------------------------------
def test_due_stages_respect_slot_weekday_and_already_ran_today():
    sunday = datetime(2026, 10, 4, 1, 30, tzinfo=timezone.utc)   # Sunday, after all slots
    assert rs.due(sunday, {}) == ["paper", "testnet", "canary"]
    monday = datetime(2026, 10, 5, 0, 10, tzinfo=timezone.utc)   # after paper, before testnet
    assert rs.due(monday, {}) == ["paper"]
    assert rs.due(monday, {"paper": "2026-10-05"}) == []
    assert rs.due(monday, {"paper": "2026-10-04"}) == ["paper"], "yesterday's run does not count"


def test_next_slot_is_the_soonest_future_slot():
    now = datetime(2026, 10, 5, 0, 10, tzinfo=timezone.utc)
    assert rs.next_slot(now) == datetime(2026, 10, 5, 0, 20, tzinfo=timezone.utc)
    late = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    assert rs.next_slot(late) == datetime(2026, 10, 6, 0, 5, tzinfo=timezone.utc)
    sat = datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc)      # Saturday: Sunday canary is nearer than... no, paper Sunday 00:05 first
    assert rs.next_slot(sat) == datetime(2026, 10, 4, 0, 5, tzinfo=timezone.utc)


def test_tick_runs_each_due_stage_once_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "STATE", tmp_path / "state.json")
    ran = []
    now = datetime(2026, 10, 5, 0, 30, tzinfo=timezone.utc)
    state = rs.tick(now, {}, run=lambda s: (ran.append(s), 0)[1])
    assert ran == ["paper", "testnet"]
    assert state == {"paper": "2026-10-05", "testnet": "2026-10-05"}
    state = rs.tick(now, state, run=lambda s: (ran.append(s), 0)[1])
    assert ran == ["paper", "testnet"], "second tick the same day launches nothing"
    assert rs._load() == state


# --- healthcheck -----------------------------------------------------------------------
def test_healthcheck_flags_marker_stale_lock_and_quiet_ledger(tmp_path):
    (tmp_path / ".execution").mkdir()
    assert hc.problems(tmp_path) == []
    (tmp_path / ".execution" / "ATTENTION").write_text("{}", encoding="utf-8")
    assert hc.problems(tmp_path) == ["ATTENTION marker present"]
    (tmp_path / ".execution" / "ATTENTION").unlink()
    lock = tmp_path / ".execution" / "testnet_daily.lock"
    lock.write_text("{}", encoding="utf-8")
    import os
    old = 1_700_000_000
    os.utime(lock, (old, old))
    assert hc.problems(tmp_path, now=old + 3 * 3600) == ["testnet_daily.lock older than 2h"]
    lock.unlink()
    (tmp_path / "carry_paper_ledger.csv").write_text("signal_day,fill_day,pnl\n2026-09-01,2026-09-02,0.0\n", encoding="utf-8")
    later = datetime(2026, 9, 10, tzinfo=timezone.utc).timestamp()
    assert hc.problems(tmp_path, now=later) == ["paper ledger last booked 2026-09-02"]
