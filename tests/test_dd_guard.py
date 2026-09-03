"""The equity drawdown guard must stop the unattended loop BEFORE it places anything.

GO_LIVE_CHECKLIST.md says "DD -20% -> stop"; run_carry_testnet_daily.py enforces it as
dollars lost from the equity high-water mark versus the frozen budget. These tests pin the
ratchet, the threshold, the halt side-effects, the file's durability, and the reset flag.
Same fakes and redirection as tests/test_testnet_daily.py.
"""
from __future__ import annotations

import json
import os

import pytest

import run_carry_testnet_daily as daily
from execution.engine import KillSwitch
from test_execution import TrackingClient
from test_testnet_daily import _wire

BUDGET = 500.0                          # frozen testnet ceiling as seen by the guard in these tests
MAX_LOSS = daily.MAX_LOSS_FRACTION_OF_BUDGET * BUDGET   # 100 USD


class EquityClient(TrackingClient):
    """TrackingClient whose account equity the test can set per run."""

    equity = "100"

    def account(self):
        return {"totalMarginBalance": self.equity, "availableBalance": "1"}


def _wire_guard(monkeypatch, tmp_path, equity: str = "100"):
    """_wire + a budget the guard can see + a client holder so tests can move equity between runs."""
    holder = {}

    def factory():
        holder["c"] = EquityClient()
        holder["c"].equity = holder.get("equity", equity)
        return holder["c"]

    calls = _wire(monkeypatch, tmp_path, factory)
    monkeypatch.setattr(daily, "frozen_ceiling", lambda env: BUDGET)
    holder["equity"] = equity
    holder["calls"] = calls
    return holder


def _hwm():
    return json.loads(daily._hwm_path(daily.ENVIRONMENT).read_text(encoding="utf-8"))


def _seed_hwm(hwm: float, history=None):
    path = daily._hwm_path(daily.ENVIRONMENT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hwm": hwm, "hwm_utc": "2026-09-01T00:20:00+00:00", "last_equity": hwm,
                                "last_utc": "2026-09-01T00:20:00+00:00", "history": history or []}), encoding="utf-8")
    return path


def _clear_marker():
    if daily.ATTENTION.exists():
        daily.ATTENTION.unlink()


def _next_day(target_id: str):
    """The engine refuses to execute a target_id it already completed; a new day is a new id."""
    book = json.loads(daily.TARGETS.read_text(encoding="utf-8"))
    book["target_id"] = target_id
    daily.TARGETS.write_text(json.dumps(book), encoding="utf-8")


def test_hwm_file_lives_beside_kill_switch_in_execution_dir():
    """The path is derived from KILL so a redirected kill switch redirects the mark too."""
    assert daily._hwm_path("testnet").parent == daily.KILL.parent
    assert daily._hwm_path("testnet").name == "equity_hwm_testnet.json"
    assert daily._hwm_path("live").name == "equity_hwm_live.json"


def test_first_run_seeds_mark_and_later_runs_ratchet_only_upwards(monkeypatch, tmp_path):
    h = _wire_guard(monkeypatch, tmp_path, equity="100")
    assert daily.main() == 0
    state = _hwm()
    assert state["hwm"] == 100.0 and state["last_equity"] == 100.0 and len(state["history"]) == 1
    first_hwm_utc = state["hwm_utc"]

    h["equity"] = "150"                      # up: mark follows
    _next_day("daily-2")
    assert daily.main() == 0
    state = _hwm()
    assert state["hwm"] == 150.0 and state["last_equity"] == 150.0

    h["equity"] = "140"                      # down 10: mark holds, equity recorded
    _next_day("daily-3")
    assert daily.main() == 0
    state = _hwm()
    assert state["hwm"] == 150.0 and state["last_equity"] == 140.0
    assert len(state["history"]) == 3 and [r["equity"] for r in state["history"]] == [100.0, 150.0, 140.0]
    assert state["hwm_utc"] >= first_hwm_utc
    assert "DD_GUARD_HALT" not in daily.LOG.read_text(encoding="utf-8")


def test_loss_just_below_threshold_does_not_halt(monkeypatch, tmp_path):
    h = _wire_guard(monkeypatch, tmp_path, equity=str(1000 - MAX_LOSS + 1))   # loss 99 < 100
    _seed_hwm(1000.0)
    assert daily.main() == 0
    assert h["c"].orders, "orders must be placed on a normal day"
    assert not daily.ATTENTION.exists()
    assert "COMPLETE" in daily.LOG.read_text(encoding="utf-8")
    assert _hwm()["hwm"] == 1000.0


