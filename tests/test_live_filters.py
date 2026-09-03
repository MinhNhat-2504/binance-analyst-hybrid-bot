"""check_live_filters must reproduce the engine's rounding, flag live-only surprises, and survive a dead host."""
from __future__ import annotations

import json
from decimal import Decimal, ROUND_DOWN

import pytest

import check_live_filters as clf


def _entry(symbol, *, status="TRADING", step="0.001", min_qty="0.001", notional="20", tick="0.01",
           qprec=3, ctype="PERPETUAL"):
    return {
        "symbol": symbol, "status": status, "contractType": ctype, "quantityPrecision": qprec,
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": tick, "minPrice": "0", "maxPrice": "0"},
            {"filterType": "LOT_SIZE", "stepSize": step, "minQty": min_qty, "maxQty": "1000"},
            {"filterType": "MIN_NOTIONAL", "notional": notional},
        ],
    }


def _fake_fetch(live_info, live_marks, demo_info, demo_marks, *, fail_host=None):
    """Return a fetch(url) that serves canned payloads and raises for fail_host."""
    def fetch(url):
        if fail_host and url.startswith(fail_host):
            raise ConnectionError("simulated outage")
        base = clf.LIVE_BASE_URL if url.startswith(clf.LIVE_BASE_URL) else clf.DEMO_BASE_URL
        info, marks = (live_info, live_marks) if base == clf.LIVE_BASE_URL else (demo_info, demo_marks)
        if url.endswith("/exchangeInfo"):
            return {"symbols": info}
        if url.endswith("/premiumIndex"):
            return [{"symbol": s, "markPrice": str(m)} for s, m in marks.items()]
        raise AssertionError(f"unexpected url {url}")
    return fetch


