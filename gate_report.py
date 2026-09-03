"""Day-60 GO / NO-GO / NOT-YET, computed - not eyeballed.

GO_LIVE_CHECKLIST.md lists the conditions that must ALL hold before real capital is
authorised. status.py shows the raw numbers; this script applies the thresholds and
prints one verdict, so the decision on ~2026-10-02 is read off a screen, not argued.

    python gate_report.py                      # evaluate as of today, write reports/GATE_REPORT.md
    python gate_report.py --today 2026-10-02   # what the same data would say on the gate day
    python gate_report.py --root <dir>         # tests: point at a directory with synthetic inputs

Inputs, ALL local (no network, nothing is written except the report):
    carry_paper_config_v1.json       thresholds (go_live_gate, cost_per_leg, paper_start_utc)
    carry_paper_ledger.csv           paper days, return, Sharpe, drawdown, staleness
    .execution/testnet_execution.sqlite3   COMPLETE rehearsal runs (dry_run=0) since TESTNET_START
    carry_testnet_log.csv            reconcile exit code of each COMPLETE run (if present)
    tracking_log.csv                 paper-vs-actual divergence per calendar week (if present)
    execution_quality.csv            fill shortfall vs the paper cost assumption (if present)
    canary_log.csv                   signal-health canary freshness and alerts (if present)
    carry_paper_incidents.md         every incident has a written resolution (if present)
    .execution/{ATTENTION,canary_ALERT,testnet_daily.lock}   markers that block runs

Each condition is PASS / FAIL / NOT-YET with the measured value and the threshold.
    GO       every condition PASS                                   exit 0
    NO-GO    a FAIL that waiting cannot undo (drawdown breach, tracking breach,
             negative Sharpe at day 60, any paper miss at day 90)    exit 1
    NOT-YET  everything else; prints the earliest evaluable date and
             what must happen by then                                exit 2

Any missing input degrades to a clear NOT-YET line; the script never crashes on data.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

# Not in the config file: the checklist (Part 3) fixes 20 COMPLETE runs, and the
# rehearsal only counts from the day the $2000 ceiling was in force (same as status.py).
TESTNET_START = date(2026, 8, 25)
TESTNET_COMPLETE_REQUIRED = 20
TRACKING_WEEKLY_LIMIT_PCT = 1.0        # checklist Part 6: > 1%/week ...
TRACKING_CONSECUTIVE_WEEKS = 3         # ... for 3 consecutive weeks -> stop
CANARY_STALE_DAYS = 8                  # weekly task; > 8 days means it is not running
CANARY_LOOKBACK_ROWS = 4
PAPER_STALE_DAYS = 2                   # a fill day books at the next open, so 1-2 days lag is normal
MARKERS = ("ATTENTION", "canary_ALERT", "testnet_daily.lock")

# Mirrors carry_paper_config_v1.json["go_live_gate"]; used ONLY when that file is unreadable.
DEFAULT_GATE: dict[str, float] = {
    "min_paper_days": 60, "recommended_paper_days": 90, "paper_sharpe_min": 0.5,
    "paper_total_return_min_pct": 0.0, "max_paper_drawdown_pct": -20.0,
}
DEFAULT_COST_PER_LEG = 0.0010
DEFAULT_PAPER_START = "2026-08-03"

PASS, FAIL, NOT_YET = "PASS", "FAIL", "NOT-YET"


@dataclass
class Condition:
    name: str
    status: str
    measured: str
    threshold: str
    note: str = ""
    terminal: bool = False      # a FAIL that waiting cannot undo -> NO-GO
    flip: str = ""              # what would change this line's status


@dataclass
class GateResult:
    today: date
    conditions: list[Condition]
    verdict: str
    exit_code: int
    earliest: date | None
    must_happen: list[str]
    facts: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# input readers - each returns None (plus a note) when its file is absent/unreadable
# ---------------------------------------------------------------------------
def _read_csv(path: Path) -> list[dict[str, str]] | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _read_config(root: Path, notes: list[str]) -> tuple[dict[str, float], float, date]:
    path = root / "carry_paper_config_v1.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        gate = {k: float(cfg["go_live_gate"].get(k, v)) for k, v in DEFAULT_GATE.items()}
        return gate, float(cfg.get("cost_per_leg", DEFAULT_COST_PER_LEG)), \
            date.fromisoformat(cfg.get("paper_start_utc", DEFAULT_PAPER_START))
    except Exception as exc:  # noqa: BLE001 - degrade, never crash
        notes.append(f"config {path.name} unreadable ({type(exc).__name__}); using built-in defaults")
        return dict(DEFAULT_GATE), DEFAULT_COST_PER_LEG, date.fromisoformat(DEFAULT_PAPER_START)


def paper_stats(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Return, Sharpe, drawdowns, worst month from ledger rows (pnl = daily fraction)."""
    pnl = [float(r["pnl"]) for r in rows]
    eq, peak, maxdd = 1.0, 1.0, 0.0
    months: dict[str, float] = {}
    for r, p in zip(rows, pnl):
        eq *= 1 + p
        peak = max(peak, eq)
        maxdd = min(maxdd, eq / peak - 1)
        m = r["fill_day"][:7]
        months[m] = months.get(m, 1.0) * (1 + p)
    n = len(pnl)
    sd = statistics.stdev(pnl) if n > 2 else 0.0
    sharpe = statistics.mean(pnl) / sd * (365 ** 0.5) if sd > 0 else float("nan")
    worst_m = min(months.items(), key=lambda kv: kv[1]) if months else ("-", 1.0)
    return {
        "days": n, "equity": eq, "total_pct": (eq - 1) * 100, "sharpe": sharpe,
        "maxdd_pct": maxdd * 100, "current_dd_pct": (eq / peak - 1) * 100, "peak": peak,
        "worst_month": worst_m[0], "worst_month_pct": (worst_m[1] - 1) * 100,
        "last_fill_day": date.fromisoformat(rows[-1]["fill_day"]),
    }


