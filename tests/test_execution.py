from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from execution.engine import ExecutionAudit, ExecutionPolicy, KillSwitch, TargetMismatchError, TestnetExecutor
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
    book = TargetBook("unit-test", "planning-fails", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": -1.0}, {}, "unit-test")
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
    book = TargetBook("unit-test", "pre-post-timeout", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {}, "unit-test")
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
    legs, _ = executor.build_plan(book)
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
    legs, skips = executor.build_plan(book)
    assert not legs
    assert skips == ["ETHUSDT:delta_below_exchange_minimum_safe_noop"]
    plan = executor.build_plan(book)
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
    book = TargetBook("unit-test", "configured", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {}, "unit-test")
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
    class MidRunHaltClient(FakeClient):
        def __init__(self, switch):
            self.switch = switch
            self.submitted_orders = []

        def positions(self):
            return []

        def order(self, **params):
            if params["reduceOnly"] == "false":
                self.submitted_orders.append(params)
                if len(self.submitted_orders) == 1:
                    self.switch.engage("operator halted between legs")
            return {"status": "FILLED"}

        def cancel_all(self, symbol):
            return {}

    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    book = TargetBook("unit-test", "mid-run-halt", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {}, "unit-test")
    kill = KillSwitch(tmp_path / "kill.json")
    _release(kill, book)
    client = MidRunHaltClient(kill)
    executor = TestnetExecutor(client, ExecutionPolicy(poll_seconds=0, flatten_retry_seconds=0, expected_config_sha256="a" * 64), kill, ExecutionAudit(tmp_path / "audit.sqlite3"), now=lambda: now)
    with pytest.raises(RuntimeError, match="kill switch is engaged"):
        executor.execute(book, dry_run=False)
    assert len(client.submitted_orders) == 1


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

    book = TargetBook("unit-test", "tiny", "a" * 64, "2026-08-15T00:00:00Z", "2026-08-15T00:00:00Z", {"AAAUSDT": 1.0}, {}, "unit-test")
    executor = TestnetExecutor(TinyBudgetClient(), ExecutionPolicy(max_gross_notional_usd=4), KillSwitch(tmp_path / "kill.json"), ExecutionAudit(tmp_path / "audit.sqlite3"))
    legs, skips = executor.build_plan(book)
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
    book = TargetBook("unit-test", "orphan-surface", "a" * 64, now.isoformat(), now.isoformat(), {"BBBUSDT": 1.0}, {}, "unit-test")
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
    book = TargetBook("unit-test", "wrong-size", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {}, "unit-test")
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
    class RefusesFlattenClient(TrackingClient):
        def order(self, **params):
            if params["reduceOnly"] == "true":
                self.orders.append(params)
                return {"status": "FILLED", "avgPrice": "100"}
            return super().order(**params)

        def get_order(self, symbol, order_id):
            return {"orderId": order_id, "status": "FILLED", "avgPrice": "120"}

    now = datetime.now(timezone.utc)
    book = TargetBook("unit-test", "unresolved", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {"AAAUSDT": 100, "BBBUSDT": 100}, "unit-test")
    client, kill = RefusesFlattenClient(), KillSwitch(tmp_path / "kill.json")
    _release(kill, book)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    policy = ExecutionPolicy(max_fill_slippage_bps=5, poll_seconds=0, flatten_retry_seconds=0, flatten_max_attempts=2, expected_config_sha256="a" * 64)
    with pytest.raises(RuntimeError, match="slippage"):
        TestnetExecutor(client, policy, kill, audit, now=lambda: now).execute(book, dry_run=False)
    row = audit.connection.execute("SELECT status FROM execution_runs WHERE target_id='unresolved'").fetchone()
    assert row[0] == "UNRESOLVED_EXPOSURE"
    assert client.inventory


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
    book = TargetBook("unit-test", "never-filled", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 1.0}, {}, "unit-test")
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
    book = TargetBook("unit-test", "mid-margin", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {}, "unit-test")
    client, kill = MidPortfolioMarginError(), KillSwitch(tmp_path / "kill.json")
    _release(kill, book)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    policy = ExecutionPolicy(poll_seconds=0, flatten_retry_seconds=0, expected_config_sha256="a" * 64)
    with pytest.raises(BinanceAPIError, match="insufficient margin"):
        TestnetExecutor(client, policy, kill, audit, now=lambda: now).execute(book, dry_run=False)
    assert client.inventory == {}
    assert audit.connection.execute("SELECT status FROM execution_runs WHERE target_id='mid-margin'").fetchone()[0] == "FAILED"
    assert audit.connection.execute("SELECT 1 FROM position_snapshots WHERE phase='emergency_flatten_verified'").fetchone()


def test_kill_switch_write_failure_cannot_skip_flatten_or_audit(tmp_path) -> None:
    class BrokenEngageSwitch(KillSwitch):
        def engage(self, reason: str) -> None:
            raise OSError("disk read-only")

    now = datetime.now(timezone.utc)
    book = TargetBook("unit-test", "engage-fails", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 1.0}, {}, "unit-test")
    client = TrackingClient()
    kill = BrokenEngageSwitch(tmp_path / "kill.json")
    _release(kill, book)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    with pytest.raises(RuntimeError, match="could not re-engage"):
        TestnetExecutor(client, ExecutionPolicy(poll_seconds=0, flatten_retry_seconds=0, expected_config_sha256="a" * 64), kill, audit, now=lambda: now).execute(book, dry_run=False)
    assert client.inventory == {}
    status = audit.connection.execute("SELECT status FROM execution_runs WHERE target_id='engage-fails'").fetchone()[0]
    assert status == "FAILED"
    assert audit.connection.execute("SELECT 1 FROM position_snapshots WHERE phase='kill_switch_engage_failed'").fetchone()


def test_from_env_never_falls_back_to_legacy_credentials(monkeypatch) -> None:
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
    book = TargetBook("unit-test", "budget-bound", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 1.0}, {}, "unit-test")
    client, switch = NoMutationClient(), KillSwitch(tmp_path / "kill.json")
    switch.release("approved", target_id=book.target_id, authorized_budget_usd=500)
    executor = TestnetExecutor(client, ExecutionPolicy(max_gross_notional_usd=5000, expected_config_sha256="a" * 64), switch, ExecutionAudit(tmp_path / "audit.sqlite3"), now=lambda: now)
    with pytest.raises(RuntimeError, match="budget does not match"):
        executor.execute(book, dry_run=False)
    assert client.synced == 0 and client.orders == 0


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
    book = TargetBook("unit-test", "ambiguous-success", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 1.0}, {}, "unit-test")
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
    book = TargetBook("unit-test", "verify-timeout", "a" * 64, now.isoformat(), now.isoformat(), {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {}, "unit-test")
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
