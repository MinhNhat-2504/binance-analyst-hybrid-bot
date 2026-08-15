"""Versioned predictor schema.

Unknown numeric columns are denied by default.  This is the opposite of the legacy
blacklist behaviour, which could accidentally admit outcome and execution columns from a
runtime ledger.
"""

from __future__ import annotations

from collections.abc import Iterable


FEATURE_SCHEMA_VERSION = "CLEAN_DAILY_V1"

FEATURE_SCHEMA_V1 = (
    # Time-series price/volatility state, all known at decision_time.
    "ret_1d",
    "ret_3d",
    "ret_7d",
    "ret_14d",
    "ret_30d",
    "vol_7d",
    "vol_30d",
    "range_1d",
    "range_5d",
    "volume_z_30d",
    "log_quote_volume_30d",
    "taker_imbalance_1d",
    "taker_imbalance_3d",
    # Funding observations settled before decision_time.
    "funding_last",
    "funding_sum_1d",
    "funding_mean_3d",
    "funding_sum_7d",
    "funding_z_30",
    # Cross-sectional state at the same decision timestamp.
    "xs_ret_1d",
    "xs_ret_3d",
    "xs_ret_7d",
    "xs_ret_14d",
    "xs_vol_30d",
    "xs_funding",
    "xs_funding_7d",
    "xs_liquidity",
    "xs_taker_imbalance",
    # Market context and beta-adjusted residual momentum.
    "market_ret_1d",
    "market_ret_3d",
    "market_breadth_1d",
    "market_dispersion_1d",
    "beta_60d",
    "resid_ret_3d",
    "xs_resid_ret_3d",
)

_FORBIDDEN_MARKERS = (
    "outcome",
    "target",
    "future",
    "forward",
    "exit_",
    "bars_held",
    "mfe",
    "mae",
    "provisional",
    "is_backfilled",
    "ret_15m",
    "ret_30m",
    "ret_1h",
    "ret_3h",
    "ret_6h",
    "ret_12h",
    "pass_live",
    "is_rejected",
    "size_usd",
    "final_proba",
    "gating_proba",
)


def validate_feature_schema(columns: Iterable[str], *, require_all: bool = True) -> tuple[str, ...]:
    """Validate a predictor list and return it as an immutable tuple.

    Models are only allowed to consume the exact versioned schema.  The marker check is a
    second line of defence so a future schema edit cannot casually add a post-decision
    column.
    """

    cols = tuple(columns)
    unknown = sorted(set(cols) - set(FEATURE_SCHEMA_V1))
    missing = sorted(set(FEATURE_SCHEMA_V1) - set(cols))
    forbidden = sorted(c for c in cols if any(m in c.lower() for m in _FORBIDDEN_MARKERS))
    if unknown:
        raise ValueError(f"unknown predictors denied by {FEATURE_SCHEMA_VERSION}: {unknown}")
    if require_all and missing:
        raise ValueError(f"missing predictors for {FEATURE_SCHEMA_VERSION}: {missing}")
    if forbidden:
        raise ValueError(f"post-decision predictors are forbidden: {forbidden}")
    if len(cols) != len(set(cols)):
        raise ValueError("duplicate predictors are not allowed")
    if require_all and cols != FEATURE_SCHEMA_V1:
        raise ValueError(
            f"predictors must use the canonical {FEATURE_SCHEMA_VERSION} column order"
        )
    return cols
