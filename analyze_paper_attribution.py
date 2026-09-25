"""Which symbols made the paper PnL, and how concentrated is it?

The paper ledger books one number per day. That answers "is the book up" and nothing
else: not which names earned it, not whether one name is the whole story, not whether
the funding leg or the price leg is paying. An earlier README claimed "no symbol is more
than 15% of the profit" without any file to back it; this is the file.

Reconstruction is exact, not approximate: the same opens, the same daily funding, the
same 2/3 - 1/3 boundary split and the same cost-on-turnover the executor uses, so the
per-symbol contributions of a day sum to the ledger's pnl for that day to rounding. The
script asserts that on every row and refuses to report if any day disagrees by more than
1e-5 - a silent divergence here would mean the executor and this file are computing
different strategies.

Read-only. Fetches opens and funding fresh (like the executor), writes
reports/paper_attribution.json and reports/paper_attribution_by_symbol.csv.

    python analyze_paper_attribution.py
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from honest.daily import build_panel  # noqa: E402
from run_carry_paper import build_opens  # noqa: E402

CONFIG = ROOT / "carry_paper_config_v1.json"
LEDGER = ROOT / "carry_paper_ledger.csv"
OUT_JSON = ROOT / "reports" / "paper_attribution.json"
OUT_CSV = ROOT / "reports" / "paper_attribution_by_symbol.csv"
TOLERANCE = 1e-5


def weights_from_row(row: dict, columns: pd.Index) -> pd.Series:
    w = pd.Series(0.0, index=columns)
    longs = [s for s in (row.get("longs") or "").split(",") if s]
    shorts = [s for s in (row.get("shorts") or "").split(",") if s]
    if longs:
        w[longs] = 0.5 / len(longs)
    if shorts:
        w[shorts] = -0.5 / len(shorts)
    return w


def attribute(rows: list[dict], opens: pd.DataFrame, fday: pd.DataFrame, cost_per_leg: float) -> pd.DataFrame:
    """Long frame: one row per (fill_day, symbol) with price, funding, cost, total contributions."""
    prev_w = pd.Series(0.0, index=opens.columns)
    out = []
    for row in rows:
        fill = pd.Timestamp(row["fill_day"])
        nxt = fill + pd.Timedelta(days=1)
        w = weights_from_row(row, opens.columns)
        ret = (opens.loc[nxt] / opens.loc[fill] - 1).fillna(0.0)
        f_day = fday.loc[fill].fillna(0.0) if fill in fday.index else pd.Series(0.0, index=opens.columns)
        price = w * ret
        funding = -w * f_day * (2 / 3) + (-prev_w * f_day * (1 / 3))
        cost = -(w - prev_w).abs() * cost_per_leg
        total = price + funding + cost
        booked = float(row["pnl"])
        gap = abs(float(total.sum()) - booked)
        if gap > TOLERANCE:
            raise RuntimeError(f"{row['fill_day']}: reconstruction {total.sum():+.6f} != ledger {booked:+.6f} (gap {gap:.2e})")
        for sym in opens.columns:
            if w[sym] == 0 and prev_w[sym] == 0:
                continue
            # An exit day (weight 0, prev weight != 0) still carries the old side's boundary
            # funding and its exit cost; charge it to the side that held the position.
            side = float(np.sign(w[sym]) if w[sym] != 0 else np.sign(prev_w[sym]))
            out.append({"fill_day": row["fill_day"], "symbol": sym, "weight": float(w[sym]), "side": side,
                        "price": float(price[sym]), "funding": float(funding[sym]),
                        "cost": float(cost[sym]), "total": float(total[sym])})
        prev_w = w
    return pd.DataFrame(out)


def summarize(long: pd.DataFrame, n_days: int) -> dict:
    by = long.groupby("symbol").agg(total=("total", "sum"), price=("price", "sum"), funding=("funding", "sum"),
                                     cost=("cost", "sum"), days_in_book=("weight", lambda s: int((s != 0).sum())),
                                     days_long=("weight", lambda s: int((s > 0).sum())),
                                     days_short=("weight", lambda s: int((s < 0).sum())))
    by = by.sort_values("total", ascending=False)
    grand = float(by["total"].sum())
    gains = by["total"].clip(lower=0)
    share_of_gains = (gains / gains.sum()) if gains.sum() > 0 else gains * 0
    by["share_of_total_pct"] = (by["total"] / grand * 100.0) if grand else np.nan
    by["share_of_gains_pct"] = share_of_gains * 100.0
    daily = long.groupby("fill_day")["total"].sum()
    side = {
        "long_leg_pct": round(float(long.loc[long["side"] > 0, "total"].sum()) * 100, 3),
        "short_leg_pct": round(float(long.loc[long["side"] < 0, "total"].sum()) * 100, 3),
        "long_leg_price_pct": round(float(long.loc[long["side"] > 0, "price"].sum()) * 100, 3),
        "short_leg_price_pct": round(float(long.loc[long["side"] < 0, "price"].sum()) * 100, 3),
        "long_leg_funding_pct": round(float(long.loc[long["side"] > 0, "funding"].sum()) * 100, 3),
        "short_leg_funding_pct": round(float(long.loc[long["side"] < 0, "funding"].sum()) * 100, 3),
    }
    best_day_share = float(daily.max() / daily.sum() * 100) if daily.sum() > 0 else float("nan")
    return {
        "n_days": n_days, "n_symbols_ever_held": int(len(by)),
        "total_pnl_pct": round(grand * 100, 3),
        "split_pct_of_total": {
            "price": round(float(by["price"].sum()) / grand * 100, 1) if grand else None,
            "funding": round(float(by["funding"].sum()) / grand * 100, 1) if grand else None,
            "cost": round(float(by["cost"].sum()) / grand * 100, 1) if grand else None,
        },
        "by_side": side,
        "concentration": {
            "top1_symbol": by.index[0], "top1_share_of_gains_pct": round(float(share_of_gains.iloc[0] * 100), 1),
            "top3_share_of_gains_pct": round(float(share_of_gains.iloc[:3].sum() * 100), 1),
            "herfindahl_of_gains": round(float((share_of_gains ** 2).sum()), 3),
            "total_without_top1_pct": round(float(grand - by["total"].iloc[0]) * 100, 3),
            "total_without_top3_pct": round(float(grand - by["total"].iloc[:3].sum()) * 100, 3),
            "n_symbols_negative": int((by["total"] < 0).sum()),
            "best_day_share_of_total_pct": round(best_day_share, 1),
            "median_day_bps": round(float(daily.median() * 1e4), 2),
            "days_positive_pct": round(float((daily > 0).mean() * 100), 1),
        },
        "by_symbol": {s: {k: (int(v) if k.startswith("days") else round(float(v), 6))
                          for k, v in r.items()} for s, r in by.iterrows()},
    }


def main() -> int:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    rows = list(csv.DictReader(LEDGER.open(encoding="utf-8")))
    if not rows:
        print("no ledger rows"); return 0
    first = pd.Timestamp(rows[0]["fill_day"])
    days = (pd.Timestamp.utcnow().tz_localize(None).normalize() - first).days + cfg["funding_lookback_days"] + 10
    px, fday = build_panel(cfg["universe"], days=int(days), min_days=10, use_cache=False, verbose=False)
    opens = build_opens(cfg["universe"], int(days)).reindex(columns=px.columns)
    long = attribute(rows, opens, fday, cfg["cost_per_leg"])
    summary = summarize(long, len(rows))
    OUT_JSON.parent.mkdir(exist_ok=True)
    OUT_JSON.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    pd.DataFrame(summary["by_symbol"]).T.to_csv(OUT_CSV)
    c = summary["concentration"]
    print(f"paper attribution: {summary['n_days']} days, total {summary['total_pnl_pct']:+.2f}%  "
          f"(price {summary['split_pct_of_total']['price']}%, funding {summary['split_pct_of_total']['funding']}%, cost {summary['split_pct_of_total']['cost']}%)")
    print(f"  top1 {c['top1_symbol']} = {c['top1_share_of_gains_pct']}% of gains; top3 {c['top3_share_of_gains_pct']}%; "
          f"HHI {c['herfindahl_of_gains']}; without top1 {c['total_without_top1_pct']:+.2f}%; without top3 {c['total_without_top3_pct']:+.2f}%")
    b = summary["by_side"]
    print(f"  long leg {b['long_leg_pct']:+.2f}% (price {b['long_leg_price_pct']:+.2f}, funding {b['long_leg_funding_pct']:+.2f})  "
          f"short leg {b['short_leg_pct']:+.2f}% (price {b['short_leg_price_pct']:+.2f}, funding {b['short_leg_funding_pct']:+.2f})")
    print(f"  best day {c['best_day_share_of_total_pct']}% of total; median day {c['median_day_bps']:+.1f} bps; "
          f"{c['days_positive_pct']}% days positive; {c['n_symbols_negative']}/{summary['n_symbols_ever_held']} symbols net negative")
    for s, r in list(summary["by_symbol"].items())[:8]:
        print(f"    {s:12s} {r['total'] * 100:+6.2f}%  price {r['price'] * 100:+6.2f}  funding {r['funding'] * 100:+6.2f}  "
              f"cost {r['cost'] * 100:+6.2f}  days {r['days_in_book']:2d} (L{r['days_long']}/S{r['days_short']})")
    print(f"-> {OUT_JSON.relative_to(ROOT)}, {OUT_CSV.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
