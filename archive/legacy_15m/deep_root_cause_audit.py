"""
Deep root-cause audit for shadow_ledger_candidates_v4.csv.

This version is intentionally conservative:
- separates all historical shadow rows from actionable/deployable rows
- does not count DISCOVERY_WATCH rows as deployable proof
- checks probability inversion per side/regime instead of mixing every row
- uses ASCII output so it runs cleanly in Windows PowerShell
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd


try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


LEDGER_PATH = Path("shadow_ledger_candidates_v4.csv")
PROBA_BINS = [0.0, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 1.01]
DEPLOY_STATUS_EXACT = {"PROMOTE_SYMBOL", "ADAPTIVE_PROMOTE_SYMBOL", "DISCOVERY_PROMOTE_SYMBOL_BUCKET"}
WATCH_PREFIX = "DISCOVERY_WATCH_SYMBOL_BUCKET"
PROMOTE_PREFIX = "DISCOVERY_PROMOTE_SYMBOL_BUCKET"


def as_bool(series: pd.Series) -> pd.Series:
    if series is None:
        return pd.Series(dtype=bool)
    if series.dtype == bool:
        return series.fillna(False)
    return series.fillna(False).astype(str).str.strip().str.lower().isin({"true", "1", "yes", "y"})


def metric(values: pd.Series) -> dict:
    s = pd.to_numeric(values, errors="coerce").dropna()
    if s.empty:
        return {"n": 0, "sum": np.nan, "mean": np.nan, "win": np.nan, "pf": np.nan, "max_dd": np.nan}
    gains = float(s[s > 0].sum())
    losses = float(-s[s < 0].sum())
    pf = math.inf if losses == 0 and gains > 0 else (gains / losses if losses > 0 else 0.0)
    equity = s.cumsum()
    max_dd = float((equity.cummax() - equity).max()) if len(equity) else 0.0
    return {
        "n": int(len(s)),
        "sum": float(s.sum()),
        "mean": float(s.mean()),
        "win": float((s > 0).mean()),
        "pf": pf,
        "max_dd": max_dd,
    }


def fmt_pct(x: float) -> str:
    return "n/a" if pd.isna(x) else f"{x * 100:+.3f}%"


def fmt_pf(x: float) -> str:
    if pd.isna(x):
        return "n/a"
    if math.isinf(x):
        return "inf"
    return f"{x:.3f}"


def print_metric(label: str, values: pd.Series) -> None:
    m = metric(values)
    print(
        f"{label:<34} n={m['n']:>3} total={fmt_pct(m['sum']):>9} "
        f"mean={fmt_pct(m['mean']):>9} win={m['win'] * 100 if not pd.isna(m['win']) else math.nan:>5.1f}% "
        f"PF={fmt_pf(m['pf']):>6} maxDD={fmt_pct(m['max_dd']):>9}"
    )


def proba_bucket_id(proba: float) -> str:
    if pd.isna(proba):
        return "NA"
    for i in range(len(PROBA_BINS) - 1):
        if PROBA_BINS[i] <= float(proba) < PROBA_BINS[i + 1]:
            return f"{PROBA_BINS[i]:.2f}-{PROBA_BINS[i + 1]:.2f}"
    return "OUT_OF_RANGE"


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    defaults = {
        "timestamp_utc": pd.NaT,
        "symbol": "",
        "side": "",
        "regime": np.nan,
        "final_proba": np.nan,
        "xgb_proba": np.nan,
        "meta_ev": np.nan,
        "meta_uncertainty": np.nan,
        "edge_after_cost": np.nan,
        "confidence_multiplier": np.nan,
        "pocket_health_multiplier": np.nan,
        "symbol_prior_multiplier": np.nan,
        "symbol_prior_status": "",
        "profit_focus_reason": "",
        "final_gate_decision": "",
        "execution_stage": "",
        "pass_live_gate": False,
        "is_trade_live": False,
        "is_rejected": False,
        "optimizer_candidate": False,
        "size_usd": 0.0,
        "Outcome_PnL": np.nan,
        "Exit_Reason": "",
    }
    for col, default in defaults.items():
        if col not in out.columns:
            out[col] = default

    out["_ts"] = pd.to_datetime(out["timestamp_utc"], errors="coerce", utc=True, format="mixed")
    out["_symbol"] = out["symbol"].fillna("").astype(str).str.upper().str.strip()
    out["_side"] = out["side"].fillna("").astype(str).str.upper().str.strip()
    out["_regime"] = pd.to_numeric(out["regime"], errors="coerce")
    out["_proba"] = pd.to_numeric(out["final_proba"], errors="coerce")
    out["_xgb_proba"] = pd.to_numeric(out["xgb_proba"], errors="coerce")
    out["_outcome"] = pd.to_numeric(out["Outcome_PnL"], errors="coerce")
    out["_size_usd"] = pd.to_numeric(out["size_usd"], errors="coerce").fillna(0.0)
    out["_confidence"] = pd.to_numeric(out["confidence_multiplier"], errors="coerce")
    out["_edge_after_cost"] = pd.to_numeric(out["edge_after_cost"], errors="coerce")
    out["_symbol_prior_mult"] = pd.to_numeric(out["symbol_prior_multiplier"], errors="coerce").fillna(0.0)
    out["_pocket_health_mult"] = pd.to_numeric(out["pocket_health_multiplier"], errors="coerce").fillna(0.0)
    out["_status"] = out["symbol_prior_status"].fillna("").astype(str).str.upper()
    out["_reason"] = out["profit_focus_reason"].fillna("").astype(str).str.upper()
    out["_stage"] = out["execution_stage"].fillna("").astype(str).str.upper()
    out["_gate"] = out["final_gate_decision"].fillna("").astype(str).str.upper()
    out["_exit"] = out["Exit_Reason"].fillna("").astype(str).str.upper()
    out["_live"] = as_bool(out["is_trade_live"]) | as_bool(out["pass_live_gate"])
    out["_rejected"] = as_bool(out["is_rejected"]) | out["_stage"].str.startswith("REJECTED") | out["_gate"].str.startswith("REJECTED")
    out["_optimizer"] = as_bool(out["optimizer_candidate"])
    out["_actionable"] = (~out["_rejected"]) & (out["_size_usd"] > 0)
    out["_watch"] = out["_status"].str.startswith(WATCH_PREFIX)
    out["_promote"] = out["_status"].isin(DEPLOY_STATUS_EXACT) | out["_status"].str.startswith(PROMOTE_PREFIX)
    out["_high_tail"] = out["_proba"] >= 0.75
    out["_deployable"] = out["_actionable"] & out["_optimizer"] & out["_promote"] & out["_high_tail"]
    out["_bucket"] = out["_proba"].apply(proba_bucket_id)
    return out


def proba_inversion_table(df: pd.DataFrame, title: str) -> list[dict]:
    closed = df.dropna(subset=["_outcome", "_proba"]).copy()
    print(f"\nProbability monotonicity - {title}")
    print("-" * 78)
    findings = []
    if closed.empty:
        print("No closed rows.")
        return findings

    for (side, regime), group in closed.groupby(["_side", "_regime"], dropna=False):
        if len(group) < 8:
            continue
        corr = group["_proba"].rank().corr(group["_outcome"].rank())
        high = group[group["_proba"] >= 0.75]["_outcome"]
        low = group[group["_proba"] < 0.65]["_outcome"]
        high_m = float(high.mean()) if len(high) else np.nan
        low_m = float(low.mean()) if len(low) else np.nan
        inverted = bool(
            len(high) >= 3
            and (
                (not pd.isna(corr) and corr < -0.15 and high_m < 0)
                or (len(low) >= 3 and high_m + 0.001 < low_m)
            )
        )
        print(
            f"{side}_R{int(regime) if not pd.isna(regime) else 'NA':<2} "
            f"n={len(group):>3} spearman={corr:+.3f} "
            f"high>=0.75 n={len(high):>2} mean={fmt_pct(high_m):>9} "
            f"low<0.65 n={len(low):>2} mean={fmt_pct(low_m):>9} "
            f"{'INVERTED' if inverted else 'ok/pending'}"
        )
        if inverted:
            findings.append(
                {
                    "scope": f"{side}_Regime{int(regime) if not pd.isna(regime) else 'NA'}",
                    "n": int(len(group)),
                    "spearman": float(corr),
                    "high_mean": high_m,
                    "low_mean": low_m,
                }
            )
    if not findings:
        print("No deploy-grade probability inversion found in this scope.")
    return findings


def bucket_table(df: pd.DataFrame, title: str) -> None:
    closed = df.dropna(subset=["_outcome", "_proba"]).copy()
    print(f"\nProba buckets - {title}")
    print("-" * 78)
    if closed.empty:
        print("No closed rows.")
        return
    rows = []
    for (side, regime, bucket), group in closed.groupby(["_side", "_regime", "_bucket"], dropna=False):
        m = metric(group["_outcome"])
        rows.append(
            {
                "Group": f"{side}_R{int(regime) if not pd.isna(regime) else 'NA'}_{bucket}",
                "N": m["n"],
                "Total_%": round(m["sum"] * 100, 3),
                "Mean_bps": round(m["mean"] * 10000, 1),
                "Win_%": round(m["win"] * 100, 1),
                "PF": round(m["pf"], 3) if np.isfinite(m["pf"]) else m["pf"],
            }
        )
    out = pd.DataFrame(rows).sort_values(["Total_%", "N"], ascending=[True, False])
    print(out.to_string(index=False))


def exit_table(df: pd.DataFrame, title: str) -> None:
    closed = df.dropna(subset=["_outcome"]).copy()
    print(f"\nExit attribution - {title}")
    print("-" * 78)
    if closed.empty:
        print("No closed rows.")
        return
    rows = []
    for reason, group in closed.groupby("_exit", dropna=False):
        m = metric(group["_outcome"])
        rows.append(
            {
                "Exit": reason or "NA",
                "N": m["n"],
                "Total_%": round(m["sum"] * 100, 3),
                "Mean_bps": round(m["mean"] * 10000, 1),
                "Win_%": round(m["win"] * 100, 1),
                "PF": round(m["pf"], 3) if np.isfinite(m["pf"]) else m["pf"],
            }
        )
    out = pd.DataFrame(rows).sort_values("Total_%")
    print(out.to_string(index=False))


def gate_table(df: pd.DataFrame) -> None:
    print("\nGate profile")
    print("-" * 78)
    total = max(len(df), 1)
    checks = [
        ("live rows", df["_live"]),
        ("actionable rows", df["_actionable"]),
        ("deployable rows", df["_deployable"]),
        ("watch-only rows", df["_watch"]),
        ("rejected rows", df["_rejected"]),
        ("pocket_health <= 0", df["_pocket_health_mult"] <= 0),
        ("symbol_prior <= 0", df["_symbol_prior_mult"] <= 0),
        ("edge_after_cost <= 0", df["_edge_after_cost"] <= 0),
        ("confidence < 0.25", df["_confidence"] < 0.25),
    ]
    for label, mask in checks:
        n = int(pd.Series(mask).fillna(False).sum())
        print(f"{label:<24} {n:>4} / {len(df):<4} ({n / total * 100:>5.1f}%)")


def side_regime_table(df: pd.DataFrame, title: str) -> None:
    closed = df.dropna(subset=["_outcome"]).copy()
    print(f"\nSide/regime PnL - {title}")
    print("-" * 78)
    if closed.empty:
        print("No closed rows.")
        return
    rows = []
    for (side, regime), group in closed.groupby(["_side", "_regime"], dropna=False):
        m = metric(group["_outcome"])
        rows.append(
            {
                "Group": f"{side}_Regime{int(regime) if not pd.isna(regime) else 'NA'}",
                "N": m["n"],
                "Total_%": round(m["sum"] * 100, 3),
                "Mean_bps": round(m["mean"] * 10000, 1),
                "Win_%": round(m["win"] * 100, 1),
                "PF": round(m["pf"], 3) if np.isfinite(m["pf"]) else m["pf"],
            }
        )
    print(pd.DataFrame(rows).sort_values("Total_%").to_string(index=False))


def main() -> None:
    if not LEDGER_PATH.exists():
        raise FileNotFoundError(f"Missing ledger: {LEDGER_PATH}")

    df = normalize(pd.read_csv(LEDGER_PATH))
    print("\n" + "=" * 78)
    print("DEEP ROOT CAUSE AUDIT - CLEAN V4")
    print("=" * 78)
    print(f"Rows={len(df)} | closed={df['_outcome'].notna().sum()} | file={LEDGER_PATH}")
    if df["_ts"].notna().any():
        print(f"Time range: {df['_ts'].min()} -> {df['_ts'].max()}")

    gate_table(df)

    closed = df.dropna(subset=["_outcome"])
    actionable = df[df["_actionable"]]
    deployable = df[df["_deployable"]]
    watch = df[df["_watch"]]

    print("\nCore PnL")
    print("-" * 78)
    print_metric("All closed shadow/history", closed["_outcome"])
    print_metric("Actionable closed", actionable["_outcome"])
    print_metric("Deployable/promote closed", deployable["_outcome"])
    print_metric("Watch-only closed", watch["_outcome"])

    side_regime_table(df, "all closed")
    side_regime_table(actionable, "actionable only")
    exit_table(df, "all closed")
    exit_table(actionable, "actionable only")
    bucket_table(df, "all closed")
    bucket_table(actionable, "actionable only")

    inversion_all = proba_inversion_table(df, "all closed")
    inversion_actionable = proba_inversion_table(actionable, "actionable only")
    inversion_deployable = proba_inversion_table(deployable, "deployable/promote only")

    print("\nExecutive conclusion")
    print("-" * 78)
    long_r0 = closed[(closed["_side"] == "LONG") & (closed["_regime"] == 0)]
    short_r0_high = closed[(closed["_side"] == "SHORT") & (closed["_regime"] == 0) & (closed["_proba"] >= 0.75)]
    sl_all = closed[closed["_exit"] == "SL_OR_TRAIL"]

    if not long_r0.empty:
        m = metric(long_r0["_outcome"])
        print(f"RC-LONG-R0: true historically. LONG_Regime0 total={fmt_pct(m['sum'])}, PF={fmt_pf(m['pf'])}. Keep hard block.")
    else:
        print("RC-LONG-R0: no closed evidence.")

    if inversion_actionable or inversion_deployable:
        print("RC-PROBA-INVERSION: true in actionable/deployable scope. Do not invert blindly; quarantine the broken scope and retrain/calibrate.")
    elif inversion_all:
        print("RC-PROBA-INVERSION: visible in historical mixed shadow rows, but not proven in actionable/deployable rows.")
    else:
        print("RC-PROBA-INVERSION: not proven after separating scopes.")

    if not short_r0_high.empty:
        m = metric(short_r0_high["_outcome"])
        print(f"SHORT_R0 high-tail proof: n={m['n']}, total={fmt_pct(m['sum'])}, PF={fmt_pf(m['pf'])}. This is the only pocket worth testing.")
    else:
        print("SHORT_R0 high-tail proof: not enough closed rows yet.")

    if not sl_all.empty:
        sl_frac = len(sl_all) / max(len(closed), 1)
        m = metric(sl_all["_outcome"])
        print(f"RC-SL-CLUSTER: true historically. SL_OR_TRAIL={sl_frac * 100:.1f}% of closed rows, total={fmt_pct(m['sum'])}.")

    if int(df["_live"].sum()) == 0:
        print("RC-LIVE-GATE: live rows are 0. This is safe paper mode, not proof of a bug. Enable live only after deployable OOS proof.")

    print("\nRecommended next action")
    print("-" * 78)
    print("1. Keep LONG_Regime0 hard-blocked.")
    print("2. Keep DISCOVERY_WATCH as observation-only; only DISCOVERY_PROMOTE may get size.")
    print("3. Do not invert probabilities globally. If inversion appears in actionable scope, quarantine/retrain that exact side/regime.")
    print("4. Continue running backfiller until SHORT_Regime0 proba>=0.75 has enough closed deployable outcomes.")
    print("5. Treat SL_OR_TRAIL as a symptom unless it persists in the new deployable scope.")


if __name__ == "__main__":
    main()
