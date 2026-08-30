"""Profit attribution dashboard for the v4 shadow ledger.

This is deliberately read-only. It separates four layers so paper/research
diagnostics do not get confused with real execution:
- LIVE
- PAPER
- REJECTED_BACKFILL
- RESEARCH
"""

from __future__ import annotations

import argparse
import math
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
    out["_ts"] = parse_mixed_utc(out[time_col].astype(str)) if time_col else pd.NaT

    for target, candidates in {
        "_symbol": ["symbol", "Symbol"],
        "_side": ["side", "Side"],
        "_regime": ["regime", "Regime"],
        "_proba": ["final_proba", "Calib_Proba", "Final_Proba"],
        "_outcome": ["Outcome_PnL"],
        "_status": ["Outcome_Status"],
        "_exit_reason": ["Exit_Reason"],
        "_final_decision": ["final_gate_decision", "Final_Action"],
        "_execution_stage": ["execution_stage", "Execution_Mode"],
        "_is_live": ["is_trade_live", "IsActualTrade"],
        "_pass_live_gate": ["pass_live_gate"],
        "_is_research": ["is_research_log"],
        "_is_rejected": ["is_rejected"],
        "_is_backfilled": ["is_backfilled"],
        "_size_usd": ["size_usd", "Size_USD", "Trade_Size_USD"],
        "_exit_profile": ["Exit_Profile"],
        "_exit_profile_source": ["Exit_Profile_Source"],
    }.items():
        source = col(out, *candidates)
        out[target] = out[source] if source else np.nan

    out["_symbol"] = out["_symbol"].fillna("").astype(str).str.upper().str.strip()
    out["_side"] = out["_side"].fillna("").astype(str).str.upper().str.strip()
    out["_regime_norm"] = pd.to_numeric(out["_regime"], errors="coerce").astype("Int64").astype(str)
    out["_proba_num"] = to_num(out["_proba"], idx)
    out["_outcome_num"] = to_num(out["_outcome"], idx)
    out["_size_usd_num"] = to_num(out["_size_usd"], idx, 0.0).fillna(0.0)
    out["_stage_upper"] = (
        out["_final_decision"].fillna("").astype(str).str.upper()
        + "|"
        + out["_execution_stage"].fillna("").astype(str).str.upper()
    )
    out["_live_bool"] = (
        as_bool(out["_is_live"], idx)
        | as_bool(out["_pass_live_gate"], idx)
        | out["_stage_upper"].str.contains("TRADE_LIVE|TRADE_MICRO_LIVE", regex=True)
    )
    out["_paper_bool"] = out["_stage_upper"].str.contains("PAPER_TRADE", regex=False)
    out["_rejected_bool"] = as_bool(out["_is_rejected"], idx) | out["_stage_upper"].str.contains("REJECTED", regex=False)
    out["_research_bool"] = as_bool(out["_is_research"], idx) | out["_stage_upper"].str.contains("LOG_RESEARCH", regex=False)
    out["_backfilled_bool"] = as_bool(out["_is_backfilled"], idx)
    out["_closed_bool"] = out["_outcome_num"].notna()
    out["_layer"] = np.select(
        [
            out["_live_bool"],
            out["_paper_bool"],
            out["_rejected_bool"],
            out["_research_bool"],
        ],
        ["LIVE", "PAPER", "REJECTED_BACKFILL", "RESEARCH"],
        default="OTHER",
    )
    return out


def max_drawdown(pnl: pd.Series) -> float:
    if pnl.empty:
        return math.nan
    curve = pnl.cumsum()
    dd = curve - curve.cummax()
    return float(dd.min())


def profit_factor(pnl: pd.Series) -> float:
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    gross_win = float(wins.sum())
    gross_loss = float(-losses.sum())
    if gross_loss > 0:
        return gross_win / gross_loss
    if gross_win > 0:
        return math.inf
    return math.nan


def layer_metrics(group: pd.DataFrame) -> dict[str, float | int | str]:
    pnl = pd.to_numeric(group["_outcome_num"], errors="coerce").dropna()
    return {
        "rows": int(len(group)),
        "closed": int(group["_closed_bool"].sum()),
        "open": int((~group["_closed_bool"]).sum()),
        "size_rows": int(group["_size_usd_num"].gt(0).sum()),
        "total_pnl_pct": float(pnl.sum() * 100.0) if not pnl.empty else math.nan,
        "mean_bps": float(pnl.mean() * 10000.0) if not pnl.empty else math.nan,
        "win_rate": float((pnl > 0).mean()) if not pnl.empty else math.nan,
        "pf": profit_factor(pnl),
        "max_dd_pct": max_drawdown(pnl) * 100.0 if not pnl.empty else math.nan,
    }