def _ledger(tmp_path, longs="AAAUSDT,BBBUSDT", shorts="CCCUSDT", n_long=2, n_short=1):
    p = tmp_path / "ledger.csv"
    p.write_text(
        "signal_day,fill_day,mark,n_long,n_short,turnover,pnl,equity,shorts,longs,run_utc\n"
        f'2026-09-01,2026-09-02,open_to_open,{n_long},{n_short},0.1,0.001,1.01,"{shorts}","{longs}",2026-09-03T00:00:00+00:00\n',
        encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 1. min-budget arithmetic with stepSize rounding
# ---------------------------------------------------------------------------
def _engine_accepts(budget, weight, mark, step, min_qty, min_notional) -> bool:
    """The exact check PortfolioExecutor.build_plan applies: round DOWN, then both floors."""
    qty = (Decimal(str(weight)) * Decimal(str(budget)) / Decimal(str(mark)) / Decimal(step)).to_integral_value(
        rounding=ROUND_DOWN) * Decimal(step)
    return qty >= Decimal(min_qty) and qty * Decimal(str(mark)) >= Decimal(min_notional)


def test_min_budget_rounds_up_to_step_and_is_tight():
    # minNotional 25 at mark 100 needs 0.25 units, but step 0.1 forces 0.3 -> $30 / 5% = $600.
    info = {"stepSize": "0.1", "minQty": "0.1", "minNotional": "25"}
    r = clf.min_budget_for(info, 100.0, Decimal("0.05"))
    assert r["min_budget_usd"] == pytest.approx(600.0)
    assert r["required_qty"] == "0.3"
    assert r["binds_on"] == "minNotional"
    # Tight: one cent below fails the engine's own check, at the number it passes.
    assert not _engine_accepts(599.99, 0.05, 100, "0.1", "0.1", "25")
    assert _engine_accepts(600.0, 0.05, 100, "0.1", "0.1", "25")


def test_min_budget_binds_on_min_qty_when_it_is_the_larger_floor():
    info = {"stepSize": "1", "minQty": "5", "minNotional": "5"}     # 5 units at $2 = $10 > $5 notional
    r = clf.min_budget_for(info, 2.0, Decimal("0.1"))
    assert r["binds_on"] == "minQty"
    assert r["min_budget_usd"] == pytest.approx(100.0)
    assert not _engine_accepts(99.99, 0.1, 2, "1", "5", "5")
    assert _engine_accepts(100.0, 0.1, 2, "1", "5", "5")


def test_book_min_budget_picks_the_most_demanding_and_lists_unpriced():
    venue = {"ok": True, "symbols": {
        "BTCUSDT": {"stepSize": "0.001", "minQty": "0.001", "minNotional": "50"},
        "ETHUSDT": {"stepSize": "0.001", "minQty": "0.001", "minNotional": "20"},
        "NOMARK": {"stepSize": "1", "minQty": "1", "minNotional": "5"},
    }, "marks": {"BTCUSDT": 80000.0, "ETHUSDT": 2500.0}}
    w = clf.smallest_weight(8, 9)
    assert w == Decimal("0.5") / 9
    r = clf.book_min_budget(["BTCUSDT", "ETHUSDT", "NOMARK"], venue, w)
    # BTC: 0.001 * 80000 = $80 / (0.5/9) = $1440
    assert r["min_budget_usd"] == pytest.approx(1440.0)
    assert r["binding"][0]["symbol"] == "BTCUSDT"
    assert r["unpriced"] == ["NOMARK"]


def test_smallest_weight_handles_empty_book():
    assert clf.smallest_weight(0, 0) == 0
    assert clf.min_budget_for({"stepSize": "1", "minQty": "1", "minNotional": "5"}, 1.0, Decimal("0"))["min_budget_usd"] is None


# ---------------------------------------------------------------------------
# 2-3. non-TRADING on live and a live/demo filter difference
# ---------------------------------------------------------------------------
def test_end_to_end_flags_non_trading_and_filter_diffs(tmp_path, monkeypatch):
    live_info = [
        _entry("AAAUSDT", step="0.001", min_qty="0.001", notional="50"),        # differs from demo
        _entry("BBBUSDT", status="SETTLING"),                                    # not tradable live
        _entry("CCCUSDT"),
    ]
    demo_info = [
        _entry("AAAUSDT", step="0.0001", min_qty="0.0001", notional="50", qprec=4),
        _entry("BBBUSDT"),
        _entry("CCCUSDT"),
        _entry("DDDUSDT"),                                                       # demo-only
    ]
    marks = {"AAAUSDT": 80000.0, "BBBUSDT": 10.0, "CCCUSDT": 100.0, "DDDUSDT": 1.0}
    monkeypatch.setattr(clf, "fetch_json", _fake_fetch(live_info, marks, demo_info, marks))
    out = tmp_path / "reports" / "live_filters_check.json"
    rep = clf.run(fetch=clf.fetch_json, ledger=_ledger(tmp_path), report_path=out,
                  universe=(["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"], []),
                  ceilings={"testnet": 2000.0, "live": 0.0})

    comp = rep["comparison"]
    assert [x["symbol"] for x in comp["not_trading_live"]] == ["BBBUSDT"]
    assert comp["not_trading_live"][0]["status"] == "SETTLING"
    diffs = {d["symbol"]: d["fields"] for d in comp["filter_diffs"]}
    assert set(diffs) == {"AAAUSDT", "BBBUSDT"}
    assert set(diffs["AAAUSDT"]) == {"stepSize", "minQty", "quantityPrecision"}
    assert diffs["AAAUSDT"]["stepSize"] == {"live": "0.001", "demo": "0.0001"}
    assert diffs["BBBUSDT"] == {"status": {"live": "SETTLING", "demo": "TRADING"}}
    assert comp["missing_on_live"] == ["DDDUSDT"] and comp["missing_on_demo"] == []

    # Weight from the ledger: 0.5 / max(2, 1) = 0.25. AAA on live needs 0.001*80000 = $80 -> $320;
    # on demo 0.0001*80000 = $8 < $50 -> 0.0007 -> $56 -> $224.
    assert rep["weights"]["ledger_smallest"] == pytest.approx(0.25)
    assert rep["min_budget"]["live"]["held_book_at_ledger_weight"]["min_budget_usd"] == pytest.approx(320.0)
    assert rep["min_budget"]["demo"]["held_book_at_ledger_weight"]["min_budget_usd"] == pytest.approx(224.0)
    cc = rep["crosscheck"]
    assert cc["frozen_ceiling_testnet_usd"] == 2000.0 and cc["live_ceiling_is_zero"] is True
    assert cc["standing_estimate_covers_live_universe"] is True
    assert cc["testnet_ceiling_covers_demo_universe"] is True

    # JSON on disk matches, and the table renders without touching the network.
    assert json.loads(out.read_text(encoding="utf-8"))["comparison"] == comp
    clf.print_table(rep)


def test_normalised_decimal_strings_are_not_reported_as_differences():
    live = {"ok": True, "symbols": {"X": {"status": "TRADING", "contractType": "PERPETUAL", "quantityPrecision": 3,
                                          "stepSize": "0.0010", "minQty": "0.001", "minNotional": "20", "tickSize": "0.10"}}}
    demo = {"ok": True, "symbols": {"X": {"status": "TRADING", "contractType": "PERPETUAL", "quantityPrecision": 3,
                                          "stepSize": "0.001", "minQty": "0.001", "minNotional": "20.0", "tickSize": "0.1"}}}
    assert clf.compare_venues(["X"], live, demo)["filter_diffs"] == []


# ---------------------------------------------------------------------------
# 4. a host that fails
# ---------------------------------------------------------------------------
def test_dead_demo_host_degrades_and_live_still_reports(tmp_path, monkeypatch):
    info = [_entry("AAAUSDT"), _entry("CCCUSDT")]
    marks = {"AAAUSDT": 100.0, "CCCUSDT": 100.0}
    monkeypatch.setattr(clf, "fetch_json", _fake_fetch(info, marks, info, marks, fail_host=clf.DEMO_BASE_URL))
    out = tmp_path / "live_filters_check.json"
    rep = clf.run(fetch=clf.fetch_json, ledger=_ledger(tmp_path, longs="AAAUSDT", shorts="CCCUSDT", n_long=1, n_short=1),
                  report_path=out, universe=(["AAAUSDT", "CCCUSDT"], []), ceilings={"testnet": 2000.0, "live": 0.0})
    assert rep["venues"]["demo"]["ok"] is False
    assert "simulated outage" in rep["venues"]["demo"]["error"]
    assert rep["venues"]["live"]["ok"] is True
    assert rep["min_budget"]["demo"]["available"] is False
    assert rep["min_budget"]["live"]["held_book_at_ledger_weight"]["min_budget_usd"] == pytest.approx(40.0)  # $20 / 0.5
    assert rep["crosscheck"]["testnet_ceiling_covers_demo_universe"] is None
    assert rep["comparison"]["filter_diffs"] == [] and rep["comparison"]["missing_on_demo"] == []
    assert out.exists()
    clf.print_table(rep)


def test_premium_index_failure_keeps_filters_but_no_budgets():
    def fetch(url):
        if url.endswith("/premiumIndex"):
            raise TimeoutError("slow")
        return {"symbols": [_entry("AAAUSDT")]}
    v = clf.fetch_venue(clf.LIVE_BASE_URL, ["AAAUSDT"], fetch)
    assert v["ok"] is True and "premiumIndex" in v["error"]
    assert v["symbols"]["AAAUSDT"]["minNotional"] == "20" and v["marks"] == {}


def test_missing_ledger_degrades_to_empty_book(tmp_path, monkeypatch):
    info = [_entry("AAAUSDT")]
    monkeypatch.setattr(clf, "fetch_json", _fake_fetch(info, {"AAAUSDT": 10.0}, info, {"AAAUSDT": 10.0}))
    rep = clf.run(fetch=clf.fetch_json, ledger=tmp_path / "absent.csv", report_path=tmp_path / "r.json",
                  universe=(["AAAUSDT"], []), ceilings={"testnet": 2000.0, "live": 0.0})
    assert rep["universe"]["held"] == [] and rep["weights"]["ledger_smallest"] == 0.0
    assert rep["min_budget"]["live"]["held_book_at_ledger_weight"]["min_budget_usd"] is None
    assert rep["min_budget"]["live"]["main_universe_at_worst_case_weight"]["min_budget_usd"] == pytest.approx(360.0)


def test_reported_budget_is_one_the_engine_accepts_not_one_the_algebra_accepts():
    """The closed form is exact in Decimal; build_plan is not.

    PortfolioExecutor.build_plan computes abs(weight) * budget / mark as a FLOAT (with the
    weight already rounded to 10 dp by export_carry_targets) and only then rounds DOWN to
    stepSize, so a budget that is exact in Decimal can floor one whole lot low and the
    engine refuses the entire book - the 2026-08-17 incident. Sweep marks and weights that
    are hostile to binary floating point and require the ENGINE to accept every reported
    number, and one cent less to be genuinely on the boundary.
    """
    from decimal import Decimal as D
    for mark in ("81379.90", "0.00348350", "1.0000001", "7.5190", "134.1170"):
        for n in (7, 9, 11, 12):
            weight = clf.SIDE_GROSS / D(n)
            info = {"stepSize": "0.001", "minQty": "0.001", "minNotional": "20"}
            r = clf.min_budget_for(info, mark, weight)
            budget = D(str(r["min_budget_usd"]))
            assert r["engine_verified"] is True
            assert budget >= D(str(r["closed_form_usd"]))
            assert clf.engine_accepts(weight, budget, D(mark), D(info["stepSize"]),
                                      D(info["minQty"]), D(info["minNotional"]))
            # tight: a cent below the reported figure the engine must refuse
            assert not clf.engine_accepts(weight, budget - D("0.01"), D(mark), D(info["stepSize"]),
                                          D(info["minQty"]), D(info["minNotional"]))


def test_dead_live_host_does_not_fabricate_a_capital_shortfall(tmp_path, monkeypatch, capsys):
    """A network outage must not print 'standing estimate DOES NOT COVER it'.

    False is a verdict about capital; None is 'we could not ask'. The demo line was already
    tri-state, the live line was not.
    """
    info = [_entry("AAAUSDT"), _entry("CCCUSDT")]
    marks = {"AAAUSDT": 100.0, "CCCUSDT": 100.0}
    monkeypatch.setattr(clf, "fetch_json", _fake_fetch(info, marks, info, marks, fail_host=clf.LIVE_BASE_URL))
    rep = clf.run(fetch=clf.fetch_json, ledger=_ledger(tmp_path, longs="AAAUSDT", shorts="CCCUSDT", n_long=1, n_short=1),
                  report_path=tmp_path / "r.json", universe=(["AAAUSDT", "CCCUSDT"], []),
                  ceilings={"testnet": 2000.0, "live": 0.0})
    assert rep["venues"]["live"]["ok"] is False
    assert rep["crosscheck"]["standing_estimate_covers_live_universe"] is None
    clf.print_table(rep)
    out = capsys.readouterr().out
    assert "n/a (live unavailable)" in out
    assert "DOES NOT COVER" not in out
