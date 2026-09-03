"""Daily collector of Binance USDT-M positioning data that the public API forgets after ~30 days.

WHY. Open interest, top-trader long/short ratios, global account ratios and taker buy/sell
volume are free on fapi.binance.com, but only the trailing ~30 days are served. Nobody can
download 2026-Q3 positioning in 2027. Running this once a day, unattended, makes the project
the owner of a history that the Q4 research (crowding, basis, OI-conditioned carry) will
need and could not otherwise buy. It reads public endpoints only - no API key, no orders.

    python collect_daily_snapshots.py                       # normal daily run (idempotent)
    python collect_daily_snapshots.py --dry-run             # fetch ONE symbol, print, write nothing
    python collect_daily_snapshots.py --symbols BTCUSDT,ETHUSDT

Universe: the discovery set (run_daily_lab.UNIVERSE) plus the hold-out set
(run_carry_holdout.HOLDOUT_UNIVERSE), ~75 perps.

Storage: data_snapshots/YYYY-MM-DD.csv, long format  day,symbol,field,value  (one file per
UTC day, written atomically: temp file then os.replace), and data_snapshots/manifest.json
recording, per day and per source, which symbols are already stored. A (day, symbol, source)
triple is fetched once and never again; a run that finds nothing missing makes no calls.

Sources and fields (all values are the raw API strings, numeric where the API is numeric):
  oi            oi_contracts, oi_value_usdt, cmc_circulating_supply    (openInterestHist, 1d)
  top_pos       top_pos_ls_ratio, top_pos_long_share, top_pos_short_share
                                                       (topLongShortPositionRatio, 1d)
  top_acct      top_acct_ls_ratio, top_acct_long_share, top_acct_short_share
                                                       (topLongShortAccountRatio, 1d)
  global_acct   global_acct_ls_ratio, global_acct_long_share, global_acct_short_share
                                                       (globalLongShortAccountRatio, 1d)
  taker         taker_buy_sell_ratio, taker_buy_vol, taker_sell_vol   (takerlongshortRatio, 1d)
  premium       mark_price, index_price, last_funding_rate, snapshot_ms   (premiumIndex, live)
  spot          spot_price, spot_multiplier   (api.binance.com ticker/price, live; the
                1000XXX perps map to XXX spot with multiplier 1000; absent spot = no rows)

Day semantics. History rows carry Binance's 1d bar timestamp (00:00 UTC): the OI and ratio
rows are the state at that instant, the taker row is the volume over that day. Only days
strictly before the run's UTC date are stored from history endpoints (the current day's bar
is still forming for taker volume), so day D's history rows land on D+1. The premium and
spot rows are live snapshots labelled with the run's UTC date; the scheduled run happens
just after 00:00 UTC, so they line up with the 00:00 history rows of the same day.

Exit code 0 on success, 1 when more than 20% of the symbols failed (a partial run still
writes everything it did get; the missing triples are simply retried tomorrow).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data_snapshots"
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1

FAPI = "https://fapi.binance.com"
SPOT = "https://api.binance.com"
LOOKBACK_DAYS = 30          # what the /futures/data endpoints keep for period=1d
CALL_SLEEP_S = 0.08         # between calls, per worker; weight-1 endpoints, 2400/min budget
RETRY_SLEEP_S = 2.0
RATE_LIMIT_SLEEP_S = 6.0
FAIL_FRACTION_LIMIT = 0.20
DEFAULT_WORKERS = 4

CSV_COLUMNS = ["day", "symbol", "field", "value"]

# source -> (endpoint path, {api key: stored field})
HISTORY_SOURCES: dict[str, tuple[str, dict[str, str]]] = {
    "oi": ("/futures/data/openInterestHist", {
        "sumOpenInterest": "oi_contracts",
        "sumOpenInterestValue": "oi_value_usdt",
        "CMCCirculatingSupply": "cmc_circulating_supply",
    }),
    "top_pos": ("/futures/data/topLongShortPositionRatio", {
        "longShortRatio": "top_pos_ls_ratio",
        "longAccount": "top_pos_long_share",
        "shortAccount": "top_pos_short_share",
    }),
    "top_acct": ("/futures/data/topLongShortAccountRatio", {
        "longShortRatio": "top_acct_ls_ratio",
        "longAccount": "top_acct_long_share",
        "shortAccount": "top_acct_short_share",
    }),
    "global_acct": ("/futures/data/globalLongShortAccountRatio", {
        "longShortRatio": "global_acct_ls_ratio",
        "longAccount": "global_acct_long_share",
        "shortAccount": "global_acct_short_share",
    }),
    "taker": ("/futures/data/takerlongshortRatio", {
        "buySellRatio": "taker_buy_sell_ratio",
        "buyVol": "taker_buy_vol",
        "sellVol": "taker_sell_vol",
    }),
}
PREMIUM_FIELDS = {"markPrice": "mark_price", "indexPrice": "index_price",
                  "lastFundingRate": "last_funding_rate", "time": "snapshot_ms"}
LIVE_SOURCES = ("premium", "spot")
ALL_SOURCES = tuple(HISTORY_SOURCES) + LIVE_SOURCES

FIELD_TO_SOURCE: dict[str, str] = {}
for _src, (_path, _map) in HISTORY_SOURCES.items():
    for _field in _map.values():
        FIELD_TO_SOURCE[_field] = _src
for _field in PREMIUM_FIELDS.values():
    FIELD_TO_SOURCE[_field] = "premium"
FIELD_TO_SOURCE["spot_price"] = "spot"
FIELD_TO_SOURCE["spot_multiplier"] = "spot"

Row = tuple[str, str, str, str]  # day, symbol, field, value


class FetchError(Exception):
    """One failed HTTP call; ``code`` is the HTTP status or None for transport errors."""

    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code


# ----------------------------------------------------------------------------- universe

def default_universe() -> list[str]:
    from run_carry_holdout import HOLDOUT_UNIVERSE
    from run_daily_lab import UNIVERSE
    return sorted(set(UNIVERSE) | set(HOLDOUT_UNIVERSE))


def spot_symbol(perp: str) -> tuple[str, int]:
    """1000PEPEUSDT -> (PEPEUSDT, 1000); BTCUSDT -> (BTCUSDT, 1)."""
    if perp.startswith("1000") and len(perp) > 4 and not perp[4].isdigit():
        return perp[4:], 1000
    return perp, 1


# ----------------------------------------------------------------------------- network

def fetch_json(url: str, timeout: float = 20.0) -> Any:
    """The single network edge (tests monkeypatch this)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} {url}", exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise FetchError(f"{type(exc).__name__}: {exc} {url}") from exc


