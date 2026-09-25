"""Cell 1 of RESEARCH_PREREG_Q4_2026.md: rank-band hysteresis on CARRY-7d membership.

WHAT IT TESTS. v1 exits a name the day its funding rank slips past the 0.80 / 0.20
boundary, and 66% of those exits happen within 10 percentiles of the line. Each round
trip costs two legs. v2(h) keeps a name until its rank crosses 0.80-h (short side) or
0.20+h (long side). Same signal, same universe, same costs; only WHEN to act changes.

WHAT IS FIXED (pre-registered, do not touch after results are seen):
  grid          h in {0.05, 0.10}; v1 is the comparator, not a cell; tie-break h=0.05
  nulls         (1) column permutation THROUGH the hysteresis builder
                (2) paired circular block bootstrap (22d, 999) on r_v2 - r_v1, H0 mean <= 0
                (3) blind-delay placebo with turnover matched to v2
  decision      (a)..(g) below, all must hold; hold-out is read once, after discovery
  kill          gross Sharpe(h=0.05) < v1 - 0.15; turnover cut < 25% at h=0.10;
                hold-out net < v1 for both h; sign flips between halves; loses to placebo

WHEN. Not before 2026-10-01. The script refuses real data before that date; the tests
drive it with a synthetic panel through --synthetic, which never touches the cache.

    python run_carry_hyst_lab.py --cheap-check         # discovery, h=0.10 only, ~5 min. Kills or continues.
    python run_carry_hyst_lab.py                       # full discovery run -> reports/carry_hyst_report.json
    python run_carry_hyst_lab.py --holdout             # ONCE, after the discovery verdict is on disk
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from clean_research.carry import block_bootstrap_net_profit_p_value  # noqa: E402
from honest.daily import _xs_weights, build_panel, evaluate, permutation_null_sharpe  # noqa: E402

EARLIEST_RUN = date(2026, 10, 1)
# Rank percentiles are k/n; 1 - Q - h is not. Without a tolerance a name sitting exactly on
# the band edge (rank 0.7 vs 0.7000000000000001) is thrown out by one ulp.
EPS = 1e-9
GRID_H = (0.05, 0.10)
Q = 0.2
LOOKBACK = 7
COST = 0.0010
STRESS = 0.0020
REPORT = ROOT / "reports" / "carry_hyst_report.json"
HOLDOUT_REPORT = ROOT / "reports" / "carry_hyst_holdout_report.json"


# ---------------------------------------------------------------------------
# weight builders
# ---------------------------------------------------------------------------
def v1_weights(signal: pd.DataFrame) -> pd.DataFrame:
    return _xs_weights(signal, q=Q, direction=-1)


def hysteresis_weights(signal: pd.DataFrame, h: float) -> pd.DataFrame:
    """Membership with a no-trade band. Enter at the v1 boundary, exit only past it by h.

    Short side: enter when rank >= 1-Q, stay while rank >= 1-Q-h.
    Long side:  enter when rank <= Q,   stay while rank <= Q+h.
    Equal weight within a side, 0.5 gross per side, decided at close t and held t+1
    (the same shift(1) convention as _xs_weights). h=0 reproduces v1 exactly.
    """
    rank = signal.rank(axis=1, pct=True).to_numpy()
    n_days, n_sym = rank.shape
    state = np.zeros(n_sym, dtype=int)          # +1 long, -1 short, 0 flat
    out = np.zeros_like(rank)
    for t in range(n_days):
        r = rank[t]
        ok = ~np.isnan(r)
        enter_short = ok & (r >= 1 - Q - EPS)
        enter_long = ok & (r <= Q + EPS)
        keep_short = ok & (state == -1) & (r >= 1 - Q - h - EPS)
        keep_long = ok & (state == 1) & (r <= Q + h + EPS)
        new = np.zeros(n_sym, dtype=int)
        new[keep_long] = 1
        new[keep_short] = -1
        new[enter_long] = 1
        new[enter_short] = -1
        state = new
        n_l, n_s = (state == 1).sum(), (state == -1).sum()
        if n_l:
            out[t, state == 1] = 0.5 / n_l
        if n_s:
            out[t, state == -1] = -0.5 / n_s
    w = pd.DataFrame(out, index=signal.index, columns=signal.columns)
    return w.shift(1).fillna(0.0)


def delayed_exit_weights(signal: pd.DataFrame, k: int) -> pd.DataFrame:
    """Placebo: v1 membership, but every EXIT is executed k days late.

    Same entries as v1, so it uses no rank information beyond v1's; it only trades less.
    If hysteresis cannot beat the placebo whose turnover matches its own, the band was
    just "trade less", not "use the rank".
    """
    rank = signal.rank(axis=1, pct=True).to_numpy()
    n_days, n_sym = rank.shape
    state = np.zeros(n_sym, dtype=int)
    since_exit_signal = np.full(n_sym, 10**6)
    out = np.zeros_like(rank)
    for t in range(n_days):
        r = rank[t]
        ok = ~np.isnan(r)
        want = np.where(ok & (r >= 1 - Q - EPS), -1, np.where(ok & (r <= Q + EPS), 1, 0))
        for j in range(n_sym):
            if want[j] != 0:
                state[j] = want[j]
                since_exit_signal[j] = 10**6
            elif state[j] != 0:
                since_exit_signal[j] = 0 if since_exit_signal[j] >= 10**6 else since_exit_signal[j] + 1
                if since_exit_signal[j] >= k or not ok[j]:
                    state[j] = 0
                    since_exit_signal[j] = 10**6
        n_l, n_s = (state == 1).sum(), (state == -1).sum()
        if n_l:
            out[t, state == 1] = 0.5 / n_l
        if n_s:
            out[t, state == -1] = -0.5 / n_s
    return pd.DataFrame(out, index=signal.index, columns=signal.columns).shift(1).fillna(0.0)


def matched_placebo(signal: pd.DataFrame, px: pd.DataFrame, fday: pd.DataFrame,
                    target_turnover: float, max_k: int = 12) -> tuple[int, pd.DataFrame]:
    best_k, best_w, best_gap = 1, None, float("inf")
    for k in range(1, max_k + 1):
        w = delayed_exit_weights(signal, k)
        gap = abs(evaluate(w, px, fday, COST)["avg_daily_turnover"] - target_turnover)
        if gap < best_gap:
            best_k, best_w, best_gap = k, w, gap
    return best_k, best_w


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------
def paired_block_p(r_a: pd.Series, r_b: pd.Series, *, block: int = 22, n_boot: int = 999) -> float:
    """One-sided p for mean(r_a - r_b) > 0 with serial dependence kept (circular blocks)."""
    d = (r_a - r_b).dropna()
    p, _ = block_bootstrap_net_profit_p_value(d, n_boot=n_boot, block_length=block, seed=20261001)
    return float(p)


def halves(daily: pd.Series) -> tuple[float, float]:
    mid = len(daily) // 2
    def sh(x: pd.Series) -> float:
        return float(x.mean() / x.std(ddof=1) * math.sqrt(365)) if x.std(ddof=1) > 0 else float("nan")
    return sh(daily.iloc[:mid]), sh(daily.iloc[mid:])


def worst_day(daily: pd.Series) -> float:
    return float(daily.min() * 1e4)


def cell(signal: pd.DataFrame, px: pd.DataFrame, fday: pd.DataFrame, W: pd.DataFrame, cost: float) -> dict:
    r = evaluate(W, px, fday, cost)
    h1, h2 = halves(r["daily"])
    member = W != 0
    flips = float((member != member.shift(1, fill_value=False)).sum(axis=1).mean())
    return {"sharpe": r["sharpe"], "ann_ret_pct": r["ann_ret_pct"], "max_dd_pct": r["max_dd_pct"],
            "turnover": r["avg_daily_turnover"], "worst_day_bps": worst_day(r["daily"]),
            "membership_flips_per_day": flips, "names_in_book": float(member.sum(axis=1).mean()),
            "sharpe_h1": h1, "sharpe_h2": h2, "daily": r["daily"]}


def strip(d: dict) -> dict:
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items() if k != "daily"}


# ---------------------------------------------------------------------------
# the cell
# ---------------------------------------------------------------------------
def run_universe(px: pd.DataFrame, fday: pd.DataFrame, *, n_perm: int, cheap: bool = False,
                 verbose: bool = True) -> dict:
    signal = fday.rolling(LOOKBACK).sum()
    W1 = v1_weights(signal)
    base = {c: cell(signal, px, fday, W1, c) for c in (0.0, COST, STRESS)}
    out: dict = {"v1": {f"{int(c * 1e4)}bps": strip(base[c]) for c in base}, "cells": {}}
    if verbose:
        b = base[COST]
        print(f"  v1        turnover {b['turnover']:.3f}  flips/day {b['membership_flips_per_day']:.2f}  "
              f"book {b['names_in_book']:.1f}  sharpe {b['sharpe']:+.2f}  "
              f"net {b['ann_ret_pct']:+.1f}%/yr  gross {base[0.0]['ann_ret_pct']:+.1f}%/yr")

    grid = (0.10,) if cheap else GRID_H
    for h in grid:
        W2 = hysteresis_weights(signal, h)
        cells = {c: cell(signal, px, fday, W2, c) for c in (0.0, COST, STRESS)}
        c10, c0, c20 = cells[COST], cells[0.0], cells[STRESS]
        rec: dict = {f"{int(c * 1e4)}bps": strip(cells[c]) for c in cells}
        rec["turnover_ratio"] = c10["turnover"] / base[COST]["turnover"] if base[COST]["turnover"] else float("nan")
        rec["delta_net_ann_pct_10bps"] = c10["ann_ret_pct"] - base[COST]["ann_ret_pct"]
        rec["delta_net_ann_pct_20bps"] = c20["ann_ret_pct"] - base[STRESS]["ann_ret_pct"]
        rec["delta_gross_sharpe"] = c0["sharpe"] - base[0.0]["sharpe"]
        rec["kill_cheap"] = {
            "net_not_better_at_10bps": bool(rec["delta_net_ann_pct_10bps"] <= 0),
            "turnover_cut_below_25pct": bool(rec["turnover_ratio"] > 0.75),
        }
        if verbose:
            print(f"  v2 h={h:.2f} turnover {c10['turnover']:.3f} ({rec['turnover_ratio']:.2f}x)  "
                  f"flips/day {c10['membership_flips_per_day']:.2f}  book {c10['names_in_book']:.1f}  "
                  f"sharpe {c10['sharpe']:+.2f}  net {c10['ann_ret_pct']:+.1f}%/yr  "
                  f"gross {c0['ann_ret_pct']:+.1f}%/yr  d_net10 {rec['delta_net_ann_pct_10bps']:+.1f}pp  "
                  f"d_net20 {rec['delta_net_ann_pct_20bps']:+.1f}pp")
        if not cheap:
            rec["paired_block_p_10bps"] = paired_block_p(c10["daily"], base[COST]["daily"])
            null = permutation_null_sharpe(lambda s, h=h: hysteresis_weights(s, h), signal, px, fday,
                                           c10["sharpe"], n_perm=n_perm, seed=0)
            rec["column_null"] = {k: v for k, v in null.items() if k != "sharpes"}
            k, Wp = matched_placebo(signal, px, fday, c10["turnover"])
            placebo = cell(signal, px, fday, Wp, COST)
            rec["placebo"] = {"delay_days": k, **strip(placebo),
                              "beats_placebo_paired_mean": bool((c10["daily"] - placebo["daily"]).mean() > 0),
                              "paired_p_vs_placebo": paired_block_p(c10["daily"], placebo["daily"])}
            rec["decision"] = decide(rec, base)
            if verbose:
                print(f"           paired p {rec['paired_block_p_10bps']:.3f}  column-null p {rec['column_null'].get('p_value')}  "
                      f"placebo k={k} turnover {placebo['turnover']:.3f} beats={rec['placebo']['beats_placebo_paired_mean']}  "
                      f"-> {'NOMINEE' if rec['decision']['nominee'] else 'no'}")
        out["cells"][f"h={h:.2f}"] = rec
    return out


def decide(rec: dict, base: dict) -> dict:
    c10, c20 = rec["10bps"], rec["20bps"]
    b10, b20 = base[COST], base[STRESS]
    checks = {
        "a_turnover_le_0.75x": rec["turnover_ratio"] <= 0.75,
        "b_net_gain_ge_2pp_at_10bps": rec["delta_net_ann_pct_10bps"] >= 2.0,
        "b_net_gain_ge_4pp_at_20bps": rec["delta_net_ann_pct_20bps"] >= 4.0,
        "c_sharpe_not_below_v1_minus_0.05": c10["sharpe"] >= b10["sharpe"] - 0.05,
        "c_halves_not_worse_than_0.20": (c10["sharpe_h1"] >= b10["sharpe_h1"] - 0.20) and (c10["sharpe_h2"] >= b10["sharpe_h2"] - 0.20),
        "d_paired_block_p_lt_0.025": rec["paired_block_p_10bps"] < 0.025,
        "e_column_null_p_lt_0.025": (rec["column_null"].get("p_value") or 1.0) < 0.025,
        "f_maxdd_not_worse_than_1pp": c10["max_dd_pct"] >= b10["max_dd_pct"] - 1.0,
        "f_worst_day_not_worse_than_100bps": c10["worst_day_bps"] >= b10["worst_day_bps"] - 100.0,
        "g_beats_placebo": rec["placebo"]["beats_placebo_paired_mean"],
    }
    return {"checks": checks, "nominee": all(checks.values())}


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------
def synthetic_panel(n_days: int = 400, n_sym: int = 30, seed: int = 7) -> tuple[pd.DataFrame, pd.DataFrame]:
    """A panel with persistent cross-sectional funding and a small negative funding->return link."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-01", periods=n_days, freq="D")
    cols = [f"S{i:02d}USDT" for i in range(n_sym)]
    f = np.zeros((n_days, n_sym))
    for t in range(1, n_days):
        f[t] = 0.9 * f[t - 1] + rng.normal(0, 0.0004, n_sym)
    ret = rng.normal(0, 0.03, (n_days, n_sym)) - 3.0 * f
    px = pd.DataFrame(100 * np.exp(np.cumsum(ret, axis=0)), index=idx, columns=cols)
    return px, pd.DataFrame(f, index=idx, columns=cols)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cheap-check", action="store_true", help="discovery, h=0.10 only, cost 0 and 10bps; no nulls")
    ap.add_argument("--holdout", action="store_true", help="run the 33-symbol hold-out ONCE, after the discovery report exists")
    ap.add_argument("--n-perm", type=int, default=200)
    ap.add_argument("--days", type=int, default=600)
    ap.add_argument("--synthetic", action="store_true", help="tests only: synthetic panel, never the cache")
    ap.add_argument("--today", default=None, help="tests only")
    args = ap.parse_args(argv)

    today = date.fromisoformat(args.today) if args.today else date.today()
    if not args.synthetic and today < EARLIEST_RUN:
        print(f"REFUSING: pre-registered start is {EARLIEST_RUN}; today is {today}. "
              f"Running early would void the cell (RESEARCH_PREREG_Q4_2026.md).")
        return 3
    if args.holdout and not args.synthetic and not REPORT.exists():
        print("REFUSING: hold-out is read once, after the discovery verdict is on disk (reports/carry_hyst_report.json).")
        return 4

    if args.synthetic:
        px, fday = synthetic_panel()
        tag = "synthetic"
    else:
        from run_carry_holdout import HOLDOUT_UNIVERSE
        from run_daily_lab import UNIVERSE
        symbols = HOLDOUT_UNIVERSE if args.holdout else UNIVERSE
        tag = "holdout" if args.holdout else "discovery"
        px, fday = build_panel(symbols, args.days, verbose=False)
    print(f"CARRY-7d-HYST  {tag}  {px.shape[1]} symbols x {px.shape[0]} days  {'CHEAP CHECK' if args.cheap_check else 'full'}")
    out = run_universe(px, fday, n_perm=args.n_perm, cheap=args.cheap_check)
    out.update({"universe": tag, "as_of": str(today), "n_symbols": int(px.shape[1]), "n_days": int(px.shape[0])})

    if args.cheap_check:
        k = out["cells"]["h=0.10"]["kill_cheap"]
        dead = any(k.values())
        print(f"\nCHEAP CHECK: {'DEAD - do not spend the slot' if dead else 'alive - proceed to the full run'}  {k}")
        return 1 if dead else 0
    target = HOLDOUT_REPORT if args.holdout else REPORT
    if not args.synthetic:
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(out, indent=2, default=float), encoding="utf-8")
        print(f"-> {target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