def test_loss_at_threshold_halts_before_any_order(monkeypatch, tmp_path):
    h = _wire_guard(monkeypatch, tmp_path, equity=str(1000 - MAX_LOSS))       # loss 100 >= 100
    _seed_hwm(1000.0)
    rc = daily.main()
    assert rc == daily.EXIT_DD_GUARD_HALT == 8
    assert rc not in (0, 4, 5, 6, 7), "must be a distinct exit code"
    # no orders, ever
    assert h["c"].orders == []
    # kill switch engaged, with the numbers in the reason
    payload = json.loads(daily.KILL.read_text(encoding="utf-8"))
    assert payload["trading_enabled"] is False
    for token in ("dd_guard", "900.00", "1000.00", "100.00", "20%"):
        assert token in payload["reason"], token
    with pytest.raises(RuntimeError, match="engaged"):
        KillSwitch(daily.KILL).assert_released_for_testnet()
    # ATTENTION marker + incident + loop-level log row
    marker = json.loads(daily.ATTENTION.read_text(encoding="utf-8"))
    assert marker["reason"] == "dd_guard"
    assert marker["loss_usd"] == 100.0 and marker["max_loss_usd"] == 100.0 and marker["hwm_usd"] == 1000.0
    assert "dd_guard" in daily.INCIDENTS.read_text(encoding="utf-8")
    log = daily.LOG.read_text(encoding="utf-8")
    assert "DD_GUARD_HALT" in log and "COMPLETE" not in log
    # the mark file still recorded the day
    state = _hwm()
    assert state["hwm"] == 1000.0 and state["last_equity"] == 900.0 and len(state["history"]) == 1
    # and the next scheduled fire refuses until a human clears the marker
    with pytest.raises(SystemExit, match="ATTENTION"):
        daily.main()


def test_threshold_is_dollars_of_budget_not_percent_of_equity(monkeypatch, tmp_path):
    """A fat demo balance must not hide a 20%-of-book loss: 1% of a 10000 account = 100 USD = halt."""
    h = _wire_guard(monkeypatch, tmp_path, equity="9900")
    _seed_hwm(10000.0)
    assert daily.main() == daily.EXIT_DD_GUARD_HALT
    assert h["c"].orders == []


def test_halt_repeats_until_operator_resets_even_after_marker_cleared(monkeypatch, tmp_path):
    """Deleting ATTENTION alone must not re-arm the loop while the loss still stands."""
    h = _wire_guard(monkeypatch, tmp_path, equity="850")
    _seed_hwm(1000.0)
    assert daily.main() == daily.EXIT_DD_GUARD_HALT
    _clear_marker()
    assert daily.main() == daily.EXIT_DD_GUARD_HALT
    assert h["c"].orders == []


def test_history_is_capped_at_120_rows_keeping_the_newest(monkeypatch, tmp_path):
    _wire_guard(monkeypatch, tmp_path, equity="100")
    old = [{"utc": f"2026-01-01T00:00:{i % 60:02d}+00:00", "equity": float(i)} for i in range(125)]
    _seed_hwm(100.0, history=old)
    assert daily.main() == 0
    state = _hwm()
    assert len(state["history"]) == daily.HWM_HISTORY_ROWS == 120
    assert state["history"][-1]["equity"] == 100.0
    assert state["history"][0]["equity"] == 6.0          # oldest 6 of 126 dropped


def test_persist_is_temp_plus_rename(monkeypatch, tmp_path):
    _wire_guard(monkeypatch, tmp_path, equity="100")
    seen = {}
    real_replace = os.replace

    target = daily._hwm_path(daily.ENVIRONMENT)

    def spy(src, dst):
        if str(dst) == str(target):          # the kill switch renames through os.replace too
            seen["src"], seen["dst"] = str(src), str(dst)
            seen["src_exists_before"] = os.path.exists(src)
            seen["dst_exists_before"] = os.path.exists(dst)
        return real_replace(src, dst)

    monkeypatch.setattr(daily.os, "replace", spy)
    assert daily.main() == 0
    assert seen["dst"] == str(target)
    assert seen["src"].endswith(".tmp") and os.path.dirname(seen["src"]) == str(target.parent)
    assert seen["src_exists_before"] and not seen["dst_exists_before"]
    assert not os.path.exists(seen["src"]), "temp file must not linger"
    assert target.exists()


