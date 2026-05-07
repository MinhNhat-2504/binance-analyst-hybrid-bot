import os
import json
import time
import math
import threading
from pathlib import Path
from collections import deque
import schedule
import concurrent.futures
import joblib
from tensorflow.keras.models import load_model
import xgboost as xgb
from sklearn.metrics import log_loss
import shap

import feedparser
import numpy as np
import pandas as pd
from binance.client import Client
from dotenv import load_dotenv
from sklearn.linear_model import LogisticRegression
import copy
import warnings
from pandas.errors import PerformanceWarning
warnings.filterwarnings("ignore", category=PerformanceWarning)
warnings.filterwarnings("ignore", message="DataFrame is highly fragmented.")
# ==========================================

load_dotenv()
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")

if not api_key or not api_secret:
    raise ValueError("🚨 LỖI BẢO MẬT: KHÔNG TÌM THẤY API KEY TRONG BIẾN MÔI TRƯỜNG! Vui lòng kiểm tra lại file .env")

if "client" not in globals():
    client = Client(api_key, api_secret)
    print("🔒 [AN NINH MẠNG] Kết nối API thành công. Chìa khóa đã được mã hóa an toàn.")

TARGET_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "NEARUSDT", "ADAUSDT"
]
LONG_PROBA_THRESHOLD = 0.55
SHORT_PROBA_THRESHOLD = 0.55
TOTAL_PORTFOLIO_USDT = globals().get("TOTAL_PORTFOLIO_USDT", 1000.0)

COIN_PARAMS = {
    "BTCUSDT": {"STOP_LOSS_PCT": 0.015, "TAKE_PROFIT_PCT": 0.045, "TRAILING_PCT": 0.012},
    "ETHUSDT": {"STOP_LOSS_PCT": 0.020, "TAKE_PROFIT_PCT": 0.050, "TRAILING_PCT": 0.015},
    "SOLUSDT": {"STOP_LOSS_PCT": 0.035, "TAKE_PROFIT_PCT": 0.090, "TRAILING_PCT": 0.025},
    "BNBUSDT": {"STOP_LOSS_PCT": 0.018, "TAKE_PROFIT_PCT": 0.045, "TRAILING_PCT": 0.015},
    "XRPUSDT": {"STOP_LOSS_PCT": 0.025, "TAKE_PROFIT_PCT": 0.070, "TRAILING_PCT": 0.020},

    "DOGEUSDT": {"STOP_LOSS_PCT": 0.035, "TAKE_PROFIT_PCT": 0.090, "TRAILING_PCT": 0.025}, # Meme coin giật mạnh như SOL
    "AVAXUSDT": {"STOP_LOSS_PCT": 0.030, "TAKE_PROFIT_PCT": 0.080, "TRAILING_PCT": 0.020}, # Altcoin Layer 1 giật khá
    "LINKUSDT": {"STOP_LOSS_PCT": 0.025, "TAKE_PROFIT_PCT": 0.065, "TRAILING_PCT": 0.018}, # Biến động trung bình khá
    "NEARUSDT": {"STOP_LOSS_PCT": 0.035, "TAKE_PROFIT_PCT": 0.085, "TRAILING_PCT": 0.025}, # Altcoin giật mạnh
    "ADAUSDT":  {"STOP_LOSS_PCT": 0.020, "TAKE_PROFIT_PCT": 0.055, "TRAILING_PCT": 0.015}  # Biến động đầm, giống ETH
}


KLINE_COLUMNS = [
    "Open time", "Open", "High", "Low", "Close", "Volume", "Close time",
    "Quote Asset", "Trades", "Taker Buy Base", "Taker Buy Quote", "Ignore"
]

MARKET_REGIME_NAMES = {0: "ĐÌNH TRỆ (SLEEP)", 1: "SIDEWAY (DU KÍCH)", 2: "SIÊU SÓNG (TRENDING)"}
NEWS_CACHE_TTL_SECONDS = 15 * 60
MARKET_CACHE_TTL_SECONDS = 30
ORDER_BOOK_DEPTH = 20
MAX_HEADLINES_PER_FEED = 30
MAX_SIGNAL_PER_CYCLE = 3

NEWS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
]

SYMBOL_NEWS_ALIASES = {
    "BTCUSDT": ["bitcoin", "btc"],
    "ETHUSDT": ["ethereum", "eth"],
    "SOLUSDT": ["solana", "sol"],
    "BNBUSDT": ["bnb", "binance coin", "binance"],
    "XRPUSDT": ["xrp", "ripple"],
}

POSITIVE_SENTIMENT_TERMS = {
    "surge": 1.2, "breakout": 1.0, "rally": 1.0, "bullish": 1.2,
    "approval": 0.8, "adoption": 0.9, "partnership": 0.8, "buy": 0.6,
    "support": 0.6, "inflow": 0.8, "upgrade": 0.6, "launch": 0.5,
    "record high": 1.0, "accumulate": 0.7, "growth": 0.6,
}

NEGATIVE_SENTIMENT_TERMS = {
    "crash": 1.4, "hack": 1.3, "exploit": 1.4, "lawsuit": 1.0,
    "bearish": 1.2, "dump": 1.2, "sell-off": 1.0, "ban": 1.0,
    "liquidation": 1.1, "outflow": 0.8, "recession": 0.7, "fear": 0.8,
    "investigation": 0.8, "delay": 0.6, "rejected": 0.8, "fraud": 1.2,
}

state_lock = threading.Lock()
news_cache = {}
market_data_cache = {}

expert_models = {
    0: {"name": "CHUYÊN GIA SLEEP", "long": None, "short": None}, 
    1: {"name": "CHUYÊN GIA SIDEWAY", "long": None, "short": None},       
    2: {"name": "CHUYÊN GIA TRENDING", "long": None, "short": None}       
}

print("⚙️ Đang khởi tạo Hệ thống Đa Chuyên Gia (Multi-Expert)...")

import joblib
try:
    meta_feature_cols = joblib.load("xgb_v8_meta_features.pkl")
except:
    meta_feature_cols = ["pred_proba", "Market_Regime", "Taker_Imbalance", "Sentiment_Score", "Volatility_24", "Volume_Z", "Trend_Strength"]
try:
    model_long = joblib.load("xgb_v8_long_fee_aware_multi.pkl")
    model_short = joblib.load("xgb_v8_short_fee_aware_multi.pkl")
    meta_v8 = joblib.load("xgb_v8_meta.pkl")
    feature_columns_v8 = meta_v8["feature_columns"]
    print("✅ Đã nạp Sư phụ XGBoost & Từ điển Cột (Feature Schema)!")
except Exception as e:
    print(f"❌ LỖI NẠP SƯ PHỤ: {e}")
try:
    LIVE_META_ARTIFACT = joblib.load("xgb_v8_meta_model.pkl")
    print("✅ Đã load Meta Artifact thế hệ mới (Dual Regressor).")
except Exception as e:
    print(f"⚠️ Lỗi load Meta Model: {e}")
    LIVE_META_ARTIFACT = None
try:
    exit_model_ai = joblib.load("xgb_v8_exit_model.pkl")
    print("✅ Đã nạp AI Chuyên gia Thoát lệnh!")
except FileNotFoundError:
    exit_model_ai = None
    print("ℹ️ Chưa có AI Thoát lệnh. Hệ thống tự động dùng Quantile TP/SL Động (ATR-based) thay thế.")

try:
    dl_model_long = load_model("lstm_seq128_long.keras")
    dl_model_short = load_model("lstm_seq128_short.keras")
    print("🧠 Đã nạp Module Deep Learning (LSTM) phân tích chuỗi thời gian!")
except Exception as e:
    dl_model_long = dl_model_short = None
    print(f"⚠️ Chưa có module Deep Learning, chạy tạm 100% XGBoost: {e}")
DL_SEQUENCE_LENGTH = 128
DL_FEATURES = ["Log_Return", "High_Low_Spread", "Close_Open_Spread", "Volume_Log", "Volatility_24"]

if "STATE_FILE" not in globals():
    STATE_FILE = Path("bot_runtime_state_dual.json")

if "runtime_state" not in globals():
    runtime_state = {"symbols": {}}

if "bot_memory" not in globals():
    bot_memory = runtime_state.setdefault("symbols", {})
else:
    runtime_state.setdefault("symbols", bot_memory)

try:
    meta_v8 = joblib.load("xgb_v8_meta.pkl")
    feature_columns_v8 = meta_v8["feature_columns"]
    LAG_STEPS_V8 = meta_v8.get("lag_steps", [1, 2, 3, 6, 12])
except Exception as e:
    ghi_log(f"⚠️ Lỗi nạp Meta Data (xgb_v8_meta.pkl): {e}")
    feature_columns_v8 = [] 
try:
    meta_model_ai = joblib.load("xgb_v8_meta_model.pkl")
    print("🛡️ Đã nạp AI Vệ Sĩ Meta-Model (Lọc Nhiễu) thành công!")
except Exception as e:
    meta_model_ai = None
    print(f"⚠️ Chưa có AI Vệ Sĩ Meta-Model: {e}")
try:
    gating_network_ai = joblib.load("xgb_v8_gating_network.pkl")
    gating_features = joblib.load("xgb_v8_gating_features.pkl")
    print("⚖️ Đã nạp Gating Network (Soft MoE Router)!")
except:
    gating_network_ai = None
try:
    ensemble_weights = joblib.load("ensemble_weights_v8.pkl")
    print("✅ Đã nạp Trọng số Ensemble OOS (Multi-Regime) thành công!")
except Exception as e:
    print(f"⚠️ Không tìm thấy ensemble_weights_v8.pkl. Dùng cấu hình 50/50. Lỗi: {e}")
    ensemble_weights = {"LONG": {}, "SHORT": {}}
# 3. TẢI HỢP ĐỒNG BỘ NHỚ (FEATURE METADATA)
try:
    LIVE_META_CONFIG = joblib.load("xgb_v8_meta.pkl")
    LIVE_FEATURE_COLS = LIVE_META_CONFIG["feature_columns"]
    LIVE_META_FEATURES = joblib.load("xgb_v8_meta_features.pkl")
    print(f"✅ Đã load Artifact Meta: {len(LIVE_FEATURE_COLS)} features Tầng 1 | {len(LIVE_META_FEATURES)} features Tầng 2.")
except Exception as e:
    print(f"❌ LỖI CHÍNH MẠNG: KHÔNG THỂ NẠP HỢP ĐỒNG TÍNH NĂNG! {e}")
    LIVE_FEATURE_COLS = []
    LIVE_META_FEATURES = []
try:
    with open("model_calibrations.json", "r") as f:
        LIVE_CALIBRATIONS = json.load(f)
    print("✅ Đã load Artifact: model_calibrations.json")
except Exception as e:
    print(f"⚠️ Lỗi load Calibrations: {e}. Bot sẽ chuyển về chế độ phòng thủ tối đa!")
    LIVE_CALIBRATIONS = {}

def is_regime_profitable(direction, regime_val):
    """Cầu dao Risk Manager: Kiểm tra PF ròng của Sư phụ Tầng 1"""
    if not LIVE_CALIBRATIONS: return False 
    
    regime_str = str(int(regime_val)) if pd.notna(regime_val) else "base"
    if regime_str not in LIVE_CALIBRATIONS:
        regime_str = "base"
        
    pf_score = LIVE_CALIBRATIONS[regime_str][direction.upper()].get("PF", 0.0)
    
    # CHỈ MỞ KHÓA NẾU PF RÒNG > 1.05
    return pf_score > 1.05

if "ghi_log" not in globals():
    def ghi_log(thong_bao):
        print(thong_bao)

if "send_ban_signal" not in globals():
    def send_ban_signal(message, target="standard"):
        print(f"[{target}] {message}")

if "get_sac_portfolio_allocations" not in globals():
    def get_sac_portfolio_allocations(list_active_signals, total_portfolio_usdt=1000.0):
        return {}

if "get_dynamic_budget_kelly" not in globals():
    def get_dynamic_budget_kelly(probability, tp_pct, sl_pct, total_capital=TOTAL_PORTFOLIO_USDT):
        budget = max(10.0, min(total_capital * 0.05, total_capital))
        return budget, "TIÊU CHUẨN"

if "apply_cmc_fusion" not in globals():
    def apply_cmc_fusion(raw_proba, asset_sentiment, trend_str, vol_regime, action_type):
        fused_proba = raw_proba
        if action_type == "OPEN_LONG":
            if asset_sentiment > 0.2: fused_proba += 0.05
            if trend_str > 0: fused_proba += 0.03
        elif action_type == "OPEN_SHORT":
            if asset_sentiment < -0.2: fused_proba += 0.05
            if trend_str < 0: fused_proba += 0.03
        return max(0.0, min(1.0, fused_proba))

def apply_triple_barrier(df, pt_multiplier=1.0, sl_multiplier=1.0, t1_bars=24):
    # Tính biến động (Volatility) làm ngưỡng động cho rào cản
    df['volatility'] = df['Close'].pct_change().rolling(window=100).std()
    
    # Danh sách kết quả: 1 (TP), -1 (SL), 0 (Timeout)
    labels = []
    
    for i in range(len(df) - t1_bars):
        price_start = df['Close'].iloc[i]
        vol = df['volatility'].iloc[i]
        
        # Ngưỡng động dựa trên Volatility
        upper_barrier = price_start * (1 + vol * pt_multiplier)
        lower_barrier = price_start * (1 - vol * sl_multiplier)
        
        # Kiểm tra nến nào chạm rào cản trước
        found_barrier = False
        for j in range(1, t1_bars + 1):
            price_current = df['Close'].iloc[i + j]
            
            if price_current >= upper_barrier:
                labels.append(1) # Hit TP
                found_barrier = True
                break
            elif price_current <= lower_barrier:
                labels.append(-1) # Hit SL
                found_barrier = True
                break
                
        if not found_barrier:
            labels.append(0) # Timeout (không chạm rào nào)
            
    # Pad kết quả cho đủ độ dài dataframe
    return labels + [np.nan] * t1_bars

