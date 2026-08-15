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
    monkeypatch.setattr(daily.FuturesREST, "from_env", staticmethod(lambda env, required=True: client_factory()))
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