def _get(url: str, sleep: Callable[[float], None] = time.sleep) -> Any:
    """One retry on 429 / 5xx / transport errors; 4xx other than 429 surface immediately."""
    for attempt in (0, 1):
        try:
            data = fetch_json(url)
            sleep(CALL_SLEEP_S)
            return data
        except FetchError as exc:
            retryable = exc.code is None or exc.code == 429 or exc.code >= 500
            if attempt == 1 or not retryable:
                raise
            sleep(RATE_LIMIT_SLEEP_S if exc.code == 429 else RETRY_SLEEP_S)
    raise AssertionError("unreachable")


def _url(base: str, path: str, **params: Any) -> str:
    return f"{base}{path}?{urllib.parse.urlencode(params)}"


def _ms_to_day(ms: Any) -> str:
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc).date().isoformat()


# ----------------------------------------------------------------------------- planning

def window_days(today: date, lookback: int = LOOKBACK_DAYS) -> list[str]:
    """History days eligible for storage: [today-(lookback-1), today-1].

    The API's ``limit=30`` returns 30 bars INCLUDING today's forming bar, so 29 completed
    days is all it can deliver; asking for a 30th would leave a permanently-missing day
    that every run re-requests.
    """
    return [(today - timedelta(days=k)).isoformat() for k in range(lookback - 1, 0, -1)]


def plan_needed(manifest: dict, symbol: str, today: date,
                lookback: int = LOOKBACK_DAYS) -> dict[str, set[str]]:
    """Per source, the days this symbol is still missing. Empty dict = nothing to fetch."""
    days = manifest.get("days", {})

    def stored(day: str, source: str) -> bool:
        return symbol in days.get(day, {}).get("sources", {}).get(source, [])

    needed: dict[str, set[str]] = {}
    for source in HISTORY_SOURCES:
        missing = {d for d in window_days(today, lookback) if not stored(d, source)}
        if missing:
            needed[source] = missing
    t = today.isoformat()
    for source in LIVE_SOURCES:
        if not stored(t, source):
            needed[source] = {t}
    return needed


# ----------------------------------------------------------------------------- collecting

