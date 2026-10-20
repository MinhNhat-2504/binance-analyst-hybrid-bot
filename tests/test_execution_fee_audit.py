"""The fee audit must query Binance with valid, non-overlapping history windows."""
from datetime import date

from audit_execution_fees import _ms, windows


def test_sparse_days_are_grouped_without_exceeding_seven_day_api_limit() -> None:
    days = [date(2026, 8, 25), date(2026, 8, 27), date(2026, 9, 26),
            date(2026, 10, 5), date(2026, 10, 10)]
    groups = windows(days)
    assert groups == [(date(2026, 8, 25), date(2026, 8, 27)),
                      (date(2026, 9, 26), date(2026, 9, 26)),
                      (date(2026, 10, 5), date(2026, 10, 10))]
    assert all(0 <= _ms(last, end=True) - _ms(first, end=False) < 7 * 86_400_000
               for first, last in groups)
