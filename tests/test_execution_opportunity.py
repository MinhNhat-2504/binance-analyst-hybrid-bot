"""Maker fee ceiling is arithmetic, not an assumed passive fill."""
from __future__ import annotations

import pytest

from audit_execution_opportunity import summarize


def test_fee_ceiling_keeps_observed_shortfall_and_checks_coverage() -> None:
    rows = [{"fill_date": "2026-10-20", "run_id": "r1", "order_id": "1",
             "symbol": "AAAUSDT", "side": "BUY", "reason": "rebalance", "qty": "1",
             "avg_fill": "100", "shortfall_bps": "8", "engine_slippage_bps": "5"}]
    fees = {"complete": True, "matched_order_count": 1, "by_run": {"r1": {}},
            "totals": {"commission_bps": 4.0},
            "price_quality": {"shortfall_bps_vs_paper_open": 8.0}}
    rates = {"AAAUSDT": {"makerCommissionRate": "0.0002"}}
    result = summarize(rows, fees, rates)
    assert result["maximum_fee_only_saving_bps_same_prices"] == pytest.approx(2.0)
    assert result["hypothetical_all_maker_all_in_bps_same_prices"] == pytest.approx(10.0)
    assert "fill probability" in result["maker_caveat"]
    with pytest.raises(ValueError, match="different order counts"):
        summarize(rows, {**fees, "matched_order_count": 2}, rates)
