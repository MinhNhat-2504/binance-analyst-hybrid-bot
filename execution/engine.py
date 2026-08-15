"""Fail-closed, testnet-only portfolio reconciliation for Binance Futures."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import sqlite3
import statistics
import time
import uuid
from dataclasses import field, asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Any, Callable

from .binance_futures import PAPER_BASE_URLS, BinanceAPIError, FuturesREST
from .contracts import CEILINGS_SHA256, FROZEN_TESTNET_GROSS_CEILING_USD, quantity_tolerance
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
    order_poll_attempts: int = 5
    kill_switch_release_ttl_seconds: int = 15 * 60
    max_fill_slippage_bps: float = 50.0
    # Reference-drift gate. The paper reference is the signal-day close, legitimately hours
    # old by execution time. Measured on the live 17-symbol book over 200 days (round-7
    # review): median worst-symbol drift at +8h is ~440bps and even at +15min ~80bps, so a
    # 50bps per-symbol trip made P(run proceeds) = 0.000. This is a "the file is from a
    # different world" tripwire, not a slippage control - slippage has its own gate. Two
    # thresholds: the portfolio MEDIAN must stay tight (a broad regime move), and any single
    # symbol may drift further before it alone vetoes the whole rebalance.
    max_reference_drift_bps: float = 300.0
    max_median_reference_drift_bps: float = 150.0
    # Per-symbol quantity tolerance budget, hashed into the contract. Half a lot step, and
    # 1% of the exchange minimum notional expressed in quantity. Owned here so they are
    # reviewable at policy level rather than buried as literals in build_plan.
    tolerance_rounding_steps: float = 0.5
    tolerance_min_notional_fraction: float = 0.01
    expected_config_sha256: str = ""
    order_style: str = "MARKET"
    limit_buffer_bps: float = 3.0
    poll_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.environment != "testnet":
            raise ValueError("this executor is testnet-only; production is intentionally blocked")
        if self.max_gross_notional_usd <= 0 or self.max_order_notional_usd <= 0:
            raise ValueError("notional limits must be positive")
        if self.max_gross_notional_usd > FROZEN_TESTNET_GROSS_CEILING_USD:
            raise ValueError(
                f"max_gross_notional_usd exceeds frozen testnet ceiling "
                f"${FROZEN_TESTNET_GROSS_CEILING_USD:.2f}"
            )
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
        if self.order_poll_attempts < 1 or self.poll_seconds < 0:
            raise ValueError("order poll policy is invalid")
        if self.kill_switch_release_ttl_seconds < 1:
            raise ValueError("kill switch release TTL must be positive")
        if self.max_fill_slippage_bps < 0:
            raise ValueError("max fill slippage cannot be negative")
        if self.max_median_reference_drift_bps < 0 or self.max_median_reference_drift_bps > self.max_reference_drift_bps:
            raise ValueError("max_median_reference_drift_bps must be in [0, max_reference_drift_bps]")
        if self.tolerance_rounding_steps < 0 or not (0 <= self.tolerance_min_notional_fraction <= 1):
            raise ValueError("tolerance budget constants out of range")
        if self.max_reference_drift_bps < 0:
            raise ValueError("max reference drift cannot be negative")
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
    execution_reference_price: float = 0.0


@dataclass
class ExecutionPlan:
    legs: list[PlanLeg]
    skips: list[str]
    expected_positions: dict[str, Decimal]
    tolerance_budgets: dict[str, dict[str, str]]
    gross_budget: float
    orphan_symbols: list[str]
    # The exact positionRisk rows this plan was computed from. execute() audits THIS as
    # before_orders instead of taking a second, independent read that could differ.
    position_snapshot: list[dict[str, Any]] = field(default_factory=list)
    reference_drift_bps: dict[str, float] = field(default_factory=dict)


class VerificationUnavailableError(RuntimeError):
    """The exchange could not provide a trustworthy verification snapshot."""


class TargetMismatchError(RuntimeError):
    """Orders returned, but the independently observed account missed the target."""


class AuditWriteError(RuntimeError):
    """The audit store rejected a write after orders were live.

    A sqlite hiccup is an operational fault: the book on the exchange is exactly as
    correct (or not) as it was a millisecond earlier. Liquidating it because we could not
    RECORD it is the wrong trade. Handled as a halt; and since the audit is the broken
    part, the handler falls back to a plain-text sidecar so the state is never lost.
    """


class HaltedError(RuntimeError):
    """Stop issuing orders; cancel resting orders; leave positions; hand to a human.

    Reserved for anomalies that say nothing about whether the positions on the exchange
    are WRONG: a kill-switch release that timed out mid-book, market data that went away
    before a leg was submitted, a stray order appearing on an unrelated symbol. Every one of
    these used to inherit the nearest handler's action - full market flatten - which is the
    most expensive response available and the wrong one for a book that is, as far as
    anyone can tell, correct. Flatten is now reserved for a confirmed position error.
    """


class ExternalPositionDriftError(RuntimeError):
    """A no-order/safe-noop symbol changed outside this execution run."""


class KillSwitch:
    """A missing or malformed file halts execution.  This is intentionally fail-closed."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def assert_released_for_testnet(
        self, *, max_age_seconds: int | None = None, now: datetime | None = None,
        expected_target_id: str | None = None, expected_budget_usd: float | None = None,
    ) -> dict[str, Any]:
        if not self.path.exists():
            raise RuntimeError(f"kill switch missing: {self.path}; execution remains halted")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("environment") != "testnet" or payload.get("trading_enabled") is not True:
            raise RuntimeError(f"kill switch is engaged: {payload.get('reason', 'no reason')}")
        if expected_target_id is not None and payload.get("authorized_target_id") != expected_target_id:
            raise RuntimeError("kill switch release is bound to a different target_id")
        if expected_budget_usd is not None:
            approved = payload.get("authorized_budget_usd")
            if approved is None or not math.isclose(float(approved), float(expected_budget_usd), rel_tol=0, abs_tol=1e-9):
                raise RuntimeError("kill switch release budget does not match execution budget")
        if max_age_seconds is not None:
            released = payload.get("released_utc")
            if not released:
                raise RuntimeError("kill switch release has no timestamp")
            age = ((now or datetime.now(timezone.utc)) - parse_utc(str(released))).total_seconds()
            if age < 0 or age > max_age_seconds:
                raise RuntimeError(f"kill switch release expired: age {age:.1f}s > TTL {max_age_seconds}s")
        return payload

    def engage(self, reason: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write({"environment": "testnet", "trading_enabled": False, "reason": str(reason), "engaged_utc": utc_now()})

    def release(self, reason: str, *, target_id: str, authorized_budget_usd: float) -> None:
        if not reason.strip():
            raise ValueError("a human-readable release reason is required")
        if not target_id.strip() or authorized_budget_usd <= 0:
            raise ValueError("release requires a target_id and positive authorized budget")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write({
            "environment": "testnet", "trading_enabled": True, "reason": str(reason),
            "authorized_target_id": target_id, "authorized_budget_usd": float(authorized_budget_usd),
            "released_utc": utc_now(),
        })

    def _atomic_write(self, payload: dict[str, Any]) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


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
            client_order_id TEXT, desired_quantity TEXT, current_quantity TEXT, target_weight REAL,
            execution_reference_price REAL)""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS position_snapshots (
            run_id TEXT, phase TEXT, captured_utc TEXT, positions_json TEXT)""")
        self._migrate_legs()
        self.connection.commit()

    def _migrate_legs(self) -> None:
        existing = {row[1] for row in self.connection.execute("PRAGMA table_info(execution_legs)")}
        for name, typ in (
            ("client_order_id", "TEXT"), ("desired_quantity", "TEXT"),
            ("current_quantity", "TEXT"), ("target_weight", "REAL"),
            ("execution_reference_price", "REAL"),
        ):
            if name not in existing:
                self.connection.execute(f"ALTER TABLE execution_legs ADD COLUMN {name} {typ}")

    def sidecar_path(self) -> Path:
        """Plain-text fallback next to the DB for terminal states the DB could not take."""
        return Path(str(self.path) + ".sidecar.jsonl")

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
        if avg_price and leg.execution_reference_price > 0:
            slip = (1.0 if leg.side == "BUY" else -1.0) * (avg_price / leg.execution_reference_price - 1.0) * 10_000
        self.connection.execute(
            """INSERT INTO execution_legs (run_id,sequence,symbol,side,quantity,reduce_only,reference_price,reason,status,order_id,avg_fill_price,fill_slippage_bps,response_json,client_order_id,desired_quantity,current_quantity,target_weight,execution_reference_price)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, sequence, leg.symbol, leg.side, str(leg.quantity), int(leg.reduce_only), leg.reference_price, leg.reason, status, order_id, avg_price, slip, json.dumps(response or {}, default=str), client_order_id, str(leg.desired_quantity), str(leg.current_quantity), leg.target_weight, leg.execution_reference_price),
        )
        self.connection.commit()

    def finish(self, run_id: str, status: str, message: str) -> None:
        self.connection.execute("UPDATE execution_runs SET finished_utc=?, status=?, message=? WHERE run_id=?", (utc_now(), status, message[:1_000], run_id))
        self.connection.commit()

    def positions(self, run_id: str, phase: str, payload: Any) -> None:
        self.connection.execute("INSERT INTO position_snapshots VALUES (?, ?, ?, ?)", (run_id, phase, utc_now(), json.dumps(payload, default=str)))
        self.connection.commit()

    def target_in_flight(self, target_id: str, *, except_run_id: str = "") -> bool:
        """A live run of this target is still RUNNING (or died without a terminal row).
        A second executor must not stack orders on top of an unknown state. The caller
        passes its own run_id, since audit.start() has already written that row."""
        return self.connection.execute(
            "SELECT 1 FROM execution_runs WHERE target_id=? AND dry_run=0 AND status='RUNNING' "
            "AND run_id<>? LIMIT 1",
            (target_id, except_run_id),
        ).fetchone() is not None

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
        # A real REST client also has to be POINTED at testnet, not merely labelled testnet.
        # Fakes in tests carry no base_url and are exempt; anything with one must match.
        base_url = getattr(client, "base_url", None)
        if base_url is not None and str(base_url) not in PAPER_BASE_URLS:
            raise ValueError(f"TestnetExecutor refuses a client whose base_url is not a paper host: {base_url}")
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

    def _gross_budget(self) -> float:
        return min(
            self.policy.max_gross_notional_usd,
            self._equity(self.client.account()) * self.policy.max_notional_to_equity,
        )

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
            legs.append(PlanLeg(
                symbol, side, chunk, reduce_only, reference, reason, desired, current,
                target_weight, execution_reference_price=mark,
            ))

    def build_plan(self, book: TargetBook, *, gross_budget: float | None = None) -> ExecutionPlan:
        self.client.sync_time()
        if self.client.position_mode():
            raise RuntimeError("dualSidePosition is enabled; one-way position mode is required")
        instruments = instruments_from_exchange_info(self.client.exchange_info())
        position_snapshot = list(self.client.positions())
        positions = {str(row.get("symbol", "")).upper(): _decimal(row.get("positionAmt", 0)) for row in position_snapshot}
        positions = {symbol: qty for symbol, qty in positions.items() if qty != 0}
        drift_by_symbol: dict[str, float] = {}
        gross_budget = self._gross_budget() if gross_budget is None else float(gross_budget)
        active = {symbol: weight for symbol, weight in book.weights.items() if abs(weight) > 0}
        if len(active) > self.policy.max_positions:
            raise RuntimeError(f"target has {len(active)} positions > configured max {self.policy.max_positions}")
        legs: list[PlanLeg] = []
        skips: list[str] = []
        expected_positions: dict[str, Decimal] = dict(positions)
        tolerance_budgets: dict[str, dict[str, str]] = {}
        orphan_symbols: list[str] = []
        for symbol in sorted(set(active) | set(positions)):
            weight, current = active.get(symbol, 0.0), positions.get(symbol, Decimal("0"))
            instrument = instruments.get(symbol)
            if not instrument:
                skips.append(f"{symbol}:{'uncloseable_position' if current else 'not_trading_or_unknown'}")
                continue
            tolerance_budgets[symbol] = {
                "step_size": str(instrument.step_size), "min_qty": str(instrument.min_qty),
                "min_notional": str(instrument.min_notional),
                "rounding_steps": str(self.policy.tolerance_rounding_steps),
                "min_notional_fraction": str(self.policy.tolerance_min_notional_fraction),
            }
            ticker = self.client.book_ticker(symbol)
            bid, ask = float(ticker["bidPrice"]), float(ticker["askPrice"])
            mark = (bid + ask) / 2.0
            # Fail CLOSED on a missing or non-positive paper reference. The old
            # `reference_prices.get(symbol) or mark` collapsed a missing price to the live
            # mark, giving drift == 0 and silently disabling this gate exactly when the
            # target file was incomplete - which is when it matters most.
            raw_reference = book.reference_prices.get(symbol)
            if not weight:
                # Orphan: on the exchange, absent from the target, about to be CLOSED. The
                # target file legitimately has no paper reference for it, and a drift gate
                # is meaningless for a close. Reference = live mark for attribution only.
                reference = mark
            else:
                if raw_reference is None or float(raw_reference) <= 0:
                    raise RuntimeError(
                        f"{symbol}: target has no positive paper reference price; refusing to plan "
                        f"(drift gate would be blind)"
                    )
                reference = float(raw_reference)
                reference_drift_bps = abs(mark / reference - 1.0) * 10_000
                drift_by_symbol[symbol] = reference_drift_bps
            if not weight:
                self._append_split(legs, instrument=instrument, mark=mark, symbol=symbol, side="SELL" if current > 0 else "BUY", quantity=abs(current), reduce_only=True, reference=reference, reason="close_orphan", desired=Decimal("0"), current=current, target_weight=0.0)
                expected_positions[symbol] = Decimal("0")
                orphan_symbols.append(symbol)
                continue
            desired_abs = _round_step(_decimal(abs(weight) * gross_budget / mark), instrument.step_size)
            if desired_abs < instrument.min_qty or desired_abs * _decimal(mark) < instrument.min_notional:
                # The requested non-zero target cannot be represented at this budget.
                # Abort the plan without constructing a close leg that can never run.
                skips.append(f"{symbol}:below_min_notional")
                continue
            desired = desired_abs if weight > 0 else -desired_abs
            if current and current * desired < 0:
                self._append_split(legs, instrument=instrument, mark=mark, symbol=symbol, side="SELL" if current > 0 else "BUY", quantity=abs(current), reduce_only=True, reference=reference, reason="close_before_flip", desired=desired, current=current, target_weight=weight)
                self._append_split(legs, instrument=instrument, mark=mark, symbol=symbol, side="BUY" if desired > 0 else "SELL", quantity=abs(desired), reduce_only=False, reference=reference, reason="open_after_flip", desired=desired, current=Decimal("0"), target_weight=weight)
                expected_positions[symbol] = desired
            else:
                delta = desired - current
                if abs(delta) >= instrument.min_qty and abs(delta) * _decimal(mark) >= instrument.min_notional:
                    self._append_split(legs, instrument=instrument, mark=mark, symbol=symbol, side="BUY" if delta > 0 else "SELL", quantity=abs(delta), reduce_only=current != 0 and current * delta < 0, reference=reference, reason="rebalance", desired=desired, current=current, target_weight=weight)
                    expected_positions[symbol] = desired
                elif delta != 0:
                    skips.append(f"{symbol}:delta_below_exchange_minimum_safe_noop")
        if any("uncloseable_position" in message for message in skips):
            raise RuntimeError("cannot safely close a current position: " + ", ".join(skips))
        if len(legs) > self.policy.max_orders:
            raise RuntimeError(f"reconciliation plan has {len(legs)} orders > configured max {self.policy.max_orders}")
        # Across the whole portfolio every reduce-only/closing leg must complete before
        # any opening leg.  Symbol sort alone can interleave opens ahead of later closes.
        legs.sort(key=lambda leg: (0 if leg.reduce_only else 1, leg.symbol, leg.reason))
        # Portfolio-level drift: a broad regime move since the paper close means every
        # reference is stale in the same direction; the median catches that even when no
        # single symbol crosses its own (wider) veto line.
        # Only symbols this plan will actually TRADE may veto it. A stale dust position we
        # deliberately leave alone (safe_noop) or a below-minimum target has no order to
        # protect, so its drift is recorded for the audit but does not gate.
        traded = {leg.symbol for leg in legs if not leg.reduce_only or leg.reason != "close_orphan"}
        gated = {sym: d for sym, d in drift_by_symbol.items() if sym in traded}
        for sym, d in sorted(gated.items(), key=lambda kv: -kv[1]):
            if d > self.policy.max_reference_drift_bps:
                raise RuntimeError(
                    f"{sym}: frozen reference drift {d:.2f}bps exceeds per-symbol limit "
                    f"{self.policy.max_reference_drift_bps:.2f}bps"
                )
        if gated:
            median_drift = float(statistics.median(gated.values()))
            if median_drift > self.policy.max_median_reference_drift_bps:
                worst = sorted(gated.items(), key=lambda kv: -kv[1])[:5]
                raise RuntimeError(
                    f"portfolio median reference drift {median_drift:.2f}bps exceeds "
                    f"{self.policy.max_median_reference_drift_bps:.2f}bps; worst: "
                    + ", ".join(f"{sym}={d:.0f}" for sym, d in worst)
                )
        return ExecutionPlan(
            legs=legs, skips=skips,
            expected_positions={symbol: quantity for symbol, quantity in sorted(expected_positions.items())},
            tolerance_budgets=tolerance_budgets, gross_budget=float(gross_budget),
            orphan_symbols=sorted(orphan_symbols),
            position_snapshot=position_snapshot, reference_drift_bps=drift_by_symbol,
        )

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

    def _submit_leg(
        self, run_id: str, book: TargetBook, leg: PlanLeg, sequence: int, *,
        force_market: bool = False, prepared_params: dict[str, Any] | None = None,
        prepared_client_order_id: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        client_order_id = prepared_client_order_id or self._client_order_id(run_id, book, leg, sequence)
        params = prepared_params or self._order_params(leg, client_order_id, force_market=force_market)
        # Re-quote immediately before the POST so the slippage gate measures the fill
        # against the price that existed when the order left, not the plan-time mark
        # (which for a late leg is minutes old and contaminated by market drift). Best
        # effort: a failed re-quote keeps the plan-time reference rather than blocking the
        # submit, and emergency flattens (force_market) are ungated anyway.
        if not force_market:
            try:
                fresh = self.client.book_ticker(leg.symbol)
                fresh_mid = (float(fresh["bidPrice"]) + float(fresh["askPrice"])) / 2.0
                if fresh_mid > 0:
                    leg.execution_reference_price = fresh_mid
            except Exception:
                pass
        try:
            response = self.client.order(**params)
        except BinanceAPIError as original:
            try:
                response = self.client.get_order_by_client_id(leg.symbol, client_order_id)
            except Exception:
                raise original
        if response.get("orderId"):
            order_id = int(response["orderId"])
            for _ in range(self.policy.order_poll_attempts):
                time.sleep(self.policy.poll_seconds)
                response = self.client.get_order(leg.symbol, order_id)
                if str(response.get("status", "UNKNOWN")) in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}:
                    break
        return response, client_order_id

    @staticmethod
    def _adverse_slippage_bps(leg: PlanLeg, response: dict[str, Any]) -> float | None:
        try:
            average = float(response.get("avgPrice", 0) or 0)
        except (TypeError, ValueError):
            return None
        if average <= 0 or leg.execution_reference_price <= 0:
            return None
        return (1.0 if leg.side == "BUY" else -1.0) * (
            average / leg.execution_reference_price - 1.0
        ) * 10_000

    def _safe_engage(self, run_id: str, reason: str) -> bool:
        try:
            self.kill_switch.engage(reason)
            return True
        except BaseException as exc:
            try:
                self.audit.positions(run_id, "kill_switch_engage_failed", {"reason": reason, "error": str(exc)})
            except BaseException:
                pass
            return False

    @staticmethod
    def _contract_sha(payload: dict[str, Any]) -> str:
        core = {key: value for key, value in payload.items() if key != "contract_sha256"}
        return hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _execution_contract(
        self, book: TargetBook, plan: ExecutionPlan, approval: dict[str, Any]
    ) -> dict[str, Any]:
        payload = {
            "version": "EXECUTION_POSITION_CONTRACT_V1",
            "target_id": book.target_id,
            "authorized_budget_usd": float(approval["authorized_budget_usd"]),
            "effective_gross_budget_usd": plan.gross_budget,
            "frozen_testnet_gross_ceiling_usd": FROZEN_TESTNET_GROSS_CEILING_USD,
            "ceilings_file_sha256": CEILINGS_SHA256,
            "expected_positions": {symbol: str(quantity) for symbol, quantity in plan.expected_positions.items()},
            # The tolerance rule is part of the hashed contract.  Its min-notional
            # component is converted to quantity only once verification prices exist.
            "tolerance_budgets": plan.tolerance_budgets,
            "accepted_skips": [item for item in plan.skips if item.endswith("safe_noop")],
            "orphan_symbols": plan.orphan_symbols,
            "requires_no_open_orders": True,
        }
        payload["contract_sha256"] = self._contract_sha(payload)
        return payload

    def _verify_execution_plan(
        self, plan: ExecutionPlan, positions: list[dict[str, Any]]
    ) -> tuple[bool, dict[str, Any]]:
        """Compare the account with the exact rounded vector emitted by build_plan."""

        actual = {
            str(row.get("symbol", "")).upper(): _decimal(row.get("positionAmt", 0))
            for row in positions if str(row.get("symbol", "")).strip()
        }
        symbols = sorted(set(plan.expected_positions) | {symbol for symbol, qty in actual.items() if qty != 0})
        try:
            prices = {}
            for symbol in symbols:
                ticker = self.client.book_ticker(symbol)
                prices[symbol] = (float(ticker["bidPrice"]) + float(ticker["askPrice"])) / 2.0
            global_open_orders = self.client.open_orders()
        except BaseException as exc:
            raise VerificationUnavailableError(f"verification snapshot unavailable: {exc}") from exc
        open_order_counts: dict[str, int] = {}
        for order in global_open_orders:
            symbol = str(order.get("symbol", "")).upper()
            open_order_counts[symbol] = open_order_counts.get(symbol, 0) + 1
        rows = []
        for symbol in symbols:
            expected = plan.expected_positions.get(symbol, Decimal("0"))
            observed = actual.get(symbol, Decimal("0"))
            price = _decimal(prices[symbol])
            tolerance = quantity_tolerance(plan.tolerance_budgets.get(symbol, {}), price)
            error = abs(observed - expected)
            rows.append({
                "symbol": symbol, "verification_price": prices[symbol],
                "expected_quantity": str(expected), "actual_quantity": str(observed),
                "quantity_error": str(error), "quantity_tolerance": str(tolerance),
                "expected_notional": float(expected * price), "actual_notional": float(observed * price),
                "open_orders": open_order_counts.get(symbol, 0),
                # quantity_ok answers "is the POSITION right"; ok additionally requires no
                # resting order. execute() needs them separately: a wrong quantity is a
                # flatten, a stray order on a correct quantity is a cancel.
                "price_ok": price > 0,
                "quantity_ok": error <= tolerance,
                "ok": price > 0 and error <= tolerance and open_order_counts.get(symbol, 0) == 0,
            })
        unpriced = [row["symbol"] for row in rows if not row["price_ok"]]
        if unpriced:
            # A zero quote does not tell us the position is wrong; it tells us we cannot
            # verify. Same class as a quote timeout: hand off, do not liquidate.
            raise VerificationUnavailableError(
                f"verification quote is zero for {unpriced}; cannot judge positions"
            )
        expected_gross = sum(abs(row["expected_notional"]) for row in rows)
        actual_gross = sum(abs(row["actual_notional"]) for row in rows)
        payload = {
            "price_basis": "single post-trade book-ticker pass",
            "expected_gross_notional": expected_gross,
            "actual_gross_notional": actual_gross,
            "global_open_orders": global_open_orders,
            "rows": rows,
        }
        return bool(rows) and not global_open_orders and all(row["ok"] for row in rows), payload

    def _configure_symbol(self, symbol: str) -> None:
        try:
            self.client.set_margin_type(symbol, self.policy.margin_type)
        except BinanceAPIError as exc:
            code = exc.payload.get("code") if isinstance(exc.payload, dict) else None
            if int(code or 0) != -4046:  # "No need to change margin type."
                raise
        self.client.set_leverage(symbol, self.policy.leverage)

    def _write_sidecar(self, run_id: str, book: TargetBook, final_status: str,
                       exc: BaseException, finish_exc: BaseException, orders_started: bool) -> None:
        """Out-of-band terminal record for when the audit DB itself is unwritable."""
        try:
            held = self.client.positions()
        except BaseException as pos_exc:
            held = [{"snapshot_error": str(pos_exc)}]
        record = {
            "run_id": run_id, "target_id": book.target_id, "final_status": final_status,
            "orders_started": orders_started, "error": f"{type(exc).__name__}: {exc}",
            "audit_finish_error": f"{type(finish_exc).__name__}: {finish_exc}",
            "positions_held": held, "written_utc": utc_now(),
        }
        sidecar = self.audit.sidecar_path()
        with open(sidecar, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")

    def _cancel_only(self, run_id: str, symbols: set[str], reason: str) -> bool:
        """Cancel known and globally visible orders without changing any position."""

        try:
            pending_before = self.client.open_orders()
        except BaseException as exc:
            pending_before = []
            try:
                self.audit.positions(run_id, "cancel_only_snapshot_failed", {"error": str(exc)})
            except BaseException:
                pass
        pending_symbols = {
            str(row.get("symbol", "")).upper()
            for row in pending_before if str(row.get("symbol", "")).strip()
        }
        failures = {}
        for symbol in sorted(set(symbols) | pending_symbols):
            try:
                self.client.cancel_all(symbol)
            except BaseException as exc:
                failures[symbol] = str(exc)
        try:
            pending_after = self.client.open_orders()
            verified = not pending_after
        except BaseException as exc:
            pending_after = [{"verification_error": str(exc)}]
            verified = False
        # The whole point of cancel-only is that positions are LEFT on the exchange for a
        # human. That human needs to know what they were handed: snapshot the live book
        # into the audit under a phase the reconciler knows to read.
        try:
            held = self.client.positions()
        except BaseException as exc:
            held = [{"snapshot_error": str(exc)}]
        # `verified` reflects the EXCHANGE (orders gone or not). Failing to WRITE that fact
        # to the audit must not be reported as a failed cancel - the two are different
        # facts, and this method is also called precisely when the audit is broken.
        try:
            self.audit.positions(run_id, "cancel_only_positions", held)
            self.audit.positions(run_id, "cancel_only_summary", {
                "reason": reason, "known_symbols": sorted(symbols),
                "open_orders_before": pending_before, "open_orders_after": pending_after,
                "cancel_failures": failures, "verified": verified,
                "positions_held": held,
            })
        except BaseException:
            pass  # sidecar in execute()'s handler carries the held book when the DB is down
        return verified

    def _flatten_all(self, run_id: str, reason: str, target_symbols: set[str]) -> bool:
        """MARKET-only emergency flatten verified by inventory and open orders.

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
                pending_before = self.client.open_orders()
            except BaseException as exc:
                self.audit.positions(run_id, f"flatten_inventory_failed:{attempt}", {"error": str(exc), "reason": reason})
                try:
                    time.sleep(self.policy.flatten_retry_seconds)
                except BaseException as sleep_exc:
                    self.audit.positions(run_id, f"flatten_retry_interrupted:{attempt}", {"error": str(sleep_exc)})
                continue
            nonzero = [row for row in positions if _decimal(row.get("positionAmt", 0)) != 0]
            pending_symbols = {
                str(row.get("symbol", "")).upper()
                for row in pending_before if str(row.get("symbol", "")).strip()
            }
            for symbol in sorted(pending_symbols):
                try:
                    self.client.cancel_all(symbol)
                    cancelled.add(symbol)
                except BaseException as exc:
                    self.audit.positions(run_id, f"cancel_all_failed:{symbol}", {"error": str(exc)})
            try:
                pending_after = self.client.open_orders()
            except BaseException as exc:
                self.audit.positions(run_id, f"flatten_open_orders_verify_failed:{attempt}", {"error": str(exc)})
                pending_after = pending_before
            self.audit.positions(run_id, f"emergency_flatten_attempt:{attempt}", {
                "positions": positions, "open_orders_before": pending_before,
                "open_orders_after": pending_after,
            })
            if not nonzero and not pending_after:
                self.audit.positions(run_id, "emergency_flatten_verified", {
                    "positions": [], "open_orders": [],
                })
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
                    leg = PlanLeg(
                        symbol, "SELL" if current > 0 else "BUY", abs(current), True,
                        reference, "emergency_flatten", Decimal(0), current, 0.0,
                        execution_reference_price=reference,
                    )
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
            pending = self.client.open_orders()
            self.audit.positions(run_id, "emergency_flatten_unresolved", {
                "positions": remaining, "open_orders": pending,
            })
        except BaseException as exc:
            self.audit.positions(run_id, "emergency_flatten_verify_failed", {"error": str(exc)})
        return False

    def execute(self, book: TargetBook, *, dry_run: bool) -> dict[str, Any]:
        run_id, armed, orders_started = uuid.uuid4().hex, False, False
        approval: dict[str, Any] = {}
        plan: ExecutionPlan | None = None
        self.audit.start(run_id, book, dry_run)
        try:
            if not dry_run:
                self.assert_target_fresh(book)
                self.assert_target_identity(book)
                if self.audit.target_completed(book.target_id):
                    raise RuntimeError(f"target_id already completed: {book.target_id}")
                if self.audit.target_in_flight(book.target_id, except_run_id=run_id):
                    raise RuntimeError(
                        f"target_id has a run still RUNNING: {book.target_id}; refusing to stack a "
                        f"second execution on an unknown state (resolve or mark the old run first)"
                    )
                approval = self.kill_switch.assert_released_for_testnet(
                    max_age_seconds=self.policy.kill_switch_release_ttl_seconds,
                    expected_target_id=book.target_id,
                    expected_budget_usd=self.policy.max_gross_notional_usd,
                )
                armed = True
            gross_budget = self._gross_budget()
            plan = self.build_plan(book, gross_budget=gross_budget)
            legs, skips = plan.legs, plan.skips
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
            # Same read the contract was derived from - not a second, independent call.
            self.audit.positions(run_id, "before_orders", plan.position_snapshot)
            self.audit.positions(run_id, "reference_drift_bps", plan.reference_drift_bps)
            orphan_symbols = plan.orphan_symbols
            self.audit.positions(run_id, "orphan_symbols", orphan_symbols)
            pending_before = self.client.open_orders()
            pending_symbols = sorted({str(item.get("symbol", "")).upper() for item in pending_before if item.get("symbol")})
            for symbol in sorted(set(orphan_symbols) | set(pending_symbols)):
                # Always cancel orphan symbols, even when the first snapshot is empty:
                # an order can race the snapshot and otherwise reopen a just-flat book.
                self.client.cancel_all(symbol)
            pending_after = self.client.open_orders()
            self.audit.positions(run_id, "preflight_open_orders", {"before": pending_before, "after": pending_after, "cancelled_symbols": sorted(set(orphan_symbols) | set(pending_symbols))})
            if pending_after:
                raise RuntimeError("open orders remain after preflight cancel_all")
            contract = self._execution_contract(book, plan, approval)
            self.audit.positions(run_id, "execution_contract", contract)
            for symbol, weight in book.weights.items():
                if abs(weight) > 0:
                    self._configure_symbol(symbol)
            for index, leg in enumerate(legs):
                # Everything BEFORE _submit_leg on a given iteration cannot have moved a
                # position. If it fails after an earlier leg already filled, the right
                # response is to stop and hand off (HaltedError), not to liquidate a book
                # that is correct as far as anyone can tell. Only the submit itself, and the
                # post-fill checks, may escalate to a flatten.
                client_order_id = self._client_order_id(run_id, book, leg, index)
                try:
                    prepared_params = self._order_params(leg, client_order_id)
                    self.kill_switch.assert_released_for_testnet(
                        max_age_seconds=self.policy.kill_switch_release_ttl_seconds,
                        expected_target_id=book.target_id,
                        expected_budget_usd=self.policy.max_gross_notional_usd,
                    )
                except BaseException as pre_exc:
                    if orders_started:
                        raise HaltedError(
                            f"halted before leg {index} ({leg.symbol} {leg.reason}) with "
                            f"{index} legs already filled: {type(pre_exc).__name__}: {pre_exc}"
                        ) from pre_exc
                    raise
                orders_started = True
                response, client_order_id = self._submit_leg(
                    run_id, book, leg, index, prepared_params=prepared_params,
                    prepared_client_order_id=client_order_id,
                )
                status = str(response.get("status", "UNKNOWN"))
                try:
                    self.audit.leg(run_id, index, leg, status, response, client_order_id=client_order_id)
                except BaseException as audit_exc:
                    raise AuditWriteError(
                        f"audit rejected leg row {index} ({leg.symbol}, order status {status}): {audit_exc}"
                    ) from audit_exc
                if status != "FILLED":
                    raise RuntimeError(f"{leg.symbol} {leg.reason} did not fill: {status}")
                slippage = self._adverse_slippage_bps(leg, response)
                if slippage is not None and slippage > self.policy.max_fill_slippage_bps:
                    # The fill happened and the quantity is what the contract asked for;
                    # only the PRICE was worse than the submit-time quote. That is a venue
                    # event, not a position error - stop issuing, keep what we have.
                    raise HaltedError(
                        f"{leg.symbol} adverse fill slippage {slippage:.2f}bps exceeds "
                        f"limit {self.policy.max_fill_slippage_bps:.2f}bps; halting"
                    )
            try:
                after_positions = self.client.positions()
            except BaseException as exc:
                raise VerificationUnavailableError(f"positionRisk verification unavailable: {exc}") from exc
            try:
                self.audit.positions(run_id, "after_orders", after_positions)
            except BaseException as audit_exc:
                raise AuditWriteError(f"audit rejected after_orders snapshot: {audit_exc}") from audit_exc
            positions_ok, verification = self._verify_execution_plan(plan, after_positions)
            verification["contract_sha256"] = contract["contract_sha256"]
            try:
                self.audit.positions(run_id, "target_verification", verification)
            except BaseException as audit_exc:
                raise AuditWriteError(f"audit rejected target_verification: {audit_exc}") from audit_exc
            if not positions_ok:
                quantity_failed = {
                    str(row["symbol"]) for row in verification["rows"]
                    if not row.get("quantity_ok", row["ok"])
                }
                safe_noop_symbols = {
                    message.split(":", 1)[0]
                    for message in plan.skips if message.endswith("safe_noop")
                }
                if not quantity_failed:
                    # Every position quantity is right; only resting orders (on these or on
                    # unrelated symbols) spoiled verification. Cancel them, do not liquidate.
                    stray = verification.get("global_open_orders") or []
                    stray_symbols = sorted({str(o.get("symbol", "")) for o in stray})
                    raise HaltedError(
                        f"positions match contract but {len(stray)} open order(s) remain "
                        f"on {stray_symbols}; cancel-only"
                    )
                if quantity_failed <= safe_noop_symbols:
                    raise ExternalPositionDriftError(
                        "external position drift on safe-noop symbols: "
                        + ", ".join(sorted(quantity_failed))
                    )
                bad = [
                    "{} obs={} exp={} tol={}".format(
                        row["symbol"], row.get("actual_quantity"),
                        row.get("expected_quantity"), row.get("quantity_tolerance"))
                    for row in verification["rows"] if str(row["symbol"]) in quantity_failed
                ]
                raise TargetMismatchError(
                    "final exchange positions do not match execution contract on "
                    + "; ".join(bad)
                )
            if not self._safe_engage(run_id, f"run {run_id} complete; manual release required for next run"):
                # Verification already passed: the book is exactly the contract. Failing to
                # WRITE the kill-switch file is an operational fault, not a position fault.
                raise HaltedError(
                    "positions verified correct but the kill switch could not be re-engaged; "
                    "book left intact - engage the switch manually before any further run"
                )
            try:
                self.audit.finish(run_id, "COMPLETE", f"{len(legs)} legs, {len(skips)} skips")
            except BaseException as audit_exc:
                # Positions verified, kill switch re-engaged - the book is right. Failing to
                # write the word COMPLETE must not liquidate it (round-9 review found this
                # was the one post-order audit write still routed to a flatten).
                raise AuditWriteError(f"audit rejected COMPLETE for a verified book: {audit_exc}") from audit_exc
            return {"run_id": run_id, "status": "COMPLETE", "legs": len(legs), "skips": skips, "contract_sha256": contract["contract_sha256"], "verification": verification}
        except BaseException as exc:
            if armed:
                self._safe_engage(run_id, f"automatic emergency stop: {type(exc).__name__}: {exc}")
            flatten_ok: bool | None = None
            cancel_only_ok: bool | None = None
            surface_symbols = set(book.weights)
            if plan is not None:
                surface_symbols.update(plan.expected_positions)
                surface_symbols.update(plan.orphan_symbols)
            if isinstance(exc, (ExternalPositionDriftError, HaltedError, AuditWriteError)):
                try:
                    cancel_only_ok = self._cancel_only(run_id, surface_symbols, str(exc))
                except BaseException as cleanup_exc:
                    cancel_only_ok = False
                    try:
                        self.audit.positions(run_id, "external_drift_cancel_crashed", {"error": str(cleanup_exc)})
                    except BaseException:
                        pass
            elif orders_started and not isinstance(exc, VerificationUnavailableError):
                try:
                    flatten_ok = self._flatten_all(run_id, f"{type(exc).__name__}: {exc}", surface_symbols)
                except BaseException as cleanup_exc:
                    flatten_ok = False
                    try:
                        self.audit.positions(run_id, "emergency_cleanup_crashed", {"error": str(cleanup_exc)})
                    except BaseException:
                        pass
            if isinstance(exc, AuditWriteError):
                final_status = "HALTED_AUDIT_UNAVAILABLE" if cancel_only_ok else "HALTED_CANCEL_FAILED"
            elif isinstance(exc, HaltedError):
                final_status = "HALTED_MID_BOOK" if cancel_only_ok else "HALTED_CANCEL_FAILED"
            elif isinstance(exc, ExternalPositionDriftError):
                final_status = "EXTERNAL_POSITION_DRIFT" if cancel_only_ok else "EXTERNAL_DRIFT_CANCEL_FAILED"
            elif flatten_ok is False:
                final_status = "UNRESOLVED_EXPOSURE"
            elif isinstance(exc, VerificationUnavailableError):
                final_status = "VERIFICATION_UNAVAILABLE"
            elif isinstance(exc, TargetMismatchError):
                final_status = "MISMATCH"
            elif isinstance(exc, KeyboardInterrupt):
                final_status = "INTERRUPTED"
            else:
                final_status = "FAILED"
            try:
                self.audit.finish(run_id, final_status, str(exc))
            except BaseException as finish_exc:
                # The audit store is the thing that failed. Do not let the terminal state
                # vanish: write a plain-text sidecar next to the DB with everything a human
                # needs to pick the book up. This is the last line, so it must not raise.
                try:
                    self._write_sidecar(run_id, book, final_status, exc, finish_exc, orders_started)
                except BaseException:
                    pass
            raise
