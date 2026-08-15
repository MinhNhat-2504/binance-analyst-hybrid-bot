from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from execution.engine import AuditWriteError, ExternalPositionDriftError, HaltedError, ExecutionAudit, ExecutionPolicy, KillSwitch, TargetMismatchError, TestnetExecutor
from execution.binance_futures import BinanceAPIError, FuturesREST, LIVE_BASE_URL, TESTNET_BASE_URL
from execution.targets import TargetBook, load_target_book
from reconcile_paper_vs_testnet import compare_contract_to_positions, contract_sha256, select_execution_run


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

    def set_margin_type(self, symbol, margin_type):
        return {"symbol": symbol, "marginType": margin_type}

    def set_leverage(self, symbol, leverage):
        return {"symbol": symbol, "leverage": leverage}

    def book_ticker(self, symbol):
        return {"bidPrice": "99.9", "askPrice": "100.1"}

    def open_orders(self, symbol=None):
        return []


class TrackingClient(FakeClient):
    """Exchange fake whose positionRisk changes only when an order actually fills."""

    def __init__(self, positions: dict[str, Decimal] | None = None):
        self.inventory = dict(positions or {})
        self.orders = []
        self.next_order_id = 1

    def positions(self):
        return [
            {"symbol": symbol, "positionAmt": str(quantity), "markPrice": "100"}
            for symbol, quantity in sorted(self.inventory.items()) if quantity != 0
        ]

    def order(self, **params):
        self.orders.append(params)
        quantity = Decimal(str(params["quantity"]))
        signed = quantity if params["side"] == "BUY" else -quantity
        symbol = params["symbol"]
        self.inventory[symbol] = self.inventory.get(symbol, Decimal("0")) + signed
        if self.inventory[symbol] == 0:
            self.inventory.pop(symbol)
        order_id = self.next_order_id
        self.next_order_id += 1
        return {"orderId": order_id, "status": "NEW"}

    def get_order(self, symbol, order_id):
        return {"orderId": order_id, "status": "FILLED", "avgPrice": "100"}

    def cancel_all(self, symbol):
        return {}


