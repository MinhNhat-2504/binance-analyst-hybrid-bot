"""Fail-closed, testnet-only portfolio reconciliation for Binance Futures."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Any, Callable

from .binance_futures import BinanceAPIError, FuturesREST
from .targets import TargetBook


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class ExecutionPolicy:
    environment: str = "testnet"
    max_gross_notional_usd: float = 500.0
    max_order_notional_usd: float = 100.0
    max_positions: int = 20
    max_orders: int = 80
    max_notional_to_equity: float = 1.0
    max_target_age_seconds: int = 6 * 60 * 60
    max_target_future_seconds: int = 5 * 60
    # Keep deliberate margin headroom: gross exposure is capped at 1x equity while
    # the exchange leverage is configured to 2x.  The previous 2x/1x combination
    # was mathematically impossible and failed mid-portfolio with -2019.
    leverage: int = 2
    margin_type: str = "CROSSED"
    flatten_max_attempts: int = 3
    flatten_retry_seconds: float = 0.5
    expected_config_sha256: str = ""
    order_style: str = "MARKET"
    limit_buffer_bps: float = 3.0
    poll_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.environment != "testnet":
            raise ValueError("this executor is testnet-only; production is intentionally blocked")
        if self.max_gross_notional_usd <= 0 or self.max_order_notional_usd <= 0:
            raise ValueError("notional limits must be positive")
        if self.max_positions < 1 or self.max_orders < 1:
            raise ValueError("position/order limits must be positive")
        if self.max_target_age_seconds < 1 or self.max_target_future_seconds < 0:
            raise ValueError("target freshness limits are invalid")
        if self.leverage < 1 or self.leverage > 125:
            raise ValueError("leverage must be in [1, 125]")
        if self.max_notional_to_equity <= 0 or self.max_notional_to_equity > self.leverage:
            raise ValueError("max_notional_to_equity must be positive and cannot exceed leverage")
        if self.margin_type not in {"CROSSED", "ISOLATED"}:
            raise ValueError("margin_type must be CROSSED or ISOLATED")
        if self.flatten_max_attempts < 1 or self.flatten_retry_seconds < 0:
            raise ValueError("flatten retry policy is invalid")
        if self.expected_config_sha256 and (
            len(self.expected_config_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.expected_config_sha256.lower())
        ):
            raise ValueError("expected_config_sha256 must be a 64-character hex digest")
        if self.order_style not in {"MARKET", "LIMIT_IOC"}:
            raise ValueError("order_style must be MARKET or LIMIT_IOC")


@dataclass(frozen=True)
class Instrument:
    symbol: str
    step_size: Decimal
    min_qty: Decimal
    tick_size: Decimal
    min_notional: Decimal


@dataclass
class PlanLeg:
    symbol: str
    side: str
    quantity: Decimal
    reduce_only: bool
    reference_price: float
    reason: str
    desired_quantity: Decimal
    current_quantity: Decimal
    target_weight: float


class KillSwitch:
    """A missing or malformed file halts execution.  This is intentionally fail-closed."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def assert_released_for_testnet(self) -> None:
        if not self.path.exists():
            raise RuntimeError(f"kill switch missing: {self.path}; execution remains halted")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("environment") != "testnet" or payload.get("trading_enabled") is not True:
            raise RuntimeError(f"kill switch is engaged: {payload.get('reason', 'no reason')}")

    def engage(self, reason: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"environment": "testnet", "trading_enabled": False, "reason": str(reason), "engaged_utc": utc_now()}, indent=2), encoding="utf-8")

    def release(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("a human-readable release reason is required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"environment": "testnet", "trading_enabled": True, "reason": str(reason), "released_utc": utc_now()}, indent=2), encoding="utf-8")


class ExecutionAudit:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS execution_runs (
            run_id TEXT PRIMARY KEY, started_utc TEXT, finished_utc TEXT, target_id TEXT,
            environment TEXT, dry_run INTEGER, status TEXT, message TEXT)""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS execution_legs (
            run_id TEXT, sequence INTEGER, symbol TEXT, side TEXT, quantity TEXT,
            reduce_only INTEGER, reference_price REAL, reason TEXT, status TEXT,
            order_id TEXT, avg_fill_price REAL, fill_slippage_bps REAL, response_json TEXT,
            client_order_id TEXT, desired_quantity TEXT, current_quantity TEXT, target_weight REAL)""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS position_snapshots (
            run_id TEXT, phase TEXT, captured_utc TEXT, positions_json TEXT)""")
        self._migrate_legs()
        self.connection.commit()

    def _migrate_legs(self) -> None:
        existing = {row[1] for row in self.connection.execute("PRAGMA table_info(execution_legs)")}
        for name, typ in (("client_order_id", "TEXT"), ("desired_quantity", "TEXT"), ("current_quantity", "TEXT"), ("target_weight", "REAL")):
            if name not in existing:
                self.connection.execute(f"ALTER TABLE execution_legs ADD COLUMN {name} {typ}")

    def start(self, run_id: str, book: TargetBook, dry_run: bool) -> None:
        self.connection.execute("INSERT INTO execution_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (run_id, utc_now(), "", book.target_id, "testnet", int(dry_run), "RUNNING", ""))
        self.connection.commit()

    def leg(self, run_id: str, sequence: int, leg: PlanLeg, status: str, response: Any = None, *, client_order_id: str = "") -> None:
        avg_price, order_id = None, ""
        if isinstance(response, dict):
            order_id = str(response.get("orderId", ""))
            try:
                avg_price = float(response.get("avgPrice", 0) or 0) or None
            except (TypeError, ValueError):
                pass
        slip = None
        if avg_price and leg.reference_price > 0:
            slip = (1.0 if leg.side == "BUY" else -1.0) * (avg_price / leg.reference_price - 1.0) * 10_000
        self.connection.execute(
            """INSERT INTO execution_legs (run_id,sequence,symbol,side,quantity,reduce_only,reference_price,reason,status,order_id,avg_fill_price,fill_slippage_bps,response_json,client_order_id,desired_quantity,current_quantity,target_weight)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, sequence, leg.symbol, leg.side, str(leg.quantity), int(leg.reduce_only), leg.reference_price, leg.reason, status, order_id, avg_price, slip, json.dumps(response or {}, default=str), client_order_id, str(leg.desired_quantity), str(leg.current_quantity), leg.target_weight),
        )
        self.connection.commit()

    def finish(self, run_id: str, status: str, message: str) -> None:
        self.connection.execute("UPDATE execution_runs SET finished_utc=?, status=?, message=? WHERE run_id=?", (utc_now(), status, message[:1_000], run_id))
        self.connection.commit()

    def positions(self, run_id: str, phase: str, payload: Any) -> None:
        self.connection.execute("INSERT INTO position_snapshots VALUES (?, ?, ?, ?)", (run_id, phase, utc_now(), json.dumps(payload, default=str)))
        self.connection.commit()

    def target_completed(self, target_id: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM execution_runs WHERE target_id=? AND dry_run=0 AND status='COMPLETE' LIMIT 1",
            (target_id,),
        ).fetchone() is not None


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _round_step(value: Decimal, step: Decimal, *, up: bool = False) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN) * step