def weekly_tracking(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], float | None]:
    """Per ISO-calendar-week divergence in % of budget, plus the budget inferred from the log."""
    budget: float | None = None
    for r in rows:
        pct = float(r.get("cum_diff_pct_of_budget") or 0)
        if pct:
            budget = float(r["cum_diff_usd"]) / pct * 100
            break
    weeks: dict[tuple[int, int], dict[str, Any]] = {}
    for r in rows:
        d = date.fromisoformat(r["day"])
        key = d.isocalendar()[:2]
        w = weeks.setdefault(key, {"week": f"{key[0]}-W{key[1]:02d}", "days": 0, "diff_usd": 0.0,
                                   "first": d, "last": d})
        w["days"] += 1
        w["diff_usd"] += float(r["diff_usd"])
        w["first"], w["last"] = min(w["first"], d), max(w["last"], d)
    out = []
    for key in sorted(weeks):
        w = weeks[key]
        w["pct"] = (w["diff_usd"] / budget * 100) if budget else None
        w["breach"] = bool(w["pct"] is not None and abs(w["pct"]) > TRACKING_WEEKLY_LIMIT_PCT)
        out.append(w)
    return out, budget


def _consecutive_breaches(weeks: list[dict[str, Any]]) -> tuple[int, int]:
    """(longest run of adjacent breaching weeks, length of the run ending at the last week)."""
    longest = current = 0
    prev_end: date | None = None
    for w in weeks:
        adjacent = prev_end is not None and (w["first"] - prev_end).days <= 7
        current = (current + 1 if adjacent else 1) if w["breach"] else 0
        longest = max(longest, current)
        prev_end = w["last"]
    return longest, current


def _testnet_runs(db: Path) -> list[tuple[str, str, str]]:
    """(day, status, message) of every non-dry run, oldest first. Opened read-only."""
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        conn = sqlite3.connect(str(db))
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT substr(started_utc,1,10), status, COALESCE(message,'') FROM execution_runs "
            "WHERE dry_run=0 ORDER BY started_utc")]
    finally:
        conn.close()


