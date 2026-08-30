"""Runtime safety and route-aware profit guardrails.

The module is deliberately independent from the notebook so its policy,
heartbeat, and stop-hit inference can be tested without starting the bot loop.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd


HEARTBEAT_PATH = Path("bot_heartbeat.json")
STOP_HIT_MODEL_PATH = Path("stop_hit_risk_model.pkl")
ROUTE_POLICY_PATH = Path("route_policy_v1.json")


DEFAULT_ROUTE_POLICY = {
    "version": "ROUTE_ROUTER_V2",
    "default": {
        "mode": "RESEARCH_ONLY",
        "min_proba": 1.01,
        "max_stop_hit_probability": 0.0,
        "size_multiplier": 0.0,
    },
    "routes": {
        "MIRROR_SHORT": {
            "mode": "PAPER_ONLY",
            "min_proba": 0.82,
            "max_stop_hit_probability": 0.58,
            "size_multiplier": 0.10,
            "allow_unvalidated_model_for_paper": True,
            "min_closed_proof": 30,
            "min_pf": 1.25,
            "min_distinct_days": 3,
            "max_proof_age_hours": 168,
            "max_drawdown": 0.03,
        },
        "CANARY_MIRROR": {
            "mode": "PAPER_ONLY",
            "min_proba": 0.90,
            "max_stop_hit_probability": 0.52,
            "size_multiplier": 0.05,
            "allow_unvalidated_model_for_paper": True,
            "min_closed_proof": 2,
            "min_pf": 1.0,
            "min_distinct_days": 1,
            "max_proof_age_hours": 0,
            "max_drawdown": 0.01,
        },
        "CANARY_EXACT_BUCKET": {
            "mode": "PAPER_ONLY",
            "min_proba": 0.75,
            "max_stop_hit_probability": 0.52,
            "size_multiplier": 0.05,
            "allow_unvalidated_model_for_paper": True,
            "min_closed_proof": 0,
            "min_pf": 0.0,
            "min_distinct_days": 0,
            "max_proof_age_hours": 0,
            "max_drawdown": 0.0,
        },
        "STRICT_PROMOTE": {
            "mode": "RESEARCH_ONLY",
            "min_proba": 0.90,
            "max_stop_hit_probability": 0.40,
            "size_multiplier": 0.0,
        },
        "DISCOVERY": {
            "mode": "RESEARCH_ONLY",
            "min_proba": 0.95,
            "max_stop_hit_probability": 0.35,
            "size_multiplier": 0.0,
        },
        "SCOUT": {
            "mode": "DISABLED",
            "min_proba": 1.01,
            "max_stop_hit_probability": 0.0,
            "size_multiplier": 0.0,
        },
    },
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class HeartbeatState:
    status: str = "INIT"
    cycle_id: str = ""
    cycle_started_utc: str = ""
    cycle_finished_utc: str = ""
    last_progress_utc: str = ""
    last_ledger_mtime_utc: str = ""
    last_symbol: str = ""
    scanned_symbols: int = 0
    expected_symbols: int = 0
    consecutive_failures: int = 0
    last_error: str = ""
    policy_version: str = ""
    process_id: int = 0
    working_directory: str = ""
    python_executable: str = ""
    ledger_path: str = ""
    notebook_mtime_utc: str = ""


class RuntimeHeartbeat:
    def __init__(
        self,
        path: str | Path = HEARTBEAT_PATH,
        stale_seconds: int = 1_200,
        check_interval_seconds: int = 30,
    ) -> None:
        self.path = Path(path)
        self.stale_seconds = int(stale_seconds)
        self.check_interval_seconds = int(check_interval_seconds)
        self.state = HeartbeatState()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _write(self) -> None:
        with self._lock:
            atomic_write_json(self.path, asdict(self.state))

    def start_watchdog(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch_loop, daemon=True, name="bot-heartbeat-watchdog")
        self._thread.start()

    def startup(
        self,
        policy_version: str = "",
        ledger_path: str | Path | None = None,
        notebook_path: str | Path | None = None,
    ) -> None:
        ledger = Path(ledger_path).resolve() if ledger_path else None
        notebook = Path(notebook_path).resolve() if notebook_path else None
        notebook_mtime = ""
        if notebook and notebook.exists():
            notebook_mtime = datetime.fromtimestamp(
                notebook.stat().st_mtime, tz=timezone.utc
            ).isoformat()
        self.state = HeartbeatState(
            status="STARTING",
            last_progress_utc=utc_now_iso(),
            policy_version=str(policy_version),
            process_id=os.getpid(),
            working_directory=str(Path.cwd().resolve()),
            python_executable=str(Path(sys.executable).resolve()),
            ledger_path=str(ledger) if ledger else "",
            notebook_mtime_utc=notebook_mtime,
        )
        self._write()

    def stop_watchdog(self) -> None:
        self._stop.set()

    def cycle_start(self, expected_symbols: int, policy_version: str = "") -> str:
        cycle_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        process_id = self.state.process_id or os.getpid()
        working_directory = self.state.working_directory or str(Path.cwd().resolve())
        python_executable = self.state.python_executable or str(Path(sys.executable).resolve())
        ledger_path = self.state.ledger_path
        notebook_mtime_utc = self.state.notebook_mtime_utc
        self.state = HeartbeatState(
            status="RUNNING",
            cycle_id=cycle_id,
            cycle_started_utc=utc_now_iso(),
            last_progress_utc=utc_now_iso(),
            expected_symbols=int(expected_symbols),
            policy_version=str(policy_version),
            process_id=process_id,
            working_directory=working_directory,
            python_executable=python_executable,
            ledger_path=ledger_path,
            notebook_mtime_utc=notebook_mtime_utc,
        )
        self._write()
        return cycle_id

    def progress(self, symbol: str, scanned_symbols: int, ledger_path: str | Path | None = None) -> None:
        self.state.status = "RUNNING"
        self.state.last_progress_utc = utc_now_iso()
        self.state.last_symbol = str(symbol)
        self.state.scanned_symbols = int(scanned_symbols)
        if ledger_path:
            path = Path(ledger_path)
            if path.exists():
                self.state.last_ledger_mtime_utc = datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc
                ).isoformat()
        self._write()

    def cycle_end(self, ledger_path: str | Path | None = None) -> None:
        self.state.status = "IDLE"
        self.state.scanned_symbols = self.state.expected_symbols
        self.state.cycle_finished_utc = utc_now_iso()
        self.state.last_progress_utc = utc_now_iso()
        self.state.consecutive_failures = 0
        self.state.last_error = ""
        if ledger_path:
            path = Path(ledger_path)
            if path.exists():
                self.state.last_ledger_mtime_utc = datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc
                ).isoformat()
        self._write()

    def cycle_error(self, error: BaseException) -> None:
        self.state.status = "ERROR"
        self.state.last_progress_utc = utc_now_iso()
        self.state.consecutive_failures += 1
        self.state.last_error = f"{type(error).__name__}: {error}"[:1_000]
        self._write()

    def _watch_loop(self) -> None:
        while not self._stop.wait(self.check_interval_seconds):
            if self.state.status != "RUNNING" or not self.state.last_progress_utc:
                continue
            last = pd.to_datetime(self.state.last_progress_utc, errors="coerce", utc=True)
            if pd.isna(last):
                continue
            age = (pd.Timestamp.now(tz="UTC") - last).total_seconds()
            if age > self.stale_seconds:
                self.state.status = "STALE"
                self.state.last_error = f"No scan progress for {int(age)} seconds"
                self._write()


def classify_route(profit_focus_reason: Any, symbol_prior_status: Any = "") -> str:
    reason = str(profit_focus_reason or "").upper()
    status = str(symbol_prior_status or "").upper()
    if "CANARY_MIRROR" in reason or "SHORT0_CANARY_MIRROR" in status:
        return "CANARY_MIRROR"
    if "SHORT0_CANARY_EXACT_BUCKET" in reason or "SHORT0_CANARY_EXACT_BUCKET" in status:
        return "CANARY_EXACT_BUCKET"
    if "MIRROR" in reason or status == "MIRROR_SHORT_FROM_LONG_R0":
        return "MIRROR_SHORT"
    if "SCOUT" in reason:
        return "SCOUT"
    if "DISCOVERY" in reason or status.startswith("DISCOVERY_"):
        return "DISCOVERY"
    if "STRICT_SYMBOL" in reason or status in {"PROMOTE_SYMBOL", "ADAPTIVE_PROMOTE_SYMBOL"}:
        return "STRICT_PROMOTE"
    return "UNKNOWN"


def load_route_policy(path: str | Path = ROUTE_POLICY_PATH) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        atomic_write_json(path, DEFAULT_ROUTE_POLICY)
        return json.loads(json.dumps(DEFAULT_ROUTE_POLICY))
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else json.loads(json.dumps(DEFAULT_ROUTE_POLICY))
    except Exception:
        return json.loads(json.dumps(DEFAULT_ROUTE_POLICY))


@dataclass
class RouteDecision:
    allowed: bool
    route: str
    mode: str
    reason: str
    size_multiplier: float
    min_proba: float
    max_stop_hit_probability: float


@dataclass
class RouteProof:
    closed_trades: int = 0
    total_pnl: float = 0.0
    profit_factor: float = math.nan
    win_rate: float = math.nan
    distinct_days: int = 0
    newest_closed_utc: str = ""
    proof_age_hours: float = math.inf
    max_drawdown: float = math.nan


class RouteAwareRouter:
    def __init__(
        self,
        policy_path: str | Path = ROUTE_POLICY_PATH,
        ledger_path: str | Path = "shadow_ledger_candidates_v4.csv",
    ) -> None:
        self.policy_path = Path(policy_path)
        self.ledger_path = Path(ledger_path)
        self.policy = load_route_policy(self.policy_path)
        self._mtime = self.policy_path.stat().st_mtime if self.policy_path.exists() else None
        self._ledger_mtime: float | None = None
        self._route_proofs: dict[str, RouteProof] = {}

    def refresh(self) -> None:
        if not self.policy_path.exists():
            return
        mtime = self.policy_path.stat().st_mtime
        if self._mtime != mtime:
            self.policy = load_route_policy(self.policy_path)
            self._mtime = mtime
        self._refresh_route_proofs()

    def _refresh_route_proofs(self) -> None:
        if not self.ledger_path.exists():
            self._route_proofs = {}
            self._ledger_mtime = None
            return
        mtime = self.ledger_path.stat().st_mtime
        if self._ledger_mtime == mtime:
            return
        try:
            frame = pd.read_csv(self.ledger_path, low_memory=False)
            for column in [
                "final_gate_decision",
                "execution_stage",
                "size_usd",
                "Outcome_PnL",
                "route_name",
                "profit_focus_reason",
                "symbol_prior_status",
                "timestamp_utc",
                "Exit_Timestamp_UTC",
            ]:
                if column not in frame.columns:
                    frame[column] = np.nan
            decision = frame["final_gate_decision"].fillna(frame["execution_stage"]).fillna("")
            executable = decision.astype(str).str.upper().isin(
                ["PAPER_TRADE", "TRADE_LIVE", "TRADE_MICRO_LIVE"]
            )
            size = pd.to_numeric(frame["size_usd"], errors="coerce").fillna(0.0)
            pnl = pd.to_numeric(frame["Outcome_PnL"], errors="coerce")
            frame = frame.loc[executable & size.gt(0) & pnl.notna()].copy()
            frame["_pnl"] = pnl.loc[frame.index]
            explicit_route = frame["route_name"].fillna("").astype(str).str.upper()
            inferred_route = [
                classify_route(reason, status)
                for reason, status in zip(
                    frame["profit_focus_reason"],
                    frame["symbol_prior_status"],
                )
            ]
            frame["_route"] = np.where(explicit_route.str.len().gt(0), explicit_route, inferred_route)
            frame["_ts"] = pd.to_datetime(
                frame["timestamp_utc"], errors="coerce", utc=True, format="mixed"
            )
            frame["_closed_ts"] = pd.to_datetime(
                frame["Exit_Timestamp_UTC"], errors="coerce", utc=True, format="mixed"
            ).fillna(frame["_ts"])
            proof_window = int(self.policy.get("proof_window_closed_trades", 60))
            proofs: dict[str, RouteProof] = {}
            for route, group in frame.sort_values("_ts").groupby("_route"):
                group = group.tail(proof_window)
                values = group["_pnl"].astype(float)
                gains = float(values[values > 0].sum())
                losses = float(-values[values < 0].sum())
                pf = gains / losses if losses > 0 else (math.inf if gains > 0 else math.nan)
                timestamps = group["_closed_ts"].dropna()
                newest = timestamps.max() if not timestamps.empty else pd.NaT
                proof_age = (
                    float((pd.Timestamp.now(tz="UTC") - newest).total_seconds() / 3600.0)
                    if pd.notna(newest)
                    else math.inf
                )
                curve = values.cumsum()
                drawdown = curve - curve.cummax()
                proofs[str(route)] = RouteProof(
                    closed_trades=int(len(values)),
                    total_pnl=float(values.sum()),
                    profit_factor=float(pf),
                    win_rate=float((values > 0).mean()),
                    distinct_days=int(timestamps.dt.date.nunique()) if not timestamps.empty else 0,
                    newest_closed_utc=newest.isoformat() if pd.notna(newest) else "",
                    proof_age_hours=proof_age,
                    max_drawdown=float(drawdown.min()) if not drawdown.empty else math.nan,
                )
            self._route_proofs = proofs
            self._ledger_mtime = mtime
        except Exception:
            self._route_proofs = {}
            self._ledger_mtime = mtime

    def get_route_proof(self, route: str) -> RouteProof:
        self.refresh()
        return self._route_proofs.get(str(route).upper(), RouteProof())

    def decide(
        self,
        route: str,
        final_proba: float,
        stop_hit_probability: float | None,
        trade_live: bool = False,
    ) -> RouteDecision:
        self.refresh()
        default = self.policy.get("default", {})
        config = self.policy.get("routes", {}).get(route, default)
        mode = str(config.get("mode", "RESEARCH_ONLY")).upper()
        min_proba = float(config.get("min_proba", 1.01))
        max_stop = float(config.get("max_stop_hit_probability", 0.0))
        size_mult = float(config.get("size_multiplier", 0.0))
        allow_unvalidated_paper = bool(config.get("allow_unvalidated_model_for_paper", False))
        min_closed_proof = int(config.get("min_closed_proof", 0))
        min_pf = float(config.get("min_pf", 0.0))
        min_distinct_days = int(config.get("min_distinct_days", 0))
        max_proof_age_hours = float(config.get("max_proof_age_hours", 0.0))
        max_drawdown = float(config.get("max_drawdown", 0.0))
        proba = float(final_proba or 0.0)

        if mode in {"DISABLED", "RESEARCH_ONLY"}:
            return RouteDecision(False, route, mode, f"ROUTE_{mode}", 0.0, min_proba, max_stop)
        if proba < min_proba:
            return RouteDecision(False, route, mode, f"ROUTE_PROBA_BLOCK:{proba:.3f}<{min_proba:.2f}", 0.0, min_proba, max_stop)
        proof = self.get_route_proof(route)
        if proof.closed_trades < min_closed_proof:
            return RouteDecision(
                False,
                route,
                mode,
                f"ROUTE_PROOF_WARMUP:{proof.closed_trades}<{min_closed_proof}",
                0.0,
                min_proba,
                max_stop,
            )
        if min_pf > 0 and (
            not math.isfinite(proof.profit_factor) and proof.profit_factor != math.inf
            or proof.profit_factor < min_pf
        ):
            return RouteDecision(
                False,
                route,
                mode,
                f"ROUTE_PROOF_BLOCK:PF{proof.profit_factor:.2f}<{min_pf:.2f}",
                0.0,
                min_proba,
                max_stop,
            )
        if proof.distinct_days < min_distinct_days:
            return RouteDecision(
                False,
                route,
                mode,
                f"ROUTE_DAY_DIVERSITY_BLOCK:{proof.distinct_days}<{min_distinct_days}",
                0.0,
                min_proba,
                max_stop,
            )
        if max_proof_age_hours > 0 and proof.proof_age_hours > max_proof_age_hours:
            return RouteDecision(
                False,
                route,
                mode,
                f"ROUTE_STALE_PROOF:{proof.proof_age_hours:.0f}h>{max_proof_age_hours:.0f}h",
                0.0,
                min_proba,
                max_stop,
            )
        if max_drawdown > 0 and math.isfinite(proof.max_drawdown) and proof.max_drawdown < -max_drawdown:
            return RouteDecision(
                False,
                route,
                mode,
                f"ROUTE_DRAWDOWN_BLOCK:{proof.max_drawdown:.4f}<-{max_drawdown:.4f}",
                0.0,
                min_proba,
                max_stop,
            )
        if stop_hit_probability is None or not math.isfinite(float(stop_hit_probability)):
            if mode == "PAPER_ONLY" and allow_unvalidated_paper and not trade_live:
                return RouteDecision(
                    True,
                    route,
                    "PAPER_ONLY",
                    "ROUTE_PAPER_APPROVED_MODEL_PENDING",
                    size_mult,
                    min_proba,
                    max_stop,
                )
            return RouteDecision(False, route, mode, "STOP_HIT_MODEL_UNAVAILABLE", 0.0, min_proba, max_stop)
        if float(stop_hit_probability) > max_stop:
            return RouteDecision(
                False,
                route,
                mode,
                f"STOP_HIT_RISK_BLOCK:{float(stop_hit_probability):.3f}>{max_stop:.2f}",
                0.0,
                min_proba,
                max_stop,
            )
        if mode == "PAPER_ONLY" or not trade_live:
            return RouteDecision(True, route, "PAPER_ONLY", "ROUTE_PAPER_APPROVED", size_mult, min_proba, max_stop)
        return RouteDecision(True, route, "LIVE", "ROUTE_LIVE_APPROVED", size_mult, min_proba, max_stop)


class StopHitRiskPredictor:
    def __init__(self, model_path: str | Path = STOP_HIT_MODEL_PATH) -> None:
        self.model_path = Path(model_path)
        self.bundle: dict[str, Any] | None = None
        self._mtime: float | None = None

    def refresh(self) -> None:
        if not self.model_path.exists():
            self.bundle = None
            self._mtime = None
            return
        mtime = self.model_path.stat().st_mtime
        if self.bundle is None or self._mtime != mtime:
            loaded = joblib.load(self.model_path)
            if not isinstance(loaded, dict) or "pipeline" not in loaded:
                raise ValueError("Invalid stop-hit model bundle")
            self.bundle = loaded
            self._mtime = mtime

    def is_deployment_eligible(self) -> bool:
        try:
            self.refresh()
            return bool(self.bundle and self.bundle.get("deployment_eligible", False))
        except Exception:
            return False

    def predict_probability(
        self,
        features: dict[str, Any],
        require_eligible: bool = True,
    ) -> float | None:
        try:
            self.refresh()
            if not self.bundle:
                return None
            if require_eligible and not bool(self.bundle.get("deployment_eligible", False)):
                return None
            feature_columns = list(self.bundle.get("feature_columns", []))
            row = {column: features.get(column, np.nan) for column in feature_columns}
            frame = pd.DataFrame([row], columns=feature_columns)
            probability = float(self.bundle["pipeline"].predict_proba(frame)[0, 1])
            return float(np.clip(probability, 0.0, 1.0))
        except Exception:
            return None


def build_stop_hit_features(
    *,
    route: str,
    symbol: str,
    side: str,
    regime: Any,
    final_proba: float,
    edge_after_cost: float,
    confidence_multiplier: float,
    pocket_health_multiplier: float,
    symbol_prior_multiplier: float,
    proba_bucket_multiplier: float,
    entry_edge_multiplier: float,
    exit_cluster_multiplier: float,
    meta_ev: float,
    meta_uncertainty: float,
    gating_proba: float = 0.0,
    transition_proba: float = 0.0,
    dl_proba: float = 0.0,
    xgb_proba: float = 0.0,
    edge_short: float = 0.0,
    t2_ev_short: float = 0.0,
) -> dict[str, Any]:
    return {
        "route": str(route),
        "symbol": str(symbol).upper(),
        "side": str(side).upper(),
        "regime": str(regime),
        "final_proba": float(final_proba or 0.0),
        "gating_proba": float(gating_proba or 0.0),
        "transition_proba": float(transition_proba or 0.0),
        "dl_proba": float(dl_proba or 0.0),
        "xgb_proba": float(xgb_proba or 0.0),
        "edge_short": float(edge_short or 0.0),
        "edge_after_cost": float(edge_after_cost or 0.0),
        "confidence_multiplier": float(confidence_multiplier or 0.0),
        "pocket_health_multiplier": float(pocket_health_multiplier or 0.0),
        "symbol_prior_multiplier": float(symbol_prior_multiplier or 0.0),
        "proba_bucket_multiplier": float(proba_bucket_multiplier or 0.0),
        "entry_edge_multiplier": float(entry_edge_multiplier or 0.0),
        "exit_cluster_multiplier": float(exit_cluster_multiplier or 0.0),
        "meta_ev": float(meta_ev or 0.0),
        "meta_uncertainty": float(meta_uncertainty or 0.0),
        "t2_ev_short": float(t2_ev_short or 0.0),
    }