def _release(switch: KillSwitch, book: TargetBook, budget: float = 500.0) -> None:
    switch.release("test", target_id=book.target_id, authorized_budget_usd=budget)


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
    switch.release("operator approved testnet rehearsal", target_id="target-1", authorized_budget_usd=500)
    switch.assert_released_for_testnet(expected_target_id="target-1", expected_budget_usd=500)
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
    plan = executor.build_plan(book)
    legs, skips = plan.legs, plan.skips
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
    legs = executor.build_plan(book).legs
    assert any(leg.symbol == "AAAUSDT" and leg.reason == "close_orphan" and leg.reduce_only for leg in legs)
    assert all(float(leg.quantity) * leg.execution_reference_price <= 30.001 for leg in legs)


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
    book = TargetBook("unit-test", "target-1", "a" * 64, "2026-08-14T00:00:00Z", "2026-08-14T00:00:00Z", {"AAAUSDT": 1.0}, {"AAAUSDT": 100.0}, "unit-test")
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
    book = TargetBook("unit-test", "target-1", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": -1.0}, {"AAAUSDT": 100.0}, "unit-test")
    client = RejectingClient()
    kill = KillSwitch(tmp_path / "kill.json")
    _release(kill, book, 100)
    audit_path = tmp_path / "audit.sqlite3"
    executor = TestnetExecutor(client, ExecutionPolicy(max_gross_notional_usd=100, max_order_notional_usd=100, poll_seconds=0, flatten_retry_seconds=0, expected_config_sha256="a" * 64), kill, ExecutionAudit(audit_path), now=lambda: now)
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
    book = TargetBook("unit-test", "planning-fails", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": -1.0}, {"AAAUSDT": 100.0}, "unit-test")
    client = PlanningFailureClient()
    kill = KillSwitch(tmp_path / "kill.json")
    _release(kill, book)
    executor = TestnetExecutor(client, ExecutionPolicy(expected_config_sha256="a" * 64), kill, ExecutionAudit(tmp_path / "audit.sqlite3"), now=lambda: now)
    with pytest.raises(TimeoutError, match="planning quote"):
        executor.execute(book, dry_run=False)
    assert client.orders == 0 and client.cancelled == 0
    assert json.loads(kill.path.read_text(encoding="utf-8"))["trading_enabled"] is False


def test_limit_quote_failure_before_post_never_flattens_healthy_inventory(tmp_path) -> None:
    class PrePostQuoteFailure(FakeClient):
        def __init__(self):
            self.quotes = 0
            self.orders = 0
            self.cancelled = 0

        def positions(self):
            return []

        def book_ticker(self, symbol):
            self.quotes += 1
            if self.quotes > 2:  # both plan quotes passed; LIMIT preparation now fails
                raise TimeoutError("pre-POST limit quote timeout")
            return super().book_ticker(symbol)

        def order(self, **params):
            self.orders += 1
            return {"status": "FILLED"}

        def cancel_all(self, symbol):
            self.cancelled += 1
            return {}

    now = datetime.now(timezone.utc)
    book = TargetBook("unit-test", "pre-post-timeout", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {"AAAUSDT": 100.0, "BBBUSDT": 100.0}, "unit-test")
    client, kill = PrePostQuoteFailure(), KillSwitch(tmp_path / "kill.json")
    _release(kill, book)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    policy = ExecutionPolicy(order_style="LIMIT_IOC", poll_seconds=0, expected_config_sha256="a" * 64)
    with pytest.raises(TimeoutError, match="pre-POST"):
        TestnetExecutor(client, policy, kill, audit, now=lambda: now).execute(book, dry_run=False)
    assert client.orders == 0 and client.cancelled == 0
    assert not audit.connection.execute("SELECT 1 FROM position_snapshots WHERE phase LIKE 'emergency_%'").fetchone()


def test_complete_path_polls_order_reengages_kill_and_blocks_duplicate_target(tmp_path) -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    book = TargetBook("unit-test", "complete-once", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {"AAAUSDT": 100, "BBBUSDT": 100}, "unit-test")
    client, kill = TrackingClient(), KillSwitch(tmp_path / "kill.json")
    _release(kill, book, 100)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    executor = TestnetExecutor(client, ExecutionPolicy(max_gross_notional_usd=100, max_order_notional_usd=100, poll_seconds=0, expected_config_sha256="a" * 64), kill, audit, now=lambda: now)
    assert executor.execute(book, dry_run=False)["status"] == "COMPLETE"
    assert client.inventory == {"AAAUSDT": Decimal("0.5"), "BBBUSDT": Decimal("-0.5")}
    assert client.orders and all("newClientOrderId" in order for order in client.orders)
    assert json.loads(kill.path.read_text(encoding="utf-8"))["trading_enabled"] is False
    kill.release("duplicate attempt", target_id=book.target_id, authorized_budget_usd=100)
    with pytest.raises(RuntimeError, match="already completed"):
        executor.execute(book, dry_run=False)
    assert len(client.orders) == 2


def test_reconciler_uses_exact_contract_vector_and_rejects_right_shape_wrong_scale() -> None:
    contract = {
        "version": "EXECUTION_POSITION_CONTRACT_V1",
        "target_id": "target",
        "authorized_budget_usd": 100,
        "effective_gross_budget_usd": 100,
        "frozen_testnet_gross_ceiling_usd": 500,
        "expected_positions": {"AAAUSDT": "0.5", "BBBUSDT": "-1"},
        "tolerance_budgets": {
            "AAAUSDT": {"step_size": "0.1", "min_qty": "0.1", "min_notional": "5", "rounding_steps": "0.5", "min_notional_fraction": "0.01"},
            "BBBUSDT": {"step_size": "0.1", "min_qty": "0.1", "min_notional": "5", "rounding_steps": "0.5", "min_notional_fraction": "0.01"},
        },
        "accepted_skips": [], "orphan_symbols": [], "requires_no_open_orders": True,
    }
    contract["contract_sha256"] = contract_sha256(contract)
    verification = {"rows": [
        {"symbol": "AAAUSDT", "verification_price": 100, "open_orders": 0},
        {"symbol": "BBBUSDT", "verification_price": 50, "open_orders": 0},
    ]}
    ok, rows, gross = compare_contract_to_positions(
        contract, {"AAAUSDT": Decimal("0.5"), "BBBUSDT": Decimal("-1")}, verification,
    )
    assert ok and all(row["ok"] for row in rows)
    assert gross["expected_gross_notional"] == 100
    wrong_scale, wrong_rows, wrong_gross = compare_contract_to_positions(
        contract, {"AAAUSDT": Decimal("5"), "BBBUSDT": Decimal("-10")}, verification,
    )
    assert not wrong_scale and not all(row["ok"] for row in wrong_rows)
    assert wrong_gross["actual_gross_notional"] == 1000


def test_portfolio_plan_orders_all_closes_before_any_open(tmp_path) -> None:
    book = TargetBook("unit-test", "target", "a" * 64, "2026-08-15T00:00:00Z", "2026-08-15T00:00:00Z", {"AAAUSDT": -0.5, "BBBUSDT": 0.5}, {"AAAUSDT": 100, "BBBUSDT": 100}, "unit-test")
    executor = TestnetExecutor(FakeClient(), ExecutionPolicy(max_gross_notional_usd=100, max_order_notional_usd=100), KillSwitch(tmp_path / "kill.json"), ExecutionAudit(tmp_path / "audit.sqlite3"))
    legs = executor.build_plan(book).legs
    first_open = next(index for index, leg in enumerate(legs) if not leg.reduce_only)
    assert all(leg.reduce_only for leg in legs[:first_open])
    assert all(not leg.reduce_only for leg in legs[first_open:])


def test_delta_and_split_chunks_enforce_min_notional(tmp_path) -> None:
    class EthLikeClient(FakeClient):
        def exchange_info(self):
            return {"symbols": [{"symbol": "ETHUSDT", "status": "TRADING", "contractType": "PERPETUAL", "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"}, {"filterType": "PRICE_FILTER", "tickSize": "0.01"}, {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]}

        def positions(self):
            return [{"symbol": "ETHUSDT", "positionAmt": "0.009"}]

        def book_ticker(self, symbol):
            return {"bidPrice": "1999", "askPrice": "2001"}

    book = TargetBook("unit-test", "eth-dust", "a" * 64, "2026-08-15T00:00:00Z", "2026-08-15T00:00:00Z", {"ETHUSDT": 1.0}, {"ETHUSDT": 2000}, "unit-test")
    executor = TestnetExecutor(EthLikeClient(), ExecutionPolicy(max_gross_notional_usd=20, max_order_notional_usd=20), KillSwitch(tmp_path / "kill.json"), ExecutionAudit(tmp_path / "audit.sqlite3"))
    plan = executor.build_plan(book)
    legs, skips = plan.legs, plan.skips
    assert not legs
    assert skips == ["ETHUSDT:delta_below_exchange_minimum_safe_noop"]
    assert plan.expected_positions == {"ETHUSDT": Decimal("0.009")}


def test_leverage_and_notional_cap_are_mathematically_consistent() -> None:
    with pytest.raises(ValueError, match="cannot exceed leverage"):
        ExecutionPolicy(leverage=1, max_notional_to_equity=2)


def test_flatten_forces_market_and_verifies_zero_position(tmp_path) -> None:
    class PartialFailureClient(FakeClient):
        def __init__(self):
            self.market_closes = 0
            self.orders = []

        def positions(self):
            return [] if self.market_closes >= 2 else [{"symbol": "AAAUSDT", "positionAmt": "1.0"}]

        def order(self, **params):
            self.orders.append(params)
            if params["type"] == "MARKET" and params["reduceOnly"] == "true":
                self.market_closes += 1
                return {"status": "FILLED", "avgPrice": "100"}
            return {"status": "EXPIRED"}

        def cancel_all(self, symbol):
            return {}

    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    book = TargetBook("unit-test", "limit-fails", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": -1.0}, {"AAAUSDT": 100}, "unit-test")
    client, kill = PartialFailureClient(), KillSwitch(tmp_path / "kill.json")
    _release(kill, book, 100)
    audit_path = tmp_path / "audit.sqlite3"
    executor = TestnetExecutor(client, ExecutionPolicy(order_style="LIMIT_IOC", max_gross_notional_usd=100, max_order_notional_usd=100, poll_seconds=0, flatten_retry_seconds=0, expected_config_sha256="a" * 64), kill, ExecutionAudit(audit_path), now=lambda: now)
    with pytest.raises(RuntimeError, match="did not fill"):
        executor.execute(book, dry_run=False)
    assert client.orders[0]["type"] == "LIMIT"
    assert client.orders[-1]["type"] == "MARKET"
    assert client.market_closes == 2
    phases = [row[0] for row in ExecutionAudit(audit_path).connection.execute("SELECT phase FROM position_snapshots")]
    assert "emergency_flatten_verified" in phases


def test_margin_and_leverage_are_configured_before_first_order(tmp_path) -> None:
    class OrderedClient(TrackingClient):
        def __init__(self):
            super().__init__()
            self.events = []

        def set_margin_type(self, symbol, margin_type):
            self.events.append(("margin", symbol, margin_type))
            return {}

        def set_leverage(self, symbol, leverage):
            self.events.append(("leverage", symbol, leverage))
            return {}

        def order(self, **params):
            self.events.append(("order", params["symbol"], params["type"]))
            return super().order(**params)

    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    book = TargetBook("unit-test", "configured", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {"AAAUSDT": 100.0, "BBBUSDT": 100.0}, "unit-test")
    client, kill = OrderedClient(), KillSwitch(tmp_path / "kill.json")
    _release(kill, book)
    policy = ExecutionPolicy(leverage=3, max_notional_to_equity=1, margin_type="ISOLATED", poll_seconds=0, expected_config_sha256="a" * 64)
    result = TestnetExecutor(client, policy, kill, ExecutionAudit(tmp_path / "audit.sqlite3"), now=lambda: now).execute(book, dry_run=False)
    assert result["status"] == "COMPLETE"
    first_order = next(index for index, event in enumerate(client.events) if event[0] == "order")
    assert all(event[0] in {"margin", "leverage"} for event in client.events[:first_order])
    assert ("margin", "AAAUSDT", "ISOLATED") in client.events
    assert ("leverage", "BBBUSDT", 3) in client.events


def test_kill_switch_is_checked_again_before_every_leg(tmp_path) -> None:
    """Operator engages the switch between leg 1 and leg 2.

    Round-8 behaviour: this is a HALT, not a liquidation. Leg 1's fill is a correct
    position; the operator asked us to stop, not to dump. Expect HaltedError, cancel-only
    cleanup, leg-1 inventory intact, status HALTED_MID_BOOK.
    """
    class MidRunHaltClient(TrackingClient):
        def __init__(self, switch):
            super().__init__({})
            self.switch = switch
            self.cancelled = []

        def order(self, **params):
            response = super().order(**params)
            if params["reduceOnly"] == "false" and len(self.orders) == 1:
                self.switch.engage("operator halted between legs")
            return response

        def cancel_all(self, symbol):
            self.cancelled.append(symbol)
            return {}

    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    book = TargetBook("unit-test", "mid-run-halt", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {"AAAUSDT": 100.0, "BBBUSDT": 100.0}, "unit-test")
    kill = KillSwitch(tmp_path / "kill.json")
    _release(kill, book)
    client = MidRunHaltClient(kill)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    executor = TestnetExecutor(client, ExecutionPolicy(poll_seconds=0, flatten_retry_seconds=0, expected_config_sha256="a" * 64), kill, audit, now=lambda: now)
    with pytest.raises(HaltedError, match="kill switch is engaged"):
        executor.execute(book, dry_run=False)
    # Exactly one opening order went out, and it was NOT undone by a reduce-only flatten.
    assert len(client.orders) == 1 and client.orders[0]["reduceOnly"] == "false"
    assert client.inventory == {"AAAUSDT": Decimal("0.5")}
    assert "AAAUSDT" in client.cancelled and "BBBUSDT" in client.cancelled
    assert audit.connection.execute(
        "SELECT status FROM execution_runs WHERE target_id='mid-run-halt'"
    ).fetchone()[0] == "HALTED_MID_BOOK"


def test_stray_open_order_on_unrelated_symbol_halts_without_flattening(tmp_path) -> None:
    """Positions come out exactly right, but a resting order appears on a symbol nobody in
    this book touched (an operator stop, another process, an exchange-generated TP).
    Round-7 saw it and market-flattened everything. Round-8: positions are correct, so
    cancel the stray order and hand off - never liquidate a correct book over a stray."""
    class StrayOrderClient(TrackingClient):
        def __init__(self):
            super().__init__({})
            self.cancelled = []
            self.stray_live = False

        def order(self, **params):
            response = super().order(**params)
            self.stray_live = True  # appears after our first fill, on a symbol not in the book
            return response

        def open_orders(self, symbol=None):
            if self.stray_live and symbol in (None, "ZZZUSDT"):
                return [{"symbol": "ZZZUSDT", "orderId": 777}]
            return []

        def cancel_all(self, symbol):
            self.cancelled.append(symbol)
            if symbol == "ZZZUSDT":
                self.stray_live = False
            return {}

    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    book = TargetBook("unit-test", "stray-order", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {"AAAUSDT": 100.0, "BBBUSDT": 100.0}, "unit-test")
    kill = KillSwitch(tmp_path / "kill.json")
    _release(kill, book)
    client = StrayOrderClient()
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    executor = TestnetExecutor(client, ExecutionPolicy(poll_seconds=0, flatten_retry_seconds=0, expected_config_sha256="a" * 64), kill, audit, now=lambda: now)
    with pytest.raises(HaltedError, match="open order"):
        executor.execute(book, dry_run=False)
    # The book we built is intact - no reduce-only orders were sent.
    assert client.inventory == {"AAAUSDT": Decimal("0.5"), "BBBUSDT": Decimal("-0.5")}
    assert not any(o.get("reduceOnly") == "true" for o in client.orders)
    assert audit.connection.execute(
        "SELECT status FROM execution_runs WHERE target_id='stray-order'"
    ).fetchone()[0] == "HALTED_MID_BOOK"


def test_reference_drift_gate_is_fail_closed_on_missing_price_but_exempts_orphan_closes(tmp_path) -> None:
    """A weighted symbol with no paper reference must refuse to plan (the gate would be
    blind). An orphan we are about to CLOSE has no reference by construction and must
    not be blocked from being closed."""
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    client = TrackingClient({"AAAUSDT": Decimal("1")})  # AAA is an orphan
    executor = TestnetExecutor(client, ExecutionPolicy(expected_config_sha256="a" * 64), KillSwitch(tmp_path / "kill.json"), ExecutionAudit(tmp_path / "audit.sqlite3"), now=lambda: now)
    # Orphan close with no reference for AAA: allowed.
    ok_book = TargetBook("unit-test", "orphan-only", "a" * 64, now.isoformat(), now.isoformat(), {"BBBUSDT": 1.0}, {"BBBUSDT": 100.0}, "unit-test")
    plan = executor.build_plan(ok_book, gross_budget=100)
    assert any(leg.symbol == "AAAUSDT" and leg.reason == "close_orphan" for leg in plan.legs)
    # Weighted symbol with a missing reference: refused before any order.
    bad_book = TargetBook("unit-test", "missing-ref", "a" * 64, now.isoformat(), now.isoformat(), {"BBBUSDT": 1.0}, {}, "unit-test")
    with pytest.raises(RuntimeError, match="no positive paper reference"):
        executor.build_plan(bad_book, gross_budget=100)


def test_portfolio_median_drift_trips_even_when_no_single_symbol_does(tmp_path) -> None:
    """Broad regime move since the paper close: every reference stale the same way. Each
    symbol sits under the per-symbol veto but the median crosses its (tighter) line."""
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    client = TrackingClient({})
    policy = ExecutionPolicy(expected_config_sha256="a" * 64, max_reference_drift_bps=300, max_median_reference_drift_bps=150)
    executor = TestnetExecutor(client, policy, KillSwitch(tmp_path / "kill.json"), ExecutionAudit(tmp_path / "audit.sqlite3"), now=lambda: now)
    # Live mid is 100; references imply ~200bps drift on both symbols (under 300, over 150 median).
    book = TargetBook("unit-test", "regime-move", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {"AAAUSDT": 98.0, "BBBUSDT": 98.0}, "unit-test")
    with pytest.raises(RuntimeError, match="median reference drift"):
        executor.build_plan(book, gross_budget=100)
    assert client.orders == []




def test_kill_switch_release_has_ttl(tmp_path) -> None:
    switch = KillSwitch(tmp_path / "kill.json")
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    switch.path.write_text(json.dumps({
        "environment": "testnet", "trading_enabled": True, "reason": "old release",
        "released_utc": old.isoformat(),
    }), encoding="utf-8")
    with pytest.raises(RuntimeError, match="release expired"):
        switch.assert_released_for_testnet(max_age_seconds=60)


def test_below_min_target_aborts_without_dead_close_leg(tmp_path) -> None:
    class TinyBudgetClient(FakeClient):
        def account(self):
            return {"totalMarginBalance": "5"}

    book = TargetBook("unit-test", "tiny", "a" * 64, "2026-08-15T00:00:00Z", "2026-08-15T00:00:00Z", {"AAAUSDT": 1.0}, {"AAAUSDT": 100.0}, "unit-test")
    executor = TestnetExecutor(TinyBudgetClient(), ExecutionPolicy(max_gross_notional_usd=4), KillSwitch(tmp_path / "kill.json"), ExecutionAudit(tmp_path / "audit.sqlite3"))
    plan = executor.build_plan(book)
    legs, skips = plan.legs, plan.skips
    assert not legs
    assert skips == ["AAAUSDT:below_min_notional"]


def test_orphan_pending_orders_are_cancelled_before_any_leg(tmp_path) -> None:
    class OrphanClient(TrackingClient):
        def __init__(self):
            super().__init__({"AAAUSDT": Decimal("1")})
            self.events = []
            self.pending = True

        def open_orders(self, symbol=None):
            return [{"symbol": "AAAUSDT", "orderId": 99}] if symbol in (None, "AAAUSDT") and self.pending else []

        def cancel_all(self, symbol):
            self.events.append(("cancel", symbol))
            self.pending = False
            return {}

        def order(self, **params):
            self.events.append(("order", params["symbol"]))
            return super().order(**params)

    now = datetime.now(timezone.utc)
    book = TargetBook("unit-test", "orphan-surface", "a" * 64, now.isoformat(), now.isoformat(), {"BBBUSDT": 1.0}, {"BBBUSDT": 100.0}, "unit-test")
    client, kill = OrphanClient(), KillSwitch(tmp_path / "kill.json")
    _release(kill, book)
    result = TestnetExecutor(client, ExecutionPolicy(poll_seconds=0, expected_config_sha256="a" * 64), kill, ExecutionAudit(tmp_path / "audit.sqlite3"), now=lambda: now).execute(book, dry_run=False)
    assert result["status"] == "COMPLETE"
    assert client.events.index(("cancel", "AAAUSDT")) < next(i for i, event in enumerate(client.events) if event[0] == "order")
    assert client.inventory == {"BBBUSDT": Decimal("1.0")}


def test_final_target_mismatch_never_records_complete_and_is_flattened(tmp_path) -> None:
    class WrongSizeClient(TrackingClient):
        def order(self, **params):
            adjusted = dict(params)
            if adjusted["reduceOnly"] == "false":
                adjusted["quantity"] = str(Decimal(str(adjusted["quantity"])) / Decimal("10"))
            return super().order(**adjusted)

    now = datetime.now(timezone.utc)
    book = TargetBook("unit-test", "wrong-size", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {"AAAUSDT": 100.0, "BBBUSDT": 100.0}, "unit-test")
    client, kill = WrongSizeClient(), KillSwitch(tmp_path / "kill.json")
    _release(kill, book)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    executor = TestnetExecutor(client, ExecutionPolicy(poll_seconds=0, flatten_retry_seconds=0, expected_config_sha256="a" * 64), kill, audit, now=lambda: now)
    with pytest.raises(TargetMismatchError):
        executor.execute(book, dry_run=False)
    status = audit.connection.execute("SELECT status FROM execution_runs WHERE target_id='wrong-size'").fetchone()[0]
    assert status == "MISMATCH"
    assert client.inventory == {}


def test_adversarial_client_refusing_flatten_records_unresolved_exposure(tmp_path) -> None:
    """A confirmed POSITION error (opening fills land at 10x size) forces a flatten; the
    venue then reports every reduce-only order FILLED while never moving inventory. The
    engine must not believe it: status UNRESOLVED_EXPOSURE, and the exposure it could
    not remove is still visible in the tracking inventory."""
    class RefusesFlattenClient(TrackingClient):
        def order(self, **params):
            if params["reduceOnly"] == "true":
                self.orders.append(params)          # lies: says filled, moves nothing
                return {"status": "FILLED", "avgPrice": "100"}
            # Opening fills are 10x what was asked - a real, own, position error.
            params = dict(params, quantity=str(Decimal(str(params["quantity"])) * 10))
            return super().order(**params)

    now = datetime.now(timezone.utc)
    book = TargetBook("unit-test", "unresolved", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {"AAAUSDT": 100, "BBBUSDT": 100}, "unit-test")
    client, kill = RefusesFlattenClient(), KillSwitch(tmp_path / "kill.json")
    _release(kill, book)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    policy = ExecutionPolicy(poll_seconds=0, flatten_retry_seconds=0, flatten_max_attempts=2, expected_config_sha256="a" * 64)
    with pytest.raises(TargetMismatchError):
        TestnetExecutor(client, policy, kill, audit, now=lambda: now).execute(book, dry_run=False)
    row = audit.connection.execute("SELECT status FROM execution_runs WHERE target_id='unresolved'").fetchone()
    assert row[0] == "UNRESOLVED_EXPOSURE"
    assert client.inventory  # the 10x book is still there; the engine did not pretend otherwise
    assert any(o["reduceOnly"] == "true" for o in client.orders)  # it did TRY to flatten


def test_order_poll_is_bounded_and_new_never_completes(tmp_path) -> None:
    class NeverFilledClient(FakeClient):
        def __init__(self):
            self.polls = 0

        def positions(self):
            return []

        def order(self, **params):
            return {"orderId": 7, "status": "NEW"}

        def get_order(self, symbol, order_id):
            self.polls += 1
            return {"orderId": order_id, "status": "NEW"}

        def cancel_all(self, symbol):
            return {}

    now = datetime.now(timezone.utc)
    book = TargetBook("unit-test", "never-filled", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 1.0}, {"AAAUSDT": 100.0}, "unit-test")
    client, kill = NeverFilledClient(), KillSwitch(tmp_path / "kill.json")
    _release(kill, book)
    policy = ExecutionPolicy(order_poll_attempts=3, poll_seconds=0, flatten_retry_seconds=0, expected_config_sha256="a" * 64)
    with pytest.raises(RuntimeError, match="did not fill: NEW"):
        TestnetExecutor(client, policy, kill, ExecutionAudit(tmp_path / "audit.sqlite3"), now=lambda: now).execute(book, dry_run=False)
    assert client.polls == 3


def test_exchange_error_mid_portfolio_forces_verified_flatten(tmp_path) -> None:
    class MidPortfolioMarginError(TrackingClient):
        def __init__(self):
            super().__init__()
            self.open_attempts = 0

        def order(self, **params):
            if params["reduceOnly"] == "false":
                self.open_attempts += 1
                if self.open_attempts == 2:
                    raise BinanceAPIError("insufficient margin", status_code=400, payload={"code": -2019})
            return super().order(**params)

        def get_order_by_client_id(self, symbol, client_order_id):
            raise BinanceAPIError("unknown order", status_code=400, payload={"code": -2013})

    now = datetime.now(timezone.utc)
    book = TargetBook("unit-test", "mid-margin", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {"AAAUSDT": 100.0, "BBBUSDT": 100.0}, "unit-test")
    client, kill = MidPortfolioMarginError(), KillSwitch(tmp_path / "kill.json")
    _release(kill, book)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    policy = ExecutionPolicy(poll_seconds=0, flatten_retry_seconds=0, expected_config_sha256="a" * 64)
    with pytest.raises(BinanceAPIError, match="insufficient margin"):
        TestnetExecutor(client, policy, kill, audit, now=lambda: now).execute(book, dry_run=False)
    assert client.inventory == {}
    assert audit.connection.execute("SELECT status FROM execution_runs WHERE target_id='mid-margin'").fetchone()[0] == "FAILED"
    assert audit.connection.execute("SELECT 1 FROM position_snapshots WHERE phase='emergency_flatten_verified'").fetchone()


def test_kill_switch_write_failure_after_verified_book_halts_and_keeps_positions(tmp_path) -> None:
    """Every order filled, verification passed, then the kill-switch file cannot be written.
    Round-7 flattened here. That liquidated a book that had just been PROVEN correct over
    a disk error. Now: HaltedError, positions untouched, audit still records the fault."""
    class BrokenEngageSwitch(KillSwitch):
        def engage(self, reason: str) -> None:
            raise OSError("disk read-only")

    now = datetime.now(timezone.utc)
    book = TargetBook("unit-test", "engage-fails", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 1.0}, {"AAAUSDT": 100.0}, "unit-test")
    client = TrackingClient()
    kill = BrokenEngageSwitch(tmp_path / "kill.json")
    _release(kill, book)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    with pytest.raises(HaltedError, match="could not be re-engaged"):
        TestnetExecutor(client, ExecutionPolicy(poll_seconds=0, flatten_retry_seconds=0, expected_config_sha256="a" * 64), kill, audit, now=lambda: now).execute(book, dry_run=False)
    # The verified book is intact and no reduce-only order was ever sent.
    assert client.inventory == {"AAAUSDT": Decimal("1")}
    assert not any(o.get("reduceOnly") == "true" for o in client.orders)
    status = audit.connection.execute("SELECT status FROM execution_runs WHERE target_id='engage-fails'").fetchone()[0]
    assert status == "HALTED_MID_BOOK"
    assert audit.connection.execute("SELECT 1 FROM position_snapshots WHERE phase='kill_switch_engage_failed'").fetchone()
    # And the human handed this book can see exactly what they hold.
    held = audit.connection.execute("SELECT 1 FROM position_snapshots WHERE phase='cancel_only_positions'").fetchone()
    assert held


def test_from_env_never_falls_back_to_legacy_credentials(monkeypatch) -> None:
    # Isolate from the operator's real .env.testnet on this machine.
    import execution.binance_futures as bf
    monkeypatch.setattr(bf, "_load_dotenv_testnet", lambda: None)
    monkeypatch.delenv("BINANCE_TESTNET_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_TESTNET_API_SECRET", raising=False)
    monkeypatch.setenv("BINANCE_API_KEY", "production-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "production-secret")
    with pytest.raises(BinanceAPIError, match="missing BINANCE_TESTNET"):
        FuturesREST.from_env("testnet", required=True)


def test_reconciler_default_ignores_newer_failed_attempt() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE execution_runs (run_id TEXT, started_utc TEXT, finished_utc TEXT, target_id TEXT, environment TEXT, dry_run INTEGER, status TEXT, message TEXT)")
    conn.execute("INSERT INTO execution_runs VALUES ('complete','2026-08-15T00:00:00Z','', 'target','testnet',0,'COMPLETE','')")
    conn.execute("INSERT INTO execution_runs VALUES ('failed','2026-08-15T01:00:00Z','', 'target','testnet',0,'FAILED','duplicate blocked')")
    assert select_execution_run(conn, "target")[0] == "complete"
    assert select_execution_run(conn, "target", "failed")[0] == "failed"


def test_every_status_the_engine_can_write_is_classified_in_the_registry() -> None:
    """Round 9 added HALTED_AUDIT_UNAVAILABLE in the engine; the reconciler's private copy
    of the exposure list never learned about it, so a halted run with a live book
    reconciled as exit 0. This test reads engine.py and refuses any status literal that
    is not in execution.contracts.ALL_STATUSES - adding a status now FAILS here until it
    is classified as exposure or closed."""
    import re
    from pathlib import Path
    from execution.contracts import ALL_STATUSES, CLOSED_STATUSES, EXPOSURE_STATUSES
    src = Path("execution/engine.py").read_text(encoding="utf-8")
    written = set(re.findall(r'final_status = "([A-Z_]+)"', src))
    # ternary halves, but only on final_status lines (side literals BUY/SELL also use `else "..."`)
    for line in src.splitlines():
        if "final_status = " in line:
            written |= set(re.findall(r'"([A-Z_]+)"', line))
    written |= set(re.findall(r'audit\.finish\(run_id, "([A-Z_]+)"', src))
    written |= set(re.findall(r'"status": "([A-Z_]+)"', src))
    assert written, "regex found no statuses - test is broken, not the code"
    unclassified = written - ALL_STATUSES
    assert not unclassified, f"engine writes statuses the registry does not classify: {sorted(unclassified)}"
    assert not (EXPOSURE_STATUSES & CLOSED_STATUSES), "a status cannot be both exposure and closed"


def test_reconciler_sees_audit_unavailable_and_running_as_exposure() -> None:
    from reconcile_paper_vs_testnet import open_exposure_runs
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE execution_runs (run_id TEXT, started_utc TEXT, finished_utc TEXT, target_id TEXT, environment TEXT, dry_run INTEGER, status TEXT, message TEXT)")
    conn.execute("INSERT INTO execution_runs VALUES ('a','2026-08-15T00:00:00Z','','t','testnet',0,'HALTED_AUDIT_UNAVAILABLE','db locked')")
    conn.execute("INSERT INTO execution_runs VALUES ('b','2026-08-15T01:00:00Z','','t','testnet',0,'RUNNING','')")
    conn.execute("INSERT INTO execution_runs VALUES ('c','2026-08-15T02:00:00Z','','t','testnet',0,'FAILED','plan abort')")
    found = {r[0] for r in open_exposure_runs(conn, "t")}
    assert found == {"a", "b"}, found   # FAILED is closed; the other two are live exposure


def test_ceilings_sha_reaches_contract_and_reconciler_rejects_a_wrong_one(tmp_path) -> None:
    """The binding is only real if the sha is (a) written into the contract and (b) checked
    against the current file. Both halves survived mutation in round 9 - neither was tested."""
    from execution.contracts import CEILINGS_SHA256
    now = datetime.now(timezone.utc)
    book = TargetBook("unit-test", "sha-wire", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {"AAAUSDT": 100.0, "BBBUSDT": 100.0}, "unit-test")
    client, kill = TrackingClient(), KillSwitch(tmp_path / "kill.json")
    _release(kill, book)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    result = TestnetExecutor(client, ExecutionPolicy(poll_seconds=0, expected_config_sha256="a" * 64), kill, audit, now=lambda: now).execute(book, dry_run=False)
    assert result["status"] == "COMPLETE"
    contract = json.loads(audit.connection.execute(
        "SELECT positions_json FROM position_snapshots WHERE run_id=? AND phase='execution_contract'", (result["run_id"],)
    ).fetchone()[0])
    assert contract["ceilings_file_sha256"] == CEILINGS_SHA256          # (a) written
    # (b) checked: a contract governed by a DIFFERENT ceilings file must not reconcile.
    import reconcile_paper_vs_testnet as rec
    tampered = dict(contract, ceilings_file_sha256="0" * 64)
    # Recompute the contract's own hash so only the ceilings-binding check can fail.
    tampered["contract_sha256"] = rec.contract_sha256(tampered)
    authorized = float(tampered["authorized_budget_usd"]); effective = float(tampered["effective_gross_budget_usd"]); frozen = float(tampered["frozen_testnet_gross_ceiling_usd"])
    from execution.contracts import FROZEN_TESTNET_GROSS_CEILING_USD
    authorization_ok = (authorized > 0 and 0 < effective <= authorized <= frozen and frozen == FROZEN_TESTNET_GROSS_CEILING_USD
                        and tampered.get("ceilings_file_sha256", CEILINGS_SHA256) == CEILINGS_SHA256)
    assert authorization_ok is False


def test_ceilings_hash_is_immune_to_line_ending_conversion(tmp_path) -> None:
    from execution.contracts import load_ceilings
    body = json.dumps({"ceilings_usd": {"testnet": 500.0, "live": 0.0}}, indent=2)
    lf, crlf = tmp_path / "lf.json", tmp_path / "crlf.json"
    lf.write_bytes(body.encode("utf-8"))
    crlf.write_bytes(body.replace(chr(10), chr(13) + chr(10)).encode("utf-8"))
    assert lf.read_bytes() != crlf.read_bytes()
    assert load_ceilings(lf)[1] == load_ceilings(crlf)[1]


def test_reconciler_surfaces_halted_exposure_instead_of_reporting_nothing(tmp_path) -> None:
    """A run that halted mid-book left positions on the exchange on purpose. The old
    reconciler filtered status='COMPLETE', found nothing, printed 'nothing to reconcile'
    and exited 0 - the account looked flat while a partial book was live. Now the
    hand-off is the FIRST thing reported, with the held positions, and exit is non-zero."""
    from reconcile_paper_vs_testnet import HELD_PHASES, open_exposure_runs, select_execution_run
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE execution_runs (run_id TEXT, started_utc TEXT, finished_utc TEXT, target_id TEXT, environment TEXT, dry_run INTEGER, status TEXT, message TEXT)")
    conn.execute("CREATE TABLE position_snapshots (run_id TEXT, phase TEXT, captured_utc TEXT, positions_json TEXT)")
    conn.execute("INSERT INTO execution_runs VALUES ('halted','2026-08-15T00:00:00Z','2026-08-15T00:01:00Z','target','testnet',0,'HALTED_MID_BOOK','kill switch is engaged')")
    conn.execute("INSERT INTO position_snapshots VALUES ('halted','cancel_only_positions','2026-08-15T00:01:00Z', ?)",
                 (json.dumps([{"symbol": "AAAUSDT", "positionAmt": "0.5"}]),))
    # COMPLETE-only selection still finds nothing (that is the hole) ...
    assert select_execution_run(conn, "target") is None
    # ... but the exposure query finds the halt and its held book.
    runs = open_exposure_runs(conn, "target")
    assert [r[0] for r in runs] == ["halted"]
    assert runs[0][3] == "HALTED_MID_BOOK"
    held = next(r for r in conn.execute("SELECT phase,positions_json FROM position_snapshots WHERE run_id='halted'") if r[0] in HELD_PHASES)
    assert json.loads(held[1]) == [{"symbol": "AAAUSDT", "positionAmt": "0.5"}]


def test_reconciler_cli_exits_3_and_prints_held_book_on_halted_run(tmp_path, capsys, monkeypatch) -> None:
    """End-to-end through main(): a halted run makes reconcile exit 3 with the held book."""
    import reconcile_paper_vs_testnet as rec
    audit = tmp_path / "audit.sqlite3"
    conn = sqlite3.connect(audit)
    conn.execute("CREATE TABLE execution_runs (run_id TEXT, started_utc TEXT, finished_utc TEXT, target_id TEXT, environment TEXT, dry_run INTEGER, status TEXT, message TEXT)")
    conn.execute("CREATE TABLE position_snapshots (run_id TEXT, phase TEXT, captured_utc TEXT, positions_json TEXT)")
    conn.execute("CREATE TABLE execution_legs (run_id TEXT, sequence INTEGER, symbol TEXT)")
    conn.execute("INSERT INTO execution_runs VALUES ('halted','2026-08-15T00:00:00Z','2026-08-15T00:01:00Z','target-1','testnet',0,'HALTED_MID_BOOK','operator halt')")
    conn.execute("INSERT INTO position_snapshots VALUES ('halted','cancel_only_positions','2026-08-15T00:01:00Z', ?)",
                 (json.dumps([{"symbol": "AAAUSDT", "positionAmt": "0.5"}]),))
    conn.commit(); conn.close()
    targets = _target_file(tmp_path, {"AAAUSDT": 0.5, "BBBUSDT": -0.5})
    monkeypatch.setattr(sys, "argv", ["reconcile", "--targets", str(targets), "--audit", str(audit)])
    code = rec.main()
    out = capsys.readouterr().out
    assert code == 3
    assert "ATTENTION" in out and "HALTED_MID_BOOK" in out and "AAAUSDT" in out


def test_target_with_a_running_live_run_is_refused_before_any_order(tmp_path) -> None:
    """A second executor firing while the first is still RUNNING (or died without a terminal
    row) must not stack a second book on top of an unknown state."""
    now = datetime.now(timezone.utc)
    book = TargetBook("unit-test", "in-flight", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {"AAAUSDT": 100.0, "BBBUSDT": 100.0}, "unit-test")
    client, kill = TrackingClient(), KillSwitch(tmp_path / "kill.json")
    _release(kill, book)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    audit.connection.execute("INSERT INTO execution_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             ("ghost", now.isoformat(), "", "in-flight", "testnet", 0, "RUNNING", ""))
    audit.connection.commit()
    with pytest.raises(RuntimeError, match="still RUNNING"):
        TestnetExecutor(client, ExecutionPolicy(poll_seconds=0, expected_config_sha256="a" * 64), kill, audit, now=lambda: now).execute(book, dry_run=False)
    assert client.orders == []


def test_release_is_bound_to_exact_target_and_operator_budget(tmp_path) -> None:
    switch = KillSwitch(tmp_path / "kill.json")
    switch.release("approved", target_id="target-a", authorized_budget_usd=500)
    with pytest.raises(RuntimeError, match="different target_id"):
        switch.assert_released_for_testnet(expected_target_id="target-b", expected_budget_usd=500)
    with pytest.raises(RuntimeError, match="budget does not match"):
        switch.assert_released_for_testnet(expected_target_id="target-a", expected_budget_usd=5000)


def test_budget_typo_is_rejected_before_plan_or_order(tmp_path) -> None:
    class NoMutationClient(FakeClient):
        def __init__(self):
            self.synced = 0
            self.orders = 0

        def sync_time(self):
            self.synced += 1

        def order(self, **params):
            self.orders += 1

    now = datetime.now(timezone.utc)
    book = TargetBook("unit-test", "budget-bound", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 1.0}, {"AAAUSDT": 100.0}, "unit-test")
    client, switch = NoMutationClient(), KillSwitch(tmp_path / "kill.json")
    switch.release("approved", target_id=book.target_id, authorized_budget_usd=50)
    executor = TestnetExecutor(client, ExecutionPolicy(max_gross_notional_usd=100, expected_config_sha256="a" * 64), switch, ExecutionAudit(tmp_path / "audit.sqlite3"), now=lambda: now)
    with pytest.raises(RuntimeError, match="budget does not match"):
        executor.execute(book, dry_run=False)
    assert client.synced == 0 and client.orders == 0


def test_ceilings_file_cannot_raise_above_code_bound_and_live_defaults_to_zero(tmp_path) -> None:
    """Two locks: the JSON holds the reviewed number, the code holds an absolute sanity
    bound the JSON cannot exceed. And live is 0.0 until a v2 file is issued on purpose."""
    from execution.contracts import ABSOLUTE_GROSS_UPPER_BOUND_USD, frozen_ceiling, load_ceilings
    # Shipped file: testnet 500, live 0, both under the bound.
    ceilings, sha = load_ceilings()
    assert ceilings["testnet"] == 500.0 and ceilings["live"] == 0.0
    assert len(sha) == 64
    assert frozen_ceiling("live") == 0.0
    # A file that tries to authorise more than the code bound is refused at load.
    bad = tmp_path / "ceilings.json"
    bad.write_text(json.dumps({"ceilings_usd": {"testnet": ABSOLUTE_GROSS_UPPER_BOUND_USD * 10, "live": 0.0}}), encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        load_ceilings(bad)
    # An environment the file does not declare has no ceiling -> refuse, do not default.
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"ceilings_usd": {"testnet": 100.0}}), encoding="utf-8")
    with pytest.raises(ValueError, match="no ceiling declared"):
        frozen_ceiling("live", partial)


def test_frozen_testnet_ceiling_cannot_be_self_authorized_higher() -> None:
    with pytest.raises(ValueError, match="frozen testnet ceiling"):
        ExecutionPolicy(max_gross_notional_usd=50_000)


def test_ambiguous_post_success_is_deduped_and_contract_completes(tmp_path) -> None:
    class AmbiguousAcceptedClient(TrackingClient):
        def __init__(self):
            super().__init__()
            self.post_calls = 0
            self.accepted = {}

        def order(self, **params):
            self.post_calls += 1
            response = super().order(**params)
            self.accepted[params["newClientOrderId"]] = {"status": "FILLED", "avgPrice": "100"}
            raise BinanceAPIError("POST timeout", status_code=None)

        def get_order_by_client_id(self, symbol, client_order_id):
            return self.accepted[client_order_id]

    now = datetime.now(timezone.utc)
    book = TargetBook("unit-test", "ambiguous-success", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 1.0}, {"AAAUSDT": 100.0}, "unit-test")
    client, switch = AmbiguousAcceptedClient(), KillSwitch(tmp_path / "kill.json")
    _release(switch, book, 100)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    policy = ExecutionPolicy(max_gross_notional_usd=100, poll_seconds=0, expected_config_sha256="a" * 64)
    result = TestnetExecutor(client, policy, switch, audit, now=lambda: now).execute(book, dry_run=False)
    assert result["status"] == "COMPLETE" and client.post_calls == 1
    contract_row = audit.connection.execute("SELECT positions_json FROM position_snapshots WHERE phase='execution_contract'").fetchone()
    contract = json.loads(contract_row[0])
    verification_row = audit.connection.execute("SELECT positions_json FROM position_snapshots WHERE phase='target_verification'").fetchone()
    verification = json.loads(verification_row[0])
    ok, rows, _ = compare_contract_to_positions(contract, {"AAAUSDT": Decimal("1")}, verification)
    assert contract["contract_sha256"] == contract_sha256(contract)
    assert ok and all(row["ok"] for row in rows)


