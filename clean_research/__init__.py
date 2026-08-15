"""Leakage-resistant research pipeline for Binance futures signals.

The package is intentionally separate from the legacy notebook artifacts.  It builds an
immutable decision ledger from public market data and only exposes an explicit feature
allowlist to models.
"""

from .ledger import build_daily_decision_ledger
from .schema import FEATURE_SCHEMA_V1, validate_feature_schema

__all__ = ["FEATURE_SCHEMA_V1", "build_daily_decision_ledger", "validate_feature_schema"]
