# Binance Analyst

Dự án phân tích và giao dịch đa tài sản trên Binance bằng ensemble ML models (XGBoost + Deep Learning + XGBoost gates). Bao gồm notebook huấn luyện, analysis dashboard, và logic bot thương mại.

## ⚡ Bắt đầu nhanh

1. **Clone repo** và tạo file `.env`:
   ```
   BINANCE_API_KEY=your_key_here
   BINANCE_API_SECRET=your_secret_here
   ```

2. **Cài dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Chạy notebook chính**:
   - `binance-analyst.ipynb` - Bot runtime & analysis (chạy end-to-end)
   - `Model_Training_Lab.ipynb` - Huấn luyện mô hình từ dữ liệu mới

## 📚 Notebook & Script

| File | Mục đích |
|------|---------|
| [binance-analyst.ipynb](binance-analyst.ipynb) | Bot runtime, load models, giao dịch & tracking |
| [Model_Training_Lab.ipynb](Model_Training_Lab.ipynb) | Huấn luyện XGBoost, LSTM, gates từ OHLCV data |
| [train_lstm.ipynb](train_lstm.ipynb) | Thử nghiệm mô hình LSTM sequence |
| [Dashboard_Analytics.ipynb](Dashboard_Analytics.ipynb) | Phân tích outcome ledger, PnL breakdown |
| [analysis.py](analysis.py) | Utility analysis functions |
| [deep_root_cause_audit.py](deep_root_cause_audit.py) | Deep dive vào root cause losses |
| [fast_feedback_audit.py](fast_feedback_audit.py) | Quick diagnostics |

## 🎯 Artifacts (git ignored)

Files sau được ignore vì kích thước lớn hoặc data động:
- **Models**: `*.keras`, `*.pkl` (LSTM, XGBoost, gates)
- **Config**: `model_calibrations.json`, `optimized_gates_v1.json`, `feature_drift_baseline.json`, `toxic_zones_blacklist.json`
- **Runtime state**: `bot_runtime_state_dual.json`, `shadow_ledger_*.csv`
- **API keys**: `.env` file

Để chạy, bạn cần **regenerate models** bằng cách chạy `Model_Training_Lab.ipynb` trên dữ liệu mới.

## ⚙️ Workflow

```
1. Chuẩn bị dữ liệu (fetch từ Binance hoặc CSV)
   ↓
2. Chạy Model_Training_Lab → generate models
   ↓
3. Chạy binance-analyst.ipynb → load models, test bot logic
   ↓
4. Phân tích outcome với Dashboard_Analytics.ipynb
```

## ⚠️ Lưu ý

- **TRADE_LIVE=False** được khuyến khích cho test ban đầu. Chỉ bật khi confident vào logic.
- Mô hình XGBoost/gates được huấn luyện trên dữ liệu lịch sử; thường xuyên retrain trên dữ liệu mới.
- Cost (fee Binance ~18 bps round-trip) đã include trong model calibration.
- Gating network phải tuning phù hợp để tránh overfitting.

## 📦 Dependencies

Xem [requirements.txt](requirements.txt) hoặc cài:
```bash
pip install numpy pandas scikit-learn xgboost tensorflow keras joblib python-binance python-dotenv feedparser
```

## 📄 Disclaimer

Dự án chỉ phục vụ mục đích **nghiên cứu & giáo dục**. Giao dịch crypto có **rủi ro cao**; bạn chịu trách nhiệm khoản lỗi khi deploy live.
