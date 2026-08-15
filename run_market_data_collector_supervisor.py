"""Restart the credential-free market-data collector after unexpected exits."""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
COLLECTOR = ROOT / "run_market_data_collector.py"
STATE = ROOT / "market_data_collector_supervisor_state.json"
LOG = ROOT / "market_data_collector_supervisor.log"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state(payload: dict) -> None:
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, STATE)


def child_command(config: Path, transport: str) -> list[str]:
    return [
        sys.executable, "-B", str(COLLECTOR), "--config", str(config),
        "--transport", transport,
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(ROOT / "market_data_collection_v1.json"))
    parser.add_argument("--transport", choices=["websocket", "rest"], default="rest")
    parser.add_argument("--min-backoff-seconds", type=float, default=2.0)
    parser.add_argument("--max-backoff-seconds", type=float, default=60.0)
    parser.add_argument("--healthy-reset-seconds", type=float, default=300.0)
    args = parser.parse_args()
    config = Path(args.config).resolve()
    if not config.exists():
        raise FileNotFoundError(config)
    if args.min_backoff_seconds < 0 or args.max_backoff_seconds < args.min_backoff_seconds:
        raise ValueError("invalid supervisor backoff")

    failures = 0
    process: subprocess.Popen | None = None
    try:
        while True:
            launched = time.monotonic()
            with LOG.open("a", encoding="utf-8") as log:
                log.write(f"{_utc()} launch transport={args.transport}\n")
                log.flush()
                process = subprocess.Popen(
                    child_command(config, args.transport), cwd=ROOT,
                    stdout=log, stderr=subprocess.STDOUT,
                )
                _state({"status": "CHILD_RUNNING", "updated_utc": _utc(), "pid": process.pid, "transport": args.transport, "consecutive_short_exits": failures})
                return_code = process.wait()
                runtime = time.monotonic() - launched
                log.write(f"{_utc()} child_exit code={return_code} runtime_seconds={runtime:.3f}\n")
            failures = 0 if runtime >= args.healthy_reset_seconds else failures + 1
            delay = min(args.max_backoff_seconds, args.min_backoff_seconds * 2 ** min(failures - 1, 8))
            delay *= random.uniform(0.8, 1.2)
            _state({"status": "RESTART_BACKOFF", "updated_utc": _utc(), "last_exit_code": return_code, "last_runtime_seconds": runtime, "restart_in_seconds": delay, "transport": args.transport, "consecutive_short_exits": failures})
            time.sleep(delay)
    except KeyboardInterrupt:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        _state({"status": "STOPPED_BY_OPERATOR", "updated_utc": _utc(), "transport": args.transport})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
