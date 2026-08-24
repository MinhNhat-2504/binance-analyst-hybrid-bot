"""The unattended testnet loop must be (a) impossible to point at live and (b) loud on anything but a clean day."""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import pytest

import run_carry_testnet_daily as daily
from execution.engine import KillSwitch


def _wire(monkeypatch, tmp_path, client_factory, ceilings_live=0.0):
    """Redirect every filesystem/network edge of the script into tmp_path + fakes."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    targets = tmp_path / "targets.json"
    targets.write_text(json.dumps({
        "version": "CARRY_EXECUTION_TARGET_V1", "strategy": "CARRY-7d", "target_id": "daily-1",
        "config_sha256": "a" * 64, "signal_time_utc": now, "intended_execution_utc": now,
        "weights": {"AAAUSDT": 0.5, "BBBUSDT": -0.5},
        "reference_prices": {"AAAUSDT": 100.0, "BBBUSDT": 100.0},
    }), encoding="utf-8")
    (tmp_path / ".execution").mkdir()
    monkeypatch.setattr(daily, "TARGETS", targets)
    monkeypatch.setattr(daily, "KILL", tmp_path / ".execution" / "kill.json")
    monkeypatch.setattr(daily, "AUDIT", tmp_path / ".execution" / "audit.sqlite3")
    monkeypatch.setattr(daily, "ATTENTION", tmp_path / ".execution" / "ATTENTION")
    monkeypatch.setattr(daily, "LOG", tmp_path / "log.csv")
    monkeypatch.setattr(daily, "INCIDENTS", tmp_path / "incidents.md")
    monkeypatch.setattr(daily, "load_ceilings", lambda: ({"testnet": 500.0, "live": ceilings_live}, "x" * 64))
    monkeypatch.setattr(daily, "sha256_file", lambda p: "a" * 64)
    # export step: pretend it succeeded (targets already on disk); reconcile: capture argv, return code injected
    class Proc:
        def __init__(self, rc, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""
    calls = {"reconcile_rc": 0}
    def fake_run(cmd, **kw):
        if "export_carry_targets.py" in cmd[2]:
            return Proc(0)
        if "reconcile_paper_vs_testnet.py" in cmd[2]:
            return Proc(calls["reconcile_rc"], "reconcile output")
        raise AssertionError(cmd)
    monkeypatch.setattr(daily.subprocess, "run", fake_run)
    def fake_from_env(env, required=True):
        calls["from_env_environment"] = env
        return client_factory()
    monkeypatch.setattr(daily.FuturesREST, "from_env", staticmethod(fake_from_env))
    monkeypatch.setattr(daily, "LOCK", tmp_path / ".execution" / "daily.lock")
    return calls


def test_refuses_when_live_ceiling_is_nonzero(monkeypatch, tmp_path):
    from test_execution import TrackingClient
    _wire(monkeypatch, tmp_path, TrackingClient, ceilings_live=100.0)
    with pytest.raises(SystemExit, match="non-zero LIVE ceiling"):
        daily.main()


def test_refuses_while_previous_attention_marker_exists(monkeypatch, tmp_path):
    from test_execution import TrackingClient
    _wire(monkeypatch, tmp_path, TrackingClient)
    daily.ATTENTION.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="ATTENTION"):
        daily.main()


def test_clean_day_is_silent_logged_and_reengaged(monkeypatch, tmp_path):
    from test_execution import TrackingClient
    holder = {}
    def factory():
        holder["c"] = TrackingClient()
        return holder["c"]
    _wire(monkeypatch, tmp_path, factory)
    assert daily.main() == 0
    # book built, log has one COMPLETE row, no ATTENTION, kill switch engaged again
    assert holder["c"].inventory == {"AAAUSDT": Decimal("0.5"), "BBBUSDT": Decimal("-0.5")}
    assert not daily.ATTENTION.exists()
    assert "COMPLETE" in daily.LOG.read_text(encoding="utf-8")
    with pytest.raises(RuntimeError, match="engaged"):
        KillSwitch(daily.KILL).assert_released_for_testnet()


def test_halt_leaves_attention_marker_and_incident(monkeypatch, tmp_path):
    from test_execution import TrackingClient
    class HaltsAfterFirstFill(TrackingClient):
        def __init__(self):
            super().__init__({})
            self.cancelled = []
        def order(self, **params):
            r = super().order(**params)
            if len(self.orders) == 1:
                KillSwitch(daily.KILL).engage("operator halted between legs")
            return r
        def cancel_all(self, symbol):
            self.cancelled.append(symbol); return {}
    _wire(monkeypatch, tmp_path, HaltsAfterFirstFill)
    rc = daily.main()
    assert rc == 6
    assert daily.ATTENTION.exists()
    marker = json.loads(daily.ATTENTION.read_text(encoding="utf-8"))
    assert marker["engine_status"] == "HALTED_MID_BOOK"
    assert "HALTED_MID_BOOK" in daily.INCIDENTS.read_text(encoding="utf-8")
    # And the very next run refuses until a human clears the marker.
    with pytest.raises(SystemExit, match="ATTENTION"):
        daily.main()


def test_script_asks_for_testnet_credentials_by_name(monkeypatch, tmp_path):
    """Lock #1 rests on one string literal. Pin it: the daily script must call
    from_env("testnet"). (The executor also refuses any client whose base_url is not testnet.)"""
    from test_execution import TrackingClient
    calls = _wire(monkeypatch, tmp_path, TrackingClient)
    assert daily.main() == 0
    assert calls["from_env_environment"] == "testnet"


def test_concurrent_second_fire_is_refused_by_lock(monkeypatch, tmp_path):
    from test_execution import TrackingClient
    _wire(monkeypatch, tmp_path, TrackingClient)
    daily.LOCK.parent.mkdir(exist_ok=True)
    daily.LOCK.write_text('{"pid": 1, "started_utc": "now"}', encoding="utf-8")   # another run "in progress"
    assert daily.main() == 7
    assert daily.ATTENTION.exists()
    assert "concurrent_or_stale_lock" in daily.ATTENTION.read_text(encoding="utf-8")


def test_unexpected_crash_still_leaves_attention_marker(monkeypatch, tmp_path):
    """Round-10 review: a crash BEFORE the run body wrote nothing and looked like silence
    for weeks. Any unexpected exception must leave the marker."""
    from test_execution import TrackingClient
    _wire(monkeypatch, tmp_path, TrackingClient)
    monkeypatch.setattr(daily, "load_target_book", lambda p: (_ for _ in ()).throw(RuntimeError("corrupt targets file")))
    with pytest.raises(RuntimeError, match="corrupt targets"):
        daily.main()
    assert daily.ATTENTION.exists()
    assert "unexpected_crash" in daily.ATTENTION.read_text(encoding="utf-8")
    assert not daily.LOCK.exists()   # lock released even on crash


def test_executor_refuses_client_pointed_at_production_url():
    from execution.binance_futures import LIVE_BASE_URL
    from execution.engine import ExecutionAudit, ExecutionPolicy, KillSwitch, TestnetExecutor
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    class LabelledTestnetButPointedAtProd:
        environment = "testnet"          # says testnet ...
        base_url = LIVE_BASE_URL         # ... but would talk to fapi.binance.com
    with pytest.raises(ValueError, match="not a testnet host"):
        TestnetExecutor(LabelledTestnetButPointedAtProd(), ExecutionPolicy(expected_config_sha256="a" * 64),
                        KillSwitch(tmp / "k.json"), ExecutionAudit(tmp / "a.sqlite3"))
