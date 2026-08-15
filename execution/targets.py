"""Versioned target-file validation shared by the exporter and testnet executor."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TargetBook:
    strategy: str
    target_id: str
    config_sha256: str
    signal_time_utc: str
    intended_execution_utc: str
    weights: dict[str, float]
    reference_prices: dict[str, float]
    source: str

    @property
    def gross(self) -> float:
        return sum(abs(value) for value in self.weights.values())

    @property
    def net(self) -> float:
        return sum(self.weights.values())


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_target_book(path: str | Path, *, max_gross: float = 1.02, max_net: float = 0.02) -> TargetBook:
    source_path = Path(path)
    payload: dict[str, Any] = json.loads(source_path.read_text(encoding="utf-8"))
    if payload.get("version") != "CARRY_EXECUTION_TARGET_V1":
        raise ValueError("unsupported target file version")
    weights = {str(symbol).upper(): float(value) for symbol, value in payload.get("weights", {}).items()}
    prices = {str(symbol).upper(): float(value) for symbol, value in payload.get("reference_prices", {}).items()}
    if not weights or any(not symbol.endswith("USDT") for symbol in weights):
        raise ValueError("target book is empty or contains non-USDT futures symbols")
    if any(not (-1.0 <= weight <= 1.0) for weight in weights.values()):
        raise ValueError("target weights outside [-1, 1]")
    book = TargetBook(
        strategy=str(payload.get("strategy", "")),
        target_id=str(payload.get("target_id", "")),
        config_sha256=str(payload.get("config_sha256", "")),
        signal_time_utc=str(payload.get("signal_time_utc", "")),
        intended_execution_utc=str(payload.get("intended_execution_utc", "")),
        weights=weights,
        reference_prices=prices,
        source=str(source_path),
    )
    if not book.strategy or not book.target_id or not re.fullmatch(r"[0-9a-fA-F]{64}", book.config_sha256):
        raise ValueError("target book lacks immutable strategy/config identity")
    if book.gross <= 0 or book.gross > max_gross or abs(book.net) > max_net:
        raise ValueError(f"invalid exposure: gross={book.gross:.6f}, net={book.net:.6f}")
    return book
