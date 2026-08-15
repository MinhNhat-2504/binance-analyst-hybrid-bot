"""Point-in-time public market-data loading for the clean research pipeline."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

from honest.data import CACHE_DIR, fetch_klines


FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"


def _get_json(url: str, retries: int = 5) -> list[dict]:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                payload = json.loads(response.read())
            if isinstance(payload, dict) and "code" in payload:
                raise RuntimeError(f"Binance error {payload}")
            return payload
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError) as exc:
            last = exc
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"funding request failed after {retries} attempts: {last}")


def fetch_funding_rates(symbol: str, days_back: int = 630, *, use_cache: bool = True) -> pd.DataFrame:
    """Fetch settled funding rates in ascending time order.

    Funding settlement time is its availability time.  The ledger builder only joins
    observations at or before the decision timestamp and separately sums rates strictly
    inside the realised holding interval.
    """

    CACHE_DIR.mkdir(exist_ok=True)
    cache = CACHE_DIR / f"{symbol}_funding_{days_back}d.parquet"
    now_ms = int(time.time() * 1000)
    window_start = now_ms - days_back * 86_400_000
    cached = pd.DataFrame(columns=["fundingTime", "fundingRate"])
    if use_cache and cache.exists():
        cached = pd.read_parquet(cache)
        if not cached.empty:
            cached = cached[["fundingTime", "fundingRate"]].copy()
            cached["fundingTime"] = pd.to_datetime(cached["fundingTime"], utc=True).dt.tz_localize(None)
            cached["fundingRate"] = pd.to_numeric(cached["fundingRate"], errors="coerce")
            cached = cached.dropna().drop_duplicates("fundingTime").sort_values("fundingTime")
            latest_ms = int(cached["fundingTime"].max().timestamp() * 1000)
            # A settled 8h stream can legitimately be up to one interval old.  Beyond
            # 12h the cache is stale and must be incrementally extended.
            if now_ms - latest_ms <= 12 * 3_600_000:
                return cached[cached["fundingTime"] >= pd.to_datetime(window_start, unit="ms")].reset_index(drop=True)
            start = max(window_start, latest_ms + 1)
        else:
            start = window_start
    else:
        start = window_start
    rows: list[dict] = []
    while start < now_ms:
        query = urllib.parse.urlencode(
            {"symbol": symbol, "startTime": start, "endTime": now_ms, "limit": 1000}
        )
        batch = _get_json(f"{FUNDING_URL}?{query}")
        if not batch:
            break
        rows.extend(batch)
        nxt = int(batch[-1]["fundingTime"]) + 1
        if nxt <= start:
            break
        start = nxt
        if len(batch) < 1000:
            break
        time.sleep(0.15)

    fresh = pd.DataFrame(rows)
    if not fresh.empty:
        fresh["fundingTime"] = pd.to_datetime(fresh["fundingTime"], unit="ms")
        fresh["fundingRate"] = pd.to_numeric(fresh["fundingRate"], errors="coerce")
        fresh = fresh[["fundingTime", "fundingRate"]]
    else:
        fresh = pd.DataFrame(columns=["fundingTime", "fundingRate"])
    out = pd.concat([cached, fresh], ignore_index=True)
    if out.empty:
        return out
    out = out.dropna().drop_duplicates("fundingTime")
    out = out[out["fundingTime"] >= pd.to_datetime(window_start, unit="ms")]
    out = out.sort_values("fundingTime").reset_index(drop=True)
    if use_cache:
        out.to_parquet(cache, index=False)
    return out


def load_daily_bundle(
    symbols: list[str],
    *,
    days_back: int = 600,
    funding_days: int = 630,
    as_of: pd.Timestamp | str | None = None,
    use_cache: bool = True,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """Load daily futures bars and funding with a common point-in-time cutoff."""

    cutoff = pd.Timestamp(as_of) if as_of is not None else None
    if cutoff is not None and cutoff.tzinfo is not None:
        cutoff = cutoff.tz_convert("UTC").tz_localize(None)
    bars: dict[str, pd.DataFrame] = {}
    funding: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        daily = fetch_klines(symbol, "1d", days_back, use_cache=use_cache)
        rates = fetch_funding_rates(symbol, funding_days, use_cache=use_cache)
        if cutoff is not None:
            daily_times = pd.to_datetime(daily["Close time"], utc=True).dt.tz_localize(None)
            funding_times = pd.to_datetime(rates["fundingTime"], utc=True).dt.tz_localize(None)
            daily = daily[daily_times <= cutoff].copy()
            rates = rates[funding_times <= cutoff].copy()
        if len(daily) >= 120 and len(rates) >= 30:
            bars[symbol] = daily.reset_index(drop=True)
            funding[symbol] = rates.reset_index(drop=True)
    return bars, funding


def snapshot_hash(bars: dict[str, pd.DataFrame], funding: dict[str, pd.DataFrame]) -> str:
    """Stable digest for the exact point-in-time rows used by an experiment."""

    digest = hashlib.sha256()
    for symbol in sorted(bars):
        digest.update(symbol.encode())
        d = bars[symbol]
        cols = ["Open time", "Open", "High", "Low", "Close", "Volume", "Close time"]
        digest.update(pd.util.hash_pandas_object(d[cols], index=False).values.tobytes())
        f = funding.get(symbol, pd.DataFrame(columns=["fundingTime", "fundingRate"]))
        digest.update(pd.util.hash_pandas_object(f, index=False).values.tobytes())
    return digest.hexdigest()
