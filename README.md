# Binance Analyst Hybrid Bot

A multi-asset crypto analysis and trading-bot project focused on Binance data.

This project combines:
- feature engineering on OHLCV market data
- dual XGBoost classifiers (LONG and SHORT)
- Optuna hyperparameter tuning
- Autoencoder-based anomaly kill-switch
- live bot logic with sentiment and order-book gatekeepers
- portfolio-level meta-agent correlation risk filter

## Repository Contents

- `binance-analyst.ipynb`: Main end-to-end notebook (data -> training -> tuning -> autoencoder -> live bot loop)
- `live_bot.py`: Script form of bot runtime logic
- `xgb_v8_long_fee_aware_multi.pkl`: Trained LONG model artifact
- `xgb_v8_short_fee_aware_multi.pkl`: Trained SHORT model artifact
- `xgb_v8_meta.pkl`: Metadata for model features and lag settings
- `autoencoder_killswitch.keras`: Autoencoder model artifact
- `scaler_ae.pkl`, `ae_meta.pkl`: Autoencoder scaler and threshold metadata
- `bot_runtime_state.json`, `bot_runtime_state_dual.json`: Runtime state snapshots
- `nhat_ky_trade_hybrid.txt`: Runtime log file

## Main Pipeline

1. Fetch and process 1h data for multiple symbols.
2. Build technical and regime features.
3. Generate fee-aware dual labels (LONG and SHORT).
4. Train XGBoost dual models.
5. (Optional) Auto-tune model hyperparameters with Optuna.
6. Train Autoencoder for anomaly detection kill-switch.
7. Run live bot with:
   - setup filters
   - sentiment gatekeeper
   - order-book gatekeeper
   - correlation-aware meta-agent budget control

## Requirements

Python 3.10+ recommended.

Install dependencies:

```bash
pip install numpy pandas requests schedule feedparser joblib scikit-learn xgboost optuna tensorflow python-binance
```

## How To Run

### Option 1: Notebook

Open `binance-analyst.ipynb` and run cells in order:
- data + features
- model training
- optional auto-tuning
- autoencoder training
- bot runtime

### Option 2: Script

Run:

```bash
python live_bot.py
```

## Notes

- The bot is currently configured for Binance `testnet=True` in the provided code.
- Review risk parameters (`BUY_PROBA_THRESHOLD`, stop-loss/take-profit/trailing values) before live use.
- Keep credentials secure even in private repositories.

## Disclaimer

This software is for research and educational use. Trading crypto involves risk. Use at your own responsibility.
