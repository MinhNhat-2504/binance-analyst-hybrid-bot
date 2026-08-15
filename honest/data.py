"""Kline fetching with on-disk cache.

Public Binance futures endpoint - no API key needed for klines.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd

FAPI = "https://fapi.binance.com/fapi/v1/klines"
CACHE_DIR = Path(__file__).resolve().parent.parent / ".honest_cache"

KLINE_COLS = [
    "Open time", "Open", "High", "Low", "Close", "Volume", "Close time",
    "Quote Asset", "Trades", "Taker Buy Base", "Taker Buy Quote", "Ignore",
]
NUMERIC_COLS = ["Open", "High", "Low", "Close", "Volume", "Taker Buy Base", "Taker Buy Quote", "Trades"]

_MS_PER_MIN = 60_000
INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "8h": 480, "1d": 1440}


def _get(url: str, retries: int = 5) -> list:
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Binance request failed after {retries} tries: {last}")


def fetch_klines(symbol: str, interval: str = "15m", days_back: int = 720,
                 use_cache: bool = True) -> pd.DataFrame:
    """Fetch klines, paginating forward. Cached per (symbol, interval, days_back)."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache = CACHE_DIR / f"{symbol}_{interval}_{days_back}d.parquet"
    if use_cache and cache.exists():
        return pd.read_parquet(cache)

    step = INTERVAL_MINUTES[interval] * _MS_PER_MIN
    now_ms = int(time.time() * 1000)
    start = now_ms - days_back * 24 * 60 * _MS_PER_MIN
    rows: list[list] = []

    while start < now_ms:
        batch = _get(f"{FAPI}?symbol={symbol}&interval={interval}&startTime={start}&limit=1500")
        if not batch:
            break
        rows.extend(batch)
        nxt = batch[-1][0] + step
        if nxt <= start:  # no forward progress -> stop rather than spin
            break
        start = nxt
        time.sleep(0.25)  # stay well under the weight limit

    if not rows:
        return pd.DataFrame(columns=KLINE_COLS)

    df = pd.DataFrame(rows, columns=KLINE_COLS)
    df = df.drop_duplicates(subset="Open time").sort_values("Open time").reset_index(drop=True)
    df["Open time"] = pd.to_datetime(df["Open time"], unit="ms")
    df["Close time"] = pd.to_datetime(df["Close time"], unit="ms")
    for col in NUMERIC_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Drop the final bar: it is still forming, so High/Low/Close are not settled
    # and any label built on it would be wrong.
    df = df.iloc[:-1].reset_index(drop=True)

    if use_cache:
        df.to_parquet(cache, index=False)
    return df


def load_universe(symbols: list[str], interval: str = "15m", days_back: int = 720,
                  use_cache: bool = True, verbose: bool = True) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            df = fetch_klines(sym, interval, days_back, use_cache)
            if len(df) < 500:
                if verbose:
                    print(f"  skip {sym}: only {len(df)} bars")
                continue
            out[sym] = df
            if verbose:
                span = f"{df['Open time'].iloc[0]:%Y-%m-%d} -> {df['Open time'].iloc[-1]:%Y-%m-%d}"
                print(f"  {sym:14s} {len(df):6,d} bars  {span}")
        except Exception as exc:
            if verbose:
                print(f"  FAIL {sym}: {exc}")
    return out
