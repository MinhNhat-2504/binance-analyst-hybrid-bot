"""Full-order fill quality must not silently reuse a partial CSV."""
from __future__ import annotations

from datetime import date

import pytest

from analyze_execution_quality import build_rows


def test_split_orders_are_distinct_and_signed_shortfall_is_correct() -> None:
    legs = [
        ("run1", "2026-10-20", "101", "AAAUSDT", "BUY", "rebalance", "1", 101.0, 3.0),
        ("run1", "2026-10-20", "102", "AAAUSDT", "BUY", "rebalance", "1", 102.0, 4.0),
        ("run1", "2026-10-20", "103", "BBBUSDT", "SELL", "rebalance", "1", 99.0, 5.0),
    ]

    def lookup(symbol: str, first: date, last: date) -> dict[date, float]:
        assert first == last == date(2026, 10, 20)
        return {first: 100.0}

    rows = build_rows(legs, lookup)
    assert [row["order_id"] for row in rows] == ["101", "102", "103"]
    assert [row["shortfall_bps"] for row in rows] == pytest.approx([100, 200, 100])


def test_missing_open_aborts_before_replacing_report() -> None:
    legs = [("run1", "2026-10-20", "101", "AAAUSDT", "BUY", "rebalance", "1", 101.0, 3.0)]
    with pytest.raises(RuntimeError, match="missing daily open"):
        build_rows(legs, lambda *_: {})
