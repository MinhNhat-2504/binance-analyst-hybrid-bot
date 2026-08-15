"""Testnet-only execution runner for a frozen CARRY target book.

Default mode is a non-mutating dry run.  Actual testnet orders require every one of:
``--execute``, an exact confirmation phrase, separate ``BINANCE_TESTNET_*`` credentials,
and a manually released testnet kill-switch file.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from execution.binance_futures import FuturesREST
from execution.contracts import FROZEN_TESTNET_GROSS_CEILING_USD
from execution.engine import ExecutionAudit, ExecutionPolicy, KillSwitch, TestnetExecutor
from execution.targets import load_target_book, sha256_file


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".execution"
KILL = RUNTIME / "kill_switch.json"
AUDIT = RUNTIME / "testnet_execution.sqlite3"
PAPER_CONFIG = ROOT / "carry_paper_config_v1.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", default=str(ROOT / "execution" / "carry_targets_latest.json"))
    parser.add_argument("--budget-usd", type=float, default=500.0)
    parser.add_argument("--authorize-budget-usd", type=float, help="required only when releasing; binds operator approval to an exact budget")
    parser.add_argument("--order-style", choices=["MARKET", "LIMIT_IOC"], default="MARKET")
    parser.add_argument("--plan", action="store_true", help="read-only network calls (time, exchangeInfo, positions, quotes) to print the exact legs, skips, drift and contract this run WOULD execute; places nothing, needs no kill-switch release")
    parser.add_argument("--execute", action="store_true", help="submit TESTNET orders; default is dry-run")
    parser.add_argument("--confirm-testnet", default="")
    parser.add_argument("--release-kill-switch", metavar="REASON")
    parser.add_argument("--engage-kill-switch", metavar="REASON")
    parser.add_argument("--set-one-way-mode", action="store_true", help="testnet account setup only; does not place orders")
    args = parser.parse_args()

    kill = KillSwitch(KILL)
    if args.engage_kill_switch:
        kill.engage(args.engage_kill_switch)
        print(f"testnet kill switch engaged: {KILL}")
        return 0

    if args.set_one_way_mode:
        if args.confirm_testnet != "I_ACCEPT_TESTNET_ORDERS":
            raise RuntimeError("refusing account-mode change without --confirm-testnet I_ACCEPT_TESTNET_ORDERS")
        client = FuturesREST.from_env("testnet", required=True)
        if client.position_mode():
            client.set_position_mode(dual_side=False)
            print("testnet account switched to one-way position mode")
        else:
            print("testnet account is already in one-way position mode")
        return 0

    target_path = Path(args.targets)
    if not target_path.exists():
        raise FileNotFoundError(
            f"target file not found: {target_path}. Generate the frozen paper target first: "
            "python -B export_carry_targets.py"
        )
    book = load_target_book(target_path)
    if args.release_kill_switch:
        if args.authorize_budget_usd is None or args.authorize_budget_usd <= 0:
            raise RuntimeError("release requires --authorize-budget-usd with the operator-approved amount")
        if args.authorize_budget_usd > FROZEN_TESTNET_GROSS_CEILING_USD:
            raise RuntimeError(
                f"authorized budget exceeds frozen testnet ceiling "
                f"${FROZEN_TESTNET_GROSS_CEILING_USD:.2f}"
            )
        kill.release(
            args.release_kill_switch, target_id=book.target_id,
            authorized_budget_usd=args.authorize_budget_usd,
        )
        print(f"testnet kill switch released for target={book.target_id} budget=${args.authorize_budget_usd:.2f}: {KILL}")
        return 0
    if args.plan:
        # The drift gate, min-notional skips and orphan closes only reveal themselves when
        # a plan is actually built against live quotes. Without this the operator's first
        # encounter with an abort was AFTER releasing the kill switch and typing the
        # confirmation phrase. dry_run=True never arms, never submits, never cancels.
        client = FuturesREST.from_env("testnet", required=True)
        policy = ExecutionPolicy(
            max_gross_notional_usd=args.budget_usd, order_style=args.order_style,
            expected_config_sha256=sha256_file(PAPER_CONFIG),
        )
        executor = TestnetExecutor(client, policy, kill, ExecutionAudit(AUDIT))
        # These two are pure local checks with no side effects; a preview that skipped
        # them would show a clean plan for a target that --execute is about to refuse.
        executor.assert_target_fresh(book)
        executor.assert_target_identity(book)
        result = executor.execute(book, dry_run=True)
        legs = result.get("legs", [])
        print(f"PLAN for target={book.target_id}: {len(legs)} legs, {len(result.get('skips', []))} skips (nothing placed)")
        for leg in legs:
            print(f"  {leg['symbol']:12s} {leg['side']:4s} qty={leg['quantity']} reduce_only={leg['reduce_only']} reason={leg['reason']}")
        for skip in result.get("skips", []):
            print(f"  SKIP {skip}")
        return 0
    if not args.execute:
        print("DRY RUN ONLY: no network request and no order will be placed.")
        print(json.dumps({"target_id": book.target_id, "strategy": book.strategy, "gross": book.gross, "net": book.net, "positions": len(book.weights)}, indent=2))
        print("To test execution: release with --release-kill-switch REASON --authorize-budget-usd AMOUNT for this exact target, then use --execute --budget-usd AMOUNT --confirm-testnet I_ACCEPT_TESTNET_ORDERS")
        return 0
    if args.confirm_testnet != "I_ACCEPT_TESTNET_ORDERS":
        raise RuntimeError("refusing order submission without --confirm-testnet I_ACCEPT_TESTNET_ORDERS")
    if args.budget_usd > FROZEN_TESTNET_GROSS_CEILING_USD:
        raise RuntimeError(
            f"execution budget exceeds frozen testnet ceiling "
            f"${FROZEN_TESTNET_GROSS_CEILING_USD:.2f}"
        )

    client = FuturesREST.from_env("testnet", required=True)
    policy = ExecutionPolicy(
        max_gross_notional_usd=args.budget_usd,
        order_style=args.order_style,
        expected_config_sha256=sha256_file(PAPER_CONFIG),
    )
    executor = TestnetExecutor(client, policy, kill, ExecutionAudit(AUDIT))
    result = executor.execute(book, dry_run=False)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
