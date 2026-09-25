"""measure_funding_interval.py: offline, with the exchange list and the ledger faked."""
from __future__ import annotations

import pytest

import measure_funding_interval as mfi


FAKE_INFO = [
    {"symbol": "PYTHUSDT", "fundingIntervalHours": 4},
    {"symbol": "TAOUSDT", "fundingIntervalHours": 4},
    {"symbol": "BTCUSDT", "fundingIntervalHours": 8},
    {"symbol": "ZILUSDT", "fundingIntervalHours": 4},       # hold-out name
    {"symbol": "NOPEUSDT", "fundingIntervalHours": 4},      # not in universe
]


def test_four_hour_list_is_filtered_to_the_universe():
    fast = mfi.four_hour_symbols({"PYTHUSDT", "TAOUSDT", "BTCUSDT", "ZILUSDT"}, fetch=lambda url: FAKE_INFO)
    assert fast == {"PYTHUSDT": 4, "TAOUSDT": 4, "ZILUSDT": 4}


def test_ledger_exposure_counts_sides_and_shares(tmp_path):
    led = tmp_path / "ledger.csv"
    led.write_text(
        "signal_day,fill_day,mark,n_long,n_short,turnover,pnl,equity,shorts,longs,run_utc\n"
        '2026-08-02,2026-08-03,o,2,2,1,0,1,"PYTHUSDT,TAOUSDT","BTCUSDT,ETHUSDT",x\n'
        '2026-08-03,2026-08-04,o,2,2,1,0,1,"PYTHUSDT,ETHUSDT","BTCUSDT,SOLUSDT",x\n'
        '2026-08-04,2026-08-05,o,2,2,1,0,1,"ETHUSDT,SOLUSDT","BTCUSDT,LINKUSDT",x\n',
        encoding="utf-8")
    out = mfi.ledger_exposure(led, {"PYTHUSDT": 4, "TAOUSDT": 4})
    assert out["days"] == 3
    assert out["days_with_any_4h_name_pct"] == pytest.approx(66.7, abs=0.1)
    assert out["share_of_book_slots_pct"] == pytest.approx(100 * 3 / 12, abs=0.1)
    assert out["per_symbol"]["PYTHUSDT"] == {"interval_h": 4, "days_long": 0, "days_short": 2, "in_book_pct": pytest.approx(66.7, abs=0.1)}
    assert out["per_symbol"]["TAOUSDT"]["days_short"] == 1


def test_backtest_part_waits_for_the_registered_date(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(mfi, "four_hour_symbols", lambda universe, fetch=None: {"PYTHUSDT": 4})
    monkeypatch.setattr(mfi, "LEDGER", tmp_path / "absent.csv")
    monkeypatch.setattr(mfi, "REPORT", tmp_path / "r.json")
    rc = mfi.main(["--backtest", "--today", "2026-09-30"])
    assert rc == 3
    assert "REFUSING backtest" in capsys.readouterr().out


def test_ledger_part_runs_any_time_and_writes_the_report(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(mfi, "four_hour_symbols", lambda universe, fetch=None: {"PYTHUSDT": 4})
    monkeypatch.setattr(mfi, "LEDGER", tmp_path / "absent.csv")
    monkeypatch.setattr(mfi, "REPORT", tmp_path / "r.json")
    assert mfi.main(["--ledger", "--today", "2026-09-25"]) == 0
    assert (tmp_path / "r.json").exists()
    assert "4h-funding names" in capsys.readouterr().out