def _open_incidents(path: Path) -> tuple[int, list[str]]:
    """Sections ('## ' headings) without a resolution paragraph ('**Xu ly' / '**Resolved' / '**Closed')."""
    text = path.read_text(encoding="utf-8")
    sections = [s for s in text.split("\n## ") if s.strip()]
    if text.startswith("## "):
        sections[0] = sections[0][3:]
    resolved_marks = ("**xử lý", "**resolved", "**closed", "**đã xử lý")
    open_ones = []
    for s in sections:
        low = s.lower()
        if not any(m in low for m in resolved_marks):
            open_ones.append(s.splitlines()[0].strip()[:60])
    return len(sections), open_ones


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------
def evaluate(root: Path, today: date) -> GateResult:
    notes: list[str] = []
    gate, cost_per_leg, paper_start = _read_config(root, notes)
    conds: list[Condition] = []
    must: list[str] = []
    facts: dict[str, Any] = {}
    earliest_candidates: list[date] = []

    min_days = int(gate["min_paper_days"])
    rec_days = int(gate["recommended_paper_days"])
    sharpe_min = gate["paper_sharpe_min"]
    ret_min = gate["paper_total_return_min_pct"]
    dd_min = gate["max_paper_drawdown_pct"]

    # ---- paper record ------------------------------------------------------
    ledger = _read_csv(root / "carry_paper_ledger.csv")
    if not ledger:
        day60 = paper_start + timedelta(days=min_days)
        earliest_candidates.append(day60)
        conds.append(Condition("paper days", NOT_YET, "0 (no ledger)", f">= {min_days}",
                               "carry_paper_ledger.csv missing or empty", flip="paper executor must book days"))
        for nm in ("paper Sharpe (ann.)", "paper total return", "paper max drawdown", "paper record current"):
            conds.append(Condition(nm, NOT_YET, "n/a", "-", "no ledger"))
        must.append(f"paper ledger must exist and reach {min_days} days (~{day60})")
        facts["paper"] = None
    else:
        st = paper_stats(ledger)
        facts["paper"] = st
        n = st["days"]
        remaining = max(min_days - n, 0)
        day60 = st["last_fill_day"] + timedelta(days=remaining + 1)   # a fill day books at the next open
        evaluable = n >= min_days
        extended = n >= rec_days
        if evaluable:
            conds.append(Condition("paper days", PASS, str(n), f">= {min_days}",
                                   f"day {n}; recommended {rec_days}" + (" reached" if extended else "")))
        else:
            earliest_candidates.append(day60)
            conds.append(Condition("paper days", NOT_YET, str(n), f">= {min_days}",
                                   f"day {min_days} books on {day60} ({(day60 - today).days}d)",
                                   flip=f"{remaining} more booked days"))
            must.append(f"paper must book {remaining} more days without interruption (day {min_days} on {day60})")

        # Sharpe / total return: measured now, judged at day 60 (extendable to day 90 per config note)
        def _paper_metric(name: str, value: float, thresh: float, ok: bool, fmt: str, unit: str) -> None:
            meas = fmt.format(value) + unit
            thr = f"> {fmt.format(thresh)}{unit}"
            if not evaluable:
                conds.append(Condition(name, NOT_YET, meas, thr,
                                       f"so far would {'PASS' if ok else 'FAIL'}; judged at day {min_days}",
                                       flip=f"stays {'above' if ok else 'below'} {fmt.format(thresh)}{unit} at day {min_days}"))
            elif ok:
                conds.append(Condition(name, PASS, meas, thr, flip=f"drops below {fmt.format(thresh)}{unit}"))
            elif extended:
                # The pre-registered rule (config go_live_gate.note, checklist Part 3) is
                # unconditional: a day-60 miss extends to day 90, whatever the sign. Only a
                # day-90 miss is terminal. No judgement call is added here on purpose.
                conds.append(Condition(name, FAIL, meas, thr,
                                       f"day {rec_days} reached - config says stop, do not tune", terminal=True))
            else:
                day90 = st["last_fill_day"] + timedelta(days=rec_days - n + 1)
                earliest_candidates.append(day90)
                conds.append(Condition(name, FAIL, meas, thr,
                                       f"config note: extend to day {rec_days} ({day90}); stop if still failing",
                                       flip=f"recovers above {fmt.format(thresh)}{unit} by day {rec_days}"))
                must.append(f"{name} must recover above {fmt.format(thresh)}{unit} by day {rec_days} ({day90})")

        sh = st["sharpe"]
        _paper_metric("paper Sharpe (ann.)", sh, sharpe_min, sh == sh and sh > sharpe_min, "{:+.2f}", "")
        _paper_metric("paper total return", st["total_pct"], ret_min, st["total_pct"] > ret_min, "{:+.2f}", "%")

        dd = st["maxdd_pct"]
        room = (st["peak"] * (1 + dd_min / 100) / st["equity"] - 1) * 100
        if dd <= dd_min:
            conds.append(Condition("paper max drawdown", FAIL, f"{dd:.1f}%", f"> {dd_min:.0f}%",
                                   "breached in the paper record - cannot un-happen", terminal=True))
        else:
            conds.append(Condition("paper max drawdown", PASS, f"{dd:.1f}%", f"> {dd_min:.0f}%",
                                   f"current DD {st['current_dd_pct']:.1f}%; worst month {st['worst_month']} {st['worst_month_pct']:+.1f}%",
                                   flip=f"equity falls another {abs(room):.1f}% from here"))

        lag = (today - st["last_fill_day"]).days
        if lag <= PAPER_STALE_DAYS:
            conds.append(Condition("paper record current", PASS, f"last fill {st['last_fill_day']} ({lag}d ago)",
                                   f"<= {PAPER_STALE_DAYS}d", flip="paper task stops booking"))
        else:
            conds.append(Condition("paper record current", FAIL, f"last fill {st['last_fill_day']} ({lag}d ago)",
                                   f"<= {PAPER_STALE_DAYS}d", "paper task not booking - it catches up by itself once the machine is awake at 00:05 UTC",
                                   flip="next paper run catches up"))
            must.append("paper task must catch up (last booked day is stale)")

    # ---- testnet rehearsal ---------------------------------------------------
    db = root / ".execution" / "testnet_execution.sqlite3"
    runs: list[tuple[str, str, str]] | None = None
    if db.exists():
        try:
            runs = _testnet_runs(db)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"audit DB unreadable ({type(exc).__name__}: {exc})")
    if runs is None:
        conds.append(Condition("testnet COMPLETE runs", NOT_YET, "0 (no audit DB)", f">= {TESTNET_COMPLETE_REQUIRED}",
                               "no .execution/testnet_execution.sqlite3", flip="unattended rehearsal must run"))
        conds.append(Condition("testnet exposure clean", NOT_YET, "n/a", "no HALTED_*/MISMATCH", "no audit DB"))
        earliest_candidates.append(today + timedelta(days=TESTNET_COMPLETE_REQUIRED))
        must.append(f"{TESTNET_COMPLETE_REQUIRED} COMPLETE testnet runs (none recorded)")
        facts["testnet"] = None
    else:
        since = [r for r in runs if date.fromisoformat(r[0]) >= TESTNET_START]
        complete_days = sorted({d for d, s, _ in since if s == "COMPLETE"})
        other = [(d, s) for d, s, _ in since if s != "COMPLETE"]
        n_c = len(complete_days)
        need = max(TESTNET_COMPLETE_REQUIRED - n_c, 0)
        facts["testnet"] = {"complete": n_c, "other": other, "last_run": runs[-1][0] if runs else None,
                            "last_complete": complete_days[-1] if complete_days else None}
        other_txt = ", ".join(f"{d}:{s}" for d, s in other) or "none"
        if need == 0:
            conds.append(Condition("testnet COMPLETE runs", PASS, str(n_c), f">= {TESTNET_COMPLETE_REQUIRED}",
                                   f"since {TESTNET_START}; non-COMPLETE: {other_txt}"))
        else:
            t_date = today + timedelta(days=need)
            earliest_candidates.append(t_date)
            conds.append(Condition("testnet COMPLETE runs", NOT_YET, str(n_c), f">= {TESTNET_COMPLETE_REQUIRED}",
                                   f"since {TESTNET_START}; {need} more at one/day -> {t_date}; non-COMPLETE: {other_txt}",
                                   flip=f"{need} more COMPLETE runs"))
            must.append(f"needs {need} more COMPLETE testnet runs")
        try:
            from execution.contracts import EXPOSURE_STATUSES  # noqa: WPS433 - local import keeps the report standalone
        except Exception:  # noqa: BLE001
            EXPOSURE_STATUSES = frozenset({"HALTED_MID_BOOK", "HALTED_AUDIT_UNAVAILABLE", "HALTED_CANCEL_FAILED",
                                           "EXTERNAL_POSITION_DRIFT", "EXTERNAL_DRIFT_CANCEL_FAILED",
                                           "UNRESOLVED_EXPOSURE", "VERIFICATION_UNAVAILABLE", "MISMATCH", "RUNNING"})
        last_status = runs[-1][1] if runs else "-"
        if runs and last_status in EXPOSURE_STATUSES:
            conds.append(Condition("testnet exposure clean", FAIL, f"last run {runs[-1][0]} {last_status}",
                                   "no HALTED_*/MISMATCH", "hand-off state: resolve per EXECUTION_RUNBOOK.md",
                                   flip="a later COMPLETE run + reconcile exit 0"))
            must.append(f"resolve the {last_status} run of {runs[-1][0]}")
        else:
            conds.append(Condition("testnet exposure clean", PASS, f"last run {runs[-1][0] if runs else '-'} {last_status}",
                                   "no HALTED_*/MISMATCH", flip="any run ending in a hand-off state"))

    # reconcile exit codes of COMPLETE runs (checklist: COMPLETE + reconcile exit 0)
    tlog = _read_csv(root / "carry_testnet_log.csv")
    if tlog:
        bad = [r["utc"][:10] for r in tlog if r.get("status") == "COMPLETE"
               and (r.get("reconcile_exit") or "0") not in ("0", "0.0")]
        if bad:
            conds.append(Condition("testnet reconcile exit 0", FAIL, f"{len(bad)} COMPLETE run(s) with non-zero reconcile",
                                   "0 such runs", ", ".join(bad[-5:]), flip="understand and close each one"))
            must.append("close the COMPLETE runs whose reconcile exited non-zero")
        else:
            n_ok = sum(1 for r in tlog if r.get("status") == "COMPLETE")
            conds.append(Condition("testnet reconcile exit 0", PASS, f"{n_ok} COMPLETE run(s), all reconcile 0", "0 non-zero"))

    # ---- tracking error --------------------------------------------------------
    trows = _read_csv(root / "tracking_log.csv")
    if not trows:
        conds.append(Condition("tracking error", NOT_YET, "no tracking_log.csv",
                               f"no {TRACKING_CONSECUTIVE_WEEKS} consecutive weeks > {TRACKING_WEEKLY_LIMIT_PCT:.0f}%/wk",
                               "run track_paper_vs_testnet.py after COMPLETE runs exist", flip="tracking log with no breach"))
        must.append("tracking_log.csv must exist (track_paper_vs_testnet.py)")
        facts["tracking"] = None
    else:
        weeks, budget = weekly_tracking(trows)
        longest, current_run = _consecutive_breaches(weeks)
        cum_pct = float(trows[-1].get("cum_diff_pct_of_budget") or 0)
        n_days = len(trows)
        per_week = cum_pct / max(n_days / 7, 1e-9)
        facts["tracking"] = {"weeks": weeks, "budget": budget, "cum_pct": cum_pct, "days": n_days,
                             "per_week_pct": per_week, "longest": longest, "current_run": current_run}
        thr = f"no {TRACKING_CONSECUTIVE_WEEKS} consecutive weeks |div| > {TRACKING_WEEKLY_LIMIT_PCT:.0f}%/wk"
        meas = f"cum {cum_pct:+.2f}% over {n_days}d (~{per_week:+.2f}%/wk), {len(weeks)} wk"
        if budget is None:
            conds.append(Condition("tracking error", NOT_YET, meas, thr, "budget not inferable from log"))
        elif longest >= TRACKING_CONSECUTIVE_WEEKS:
            breached = [w["week"] for w in weeks if w["breach"]]
            conds.append(Condition("tracking error", FAIL, meas, thr,
                                   f"stop rule hit: {longest} consecutive breaching weeks ({', '.join(breached[-longest:])})",
                                   terminal=True))
        else:
            note = (f"warning: {current_run} consecutive breaching week(s) so far" if current_run
                    else "no breaching week" + (f" (partial: {weeks[-1]['days']}d in {weeks[-1]['week']})" if weeks and weeks[-1]['days'] < 7 else ""))
            conds.append(Condition("tracking error", PASS, meas, thr, note,
                                   flip=f"{TRACKING_CONSECUTIVE_WEEKS - current_run} more consecutive week(s) beyond {TRACKING_WEEKLY_LIMIT_PCT:.0f}%"))

    # ---- fill quality ----------------------------------------------------------
    qrows = _read_csv(root / "execution_quality.csv")
    cost_bps = cost_per_leg * 1e4
    if not qrows:
        conds.append(Condition("fill shortfall", NOT_YET, "no execution_quality.csv", f"mean <= {cost_bps:.0f} bps",
                               "run analyze_execution_quality.py after COMPLETE runs", flip="fill analysis with mean inside cost"))
        must.append("execution_quality.csv must exist (analyze_execution_quality.py)")
        facts["fills"] = None
    else:
        s = sorted(float(r["shortfall_bps"]) for r in qrows if r.get("shortfall_bps") not in (None, ""))
        if s:
            mean = sum(s) / len(s)
            med = statistics.median(s)
            p90 = s[int(0.9 * (len(s) - 1))]
            facts["fills"] = {"legs": len(s), "mean": mean, "median": med, "p90": p90}
            meas = f"{len(s)} legs mean {mean:+.1f} med {med:+.1f} p90 {p90:+.1f} bps"
            if mean > cost_bps:
                conds.append(Condition("fill shortfall", FAIL, meas, f"mean <= {cost_bps:.0f} bps",
                                       "fills cost more than the paper assumes - fix order style/timing, re-measure",
                                       flip=f"mean back under {cost_bps:.0f} bps"))
                must.append(f"mean fill shortfall must come back under {cost_bps:.0f} bps")
            else:
                conds.append(Condition("fill shortfall", PASS, meas, f"mean <= {cost_bps:.0f} bps",
                                       flip=f"mean exceeds {cost_bps:.0f} bps"))
        else:
            facts["fills"] = None
            conds.append(Condition("fill shortfall", NOT_YET, "0 legs", f"mean <= {cost_bps:.0f} bps", "file has no rows"))

    # ---- signal-health canary --------------------------------------------------
    crows = _read_csv(root / "canary_log.csv")
    if not crows:
        conds.append(Condition("signal canary", NOT_YET, "never run", f"fresh (<= {CANARY_STALE_DAYS}d), no ALERT",
                               "run_canaries.py (weekly task)", flip="a fresh clear canary row"))
        must.append("canary must run (run_canaries.py)")
        facts["canary"] = None
    else:
        last = crows[-1]
        age = (today - date.fromisoformat(last["date"])).days
        recent = crows[-CANARY_LOOKBACK_ROWS:]
        alerts = [(r["date"], r.get("alerts", "")) for r in recent if (r.get("alerts") or "").strip()]
        facts["canary"] = {"last": last, "age": age, "alerts": alerts}
        meas = (f"{last['date']} ({age}d ago) Binance {float(last['sharpe_binance_weights']):+.2f} "
                f"vs Bybit {float(last['sharpe_bybit_weights']):+.2f}")
        thr = f"fresh (<= {CANARY_STALE_DAYS}d), no ALERT in last {CANARY_LOOKBACK_ROWS}"
        if alerts:
            conds.append(Condition("signal canary", FAIL, meas, thr,
                                   "ALERT: " + "; ".join(f"{d}: {a}" for d, a in alerts)[:200],
                                   flip=f"{CANARY_LOOKBACK_ROWS} consecutive clear rows"))
            must.append("canary alerts must clear (edge fading / regime change)")
        elif age > CANARY_STALE_DAYS:
            conds.append(Condition("signal canary", NOT_YET, meas, thr, "STALE - weekly task not running?",
                                   flip="a fresh clear row"))
            must.append("canary must run again (stale)")
        else:
            conds.append(Condition("signal canary", PASS, meas, thr, "clear", flip="any ALERT row"))

    # ---- markers ------------------------------------------------------------------
    ex = root / ".execution"
    present = [m for m in MARKERS if (ex / m).exists()]
    facts["markers"] = present
    if present:
        conds.append(Condition("no blocking markers", FAIL, ", ".join(present), "none",
                               "runs are blocked until resolved per EXECUTION_RUNBOOK.md", flip="resolve and delete each marker"))
        must.append("resolve markers: " + ", ".join(present))
    else:
        conds.append(Condition("no blocking markers", PASS, "none", "none", flip="ATTENTION/canary_ALERT/lock appears"))

    # ---- incidents -----------------------------------------------------------------
    inc = root / "carry_paper_incidents.md"
    if inc.exists():
        try:
            total, open_ones = _open_incidents(inc)
            if open_ones:
                conds.append(Condition("incidents closed", FAIL, f"{len(open_ones)}/{total} open", "all closed",
                                       "; ".join(open_ones)[:160], flip="write a resolution under each"))
                must.append(f"close {len(open_ones)} open incident(s) in carry_paper_incidents.md")
            else:
                conds.append(Condition("incidents closed", PASS, f"{total} recorded, all closed", "all closed",
                                       flip="a new ATTENTION incident"))
        except Exception as exc:  # noqa: BLE001
            conds.append(Condition("incidents closed", NOT_YET, "unreadable", "all closed", f"{type(exc).__name__}"))
    else:
        conds.append(Condition("incidents closed", PASS, "no incidents file", "all closed"))

    # ---- verdict ---------------------------------------------------------------------
    if any(c.status == FAIL and c.terminal for c in conds):
        verdict, code = "NO-GO", 1
    elif all(c.status == PASS for c in conds):
        verdict, code = "GO", 0
    else:
        verdict, code = "NOT-YET", 2
    earliest = max(earliest_candidates) if earliest_candidates else (today if verdict != "GO" else None)
    if earliest is not None and earliest < today:
        earliest = today
    return GateResult(today, conds, verdict, code, earliest, must, facts, notes)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def _flip_lines(res: GateResult) -> list[str]:
    lines = []
    for c in res.conditions:
        if not c.flip:
            continue
        if c.status == PASS:
            lines.append(f"{c.name}: -> FAIL if {c.flip}")
        else:
            lines.append(f"{c.name}: -> PASS when {c.flip}")
    return lines


