"""The snapshot collector must be idempotent, partial-failure tolerant and atomic - it runs unattended for months."""
from __future__ import annotations

import csv
import json
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

import pytest

import collect_daily_snapshots as col

TODAY = date(2026, 9, 3)
DAY_MS = 86_400_000


def _ms(d: date) -> int:
    return (d - date(1970, 1, 1)).days * DAY_MS


class FakeAPI:
    """Serves the seven endpoints from memory; records every call; can be told to fail symbols."""

    def __init__(self, days_available: int = 30, fail: dict[str, int | None] | None = None,
                 no_spot: set[str] = frozenset(), taker_lag: bool = True):
        self.days_available = days_available
        self.fail = fail or {}          # symbol -> HTTP code (None = transport error)
        self.no_spot = set(no_spot)
        self.taker_lag = taker_lag
        self.calls: list[str] = []
        self.fail_once: dict[str, int] = {}
        self.today = TODAY

    def __call__(self, url: str, timeout: float = 20.0):
        self.calls.append(url)
        parsed = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        sym = q["symbol"]
        if sym in self.fail_once:
            code = self.fail_once.pop(sym)
            raise col.FetchError("transient", code)
        if sym in self.fail:
            raise col.FetchError("boom", self.fail[sym])
        path = parsed.path
        if path == "/api/v3/ticker/price":
            if sym in self.no_spot:
                raise col.FetchError("HTTP 400", 400)
            return {"symbol": sym, "price": "100.5"}
        if path == "/fapi/v1/premiumIndex":
            return {"symbol": sym, "markPrice": "101.0", "indexPrice": "100.9",
                    "lastFundingRate": "0.0001", "time": _ms(self.today) + 5 * 60_000}
        limit = int(q["limit"])
        # rows for the last `limit` bars ending today (snapshot endpoints) or yesterday (taker)
        last = self.today - timedelta(days=1) if (self.taker_lag and "taker" in path) else self.today
        rows = []
        for k in range(limit - 1, -1, -1):
            d = last - timedelta(days=k)
            if (self.today - d).days >= self.days_available:
                continue
            ts = _ms(d)
            if "openInterestHist" in path:
                rows.append({"symbol": sym, "sumOpenInterest": "10", "sumOpenInterestValue": "1000",
                             "CMCCirculatingSupply": "5", "timestamp": ts})
            elif "taker" in path:
                rows.append({"buySellRatio": "1.1", "buyVol": "7", "sellVol": "6", "timestamp": ts})
            else:
                rows.append({"symbol": sym, "longShortRatio": "2.0", "longAccount": "0.66",
                             "shortAccount": "0.34", "timestamp": ts})
        return rows


@pytest.fixture
def api(monkeypatch):
    fake = FakeAPI()
    monkeypatch.setattr(col, "fetch_json", fake)
    monkeypatch.setattr(col.time, "sleep", lambda s: None)
    return fake


def _read_all(data_dir: Path) -> list[tuple[str, str, str, str]]:
    rows = []
    for p in sorted(data_dir.glob("????-??-??.csv")):
        rows.extend(col.read_day_file(p))
    return rows


def _run(symbols, data_dir, today=TODAY, **kw):
    return col.run(symbols, data_dir=data_dir, today=today, workers=1, sleep=lambda s: None, **kw)


# ----------------------------------------------------------------------------- schema

def test_long_format_schema_and_day_files(api, tmp_path):
    rc = _run(["BTCUSDT", "1000PEPEUSDT"], tmp_path)
    assert rc == 0
    files = sorted(tmp_path.glob("????-??-??.csv"))
    # 29 completed history days (today-29 .. today-1: the 30-bar limit includes today) + today
    assert files[-1].name == "2026-09-03.csv"
    assert files[0].name == "2026-08-05.csv"
    with files[-1].open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == ["day", "symbol", "field", "value"]
        rows = list(reader)
    assert all(r["day"] == "2026-09-03" for r in rows)
    fields = {(r["symbol"], r["field"]): r["value"] for r in rows}
    # today: live premium + spot only; history days must not include today's forming bar
    assert fields[("BTCUSDT", "mark_price")] == "101.0"
    assert fields[("BTCUSDT", "spot_multiplier")] == "1"
    assert fields[("1000PEPEUSDT", "spot_multiplier")] == "1000"
    assert ("BTCUSDT", "oi_contracts") not in fields
    # yesterday: all five history sources, no live fields
    yday = {(r[1], r[2]) for r in col.read_day_file(tmp_path / "2026-09-02.csv")}
    for f in ("oi_contracts", "oi_value_usdt", "top_pos_ls_ratio", "top_acct_long_share",
              "global_acct_short_share", "taker_buy_vol", "taker_buy_sell_ratio"):
        assert ("BTCUSDT", f) in yday
    assert ("BTCUSDT", "mark_price") not in yday
    assert all(f in col.FIELD_TO_SOURCE for _, f in yday)