def instruments_from_exchange_info(payload: dict[str, Any]) -> dict[str, Instrument]:
    result: dict[str, Instrument] = {}
    for item in payload.get("symbols", []):
        if item.get("status") != "TRADING" or item.get("contractType") != "PERPETUAL":
            continue
        filters = {entry.get("filterType"): entry for entry in item.get("filters", [])}
        lot, price = filters.get("LOT_SIZE", {}), filters.get("PRICE_FILTER", {})
        notional = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
        try:
            symbol = str(item["symbol"]).upper()
            result[symbol] = Instrument(symbol, _decimal(lot["stepSize"]), _decimal(lot["minQty"]), _decimal(price["tickSize"]), _decimal(notional.get("notional", notional.get("minNotional", "0"))))
        except (KeyError, ArithmeticError):
            continue
    return result


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


class TestnetExecutor:
    __test__ = False

    def __init__(self, client: FuturesREST, policy: ExecutionPolicy, kill_switch: KillSwitch, audit: ExecutionAudit, *, now: Callable[[], datetime] | None = None) -> None:
        if client.environment != "testnet" or policy.environment != "testnet":
            raise ValueError("TestnetExecutor refuses any non-testnet client or policy")
        self.client, self.policy, self.kill_switch, self.audit = client, policy, kill_switch, audit
        self._now = now or (lambda: datetime.now(timezone.utc))

    def assert_target_fresh(self, book: TargetBook) -> None:
        age = (self._now() - parse_utc(book.intended_execution_utc)).total_seconds()
        if age > self.policy.max_target_age_seconds:
            raise RuntimeError(f"stale target: intended execution is {age / 3600:.2f}h old (limit {self.policy.max_target_age_seconds / 3600:.2f}h)")
        if age < -self.policy.max_target_future_seconds:
            raise RuntimeError("target is dated too far in the future")

    def assert_target_identity(self, book: TargetBook) -> None:
        expected = self.policy.expected_config_sha256.lower()
        if not expected:
            raise RuntimeError("execution policy lacks the frozen paper config SHA-256")
        if not hmac.compare_digest(book.config_sha256.lower(), expected):
            raise RuntimeError("target config SHA-256 does not match the frozen paper config")

    def _equity(self, account: dict[str, Any]) -> float:
        equity = float(account.get("totalMarginBalance", 0) or 0)
        if equity <= 0:
            raise RuntimeError("insufficient totalMarginBalance for testnet execution")
        return equity

    def _append_split(self, legs: list[PlanLeg], *, instrument: Instrument, mark: float, symbol: str, side: str, quantity: Decimal, reduce_only: bool, reference: float, reason: str, desired: Decimal, current: Decimal, target_weight: float) -> None:
        maximum = _round_step(_decimal(self.policy.max_order_notional_usd / mark), instrument.step_size)
        minimum_notional_qty = _round_step(instrument.min_notional / _decimal(mark), instrument.step_size, up=True)
        minimum = max(instrument.min_qty, minimum_notional_qty)
        if maximum < instrument.min_qty:
            raise RuntimeError(f"{symbol}: max_order_notional_usd is below min quantity")
        if maximum < minimum and not reduce_only:
            raise RuntimeError(f"{symbol}: max_order_notional_usd is below exchange minimum")
        if quantity < minimum and not reduce_only:
            raise RuntimeError(f"{symbol}: order delta is below exchange min-notional")
        chunk_minimum = instrument.min_qty if reduce_only else minimum
        if quantity < chunk_minimum:
            raise RuntimeError(f"{symbol}: order quantity is below exchange min quantity")
        chunks, remaining = [], quantity
        while remaining > maximum:
            chunks.append(maximum)
            remaining -= maximum
        chunks.append(remaining)
        if chunks[-1] < chunk_minimum and len(chunks) > 1:
            needed = chunk_minimum - chunks[-1]
            if chunks[-2] - needed < chunk_minimum:
                raise RuntimeError(f"{symbol}: cannot split order into exchange-valid notionals")
            chunks[-2] -= needed
            chunks[-1] += needed
        for chunk in chunks:
            if chunk < instrument.min_qty or (not reduce_only and chunk * _decimal(mark) < instrument.min_notional):
                raise RuntimeError(f"{symbol}: order chunk is below exchange minimum")
            legs.append(PlanLeg(symbol, side, chunk, reduce_only, reference, reason, desired, current, target_weight))

    def build_plan(self, book: TargetBook) -> tuple[list[PlanLeg], list[str]]:
        self.client.sync_time()
        if self.client.position_mode():
            raise RuntimeError("dualSidePosition is enabled; one-way position mode is required")
        instruments = instruments_from_exchange_info(self.client.exchange_info())
        positions = {str(row.get("symbol", "")).upper(): _decimal(row.get("positionAmt", 0)) for row in self.client.positions()}
        positions = {symbol: qty for symbol, qty in positions.items() if qty != 0}
        gross_budget = min(self.policy.max_gross_notional_usd, self._equity(self.client.account()) * self.policy.max_notional_to_equity)
        active = {symbol: weight for symbol, weight in book.weights.items() if abs(weight) > 0}
        if len(active) > self.policy.max_positions:
            raise RuntimeError(f"target has {len(active)} positions > configured max {self.policy.max_positions}")
        legs: list[PlanLeg] = []
        skips: list[str] = []
        for symbol in sorted(set(active) | set(positions)):
            weight, current = active.get(symbol, 0.0), positions.get(symbol, Decimal("0"))
            instrument = instruments.get(symbol)
            if not instrument:
                skips.append(f"{symbol}:{'uncloseable_position' if current else 'not_trading_or_unknown'}")
                continue
            ticker = self.client.book_ticker(symbol)
            bid, ask = float(ticker["bidPrice"]), float(ticker["askPrice"])
            mark, reference = (bid + ask) / 2.0, float(book.reference_prices.get(symbol) or (bid + ask) / 2.0)
            if not weight:
                self._append_split(legs, instrument=instrument, mark=mark, symbol=symbol, side="SELL" if current > 0 else "BUY", quantity=abs(current), reduce_only=True, reference=reference, reason="close_orphan", desired=Decimal("0"), current=current, target_weight=0.0)
                continue
            desired_abs = _round_step(_decimal(abs(weight) * gross_budget / mark), instrument.step_size)
            if desired_abs < instrument.min_qty or desired_abs * _decimal(mark) < instrument.min_notional:
                if current:
                    self._append_split(legs, instrument=instrument, mark=mark, symbol=symbol, side="SELL" if current > 0 else "BUY", quantity=abs(current), reduce_only=True, reference=reference, reason="close_below_min_target", desired=Decimal("0"), current=current, target_weight=0.0)
                skips.append(f"{symbol}:below_min_notional")
                continue
            desired = desired_abs if weight > 0 else -desired_abs
            if current and current * desired < 0:
                self._append_split(legs, instrument=instrument, mark=mark, symbol=symbol, side="SELL" if current > 0 else "BUY", quantity=abs(current), reduce_only=True, reference=reference, reason="close_before_flip", desired=desired, current=current, target_weight=weight)
                self._append_split(legs, instrument=instrument, mark=mark, symbol=symbol, side="BUY" if desired > 0 else "SELL", quantity=abs(desired), reduce_only=False, reference=reference, reason="open_after_flip", desired=desired, current=Decimal("0"), target_weight=weight)
            else:
                delta = desired - current
                if abs(delta) >= instrument.min_qty and abs(delta) * _decimal(mark) >= instrument.min_notional:
                    self._append_split(legs, instrument=instrument, mark=mark, symbol=symbol, side="BUY" if delta > 0 else "SELL", quantity=abs(delta), reduce_only=current != 0 and current * delta < 0, reference=reference, reason="rebalance", desired=desired, current=current, target_weight=weight)
                elif delta != 0:
                    skips.append(f"{symbol}:delta_below_exchange_minimum_safe_noop")
        if any("uncloseable_position" in message for message in skips):
            raise RuntimeError("cannot safely close a current position: " + ", ".join(skips))
        if len(legs) > self.policy.max_orders:
            raise RuntimeError(f"reconciliation plan has {len(legs)} orders > configured max {self.policy.max_orders}")
        # Across the whole portfolio every reduce-only/closing leg must complete before
        # any opening leg.  Symbol sort alone can interleave opens ahead of later closes.
        legs.sort(key=lambda leg: (0 if leg.reduce_only else 1, leg.symbol, leg.reason))
        return legs, skips

    @staticmethod
    def _client_order_id(run_id: str, book: TargetBook, leg: PlanLeg, sequence: int, prefix: str = "carry") -> str:
        return f"{prefix}-{hashlib.sha256(f'{run_id}|{book.target_id}|{leg.symbol}|{leg.reason}|{sequence}'.encode()).hexdigest()[:28]}"

    def _order_params(self, leg: PlanLeg, client_order_id: str, *, force_market: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {"symbol": leg.symbol, "side": leg.side, "quantity": format(leg.quantity, "f"), "newOrderRespType": "RESULT", "newClientOrderId": client_order_id, "reduceOnly": "true" if leg.reduce_only else "false"}
        if force_market or self.policy.order_style == "MARKET":
            params["type"] = "MARKET"
            return params
        ticker = self.client.book_ticker(leg.symbol)
        raw = _decimal(ticker["askPrice"] if leg.side == "BUY" else ticker["bidPrice"])
        factor = _decimal(1 + self.policy.limit_buffer_bps / 10_000 if leg.side == "BUY" else 1 - self.policy.limit_buffer_bps / 10_000)
        instrument = instruments_from_exchange_info(self.client.exchange_info())[leg.symbol]
        params.update({"type": "LIMIT", "timeInForce": "IOC", "price": format(_round_step(raw * factor, instrument.tick_size, up=leg.side == "BUY"), "f")})
        return params

    def _submit_leg(self, run_id: str, book: TargetBook, leg: PlanLeg, sequence: int, *, force_market: bool = False) -> tuple[dict[str, Any], str]:
        client_order_id = self._client_order_id(run_id, book, leg, sequence)
        try:
            response = self.client.order(**self._order_params(leg, client_order_id, force_market=force_market))
        except BinanceAPIError as original:
            try:
                response = self.client.get_order_by_client_id(leg.symbol, client_order_id)
            except Exception:
                raise original
        if response.get("orderId"):
            time.sleep(self.policy.poll_seconds)
            response = self.client.get_order(leg.symbol, int(response["orderId"]))
        return response, client_order_id

    def _configure_symbol(self, symbol: str) -> None:
        try:
            self.client.set_margin_type(symbol, self.policy.margin_type)
        except BinanceAPIError as exc:
            code = exc.payload.get("code") if isinstance(exc.payload, dict) else None
            if int(code or 0) != -4046:  # "No need to change margin type."
                raise
        self.client.set_leverage(symbol, self.policy.leverage)

    def _flatten_all(self, run_id: str, reason: str, target_symbols: set[str]) -> bool:
        """MARKET-only, retrying emergency flatten that verifies the final inventory.

        No BaseException is allowed to escape this cleanup routine: a second Ctrl+C is
        recorded but cannot turn a partially flattened account into silent success.
        """
        cancelled: set[str] = set()
        for symbol in sorted(target_symbols):
            try:
                self.client.cancel_all(symbol)
                cancelled.add(symbol)
            except BaseException as exc:
                self.audit.positions(run_id, f"cancel_all_failed:{symbol}", {"error": str(exc)})
        emergency_book = TargetBook("EMERGENCY", f"flatten-{run_id[:16]}", "0" * 64, utc_now(), utc_now(), {}, {}, "emergency")
        for attempt in range(1, self.policy.flatten_max_attempts + 1):
            try:
                positions = self.client.positions()
                instruments = instruments_from_exchange_info(self.client.exchange_info())
            except BaseException as exc:
                self.audit.positions(run_id, f"flatten_inventory_failed:{attempt}", {"error": str(exc), "reason": reason})
                try:
                    time.sleep(self.policy.flatten_retry_seconds)
                except BaseException as sleep_exc:
                    self.audit.positions(run_id, f"flatten_retry_interrupted:{attempt}", {"error": str(sleep_exc)})
                continue
            nonzero = [row for row in positions if _decimal(row.get("positionAmt", 0)) != 0]
            self.audit.positions(run_id, f"emergency_flatten_attempt:{attempt}", positions)
            if not nonzero:
                self.audit.positions(run_id, "emergency_flatten_verified", [])
                return True
            for row_index, row in enumerate(nonzero):
                symbol, current = str(row.get("symbol", "")).upper(), _decimal(row.get("positionAmt", 0))
                sequence = 10_000 + attempt * 1_000 + row_index
                if symbol not in cancelled:
                    try:
                        self.client.cancel_all(symbol)
                        cancelled.add(symbol)
                    except BaseException as exc:
                        self.audit.positions(run_id, f"cancel_all_failed:{symbol}", {"error": str(exc)})
                instrument = instruments.get(symbol)
                if not instrument:
                    self.audit.leg(run_id, sequence, PlanLeg(symbol, "", Decimal(0), True, 0, "flatten_unknown_symbol", Decimal(0), current, 0.0), "ERROR", {"reason": reason})
                    continue
                try:
                    # A MARKET emergency close does not need a live quote.  Depending
                    # on book_ticker here would make a market-data outage disable the
                    # one path that must remain available during cleanup.
                    reference = float(row.get("markPrice", row.get("entryPrice", 0)) or 0)
                    leg = PlanLeg(symbol, "SELL" if current > 0 else "BUY", abs(current), True, reference, "emergency_flatten", Decimal(0), current, 0.0)
                    response, client_id = self._submit_leg(run_id, emergency_book, leg, sequence, force_market=True)
                    self.audit.leg(run_id, sequence, leg, str(response.get("status", "UNKNOWN")), response, client_order_id=client_id)
                except BaseException as exc:
                    self.audit.leg(run_id, sequence, PlanLeg(symbol, "", Decimal(0), True, 0, "emergency_flatten_error", Decimal(0), current, 0.0), "ERROR", {"reason": reason, "error": str(exc)})
            try:
                time.sleep(self.policy.flatten_retry_seconds)
            except BaseException as exc:
                self.audit.positions(run_id, f"flatten_retry_interrupted:{attempt}", {"error": str(exc)})
        try:
            remaining = [row for row in self.client.positions() if _decimal(row.get("positionAmt", 0)) != 0]
            self.audit.positions(run_id, "emergency_flatten_unresolved", remaining)
        except BaseException as exc:
            self.audit.positions(run_id, "emergency_flatten_verify_failed", {"error": str(exc)})
        return False

    def execute(self, book: TargetBook, *, dry_run: bool) -> dict[str, Any]:
        run_id, armed, orders_started = uuid.uuid4().hex, False, False
        self.audit.start(run_id, book, dry_run)
        try:
            if not dry_run:
                self.assert_target_fresh(book)
                self.assert_target_identity(book)
                if self.audit.target_completed(book.target_id):
                    raise RuntimeError(f"target_id already completed: {book.target_id}")
                self.kill_switch.assert_released_for_testnet()
                armed = True
            legs, skips = self.build_plan(book)
            for message in skips:
                self.audit.leg(run_id, -1, PlanLeg("", "", Decimal(0), False, 0, message, Decimal(0), Decimal(0), 0.0), "SKIPPED")
            if dry_run:
                for index, leg in enumerate(legs):
                    self.audit.leg(run_id, index, leg, "DRY_RUN", client_order_id=self._client_order_id(run_id, book, leg, index))
                self.audit.finish(run_id, "DRY_RUN", f"{len(legs)} legs, {len(skips)} skips")
                return {"run_id": run_id, "status": "DRY_RUN", "legs": [asdict(leg) for leg in legs], "skips": skips}
            critical_skips = [message for message in skips if not message.endswith("safe_noop")]
            if critical_skips:
                raise RuntimeError("incomplete target; refuse partial portfolio: " + ", ".join(critical_skips))
            self.audit.positions(run_id, "before_orders", self.client.positions())
            for symbol, weight in book.weights.items():
                if abs(weight) > 0:
                    self._configure_symbol(symbol)
            for index, leg in enumerate(legs):
                # From this boundary onward an ambiguous POST or interruption may have
                # mutated positions, so failures must cancel per-symbol and flatten.
                self.kill_switch.assert_released_for_testnet()
                orders_started = True
                response, client_order_id = self._submit_leg(run_id, book, leg, index)
                status = str(response.get("status", "UNKNOWN"))
                self.audit.leg(run_id, index, leg, status, response, client_order_id=client_order_id)
                if status != "FILLED":
                    raise RuntimeError(f"{leg.symbol} {leg.reason} did not fill: {status}")
            self.audit.positions(run_id, "after_orders", self.client.positions())
            self.kill_switch.engage(f"run {run_id} complete; manual release required for next run")
            self.audit.finish(run_id, "COMPLETE", f"{len(legs)} legs, {len(skips)} skips")
            return {"run_id": run_id, "status": "COMPLETE", "legs": len(legs), "skips": skips}
        except BaseException as exc:
            if armed:
                self.kill_switch.engage(f"automatic emergency stop: {type(exc).__name__}: {exc}")
            if orders_started:
                try:
                    self._flatten_all(run_id, f"{type(exc).__name__}: {exc}", set(book.weights))
                except BaseException as cleanup_exc:
                    try:
                        self.audit.positions(run_id, "emergency_cleanup_crashed", {"error": str(cleanup_exc)})
                    except BaseException:
                        pass
            self.audit.finish(run_id, "INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "FAILED", str(exc))
            raise