def _ascii(text: str) -> str:
    """Console-safe. Conditions quote input files - the incidents log is Vietnamese and this
    machine's console is cp1252, so an un-encodable character would crash the report rather
    than inform anyone. The markdown report keeps the original text."""
    return text.encode("ascii", "replace").decode("ascii")


def render_console(res: GateResult) -> str:
    p, t = res.facts.get("paper"), res.facts.get("testnet")
    head = f"CARRY-7d go-live gate  as of {res.today}  "
    head += f"(paper day {p['days'] if p else 0}, testnet COMPLETE {t['complete'] if t else 0}/{TESTNET_COMPLETE_REQUIRED})"
    out = [head]
    for n in res.notes:
        out.append(f"  note: {n}")
    for c in res.conditions:
        line = f"  [{c.status:7s}] {c.name:26s} {c.measured:44s} {c.threshold}"
        if c.note:
            line += f"\n            {c.note}"
        out.append(line)
    if res.verdict == "GO":
        out.append("VERDICT: GO - every condition passes. Follow GO_LIVE_CHECKLIST.md Part 4 (sleep on the capital number first).")
    elif res.verdict == "NO-GO":
        bad = "; ".join(f"{c.name} {c.measured}" for c in res.conditions if c.status == FAIL and c.terminal)
        out.append(f"VERDICT: NO-GO - {bad}. Waiting does not fix this. Record it in HONEST_FINDINGS.md; do not tune.")
    else:
        days = (res.earliest - res.today).days if res.earliest else 0
        out.append(f"VERDICT: NOT-YET - earliest evaluable {res.earliest} ({days}d). By then:")
        for m in res.must_happen:
            out.append(f"  - {m}")
    out.append("What would flip this:")
    for line in _flip_lines(res):
        out.append(f"  - {line}")
    return _ascii("\n".join(out))


