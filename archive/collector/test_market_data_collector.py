from __future__ import annotations

import json
import gzip

import run_market_data_collector as collector
import run_market_data_collector_supervisor as supervisor


def test_collector_never_invents_last_message_and_persists_counters(tmp_path, monkeypatch) -> None:
    import websocket

    state = tmp_path / "state.json"
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    monkeypatch.setattr(collector, "STATE_PATH", state)
    monkeypatch.setattr(websocket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionRefusedError("blocked")))
    config = {
        "symbols": ["BTCUSDT"], "depth_levels": 10, "depth_stream_interval": "1000ms",
        "websocket_base": "wss://example.invalid/stream?streams=", "storage": "data",
        "max_disk_gb": 1, "rotate_minutes": 60, "snapshot_min_interval_seconds": 5,
    }
    collector.run(config, config_sha256="a" * 64, duration_seconds=0.02)
    first = json.loads(state.read_text(encoding="utf-8"))
    assert first["last_message_utc"] is None
    assert first["starts"] == 1 and first["gaps"] >= 1
    first_gaps = first["gaps"]
    collector.run(config, config_sha256="a" * 64, duration_seconds=0.02)
    second = json.loads(state.read_text(encoding="utf-8"))
    assert second["last_message_utc"] is None
    assert second["starts"] == 2 and second["gaps"] > first_gaps
    collector.run(config, config_sha256="b" * 64, duration_seconds=0.02)
    third = json.loads(state.read_text(encoding="utf-8"))
    assert third["starts"] == 3 and third["gaps"] > second["gaps"]
    assert third["config_changed_from"] == "a" * 64


def test_rest_degraded_mode_writes_real_depth_and_declares_no_liquidations(tmp_path, monkeypatch) -> None:
    state = tmp_path / "state.json"
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    monkeypatch.setattr(collector, "STATE_PATH", state)
    monkeypatch.setattr(
        collector,
        "_rest_depth",
        lambda url, symbol, levels: {"lastUpdateId": 123, "bids": [["100", "1"]], "asks": [["101", "2"]]},
    )
    config = {
        "symbols": ["BTCUSDT"], "depth_levels": 10, "storage": "data",
        "max_disk_gb": 1, "rotate_minutes": 60, "rest_poll_interval_seconds": 0,
    }
    result = collector.run_rest_depth(config, config_sha256="b" * 64, duration_seconds=0.01)
    assert result["depth_written_this_run"] > 0
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["last_message_utc"] is not None
    assert saved["transport"] == "rest_depth_only"
    assert saved["liquidations_available"] is False
    records = []
    for path in (tmp_path / "data").rglob("depth-*.jsonl.gz"):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            records.extend(json.loads(line) for line in handle)
    assert any(row["kind"] == "depth" and row["symbol"] == "BTCUSDT" for row in records)


def test_supervisor_child_command_is_explicit_and_shell_free(tmp_path) -> None:
    command = supervisor.child_command(tmp_path / "config.json", "rest")
    assert command[1:3] == ["-B", str(supervisor.COLLECTOR)]
    assert command[-2:] == ["--transport", "rest"]
