"""CARRY-7d paper executor - the 60-120 day forward test, and nothing else.

This is deliberately NOT the old bot. No gates, no models, no multipliers: one rule,
one config, one ledger. Run it once a day (or whenever - see catch-up below):

    python run_carry_paper.py            # process all completed days since last run
    python run_carry_paper.py --status   # just print where the paper period stands

Design decisions, each with a reason:

  CATCH-UP BY CONSTRUCTION. Decisions are deterministic functions of daily closes and
  settled funding, both fetchable retroactively. A missed day is reconstructed exactly as
  it would have been traded, so the paper record has no holes and no survivor bias from
  "the machine was off".

  NEXT-OPEN FILLS. The signal uses data through the close of day t; fills happen at the
  open of day t+1 - a price that exists AFTER the decision. Same convention the parallel
  clean-OOS pipeline adopted, and strictly harder than the backtest's at-close fills.

  CONSERVATIVE FUNDING BOUNDARY. The 00:00 UTC settlement of day t goes to the weights
  held BEFORE that day's rebalance (audit finding FA-2: paying it to the fresh weights
  flatters carry).

  NO-TUNE LOCK. The config's sha256 is recorded at first run; any later mismatch aborts.
  Editing the config restarts the paper clock - the point of the lock is that a 60-day
  record of rule X is worthless if rule X drifted along the way.

State: carry_paper_state.json.  Ledger: carry_paper_ledger.csv (one row per day).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Task Scheduler launches with an arbitrary CWD; make the repo importable regardless.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

from honest.daily import build_panel
from honest.data import fetch_klines

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "carry_paper_config_v1.json"
STATE_PATH = ROOT / "carry_paper_state.json"
LEDGER_PATH = ROOT / "carry_paper_ledger.csv"

TRADING_DAYS = 365


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def build_opens(symbols: list[str], days: int) -> pd.DataFrame:
    """Open-price panel (build_panel only exposes closes; fills need opens)."""
    opens = {}
    for sym in symbols:
        try:
            k = fetch_klines(sym, "1d", days, use_cache=False)
            idx = pd.to_datetime(k["Open time"]).dt.normalize()
            s = k.set_index(idx)["Open"]
            opens[sym] = s[~s.index.duplicated(keep="last")]
        except Exception:
            pass
    return pd.DataFrame(opens).sort_index()


def target_weights(fday: pd.DataFrame, day: pd.Timestamp, lookback: int, q: float) -> pd.Series:
    """CARRY weights decided at the close of `day` - identical math to the lab's
    _xs_weights(direction=-1) for a single row."""
    sig = fday.rolling(lookback).sum().loc[day]
    rank = sig.rank(pct=True)
    n_top = int((rank >= 1 - q).sum())
    n_bot = int((rank <= q).sum())
    w = pd.Series(0.0, index=sig.index)
    if n_top:
        w[rank >= 1 - q] = -0.5 / n_top   # crowded longs: short them
    if n_bot:
        w[rank <= q] = +0.5 / n_bot       # crowded shorts: long them
    if not n_top or not n_bot:
        w[:] = 0.0                        # one-sided book -> stand flat, same as the lab
    return w


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg_hash = _sha(CONFIG_PATH)
    state = load_state()

    if state.get("config_sha256") and state["config_sha256"] != cfg_hash:
        print("REFUSING TO RUN: carry_paper_config_v1.json changed since the paper period began.")
        print(f"  recorded {state['config_sha256'][:16]}...  current {cfg_hash[:16]}...")
        print("  The no-tune pledge means a changed rule = a new experiment. Restore the old")
        print("  config, or delete carry_paper_state.json + ledger to start a fresh period.")
        return 2

    if args.status and not state:
        print("Paper period has not started - run without --status first.")
        return 0

    # --- data ------------------------------------------------------------------
    days_back = max(60, cfg["funding_lookback_days"] + 30)
    px, fday = build_panel(cfg["universe"], days=days_back, min_days=10,
                           use_cache=False, verbose=False)
    opens = build_opens(cfg["universe"], days_back).reindex(columns=px.columns)

    paper_start = pd.Timestamp(cfg["paper_start_utc"])
    today = pd.Timestamp(datetime.now(timezone.utc).date())
    # Signal days: complete days from which we can decide. A decision on day t fills at
    # open of t+1, so the last usable signal day is the last day whose NEXT day exists
    # in the open panel (today's open is known once today has started).
    signal_days = [d for d in px.index
                   if d >= paper_start - pd.Timedelta(days=1)
                   and d < today
                   and (d + pd.Timedelta(days=1)) in opens.index]

    last_done = pd.Timestamp(state["last_signal_day"]) if state.get("last_signal_day") else None
    todo = [d for d in signal_days if last_done is None or d > last_done]

    prev_w = pd.Series(state.get("weights", {}), dtype=float).reindex(px.columns).fillna(0.0)
    equity = float(state.get("equity", 1.0))
    rows = []

    for d in todo:
        fill_day = d + pd.Timedelta(days=1)
        w = target_weights(fday, d, cfg["funding_lookback_days"], cfg["quantile"])
        w = w.where(px.loc[d].notna() & opens.loc[fill_day].notna(), 0.0)

        # PnL attributed to fill_day: open(fill_day) -> next known open (or latest close
        # for the still-running day). Funding: 08:00/16:00 of fill_day to the NEW weights,
        # 00:00 of fill_day to the OLD weights (conservative boundary rule).
        # Only book a day once its exit price (next day's open) exists. A partial
        # open-to-close mark was previously written to the ledger and never revised,
        # leaving a permanent overnight-gap error on every catch-up boundary. The
        # still-running day is now displayed but not persisted; it books tomorrow.
        nxt = fill_day + pd.Timedelta(days=1)
        if nxt not in opens.index:
            break
        ret = (opens.loc[nxt] / opens.loc[fill_day] - 1).fillna(0.0)
        mark = "open_to_open"

        f_day = fday.loc[fill_day].fillna(0.0) if fill_day in fday.index else pd.Series(0.0, index=px.columns)
        # Approximation note: fday aggregates the whole day; the 00:00 slice is ~1/3.
        f_new = f_day * (2 / 3)
        f_old = f_day * (1 / 3)

        turnover = (w - prev_w).abs().sum()
        pnl = float((w * ret).sum()
                    + (-w * f_new).sum() + (-prev_w * f_old).sum()
                    - turnover * cfg["cost_per_leg"])
        equity *= (1 + pnl)

        n_short = int((w < 0).sum())
        n_long = int((w > 0).sum())
        rows.append({
            "signal_day": d.date(), "fill_day": fill_day.date(), "mark": mark,
            "n_long": n_long, "n_short": n_short, "turnover": round(float(turnover), 4),
            "pnl": round(pnl, 6), "equity": round(equity, 6),
            "shorts": ",".join(sorted(w[w < 0].index)), "longs": ",".join(sorted(w[w > 0].index)),
            "run_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        prev_w = w

    if rows:
        df = pd.DataFrame(rows)
        header = not LEDGER_PATH.exists()
        df.to_csv(LEDGER_PATH, mode="a", header=header, index=False, encoding="utf-8")

    if rows:
        # last_signal_day must be the last day actually BOOKED, not the last day attempted.
        # The loop breaks on the first day whose exit open does not exist yet; recording
        # todo[-1] there labelled a day as done that was never written, so it was skipped
        # forever on the next run and state.weights (still the previous day's) carried the
        # wrong date. Book-keeping only: the rule and every booked number are unchanged.
        booked_last = pd.Timestamp(rows[-1]["signal_day"])
        state.update({
            "config_sha256": cfg_hash,
            "started_utc": state.get("started_utc", datetime.now(timezone.utc).isoformat(timespec="seconds")),
            "last_signal_day": str(booked_last.date()),
            "weights": {k: round(v, 6) for k, v in prev_w.items() if v != 0.0},
            "equity": equity,
        })
        save_state(state)

    # --- status report -----------------------------------------------------------
    if not LEDGER_PATH.exists():
        print("No paper days processed yet (paper starts", cfg["paper_start_utc"], ")")
        return 0

    led = pd.read_csv(LEDGER_PATH)
    daily = led["pnl"].astype(float)
    n = len(led)
    sd = daily.std(ddof=1)
    sharpe = daily.mean() / sd * np.sqrt(TRADING_DAYS) if n > 2 and sd > 0 else float("nan")
    eq = (1 + daily).cumprod()
    dd = float((eq / eq.cummax() - 1).min() * 100)
    gate = cfg["go_live_gate"]

    print("=" * 70)
    print(f"CARRY-7d PAPER  day {n}/{gate['min_paper_days']} (target {gate['recommended_paper_days']})")
    print("=" * 70)
    print(f"  booked {len(rows)} new day(s) this run ({len(todo) - len(rows)} pending next open)")
    print(f"  equity {eq.iloc[-1]:.4f}  total {(eq.iloc[-1] - 1) * 100:+.2f}%  "
          f"sharpe-to-date {sharpe:+.2f}  maxDD {dd:.1f}%")
    print(f"  gate: >= {gate['min_paper_days']}d AND sharpe > {gate['paper_sharpe_min']} AND "
          f"total > {gate['paper_total_return_min_pct']}% AND DD > {gate['max_paper_drawdown_pct']}%")
    if n >= gate["min_paper_days"]:
        ok = (sharpe > gate["paper_sharpe_min"]
              and (eq.iloc[-1] - 1) * 100 > gate["paper_total_return_min_pct"]
              and dd > gate["max_paper_drawdown_pct"])
        print(f"  GATE {'PASSES - eligible for small-capital live decision' if ok else 'FAILS - do not go live; do not tune'}")
    else:
        print(f"  gate not yet evaluable ({gate['min_paper_days'] - n} days to go)")
    print(f"  backtest reference: sharpe {cfg['backtest_reference']['discovery_sharpe']} "
          f"ann {cfg['backtest_reference']['discovery_ann_pct']}%  "
          f"(live_enabled={cfg['live_enabled']}, capital={cfg['capital_authorized_usd']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