def format_summary(work: pd.DataFrame) -> pd.DataFrame:
    order = ["LIVE", "PAPER", "REJECTED_BACKFILL", "RESEARCH", "OTHER"]
    rows = []
    for layer in order:
        group = work[work["_layer"].eq(layer)]
        metrics = layer_metrics(group)
        metrics["layer"] = layer
        rows.append(metrics)
    return pd.DataFrame(rows)[
        ["layer", "rows", "closed", "open", "size_rows", "total_pnl_pct", "mean_bps", "win_rate", "pf", "max_dd_pct"]
    ]


def side_regime_table(work: pd.DataFrame, layer: str, limit: int) -> pd.DataFrame:
    subset = work[work["_layer"].eq(layer) & work["_closed_bool"]].copy()
    if subset.empty:
        return pd.DataFrame()
    subset["_side_regime"] = subset["_side"] + "_Regime" + subset["_regime_norm"]
    rows = []
    for key, group in subset.groupby("_side_regime"):
        pnl = pd.to_numeric(group["_outcome_num"], errors="coerce").dropna()
        rows.append(
            {
                "side_regime": key,
                "n": int(len(pnl)),
                "total_pnl_pct": float(pnl.sum() * 100.0),
                "mean_bps": float(pnl.mean() * 10000.0),
                "win_rate": float((pnl > 0).mean()),
                "pf": profit_factor(pnl),
                "max_dd_pct": max_drawdown(pnl) * 100.0,
            }
        )
    return pd.DataFrame(rows).sort_values("total_pnl_pct", ascending=False).head(limit)


def recent_rows(work: pd.DataFrame, limit: int) -> pd.DataFrame:
    cols = [
        "_ts",
        "_layer",
        "_symbol",
        "_side",
        "_regime_norm",
        "_proba_num",
        "_size_usd_num",
        "_outcome_num",
        "_status",
        "_exit_reason",
        "_exit_profile",
    ]
    return work.sort_values("_ts", na_position="first").tail(limit)[cols]


def verdict(work: pd.DataFrame) -> str:
    live_closed = work[work["_layer"].eq("LIVE") & work["_closed_bool"]]
    paper_closed = work[work["_layer"].eq("PAPER") & work["_closed_bool"]]
    if live_closed.empty:
        live_msg = "LIVE: no closed live trades yet, so no real-profit proof."
    else:
        pnl = live_closed["_outcome_num"].dropna()
        live_msg = f"LIVE: closed={len(pnl)} total={pnl.sum() * 100:.2f}% PF={profit_factor(pnl):.2f}."
    if paper_closed.empty:
        paper_msg = "PAPER: no closed paper trades yet."
    else:
        pnl = paper_closed["_outcome_num"].dropna()
        paper_msg = f"PAPER: closed={len(pnl)} total={pnl.sum() * 100:.2f}% PF={profit_factor(pnl):.2f} win={(pnl > 0).mean():.2f}."
    return live_msg + " " + paper_msg


def main() -> int:
    parser = argparse.ArgumentParser(description="Separate live/paper/rejected/research profit attribution.")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER, help="Ledger CSV path.")
    parser.add_argument("--show-recent", type=int, default=12, help="Recent rows to display.")
    parser.add_argument("--top", type=int, default=8, help="Rows per side/regime table.")
    args = parser.parse_args()

    ledger_path = Path(args.ledger)
    if not ledger_path.exists():
        raise FileNotFoundError(f"Ledger not found: {ledger_path}")
    df = read_csv_with_retry(ledger_path)
    work = normalize(df)

    latest_ts = work["_ts"].max()
    print(f"Profit Attribution Dashboard: {ledger_path} | rows={len(work)} | latest_utc={latest_ts}")
    print("\nLayer summary")
    print(format_summary(work).to_string(index=False))
    print("\nVerdict")
    print(verdict(work))

    for layer in ["LIVE", "PAPER", "REJECTED_BACKFILL", "RESEARCH"]:
        table = side_regime_table(work, layer, args.top)
        if table.empty:
            continue
        print(f"\n{layer} by side/regime")
        print(table.to_string(index=False))

    print("\nRecent rows")
    recent = recent_rows(work, args.show_recent)
    print(recent.to_string(index=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
