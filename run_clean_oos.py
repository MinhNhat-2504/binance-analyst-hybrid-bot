"""Run the frozen daily ledger/OOS protocol without touching live-bot settings.

This script deliberately separates model selection from three confirmations:

* unseen symbols over the development calendar;
* later time on discovery symbols;
* later time and unseen symbols simultaneously.

Historical post-cutoff data has already been inspected by earlier carry experiments, so
the report calls it a retrospective OOS replay rather than a pristine future holdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from clean_research.carry import (
    CarryConfig,
    build_carry_panel,
    evaluate_carry,
    make_carry_weights,
)
from clean_research.data import load_daily_bundle, snapshot_hash
from clean_research.ledger import build_daily_decision_ledger
from clean_research.model import (
    default_model_specs,
    fit_ridge,
    make_prediction_weights,
    select_ridge_spec,
)
from clean_research.schema import FEATURE_SCHEMA_VERSION


PROTOCOL_PATH = Path("clean_oos_protocol_v1.json")
AS_OF = pd.Timestamp("2026-08-01 23:59:59.999")
TEMPORAL_CUTOFF = pd.Timestamp("2026-04-03 00:00:00")
BASE_COST_BPS = 10.0
STRESS_COST_BPS = 20.0
TAIL_FRACTION = 0.20
ADJUSTED_ALPHA = 0.025  # two final routes: carry and trained Ridge

RESEARCH_CODE_PATHS = (
    Path("run_clean_oos.py"),
    Path("clean_research/__init__.py"),
    Path("clean_research/schema.py"),
    Path("clean_research/data.py"),
    Path("clean_research/ledger.py"),
    Path("clean_research/carry.py"),
    Path("clean_research/model.py"),
    Path("tests/test_clean_research.py"),
    Path("pytest.ini"),
)

DISCOVERY_UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "AVAXUSDT", "LINKUSDT", "NEARUSDT", "ADAUSDT", "LTCUSDT", "BCHUSDT",
    "DOTUSDT", "ATOMUSDT", "UNIUSDT", "FILUSDT", "TRXUSDT", "ETCUSDT",
    "XLMUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT", "SUIUSDT",
    "TIAUSDT", "SEIUSDT", "RUNEUSDT", "AAVEUSDT", "MKRUSDT", "CRVUSDT",
    "SANDUSDT", "MANAUSDT", "GALAUSDT", "1000PEPEUSDT", "1000SHIBUSDT",
    "WLDUSDT", "TONUSDT", "ENAUSDT", "JTOUSDT", "PYTHUSDT", "TAOUSDT",
    "ORDIUSDT",
]

SYMBOL_HOLDOUT_UNIVERSE = [
    "ICPUSDT", "HBARUSDT", "ALGOUSDT", "VETUSDT", "EGLDUSDT", "THETAUSDT",
    "XTZUSDT", "NEOUSDT", "IOTAUSDT", "KAVAUSDT", "ZILUSDT", "DYDXUSDT",
    "GMTUSDT", "APEUSDT", "LDOUSDT", "IMXUSDT", "STXUSDT", "FLOWUSDT",
    "CHZUSDT", "SNXUSDT", "COMPUSDT", "YFIUSDT", "SUSHIUSDT", "1INCHUSDT",
    "ENJUSDT", "DASHUSDT", "ZECUSDT", "GRTUSDT", "MINAUSDT", "ROSEUSDT",
    "ARUSDT", "CELOUSDT", "QNTUSDT",
]


def _json_default(value):
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"cannot JSON encode {type(value)!r}")


def _json_safe(value):
    """Recursively replace non-finite numbers so artifacts are strict JSON."""

    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (np.floating, float)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, np.ndarray):
        return _json_safe(value.tolist())
    return value


def _write_json(path: Path, payload: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(
            _json_safe(payload),
            indent=2,
            default=_json_default,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    temp.replace(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _frame_hash(frame: pd.DataFrame) -> str:
    ordered = frame.sort_values(["decision_time", "symbol"]).reset_index(drop=True)
    values = pd.util.hash_pandas_object(ordered, index=False).values.tobytes()
    return hashlib.sha256(values).hexdigest()


def _code_manifest() -> dict[str, str]:
    missing = [str(path) for path in RESEARCH_CODE_PATHS if not path.exists()]
    if missing:
        raise FileNotFoundError(f"research code manifest is missing files: {missing}")
    return {str(path).replace("\\", "/"): _sha256_file(path) for path in RESEARCH_CODE_PATHS}


def _return_metrics(values: pd.Series) -> dict:
    r = pd.to_numeric(values, errors="coerce").dropna()
    gains = float(r[r > 0].sum())
    losses = float(-r[r < 0].sum())
    return {
        "n": int(len(r)),
        "mean_bps": float(r.mean() * 10_000) if len(r) else None,
        "profit_factor": gains / losses if losses > 0 else None,
        "sum_return": float(r.sum()),
        "win_rate": float((r > 0).mean()) if len(r) else None,
    }


def _non_overlapping(frame: pd.DataFrame, key_columns: list[str]) -> pd.DataFrame:
    accepted = []
    next_available: dict[tuple, pd.Timestamp] = {}
    for idx, row in frame.sort_values("timestamp_utc").iterrows():
        key = tuple(row[c] for c in key_columns)
        available = next_available.get(key)
        if available is None or pd.Timestamp(row["timestamp_utc"]) >= available:
            accepted.append(idx)
            next_available[key] = pd.Timestamp(row["reconstructed_exit"])
    return frame.loc[accepted].copy()


def audit_runtime_ledger(path: Path) -> dict:
    """Show why the legacy runtime ledger is not a valid model-training table."""

    if not path.exists():
        return {"exists": False, "path": str(path)}
    use = [
        "timestamp_utc", "symbol", "side", "execution_stage", "size_usd",
        "Outcome_PnL", "Bars_Held", "is_trade_live", "is_rejected",
    ]
    frame = pd.read_csv(path, usecols=use, low_memory=False)
    frame["timestamp_utc"] = pd.to_datetime(
        frame["timestamp_utc"], format="mixed", errors="coerce", utc=True
    )
    frame["size_usd"] = pd.to_numeric(frame["size_usd"], errors="coerce")
    frame["Outcome_PnL"] = pd.to_numeric(frame["Outcome_PnL"], errors="coerce")
    frame["Bars_Held"] = pd.to_numeric(frame["Bars_Held"], errors="coerce")
    stage = frame["execution_stage"].astype(str).str.upper()
    actionable = frame[
        (stage.str.startswith("PAPER") | stage.str.startswith("LIVE"))
        & (frame["size_usd"].fillna(0) > 0)
        & frame["Outcome_PnL"].notna()
        & frame["timestamp_utc"].notna()
    ].copy()
    actionable["entry_bar_15m"] = actionable["timestamp_utc"].dt.floor("15min")
    dedup = actionable.sort_values("timestamp_utc").drop_duplicates(
        ["entry_bar_15m", "symbol", "side"], keep="first"
    )
    dedup["reconstructed_exit"] = (
        dedup["timestamp_utc"].dt.ceil("15min")
        + pd.to_timedelta(dedup["Bars_Held"].fillna(0) * 15, unit="min")
    )
    by_symbol_side = _non_overlapping(dedup, ["symbol", "side"])
    by_symbol = _non_overlapping(dedup, ["symbol"])
    forbidden_markers = [
        "Outcome_PnL", "Exit_Price", "Bars_Held", "MFE", "MAE",
        "Ret_15M", "Ret_1H", "Ret_12H", "is_backfilled",
    ]
    return {
        "exists": True,
        "path": str(path),
        "sha256": _sha256_file(path),
        "rows": int(len(frame)),
        "closed_outcomes": int(frame["Outcome_PnL"].notna().sum()),
        "actionable_closed_rows": int(len(actionable)),
        "raw_actionable": _return_metrics(actionable["Outcome_PnL"]),
        "deduplicated_15m_symbol_side": _return_metrics(dedup["Outcome_PnL"]),
        "one_open_position_per_symbol_side": _return_metrics(by_symbol_side["Outcome_PnL"]),
        "one_open_position_per_symbol": _return_metrics(by_symbol["Outcome_PnL"]),
        "actionable_calendar_days": int(actionable["timestamp_utc"].dt.normalize().nunique()),
        "usable_for_primary_training": False,
        "reason": (
            "The file is a short, repeated runtime decision log without raw causal market "
            "features or realised fills. Outcome/execution columns must never be predictors."
        ),
        "examples_of_forbidden_predictors": forbidden_markers,
    }


def _split_bundle(bundle: dict[str, pd.DataFrame], symbols: list[str]) -> dict[str, pd.DataFrame]:
    return {symbol: bundle[symbol] for symbol in symbols}


def _load_frozen_data() -> tuple[dict, dict, dict]:
    all_symbols = DISCOVERY_UNIVERSE + SYMBOL_HOLDOUT_UNIVERSE
    if set(DISCOVERY_UNIVERSE) & set(SYMBOL_HOLDOUT_UNIVERSE):
        raise AssertionError("discovery and symbol holdout universes overlap")
    bars, funding = load_daily_bundle(
        all_symbols,
        days_back=600,
        funding_days=630,
        as_of=AS_OF,
        use_cache=True,
    )
    missing = sorted(set(all_symbols) - set(bars))
    if missing:
        raise RuntimeError(f"frozen data bundle is missing symbols: {missing}")
    common_last_close = min(pd.to_datetime(frame["Close time"]).max() for frame in bars.values())
    bars = {
        symbol: frame[pd.to_datetime(frame["Close time"]) <= common_last_close]
        .copy()
        .reset_index(drop=True)
        for symbol, frame in bars.items()
    }
    manifest = {
        "as_of_utc": AS_OF.isoformat(),
        "common_last_completed_bar": pd.Timestamp(common_last_close).isoformat(),
        "n_symbols": len(all_symbols),
        "discovery_symbols": DISCOVERY_UNIVERSE,
        "symbol_holdout_symbols": SYMBOL_HOLDOUT_UNIVERSE,
        "market_snapshot_sha256": snapshot_hash(bars, funding),
        "bar_rows": int(sum(len(frame) for frame in bars.values())),
        "funding_rows": int(sum(len(frame) for frame in funding.values())),
    }
    return bars, funding, manifest


def _evaluate_partitions(
    discovery_weighted: pd.DataFrame,
    holdout_weighted: pd.DataFrame,
    *,
    n_perm: int,
) -> dict:
    definitions = {
        "discovery_pre_cutoff": (discovery_weighted, None, TEMPORAL_CUTOFF),
        "symbol_holdout_pre_cutoff": (holdout_weighted, None, TEMPORAL_CUTOFF),
        "time_replay_discovery": (discovery_weighted, TEMPORAL_CUTOFF, None),
        "double_holdout_replay": (holdout_weighted, TEMPORAL_CUTOFF, None),
    }
    report = {}
    for name, (weighted, start, end) in definitions.items():
        cells = {}
        for cost in (BASE_COST_BPS, STRESS_COST_BPS):
            metrics, _ = evaluate_carry(
                weighted,
                cost_per_leg_bps=cost,
                start=start,
                end=end,
                n_perm=n_perm,
            )
            cells[f"{int(cost)}bps_per_leg"] = asdict(metrics)
        report[name] = cells
    return report


def _evaluate_latency_replay(
    discovery_weighted: pd.DataFrame,
    holdout_weighted: pd.DataFrame,
    *,
    n_perm: int,
) -> dict:
    report = {}
    for name, weighted in (
        ("time_replay_discovery", discovery_weighted),
        ("double_holdout_replay", holdout_weighted),
    ):
        report[name] = {}
        for cost in (BASE_COST_BPS, STRESS_COST_BPS):
            metrics, _ = evaluate_carry(
                weighted,
                cost_per_leg_bps=cost,
                start=TEMPORAL_CUTOFF,
                n_perm=n_perm,
            )
            report[name][f"{int(cost)}bps_per_leg"] = asdict(metrics)
    return report


def _route_gate(partitions: dict, latency: dict) -> dict:
    time_base = partitions["time_replay_discovery"]["10bps_per_leg"]
    double_base = partitions["double_holdout_replay"]["10bps_per_leg"]
    time_stress = partitions["time_replay_discovery"]["20bps_per_leg"]
    double_stress = partitions["double_holdout_replay"]["20bps_per_leg"]
    lag_time = latency["time_replay_discovery"]["10bps_per_leg"]
    lag_double = latency["double_holdout_replay"]["10bps_per_leg"]
    lag_time_stress = latency["time_replay_discovery"]["20bps_per_leg"]
    lag_double_stress = latency["double_holdout_replay"]["20bps_per_leg"]
    checks = {
        "base_net_positive_both_time_blocks": min(
            time_base["mean_bps_day"], double_base["mean_bps_day"]
        ) > 0,
        "stress_net_positive_both_time_blocks": min(
            time_stress["mean_bps_day"], double_stress["mean_bps_day"]
        ) > 0,
        "base_hac_lower_positive_both_time_blocks": min(
            time_base["hac_ci_lo_bps"], double_base["hac_ci_lo_bps"]
        ) > 0,
        "base_shift_p_adjusted_both_time_blocks": max(
            time_base["permutation_p"], double_base["permutation_p"]
        ) < ADJUSTED_ALPHA,
        "latency_net_positive_at_base_and_stress_cost_both_time_blocks": min(
            lag_time["mean_bps_day"],
            lag_double["mean_bps_day"],
            lag_time_stress["mean_bps_day"],
            lag_double_stress["mean_bps_day"],
        ) > 0,
        "max_drawdown_under_25pct_both_time_blocks": min(
            time_base["max_drawdown"], double_base["max_drawdown"]
        ) > -0.25,
    }
    return {
        "checks": checks,
        "historical_research_gate_passed": all(checks.values()),
        "temporal_holdout_is_pristine": False,
        "profit_verified": False,
        "live_eligible": False,
    }


def _build_carry_route(bars: dict, funding: dict, *, n_perm: int) -> dict:
    base_config = CarryConfig(
        lookback_days=7,
        tail_fraction=TAIL_FRACTION,
        cost_per_leg_bps=BASE_COST_BPS,
        min_history_days=30,
        min_funding_observations=18,
        min_holding_funding_observations=2,
        min_symbols=10,
        execution_lag_bars=1,
    )
    discovery_bars = _split_bundle(bars, DISCOVERY_UNIVERSE)
    discovery_funding = _split_bundle(funding, DISCOVERY_UNIVERSE)
    holdout_bars = _split_bundle(bars, SYMBOL_HOLDOUT_UNIVERSE)
    holdout_funding = _split_bundle(funding, SYMBOL_HOLDOUT_UNIVERSE)

    discovery_panel = build_carry_panel(discovery_bars, discovery_funding, base_config)
    holdout_panel = build_carry_panel(holdout_bars, holdout_funding, base_config)
    discovery_weighted = make_carry_weights(discovery_panel, base_config)
    holdout_weighted = make_carry_weights(holdout_panel, base_config)
    partitions = _evaluate_partitions(
        discovery_weighted, holdout_weighted, n_perm=n_perm
    )

    delayed_config = replace(base_config, execution_lag_bars=2)
    delayed_discovery = make_carry_weights(
        build_carry_panel(discovery_bars, discovery_funding, delayed_config), delayed_config
    )
    delayed_holdout = make_carry_weights(
        build_carry_panel(holdout_bars, holdout_funding, delayed_config), delayed_config
    )
    latency = _evaluate_latency_replay(
        delayed_discovery, delayed_holdout, n_perm=n_perm
    )
    gate = _route_gate(partitions, latency)
    return {
        "config": asdict(base_config),
        "partitions": partitions,
        "latency_stress_second_next_open": latency,
        "gate": gate,
    }


def _ledger_bundle(
    bars: dict,
    funding: dict,
    symbols: list[str],
    *,
    execution_lag_bars: int,
) -> pd.DataFrame:
    return build_daily_decision_ledger(
        _split_bundle(bars, symbols),
        _split_bundle(funding, symbols),
        horizon_days=1,
        execution_lag_bars=execution_lag_bars,
        round_trip_cost_bps=2 * BASE_COST_BPS,
        min_cross_section=10,
    )


def _build_trained_route(
    bars: dict,
    funding: dict,
    *,
    n_perm: int,
    model_out: Path,
) -> dict:
    discovery = _ledger_bundle(
        bars, funding, DISCOVERY_UNIVERSE, execution_lag_bars=1
    )
    holdout = _ledger_bundle(
        bars, funding, SYMBOL_HOLDOUT_UNIVERSE, execution_lag_bars=1
    )
    development = discovery[
        (pd.to_datetime(discovery["entry_time"]) < TEMPORAL_CUTOFF)
        & (pd.to_datetime(discovery["label_available_at"]) < TEMPORAL_CUTOFF)
    ].copy()
    selected_spec, selection = select_ridge_spec(
        development,
        default_model_specs(),
        cost_per_leg_bps=BASE_COST_BPS,
        tail_fraction=TAIL_FRACTION,
        min_symbols=10,
        min_train_days=180,
        validation_days=60,
    )
    fitted = fit_ridge(development, selected_spec)
    discovery_weighted = make_prediction_weights(
        discovery,
        fitted.predict(discovery),
        tail_fraction=TAIL_FRACTION,
        min_symbols=10,
    )
    holdout_weighted = make_prediction_weights(
        holdout,
        fitted.predict(holdout),
        tail_fraction=TAIL_FRACTION,
        min_symbols=10,
    )
    partitions = _evaluate_partitions(
        discovery_weighted, holdout_weighted, n_perm=n_perm
    )

    delayed_discovery_ledger = _ledger_bundle(
        bars, funding, DISCOVERY_UNIVERSE, execution_lag_bars=2
    )
    delayed_holdout_ledger = _ledger_bundle(
        bars, funding, SYMBOL_HOLDOUT_UNIVERSE, execution_lag_bars=2
    )
    delayed_discovery = make_prediction_weights(
        delayed_discovery_ledger,
        fitted.predict(delayed_discovery_ledger),
        tail_fraction=TAIL_FRACTION,
        min_symbols=10,
    )
    delayed_holdout = make_prediction_weights(
        delayed_holdout_ledger,
        fitted.predict(delayed_holdout_ledger),
        tail_fraction=TAIL_FRACTION,
        min_symbols=10,
    )
    latency = _evaluate_latency_replay(
        delayed_discovery, delayed_holdout, n_perm=n_perm
    )
    gate = _route_gate(partitions, latency)

    joblib.dump(
        {
            "stage": "RESEARCH_ONLY",
            "live_eligible": False,
            "protocol_version": "CLEAN_DAILY_OOS_V1",
            "temporal_cutoff": TEMPORAL_CUTOFF,
            "feature_schema": FEATURE_SCHEMA_VERSION,
            "model": fitted,
        },
        model_out,
    )
    return {
        "ledger": {
            "discovery_rows": len(discovery),
            "symbol_holdout_rows": len(holdout),
            "development_rows": len(development),
            "discovery_sha256": _frame_hash(discovery),
            "symbol_holdout_sha256": _frame_hash(holdout),
            "feature_schema": FEATURE_SCHEMA_VERSION,
            "entry_is_after_decision": bool((discovery["entry_time"] > discovery["decision_time"]).all()),
            "decision_ids_unique": bool(
                discovery["decision_id"].is_unique and holdout["decision_id"].is_unique
            ),
        },
        "selection": selection,
        "selected_model": {
            "name": selected_spec.name,
            "alpha": selected_spec.alpha,
            "features": list(selected_spec.features),
            "n_training_rows": fitted.n_training_rows,
            "training_first_entry": fitted.training_first_entry,
            "training_last_label": fitted.training_last_label,
            "standardized_coefficients": fitted.coefficient_table(),
            "artifact": str(model_out),
            "artifact_sha256": _sha256_file(model_out),
            "artifact_stage": "RESEARCH_ONLY",
        },
        "partitions": partitions,
        "latency_stress_second_next_open": latency,
        "gate": gate,
    }


def _paper_policy(routes: dict) -> dict:
    candidates = []
    for name, route in routes.items():
        partitions = route["partitions"]
        latency = route["latency_stress_second_next_open"]
        key_metrics = [
            partitions["time_replay_discovery"]["10bps_per_leg"]["mean_bps_day"],
            partitions["double_holdout_replay"]["10bps_per_leg"]["mean_bps_day"],
            partitions["time_replay_discovery"]["20bps_per_leg"]["mean_bps_day"],
            partitions["double_holdout_replay"]["20bps_per_leg"]["mean_bps_day"],
            latency["time_replay_discovery"]["10bps_per_leg"]["mean_bps_day"],
            latency["double_holdout_replay"]["10bps_per_leg"]["mean_bps_day"],
        ]
        if min(key_metrics) > 0:
            candidates.append((min(key_metrics), name))
    selected = max(candidates)[1] if candidates else None
    historical_gate = bool(
        selected and routes[selected]["gate"]["historical_research_gate_passed"]
    )
    return {
        "stage": "PAPER_SHADOW_ONLY" if selected else "RESEARCH_ONLY",
        "selected_route": selected,
        "selection_basis": "positive point estimates only; this is not a passed profit gate",
        "historical_research_gate_passed": historical_gate,
        "profit_verified": False,
        "live_enabled": False,
        "capital_authorized": 0.0,
        "reason": (
            "Historical replay is not a pristine future holdout and the statistical gate "
            "must remain closed until genuinely new paper observations accumulate."
        ),
        "forward_requirements": {
            "minimum_calendar_days": 60,
            "minimum_active_days": 40,
            "same_frozen_symbols_features_and_cost_contract": True,
            "base_hac_95_ci_lower_above_zero": True,
            "stress_20bps_per_leg_net_positive": True,
            "shift_permutation_p_below_0_05_for_single_frozen_route": True,
            "max_drawdown_under_25pct": True,
            "paper_fills_include_observed_latency_spread_and_slippage": True,
            "no_live_activation_without_new_review": True,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-perm", type=int, default=999)
    parser.add_argument("--report", default="clean_oos_report.json")
    parser.add_argument("--policy", default="clean_research_policy_v1.json")
    parser.add_argument("--model", default="clean_research_model_v1.joblib")
    parser.add_argument("--runtime-ledger", default="shadow_ledger_candidates_v4.csv")
    args = parser.parse_args()
    if args.n_perm < 1:
        raise ValueError("n-perm must be positive for the final protocol")
    if not PROTOCOL_PATH.exists():
        raise FileNotFoundError(f"missing frozen protocol: {PROTOCOL_PATH}")

    print("Loading the frozen market snapshot...")
    bars, funding, data_manifest = _load_frozen_data()
    print("Running corrected funding-crowding portfolio...")
    carry = _build_carry_route(bars, funding, n_perm=args.n_perm)
    print("Building the causal decision ledger and selecting the Ridge on development folds...")
    trained = _build_trained_route(
        bars,
        funding,
        n_perm=args.n_perm,
        model_out=Path(args.model),
    )
    routes = {"carry_7d": carry, "trained_ridge": trained}
    policy = _paper_policy(routes)
    protocol_sha = _sha256_file(PROTOCOL_PATH)
    code_sha = _code_manifest()
    if policy["selected_route"] == "carry_7d":
        selected_definition = {
            "route": "carry_7d",
            "config": carry["config"],
            "protocol_sha256": protocol_sha,
        }
    elif policy["selected_route"] == "trained_ridge":
        selected_definition = {
            "route": "trained_ridge",
            "selected_model": trained["selected_model"],
            "protocol_sha256": protocol_sha,
        }
    else:
        selected_definition = None
    policy["selected_route_definition"] = selected_definition
    policy["selected_route_definition_sha256"] = (
        hashlib.sha256(
            json.dumps(
                _json_safe(selected_definition),
                sort_keys=True,
                default=_json_default,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        if selected_definition is not None
        else None
    )
    policy["protocol_sha256"] = protocol_sha
    policy["code_sha256"] = code_sha
    runtime_audit = audit_runtime_ledger(Path(args.runtime_ledger))
    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_version": "CLEAN_DAILY_OOS_V1",
        "protocol_path": str(PROTOCOL_PATH),
        "protocol_sha256": protocol_sha,
        "code_sha256": code_sha,
        "temporal_cutoff_entry_utc": TEMPORAL_CUTOFF.isoformat(),
        "temporal_replay_is_pristine": False,
        "profit_verified": False,
        "live_enabled": False,
        "data_manifest": data_manifest,
        "legacy_runtime_ledger_audit": runtime_audit,
        "routes": routes,
        "paper_policy": policy,
        "verdict": (
            "No route is considered verified profitable until it passes the frozen gate on "
            "genuinely new forward paper data. Positive retrospective point estimates alone "
            "do not authorize live trading."
        ),
    }
    _write_json(Path(args.report), report)
    _write_json(Path(args.policy), policy)

    print("\nRoute summary (10 bps/leg, post-cutoff replay):")
    for name, route in routes.items():
        t = route["partitions"]["time_replay_discovery"]["10bps_per_leg"]
        h = route["partitions"]["double_holdout_replay"]["10bps_per_leg"]
        print(
            f"  {name:16s} discovery {t['mean_bps_day']:+7.2f} bps/day "
            f"CI[{t['hac_ci_lo_bps']:+.2f},{t['hac_ci_hi_bps']:+.2f}] p={t['permutation_p']:.4f} | "
            f"double {h['mean_bps_day']:+7.2f} CI[{h['hac_ci_lo_bps']:+.2f},"
            f"{h['hac_ci_hi_bps']:+.2f}] p={h['permutation_p']:.4f}"
        )
    print(f"\nPolicy: {policy['stage']} / live_enabled={policy['live_enabled']}")
    print(f"Report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