def render_markdown(res: GateResult) -> str:
    p, t = res.facts.get("paper"), res.facts.get("testnet")
    md = [f"# CARRY-7d go-live gate report", "",
          f"Evaluated as of **{res.today}** (generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by gate_report.py).",
          "", f"## VERDICT: {res.verdict}", ""]
    if res.verdict == "NOT-YET":
        days = (res.earliest - res.today).days if res.earliest else 0
        md.append(f"Earliest evaluable date: **{res.earliest}** ({days} days). By then:")
        md.append("")
        md += [f"- {m}" for m in res.must_happen]
    elif res.verdict == "GO":
        md.append("Every condition passes. Next: GO_LIVE_CHECKLIST.md Part 4 - choose capital by the drawdown column, sleep on it, then ceilings v2.")
    else:
        md.append("A condition failed that waiting cannot undo. Per the config note: stop, do not tune; record in HONEST_FINDINGS.md.")
    md += ["", "## Conditions", "", "| status | condition | measured | threshold | note |", "|---|---|---|---|---|"]
    for c in res.conditions:
        md.append(f"| {c.status} | {c.name} | {c.measured} | {c.threshold} | {c.note} |")
    md += ["", "## What would flip this", ""]
    md += [f"- {line}" for line in _flip_lines(res)]
    md += ["", "## Facts", ""]
    if p:
        md += [f"- paper: day {p['days']}, equity {p['equity']:.4f} ({p['total_pct']:+.2f}%), Sharpe {p['sharpe']:+.2f}, "
               f"maxDD {p['maxdd_pct']:.1f}%, current DD {p['current_dd_pct']:.1f}%, worst month {p['worst_month']} "
               f"{p['worst_month_pct']:+.1f}%, last fill day {p['last_fill_day']}"]
    else:
        md.append("- paper: no ledger")
    if t:
        md.append(f"- testnet: {t['complete']} COMPLETE day(s) since {TESTNET_START}, last run {t['last_run']}, "
                  f"non-COMPLETE: {', '.join(f'{d}:{s}' for d, s in t['other']) or 'none'}")
    else:
        md.append("- testnet: no audit DB")
    tr = res.facts.get("tracking")
    if tr:
        md.append(f"- tracking: {tr['days']} days, cumulative {tr['cum_pct']:+.2f}% of budget (~{tr['per_week_pct']:+.2f}%/week), "
                  f"budget {tr['budget']:.0f} USD" if tr["budget"] else f"- tracking: {tr['days']} days, budget unknown")
        md += ["", "| week | days | divergence USD | % of budget | breach |", "|---|---|---|---|---|"]
        for w in tr["weeks"]:
            pct = f"{w['pct']:+.2f}%" if w["pct"] is not None else "-"
            md.append(f"| {w['week']} | {w['days']} | {w['diff_usd']:+.2f} | {pct} | {'YES' if w['breach'] else ''} |")
        md.append("")
    else:
        md.append("- tracking: no log")
    f = res.facts.get("fills")
    md.append(f"- fills: {f['legs']} legs, shortfall mean {f['mean']:+.1f} / median {f['median']:+.1f} / p90 {f['p90']:+.1f} bps"
              if f else "- fills: none analysed")
    c = res.facts.get("canary")
    md.append(f"- canary: last {c['last']['date']} ({c['age']}d ago), alerts in last {CANARY_LOOKBACK_ROWS}: {len(c['alerts'])}"
              if c else "- canary: never run")
    md.append(f"- markers: {', '.join(res.facts.get('markers') or []) or 'none'}")
    for n in res.notes:
        md.append(f"- note: {n}")
    md += ["", "Exit codes: 0 GO, 1 NO-GO, 2 NOT-YET. Thresholds come from carry_paper_config_v1.json[go_live_gate]; "
           f"the {TESTNET_COMPLETE_REQUIRED}-run and tracking rules from GO_LIVE_CHECKLIST.md.", ""]
    return "\n".join(md)


def write_report(res: GateResult, root: Path) -> Path:
    out = root / "reports" / "GATE_REPORT.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(res), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(ROOT), help="project directory holding the inputs (tests)")
    ap.add_argument("--today", default=None, help="evaluate as of this UTC date (YYYY-MM-DD)")
    ap.add_argument("--no-write", action="store_true", help="print only, do not write reports/GATE_REPORT.md")
    args = ap.parse_args(argv)
    root = Path(args.root)
    today = date.fromisoformat(args.today) if args.today else datetime.now(timezone.utc).date()
    res = evaluate(root, today)
    print(render_console(res))
    if not args.no_write:
        try:
            out = write_report(res, root)
            print(f"-> {(out.relative_to(root) if out.is_relative_to(root) else out).as_posix()}")
        except OSError as exc:
            print(f"(could not write report: {exc})")
    return res.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