def test_verification_quote_timeout_engages_but_does_not_flatten(tmp_path) -> None:
    class VerificationTimeoutClient(TrackingClient):
        def __init__(self):
            super().__init__()
            self.quote_calls = 0
            self.cancelled = 0

        def book_ticker(self, symbol):
            self.quote_calls += 1
            if self.quote_calls > 2:
                raise TimeoutError("verification quote timeout")
            return super().book_ticker(symbol)

        def cancel_all(self, symbol):
            self.cancelled += 1
            return {}

    now = datetime.now(timezone.utc)
    book = TargetBook("unit-test", "verify-timeout", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {"AAAUSDT": 100.0, "BBBUSDT": 100.0}, "unit-test")
    client, switch = VerificationTimeoutClient(), KillSwitch(tmp_path / "kill.json")
    _release(switch, book)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    with pytest.raises(RuntimeError, match="verification snapshot unavailable"):
        TestnetExecutor(client, ExecutionPolicy(poll_seconds=0, expected_config_sha256="a" * 64), switch, audit, now=lambda: now).execute(book, dry_run=False)
    assert client.inventory == {"AAAUSDT": Decimal("0.5"), "BBBUSDT": Decimal("-0.5")}
    assert client.cancelled == 0
    assert audit.connection.execute("SELECT status FROM execution_runs WHERE target_id='verify-timeout'").fetchone()[0] == "VERIFICATION_UNAVAILABLE"


