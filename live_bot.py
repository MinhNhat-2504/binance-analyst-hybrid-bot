import os
import time
import warnings
from decimal import Decimal, ROUND_DOWN

import feedparser
import joblib
import numpy as np
import pandas as pd
import requests
import schedule
from binance.client import Client
from stable_baselines3 import PPO
from tensorflow.keras.models import load_model

warnings.filterwarnings("ignore")
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"


def ghi_log(thong_bao):
    print(thong_bao)
    with open("nhat_ky_trade_hybrid.txt", "a", encoding="utf-8") as f:
        f.write(thong_bao + "\n")


class ReturnsScaler:
    def inverse_transform(self, x):
        return x / 100.0


schedule.clear()

API_KEY = "wR3cb7wAkuNPPxdxnMjWtlgQzQZ0xOlDxHkOJcduxrPRGcUn3DsUxzGops4tm8zf"
API_SECRET = "Ncw5w4mR23l7ZMFOvhS4xY4TP1C33ZnzZiV3BiIvYG6EwMuQVJzP4m506PNJeZ1g"
client = Client(API_KEY, API_SECRET, testnet=True)

bot_memory = {}

print("Dang nap he thong Loi Kep (Attention-LSTM + PPO)...")
model_lstm = load_model("lstm_attention_hybrid_bot.keras")
feature_scaler = joblib.load("features_hybrid_scaler.pkl")

try:
    returns_scaler = joblib.load("returns_hybrid_scaler.pkl")
except Exception as e:
    returns_scaler = ReturnsScaler()
    ghi_log(f"Khong load duoc returns_hybrid_scaler, dung mac dinh: {e}")

ppo_agent = PPO.load("ppo_trading_agent", device="cpu")
sequence_length = 72


def _fallback_sentiment_score(titles):
    positive_words = {
        "bull", "bullish", "surge", "rally", "rise", "up",
        "breakout", "gain", "positive", "adoption", "approval",
    }
    negative_words = {
        "bear", "bearish", "drop", "fall", "down", "crash",
        "hack", "lawsuit", "ban", "negative", "rejection",
    }

    if not titles:
        return 0.0

    scores = []
    for title in titles:
        text = str(title).lower()
        pos_hits = sum(word in text for word in positive_words)
        neg_hits = sum(word in text for word in negative_words)
        if pos_hits == 0 and neg_hits == 0:
            scores.append(0.0)
        else:
            scores.append((pos_hits - neg_hits) / (pos_hits + neg_hits))
    return float(np.mean(scores)) if scores else 0.0


def get_live_sentiment_finbert():
    rss_url = "https://cointelegraph.com/rss"
    api_url = "https://api-inference.huggingface.co/models/ProsusAI/finbert"

    try:
        feed = feedparser.parse(rss_url)
        titles = [entry.title for entry in feed.entries[:10]]
        if not titles:
            return 0.0

        response = requests.post(api_url, json={"inputs": titles}, timeout=10)
        if response.status_code == 200:
            results = response.json()
            scores = []
            for res in results:
                best_label = max(res, key=lambda x: x["score"])
                if best_label["label"] == "positive":
                    scores.append(best_label["score"])
                elif best_label["label"] == "negative":
                    scores.append(-best_label["score"])
                else:
                    scores.append(0.0)
            return float(np.mean(scores))

        ghi_log(f"Server FinBERT ban (ma {response.status_code}), chuyen sang fallback sentiment.")
        return _fallback_sentiment_score(titles)

    except Exception as e:
        ghi_log(f"Loi ket noi API: {e}. Chuyen sang fallback sentiment.")
        return _fallback_sentiment_score(titles)


