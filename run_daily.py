"""One entry point for every scheduled job, on any operating system.

Before this file the daily chain lived in three Windows .bat files with hardcoded paths
to one laptop's Python. Moving the bot anywhere else meant rewriting them. Now the .bat
files, a cron line, or the in-container scheduler all call the same thing:

    python run_daily.py paper      # 00:05 UTC  book paper, collect snapshots, notify
    python run_daily.py testnet    # 00:20 UTC  unattended testnet loop, then the (inert) live loop, status, notify
    python run_daily.py canary     # Sun 01:00 UTC  signal-health canaries
    python run_daily.py status     # write status.txt (and a Desktop copy when there is one)

Each step runs as a subprocess with the same interpreter, appends to its own log next to
the repo, and never stops the chain: the testnet runner returning 5 (missed window) must
not prevent the status file from being written. Exit code is the first non-zero step's,
so a scheduler can still see that something went wrong.

State (ledgers, .execution/, caches, logs) stays in the repo directory. In Docker that
directory is a bind mount, so the container is stateless and replaceable.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
STATUS_FILE = ROOT / "status.txt"
SUMMARY_LOG = "run_daily_task.log"
PAPER_RUNNING = ".execution/paper_stage.running"
PAPER_WAIT_SECONDS = 900          # testnet waits this long for an overrunning paper stage
PAPER_MARKER_STALE_SECONDS = 3600  # a marker older than this is a dead process, not a running one
DESKTOP_STATUS_NAME = "BINANCE BOT - TINH TRANG.txt"

STAGES: dict[str, list[tuple[str, list[str], str]]] = {
    # (label, command args after python, log file)
    "paper": [
        ("paper", ["run_carry_paper.py"], "carry_paper_task.log"),
        ("snapshots", ["collect_daily_snapshots.py"], "collect_snapshots_task.log"),
        ("backup", ["backup_state.py"], "backup_task.log"),
        ("notify", ["notify_markers.py"], "notify_markers_task.log"),
    ],
    # The 00:20 UTC slot runs BOTH loops. Exactly one is ever armed: the testnet loop
    # refuses once the ceilings file declares live > 0, and the live loop exits 2 (nothing
    # written) until ceilings v2 AND unattended_live_v1.json AND a matching ceilings sha
    # all exist. So the day the operator arms live, the schedule needs no change.
    "testnet": [
        ("testnet", ["-B", "run_carry_testnet_daily.py"], "carry_testnet_task.log"),
        ("live", ["-B", "run_carry_live_daily.py"], "carry_live_task.log"),
        # Evidence the gate reads. Both are read-only and idempotent; without them the gate
        # judged tracking and fill quality on three days from August for six weeks.
        ("fills", ["analyze_execution_quality.py"], "evidence_task.log"),
        ("tracking", ["track_paper_vs_testnet.py"], "evidence_task.log"),
        ("status", ["run_daily.py", "status"], "status_task.log"),
        ("notify", ["notify_markers.py"], "notify_markers_task.log"),
    ],
    "canary": [
        ("canary", ["-B", "run_canaries.py"], "canary_task.log"),
        ("notify", ["notify_markers.py"], "notify_markers_task.log"),
    ],
}


# Exit codes that are a normal day, not a failure. The live loop exits 2 until it is armed
# (ceilings v2 + hand-written authorization); reporting that as the stage's exit code made
# every healthy morning look like "testnet: exit 2" in the scheduler log.
EXPECTED_EXITS = {("live", 2)}


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_step(label: str, args: list[str], log_name: str, *, cwd: Path = ROOT) -> int:
    log = cwd / log_name
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n=== {_stamp()} {label} ===\n")
        fh.flush()
        proc = subprocess.run([PY, "-X", "utf8", *args], cwd=cwd, stdout=fh, stderr=subprocess.STDOUT)
    return proc.returncode


def canary_marker(rc: int, *, root: Path = ROOT) -> None:
    """Marker mirrors the latest week only; canary_log.csv is the record."""
    marker = root / ".execution" / "canary_ALERT"
    marker.parent.mkdir(exist_ok=True)
    if rc != 0:
        marker.write_text("see canary_log.csv\n", encoding="utf-8")
    elif marker.exists():
        marker.unlink()


def write_status(*, root: Path = ROOT) -> int:
    proc = subprocess.run([PY, "-X", "utf8", str(root / "status.py")], cwd=root, capture_output=True, text=True)
    text = proc.stdout + (proc.stderr if proc.returncode else "")
    (root / "status.txt").write_text(text, encoding="utf-8")
    for desk in (Path.home() / "Desktop", Path.home() / "OneDrive" / "Desktop"):
        if desk.is_dir():
            try:
                shutil.copyfile(root / "status.txt", desk / DESKTOP_STATUS_NAME)
            except OSError:
                pass
            break
    return proc.returncode


def keep_awake() -> None:
    """Windows: hold a SYSTEM_REQUIRED power request for the life of this process.

    2026-10-11: the wake timer woke the laptop at 07:19:47 for the 07:20 run and the system
    idled back to sleep at 07:24:44, mid-run ("Sleep Reason: System Idle"). A timer wake has
    no user present, so Windows applies a short unattended idle timeout unless something
    says work is in progress. This says so. It cannot stop an explicit sleep (lid, Start
    menu); nothing can.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except Exception:  # noqa: BLE001 - a courtesy to the OS, never a reason to skip the day
        pass


