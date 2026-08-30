"""Unattended daily testnet rehearsal for CARRY-7d.

Runs the runbook's normal-day cycle without a human at the keyboard:

    export targets -> plan -> release kill switch for THIS target -> execute -> re-engage
    -> reconcile -> if anything is not COMPLETE/exit-0, leave a loud marker and log it.

Why this is allowed to self-release the kill switch, and why only here:

  The manual release (target_id + budget + 15-minute TTL) exists to put a human decision
  in front of every real-money order. On testnet there is no money, and the thing we are
  trying to accumulate - twenty-plus consecutive daily runs across flips, orphans and bad
  days - is exactly the thing a human-in-the-loop requirement prevents from happening.
  So this script releases for the current target immediately before executing and
  re-engages immediately after, in the same process, and it is structurally unable to do
  that for anything but testnet:

    1. It constructs FuturesREST("testnet") and TestnetExecutor, both of which refuse any
       other environment at construction (ExecutionPolicy.__post_init__ raises).
    2. It refuses to run at all unless the ceilings file still declares live == 0.0. The
       day execution_ceilings_v2.json authorises live capital, this script stops working
       by itself; nobody has to remember to turn it off.

Outcomes:
  * COMPLETE and reconcile exit 0  -> silent. One line appended to carry_testnet_log.csv.
  * Anything else                  -> writes .execution/ATTENTION (a file whose mere presence
                                      says "read the runbook"), appends to
                                      carry_paper_incidents.md, exits non-zero.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from execution.binance_futures import FuturesREST  # noqa: E402
from execution.contracts import FROZEN_TESTNET_GROSS_CEILING_USD, load_ceilings  # noqa: E402
from execution.engine import ExecutionAudit, ExecutionPolicy, KillSwitch, TestnetExecutor  # noqa: E402
from execution.targets import load_target_book, sha256_file  # noqa: E402

PYTHON = sys.executable
TARGETS = ROOT / "execution" / "carry_targets_latest.json"
PAPER_CONFIG = ROOT / "carry_paper_config_v1.json"
KILL = ROOT / ".execution" / "kill_switch.json"
AUDIT = ROOT / ".execution" / "testnet_execution.sqlite3"
ATTENTION = ROOT / ".execution" / "ATTENTION"
LOG = ROOT / "carry_testnet_log.csv"
INCIDENTS = ROOT / "carry_paper_incidents.md"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


LOG_COLUMNS = ("utc", "target_id", "status", "reconcile_exit", "plan_legs", "plan_skips", "detail")


def _log(row: dict) -> None:
    new = not LOG.exists()
    with LOG.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_COLUMNS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({c: row.get(c, "") for c in LOG_COLUMNS})


def _attention(reason: str, detail: dict) -> None:
    ATTENTION.parent.mkdir(parents=True, exist_ok=True)
    ATTENTION.write_text(json.dumps({"utc": _now(), "reason": reason, **detail}, indent=2, default=str), encoding="utf-8")
    with INCIDENTS.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## {_now()} — {reason}\n\n```\n{json.dumps(detail, indent=2, default=str)}\n```\n\n"
                 f"_Xử lý theo EXECUTION_RUNBOOK.md, ghi quyết định vào đây, rồi xóa `.execution/ATTENTION`._\n")


def _refuse_unless_testnet_only() -> None:
    """Both locks. Fail loud, not silent, so a wrong deployment cannot look like 'ran fine'."""
    ceilings, _ = load_ceilings()
    if ceilings.get("live", 1.0) != 0.0:
        raise SystemExit(
            "REFUSING: execution_ceilings declares a non-zero LIVE ceiling. Unattended "
            "self-release is testnet-only by design and disables itself once live capital exists."
        )
    # A stale ATTENTION marker means a previous hand-off was never acknowledged. Do not
    # pile a new run on top of an un-reviewed partial book.
    if ATTENTION.exists():
        raise SystemExit(
            f"REFUSING: {ATTENTION} exists from a previous run. Read the runbook, resolve, "
            f"record in {INCIDENTS.name}, delete the marker, then runs resume."
        )


LOCK = ROOT / ".execution" / "testnet_daily.lock"


def _acquire_lock():
    """Exclusive create; a second concurrent fire must not double-execute the same book.

    O_EXCL is atomic on NTFS/POSIX. A stale lock (previous process killed) is left for a
    human: it is written with the pid + start time and shows up as an ATTENTION so nothing
    piles a new run onto an unknown state.
    """
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"pid": os.getpid(), "started_utc": _now()}))
    return LOCK


def main() -> int:
    try:
        return _main()
    except SystemExit:
        raise
    except BaseException as exc:   # noqa: BLE001 - last line of defence; must never be silent
        try:
            _attention("unexpected_crash", {"error": f"{type(exc).__name__}: {exc}"})
        except BaseException:
            pass
        raise


def _main() -> int:
    _refuse_unless_testnet_only()
    lock = _acquire_lock()
    if lock is None:
        _attention("concurrent_or_stale_lock", {
            "lock": str(LOCK),
            "note": "another run is in progress, or a previous run died without releasing. "
                    "Check Task Scheduler / process list; if none is running, inspect the exchange, "
                    "then delete the lock AND the ATTENTION marker.",
        })
        return 7
    try:
        return _run()
    finally:
        try:
            LOCK.unlink()
        except FileNotFoundError:
            pass


def _run() -> int:
    started = _now()

    # 1. Fresh targets from today's paper state (deterministic; safe to re-run).
    exp = subprocess.run([PYTHON, "-B", str(ROOT / "export_carry_targets.py")], capture_output=True, text=True, cwd=ROOT, timeout=900)
    if exp.returncode != 0:
        _attention("export_targets_failed", {"stdout": exp.stdout[-2000:], "stderr": exp.stderr[-2000:]})
        return 4
    book = load_target_book(TARGETS)

    client = FuturesREST.from_env("testnet", required=True)   # refuses anything but testnet creds
    policy = ExecutionPolicy(
        max_gross_notional_usd=FROZEN_TESTNET_GROSS_CEILING_USD,
        expected_config_sha256=sha256_file(PAPER_CONFIG),
    )
    kill = KillSwitch(KILL)
    audit = ExecutionAudit(AUDIT)
    executor = TestnetExecutor(client, policy, kill, audit)   # refuses non-testnet client/policy

    # 2. Plan first. Any refusal here is free (nothing placed) and worth seeing on its own.
    try:
        executor.assert_target_fresh(book)
        executor.assert_target_identity(book)
        plan = executor.execute(book, dry_run=True)
    except Exception as exc:
        if "stale target" in str(exc):
            # Ran too long after the daily close (machine woke late, or a manual run at
            # noon). Nothing was placed and nothing needs a human: log it and let
            # tomorrow's run proceed. Missed days are surfaced by status.py instead.
            _log({"utc": started, "target_id": book.target_id, "status": "MISSED_WINDOW", "detail": str(exc)[:200]})
            print(f"missed window (no orders, no marker): {exc}")
            return 5
        _attention("plan_refused", {"target_id": book.target_id, "error": f"{type(exc).__name__}: {exc}"})
        _log({"utc": started, "target_id": book.target_id, "status": "PLAN_REFUSED", "detail": str(exc)[:200]})
        return 5

    # 3. Release for exactly this target and budget, execute, and re-engage no matter what.
    status, detail = "UNKNOWN", ""
    kill.release(f"unattended testnet rehearsal {started}", target_id=book.target_id,
                 authorized_budget_usd=FROZEN_TESTNET_GROSS_CEILING_USD)
    try:
        result = executor.execute(book, dry_run=False)
        status, detail = str(result.get("status", "UNKNOWN")), f"{result.get('legs')} legs"
    except Exception as exc:
        # The engine already classified this and wrote its terminal status; we only report.
        row = audit.connection.execute(
            "SELECT status, message FROM execution_runs WHERE target_id=? ORDER BY started_utc DESC LIMIT 1",
            (book.target_id,),
        ).fetchone()
        status, detail = (row[0], row[1]) if row else ("EXCEPTION", f"{type(exc).__name__}: {exc}")
    finally:
        try:
            kill.engage(f"unattended run finished {status} at {_now()}")
        except Exception as exc:   # engine's own _safe_engage already tried; this is belt-and-braces
            _attention("kill_switch_reengage_failed", {"error": str(exc), "status": status})

    # 4. Reconcile. Exit 0 = matches contract; 2 = mismatch; 3 = hand-off state exists.
    rec = subprocess.run([PYTHON, "-B", str(ROOT / "reconcile_paper_vs_testnet.py"),
                          "--targets", str(TARGETS), "--audit", str(AUDIT)],
                         capture_output=True, text=True, cwd=ROOT, timeout=300)
    _log({"utc": started, "target_id": book.target_id, "status": status,
          "reconcile_exit": rec.returncode, "detail": detail,
          "plan_legs": len(plan.get("legs", [])), "plan_skips": len(plan.get("skips", []))})

    if status == "COMPLETE" and rec.returncode == 0:
        return 0
    _attention("run_needs_review", {
        "target_id": book.target_id, "engine_status": status, "engine_detail": detail,
        "reconcile_exit": rec.returncode, "reconcile_tail": rec.stdout[-3000:],
    })
    return 6


if __name__ == "__main__":
    raise SystemExit(main())
