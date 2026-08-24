"""LIVE execution CLI for CARRY-7d. Manual only; structurally inert until authorized.

This file exists so that the live path is written, reviewed and tested in calm - not
improvised in the excitement of the day the paper gate passes. Every invocation today
fails at ExecutionPolicy construction, because execution_ceilings_v1.json declares
live: 0.0 and the policy refuses a live environment without a positive reviewed ceiling.
The ONLY unlock is a reviewed ceilings revision - no flag, env var, or edit here can arm it.

Differences from the testnet CLI, all deliberate:
  * No unattended mode, ever. Every live run is a human typing two confirmations.
  * Its own kill-switch file (kill_switch_live.json, environment='live'). A testnet
    release can never arm a live run - the KillSwitch environment binding refuses it.
  * Requires --confirm-live I_AUTHORIZE_REAL_MONEY_ORDERS and --acknowledge-max-loss with
    the dollar figure you accept losing (must be >= 35% of budget: backtest maxDD -14.7%,
    live planning assumption 2x, plus margin for being wrong about that too).
  * Separate credentials: BINANCE_LIVE_API_KEY / BINANCE_LIVE_API_SECRET. The .env.testnet
    loader intentionally cannot supply these.

Read GO_LIVE_CHECKLIST.md before the first use. If anything here surprises you, stop.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from execution.binance_futures import FuturesREST  # noqa: E402
from execution.contracts import frozen_ceiling  # noqa: E402
from execution.engine import ExecutionAudit, ExecutionPolicy, KillSwitch, PortfolioExecutor  # noqa: E402
from execution.targets import load_target_book, sha256_file  # noqa: E402

TARGETS = ROOT / "execution" / "carry_targets_latest.json"
PAPER_CONFIG = ROOT / "carry_paper_config_v1.json"
KILL_LIVE = ROOT / ".execution" / "kill_switch_live.json"
AUDIT_LIVE = ROOT / ".execution" / "live_execution.sqlite3"
CONFIRM = "I_AUTHORIZE_REAL_MONEY_ORDERS"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", default=str(TARGETS))
    parser.add_argument("--budget-usd", type=float, required=False)
    parser.add_argument("--authorize-budget-usd", type=float)
    parser.add_argument("--plan", action="store_true", help="read-only preview of legs/skips/drift; places nothing")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-live", default="")
    parser.add_argument("--acknowledge-max-loss", type=float, help="dollar loss you accept; must be >= 35%% of budget")
    parser.add_argument("--release-kill-switch", metavar="REASON")
    parser.add_argument("--engage-kill-switch", metavar="REASON")
    args = parser.parse_args()

    ceiling = frozen_ceiling("live")
    if ceiling <= 0:
        print("LIVE IS NOT AUTHORIZED: execution_ceilings declares live=0.0.")
        print("This CLI stays inert until a REVIEWED ceilings revision sets a positive live number.")
        print("Process and capital framework: GO_LIVE_CHECKLIST.md")
        return 2

    kill = KillSwitch(KILL_LIVE, environment="live")
    if args.engage_kill_switch:
        kill.engage(args.engage_kill_switch)
        print(f"LIVE kill switch engaged: {KILL_LIVE}")
        return 0

    book = load_target_book(args.targets)

    if args.release_kill_switch:
        if not args.authorize_budget_usd:
            raise RuntimeError("release requires --authorize-budget-usd")
        if args.authorize_budget_usd > ceiling:
            raise RuntimeError(f"authorized budget exceeds LIVE ceiling ${ceiling:.2f}")
        kill.release(args.release_kill_switch, target_id=book.target_id,
                     authorized_budget_usd=args.authorize_budget_usd)
        print(f"LIVE kill switch released for target={book.target_id} budget=${args.authorize_budget_usd:.2f} (TTL 15m)")
        return 0

    if args.budget_usd is None:
        raise RuntimeError("--budget-usd is required for --plan and --execute")
    policy = ExecutionPolicy(environment="live", max_gross_notional_usd=args.budget_usd,
                             expected_config_sha256=sha256_file(PAPER_CONFIG))
    client = FuturesREST.from_env("live", required=True)
    executor = PortfolioExecutor(client, policy, kill, ExecutionAudit(AUDIT_LIVE))

    if args.plan:
        executor.assert_target_fresh(book)
        executor.assert_target_identity(book)
        result = executor.execute(book, dry_run=True)
        print(f"LIVE PLAN target={book.target_id}: {len(result.get('legs', []))} legs, "
              f"{len(result.get('skips', []))} skips (nothing placed)")
        for leg in result.get("legs", []):
            print(f"  {leg['symbol']:12s} {leg['side']:4s} qty={leg['quantity']} reduce_only={leg['reduce_only']} {leg['reason']}")
        for skip in result.get("skips", []):
            print(f"  SKIP {skip}")
        return 0

    if not args.execute:
        print("Nothing to do: use --plan, --release-kill-switch, or --execute.")
        return 0
    if args.confirm_live != CONFIRM:
        raise RuntimeError(f"refusing real-money orders without --confirm-live {CONFIRM}")
    required_ack = 0.35 * args.budget_usd
    if not args.acknowledge_max_loss or args.acknowledge_max_loss < required_ack:
        raise RuntimeError(
            f"refusing: --acknowledge-max-loss must be >= ${required_ack:.0f} "
            f"(35% of budget; backtest maxDD -14.7%, live planning assumption 2x)"
        )
    result = executor.execute(book, dry_run=False)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