def _paper_marker(root: Path) -> Path:
    return root / PAPER_RUNNING


def wait_for_paper(root: Path = ROOT, *, timeout: float = PAPER_WAIT_SECONDS, sleep=time.sleep,
                   now=time.time) -> float:
    """Block while the paper stage is still running; return the seconds waited.

    The testnet loop exports targets from the paper state. If the paper stage overran its
    15 minutes (slow network, or the machine slept in the middle of it), starting on a
    half-written state would be refused at best. A marker older than an hour belongs to a
    process that died and is ignored.
    """
    marker = _paper_marker(root)
    waited = 0.0
    while marker.exists() and waited < timeout:
        try:
            if now() - marker.stat().st_mtime > PAPER_MARKER_STALE_SECONDS:
                break
        except OSError:
            break
        sleep(5.0)
        waited += 5.0
    return waited


def run_stage(stage: str, *, root: Path = ROOT) -> int:
    if stage == "status":
        return write_status(root=root)
    keep_awake()
    marker = _paper_marker(root)
    if stage == "paper":
        marker.parent.mkdir(exist_ok=True)
        marker.write_text(json.dumps({"pid": os.getpid(), "started_utc": _stamp()}), encoding="utf-8")
    elif stage == "testnet":
        wait_for_paper(root)
    try:
        return _run_steps(stage, root)
    finally:
        if stage == "paper":
            try:
                marker.unlink()
            except OSError:
                pass


def _run_steps(stage: str, root: Path) -> int:
    first_error = 0
    for label, args, log_name in STAGES[stage]:
        if label == "status":
            rc = write_status(root=root)
        else:
            rc = run_step(label, args, log_name, cwd=root)
        if label == "canary":
            canary_marker(rc, root=root)
        if (label, rc) in EXPECTED_EXITS:
            rc = 0
        if rc and not first_error:
            first_error = rc
    return first_error


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("stage", choices=[*STAGES, "status"])
    args = ap.parse_args(argv)
    rc = run_stage(args.stage)
    line = f"{_stamp()} {args.stage}: exit {rc}"
    print(line)
    if args.stage != "status":
        # Written from Python, not by the .bat redirect: cmd opens a redirect target without
        # write sharing, so two stages redirecting to one file meant the second never started
        # ("being used by another process"). That silently cost the 2026-10-11 testnet run.
        try:
            with (ROOT / SUMMARY_LOG).open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
