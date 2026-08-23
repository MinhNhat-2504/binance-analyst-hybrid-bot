"""Bybit and OKX perp data, shaped exactly like the Binance panels in honest.daily.

Two questions this enables, both pre-registered in run_crossex_lab.py:

  A. UNIVERSALITY - does the CARRY-7d rule, unchanged, produce the same edge when fed
     Bybit's funding and Bybit's prices? OKX's? An edge that exists on one venue only is
     a venue quirk (or an artefact), not a market phenomenon. This is the strongest
     hold-out available: different exchange, different participants, different funding
     formula, same rule.

  B. CROSS-EXCHANGE FUNDING SPREAD - for the SAME coin, perps on different venues settle
     different funding. Short the perp where funding is highest, long the perp where it is
     lowest: price risk cancels within the coin (it is the same asset), what remains is the
     funding differential plus a small perp-perp basis wobble. Two perp legs, no spot fee.
     Information that does not exist anywhere in the single-venue data.

All endpoints are public and keyless. Lookahead discipline is honest.daily's.
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from .data import CACHE_DIR

_UA = {"User-Agent": "Mozilla/5.0"}


def _get(url: str, retries: int = 5):
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=30) as r:
                return json.loads(r.read())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"request failed: {url} :: {last}")


# ---------------------------------------------------------------------------
# Symbol mapping: Binance perp name -> venue instrument
# ---------------------------------------------------------------------------

def bybit_symbol(binance_sym: str) -> str:
    return binance_sym                      # Bybit mirrors Binance naming incl. 1000PEPEUSDT


def okx_inst(binance_sym: str) -> str:
    base = binance_sym[:-4]                 # strip USDT
    if base.startswith("1000"):
        base = base[4:]                     # OKX quotes PEPE per coin; returns are scale-free
    return f"{base}-USDT-SWAP"


# ---------------------------------------------------------------------------
# Bybit
# ---------------------------------------------------------------------------

def fetch_bybit_funding(symbol: str, days_back: int = 630, use_cache: bool = True) -> pd.DataFrame:
    cache = CACHE_DIR / f"BYBIT_{symbol}_funding_{days_back}d.parquet"
    if use_cache and cache.exists():
        return pd.read_parquet(cache)
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days_back * 86_400_000
    end = now_ms
    rows = []
    while end > start_ms:
        d = _get(f"https://api.bybit.com/v5/market/funding/history?category=linear&symbol={symbol}"
                 f"&startTime={start_ms}&endTime={end}&limit=200")
        lst = (d.get("result") or {}).get("list") or []
        if not lst:
            break
        rows.extend(lst)
        oldest = min(int(x["fundingRateTimestamp"]) for x in lst)
        if oldest <= start_ms or len(lst) < 200:
            break
        end = oldest - 1
        time.sleep(0.15)
    if not rows:
        return pd.DataFrame(columns=["fundingTime", "fundingRate"])
    df = pd.DataFrame({"fundingTime": pd.to_datetime([int(x["fundingRateTimestamp"]) for x in rows], unit="ms"),
                       "fundingRate": [float(x["fundingRate"]) for x in rows]})
    df = df.drop_duplicates("fundingTime").sort_values("fundingTime").reset_index(drop=True)
    CACHE_DIR.mkdir(exist_ok=True); df.to_parquet(cache, index=False)
    return df


def fetch_bybit_klines_1d(symbol: str, days_back: int = 600, use_cache: bool = True) -> pd.DataFrame:
    cache = CACHE_DIR / f"BYBIT_{symbol}_1d_{days_back}d.parquet"
    if use_cache and cache.exists():
        return pd.read_parquet(cache)
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days_back * 86_400_000
    end = now_ms
    rows = []
    while end > start_ms:
        d = _get(f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval=D"
                 f"&start={start_ms}&end={end}&limit=1000")
        lst = (d.get("result") or {}).get("list") or []
        if not lst:
            break
        rows.extend(lst)
        oldest = min(int(x[0]) for x in lst)
        if oldest <= start_ms or len(lst) < 1000:
            break
        end = oldest - 1
        time.sleep(0.15)
    if not rows:
        return pd.DataFrame(columns=["Open time", "Open", "High", "Low", "Close", "Volume"])
    df = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume", "Turnover"])
    df["Open time"] = pd.to_datetime(df["ts"].astype(np.int64), unit="ms")
    for c in ("Open", "High", "Low", "Close", "Volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.drop_duplicates("Open time").sort_values("Open time").reset_index(drop=True)
    df = df[df["Open time"] < pd.Timestamp.utcnow().tz_localize(None).normalize()]   # drop forming day
    df = df[["Open time", "Open", "High", "Low", "Close", "Volume"]].reset_index(drop=True)
    CACHE_DIR.mkdir(exist_ok=True); df.to_parquet(cache, index=False)
    return df


# ---------------------------------------------------------------------------
# OKX
# ---------------------------------------------------------------------------

def fetch_okx_funding(inst: str, days_back: int = 630, use_cache: bool = True) -> pd.DataFrame:
    cache = CACHE_DIR / f"OKX_{inst}_funding_{days_back}d.parquet"
    if use_cache and cache.exists():
        return pd.read_parquet(cache)
    start_ms = int(time.time() * 1000) - days_back * 86_400_000
    rows, after = [], None
    while True:
        url = f"https://www.okx.com/api/v5/public/funding-rate-history?instId={inst}&limit=100"
        if after:
            url += f"&after={after}"
        d = _get(url)
        lst = d.get("data") or []
        if not lst:
            break
        rows.extend(lst)
        oldest = min(int(x["fundingTime"]) for x in lst)
        if oldest <= start_ms or len(lst) < 100:
            break
        after = oldest
        time.sleep(0.15)
    if not rows:
        return pd.DataFrame(columns=["fundingTime", "fundingRate"])
    df = pd.DataFrame({"fundingTime": pd.to_datetime([int(x["fundingTime"]) for x in rows], unit="ms"),
                       "fundingRate": [float(x["realizedRate"] if x.get("realizedRate") not in (None, "") else x["fundingRate"]) for x in rows]})
    df = df[df["fundingTime"] >= pd.to_datetime(start_ms, unit="ms")]
    df = df.drop_duplicates("fundingTime").sort_values("fundingTime").reset_index(drop=True)
    CACHE_DIR.mkdir(exist_ok=True); df.to_parquet(cache, index=False)
    return df


def fetch_okx_klines_1d(inst: str, days_back: int = 600, use_cache: bool = True) -> pd.DataFrame:
    cache = CACHE_DIR / f"OKX_{inst}_1d_{days_back}d.parquet"
    if use_cache and cache.exists():
        return pd.read_parquet(cache)
    start_ms = int(time.time() * 1000) - days_back * 86_400_000
    rows, after = [], None
    while True:
        url = f"https://www.okx.com/api/v5/market/history-candles?instId={inst}&bar=1Dutc&limit=100"
        if after:
            url += f"&after={after}"
        d = _get(url)
        lst = d.get("data") or []
        if not lst:
            break
        rows.extend(lst)
        oldest = min(int(x[0]) for x in lst)
        if oldest <= start_ms or len(lst) < 100:
            break
        after = oldest
        time.sleep(0.15)
    if not rows:
        return pd.DataFrame(columns=["Open time", "Open", "High", "Low", "Close", "Volume"])
    df = pd.DataFrame([r[:6] for r in rows], columns=["ts", "Open", "High", "Low", "Close", "Volume"])
    df["Open time"] = pd.to_datetime(df["ts"].astype(np.int64), unit="ms")
    for c in ("Open", "High", "Low", "Close", "Volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.drop_duplicates("Open time").sort_values("Open time").reset_index(drop=True)
    df = df[df["Open time"] >= pd.to_datetime(start_ms, unit="ms")]
    df = df[df["Open time"] < pd.Timestamp.utcnow().tz_localize(None).normalize()]
    df = df[["Open time", "Open", "High", "Low", "Close", "Volume"]].reset_index(drop=True)
    CACHE_DIR.mkdir(exist_ok=True); df.to_parquet(cache, index=False)
    return df


# ---------------------------------------------------------------------------
# Panels, shaped like honest.daily.build_panel: (px closes, fday per-day funding sums)
# ---------------------------------------------------------------------------

def build_venue_panel(venue: str, binance_symbols: list[str], days: int = 600, min_days: int = 400,
                      use_cache: bool = True, verbose: bool = True):
    closes, vols, fund = {}, {}, {}
    for sym in binance_symbols:
        try:
            if venue == "bybit":
                k = fetch_bybit_klines_1d(bybit_symbol(sym), days, use_cache)
                f = fetch_bybit_funding(bybit_symbol(sym), days + 30, use_cache)
            elif venue == "okx":
                k = fetch_okx_klines_1d(okx_inst(sym), days, use_cache)
                f = fetch_okx_funding(okx_inst(sym), days + 30, use_cache)
            else:
                raise ValueError(venue)
            if len(k) < min_days or f.empty:
                if verbose:
                    print(f"  skip {venue}:{sym}: {len(k)}d klines, {len(f)} funding")
                continue
            idx = pd.to_datetime(k["Open time"]).dt.normalize()
            c = k.set_index(idx)["Close"]; v = k.set_index(idx)["Volume"]
            d = ~c.index.duplicated(keep="last")
            closes[sym], vols[sym] = c[d], v[d]
            fund[sym] = f.groupby(f["fundingTime"].dt.normalize())["fundingRate"].sum()
            if verbose:
                print(f"  {venue}:{sym:14s} {len(c):4d}d klines, {len(fund[sym]):4d}d funding")
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"  FAIL {venue}:{sym}: {str(exc)[:90]}")
    px = pd.DataFrame(closes).sort_index()
    vol = pd.DataFrame(vols).reindex(px.index)
    px = px.where(vol > 0)
    fday = pd.DataFrame(fund).reindex(px.index)
    for sym, f in fund.items():
        alive = (px.index >= f.index.min()) & (px.index <= f.index.max())
        fday.loc[alive, sym] = fday.loc[alive, sym].fillna(0.0)
        px.loc[~alive, sym] = np.nan
    return px, fday
