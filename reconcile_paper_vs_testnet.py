"""Read-only target-vs-realized-position reconciliation for the testnet audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from execution.contracts import CEILINGS_SHA256, EXPOSURE_STATUSES as _EXPOSURE_STATUSES, FROZEN_TESTNET_GROSS_CEILING_USD, frozen_ceiling, quantity_tolerance
from execution.targets import load_target_book


ROOT = Path(__file__).resolve().parent
DEFAULT_AUDIT = ROOT / ".execution" / "testnet_execution.sqlite3"
DEFAULT_TARGET = ROOT / "execution" / "carry_targets_latest.json"


def _position_map(payload: str) -> dict[str, Decimal]:
    return {str(row.get("symbol", "")).upper(): Decimal(str(row.get("positionAmt", 0))) for row in json.loads(payload)}


# Single source of truth for which statuses mean "positions may be live": the registry in
# execution.contracts. Round 9 taught us not to keep a second copy here.
EXPOSURE_STATUSES = tuple(sorted(_EXPOSURE_STATUSES))
# Snapshot phases that describe what is HELD at the end of a run, in preference order.
HELD_PHASES = ("after_orders", "cancel_only_positions", "emergency_flatten_unresolved", "emergency_flatten_verified")


def open_exposure_runs(conn: sqlite3.Connection, target_id: str):
    """Non-COMPLETE live runs for this target that may have left a book on the exchange."""
    placeholders = ",".join("?" for _ in EXPOSURE_STATUSES)
    return conn.execute(
        "SELECT run_id,started_utc,finished_utc,status,message FROM execution_runs "
        f"WHERE target_id=? AND dry_run=0 AND status IN ({placeholders}) ORDER BY started_utc DESC",
        (target_id, *EXPOSURE_STATUSES),
    ).fetchall()


def select_execution_run(conn: sqlite3.Connection, target_id: str, run_id: str | None = None):
    if run_id:
        return conn.execute(
            "SELECT run_id,started_utc,finished_utc,environment,dry_run,status,message FROM execution_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    return conn.execute(
        "SELECT run_id,started_utc,finished_utc,environment,dry_run,status,message "
        "FROM execution_runs WHERE target_id=? AND dry_run=0 AND status='COMPLETE' "
        "ORDER BY started_utc DESC LIMIT 1",
        (target_id,),
    ).fetchone()


def contract_sha256(contract: dict) -> str:
    core = {key: value for key, value in contract.items() if key != "contract_sha256"}
    return hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def compare_contract_to_positions(
    contract: dict, actual: dict[str, Decimal], verification: dict
) -> tuple[bool, list[dict], dict]:
    """Recompute the exact rounded-vector contract without plan-leg internals."""

    expected = {symbol: Decimal(str(quantity)) for symbol, quantity in contract.get("expected_positions", {}).items()}
    tolerance_budgets = contract.get("tolerance_budgets", {})
    verification_rows = {str(row.get("symbol", "")).upper(): row for row in verification.get("rows", [])}
    symbols = sorted(set(expected) | {symbol for symbol, quantity in actual.items() if quantity != 0})
    rows = []
    for symbol in symbols:
        source = verification_rows.get(symbol, {})
        price = Decimal(str(source.get("verification_price", 0) or 0))
        budget = tolerance_budgets.get(symbol, {})
        tolerance = quantity_tolerance(budget, price)
        expected_quantity = expected.get(symbol, Decimal("0"))
        actual_quantity = actual.get(symbol, Decimal("0"))
        error = abs(actual_quantity - expected_quantity)
        open_orders = int(source.get("open_orders", 0) or 0)
        rows.append({
            "symbol": symbol, "verification_price": float(price),
            "expected_quantity": str(expected_quantity), "actual_quantity": str(actual_quantity),
            "quantity_error": str(error), "quantity_tolerance": str(tolerance),
            "expected_notional": float(expected_quantity * price),
            "actual_notional": float(actual_quantity * price), "open_orders": open_orders,
            "ok": price > 0 and error <= tolerance and open_orders == 0,
        })
    gross = {
        "expected_gross_notional": sum(abs(row["expected_notional"]) for row in rows),
        "actual_gross_notional": sum(abs(row["actual_notional"]) for row in rows),
    }
    gross["gross_error_usd"] = abs(gross["actual_gross_notional"] - gross["expected_gross_notional"])
    no_global_open_orders = not verification.get("global_open_orders", [])
    return bool(rows) and no_global_open_orders and all(row["ok"] for row in rows), rows, gross


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", default=str(DEFAULT_TARGET))
    parser.add_argument("--audit", default=str(DEFAULT_AUDIT))
    parser.add_argument("--run-id", help="default: latest run for the target")
    args = parser.parse_args()
    target = load_target_book(args.targets)
    if not Path(args.audit).exists():
        print("No testnet execution audit exists yet; nothing to reconcile.")
        return 0
    conn = sqlite3.connect(args.audit)
    # LOUD FIRST: any run that ended in a hand-off state is live exposure that a
    # COMPLETE-only lookup would silently skip. Report it before anything else, and never
    # exit 0 while one exists.
    exposure = open_exposure_runs(conn, target.target_id)
    if exposure and not args.run_id:
        newest = exposure[0]
        held_row = next((r for r in conn.execute(
            "SELECT phase,positions_json FROM position_snapshots WHERE run_id=? ORDER BY captured_utc DESC", (newest[0],)
        ).fetchall() if r[0] in HELD_PHASES), None)
        held = json.loads(held_row[1]) if held_row else None
        sidecar = Path(args.audit + ".sidecar.jsonl")
        print(json.dumps({
            "ATTENTION": "target has a run that ended in a hand-off state; positions may be live and unhedged",
            "sidecar": (f"{sidecar} exists - read its LAST line, it is the audit's out-of-band terminal record"
                        if sidecar.exists() else "no sidecar (audit DB accepted the terminal row)"),
            "target_id": target.target_id,
            "runs_with_open_exposure": [dict(zip(("run_id", "started_utc", "finished_utc", "status", "message"), r)) for r in exposure],
            "positions_held_at_handoff": held if held is not None else "NO POSITION SNAPSHOT RECORDED - query the exchange directly",
            "action": "inspect the exchange, decide flatten/keep manually, then reconcile with --run-id if a later COMPLETE run exists",
        }, indent=2, default=str))
        return 3
    row = select_execution_run(conn, target.target_id, args.run_id)
    if not row:
        sidecar = Path(args.audit + ".sidecar.jsonl")
        if sidecar.exists():
            print(f"No COMPLETE run for {target.target_id}, but {sidecar} exists: a run ended while "
                  f"the audit DB was unwritable. Read its last line before assuming the account is flat.")
            return 3
        print(f"No execution run exists for frozen target {target.target_id}.")
        return 0
    run_id = row[0]
    leg_columns = {item[1] for item in conn.execute("PRAGMA table_info(execution_legs)")}
    execution_reference_column = "execution_reference_price" if "execution_reference_price" in leg_columns else "NULL"
    legs = conn.execute(
        "SELECT symbol,side,quantity,reduce_only,reference_price,reason,status,order_id,"
        "avg_fill_price,fill_slippage_bps,client_order_id,desired_quantity,current_quantity,"
        f"target_weight,{execution_reference_column} FROM execution_legs "
        "WHERE run_id=? ORDER BY sequence", (run_id,),
    ).fetchall()
    snapshots = conn.execute("SELECT phase,captured_utc,positions_json FROM position_snapshots WHERE run_id=? ORDER BY captured_utc", (run_id,)).fetchall()
    # What is held at the end: after_orders for a normal run; for hand-off runs the
    # cancel-only / flatten snapshots are the truth. Never default to "nothing".
    final_snapshot = next(
        (item for phase in HELD_PHASES for item in reversed(snapshots) if item[0] == phase),
        None,
    )
    contract_snapshot = next((item for item in reversed(snapshots) if item[0] == "execution_contract"), None)
    verification_snapshot = next((item for item in reversed(snapshots) if item[0] == "target_verification"), None)
    actual = _position_map(final_snapshot[2]) if final_snapshot else {}
    contract = json.loads(contract_snapshot[2]) if contract_snapshot else {}
    verification = json.loads(verification_snapshot[2]) if verification_snapshot else {}
    contract_hash_ok = bool(contract) and contract.get("contract_sha256") == contract_sha256(contract)
    binding_ok = contract.get("target_id") == target.target_id and verification.get("contract_sha256") == contract.get("contract_sha256")
    try:
        authorized_budget = float(contract["authorized_budget_usd"])
        effective_budget = float(contract["effective_gross_budget_usd"])
        env = str(contract.get("environment", "testnet"))
        recorded_ceiling = float(contract.get("frozen_gross_ceiling_usd",
                                              contract.get("frozen_testnet_gross_ceiling_usd")))
        authorization_ok = (
            authorized_budget > 0
            and 0 < effective_budget <= authorized_budget <= recorded_ceiling
            and recorded_ceiling == frozen_ceiling(env)
            and contract.get("ceilings_file_sha256", CEILINGS_SHA256) == CEILINGS_SHA256
        )
    except (KeyError, TypeError, ValueError):
        authorization_ok = False
    vector_ok, comparison, gross = compare_contract_to_positions(contract, actual, verification) if contract else (False, [], {})
    ok = bool(final_snapshot) and bool(contract_snapshot) and bool(verification_snapshot) and row[5] == "COMPLETE" and not bool(row[4]) and contract_hash_ok and binding_ok and authorization_ok and vector_ok
    report = {
        "target": {"target_id": target.target_id, "strategy": target.strategy, "gross": target.gross, "net": target.net},
        "run": dict(zip(("run_id", "started_utc", "finished_utc", "environment", "dry_run", "status", "message"), row)),
        "reconciliation_pass": ok,
        "contract": contract,
        "contract_hash_ok": contract_hash_ok,
        "target_binding_ok": binding_ok,
        "authorization_binding_ok": authorization_ok,
        "gross_reconciliation": gross,
        "comparison": comparison,
        "fills": [dict(zip(("symbol", "side", "quantity", "reduce_only", "paper_reference_price", "reason", "status", "order_id", "avg_fill_price", "execution_slippage_bps", "client_order_id", "desired_quantity", "current_quantity", "target_weight", "execution_reference_price"), leg)) for leg in legs],
        "position_snapshots": [{"phase": phase, "captured_utc": captured, "positions": json.loads(payload)} for phase, captured, payload in snapshots],
    }
    print(json.dumps(report, indent=2, default=str, allow_nan=False))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
