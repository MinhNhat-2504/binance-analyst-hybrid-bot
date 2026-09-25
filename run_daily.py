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
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
STATUS_FILE = ROOT / "status.txt"
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
        ("status", ["run_daily.py", "status"], "status_task.log"),
        ("notify", ["notify_markers.py"], "notify_markers_task.log"),
    ],
    "canary": [
        ("canary", ["-B", "run_canaries.py"], "canary_task.log"),
        ("notify", ["notify_markers.py"], "notify_markers_task.log"),
    ],
}


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


def run_stage(stage: str, *, root: Path = ROOT) -> int:
    if stage == "status":
        return write_status(root=root)
    first_error = 0
    for label, args, log_name in STAGES[stage]:
        if label == "status":
            rc = write_status(root=root)
        else:
            rc = run_step(label, args, log_name, cwd=root)
        if label == "canary":
            canary_marker(rc, root=root)
        if rc and not first_error:
            first_error = rc
    return first_error


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("stage", choices=[*STAGES, "status"])
    args = ap.parse_args(argv)
    rc = run_stage(args.stage)
    print(f"{_stamp()} {args.stage}: exit {rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