# =====================================================================
# 🏭 NHÀ MÁY SẢN XUẤT ĐẶC TRƯNG (ĐÃ VÁ 5 LỖI THIẾU CỘT)
# =====================================================================
class QuantFeatureEngineer:
    @staticmethod
    def _build_base(df):
        df = df.copy()
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
            
        if "Open time" in df.columns:
            if not pd.api.types.is_datetime64_any_dtype(df["Open time"]):
                sample = df["Open time"].dropna().iloc[0]
                if isinstance(sample, (int, float, np.integer, np.floating)):
                    df["Open time"] = pd.to_datetime(df["Open time"], unit="ms", utc=True).dt.tz_localize(None)
                else:
                    df["Open time"] = pd.to_datetime(df["Open time"], utc=True).dt.tz_localize(None)
            
            # [VÁ LỖI 1 & 2]: Trả lại Hour và DayOfWeek
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
        
        # [VÁ LỖI 3]: Trả lại MACD_Hist
        df["MACD_Hist"] = df["MACD"] - df["MACD_Signal"]
        
        df["BB_Mid"] = df["Close"].rolling(window=20).mean()
        df["BB_Std"] = df["Close"].rolling(window=20).std()
        df["BB_Upper"] = df["BB_Mid"] + (df["BB_Std"] * 2)
        df["BB_Lower"] = df["BB_Mid"] - (df["BB_Std"] * 2)
        return df

    @staticmethod
    def _build_advanced(df):
        df["Log_Return_1"] = np.log(df["Close"]).diff()
        for i in [3, 6, 12, 24]: df[f"Return_{i}"] = df["Close"].pct_change(i)
        df["Range_Pct"] = (df["High"] - df["Low"]) / df["Close"].replace(0, np.nan)
        df["Body_Pct"] = (df["Close"] - df["Open"]) / df["Open"].replace(0, np.nan)
        df["Volume_Change"] = df["Volume"].pct_change().replace([np.inf, -np.inf], np.nan)
        df["BB_Width"] = (df["BB_Upper"] - df["BB_Lower"]) / df["BB_Mid"].replace(0, np.nan)
        volume_mean_24 = df["Volume"].rolling(24).mean()
        volume_std_24 = df["Volume"].rolling(24).std().replace(0, np.nan)
        df["Volume_Z"] = (df["Volume"] - volume_mean_24) / volume_std_24
        df["Volume_Regime"] = df["Volume"] / df["Volume"].rolling(72).mean().replace(0, np.nan) - 1.0
        
        # 🐋 ĐỊNH VỊ BẢN ĐỒ THANH KHOẢN CÁ MẬP (ORDER BLOCKS)
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
        true_range = pd.concat([df["High"] - df["Low"], (df["High"] - prev_close).abs(), (df["Low"] - prev_close).abs()], axis=1).max(axis=1)
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

        # 🔬 MICROSTRUCTURE: TAKER IMBALANCE & ORDER FLOW
        df["Taker_Buy_Vol"] = pd.to_numeric(df["Taker Buy Base"], errors="coerce").fillna(0)
        df["Taker_Sell_Vol"] = df["Volume"] - df["Taker_Buy_Vol"]
        df["Taker_Buy_Ratio"] = df["Taker_Buy_Vol"] / df["Volume"].replace(0, np.nan)
        df["Taker_Imbalance"] = (df["Taker_Buy_Vol"] - df["Taker_Sell_Vol"]) / df["Volume"].replace(0, np.nan)
        df["Taker_Imbalance_Delta"] = df["Taker_Imbalance"].diff()
        vol_percentile_75 = df["Volume"].rolling(72).quantile(0.75)
        df["Is_High_Vol_Bucket"] = (df["Volume"] > vol_percentile_75).astype(int)
        df["Smart_Money_Flow"] = df["Taker_Imbalance"] * df["Is_High_Vol_Bucket"]
        return df

    @staticmethod
    def _add_lags(df):
        lag_steps = globals().get("LAG_STEPS_V8", [1, 2, 3, 6, 12])
        lag_cols = ["Log_Return_1", "Return_3", "Return_6", "Return_12", "Return_24", "EMA_14_Dist", "EMA_50_Dist", "Trend_Strength", "RSI_14_Norm", "MACD_Gap", "ATR_Pct", "Volume_Z", "Volume_Regime", "Volatility_24", "Volatility_Regime", "Breakout_20", "Breakdown_20"]
        for col in lag_cols:
            if col in df.columns:
                for lag in lag_steps: df[f"{col}_lag_{lag}"] = df[col].shift(lag)
        return df

    @staticmethod
    def _build_gatekeeper_filters(df):
        df["Setup_Trend_Primary_L"] = ((df["EMA_14"] > df["EMA_50"]) & (df["Trend_Strength"] > -0.002)).astype(int)
        df["Setup_MACD_OK_L"] = (df["MACD_Gap"] > -0.0005).astype(int)
        df["Setup_RSI_OK_L"] = ((df["RSI_14_Norm"] >= 0.44) & (df["RSI_14_Norm"] <= 0.74)).astype(int)
        df["Setup_ATR_OK"] = (df["ATR_Pct"] <= 0.03).astype(int) 
        df["Setup_Vol_OK"] = (df["Volatility_Regime"] <= 1.9).astype(int) 
        df["Setup_Volume_OK"] = (df["Volume_Regime"] > -0.40).astype(int) 
        df["Setup_Breakout_OK_L"] = (df["Breakout_20"] > -0.03).astype(int)
        df["Setup_Slope_OK_L"] = ((df["EMA_14_Slope_3"] > -0.002) & (df["EMA_50_Slope_6"] > -0.003)).astype(int)
        
        df["Setup_Long_Score"] = df[["Setup_Trend_Primary_L", "Setup_MACD_OK_L", "Setup_RSI_OK_L", "Setup_ATR_OK", "Setup_Vol_OK", "Setup_Volume_OK", "Setup_Breakout_OK_L", "Setup_Slope_OK_L"]].sum(axis=1)
        df["setup_long_candidate"] = ((df["Setup_Long_Score"] >= 5) & (df["Setup_Trend_Primary_L"] == 1) & (df["Setup_ATR_OK"] == 1))
        
        df["Setup_Trend_Primary_S"] = ((df["EMA_14"] < df["EMA_50"]) & (df["Trend_Strength"] < 0.002)).astype(int)
        df["Setup_MACD_OK_S"] = (df["MACD_Gap"] < 0.0005).astype(int)
        df["Setup_RSI_OK_S"] = ((df["RSI_14_Norm"] <= 0.56) & (df["RSI_14_Norm"] >= 0.26)).astype(int)
        df["Setup_Breakdown_OK_S"] = (df["Breakdown_20"] < 0.03).astype(int)
        df["Setup_Slope_OK_S"] = ((df["EMA_14_Slope_3"] < 0.002) & (df["EMA_50_Slope_6"] < 0.003)).astype(int)
        
        df["Setup_Short_Score"] = df[["Setup_Trend_Primary_S", "Setup_MACD_OK_S", "Setup_RSI_OK_S", "Setup_ATR_OK", "Setup_Vol_OK", "Setup_Volume_OK", "Setup_Breakdown_OK_S", "Setup_Slope_OK_S"]].sum(axis=1)
        df["setup_short_candidate"] = ((df["Setup_Short_Score"] >= 5) & (df["Setup_Trend_Primary_S"] == 1) & (df["Setup_ATR_OK"] == 1))
        return df

    @classmethod
    def run_pipeline(cls, df_raw, asset_sentiment=0.0, symbol="BTCUSDT"): # Thêm tham số symbol
        if df_raw is None or len(df_raw) == 0: return pd.DataFrame()
        
        df = df_raw.copy()
        if "Sentiment_Score" not in df.columns: df["Sentiment_Score"] = asset_sentiment
        else: df["Sentiment_Score"] = df["Sentiment_Score"].fillna(asset_sentiment)
        
        df = cls._build_base(df)
        df = cls._build_advanced(df)
        df = cls._add_lags(df)
        df = cls._build_gatekeeper_filters(df)
        
        # ===============================================================
        # 🧬 ĐÓNG DẤU DNA TÀI SẢN (ASSET IDENTIFIER - ONE HOT ENCODING)
        # ===============================================================
        TARGET_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", 
                          "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "NEARUSDT", "ADAUSDT"]
        for target_sym in TARGET_SYMBOLS:
            # Tạo các cột Asset_BTCUSDT, Asset_ETHUSDT... gán = 1 nếu đúng coin đang chạy
            df[f"Asset_{target_sym}"] = (1 if symbol == target_sym else 0)
        
        return df.replace([np.inf, -np.inf], np.nan).dropna().reset_index(drop=True)

def prepare_live_feature_frame_dual(df, asset_sentiment=0.0, symbol="BTCUSDT"):
    return QuantFeatureEngineer.run_pipeline(df, asset_sentiment, symbol)

def evaluate_trading_performance(preds_proba, df_val, action_type):
    try:
        threshold = 0.55
        trades = []
        returns = df_val["Future_Return"].values
        for i in range(len(preds_proba)):
            if preds_proba[i] >= threshold:
                pnl = returns[i] if action_type == "LONG" else -returns[i]
                trades.append(pnl)
        if not trades: 
            return {"pnl": -1.0, "profit_factor": 0.0, "max_drawdown": 1.0}
        total_pnl = sum(trades)
        wins = [t for t in trades if t > 0]
        losses = [abs(t) for t in trades if t <= 0]
        total_win = sum(wins)
        total_loss = sum(losses)
        if total_loss > 0:
            profit_factor = total_win / total_loss
        else:
            profit_factor = 99.0 if total_win > 0 else 0.0
        cum_pnl = np.cumsum(trades)
        drawdown = np.maximum.accumulate(cum_pnl) - cum_pnl
        max_dd = np.max(drawdown) if len(drawdown) > 0 else 0.0
        return {
            "pnl": total_pnl, 
            "profit_factor": profit_factor, 
            "max_drawdown": max_dd, 
            "trade_count": len(trades)
        }
    except: 
        return {"pnl": -1.0, "profit_factor": 0.0, "max_drawdown": 1.0}

def train_meta_model(X_train, y_primary_pred, y_actual):
    meta_labels = []
    for pred, actual in zip(y_primary_pred, y_actual):
        if pred == actual and actual != 0:
            meta_labels.append(1) 
        else:
            meta_labels.append(0)       
    meta_model.fit(X_train, meta_labels)
    return meta_labels

def champion_challenger_retrain(df_retrain):
    ghi_log("🧠 Bắt đầu đúc lại Mô hình (Champion vs Challenger)...")
    # 🛡️ 1. GÁN NHÃN TẦNG 1 (TBM HIGH/LOW - CÁCH LY THEO COIN)
    df_retrain = df_retrain.groupby('symbol', group_keys=False).apply(
        lambda x: apply_triple_barrier_high_low(x, pt_multiplier=2.0, sl_multiplier=1.0, time_limit=12)
    )
    df_train_clean = df_retrain[df_retrain['TBM_Label'] != 0].copy()
    df_train_clean["target_long"] = (df_train_clean["TBM_Label"] == 1).astype(int)
    df_train_clean["target_short"] = (df_train_clean["TBM_Label"] == -1).astype(int)

    # 🎯 2. TÍNH TARGET CHO EXIT MODEL (QUANTILE) BẰNG GROUPBY
    df_retrain['Forward_Return_20'] = df_retrain.groupby('symbol')['Close'].transform(lambda x: x.shift(-20) / x - 1)
    df_quant_clean = df_retrain.dropna(subset=['Forward_Return_20']).copy()
    y_quant = np.clip(df_quant_clean['Forward_Return_20'].astype(np.float32), -0.30, 0.30)
    X_quant = df_quant_clean[feature_columns_v8].astype(np.float32)

    # ⚖️ 3. CHUẨN BỊ NHÃN PNL (COST-AWARE) CHO META-MODEL
    FEE_RATE, SLIPPAGE, FUNDING_COST = 0.0004, 0.0005, 6 * 0.00001
    ROUND_TRIP_COST = (FEE_RATE + SLIPPAGE) * 2 + FUNDING_COST
    vol = df_train_clean["Volatility_24"].fillna(0.02)
    MIN_PROFIT_THRESHOLD = 0.0015 
    is_win_l = df_train_clean["target_long"] == 1
    df_train_clean["realized_net_return_long"] = np.where(is_win_l, (vol * 2.0) - ROUND_TRIP_COST, (-vol * 1.0) - ROUND_TRIP_COST)
    df_train_clean["meta_target_long"] = (df_train_clean["realized_net_return_long"] > MIN_PROFIT_THRESHOLD).astype(int)
    is_win_s = df_train_clean["target_short"] == 1
    df_train_clean["realized_net_return_short"] = np.where(is_win_s, (vol * 2.0) - ROUND_TRIP_COST, (-vol * 1.0) - ROUND_TRIP_COST)
    df_train_clean["meta_target_short"] = (df_train_clean["realized_net_return_short"] > MIN_PROFIT_THRESHOLD).astype(int)

    # 🚀 4. TIẾN HÀNH FIT MODEL MỚI (CHALLENGER) VÀ LƯU FILE
    try:
        X_train = df_train_clean[feature_columns_v8].astype(np.float32)
        ghi_log("⏳ Đang đúc Sư Phụ Tầng 1 (LONG/SHORT)...")
        new_long_model = build_xgb_v8_model(df_train_clean["target_long"])
        new_long_model.fit(X_train, df_train_clean["target_long"], verbose=False)
        joblib.dump(new_long_model, "xgb_v8_long_fee_aware_multi.pkl")
        new_short_model = build_xgb_v8_model(df_train_clean["target_short"])
        new_short_model.fit(X_train, df_train_clean["target_short"], verbose=False)
        joblib.dump(new_short_model, "xgb_v8_short_fee_aware_multi.pkl")
        ghi_log("⏳ Đang đúc Vệ Sĩ Tầng 2 (PnL Aware)...")
        meta_features_live = df_train_clean[meta_feature_cols].astype(np.float32)
        y_meta = df_train_clean["meta_target_long"] 
        new_meta_model = build_xgb_v8_model(y_meta)
        new_meta_model.fit(meta_features_live, y_meta, verbose=False)
        joblib.dump(new_meta_model, "xgb_v8_meta_model.pkl")
        ghi_log("⏳ Đang đúc AI Exit Quantile (Khóa ảo giác)...")
        quantile_models = {}
        for q in [0.10, 0.50, 0.90]:
            from xgboost import XGBRegressor
            q_model = XGBRegressor(objective='reg:quantileerror', quantile_alpha=q, n_estimators=100, max_depth=4, learning_rate=0.05, tree_method='hist', random_state=42)
            q_model.fit(X_quant, y_quant, verbose=False)
            quantile_models[f"q_{q}"] = q_model
        joblib.dump(quantile_models, "xgb_v8_exit_model.pkl")
        ghi_log("🎉 BẢO DƯỠNG HOÀN TẤT! Toàn bộ Model V8 đã được cập nhật thành công!")
    except Exception as e:
        ghi_log(f"⚠️ Lỗi trong lúc Fit Model Retrain: {e}")

def _clamp(value, low, high): return max(low, min(high, value))

