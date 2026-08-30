"""Read-only interim monitor for OPEN paper trades.

This is a fast-feedback helper, not final profit proof.  The 12h backfiller is
still the source of truth for Outcome_PnL.  This script answers: "While we wait,
are the current PAPER_TRADE rows moving in the right direction?"
"""

from __future__ import annotations

import argparse
import math
import time
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests


DEFAULT_LEDGER = Path("shadow_ledger_candidates_v4.csv")
BINANCE_FAPI = "https://fapi.binance.com"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show interim mark-to-market for OPEN PAPER_TRADE rows.")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="Ledger CSV path.")
    parser.add_argument("--timezone", default="Asia/Saigon", help="Local timezone for printed timestamps.")
    parser.add_argument("--horizon-hours", type=float, default=12.0, help="Final outcome horizon used by backfiller.")
    parser.add_argument("--show", type=int, default=20, help="Number of open paper rows to show.")
    parser.add_argument("--timeout", type=float, default=8.0, help="HTTP timeout in seconds.")
    parser.add_argument("--no-network", action="store_true", help="Do not call Binance; only show ledger age/readiness.")
    parser.add_argument("--skip-klines", action="store_true", help="Only fetch current mark price, skip MFE/MAE klines.")
    parser.add_argument("--sleep", type=float, default=0.08, help="Small delay between Binance requests.")
    return parser.parse_args()


def read_csv_with_retry(path: Path, retries: int = 3, delay_sec: float = 0.35) -> pd.DataFrame:
    last_error: Exception | None = None
    for _ in range(retries):
        try:
            return pd.read_csv(path)
        except Exception as exc:
            last_error = exc
            time.sleep(delay_sec)
    raise RuntimeError(f"Could not read {path}: {last_error}") from last_error


def col(df: pd.DataFrame, *names: str) -> str | None:
    direct = {c: c for c in df.columns}
    lower = {c.lower(): c for c in df.columns}
    for name in names:
        if name in direct:
            return direct[name]
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def parse_mixed_utc(series: pd.Series) -> pd.Series:
    try:
        return pd.to_datetime(series, errors="coerce", utc=True, format="mixed")
    except TypeError:
        return pd.to_datetime(series, errors="coerce", utc=True)


def as_bool(series: pd.Series | None, index: pd.Index) -> pd.Series:
    if series is None:
        return pd.Series(False, index=index)
    return series.astype(str).str.strip().str.lower().isin(["true", "1", "yes", "y"])


def to_num(series: pd.Series | None, index: pd.Index, default: float = np.nan) -> pd.Series:
    if series is None:
        return pd.Series(default, index=index, dtype="float64")
    return pd.to_numeric(series, errors="coerce")


def normalize(df: pd.DataFrame, horizon_hours: float) -> pd.DataFrame:
    out = df.copy()
    idx = out.index
    time_col = col(out, "timestamp_utc", "Timestamp_UTC")
    out["_ts"] = parse_mixed_utc(out[time_col].astype(str)) if time_col else pd.NaT

    mappings = {
        "_symbol": ["symbol", "Symbol"],
        "_side": ["side", "Side"],
        "_regime": ["regime", "Regime"],
        "_proba": ["final_proba", "Calib_Proba", "Final_Proba"],
        "_price": ["price", "Price", "Entry_Price"],
        "_size_usd": ["size_usd", "Size_USD", "Trade_Size_USD"],
        "_outcome": ["Outcome_PnL"],
        "_status": ["Outcome_Status"],
        "_decision": ["final_gate_decision", "Final_Action"],
        "_reason": ["profit_focus_reason"],
        "_symbol_status": ["symbol_prior_status"],
        "_pocket_status": ["pocket_health_status"],
        "_backfilled": ["is_backfilled"],
    }
    for target, candidates in mappings.items():
        source = col(out, *candidates)
        out[target] = out[source] if source else pd.NA

    out["_symbol"] = out["_symbol"].fillna("").astype(str).str.upper().str.strip()
    out["_side"] = out["_side"].fillna("").astype(str).str.upper().str.strip()
    out["_regime"] = pd.to_numeric(out["_regime"], errors="coerce")
    out["_proba"] = pd.to_numeric(out["_proba"], errors="coerce")
    out["_price"] = pd.to_numeric(out["_price"], errors="coerce")
    out["_size_usd"] = pd.to_numeric(out["_size_usd"], errors="coerce").fillna(0.0)
    out["_outcome"] = pd.to_numeric(out["_outcome"], errors="coerce")
    out["_decision"] = out["_decision"].fillna("").astype(str).str.upper()
    out["_status"] = out["_status"].fillna("").astype(str).str.upper()
    out["_backfilled_bool"] = as_bool(out["_backfilled"], idx)
    out["_closed"] = out["_outcome"].notna() | out["_backfilled_bool"] | out["_status"].isin(["CLOSED", "WIN", "LOSS"])
    now = pd.Timestamp.now(tz=timezone.utc)
    horizon = pd.Timedelta(hours=float(horizon_hours))
    out["_age_minutes"] = (now - out["_ts"]).dt.total_seconds() / 60.0
    out["_ready_at"] = out["_ts"] + horizon
    out["_minutes_to_final"] = (out["_ready_at"] - now).dt.total_seconds() / 60.0
    return out


