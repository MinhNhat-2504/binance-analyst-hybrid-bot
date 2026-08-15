"""Leak-safe ledger training and portfolio evaluation for daily cross-sectional alpha."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

from .carry import PortfolioMetrics, evaluate_carry
from .schema import FEATURE_SCHEMA_V1, validate_feature_schema


FUNDING_CROWDING_FEATURES = (
    "funding_last",
    "funding_sum_1d",
    "funding_mean_3d",
    "funding_sum_7d",
    "funding_z_30",
    "xs_funding",
    "xs_funding_7d",
    "ret_3d",
    "ret_7d",
    "ret_14d",
    "xs_ret_3d",
    "xs_ret_7d",
    "xs_ret_14d",
    "vol_30d",
    "xs_vol_30d",
    "market_dispersion_1d",
)


@dataclass(frozen=True)
class RidgeSpec:
    name: str
    alpha: float
    features: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.alpha < 0:
            raise ValueError("ridge alpha cannot be negative")
        validate_feature_schema(self.features, require_all=False)
        canonical = tuple(c for c in FEATURE_SCHEMA_V1 if c in set(self.features))
        if self.features != canonical:
            raise ValueError("model features must follow the canonical schema order")


@dataclass
class TrainedRidge:
    spec: RidgeSpec
    scaler: StandardScaler
    model: Ridge
    n_training_rows: int
    training_first_entry: pd.Timestamp
    training_last_label: pd.Timestamp

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        _validate_feature_frame(frame, self.spec.features)
        x = self.scaler.transform(frame.loc[:, self.spec.features].to_numpy(float))
        return self.model.predict(x)

    def coefficient_table(self) -> list[dict[str, float | str]]:
        rows = [
            {"feature": feature, "standardized_coefficient": float(coef)}
            for feature, coef in zip(self.spec.features, self.model.coef_)
        ]
        return sorted(rows, key=lambda row: abs(float(row["standardized_coefficient"])), reverse=True)


def default_model_specs() -> tuple[RidgeSpec, ...]:
    """Small, fixed development grid; tail fraction and target are not tuned."""

    specs = []
    for alpha in (100.0, 1_000.0, 10_000.0):
        specs.append(
            RidgeSpec(
                name=f"funding_crowding_ridge_a{int(alpha)}",
                alpha=alpha,
                features=tuple(c for c in FEATURE_SCHEMA_V1 if c in FUNDING_CROWDING_FEATURES),
            )
        )
        specs.append(
            RidgeSpec(
                name=f"full_schema_ridge_a{int(alpha)}",
                alpha=alpha,
                features=FEATURE_SCHEMA_V1,
            )
        )
    return tuple(specs)


def _validate_feature_frame(frame: pd.DataFrame, features: tuple[str, ...]) -> None:
    validate_feature_schema(features, require_all=False)
    missing = [c for c in features if c not in frame]
    if missing:
        raise ValueError(f"feature frame is missing columns: {missing}")
    values = frame.loc[:, features].to_numpy(float)
    if not np.isfinite(values).all():
        raise ValueError("feature frame contains non-finite values")


def _rank_target(frame: pd.DataFrame) -> pd.Series:
    """Within-date rank of the executable long gross outcome.

    A positive weight earns price return and pays funding, so the long-equivalent gross
    outcome is ``price_return - realized_funding``.  Cross-sectional ranks prevent a few
    extreme altcoin days from dominating the regression fit.
    """

    gross_long = frame["price_return"] - frame["realized_funding"]
    return gross_long.groupby(frame["decision_time"]).rank(method="average", pct=True) - 0.5


def fit_ridge(frame: pd.DataFrame, spec: RidgeSpec) -> TrainedRidge:
    if frame.empty:
        raise ValueError("cannot train on an empty ledger")
    _validate_feature_frame(frame, spec.features)
    y = _rank_target(frame).to_numpy(float)
    x_raw = frame.loc[:, spec.features].to_numpy(float)
    scaler = StandardScaler()
    x = scaler.fit_transform(x_raw)
    counts = frame.groupby("decision_time")["symbol"].transform("size").to_numpy(float)
    sample_weight = (1.0 / counts)
    sample_weight /= sample_weight.mean()
    model = Ridge(alpha=spec.alpha, fit_intercept=True)
    model.fit(x, y, sample_weight=sample_weight)
    return TrainedRidge(
        spec=spec,
        scaler=scaler,
        model=model,
        n_training_rows=len(frame),
        training_first_entry=pd.Timestamp(frame["entry_time"].min()),
        training_last_label=pd.Timestamp(frame["label_available_at"].max()),
    )


def make_prediction_weights(
    ledger: pd.DataFrame,
    predictions: np.ndarray | pd.Series,
    *,
    tail_fraction: float = 0.20,
    min_symbols: int = 10,
) -> pd.DataFrame:
    """Convert predictions to a complete zero-filled daily outcome/weight panel."""

    if not 0.0 < tail_fraction < 0.5:
        raise ValueError("tail_fraction must be in (0, 0.5)")
    if len(ledger) != len(predictions):
        raise ValueError("prediction length does not match ledger")
    out = ledger.copy()
    out["prediction"] = np.asarray(predictions, dtype=float)
    out["eligible"] = np.isfinite(out["prediction"])
    out["weight"] = 0.0
    for _, group in out.groupby("decision_time", sort=True):
        eligible = group[group["eligible"]]
        if len(eligible) < min_symbols:
            continue
        ranks = eligible["prediction"].rank(method="average", pct=True)
        long_idx = ranks[ranks >= 1.0 - tail_fraction].index
        short_idx = ranks[ranks <= tail_fraction].index
        if not len(long_idx) or not len(short_idx):
            continue
        if set(long_idx) & set(short_idx):
            raise AssertionError("prediction tails overlap")
        out.loc[long_idx, "weight"] = 0.5 / len(long_idx)
        out.loc[short_idx, "weight"] = -0.5 / len(short_idx)
    return out.sort_values(["decision_time", "symbol"]).reset_index(drop=True)


def walk_forward_folds(
    ledger: pd.DataFrame,
    *,
    min_train_days: int = 180,
    validation_days: int = 60,
    min_validation_days: int = 30,
) -> list[tuple[pd.DataFrame, pd.DataFrame, dict]]:
    """Expanding timestamp folds with label-availability purging."""

    dates = pd.DatetimeIndex(sorted(pd.to_datetime(ledger["entry_time"]).unique()))
    folds = []
    start = min_train_days
    fold_id = 0
    while start < len(dates):
        stop = min(start + validation_days, len(dates))
        if stop - start < min_validation_days:
            break
        val_start, val_end = dates[start], dates[stop - 1] + pd.Timedelta(days=1)
        train = ledger[pd.to_datetime(ledger["label_available_at"]) < val_start].copy()
        validation = ledger[
            (pd.to_datetime(ledger["entry_time"]) >= val_start)
            & (pd.to_datetime(ledger["entry_time"]) < val_end)
        ].copy()
        if train.empty or validation.empty:
            start = stop
            continue
        if pd.Timestamp(train["label_available_at"].max()) >= val_start:
            raise AssertionError("walk-forward train labels cross the validation boundary")
        meta = {
            "fold": fold_id,
            "train_rows": len(train),
            "validation_rows": len(validation),
            "train_last_label": pd.Timestamp(train["label_available_at"].max()).isoformat(),
            "validation_first_entry": pd.Timestamp(validation["entry_time"].min()).isoformat(),
            "validation_last_entry": pd.Timestamp(validation["entry_time"].max()).isoformat(),
        }
        folds.append((train, validation, meta))
        fold_id += 1
        start = stop
    if not folds:
        raise ValueError("not enough ledger history to create walk-forward folds")
    return folds


def select_ridge_spec(
    development_ledger: pd.DataFrame,
    specs: tuple[RidgeSpec, ...],
    *,
    cost_per_leg_bps: float,
    tail_fraction: float = 0.20,
    min_symbols: int = 10,
    min_train_days: int = 180,
    validation_days: int = 60,
) -> tuple[RidgeSpec, dict]:
    """Select one spec using development folds only; holdouts never enter this function."""

    folds = walk_forward_folds(
        development_ledger,
        min_train_days=min_train_days,
        validation_days=validation_days,
    )
    candidates = []
    for spec in specs:
        fold_rows = []
        for train, validation, meta in folds:
            fitted = fit_ridge(train, spec)
            predictions = fitted.predict(validation)
            weighted = make_prediction_weights(
                validation,
                predictions,
                tail_fraction=tail_fraction,
                min_symbols=min_symbols,
            )
            metrics, _ = evaluate_carry(
                weighted,
                cost_per_leg_bps=cost_per_leg_bps,
                n_perm=0,
            )
            fold_rows.append({**meta, **asdict(metrics)})
        means = np.asarray([row["mean_bps_day"] for row in fold_rows], dtype=float)
        candidates.append(
            {
                "name": spec.name,
                "alpha": spec.alpha,
                "features": list(spec.features),
                "n_features": len(spec.features),
                "median_fold_net_bps_day": float(np.median(means)),
                "mean_fold_net_bps_day": float(np.mean(means)),
                "worst_fold_net_bps_day": float(np.min(means)),
                "positive_fold_fraction": float(np.mean(means > 0)),
                "folds": fold_rows,
            }
        )
    # Median fold result is deliberately the only selection statistic.  Tie breaks
    # prefer fewer features and then stronger regularisation.
    selected_row = max(
        candidates,
        key=lambda row: (
            row["median_fold_net_bps_day"],
            -row["n_features"],
            row["alpha"],
        ),
    )
    selected = next(spec for spec in specs if spec.name == selected_row["name"])
    return selected, {
        "selection_statistic": "median_fold_net_bps_day",
        "n_candidates": len(specs),
        "n_folds": len(folds),
        "selected": selected.name,
        "candidates": candidates,
    }
