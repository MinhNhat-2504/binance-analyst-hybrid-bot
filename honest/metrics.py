"""Positioning metrics from Binance's public daily archive.

WHY THIS FILE EXISTS. Two of the three cells registered in RESEARCH_PREREG_Q4_2026.md need
data that funding cannot see: how much leverage is outstanding (open interest) and WHO is
on each side (top-trader position ratio vs. retail account headcount). Binance publishes
both, but the REST endpoints under /futures/data serve only the trailing 30 days - too
short for a 600-day panel, and the reason collect_daily_snapshots.py exists at all. The
daily archive at data.binance.vision keeps the same series back to 2021-12-01 (or listing),
one zip per symbol-day, and it is free and keyless.

WHAT THIS FILE IS NOT. A loader. It fetches, caches and reshapes; it computes no weights,
no returns, no Sharpe, nothing that could be read as a result. The pre-registration forbids
running any Q4 cell before 2026-10-01, and building the pipe in September only helps if
looking through it stays off-limits. Keep evaluation in the cell scripts where the null,
the hold-out and the decision rule live with it.

SHAPE OF THE SOURCE. Each zip holds one CSV of 5-minute rows for one UTC day:

    create_time, symbol, sum_open_interest, sum_open_interest_value,
    count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio,
    count_long_short_ratio, sum_taker_long_short_vol_ratio

The daily row this module returns is the LAST 5-minute snapshot of the day plus the day's
mean of every column. Last, not 23:55: the files observed on 2026-09-04 end at 23:50, so a
hardcoded timestamp would silently drop every day. Both the end-of-day value and the mean
are kept because the pre-registration's signal is the end-of-day snapshot while the mean is
the obvious robustness variant - having it cached costs nothing and choosing between them
after seeing results would void the cell.

CACHING. One parquet per symbol holding every day ever fetched, not one per (symbol,
window) as in data.py. 75 symbols x 600 days is ~45,000 small downloads; a cache keyed by
window would repeat all of it the first time a script asks for 601 days. A sidecar json
records days the archive answered 404 for (before listing, or not yet published) so those
are never re-requested.

    python -m honest.metrics --warm --days 600     # fill the cache, ~45-90 min once
    python -m honest.metrics --verify BTCUSDT      # archive vs the 30-day REST endpoint
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .data import CACHE_DIR, _get

ARCHIVE = "https://data.binance.vision/data/futures/um/daily/metrics"
REST_OI = "https://fapi.binance.com/futures/data/openInterestHist"

# Source column -> the name used everywhere downstream. Renamed once, here, so a cell
# never has to remember that "count_" means accounts and "sum_" means positions.
COLUMNS = {
    "sum_open_interest": "open_interest",
    "sum_open_interest_value": "open_interest_value",
    "sum_toptrader_long_short_ratio": "toptrader_position_ratio",
    "count_toptrader_long_short_ratio": "toptrader_account_ratio",
    "count_long_short_ratio": "global_account_ratio",
    "sum_taker_long_short_vol_ratio": "taker_buy_sell_ratio",
}
FIELDS = tuple(COLUMNS.values())

# A complete UTC day is 288 five-minute rows. Days with fewer are kept but flagged via
# n_rows so a cell can decide - silently dropping them here would hide an outage.
ROWS_PER_FULL_DAY = 288


def _cache_paths(symbol: str) -> tuple[Path, Path]:
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / f"METRICS_{symbol}.parquet", CACHE_DIR / f"METRICS_{symbol}.absent.json"


def _load_absent(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        return set()


def _save_absent(path: Path, days: set[str]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(days)), encoding="utf-8")
    tmp.replace(path)


def fetch_metrics_day(symbol: str, day: date, retries: int = 3) -> pd.DataFrame | None:
    """One symbol-day of 5-minute rows, or None when the archive has no such file.

    None means 404 - the day is before listing, or today's file is not published yet.
    That is data, not failure. Anything else raises after the retries.
    """
    url = f"{ARCHIVE}/{symbol}/{symbol}-metrics-{day.isoformat()}.zip"
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=45) as resp:
                raw = resp.read()
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                return None
            last = exc
            time.sleep(1.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    else:
        raise RuntimeError(f"{symbol} {day}: archive fetch failed after {retries} tries: {last}")

    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        frame = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])))
    frame["create_time"] = pd.to_datetime(frame["create_time"], utc=True)
    return frame.sort_values("create_time")


def _daily_row(frame: pd.DataFrame, day: date) -> dict[str, float]:
    """End-of-day snapshot plus the day's mean, for every metric column."""
    present = [c for c in COLUMNS if c in frame.columns]
    numeric = frame[present].apply(pd.to_numeric, errors="coerce")
    eod = numeric.iloc[-1]
    mean = numeric.mean()
    row: dict[str, float] = {"day": day.isoformat(), "n_rows": float(len(frame))}
    for src, name in COLUMNS.items():
        row[name] = float(eod[src]) if src in present else float("nan")
        row[f"{name}_mean"] = float(mean[src]) if src in present else float("nan")
    return row