def collect_symbol(symbol: str, needed: dict[str, set[str]], today: date,
                   lookback: int = LOOKBACK_DAYS,
                   sleep: Callable[[float], None] = time.sleep) -> tuple[list[Row], list[str]]:
    """Fetch every needed (source, day) for one symbol. Returns (rows, error strings)."""
    rows: list[Row] = []
    errors: list[str] = []
    t = today.isoformat()

    for source, (path, field_map) in HISTORY_SOURCES.items():
        days = needed.get(source)
        if not days:
            continue
        oldest = date.fromisoformat(min(days))
        limit = max(2, min(lookback, (today - oldest).days + 1))
        try:
            data = _get(_url(FAPI, path, symbol=symbol, period="1d", limit=limit), sleep)
        except FetchError as exc:
            errors.append(f"{symbol} {source}: {exc}")
            continue
        if not isinstance(data, list):
            errors.append(f"{symbol} {source}: unexpected payload {str(data)[:80]}")
            continue
        for item in data:
            day = _ms_to_day(item.get("timestamp", 0))
            if day not in days:
                continue
            for api_key, field in field_map.items():
                if api_key in item:
                    rows.append((day, symbol, field, str(item[api_key])))

    if "premium" in needed:
        try:
            data = _get(_url(FAPI, "/fapi/v1/premiumIndex", symbol=symbol), sleep)
            for api_key, field in PREMIUM_FIELDS.items():
                if api_key in data:
                    rows.append((t, symbol, field, str(data[api_key])))
        except FetchError as exc:
            errors.append(f"{symbol} premium: {exc}")

    if "spot" in needed:
        sym, mult = spot_symbol(symbol)
        try:
            data = _get(_url(SPOT, "/api/v3/ticker/price", symbol=sym), sleep)
            rows.append((t, symbol, "spot_price", str(data["price"])))
            rows.append((t, symbol, "spot_multiplier", str(mult)))
        except FetchError as exc:
            if exc.code != 400:        # 400 = no such spot market; that is data, not failure
                errors.append(f"{symbol} spot: {exc}")
        except (KeyError, TypeError) as exc:
            errors.append(f"{symbol} spot: bad payload {exc}")

    return rows, errors


# ----------------------------------------------------------------------------- storage

