"""Small signed REST client for Binance USD-M Futures.

The client intentionally uses separate credential names for testnet and production.  The
testnet runner never accepts the legacy ``BINANCE_API_*`` variables, preventing an existing
production key in a developer's shell from being used accidentally.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests


TESTNET_BASE_URL = "https://testnet.binancefuture.com"
LIVE_BASE_URL = "https://fapi.binance.com"


class BinanceAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


@dataclass(frozen=True)
class FuturesCredentials:
    api_key: str
    api_secret: str


class FuturesREST:
    """HMAC client with server-time offset and explicit environment isolation."""

    def __init__(
        self,
        environment: str,
        credentials: FuturesCredentials | None = None,
        *,
        timeout_seconds: float = 15.0,
    ) -> None:
        environment = str(environment).lower()
        if environment not in {"testnet", "live"}:
            raise ValueError("environment must be 'testnet' or 'live'")
        self.environment = environment
        self.base_url = TESTNET_BASE_URL if environment == "testnet" else LIVE_BASE_URL
        self.credentials = credentials
        self.timeout_seconds = float(timeout_seconds)
        self.session = requests.Session()
        self.time_offset_ms = 0

    @classmethod
    def from_env(cls, environment: str, *, required: bool = True) -> "FuturesREST":
        env = str(environment).upper()
        key = os.getenv(f"BINANCE_{env}_API_KEY", "").strip()
        secret = os.getenv(f"BINANCE_{env}_API_SECRET", "").strip()
        if required and (not key or not secret):
            raise BinanceAPIError(
                f"missing BINANCE_{env}_API_KEY / BINANCE_{env}_API_SECRET; "
                "do not reuse the legacy BINANCE_API_* variables"
            )
        credentials = FuturesCredentials(key, secret) if key and secret else None
        return cls(environment, credentials)

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    def sync_time(self) -> int:
        payload = self.public("GET", "/fapi/v1/time")
        server_ms = int(payload["serverTime"])
        self.time_offset_ms = server_ms - int(time.time() * 1000)
        return self.time_offset_ms

    def public(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request(method, path, params or {}, signed=False)

    def signed(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.credentials:
            raise BinanceAPIError("signed request requires credentials")
        payload = dict(params or {})
        payload.setdefault("recvWindow", 5_000)
        payload["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
        return self._request(method, path, payload, signed=True)

    def _request(self, method: str, path: str, params: dict[str, Any], *, signed: bool) -> Any:
        query = dict(params)
        headers: dict[str, str] = {}
        if signed:
            assert self.credentials is not None
            encoded = urlencode(query, doseq=True)
            query["signature"] = hmac.new(
                self.credentials.api_secret.encode("utf-8"), encoded.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            headers["X-MBX-APIKEY"] = self.credentials.api_key
        try:
            response = self.session.request(
                method.upper(), self._url(path), params=query, headers=headers, timeout=self.timeout_seconds
            )
        except requests.RequestException as exc:
            raise BinanceAPIError(f"network failure calling {path}: {exc}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text[:500]}
        if response.status_code >= 400 or (isinstance(payload, dict) and "code" in payload and int(payload["code"]) < 0):
            raise BinanceAPIError(
                f"Binance {path} failed: {payload}", status_code=response.status_code, payload=payload
            )
        return payload

    # Public endpoints
    def exchange_info(self) -> dict[str, Any]:
        return self.public("GET", "/fapi/v1/exchangeInfo")

    def book_ticker(self, symbol: str) -> dict[str, Any]:
        return self.public("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol.upper()})

    # Signed endpoints
    def account(self) -> dict[str, Any]:
        return self.signed("GET", "/fapi/v2/account")

    def positions(self) -> list[dict[str, Any]]:
        return self.signed("GET", "/fapi/v2/positionRisk")

    def position_mode(self) -> bool:
        """Return True only when Binance account is in hedge/dual-side mode."""
        payload = self.signed("GET", "/fapi/v1/positionSide/dual")
        return bool(payload.get("dualSidePosition", False))

    def set_position_mode(self, *, dual_side: bool) -> dict[str, Any]:
        return self.signed("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "true" if dual_side else "false"})

    def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        return self.signed("POST", "/fapi/v1/leverage", {"symbol": symbol.upper(), "leverage": int(leverage)})

    def set_margin_type(self, symbol: str, margin_type: str) -> dict[str, Any]:
        margin_type = str(margin_type).upper()
        if margin_type not in {"CROSSED", "ISOLATED"}:
            raise ValueError("margin_type must be CROSSED or ISOLATED")
        return self.signed("POST", "/fapi/v1/marginType", {"symbol": symbol.upper(), "marginType": margin_type})

    def order(self, **params: Any) -> dict[str, Any]:
        return self.signed("POST", "/fapi/v1/order", params)

    def get_order(self, symbol: str, order_id: int) -> dict[str, Any]:
        return self.signed("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})

    def get_order_by_client_id(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        return self.signed("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_order_id})

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol.upper()} if symbol else {}
        payload = self.signed("GET", "/fapi/v1/openOrders", params)
        if not isinstance(payload, list):
            raise BinanceAPIError("openOrders returned a non-list payload", payload=payload)
        return payload

    def cancel_order(self, symbol: str, order_id: int) -> dict[str, Any]:
        return self.signed("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})

    def cancel_all(self, symbol: str) -> Any:
        if not symbol:
            raise ValueError("USD-M cancel-all requires a symbol")
        return self.signed("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol.upper()})
