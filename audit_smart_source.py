"""Read-only archive-vs-REST check for the pre-registered positioning signal.

Binance's daily REST bar stamped 00:00 on D is the archive's last 5-minute
snapshot of D-1. Compare those same instants before opening strategy results.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from honest.data import _get
from honest.daily import build_panel
from honest.metrics import fetch_metrics
from run_daily_lab import UNIVERSE
from run_smart_vs_crowd_lab import load_cached_metrics

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "reports" / "smart_vs_crowd_source_check.json"
FIELDS = {
    "toptrader_position_ratio": ("topLongShortPositionRatio", "longShortRatio"),
    "global_account_ratio": ("globalLongShortAccountRatio", "longShortRatio"),
    "open_interest": ("openInterestHist", "sumOpenInterest"),
}
TOLERANCE_PCT = 1.0
MAX_BAD_SHARE_PCT = 5.0


def compare_series(archive: pd.Series, rest: list[dict], rest_key: str) -> dict:
    values = {datetime.fromtimestamp(int(row["timestamp"]) / 1000,
                                    tz=timezone.utc).date(): float(row[rest_key])
              for row in rest}
    errors = []
    for stamp, value in archive.dropna().items():
        rest_value = values.get(stamp.date() + timedelta(days=1))
        if value > 0 and rest_value is not None and rest_value > 0:
            errors.append(abs(float(value) / rest_value - 1) * 100)
    return {"overlap": len(errors), "bad": sum(error > TOLERANCE_PCT for error in errors),
            "median_error_pct": float(pd.Series(errors).median()) if errors else None,
            "max_error_pct": max(errors) if errors else None}


def main() -> int:
    px, _ = build_panel(UNIVERSE, 600, min_days=400, use_cache=True, verbose=False)
    _, _, eligibility = load_cached_metrics(px)
    # Delisted names remain in the historical panel until their last tradable day,
    # but have no current REST series to compare with the archive.
    symbols = [symbol for symbol in eligibility["eligible_symbols"]
               if pd.notna(px[symbol].iloc[-1])]
    eligibility["inactive_at_panel_end"] = [symbol for symbol in eligibility["eligible_symbols"]
                                             if symbol not in symbols]
    results = {}
    for index, symbol in enumerate(symbols, 1):
        archive = fetch_metrics(symbol, days_back=35, workers=8)
        by_field = {}
        for field, (endpoint, rest_key) in FIELDS.items():
            try:
                rest = _get(f"https://fapi.binance.com/futures/data/{endpoint}"
                            f"?symbol={symbol}&period=1d&limit=30")
                by_field[field] = compare_series(archive[field], rest, rest_key)
            except Exception as exc:  # source check must fail closed, with no signed URL in output
                by_field[field] = {"overlap": 0, "bad": 0, "error": type(exc).__name__}
        results[symbol] = by_field
        print(f"[{index:2d}/{len(symbols)}] {symbol:14s} "
              + " ".join(f"{field}:{row['bad']}/{row['overlap']}"
                         for field, row in by_field.items()), flush=True)
    totals = {field: {"overlap": sum(results[s][field]["overlap"] for s in results),
                      "bad": sum(results[s][field]["bad"] for s in results)}
              for field in FIELDS}
    for row in totals.values():
        row["bad_share_pct"] = 100 * row["bad"] / row["overlap"] if row["overlap"] else None
    incomplete = [f"{symbol}:{field}" for symbol, fields in results.items()
                  for field, row in fields.items() if row["overlap"] == 0]
    passed = bool(not incomplete and all(row["bad_share_pct"] is not None
                                         and row["bad_share_pct"] <= MAX_BAD_SHARE_PCT
                                         for row in totals.values()))
    report = {"generated_utc": datetime.now(timezone.utc).isoformat(),
              "protocol": "RESEARCH_PREREG_Q4_2026.md cell 3 data kill",
              "label_alignment": "archive[D] == REST[D+1]",
              "threshold": {"error_pct": TOLERANCE_PCT, "bad_share_pct": MAX_BAD_SHARE_PCT},
              "totals": totals, "incomplete": incomplete, "passes": passed,
              "eligibility": eligibility,
              "symbols": results}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(f"-> {OUT}  passes={passed}  totals={totals}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