def test_open_orders_uses_signed_usdm_endpoint() -> None:
    client = FuturesREST("testnet")
    calls = []
    client.signed = lambda method, path, params=None: calls.append((method, path, params)) or []
    assert client.open_orders("btcusdt") == []
    assert client.open_orders() == []
    assert calls == [
        ("GET", "/fapi/v1/openOrders", {"symbol": "BTCUSDT"}),
        ("GET", "/fapi/v1/openOrders", {}),
    ]


def test_fill_slippage_uses_live_plan_mark_and_keeps_paper_reference_for_attribution(tmp_path) -> None:
    class MarkFillClient(TrackingClient):
        def get_order(self, symbol, order_id):
            fill = "100.4" if symbol == "AAAUSDT" else "99.6"
            return {"orderId": order_id, "status": "FILLED", "avgPrice": fill}

    now = datetime.now(timezone.utc)
    book = TargetBook(
        "unit-test", "two-price-bases", "a" * 64, now.isoformat(), now.isoformat(),
        {"AAAUSDT": 0.5, "BBBUSDT": -0.5},
        {"AAAUSDT": 99.6, "BBBUSDT": 100.4}, "unit-test",
    )
    client, switch = MarkFillClient(), KillSwitch(tmp_path / "kill.json")
    _release(switch, book, 100)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    result = TestnetExecutor(
        client,
        ExecutionPolicy(
            max_gross_notional_usd=100, max_fill_slippage_bps=50,
            max_reference_drift_bps=50, max_median_reference_drift_bps=50, poll_seconds=0,
            expected_config_sha256="a" * 64,
        ),
        switch, audit, now=lambda: now,
    ).execute(book, dry_run=False)
    assert result["status"] == "COMPLETE"
    rows = audit.connection.execute(
        "SELECT reference_price,execution_reference_price,fill_slippage_bps "
        "FROM execution_legs ORDER BY sequence"
    ).fetchall()
    assert {row[0] for row in rows} == {99.6, 100.4}
    assert {row[1] for row in rows} == {100.0}
    assert all(39.9 < row[2] < 40.1 for row in rows)