def _missing_days(cached: pd.DataFrame | None, absent: set[str], wanted: list[date]) -> list[date]:
    have = set(cached["day"]) if cached is not None and len(cached) else set()
    return [d for d in wanted if d.isoformat() not in have and d.isoformat() not in absent]


def fetch_metrics(symbol: str, days_back: int = 600, *, use_cache: bool = True,
                  workers: int = 8, verbose: bool = False) -> pd.DataFrame:
    """Daily positioning metrics for one symbol, indexed by UTC date.

    Tops the per-symbol cache up rather than refetching it: only days that are neither
    cached nor known-absent are downloaded. Today is excluded - its file does not exist
    until the day closes, and a half-day row would be a silent lookahead.
    """
    cache_path, absent_path = _cache_paths(symbol)
    cached = pd.read_parquet(cache_path) if use_cache and cache_path.exists() else None
    absent = _load_absent(absent_path) if use_cache else set()

    last_day = datetime.now(timezone.utc).date() - timedelta(days=1)
    wanted = [last_day - timedelta(days=i) for i in range(days_back)][::-1]
    todo = _missing_days(cached, absent, wanted)

    if todo:
        rows: list[dict[str, float]] = []
        new_absent: set[str] = set()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_metrics_day, symbol, d): d for d in todo}
            for future in as_completed(futures):
                day = futures[future]
                frame = future.result()
                if frame is None or frame.empty:
                    new_absent.add(day.isoformat())
                else:
                    rows.append(_daily_row(frame, day))
        if rows:
            fresh = pd.DataFrame(rows)
            cached = fresh if cached is None else pd.concat([cached, fresh], ignore_index=True)
            cached = cached.drop_duplicates("day", keep="last").sort_values("day")
            cached.to_parquet(cache_path, index=False)
        if new_absent:
            _save_absent(absent_path, absent | new_absent)
        if verbose:
            print(f"  {symbol}: +{len(rows)} days, {len(new_absent)} absent, {len(cached) if cached is not None else 0} cached")

    if cached is None or cached.empty:
        return pd.DataFrame(columns=["day", "n_rows", *FIELDS])
    out = cached.copy()
    out["day"] = pd.to_datetime(out["day"])
    keep = out["day"].dt.date >= wanted[0]
    return out.loc[keep].set_index("day").sort_index()


def build_metrics_panel(symbols: list[str], field: str = "open_interest", days_back: int = 600,
                        *, min_days: int = 400, use_cache: bool = True, workers: int = 8,
                        verbose: bool = True) -> pd.DataFrame:
    """Wide date x symbol panel of one metric. Plumbing only - no signal is built here.

    Symbols with fewer than min_days of history are dropped, the same rule build_panel
    applies to prices, so a cell cannot accidentally run on a handful of new listings.
    """
    if field not in FIELDS and not field.endswith("_mean"):
        raise ValueError(f"unknown field {field!r}; expected one of {FIELDS} (optionally + '_mean')")
    series: dict[str, pd.Series] = {}
    for i, symbol in enumerate(symbols, 1):
        frame = fetch_metrics(symbol, days_back, use_cache=use_cache, workers=workers)
        if field not in frame.columns:
            continue
        column = frame[field].dropna()
        if len(column) >= min_days:
            series[symbol] = column
        elif verbose:
            print(f"  skip {symbol}: {len(column)} days < {min_days}")
        if verbose and i % 10 == 0:
            print(f"  ...{i}/{len(symbols)} symbols")
    if not series:
        return pd.DataFrame()
    panel = pd.DataFrame(series).sort_index()
    if verbose:
        print(f"panel {field}: {panel.shape[0]} days x {panel.shape[1]} symbols "
              f"({panel.index.min().date()} -> {panel.index.max().date()})")
    return panel