def apply_cross_sectional_ranking(all_candidates, max_open_positions=3):
    valid_candidates = [c for c in all_candidates if c is not None and c["action"] in ["LONG", "SHORT"]]
    if not valid_candidates:
        return []
    for c in valid_candidates:
        exp_pnl = c.get("expected_pnl", 0.01) 
        c["edge_score"] = c["final_proba"] * exp_pnl
    ranked_candidates = sorted(valid_candidates, key=lambda x: x["edge_score"], reverse=True)
    long_queue = [c for c in ranked_candidates if c["action"] == "LONG"]
    short_queue = [c for c in ranked_candidates if c["action"] == "SHORT"]
    top_k_selected = []
    ghi_log(f"\n{'='*20} 🏆 BẢNG XẾP HẠNG EDGE TOÀN THỊ TRƯỜNG {'='*20}")
    for i, c in enumerate(ranked_candidates):
        medal = "🥇" if i==0 else "🥈" if i==1 else "🥉" if i==2 else "⭐"
        ghi_log(f" {medal} Rank {i+1}: {c['symbol']} [{c['action']}] | Edge Score: {c['edge_score']*10000:.2f} | Proba: {c['final_proba']*100:.1f}% | ExpPnL: {c.get('expected_pnl', 0)*100:.2f}%")
    ghi_log(f"{'='*72}")
    top_k_selected = ranked_candidates[:max_open_positions]
    return top_k_selected

def _safe_float(value, default=0.0):
    try:
        return default if value is None else float(value)
    except: return default

shap.initjs() if 'shap' in globals() else None
def extract_shap_insights(model, df_features, feature_cols):
    try:
        if model is None: return [], []
        X_live = df_features[feature_cols].astype(np.float32).iloc[[-1]]
        explainer = shap.TreeExplainer(model)
        shap_vals = explainer.shap_values(X_live)[0]
        feature_impacts = list(zip(feature_cols, shap_vals))
        feature_impacts.sort(key=lambda x: x[1], reverse=True)
        top_pushers = [(f, v) for f, v in feature_impacts if v > 0][:3]
        top_pullers = [(f, v) for f, v in feature_impacts if v < 0][::-1][:3] 
        return top_pushers, top_pullers
    except Exception as e:
        ghi_log(f"⚠️ Lỗi bóc tách SHAP: {e}")
        return [], []

def _utc_now(): return pd.Timestamp.utcnow()

def _json_default(value):
    if isinstance(value, deque): return list(value)
    if isinstance(value, (np.floating, np.integer)): return value.item()
    if isinstance(value, (pd.Timestamp, np.datetime64)): return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

def _serialize_feature_dict(feature_dict):
    serializable = {}
    for key, value in (feature_dict or {}).items():
        if isinstance(value, (np.floating, np.integer)): serializable[key] = value.item()
        elif isinstance(value, (pd.Timestamp, np.datetime64)): serializable[key] = str(value)
        else: serializable[key] = value
    return serializable

def _symbol_state_defaults():
    return {
        "position_side": "NONE", "entry_price": 0.0, "quantity": 0.0, "invested_usdt": 0.0,
        "peak_price": 0.0, "trough_price": 0.0, "opened_at": None, "entry_features": None,
        "l2_buffer": deque(maxlen=10), "last_trade": None, "last_signal_target": "basic",
        "last_signal_reason": "", "last_prediction_side": "NONE", "last_prediction_proba": 0.0,
        "last_scan_price": 0.0,
    }

def _ensure_symbol_state(symbol):
    memory = bot_memory.setdefault(symbol, {})
    defaults = _symbol_state_defaults()
    for key, default_value in defaults.items():
        if key not in memory:
            memory[key] = deque(default_value, maxlen=10) if isinstance(default_value, deque) else default_value
    if not isinstance(memory.get("l2_buffer"), deque):
        memory["l2_buffer"] = deque(memory.get("l2_buffer", []), maxlen=10)
    return memory

