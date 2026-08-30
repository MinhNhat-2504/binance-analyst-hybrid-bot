"""Train and validate the probability that a candidate hits a losing stop first.

The validation is event-aware:
- signals are deduplicated by symbol/side within a cooldown window;
- training events whose holding horizon overlaps validation are purged;
- an embargo is applied around every validation boundary;
- all thresholds are selected from training folds only.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from runtime_profit_guardrails import classify_route


DEFAULT_LEDGER = Path("shadow_ledger_candidates_v4.csv")
DEFAULT_MODEL = Path("stop_hit_risk_model.pkl")
DEFAULT_REPORT = Path("stop_hit_wfa_report.json")

NUMERIC_FEATURES = [
    "final_proba",
    "gating_proba",
    "transition_proba",
    "dl_proba",
    "xgb_proba",
    "edge_short",
    "edge_after_cost",
    "confidence_multiplier",
    "pocket_health_multiplier",
    "symbol_prior_multiplier",
    "meta_ev",
    "meta_uncertainty",
    "t2_ev_short",
]
CATEGORICAL_FEATURES = ["route", "symbol", "side", "regime"]
FEATURE_COLUMNS = CATEGORICAL_FEATURES + NUMERIC_FEATURES


def parse_mixed_utc(series: pd.Series) -> pd.Series:
    try:
        return pd.to_datetime(series, errors="coerce", utc=True, format="mixed")
    except TypeError:
        return series.map(lambda value: pd.to_datetime(value, errors="coerce", utc=True))


def normalize_regime(value: object) -> str:
    try:
        return str(int(float(value)))
    except Exception:
        return str(value)


def prepare_training_frame(
    ledger_path: Path,
    dedup_minutes: int = 30,
    default_horizon_hours: float = 12.0,
    executable_only: bool = False,
) -> tuple[pd.DataFrame, dict[str, int]]:
    raw = pd.read_csv(ledger_path)
    for column in [
        "timestamp_utc",
        "symbol",
        "side",
        "regime",
        "final_gate_decision",
        "profit_focus_reason",
        "symbol_prior_status",
        "Exit_Reason",
        "Exit_Timestamp_UTC",
        "Outcome_PnL",
    ] + NUMERIC_FEATURES:
        if column not in raw.columns:
            raw[column] = np.nan

    raw["_ts"] = parse_mixed_utc(raw["timestamp_utc"])
    raw["_event_end"] = parse_mixed_utc(raw["Exit_Timestamp_UTC"])
    raw["_event_end"] = raw["_event_end"].fillna(
        raw["_ts"] + pd.Timedelta(hours=float(default_horizon_hours))
    )
    raw["Outcome_PnL"] = pd.to_numeric(raw["Outcome_PnL"], errors="coerce")
    decision = raw["final_gate_decision"].fillna("").astype(str).str.upper()
    executable = decision.isin(["PAPER_TRADE", "TRADE_LIVE", "TRADE_MICRO_LIVE"])
    closed = raw["Outcome_PnL"].notna()
    short0 = (
        raw["side"].fillna("").astype(str).str.upper().eq("SHORT")
        & raw["regime"].apply(normalize_regime).eq("0")
    )
    scope = executable if executable_only else short0
    frame = raw.loc[scope & closed & raw["_ts"].notna()].copy()
    before_dedup = len(frame)

    frame["symbol"] = frame["symbol"].fillna("UNKNOWN").astype(str).str.upper()
    frame["side"] = frame["side"].fillna("UNKNOWN").astype(str).str.upper()
    frame["regime"] = frame["regime"].apply(normalize_regime)
    frame["route"] = [
        classify_route(reason, status)
        for reason, status in zip(
            frame["profit_focus_reason"],
            frame["symbol_prior_status"],
        )
    ]
    for column in NUMERIC_FEATURES:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame = frame.sort_values("_ts")
    keep: list[int] = []
    last_kept: dict[tuple[str, str], pd.Timestamp] = {}
    cooldown = pd.Timedelta(minutes=int(dedup_minutes))
    for idx, row in frame.iterrows():
        key = (row["symbol"], row["side"])
        previous = last_kept.get(key)
        if previous is not None and row["_ts"] - previous < cooldown:
            continue
        keep.append(idx)
        last_kept[key] = row["_ts"]
    frame = frame.loc[keep].copy().sort_values("_ts").reset_index(drop=True)

    exit_reason = frame["Exit_Reason"].fillna("").astype(str).str.upper()
    frame["target_stop_hit"] = (
        exit_reason.eq("SL_OR_TRAIL") & frame["Outcome_PnL"].lt(0)
    ).astype(int)
    stats = {
        "raw_rows": int(len(raw)),
        "scope": "EXECUTABLE_ONLY" if executable_only else "ALL_CLOSED_SHORT_REGIME0",
        "closed_before_dedup": int(before_dedup),
        "training_rows_after_dedup": int(len(frame)),
        "deduplicated_rows": int(before_dedup - len(frame)),
        "positive_stop_hit_rows": int(frame["target_stop_hit"].sum()),
    }
    return frame, stats


@dataclass
class EventPurgedWalkForward:
    min_train_rows: int = 50
    validation_rows: int = 20
    step_rows: int = 20
    embargo_minutes: int = 60

    def split(self, frame: pd.DataFrame) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        n_rows = len(frame)
        val_start = self.min_train_rows
        embargo = pd.Timedelta(minutes=int(self.embargo_minutes))
        while val_start + self.validation_rows <= n_rows:
            val_end = val_start + self.validation_rows
            validation = frame.iloc[val_start:val_end]
            validation_start = validation["_ts"].min()
            validation_end = validation["_event_end"].max()

            train_mask = (
                frame["_event_end"].lt(validation_start - embargo)
                | frame["_ts"].gt(validation_end + embargo)
            )
            train_mask.iloc[val_start:val_end] = False
            # Expanding walk-forward: future rows are never used for this fold.
            train_mask.iloc[val_end:] = False
            train_idx = np.flatnonzero(train_mask.to_numpy())
            val_idx = np.arange(val_start, val_end)
            if len(train_idx) >= max(20, self.min_train_rows // 2):
                yield train_idx, val_idx
            val_start += self.step_rows


def build_pipeline() -> Pipeline:
    numeric = Pipeline(
        [
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
        ]
    )
    categorical = Pipeline(
        [
            ("impute", SimpleImputer(strategy="most_frequent")),
            ("onehot", OneHotEncoder(handle_unknown="ignore", min_frequency=2)),
        ]
    )
    transform = ColumnTransformer(
        [
            ("numeric", numeric, NUMERIC_FEATURES),
            ("categorical", categorical, CATEGORICAL_FEATURES),
        ]
    )
    model = LogisticRegression(
        class_weight=None,
        C=0.03,
        max_iter=2_000,
        solver="liblinear",
        random_state=42,
    )
    return Pipeline([("transform", transform), ("model", model)])


def safe_auc(y_true: pd.Series, probabilities: np.ndarray) -> float:
    if pd.Series(y_true).nunique() < 2:
        return math.nan
    return float(roc_auc_score(y_true, probabilities))


def validate_walk_forward(
    frame: pd.DataFrame,
    splitter: EventPurgedWalkForward,
) -> tuple[list[dict[str, float]], pd.DataFrame]:
    reports: list[dict[str, float]] = []
    oos_parts: list[pd.DataFrame] = []
    for fold, (train_idx, val_idx) in enumerate(splitter.split(frame), start=1):
        train = frame.iloc[train_idx]
        val = frame.iloc[val_idx]
        if train["target_stop_hit"].nunique() < 2 or val["target_stop_hit"].nunique() < 2:
            continue
        pipeline = build_pipeline()
        pipeline.fit(train[FEATURE_COLUMNS], train["target_stop_hit"])
        probabilities = pipeline.predict_proba(val[FEATURE_COLUMNS])[:, 1]
        prevalence = float(train["target_stop_hit"].mean())
        baseline = np.full(len(val), prevalence)
        report = {
            "fold": fold,
            "train_rows": int(len(train)),
            "validation_rows": int(len(val)),
            "auc": safe_auc(val["target_stop_hit"], probabilities),
            "brier": float(brier_score_loss(val["target_stop_hit"], probabilities)),
            "baseline_brier": float(brier_score_loss(val["target_stop_hit"], baseline)),
            "log_loss": float(log_loss(val["target_stop_hit"], probabilities, labels=[0, 1])),
            "validation_prevalence": float(val["target_stop_hit"].mean()),
        }
        reports.append(report)
        fold_oos = val[["_ts", "symbol", "side", "regime", "route", "target_stop_hit"]].copy()
        fold_oos["stop_hit_probability"] = probabilities
        fold_oos["fold"] = fold
        oos_parts.append(fold_oos)
    oos = pd.concat(oos_parts, ignore_index=True) if oos_parts else pd.DataFrame()
    return reports, oos


def summarize_validation(reports: list[dict[str, float]], oos: pd.DataFrame) -> dict[str, object]:
    if not reports or oos.empty:
        return {
            "folds": 0,
            "oos_rows": 0,
            "auc": math.nan,
            "brier": math.nan,
            "baseline_brier": math.nan,
            "deployment_eligible": False,
            "reason": "NO_VALID_PURGED_FOLDS",
        }
    auc = safe_auc(oos["target_stop_hit"], oos["stop_hit_probability"].to_numpy())
    brier = float(brier_score_loss(oos["target_stop_hit"], oos["stop_hit_probability"]))
    prevalence = float(oos["target_stop_hit"].mean())
    baseline = np.full(len(oos), prevalence)
    baseline_brier = float(brier_score_loss(oos["target_stop_hit"], baseline))
    eligible = bool(
        len(reports) >= 3
        and len(oos) >= 50
        and math.isfinite(auc)
        and auc >= 0.56
        and brier <= baseline_brier * 0.98
    )
    reason = "PASS" if eligible else "INSUFFICIENT_OOS_DISCRIMINATION"
    return {
        "folds": int(len(reports)),
        "oos_rows": int(len(oos)),
        "auc": auc,
        "brier": brier,
        "baseline_brier": baseline_brier,
        "prevalence": prevalence,
        "deployment_eligible": eligible,
        "reason": reason,
    }


def train_stop_hit_model(
    *,
    ledger_path: Path = DEFAULT_LEDGER,
    model_out: Path = DEFAULT_MODEL,
    report_out: Path = DEFAULT_REPORT,
    dedup_minutes: int = 30,
    min_train_rows: int = 300,
    validation_rows: int = 100,
    step_rows: int = 100,
    embargo_minutes: int = 60,
    executable_only: bool = False,
) -> dict[str, object]:
    frame, data_stats = prepare_training_frame(
        ledger_path,
        dedup_minutes,
        executable_only=executable_only,
    )
    splitter = EventPurgedWalkForward(
        min_train_rows=min_train_rows,
        validation_rows=validation_rows,
        step_rows=step_rows,
        embargo_minutes=embargo_minutes,
    )
    fold_reports, oos = validate_walk_forward(frame, splitter)
    summary = summarize_validation(fold_reports, oos)

    pipeline = build_pipeline()
    if len(frame) >= 40 and frame["target_stop_hit"].nunique() == 2:
        pipeline.fit(frame[FEATURE_COLUMNS], frame["target_stop_hit"])
    else:
        summary["deployment_eligible"] = False
        summary["reason"] = "INSUFFICIENT_TRAINING_ROWS"

    bundle = {
        "pipeline": pipeline,
        "feature_columns": FEATURE_COLUMNS,
        "numeric_features": NUMERIC_FEATURES,
        "categorical_features": CATEGORICAL_FEATURES,
        "deployment_eligible": bool(summary["deployment_eligible"]),
        "validation_summary": summary,
        "data_stats": data_stats,
        "trained_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "policy": "Paper gate only; never enables live trading.",
    }
    joblib.dump(bundle, model_out)
    report = {
        "data_stats": data_stats,
        "validation_summary": summary,
        "folds": fold_reports,
        "route_counts": frame["route"].value_counts().to_dict(),
    }
    report_out.write_text(json.dumps(report, indent=2, allow_nan=True), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Train event-purged stop-hit risk model.")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--model-out", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--report-out", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--dedup-minutes", type=int, default=30)
    parser.add_argument("--min-train-rows", type=int, default=300)
    parser.add_argument("--validation-rows", type=int, default=100)
    parser.add_argument("--step-rows", type=int, default=100)
    parser.add_argument("--embargo-minutes", type=int, default=60)
    parser.add_argument("--executable-only", action="store_true")
    args = parser.parse_args()

    report = train_stop_hit_model(
        ledger_path=args.ledger,
        model_out=args.model_out,
        report_out=args.report_out,
        dedup_minutes=args.dedup_minutes,
        min_train_rows=args.min_train_rows,
        validation_rows=args.validation_rows,
        step_rows=args.step_rows,
        embargo_minutes=args.embargo_minutes,
        executable_only=args.executable_only,
    )
    print(json.dumps(report, indent=2, allow_nan=True))
    eligible = report["validation_summary"]["deployment_eligible"]
    print(f"\nModel: {args.model_out} | deployment_eligible={eligible}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
