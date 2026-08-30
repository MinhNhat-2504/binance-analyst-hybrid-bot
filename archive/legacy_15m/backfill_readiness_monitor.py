"""Backfill readiness monitor for the Binance analyst shadow ledger.

It answers the practical question: "Can I run the backfiller now, or should I
wait?"  The script is read-only and does not change the ledger.
"""

from __future__ import annotations

import argparse
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


DEFAULT_LEDGER = Path("shadow_ledger_candidates_v4.csv")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report which ledger rows are ready for 12h outcome backfill.")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="Ledger CSV path.")
    parser.add_argument("--horizon-hours", type=float, default=12.0, help="Outcome horizon required before final backfill.")
    parser.add_argument("--timezone", default="Asia/Saigon", help="Local timezone for printed timestamps.")
    parser.add_argument("--show", type=int, default=12, help="Number of ready/waiting rows to show.")
    parser.add_argument("--min-proba", type=float, default=0.75, help="High-proba threshold for SHORT_Regime0 readiness.")
    parser.add_argument("--executable-only", action="store_true", help="Show only PAPER_TRADE/TRADE_LIVE rows.")
    return parser.parse_args()


def parse_mixed_utc(series: pd.Series) -> pd.Series:
    try:
        return pd.to_datetime(series, errors="coerce", utc=True, format="mixed")
    except TypeError:
        return pd.to_datetime(series, errors="coerce", utc=True)


def bool_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(False, index=frame.index)
    return frame[column].astype(str).str.lower().isin(["true", "1", "yes", "y"])


def normalize_regime(value: object) -> str:
    try:
        return str(int(float(value)))
    except Exception:
        return str(value)


def stage_label(frame: pd.DataFrame) -> pd.Series:
    decision = frame.get("final_gate_decision", pd.Series("", index=frame.index)).fillna("").astype(str).str.upper()
    execution = frame.get("execution_stage", pd.Series("", index=frame.index)).fillna("").astype(str).str.upper()
    is_live = bool_series(frame, "is_trade_live") | decision.isin(["TRADE_LIVE", "TRADE_MICRO_LIVE"])
    is_paper = decision.eq("PAPER_TRADE")
    is_rejected = bool_series(frame, "is_rejected") | decision.str.startswith("REJECTED") | execution.str.startswith("REJECTED")
    is_research = bool_series(frame, "is_research_log") | decision.eq("LOG_RESEARCH")
    labels = pd.Series("OTHER", index=frame.index, dtype="object")
    labels.loc[is_research] = "RESEARCH_LOG"
    labels.loc[is_rejected] = "REJECTED_BACKFILL"
    labels.loc[is_paper] = "PAPER_EXECUTABLE"
    labels.loc[is_live] = "LIVE_EXECUTION"
    return labels


