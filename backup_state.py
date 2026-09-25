"""Copy everything that cannot be regenerated into backups/<UTC date>/, keep 30 days.

What is at risk on one laptop: the paper ledger and state (53 days of record that the
gate is judged on), the execution audit databases (the only record of every real order),
the equity high-water marks, kill-switch files, canary and tracking logs, and the
snapshots the Q4 cells need (Binance serves only 30 days of those). Code is on GitHub;
none of this is.

SQLite files are copied through the sqlite3 backup API so a copy taken while the loop is
writing is consistent, not a torn file. Everything else is a plain copy. Runs after the
paper stage every morning (run_daily.py); safe to run by hand any time.

    python backup_state.py              # -> backups/2026-09-26/
    python backup_state.py --keep 60
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKUPS = ROOT / "backups"

FILES = (
    "carry_paper_ledger.csv", "carry_paper_state.json", "carry_paper_config_v1.json",
    "execution_ceilings_v1.json", "carry_paper_incidents.md", "canary_log.csv",
    "tracking_log.csv", "execution_quality.csv", "carry_testnet_log.csv", "carry_live_log.csv",
    "execution/carry_targets_latest.json",
)
EXECUTION_GLOBS = ("*.json",)          # kill switches, equity high-water marks
SQLITE_GLOB = "*.sqlite3"
DIRS = ("data_snapshots",)


def _sqlite_copy(src: Path, dst: Path) -> None:
    with sqlite3.connect(src) as source, sqlite3.connect(dst) as target:
        source.backup(target)


def run(root: Path = ROOT, backups: Path = BACKUPS, *, keep: int = 30,
        today: str | None = None) -> tuple[Path, list[str]]:
    day = today or datetime.now(timezone.utc).date().isoformat()
    out = backups / day
    out.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for rel in FILES:
        src = root / rel
        if src.exists():
            dst = out / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(rel)
    ex = root / ".execution"
    if ex.is_dir():
        (out / ".execution").mkdir(exist_ok=True)
        for pattern in EXECUTION_GLOBS:
            for src in ex.glob(pattern):
                shutil.copy2(src, out / ".execution" / src.name)
                copied.append(f".execution/{src.name}")
        for src in ex.glob(SQLITE_GLOB):
            _sqlite_copy(src, out / ".execution" / src.name)
            copied.append(f".execution/{src.name}")
    for d in DIRS:
        src = root / d
        if src.is_dir():
            shutil.copytree(src, out / d, dirs_exist_ok=True)
            copied.append(f"{d}/")
    # retention: keep the newest `keep` day folders
    days = sorted(p for p in backups.iterdir() if p.is_dir() and len(p.name) == 10)
    for old in days[:-keep] if keep > 0 else []:
        shutil.rmtree(old, ignore_errors=True)
    return out, copied


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keep", type=int, default=30)
    args = ap.parse_args(argv)
    out, copied = run(keep=args.keep)
    print(f"backup -> {out.relative_to(ROOT)}  ({len(copied)} items)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
