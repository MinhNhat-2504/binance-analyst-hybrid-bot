"""Go-live pre-flight: does the LIVE exchange accept this book at this capital? Keyless, read-only.

    python -X utf8 check_live_filters.py            # prints a table, writes reports/live_filters_check.json
    python -X utf8 check_live_filters.py --json-only

Why this exists. The testnet rehearsal proves the ENGINE works, but it runs against the demo
host's filters. The first unattended run (2026-08-17) refused the whole book because one
symbol's minimum notional was larger than the smallest CARRY-7d weight could buy at the
budget. Live filters (LOT_SIZE, MIN_NOTIONAL, status) are not guaranteed to match demo, and
they change without notice. So before any live ceiling is ever reviewed, this script asks
BOTH venues, with no credentials:

  1. Is every symbol we might trade TRADING and PERPETUAL on live?
  2. At what gross budget does EVERY symbol clear minNotional/minQty after rounding DOWN to
     stepSize - exactly the arithmetic the engine applies in build_plan?  Reported for the
     book currently held (weight 0.5 / max(n_long, n_short) from the last ledger row), for
     the 42-symbol strategy universe at the worst-case weight 0.5/9, and per venue.
  3. Which symbols have DIFFERENT filters on live than on demo (the go-live surprises), and
     which are missing from either venue?
  4. Cross-check against the standing ~$1,800 minimum-viable-capital estimate and the frozen
     ceilings in execution_ceilings_v1.json.

Public endpoints only (/fapi/v1/exchangeInfo, /fapi/v1/premiumIndex). Never signs, never
orders, never writes anything but reports/live_filters_check.json. A venue that fails is
reported as unavailable; the script still finishes on the other one. Exit 0 always: it is a
report, not a gate.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Any, Callable

import requests

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

LIVE_BASE_URL = "https://fapi.binance.com"
DEMO_BASE_URL = "https://demo-fapi.binance.com"
VENUES = {"live": LIVE_BASE_URL, "demo": DEMO_BASE_URL}

LEDGER = ROOT / "carry_paper_ledger.csv"
REPORT = ROOT / "reports" / "live_filters_check.json"

STANDING_MIN_CAPITAL_USD = 1_800.0     # execution_ceilings_v1.json note, 2026-08-24
SIDE_GROSS = Decimal("0.5")            # each side of CARRY-7d is 0.5 gross, split equally
WORST_CASE_NAMES_PER_SIDE = 9          # 20% of 42 rounds to 8-9; 9 gives the smallest weight
FILTER_FIELDS = ("status", "contractType", "quantityPrecision", "stepSize", "minQty",
                 "minNotional", "tickSize")
TIMEOUT_S = 20

FetchFn = Callable[[str], Any]


# ---------------------------------------------------------------------------
# HTTP (the only function tests replace)
# ---------------------------------------------------------------------------
def fetch_json(url: str) -> Any:
    """GET a public endpoint and return the decoded JSON. Raises on any failure."""
    resp = requests.get(url, timeout=TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
def strategy_universe() -> tuple[list[str], list[str]]:
    """(main 42-symbol universe, disjoint hold-out universe). Missing modules degrade to []."""
    main: list[str] = []
    holdout: list[str] = []
    try:
        from run_daily_lab import UNIVERSE
        main = list(UNIVERSE)
    except Exception as exc:  # noqa: BLE001 - report script must not crash
        print(f"  warn: run_daily_lab.UNIVERSE unavailable ({exc})")
    try:
        from run_carry_holdout import HOLDOUT_UNIVERSE
        holdout = list(HOLDOUT_UNIVERSE)
    except Exception as exc:  # noqa: BLE001
        print(f"  warn: run_carry_holdout.HOLDOUT_UNIVERSE unavailable ({exc})")
    return main, holdout


def _split(cell: str) -> list[str]:
    return [s.strip() for s in (cell or "").split(",") if s.strip()]


def latest_ledger_book(path: Path = LEDGER) -> dict[str, Any]:
    """Held names and side counts from the last ledger row. Missing/empty ledger -> empty book."""
    empty = {"available": False, "fill_day": None, "longs": [], "shorts": [], "n_long": 0, "n_short": 0}
    if not path.exists():
        return empty
    try:
        rows = list(csv.DictReader(path.open(encoding="utf-8")))
    except (OSError, csv.Error):
        return empty
    if not rows:
        return empty
    last = rows[-1]
    longs, shorts = _split(last.get("longs", "")), _split(last.get("shorts", ""))
    return {
        "available": True, "fill_day": last.get("fill_day"), "longs": longs, "shorts": shorts,
        "n_long": int(last.get("n_long") or len(longs)), "n_short": int(last.get("n_short") or len(shorts)),
    }


def smallest_weight(n_long: int, n_short: int) -> Decimal:
    """0.5 gross per side split equally: the smallest weight sits on the larger side."""
    n = max(int(n_long), int(n_short))
    if n <= 0:
        return Decimal("0")
    return SIDE_GROSS / Decimal(n)


# ---------------------------------------------------------------------------
# Venue snapshot
# ---------------------------------------------------------------------------
def _filter(filters: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    for f in filters or []:
        if f.get("filterType") == kind:
            return f
    return {}


def extract_symbol(entry: dict[str, Any]) -> dict[str, Any]:
    """Flatten one exchangeInfo symbol entry to the fields that decide whether we can trade it."""
    lot = _filter(entry.get("filters", []), "LOT_SIZE")
    notional = _filter(entry.get("filters", []), "MIN_NOTIONAL") or _filter(entry.get("filters", []), "NOTIONAL")
    price = _filter(entry.get("filters", []), "PRICE_FILTER")
    return {
        "status": entry.get("status"),
        "contractType": entry.get("contractType"),
        "quantityPrecision": entry.get("quantityPrecision"),
        "stepSize": lot.get("stepSize"),
        "minQty": lot.get("minQty"),
        "minNotional": notional.get("notional", notional.get("minNotional")),
        "tickSize": price.get("tickSize"),
    }


def fetch_venue(base_url: str, wanted: list[str], fetch: FetchFn = fetch_json) -> dict[str, Any]:
    """exchangeInfo + premiumIndex for one host, reduced to the wanted symbols.

    Any failure (network, HTTP status, bad JSON shape) yields ok=False with the reason; the
    caller keeps going with the other venue.
    """
    out: dict[str, Any] = {"base_url": base_url, "ok": False, "error": None,
                           "n_symbols_listed": 0, "symbols": {}, "marks": {}}
    try:
        info = fetch(f"{base_url}/fapi/v1/exchangeInfo")
        listed = info["symbols"]
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"exchangeInfo: {type(exc).__name__}: {exc}"
        return out
    want = set(wanted)
    out["n_symbols_listed"] = len(listed)
    out["symbols"] = {e["symbol"]: extract_symbol(e) for e in listed if e.get("symbol") in want}
    try:
        prem = fetch(f"{base_url}/fapi/v1/premiumIndex")
        out["marks"] = {p["symbol"]: float(p["markPrice"]) for p in prem
                        if p.get("symbol") in want and p.get("markPrice") not in (None, "")}
    except Exception as exc:  # noqa: BLE001
        # Filters alone still answer the status/diff questions; only budgets need marks.
        out["error"] = f"premiumIndex: {type(exc).__name__}: {exc}"
    out["ok"] = True
    return out


# ---------------------------------------------------------------------------
# Budget arithmetic (mirrors execution.engine.PortfolioExecutor.build_plan)
# ---------------------------------------------------------------------------
def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _round_step(value: Decimal, step: Decimal, *, up: bool = False) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN) * step


def engine_accepts(weight: Decimal, budget: Decimal, mark: Decimal, step: Decimal,
                   min_qty: Decimal, min_notional: Decimal) -> bool:
    """The engine's own acceptance test, arithmetic included.

    PortfolioExecutor.build_plan computes the quantity as a FLOAT product of a weight that
    export_carry_targets.py has already rounded to 10 dp, converts to Decimal and only then
    rounds DOWN to stepSize. The closed form below is exact in Decimal but a budget can be
    one ulp short in float and floor a whole lot low - which refuses the entire book, the
    exact failure of 2026-08-17. So the reported number is the one the ENGINE takes.
    """
    qty = _round_step(_dec(abs(round(float(weight), 10)) * float(budget) / float(mark)), step)
    return qty >= min_qty and qty * mark >= min_notional


def min_budget_for(info: dict[str, Any], mark: float | Decimal, weight: Decimal) -> dict[str, Any]:
    """Smallest gross budget at which the engine builds a leg for this symbol.

    Closed form first (floor(x) >= k for whole k iff x >= k, so (smallest admissible lot
    count) * stepSize * mark / weight), then rounded up to the cent and verified against
    engine_accepts(), bumping by cents until the engine agrees.
    """
    mark_d = _dec(mark)
    step, min_qty, min_notional = _dec(info["stepSize"]), _dec(info["minQty"]), _dec(info["minNotional"] or 0)
    if weight <= 0 or mark_d <= 0:
        return {"min_budget_usd": None, "min_qty_lots": None, "reason": "no weight or mark"}
    qty_from_notional = _round_step(min_notional / mark_d, step, up=True)
    qty_from_min_qty = _round_step(min_qty, step, up=True)
    need_qty = max(qty_from_notional, qty_from_min_qty)
    closed_form = need_qty * mark_d / weight
    budget = (closed_form * 100).to_integral_value(rounding=ROUND_UP) / 100
    bumped = 0
    while bumped < 1000 and not engine_accepts(weight, budget, mark_d, step, min_qty, min_notional):
        budget += Decimal("0.01")
        bumped += 1
    binds_on = "minNotional" if qty_from_notional >= qty_from_min_qty else "minQty"
    return {"min_budget_usd": float(budget), "required_qty": str(need_qty.normalize()),
            "required_notional_usd": float(need_qty * mark_d), "binds_on": binds_on,
            "mark": float(mark_d), "closed_form_usd": float(closed_form),
            "engine_verified": bumped < 1000, "cents_bumped": bumped}


def book_min_budget(symbols: list[str], venue: dict[str, Any], weight: Decimal, top: int = 5) -> dict[str, Any]:
    """Max over the book of the per-symbol minimum budget, plus the names that bind."""
    per: list[dict[str, Any]] = []
    missing: list[str] = []
    for s in symbols:
        info, mark = venue["symbols"].get(s), venue["marks"].get(s)
        if not info or mark is None or not info.get("stepSize"):
            missing.append(s)
            continue
        r = min_budget_for(info, mark, weight)
        if r.get("min_budget_usd") is not None:
            per.append({"symbol": s, **r})
    per.sort(key=lambda r: -r["min_budget_usd"])
    return {
        "weight": float(weight), "n_symbols": len(symbols), "n_priced": len(per),
        "min_budget_usd": (per[0]["min_budget_usd"] if per else None),
        "binding": per[:top], "unpriced": missing,
    }


# ---------------------------------------------------------------------------
# Live vs demo
# ---------------------------------------------------------------------------
def _norm(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field in ("stepSize", "minQty", "minNotional", "tickSize"):
        try:
            return str(_dec(value).normalize())
        except Exception:  # noqa: BLE001
            return str(value)
    return value


def compare_venues(symbols: list[str], live: dict[str, Any], demo: dict[str, Any]) -> dict[str, Any]:
    """Symbols not TRADING on live, filter fields that differ, and symbols absent from a venue."""
    not_trading_live = []
    diffs = []
    missing_live, missing_demo = [], []
    for s in symbols:
        li, de = live["symbols"].get(s) if live["ok"] else None, demo["symbols"].get(s) if demo["ok"] else None
        if live["ok"] and li is None:
            missing_live.append(s)
        if demo["ok"] and de is None:
            missing_demo.append(s)
        if li is not None and (li.get("status") != "TRADING" or li.get("contractType") != "PERPETUAL"):
            not_trading_live.append({"symbol": s, "status": li.get("status"), "contractType": li.get("contractType")})
        if li is not None and de is not None:
            changed = {f: {"live": li.get(f), "demo": de.get(f)} for f in FILTER_FIELDS
                       if _norm(f, li.get(f)) != _norm(f, de.get(f))}
            if changed:
                diffs.append({"symbol": s, "fields": changed})
    return {"not_trading_live": not_trading_live, "filter_diffs": diffs,
            "missing_on_live": missing_live, "missing_on_demo": missing_demo}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _ceilings() -> dict[str, Any]:
    try:
        from execution.contracts import frozen_ceiling
        return {"testnet": frozen_ceiling("testnet"), "live": frozen_ceiling("live")}
    except Exception as exc:  # noqa: BLE001
        return {"testnet": None, "live": None, "error": f"{type(exc).__name__}: {exc}"}


def build_report(main: list[str], holdout: list[str], book: dict[str, Any],
                 venues: dict[str, dict[str, Any]], ceilings: dict[str, Any]) -> dict[str, Any]:
    held = sorted(set(book["longs"]) | set(book["shorts"]))
    everything = list(dict.fromkeys(main + holdout + held))
    w_ledger = smallest_weight(book["n_long"], book["n_short"])
    w_worst = SIDE_GROSS / Decimal(WORST_CASE_NAMES_PER_SIDE)
    scenarios = {
        "held_book_at_ledger_weight": (held, w_ledger),
        "held_book_at_worst_case_weight": (held, w_worst),
        "main_universe_at_worst_case_weight": (main, w_worst),
    }
    budgets: dict[str, Any] = {}
    for name, venue in venues.items():
        budgets[name] = {"available": venue["ok"] and bool(venue["marks"])}
        if not budgets[name]["available"]:
            budgets[name]["error"] = venue.get("error") or "no mark prices"
            continue
        for label, (syms, w) in scenarios.items():
            budgets[name][label] = book_min_budget(syms, venue, w)

    def _budget(venue: str, label: str) -> float | None:
        return (budgets.get(venue, {}).get(label) or {}).get("min_budget_usd")

    live_held, live_univ = _budget("live", "held_book_at_ledger_weight"), _budget("live", "main_universe_at_worst_case_weight")
    testnet_ceiling, live_ceiling = ceilings.get("testnet"), ceilings.get("live")
    crosscheck = {
        "standing_min_capital_usd": STANDING_MIN_CAPITAL_USD,
        "frozen_ceiling_testnet_usd": testnet_ceiling,
        "frozen_ceiling_live_usd": live_ceiling,
        "live_held_book_min_budget_usd": live_held,
        "live_universe_worst_case_min_budget_usd": live_univ,
        "standing_estimate_covers_live_universe": (None if live_univ is None else live_univ <= STANDING_MIN_CAPITAL_USD),
        "testnet_ceiling_covers_demo_universe": None,
        "live_ceiling_is_zero": (live_ceiling == 0.0) if live_ceiling is not None else None,
    }
    demo_univ = _budget("demo", "main_universe_at_worst_case_weight")
    if demo_univ is not None and testnet_ceiling is not None:
        crosscheck["testnet_ceiling_covers_demo_universe"] = demo_univ <= testnet_ceiling
    crosscheck["demo_universe_worst_case_min_budget_usd"] = demo_univ

    comparison = compare_venues(everything, venues["live"], venues["demo"])
    return {
        "version": "LIVE_FILTERS_CHECK_V1",
        "generated_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "read_only": True, "credentials_used": False,
        "universe": {"main": main, "holdout": holdout, "held": held,
                     "ledger_fill_day": book.get("fill_day"), "n_long": book["n_long"], "n_short": book["n_short"]},
        "weights": {"ledger_smallest": float(w_ledger), "worst_case": float(w_worst),
                    "rule": "0.5 gross per side / max(n_long, n_short); worst case 0.5/9"},
        "venues": venues,
        "min_budget": budgets,
        "comparison": comparison,
        "crosscheck": crosscheck,
    }


def _fmt_usd(v: Any) -> str:
    return "n/a" if v is None else f"{v:,.0f}"


def print_table(rep: dict[str, Any]) -> None:
    u, w, cc = rep["universe"], rep["weights"], rep["crosscheck"]
    print("LIVE FILTERS CHECK  (keyless, read-only)  " + rep["generated_utc"])
    for name, v in rep["venues"].items():
        state = f"OK  {v['n_symbols_listed']} listed, {len(v['symbols'])} of ours found, {len(v['marks'])} marks" if v["ok"] else "UNAVAILABLE"
        print(f"  {name:<5} {v['base_url']:<32} {state}" + (f"  ({v['error']})" if v.get("error") else ""))
    print(f"  book: {len(u['held'])} held ({u['n_long']}L/{u['n_short']}S, fill {u['ledger_fill_day']}), "
          f"main universe {len(u['main'])}, hold-out {len(u['holdout'])}")
    print(f"  smallest weight: ledger {w['ledger_smallest']:.4f}   worst case {w['worst_case']:.4f}")
    print()
    print(f"  {'min gross budget (USD)':<40} {'live':>10} {'demo':>10}")
    for label in ("held_book_at_ledger_weight", "held_book_at_worst_case_weight", "main_universe_at_worst_case_weight"):
        vals = [(rep["min_budget"].get(v, {}).get(label) or {}).get("min_budget_usd") for v in ("live", "demo")]
        print(f"  {label:<40} {_fmt_usd(vals[0]):>10} {_fmt_usd(vals[1]):>10}")
    print()
    for venue in ("live", "demo"):
        b = rep["min_budget"].get(venue, {}).get("main_universe_at_worst_case_weight")
        if not b:
            continue
        print(f"  {venue} binding names (universe, worst-case weight):")
        print(f"    {'symbol':<14} {'mark':>12} {'minNotional':>12} {'stepSize':>10} {'req qty':>10} {'min budget':>12}")
        for r in b["binding"]:
            info = rep["venues"][venue]["symbols"][r["symbol"]]
            print(f"    {r['symbol']:<14} {r['mark']:>12.4f} {str(info['minNotional']):>12} {str(info['stepSize']):>10} "
                  f"{r['required_qty']:>10} {r['min_budget_usd']:>12,.0f}")
        if b["unpriced"]:
            print(f"    unpriced: {', '.join(b['unpriced'])}")
    print()
    comp = rep["comparison"]
    nt = comp["not_trading_live"]
    print(f"  not TRADING/PERPETUAL on live: {len(nt)}" + (" -> " + ", ".join(f"{x['symbol']}({x['status']})" for x in nt) if nt else ""))
    print(f"  live vs demo filter differences: {len(comp['filter_diffs'])}")
    for d in comp["filter_diffs"]:
        parts = [f"{f}: live={c['live']} demo={c['demo']}" for f, c in d["fields"].items()]
        print(f"    {d['symbol']:<14} " + "; ".join(parts))
    print(f"  missing on live: {len(comp['missing_on_live'])}" + (" -> " + ", ".join(comp["missing_on_live"]) if comp["missing_on_live"] else ""))
    print(f"  missing on demo: {len(comp['missing_on_demo'])}" + (" -> " + ", ".join(comp["missing_on_demo"]) if comp["missing_on_demo"] else ""))
    print()
    print(f"  cross-check: standing estimate {_fmt_usd(cc['standing_min_capital_usd'])}   "
          f"testnet ceiling {_fmt_usd(cc['frozen_ceiling_testnet_usd'])}   live ceiling {_fmt_usd(cc['frozen_ceiling_live_usd'])}")
    sc = cc["standing_estimate_covers_live_universe"]
    print(f"    live universe worst-case needs {_fmt_usd(cc['live_universe_worst_case_min_budget_usd'])} "
          f"-> standing estimate {'COVERS it' if sc else ('DOES NOT COVER it' if sc is False else 'n/a (live unavailable)')}")
    tc = cc["testnet_ceiling_covers_demo_universe"]
    print(f"    demo universe worst-case needs {_fmt_usd(cc.get('demo_universe_worst_case_min_budget_usd'))} "
          f"-> testnet ceiling {'COVERS it' if tc else ('DOES NOT COVER it' if tc is False else 'n/a (demo unavailable)')}")
    print(f"    live ceiling is zero: {cc['live_ceiling_is_zero']} (live stays structurally locked until v2 ceilings)")


def run(fetch: FetchFn = fetch_json, ledger: Path = LEDGER, report_path: Path = REPORT,
        universe: tuple[list[str], list[str]] | None = None, ceilings: dict[str, Any] | None = None) -> dict[str, Any]:
    main, holdout = universe if universe is not None else strategy_universe()
    book = latest_ledger_book(ledger)
    wanted = list(dict.fromkeys(main + holdout + book["longs"] + book["shorts"]))
    venues = {name: fetch_venue(url, wanted, fetch) for name, url in VENUES.items()}
    rep = build_report(main, holdout, book, venues, ceilings if ceilings is not None else _ceilings())
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(rep, indent=2, sort_keys=True), encoding="utf-8")
    return rep


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json-only", action="store_true", help="write the report, print only its path")
    args = ap.parse_args()
    rep = run()
    if not args.json_only:
        print_table(rep)
    print(f"  wrote {REPORT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
