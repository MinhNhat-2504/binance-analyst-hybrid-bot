"""honest/metrics.py: the loader for the Q4 positioning cells.

These run offline. Every test monkeypatches the archive fetch, because a test that needs
data.binance.vision to be up is a test that fails for reasons that have nothing to do with
the code.

The one that matters most is test_rest_and_archive_are_the_same_series_one_day_apart. On
2026-09-04 the naive date-to-date comparison said the archive disagreed with the live REST
endpoint by 1-2% typically and up to 10%, which trips the pre-registered data kill in
RESEARCH_PREREG_Q4_2026.md and would have closed two cells before they ran. The series are
identical; REST stamps a 1d row at 00:00 of day D and reports the state at that instant,
which is the archive's last row of D-1. Lined up correctly the difference is 0.00%.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from honest import metrics as m

HEADER = ("create_time,symbol,sum_open_interest,sum_open_interest_value,"
          "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
          "count_long_short_ratio,sum_taker_long_short_vol_ratio")


def _day_frame(symbol: str, day: date, *, rows: int = 288, oi_first: float = 100.0,
               oi_last: float = 200.0) -> pd.DataFrame:
    """A day of 5-minute rows whose open interest ramps from oi_first to oi_last."""
    stamps = [datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(minutes=5 * i)
              for i in range(rows)]
    step = (oi_last - oi_first) / max(1, rows - 1)
    return pd.DataFrame({
        "create_time": pd.to_datetime(stamps, utc=True),
        "symbol": symbol,
        "sum_open_interest": [oi_first + step * i for i in range(rows)],
        "sum_open_interest_value": [(oi_first + step * i) * 10 for i in range(rows)],
        "count_toptrader_long_short_ratio": [1.5] * rows,
        "sum_toptrader_long_short_ratio": [2.0] * rows,
        "count_long_short_ratio": [0.8] * rows,
        "sum_taker_long_short_vol_ratio": [1.1] * rows,
    })


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "CACHE_DIR", tmp_path)
    return tmp_path


def _serve(days: dict[date, pd.DataFrame], calls: list[tuple[str, date]] | None = None):
    def fake(symbol: str, day: date, retries: int = 3):
        if calls is not None:
            calls.append((symbol, day))
        return days.get(day)
    return fake


def test_daily_row_is_the_last_snapshot_not_a_hardcoded_time(cache, monkeypatch):
    """The archive's last row is 23:50, not 23:55. A hardcoded time drops every day."""
    day = date(2026, 9, 1)
    frame = _day_frame("BTCUSDT", day, oi_first=100.0, oi_last=200.0)
    monkeypatch.setattr(m, "fetch_metrics_day", _serve({day: frame}))
    monkeypatch.setattr(m, "datetime", _FrozenDatetime(date(2026, 9, 2)))

    out = m.fetch_metrics("BTCUSDT", days_back=1)
    assert len(out) == 1
    assert out["open_interest"].iloc[0] == pytest.approx(200.0)          # end of day
    assert out["open_interest_mean"].iloc[0] == pytest.approx(150.0)     # mean of the ramp
    assert out["n_rows"].iloc[0] == 288


def test_short_days_are_kept_but_flagged(cache, monkeypatch):
    """An outage must be visible to the cell, not silently dropped here."""
    day = date(2026, 9, 1)
    monkeypatch.setattr(m, "fetch_metrics_day", _serve({day: _day_frame("BTCUSDT", day, rows=12)}))
    monkeypatch.setattr(m, "datetime", _FrozenDatetime(date(2026, 9, 2)))
    out = m.fetch_metrics("BTCUSDT", days_back=1)
    assert out["n_rows"].iloc[0] == 12 and out["n_rows"].iloc[0] < m.ROWS_PER_FULL_DAY


def test_absent_days_are_remembered_and_never_refetched(cache, monkeypatch):
    """45,000 downloads is the budget; re-asking for pre-listing days every run wastes it."""
    present, missing = date(2026, 9, 1), date(2026, 8, 31)
    calls: list[tuple[str, date]] = []
    monkeypatch.setattr(m, "fetch_metrics_day", _serve({present: _day_frame("NEWUSDT", present)}, calls))
    monkeypatch.setattr(m, "datetime", _FrozenDatetime(date(2026, 9, 2)))

    m.fetch_metrics("NEWUSDT", days_back=2)
    assert sorted({d for _, d in calls}) == [missing, present]
    assert json.loads((cache / "METRICS_NEWUSDT.absent.json").read_text()) == [missing.isoformat()]

    calls.clear()
    m.fetch_metrics("NEWUSDT", days_back=2)
    assert calls == [], "a second run must download nothing: one day cached, one known absent"


def test_cache_tops_up_instead_of_refetching(cache, monkeypatch):
    d1, d2 = date(2026, 9, 1), date(2026, 9, 2)
    calls: list[tuple[str, date]] = []
    monkeypatch.setattr(m, "fetch_metrics_day",
                        _serve({d1: _day_frame("BTCUSDT", d1), d2: _day_frame("BTCUSDT", d2)}, calls))

    monkeypatch.setattr(m, "datetime", _FrozenDatetime(date(2026, 9, 2)))
    m.fetch_metrics("BTCUSDT", days_back=1)
    assert [d for _, d in calls] == [d1]

    calls.clear()
    monkeypatch.setattr(m, "datetime", _FrozenDatetime(date(2026, 9, 3)))
    out = m.fetch_metrics("BTCUSDT", days_back=2)
    assert [d for _, d in calls] == [d2], "only the new day is fetched"
    assert len(out) == 2


