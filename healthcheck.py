"""Docker HEALTHCHECK: is the bot waiting for a human, or has it gone quiet?

Exit 0 = healthy. Exit 1 when any of:
  - .execution/ATTENTION exists (the loop is blocked until someone reads it)
  - .execution/testnet_daily.lock is older than 2 hours (a run died holding the lock)
  - the paper ledger has not booked a day for 3 days

Reads files only. Used by the Dockerfile so `docker ps` shows (unhealthy) instead of a
silent green container - the 20 quiet days of 2026-09 must not repeat somewhere nobody
looks at a Desktop.
"""
from __future__ import annotations

import csv
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def problems(root: Path = ROOT, *, now: float | None = None) -> list[str]:
    now = now or time.time()
    out = []
    if (root / ".execution" / "ATTENTION").exists():
        out.append("ATTENTION marker present")
    lock = root / ".execution" / "testnet_daily.lock"
    if lock.exists() and now - lock.stat().st_mtime > 2 * 3600:
        out.append("testnet_daily.lock older than 2h")
    ledger = root / "carry_paper_ledger.csv"
    if ledger.exists():
        rows = list(csv.DictReader(ledger.open(encoding="utf-8")))
        if rows:
            last = date.fromisoformat(rows[-1]["fill_day"])
            if (datetime.fromtimestamp(now, tz=timezone.utc).date() - last).days > 3:
                out.append(f"paper ledger last booked {last}")
    return out


def main() -> int:
    bad = problems()
    print("healthy" if not bad else "; ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
