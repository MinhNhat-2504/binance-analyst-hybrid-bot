"""Monthly income statement from the exchange, one CSV per month, for the records.

Binance keeps a limited window of income history in its UI and the audit DB only knows
what the engine did, not what the exchange charged (funding, commission, insurance
clears, transfers). Once real money is involved, a durable per-month record of every
income row is the minimum for tax and for arguing with the exchange.

Read-only signed GET on /fapi/v1/income, paged by time, every incomeType. Written to
reports/income_<env>_<YYYY-MM>.csv plus a per-type total line printed. Idempotent:
re-running a month overwrites the same file.

    python export_income.py --env testnet --month 2026-09
    python export_income.py --env live --month 2026-11
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from execution.binance_futures import FuturesREST  # noqa: E402

COLUMNS = ("time_utc", "incomeType", "symbol", "income", "asset", "info", "tranId", "tradeId")


def month_bounds(month: str) -> tuple[datetime, datetime]:
    first = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt


def fetch_income(client: FuturesREST, start: datetime, end: datetime, *, sleep=time.sleep) -> list[dict]:
    rows: list[dict] = []
    cursor = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    for _ in range(200):
        batch = client.signed("GET", "/fapi/v1/income", {"startTime": cursor, "endTime": end_ms, "limit": 1000})
        if not batch:
            break
        rows.extend(batch)
        last = max(int(r["time"]) for r in batch)
        if len(batch) < 1000 or last <= cursor:
            break
        cursor = last + 1
        sleep(0.2)
    seen = set()
    unique = []
    for r in rows:
        key = (r.get("tranId"), r.get("incomeType"), r.get("time"), r.get("symbol"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return sorted(unique, key=lambda r: int(r["time"]))


def write_csv(rows: list[dict], path: Path) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    path.parent.mkdir(exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for r in rows:
            t = datetime.fromtimestamp(int(r["time"]) / 1000, tz=timezone.utc).isoformat(timespec="seconds")
            w.writerow([t, r.get("incomeType"), r.get("symbol", ""), r.get("income"), r.get("asset"),
                        r.get("info", ""), r.get("tranId", ""), r.get("tradeId", "")])
            totals[str(r.get("incomeType"))] += float(r.get("income") or 0)
    return dict(totals)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", default="testnet", choices=["testnet", "live"])
    ap.add_argument("--month", default=date.today().strftime("%Y-%m"), help="YYYY-MM (default: this month)")
    args = ap.parse_args(argv)
    start, end = month_bounds(args.month)
    client = FuturesREST.from_env(args.env, required=True)
    rows = fetch_income(client, start, end)
    out = ROOT / "reports" / f"income_{args.env}_{args.month}.csv"
    totals = write_csv(rows, out)
    print(f"{args.env} {args.month}: {len(rows)} rows -> {out.relative_to(ROOT)}")
    for k, v in sorted(totals.items()):
        print(f"  {k:18s} {v:+12.4f}")
    print(f"  {'TOTAL':18s} {sum(totals.values()):+12.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
