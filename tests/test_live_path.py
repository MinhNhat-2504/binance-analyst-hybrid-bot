"""The live path must be fully testable while remaining structurally unreachable.

Locks under test:
  1. With the SHIPPED ceilings file (live: 0.0), a live ExecutionPolicy cannot even be
     constructed. No flag or code path arms live; only a reviewed ceilings revision.
  2. A testnet kill-switch release can never arm a live run, and vice versa.
  3. Client/policy/kill-switch environments must all agree; a live client must be POINTED
     at fapi.binance.com and a testnet client at a paper host.
  4. from_env("live") reads only BINANCE_LIVE_* credentials.
  5. When (and only when) a ceilings file authorizes live, the full hardened execute()
     path runs unchanged - contract records environment='live' and the live ceiling.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

import execution.engine as eng
from execution.binance_futures import FuturesREST, LIVE_BASE_URL, TESTNET_BASE_URL
from execution.engine import ExecutionAudit, ExecutionPolicy, KillSwitch, PortfolioExecutor, TestnetExecutor
from test_execution import TrackingClient


def _authorize_live(monkeypatch, amount=2000.0):
    """Simulate a reviewed ceilings revision that authorizes live capital."""
    monkeypatch.setattr(eng, "frozen_ceiling",
                        lambda env="testnet": amount if env == "live" else 2000.0)


def test_shipped_ceilings_keep_live_policy_unconstructible() -> None:
    with pytest.raises(ValueError, match="live is not authorized"):
        ExecutionPolicy(environment="live", expected_config_sha256="a" * 64)


def test_unknown_environment_refused() -> None:
    with pytest.raises(ValueError, match="unknown environment"):
        ExecutionPolicy(environment="prod", expected_config_sha256="a" * 64)
    with pytest.raises(ValueError, match="unknown environment"):
        KillSwitch("x.json", environment="prod")


def test_live_cli_is_inert_with_shipped_ceilings(tmp_path, monkeypatch, capsys) -> None:
    import run_live_execution as live
    monkeypatch.setattr("sys.argv", ["live", "--plan", "--budget-usd", "2000"])
    assert live.main() == 2
    assert "NOT AUTHORIZED" in capsys.readouterr().out


def test_testnet_release_cannot_arm_live_and_vice_versa(tmp_path) -> None:
    t = KillSwitch(tmp_path / "kt.json", environment="testnet")
    t.release("ok", target_id="x", authorized_budget_usd=100)
    live_view = KillSwitch(tmp_path / "kt.json", environment="live")
    with pytest.raises(RuntimeError, match="engaged"):
        live_view.assert_released_for_testnet()
    l = KillSwitch(tmp_path / "kl.json", environment="live")
    l.release("ok", target_id="x", authorized_budget_usd=100)
    testnet_view = KillSwitch(tmp_path / "kl.json", environment="testnet")
    with pytest.raises(RuntimeError, match="engaged"):
        testnet_view.assert_released_for_testnet()


def test_environment_mismatches_are_refused_at_construction(tmp_path, monkeypatch) -> None:
    _authorize_live(monkeypatch)
    live_policy = ExecutionPolicy(environment="live", expected_config_sha256="a" * 64)
    testnet_policy = ExecutionPolicy(expected_config_sha256="a" * 64)
    audit = ExecutionAudit(tmp_path / "a.sqlite3")

    class LiveFake(TrackingClient):
        environment = "live"

    # live client + testnet policy
    with pytest.raises(ValueError, match="!= policy environment"):
        PortfolioExecutor(LiveFake(), testnet_policy, KillSwitch(tmp_path / "k1.json"), audit)
    # matching envs but the kill switch belongs to the other environment
    with pytest.raises(ValueError, match="different environment"):
        PortfolioExecutor(LiveFake(), live_policy, KillSwitch(tmp_path / "k2.json", environment="testnet"), audit)
    # live-labelled REAL client pointed at a paper host
    class MislabelledLive:
        environment = "live"
        base_url = TESTNET_BASE_URL
    with pytest.raises(ValueError, match="not a live host"):
        PortfolioExecutor(MislabelledLive(), live_policy, KillSwitch(tmp_path / "k3.json", environment="live"), audit)
    # TestnetExecutor stays pinned: refuses the live pair outright
    with pytest.raises(ValueError, match="refuses any non-testnet"):
        TestnetExecutor(LiveFake(), live_policy, KillSwitch(tmp_path / "k4.json", environment="live"), audit)


def test_from_env_live_reads_only_live_credentials(monkeypatch) -> None:
    import execution.binance_futures as bf
    monkeypatch.setattr(bf, "_load_dotenv_testnet", lambda: None)
    for name in ("BINANCE_LIVE_API_KEY", "BINANCE_LIVE_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "t-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "t-secret")
    with pytest.raises(Exception, match="BINANCE_LIVE"):
        FuturesREST.from_env("live", required=True)
    monkeypatch.setenv("BINANCE_LIVE_API_KEY", "l-key")
    monkeypatch.setenv("BINANCE_LIVE_API_SECRET", "l-secret")
    client = FuturesREST.from_env("live", required=True)
    assert client.base_url == LIVE_BASE_URL and client.environment == "live"


def test_authorized_live_run_completes_with_live_contract(tmp_path, monkeypatch) -> None:
    """With a (simulated) reviewed ceilings revision, the SAME hardened execute() path runs
    and the audit contract records the live environment and its ceiling."""
    _authorize_live(monkeypatch, amount=2000.0)

    class LiveTracking(TrackingClient):
        environment = "live"

    now = datetime.now(timezone.utc)
    from execution.targets import load_target_book
    iso = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    tpath = tmp_path / "live_targets.json"
    tpath.write_text(json.dumps({
        "version": "CARRY_EXECUTION_TARGET_V1", "strategy": "CARRY-7d", "target_id": "live-1",
        "config_sha256": "a" * 64, "signal_time_utc": iso, "intended_execution_utc": iso,
        "weights": {"AAAUSDT": 0.5, "BBBUSDT": -0.5},
        "reference_prices": {"AAAUSDT": 100.0, "BBBUSDT": 100.0},
    }), encoding="utf-8")
    book = load_target_book(tpath)
    kill = KillSwitch(tmp_path / "kill_live.json", environment="live")
    kill.release("go-live day", target_id=book.target_id, authorized_budget_usd=2000.0)
    audit = ExecutionAudit(tmp_path / "audit_live.sqlite3")
    client = LiveTracking()
    result = PortfolioExecutor(
        client,
        ExecutionPolicy(environment="live", max_gross_notional_usd=2000.0,
                        poll_seconds=0, expected_config_sha256="a" * 64),
        kill, audit, now=lambda: now,
    ).execute(book, dry_run=False)
    assert result["status"] == "COMPLETE"
    # Effective budget = min(policy cap $2000, fake equity $100 x notional/equity 1.0) = $100,
    # so each 0.5 weight is $50 notional at mark 100 -> qty 0.5. The point is the CLOSED
    # LOOP: inventory equals the contract the verifier accepted, on the live path.
    assert client.inventory == {"AAAUSDT": Decimal("0.5"), "BBBUSDT": Decimal("-0.5")}
    contract = json.loads(audit.connection.execute(
        "SELECT positions_json FROM position_snapshots WHERE run_id=? AND phase='execution_contract'",
        (result["run_id"],),
    ).fetchone()[0])
    assert contract["environment"] == "live"
    assert contract["frozen_gross_ceiling_usd"] == 2000.0
