from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from execution.engine import ExecutionAudit, ExecutionPolicy, KillSwitch, TestnetExecutor
from execution.binance_futures import FuturesREST, LIVE_BASE_URL, TESTNET_BASE_URL
from execution.targets import TargetBook, load_target_book
from reconcile_paper_vs_testnet import compare_target_to_positions


def _target_file(tmp_path, weights: dict[str, float]):
    config = "a" * 64
    path = tmp_path / "targets.json"
    path.write_text(
        json.dumps(
            {
                "version": "CARRY_EXECUTION_TARGET_V1", "strategy": "CARRY-7d", "target_id": "target-1",
                "config_sha256": config, "signal_time_utc": "2026-08-14T23:59:59Z",
                "intended_execution_utc": "2026-08-15T00:00:00Z", "weights": weights,
                "reference_prices": {symbol: 100.0 for symbol in weights},
            }
        ),
        encoding="utf-8",
    )
    return path


class FakeClient:
    environment = "testnet"

    def sync_time(self):
        return 0

    def exchange_info(self):
        return {
            "symbols": [
                {
                    "symbol": "AAAUSDT", "status": "TRADING", "contractType": "PERPETUAL",
                    "filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                },
                {
                    "symbol": "BBBUSDT", "status": "TRADING", "contractType": "PERPETUAL",
                    "filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                },
            ]
        }

    def position_mode(self):
        return False

    def positions(self):
        return [{"symbol": "AAAUSDT", "positionAmt": "1.0"}]

    def account(self):
        return {"totalMarginBalance": "100", "availableBalance": "1"}

    def book_ticker(self, symbol):
        return {"bidPrice": "99.9", "askPrice": "100.1"}


def test_target_book_validates_exposure_and_identity(tmp_path) -> None:
    path = _target_file(tmp_path, {"AAAUSDT": 0.5, "BBBUSDT": -0.5})
    book = load_target_book(path)
    assert book.gross == pytest.approx(1.0)
    assert book.net == pytest.approx(0.0)

    bad = _target_file(tmp_path, {"AAAUSDT": 1.0})
    payload = json.loads(bad.read_text(encoding="utf-8"))
    payload["config_sha256"] = "broken"
    bad.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="immutable"):
        load_target_book(bad)


def test_kill_switch_fails_closed_and_can_be_released(tmp_path) -> None:
    switch = KillSwitch(tmp_path / "kill.json")
    with pytest.raises(RuntimeError, match="missing"):
        switch.assert_released_for_testnet()
    switch.release("operator approved testnet rehearsal")
    switch.assert_released_for_testnet()
    switch.engage("rehearsal complete")
    with pytest.raises(RuntimeError, match="engaged"):
        switch.assert_released_for_testnet()


def test_reconciliation_closes_before_flipping_position(tmp_path) -> None:
    # The executor is separately tested with a deliberately directional target
    # here, so that the reconciliation leg is isolated from target-file exposure
    # validation (which correctly enforces a delta-neutral carry book).
    book = TargetBook(
        strategy="unit-test", target_id="target-1", config_sha256="a" * 64,
        signal_time_utc="2026-08-14T23:59:59Z", intended_execution_utc="2026-08-15T00:00:00Z",
        weights={"AAAUSDT": -1.0}, reference_prices={"AAAUSDT": 100.0}, source="unit-test",
    )
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    executor = TestnetExecutor(
        FakeClient(),
        ExecutionPolicy(max_gross_notional_usd=100.0, max_order_notional_usd=100.0, max_positions=2),
        KillSwitch(tmp_path / "kill.json"),
        audit,
    )
    legs, skips = executor.build_plan(book)
    assert not skips
    assert len(legs) == 2
    assert legs[0].reduce_only and legs[0].side == "SELL" and legs[0].quantity == Decimal("1.0")
    assert not legs[1].reduce_only and legs[1].side == "SELL"
    assert legs[1].target_weight == -1.0


def test_executor_dry_run_never_needs_kill_switch_release(tmp_path) -> None:
    book = TargetBook(
        strategy="unit-test", target_id="target-1", config_sha256="a" * 64,
        signal_time_utc="2026-08-14T23:59:59Z", intended_execution_utc="2026-08-15T00:00:00Z",
        weights={"AAAUSDT": -1.0}, reference_prices={"AAAUSDT": 100.0}, source="unit-test",
    )
    executor = TestnetExecutor(
        FakeClient(), ExecutionPolicy(max_gross_notional_usd=100.0, max_positions=2),
        KillSwitch(tmp_path / "absent-kill.json"), ExecutionAudit(tmp_path / "audit.sqlite3"),
    )
    result = executor.execute(book, dry_run=True)
    assert result["status"] == "DRY_RUN"


def test_orphan_position_is_closed_and_max_order_size_is_enforced(tmp_path) -> None:
    book = TargetBook("unit-test", "target-1", "a" * 64, "2026-08-15T00:00:00Z", "2026-08-15T00:00:00Z", {"BBBUSDT": 1.0}, {"BBBUSDT": 100.0}, "unit-test")
    executor = TestnetExecutor(FakeClient(), ExecutionPolicy(max_gross_notional_usd=100, max_order_notional_usd=30, max_positions=2), KillSwitch(tmp_path / "kill.json"), ExecutionAudit(tmp_path / "audit.sqlite3"))
    legs, _ = executor.build_plan(book)
    assert any(leg.symbol == "AAAUSDT" and leg.reason == "close_orphan" and leg.reduce_only for leg in legs)
    assert all(float(leg.quantity) * leg.reference_price <= 30.001 for leg in legs)


def test_stale_target_and_production_client_are_refused(tmp_path) -> None:
    class LiveClient(FakeClient):
        environment = "live"
    with pytest.raises(ValueError, match="non-testnet"):
        TestnetExecutor(LiveClient(), ExecutionPolicy(), KillSwitch(tmp_path / "kill.json"), ExecutionAudit(tmp_path / "audit.sqlite3"))
    assert FuturesREST("testnet").base_url == TESTNET_BASE_URL
    live_rest = FuturesREST("live")
    assert live_rest.base_url == LIVE_BASE_URL
    with pytest.raises(ValueError, match="non-testnet"):
        TestnetExecutor(live_rest, ExecutionPolicy(), KillSwitch(tmp_path / "kill2.json"), ExecutionAudit(tmp_path / "audit2.sqlite3"))
    book = TargetBook("unit-test", "target-1", "a" * 64, "2026-08-14T00:00:00Z", "2026-08-14T00:00:00Z", {"AAAUSDT": 1.0}, {}, "unit-test")
    executor = TestnetExecutor(FakeClient(), ExecutionPolicy(), KillSwitch(tmp_path / "kill.json"), ExecutionAudit(tmp_path / "audit.sqlite3"), now=lambda: datetime(2026, 8, 15, tzinfo=timezone.utc))
    with pytest.raises(RuntimeError, match="stale target"):
        executor.assert_target_fresh(book)


def test_failed_order_engages_kill_switch_and_attempts_reduce_only_flatten(tmp_path) -> None:
    class RejectingClient(FakeClient):
        def __init__(self):
            self.orders = []
            self.cancelled = 0

        def set_leverage(self, symbol, leverage):
            return {"symbol": symbol, "leverage": leverage}

        def order(self, **params):
            self.orders.append(params)
            return {"status": "REJECTED"}

        def cancel_all(self, symbol):
            self.cancelled += 1
            return {}

    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    book = TargetBook("unit-test", "target-1", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": -1.0}, {}, "unit-test")
    client = RejectingClient()
    kill = KillSwitch(tmp_path / "kill.json")
    kill.release("test")
    audit_path = tmp_path / "audit.sqlite3"
    executor = TestnetExecutor(client, ExecutionPolicy(max_gross_notional_usd=100, max_order_notional_usd=100, poll_seconds=0, expected_config_sha256="a" * 64), kill, ExecutionAudit(audit_path), now=lambda: now)
    with pytest.raises(RuntimeError, match="did not fill"):
        executor.execute(book, dry_run=False)
    assert client.cancelled == 1
    assert any(order["reduceOnly"] == "true" for order in client.orders)
    assert all(order["newClientOrderId"].startswith("carry-") for order in client.orders)
    assert json.loads(kill.path.read_text(encoding="utf-8"))["trading_enabled"] is False
    reasons = [row[0] for row in ExecutionAudit(audit_path).connection.execute("SELECT reason FROM execution_legs")]
    assert "emergency_flatten" in reasons


def test_planning_failure_never_cancels_or_flattens(tmp_path) -> None:
    class PlanningFailureClient(FakeClient):
        def __init__(self):
            self.cancelled = 0
            self.orders = 0

        def book_ticker(self, symbol):
            raise TimeoutError("planning quote timeout")

        def cancel_all(self, symbol):
            self.cancelled += 1

        def order(self, **params):
            self.orders += 1

    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    book = TargetBook("unit-test", "planning-fails", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": -1.0}, {}, "unit-test")
    client = PlanningFailureClient()
    kill = KillSwitch(tmp_path / "kill.json")
    kill.release("test")
    executor = TestnetExecutor(client, ExecutionPolicy(expected_config_sha256="a" * 64), kill, ExecutionAudit(tmp_path / "audit.sqlite3"), now=lambda: now)
    with pytest.raises(TimeoutError, match="planning quote"):
        executor.execute(book, dry_run=False)
    assert client.orders == 0 and client.cancelled == 0
    assert json.loads(kill.path.read_text(encoding="utf-8"))["trading_enabled"] is False


def test_complete_path_polls_order_reengages_kill_and_blocks_duplicate_target(tmp_path) -> None:
    class FilledClient(FakeClient):
        def __init__(self):
            self.orders = []
            self.next_order_id = 1

        def positions(self):
            return []

        def set_leverage(self, symbol, leverage):
            return {"symbol": symbol, "leverage": leverage}

        def order(self, **params):
            self.orders.append(params)
            order_id = self.next_order_id
            self.next_order_id += 1
            return {"orderId": order_id, "status": "NEW"}

        def get_order(self, symbol, order_id):
            return {"orderId": order_id, "status": "FILLED", "avgPrice": "100"}

    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    book = TargetBook("unit-test", "complete-once", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {"AAAUSDT": 100, "BBBUSDT": 100}, "unit-test")
    client, kill = FilledClient(), KillSwitch(tmp_path / "kill.json")
    kill.release("test")
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    executor = TestnetExecutor(client, ExecutionPolicy(max_gross_notional_usd=100, max_order_notional_usd=100, poll_seconds=0, expected_config_sha256="a" * 64), kill, audit, now=lambda: now)
    assert executor.execute(book, dry_run=False)["status"] == "COMPLETE"
    assert client.orders and all("newClientOrderId" in order for order in client.orders)
    assert json.loads(kill.path.read_text(encoding="utf-8"))["trading_enabled"] is False
    kill.release("duplicate attempt")
    with pytest.raises(RuntimeError, match="already completed"):
        executor.execute(book, dry_run=False)
    assert len(client.orders) == 2


def test_reconciler_uses_target_weights_not_executor_desired_quantity() -> None:
    target = TargetBook("unit-test", "target", "a" * 64, "2026-08-15T00:00:00Z", "2026-08-15T00:00:00Z", {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {"AAAUSDT": 100, "BBBUSDT": 50}, "unit-test")
    ok, rows = compare_target_to_positions(target, {"AAAUSDT": Decimal("0.5"), "BBBUSDT": Decimal("-1")}, {}, weight_tolerance=0.001, flat_quantity_tolerance=0.000001)
    assert ok and all(row["ok"] for row in rows)
    bad, _ = compare_target_to_positions(target, {"AAAUSDT": Decimal("1"), "BBBUSDT": Decimal("-1")}, {}, weight_tolerance=0.001, flat_quantity_tolerance=0.000001)
    assert not bad


def test_portfolio_plan_orders_all_closes_before_any_open(tmp_path) -> None:
    book = TargetBook("unit-test", "target", "a" * 64, "2026-08-15T00:00:00Z", "2026-08-15T00:00:00Z", {"AAAUSDT": -0.5, "BBBUSDT": 0.5}, {"AAAUSDT": 100, "BBBUSDT": 100}, "unit-test")
    executor = TestnetExecutor(FakeClient(), ExecutionPolicy(max_gross_notional_usd=100, max_order_notional_usd=100), KillSwitch(tmp_path / "kill.json"), ExecutionAudit(tmp_path / "audit.sqlite3"))
    legs, _ = executor.build_plan(book)
    first_open = next(index for index, leg in enumerate(legs) if not leg.reduce_only)
    assert all(leg.reduce_only for leg in legs[:first_open])
    assert all(not leg.reduce_only for leg in legs[first_open:])