def side_pnl(side: str, entry: float, price: float) -> float:
    if not entry or not price or entry <= 0 or price <= 0:
        return math.nan
    if str(side).upper() == "SHORT":
        return (entry / price) - 1.0
    return (price / entry) - 1.0


def fetch_current_price(session: requests.Session, symbol: str, timeout: float) -> float:
    resp = session.get(f"{BINANCE_FAPI}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    return float(payload["price"])


def fetch_mfe_mae(
    session: requests.Session,
    symbol: str,
    side: str,
    entry: float,
    start_ts: pd.Timestamp,
    timeout: float,
) -> tuple[float, float, int]:
    if pd.isna(start_ts) or entry <= 0:
        return math.nan, math.nan, 0
    start_ms = int(pd.Timestamp(start_ts).timestamp() * 1000)
    end_ms = int(pd.Timestamp.now(tz=timezone.utc).timestamp() * 1000)
    resp = session.get(
        f"{BINANCE_FAPI}/fapi/v1/klines",
        params={"symbol": symbol, "interval": "1m", "startTime": start_ms, "endTime": end_ms, "limit": 1000},
        timeout=timeout,
    )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return math.nan, math.nan, 0
    highs = pd.to_numeric(pd.Series([r[2] for r in rows]), errors="coerce").dropna()
    lows = pd.to_numeric(pd.Series([r[3] for r in rows]), errors="coerce").dropna()
    if highs.empty or lows.empty:
        return math.nan, math.nan, len(rows)
    if str(side).upper() == "SHORT":
        mfe = (entry / float(lows.min())) - 1.0
        mae = (entry / float(highs.max())) - 1.0
    else:
        mfe = (float(highs.max()) / entry) - 1.0
        mae = (float(lows.min()) / entry) - 1.0
    return float(mfe), float(mae), len(rows)


def pct(value: float) -> str:
    if pd.isna(value):
        return "n/a"
    return f"{100.0 * float(value):+.3f}%"


def main() -> int:
    args = parse_args()
    ledger_path = Path(args.ledger)
    tz = ZoneInfo(args.timezone)
    if not ledger_path.exists():
        print(f"Ledger not found: {ledger_path}")
        return 1

    df = normalize(read_csv_with_retry(ledger_path), args.horizon_hours)
    open_paper = df.loc[
        df["_decision"].eq("PAPER_TRADE")
        & df["_size_usd"].gt(0)
        & df["_price"].gt(0)
        & ~df["_closed"]
    ].copy()
    open_paper = open_paper.sort_values("_ts")

    print(f"Paper interim monitor: {ledger_path}")
    print(f"Open PAPER_TRADE rows={len(open_paper)} | ledger rows={len(df)}")
    print(f"Now local: {pd.Timestamp.now(tz=timezone.utc).tz_convert(tz).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    if open_paper.empty:
        print("No open paper trades to monitor.")
        return 0

    view = open_paper.tail(int(args.show)).copy()
    view["entry_time_local"] = view["_ts"].dt.tz_convert(tz).dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    view["ready_at_local"] = view["_ready_at"].dt.tz_convert(tz).dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    view["mark_price"] = np.nan
    view["mark_pnl"] = np.nan
    view["mfe"] = np.nan
    view["mae"] = np.nan
    view["kline_n"] = 0
    view["fetch_status"] = "network_skipped" if args.no_network else ""

    if not args.no_network:
        session = requests.Session()
        for idx, row in view.iterrows():
            symbol = str(row["_symbol"])
            try:
                mark = fetch_current_price(session, symbol, args.timeout)
                view.loc[idx, "mark_price"] = mark
                view.loc[idx, "mark_pnl"] = side_pnl(str(row["_side"]), float(row["_price"]), mark)
                if not args.skip_klines:
                    mfe, mae, count = fetch_mfe_mae(session, symbol, str(row["_side"]), float(row["_price"]), row["_ts"], args.timeout)
                    view.loc[idx, "mfe"] = mfe
                    view.loc[idx, "mae"] = mae
                    view.loc[idx, "kline_n"] = count
                view.loc[idx, "fetch_status"] = "ok"
            except Exception as exc:
                view.loc[idx, "fetch_status"] = f"{type(exc).__name__}: {str(exc)[:80]}"
            time.sleep(max(0.0, float(args.sleep)))

    view["mark_pnl_%"] = view["mark_pnl"].map(pct)
    view["mfe_%"] = view["mfe"].map(pct)
    view["mae_%"] = view["mae"].map(pct)
    view["age_min"] = view["_age_minutes"].round(1)
    view["min_to_final"] = view["_minutes_to_final"].clip(lower=0).round(1)
    columns = [
        "entry_time_local",
        "_symbol",
        "_side",
        "_regime",
        "_proba",
        "_price",
        "mark_price",
        "mark_pnl_%",
        "mfe_%",
        "mae_%",
        "age_min",
        "min_to_final",
        "ready_at_local",
        "_reason",
        "fetch_status",
    ]
    print()
    print(view[columns].to_string(index=False))

    ready_now = int((open_paper["_minutes_to_final"] <= 0).sum())
    print()
    print("Interpretation")
    print(f"- Ready for final backfill now: {ready_now}/{len(open_paper)}")
    print("- Interim mark PnL is only a fast proxy; do not promote live from this alone.")
    print("- Use the 12h backfiller for final Outcome_PnL and optimizer promotion.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