def fmt_time(ts: pd.Timestamp | None, tz: ZoneInfo) -> str:
    if ts is None or pd.isna(ts):
        return "n/a"
    return pd.Timestamp(ts).tz_convert(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def print_table(frame: pd.DataFrame, columns: list[str], max_rows: int) -> None:
    if frame.empty:
        print("none")
        return
    print(frame[columns].head(max_rows).to_string(index=False))


def main() -> int:
    args = parse_args()
    ledger_path = Path(args.ledger)
    tz = ZoneInfo(args.timezone)
    now_utc = pd.Timestamp.now(tz=timezone.utc)
    horizon = pd.Timedelta(hours=float(args.horizon_hours))

    if not ledger_path.exists():
        print(f"Ledger not found: {ledger_path}")
        return 1

    df = pd.read_csv(ledger_path)
    if df.empty:
        print(f"Ledger is empty: {ledger_path}")
        return 0

    for col in ["timestamp_utc", "price", "Outcome_PnL", "Outcome_Status", "final_proba", "side", "regime", "symbol"]:
        if col not in df.columns:
            df[col] = pd.NA

    df["_ts"] = parse_mixed_utc(df["timestamp_utc"])
    df["_age"] = now_utc - df["_ts"]
    df["_ready_at"] = df["_ts"] + horizon
    df["_price"] = pd.to_numeric(df["price"], errors="coerce")
    df["_pnl"] = pd.to_numeric(df["Outcome_PnL"], errors="coerce")
    df["_proba"] = pd.to_numeric(df["final_proba"], errors="coerce")
    df["_regime"] = df["regime"].apply(normalize_regime)
    df["_stage"] = stage_label(df)
    df["_is_backfilled"] = bool_series(df, "is_backfilled")
    if args.executable_only:
        before = len(df)
        df = df[df["_stage"].isin(["PAPER_EXECUTABLE", "LIVE_EXECUTION"])].copy()
        print(f"Executable-only view: kept {len(df)}/{before} PAPER/LIVE rows.")

    has_timestamp = df["_ts"].notna()
    has_price = df["_price"].gt(0)
    closed = df["_pnl"].notna() | df["_is_backfilled"]
    pending = has_timestamp & has_price & ~closed
    ready = pending & (df["_age"] >= horizon)
    waiting = pending & ~ready
    open_status = df["Outcome_Status"].fillna("").astype(str).str.upper().eq("OPEN")
    high_short0 = (
        df["side"].astype(str).str.upper().eq("SHORT")
        & df["_regime"].eq("0")
        & df["_proba"].ge(float(args.min_proba))
    )
    paper_exec = df["_stage"].eq("PAPER_EXECUTABLE")
    live_exec = df["_stage"].eq("LIVE_EXECUTION")
    executable = paper_exec | live_exec
    paper_ready = ready & paper_exec
    paper_waiting = waiting & paper_exec
    executable_ready = ready & executable
    executable_waiting = waiting & executable

    print(f"Backfill readiness: {ledger_path}")
    print(f"Rows={len(df)} | parsed_ts={int(has_timestamp.sum())} | closed={int(closed.sum())} | pending={int(pending.sum())}")
    print(f"Now local: {fmt_time(now_utc, tz)} | horizon={args.horizon_hours:g}h")
    print(f"Ledger newest: {fmt_time(df['_ts'].max(), tz)} | file modified: {fmt_time(pd.Timestamp.fromtimestamp(ledger_path.stat().st_mtime, tz=timezone.utc), tz)}")
    print()

    print("Readiness summary")
    print(f"- OPEN status rows: {int(open_status.sum())}")
    print(f"- Pending and ready now: {int(ready.sum())}")
    print(f"- Pending but still waiting: {int(waiting.sum())}")
    print(f"- Executable PAPER/LIVE ready now: {int(executable_ready.sum())}")
    print(f"- PAPER executable ready now: {int(paper_ready.sum())}")
    print(f"- PAPER executable still waiting: {int(paper_waiting.sum())}")
    print(f"- SHORT_Regime0 proba>={args.min_proba:.2f} pending: {int((pending & high_short0).sum())}")
    print(f"- SHORT_Regime0 proba>={args.min_proba:.2f} ready now: {int((ready & high_short0).sum())}")

    if int(executable_ready.sum()) > 0:
        oldest_ready = df.loc[executable_ready, "_ts"].min()
        print(f"\nAction: run backfiller now for executable PAPER/LIVE. Oldest executable ready signal: {fmt_time(oldest_ready, tz)}")
    elif int(ready.sum()) > 0:
        oldest_ready = df.loc[ready, "_ts"].min()
        print(f"\nAction: optional research backfill only. No executable PAPER/LIVE is ready yet; oldest diagnostic ready signal: {fmt_time(oldest_ready, tz)}")
        if int(executable_waiting.sum()) > 0:
            next_exec_ready = df.loc[executable_waiting, "_ready_at"].min()
            remaining = next_exec_ready - now_utc
            minutes = max(0, int(remaining.total_seconds() // 60))
            print(f"Next executable PAPER/LIVE becomes ready at {fmt_time(next_exec_ready, tz)} (~{minutes} minutes).")
    elif int(waiting.sum()) > 0:
        next_ready = df.loc[waiting, "_ready_at"].min()
        remaining = next_ready - now_utc
        minutes = max(0, int(remaining.total_seconds() // 60))
        print(f"\nAction: wait. Next row becomes 12h-ready at {fmt_time(next_ready, tz)} (~{minutes} minutes).")
    else:
        print("\nAction: no pending rows with valid timestamp+price.")

    print("\nPending by execution layer")
    pending_stage = df.loc[pending, "_stage"].value_counts().rename_axis("stage").reset_index(name="pending")
    ready_stage = df.loc[ready, "_stage"].value_counts().rename_axis("stage").reset_index(name="ready")
    stage_summary = pending_stage.merge(ready_stage, on="stage", how="outer").fillna(0)
    if stage_summary.empty:
        print("none")
    else:
        stage_summary[["pending", "ready"]] = stage_summary[["pending", "ready"]].astype(int)
        print(stage_summary.sort_values(["ready", "pending"], ascending=False).to_string(index=False))

    view_cols = ["timestamp_utc", "symbol", "side", "regime", "final_proba", "final_gate_decision", "profit_focus_reason", "_stage"]
    print(f"\nOldest rows ready now (top {args.show})")
    print_table(df.loc[ready].sort_values("_ts"), view_cols, args.show)

    print(f"\nNext rows waiting (top {args.show})")
    waiting_view = df.loc[waiting].sort_values("_ready_at").copy()
    waiting_view["ready_at_local"] = waiting_view["_ready_at"].dt.tz_convert(tz).dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    print_table(waiting_view, ["timestamp_utc", "ready_at_local", "symbol", "side", "regime", "final_proba", "final_gate_decision", "_stage"], args.show)

    print(f"\nNext executable PAPER/LIVE waiting (top {args.show})")
    exec_waiting_view = df.loc[executable_waiting].sort_values("_ready_at").copy()
    if not exec_waiting_view.empty:
        exec_waiting_view["ready_at_local"] = exec_waiting_view["_ready_at"].dt.tz_convert(tz).dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    print_table(exec_waiting_view, ["timestamp_utc", "ready_at_local", "symbol", "side", "regime", "final_proba", "final_gate_decision", "_stage"], args.show)

    bad_ts = int((~has_timestamp).sum())
    bad_price = int((has_timestamp & ~has_price).sum())
    if bad_ts or bad_price:
        print("\nData quality notes")
        print(f"- Unparsed timestamps: {bad_ts}")
        print(f"- Parsed rows with missing/non-positive price: {bad_price}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
