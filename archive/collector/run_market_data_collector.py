"""Public USD-M Futures depth/liquidation collector with compressed hourly rotation.

It records top-of-book snapshots for a fixed universe and all-market liquidation events.
No API key is accepted or needed; this process has no trading capability.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import random
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "market_data_collection_v1.json"
STATE_PATH = ROOT / "market_data_collector_state.json"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RotatingGzipWriter:
    def __init__(self, root: Path, kind: str, rotate_minutes: int) -> None:
        self.root, self.kind, self.rotate_minutes = root, kind, int(rotate_minutes)
        self.bucket = ""
        self.handle: TextIO | None = None

    def _open(self, now: datetime) -> None:
        bucket = now.strftime("%Y%m%dT%H") + f"{(now.minute // self.rotate_minutes) * self.rotate_minutes:02d}Z"
        if bucket == self.bucket and self.handle:
            return
        if self.handle:
            self.handle.close()
        folder = self.root / now.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        self.handle = gzip.open(folder / f"{self.kind}-{bucket}.jsonl.gz", "at", encoding="utf-8")
        self.bucket = bucket

    def write(self, record: dict[str, Any]) -> None:
        self._open(utc_now())
        assert self.handle is not None
        self.handle.write(json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n")
        self.handle.flush()

    def close(self) -> None:
        if self.handle:
            self.handle.close()
            self.handle = None


def _state(payload: dict[str, Any]) -> None:
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, STATE_PATH)


def _prior_counters(config_sha256: str) -> dict[str, int]:
    if not STATE_PATH.exists():
        return {"depth_written": 0, "liquidations_written": 0, "reconnects": 0, "gaps": 0, "starts": 0}
    try:
        previous = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"depth_written": 0, "liquidations_written": 0, "reconnects": 0, "gaps": 0, "starts": 0}
    # Lifetime counters describe the data asset, not one config revision.  Resetting
    # them on a harmless universe/config edit made a crash-loop look healthy.
    return {key: int(previous.get(key, 0)) for key in ("depth_written", "liquidations_written", "reconnects", "gaps", "starts")}


def _config_changed_from(config_sha256: str) -> str | None:
    if not STATE_PATH.exists():
        return None
    try:
        previous = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    old = str(previous.get("config_sha256", ""))
    return old if old and old != config_sha256 else None


def _remaining(started: float, duration_seconds: float | None) -> float | None:
    return None if duration_seconds is None else max(0.0, duration_seconds - (time.monotonic() - started))


def _pause_for_storage_cap(
    *, root: Path, max_bytes: int, config: dict[str, Any], started: float,
    duration_seconds: float | None, state_payload: dict[str, Any],
) -> bool:
    """Pause without deleting data or killing a supervised long-running process."""

    if _disk_bytes(root) < max_bytes:
        return False
    remaining = _remaining(started, duration_seconds)
    _state({
        **state_payload, "status": "PAUSED_STORAGE_CAP", "updated_utc": utc_now().isoformat(),
        "last_error": f"storage cap reached: {config['max_disk_gb']} GB; no files were deleted",
    })
    if remaining == 0:
        return True
    time.sleep(60.0 if remaining is None else min(60.0, remaining))
    return True


def _disk_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _streams(symbols: list[str], levels: int, interval: str) -> list[str]:
    return [f"{symbol.lower()}@depth{levels}@{interval}" for symbol in symbols] + ["!forceOrder@arr"]


def _depth_record(data: dict[str, Any], received: datetime, stream: str) -> dict[str, Any]:
    return {
        "kind": "depth", "received_utc": received.isoformat(), "event_time_ms": data.get("E"),
        "transaction_time_ms": data.get("T"), "symbol": str(data.get("s", stream.split("@")[0])).upper(),
        "last_update_id": data.get("u", data.get("lastUpdateId")), "bids": data.get("b", data.get("bids", [])),
        "asks": data.get("a", data.get("asks", [])), "stream": stream,
    }


def _liquidation_records(data: Any, received: datetime) -> list[dict[str, Any]]:
    events = data if isinstance(data, list) else [data]
    rows = []
    for event in events:
        if not isinstance(event, dict):
            continue
        order = event.get("o", event)
        rows.append({
            "kind": "liquidation", "received_utc": received.isoformat(), "event_time_ms": event.get("E"),
            "transaction_time_ms": order.get("T"), "symbol": str(order.get("s", "")).upper(),
            "side": order.get("S"), "order_type": order.get("o"), "time_in_force": order.get("f"),
            "quantity": order.get("q"), "price": order.get("p"), "average_price": order.get("ap"),
            "status": order.get("X"), "raw": event,
        })
    return rows


def _rest_depth(url: str, symbol: str, levels: int, timeout: float = 10.0) -> dict[str, Any]:
    query = urllib.parse.urlencode({"symbol": symbol, "limit": levels})
    with urllib.request.urlopen(f"{url}?{query}", timeout=timeout) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict) or "bids" not in payload or "asks" not in payload:
        raise RuntimeError(f"invalid REST depth response for {symbol}: {payload}")
    return payload


def run_rest_depth(
    config: dict[str, Any], *, config_sha256: str, duration_seconds: float | None = None
) -> dict[str, Any]:
    """Credential-free degraded mode for networks that suppress Futures WS frames.

    This records depth only.  It cannot reconstruct the throttled forceOrder stream,
    and the state file makes that limitation explicit rather than pretending the two
    transports are equivalent.
    """

    symbols = [str(symbol).upper() for symbol in config["symbols"]]
    levels = int(config["depth_levels"])
    root = ROOT / str(config["storage"])
    max_bytes = int(float(config["max_disk_gb"]) * 1024**3)
    writer = RotatingGzipWriter(root, "depth", int(config["rotate_minutes"]))
    counters = _prior_counters(config_sha256)
    config_changed_from = _config_changed_from(config_sha256)
    initial_depth = counters["depth_written"]
    counters["starts"] += 1
    started = time.monotonic()
    last_message_utc: str | None = None
    last_error = ""
    endpoint = str(config.get("rest_depth_url", "https://fapi.binance.com/fapi/v1/depth"))
    interval = float(config.get("rest_poll_interval_seconds", 5.0))
    try:
        while duration_seconds is None or time.monotonic() - started < duration_seconds:
            state_base = {
                "transport": "rest_depth_only", "liquidations_available": False,
                "last_message_utc": last_message_utc, "config_sha256": config_sha256,
                "config_changed_from": config_changed_from, **counters,
            }
            if _pause_for_storage_cap(root=root, max_bytes=max_bytes, config=config, started=started, duration_seconds=duration_seconds, state_payload=state_base):
                continue
            for symbol in symbols:
                if duration_seconds is not None and time.monotonic() - started >= duration_seconds:
                    break
                try:
                    received = utc_now()
                    payload = _rest_depth(endpoint, symbol, levels)
                    payload = {**payload, "s": symbol}
                    writer.write(_depth_record(payload, received, f"rest:{symbol.lower()}@depth{levels}"))
                    counters["depth_written"] += 1
                    last_message_utc = received.isoformat()
                    last_error = ""
                except Exception as exc:
                    last_error = str(exc)
                    counters["gaps"] += 1
                    writer.write({
                        "kind": "gap", "transport": "rest_depth_only", "symbol": symbol,
                        "recorded_utc": utc_now().isoformat(), "last_message_utc": last_message_utc,
                        "error": last_error,
                    })
            _state({
                "status": "RUNNING", "transport": "rest_depth_only",
                "liquidations_available": False, "updated_utc": utc_now().isoformat(),
                "last_message_utc": last_message_utc, "last_error": last_error,
                "config_sha256": config_sha256, "config_changed_from": config_changed_from, **counters,
            })
            remaining = None if duration_seconds is None else max(0.0, duration_seconds - (time.monotonic() - started))
            if remaining == 0:
                break
            time.sleep(interval if remaining is None else min(interval, remaining))
    finally:
        writer.close()
        _state({
            "status": "STOPPED", "transport": "rest_depth_only",
            "liquidations_available": False, "updated_utc": utc_now().isoformat(),
            "last_message_utc": last_message_utc, "last_error": last_error,
            "config_sha256": config_sha256, "config_changed_from": config_changed_from, **counters,
        })
    return {**counters, "depth_written_this_run": counters["depth_written"] - initial_depth}


def run(config: dict[str, Any], *, config_sha256: str, duration_seconds: float | None = None) -> dict[str, Any]:
    try:
        import websocket  # websocket-client; imported only when the collector actually starts
    except ImportError as exc:
        raise RuntimeError("missing websocket-client; run: python -m pip install websocket-client>=1.8") from exc
    symbols = [str(symbol).upper() for symbol in config["symbols"]]
    levels = int(config["depth_levels"])
    streams = _streams(symbols, levels, str(config["depth_stream_interval"]))
    endpoint = str(config["websocket_base"]) + "/".join(streams)
    root = ROOT / str(config["storage"])
    max_bytes = int(float(config["max_disk_gb"]) * 1024**3)
    depth_writer = RotatingGzipWriter(root, "depth", int(config["rotate_minutes"]))
    liq_writer = RotatingGzipWriter(root, "liquidations", int(config["rotate_minutes"]))
    last_depth: dict[str, float] = {}
    started = time.monotonic()
    counters = _prior_counters(config_sha256)
    config_changed_from = _config_changed_from(config_sha256)
    initial_depth = counters["depth_written"]
    counters["starts"] += 1
    last_error = ""
    # Null means exactly what it says: this process has not received a market byte yet.
    last_message_utc: str | None = None
    consecutive_failures = 0
    last_reconnect_utc: str | None = None
    ws = None
    try:
        while duration_seconds is None or time.monotonic() - started < duration_seconds:
            state_base = {
                "transport": "websocket", "last_message_utc": last_message_utc,
                "last_reconnect_utc": last_reconnect_utc, "consecutive_failures": consecutive_failures,
                "config_sha256": config_sha256, "config_changed_from": config_changed_from, **counters,
            }
            if _pause_for_storage_cap(root=root, max_bytes=max_bytes, config=config, started=started, duration_seconds=duration_seconds, state_payload=state_base):
                continue
            try:
                ws = websocket.create_connection(endpoint, timeout=30)
                ws.settimeout(20)
                counters["reconnects"] += 1
                last_reconnect_utc = utc_now().isoformat()
                last_error = ""
                _state({"status": "RUNNING", "transport": "websocket", "updated_utc": utc_now().isoformat(), "last_message_utc": last_message_utc, "last_reconnect_utc": last_reconnect_utc, "consecutive_failures": consecutive_failures, "config_sha256": config_sha256, "config_changed_from": config_changed_from, **counters})
                while duration_seconds is None or time.monotonic() - started < duration_seconds:
                    raw = ws.recv()
                    received = utc_now()
                    last_message_utc = received.isoformat()
                    consecutive_failures = 0
                    packet = json.loads(raw)
                    stream, data = packet.get("stream", ""), packet.get("data", {})
                    if stream == "!forceOrder@arr":
                        for row in _liquidation_records(data, received):
                            liq_writer.write(row)
                            counters["liquidations_written"] += 1
                    elif "@depth" in stream:
                        symbol = str(data.get("s", stream.split("@")[0])).upper()
                        now = time.monotonic()
                        if now - last_depth.get(symbol, -float("inf")) >= float(config["snapshot_min_interval_seconds"]):
                            depth_writer.write(_depth_record(data, received, stream))
                            last_depth[symbol] = now
                            counters["depth_written"] += 1
                    if (counters["depth_written"] + counters["liquidations_written"]) % 100 == 0:
                        _state({"status": "RUNNING", "transport": "websocket", "updated_utc": received.isoformat(), "last_message_utc": last_message_utc, "last_reconnect_utc": last_reconnect_utc, "consecutive_failures": consecutive_failures, "config_sha256": config_sha256, "config_changed_from": config_changed_from, **counters})
            except Exception as exc:
                last_error = str(exc)
                consecutive_failures += 1
                gap = {"kind": "gap", "recorded_utc": utc_now().isoformat(), "last_message_utc": last_message_utc, "error": last_error, "reconnect_attempt": counters["reconnects"] + 1}
                depth_writer.write(gap)
                liq_writer.write(gap)
                counters["gaps"] += 1
                _state({"status": "RECONNECTING", "transport": "websocket", "updated_utc": utc_now().isoformat(), "last_error": last_error, "last_message_utc": last_message_utc, "last_reconnect_utc": last_reconnect_utc, "consecutive_failures": consecutive_failures, "config_sha256": config_sha256, "config_changed_from": config_changed_from, **counters})
                remaining = None if duration_seconds is None else max(0.0, duration_seconds - (time.monotonic() - started))
                delay = min(60.0, 2.0 ** min(consecutive_failures, 5)) * random.uniform(0.75, 1.25)
                time.sleep(delay if remaining is None else min(delay, remaining))
            finally:
                try:
                    if ws is not None:
                        ws.close()
                except Exception:
                    pass
    finally:
        depth_writer.close()
        liq_writer.close()
        payload = {"status": "STOPPED", "transport": "websocket", "updated_utc": utc_now().isoformat(), "last_message_utc": last_message_utc, "last_reconnect_utc": last_reconnect_utc, "consecutive_failures": consecutive_failures, "config_sha256": config_sha256, "config_changed_from": config_changed_from, **counters}
        if last_error:
            payload["last_error"] = last_error
        _state(payload)
    return {**counters, "depth_written_this_run": counters["depth_written"] - initial_depth}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--duration-seconds", type=float, help="omit to run continuously")
    parser.add_argument("--transport", choices=["websocket", "rest"], default="websocket")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    path = Path(args.config)
    config = json.loads(path.read_text(encoding="utf-8"))
    streams = _streams(config["symbols"], int(config["depth_levels"]), str(config["depth_stream_interval"]))
    if args.dry_run:
        endpoint = config["websocket_base"] + "/".join(streams) if args.transport == "websocket" else config.get("rest_depth_url", "https://fapi.binance.com/fapi/v1/depth")
        print(json.dumps({"transport": args.transport, "symbols": len(config["symbols"]), "streams": len(streams) if args.transport == "websocket" else len(config["symbols"]), "endpoint": endpoint, "storage": config["storage"]}, indent=2))
        return 0
    result = (run_rest_depth if args.transport == "rest" else run)(
        config, config_sha256=_sha(path), duration_seconds=args.duration_seconds
    )
    if result["depth_written_this_run"] <= 0:
        print("collector stopped without a real depth record", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