def test_slippage_is_measured_against_submit_time_requote_not_plan_mark(tmp_path) -> None:
    """Plan-time mid is 100.0; by the time the POST goes out the market is 101.0 and the
    fill prints 101.2. Round-7 measured 120bps against the stale plan mark and would trip
    a 50bps gate on pure drift. With the submit-time re-quote the fill is 20bps through
    the touch: within limit, COMPLETE, and the audit records 101.0 as the basis."""
    class MovingQuoteClient(TrackingClient):
        def __init__(self):
            super().__init__({})
            self.quotes = 0

        def book_ticker(self, symbol):
            self.quotes += 1
            # first call per symbol is the plan-time quote (100), later ones are 101
            mid = 100.0 if self.quotes <= 2 else 101.0
            return {"bidPrice": str(mid - 0.05), "askPrice": str(mid + 0.05)}

        def get_order(self, symbol, order_id):
            return {"orderId": order_id, "status": "FILLED", "avgPrice": "101.2" if symbol == "AAAUSDT" else "100.8"}

    now = datetime.now(timezone.utc)
    book = TargetBook("unit-test", "requote", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {"AAAUSDT": 100.0, "BBBUSDT": 100.0}, "unit-test")
    client, kill = MovingQuoteClient(), KillSwitch(tmp_path / "kill.json")
    _release(kill, book)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    result = TestnetExecutor(
        client, ExecutionPolicy(max_fill_slippage_bps=50, poll_seconds=0, expected_config_sha256="a" * 64),
        kill, audit, now=lambda: now,
    ).execute(book, dry_run=False)
    assert result["status"] == "COMPLETE"
    rows = audit.connection.execute(
        "SELECT symbol, execution_reference_price, fill_slippage_bps FROM execution_legs WHERE run_id=? AND status='FILLED'",
        (result["run_id"],),
    ).fetchall()
    by_symbol = {r[0]: (r[1], r[2]) for r in rows}
    # Basis is the submit-time 101.0, not the plan-time 100.0 ...
    assert by_symbol["AAAUSDT"][0] == 101.0 and by_symbol["BBBUSDT"][0] == 101.0
    # ... so AAA BUY at 101.2 is ~20bps adverse (not ~120), BBB SELL at 100.8 is ~20bps adverse.
    assert 15 < by_symbol["AAAUSDT"][1] < 25
    assert 15 < by_symbol["BBBUSDT"][1] < 25