def test_today_is_never_included(cache, monkeypatch):
    """Today's file does not exist until the day closes; half a day would be lookahead."""
    calls: list[tuple[str, date]] = []
    monkeypatch.setattr(m, "fetch_metrics_day", _serve({}, calls))
    monkeypatch.setattr(m, "datetime", _FrozenDatetime(date(2026, 9, 4)))
    m.fetch_metrics("BTCUSDT", days_back=3)
    assert date(2026, 9, 4) not in {d for _, d in calls}
    assert max(d for _, d in calls) == date(2026, 9, 3)


def test_http_404_is_data_not_failure(monkeypatch):
    import urllib.error

    def raise_404(url, timeout=0):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(m.urllib.request, "urlopen", raise_404)
    assert m.fetch_metrics_day("BTCUSDT", date(2020, 1, 1)) is None


def test_network_failure_raises_rather_than_looking_like_a_missing_day(monkeypatch):
    import urllib.error

    def raise_500(url, timeout=0):
        raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(m.urllib.request, "urlopen", raise_500)
    monkeypatch.setattr(m.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError, match="archive fetch failed"):
        m.fetch_metrics_day("BTCUSDT", date(2026, 9, 1))


def test_zip_is_parsed_from_the_real_wire_format(monkeypatch):
    """Guard the actual bytes: a header rename upstream must fail here, not in a cell."""
    csv = HEADER + "\n2026-09-01 00:00:00,BTCUSDT,107896.98,8481814045.5,1.058,2.097,0.998,2.317\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("BTCUSDT-metrics-2026-09-01.csv", csv)
    payload = buffer.getvalue()

    class Response:
        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(m.urllib.request, "urlopen", lambda url, timeout=0: Response())
    frame = m.fetch_metrics_day("BTCUSDT", date(2026, 9, 1))
    assert list(frame.columns)[:3] == ["create_time", "symbol", "sum_open_interest"]
    row = m._daily_row(frame, date(2026, 9, 1))
    assert row["open_interest"] == pytest.approx(107896.98)
    assert row["toptrader_position_ratio"] == pytest.approx(2.097)
    assert row["global_account_ratio"] == pytest.approx(0.998)


def test_rest_and_archive_are_the_same_series_one_day_apart(cache, monkeypatch):
    """The check that saved two Q4 cells from a false data kill.

    REST[D at 00:00] is the state at that instant = archive end-of-day[D-1]. Compared on
    the raw date the two look 1-2% apart; shifted by one day they are identical.
    """
    days = {date(2026, 9, d): _day_frame("BTCUSDT", date(2026, 9, d),
                                         oi_first=1000.0 + d, oi_last=1100.0 + d) for d in (1, 2, 3)}
    monkeypatch.setattr(m, "fetch_metrics_day", _serve(days))
    monkeypatch.setattr(m, "datetime", _FrozenDatetime(date(2026, 9, 4)))

    def fake_rest(url: str):
        # REST stamps 00:00 of D with the archive's end-of-day value for D-1.
        out = []
        for d in (2, 3, 4):
            stamp = datetime(2026, 9, d, tzinfo=timezone.utc)
            out.append({"timestamp": int(stamp.timestamp() * 1000),
                        "sumOpenInterest": str(1100.0 + (d - 1))})
        return out

    monkeypatch.setattr(m, "_get", fake_rest)
    report = m.verify_against_rest("BTCUSDT", days=5)
    assert report["overlap"] == 3
    assert report["worst_diff_pct"] == 0.0
    assert report["passes_prereg_kill"] is True


def test_panel_drops_symbols_without_enough_history(cache, monkeypatch):
    long_days = {date(2026, 9, 1) - timedelta(days=i): _day_frame("OLDUSDT", date(2026, 9, 1) - timedelta(days=i))
                 for i in range(5)}

    def fake_fetch(symbol, days_back=600, **kw):
        if symbol == "OLDUSDT":
            return _frame_from(long_days)
        return _frame_from({date(2026, 9, 1): _day_frame("NEWUSDT", date(2026, 9, 1))})

    monkeypatch.setattr(m, "fetch_metrics", fake_fetch)
    panel = m.build_metrics_panel(["OLDUSDT", "NEWUSDT"], "open_interest", min_days=3, verbose=False)
    assert list(panel.columns) == ["OLDUSDT"]
    assert len(panel) == 5


def test_panel_refuses_an_unknown_field(cache):
    with pytest.raises(ValueError, match="unknown field"):
        m.build_metrics_panel(["BTCUSDT"], "funding_rate", verbose=False)


def _frame_from(days: dict[date, pd.DataFrame]) -> pd.DataFrame:
    rows = [m._daily_row(frame, day) for day, frame in sorted(days.items())]
    out = pd.DataFrame(rows)
    out["day"] = pd.to_datetime(out["day"])
    return out.set_index("day").sort_index()


class _FrozenDatetime:
    """Freeze datetime.now(timezone.utc).date() so the day window is deterministic."""

    def __init__(self, today: date) -> None:
        self._today = today

    def now(self, tz=None):
        return datetime(self._today.year, self._today.month, self._today.day, 12, 0, tzinfo=tz or timezone.utc)

    def fromtimestamp(self, ts, tz=None):
        return datetime.fromtimestamp(ts, tz=tz)
