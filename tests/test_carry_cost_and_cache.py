from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest

from clean_research import data as data_module
from clean_research.carry import block_bootstrap_net_profit_p_value, evaluate_carry


def _alternating_weighted(n_days: int = 60) -> pd.DataFrame:
    rows = []
    for day_index, entry in enumerate(pd.date_range("2026-01-01", periods=n_days, freq="1D")):
        sign = 1.0 if day_index % 2 == 0 else -1.0
        for symbol, weight, outcome in (("A", 0.5 * sign, 0.003 * sign), ("B", -0.5 * sign, -0.003 * sign)):
            rows.append({
                "decision_time": entry - pd.Timedelta(milliseconds=1), "entry_time": entry,
                "exit_time": entry + pd.Timedelta(days=1), "symbol": symbol,
                "price_return": outcome, "realized_funding": 0.0, "weight": weight,
            })
    return pd.DataFrame(rows)


def test_net_profit_block_null_changes_with_cost() -> None:
    weighted = _alternating_weighted()
    low, _ = evaluate_carry(weighted, cost_per_leg_bps=10, n_perm=199, permutation_min_shift=5)
    high, _ = evaluate_carry(weighted, cost_per_leg_bps=20, n_perm=199, permutation_min_shift=5)
    assert low.permutation_p == high.permutation_p
    assert low.net_profit_block_p < high.net_profit_block_p
    assert low.mean_bps_day > 0 > high.mean_bps_day


def test_block_bootstrap_net_profit_input_guards() -> None:
    p, n = block_bootstrap_net_profit_p_value(pd.Series([0.01, 0.02]), n_boot=99)
    assert pd.isna(p) and n == 0


def test_stale_funding_cache_is_incrementally_extended(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data_module, "CACHE_DIR", tmp_path)
    now = pd.Timestamp("2026-08-15T12:00:00Z")
    monkeypatch.setattr(data_module.time, "time", lambda: now.timestamp())
    old = pd.Timestamp("2026-08-01T00:00:00")
    pd.DataFrame({"fundingTime": [old], "fundingRate": [0.0001]}).to_parquet(
        tmp_path / "BTCUSDT_funding_30d.parquet", index=False
    )
    requested_starts = []

    def fake_get_json(url: str):
        requested_starts.append(int(parse_qs(urlparse(url).query)["startTime"][0]))
        return [{"fundingTime": int(pd.Timestamp("2026-08-15T08:00:00Z").timestamp() * 1000), "fundingRate": "0.0002"}]

    monkeypatch.setattr(data_module, "_get_json", fake_get_json)
    out = data_module.fetch_funding_rates("BTCUSDT", 30, use_cache=True)
    assert requested_starts == [int(old.timestamp() * 1000) + 1]
    assert out["fundingTime"].max() == pd.Timestamp("2026-08-15 08:00:00")
    assert len(out) == 2
