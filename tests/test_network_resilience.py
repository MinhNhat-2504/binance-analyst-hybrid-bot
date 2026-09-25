"""The 2026-09-05 incident, replayed against the engine.

What happened: the POST for one leg was acknowledged, the GET that polled its status died
with an SSL EOF, the engine treated that generic error as "orders started + unknown" and
went to emergency flatten; cancel-all then failed on the same outage; the flatten's third
and last attempt closed all eight positions, but verification only lived at the top of the
NEXT attempt, so a fully flat account was recorded UNRESOLVED_EXPOSURE and blocked the loop
for twenty days.

Two rules come out of it and are pinned here:
  1. An order that exists but cannot be READ is unknown, not wrong. Unknown is the
     cancel-only hand-off (HALTED_MID_BOOK, book kept), never a flatten.
  2. A flatten whose last attempt left the account flat IS verified.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from execution.binance_futures import BinanceAPIError
from execution.engine import ExecutionAudit, ExecutionPolicy, HaltedError, KillSwitch, TestnetExecutor
from execution.targets import TargetBook
from test_execution import TrackingClient, _release


def _book(target_id: str) -> tuple[TargetBook, datetime]:
    now = datetime.now(timezone.utc)
    book = TargetBook("unit-test", target_id, "a" * 64, now.isoformat(), now.isoformat(),
                      {"AAAUSDT": 0.5, "BBBUSDT": -0.5}, {"AAAUSDT": 100.0, "BBBUSDT": 100.0}, "unit-test")
    return book, now


def test_status_read_failure_after_a_placed_order_is_cancel_only_not_flatten(tmp_path):
    class PollDiesOnce(TrackingClient):
        def __init__(self):
            super().__init__()
            self.reads = 0
            self.cancelled = []

        def get_order(self, symbol, order_id):
            self.reads += 1
            if self.reads == 1:
                raise BinanceAPIError("network failure calling /fapi/v1/order: SSLEOFError(8)")
            return super().get_order(symbol, order_id)

        def cancel_all(self, symbol):
            self.cancelled.append(symbol)
            return {}

    book, now = _book("poll-dies")
    client, kill = PollDiesOnce(), KillSwitch(tmp_path / "kill.json")
    _release(kill, book)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    policy = ExecutionPolicy(poll_seconds=0, flatten_retry_seconds=0, expected_config_sha256="a" * 64)
    with pytest.raises(HaltedError, match="could not be read"):
        TestnetExecutor(client, policy, kill, audit, now=lambda: now).execute(book, dry_run=False)

    status = audit.connection.execute("SELECT status FROM execution_runs WHERE target_id='poll-dies'").fetchone()[0]
    assert status == "HALTED_MID_BOOK"
    # The first leg filled and STAYS: nothing reduce-only was sent, the book is handed off intact.
    assert client.inventory == {"AAAUSDT": Decimal("0.5")}
    assert not any(o.get("reduceOnly") == "true" for o in client.orders)
    assert client.cancelled, "cancel-only must still sweep resting orders"
    assert audit.connection.execute("SELECT 1 FROM position_snapshots WHERE phase LIKE 'emergency_flatten%'").fetchone() is None


def test_flatten_that_succeeds_on_its_last_attempt_is_verified_not_unresolved(tmp_path):
    class MidPortfolioError(TrackingClient):
        def __init__(self):
            super().__init__()
            self.opens = 0

        def order(self, **params):
            if params["reduceOnly"] == "false":
                self.opens += 1
                if self.opens == 2:
                    raise BinanceAPIError("insufficient margin", status_code=400, payload={"code": -2019})
            return super().order(**params)

        def get_order_by_client_id(self, symbol, client_order_id):
            raise BinanceAPIError("unknown order", status_code=400, payload={"code": -2013})

    book, now = _book("last-attempt")
    client, kill = MidPortfolioError(), KillSwitch(tmp_path / "kill.json")
    _release(kill, book)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    # ONE attempt: the closes go out on it, and there is no second pass to observe them.
    policy = ExecutionPolicy(poll_seconds=0, flatten_retry_seconds=0, flatten_max_attempts=1,
                             expected_config_sha256="a" * 64)
    with pytest.raises(BinanceAPIError, match="insufficient margin"):
        TestnetExecutor(client, policy, kill, audit, now=lambda: now).execute(book, dry_run=False)

    assert client.inventory == {}, "the single attempt did flatten the account"
    status = audit.connection.execute("SELECT status FROM execution_runs WHERE target_id='last-attempt'").fetchone()[0]
    assert status == "FAILED", "flat account + no open orders is a verified flatten, whatever attempt it happened on"
    phases = {r[0] for r in audit.connection.execute("SELECT phase FROM position_snapshots")}
    assert "emergency_flatten_verified" in phases
    assert "emergency_flatten_unresolved" not in phases


def test_flatten_that_truly_fails_is_still_unresolved(tmp_path):
    """The relaxation must not leak: a venue that refuses to close keeps UNRESOLVED_EXPOSURE."""
    class RefusesClose(TrackingClient):
        def __init__(self):
            super().__init__()
            self.opens = 0

        def order(self, **params):
            if params["reduceOnly"] == "true":
                return {"orderId": 99, "status": "FILLED", "avgPrice": "100"}   # lies, moves nothing
            self.opens += 1
            if self.opens == 2:
                raise BinanceAPIError("insufficient margin", status_code=400, payload={"code": -2019})
            return super().order(**params)

        def get_order_by_client_id(self, symbol, client_order_id):
            raise BinanceAPIError("unknown order", status_code=400, payload={"code": -2013})

    book, now = _book("refuses")
    client, kill = RefusesClose(), KillSwitch(tmp_path / "kill.json")
    _release(kill, book)
    audit = ExecutionAudit(tmp_path / "audit.sqlite3")
    policy = ExecutionPolicy(poll_seconds=0, flatten_retry_seconds=0, flatten_max_attempts=1,
                             expected_config_sha256="a" * 64)
    with pytest.raises(BinanceAPIError):
        TestnetExecutor(client, policy, kill, audit, now=lambda: now).execute(book, dry_run=False)
    assert client.inventory, "position is still there"
    status = audit.connection.execute("SELECT status FROM execution_runs WHERE target_id='refuses'").fetchone()[0]
    assert status == "UNRESOLVED_EXPOSURE"
