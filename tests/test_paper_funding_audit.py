"""The audit must distinguish actual settlement timing from the paper approximation."""
from __future__ import annotations

import pandas as pd
import pytest

from audit_paper_funding import audit, load_cached_funding, settlement_split


def _funding(day: str, rates: list[float], hours: list[int]) -> pd.DataFrame:
    start = pd.Timestamp(day)
    return pd.DataFrame({"fundingTime": [start + pd.Timedelta(hours=h, milliseconds=2)
                                         for h in hours], "fundingRate": rates})


def test_four_hour_settlements_are_assigned_to_the_actual_weight() -> None:
    day = "2026-08-04"
    funding = {
        "A": _funding(day, [0.006, 0, 0, 0, 0, 0], [0, 4, 8, 12, 16, 20]),
        "B": _funding(day, [0, 0, 0], [0, 8, 16]),
    }
    rows = [{"fill_day": day, "longs": "A", "shorts": "B", "pnl": "0.000000"}]
    result = audit(rows, funding)
    # Paper gives 2/3 of A's funding to the new long; at midnight it was flat.
    assert result["status"] == "complete"
    assert result["funding_correction_bps_sum"] == pytest.approx(20.0)
    assert result["corrected_return_pct"] == pytest.approx(0.2)


def test_midnight_uses_previous_weight_even_with_millisecond_timestamp() -> None:
    day = "2026-08-04"
    frame = _funding(day, [0.003, 0.001, 0.001], [0, 8, 16])
    assert settlement_split(frame, pd.Timestamp(day)) == pytest.approx((0.003, 0.002))


def test_missing_settlement_day_is_reported_without_invented_zero_funding() -> None:
    rows = [{"fill_day": "2026-08-04", "longs": "A", "shorts": "B", "pnl": "0.01"}]
    result = audit(rows, {"A": _funding("2026-08-04", [0.01], [0])})
    assert result["status"] == "partial"
    assert result["audited_days"] == 0
    assert result["corrected_return_pct"] is None
    assert result["missing_days"][0]["symbols"] == ["A", "B"]


def test_fixed_paper_funding_cache_is_loaded_and_conflicts_fail(tmp_path) -> None:
    frame = _funding("2026-08-04", [0.001, 0.002, 0.003], [0, 8, 16])
    frame.to_parquet(tmp_path / "PAPER_FUNDING_A_2026-08-04_2026-08-04.parquet", index=False)
    assert len(load_cached_funding("A", tmp_path)) == 3
    copy = frame.copy()
    copy.loc[0, "fundingRate"] = 0.005
    copy.to_parquet(tmp_path / "A_funding_90d.parquet", index=False)
    with pytest.raises(ValueError, match="conflicting"):
        load_cached_funding("A", tmp_path)