def _save_runtime_state():
    try:
        with state_lock:
            runtime_state["symbols"] = bot_memory
            STATE_FILE.write_text(json.dumps(runtime_state, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    except Exception as exc: ghi_log(f"❌ Lỗi lưu trạng thái: {exc}")

def _get_cached_klines(symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=260, ttl_seconds=MARKET_CACHE_TTL_SECONDS):
    cache_key = (symbol, interval, limit)
    now_ts = time.time()
    cached = market_data_cache.get(cache_key)
    if cached and (now_ts - cached["fetched_at"] <= ttl_seconds):
        return pd.DataFrame(cached["rows"], columns=KLINE_COLUMNS)
    klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    market_data_cache[cache_key] = {"fetched_at": now_ts, "rows": klines}
    return pd.DataFrame(klines, columns=KLINE_COLUMNS)

def _hours_since(timestamp_value):
    if not timestamp_value: return None
    try: return float((_utc_now() - pd.Timestamp(timestamp_value)).total_seconds() / 3600.0)
    except: return None

def _headline_sentiment_score(text):
    text = (text or "").lower()
    raw_score = sum(weight for term, weight in POSITIVE_SENTIMENT_TERMS.items() if term in text)
    raw_score -= sum(weight for term, weight in NEGATIVE_SENTIMENT_TERMS.items() if term in text)
    if "etf" in text and "approval" in text: raw_score += 0.8
    if "sec" in text and any(x in text for x in ["lawsuit", "delay", "reject"]): raw_score -= 0.7
    return math.tanh(raw_score / 2.5)

def get_asset_sentiment(symbol):
    now_ts = time.time()
    cached = news_cache.get(symbol)
    if cached and (now_ts - cached.get("fetched_at", 0) <= NEWS_CACHE_TTL_SECONDS):
        return _safe_float(cached.get("score"), 0.0)

    aliases = SYMBOL_NEWS_ALIASES.get(symbol, [symbol.replace("USDT", "").lower()])
    scored_entries, matched_titles = [], []

    for feed_url in NEWS_FEEDS:
        try:
            parsed_feed = feedparser.parse(feed_url)
            for entry in parsed_feed.entries[:MAX_HEADLINES_PER_FEED]:
                title = getattr(entry, "title", "") or ""
                summary = getattr(entry, "summary", "") or ""
                combined_text = f"{title} {summary}".lower()

                specific_match = any(alias in combined_text for alias in aliases)
                macro_match = any(k in combined_text for k in ["crypto", "market", "bitcoin", "binance", "etf", "fed"])
                if not specific_match and not macro_match: continue

                base_score = _headline_sentiment_score(combined_text)
                if base_score == 0.0 and not specific_match: continue

                recency_weight = 1.0
                published_parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
                if published_parsed:
                    published_ts = time.mktime(published_parsed)
                    age_hours = max(0.0, (now_ts - published_ts) / 3600.0)
                    recency_weight = _clamp(1.2 - (age_hours / 72.0), 0.35, 1.2)

                relevance_weight = 1.35 if specific_match else 0.55
                scored_entries.append(base_score * recency_weight * relevance_weight)
                if title: matched_titles.append(title.strip())
        except: continue

    sentiment_score = _clamp(sum(scored_entries) / len(scored_entries), -1.0, 1.0) if scored_entries else 0.0
    news_cache[symbol] = {"score": sentiment_score, "fetched_at": now_ts, "headline_count": len(scored_entries), "headlines": matched_titles[:5]}
    return sentiment_score

def check_black_swan(df, symbol):
    live_row = df.iloc[-1]
    realized_z = abs(_safe_float(live_row.get("Return"), 0.0)) / max(_safe_float(live_row.get("Volatility_24"), 0.0), 1e-6)
    atr_regime = _safe_float(live_row.get("ATR_Regime"), 1.0)
    vol_regime = _safe_float(live_row.get("Volatility_Regime"), 1.0)
    volume_z = abs(_safe_float(live_row.get("Volume_Z"), 0.0))
    body_pct = abs(_safe_float(live_row.get("Body_Pct"), 0.0))
    range_pct = abs(_safe_float(live_row.get("Range_Pct"), 0.0))
    return_6 = abs(_safe_float(live_row.get("Return_6"), 0.0))

    rule_score = (
        max(realized_z - 2.0, 0.0) * 0.55 + max(atr_regime - 1.8, 0.0) * 0.70 +
        max(vol_regime - 2.0, 0.0) * 0.95 + max(volume_z - 2.5, 0.0) * 0.35 +
        max(range_pct - 0.03, 0.0) * 18.0 + max(body_pct - 0.02, 0.0) * 15.0 +
        max(return_6 - 0.08, 0.0) * 12.0
    )

    ae_score, ae_trigger, dynamic_threshold = None, False, 0.028
    try:
        local_ae_threshold, local_ae_model, local_ae_scaler, local_ae_features = globals().get("ae_threshold"), globals().get("ae_model"), globals().get("ae_scaler"), globals().get("ae_features")
        if local_ae_threshold is not None: dynamic_threshold = max(_safe_float(local_ae_threshold, 0.0) * 1.25, 0.028)
        if local_ae_model and local_ae_scaler and local_ae_features and all(f in df.columns for f in local_ae_features):
            live_ae_features = df[local_ae_features].iloc[-1:].replace([np.inf, -np.inf], np.nan).fillna(0.0).astype(float).values
            live_ae_features_scaled = local_ae_scaler.transform(live_ae_features)
            reconstructed = local_ae_model.predict(live_ae_features_scaled, verbose=0)
            ae_score = float(np.mean(np.power(live_ae_features_scaled - reconstructed, 2), axis=1)[0])
            ae_trigger = ae_score > dynamic_threshold
    except Exception as exc: return False, {"triggered": False, "error": str(exc), "symbol": symbol}

    hard_trigger = realized_z >= 6.0 or return_6 >= 0.10
    triggered = bool(ae_trigger or rule_score >= 2.8 or hard_trigger)
    return triggered, {"triggered": triggered, "ae_score": ae_score, "ae_threshold": dynamic_threshold, "rule_score": round(rule_score, 4), "realized_z": round(realized_z, 4), "vol_regime": round(vol_regime, 4), "symbol": symbol}

def check_derivatives_squeeze(symbol, df_spot):
    try:
        df_local = df_spot.copy()
        for col in ["Close", "High", "Low", "Volume", "Taker Buy Base"]: df_local[col] = pd.to_numeric(df_local[col])
        recent_volume = df_local["Volume"].tail(6).sum()
        aggressive_buy_ratio = df_local["Taker Buy Base"].tail(6).sum() / max(recent_volume, 1e-9)
        price_change_6 = _safe_float(df_local["Close"].pct_change(6).iloc[-1], 0.0)
        range_6 = (df_local["High"].tail(6).max() - df_local["Low"].tail(6).min()) / max(_safe_float(df_local["Close"].iloc[-1], 0.0), 1e-9)

        if aggressive_buy_ratio >= 0.62 and price_change_6 > 0.015 and range_6 > 0.02: return "UP_SQUEEZE", f"Phe mua ép giá (BuyRatio: {aggressive_buy_ratio:.2f})"
        if aggressive_buy_ratio <= 0.38 and price_change_6 < -0.015 and range_6 > 0.02: return "DOWN_SQUEEZE", f"Phe bán xả hàng (BuyRatio: {aggressive_buy_ratio:.2f})"
        return "SAFE", f"Tỷ lệ mua ổn định ({aggressive_buy_ratio:.2f})"
    except Exception as exc: return "SAFE", f"Thiếu dữ liệu Squeeze: {exc}"

def track_champion_performance(memory, current_price, previous_price, symbol):
    try:
        actual_return = (current_price - previous_price) / previous_price
        last_side = memory.get("last_prediction_side", "NONE")
        last_proba = _safe_float(memory.get("last_prediction_proba"), 0.0)
        if last_side not in {"LONG", "SHORT"}: 
            return f"📊 {symbol} Lịch sử: {actual_return*100:+.2f}%"
        correct = (actual_return > 0 and last_side == "LONG") or (actual_return < 0 and last_side == "SHORT")
        status = "🟢 WIN" if correct else "🔴 LOSS"
        streak = memory.get("performance_streak", deque(maxlen=20))
        streak.append(1 if correct else 0)
        memory["performance_streak"] = streak
        win_rate = sum(streak) / len(streak) if len(streak) > 0 else 0.0
        warning = "⚠️ CẢNH BÁO DRIFT" if win_rate < 0.4 and len(streak) >= 10 else "Ổn định"
        return f"🏆 Champion {symbol}: Dự đoán {last_side} ({last_proba*100:.1f}%) | Thực tế: {actual_return*100:+.2f}% | {status} | WinRate(20): {win_rate*100:.0f}% ({warning})"
    except Exception as exc: 
        return f"⚠️ Lỗi Telemetry {symbol}: {exc}"

def detect_market_regime(df):
    hmm_model_local = globals().get("hmm_model", None)
    if hmm_model_local:
        try:
            features = df[["Return", "Volatility_24"]].dropna().values
            if len(features) >= 5:
                regime = int(hmm_model_local.predict(features)[-1])
                return regime if regime in MARKET_REGIME_NAMES else int(_clamp(regime, 0, 2))
        except: pass
    live_row = df.iloc[-1]
    trend_strength, vol_regime = abs(_safe_float(live_row.get("Trend_Strength"), 0.0)), _safe_float(live_row.get("Volatility_Regime"), 1.0)
    if trend_strength < 0.002 and vol_regime <= 0.9: return 0
    if trend_strength >= 0.006 or vol_regime >= 1.6: return 2
    return 1

def check_causal_validity(symbol, df_features, action):
    if symbol == "BTCUSDT": return True, "Tài sản dẫn dắt (Leader)"
    try:
        btc_raw = _get_cached_klines("BTCUSDT", interval=Client.KLINE_INTERVAL_1HOUR, limit=120)
        btc_close = pd.to_numeric(btc_raw["Close"], errors="coerce")
        btc_returns_series = btc_close.pct_change()
        asset_returns_series = pd.to_numeric(df_features["Return"], errors="coerce")
        comparison = pd.DataFrame({
            "asset": asset_returns_series.tail(72).reset_index(drop=True), 
            "btc": btc_returns_series.tail(72).reset_index(drop=True)
        }).dropna()
        if len(comparison) < 24: return True, "Dữ liệu so sánh mỏng"    
        asset_returns, btc_returns = comparison["asset"].values, comparison["btc"].values
        btc_variance = np.var(btc_returns)
        if btc_variance < 1e-8: return True, "BTC đi ngang"
        beta = float(np.cov(asset_returns, btc_returns)[0, 1] / btc_variance)
        corr = float(np.corrcoef(asset_returns, btc_returns)[0, 1])
        residuals = asset_returns - (beta * btc_returns)
        residual_trend = float(np.nanmean(residuals[-6:]))
        if action == "LONG" and corr > 0.70 and beta > 0.60 and residual_trend < -0.0015: 
            return False, f"Yếu hơn BTC (Beta={beta:.2f}, Corr={corr:.2f})"
        if action == "SHORT" and corr > 0.70 and beta > 0.60 and residual_trend > 0.0015: 
            return False, f"Mạnh hơn BTC (Beta={beta:.2f}, Corr={corr:.2f})"
        return True, f"Hợp lệ (Beta={beta:.2f}, Corr={corr:.2f}, Nhiễu={residual_trend:+.4f})"
    except Exception as exc: 
        return True, f"Lỗi Causal: {exc}"

def analyze_order_book(symbol, memory, depth=50): 
    metrics = {
        "ofi": 0.0, "spread_bps": 999.0, "best_bid": 0.0, "best_ask": 0.0, 
        "mid_price": 0.0, "bid_ask_ratio": 1.0, "wall_ratio": 0.0, 
        "sweep_risk": 0.0, "cancel_rate": 0.0
    }
    try:
        order_book = client.get_order_book(symbol=symbol, limit=depth)
        bids = [(float(p), float(s)) for p, s in order_book.get("bids", [])[:depth]]
        asks = [(float(p), float(s)) for p, s in order_book.get("asks", [])[:depth]]
        if not bids or not asks: return metrics
        best_bid, best_ask = bids[0][0], asks[0][0]
        mid_price = (best_bid + best_ask) / 2.0
        bid_vol_total = sum(s for _, s in bids)
        ask_vol_total = sum(s for _, s in asks)
        raw_imbalance = (bid_vol_total - ask_vol_total) / max(bid_vol_total + ask_vol_total, 1e-9)
        weighted_bid = sum(s / (i + 1) for i, (_, s) in enumerate(bids[:10]))
        weighted_ask = sum(s / (i + 1) for i, (_, s) in enumerate(asks[:10]))
        ofi = _clamp((raw_imbalance * 0.4) + (((weighted_bid - weighted_ask) / max(weighted_bid + weighted_ask, 1e-9)) * 0.6), -1.0, 1.0)
        top5_bid_vol = sum(s for _, s in bids[:5])
        top5_ask_vol = sum(s for _, s in asks[:5])
        sweep_risk = max(0.0, 1.0 - (top5_bid_vol + top5_ask_vol) / max((bid_vol_total + ask_vol_total) * 0.2, 1e-9))
        all_sizes = [s for _, s in bids + asks]
        spread_bps = ((best_ask - best_bid) / max(mid_price, 1e-9)) * 10000.0
        wall_ratio = max(max(s for _, s in bids), max(s for _, s in asks)) / max(float(np.mean(all_sizes)) if all_sizes else 0.0, 1e-9)
        metrics.update({
            "ofi": ofi, "spread_bps": spread_bps, "best_bid": best_bid, 
            "best_ask": best_ask, "mid_price": mid_price, 
            "bid_ask_ratio": bid_vol_total / max(ask_vol_total, 1e-9), 
            "wall_ratio": wall_ratio, "sweep_risk": sweep_risk
        })
        cancel_rate = 0.0
        if len(memory["l2_buffer"]) > 0:
            last_snap = memory["l2_buffer"][-1]
            last_bid_vol = last_snap.get("bid_vol_total", bid_vol_total)
            last_ask_vol = last_snap.get("ask_vol_total", ask_vol_total)
            vol_drop = (max(0, last_bid_vol - bid_vol_total) + max(0, last_ask_vol - ask_vol_total))
            cancel_rate = min(vol_drop / max(last_bid_vol + last_ask_vol, 1e-9), 1.0)
            metrics["cancel_rate"] = cancel_rate
        memory["l2_buffer"].append({
            "ts": time.time(), "mid": mid_price, "spread_bps": spread_bps, 
            "ofi": ofi, "best_bid_vol": bids[0][1], "best_ask_vol": asks[0][1], 
            "bid_vol_total": bid_vol_total, "ask_vol_total": ask_vol_total,
            "wall_ratio": wall_ratio
        }) 
        return metrics
    except Exception as e:
        print(f"Lỗi OrderBook: {e}")
        return metrics

def detect_spoofing_hawkes(symbol, memory, limit=50):
    snapshots = [s for s in memory.get("l2_buffer", []) if isinstance(s, dict)]
    if len(snapshots) < 3: return "CLEAN", 0.0, "Chờ thêm dữ liệu L2"
    bid_sizes, ask_sizes, mids, wall_ratios = (np.array([_safe_float(s.get(k), 0.0) for s in snapshots]) for k in ["best_bid_vol", "best_ask_vol", "mid", "wall_ratio"])
    bid_jump = np.max(np.abs(np.diff(bid_sizes))) / max(np.mean(bid_sizes), 1e-9)
    ask_jump = np.max(np.abs(np.diff(ask_sizes))) / max(np.mean(ask_sizes), 1e-9)
    raw_score = (max(bid_jump, ask_jump) * 0.15) + max(np.max(wall_ratios) - 5.0, 0.0) * 0.10 + max(0.00035 - (np.mean(np.abs(np.diff(mids))) / max(np.mean(mids), 1e-9)), 0.0) * 300.0
    score = _clamp(raw_score, 0.0, 1.0)
    if score >= 0.88:
        side = "ASK (BÁN)" if ask_jump > bid_jump else "BID (MUA)"
        return f"SPOOF_RISK_{side[:3]}", score, f"Rung lắc tường {side} (Điểm={score:.2f})"
    return "CLEAN", score, "Sổ lệnh ổn định"

def calculate_trade_expectancy(win_probability, take_profit_pct, stop_loss_pct):
    adj_win_prob = max(0.0, win_probability - 0.05) 
    loss_prob = 1.0 - adj_win_prob
    expected_profit = adj_win_prob * take_profit_pct
    expected_loss = loss_prob * stop_loss_pct
    expectancy_score = expected_profit - expected_loss
    rr_ratio = take_profit_pct / max(stop_loss_pct, 1e-9)
    if expectancy_score <= 0.0005 or rr_ratio < 1.2:
        return False, expectancy_score, rr_ratio
    return True, expectancy_score, rr_ratio

def fetch_derivatives_and_alt_data(symbol, current_sentiment_score, memory=None):
    sentiment_dir = np.sign(current_sentiment_score) 
    sentiment_sev = abs(current_sentiment_score)    
    funding_rate, ls_ratio, oi_change_24h = 0.0, 1.0, 0.0 
    liq_risk = "SAFE"
    try:
        mark_info = client.futures_mark_price(symbol=symbol)
        if isinstance(mark_info, dict) and 'lastFundingRate' in mark_info:
            funding_rate = float(mark_info.get('lastFundingRate', 0.0))
        ls_data = client.futures_global_longshort_ratio(symbol=symbol, period="5m", limit=1)
        if ls_data:
            ls_ratio = float(ls_data[0]['longShortRatio'])
        oi_data = client.futures_open_interest(symbol=symbol)
        current_oi = float(oi_data['openInterest'])
        if memory is not None:
            last_oi = memory.get("last_oi", current_oi)
            oi_change_24h = (current_oi - last_oi) / max(last_oi, 1e-9)
            memory["last_oi"] = current_oi
    except Exception as e:
        ghi_log(f"⚠️ Lỗi API Phái sinh {symbol}: {e}")
    liq_note = f"Fund: {funding_rate*100:.3f}% | L/S: {ls_ratio:.2f} | ΔOI: {oi_change_24h*100:.2f}%"
    if funding_rate >= 0.0008 and ls_ratio > 2.0:
        liq_risk = "LONG_SQUEEZE"
        liq_note += " (🚨 Báo động đỏ: Đám đông Long quá tải)"
    elif funding_rate <= -0.0008 and ls_ratio < 0.5:
        liq_risk = "SHORT_SQUEEZE"
        liq_note += " (🚨 Báo động đỏ: Đám đông Short quá tải)"
    return sentiment_dir, sentiment_sev, funding_rate, oi_change_24h, liq_risk, liq_note

def apply_meta_labeling(symbol, action, live_row, ob_metrics, sentiment_dir, sentiment_sev, liq_risk, market_regime, meta_prob=1.0):
    if meta_prob < 0.50:
        return False, f"❌ AI Vệ Sĩ từ chối (Độ tin cậy của Setup chỉ đạt {meta_prob*100:.1f}%)"
    ofi = _safe_float(ob_metrics.get("ofi"), 0.0)
    sweep_risk = _safe_float(ob_metrics.get("sweep_risk"), 0.0)
    if action == "LONG" and liq_risk == "LONG_SQUEEZE":
        return False, "❌ Đám đông đang đu đỉnh (Rủi ro Long Squeeze)"
    if action == "SHORT" and liq_risk == "SHORT_SQUEEZE":
        return False, "❌ Đám đông đang bán đáy (Rủi ro Short Squeeze)"
    if sweep_risk > 0.6: 
        return False, f"Rủi ro bị Cá mập Sweep sổ lệnh ({sweep_risk*100:.1f}%)"
    if action == "LONG" and ofi < -0.65:
        return False, f"Dòng lệnh (OFI) xả quá mạnh ({ofi:.2f})"
    if action == "SHORT" and ofi > 0.65:
        return False, f"Dòng lệnh (OFI) gom quá mạnh ({ofi:.2f})"
    return True, f"AI Vệ Sĩ Duyệt ({meta_prob*100:.1f}%) + Cầu dao L2 Sạch"
CALIB_FILE = "model_calibrations.json"

def load_calibrations():
    try:
        with open(CALIB_FILE, "r") as f: return json.load(f)
    except:
        return {}

def save_calibrations(calibs):
    try:
        with open(CALIB_FILE, "w") as f: json.dump(calibs, f)
    except Exception as e:
        ghi_log(f"⚠️ Lỗi lưu Calibration: {e}")

model_calibrations = load_calibrations()

def fit_platt_scaling(y_true, y_pred_proba):
    eps = 1e-7
    y_pred_proba = np.clip(y_pred_proba, eps, 1 - eps)
    log_odds = np.log(y_pred_proba / (1 - y_pred_proba)).reshape(-1, 1)
    lr = LogisticRegression(solver='lbfgs', C=1.0)
    try:
        lr.fit(log_odds, y_true)
        return float(lr.coef_[0][0]), float(lr.intercept_[0])
    except:
        return 1.0, 0.0 

def calibrate_xgboost_probability(raw_prob, A=1.0, B=0.0):
    if raw_prob <= 0.0 or raw_prob >= 1.0: return raw_prob
    log_odds = np.log(raw_prob / (1 - raw_prob))
    calibrated_log_odds = A * log_odds + B
    calibrated_prob = 1 / (1 + np.exp(-calibrated_log_odds))
    return float(calibrated_prob)

def analyze_multi_timeframe(symbol):
    try:
        df_4h = _get_cached_klines(symbol, Client.KLINE_INTERVAL_4HOUR, limit=60)
        close_4h = pd.to_numeric(df_4h["Close"])
        ema20 = close_4h.ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = close_4h.ewm(span=50, adjust=False).mean().iloc[-1]
        price_4h = close_4h.iloc[-1] 
        if ema20 > ema50 and price_4h > ema50: bias_4h = "BULLISH"
        elif ema20 < ema50 and price_4h < ema50: bias_4h = "BEARISH"
        else: bias_4h = "NEUTRAL"
        df_15m = _get_cached_klines(symbol, Client.KLINE_INTERVAL_15MINUTE, limit=60)
        close_15m = pd.to_numeric(df_15m["Close"])
        delta = close_15m.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi_15m = 100 - (100 / (1 + rs)).iloc[-1]
        return bias_4h, rsi_15m
    except Exception as e:
        ghi_log(f"Lỗi MTF {symbol}: {e}")
        return "NEUTRAL", 50.0

# 2. TẢI HỢP ĐỒNG TRỌNG SỐ (ENSEMBLE WEIGHTS)
try:
    LIVE_WEIGHTS = joblib.load("ensemble_weights_v8.pkl")
    print("✅ Đã load Artifact: ensemble_weights_v8.pkl")
except Exception as e:
    print(f"⚠️ Lỗi load Weights: {e}.")
    LIVE_WEIGHTS = {}

def get_live_ensemble_weights(direction, regime_val):
    """Lấy trọng số ghép cặp XGB/DL chính xác theo từng Regime"""
    if not LIVE_WEIGHTS: 
        return {"xgb": 1.0, "dl": 0.0} 
    regime_str = str(int(regime_val)) if pd.notna(regime_val) else "base"
    if regime_str not in LIVE_WEIGHTS[direction.upper()]:
        regime_str = "base"
    return LIVE_WEIGHTS[direction.upper()][regime_str]

def apply_mtf_gatekeeper(action, bias_4h, rsi_15m):
    if action == "LONG":
        if rsi_15m >= 80: 
            return False, f"BÁC BỎ: FOMO Đỉnh 15m (RSI={rsi_15m:.1f})"
        if bias_4h == "BEARISH":
            return True, f"Cảnh báo: Đánh ngược sóng 4H ({bias_4h}) - Ăn sóng hồi"
        return True, "Hợp lưu MTF (Đồng thuận)"
    elif action == "SHORT":
        if rsi_15m <= 20: 
            return False, f"BÁC BỎ: Hoảng loạn Đáy 15m (RSI={rsi_15m:.1f})"
        if bias_4h == "BULLISH":
            return True, f"Cảnh báo: Đánh ngược sóng 4H ({bias_4h}) - Ăn sóng hồi"
        return True, "Hợp lưu MTF (Đồng thuận)"

def _build_trade_candidate(symbol, action, raw_proba, live_row, raw_sentiment, market_regime, 
                           causal_ok, causal_note, ob_metrics, hawkes_status, hawkes_score, 
                           squeeze_status, squeeze_note, mtf_ok, mtf_note, 
                           liq_risk, sentiment_dir, sentiment_sev, shap_pushers=[], meta_prob=1.0):
    threshold, setup_key, setup_score_key = (LONG_PROBA_THRESHOLD, "setup_long_candidate", "Setup_Long_Score") if action == "LONG" else (SHORT_PROBA_THRESHOLD, "setup_short_candidate", "Setup_Short_Score")
    trend_strength = _safe_float(live_row.get("Trend_Strength"))
    vol_regime = _safe_float(live_row.get("Volatility_Regime"), 1.0)
    ofi = _safe_float(ob_metrics.get("ofi"))
    spread_bps = _safe_float(ob_metrics.get("spread_bps"), 999.0)
    setup_ok = bool(live_row.get(setup_key, 0))
    setup_score = int(_safe_float(live_row.get(setup_score_key), 0))
    fused_proba, notes = apply_cmc_fusion(raw_proba, raw_sentiment, trend_strength, vol_regime, f"OPEN_{action}"), []
    dist_to_demand = _safe_float(live_row.get("Dist_to_Demand"), 999.0)
    dist_to_supply = _safe_float(live_row.get("Dist_to_Supply"), 999.0)
    ob_boost = 0.0
    if action == "LONG" and 0.0 <= dist_to_demand <= 0.015:
        ob_boost = 0.15 # Buff ngay 15% xác suất
        notes.append(f"🐋 Hợp lưu Cá Mập: Chạm vùng Cầu (Demand Zone) - Cửa bật cực cao!")
    elif action == "SHORT" and 0.0 <= dist_to_supply <= 0.015:
        ob_boost = 0.15
        notes.append(f"🐋 Hợp lưu Cá Mập: Chạm đỉnh Bán (Supply Zone) - Cửa sập cực cao!")
    fused_proba += ob_boost
    final_proba = _clamp(fused_proba, 0.0, 0.999)
    atr_pct = _safe_float(live_row.get("ATR_Pct"), 0.02)
    dynamic_sl_pct = _clamp(atr_pct * 1.5, 0.01, 0.06) 
    dynamic_tp_pct = dynamic_sl_pct * 2.0
    is_expectancy_positive, exp_score, rr_ratio = calculate_trade_expectancy(final_proba, dynamic_tp_pct, dynamic_sl_pct)
    if is_expectancy_positive: notes.append(f"Kỳ vọng DƯƠNG (+{exp_score*100:.2f}%)")
    else: notes.append(f"Kỳ vọng ÂM ({exp_score*100:.2f}%) -> ÉP NO_TRADE")     
    meta_approved, meta_reason = apply_meta_labeling(
        symbol, action, live_row, ob_metrics, 
        sentiment_dir, sentiment_sev, liq_risk, market_regime
    )
    if not meta_approved: notes.append(f"Meta-Model Bác bỏ: {meta_reason}")
    else: notes.append(f"Meta-Model: {meta_reason}")
    if not mtf_ok: notes.append(f"MTF Bác bỏ: {mtf_note}")
    else: notes.append(f"MTF: {mtf_note}")
    if not causal_ok: notes.append(f"Causal Bác bỏ: {causal_note}")
    edge_score = (final_proba - threshold) + (setup_score - 5) * 0.01 - (hawkes_score * 0.05)
    is_smart_entry = False
    if final_proba >= 0.70 and setup_score >= 3:
        is_smart_entry = True
        notes.append("🎯 Đột phá: AI Tự tin gánh Setup")   
    elif final_proba >= 0.55 and setup_score >= 7:
        is_smart_entry = True
        notes.append("📊 Đột phá: Setup đẹp gánh AI")
    elif final_proba >= 0.60 and setup_score >= 5:
        is_smart_entry = True
    if meta_prob < 0.50:
        action = "NO_TRADE"  
        notes.append(f"AI Vệ Sĩ Tầng 2 BÁC BỎ (Xác suất thắng chỉ {meta_prob*100:.1f}%)")
    else:
        notes.append(f"AI Vệ Sĩ Tầng 2 DUYỆT ({meta_prob*100:.1f}%)")
    should_open = (
        is_smart_entry 
        and is_expectancy_positive    # Bắt buộc: Toán học phải có lãi
        and mtf_ok                    # Bắt buộc: Không đánh ngược trend 4H
        and meta_approved             # Bắt buộc: Heuristic vệ sĩ cũ
        and causal_ok                 # Bắt buộc: Phải có tính nhân quả
        and (meta_prob >= 0.50)       # <--- THÊM ĐIỀU KIỆN: AI Vệ Sĩ ML phải đồng ý
    )
    return {
        "symbol": symbol, "action": action, "raw_proba": raw_proba, "final_proba": final_proba, "proba": final_proba, "threshold": threshold,
        "should_open": should_open,
        "edge_score": edge_score, "setup_score": setup_score, "setup_ok": setup_ok, "raw_sentiment": raw_sentiment, "market_regime": market_regime,
        "order_book_ofi": ofi, "spread_bps": spread_bps, "causal_ok": causal_ok, "causal_note": causal_note, "hawkes_status": hawkes_status,
        "hawkes_score": hawkes_score, "squeeze_status": squeeze_status, "squeeze_note": squeeze_note, "notes": notes,
        "entry_price": _safe_float(ob_metrics.get("mid_price"), _safe_float(live_row.get("Close"), 0.0)),
        "entry_features": _serialize_feature_dict(live_row.to_dict()), "signal_target": "standard" if final_proba >= threshold + 0.06 and setup_score >= 6 else "basic",
        "shap_pushers": shap_pushers,
        "meta_prob": meta_prob 
    }

def evaluate_risk_adjusted_ev(meta_features_df):
    """
    Đo lường Kỳ vọng Lợi nhuận (EV) và Rủi ro (Uncertainty).
    meta_features_df: DataFrame chứa đúng 7 cột meta features của 1 dòng nến.
    """
    if not LIVE_META_ARTIFACT:
        return False, 0.0, 0.0, 0.0
    ev_model = LIVE_META_ARTIFACT["ev_model"]
    unc_model = LIVE_META_ARTIFACT["uncertainty_model"]
    expected_pnl = ev_model.predict(meta_features_df)[0]
    uncertainty = unc_model.predict(meta_features_df)[0]
    RISK_PENALTY_LAMBDA = 0.5 
    ev_adjusted = expected_pnl - (RISK_PENALTY_LAMBDA * uncertainty)
    MIN_ACCEPTABLE_EV = 0.0015 
    is_approved = ev_adjusted > MIN_ACCEPTABLE_EV
    return is_approved, expected_pnl, uncertainty, ev_adjusted

def _select_trade_candidate(long_candidate, short_candidate):
    tradable = []
    for c in [long_candidate, short_candidate]:
        if not c: continue
        if c.get("should_open", False): 
            tradable.append(c)
    if not tradable: 
        return None
    tradable.sort(key=lambda item: (item.get("edge_score", -1.0), item.get("final_proba", 0.0)), reverse=True)
    if len(tradable) >= 2 and abs(tradable[0].get("final_proba", 0) - tradable[1].get("final_proba", 0)) < 0.03:
        return None
    return tradable[0]

def _calculate_position_pnl_pct(side, entry_price, current_price): return (current_price - entry_price) / max(entry_price, 1e-9) if side == "LONG" else (entry_price - current_price) / max(entry_price, 1e-9) if side == "SHORT" else 0.0

def _close_real_position(symbol, memory, current_price, reason):
    side = memory.get("position_side", "NONE")
    quantity = memory.get("quantity", 0.0) # Khối lượng đã khớp lúc mở lệnh
    entry_price = _safe_float(memory.get("entry_price"), 0.0)
    invested_usdt = _safe_float(memory.get("invested_usdt"), 0.0)
    target = memory.get("last_signal_target", "standard")
    shap_drivers = memory.get("shap_entry_drivers", [])
    if side == "NONE" or quantity <= 0: 
        return None, None, ""
    close_side = "SELL" if side == "LONG" else "BUY"
    try:
        order = client.futures_create_order(
            symbol=symbol,
            side=close_side,
            type='MARKET',
            quantity=quantity,
            reduceOnly=True 
        )
        ghi_log(f"🛑 [API THỰC CHIẾN] Đã Khớp lệnh ĐÓNG {side} {symbol} | Lý do: {reason}")
    except Exception as e:
        ghi_log(f"🚨 [API LỖI FATAL] Đóng lệnh {symbol} thất bại: {e}. PPO sẽ tự động thử lại!")
        return None 
    pnl_pct = _calculate_position_pnl_pct(side, entry_price, current_price)
    shap_str = ""
    if shap_drivers:
        feature_names = [f"{f} (+{v:.3f})" for f, v in shap_drivers]
        if pnl_pct > 0:
            shap_str = f"\n🎖️ BẢNG VÀNG LẬP CÔNG: {', '.join(feature_names)}"
            ghi_log(f"🟢 [SHAP ATTRIBUTION] {symbol} THẮNG. Kẻ lập công: {', '.join([f for f, v in shap_drivers])}")
        else:
            shap_str = f"\n🔪 KẺ PHẢN BỘI (TRAPPED): {', '.join(feature_names)}"
            ghi_log(f"🔴 [SHAP ATTRIBUTION] {symbol} THUA. Rút kinh nghiệm từ kẻ lừa đảo: {', '.join([f for f, v in shap_drivers])}")
    trade_summary = {
        "symbol": symbol, "side": side, "entry_price": entry_price, 
        "exit_price": current_price, "pnl_pct": pnl_pct, 
        "pnl_usdt": invested_usdt * pnl_pct, "reason": reason, 
        "closed_at": str(_utc_now())
    }
    l2_history = memory.get("l2_buffer", deque(maxlen=10))
    memory.clear()
    memory.update(_symbol_state_defaults())
    memory.update({
        "l2_buffer": l2_history, "last_trade": trade_summary, 
        "last_signal_target": target, "last_signal_reason": reason, 
        "last_scan_price": current_price
    })
    entry_features = memory.get("entry_features") or {}
    if "market_regime" in entry_features:
        regime = memory["entry_features"]["Market_Regime"]
        risk_engine.update_governor_pnl(regime, side, pnl_pct)
    return trade_summary, target, shap_str

try:
    exit_model_ai = joblib.load("xgb_v8_exit_model.pkl")
except:
    exit_model_ai = None

def ppo_exit_action_v2(symbol, memory, current_price, ob_metrics, live_row):
    side = memory.get("position_side", "NONE")
    entry_price = _safe_float(memory.get("entry_price", current_price))
    bars_held = int(memory.get("bars_held", 0))
    pnl_pct = _calculate_position_pnl_pct(side, entry_price, current_price)
    ofi = _safe_float(ob_metrics.get("ofi"), 0.0)
    atr_pct = _safe_float(live_row.get("ATR_Pct"), 0.02)
    if atr_pct == 0.0: atr_pct = 0.02
    dynamic_tp = _clamp(atr_pct * 3.0, 0.02, 0.10) 
    dynamic_sl = _clamp(atr_pct * 1.5, 0.01, 0.05)
    ai_expected_pnl = None
    if 'exit_model_ai' in globals() and isinstance(exit_model_ai, dict) and "q_0.5" in exit_model_ai:
        try:
            if "feature_columns_v8" in globals():
                X_exit = pd.DataFrame([live_row])[feature_columns_v8].astype(np.float32)
                pred_q50 = float(exit_model_ai["q_0.5"].predict(X_exit)[0])
                ai_expected_pnl = pred_q50
        except Exception as e:
            ghi_log(f"⚠️ Lỗi Inference AI Exit: {e}")
    if pnl_pct <= -dynamic_sl:
        return "PANIC_EXIT", f"Cắt lỗ động Quantile (-{dynamic_sl*100:.2f}%)"
    if pnl_pct >= dynamic_tp:
        return "PANIC_EXIT", f"Đạt Target Quantile tối ưu (+{dynamic_tp*100:.2f}%)"
    if ai_expected_pnl is not None:
        if side == "LONG" and ai_expected_pnl < -0.005:
            return "PANIC_EXIT", f"AI Exit dự báo giá giảm (Exp PnL: {ai_expected_pnl*100:.2f}%)"     
        elif side == "SHORT" and ai_expected_pnl > 0.005:
            return "PANIC_EXIT", f"AI Exit dự báo giá bơm (Exp PnL: {ai_expected_pnl*100:.2f}%)"
    if pnl_pct > 0.015: 
        if (side == "LONG" and ofi < -0.5) or (side == "SHORT" and ofi > 0.5):
            return "PANIC_EXIT", f"Chốt lời bảo vệ: Dòng lệnh L2 đảo ngược (OFI: {ofi:.2f})"
    return "HOLD", "Kỳ vọng tăng trưởng vẫn xanh"

def _evaluate_open_position(symbol, memory, current_price, ob_metrics, live_row):
    if memory.get("position_side", "NONE") == "NONE":
        return None
    time_in_trade = int(memory.get("bars_held", 0)) + 1
    memory["bars_held"] = time_in_trade
    entry_price = _safe_float(memory.get("entry_price"), current_price)
    if current_price > memory.get("peak_price", entry_price): memory["peak_price"] = current_price
    if current_price < memory.get("trough_price", entry_price): memory["trough_price"] = current_price
    action, ppo_reason = ppo_exit_action_v2(symbol, memory, current_price, ob_metrics, live_row)
    pnl_pct = _calculate_position_pnl_pct(memory["position_side"], entry_price, current_price)
    if action == "HOLD" and time_in_trade > 48 and pnl_pct < 0.005:
        action = "PANIC_EXIT"
        ppo_reason = "Time Stop: Ngâm vốn quá 48H không bay nổi"
    if action == "PANIC_EXIT":
        return _close_real_position(symbol, memory, current_price, f"🤖 VỆ SĨ PPO: {ppo_reason}")
    return None

def _open_real_position(signal):
    symbol = signal["symbol"]
    action = signal["action"]
    entry_price = _safe_float(signal.get("entry_price"), 0.0)   
    if entry_price <= 0.0: return None
    try:
        account_info = client.futures_account()
        available_margin = float(account_info['availableBalance'])
        total_wallet = float(account_info['totalWalletBalance'])
        LEVERAGE = 15
        win_prob = signal.get("final_proba", 0.55)
        entry_features = signal.get("entry_features", {})
        atr_pct = _safe_float(entry_features.get("ATR_Pct"), 0.02)
        sl_pct = max(min(atr_pct * 1.5, 0.06), 0.01) 
        tp_pct = sl_pct * 2.0
        R_ratio = tp_pct / sl_pct
        setup_score = signal.get("setup_score", 5)
        uncertainty_penalty = 0.08 - (setup_score * 0.005) 
        adj_prob = max(0.01, win_prob - uncertainty_penalty)
        raw_kelly = adj_prob - ((1.0 - adj_prob) / R_ratio)
        safe_kelly = max(0.0, raw_kelly * 0.25)
        dynamic_risk_pct = max(0.005, min(safe_kelly, 0.05))
        max_risk_usd = total_wallet * dynamic_risk_pct
        target_notional = max_risk_usd / sl_pct
        ghi_log(f"🧠 [KELLY V2] {symbol}: Raw={win_prob*100:.1f}% | Adj={adj_prob*100:.1f}% | Size: {dynamic_risk_pct*100:.2f}% (${max_risk_usd:.2f})")
        exchange_info = client.futures_exchange_info()
        symbol_rules = next((item for item in exchange_info['symbols'] if item['symbol'] == symbol), None)
        if not symbol_rules:
            ghi_log(f"⚠️ Không tìm thấy luật lệ cho {symbol}.")
            return None
        min_qty, step_size, min_notional = 0.0, 0.0, 5.0
        for f in symbol_rules['filters']:
            if f['filterType'] == 'LOT_SIZE':
                min_qty = float(f['minQty'])
                step_size = float(f['stepSize'])
            elif f['filterType'] == 'MIN_NOTIONAL':
                min_notional = float(f.get('notional', 5.0))
        actual_notional = max(target_notional, min_notional * 1.05)
        required_margin = actual_notional / LEVERAGE
        if required_margin > available_margin:
            ghi_log(f"🛡️ [PRE-FLIGHT] HỦY {symbol}: Cần {required_margin:.2f}$ cọc, nhưng ví chỉ còn {available_margin:.2f}$.")
            return None
        raw_quantity = actual_notional / entry_price
        precision = max(0, int(round(-math.log(step_size, 10), 0)))
        quantity = round(math.floor(raw_quantity / step_size) * step_size, precision)
        if quantity < min_qty:
            ghi_log(f"🛡️ [PRE-FLIGHT] HỦY {symbol}: Khối lượng {quantity} nhỏ hơn mức sàn cho phép ({min_qty}).")
            return None
        recalc_notional = quantity * entry_price
        if recalc_notional < min_notional:
            ghi_log(f"🛡️ [PRE-FLIGHT] HỦY {symbol}: Giá trị thực tế {recalc_notional:.2f}$ < Min Notional ({min_notional}$).")
            return None
        side_str = "BUY" if action == "LONG" else "SELL"
        order = client.futures_create_order(
            symbol=symbol,
            side=side_str,
            type='MARKET',
            quantity=quantity
        )
        ghi_log(f"🚀 [API THỰC CHIẾN] Khớp {action} {symbol} | Vol: {quantity} | Trị giá: ~${recalc_notional:.2f} | Cọc: ~${required_margin:.2f}")
        memory = _ensure_symbol_state(symbol)
        memory.update({
            "position_side": action, 
            "entry_price": entry_price, 
            "quantity": quantity, 
            "invested_usdt": recalc_notional, 
            "peak_price": entry_price, 
            "trough_price": entry_price, 
            "opened_at": str(_utc_now()), 
            "entry_features": signal.get("entry_features"), 
            "last_signal_target": signal.get("signal_target", "basic"), 
            "last_signal_reason": ", ".join(signal.get("notes", [])[:4]), 
            "last_prediction_side": action, 
            "last_prediction_proba": signal.get("final_proba", 0.0), 
            "last_scan_price": entry_price,
            "shap_entry_drivers": signal.get("shap_pushers", []) 
        })
        return memory           
    except Exception as e:
        ghi_log(f"🚨 [API TỪ CHỐI] Lỗi bất ngờ khi mở lệnh {symbol}: {e}")
        return None

def _format_candidate_summary(c): return f"{c['action']} Gốc={c['raw_proba']*100:.1f}% DungHợp={c['final_proba']*100:.1f}% Ngưỡng={c['threshold']*100:.1f}% Setup={c['setup_score']} OFI={c['order_book_ofi']:+.2f} Spread={c['spread_bps']:.1f}bps Hawkes={c['hawkes_score']:.2f} Causal={'HỢP LỆ' if c['causal_ok'] else 'BÁC BỎ'}"

def load_dynamic_weights():
    try:
        with open("ensemble_weights.json", "r") as f: return json.load(f)
    except:
        return {"0": {"XGB": 1.0, "LSTM": 0.0}, "1": {"XGB": 0.8, "LSTM": 0.2}, "2": {"XGB": 0.5, "LSTM": 0.5}}

ensemble_weights_dict = load_dynamic_weights()

def hybrid_ensemble_predict(xgb_model, dl_model, df_tabular_features, df_raw_history, market_regime=1):
    for col in feature_columns_v8:
        if col not in df_tabular_features.columns:
            df_tabular_features[col] = 0.0
    X_live_tabular = df_tabular_features[feature_columns_v8].iloc[[-1]].astype(np.float32)
    xgb_prob = float(xgb_model.predict_proba(X_live_tabular)[0][1]) if xgb_model else 0.0 
    DL_SEQUENCE_LENGTH = 128
    weights = ensemble_weights_dict.get(str(market_regime), {"XGB": 1.0, "LSTM": 0.0})
    weight_xgb = weights["XGB"]
    weight_dl = weights["LSTM"]
    if dl_model is None or len(df_raw_history) < DL_SEQUENCE_LENGTH or weight_dl <= 0.0:
        return xgb_prob, xgb_prob, 0.0
    try:
        df_dl = df_raw_history.copy()
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df_dl[col] = pd.to_numeric(df_dl[col], errors='coerce') 
        df_dl["Log_Return"] = np.log(df_dl["Close"] / df_dl["Close"].shift(1))
        df_dl["High_Low_Spread"] = (df_dl["High"] - df_dl["Low"]) / df_dl["Low"]
        df_dl["Close_Open_Spread"] = (df_dl["Close"] - df_dl["Open"]) / df_dl["Open"]
        df_dl["Volume_Log"] = np.log1p(df_dl["Volume"])
        df_dl["Volatility_24"] = df_dl["Log_Return"].rolling(24).std()
        df_dl = df_dl.fillna(0.0) 
        DL_FEATURES = ["Log_Return", "High_Low_Spread", "Close_Open_Spread", "Volume_Log", "Volatility_24"]
        seq_data = df_dl[DL_FEATURES].tail(DL_SEQUENCE_LENGTH).astype(np.float32).values
        seq_data_scaled = (seq_data - np.mean(seq_data, axis=0)) / (np.std(seq_data, axis=0) + 1e-9) 
        X_live_seq = seq_data_scaled.reshape(1, DL_SEQUENCE_LENGTH, len(DL_FEATURES))
        dl_prob = float(dl_model.predict(X_live_seq, verbose=0)[0][0])
        hybrid_prob = (xgb_prob * weight_xgb) + (dl_prob * weight_dl)
        return hybrid_prob, xgb_prob, dl_prob
    except Exception as e:
        ghi_log(f"Lỗi Inference Deep Learning: {e}")
        return xgb_prob, xgb_prob, 0.0

def scan_single_symbol(symbol):
    pf_long = 1.0   
    pf_short = 1.0
    ob_metrics = {}
    try:
        required_vars = ["expert_models", "dl_model_long", "dl_model_short", "feature_columns_v8"]
        missing_vars = [n for n in required_vars if n not in globals()]
        if missing_vars: return None 
        memory = _ensure_symbol_state(symbol)
        raw_sentiment = get_asset_sentiment(symbol)
        sentiment_dir, sentiment_sev, funding, oi, liq_risk, liq_note = fetch_derivatives_and_alt_data(symbol, raw_sentiment, memory)
        df_features = prepare_live_feature_frame_dual(_get_cached_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=260), raw_sentiment, symbol)
        if len(df_features) == 0: return None
        current_price = _safe_float(pd.to_numeric(df_features["Close"]).iloc[-1], 0.0)
        live_row = df_features.iloc[-1]
        black_swan_triggered, black_swan_info = check_black_swan(df_features, symbol)
        market_regime = detect_market_regime(df_features)
        raw_long, raw_short = 0.0, 0.0
        shap_pushers_l, shap_pushers_s = [], []

        # 🧠 TẦNG 1: SOFT MoE (GATING NETWORK & DUNG HỢP 3 CHUYÊN GIA)
        if 'gating_network_ai' in globals() and gating_network_ai is not None:
            try:
                df_gating = df_features[gating_features].iloc[[-1]].copy()
                for col in gating_features:
                    if col not in df_gating.columns: df_gating[col] = 0.0
                X_gate = df_gating[gating_features].astype(np.float32)
                weights = gating_network_ai.predict_proba(X_gate)[0]
                w_sleep, w_sideway, w_trend = weights[0], weights[1], weights[2]
            except Exception as e:
                w_sleep = 1.0 if market_regime == 0 else 0.0
                w_sideway = 1.0 if market_regime == 1 else 0.0
                w_trend = 1.0 if market_regime == 2 else 0.0
        else:
            w_sleep = 1.0 if market_regime == 0 else 0.0
            w_sideway = 1.0 if market_regime == 1 else 0.0
            w_trend = 1.0 if market_regime == 2 else 0.0
        exp_0 = expert_models.get(0) or expert_models.get("0")
        exp_1 = expert_models.get(1) or expert_models.get("1")
        exp_2 = expert_models.get(2) or expert_models.get("2")
        p_l_0, _, _ = hybrid_ensemble_predict(exp_0["long"], dl_model_long, df_features, df_features, 0) if exp_0 else (0.0, 0, 0)
        p_l_1, _, _ = hybrid_ensemble_predict(exp_1["long"], dl_model_long, df_features, df_features, 1) if exp_1 else (0.0, 0, 0)
        p_l_2, _, _ = hybrid_ensemble_predict(exp_2["long"], dl_model_long, df_features, df_features, 2) if exp_2 else (0.0, 0, 0)
        p_s_0, _, _ = hybrid_ensemble_predict(exp_0["short"], dl_model_short, df_features, df_features, 0) if exp_0 else (0.0, 0, 0)
        p_s_1, _, _ = hybrid_ensemble_predict(exp_1["short"], dl_model_short, df_features, df_features, 1) if exp_1 else (0.0, 0, 0)
        p_s_2, _, _ = hybrid_ensemble_predict(exp_2["short"], dl_model_short, df_features, df_features, 2) if exp_2 else (0.0, 0, 0)
        raw_long_uncal = (w_sleep * p_l_0) + (w_sideway * p_l_1) + (w_trend * p_l_2)
        raw_short_uncal = (w_sleep * p_s_0) + (w_sideway * p_s_1) + (w_trend * p_s_2)
        # 🛡️ TẦNG 2: HIỆU CHỈNH (CALIBRATION) & SHAP INSIGHTS
        calibs = model_calibrations.get(str(market_regime), {})
        long_calib = calibs.get("LONG", {"A": 1.0, "B": 0.0, "PF": 1.0})
        short_calib = calibs.get("SHORT", {"A": 1.0, "B": 0.0, "PF": 1.0})
        pf_long = float(long_calib.get("PF", 1.0))
        pf_short = float(short_calib.get("PF", 1.0))
        raw_long = calibrate_xgboost_probability(raw_long_uncal, long_calib["A"], long_calib["B"])
        raw_short = calibrate_xgboost_probability(raw_short_uncal, short_calib["A"], short_calib["B"])
        if pf_long < 1.0: raw_long = 0.0
        if pf_short < 1.0: raw_short = 0.0
        active_expert = expert_models.get(market_regime) or expert_models.get(str(market_regime))
        if active_expert:
            if raw_long > 0.40: shap_pushers_l, _ = extract_shap_insights(active_expert["long"], df_features, feature_columns_v8)
            if raw_short > 0.40: shap_pushers_s, _ = extract_shap_insights(active_expert["short"], df_features, feature_columns_v8)
        final_proba_l = raw_long
        final_proba_s = raw_short

        # =======================================================
        # 🛡️ [TẦNG 2.5] GỌI CÁC GATEKEEPER BẮT BUỘC CHẠY TRƯỚC
        # Phải chạy trước để AI Vệ Sĩ có data Orderbook (ob_metrics) và Causal
        # =======================================================
        ob_metrics = analyze_order_book(symbol, memory, ORDER_BOOK_DEPTH)
        hawkes_status, hawkes_score, _ = detect_spoofing_hawkes(symbol, memory)
        squeeze_status, squeeze_note = check_derivatives_squeeze(symbol, df_features)
        long_causal_ok, _ = check_causal_validity(symbol, df_features, "LONG")
        short_causal_ok, _ = check_causal_validity(symbol, df_features, "SHORT")
        bias_4h, rsi_15m = analyze_multi_timeframe(symbol)
        mtf_long_ok, mtf_long_note = apply_mtf_gatekeeper("LONG", bias_4h, rsi_15m)
        mtf_short_ok, mtf_short_note = apply_mtf_gatekeeper("SHORT", bias_4h, rsi_15m)

        # =======================================================
        # 🛡️ [TẦNG 2] GỌI AI VỆ SĨ (SIDE-SPECIFIC META-MODEL)
        # =======================================================
        meta_prob_l = 1.0
        meta_prob_s = 1.0
        if meta_model_ai is not None:
            try:
                # --- VỆ SĨ SOI KÈO LONG ---
                meta_input_l = {
                    "pred_proba": final_proba_l,
                    "Market_Regime": market_regime,
                    "Taker_Imbalance": _safe_float(ob_metrics.get("ofi"), 0.0), # Đã có ob_metrics từ Gatekeeper ở trên
                    "Sentiment_Score": _safe_float(live_row.get("Sentiment_Score"), 0.0), 
                    "Volatility_24": _safe_float(live_row.get("Volatility_24"), 0.02),
                    "Volume_Z": _safe_float(live_row.get("Volume_Z"), 0.0),
                    "Trend_Strength": _safe_float(live_row.get("Trend_Strength"), 0.0)
                }
                df_meta_l = pd.DataFrame([meta_input_l])
                for col in meta_feature_cols:
                    if col not in df_meta_l.columns: df_meta_l[col] = 0.0
                meta_prob_l = float(meta_model_ai.predict_proba(df_meta_l[meta_feature_cols].astype(np.float32))[0][1])

                # --- VỆ SĨ SOI KÈO SHORT ---
                meta_input_s = {
                    "pred_proba": final_proba_s,
                    "Market_Regime": market_regime,
                    "Taker_Imbalance": _safe_float(ob_metrics.get("ofi"), 0.0),
                    "Sentiment_Score": -_safe_float(live_row.get("Sentiment_Score"), 0.0), 
                    "Volatility_24": _safe_float(live_row.get("Volatility_24"), 0.02),
                    "Volume_Z": _safe_float(live_row.get("Volume_Z"), 0.0),
                    "Trend_Strength": _safe_float(live_row.get("Trend_Strength"), 0.0)
                }
                df_meta_s = pd.DataFrame([meta_input_s])
                for col in meta_feature_cols:
                    if col not in df_meta_s.columns: df_meta_s[col] = 0.0
                meta_prob_s = float(meta_model_ai.predict_proba(df_meta_s[meta_feature_cols].astype(np.float32))[0][1])
                is_approved_l, exp_pnl_l, unc_l, ev_adj_l = evaluate_risk_adjusted_ev(X_meta_l)
                is_approved_s, exp_pnl_s, unc_s, ev_adj_s = evaluate_risk_adjusted_ev(X_meta_s)

                if is_approved_l:
                    print(f"🔥 [VỆ SĨ LONG] {symbol} DUYỆT! Lãi kỳ vọng: {exp_pnl_l*100:.2f}% | Rủi ro: {unc_l*100:.2f}% | Lãi Ròng Chắc Chắn: {ev_adj_l*100:.2f}%")
                if is_approved_s:
                    print(f"🔥 [VỆ SĨ SHORT] {symbol} DUYỆT! Lãi kỳ vọng: {exp_pnl_s*100:.2f}% | Rủi ro: {unc_s*100:.2f}% | Lãi Ròng Chắc Chắn: {ev_adj_s*100:.2f}%")
                if is_regime_profitable("LONG", market_regime) and is_approved_l:
                    long_c = _build_trade_candidate(symbol, "LONG", raw_long, live_row, raw_sentiment, market_regime, long_causal_ok, "", ob_metrics, hawkes_status, hawkes_score, squeeze_status, squeeze_note, mtf_long_ok, mtf_long_note, liq_risk, sentiment_dir, sentiment_sev, shap_pushers_l, ev_adj_l)
                else:
                    long_c = None
                if is_regime_profitable("SHORT", market_regime) and is_approved_s:
                    short_c = _build_trade_candidate(symbol, "SHORT", raw_short, live_row, raw_sentiment, market_regime, short_causal_ok, "", ob_metrics, hawkes_status, hawkes_score, squeeze_status, squeeze_note, mtf_short_ok, mtf_short_note, liq_risk, sentiment_dir, sentiment_sev, shap_pushers_s, ev_adj_s)
                else:
                    short_c = None
            except Exception as e:
                ghi_log(f"⚠️ Lỗi Inference AI Vệ Sĩ Tầng 2: {e}")

        # --- XÂY DỰNG HỒ SƠ LỆNH RIÊNG BIỆT ---
        # Lúc này long_causal_ok và ob_metrics đã tồn tại và sẵn sàng được đóng gói!
        long_c = _build_trade_candidate(
            symbol, "LONG", final_proba_l, live_row, raw_sentiment, 
            market_regime, long_causal_ok, "", ob_metrics, 
            hawkes_status, hawkes_score, squeeze_status, squeeze_note, 
            mtf_long_ok, mtf_long_note, liq_risk, 
            sentiment_dir, sentiment_sev, shap_pushers_l, meta_prob_l
        )
        
        short_c = _build_trade_candidate(
            symbol, "SHORT", final_proba_s, live_row, raw_sentiment, 
            market_regime, short_causal_ok, "", ob_metrics, 
            hawkes_status, hawkes_score, squeeze_status, squeeze_note, 
            mtf_short_ok, mtf_short_note, liq_risk, 
            sentiment_dir, sentiment_sev, shap_pushers_s, meta_prob_s
        )
        
        # =======================================================
        # 🎯 [TẦNG 3] AI QUANTILE THOÁT LỆNH & TÍNH KỲ VỌNG
        # =======================================================
        expected_pnl_l, tp_pct_l, sl_pct_l = 0.0, 0.02, 0.01
        expected_pnl_s, tp_pct_s, sl_pct_s = 0.0, 0.02, 0.01
        
        if 'exit_model_ai' in globals() and exit_model_ai is not None:
            try:
                X_exit = df_features[feature_columns_v8].iloc[[-1]].astype(np.float32)
                pred_q10 = float(exit_model_ai["q_0.1"].predict(X_exit)[0])
                pred_q50 = float(exit_model_ai["q_0.5"].predict(X_exit)[0])
                pred_q90 = float(exit_model_ai["q_0.9"].predict(X_exit)[0])
                
                pred_q10 = max(min(pred_q10, 0.30), -0.30)
                pred_q50 = max(min(pred_q50, 0.30), -0.30)
                pred_q90 = max(min(pred_q90, 0.30), -0.30)
                
                # Tính kịch bản cho LONG
                sl_pct_l = abs(min(pred_q10, -0.005)) 
                tp_pct_l = max(pred_q90, 0.01)        
                expected_pnl_l = pred_q50             
                
                # Tính kịch bản cho SHORT
                sl_pct_s = abs(max(pred_q90, 0.005))  
                tp_pct_s = abs(min(pred_q10, -0.01))
                expected_pnl_s = -pred_q50            
            except Exception as e:
                ghi_log(f"⚠️ Lỗi AI Quantile: {e}")
        
        # Sửa thành meta_prob_l
        long_c = _build_trade_candidate(symbol, "LONG", raw_long, live_row, raw_sentiment, market_regime, long_causal_ok, "", ob_metrics, hawkes_status, hawkes_score, squeeze_status, squeeze_note, mtf_long_ok, mtf_long_note, liq_risk, sentiment_dir, sentiment_sev, shap_pushers_l, meta_prob_l) 
        short_c = _build_trade_candidate(symbol, "SHORT", raw_short, live_row, raw_sentiment, market_regime, short_causal_ok, "", ob_metrics, hawkes_status, hawkes_score, squeeze_status, squeeze_note, mtf_short_ok, mtf_short_note, liq_risk, sentiment_dir, sentiment_sev, shap_pushers_s, meta_prob_s)

        # --- NHÚNG SỨC MẠNH QUANTILE VÀO PHÁN QUYẾT TẦNG 4 ---
        # Lưu lại mức Cắt lỗ / Chốt lời động để Lò phản ứng Kelly dùng sau này
        long_c["dynamic_sl"] = sl_pct_l
        long_c["dynamic_tp"] = tp_pct_l
        long_c["expected_pnl"] = expected_pnl_l
        short_c["dynamic_sl"] = sl_pct_s
        short_c["dynamic_tp"] = tp_pct_s
        short_c["expected_pnl"] = expected_pnl_s

        # CẦU DAO KỲ VỌNG (EXPECTED VALUE)
        if expected_pnl_l < 0.002:  # Đánh lên mà biên lợi nhuận < 0.2% thì vứt
            long_c["action"] = "NO_TRADE"
            if "notes" in long_c: long_c["notes"].append(f"AI Quantile Cảnh báo: Lợi nhuận kỳ vọng LONG quá thấp ({expected_pnl_l*100:.2f}%)")
            
        if expected_pnl_s < 0.002:  # Đánh xuống mà biên lợi nhuận < 0.2% thì vứt
            short_c["action"] = "NO_TRADE"
            if "notes" in short_c: short_c["notes"].append(f"AI Quantile Cảnh báo: Lợi nhuận kỳ vọng SHORT quá thấp ({expected_pnl_s*100:.2f}%)")

        # =======================================================
        close_msg_clean = ""
        if black_swan_triggered and memory["position_side"] != "NONE":
            trade_summary, target, _ = _close_real_position(symbol, memory, current_price, "BÃO THANH KHOẢN (BLACK SWAN)")
            if trade_summary:
                close_msg_clean = f"ĐÓNG LỆNH KHẨN CẤP ({trade_summary['side']}) | Lãi/Lỗ: {trade_summary['pnl_pct']*100:+.2f}%"
        close_result = _evaluate_open_position(symbol, memory, current_price, ob_metrics, live_row)
        if close_result:
            trade_summary, target, _ = close_result
            close_msg_clean = f"ĐÓNG LỆNH PPO ({trade_summary['side']}) | Lý do: {trade_summary['reason']} | PnL: {trade_summary['pnl_pct']*100:+.2f}%"
        candidate = None
        if memory["position_side"] == "NONE" and not black_swan_triggered:
            candidate = _select_trade_candidate(long_c, short_c)
            
        dominant = long_c if long_c["final_proba"] >= short_c["final_proba"] else short_c
        memory.update({"last_prediction_side": dominant["action"], "last_prediction_proba": dominant["final_proba"], "last_scan_price": current_price})
        ui_log = []
        ui_log.append(f"\n{'='*55}")
        ui_log.append(f"🎯 MỤC TIÊU QUÉT: {symbol} | Giá: {current_price:.4f}")
        
        # TẦNG 1: MÔI TRƯỜNG VĨ MÔ
        ui_log.append("[TẦNG 1] ĐÁNH GIÁ MÔI TRƯỜNG & TIN TỨC")
        ui_log.append(f"  💥 Cầu dao rủi ro: {'🚨 KÍCH HOẠT (BÃO)' if black_swan_triggered else '✅ AN TOÀN'}")
        ui_log.append(f"  🔮 Regime thị trường: {MARKET_REGIME_NAMES.get(market_regime, market_regime)}")
        ui_log.append(f"  📰 Phân tích Báo chí: {'BULL' if sentiment_dir > 0 else 'BEAR' if sentiment_dir < 0 else 'NEUTRAL'} | Điểm số: {raw_sentiment:+.3f}")
        ui_log.append(f"  ⛓️ Phái sinh On-chain: {liq_note} | Trạng thái: {liq_risk}")
        
        # TẦNG 2: AI DỰ BÁO
        ui_log.append("[TẦNG 2] NÃO BỘ DỰ BÁO AI (HYBRID ENSEMBLE)")
        if active_expert:
            ui_log.append(f"  👔 Bộ định tuyến: Giao việc cho [{active_expert['name']}]")
            if pf_long < 1.0:
                ui_log.append(f"  🛑 Cửa LONG: 🔒 BỊ KHÓA BỞI CẦU DAO (Profit Factor {pf_long:.2f} < 1.0)")
            else:
                ui_log.append(f"  🟢 Cửa LONG: Xác suất {long_c['final_proba']*100:.1f}% | Kỹ thuật (Setup): {long_c['setup_score']}/10")
            if pf_short < 1.0:
                ui_log.append(f"  🛑 Cửa SHORT: 🔒 BỊ KHÓA BỞI CẦU DAO (Profit Factor {pf_short:.2f} < 1.0)")
            else:
                ui_log.append(f"  🔴 Cửa SHORT: Xác suất {short_c['final_proba']*100:.1f}% | Kỹ thuật (Setup): {short_c['setup_score']}/10")
        else:
            ui_log.append("  ⚠️ LỖI: Không có Model cho Regime này!")

        # TẦNG 3: GATEKEEPERS
        ui_log.append("[TẦNG 3] MÀNG LỌC VỆ SĨ (GATEKEEPERS)")
        ui_log.append(f"  👁️ Dòng lệnh (OFI): {ob_metrics['ofi']:+.2f} | Tỷ lệ hủy lệnh: {ob_metrics['cancel_rate']*100:.1f}%")
        ui_log.append(f"  📡 Radar Tường giả: {hawkes_status} (Rủi ro: {hawkes_score:.2f})")
        ui_log.append(f"  🗜️ Rủi ro Squeeze: {squeeze_status}")
        ui_log.append(f"  ⏳ Phân tích Đa khung: Sóng 4H [{bias_4h}] | RSI 15m [{rsi_15m:.1f}]")
        ui_log.append(f"  🔗 Tính Nhân quả: LONG [{'HỢP LỆ' if long_causal_ok else 'BÁC BỎ'}] | SHORT [{'HỢP LỆ' if short_causal_ok else 'BÁC BỎ'}]")

        # TẦNG 4: BÙ TRỪ KỲ VỌNG
        ui_log.append("[TẦNG 4] THẨM ĐỊNH CHIẾN LƯỢC & BÙ TRỪ CHÉO")
        # Rút gọn ghi chú để nhìn đỡ rối
        long_notes = ", ".join(long_c.get('notes', ['Không']))
        short_notes = ", ".join(short_c.get('notes', ['Không']))
        ui_log.append(f"  🟢 Thẩm định LONG: {long_notes.replace('Meta-Model:', 'Meta:').replace('Kỳ vọng', 'KV')}")
        ui_log.append(f"  🔴 Thẩm định SHORT: {short_notes.replace('Meta-Model:', 'Meta:').replace('Kỳ vọng', 'KV')}")

        # TẦNG 5: QUYẾT ĐỊNH
        if close_msg_clean:
            ui_log.append(f"🛑 HÀNH ĐỘNG: {close_msg_clean}")
            
        if memory["position_side"] != "NONE":
            pnl_pct = _calculate_position_pnl_pct(memory["position_side"], _safe_float(memory["entry_price"]), current_price)
            ui_log.append(f"👀 TRẠNG THÁI: Tác tử PPO đang gồng lệnh {memory['position_side']} | Lãi/Lỗ: {pnl_pct*100:+.2f}% / ${_safe_float(memory.get('invested_usdt'), 0.0) * pnl_pct:+.2f}")
        else:
            if candidate:
                ui_log.append(f"🚀 PHÁN QUYẾT: MỞ LỆNH THỰC CHIẾN [{candidate['action']}] @ Xác suất {candidate['final_proba']*100:.1f}%")
            else:
                ui_log.append("⚖️ PHÁN QUYẾT: ĐỨNG NGOÀI (NO_TRADE - Bị chặn bởi Gatekeeper)")
                
        ui_log.append(f"{'='*55}\n")
        
        ghi_log("\n".join(ui_log))
        return candidate

    except Exception as e:
        import traceback
        ghi_log(f"\n❌ LỖI HỆ THỐNG ({symbol}): {e}\n{traceback.format_exc()}")
        return None
        
def check_portfolio_correlation(intended_trades, bot_memory):
    active_positions = {sym: data["position_side"] for sym, data in bot_memory.items() if data["position_side"] != "NONE"}
    approved_trades = []
    rejected_trades = []
    for trade in intended_trades:
        sym = trade["symbol"]
        action = trade["action"]
        if sym in ["BTCUSDT", "ETHUSDT"]:
            if ("BTCUSDT" in active_positions and active_positions["BTCUSDT"] == action) or \
               ("ETHUSDT" in active_positions and active_positions["ETHUSDT"] == action):
                rejected_trades.append(f"{sym} (Bị chặn: Đã có lệnh Core {action})")
                continue
        if sym in ["SOLUSDT", "BNBUSDT", "XRPUSDT"]:
            if "BTCUSDT" in active_positions and active_positions["BTCUSDT"] == action:
                if trade["edge_score"] < 0.22:
                    rejected_trades.append(f"{sym} (Bị chặn: Rủi ro tương quan BTC, Edge quá thấp)")
                    continue
        approved_trades.append(trade)
    return approved_trades, rejected_trades

def auto_sync_positions_with_exchange():
    ghi_log("\n🔄 [HỆ THỐNG] Đang đồng bộ trạng thái với sàn Binance...")
    try:
        positions = client.futures_position_information()
        active_on_exchange = {p['symbol']: p for p in positions if float(p['positionAmt']) != 0}
        for symbol in TARGET_SYMBOLS:
            memory = _ensure_symbol_state(symbol)
            if symbol in active_on_exchange:
                pos = active_on_exchange[symbol]
                amt = float(pos['positionAmt'])
                entry_price = float(pos['entryPrice'])
                side = "LONG" if amt > 0 else "SHORT"
                abs_qty = abs(amt)
                invested = abs_qty * entry_price
                memory.update({
                    "position_side": side,
                    "entry_price": entry_price,
                    "quantity": abs_qty, 
                    "invested_usdt": invested,
                    "last_scan_price": entry_price
                })
                ghi_log(f"✅ Đã đồng bộ {symbol}: {side} | Vol: {abs_qty} | Giá vào: {entry_price}")
            else:
                if memory["position_side"] != "NONE":
                    ghi_log(f"🧹 {symbol}: Sàn đã đóng lệnh, cập nhật bộ nhớ Bot về NONE.")
                    memory.update({"position_side": "NONE", "quantity": 0.0, "invested_usdt": 0.0})
        _save_runtime_state()
        ghi_log("🏁 [HỆ THỐNG] Đồng bộ hoàn tất. Bot đã sẵn sàng chiến đấu!")
        return True
    except Exception as e:
        ghi_log(f"🚨 [LỖI ĐỒNG BỘ] Không thể kết nối với sàn: {e}")
        return False

class PortfolioRiskEngine:
    def __init__(self):
        if "governor_stats" not in runtime_state:
            runtime_state["governor_stats"] = {}
        self.stats = runtime_state["governor_stats"]

    def check_drawdown_governor(self, regime, side):
        """Cầu dao Drawdown: Tắt model nếu nó đang bị lệch pha thị trường"""
        key = f"R{regime}_{side}"
        if key not in self.stats:
            self.stats[key] = {"peak": 1.0, "current": 1.0}
        dd = (self.stats[key]["peak"] - self.stats[key]["current"]) / self.stats[key]["peak"]
        if dd > 0.12:
            return False, f"Bị chặn bởi DD Governor (Lỗ lũy kế {dd*100:.1f}%)"
        return True, "Governor An toàn"

    def update_governor_pnl(self, regime, side, pnl_pct):
        """Cập nhật dữ liệu PnL sau khi lệnh đóng"""
        key = f"R{regime}_{side}"
        if key not in self.stats:
            self.stats[key] = {"peak": 1.0, "current": 1.0}
        self.stats[key]["current"] *= (1.0 + pnl_pct)
        if self.stats[key]["current"] > self.stats[key]["peak"]:
            self.stats[key]["peak"] = self.stats[key]["current"]

    def filter_by_btc_beta_cap(self, intended_trades, bot_memory):
        """Giới hạn Net Beta của toàn danh mục (Chống rủi ro sập chung toàn thị trường)"""
        active_positions = {sym: data for sym, data in bot_memory.items() if data["position_side"] != "NONE"}
        net_beta_exposure = 0.0
        for sym, data in active_positions.items():
            beta_sign = 1 if data["position_side"] == "LONG" else -1
            weight = data["invested_usdt"] / max(globals().get("TOTAL_PORTFOLIO_USDT", 250.0), 1e-9)
            asset_beta = 1.0 if sym == "BTCUSDT" else 0.85 # Giả định Beta của Altcoin với BTC ~ 0.85
            net_beta_exposure += beta_sign * weight * asset_beta
        approved_trades, rejected_trades = [], []
        for trade in intended_trades:
            trade_beta_sign = 1 if trade["action"] == "LONG" else -1
            trade_weight = 0.05 # Giả sử lệnh mới chiếm 5% vốn
            trade_beta = 1.0 if trade["symbol"] == "BTCUSDT" else 0.85
            simulated_exposure = net_beta_exposure + (trade_beta_sign * trade_weight * trade_beta)
            if abs(simulated_exposure) > 0.40:
                rejected_trades.append(f"{trade['symbol']} (Beta Cap: Portfolio Net Beta vượt ngưỡng {simulated_exposure:+.2f})")
            else:
                net_beta_exposure = simulated_exposure
                approved_trades.append(trade)
        return approved_trades, rejected_trades
risk_engine = PortfolioRiskEngine()

def run_dual_bot_parallel():
    ghi_log(f"\n--- [{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}] ĐỘNG CƠ QUẢN TRỊ DANH MỤC (CROSS-SECTIONAL RANKER) BẬT ---")
    
    # 1. QUÉT & GOM HỒ SƠ TOÀN THỊ TRƯỜNG
    intended_trades = []
    for sym in TARGET_SYMBOLS:
        result = scan_single_symbol(sym)
        # Chỉ lấy những kèo có tín hiệu rõ ràng (Bỏ qua NO_TRADE)
        if result and result.get("action") in ["LONG", "SHORT"]:
            result["symbol"] = sym
            # Tính điểm Edge Score (Kỳ vọng toán học) ngay tại đây
            exp_pnl = result.get("expected_pnl", 0.01) 
            result["edge_score"] = result.get("final_proba", 0) * exp_pnl
            intended_trades.append(result)
            
    if not intended_trades: 
        _save_runtime_state()
        ghi_log("🛡️ [PORTFOLIO MANAGER] Quét hoàn tất. Thị trường tĩnh lặng (Không có setup hoặc bị Gatekeeper chặn).")
        return
        
    # 2. KIỂM TRA SỨC NÓNG DANH MỤC (PORTFOLIO HEAT)
    TOTAL_PORTFOLIO_USDT = 250.0 
    MAX_PORTFOLIO_HEAT = 0.99  
    current_heat = sum(_safe_float(data.get("invested_usdt"), 0.0) for data in bot_memory.values() if data.get("position_side") != "NONE")
    max_allowed_heat = TOTAL_PORTFOLIO_USDT * MAX_PORTFOLIO_HEAT
    remaining_capital = max(max_allowed_heat - current_heat, 0.0)
    
    ghi_log(f"🔥 Sức nóng Danh mục: ${current_heat:.2f} / ${max_allowed_heat:.2f}")
    
    # 3. BỘ LỌC TƯƠNG QUAN (GIỮ NGUYÊN BỘ LỌC XỊN XÒ CỦA BẠN)
    approved_trades, rejected_trades = check_portfolio_correlation(intended_trades, bot_memory)
    if not approved_trades:
        ghi_log("🛡️ [PORTFOLIO MANAGER] Các tín hiệu đều bị chặn bởi bộ lọc rủi ro tương quan.")
        return

    # 4. 🏆 CHẤM ĐIỂM CHÉO & XẾP HẠNG (CROSS-SECTIONAL RANKER)
    # Sắp xếp danh sách đã duyệt theo Edge Score từ cao xuống thấp
    ranked_trades = sorted(approved_trades, key=lambda item: (item.get("edge_score", 0), item.get("final_proba", 0)), reverse=True)
    
    ghi_log(f"\n{'='*20} 🏆 BẢNG XẾP HẠNG EDGE (SAU KHI LỌC RỦI RO) {'='*20}")
    for i, c in enumerate(ranked_trades):
        medal = "🥇" if i==0 else "🥈" if i==1 else "🥉" if i==2 else "⭐"
        ghi_log(f" {medal} Rank {i+1}: {c['symbol']} [{c['action']}] | Edge: {c.get('edge_score', 0)*10000:.2f} | Xác suất: {c.get('final_proba', 0)*100:.1f}% | ExpPnL: {c.get('expected_pnl', 0)*100:.2f}%")
    ghi_log(f"{'='*72}")

    # 5. XUẤT KÍCH TOP-K LỆNH TINH HOA NHẤT
    executed = []
    # Lấy đúng số lượng MAX_SIGNAL_PER_CYCLE từ trên xuống dưới
    for signal in ranked_trades[:MAX_SIGNAL_PER_CYCLE]:
        memory = _ensure_symbol_state(signal["symbol"])
        if memory["position_side"] != "NONE": 
            continue # Bỏ qua nếu coin này đang gồng lệnh rồi
            
        # NÂNG CẤP: Thay vì lấy TP/SL cứng, ưu tiên lấy TP/SL động từ AI Quantile
        params = COIN_PARAMS.get(signal["symbol"], {"TAKE_PROFIT_PCT": 0.05, "STOP_LOSS_PCT": 0.05})
        dynamic_sl_pct = signal.get("dynamic_sl", params["STOP_LOSS_PCT"])
        dynamic_tp_pct = signal.get("dynamic_tp", params["TAKE_PROFIT_PCT"])

        if not _open_real_position(signal): 
            continue # Gọi hàm vào lệnh API Binance của bạn
            
        entry_price = _safe_float(memory.get("entry_price", 0.0), 0.0)
        
        # Tính giá Cắt lỗ / Chốt lời cụ thể để báo cáo
        stop_price = entry_price * (1.0 - dynamic_sl_pct) if signal["action"] == "LONG" else entry_price * (1.0 + dynamic_sl_pct)
        take_price = entry_price * (1.0 + dynamic_tp_pct) if signal["action"] == "LONG" else entry_price * (1.0 - dynamic_tp_pct)
        
        # Nâng cấp tin nhắn Telegram hiển thị cả mốc AI
        open_message = f"🚀 *MỞ LỆNH THỰC CHIẾN ({signal['action']})* `{signal['symbol']}`\nĐiểm vào: `{entry_price:.4f}` | Cỡ lệnh: `Tự động theo rủi ro`\n🎯 TP (AI): `{take_price:.4f}` ({dynamic_tp_pct*100:.2f}%)\n🛡️ SL (AI): `{stop_price:.4f}` ({dynamic_sl_pct*100:.2f}%)"
        send_ban_signal(open_message, target=signal.get("signal_target", "DEFAULT"))
        
        executed.append(f"{signal['symbol']} {signal['action']} @ {entry_price:.4f} (TP: {dynamic_tp_pct*100:.1f}%, SL: {dynamic_sl_pct*100:.1f}%)")
        
    _save_runtime_state()
    ghi_log(("\n[⚡ BÁO CÁO KHỚP LỆNH]\n" + "\n".join(executed)) if executed else "🛡️ [PORTFOLIO MANAGER] Lệnh đã bị API từ chối hoặc kẹt số dư.")

def run_batch_retrain_cycle():
    from datetime import datetime
    ghi_log(f"\n{'='*50}\n🔄 [{datetime.now().strftime('%H:%M:%S')}] KÍCH HOẠT CHU TRÌNH AUTO-RETRAIN (V8)\n{'='*50}")
    try:
        all_dfs = []
        for sym in TARGET_SYMBOLS:
            raw_df = _get_cached_klines(sym, Client.KLINE_INTERVAL_1HOUR, limit=800) # Lấy 800 nến gần nhất
            if raw_df is not None and not raw_df.empty:
                df_feat = prepare_live_feature_frame_dual(raw_df, 0, sym)
                df_feat['symbol'] = sym  # Đảm bảo có cột symbol thật để lát nữa groupby
                all_dfs.append(df_feat)  
        if not all_dfs:
            ghi_log("⚠️ Auto-Retrain thất bại: Không lấy được dữ liệu từ API.")
            return False  
        mega_retrain_df = pd.concat(all_dfs, ignore_index=True)
        mega_retrain_df = mega_retrain_df.sort_values("Open time").reset_index(drop=True)
        ghi_log(f"📦 Đã gom thành công {len(mega_retrain_df)} nến đa tài sản. Đẩy vào Lò rèn Champion-Challenger...")
        champion_challenger_retrain(mega_retrain_df)
        return True 

    except Exception as e:
        import traceback
        error_msg = f"❌ LỖI NGHIÊM TRỌNG KHI RETRAIN: {e}\n{traceback.format_exc()}"
        print(error_msg) # In ra Terminal
        ghi_log(error_msg) # Ghi vào file Log
        ghi_log("⚠️ HỆ THỐNG TẠM DỪNG RETRAIN CHU KỲ NÀY. GIỮ NGUYÊN MODEL HIỆN TẠI ĐỂ TRADE AN TOÀN!")
        
        return False # 🛡️ BẢN VÁ: Graceful Exit - Trả về False chặn đứng rủi ro Crash chồng Crash

        def _assign_regime(row):
            trend = abs(_safe_float(row.get("Trend_Strength"), 0.0))
            vol = _safe_float(row.get("Volatility_Regime"), 1.0)
            if trend < 0.002 and vol <= 0.9: return 0
            if trend >= 0.006 or vol >= 1.6: return 2
            return 1
        master_df["Market_Regime"] = master_df.apply(_assign_regime, axis=1)
        for r in [0, 1, 2]:
            regime_df = master_df[master_df["Market_Regime"] == r]
            regime_name = expert_models[r]["name"]
            ghi_log(f"\n📚 Đang rèn [{regime_name}] với {len(regime_df)} nến dữ liệu...")
            if len(regime_df) < 150:
                ghi_log(f"⏩ Bỏ qua {regime_name} do dữ liệu bối cảnh này quá ít (<150 nến).")
                continue
            msg_long = champion_challenger_retrain(regime_name, regime_df, "LONG", regime_idx=r)
            msg_short = champion_challenger_retrain(regime_name, regime_df, "SHORT", regime_idx=r)
            ghi_log(msg_long)
            ghi_log(msg_short)
        ghi_log(f"\n--- [🔧 LÒ RÈN ĐÓNG] KẾT THÚC KHÓA HUẤN LUYỆN ---")
    except Exception as e:
        import traceback
        ghi_log(f"❌ Lỗi hệ thống Lò rèn: {e}\n{traceback.format_exc()}")

# --- CÔNG TẮC KÍCH HOẠT AUTO-PILOT ---
import traceback
if __name__ == "__main__":
    print("✅ CẬP NHẬT: Đã kích hoạt Walk-Forward Retraining & Tự động đồng bộ!")
    schedule.every().hour.at(":00").do(run_dual_bot_parallel)
    schedule.every().day.at("01:00").do(run_batch_retrain_cycle)
    auto_sync_positions_with_exchange()
    run_dual_bot_parallel() 
    while True:
        try:
            schedule.run_pending()
            time.sleep(1) 
        except KeyboardInterrupt:
            print("\n🛑 GIÁM ĐỐC ĐÃ RA LỆNH DỪNG BOT! Cỗ máy tắt an toàn.")
            break
        except Exception as e:
            print(f"\n❌ LỖI HỆ THỐNG: {e}\n{traceback.format_exc()}")
            print("🔄 Sẽ thử lại vòng lặp sau 10 giây...")
            time.sleep(10)