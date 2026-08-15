"""Read-only target-vs-realized-position reconciliation for the testnet audit."""

from __future__ import annotations

import argparse
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from execution.targets import load_target_book


ROOT = Path(__file__).resolve().parent
DEFAULT_AUDIT = ROOT / ".execution" / "testnet_execution.sqlite3"
DEFAULT_TARGET = ROOT / "execution" / "carry_targets_latest.json"


def _position_map(payload: str) -> dict[str, Decimal]:
    return {str(row.get("symbol", "")).upper(): Decimal(str(row.get("positionAmt", 0))) for row in json.loads(payload)}


def compare_target_to_positions(target, actual: dict[str, Decimal], fallback_prices: dict[str, float], *, weight_tolerance: float, flat_quantity_tolerance: float) -> tuple[bool, list[dict]]:
    """Compare the independent target book with realized positions, not plan internals."""
    symbols = sorted(set(target.weights) | {symbol for symbol, qty in actual.items() if qty != 0})
    prices = {symbol: float(target.reference_prices.get(symbol) or fallback_prices.get(symbol) or 0) for symbol in symbols}
    actual_gross = sum(abs(float(actual.get(symbol, 0)) * prices[symbol]) for symbol in symbols)
    rows = []
    for symbol in symbols:
        target_weight = float(target.weights.get(symbol, 0.0))
        actual_quantity = actual.get(symbol, Decimal("0"))
        actual_weight = float(actual_quantity) * prices[symbol] / actual_gross if actual_gross > 0 and prices[symbol] > 0 else None
        weight_error = abs(actual_weight - target_weight) if actual_weight is not None else None
        ok = weight_error is not None and weight_error <= weight_tolerance
        if target_weight == 0.0:
            ok = ok and abs(actual_quantity) <= Decimal(str(flat_quantity_tolerance))
        rows.append({"symbol": symbol, "reference_price": prices[symbol] or None, "target_weight": target_weight, "actual_weight": actual_weight, "weight_error": weight_error, "actual_quantity": str(actual_quantity), "ok": ok})
    return bool(rows) and all(row["ok"] for row in rows), rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", default=str(DEFAULT_TARGET))
    parser.add_argument("--audit", default=str(DEFAULT_AUDIT))
    parser.add_argument("--run-id", help="default: latest run for the target")
    parser.add_argument("--flat-quantity-tolerance", type=float, default=0.000001)
    parser.add_argument("--weight-tolerance", type=float, default=0.01)
    args = parser.parse_args()
    target = load_target_book(args.targets)
    if not Path(args.audit).exists():
        print("No testnet execution audit exists yet; nothing to reconcile.")
        return 0
    conn = sqlite3.connect(args.audit)
    row = conn.execute("SELECT run_id,started_utc,finished_utc,environment,dry_run,status,message FROM execution_runs WHERE target_id=? ORDER BY started_utc DESC LIMIT 1", (target.target_id,)).fetchone() if not args.run_id else conn.execute("SELECT run_id,started_utc,finished_utc,environment,dry_run,status,message FROM execution_runs WHERE run_id=?", (args.run_id,)).fetchone()
    if not row:
        print(f"No execution run exists for frozen target {target.target_id}.")
        return 0
    run_id = row[0]
    legs = conn.execute("SELECT symbol,side,quantity,reduce_only,reference_price,reason,status,order_id,avg_fill_price,fill_slippage_bps,client_order_id,desired_quantity,current_quantity,target_weight FROM execution_legs WHERE run_id=? ORDER BY sequence", (run_id,)).fetchall()
    snapshots = conn.execute("SELECT phase,captured_utc,positions_json FROM position_snapshots WHERE run_id=? ORDER BY captured_utc", (run_id,)).fetchall()
    final_snapshot = next((item for item in reversed(snapshots) if item[0] == "after_orders"), None)
    actual = _position_map(final_snapshot[2]) if final_snapshot else {}
    fallback_prices: dict[str, float] = {}
    for leg in legs:
        symbol, ref = leg[0], float(leg[4] or 0)
        if symbol:
            fallback_prices[symbol] = ref
    weights_ok, comparison = compare_target_to_positions(target, actual, fallback_prices, weight_tolerance=args.weight_tolerance, flat_quantity_tolerance=args.flat_quantity_tolerance)
    ok = bool(final_snapshot) and row[5] == "COMPLETE" and not bool(row[4]) and weights_ok
    report = {
        "target": {"target_id": target.target_id, "strategy": target.strategy, "gross": target.gross, "net": target.net},
        "run": dict(zip(("run_id", "started_utc", "finished_utc", "environment", "dry_run", "status", "message"), row)),
        "reconciliation_pass": ok,
        "comparison": comparison,
        "fills": [dict(zip(("symbol", "side", "quantity", "reduce_only", "reference_price", "reason", "status", "order_id", "avg_fill_price", "fill_slippage_bps", "client_order_id", "desired_quantity", "current_quantity", "target_weight"), leg)) for leg in legs],
        "position_snapshots": [{"phase": phase, "captured_utc": captured, "positions": json.loads(payload)} for phase, captured, payload in snapshots],
    }
    print(json.dumps(report, indent=2, default=str, allow_nan=False))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