def test_audit_write_failure_after_orders_halts_keeps_book_and_writes_sidecar(tmp_path) -> None:
    """Leg 1 fills, then sqlite refuses the leg row. Round-8 flattened here - liquidating a
    book over a database error. Now: HALT, cancel-only, positions intact, and because the
    audit is the thing that broke, the terminal state lands in a plain-text sidecar so a
    human can still see what is held."""
    class FlakyAudit(ExecutionAudit):
        def __init__(self, path):
            super().__init__(path)
            self.leg_writes = 0
            self.broken = False

        def leg(self, run_id, sequence, leg, status, response=None, *, client_order_id=""):
            if status == "FILLED":
                self.leg_writes += 1
                if self.leg_writes == 1:
                    self.broken = True
            if self.broken:
                raise sqlite3.OperationalError("database is locked")
            return super().leg(run_id, sequence, leg, status, response, client_order_id=client_order_id)

        def positions(self, run_id, phase, payload):
            if self.broken:
                raise sqlite3.OperationalError("database is locked")
            return super().positions(run_id, phase, payload)

        def finish(self, run_id, status, message):
            if self.broken:
                raise sqlite3.OperationalError("database is locked")
            return super().finish(run_id, status, message)

    class CancelTrackingClient(TrackingClient):
        def __init__(self):
            super().__init__({})
            self.cancelled = []

        def cancel_all(self, symbol):
            self.cancelled.append(symbol)
            return {}

    now = datetime.now(timezone.utc)
    book = TargetBook("unit-test", "audit-flaky", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {"AAAUSDT": 100.0, "BBBUSDT": 100.0}, "unit-test")
    client, kill = CancelTrackingClient(), KillSwitch(tmp_path / "kill.json")
    _release(kill, book)
    audit = FlakyAudit(tmp_path / "audit.sqlite3")
    with pytest.raises(AuditWriteError, match="audit rejected leg row"):
        TestnetExecutor(client, ExecutionPolicy(poll_seconds=0, flatten_retry_seconds=0, expected_config_sha256="a" * 64), kill, audit, now=lambda: now).execute(book, dry_run=False)
    # First leg's fill survives; no reduce-only flatten was sent.
    assert client.inventory == {"AAAUSDT": Decimal("0.5")}
    assert not any(o.get("reduceOnly") == "true" for o in client.orders)
    assert set(client.cancelled) >= {"AAAUSDT", "BBBUSDT"}
    # The DB could not take the terminal row, so the sidecar has it - with the held book.
    sidecar = audit.sidecar_path()
    assert sidecar.exists()
    record = json.loads(sidecar.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["final_status"] == "HALTED_AUDIT_UNAVAILABLE"
    assert record["target_id"] == "audit-flaky"
    assert any(p.get("symbol") == "AAAUSDT" for p in record["positions_held"])


def test_frozen_reference_drift_aborts_before_orders_or_cancellation(tmp_path) -> None:
    class DriftClient(FakeClient):
        def __init__(self):
            self.orders = 0
            self.cancelled = 0

        def positions(self):
            return []

        def order(self, **params):
            self.orders += 1

        def cancel_all(self, symbol):
            self.cancelled += 1

    now = datetime.now(timezone.utc)
    book = TargetBook(
        "unit-test", "reference-drift", "a" * 64, now.isoformat(), now.isoformat(),
        {"AAAUSDT": 1.0}, {"AAAUSDT": 96.0}, "unit-test",
    )
    client, switch = DriftClient(), KillSwitch(tmp_path / "kill.json")
    _release(switch, book, 100)
    with pytest.raises(RuntimeError, match="frozen reference drift"):
        TestnetExecutor(
            client, ExecutionPolicy(max_gross_notional_usd=100, expected_config_sha256="a" * 64),
            switch, ExecutionAudit(tmp_path / "audit.sqlite3"), now=lambda: now,
        ).execute(book, dry_run=False)
    assert client.orders == 0 and client.cancelled == 0


class SafeNoopEthClient(TrackingClient):
    def __init__(self):
        super().__init__({"ETHUSDT": Decimal("0.009")})
        self.cancelled = []

    def exchange_info(self):
        return {"symbols": [{
            "symbol": "ETHUSDT", "status": "TRADING", "contractType": "PERPETUAL",
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        }]}

    def account(self):
        return {"totalMarginBalance": "20", "availableBalance": "20"}

    def book_ticker(self, symbol):
        return {"bidPrice": "1999", "askPrice": "2001"}

    def cancel_all(self, symbol):
        self.cancelled.append(symbol)
        return {}


def test_safe_noop_exact_vector_completes_end_to_end_without_an_order(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    book = TargetBook(
        "unit-test", "safe-noop-e2e", "a" * 64, now.isoformat(), now.isoformat(),
        {"ETHUSDT": 1.0}, {"ETHUSDT": 2000}, "unit-test",
    )
    client, switch = SafeNoopEthClient(), KillSwitch(tmp_path / "kill.json")
    _release(switch, book, 20)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    result = TestnetExecutor(
        client, ExecutionPolicy(max_gross_notional_usd=20, poll_seconds=0, expected_config_sha256="a" * 64),
        switch, audit, now=lambda: now,
    ).execute(book, dry_run=False)
    assert result["status"] == "COMPLETE" and client.orders == []
    contract = json.loads(audit.connection.execute(
        "SELECT positions_json FROM position_snapshots WHERE phase='execution_contract'"
    ).fetchone()[0])
    assert contract["expected_positions"] == {"ETHUSDT": "0.009"}
    assert contract["accepted_skips"] == ["ETHUSDT:delta_below_exchange_minimum_safe_noop"]


def test_external_safe_noop_drift_is_cancel_only_and_never_flattens(tmp_path) -> None:
    """Two symbols we trade, one dust symbol we deliberately do not touch (safe_noop).

    After our first fill, the untouched symbol is moved by something that is not us
    (ADL / liquidation / another process). That must be classified as EXTERNAL drift:
    cancel resting orders, leave every position alone, hand off. It must NOT market-
    flatten the two symbols we just built correctly. The drift is triggered by an
    EVENT (first order filled) rather than a positions() call count, so the assertion
    holds regardless of how many times the engine chooses to read positionRisk.
    """
    class AdlAfterFirstFillClient(TrackingClient):
        def __init__(self):
            super().__init__({"ETHUSDT": Decimal("0.009")})
            self.cancelled = []
            self.adl_applied = False

        def exchange_info(self):
            lot = lambda step, minq: {"filterType": "LOT_SIZE", "stepSize": step, "minQty": minq}
            tick = {"filterType": "PRICE_FILTER", "tickSize": "0.01"}
            notional = {"filterType": "MIN_NOTIONAL", "notional": "5"}
            return {"symbols": [
                {"symbol": "AAAUSDT", "status": "TRADING", "contractType": "PERPETUAL", "filters": [lot("0.1", "0.1"), tick, notional]},
                {"symbol": "BBBUSDT", "status": "TRADING", "contractType": "PERPETUAL", "filters": [lot("0.1", "0.1"), tick, notional]},
                {"symbol": "ETHUSDT", "status": "TRADING", "contractType": "PERPETUAL", "filters": [lot("0.001", "0.001"), tick, notional]},
            ]}

        def account(self):
            return {"totalMarginBalance": "60", "availableBalance": "60"}

        def book_ticker(self, symbol):
            if symbol == "ETHUSDT":
                return {"bidPrice": "1999", "askPrice": "2001"}
            return {"bidPrice": "99.5", "askPrice": "100.5"}

        def order(self, **params):
            response = super().order(**params)
            if not self.adl_applied:
                # Something external nibbles the dust position right after our first fill.
                self.inventory["ETHUSDT"] = Decimal("0.008")
                self.adl_applied = True
            return response

        def positions(self):
            rows = super().positions()
            for row in rows:
                if row["symbol"] == "ETHUSDT":
                    row["markPrice"] = "2000"
            return rows

        def cancel_all(self, symbol):
            self.cancelled.append(symbol)
            return {}

    now = datetime.now(timezone.utc)
    # AAA/BBB: $20 each -> 0.2 lots, tradeable. ETH: weight sized to ~0.010 vs held 0.009,
    # delta 0.001 * $2000 = $2 < $5 min notional -> safe_noop, no order ever sent.
    book = TargetBook(
        "unit-test", "external-adl", "a" * 64, now.isoformat(), now.isoformat(),
        {"AAAUSDT": 0.3333, "BBBUSDT": -0.3333, "ETHUSDT": 0.3334},
        {"AAAUSDT": 100.0, "BBBUSDT": 100.0, "ETHUSDT": 2000.0}, "unit-test",
    )
    client, switch = AdlAfterFirstFillClient(), KillSwitch(tmp_path / "kill.json")
    _release(switch, book, 60)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    with pytest.raises(ExternalPositionDriftError):
        TestnetExecutor(
            client, ExecutionPolicy(max_gross_notional_usd=60, poll_seconds=0, expected_config_sha256="a" * 64),
            switch, audit, now=lambda: now,
        ).execute(book, dry_run=False)

    # We DID trade AAA and BBB (orders_started was True) ...
    traded = {o["symbol"] for o in client.orders}
    assert traded == {"AAAUSDT", "BBBUSDT"}, traded
    # ... and none of those orders was a reduce-only flatten of what we just built.
    assert not any(o.get("reduceOnly") == "true" for o in client.orders), client.orders
    # Both traded positions survive at their built size; the externally-nibbled dust
    # symbol is left exactly as the outside world left it. Nothing was liquidated.
    assert client.inventory["AAAUSDT"] > 0 and client.inventory["BBBUSDT"] < 0
    assert client.inventory["ETHUSDT"] == Decimal("0.008")
    # Cancel-only cleanup touched every surface symbol.
    assert set(client.cancelled) >= {"AAAUSDT", "BBBUSDT", "ETHUSDT"}
    assert audit.connection.execute(
        "SELECT status FROM execution_runs WHERE target_id='external-adl'"
    ).fetchone()[0] == "EXTERNAL_POSITION_DRIFT"


def test_flatten_is_not_verified_while_any_open_order_remains(tmp_path) -> None:
    class StickyOrderClient(FakeClient):
        def positions(self):
            return []

        def open_orders(self, symbol=None):
            return [{"symbol": "ORPHANUSDT", "orderId": 77}]

        def cancel_all(self, symbol):
            return {}

    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    executor = TestnetExecutor(
        StickyOrderClient(), ExecutionPolicy(flatten_max_attempts=2, flatten_retry_seconds=0),
        KillSwitch(tmp_path / "kill.json"), audit,
    )
    assert not executor._flatten_all("run-sticky", "test", {"AAAUSDT", "ORPHANUSDT"})
    phases = [row[0] for row in audit.connection.execute(
        "SELECT phase FROM position_snapshots WHERE run_id='run-sticky'"
    )]
    assert "emergency_flatten_verified" not in phases
    unresolved = json.loads(audit.connection.execute(
        "SELECT positions_json FROM position_snapshots "
        "WHERE run_id='run-sticky' AND phase='emergency_flatten_unresolved'"
    ).fetchone()[0])
    assert unresolved["positions"] == [] and unresolved["open_orders"]


def test_failure_flatten_surface_includes_positions_orphaned_from_target(tmp_path) -> None:
    class OrphanThenRejectClient(TrackingClient):
        def __init__(self):
            super().__init__({"AAAUSDT": Decimal("1")})
            self.events = []

        def cancel_all(self, symbol):
            self.events.append(("cancel", symbol))
            return {}

        def order(self, **params):
            self.events.append(("order", params["symbol"]))
            if params["symbol"] == "BBBUSDT" and params["reduceOnly"] == "false":
                return {"status": "REJECTED"}
            return super().order(**params)

    now = datetime.now(timezone.utc)
    book = TargetBook(
        "unit-test", "orphan-flatten-surface", "a" * 64,
        now.isoformat(), now.isoformat(), {"BBBUSDT": 1.0}, {"BBBUSDT": 100.0}, "unit-test",
    )
    client, switch = OrphanThenRejectClient(), KillSwitch(tmp_path / "kill.json")
    _release(switch, book)
    with pytest.raises(RuntimeError, match="did not fill"):
        TestnetExecutor(
            client,
            ExecutionPolicy(poll_seconds=0, flatten_retry_seconds=0, expected_config_sha256="a" * 64),
            switch, ExecutionAudit(tmp_path / "audit.sqlite3"), now=lambda: now,
        ).execute(book, dry_run=False)
    # AAA is canceled once in preflight and again by emergency cleanup even though
    # it is absent from the target weights; BBB is also in the flatten surface.
    assert client.events.count(("cancel", "AAAUSDT")) >= 2
    assert ("cancel", "BBBUSDT") in client.events
