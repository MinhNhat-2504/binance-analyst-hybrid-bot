"""Read-only health audit for shadow_ledger_candidates_v4.csv.

The goal is to catch data pollution before optimizer/backfiller decisions use it:
bad timestamps, weird trade sizes, duplicate rows, rejected rows that look
executable, and OPEN rows that are older than the backfill horizon.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd


DEFAULT_LEDGER = "shadow_ledger_candidates_v4.csv"


def read_csv_with_retry(path: Path, retries: int = 3, delay_sec: float = 0.35) -> pd.DataFrame:
    last_error: Exception | None = None
    for _ in range(retries):
        try:
            return pd.read_csv(path)
        except Exception as exc:  # pragma: no cover - defensive for concurrent notebook writes
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


def as_bool(series: pd.Series | None, index: pd.Index) -> pd.Series:
    if series is None:
        return pd.Series(False, index=index)
    return series.astype(str).str.strip().str.lower().isin(["true", "1", "yes", "y"])


def to_num(series: pd.Series | None, index: pd.Index, default: float = np.nan) -> pd.Series:
    if series is None:
        return pd.Series(default, index=index, dtype="float64")
    return pd.to_numeric(series, errors="coerce")


def parse_mixed_utc(series: pd.Series) -> pd.Series:
    try:
        return pd.to_datetime(series, errors="coerce", utc=True, format="mixed")
    except TypeError:
        return pd.to_datetime(series, errors="coerce", utc=True)


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    idx = out.index

    time_col = col(out, "timestamp_utc", "Timestamp_UTC")
    out["_timestamp_raw"] = out[time_col] if time_col else ""
    out["_ts"] = parse_mixed_utc(out["_timestamp_raw"].astype(str)) if time_col else pd.NaT

    for target, candidates in {
        "_run_id": ["run_id"],
        "_symbol": ["symbol", "Symbol"],
        "_side": ["side", "Side"],
        "_regime": ["regime", "Regime"],
        "_proba": ["final_proba", "Calib_Proba", "Final_Proba"],
        "_price": ["price", "Price", "Entry_Price"],
        "_outcome": ["Outcome_PnL"],
        "_status": ["Outcome_Status"],
        "_final_decision": ["final_gate_decision", "Final_Action"],
        "_execution_stage": ["execution_stage", "Execution_Mode"],
        "_is_live": ["is_trade_live", "IsActualTrade"],
        "_pass_live_gate": ["pass_live_gate"],
        "_is_research": ["is_research_log"],
        "_is_rejected": ["is_rejected"],
        "_is_backfilled": ["is_backfilled"],
        "_size_usd": ["size_usd", "Size_USD", "Trade_Size_USD"],
        "_invested_usdt": ["invested_usdt", "Invested_USDT"],
        "_size_asset": ["size_asset", "Size_Asset"],
    }.items():
        source = col(out, *candidates)
        out[target] = out[source] if source else np.nan

    out["_symbol"] = out["_symbol"].fillna("").astype(str).str.upper().str.strip()
    out["_side"] = out["_side"].fillna("").astype(str).str.upper().str.strip()
    out["_regime_norm"] = pd.to_numeric(out["_regime"], errors="coerce").astype("Int64").astype(str)
    out["_stage_upper"] = (
        out["_final_decision"].fillna("").astype(str).str.upper()
        + "|"
        + out["_execution_stage"].fillna("").astype(str).str.upper()
    )
    out["_outcome_num"] = to_num(out["_outcome"], idx)
    out["_proba_num"] = to_num(out["_proba"], idx)
    out["_price_num"] = to_num(out["_price"], idx)
    out["_size_usd_num"] = to_num(out["_size_usd"], idx, 0.0).fillna(0.0)
    out["_invested_usdt_num"] = to_num(out["_invested_usdt"], idx, 0.0).fillna(0.0)
    out["_size_asset_num"] = to_num(out["_size_asset"], idx, 0.0).fillna(0.0)
    out["_live_bool"] = (
        as_bool(out["_is_live"], idx)
        | as_bool(out["_pass_live_gate"], idx)
        | out["_stage_upper"].str.contains("TRADE_LIVE|TRADE_MICRO_LIVE", regex=True)
    )
    out["_paper_bool"] = out["_stage_upper"].str.contains("PAPER_TRADE", regex=False)
    out["_research_bool"] = as_bool(out["_is_research"], idx) | out["_stage_upper"].str.contains("LOG_RESEARCH", regex=False)
    out["_rejected_bool"] = as_bool(out["_is_rejected"], idx) | out["_stage_upper"].str.contains("REJECTED", regex=False)
    out["_backfilled_bool"] = as_bool(out["_is_backfilled"], idx)
    out["_executable_bool"] = out["_live_bool"] | out["_paper_bool"]
    out["_outcome_status_upper"] = out["_status"].fillna("").astype(str).str.upper()
    out["_age_hours"] = (pd.Timestamp.now(tz="UTC") - out["_ts"]).dt.total_seconds() / 3600.0
    return out


def show_rows(df: pd.DataFrame, columns: list[str], max_rows: int) -> str:
    existing = [c for c in columns if c in df.columns]
    if df.empty or not existing:
        return ""
    return df[existing].head(max_rows).to_string(index=True)


def add_check(checks: list[dict], name: str, severity: str, mask: pd.Series, details: pd.DataFrame) -> None:
    checks.append(
        {
            "check": name,
            "severity": severity,
            "count": int(mask.fillna(False).sum()),
            "details": details.loc[mask.fillna(False)].copy(),
        }
    )


def run_audit(df: pd.DataFrame, stale_hours: float) -> list[dict]:
    work = normalize(df)
    checks: list[dict] = []
    idx = work.index

    raw_ts = work["_timestamp_raw"].fillna("").astype(str).str.strip()
    add_check(
        checks,
        "timestamp_parse_error",
        "ERROR",
        raw_ts.ne("") & work["_ts"].isna(),
        work,
    )

    executable_zero_size = work["_executable_bool"] & work["_size_usd_num"].le(0)
    add_check(checks, "executable_size_usd_zero", "ERROR", executable_zero_size, work)

    invested_raw_num = pd.to_numeric(work["_invested_usdt"], errors="coerce")
    executable_missing_invested = (
        work["_executable_bool"]
        & work["_size_usd_num"].gt(0)
        & invested_raw_num.isna()
    )
    add_check(checks, "executable_invested_usdt_missing", "WARN", executable_missing_invested, work)

    executable_zero_invested = (
        work["_executable_bool"]
        & work["_size_usd_num"].gt(0)
        & invested_raw_num.notna()
        & work["_invested_usdt_num"].le(0)
    )
    add_check(checks, "executable_invested_usdt_zero", "ERROR", executable_zero_invested, work)

    executable_size_invested_mismatch = (
        work["_executable_bool"]
        & work["_size_usd_num"].gt(0)
        & invested_raw_num.notna()
        & work["_invested_usdt_num"].gt(0)
        & (work["_size_usd_num"] - work["_invested_usdt_num"]).abs().gt(1e-6)
    )
    add_check(checks, "executable_size_invested_mismatch", "WARN", executable_size_invested_mismatch, work)

    executable_zero_asset = work["_executable_bool"] & work["_size_asset_num"].le(0)
    if "_size_asset" in work.columns:
        add_check(checks, "executable_size_asset_zero", "WARN", executable_zero_asset, work)

    rejected_executable = work["_rejected_bool"] & (
        work["_live_bool"] | work["_paper_bool"] | work["_size_usd_num"].gt(0)
    )
    add_check(checks, "rejected_row_looks_executable", "ERROR", rejected_executable, work)

    non_exec_size = (~work["_executable_bool"]) & work["_size_usd_num"].gt(0)
    add_check(checks, "non_executable_has_size_usd", "WARN", non_exec_size, work)

    final_missing_pnl = (
        work["_outcome_status_upper"].str.contains("FINAL|12H|TP|SL", regex=True)
        & work["_outcome_num"].isna()
    )
    add_check(checks, "final_status_missing_outcome_pnl", "ERROR", final_missing_pnl, work)

    pnl_but_open = work["_outcome_status_upper"].str.contains("OPEN", regex=False) & work["_outcome_num"].notna()
    add_check(checks, "open_status_has_outcome_pnl", "WARN", pnl_but_open, work)

    stale_open = (
        work["_ts"].notna()
        & work["_age_hours"].gt(float(stale_hours))
        & (~work["_backfilled_bool"])
        & work["_price_num"].gt(0)
    )
    add_check(checks, f"stale_open_over_{stale_hours:g}h", "WARN", stale_open, work)

    dup_cols = [
        "_timestamp_raw",
        "_symbol",
        "_side",
        "_regime_norm",
        "_proba_num",
        "_price_num",
        "_final_decision",
        "_execution_stage",
    ]
    dup_mask = work.duplicated(subset=dup_cols, keep=False) if len(work) else pd.Series(False, index=idx)
    add_check(checks, "duplicated_signal_rows", "WARN", dup_mask, work)

    return checks


def summarize_checks(checks: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"severity": item["severity"], "check": item["check"], "count": item["count"]}
            for item in checks
        ]
    ).sort_values(["severity", "count"], ascending=[True, False])


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit ledger health before optimizer/backfiller use.")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER, help="Ledger CSV path.")
    parser.add_argument("--stale-hours", type=float, default=14.0, help="Age threshold for stale unbackfilled rows.")
    parser.add_argument("--show", type=int, default=8, help="Example rows per failing check.")
    parser.add_argument("--fail-on-errors", action="store_true", help="Exit 1 when ERROR checks are non-zero.")
    args = parser.parse_args()

    ledger_path = Path(args.ledger)
    if not ledger_path.exists():
        raise FileNotFoundError(f"Ledger not found: {ledger_path}")
    df = read_csv_with_retry(ledger_path)
    checks = run_audit(df, args.stale_hours)
    summary = summarize_checks(checks)

    print(f"Ledger Health Audit: {ledger_path} | rows={len(df)}")
    print(summary.to_string(index=False))

    columns = [
        "timestamp_utc",
        "symbol",
        "side",
        "regime",
        "final_proba",
        "price",
        "final_gate_decision",
        "execution_stage",
        "size_usd",
        "invested_usdt",
        "Outcome_Status",
        "Outcome_PnL",
        "is_backfilled",
    ]
    for item in checks:
        if item["count"] <= 0:
            continue
        print(f"\n[{item['severity']}] {item['check']} count={item['count']}")
        sample = show_rows(item["details"], columns, args.show)
        if sample:
            print(sample)

    error_count = int(summary.loc[summary["severity"].eq("ERROR"), "count"].sum()) if not summary.empty else 0
    if error_count:
        print(f"\nHealth verdict: {error_count} ERROR rows need attention before trusting optimizer metrics.")
    else:
        print("\nHealth verdict: no ERROR-level ledger pollution detected.")
    return 1 if args.fail_on_errors and error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
