"""Leak-free measurement harness.

Exists to answer one question the current stack cannot: does any tradeable edge survive
honest, purged, cost-aware, out-of-sample evaluation?

It deliberately does NOT trade, size, gate, or route. Those layers are what buried the
answer in the first place. Measure first.
"""
from .cv import Fold, purged_walk_forward, assert_no_leakage
from .data import fetch_klines, load_universe
from .evaluate import SideResult, permutation_null, p_value_vs_null, run_side
from .features import build_features, feature_columns
from .labels import DEFAULT_COST, label_universe, uniqueness_weights

__all__ = [
    "Fold", "purged_walk_forward", "assert_no_leakage",
    "fetch_klines", "load_universe",
    "SideResult", "permutation_null", "p_value_vs_null", "run_side",
    "build_features", "feature_columns",
    "DEFAULT_COST", "label_universe", "uniqueness_weights",
]