def calculate_features(df):
    df["Return"] = df["Close"].pct_change()
    df["EMA_14"] = df["Close"].ewm(span=14, adjust=False).mean()
    df["EMA_50"] = df["Close"].ewm(span=50, adjust=False).mean()

    delta = df["Close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df["RSI_14"] = 100 - (100 / (1 + rs))

    ema_12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema_26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = ema_12 - ema_26
    df["MACD_Signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_Hist"] = df["MACD"] - df["MACD_Signal"]

    df["BB_Mid"] = df["Close"].rolling(window=20).mean()
    df["BB_Std"] = df["Close"].rolling(window=20).std()
    df["BB_Upper"] = df["BB_Mid"] + (df["BB_Std"] * 2)
    df["BB_Lower"] = df["BB_Mid"] - (df["BB_Std"] * 2)

    df["Open time"] = pd.to_datetime(df["Open time"], unit="ms")
    df["Hour"] = df["Open time"].dt.hour
    df["DayOfWeek"] = df["Open time"].dt.dayofweek

    return df.dropna().copy()


def engineer_model_features(df):
    df = df.copy()

    if "Sentiment_Score" not in df.columns:
        df["Sentiment_Score"] = 0.0
    df["Sentiment_Score"] = df["Sentiment_Score"].fillna(0.0)

    df["Log_Return_1"] = np.log(df["Close"]).diff()
    df["Return_3"] = df["Close"].pct_change(3)
    df["Return_6"] = df["Close"].pct_change(6)
    df["Return_12"] = df["Close"].pct_change(12)
    df["Range_Pct"] = (df["High"] - df["Low"]) / df["Close"].replace(0, np.nan)
    df["Body_Pct"] = (df["Close"] - df["Open"]) / df["Open"].replace(0, np.nan)
    df["Volume_Change"] = df["Volume"].pct_change().replace([np.inf, -np.inf], np.nan)

    volume_mean = df["Volume"].rolling(24).mean()
    volume_std = df["Volume"].rolling(24).std().replace(0, np.nan)
    df["Volume_Z"] = (df["Volume"] - volume_mean) / volume_std

    prev_close = df["Close"].shift(1)
    true_range = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["ATR_14"] = true_range.rolling(14).mean()
    df["ATR_Pct"] = df["ATR_14"] / df["Close"].replace(0, np.nan)

    df["Volatility_24"] = df["Return"].rolling(24).std()
    df["EMA_14_Dist"] = (df["Close"] - df["EMA_14"]) / df["EMA_14"].replace(0, np.nan)
    df["EMA_50_Dist"] = (df["Close"] - df["EMA_50"]) / df["EMA_50"].replace(0, np.nan)
    df["MACD_Gap"] = df["MACD"] - df["MACD_Signal"]
    df["RSI_14_Norm"] = df["RSI_14"] / 100.0
    df["BB_Width"] = (df["BB_Upper"] - df["BB_Lower"]) / df["BB_Mid"].replace(0, np.nan)

    return df.replace([np.inf, -np.inf], np.nan).dropna().copy()


TARGET_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
KLINE_COLUMNS = [
    "Open time", "Open", "High", "Low", "Close", "Volume",
    "Close time", "Quote Asset", "Trades", "Taker Buy Base",
    "Taker Buy Quote", "Ignore",
]
MIN_SIGNAL_TO_TRADE = 0.0015
TRADE_BUDGET_USDT = 100.0
SYMBOL_RULES_CACHE = {}


def _get_symbol_rules(symbol):
    if symbol in SYMBOL_RULES_CACHE:
        return SYMBOL_RULES_CACHE[symbol]

    info = client.get_symbol_info(symbol)
    if not info:
        raise ValueError(f"Khong lay duoc symbol info cho {symbol}.")

    filters = {item["filterType"]: item for item in info["filters"]}
    lot_filter = filters["LOT_SIZE"]
    notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL")

    rules = {
        "step_size": Decimal(str(lot_filter["stepSize"])),
        "min_qty": Decimal(str(lot_filter["minQty"])),
        "min_notional": Decimal(str(notional_filter["minNotional"])) if notional_filter else Decimal("0"),
    }
    SYMBOL_RULES_CACHE[symbol] = rules
    return rules


def _round_step_down(quantity, step_size):
    quantity_dec = Decimal(str(quantity))
    if step_size <= 0:
        return quantity_dec
    return (quantity_dec // step_size) * step_size


def _normalize_quantity(symbol, quantity, current_price):
    rules = _get_symbol_rules(symbol)
    qty = _round_step_down(quantity, rules["step_size"])
    qty = qty.quantize(rules["step_size"], rounding=ROUND_DOWN)

    if qty < rules["min_qty"]:
        return None, f"So luong {qty} nho hon minQty {rules['min_qty']}"

    notional = qty * Decimal(str(current_price))
    if notional < rules["min_notional"]:
        return None, f"Notional {notional:.8f} nho hon minNotional {rules['min_notional']}"

    qty_str = format(qty.normalize(), "f")
    return qty_str, None


def run_live_bot():
    ghi_log(f"\n--- [{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}] BAT DAU QUET DA TAI SAN ---")

    market_sentiment = get_live_sentiment_finbert()
    ghi_log(f"Market sentiment (FinBERT): {market_sentiment:.4f}")

    for symbol in TARGET_SYMBOLS:
        ghi_log(f"\nDang soi Chart: {symbol}")

        try:
            asset = symbol.replace("USDT", "")
            memory = bot_memory.setdefault(symbol, {"entry_price": 0.0, "quantity": 0.0, "peak_equity": 0.0})

            klines = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=160)
            df = pd.DataFrame(klines, columns=KLINE_COLUMNS)

            for col in ["Open", "High", "Low", "Close", "Volume"]:
                df[col] = df[col].astype(float)

            df_features = calculate_features(df)
            df_features["Sentiment_Score"] = market_sentiment
            df_features = engineer_model_features(df_features)
            df_features["Sentiment_Score"] = market_sentiment

            expected_features = list(feature_scaler.feature_names_in_)
            missing_features = [col for col in expected_features if col not in df_features.columns]
            if missing_features:
                raise ValueError(f"Thieu feature khi inference: {missing_features}")

            recent_data = df_features[expected_features].iloc[-sequence_length:]
            if len(recent_data) < sequence_length:
                ghi_log(f"Khong du du lieu de du bao cho {symbol}.")
                continue

            scaled_data = feature_scaler.transform(recent_data)
            x_live = np.reshape(scaled_data, (1, sequence_length, scaled_data.shape[1]))
            pred_scaled = model_lstm.predict(x_live, verbose=0)
            pred_return = returns_scaler.inverse_transform(pred_scaled)[0][0]
            ghi_log(f"Attention-LSTM ({symbol}) forecast: {pred_return * 100:.4f}%")

            usdt_balance = float(client.get_asset_balance(asset="USDT")["free"])
            asset_balance = float(client.get_asset_balance(asset=asset)["free"])
            current_price = float(client.get_symbol_ticker(symbol=symbol)["price"])

            position_value = asset_balance * current_price
            net_worth = max(usdt_balance + position_value, 1e-8)
            memory["peak_equity"] = max(memory["peak_equity"], net_worth)
            cash_ratio = usdt_balance / net_worth
            asset_ratio = position_value / net_worth
            price_return = float(df_features["Return"].iloc[-1])
            drawdown = (memory["peak_equity"] - net_worth) / max(memory["peak_equity"], 1e-8)

            obs = np.array([cash_ratio, asset_ratio, price_return, pred_return, drawdown], dtype=np.float32)
            action, _ = ppo_agent.predict(obs, deterministic=True)

            if abs(pred_return) < MIN_SIGNAL_TO_TRADE:
                ghi_log(f"Tin hieu qua yeu ({pred_return * 100:.4f}%), ep HOLD de tranh overtrade.")
                action = 0
            elif pred_return > 0 and action == 2:
                ghi_log("PPO dang nghi ban nhung LSTM du bao tang, ep HOLD.")
                action = 0
            elif pred_return < 0 and action == 1:
                ghi_log("PPO dang nghi mua nhung LSTM du bao giam, ep HOLD.")
                action = 0

            if action == 1:
                ghi_log(f"PPO ({symbol}) action: BUY")
                if usdt_balance > TRADE_BUDGET_USDT:
                    raw_buy_quantity = TRADE_BUDGET_USDT / current_price
                    buy_quantity, quantity_error = _normalize_quantity(symbol, raw_buy_quantity, current_price)
                    if quantity_error:
                        ghi_log(f"Bo qua lenh BUY {symbol}: {quantity_error}")
                        continue
                    client.create_order(
                        symbol=symbol,
                        side=Client.SIDE_BUY,
                        type=Client.ORDER_TYPE_MARKET,
                        quantity=buy_quantity,
                    )
                    memory["entry_price"] = current_price
                    memory["quantity"] += float(buy_quantity)
                    ghi_log(f"Bought {buy_quantity} {asset} at ${current_price:,.4f}.")
                else:
                    ghi_log(f"Khong du USDT de mua {symbol}.")

            elif action == 2:
                ghi_log(f"PPO ({symbol}) action: SELL")
                if asset_balance >= memory["quantity"] and memory["quantity"] > 0:
                    sell_quantity, quantity_error = _normalize_quantity(
                        symbol,
                        min(asset_balance, memory["quantity"]),
                        current_price,
                    )
                    if quantity_error:
                        ghi_log(f"Bo qua lenh SELL {symbol}: {quantity_error}")
                        continue
                    client.create_order(
                        symbol=symbol,
                        side=Client.SIDE_SELL,
                        type=Client.ORDER_TYPE_MARKET,
                        quantity=sell_quantity,
                    )
                    sell_quantity_float = float(sell_quantity)
                    revenue = sell_quantity_float * current_price
                    cost = sell_quantity_float * memory["entry_price"]
                    profit_usd = revenue - cost
                    profit_pct = (current_price - memory["entry_price"]) / memory["entry_price"] * 100

                    if profit_usd > 0:
                        ghi_log(f"{symbol} chot loi: +${profit_usd:.2f} (+{profit_pct:.2f}%)")
                    else:
                        ghi_log(f"{symbol} cat lo: -${abs(profit_usd):.2f} ({profit_pct:.2f}%)")

                    memory["entry_price"] = 0.0
                    memory["quantity"] = 0.0
                else:
                    ghi_log(f"Khong co vi the {asset} de ban.")

            else:
                ghi_log(f"PPO ({symbol}) action: HOLD")
                if memory["quantity"] > 0 and memory["entry_price"] > 0:
                    unrealized_pnl = (current_price - memory["entry_price"]) / memory["entry_price"] * 100
                    ghi_log(f"{symbol} dang giu lenh. Loi/lo tam tinh: {unrealized_pnl:.2f}%")

            usdt_balance = float(client.get_asset_balance(asset="USDT")["free"])
            asset_balance = float(client.get_asset_balance(asset=asset)["free"])
            ghi_log(f"So du sau quet {symbol}: {usdt_balance:,.2f} USDT | {asset_balance:.5f} {asset}")

        except Exception as e:
            ghi_log(f"Loi khi phan tich {symbol}: {e}")


print("\nMULTI-ASSET HYBRID BOT SAN SANG!")

run_live_bot()

schedule.every().hour.at(":00").do(run_live_bot)

while True:
    schedule.run_pending()
    time.sleep(1)
