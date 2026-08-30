"""Controlled exit A/B lab for SHORT Regime0 high-probability candidates.

The script replays 15m futures candles after each eligible signal and compares
multiple exit profiles on the exact same entries. It never enables live trading.

Promotion is intentionally conservative:
- only SHORT_Regime0 rows with final_proba >= min_proba are tested
- metrics are checked on a chronological holdout split
- optimized_gates_v1.json is updated only when --promote is passed and a
  candidate beats the baseline while meeting PF/win/drawdown requirements
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests


LEDGER_PATH = Path("shadow_ledger_candidates_v4.csv")
GATES_PATH = Path("optimized_gates_v1.json")
REPORT_PATH = Path("exit_ab_short0_report.csv")
TRADES_PATH = Path("exit_ab_short0_trades.csv")
KLINE_CACHE_DIR = Path(".exit_ab_kline_cache")
BINANCE_FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"


@dataclass(frozen=True)
class ExitProfile:
    name: str
    tp_pct: float
    sl_pct: float
    breakeven_trigger: float
    breakeven_offset: float
    trailing_stop_pct: float
    tp1_fraction: float
    runner_tp_mult: float
    max_bars: int = 48
    pre_tp_trail_enabled: bool = True
    trail_delay_bars_after_tp: int = 0


PROFILES: list[ExitProfile] = [
    ExitProfile(
        name="baseline_current_short0",
        tp_pct=0.010,
        sl_pct=0.0045,
        breakeven_trigger=0.004,
        breakeven_offset=0.0002,
        trailing_stop_pct=0.003,
        tp1_fraction=0.50,
        runner_tp_mult=3.5,
        pre_tp_trail_enabled=True,
        trail_delay_bars_after_tp=0,
    ),
    ExitProfile(
        name="delay_runner_5bars",
        tp_pct=0.010,
        sl_pct=0.0045,
        breakeven_trigger=0.006,
        breakeven_offset=0.0002,
        trailing_stop_pct=0.0045,
        tp1_fraction=0.50,
        runner_tp_mult=4.0,
        pre_tp_trail_enabled=False,
        trail_delay_bars_after_tp=5,
    ),
    ExitProfile(
        name="faster_cut_delay_runner",
        tp_pct=0.010,
        sl_pct=0.0035,
        breakeven_trigger=0.005,
        breakeven_offset=0.0002,
        trailing_stop_pct=0.004,
        tp1_fraction=0.50,
        runner_tp_mult=3.8,
        pre_tp_trail_enabled=False,
        trail_delay_bars_after_tp=4,
    ),
    ExitProfile(
        name="rich_tp_wide_runner",
        tp_pct=0.012,
        sl_pct=0.0045,
        breakeven_trigger=0.007,
        breakeven_offset=0.0002,
        trailing_stop_pct=0.005,
        tp1_fraction=0.45,
        runner_tp_mult=4.0,
        pre_tp_trail_enabled=False,
        trail_delay_bars_after_tp=6,
    ),
    ExitProfile(
        name="bank_more_tp1",
        tp_pct=0.010,
        sl_pct=0.004,
        breakeven_trigger=0.005,
        breakeven_offset=0.0002,
        trailing_stop_pct=0.004,
        tp1_fraction=0.65,
        runner_tp_mult=3.5,
        pre_tp_trail_enabled=False,
        trail_delay_bars_after_tp=4,
    ),
    # Storm profiles: these are deliberately paper/backfill only. They try to stop
    # the current SL_OR_TRAIL bleed by moving protection earlier on SHORT0.
    ExitProfile(
        name="storm_fast_be_tight_sl",
        tp_pct=0.010,
        sl_pct=0.0028,
        breakeven_trigger=0.0025,
        breakeven_offset=0.0002,
        trailing_stop_pct=0.0022,
        tp1_fraction=0.65,
        runner_tp_mult=3.2,
        pre_tp_trail_enabled=True,
        trail_delay_bars_after_tp=1,
    ),
    ExitProfile(
        name="storm_bank_tp1_fast",
        tp_pct=0.008,
        sl_pct=0.0028,
        breakeven_trigger=0.0022,
        breakeven_offset=0.0002,
        trailing_stop_pct=0.0020,
        tp1_fraction=0.75,
        runner_tp_mult=3.0,
        pre_tp_trail_enabled=True,
        trail_delay_bars_after_tp=1,
    ),
    ExitProfile(
        name="storm_cut_early_delay_runner",
        tp_pct=0.010,
        sl_pct=0.0025,
        breakeven_trigger=0.0030,
        breakeven_offset=0.0002,
        trailing_stop_pct=0.0025,
        tp1_fraction=0.60,
        runner_tp_mult=3.5,
        pre_tp_trail_enabled=True,
        trail_delay_bars_after_tp=3,
    ),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A/B test exit profiles for SHORT_Regime0 high-proba rows.")
    parser.add_argument("--ledger", default=str(LEDGER_PATH), help="Ledger CSV path.")
    parser.add_argument("--gates", default=str(GATES_PATH), help="optimized_gates_v1.json path.")
    parser.add_argument("--min-proba", type=float, default=0.75, help="Minimum final_proba for tested rows.")
    parser.add_argument("--max-rows", type=int, default=220, help="Maximum eligible rows to replay.")
    parser.add_argument(
        "--selection",
        choices=["aged", "newest", "oldest", "closed"],
        default="aged",
        help="Which eligible rows to replay. 'aged' keeps rows older than --min-age-hours, then uses the newest of them.",
    )
    parser.add_argument("--min-age-hours", type=float, default=12.0, help="Minimum signal age used by --selection aged.")
    parser.add_argument(
        "--storm-only",
        action="store_true",
        help="Replay only the freshest closed SHORT0 high-proba rows that define the current storm batch.",
    )
    parser.add_argument("--storm-window", type=int, default=40, help="Number of closed high-proba rows in --storm-only mode.")
    parser.add_argument("--oos-frac", type=float, default=0.35, help="Chronological holdout fraction.")
    parser.add_argument("--min-trades", type=int, default=50, help="Minimum full-sample closed trades for promotion.")
    parser.add_argument("--min-oos-trades", type=int, default=20, help="Minimum holdout closed trades for promotion.")
    parser.add_argument("--min-pf", type=float, default=1.30, help="Minimum holdout profit factor for promotion.")
    parser.add_argument("--min-win-rate", type=float, default=0.50, help="Minimum holdout win rate for promotion.")
    parser.add_argument("--max-dd", type=float, default=0.025, help="Maximum holdout drawdown in return units.")
    parser.add_argument("--promote", action="store_true", help="Update optimized_gates_v1.json if a profile passes.")
    parser.add_argument("--cache-dir", default=str(KLINE_CACHE_DIR), help="Kline cache directory.")
    parser.add_argument("--sleep", type=float, default=0.08, help="Seconds to sleep between uncached API calls.")
    return parser.parse_args()


def normalize_regime(value: Any) -> str:
    try:
        return str(int(float(value)))
    except Exception:
        return str(value)


def parse_mixed_utc(series: pd.Series) -> pd.Series:
    """Parse ledger timestamps that may use either space or ISO "T" separators."""
    try:
        return pd.to_datetime(series, errors="coerce", utc=True, format="mixed")
    except TypeError:
        return pd.to_datetime(series, errors="coerce", utc=True)


def load_eligible_rows(
    ledger_path: Path,
    min_proba: float,
    max_rows: int,
    selection: str,
    min_age_hours: float,
    storm_only: bool = False,
    storm_window: int = 40,
) -> pd.DataFrame:
    if not ledger_path.exists():
        raise FileNotFoundError(f"Ledger not found: {ledger_path}")
    df = pd.read_csv(ledger_path)
    required = {"timestamp_utc", "symbol", "side", "regime", "final_proba", "price"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"Ledger missing required columns: {missing}")

    work = df.copy()
    work["_ts"] = parse_mixed_utc(work["timestamp_utc"])
    work["_age_hours"] = (pd.Timestamp.now(tz=timezone.utc) - work["_ts"]).dt.total_seconds() / 3600.0
    work["_proba"] = pd.to_numeric(work["final_proba"], errors="coerce")
    work["_price"] = pd.to_numeric(work["price"], errors="coerce")
    work["_pnl"] = pd.to_numeric(work.get("Outcome_PnL", pd.Series(np.nan, index=work.index)), errors="coerce")
    work["_regime"] = work["regime"].apply(normalize_regime)
    mask = (
        work["_ts"].notna()
        & work["_price"].gt(0)
        & work["side"].astype(str).str.upper().eq("SHORT")
        & work["_regime"].eq("0")
        & work["_proba"].ge(float(min_proba))
    )
    eligible = work.loc[mask].sort_values("_ts")
    if storm_only:
        eligible = eligible[eligible["_pnl"].notna()].tail(max(1, int(storm_window)))
    elif selection == "aged":
        eligible = eligible[eligible["_age_hours"] >= float(min_age_hours)]
    elif selection == "closed":
        eligible = eligible[eligible["_pnl"].notna()]
    elif selection == "oldest":
        pass
    elif selection == "newest":
        pass
    else:
        raise ValueError(f"Unknown selection: {selection}")

    if max_rows > 0 and len(eligible) > max_rows:
        if selection == "oldest":
            eligible = eligible.head(max_rows)
        else:
            eligible = eligible.tail(max_rows)
    return eligible.reset_index(drop=False).rename(columns={"index": "ledger_index"})


def cache_key(symbol: str, start_ms: int, limit: int) -> str:
    return f"{symbol.upper()}_15m_{start_ms}_{limit}.json"


def fetch_klines(symbol: str, start_ms: int, limit: int, cache_dir: Path, sleep_seconds: float) -> pd.DataFrame:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / cache_key(symbol, start_ms, limit)
    if cache_file.exists():
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
    else:
        params = {"symbol": symbol.upper(), "interval": "15m", "startTime": int(start_ms), "limit": int(limit)}
        response = requests.get(BINANCE_FAPI_KLINES, params=params, timeout=12)
        response.raise_for_status()
        raw = response.json()
        cache_file.write_text(json.dumps(raw), encoding="utf-8")
        if sleep_seconds > 0:
            time.sleep(float(sleep_seconds))

    if not raw:
        return pd.DataFrame()
    cols = [
        "Open time",
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
        "Close time",
        "Quote Asset",
        "Trades",
        "Taker Buy Base",
        "Taker Buy Quote",
        "Ignore",
    ]
    frame = pd.DataFrame(raw, columns=cols)
    for col in ["Open", "High", "Low", "Close"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["Open time"] = pd.to_datetime(frame["Open time"], unit="ms", utc=True)
    return frame.dropna(subset=["Open", "High", "Low", "Close"]).reset_index(drop=True)


def path_stats(bars: pd.DataFrame, entry_price: float, side: str) -> tuple[float, float]:
    if bars.empty:
        return 0.0, 0.0
    if side == "LONG":
        mfe = (bars["High"].max() - entry_price) / entry_price
        mae = (bars["Low"].min() - entry_price) / entry_price
    else:
        mfe = (entry_price - bars["Low"].min()) / entry_price
        mae = (entry_price - bars["High"].max()) / entry_price
    return float(mfe), float(mae)


def replay_exit(
    bars: pd.DataFrame,
    entry_price: float,
    side: str,
    profile: ExitProfile,
    fee_rate: float = 0.0004,
    slippage: float = 0.0005,
) -> dict[str, Any]:
    side = side.upper()
    round_trip_cost = fee_rate * 2 + slippage
    eval_bars = bars.head(profile.max_bars).reset_index(drop=True)
    if eval_bars.empty:
        return {"status": "OPEN", "reason": "", "pnl": np.nan, "bars_held": 0, "mfe": 0.0, "mae": 0.0}

    tp1_fraction = float(np.clip(profile.tp1_fraction, 0.0, 1.0))
    runner_fraction = 1.0 - tp1_fraction

    def raw_ret(price: float) -> float:
        return (price - entry_price) / entry_price if side == "LONG" else (entry_price - price) / entry_price

    def pack(reason: str, exit_price: float, bars_held: int, raw_pnl: float) -> dict[str, Any]:
        used = eval_bars.iloc[: max(1, int(bars_held))]
        mfe, mae = path_stats(used, entry_price, side)
        return {
            "status": "FINAL_EVENT",
            "reason": reason,
            "pnl": float(raw_pnl - round_trip_cost),
            "exit_price": float(exit_price),
            "bars_held": int(bars_held),
            "mfe": mfe,
            "mae": mae,
        }

    if side == "LONG":
        stop_price = entry_price * (1 - profile.sl_pct)
        tp1_price = entry_price * (1 + profile.tp_pct)
        runner_target = entry_price * (1 + profile.tp_pct * profile.runner_tp_mult)
        best_price = entry_price
        tp1_raw: float | None = None
        tp1_bar: int | None = None

        for i, bar in eval_bars.iterrows():
            high, low, close = float(bar["High"]), float(bar["Low"]), float(bar["Close"])
            bars_held = i + 1
            if tp1_raw is None:
                if low <= stop_price:
                    return pack("SL_OR_TRAIL", stop_price, bars_held, raw_ret(stop_price))
                if high >= tp1_price:
                    tp1_raw = raw_ret(tp1_price)
                    tp1_bar = bars_held
                    best_price = max(best_price, high)
                    stop_price = max(stop_price, entry_price * (1 + profile.breakeven_offset))
                    if profile.trail_delay_bars_after_tp <= 0:
                        stop_price = max(stop_price, best_price * (1 - profile.trailing_stop_pct))
                    continue
                best_price = max(best_price, high)
                if profile.pre_tp_trail_enabled and raw_ret(best_price) >= profile.breakeven_trigger:
                    stop_price = max(stop_price, entry_price * (1 + profile.breakeven_offset), best_price * (1 - profile.trailing_stop_pct))
                continue

            if low <= stop_price:
                raw_pnl = tp1_fraction * tp1_raw + runner_fraction * raw_ret(stop_price)
                return pack("TP1_TRAIL_STOP", stop_price, bars_held, raw_pnl)
            if high >= runner_target:
                raw_pnl = tp1_fraction * tp1_raw + runner_fraction * raw_ret(runner_target)
                return pack("TP1_RUNNER_TP", runner_target, bars_held, raw_pnl)
            best_price = max(best_price, high)
            if tp1_bar is None or (bars_held - tp1_bar) >= profile.trail_delay_bars_after_tp:
                stop_price = max(stop_price, entry_price * (1 + profile.breakeven_offset), best_price * (1 - profile.trailing_stop_pct))

        mfe, mae = path_stats(eval_bars, entry_price, side)
        if tp1_raw is not None:
            close_price = float(eval_bars["Close"].iloc[-1])
            raw_pnl = tp1_fraction * tp1_raw + runner_fraction * raw_ret(close_price)
            return pack("TP1_TIME", close_price, len(eval_bars), raw_pnl)
        return {"status": "OPEN", "reason": "", "pnl": np.nan, "bars_held": len(eval_bars), "mfe": mfe, "mae": mae}

    stop_price = entry_price * (1 + profile.sl_pct)
    tp1_price = entry_price * (1 - profile.tp_pct)
    runner_target = entry_price * (1 - profile.tp_pct * profile.runner_tp_mult)
    best_price = entry_price
    tp1_raw = None
    tp1_bar = None

    for i, bar in eval_bars.iterrows():
        high, low, close = float(bar["High"]), float(bar["Low"]), float(bar["Close"])
        bars_held = i + 1
        if tp1_raw is None:
            if high >= stop_price:
                return pack("SL_OR_TRAIL", stop_price, bars_held, raw_ret(stop_price))
            if low <= tp1_price:
                tp1_raw = raw_ret(tp1_price)
                tp1_bar = bars_held
                best_price = min(best_price, low)
                stop_price = min(stop_price, entry_price * (1 - profile.breakeven_offset))
                if profile.trail_delay_bars_after_tp <= 0:
                    stop_price = min(stop_price, best_price * (1 + profile.trailing_stop_pct))
                continue
            best_price = min(best_price, low)
            if profile.pre_tp_trail_enabled and raw_ret(best_price) >= profile.breakeven_trigger:
                stop_price = min(stop_price, entry_price * (1 - profile.breakeven_offset), best_price * (1 + profile.trailing_stop_pct))
            continue

        if high >= stop_price:
            raw_pnl = tp1_fraction * tp1_raw + runner_fraction * raw_ret(stop_price)
            return pack("TP1_TRAIL_STOP", stop_price, bars_held, raw_pnl)
        if low <= runner_target:
            raw_pnl = tp1_fraction * tp1_raw + runner_fraction * raw_ret(runner_target)
            return pack("TP1_RUNNER_TP", runner_target, bars_held, raw_pnl)
        best_price = min(best_price, low)
        if tp1_bar is None or (bars_held - tp1_bar) >= profile.trail_delay_bars_after_tp:
            stop_price = min(stop_price, entry_price * (1 - profile.breakeven_offset), best_price * (1 + profile.trailing_stop_pct))

    mfe, mae = path_stats(eval_bars, entry_price, side)
    if tp1_raw is not None:
        close_price = float(eval_bars["Close"].iloc[-1])
        raw_pnl = tp1_fraction * tp1_raw + runner_fraction * raw_ret(close_price)
        return pack("TP1_TIME", close_price, len(eval_bars), raw_pnl)
    return {"status": "OPEN", "reason": "", "pnl": np.nan, "bars_held": len(eval_bars), "mfe": mfe, "mae": mae}


def profit_factor(series: pd.Series) -> float:
    gains = float(series[series > 0].sum())
    losses = float(-series[series < 0].sum())
    if losses == 0:
        return 99.0 if gains > 0 else 0.0
    return gains / losses


def max_drawdown(series: pd.Series) -> float:
    if series.empty:
        return 0.0
    equity = series.cumsum()
    return float((equity.cummax() - equity).max())


def summarize(results: pd.DataFrame, oos_frac: float) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for profile_name, group in results.dropna(subset=["pnl"]).groupby("profile"):
        group = group.sort_values("timestamp_utc").reset_index(drop=True)
        split = max(1, int(math.floor(len(group) * (1.0 - oos_frac))))
        train = group.iloc[:split]
        oos = group.iloc[split:] if split < len(group) else group.iloc[0:0]
        for split_name, part in [("full", group), ("train", train), ("oos", oos)]:
            pnl = pd.to_numeric(part["pnl"], errors="coerce").dropna()
            rows.append(
                {
                    "profile": profile_name,
                    "split": split_name,
                    "n": int(len(pnl)),
                    "total_pnl": float(pnl.sum()) if len(pnl) else 0.0,
                    "mean_bps": float(pnl.mean() * 10000.0) if len(pnl) else np.nan,
                    "win_rate": float((pnl > 0).mean()) if len(pnl) else np.nan,
                    "pf": float(profit_factor(pnl)) if len(pnl) else np.nan,
                    "max_dd": float(max_drawdown(pnl)) if len(pnl) else np.nan,
                    "sl_or_trail_frac": float((part["reason"].eq("SL_OR_TRAIL")).mean()) if len(part) else np.nan,
                    "tp_runner_frac": float((part["reason"].eq("TP1_RUNNER_TP")).mean()) if len(part) else np.nan,
                    "tp_trail_frac": float((part["reason"].eq("TP1_TRAIL_STOP")).mean()) if len(part) else np.nan,
                }
            )
    return pd.DataFrame(rows)


def choose_winner(summary: pd.DataFrame, args: argparse.Namespace) -> tuple[str | None, str]:
    full = summary[summary["split"].eq("full")].set_index("profile")
    oos = summary[summary["split"].eq("oos")].set_index("profile")
    if "baseline_current_short0" not in full.index or "baseline_current_short0" not in oos.index:
        return None, "NO_BASELINE"

    baseline_oos = oos.loc["baseline_current_short0"]
    candidates: list[tuple[float, str]] = []
    for profile, row in oos.iterrows():
        if profile == "baseline_current_short0":
            continue
        full_row = full.loc[profile]
        checks = [
            full_row["n"] >= args.min_trades,
            row["n"] >= args.min_oos_trades,
            row["total_pnl"] > 0,
            row["pf"] >= args.min_pf,
            row["win_rate"] >= args.min_win_rate,
            row["max_dd"] <= args.max_dd,
            row["pf"] >= baseline_oos["pf"],
            row["total_pnl"] >= baseline_oos["total_pnl"],
        ]
        if all(bool(x) for x in checks):
            score = float(row["total_pnl"] * 10000.0 + min(row["pf"], 5.0) * 30.0 + row["win_rate"] * 20.0 - row["max_dd"] * 2500.0)
            candidates.append((score, profile))
    if not candidates:
        return None, "NO_PROFILE_MET_PROMOTION_CRITERIA"
    candidates.sort(reverse=True)
    return candidates[0][1], "PROMOTION_CANDIDATE"


def promote_profile(gates_path: Path, profile: ExitProfile, summary: pd.DataFrame, reason: str) -> None:
    if gates_path.exists():
        data = json.loads(gates_path.read_text(encoding="utf-8"))
    else:
        data = {}
    pocket = data.setdefault("SHORT_Regime0", {})
    pocket["Live_Allowed"] = False
    pocket["Paper_Allowed"] = True
    pocket["Execution_Mode"] = "PAPER_ONLY"
    pocket["Pocket_Status"] = "RESEARCH"
    pocket["Paper_Promote_Guard"] = {
        "enabled": True,
        "rule": "Exit_Profile promotion is paper-only; live deployment must be a separate manual decision.",
        "live_allowed_forced": False,
    }
    pocket["Exit_Profile"] = {
        "enabled": True,
        "source": "exit_ab_short0_lab",
        "promoted_at_utc": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        **asdict(profile),
    }
    pocket["Exit_AB_Test"] = {
        "report_path": str(REPORT_PATH),
        "trades_path": str(TRADES_PATH),
        "metrics": summary[summary["profile"].eq(profile.name)].to_dict(orient="records"),
    }
    gates_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    ledger_path = Path(args.ledger)
    gates_path = Path(args.gates)
    cache_dir = Path(args.cache_dir)

    eligible = load_eligible_rows(
        ledger_path,
        args.min_proba,
        args.max_rows,
        args.selection,
        args.min_age_hours,
        storm_only=args.storm_only,
        storm_window=args.storm_window,
    )
    print(
        "Eligible SHORT_Regime0 rows: "
        f"{len(eligible)} | min_proba={args.min_proba} | max_rows={args.max_rows} "
        f"| selection={args.selection} | min_age_hours={args.min_age_hours:g} "
        f"| storm_only={args.storm_only} | storm_window={args.storm_window}"
    )
    if eligible.empty:
        if args.selection == "aged":
            print("No aged eligible rows. Wait for more candles or lower --min-age-hours for a diagnostic-only run.")
        else:
            print("No eligible rows. Nothing to test.")
        return 0
    print(f"Eligible time range: {eligible['_ts'].min().isoformat()} -> {eligible['_ts'].max().isoformat()}")

    trade_rows: list[dict[str, Any]] = []
    fetch_errors = 0
    for n, row in eligible.iterrows():
        symbol = str(row["symbol"]).upper()
        ts = pd.Timestamp(row["_ts"])
        start_ms = int(ts.timestamp() * 1000)
        entry_price = float(row["_price"])
        try:
            bars = fetch_klines(symbol, start_ms, 50, cache_dir, args.sleep)
        except Exception as exc:
            fetch_errors += 1
            print(f"fetch_error {symbol} {ts.isoformat()}: {type(exc).__name__}: {exc}")
            continue
        bars = bars[bars["Open time"] > ts].reset_index(drop=True)
        if bars.empty:
            continue
        for profile in PROFILES:
            event = replay_exit(bars, entry_price, "SHORT", profile)
            if event["status"] != "FINAL_EVENT":
                continue
            trade_rows.append(
                {
                    "ledger_index": int(row["ledger_index"]),
                    "timestamp_utc": ts.isoformat(),
                    "symbol": symbol,
                    "final_proba": float(row["_proba"]),
                    "profile": profile.name,
                    "pnl": event["pnl"],
                    "reason": event["reason"],
                    "bars_held": event["bars_held"],
                    "mfe": event["mfe"],
                    "mae": event["mae"],
                }
            )
        if (n + 1) % 25 == 0:
            print(f"processed {n + 1}/{len(eligible)} rows")

    if not trade_rows:
        print(f"No closed events replayed. fetch_errors={fetch_errors}")
        return 1

    trades = pd.DataFrame(trade_rows)
    summary = summarize(trades, args.oos_frac).sort_values(["split", "pf", "total_pnl"], ascending=[True, False, False])
    trades.to_csv(TRADES_PATH, index=False)
    summary.to_csv(REPORT_PATH, index=False)

    print("\nA/B summary")
    display_cols = ["profile", "split", "n", "total_pnl", "mean_bps", "win_rate", "pf", "max_dd", "sl_or_trail_frac", "tp_runner_frac", "tp_trail_frac"]
    print(summary[display_cols].to_string(index=False))
    print(f"\nReports written: {REPORT_PATH} | {TRADES_PATH}")

    winner_name, reason = choose_winner(summary, args)
    print(f"\nDecision: {reason}" + (f" -> {winner_name}" if winner_name else ""))
    if args.promote and winner_name:
        profile = next(p for p in PROFILES if p.name == winner_name)
        promote_profile(gates_path, profile, summary, reason)
        print(f"Promoted paper-only Exit_Profile to {gates_path}: {winner_name}")
        print("Guard active: Execution_Mode=PAPER_ONLY and Live_Allowed=False. No live gate was enabled.")
    elif args.promote:
        print("Promotion requested, but no profile passed. Gate file unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
