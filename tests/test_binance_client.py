"""FuturesREST._request: what is retried, what is not, and the clock resync.

2026-09-05, testnet: one SSL EOF while polling an order's status cascaded into cancel
failures, a -1021 clock error and a whole-book flatten - every one of those calls a GET or
DELETE that would have succeeded a second later. These tests pin the policy that came out
of it: idempotent calls retry with backoff, POST /order never does (a duplicate would open
a second position), and a -1021 resyncs the server clock and resends once.
"""
from __future__ import annotations

import pytest
import requests

import execution.binance_futures as bf
from execution.binance_futures import BinanceAPIError, FuturesCredentials, FuturesREST


class _Response:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Session:
    """Scripted wire: each entry is either an Exception to raise or a _Response."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def request(self, method, url, params=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(bf.time, "sleep", lambda *_: None)
    c = FuturesREST("testnet", FuturesCredentials("key", "secret"))
    return c


def test_get_retries_transient_network_errors_then_succeeds(client):
    client.session = _Session([
        requests.exceptions.SSLError("EOF occurred in violation of protocol"),
        requests.exceptions.ConnectionError("reset"),
        _Response(200, {"ok": 1}),
    ])
    assert client.public("GET", "/fapi/v1/time") == {"ok": 1}
    assert len(client.session.calls) == 3


def test_get_gives_up_after_the_retry_budget(client):
    client.session = _Session([requests.exceptions.SSLError("EOF")] * bf.IDEMPOTENT_RETRIES)
    with pytest.raises(BinanceAPIError, match="network failure"):
        client.public("GET", "/fapi/v1/time")
    assert len(client.session.calls) == bf.IDEMPOTENT_RETRIES


def test_post_order_is_never_retried_on_a_network_error(client):
    """A POST that died on the wire may or may not have reached the matching engine.
    Re-sending it could open a second position; the engine resolves this by looking the
    order up by client id, so the client must raise on the first failure."""
    client.session = _Session([requests.exceptions.SSLError("EOF"), _Response(200, {"orderId": 1})])
    with pytest.raises(BinanceAPIError, match="network failure"):
        client.signed("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"})
    assert len(client.session.calls) == 1


def test_delete_cancel_all_is_retried(client):
    client.session = _Session([requests.exceptions.ReadTimeout("t"), _Response(200, {"code": 200, "msg": "ok"})])
    assert client.signed("DELETE", "/fapi/v1/allOpenOrders", {"symbol": "BTCUSDT"})["msg"] == "ok"
    assert len(client.session.calls) == 2


def test_timestamp_out_of_window_resyncs_clock_and_resends_once(client):
    synced = []
    client.session = _Session([
        _Response(400, {"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}),
        _Response(200, [{"symbol": "BTCUSDT", "positionAmt": "0"}]),
    ])

    def fake_sync():
        synced.append(True)
        client.time_offset_ms = 12_345
        return client.time_offset_ms

    client.sync_time = fake_sync
    out = client.signed("GET", "/fapi/v2/positionRisk")
    assert out[0]["symbol"] == "BTCUSDT"
    assert synced == [True]
    first, second = client.session.calls
    # The resend carries a FRESH timestamp built with the new offset, and a fresh signature.
    assert second["params"]["timestamp"] >= first["params"]["timestamp"] + 12_000
    assert second["params"]["signature"] != first["params"]["signature"]


def test_timestamp_error_resends_even_a_post_because_the_refused_request_never_ran(client):
    client.sync_time = lambda: 0
    client.session = _Session([
        _Response(400, {"code": -1021, "msg": "outside recvWindow"}),
        _Response(200, {"orderId": 9, "status": "NEW"}),
    ])
    assert client.signed("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"})["orderId"] == 9
    assert len(client.session.calls) == 2


def test_second_timestamp_error_is_not_retried_forever(client):
    client.sync_time = lambda: 0
    client.session = _Session([_Response(400, {"code": -1021, "msg": "x"})] * 5)
    with pytest.raises(BinanceAPIError, match="-1021"):
        client.signed("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"})
    assert len(client.session.calls) == 2


def test_5xx_on_get_is_transient_but_4xx_is_final(client):
    client.session = _Session([_Response(503, {"raw": "bad gateway"}), _Response(200, {"serverTime": 1})])
    assert client.public("GET", "/fapi/v1/time")["serverTime"] == 1
    client.session = _Session([_Response(400, {"code": -1102, "msg": "bad param"}), _Response(200, {})])
    with pytest.raises(BinanceAPIError, match="-1102"):
        client.public("GET", "/fapi/v1/klines")
    assert len(client.session.calls) == 1


def test_every_signed_attempt_carries_timestamp_signature_and_key(client):
    client.session = _Session([requests.exceptions.SSLError("EOF"), _Response(200, {})])
    client.signed("GET", "/fapi/v2/account")
    for call in client.session.calls:
        assert "timestamp" in call["params"] and "signature" in call["params"]
        assert call["params"]["recvWindow"] == 5_000
        assert call["headers"]["X-MBX-APIKEY"] == "key"
