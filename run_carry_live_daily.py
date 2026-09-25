"""Unattended daily LIVE loop for CARRY-7d. Written now, inert until three things agree.

The testnet loop earns its right to self-release the kill switch because there is no
money on testnet. This file is the same loop (run_carry_testnet_daily.run_loop, one body,
one set of tests) pointed at real money, so the right to run unattended has to come from
somewhere else. It comes from three independent facts that all have to hold, each owned
by a different act of review:

  1. execution_ceilings declares live > 0.       (a reviewed ceilings revision - v2)
  2. unattended_live_v1.json authorizes it.      (written by hand on the day, see below)
  3. The authorization names the sha256 of the   (so any later ceilings change silently
     ceilings file it was written against.        revokes the authorization)

With the shipped ceilings (live: 0.0) this file exits 2 "NOT AUTHORIZED" and writes
nothing - no marker, no log row, no incident. It is not an error to run it early; it is
simply not armed. The GO_LIVE_CHECKLIST timeline is: gate GO -> ceilings v2 -> >= 10
clean MANUAL live days with run_live_execution.py -> only then this file.

unattended_live_v1.json (hand-written, gitignored):
    {
      "unattended_live": true,
      "max_gross_usd": 2000.0,               <= the live ceiling; the loop uses min(both)
      "expires_utc": "2026-12-31",           the loop stops itself after this date
      "ceilings_sha256": "<CEILINGS_SHA256>", must match execution.contracts.CEILINGS_SHA256
      "operator_note": "why, when, by whom"  non-empty
    }

Everything else is inherited unchanged: lock, DD guard (20% of the AUTHORIZED budget in
dollars), plan-then-execute, kill switch release bound to target+budget+15-minute TTL
and re-engaged in finally, reconcile, ATTENTION on anything but COMPLETE. Its own kill
switch (kill_switch_live.json, environment='live'), audit (live_execution.sqlite3), lock
and log. Credentials: BINANCE_LIVE_API_KEY / _SECRET from the real environment only; the
.env.testnet loader cannot supply them.

    python run_carry_live_daily.py                    # scheduled, 00:20 UTC, via run_daily.py live
    python run_carry_live_daily.py --reset-equity-hwm # after review
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import run_carry_testnet_daily as daily  # noqa: E402
from execution.contracts import CEILINGS_SHA256, frozen_ceiling  # noqa: E402
from execution.engine import PortfolioExecutor  # noqa: E402

ENVIRONMENT = "live"
TARGETS = ROOT / "execution" / "carry_targets_latest.json"
KILL_LIVE = ROOT / ".execution" / "kill_switch_live.json"
AUDIT_LIVE = ROOT / ".execution" / "live_execution.sqlite3"
ATTENTION = ROOT / ".execution" / "ATTENTION"          # shared: an unread incident blocks every loop
LOG_LIVE = ROOT / "carry_live_log.csv"
LOCK_LIVE = ROOT / ".execution" / "live_daily.lock"
INCIDENTS = ROOT / "carry_paper_incidents.md"
AUTHORIZATION = ROOT / "unattended_live_v1.json"
EXIT_NOT_AUTHORIZED = 2


class NotAuthorized(SystemExit):
    def __init__(self, why: str) -> None:
        print(f"LIVE UNATTENDED NOT AUTHORIZED: {why}")
        super().__init__(EXIT_NOT_AUTHORIZED)


def authorized_budget(today: date | None = None) -> float:
    """Return the gross budget the loop may use, or raise NotAuthorized. Reads files only."""
    ceiling = float(frozen_ceiling(ENVIRONMENT))
    if ceiling <= 0:
        raise NotAuthorized("execution_ceilings declares live=0.0 (GO_LIVE_CHECKLIST.md, Part 4)")
    if not AUTHORIZATION.exists():
        raise NotAuthorized(f"{AUTHORIZATION.name} is absent - unattended live needs the hand-written authorization")
    try:
        auth = json.loads(AUTHORIZATION.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise NotAuthorized(f"{AUTHORIZATION.name} unreadable: {exc}") from exc
    if auth.get("unattended_live") is not True:
        raise NotAuthorized("authorization does not set unattended_live: true")
    if str(auth.get("ceilings_sha256", "")) != CEILINGS_SHA256:
        raise NotAuthorized("authorization was written against a different ceilings file; re-authorize after review")
    if not str(auth.get("operator_note", "")).strip():
        raise NotAuthorized("authorization has no operator_note")
    try:
        expires = date.fromisoformat(str(auth.get("expires_utc", "")))
    except ValueError as exc:
        raise NotAuthorized("authorization expires_utc is not a YYYY-MM-DD date") from exc
    today = today or datetime.now(timezone.utc).date()
    if today > expires:
        raise NotAuthorized(f"authorization expired on {expires}")
    try:
        max_gross = float(auth.get("max_gross_usd", 0))
    except (TypeError, ValueError) as exc:
        raise NotAuthorized("authorization max_gross_usd is not a number") from exc
    if not 0 < max_gross <= ceiling:
        raise NotAuthorized(f"authorization max_gross_usd {max_gross} must be in (0, live ceiling {ceiling}]")
    return min(max_gross, ceiling)


def _refuse_unless_live_authorized() -> None:
    authorized_budget()
    if ATTENTION.exists():
        raise SystemExit(
            f"REFUSING: {ATTENTION} exists from a previous run. Read the runbook, resolve, "
            f"record in {INCIDENTS.name}, delete the marker, then runs resume."
        )


def _live_env() -> daily.LoopEnv:
    budget = authorized_budget()
    return daily.LoopEnv(
        environment=ENVIRONMENT, targets=TARGETS, kill=KILL_LIVE, audit=AUDIT_LIVE, attention=ATTENTION,
        log=LOG_LIVE, lock=LOCK_LIVE, incidents=INCIDENTS, budget_usd=budget,
        executor_factory=PortfolioExecutor, refuse=_refuse_unless_live_authorized,
        release_reason="unattended LIVE run", guard_budget_usd=budget,
    )


def main(argv: Sequence[str] = ()) -> int:
    try:
        env = _live_env()
    except NotAuthorized as exc:
        return int(exc.code)
    return daily.run_loop(env, argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