def write_atomic(path: Path, render: Callable[[Any], None]) -> None:
    """Write via a sibling temp file and os.replace; a failure leaves the old file untouched."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with tmp.open("w", newline="", encoding="utf-8") as fh:
            render(fh)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def read_day_file(path: Path) -> list[Row]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return [(r["day"], r["symbol"], r["field"], r["value"]) for r in reader]


def write_day_file(path: Path, rows: list[Row]) -> None:
    def render(fh: Any) -> None:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        w.writerows(rows)
    write_atomic(path, render)


def merge_rows(existing: list[Row], new: list[Row]) -> list[Row]:
    """Union keyed on (day, symbol, field); new values win; deterministic order."""
    merged = {(d, s, f): v for d, s, f, v in existing}
    merged.update({(d, s, f): v for d, s, f, v in new})
    return sorted((d, s, f, v) for (d, s, f), v in merged.items())


def empty_manifest() -> dict:
    return {"version": MANIFEST_VERSION, "last_run_utc": None, "universe_size": 0, "days": {}}


def manifest_from_files(data_dir: Path) -> dict:
    """Rebuild the manifest from the day files (used when manifest.json is absent)."""
    m = empty_manifest()
    for path in sorted(data_dir.glob("????-??-??.csv")):
        rows = read_day_file(path)
        if rows:
            _index_rows(m, rows)
            _finalize_day(m, path.stem, rows)
    return m


def load_manifest(data_dir: Path) -> dict:
    path = data_dir / MANIFEST_NAME
    if not path.exists():
        return manifest_from_files(data_dir) if data_dir.exists() else empty_manifest()
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(m, dict) or not isinstance(m.get("days"), dict):
            raise ValueError("bad manifest shape")
        return m
    except (ValueError, OSError) as exc:
        print(f"  manifest unreadable ({exc}); rebuilding from day files", flush=True)
        return manifest_from_files(data_dir)


def _index_rows(manifest: dict, rows: list[Row]) -> None:
    """Record (day, source, symbol) triples present in ``rows`` into the manifest."""
    for day, symbol, field, _ in rows:
        source = FIELD_TO_SOURCE.get(field)
        if source is None:
            continue
        entry = manifest["days"].setdefault(day, {"sources": {}, "symbols": [], "n_rows": 0})
        syms = entry["sources"].setdefault(source, [])
        if symbol not in syms:
            syms.append(symbol)
            syms.sort()


def _finalize_day(manifest: dict, day: str, rows: list[Row]) -> None:
    entry = manifest["days"].setdefault(day, {"sources": {}, "symbols": [], "n_rows": 0})
    entry["symbols"] = sorted({s for _, s, _, _ in rows})
    entry["n_rows"] = len(rows)
    for src in entry["sources"]:
        entry["sources"][src].sort()


def save_manifest(data_dir: Path, manifest: dict) -> None:
    manifest["days"] = dict(sorted(manifest["days"].items()))

    def render(fh: Any) -> None:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    write_atomic(data_dir / MANIFEST_NAME, render)


def store_rows(data_dir: Path, manifest: dict, rows: list[Row]) -> dict[str, int]:
    """Merge rows into their day files and the manifest. Returns {day: rows added}."""
    by_day: dict[str, list[Row]] = {}
    for row in rows:
        by_day.setdefault(row[0], []).append(row)
    added: dict[str, int] = {}
    for day in sorted(by_day):
        path = data_dir / f"{day}.csv"
        before = read_day_file(path)
        merged = merge_rows(before, by_day[day])
        write_day_file(path, merged)
        _index_rows(manifest, by_day[day])
        _finalize_day(manifest, day, merged)
        added[day] = len(merged) - len(before)
    return added


# ----------------------------------------------------------------------------- run

def run(symbols: list[str], data_dir: Path = DATA_DIR, today: date | None = None,
        dry_run: bool = False, workers: int = DEFAULT_WORKERS,
        lookback: int = LOOKBACK_DAYS, sleep: Callable[[float], None] = time.sleep) -> int:
    t0 = time.time()
    today = today or datetime.now(timezone.utc).date()
    symbols = sorted(dict.fromkeys(symbols))
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    if dry_run:
        sym = symbols[0]
        rows, errors = collect_symbol(sym, plan_needed(empty_manifest(), sym, today, lookback),
                                      today, lookback, sleep)
        for row in rows:
            print(",".join(row))
        for err in errors:
            print("  ERROR", err)
        print(f"[snapshots {today}] DRY RUN {sym}: {len(rows)} rows, {len(errors)} errors, "
              f"{time.time() - t0:.1f}s, nothing written", flush=True)
        return 0 if not errors else 1

    manifest = load_manifest(data_dir)
    plans = {s: plan_needed(manifest, s, today, lookback) for s in symbols}
    todo = [s for s in symbols if plans[s]]
    skipped = len(symbols) - len(todo)
    print(f"[snapshots {today}] universe {len(symbols)}: {len(todo)} to fetch, "
          f"{skipped} already complete, window {lookback}d, workers {workers}", flush=True)

    def work(sym: str) -> tuple[str, list[Row], list[str]]:
        rows, errors = collect_symbol(sym, plans[sym], today, lookback, sleep)
        return sym, rows, errors

    all_rows: list[Row] = []
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for sym, rows, errors in pool.map(work, todo):
            all_rows.extend(rows)
            for err in errors:
                print("  ERROR", err, flush=True)
            if errors:
                failed.append(sym)

    added = store_rows(data_dir, manifest, all_rows) if all_rows else {}
    manifest["last_run_utc"] = stamp
    manifest["universe_size"] = len(symbols)
    manifest["last_run"] = {"today": today.isoformat(), "symbols": len(symbols), "fetched": len(todo),
                            "failed": sorted(failed), "rows_added": sum(added.values())}
    save_manifest(data_dir, manifest)

    n_added = sum(added.values())
    print(f"[snapshots {today}] ok {len(todo) - len(failed)} failed {len(failed)} skipped {skipped} "
          f"| days touched {len(added)} | rows +{n_added} | {time.time() - t0:.1f}s "
          f"| {len(manifest['days'])} days on disk", flush=True)
    if failed and len(failed) > FAIL_FRACTION_LIMIT * len(symbols):
        print(f"  FAIL: {len(failed)}/{len(symbols)} symbols failed (> {FAIL_FRACTION_LIMIT:.0%})", flush=True)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="fetch one symbol, print, write nothing")
    ap.add_argument("--symbols", default="", help="comma list, e.g. BTCUSDT,ETHUSDT (default: full universe)")
    ap.add_argument("--data-dir", default=str(DATA_DIR))
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--lookback", type=int, default=LOOKBACK_DAYS, help="history days to keep in the window")
    args = ap.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or default_universe()
    return run(symbols, Path(args.data_dir), dry_run=args.dry_run, workers=args.workers,
               lookback=args.lookback)


if __name__ == "__main__":
    sys.exit(main())
