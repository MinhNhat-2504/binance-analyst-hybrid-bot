"""Export the locked CARRY-7d target book for testnet reconciliation.

This script only writes a target file.  It has no order-placement code and does not read
any API credential.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from honest.daily import build_panel
from honest.data import fetch_klines
from run_carry_paper import target_weights


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "carry_paper_config_v1.json"
STATE = ROOT / "carry_paper_state.json"
DEFAULT_OUT = ROOT / "execution" / "carry_targets_latest.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _open_panel(symbols: list[str], days: int) -> pd.DataFrame:
    """Open-price panel; mirrors run_carry_paper.build_opens (fills happen at next-day open)."""
    opens = {}
    for sym in symbols:
        try:
            k = fetch_klines(sym, "1d", days, use_cache=False)
            idx = pd.to_datetime(k["Open time"]).dt.normalize()
            ser = k.set_index(idx)["Open"]
            opens[sym] = ser[~ser.index.duplicated(keep="last")]
        except Exception:
            pass
    return pd.DataFrame(opens).sort_index()


def _decision_close_references(symbols: list[str], day: pd.Timestamp) -> dict[str, float]:
    """Causal, independent price references from the signal day's settled close."""
    references: dict[str, float] = {}
    for symbol in symbols:
        bars = fetch_klines(symbol, "1d", 60, use_cache=False)
        match = bars[pd.to_datetime(bars["Open time"]).dt.normalize() == day]
        if not match.empty:
            references[symbol] = float(match.iloc[-1]["Close"])
    missing = sorted(set(symbols) - set(references))
    if missing:
        raise RuntimeError(f"missing signal-close reference prices: {missing}")
    return references


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--signal-day", help="UTC YYYY-MM-DD; default: latest completed daily bar")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--rebuild-from-public-data", action="store_true", help="research/debug only; default exports the frozen paper state")
    args = parser.parse_args()

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    config_sha = _sha(CONFIG)
    if STATE.exists():
        state = json.loads(STATE.read_text(encoding="utf-8"))
        recorded = state.get("config_sha256")
        if recorded and recorded != config_sha:
            raise RuntimeError("paper config hash differs from active paper state; refusing target export")

    if not args.rebuild_from_public_data:
        # Default: the SAME rule the paper bot runs, applied to the LATEST completed signal
        # day. The paper ledger books day t only once day t+1's open exists (two mornings
        # later), but the executor needs day t's weights on the morning of t+1 - so we do
        # not read the booked weights, we recompute them with the paper bot's own
        # target_weights() on fresh data with the same tradability mask. When the paper
        # state has already booked this day, the two MUST agree; that cross-check is what
        # keeps this from being a second, driftable implementation.
        if not STATE.exists():
            raise RuntimeError("frozen paper state is required for default target export")
        days_back = max(60, int(cfg["funding_lookback_days"]) + 30)
        px, fday = build_panel(cfg["universe"], days=days_back, min_days=10, use_cache=False, verbose=False)
        opens = _open_panel(cfg["universe"], days_back).reindex(columns=px.columns)
        latest = pd.Timestamp(px.index.max()).normalize()
        day = pd.Timestamp(args.signal_day).normalize() if args.signal_day else latest
        if day not in fday.index or day not in px.index:
            raise RuntimeError(f"signal day {day.date()} is unavailable in the funding/price panel")
        fill_day = day + pd.Timedelta(days=1)
        weights = target_weights(fday, day, int(cfg["funding_lookback_days"]), float(cfg["quantile"]))
        # Same mask as run_carry_paper.py: a symbol is tradeable only if it printed a close
        # on the signal day and has an open on the fill day (or the fill day is today and
        # not yet printed - then require the signal-day close only).
        tradeable = px.loc[day].notna()
        if fill_day in opens.index:
            tradeable &= opens.loc[fill_day].notna()
        weights = weights.where(tradeable, 0.0)
        booked_day = str(state.get("last_signal_day", ""))
        if booked_day == day.date().isoformat():
            booked = pd.Series(state.get("weights", {}), dtype=float).reindex(weights.index).fillna(0.0)
            diff = (booked - weights).abs().max()
            if diff > 1e-6:
                raise RuntimeError(
                    f"recomputed weights for {day.date()} differ from the booked paper state "
                    f"(max |diff| {diff:.6f}); refusing to export a target that disagrees with the paper record"
                )
        references = {s: float(px.loc[day, s]) for s in weights.index if weights[s] != 0 and pd.notna(px.loc[day, s])}
        missing = sorted(s for s in weights.index if weights[s] != 0 and s not in references)
        if missing:
            raise RuntimeError(f"missing signal-close reference prices: {missing}")
        source = (f"paper rule recomputed for latest signal day {day.date()} "
                  f"({'cross-checked against booked paper state' if booked_day == day.date().isoformat() else 'paper state not yet booked for this day'})")
    else:
        days_back = max(60, int(cfg["funding_lookback_days"]) + 30)
        px, fday = build_panel(cfg["universe"], days=days_back, min_days=10, use_cache=True, verbose=False)
        day = pd.Timestamp(args.signal_day) if args.signal_day else pd.Timestamp(px.index.max())
        day = day.normalize()
        if day not in fday.index:
            raise RuntimeError(f"signal day {day.date()} is unavailable")
        weights = target_weights(fday, day, int(cfg["funding_lookback_days"]), float(cfg["quantile"]))
        references = {symbol: float(px.loc[day, symbol]) for symbol in weights.index if pd.notna(px.loc[day, symbol])}
        source = "explicit public-data rebuild; not a paper-state promotion"
    execution_day = day + pd.Timedelta(days=1)
    active = {
        symbol: round(float(weight), 10)
        for symbol, weight in weights.items()
        if weight != 0
    }
    gross = sum(abs(value) for value in active.values())
    if not active or gross < 0.95 or gross > 1.01:
        raise RuntimeError(f"invalid exported exposure gross={gross:.6f}; no target written")
    target_id = hashlib.sha256(
        f"{config_sha}|{day.isoformat()}|{sorted(active.items())}".encode("utf-8")
    ).hexdigest()[:24]
    payload = {
        "version": "CARRY_EXECUTION_TARGET_V1",
        "strategy": "CARRY-7d",
        "target_id": target_id,
        "config_sha256": config_sha,
        "signal_time_utc": (day + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)).isoformat() + "Z",
        "intended_execution_utc": execution_day.isoformat() + "Z",
        "weights": active,
        "reference_prices": references,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source": source,
    }
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"exported {len(active)} testnet targets -> {output}")
    print(f"target_id={target_id} signal={day.date()} execution={execution_day.date()} gross={gross:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