def test_spot_symbol_mapping():
    assert col.spot_symbol("1000PEPEUSDT") == ("PEPEUSDT", 1000)
    assert col.spot_symbol("1000SHIBUSDT") == ("SHIBUSDT", 1000)
    assert col.spot_symbol("BTCUSDT") == ("BTCUSDT", 1)
    assert col.spot_symbol("1INCHUSDT") == ("1INCHUSDT", 1)


def test_missing_spot_market_is_data_not_failure(api, tmp_path):
    api.no_spot.add("XYZUSDT")
    assert _run(["XYZUSDT"], tmp_path) == 0
    today_rows = {r[2] for r in col.read_day_file(tmp_path / "2026-09-03.csv")}
    assert "mark_price" in today_rows and "spot_price" not in today_rows
    m = json.loads((tmp_path / "manifest.json").read_text())
    assert "spot" not in m["days"]["2026-09-03"]["sources"]     # so it is re-checked tomorrow


# ----------------------------------------------------------------------------- idempotency

def test_second_run_same_day_makes_no_calls_and_writes_nothing(api, tmp_path):
    _run(["BTCUSDT", "ETHUSDT"], tmp_path)
    n_calls = len(api.calls)
    assert n_calls == 2 * 7
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*.csv")}
    rc = _run(["BTCUSDT", "ETHUSDT"], tmp_path)
    assert rc == 0
    assert len(api.calls) == n_calls
    assert {p.name: p.read_bytes() for p in tmp_path.glob("*.csv")} == before
    assert json.loads((tmp_path / "manifest.json").read_text())["last_run"]["rows_added"] == 0


def test_next_day_fetches_only_the_new_days(api, tmp_path):
    _run(["BTCUSDT"], tmp_path)
    rows_day1 = _read_all(tmp_path)
    api.calls.clear()
    tomorrow = TODAY + timedelta(days=1)
    api.today = tomorrow                       # move the fake's clock forward one day
    rc = _run(["BTCUSDT"], tmp_path, today=tomorrow)
    assert rc == 0
    limits = [int(dict(urllib.parse.parse_qsl(urllib.parse.urlparse(u).query)).get("limit", 0))
              for u in api.calls]
    assert max(limits) <= 3, "steady-state run must ask for a couple of bars, not the whole window"
    rows_day2 = _read_all(tmp_path)
    new = set(rows_day2) - set(rows_day1)
    assert new and {r[0] for r in new} == {"2026-09-03", "2026-09-04"}
    assert set(rows_day1) <= set(rows_day2)      # nothing lost, nothing rewritten


def test_new_symbol_is_backfilled_without_refetching_old_ones(api, tmp_path):
    _run(["BTCUSDT"], tmp_path)
    api.calls.clear()
    _run(["BTCUSDT", "ETHUSDT"], tmp_path)
    assert all("symbol=ETHUSDT" in u for u in api.calls)
    assert len(api.calls) == 7


# ----------------------------------------------------------------------------- failures

def test_partial_failure_is_logged_skipped_and_retried_next_run(api, tmp_path, capsys):
    api.fail = {"BADUSDT": 500}
    rc = _run(["BTCUSDT", "BADUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT"], tmp_path)
    out = capsys.readouterr().out
    assert rc == 0                          # 1/6 < 20%
    assert "ERROR BADUSDT oi: boom" in out
    assert "failed 1" in out
    symbols_stored = {r[1] for r in _read_all(tmp_path)}
    assert "BADUSDT" not in symbols_stored and {"BTCUSDT", "ETHUSDT"} <= symbols_stored
    m = json.loads((tmp_path / "manifest.json").read_text())
    assert m["last_run"]["failed"] == ["BADUSDT"]
    # the failing symbol heals: next run fetches only it
    api.fail = {}
    api.calls.clear()
    assert _run(["BTCUSDT", "BADUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT"], tmp_path) == 0
    assert all("symbol=BADUSDT" in u for u in api.calls) and api.calls
    assert "BADUSDT" in {r[1] for r in _read_all(tmp_path)}


def test_more_than_20_percent_failed_exits_1_but_keeps_good_data(api, tmp_path):
    api.fail = {"AUSDT": None, "BUSDT": 503}
    rc = _run(["AUSDT", "BUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"], tmp_path)
    assert rc == 1
    assert {"BTCUSDT", "ETHUSDT", "SOLUSDT"} <= {r[1] for r in _read_all(tmp_path)}


def test_retries_once_on_429_and_5xx_not_on_400(api, monkeypatch):
    sleeps: list[float] = []
    api.fail_once = {"BTCUSDT": 429}
    assert col._get(col._url(col.FAPI, "/fapi/v1/premiumIndex", symbol="BTCUSDT"), sleeps.append)["markPrice"]
    assert col.RATE_LIMIT_SLEEP_S in sleeps
    api.fail_once = {"BTCUSDT": 502}
    assert col._get(col._url(col.FAPI, "/fapi/v1/premiumIndex", symbol="BTCUSDT"), sleeps.append)
    api.fail = {"BTCUSDT": 400}
    n = len(api.calls)
    with pytest.raises(col.FetchError):
        col._get(col._url(col.FAPI, "/fapi/v1/premiumIndex", symbol="BTCUSDT"), sleeps.append)
    assert len(api.calls) == n + 1
    api.fail = {"BTCUSDT": 500}
    n = len(api.calls)
    with pytest.raises(col.FetchError):
        col._get(col._url(col.FAPI, "/fapi/v1/premiumIndex", symbol="BTCUSDT"), sleeps.append)
    assert len(api.calls) == n + 2


