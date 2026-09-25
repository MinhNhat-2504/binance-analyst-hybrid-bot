"""The unattended LIVE loop: inert with the shipped repo, and the same loop once armed.

Arming needs three independent facts (ceilings live > 0, a hand-written authorization,
the authorization naming the current ceilings sha). Each test removes one and expects
exit 2 with nothing written. The last test supplies all three against a fake exchange
and checks the loop runs the identical body the testnet tests describe, on its own kill
switch, audit and log.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import execution.engine as eng
import run_carry_live_daily as live
import run_carry_testnet_daily as daily
from test_execution import TrackingClient


def _paths(monkeypatch, tmp_path):
    (tmp_path / ".execution").mkdir()
    for name, rel in (("KILL_LIVE", ".execution/kill_live.json"), ("AUDIT_LIVE", ".execution/live.sqlite3"),
                      ("ATTENTION", ".execution/ATTENTION"), ("LOG_LIVE", "live_log.csv"),
                      ("LOCK_LIVE", ".execution/live.lock"), ("INCIDENTS", "incidents.md"),
                      ("AUTHORIZATION", "unattended_live_v1.json"), ("TARGETS", "targets.json")):
        monkeypatch.setattr(live, name, tmp_path / rel)
    return tmp_path


def _authorize_ceilings(monkeypatch, amount=2000.0):
    monkeypatch.setattr(eng, "frozen_ceiling", lambda env="testnet": amount if env == "live" else 2000.0)
    monkeypatch.setattr(live, "frozen_ceiling", lambda env="testnet": amount if env == "live" else 2000.0)
    monkeypatch.setattr(daily, "frozen_ceiling", lambda env="testnet": amount if env == "live" else 2000.0)


def _write_auth(tmp_path, **override):
    auth = {"unattended_live": True, "max_gross_usd": 1500.0, "expires_utc": "2099-01-01",
            "ceilings_sha256": live.CEILINGS_SHA256, "operator_note": "test"}
    auth.update(override)
    (tmp_path / "unattended_live_v1.json").write_text(json.dumps(auth), encoding="utf-8")


def _nothing_written(tmp_path):
    assert not (tmp_path / ".execution" / "ATTENTION").exists()
    assert not (tmp_path / "live_log.csv").exists()
    assert not (tmp_path / ".execution" / "live.sqlite3").exists()
    assert not (tmp_path / "incidents.md").exists()


def test_shipped_repo_is_inert(monkeypatch, tmp_path, capsys):
    """Real ceilings file (live: 0.0), authorization present and valid: still exit 2, nothing written."""
    _paths(monkeypatch, tmp_path)
    _write_auth(tmp_path)
    assert live.main([]) == 2
    assert "live=0.0" in capsys.readouterr().out
    _nothing_written(tmp_path)


def test_ceilings_alone_do_not_arm_it(monkeypatch, tmp_path, capsys):
    _paths(monkeypatch, tmp_path)
    _authorize_ceilings(monkeypatch)
    assert live.main([]) == 2
    assert "absent" in capsys.readouterr().out
    _nothing_written(tmp_path)


@pytest.mark.parametrize("bad, needle", [
    ({"unattended_live": False}, "unattended_live"),
    ({"ceilings_sha256": "0" * 64}, "different ceilings"),
    ({"expires_utc": "2020-01-01"}, "expired"),
    ({"max_gross_usd": 5000.0}, "must be in"),
    ({"max_gross_usd": 0}, "must be in"),
    ({"operator_note": "  "}, "operator_note"),
])
def test_each_authorization_field_is_checked(monkeypatch, tmp_path, capsys, bad, needle):
    _paths(monkeypatch, tmp_path)
    _authorize_ceilings(monkeypatch)
    _write_auth(tmp_path, **bad)
    assert live.main([]) == 2
    assert needle in capsys.readouterr().out
    _nothing_written(tmp_path)


def test_budget_is_the_smaller_of_authorization_and_ceiling(monkeypatch, tmp_path):
    _paths(monkeypatch, tmp_path)
    _authorize_ceilings(monkeypatch, amount=1000.0)
    _write_auth(tmp_path, max_gross_usd=800.0)
    assert live.authorized_budget() == 800.0
    _write_auth(tmp_path, max_gross_usd=1000.0)
    assert live.authorized_budget() == 1000.0


def test_testnet_loop_still_disables_itself_once_live_is_authorized(monkeypatch, tmp_path):
    """The two loops can never both be armed: live > 0 turns the testnet loop off."""
    monkeypatch.setattr(daily, "load_ceilings", lambda: ({"testnet": 2000.0, "live": 1000.0}, "x" * 64))
    monkeypatch.setattr(daily, "ATTENTION", tmp_path / "ATTENTION")
    with pytest.raises(SystemExit, match="non-zero LIVE ceiling"):
        daily.main([])


def test_armed_loop_runs_the_shared_body_on_its_own_files(monkeypatch, tmp_path):
    _paths(monkeypatch, tmp_path)
    _authorize_ceilings(monkeypatch, amount=2000.0)
    _write_auth(tmp_path, max_gross_usd=1500.0)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    (tmp_path / "targets.json").write_text(json.dumps({
        "version": "CARRY_EXECUTION_TARGET_V1", "strategy": "CARRY-7d", "target_id": "live-day-1",
        "config_sha256": "a" * 64, "signal_time_utc": now, "intended_execution_utc": now,
        "weights": {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, "reference_prices": {"AAAUSDT": 100.0, "BBBUSDT": 100.0},
    }), encoding="utf-8")
    monkeypatch.setattr(daily, "sha256_file", lambda p: "a" * 64)

    class Proc:
        def __init__(self, rc, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(cmd, **kw):
        if "export_carry_targets.py" in cmd[2]:
            return Proc(0)
        if "reconcile_paper_vs_testnet.py" in cmd[2]:
            assert str(tmp_path / ".execution" / "live.sqlite3") in cmd, "reconcile must read the LIVE audit"
            return Proc(0, "ok")
        raise AssertionError(cmd)
    monkeypatch.setattr(daily.subprocess, "run", fake_run)

    class LiveFake(TrackingClient):
        environment = "live"
        base_url = "https://fapi.binance.com"

        def account(self):
            return {"totalMarginBalance": "1500", "availableBalance": "1500"}

    asked = {}

    def fake_from_env(env, required=True):
        asked["env"] = env
        return LiveFake()
    monkeypatch.setattr(daily.FuturesREST, "from_env", staticmethod(fake_from_env))
    monkeypatch.setattr(daily.ExecutionPolicy, "__init__", _policy_init_with_zero_polling(daily.ExecutionPolicy))

    rc = live.main([])
    assert rc == 0, "a clean live day is silent"
    assert asked["env"] == "live", "credentials are asked for by the LIVE name"
    kill = json.loads((tmp_path / ".execution" / "kill_live.json").read_text(encoding="utf-8"))
    assert kill["environment"] == "live" and kill["trading_enabled"] is False, "re-engaged after the run"
    log = (tmp_path / "live_log.csv").read_text(encoding="utf-8")
    assert "COMPLETE" in log and "live-day-1" in log
    hwm = json.loads((tmp_path / ".execution" / "equity_hwm_live.json").read_text(encoding="utf-8"))
    assert hwm["hwm"] == 1500.0, "the live DD guard keeps its own mark beside the live kill switch"
    assert not (tmp_path / ".execution" / "ATTENTION").exists()
    assert not (tmp_path / "log.csv").exists() and not (tmp_path / ".execution" / "kill.json").exists(), \
        "nothing of the testnet loop's files was touched"


def _policy_init_with_zero_polling(cls):
    """Tests must not sleep: force poll_seconds=0 / flatten_retry_seconds=0 through the frozen dataclass."""
    original = cls.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault("poll_seconds", 0)
        kwargs.setdefault("flatten_retry_seconds", 0)
        original(self, *args, **kwargs)
    return init
