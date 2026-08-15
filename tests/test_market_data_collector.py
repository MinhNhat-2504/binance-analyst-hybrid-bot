from __future__ import annotations

import json

import run_market_data_collector as collector


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
    collector.run(config, config_sha256="a" * 64, duration_seconds=0.001)
    first = json.loads(state.read_text(encoding="utf-8"))
    assert first["last_message_utc"] is None
    assert first["starts"] == 1 and first["gaps"] >= 1
    first_gaps = first["gaps"]
    collector.run(config, config_sha256="a" * 64, duration_seconds=0.001)
    second = json.loads(state.read_text(encoding="utf-8"))
    assert second["last_message_utc"] is None
    assert second["starts"] == 2 and second["gaps"] > first_gaps
