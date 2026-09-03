"""Tests for ExecutionPolicy(order_style="MAKER_THEN_MARKET").

WHY: the paper ledger charges 10 bps per leg. On Binance USDT-M VIP0 a taker pays 5 bps and
a maker 2 bps, and a passive fill collects ~half the spread instead of paying it. With a
daily rebalance that is the largest free improvement to net return that leaves the locked
CARRY-7d signal untouched. The style is DARK by default: ExecutionPolicy() still means
MARKET, and the first test below pins the default path's audit rows to the exact values
the pre-change engine wrote, so the rehearsal history stays comparable.

Run:  python -B -m pytest -q tests/test_maker_style.py tests/test_execution.py

The fake exchange here subclasses tests/test_execution.py's TrackingClient and adds what a
post-only order needs: an order that RESTS (inventory moves only when it fills), partial
fills, a -5022 rejection, a -2011 cancel race, and a cancel that fails outright.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from execution.binance_futures import BinanceAPIError
from execution.engine import ExecutionAudit, ExecutionPolicy, HaltedError, KillSwitch, TestnetExecutor
from execution.targets import TargetBook
from test_execution import TrackingClient

SHA = "a" * 64
NOW = datetime(2026, 8, 15, tzinfo=timezone.utc)
LEG_COLUMNS = (
    "sequence,symbol,side,quantity,reduce_only,reference_price,reason,status,order_id,"
    "avg_fill_price,fill_slippage_bps,response_json,desired_quantity,current_quantity,"
    "target_weight,execution_reference_price"
)
# Audit rows the PRE-CHANGE engine wrote for _book() through TrackingClient with the
# default policy (captured from git HEAD before MAKER_THEN_MARKET existed). run_id and
# client_order_id are excluded because they derive from a fresh uuid per run.
GOLDEN_DEFAULT_ROWS = [
    (0, "AAAUSDT", "BUY", "0.5", 0, 100.0, "rebalance", "FILLED", "1", 100.0, 0.0,
     '{"orderId": 1, "status": "FILLED", "avgPrice": "100"}', "0.5", "0", 0.5, 100.0),
    (1, "BBBUSDT", "SELL", "0.5", 0, 100.0, "rebalance", "FILLED", "2", 100.0, 0.0,
     '{"orderId": 2, "status": "FILLED", "avgPrice": "100"}', "-0.5", "0", -0.5, 100.0),
]


def _book(target_id: str = "maker-book") -> TargetBook:
    return TargetBook(
        "unit-test", target_id, SHA, NOW.isoformat(), NOW.isoformat(),
        {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {"AAAUSDT": 100, "BBBUSDT": 100}, "unit-test",
    )


def _policy(**overrides) -> ExecutionPolicy:
    base = dict(max_gross_notional_usd=100, max_order_notional_usd=100, poll_seconds=0,
                flatten_retry_seconds=0, expected_config_sha256=SHA)
    base.update(overrides)
    return ExecutionPolicy(**base)


def _run(tmp_path, client, policy: ExecutionPolicy, book: TargetBook | None = None):
    book = book or _book()
    kill = KillSwitch(tmp_path / "kill.json")
    kill.release("test", target_id=book.target_id, authorized_budget_usd=100)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    executor = TestnetExecutor(client, policy, kill, audit, now=lambda: NOW)
    return executor, audit, book


def _leg_rows(audit: ExecutionAudit) -> list[tuple]:
    return [tuple(row) for row in audit.connection.execute(f"SELECT {LEG_COLUMNS} FROM execution_legs ORDER BY sequence")]


def _run_status(audit: ExecutionAudit) -> str:
    return audit.connection.execute("SELECT status FROM execution_runs").fetchone()[0]


class MakerClient(TrackingClient):
    """TrackingClient plus resting post-only orders.

    fill_fraction    share of a GTX order that fills once fills_after_polls get_order reads
                     have happened (1 = full fill, 0 = never fills).
    reject_code      raise BinanceAPIError with this code on every GTX submit.
    cancel_error     exception raised by cancel_order (None = cancel succeeds).
    race_fill        cancel_order fills the order fully and raises -2011 (lost the race).
    """

    def __init__(self, *, fill_fraction: str = "1", fills_after_polls: int = 1, reject_code: int | None = None,
                 cancel_error: Exception | None = None, race_fill: bool = False, positions=None) -> None:
        super().__init__(positions)
        self.fill_fraction = Decimal(fill_fraction)
        self.fills_after_polls = fills_after_polls
        self.reject_code = reject_code
        self.cancel_error = cancel_error
        self.race_fill = race_fill
        self.resting: dict[int, dict] = {}
        self.maker_orders: list[dict] = []
        self.market_orders: list[dict] = []
        self.market_by_id: dict[int, dict] = {}
        self.cancels: list[tuple[str, int]] = []
        self.polls: dict[int, int] = {}

    def _apply_fill(self, order_id: int, fraction: Decimal) -> None:
        rest = self.resting[order_id]
        quantity = Decimal(rest["params"]["quantity"])
        executed = (quantity * fraction).quantize(Decimal("0.1"))
        delta = executed - rest["executed"]
        if delta > 0:
            signed = delta if rest["params"]["side"] == "BUY" else -delta
            symbol = rest["params"]["symbol"]
            self.inventory[symbol] = self.inventory.get(symbol, Decimal("0")) + signed
            if self.inventory[symbol] == 0:
                self.inventory.pop(symbol)
        rest["executed"] = executed
        rest["status"] = "FILLED" if executed >= quantity else ("PARTIALLY_FILLED" if executed > 0 else "NEW")

    def _view(self, order_id: int) -> dict:
        rest = self.resting[order_id]
        return {
            "orderId": order_id, "status": rest["status"], "executedQty": str(rest["executed"]),
            "avgPrice": rest["params"]["price"] if rest["executed"] > 0 else "0",
            "clientOrderId": rest["params"]["newClientOrderId"],
        }

    def order(self, **params):
        if params.get("type") == "LIMIT" and params.get("timeInForce") == "GTX":
            self.maker_orders.append(params)
            if self.reject_code is not None:
                raise BinanceAPIError("Binance /fapi/v1/order failed", status_code=400,
                                      payload={"code": self.reject_code, "msg": "Order could not be filled as maker"})
            order_id = self.next_order_id
            self.next_order_id += 1
            self.resting[order_id] = {"params": params, "executed": Decimal("0"), "status": "NEW"}
            return self._view(order_id)
        assert params["type"] == "MARKET", params
        self.market_orders.append(params)
        response = super().order(**params)
        self.market_by_id[int(response["orderId"])] = params
        response["executedQty"] = params["quantity"]
        return response

    def get_order(self, symbol, order_id):
        if order_id in self.resting:
            self.polls[order_id] = self.polls.get(order_id, 0) + 1
            rest = self.resting[order_id]
            if rest["status"] in {"NEW", "PARTIALLY_FILLED"} and self.polls[order_id] >= self.fills_after_polls and self.fill_fraction > 0:
                self._apply_fill(order_id, self.fill_fraction)
            return self._view(order_id)
        response = super().get_order(symbol, order_id)
        response["executedQty"] = self.market_by_id[int(order_id)]["quantity"]
        return response

    def cancel_order(self, symbol, order_id):
        self.cancels.append((symbol, order_id))
        if self.cancel_error is not None:
            raise self.cancel_error
        if self.race_fill:
            self._apply_fill(order_id, Decimal("1"))
            raise BinanceAPIError("Binance /fapi/v1/order failed", status_code=400, payload={"code": -2011, "msg": "Unknown order sent."})
        rest = self.resting[order_id]
        if rest["status"] in {"NEW", "PARTIALLY_FILLED"}:
            rest["status"] = "CANCELED"
        return self._view(order_id)

    def cancel_all(self, symbol):
        for rest in self.resting.values():
            if rest["params"]["symbol"] == symbol and rest["status"] in {"NEW", "PARTIALLY_FILLED"}:
                rest["status"] = "CANCELED"
        return {}

    def open_orders(self, symbol=None):
        return [
            {"symbol": rest["params"]["symbol"], "orderId": order_id}
            for order_id, rest in self.resting.items()
            if rest["status"] in {"NEW", "PARTIALLY_FILLED"} and (symbol is None or rest["params"]["symbol"] == symbol)
        ]


# ---------------------------------------------------------------------------
# (f) the default path is unchanged
# ---------------------------------------------------------------------------

def test_default_policy_is_market_and_audit_rows_match_pre_change_golden(tmp_path) -> None:
    assert ExecutionPolicy().order_style == "MARKET"
    client = TrackingClient()
    executor, audit, book = _run(tmp_path, client, _policy())
    assert executor.execute(book, dry_run=False)["status"] == "COMPLETE"
    assert _leg_rows(audit) == GOLDEN_DEFAULT_ROWS
    assert [o["type"] for o in client.orders] == ["MARKET", "MARKET"]
    assert all("timeInForce" not in o for o in client.orders)
    ids = [row[0] for row in audit.connection.execute("SELECT client_order_id FROM execution_legs ORDER BY sequence")]
    assert all(cid.startswith("carry-") and len(cid) == len("carry-") + 28 for cid in ids)


def test_market_style_through_maker_capable_fake_never_sends_gtx(tmp_path) -> None:
    """Same scenario, same fake, both styles: MARKET rows equal the golden, MAKER rows differ
    only in what a maker fill legitimately changes (price, response shape)."""
    market_client = MakerClient()
    executor, audit, book = _run(tmp_path / "market", market_client, _policy())
    assert executor.execute(book, dry_run=False)["status"] == "COMPLETE"
    market_rows = _leg_rows(audit)
    assert market_client.maker_orders == [] and len(market_client.market_orders) == 2
    # TrackingClient.get_order carries no executedQty; MakerClient adds it. Strip that one
    # fake-side difference and the rows are the pre-change golden rows.
    stripped = [row[:11] + (json.dumps({k: v for k, v in json.loads(row[11]).items() if k != "executedQty"}),) + row[12:] for row in market_rows]
    assert stripped == GOLDEN_DEFAULT_ROWS

    maker_client = MakerClient()
    executor, audit, book = _run(tmp_path / "maker", maker_client, _policy(order_style="MAKER_THEN_MARKET"))
    assert executor.execute(book, dry_run=False)["status"] == "COMPLETE"
    maker_rows = _leg_rows(audit)
    for m_row, k_row in zip(market_rows, maker_rows):
        # identical plan columns: sequence, symbol, side, quantity, reduce_only, reference,
        # reason, status, desired, current, weight, execution reference
        assert m_row[:8] == k_row[:8]
        assert m_row[12:] == k_row[12:]
    assert maker_client.inventory == market_client.inventory == {"AAAUSDT": Decimal("0.5"), "BBBUSDT": Decimal("-0.5")}


# ---------------------------------------------------------------------------
# policy validation and the per-leg wait cap
# ---------------------------------------------------------------------------

def test_policy_accepts_new_style_and_validates_maker_wait() -> None:
    assert ExecutionPolicy(order_style="MAKER_THEN_MARKET").maker_wait_seconds == 10.0
    with pytest.raises(ValueError, match="order_style"):
        ExecutionPolicy(order_style="POST_ONLY")
    with pytest.raises(ValueError, match="maker_wait_seconds"):
        ExecutionPolicy(maker_wait_seconds=0)
    with pytest.raises(ValueError, match="maker_wait_seconds"):
        ExecutionPolicy(maker_wait_seconds=15 * 60)
    with pytest.raises(ValueError, match="maker_wait_seconds"):
        ExecutionPolicy(maker_wait_seconds=-1)
    assert ExecutionPolicy(maker_wait_seconds=15 * 60 - 1).maker_wait_seconds == 899


def test_per_leg_wait_is_capped_by_half_the_kill_switch_ttl(tmp_path) -> None:
    """A 34-leg day must fit inside half of the release TTL the engine enforces before
    every leg (policy.kill_switch_release_ttl_seconds), whatever maker_wait_seconds says."""
    ttl = ExecutionPolicy().kill_switch_release_ttl_seconds
    assert ttl == 15 * 60
    patient = _policy(order_style="MAKER_THEN_MARKET", maker_wait_seconds=60)
    executor, _, _ = _run(tmp_path / "a", MakerClient(), patient)
    assert executor._maker_wait_seconds(34) == pytest.approx(0.5 * ttl / 34)
    assert executor._maker_wait_seconds(34) == pytest.approx(13.2352941)
    assert 34 * executor._maker_wait_seconds(34) <= 0.5 * ttl
    assert executor._maker_wait_seconds(1) == 60
    assert executor._maker_wait_seconds(0) == 60  # degenerate: never divides by zero
    default = _policy(order_style="MAKER_THEN_MARKET")
    executor, _, _ = _run(tmp_path / "b", MakerClient(), default)
    assert executor._maker_wait_seconds(34) == 10.0  # the policy value is the binding one
    assert executor._maker_wait_seconds(80) == pytest.approx(0.5 * ttl / 80)
    shorter_ttl = _policy(order_style="MAKER_THEN_MARKET", kill_switch_release_ttl_seconds=120, maker_wait_seconds=30)
    executor, _, _ = _run(tmp_path / "c", MakerClient(), shorter_ttl)
    assert executor._maker_wait_seconds(34) == pytest.approx(60 / 34)


def test_maker_params_join_the_touch_without_crossing(tmp_path) -> None:
    executor, _, book = _run(tmp_path, MakerClient(), _policy(order_style="MAKER_THEN_MARKET"))
    plan = executor.build_plan(book, gross_budget=100)
    buy = next(leg for leg in plan.legs if leg.side == "BUY")
    sell = next(leg for leg in plan.legs if leg.side == "SELL")
    buy_params = executor._order_params(buy, "cid-buy")
    sell_params = executor._order_params(sell, "cid-sell")
    assert buy_params["type"] == "LIMIT" and buy_params["timeInForce"] == "GTX"
    assert Decimal(buy_params["price"]) == Decimal("99.9")     # best bid, not the ask
    assert Decimal(sell_params["price"]) == Decimal("100.1")   # best ask, not the bid
    assert buy_params["reduceOnly"] == "false" and buy_params["newClientOrderId"] == "cid-buy"
    # force_market is the fall-through path and must be a plain MARKET
    assert executor._order_params(buy, "cid-buy", force_market=True)["type"] == "MARKET"


# ---------------------------------------------------------------------------
# (a) full maker fill
# ---------------------------------------------------------------------------

def test_full_maker_fill_needs_no_taker_and_records_price_improvement(tmp_path) -> None:
    client = MakerClient(fill_fraction="1", fills_after_polls=2)
    executor, audit, book = _run(tmp_path, client, _policy(order_style="MAKER_THEN_MARKET"))
    assert executor.execute(book, dry_run=False)["status"] == "COMPLETE"
    assert len(client.maker_orders) == 2 and client.market_orders == [] and client.cancels == []
    assert client.inventory == {"AAAUSDT": Decimal("0.5"), "BBBUSDT": Decimal("-0.5")}
    assert client.open_orders() == []
    rows = _leg_rows(audit)
    assert [row[7] for row in rows] == ["FILLED", "FILLED"]
    buy, sell = rows
    assert buy[9] == pytest.approx(99.9) and sell[9] == pytest.approx(100.1)
    # slippage is measured against the re-quoted mid (100.0): a passive fill IMPROVES on it
    assert buy[10] == pytest.approx(-10.0) and sell[10] == pytest.approx(-10.0)
    payload = json.loads(buy[11])
    assert payload["maker"]["status"] == "FILLED" and payload["taker"] is None
    assert payload["maker_executed_qty"] == "0.5" and payload["taker_executed_qty"] == "0"
    assert payload["orderId"] == payload["maker"]["orderId"]
    assert _run_status(audit) == "COMPLETE"


# ---------------------------------------------------------------------------
# (b) partial fill, cancel at the deadline, MARKET the remainder
# ---------------------------------------------------------------------------

def test_partial_maker_fill_is_cancelled_and_remainder_goes_market(tmp_path) -> None:
    client = MakerClient(fill_fraction="0.6")
    executor, audit, book = _run(tmp_path, client, _policy(order_style="MAKER_THEN_MARKET"))
    assert executor.execute(book, dry_run=False)["status"] == "COMPLETE"
    assert len(client.maker_orders) == 2 and len(client.market_orders) == 2 and len(client.cancels) == 2
    assert [o["quantity"] for o in client.market_orders] == ["0.2", "0.2"]
    assert [o["type"] for o in client.market_orders] == ["MARKET", "MARKET"]
    # the taker gets its own client id; the maker keeps the leg's canonical one
    assert all(o["newClientOrderId"].startswith("carryt-") for o in client.market_orders)
    assert all(o["newClientOrderId"].startswith("carry-") for o in client.maker_orders)
    assert client.inventory == {"AAAUSDT": Decimal("0.5"), "BBBUSDT": Decimal("-0.5")}
    assert client.open_orders() == []
    buy, sell = _leg_rows(audit)
    assert buy[7] == "FILLED" and buy[3] == "0.5"        # the leg row still describes the whole leg
    assert buy[9] == pytest.approx((0.3 * 99.9 + 0.2 * 100.0) / 0.5)
    assert sell[9] == pytest.approx((0.3 * 100.1 + 0.2 * 100.0) / 0.5)
    payload = json.loads(buy[11])
    assert payload["maker"]["status"] == "CANCELED" and payload["maker"]["executedQty"] == "0.3"
    assert payload["taker"]["status"] == "FILLED" and payload["taker_executed_qty"] == "0.2"
    assert payload["remainder_after_maker"] == "0.2"
    ids = [row[0] for row in audit.connection.execute("SELECT client_order_id FROM execution_legs ORDER BY sequence")]
    assert ids == [o["newClientOrderId"] for o in client.maker_orders]
    assert _run_status(audit) == "COMPLETE"


def test_unfilled_maker_goes_fully_market_after_cancel(tmp_path) -> None:
    client = MakerClient(fill_fraction="0")
    executor, audit, book = _run(tmp_path, client, _policy(order_style="MAKER_THEN_MARKET"))
    assert executor.execute(book, dry_run=False)["status"] == "COMPLETE"
    assert [o["quantity"] for o in client.market_orders] == ["0.5", "0.5"] and len(client.cancels) == 2
    # poll_seconds == 0: exactly order_poll_attempts zero-time reads per resting order
    assert all(n == ExecutionPolicy().order_poll_attempts + 1 for n in client.polls.values())  # +1 = re-read after cancel
    buy = _leg_rows(audit)[0]
    assert buy[9] == pytest.approx(100.0) and buy[10] == pytest.approx(0.0)


def test_deadline_is_wall_clock_when_poll_seconds_is_positive(tmp_path) -> None:
    client = MakerClient(fill_fraction="0")
    executor, _, book = _run(tmp_path, client, _policy(order_style="MAKER_THEN_MARKET", poll_seconds=0.005))
    plan = executor.build_plan(book, gross_budget=100)
    leg = plan.legs[0]
    order = client.order(**executor._order_params(leg, "cid"))
    final = executor._wait_for_resting_order(leg, order, 0.03)
    assert final["status"] == "CANCELED" and client.cancels == [(leg.symbol, order["orderId"])]
    assert 2 <= client.polls[order["orderId"]] <= 20


# ---------------------------------------------------------------------------
# (c) the exchange rejects the post-only order: straight to MARKET
# ---------------------------------------------------------------------------

def test_gtx_rejection_falls_through_to_market_for_the_full_quantity(tmp_path) -> None:
    client = MakerClient(reject_code=-5022)
    executor, audit, book = _run(tmp_path, client, _policy(order_style="MAKER_THEN_MARKET"))
    assert executor.execute(book, dry_run=False)["status"] == "COMPLETE"
    assert len(client.maker_orders) == 2 and client.resting == {} and client.cancels == []
    assert [o["quantity"] for o in client.market_orders] == ["0.5", "0.5"]
    assert client.inventory == {"AAAUSDT": Decimal("0.5"), "BBBUSDT": Decimal("-0.5")}
    buy = _leg_rows(audit)[0]
    assert buy[7] == "FILLED" and buy[9] == pytest.approx(100.0)
    payload = json.loads(buy[11])
    assert payload["maker"]["rejected"]["code"] == -5022 and payload["taker"]["status"] == "FILLED"
    assert payload["orderId"] == payload["taker"]["orderId"]


def test_gtx_that_expires_immediately_is_treated_like_a_rejection(tmp_path) -> None:
    class ExpiringClient(MakerClient):
        def order(self, **params):
            response = super().order(**params)
            if params.get("timeInForce") == "GTX":
                self.resting[response["orderId"]]["status"] = "EXPIRED"
                return self._view(response["orderId"])
            return response

    client = ExpiringClient()
    executor, _, book = _run(tmp_path, client, _policy(order_style="MAKER_THEN_MARKET"))
    assert executor.execute(book, dry_run=False)["status"] == "COMPLETE"
    assert client.cancels == [] and [o["quantity"] for o in client.market_orders] == ["0.5", "0.5"]


def test_market_fallback_still_applies_the_fill_slippage_gate(tmp_path) -> None:
    class BadTakerFill(MakerClient):
        def get_order(self, symbol, order_id):
            response = super().get_order(symbol, order_id)
            if order_id not in self.resting:
                response["avgPrice"] = "101"   # +100bps on a BUY
            return response

    client = BadTakerFill(reject_code=-5022)
    executor, audit, book = _run(tmp_path, client, _policy(order_style="MAKER_THEN_MARKET", max_fill_slippage_bps=50))
    with pytest.raises(HaltedError, match="adverse fill slippage"):
        executor.execute(book, dry_run=False)
    assert _run_status(audit) == "HALTED_MID_BOOK"
    assert not any(o["reduceOnly"] == "true" for o in client.market_orders)   # no flatten


# ---------------------------------------------------------------------------
# (d) cancel loses the race to a fill: -2011 means re-read, not escalate
# ---------------------------------------------------------------------------

def test_cancel_race_minus_2011_rereads_the_filled_order(tmp_path) -> None:
    client = MakerClient(fill_fraction="0", race_fill=True)
    executor, audit, book = _run(tmp_path, client, _policy(order_style="MAKER_THEN_MARKET"))
    assert executor.execute(book, dry_run=False)["status"] == "COMPLETE"
    assert len(client.cancels) == 2 and client.market_orders == []
    assert client.inventory == {"AAAUSDT": Decimal("0.5"), "BBBUSDT": Decimal("-0.5")}
    buy = _leg_rows(audit)[0]
    payload = json.loads(buy[11])
    assert payload["maker"]["status"] == "FILLED" and payload["taker"] is None
    assert buy[9] == pytest.approx(99.9)


# ---------------------------------------------------------------------------
# (e) cancel fails for any other reason: halt, cancel-only, never flatten
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("error", [
    BinanceAPIError("Binance /fapi/v1/order failed", status_code=500, payload={"code": -1000, "msg": "unknown"}),
    BinanceAPIError("network failure calling /fapi/v1/order: timeout"),
    ConnectionError("socket closed"),
])
def test_cancel_failure_escalates_to_cancel_only_halt(tmp_path, error) -> None:
    client = MakerClient(fill_fraction="0", cancel_error=error)
    executor, audit, book = _run(tmp_path, client, _policy(order_style="MAKER_THEN_MARKET"))
    with pytest.raises(HaltedError, match="cancel of resting post-only order"):
        executor.execute(book, dry_run=False)
    assert _run_status(audit) == "HALTED_MID_BOOK"
    assert client.market_orders == []                    # no taker, no flatten
    assert client.open_orders() == []                    # _cancel_only swept the resting order
    assert client.inventory == {}                        # positions were LEFT as they were
    phases = [row[0] for row in audit.connection.execute("SELECT phase FROM position_snapshots")]
    assert "cancel_only_summary" in phases and not any(p.startswith("emergency_") for p in phases)
    summary = json.loads(audit.connection.execute("SELECT positions_json FROM position_snapshots WHERE phase='cancel_only_summary'").fetchone()[0])
    assert summary["verified"] is True and "post-only" in summary["reason"]
    assert json.loads((tmp_path / "kill.json").read_text(encoding="utf-8"))["trading_enabled"] is False
    # the leg row exists only for legs that returned; the halting leg is described by the run
    message = audit.connection.execute("SELECT message FROM execution_runs").fetchone()[0]
    assert "AAAUSDT rebalance" in message and "cancel-only" in message


def test_order_still_resting_after_cancel_is_a_halt(tmp_path) -> None:
    class StickyCancel(MakerClient):
        def cancel_order(self, symbol, order_id):
            self.cancels.append((symbol, order_id))
            return self._view(order_id)   # says ok, but the order is still NEW on re-read

    client = StickyCancel(fill_fraction="0")
    executor, audit, book = _run(tmp_path, client, _policy(order_style="MAKER_THEN_MARKET"))
    with pytest.raises(HaltedError, match="still NEW after cancel"):
        executor.execute(book, dry_run=False)
    assert _run_status(audit) == "HALTED_MID_BOOK" and client.market_orders == []


def test_ambiguous_submit_failure_looks_up_by_client_id(tmp_path) -> None:
    """A network error on the POST: the order may rest. Found by client id -> continue;
    not found -> halt (cancel-only), never a blind MARKET on top of a maybe-resting order."""
    class FlakyPost(MakerClient):
        def __init__(self, *, lookup_works: bool):
            super().__init__(fill_fraction="1")
            self.lookup_works = lookup_works

        def order(self, **params):
            if params.get("timeInForce") == "GTX":
                super().order(**params)   # the order DID reach the book
                raise BinanceAPIError("network failure calling /fapi/v1/order: read timeout")
            return super().order(**params)

        def get_order_by_client_id(self, symbol, client_order_id):
            if not self.lookup_works:
                raise BinanceAPIError("network failure calling /fapi/v1/order: read timeout")
            for order_id, rest in self.resting.items():
                if rest["params"]["newClientOrderId"] == client_order_id:
                    return self._view(order_id)
            raise BinanceAPIError("not found", payload={"code": -2013})

    client = FlakyPost(lookup_works=True)
    executor, audit, book = _run(tmp_path / "found", client, _policy(order_style="MAKER_THEN_MARKET"))
    assert executor.execute(book, dry_run=False)["status"] == "COMPLETE"
    assert client.market_orders == []

    client = FlakyPost(lookup_works=False)
    executor, audit, book = _run(tmp_path / "lost", client, _policy(order_style="MAKER_THEN_MARKET"))
    with pytest.raises(HaltedError, match="outcome unknown"):
        executor.execute(book, dry_run=False)
    assert _run_status(audit) == "HALTED_MID_BOOK" and client.market_orders == [] and client.open_orders() == []


# ---------------------------------------------------------------------------
# (g) a remainder the exchange would refuse: halt, do not liquidate a nearly-right book
# ---------------------------------------------------------------------------

def test_remainder_below_min_notional_halts_instead_of_flattening(tmp_path) -> None:
    class WideMinNotional(MakerClient):
        def exchange_info(self):
            payload = super().exchange_info()
            for item in payload["symbols"]:
                for entry in item["filters"]:
                    if entry["filterType"] == "MIN_NOTIONAL":
                        entry["notional"] = "20"
            return payload

    client = WideMinNotional(fill_fraction="0.8")   # 0.4 fills, 0.1 (= 10 USDT < 20) is left
    executor, audit, book = _run(tmp_path, client, _policy(order_style="MAKER_THEN_MARKET"))
    with pytest.raises(HaltedError, match="below exchange min notional"):
        executor.execute(book, dry_run=False)
    assert _run_status(audit) == "HALTED_MID_BOOK"
    assert client.market_orders == [] and client.open_orders() == []
    assert client.inventory == {"AAAUSDT": Decimal("0.4")}   # kept, handed to the operator


def test_reduce_only_remainder_is_exempt_from_min_notional(tmp_path) -> None:
    class WideMinNotional(MakerClient):
        def exchange_info(self):
            payload = super().exchange_info()
            for item in payload["symbols"]:
                for entry in item["filters"]:
                    if entry["filterType"] == "MIN_NOTIONAL":
                        entry["notional"] = "20"
            return payload

    # AAAUSDT 1.0 held, target 0.5 long -> SELL 0.5 reduce-only; BBB opens as usual.
    client = WideMinNotional(fill_fraction="0.8", positions={"AAAUSDT": Decimal("1.0"), "BBBUSDT": Decimal("-1.0")})
    book = TargetBook("unit-test", "reduce", SHA, NOW.isoformat(), NOW.isoformat(),
                      {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {"AAAUSDT": 100, "BBBUSDT": 100}, "unit-test")
    executor, audit, _ = _run(tmp_path, client, _policy(order_style="MAKER_THEN_MARKET"), book)
    assert executor.execute(book, dry_run=False)["status"] == "COMPLETE"
    assert [o["reduceOnly"] for o in client.market_orders] == ["true", "true"]
    assert [o["quantity"] for o in client.market_orders] == ["0.1", "0.1"]
    assert client.inventory == {"AAAUSDT": Decimal("0.5"), "BBBUSDT": Decimal("-0.5")}
