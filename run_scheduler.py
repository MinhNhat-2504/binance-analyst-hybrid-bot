"""Always-on scheduler: the Windows Task Scheduler, for places that have none.

Runs the daily chain at fixed UTC times and nothing else. Meant for a container or a
small Linux box where cron is one more thing to get wrong; on Windows the three tasks
from INSTALL_TASKS.bat keep doing this job and this file is not used.

    00:05 UTC daily   run_daily.py paper
    00:20 UTC daily   run_daily.py testnet
    01:00 UTC Sunday  run_daily.py canary

Run-if-missed: on start-up, any stage whose time already passed today and has not run
today is launched immediately. The testnet runner itself refuses a target older than
6 hours (MISSED_WINDOW), so a late start after 06:20 UTC costs the day, exactly as on
Windows - the scheduler does not try to be cleverer than the runner.

Every launch is recorded in scheduler_state.json so a restart never double-runs a stage.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "scheduler_state.json"
PY = sys.executable

SCHEDULE: tuple[tuple[str, dtime, int | None], ...] = (
    # (stage, UTC time, weekday or None for daily; Monday=0 ... Sunday=6)
    ("paper", dtime(0, 5), None),
    ("testnet", dtime(0, 20), None),
    ("canary", dtime(1, 0), 6),
)


def _load() -> dict[str, str]:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(state: dict[str, str]) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE)


def due(now: datetime, state: dict[str, str]) -> list[str]:
    """Stages whose slot has passed today and which have not run today."""
    today = now.date()
    out = []
    for stage, at, weekday in SCHEDULE:
        if weekday is not None and today.weekday() != weekday:
            continue
        slot = datetime.combine(today, at, tzinfo=timezone.utc)
        if now >= slot and state.get(stage) != today.isoformat():
            out.append(stage)
    return out


def next_slot(now: datetime) -> datetime:
    candidates = []
    for stage, at, weekday in SCHEDULE:
        for days_ahead in range(0, 8):
            d = now.date() + timedelta(days=days_ahead)
            if weekday is not None and d.weekday() != weekday:
                continue
            slot = datetime.combine(d, at, tzinfo=timezone.utc)
            if slot > now:
                candidates.append(slot)
                break
    return min(candidates)


def launch(stage: str, *, root: Path = ROOT) -> int:
    proc = subprocess.run([PY, "-X", "utf8", str(root / "run_daily.py"), stage], cwd=root)
    return proc.returncode


def tick(now: datetime, state: dict[str, str], *, run=launch) -> dict[str, str]:
    for stage in due(now, state):
        rc = run(stage)
        print(f"{now.isoformat(timespec='seconds')} ran {stage}: exit {rc}", flush=True)
        state[stage] = now.date().isoformat()
        _save(state)
    return state


def main() -> int:
    state = _load()
    print(f"scheduler up; next slot {next_slot(datetime.now(timezone.utc)).isoformat(timespec='minutes')}", flush=True)
    while True:
        now = datetime.now(timezone.utc)
        state = tick(now, state)
        wait = (next_slot(datetime.now(timezone.utc)) - datetime.now(timezone.utc)).total_seconds()
        time.sleep(max(15.0, min(wait + 1.0, 900.0)))


if __name__ == "__main__":
    raise SystemExit(main())
