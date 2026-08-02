"""Feature engineering.

Ported verbatim from Model_Training_Lab.ipynb Cell 1 (QuantFeatureEngineer). The audit
found this code clean: every transform is backward-looking (rolling/shift with positive
lag), so it is reused as-is to keep results comparable with the existing models.

Two deliberate changes from the notebook:

1. The notebook defines engineer_mtf_features twice. The Cell 1 fallback (which only sets
   Hour=0) shadows the real Cell 3 implementation at call time, so the 4H/1H features never
   actually reached the model. Here MTF is a real, opt-in flag.
2. Asset_* one-hot columns are dropped. They hard-code a 10-symbol universe and let the
   model memorise per-symbol quirks that do not generalise to new listings.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Columns that are raw OHLCV, bookkeeping, labels, or otherwise not model inputs.
NON_FEATURE_COLS = {
    "Open", "High", "Low", "Close", "Volume", "Close time", "Quote Asset", "Trades",
    "Taker Buy Base", "Taker Buy Quote", "Ignore", "Open time", "symbol", "Symbol",
    # labels / label-derived - never features
    "TBM_Label", "target_long", "target_short", "label_horizon_end",
    "ret_long_net", "ret_short_net", "exit_bars_long", "exit_bars_short",
    "uniq_w", "fold",
    # setup flags are decisions, kept out of X to avoid double-counting
    "setup_long_candidate", "setup_short_candidate",
}

# Raw price and volume LEVELS. These are non-stationary and differ by orders of magnitude
# across symbols (BTC ~$100k vs DOGE ~$0.4; BTC volume vs a micro-cap). A single model
# trained across the whole universe can split on an absolute level to identify the symbol
# or the era, manufacturing out-of-sample "skill" that is really memorisation - the same
# failure mode as the notebook's Asset_* one-hots. Only their normalised derivatives
# (EMA_14_Dist, ATR_Pct, Range_Pct, ...) are stationary and cross-symbol comparable, so
# those stay; the levels themselves are banned. MACD and its kin scale with price too, so
# every MACD* column is dropped via the startswith check in feature_columns().
NON_STATIONARY_LEVELS = {
    "EMA_14", "EMA_50", "BB_Mid", "BB_Upper", "BB_Lower", "BB_Std",
    "Recent_Demand_Zone", "Recent_Supply_Zone", "ATR_14",
    "Taker Buy Base", "Taker_Buy_Vol", "Taker_Sell_Vol",
    "Lower_Wick", "Upper_Wick", "Body_Abs",
    "EMA_9_4H_HTF",  # MTF raw price level, only present when mtf=True
}


def _build_base(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if "Open time" in df.columns:
        if not pd.api.types.is_datetime64_any_dtype(df["Open time"]):
            df["Open time"] = pd.to_datetime(df["Open time"], utc=True).dt.tz_localize(None)
        df["Hour"] = df["Open time"].dt.hour
        df["DayOfWeek"] = df["Open time"].dt.dayofweek
    else:
        df["Hour"], df["DayOfWeek"] = 0, 0

    df["Return"] = df["Close"].pct_change()
    df["EMA_14"] = df["Close"].ewm(span=14, adjust=False).mean()
    df["EMA_50"] = df["Close"].ewm(span=50, adjust=False).mean()

    delta = df["Close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    df["RSI_14"] = 100 - (100 / (1 + (gain / loss.replace(0, np.nan))))

    df["MACD"] = df["Close"].ewm(span=12, adjust=False).mean() - df["Close"].ewm(span=26, adjust=False).mean()
    df["MACD_Signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_Gap"] = df["MACD"] - df["MACD_Signal"]
    df["MACD_Hist"] = df["MACD"] - df["MACD_Signal"]

    df["BB_Mid"] = df["Close"].rolling(window=20).mean()
    df["BB_Std"] = df["Close"].rolling(window=20).std()
    df["BB_Upper"] = df["BB_Mid"] + (df["BB_Std"] * 2)
    df["BB_Lower"] = df["BB_Mid"] - (df["BB_Std"] * 2)
    return df


def _build_advanced(df: pd.DataFrame) -> pd.DataFrame:
    df["Log_Return_1"] = np.log(df["Close"]).diff()
    for i in [3, 6, 12, 24]:
        df[f"Return_{i}"] = df["Close"].pct_change(i)
    df["Range_Pct"] = (df["High"] - df["Low"]) / df["Close"].replace(0, np.nan)
    df["Body_Pct"] = (df["Close"] - df["Open"]) / df["Open"].replace(0, np.nan)
    df["Volume_Change"] = df["Volume"].pct_change().replace([np.inf, -np.inf], np.nan)
    df["BB_Width"] = (df["BB_Upper"] - df["BB_Lower"]) / df["BB_Mid"].replace(0, np.nan)
    volume_mean_24 = df["Volume"].rolling(24).mean()
    volume_std_24 = df["Volume"].rolling(24).std().replace(0, np.nan)
    df["Volume_Z"] = (df["Volume"] - volume_mean_24) / volume_std_24
    df["Volume_Regime"] = df["Volume"] / df["Volume"].rolling(72).mean().replace(0, np.nan) - 1.0

    df["Lower_Wick"] = df[["Open", "Close"]].min(axis=1) - df["Low"]
    df["Upper_Wick"] = df["High"] - df[["Open", "Close"]].max(axis=1)
    df["Body_Abs"] = (df["Close"] - df["Open"]).abs()
    high_vol = df["Volume"] > volume_mean_24 * 1.5
    df["Is_Bullish_OB"] = ((df["Lower_Wick"] > df["Body_Abs"] * 2) & high_vol).astype(int)
    df["Is_Bearish_OB"] = ((df["Upper_Wick"] > df["Body_Abs"] * 2) & high_vol).astype(int)
    df["Recent_Demand_Zone"] = df["Low"].where(df["Is_Bullish_OB"] == 1).ffill()
    df["Recent_Supply_Zone"] = df["High"].where(df["Is_Bearish_OB"] == 1).ffill()
    df["Dist_to_Demand"] = (df["Close"] - df["Recent_Demand_Zone"]) / df["Close"]
    df["Dist_to_Supply"] = (df["Recent_Supply_Zone"] - df["Close"]) / df["Close"]

    prev_close = df["Close"].shift(1)
    true_range = pd.concat(
        [df["High"] - df["Low"], (df["High"] - prev_close).abs(), (df["Low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    df["ATR_14"] = true_range.rolling(14).mean()
    df["ATR_Pct"] = df["ATR_14"] / df["Close"].replace(0, np.nan)
    df["ATR_Regime"] = df["ATR_Pct"] / df["ATR_Pct"].rolling(72).mean().replace(0, np.nan)
    df["Volatility_24"] = df["Return"].rolling(24).std()
    df["Volatility_Regime"] = df["Volatility_24"] / df["Volatility_24"].rolling(72).mean().replace(0, np.nan)
    df["EMA_14_Dist"] = (df["Close"] - df["EMA_14"]) / df["EMA_14"].replace(0, np.nan)
    df["EMA_50_Dist"] = (df["Close"] - df["EMA_50"]) / df["EMA_50"].replace(0, np.nan)
    df["Trend_Strength"] = (df["EMA_14"] - df["EMA_50"]) / df["Close"].replace(0, np.nan)
    df["EMA_14_Slope_3"] = df["EMA_14"].pct_change(3)
    df["EMA_50_Slope_6"] = df["EMA_50"].pct_change(6)
    df["RSI_14_Norm"] = df["RSI_14"] / 100.0
    df["Breakout_20"] = df["Close"] / df["High"].rolling(20).max().shift(1).replace(0, np.nan) - 1.0
    df["Breakdown_20"] = df["Close"] / df["Low"].rolling(20).min().shift(1).replace(0, np.nan) - 1.0
    df["Dist_to_Demand"] = df["Dist_to_Demand"].fillna(999.0)
    df["Dist_to_Supply"] = df["Dist_to_Supply"].fillna(999.0)

    df["Taker_Buy_Vol"] = pd.to_numeric(df.get("Taker Buy Base", 0), errors="coerce").fillna(0)
    df["Taker_Sell_Vol"] = df["Volume"] - df["Taker_Buy_Vol"]
    df["Taker_Buy_Ratio"] = df["Taker_Buy_Vol"] / df["Volume"].replace(0, np.nan)
    df["Taker_Imbalance"] = (df["Taker_Buy_Vol"] - df["Taker_Sell_Vol"]) / df["Volume"].replace(0, np.nan)
    df["Taker_Imbalance_Delta"] = df["Taker_Imbalance"].diff()
    vol_percentile_75 = df["Volume"].rolling(72).quantile(0.75)
    df["Is_High_Vol_Bucket"] = (df["Volume"] > vol_percentile_75).astype(int)
    df["Smart_Money_Flow"] = df["Taker_Imbalance"] * df["Is_High_Vol_Bucket"]
    return df


def _add_lags(df: pd.DataFrame) -> pd.DataFrame:
    lag_steps = [1, 2, 3, 6, 12]
    lag_cols = [
        "Log_Return_1", "Return_3", "Return_6", "Return_12", "Return_24", "EMA_14_Dist",
        "EMA_50_Dist", "Trend_Strength", "RSI_14_Norm", "MACD_Gap", "ATR_Pct", "Volume_Z",
        "Volume_Regime", "Volatility_24", "Volatility_Regime", "Breakout_20", "Breakdown_20",
    ]
    new = {}
    for col in lag_cols:
        if col in df.columns:
            for lag in lag_steps:
                new[f"{col}_lag_{lag}"] = df[col].shift(lag)
    return pd.concat([df, pd.DataFrame(new, index=df.index)], axis=1)


def _build_setup_flags(df: pd.DataFrame) -> pd.DataFrame:
    df["Setup_Trend_Primary_L"] = ((df["EMA_14"] > df["EMA_50"]) & (df["Trend_Strength"] > -0.002)).astype(int)
    df["Setup_MACD_OK_L"] = (df["MACD_Gap"] > -0.0005).astype(int)
    df["Setup_RSI_OK_L"] = ((df["RSI_14_Norm"] >= 0.44) & (df["RSI_14_Norm"] <= 0.74)).astype(int)
    df["Setup_ATR_OK"] = (df["ATR_Pct"] <= 0.03).astype(int)
    df["Setup_Vol_OK"] = (df["Volatility_Regime"] <= 1.9).astype(int)
    df["Setup_Volume_OK"] = (df["Volume_Regime"] > -0.40).astype(int)
    df["Setup_Breakout_OK_L"] = (df["Breakout_20"] > -0.03).astype(int)
    df["Setup_Slope_OK_L"] = ((df["EMA_14_Slope_3"] > -0.002) & (df["EMA_50_Slope_6"] > -0.003)).astype(int)
    df["Setup_Long_Score"] = df[[
        "Setup_Trend_Primary_L", "Setup_MACD_OK_L", "Setup_RSI_OK_L", "Setup_ATR_OK",
        "Setup_Vol_OK", "Setup_Volume_OK", "Setup_Breakout_OK_L", "Setup_Slope_OK_L",
    ]].sum(axis=1)
    df["setup_long_candidate"] = (
        (df["Setup_Long_Score"] >= 5) & (df["Setup_Trend_Primary_L"] == 1) & (df["Setup_ATR_OK"] == 1)
    )

    df["Setup_Trend_Primary_S"] = ((df["EMA_14"] < df["EMA_50"]) & (df["Trend_Strength"] < 0.002)).astype(int)
    df["Setup_MACD_OK_S"] = (df["MACD_Gap"] < 0.0005).astype(int)
    df["Setup_RSI_OK_S"] = ((df["RSI_14_Norm"] <= 0.56) & (df["RSI_14_Norm"] >= 0.26)).astype(int)
    df["Setup_Breakdown_OK_S"] = (df["Breakdown_20"] < 0.03).astype(int)
    df["Setup_Slope_OK_S"] = ((df["EMA_14_Slope_3"] < 0.002) & (df["EMA_50_Slope_6"] < 0.003)).astype(int)
    df["Setup_Short_Score"] = df[[
        "Setup_Trend_Primary_S", "Setup_MACD_OK_S", "Setup_RSI_OK_S", "Setup_ATR_OK",
        "Setup_Vol_OK", "Setup_Volume_OK", "Setup_Breakdown_OK_S", "Setup_Slope_OK_S",
    ]].sum(axis=1)
    df["setup_short_candidate"] = (
        (df["Setup_Short_Score"] >= 5) & (df["Setup_Trend_Primary_S"] == 1) & (df["Setup_ATR_OK"] == 1)
    )
    return df


def add_mtf(df: pd.DataFrame) -> pd.DataFrame:
    """4H/1H context features. Each higher-frame value is shifted one full HTF bar before
    being joined, so a 15m bar only ever sees an HTF bar that has already closed."""
    d = df.set_index("Open time")

    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    df_4h = d.resample("4h").agg(agg).dropna()
    ema9 = df_4h["Close"].ewm(span=9, adjust=False).mean()
    ema21 = df_4h["Close"].ewm(span=21, adjust=False).mean()
    delta = df_4h["Close"].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    htf = pd.DataFrame({
        "EMA_9_4H_HTF": ema9,
        "Macro_Trend_HTF": (ema9 > ema21).astype(int),
        "RSI_4H_HTF": 100 - (100 / (1 + gain / loss.replace(0, np.nan))),
    }).shift(1)

    df_1h = d.resample("1h").agg(agg).dropna()
    delta1 = df_1h["Close"].diff()
    gain1 = delta1.where(delta1 > 0, 0).rolling(14).mean()
    loss1 = (-delta1.where(delta1 < 0, 0)).rolling(14).mean()
    mtf = pd.DataFrame({
        "RSI_1H_MTF": 100 - (100 / (1 + gain1 / loss1.replace(0, np.nan))),
    }).shift(1)

    d = d.join(htf, how="left").join(mtf, how="left").ffill()
    d["Dist_to_Macro_Trend"] = (d["Close"] - d["EMA_9_4H_HTF"]) / d["EMA_9_4H_HTF"].replace(0, np.nan)
    return d.reset_index()


def build_features(df_raw: pd.DataFrame, symbol: str, mtf: bool = False) -> pd.DataFrame:
    if df_raw is None or len(df_raw) == 0:
        return pd.DataFrame()
    df = _build_base(df_raw.copy())
    df = _build_advanced(df)
    df = _add_lags(df)
    df = _build_setup_flags(df)
    if mtf:
        df = add_mtf(df)
    df["symbol"] = symbol
    df = df.replace([np.inf, -np.inf], np.nan)
    return df.dropna().reset_index(drop=True)


def feature_columns(df: pd.DataFrame) -> list[str]:
    """Model inputs: everything numeric that is not raw price, bookkeeping, or a label.

    Guards against the notebook's re-run bug, where a future-return column injected by a
    later cell silently became a feature: anything whose name marks it as forward-looking
    is refused outright.
    """
    banned_markers = ("forward", "future", "target", "label", "_tb", "ret_long", "ret_short")
    cols = []
    for c in df.columns:
        if c in NON_FEATURE_COLS or c in NON_STATIONARY_LEVELS or c.startswith("Asset_"):
            continue
        # Every MACD column (MACD, MACD_Signal, MACD_Gap, MACD_Hist and their lags) is a
        # price-scaled level, non-comparable across symbols. Drop them all.
        if c.startswith("MACD"):
            continue
        if any(m in c.lower() for m in banned_markers):
            continue
        if not pd.api.types.is_numeric_dtype(df[c]):
            continue
        cols.append(c)
    return cols
