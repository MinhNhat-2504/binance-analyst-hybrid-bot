"""Read-only signal orientation and calibration audit.

Use this before changing model orientation, thresholds, or live deployment.
It checks whether higher final_proba actually corresponds to better realized
Outcome_PnL in the scopes we care about.
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
        except Exception as exc:
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


def bool_series(series: pd.Series | None, index: pd.Index) -> pd.Series:
    if series is None:
        return pd.Series(False, index=index)
    return series.astype(str).str.strip().str.lower().isin(["true", "1", "yes", "y"])


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    idx = out.index
    for target, candidates in {
        "_symbol": ["symbol", "Symbol"],
        "_side": ["side", "Side"],
        "_regime": ["regime", "Regime"],
        "_proba": ["final_proba", "Calib_Proba", "Final_Proba"],
        "_outcome": ["Outcome_PnL"],
        "_decision": ["final_gate_decision", "Final_Action"],
        "_reason": ["profit_focus_reason"],
        "_status": ["symbol_prior_status"],
        "_size_usd": ["size_usd", "Size_USD", "Trade_Size_USD"],
        "_optimizer": ["optimizer_candidate"],
        "_live": ["is_trade_live", "IsActualTrade"],
        "_pass_live_gate": ["pass_live_gate"],
    }.items():
        source = col(out, *candidates)
        out[target] = out[source] if source else pd.NA

    out["_symbol"] = out["_symbol"].fillna("").astype(str).str.upper().str.strip()
    out["_side"] = out["_side"].fillna("").astype(str).str.upper().str.strip()
    out["_regime"] = pd.to_numeric(out["_regime"], errors="coerce")
    out["_proba"] = pd.to_numeric(out["_proba"], errors="coerce")
    out["_outcome"] = pd.to_numeric(out["_outcome"], errors="coerce")
    out["_decision"] = out["_decision"].fillna("").astype(str).str.upper()
    out["_reason"] = out["_reason"].fillna("").astype(str).str.upper()
    out["_status"] = out["_status"].fillna("").astype(str).str.upper()
    out["_size_usd"] = pd.to_numeric(out["_size_usd"], errors="coerce").fillna(0.0)
    out["_optimizer_bool"] = bool_series(out["_optimizer"], idx)
    out["_live_bool"] = bool_series(out["_live"], idx) | bool_series(out["_pass_live_gate"], idx) | out["_decision"].isin(["TRADE_LIVE", "TRADE_MICRO_LIVE"])
    out["_paper_bool"] = out["_decision"].eq("PAPER_TRADE")
    out["_actionable_bool"] = (out["_live_bool"] | out["_paper_bool"]) & out["_size_usd"].gt(0)
    out["_closed_bool"] = out["_outcome"].notna()
    return out


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


def metric_row(label: str, frame: pd.DataFrame) -> dict[str, object]:
    work = frame.dropna(subset=["_proba", "_outcome"]).copy()
    if work.empty:
        return {
            "Scope": label,
            "N": 0,
            "Corr": np.nan,
            "RankCorr": np.nan,
            "PnL_%": np.nan,
            "Mean_bps": np.nan,
            "Win_%": np.nan,
            "PF": np.nan,
            "TopQ_PnL_%": np.nan,
            "BotQ_PnL_%": np.nan,
            "Orientation": "NO_DATA",
        }

    corr = work["_proba"].corr(work["_outcome"])
    rank_corr = work["_proba"].rank().corr(work["_outcome"].rank())
    pnl = work["_outcome"]
    q = max(1, int(len(work) * 0.25))
    ordered = work.sort_values("_proba")
    bottom = ordered.head(q)["_outcome"].sum()
    top = ordered.tail(q)["_outcome"].sum()
    pf = profit_factor(pnl)
    if len(work) < 20:
        orientation = "LOW_N"
    elif pd.notna(rank_corr) and rank_corr < -0.10 and top < bottom:
        orientation = "POSSIBLE_INVERTED"
    elif pd.notna(rank_corr) and rank_corr > 0.10 and top > bottom:
        orientation = "ALIGNED"
    else:
        orientation = "WEAK_OR_NOISY"
    return {
        "Scope": label,
        "N": int(len(work)),
        "Corr": round(float(corr), 4) if pd.notna(corr) else np.nan,
        "RankCorr": round(float(rank_corr), 4) if pd.notna(rank_corr) else np.nan,
        "PnL_%": round(float(pnl.sum()) * 100, 3),
        "Mean_bps": round(float(pnl.mean()) * 10000, 1),
        "Win_%": round(float((pnl > 0).mean()) * 100, 1),
        "PF": round(pf, 3) if pd.notna(pf) and math.isfinite(pf) else pf,
        "TopQ_PnL_%": round(float(top) * 100, 3),
        "BotQ_PnL_%": round(float(bottom) * 100, 3),
        "Orientation": orientation,
    }


def bucket_table(frame: pd.DataFrame, label: str) -> pd.DataFrame:
    work = frame.dropna(subset=["_proba", "_outcome"]).copy()
    if work.empty:
        return pd.DataFrame()
    bins = [0.0, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 1.01]
    work["_bucket"] = pd.cut(work["_proba"], bins=bins, right=False, include_lowest=True)
    rows = []
    for bucket, group in work.groupby("_bucket", observed=True):
        pnl = group["_outcome"]
        rows.append(
            {
                "Scope": label,
                "Bucket": str(bucket),
                "N": int(len(group)),
                "PnL_%": round(float(pnl.sum()) * 100, 3),
                "Mean_bps": round(float(pnl.mean()) * 10000, 1),
                "Win_%": round(float((pnl > 0).mean()) * 100, 1),
                "PF": round(profit_factor(pnl), 3) if pd.notna(profit_factor(pnl)) and math.isfinite(profit_factor(pnl)) else profit_factor(pnl),
            }
        )
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit model probability orientation against realized Outcome_PnL.")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER, help="Ledger CSV path.")
    parser.add_argument("--min-n", type=int, default=10, help="Hide bucket rows with fewer samples.")
    args = parser.parse_args()

    path = Path(args.ledger)
    if not path.exists():
        print(f"Ledger not found: {path}")
        return 1
    df = normalize(read_csv_with_retry(path))
    closed = df[df["_closed_bool"]].copy()
    scopes = {
        "ALL_CLOSED": closed,
        "ACTIONABLE_CLOSED": closed[closed["_actionable_bool"]],
        "PAPER_CLOSED": closed[closed["_paper_bool"]],
        "LIVE_CLOSED": closed[closed["_live_bool"]],
        "LONG_R0_CLOSED": closed[(closed["_side"].eq("LONG")) & (closed["_regime"].eq(0))],
        "SHORT_R0_CLOSED": closed[(closed["_side"].eq("SHORT")) & (closed["_regime"].eq(0))],
        "SHORT_R0_ACTIONABLE": closed[(closed["_side"].eq("SHORT")) & (closed["_regime"].eq(0)) & closed["_actionable_bool"]],
        "MIRROR_SHORT_CLOSED": closed[closed["_status"].eq("MIRROR_SHORT_FROM_LONG_R0")],
        "MIRROR_FORCE_CLOSED": closed[closed["_reason"].str.contains("MIRROR_FORCE", regex=False)],
    }
    report = pd.DataFrame([metric_row(name, frame) for name, frame in scopes.items()])
    print(f"Model signal audit: {path}")
    print(f"Rows={len(df)} | closed={len(closed)}")
    print()
    print("Orientation summary")
    print(report.to_string(index=False))

    print()
    print("Bucket detail: SHORT_R0_CLOSED")
    b = bucket_table(scopes["SHORT_R0_CLOSED"], "SHORT_R0_CLOSED")
    if b.empty:
        print("none")
    else:
        print(b[b["N"].ge(int(args.min_n))].to_string(index=False))

    print()
    print("Bucket detail: ACTIONABLE_CLOSED")
    b = bucket_table(scopes["ACTIONABLE_CLOSED"], "ACTIONABLE_CLOSED")
    if b.empty:
        print("none")
    else:
        print(b[b["N"].ge(int(args.min_n))].to_string(index=False))

    print()
    print("Interpretation")
    print("- POSSIBLE_INVERTED means high proba performed worse than low proba in that scope; do not invert globally unless it repeats in ACTIONABLE/MIRROR scopes.")
    print("- LOW_N means wait for more closed samples before changing model orientation.")
    print("- Use this as a guardrail before threshold/gate changes, not as standalone live proof.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
