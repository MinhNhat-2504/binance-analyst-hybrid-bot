"""Weekly canaries: is the edge still there, and did the world change?

Two monitors of ALREADY-MEASURED things (no new research cells):

  1. SIGNAL HEALTH. The 2026-08 cross-exchange study showed the edge lives specifically
     in Binance funding's ranking (Bybit-funding weights: Sharpe 1.07/-0.35 vs Binance
     1.75/1.85). Recompute both on a trailing window:
        - Binance-minus-Bybit gap COLLAPSING toward zero from above = the signal is
          spreading/being arbitraged away -> plan exit.
        - Both strong = healthy. Bybit catching UP while Binance holds = fine.
  2. FUNDING REGIME. Basis carry died in 2026's low-funding regime (funding leg 3-6%/yr
     vs 9-35%/yr costs). If market-median funding sustains > ~15%/yr the file
     basis_lab_report.json is worth reopening. Until then it stays closed.

Appends one row to canary_log.csv, prints a one-line verdict per canary. Non-zero exit
only on ALERT so a scheduler can surface it.
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from honest.crossex import build_venue_panel  # noqa: E402
from honest.daily import _xs_weights, build_panel, evaluate  # noqa: E402
from run_daily_lab import UNIVERSE  # noqa: E402

LOG = ROOT / "canary_log.csv"
WINDOW_DAYS = 180
FUNDING_REGIME_TRIGGER_ANN = 0.15


def main() -> int:
    now = datetime.now(timezone.utc).date().isoformat()

    px, f_bin = build_panel(UNIVERSE, WINDOW_DAYS, min_days=60, use_cache=False, verbose=False)
    _, f_byb = build_venue_panel("bybit", UNIVERSE, WINDOW_DAYS, min_days=60, use_cache=False, verbose=False)
    coins = sorted(set(px.columns) & set(f_byb.columns))
    days = px.index.intersection(f_byb.index)
    pxc, fb, fy = px.loc[days, coins], f_bin.loc[days, coins], f_byb.loc[days, coins]

    sh_bin = evaluate(_xs_weights(fb.rolling(7).sum(), 0.2, -1), pxc, fb)["sharpe"]
    sh_byb = evaluate(_xs_weights(fy.rolling(7).sum(), 0.2, -1), pxc, fb)["sharpe"]
    gap = sh_bin - sh_byb

    med_funding_ann = float(f_bin.rolling(30).sum().iloc[-1].median() / 30 * 365)

    alerts = []
    if sh_bin < 0.5:
        alerts.append(f"SIGNAL: Binance-funding Sharpe {sh_bin:+.2f} < 0.5 on trailing {WINDOW_DAYS}d")
    if gap < 0.3 and sh_bin < 1.0:
        alerts.append(f"SIGNAL: Binance edge ({sh_bin:+.2f}) converging to Bybit ({sh_byb:+.2f}) - fading, not spreading")
    if med_funding_ann > FUNDING_REGIME_TRIGGER_ANN:
        alerts.append(f"REGIME: market median funding {med_funding_ann * 100:.1f}%/yr > 15% - reopen basis-carry file")

    new = not LOG.exists()
    with LOG.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["date", "window_days", "sharpe_binance_weights", "sharpe_bybit_weights",
                        "gap", "median_funding_ann_pct", "alerts"])
        w.writerow([now, WINDOW_DAYS, round(sh_bin, 3), round(sh_byb, 3), round(gap, 3),
                    round(med_funding_ann * 100, 2), " | ".join(alerts)])

    print(f"[canary {now}] signal-health: Binance {sh_bin:+.2f} vs Bybit {sh_byb:+.2f} (gap {gap:+.2f}) "
          f"| funding regime: median {med_funding_ann * 100:+.1f}%/yr")
    if alerts:
        for a in alerts:
            print("  ALERT:", a)
        return 1
    print("  all clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
