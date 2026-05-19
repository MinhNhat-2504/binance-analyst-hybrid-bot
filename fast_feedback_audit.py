"""Fast read-only feedback loop for the v4 shadow ledger.

This script is intentionally independent from the notebooks so it can be run
while the bot notebook is still collecting rows. It never writes to the ledger.
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


def as_bool(s: pd.Series) -> pd.Series:
    return s.astype(str).str.lower().isin(["true", "1", "yes", "y"])


def to_num(s: pd.Series | None, index: pd.Index) -> pd.Series:
    if s is None:
        return pd.Series(np.nan, index=index, dtype="float64")
    return pd.to_numeric(s, errors="coerce")


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    time_col = col(out, "timestamp_utc", "Timestamp_UTC")
    if time_col:
        raw_ts = out[time_col].astype(str)
        try:
            out["_ts"] = pd.to_datetime(raw_ts, errors="coerce", utc=True, format="mixed")
        except TypeError:
            # Older pandas versions do not support format="mixed".
            out["_ts"] = raw_ts.map(lambda x: pd.to_datetime(x, errors="coerce", utc=True))
    else:
        out["_ts"] = pd.NaT

    for target, candidates in {
        "_symbol": ["symbol", "Symbol"],
        "_side": ["side", "Side"],
        "_regime": ["regime", "Regime"],
        "_proba": ["final_proba", "Calib_Proba", "Final_Proba"],
        "_outcome": ["Outcome_PnL"],
        "_provisional": ["Provisional_PnL"],
        "_ret_15m": ["Ret_15M"],
        "_ret_30m": ["Ret_30M"],
        "_ret_1h": ["Ret_1H"],
        "_optimizer": ["optimizer_candidate"],
        "_live": ["is_trade_live", "IsActualTrade"],
        "_pass_live_gate": ["pass_live_gate"],
        "_research": ["is_research_log"],
        "_shadow": ["is_shadow_trade"],
        "_is_rejected": ["is_rejected"],
        "_stage": ["final_gate_decision", "execution_stage", "Execution_Mode", "Final_Action"],
        "_size_usd": ["size_usd", "Size_USD", "Trade_Size_USD"],
        "_symbol_status": ["symbol_prior_status"],
        "_reason": ["profit_focus_reason"],
        "_exit_reason": ["Exit_Reason"],
    }.items():
        source = col(out, *candidates)
        if source is None:
            out[target] = np.nan
        else:
            out[target] = out[source]

    out["_side"] = out["_side"].astype(str).str.upper()
    out["_regime"] = pd.to_numeric(out["_regime"], errors="coerce")
    out["_proba"] = pd.to_numeric(out["_proba"], errors="coerce")
    out["_outcome"] = pd.to_numeric(out["_outcome"], errors="coerce")
    out["_provisional"] = pd.to_numeric(out["_provisional"], errors="coerce")
    out["_ret_15m"] = pd.to_numeric(out["_ret_15m"], errors="coerce")
    out["_ret_30m"] = pd.to_numeric(out["_ret_30m"], errors="coerce")
    out["_ret_1h"] = pd.to_numeric(out["_ret_1h"], errors="coerce")
    out["_optimizer_bool"] = as_bool(out["_optimizer"]) if "_optimizer" in out else False
    stage_upper = out["_stage"].fillna("").astype(str).str.upper()
    pass_live_bool = as_bool(out["_pass_live_gate"]) if "_pass_live_gate" in out else False
    out["_live_bool"] = (as_bool(out["_live"]) if "_live" in out else False) | pass_live_bool | stage_upper.isin(["TRADE_LIVE", "TRADE_MICRO_LIVE"])
    out["_paper_bool"] = stage_upper.eq("PAPER_TRADE")
    out["_research_bool"] = (as_bool(out["_research"]) if "_research" in out else False) | stage_upper.eq("LOG_RESEARCH")
    out["_shadow_bool"] = as_bool(out["_shadow"]) if "_shadow" in out else False
    out["_rejected_bool"] = as_bool(out["_is_rejected"]) | stage_upper.str.startswith("REJECTED")
    out["_size_usd_num"] = pd.to_numeric(out["_size_usd"], errors="coerce").fillna(0.0)
    out["_actionable_bool"] = (out["_live_bool"] | out["_paper_bool"]) & (out["_size_usd_num"] > 0)
    out["_stage_class"] = np.select(
        [
            out["_live_bool"],
            out["_paper_bool"],
            out["_rejected_bool"],
            out["_research_bool"],
        ],
        ["LIVE_EXECUTION", "PAPER_EXECUTABLE", "REJECTED_BACKFILL", "RESEARCH_LOG"],
        default="OTHER_SHADOW",
    )
    key = (
        out["_symbol"].fillna("").astype(str).str.upper()
        + "|"
        + out["_side"].fillna("").astype(str).str.upper()
        + "|"
        + out["_regime"].astype("Int64").astype(str)
    )
    status = out["_symbol_status"].fillna("").astype(str).str.upper()
    out["_proba_bucket"] = out["_proba"].apply(proba_bucket_id)
    status_frame = pd.DataFrame({"key": key, "status": status, "ts": out["_ts"]}).dropna(subset=["ts"])
    if status_frame.empty:
        active_status = status
    else:
        latest_status = status_frame.sort_values("ts").groupby("key")["status"].last()
        active_status = key.map(latest_status).fillna(status).astype(str).str.upper()
    deploy_exact = ["PROMOTE_SYMBOL", "ADAPTIVE_PROMOTE_SYMBOL", "DISCOVERY_PROMOTE_SYMBOL_BUCKET"]
    deploy_status = status.apply(lambda x: str(x).upper().startswith("DISCOVERY_PROMOTE_SYMBOL_BUCKET"))
    allowed_status = active_status.isin(deploy_exact) | active_status.apply(lambda x: str(x).upper().startswith("DISCOVERY_PROMOTE_SYMBOL_BUCKET")) | deploy_status
    bucket_key = out["_side"] + "|" + out["_regime"].astype("Int64").astype(str) + "|" + out["_proba_bucket"]
    symbol_bucket_key = out["_symbol"].fillna("").astype(str).str.upper().str.strip() + "|" + bucket_key
    bucket_ok = current_bucket_ok(bucket_key, out["_outcome"]) | current_symbol_bucket_ok(symbol_bucket_key, out["_outcome"])
    out["_active_symbol_status"] = active_status
    out["_active_proba_bucket_ok"] = bucket_ok
    out["_active_optimizer_bool"] = out["_optimizer_bool"] & allowed_status & bucket_ok & out["_actionable_bool"]

    # Fast proxy: real outcome first, then progressively shorter mark-to-market returns.
    out["_fast_pnl"] = out["_outcome"]
    for fallback in ["_provisional", "_ret_1h", "_ret_30m", "_ret_15m"]:
        out["_fast_pnl"] = out["_fast_pnl"].fillna(out[fallback])
    return out


def proba_bucket_id(proba: float) -> str:
    try:
        p = float(proba)
    except Exception:
        return "NA"
    bins = [0.0, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 1.01]
    for i in range(len(bins) - 1):
        if bins[i] <= p < bins[i + 1]:
            return f"{bins[i]:.2f}-{bins[i + 1]:.2f}"
    return "OUT_OF_RANGE"


def current_bucket_ok(bucket_key: pd.Series, pnl: pd.Series, min_samples: int = 8) -> pd.Series:
    frame = pd.DataFrame({"bucket_key": bucket_key, "pnl": pd.to_numeric(pnl, errors="coerce")}).dropna(subset=["pnl"])
    if frame.empty:
        return pd.Series(False, index=bucket_key.index)

    def is_allowed(s: pd.Series) -> bool:
        s = pd.to_numeric(s, errors="coerce").dropna()
        if len(s) < min_samples:
            return False
        gains = float(s[s > 0].sum())
        losses = float(-s[s < 0].sum())
        pf = 99.0 if losses == 0 and gains > 0 else (gains / losses if losses > 0 else 0.0)
        return bool(float(s.sum()) > 0 and pf >= 1.05 and float((s > 0).mean()) >= 0.45)

    allowed = frame.groupby("bucket_key")["pnl"].apply(is_allowed)
    mapped = bucket_key.map(allowed)
    return mapped.where(mapped.notna(), False).astype(bool)


def current_symbol_bucket_ok(symbol_bucket_key: pd.Series, pnl: pd.Series, min_samples: int = 3) -> pd.Series:
    frame = pd.DataFrame({"symbol_bucket_key": symbol_bucket_key, "pnl": pd.to_numeric(pnl, errors="coerce")}).dropna(subset=["pnl"])
    if frame.empty:
        return pd.Series(False, index=symbol_bucket_key.index)

    def is_allowed(s: pd.Series) -> bool:
        s = pd.to_numeric(s, errors="coerce").dropna()
        if len(s) < min_samples:
            return False
        gains = float(s[s > 0].sum())
        losses = float(-s[s < 0].sum())
        pf = 99.0 if losses == 0 and gains > 0 else (gains / losses if losses > 0 else 0.0)
        return bool(float(s.sum()) > 0.003 and pf >= 1.15 and float((s > 0).mean()) >= 0.50)

    allowed = frame.groupby("symbol_bucket_key")["pnl"].apply(is_allowed)
    mapped = symbol_bucket_key.map(allowed)
    return mapped.where(mapped.notna(), False).astype(bool)


def metrics(values: pd.Series) -> dict[str, float]:
    pnl = pd.to_numeric(values, errors="coerce").dropna()
    if pnl.empty:
        return {"n": 0, "sum": math.nan, "mean_bps": math.nan, "win": math.nan, "pf": math.nan, "max_dd_bps": math.nan}

    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    gross_win = float(wins.sum())
    gross_loss = float(-losses.sum())
    if gross_loss > 0:
        pf = gross_win / gross_loss
    elif gross_win > 0:
        pf = math.inf
    else:
        pf = math.nan

    curve = pnl.cumsum()
    dd = curve - curve.cummax()
    return {
        "n": int(len(pnl)),
        "sum": float(pnl.sum()),
        "mean_bps": float(pnl.mean() * 10000),
        "win": float((pnl > 0).mean()),
        "pf": float(pf) if math.isfinite(pf) else pf,
        "max_dd_bps": float(dd.min() * 10000),
    }


def fmt_pct(value: float) -> str:
    if pd.isna(value):
        return "n/a"
    return f"{value * 100:.2f}%"


def fmt_pf(value: float) -> str:
    if pd.isna(value):
        return "n/a"
    if value == math.inf:
        return "inf"
    return f"{value:.2f}"


def print_metrics(label: str, values: pd.Series) -> None:
    m = metrics(values)
    print(
        f"{label:<34} n={m['n']:>3} total={fmt_pct(m['sum']):>8} "
        f"mean={m['mean_bps']:>7.1f}bps win={m['win'] * 100 if not pd.isna(m['win']) else math.nan:>5.1f}% "
        f"PF={fmt_pf(m['pf']):>5} maxDD={m['max_dd_bps']:>7.1f}bps"
    )


def print_execution_layer_health(df: pd.DataFrame) -> None:
    """Separate live proof from paper/backfill diagnostics.

    Outcome_PnL on rejected rows is useful for research, but it is not evidence
    that the bot actually entered those trades. Keep these layers split.
    """
    print_metrics("LIVE execution closed", df.loc[df["_live_bool"], "_outcome"])
    print_metrics("PAPER executable closed", df.loc[df["_paper_bool"], "_outcome"])
    print_metrics("REJECTED backfill only", df.loc[df["_rejected_bool"], "_outcome"])
    research_only = df["_research_bool"] & ~(df["_live_bool"] | df["_paper_bool"] | df["_rejected_bool"])
    print_metrics("RESEARCH/log only", df.loc[research_only, "_outcome"])

    stage_rows = []
    for stage, group in df.groupby("_stage_class", dropna=False):
        stage_rows.append(
            {
                "Stage": stage,
                "Rows": len(group),
                "Closed": int(group["_outcome"].notna().sum()),
                "Size>0": int((group["_size_usd_num"] > 0).sum()),
            }
        )
    if stage_rows:
        print("\nExecution stage counts")
        print(pd.DataFrame(stage_rows).sort_values("Rows", ascending=False).to_string(index=False))


def policy_grid(df: pd.DataFrame) -> pd.DataFrame:
    status = df["_symbol_status"].astype(str)
    status_upper = status.str.upper()
    promoted = status.isin(["PROMOTE_SYMBOL", "ADAPTIVE_PROMOTE_SYMBOL", "DISCOVERY_PROMOTE_SYMBOL_BUCKET"]) | status_upper.str.startswith("DISCOVERY_PROMOTE_SYMBOL_BUCKET")
    base = (df["_side"] == "SHORT") & (df["_regime"] == 0) & promoted
    rows = []
    for threshold in [0.60, 0.62, 0.65, 0.70, 0.75]:
        mask = base & (df["_proba"] >= threshold)
        real = metrics(df.loc[mask, "_outcome"])
        fast = metrics(df.loc[mask, "_fast_pnl"])
        rows.append(
            {
                "Policy": f"SHORT0 promoted proba>={threshold:.2f}",
                "Rows": int(mask.sum()),
                "Closed": real["n"],
                "Diag_Total_%": round(real["sum"] * 100, 3) if not pd.isna(real["sum"]) else np.nan,
                "Diag_PF": round(real["pf"], 3) if pd.notna(real["pf"]) and math.isfinite(real["pf"]) else real["pf"],
                "Fast_N": fast["n"],
                "Fast_Total_%": round(fast["sum"] * 100, 3) if not pd.isna(fast["sum"]) else np.nan,
                "Fast_PF": round(fast["pf"], 3) if pd.notna(fast["pf"]) and math.isfinite(fast["pf"]) else fast["pf"],
            }
        )
    watch = status_upper.str.startswith("DISCOVERY_WATCH_SYMBOL_BUCKET")
    watch_base = (df["_side"] == "SHORT") & (df["_regime"] == 0) & watch
    for threshold in [0.70, 0.75]:
        mask = watch_base & (df["_proba"] >= threshold)
        real = metrics(df.loc[mask, "_outcome"])
        fast = metrics(df.loc[mask, "_fast_pnl"])
        rows.append(
            {
                "Policy": f"SHORT0 watch-only proba>={threshold:.2f}",
                "Rows": int(mask.sum()),
                "Closed": real["n"],
                "Diag_Total_%": round(real["sum"] * 100, 3) if not pd.isna(real["sum"]) else np.nan,
                "Diag_PF": round(real["pf"], 3) if np.isfinite(real["pf"]) else real["pf"],
                "Fast_N": fast["n"],
                "Fast_Total_%": round(fast["sum"] * 100, 3) if not pd.isna(fast["sum"]) else np.nan,
                "Fast_PF": round(fast["pf"], 3) if np.isfinite(fast["pf"]) else fast["pf"],
            }
        )
    return pd.DataFrame(rows)


def metric_row(label: object, values: pd.Series) -> dict[str, object]:
    m = metrics(values)
    return {
        "Group": label,
        "N": m["n"],
        "Total_%": round(m["sum"] * 100, 3) if not pd.isna(m["sum"]) else np.nan,
        "Mean_bps": round(m["mean_bps"], 1) if not pd.isna(m["mean_bps"]) else np.nan,
        "Win_%": round(m["win"] * 100, 1) if not pd.isna(m["win"]) else np.nan,
        "PF": round(m["pf"], 3) if pd.notna(m["pf"]) and math.isfinite(m["pf"]) else m["pf"],
    }


def print_root_cause_tables(df: pd.DataFrame) -> None:
    closed = df.dropna(subset=["_outcome"]).copy()
    if closed.empty:
        return

    actionable = closed[closed.get("_actionable_bool", False)].copy()
    if not actionable.empty:
        print("\nExecutable root-cause attribution (paper/live only)")
        actionable_rows = []
        for key, group in actionable.groupby(["_side", "_regime"]):
            actionable_rows.append(metric_row(f"{key[0]}_Regime{int(key[1]) if pd.notna(key[1]) else key[1]}", group["_outcome"]))
        print(pd.DataFrame(actionable_rows).sort_values("Total_%").to_string(index=False))

    print("\nAll-candidate root-cause attribution (diagnostic, includes rejected/backfill)")
    side_rows = []
    for key, group in closed.groupby(["_side", "_regime"]):
        side_rows.append(metric_row(f"{key[0]}_Regime{int(key[1]) if pd.notna(key[1]) else key[1]}", group["_outcome"]))
    print(pd.DataFrame(side_rows).sort_values("Total_%").to_string(index=False))

    print("\nProba bucket attribution")
    bucket_rows = []
    for key, group in closed.groupby(["_side", "_regime", "_proba_bucket"]):
        bucket_rows.append(metric_row(f"{key[0]}_R{int(key[1]) if pd.notna(key[1]) else key[1]}_{key[2]}", group["_outcome"]))
    print(pd.DataFrame(bucket_rows).sort_values("Total_%").head(12).to_string(index=False))

    if "_exit_reason" in closed.columns:
        print("\nExit reason attribution")
        exit_rows = [metric_row(key, group["_outcome"]) for key, group in closed.groupby("_exit_reason")]
        print(pd.DataFrame(exit_rows).sort_values("Total_%").to_string(index=False))


def print_discovery_candidates(df: pd.DataFrame) -> None:
    closed = df.dropna(subset=["_outcome", "_proba"]).copy()
    discovery = closed[(~closed["_actionable_bool"]) & (closed["_side"] == "SHORT") & (closed["_regime"] == 0)].copy()
    if discovery.empty:
        return

    rows = []
    for key, group in discovery.groupby(["_symbol", "_side", "_regime", "_proba_bucket"]):
        m = metrics(group["_outcome"])
        if m["n"] < 2:
            continue
        rows.append(
            {
                "Symbol": key[0],
                "Bucket": key[3],
                "N": m["n"],
                "Total_%": round(m["sum"] * 100, 3) if not pd.isna(m["sum"]) else np.nan,
                "Mean_bps": round(m["mean_bps"], 1) if not pd.isna(m["mean_bps"]) else np.nan,
                "Win_%": round(m["win"] * 100, 1) if not pd.isna(m["win"]) else np.nan,
                "PF": round(m["pf"], 3) if pd.notna(m["pf"]) and math.isfinite(m["pf"]) else m["pf"],
            }
        )
    if not rows:
        return
    report = pd.DataFrame(rows)
    print("\nDiscovery candidates: non-actionable SHORT0 symbol+buckets")
    print(report.sort_values(["Total_%", "PF"], ascending=False).head(12).to_string(index=False))


def print_recent_blockers(df: pd.DataFrame, recent_minutes: int) -> None:
    newest = df["_ts"].max()
    if pd.isna(newest):
        recent = df.tail(160).copy()
        title = "latest 160 rows"
    else:
        cutoff = newest - pd.Timedelta(minutes=recent_minutes)
        recent = df[df["_ts"] >= cutoff].copy()
        title = f"last {recent_minutes} minutes"
    if recent.empty:
        return

    recent["_stage_clean"] = recent["_stage"].fillna("").astype(str).str.upper()
    recent["_reason_clean"] = recent["_reason"].fillna("").astype(str)
    recent["_blocker"] = np.where(
        recent["_reason_clean"].str.len() > 0,
        recent["_reason_clean"],
        recent["_stage_clean"],
    )
    recent["_blocker"] = recent["_blocker"].replace("", "NO_BLOCK_REASON")

    print(f"\nRecent gate blockers: {title}")
    stage_counts = recent["_stage_clean"].replace("", "NA").value_counts().head(10)
    print(stage_counts.to_string())

    blocked = recent[recent["_rejected_bool"] | recent["_stage_clean"].str.startswith("REJECTED")]
    if blocked.empty:
        return
    blocker_counts = blocked["_blocker"].value_counts().head(12)
    print("\nTop blocker reasons")
    print(blocker_counts.to_string())


def run_audit(ledger_path: str, recent_minutes: int) -> None:
    path = Path(ledger_path)
    if not path.exists():
        raise FileNotFoundError(path)

    df = normalize(read_csv_with_retry(path))
    newest = df["_ts"].max()
    oldest = df["_ts"].min()
    print(f"\nFast feedback audit: {path}")
    print(f"Rows={len(df)} | oldest={oldest} | newest={newest} | modified={time.ctime(path.stat().st_mtime)}")

    live_count = int(df["_live_bool"].sum())
    rejected_count = int(df["_rejected_bool"].sum())
    actionable_count = int(df["_actionable_bool"].sum())
    print(f"Live rows={live_count} | Shadow/research rows={len(df) - live_count} | Actionable rows={actionable_count} | Rejected rows={rejected_count}")

    print("\nExecution-layer health (profit proof)")
    print_execution_layer_health(df)

    print("\nDiagnostic health")
    print_metrics("All closed incl rejected", df["_outcome"])
    print_metrics("All fast proxy incl rejected", df["_fast_pnl"])
    print_metrics("Executable closed", df.loc[df["_actionable_bool"], "_outcome"])
    print_metrics("Executable fast proxy", df.loc[df["_actionable_bool"], "_fast_pnl"])
    print_metrics("Historical optimizer outcome", df.loc[df["_optimizer_bool"], "_outcome"])
    print_metrics("Historical optimizer fast", df.loc[df["_optimizer_bool"], "_fast_pnl"])
    print_metrics("Active optimizer outcome", df.loc[df["_active_optimizer_bool"], "_outcome"])
    print_metrics("Active optimizer fast", df.loc[df["_active_optimizer_bool"], "_fast_pnl"])

    if pd.notna(newest):
        cutoff = newest - pd.Timedelta(minutes=recent_minutes)
        recent = df[df["_ts"] >= cutoff]
        print(f"\nRecent window by ledger time: last {recent_minutes} minutes")
        print_metrics("Recent LIVE closed", recent.loc[recent["_live_bool"], "_outcome"])
        print_metrics("Recent PAPER closed", recent.loc[recent["_paper_bool"], "_outcome"])
        print_metrics("Recent rejected backfill", recent.loc[recent["_rejected_bool"], "_outcome"])
        print_metrics("Recent fast proxy incl rejected", recent["_fast_pnl"])
        print_metrics("Recent executable outcome", recent.loc[recent["_actionable_bool"], "_outcome"])
        print_metrics("Recent executable fast", recent.loc[recent["_actionable_bool"], "_fast_pnl"])
        print_metrics("Recent historical opt outcome", recent.loc[recent["_optimizer_bool"], "_outcome"])
        print_metrics("Recent historical opt fast", recent.loc[recent["_optimizer_bool"], "_fast_pnl"])
        print_metrics("Recent active opt outcome", recent.loc[recent["_active_optimizer_bool"], "_outcome"])
        print_metrics("Recent active opt fast", recent.loc[recent["_active_optimizer_bool"], "_fast_pnl"])

    print("\nQuick policy grid")
    grid = policy_grid(df)
    print(grid.to_string(index=False))

    print_root_cause_tables(df)
    print_discovery_candidates(df)
    print_recent_blockers(df, recent_minutes)

    if df["_optimizer_bool"].sum() > 0:
        focus = df[df["_optimizer_bool"]].sort_values("_ts").tail(12)
        cols = ["_ts", "_symbol", "_side", "_regime", "_proba", "_symbol_status", "_active_symbol_status", "_outcome", "_fast_pnl", "_reason"]
        print("\nLatest historical optimizer candidates")
        print(focus[cols].to_string(index=False))

    if df["_active_optimizer_bool"].sum() > 0:
        active_focus = df[df["_active_optimizer_bool"]].sort_values("_ts").tail(12)
        cols = ["_ts", "_symbol", "_side", "_regime", "_proba", "_symbol_status", "_active_symbol_status", "_outcome", "_fast_pnl", "_reason"]
        print("\nLatest active optimizer candidates")
        print(active_focus[cols].to_string(index=False))

    live_focus = metrics(df.loc[df["_live_bool"], "_outcome"])
    paper_focus = metrics(df.loc[df["_paper_bool"], "_outcome"])
    real_focus = metrics(df.loc[df["_active_optimizer_bool"], "_outcome"])
    fast_focus = metrics(df.loc[df["_active_optimizer_bool"], "_fast_pnl"])
    print("\nVerdict")
    if live_count == 0:
        print("- No real live PnL yet. This audit is for fast debugging, not live-profit proof.")
    elif live_focus["n"] < 20:
        print("- Live sample is still too small for profit proof.")
    if paper_focus["n"] > 0 and pd.notna(paper_focus["pf"]) and paper_focus["pf"] < 1.2:
        print("- Paper executable PF is weak; keep live disabled.")
    if real_focus["n"] < 10:
        print("- Active optimizer has too few closed trades for deploy proof.")
    if pd.notna(real_focus["pf"]) and real_focus["pf"] < 1.2:
        print("- Active optimizer outcome PF is weak; keep paper-only.")
    if pd.notna(fast_focus["sum"]) and fast_focus["sum"] <= 0:
        print("- Fast proxy is not positive; do not loosen gates yet.")
    if real_focus["n"] >= 10 and real_focus["sum"] > 0 and real_focus["pf"] >= 1.2:
        print("- Candidate pocket is improving, but still confirm with walk-forward before live.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only fast feedback audit for Binance Analyst v4 ledger.")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER, help="Path to shadow ledger CSV.")
    parser.add_argument("--recent-minutes", type=int, default=90, help="Recent window based on ledger timestamps.")
    args = parser.parse_args()
    run_audit(args.ledger, args.recent_minutes)


if __name__ == "__main__":
    main()