def verify_against_rest(symbol: str, days: int = 30, tolerance_pct: float = 1.0) -> dict[str, object]:
    """Day-1 check the pre-registration demands: is the archive the same series as live REST?

    The cells are built on the archive but will one day trade on what the live endpoint
    reports, so the two must agree before any of it means anything. Compares end-of-day
    open interest against /futures/data/openInterestHist (period=1d, 30-day cap).
    Pre-registered kill: disagreement above 1% on more than 5% of the overlapping days.

    THE ONE-DAY CONVENTION, measured 2026-09-04 and the reason this function exists.
    REST stamps a 1d row at 00:00 of day D and reports open interest AT THAT INSTANT,
    which is the same moment as the archive's last row of day D-1. Lined up that way the
    two series match to 0.00%; lined up naively by date they disagree by 1-2% typical and
    5-10% on a busy day, which reads exactly like a corrupt data source and would have
    killed two Q4 cells for nothing.

        archive end-of-day[D]  ==  REST timestamp[D+1]

    collect_daily_snapshots.py stores the REST value under D (the state at 00:00 of D) and
    says so in its header. Both labels are right about the same instant, so the two sources
    must never be joined on the raw date - shift one of them first. The panel here uses the
    archive's label because that is the harness convention: a daily close belongs to its own
    day and is traded at the next open.
    """
    rest = _get(f"{REST_OI}?symbol={symbol}&period=1d&limit={days}")
    if not rest:
        return {"symbol": symbol, "overlap": 0, "note": "REST returned nothing"}
    live = {
        datetime.fromtimestamp(int(r["timestamp"]) / 1000, tz=timezone.utc).date().isoformat():
            float(r["sumOpenInterest"]) for r in rest
    }
    archive = fetch_metrics(symbol, days_back=days + 5)
    if archive.empty:
        return {"symbol": symbol, "overlap": 0, "note": "archive empty"}
    arch = {d.date().isoformat(): v for d, v in archive["open_interest"].items()}

    diffs = []
    for day, archive_value in sorted(arch.items()):
        rest_value = live.get((date.fromisoformat(day) + timedelta(days=1)).isoformat())
        if archive_value and rest_value:
            diffs.append((day, abs(archive_value / rest_value - 1.0) * 100.0))
    if not diffs:
        return {"symbol": symbol, "overlap": 0, "note": "no overlapping days"}
    bad = [d for d, pct in diffs if pct > tolerance_pct]
    worst_day, worst_pct = max(diffs, key=lambda x: x[1])
    return {
        "symbol": symbol, "overlap": len(diffs), "median_diff_pct": round(sorted(p for _, p in diffs)[len(diffs) // 2], 4),
        "worst_day": worst_day, "worst_diff_pct": round(worst_pct, 4),
        "days_over_tolerance": len(bad), "share_over_tolerance_pct": round(100.0 * len(bad) / len(diffs), 2),
        "passes_prereg_kill": len(bad) / len(diffs) <= 0.05,
    }


def _universe() -> list[str]:
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    from run_carry_holdout import HOLDOUT_UNIVERSE  # noqa: E402
    from run_daily_lab import UNIVERSE  # noqa: E402
    return sorted(set(UNIVERSE) | set(HOLDOUT_UNIVERSE))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--warm", action="store_true", help="download and cache the archive for the whole universe")
    ap.add_argument("--verify", nargs="*", metavar="SYMBOL", help="compare the archive against the 30-day REST endpoint")
    ap.add_argument("--days", type=int, default=600)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--symbols", default="", help="comma-separated override of the universe")
    args = ap.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or _universe()

    if args.verify is not None:
        targets = [s.upper() for s in args.verify] or symbols[:5]
        worst = 0.0
        for symbol in targets:
            r = verify_against_rest(symbol)
            print(f"  {symbol:14s} {r}")
            if isinstance(r.get("share_over_tolerance_pct"), float):
                worst = max(worst, r["share_over_tolerance_pct"])
        print(f"\npre-registered kill is >5% of symbol-days over 1%; worst here {worst:.2f}%")
        return 0 if worst <= 5.0 else 1

    if args.warm:
        started = time.time()
        total = 0
        for i, symbol in enumerate(symbols, 1):
            frame = fetch_metrics(symbol, args.days, workers=args.workers)
            total += len(frame)
            print(f"[{i:>2}/{len(symbols)}] {symbol:14s} {len(frame):>4} days"
                  f"{'' if frame.empty else '  ' + str(frame.index.min().date()) + ' -> ' + str(frame.index.max().date())}"
                  f"   ({time.time() - started:.0f}s)")
        print(f"\n{len(symbols)} symbols, {total} symbol-days cached in {(time.time() - started) / 60:.1f} min")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
