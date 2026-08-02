"""Funding-rate features: a genuinely different information channel.

Every one of the 131 clean features is a transform of the same 15m price/volume series,
and the harness has now measured what that channel is worth: ~5bps of SHORT excess against
a 12bps achievable cost floor. More transforms of the same series cannot change that.

Funding is different in kind: it is the price of holding a perp position, set by the
long/short imbalance. Persistent positive funding means crowded longs paying to stay in -
a positioning fact, not a price fact. If the SHORT signal has any real substrate, crowding
is a plausible mechanism for it, and funding measures crowding directly.

Lookahead discipline: a funding record's `fundingTime` is when the payment SETTLES, and the
rate is known at settlement. A 15m bar may only see settlements with
fundingTime <= bar open time, enforced via merge_asof(direction="backward"). All rolling
stats are computed on the settlement series first, so a bar inherits only completed windows.
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from .data import CACHE_DIR, _get

FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"


def fetch_funding(symbol: str, days_back: int = 720, use_cache: bool = True) -> pd.DataFrame:
    """Full funding-settlement history (8h cadence -> ~3 rows/day)."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache = CACHE_DIR / f"{symbol}_funding_{days_back}d.parquet"
    if use_cache and cache.exists():
        return pd.read_parquet(cache)

    now_ms = int(time.time() * 1000)
    start = now_ms - days_back * 24 * 3600 * 1000
    rows: list[dict] = []
    while start < now_ms:
        batch = _get(f"{FUNDING_URL}?symbol={symbol}&startTime={start}&limit=1000")
        if not batch:
            break
        rows.extend(batch)
        nxt = batch[-1]["fundingTime"] + 1
        if nxt <= start:
            break
        start = nxt
        time.sleep(0.15)

    if not rows:
        return pd.DataFrame(columns=["fundingTime", "fundingRate"])

    df = pd.DataFrame(rows)[["fundingTime", "fundingRate"]]
    df["fundingTime"] = pd.to_datetime(df["fundingTime"], unit="ms")
    df["fundingRate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
    df = (df.dropna().drop_duplicates(subset="fundingTime")
            .sort_values("fundingTime").reset_index(drop=True))
    if use_cache:
        df.to_parquet(cache, index=False)
    return df


def funding_features(funding: pd.DataFrame) -> pd.DataFrame:
    """Per-settlement features, all backward-looking on the settlement series.

    Rates are in fractional units (1e-4 = 1bp per 8h) and are ALREADY cross-symbol
    comparable - no per-symbol scale for the model to memorise, unlike the raw volume
    levels the audit caught.
    """
    f = funding.copy()
    r = f["fundingRate"]
    f["FR_last_bps"] = r * 1e4
    f["FR_delta_bps"] = r.diff() * 1e4
    f["FR_cum_3d_bps"] = r.rolling(9).sum() * 1e4        # 9 settlements = 3 days
    f["FR_cum_7d_bps"] = r.rolling(21).sum() * 1e4
    mean90 = r.rolling(90).mean()                        # 90 settlements = 30 days
    std90 = r.rolling(90).std().replace(0, np.nan)
    f["FR_z_30d"] = (r - mean90) / std90
    # Crowding persistence: how one-sided has funding been lately, sign-wise.
    f["FR_sign_persist_3d"] = np.sign(r).rolling(9).mean()
    return f.drop(columns=["fundingRate"])


def merge_funding(bars: pd.DataFrame, funding_feat: pd.DataFrame) -> pd.DataFrame:
    """Attach the latest SETTLED funding features to each 15m bar (backward asof)."""
    if funding_feat.empty:
        return bars
    out = pd.merge_asof(
        bars.sort_values("Open time"),
        funding_feat.sort_values("fundingTime"),
        left_on="Open time", right_on="fundingTime",
        direction="backward", allow_exact_matches=True,
    ).drop(columns=["fundingTime"])
    return out


def add_cross_sectional(df: pd.DataFrame,
                        cols=("Return_24", "Volatility_24", "Breakout_20",
                              "Breakdown_20", "FR_last_bps", "FR_cum_3d_bps")) -> pd.DataFrame:
    """Percentile rank of each symbol against the rest of the universe at the same bar.

    Per-symbol features answer "is BTC strong versus its own past?". These answer "is BTC
    strong versus everything else tradeable right now?" - relative information that does
    not exist anywhere in the per-symbol frame. Inputs are backward-looking same-bar
    features, so ranking them across symbols at a fixed timestamp adds no lookahead.
    """
    df = df.copy()
    for c in cols:
        if c in df.columns:
            df[f"XS_{c}_rank"] = df.groupby("Open time")[c].rank(pct=True)
    return df