def test_failed_rename_leaves_old_mark_intact_and_is_a_plan_refusal(monkeypatch, tmp_path):
    h = _wire_guard(monkeypatch, tmp_path, equity="100")
    path = _seed_hwm(1000.0)
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(daily.os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("disk full")))
    assert daily.main() == 5
    assert path.read_text(encoding="utf-8") == before
    assert h["c"].orders == []
    assert "PLAN_REFUSED" in daily.LOG.read_text(encoding="utf-8")
    assert json.loads(daily.ATTENTION.read_text(encoding="utf-8"))["reason"] == "plan_refused"


def test_account_read_failure_uses_existing_pre_plan_refusal_path(monkeypatch, tmp_path):
    class NoAccount(EquityClient):
        def account(self):
            raise RuntimeError("HTTP 503 from demo-fapi")

    h = {}

    def factory():
        h["c"] = NoAccount()
        return h["c"]

    _wire(monkeypatch, tmp_path, factory)
    monkeypatch.setattr(daily, "frozen_ceiling", lambda env: BUDGET)
    assert daily.main() == 5
    assert h["c"].orders == []
    assert not daily._hwm_path(daily.ENVIRONMENT).exists()
    marker = json.loads(daily.ATTENTION.read_text(encoding="utf-8"))
    assert marker["reason"] == "plan_refused" and "503" in marker["error"]
    assert "PLAN_REFUSED" in daily.LOG.read_text(encoding="utf-8")


def test_corrupt_mark_file_refuses_rather_than_silently_restarting(monkeypatch, tmp_path):
    h = _wire_guard(monkeypatch, tmp_path, equity="100")
    path = daily._hwm_path(daily.ENVIRONMENT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert daily.main() == 5
    assert h["c"].orders == []
    assert path.read_text(encoding="utf-8") == "{not json"


def test_mark_is_written_even_when_the_plan_step_raises(monkeypatch, tmp_path):
    from execution.engine import TestnetExecutor
    _wire_guard(monkeypatch, tmp_path, equity="100")
    monkeypatch.setattr(TestnetExecutor, "assert_target_fresh",
                        lambda self, book: (_ for _ in ()).throw(RuntimeError("stale target: intended execution is 9.50h old (limit 6.00h)")))
    assert daily.main() == 5
    assert "MISSED_WINDOW" in daily.LOG.read_text(encoding="utf-8")
    state = _hwm()
    assert state["hwm"] == 100.0 and len(state["history"]) == 1

    monkeypatch.setattr(TestnetExecutor, "assert_target_fresh",
                        lambda self, book: (_ for _ in ()).throw(RuntimeError("config sha mismatch")))
    assert daily.main() == 5
    assert "PLAN_REFUSED" in daily.LOG.read_text(encoding="utf-8")
    assert len(_hwm()["history"]) == 2


def test_reset_flag_rebases_mark_to_current_equity_and_places_nothing(monkeypatch, tmp_path):
    h = _wire_guard(monkeypatch, tmp_path, equity="700")
    _seed_hwm(1000.0)                        # loss 300: a scheduled run would halt
    assert daily.main() == daily.EXIT_DD_GUARD_HALT
    _clear_marker()

    assert daily.main(["--reset-equity-hwm"]) == 0
    assert h["c"].orders == []
    state = _hwm()
    assert state["hwm"] == 700.0 and state["last_equity"] == 700.0
    assert state["history"][-1]["equity"] == 700.0
    assert not daily.ATTENTION.exists()
    assert "DD_GUARD_HALT" in daily.LOG.read_text(encoding="utf-8").strip().splitlines()[-1], "reset writes no log row"

    # From the new base the loop runs normally again.
    assert daily.main() == 0
    assert h["c"].orders
    assert "COMPLETE" in daily.LOG.read_text(encoding="utf-8")


def test_reset_flag_works_while_attention_marker_is_present(monkeypatch, tmp_path):
    """The operator resets during review, i.e. while the marker still blocks scheduled runs."""
    h = _wire_guard(monkeypatch, tmp_path, equity="700")
    _seed_hwm(1000.0)
    assert daily.main() == daily.EXIT_DD_GUARD_HALT
    assert daily.ATTENTION.exists()
    assert daily.main(["--reset-equity-hwm"]) == 0
    assert _hwm()["hwm"] == 700.0 and h["c"].orders == []
    assert daily.ATTENTION.exists(), "reset does not clear the marker; that is the operator's explicit act"


def test_no_arg_main_ignores_pytest_argv_and_only_flag_is_reset(monkeypatch, tmp_path):
    _wire_guard(monkeypatch, tmp_path)
    assert daily.main() == 0                                    # sys.argv is never consulted
    with pytest.raises(SystemExit):
        daily.main(["--something-else"])
