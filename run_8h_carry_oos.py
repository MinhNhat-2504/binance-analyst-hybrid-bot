"""Execute the pre-registered 8h CARRY-7d research cell exactly once per snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from clean_research.carry import CarryConfig, build_carry_panel, evaluate_carry, make_carry_weights
from clean_research.data import fetch_funding_rates, snapshot_hash
from clean_research.intraday_carry import build_8h_panel, make_8h_weights
from honest.data import fetch_klines
from run_clean_oos import DISCOVERY_UNIVERSE, SYMBOL_HOLDOUT_UNIVERSE


ROOT = Path(__file__).resolve().parent
PROTOCOL = ROOT / "eight_hour_carry_protocol_v1.json"
CUTOFF = pd.Timestamp("2026-04-03")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_safe(value):
    """Strict JSON: replace non-finite diagnostic values with null, never NaN."""
    if isinstance(value, float):
        return value if pd.notna(value) and abs(value) != float("inf") else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _load(symbols: list[str], days: int) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    bars, funding = {}, {}
    for symbol in symbols:
        bars[symbol] = fetch_klines(symbol, "8h", days, use_cache=True)
        funding[symbol] = fetch_funding_rates(symbol, days + 30, use_cache=True)
    return bars, funding


def _cells(
    discovery, holdout, *, n_perm: int, periods_per_year: int,
    hac_max_lag: int, permutation_min_shift: int, require_continuous_active: bool,
) -> dict:
    out = {}
    for label, weighted, start, end in (
        ("discovery_pre_cutoff", discovery, None, CUTOFF),
        ("symbol_holdout_pre_cutoff", holdout, None, CUTOFF),
        ("time_replay_discovery", discovery, CUTOFF, None),
        ("double_holdout_replay", holdout, CUTOFF, None),
    ):
        out[label] = {}
        for cost in (10.0, 20.0):
            metrics, _ = evaluate_carry(
                weighted, cost_per_leg_bps=cost, start=start, end=end, n_perm=n_perm,
                periods_per_year=periods_per_year, hac_max_lag=hac_max_lag,
                permutation_min_shift=permutation_min_shift,
                require_continuous_active=require_continuous_active,
            )
            payload = asdict(metrics)
            payload["n_periods"] = payload.pop("n_days")
            payload["mean_bps_period"] = payload.pop("mean_bps_day")
            out[label][f"{int(cost)}bps_per_leg"] = payload
    return out


def _headline(eight_hour: dict, daily: dict) -> dict:
    key, cell = "10bps_per_leg", "double_holdout_replay"
    return {
        "predeclared_cell": f"base_next_8h_open.{cell}.{key}",
        "eight_hour_primary": eight_hour[cell][key],
        "daily_same_snapshot_comparator": daily[cell][key],
        "interpretation": "Compare mean_bps_period, Sharpe, HAC interval, turnover and actual permutation_n. This replay is not pristine and cannot promote either route live.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=600)
    parser.add_argument("--n-perm", type=int, default=999)
    parser.add_argument("--report", default="eight_hour_carry_report.json")
    args = parser.parse_args()
    if not PROTOCOL.exists():
        raise FileNotFoundError(PROTOCOL)
    all_symbols = DISCOVERY_UNIVERSE + SYMBOL_HOLDOUT_UNIVERSE
    bars, funding = _load(all_symbols, args.days)
    common_end = min(pd.to_datetime(frame["Close time"]).max() for frame in bars.values())
    bars = {symbol: frame[pd.to_datetime(frame["Close time"]) <= common_end].copy() for symbol, frame in bars.items()}
    disc_bars = {s: bars[s] for s in DISCOVERY_UNIVERSE}
    disc_funding = {s: funding[s] for s in DISCOVERY_UNIVERSE}
    hold_bars = {s: bars[s] for s in SYMBOL_HOLDOUT_UNIVERSE}
    hold_funding = {s: funding[s] for s in SYMBOL_HOLDOUT_UNIVERSE}
    base_disc = make_8h_weights(build_8h_panel(disc_bars, disc_funding, entry_lag_bars=1))
    base_hold = make_8h_weights(build_8h_panel(hold_bars, hold_funding, entry_lag_bars=1))
    lag_disc = make_8h_weights(build_8h_panel(disc_bars, disc_funding, entry_lag_bars=2))
    lag_hold = make_8h_weights(build_8h_panel(hold_bars, hold_funding, entry_lag_bars=2))
    daily_bars = {symbol: fetch_klines(symbol, "1d", args.days, use_cache=True) for symbol in all_symbols}
    daily_common_end = min(pd.to_datetime(frame["Close time"]).max() for frame in daily_bars.values())
    daily_bars = {symbol: frame[pd.to_datetime(frame["Close time"]) <= daily_common_end].copy() for symbol, frame in daily_bars.items()}
    daily_base_cfg = CarryConfig(execution_lag_bars=1)
    daily_lag_cfg = CarryConfig(execution_lag_bars=2)
    daily_base_disc = make_carry_weights(build_carry_panel({s: daily_bars[s] for s in DISCOVERY_UNIVERSE}, disc_funding, daily_base_cfg), daily_base_cfg)
    daily_base_hold = make_carry_weights(build_carry_panel({s: daily_bars[s] for s in SYMBOL_HOLDOUT_UNIVERSE}, hold_funding, daily_base_cfg), daily_base_cfg)
    daily_lag_disc = make_carry_weights(build_carry_panel({s: daily_bars[s] for s in DISCOVERY_UNIVERSE}, disc_funding, daily_lag_cfg), daily_lag_cfg)
    daily_lag_hold = make_carry_weights(build_carry_panel({s: daily_bars[s] for s in SYMBOL_HOLDOUT_UNIVERSE}, hold_funding, daily_lag_cfg), daily_lag_cfg)
    eight_base = _cells(base_disc, base_hold, n_perm=args.n_perm, periods_per_year=1095, hac_max_lag=21, permutation_min_shift=42, require_continuous_active=True)
    eight_lag = _cells(lag_disc, lag_hold, n_perm=args.n_perm, periods_per_year=1095, hac_max_lag=21, permutation_min_shift=42, require_continuous_active=True)
    daily_base = _cells(daily_base_disc, daily_base_hold, n_perm=args.n_perm, periods_per_year=365, hac_max_lag=7, permutation_min_shift=14, require_continuous_active=False)
    daily_lag = _cells(daily_lag_disc, daily_lag_hold, n_perm=args.n_perm, periods_per_year=365, hac_max_lag=7, permutation_min_shift=14, require_continuous_active=False)
    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": _sha(PROTOCOL), "temporal_replay_is_pristine": False,
        "data": {"common_last_completed_8h_bar": str(common_end), "common_last_completed_daily_bar": str(daily_common_end), "snapshot_sha256": snapshot_hash({**bars, **{f"daily:{s}": frame for s, frame in daily_bars.items()}}, funding), "symbols": len(all_symbols)},
        "headline": _headline(eight_base, daily_base),
        "base_next_8h_open": eight_base,
        "latency_second_next_8h_open": eight_lag,
        "daily_same_snapshot_comparator": {"base_next_daily_open": daily_base, "latency_second_next_daily_open": daily_lag},
        "verdict": "Research-only new cell. It does not alter the locked daily paper route regardless of result.",
    }
    Path(args.report).write_text(json.dumps(_json_safe(report), indent=2, default=str, allow_nan=False), encoding="utf-8")
    for block in ("base_next_8h_open", "latency_second_next_8h_open"):
        d = report[block]["time_replay_discovery"]["10bps_per_leg"]
        h = report[block]["double_holdout_replay"]["10bps_per_leg"]
        print(f"{block}: time {d['mean_bps_period']:+.2f} bps/bar p={d['permutation_p']:.4f}; double {h['mean_bps_period']:+.2f} p={h['permutation_p']:.4f}")
    print(f"report -> {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