def test_taker_lag_leaves_day_needed_until_it_arrives(api, tmp_path):
    api.days_available = 3
    _run(["BTCUSDT"], tmp_path)
    m = json.loads((tmp_path / "manifest.json").read_text())
    yday = (TODAY - timedelta(days=1)).isoformat()
    assert "BTCUSDT" in m["days"][yday]["sources"]["taker"]
    # a day older than the API keeps is simply absent, and still planned (bounded by the window)
    old = (TODAY - timedelta(days=10)).isoformat()
    assert old not in m["days"]
    needed = col.plan_needed(m, "BTCUSDT", TODAY)
    assert old in needed["oi"] and yday not in needed["oi"]


# ----------------------------------------------------------------------------- atomic write + manifest

def test_atomic_write_leaves_old_file_intact_on_failure(tmp_path):
    target = tmp_path / "2026-09-01.csv"
    col.write_day_file(target, [("2026-09-01", "BTCUSDT", "oi_contracts", "1")])
    original = target.read_bytes()

    def exploding(fh):
        fh.write("day,symbol,field,value\nhalf-written")
        raise OSError("disk full")

    with pytest.raises(OSError):
        col.write_atomic(target, exploding)
    assert target.read_bytes() == original
    assert list(tmp_path.glob("*.tmp-*")) == []


def test_atomic_write_never_exposes_partial_content(tmp_path, monkeypatch):
    target = tmp_path / "2026-09-01.csv"
    seen: list[bytes | None] = []
    real_replace = col.os.replace

    def spy_replace(src, dst):
        seen.append(Path(dst).read_bytes() if Path(dst).exists() else None)
        real_replace(src, dst)

    monkeypatch.setattr(col.os, "replace", spy_replace)
    col.write_day_file(target, [("2026-09-01", "BTCUSDT", "oi_contracts", "1")])
    assert seen == [None]                                    # target did not exist mid-write
    assert list(tmp_path.glob("*.tmp-*")) == []
    assert col.read_day_file(target) == [("2026-09-01", "BTCUSDT", "oi_contracts", "1")]


def test_manifest_tracks_days_sources_symbols_and_last_run(api, tmp_path):
    _run(["BTCUSDT", "ETHUSDT"], tmp_path)
    m = json.loads((tmp_path / "manifest.json").read_text())
    assert m["version"] == col.MANIFEST_VERSION
    assert m["universe_size"] == 2 and m["last_run_utc"].endswith("Z")
    assert m["last_run"] == {"today": "2026-09-03", "symbols": 2, "fetched": 2, "failed": [],
                             "rows_added": sum(d["n_rows"] for d in m["days"].values())}
    assert sorted(m["days"]) == sorted(p.stem for p in tmp_path.glob("????-??-??.csv"))
    yday = m["days"]["2026-09-02"]
    assert yday["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert set(yday["sources"]) == set(col.HISTORY_SOURCES)
    assert yday["sources"]["taker"] == ["BTCUSDT", "ETHUSDT"]
    assert yday["n_rows"] == len(col.read_day_file(tmp_path / "2026-09-02.csv"))
    assert set(m["days"]["2026-09-03"]["sources"]) == {"premium", "spot"}


def test_manifest_is_rebuilt_from_day_files_when_lost(api, tmp_path):
    _run(["BTCUSDT"], tmp_path)
    m1 = json.loads((tmp_path / "manifest.json").read_text())
    (tmp_path / "manifest.json").unlink()
    api.calls.clear()
    assert _run(["BTCUSDT"], tmp_path) == 0
    assert api.calls == []                                   # nothing refetched
    m2 = json.loads((tmp_path / "manifest.json").read_text())
    assert m2["days"] == m1["days"]


def test_dry_run_writes_nothing(api, tmp_path, capsys):
    rc = _run(["ETHUSDT", "BTCUSDT"], tmp_path, dry_run=True)
    assert rc == 0
    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []
    out = capsys.readouterr().out
    assert "DRY RUN BTCUSDT" in out and "nothing written" in out
    assert "2026-09-02,BTCUSDT,oi_contracts,10" in out
    assert api.calls and all("symbol=BTCUSDT" in u for u in api.calls)


def test_default_universe_is_union_of_lab_and_holdout():
    from run_carry_holdout import HOLDOUT_UNIVERSE
    from run_daily_lab import UNIVERSE
    u = col.default_universe()
    assert u == sorted(u) and len(u) == len(set(UNIVERSE) | set(HOLDOUT_UNIVERSE))
    assert 70 <= len(u) <= 80
