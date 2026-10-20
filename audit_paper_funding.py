"""Read-only audit of the paper ledger's approximate funding split.

The active paper rule books one third of each day's funding against yesterday's
weights and two thirds against today's.  This audit uses settlement timestamps:
the midnight settlement belongs to yesterday's weights, later settlements to
today's.  It never edits the locked paper config, ledger, or execution targets.

By default, only local parquet caches are read.  Pass --refresh to fetch fresh
public funding history before auditing; this requires access to Binance.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np
import pandas as pd

from honest.data import CACHE_DIR
from honest.funding import fetch_funding

ROOT = Path(__file__).resolve().parent
LEDGER = ROOT / "carry_paper_ledger.csv"
REPORT = ROOT / "reports" / "paper_funding_audit.json"


def _weights(row: dict, symbols: set[str]) -> dict[str, float]:
    longs = [s for s in (row.get("longs") or "").split(",") if s]
    shorts = [s for s in (row.get("shorts") or "").split(",") if s]
    if len(set(longs + shorts)) != len(longs) + len(shorts):
        raise ValueError("duplicate or conflicting symbols in paper ledger")
    symbols.update(longs + shorts)
    return {**{s: 0.5 / len(longs) for s in longs},
            **{s: -0.5 / len(shorts) for s in shorts}}


def load_cached_funding(symbol: str, cache_dir: Path = CACHE_DIR) -> pd.DataFrame:
    candidates = (list(cache_dir.glob(f"{symbol}_funding_*d.parquet"))
                  + list(cache_dir.glob(f"PAPER_FUNDING_{symbol}_*.parquet")))
    if not candidates:
        return pd.DataFrame(columns=["fundingTime", "fundingRate"])
    frames = [pd.read_parquet(path, columns=["fundingTime", "fundingRate"])
              for path in candidates]
    joined = pd.concat(frames, ignore_index=True)
    different = joined.groupby("fundingTime")["fundingRate"].nunique(dropna=False)
    if different.gt(1).any():
        raise ValueError(f"{symbol}: conflicting cached funding settlements")
    return joined.drop_duplicates("fundingTime").sort_values("fundingTime")


def settlement_split(funding: pd.DataFrame, day: pd.Timestamp) -> tuple[float, float] | None:
    """Return (midnight, later) rates, or None when the settlement day is incomplete."""
    if funding.empty:
        return None
    times = pd.to_datetime(funding["fundingTime"], utc=True).dt.tz_localize(None)
    rates = pd.to_numeric(funding["fundingRate"], errors="coerce")
    selected = (times >= day) & (times < day + pd.Timedelta(days=1))
    times, rates = times[selected], rates[selected]
    if rates.isna().any() or len(rates) < 3 or times.duplicated().any():
        return None
    boundary = times < day + pd.Timedelta(minutes=1)
    if int(boundary.sum()) != 1 or int((~boundary).sum()) < 2:
        return None
    return float(rates[boundary].iloc[0]), float(rates[~boundary].sum())


def audit(rows: list[dict], funding: dict[str, pd.DataFrame]) -> dict:
    if not rows:
        raise ValueError("paper ledger is empty")
    symbols: set[str] = set()
    daily = []
    missing = []
    prev: dict[str, float] = {}
    exact_equity = 1.0
    last_day = None
    for row in rows:
        day = pd.Timestamp(row["fill_day"]).normalize()
        if last_day is not None and day != last_day + pd.Timedelta(days=1):
            raise ValueError(f"paper ledger has a gap or duplicate at {day.date()}")
        current = _weights(row, symbols)
        active = set(prev) | set(current)
        old_funding = 0.0
        exact_funding = 0.0
        incomplete = []
        for symbol in sorted(active):
            split = settlement_split(funding.get(symbol, pd.DataFrame()), day)
            if split is None:
                incomplete.append(symbol)
                continue
            midnight, later = split
            full_day = midnight + later
            old_funding -= prev.get(symbol, 0.0) * full_day / 3
            old_funding -= current.get(symbol, 0.0) * full_day * 2 / 3
            exact_funding -= prev.get(symbol, 0.0) * midnight
            exact_funding -= current.get(symbol, 0.0) * later
        if incomplete:
            missing.append({"fill_day": str(day.date()), "symbols": incomplete})
        else:
            delta = exact_funding - old_funding
            recorded = float(row["pnl"])
            corrected = recorded + delta
            exact_equity *= 1 + corrected
            daily.append({"fill_day": str(day.date()), "recorded_pnl_bps": round(recorded * 1e4, 3),
                          "funding_correction_bps": round(delta * 1e4, 3),
                          "corrected_pnl_bps": round(corrected * 1e4, 3)})
        prev, last_day = current, day

    # A corrected compounded return only describes a contiguous run from day one.
    contiguous = len(daily) == len(rows) or (
        bool(daily) and all(d["fill_day"] == str(pd.Timestamp(rows[i]["fill_day"]).date())
                            for i, d in enumerate(daily)))
    if not contiguous:
        exact_equity = float("nan")
    recorded_equity = float(np.prod([1 + float(r["pnl"]) for r in rows[:len(daily)]])) if contiguous else float("nan")
    return {
        "status": "complete" if not missing else "partial",
        "audited_days": len(daily), "ledger_days": len(rows),
        "first_audited_day": daily[0]["fill_day"] if daily else None,
        "last_audited_day": daily[-1]["fill_day"] if daily else None,
        "funding_correction_bps_sum": round(sum(d["funding_correction_bps"] for d in daily), 3),
        "recorded_return_pct": round((recorded_equity - 1) * 100, 4) if contiguous and daily else None,
        "corrected_return_pct": round((exact_equity - 1) * 100, 4) if contiguous and daily else None,
        "missing_days": missing, "daily": daily,
        "note": "The correction changes funding attribution only; fills, fees, and prices remain as booked. "
                "A partial result is not an estimate of the full paper period.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--refresh", action="store_true", help="fetch public funding history from Binance")
    parser.add_argument("--ledger", type=Path, default=LEDGER)
    parser.add_argument("--out", type=Path, default=REPORT)
    args = parser.parse_args()
    with args.ledger.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    symbols: set[str] = set()
    for row in rows:
        _weights(row, symbols)
    if args.refresh:
        first = pd.Timestamp(rows[0]["fill_day"])
        last = pd.Timestamp(rows[-1]["fill_day"])
        days_back = (pd.Timestamp.utcnow().tz_localize(None).normalize() - first).days + 3
        histories = {}
        CACHE_DIR.mkdir(exist_ok=True)
        for symbol in sorted(symbols):
            frame = fetch_funding(symbol, days_back, use_cache=False)
            frame = frame[(frame["fundingTime"] >= first)
                          & (frame["fundingTime"] < last + pd.Timedelta(days=1))].copy()
            frame.to_parquet(
                CACHE_DIR / f"PAPER_FUNDING_{symbol}_{first.date()}_{last.date()}.parquet",
                index=False,
            )
            histories[symbol] = frame
    else:
        histories = {s: load_cached_funding(s) for s in sorted(symbols)}
    result = audit(rows, histories)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    print(f"funding audit: {result['audited_days']}/{result['ledger_days']} days; "
          f"correction {result['funding_correction_bps_sum']:+.2f} bps")
    if result["status"] == "partial":
        print("PARTIAL: funding settlements are missing; no full-period return is claimed")
    else:
        print(f"paper {result['recorded_return_pct']:+.2f}% -> settlement-corrected "
              f"{result['corrected_return_pct']:+.2f}%")
    print(f"-> {args.out}")
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
