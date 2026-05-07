import pandas as pd
import time
import os
from datetime import datetime, timedelta
from binance.client import Client

# =========================================================
# 📓 BỘ MÁY HẬU KIỂM & GẮN NHÃN TỰ ĐỘNG (OUTCOME FILLER)
# =========================================================

# Khởi tạo Client Binance (Chỉ lấy Data Public nên không cần API Key cũng được)
client = Client()

FILE_PATH = "shadow_ledger_candidates.csv"
FEE_RATE = 0.0008 # Phí giao dịch 2 chiều (Taker-Taker: 0.04% x 2)

def calculate_net_pnl(entry_price, exit_price, side):
    """Tính Lãi/Lỗ ròng (Net PnL) đã trừ sạch phí sàn"""
    if side == "LONG":
        gross_pnl = (exit_price - entry_price) / entry_price
    else: # SHORT
        gross_pnl = (entry_price - exit_price) / entry_price
    return gross_pnl - FEE_RATE

def run_backfiller():
    print(f"\n[{datetime.utcnow()}] 🔄 KHỞI ĐỘNG MÁY CHẤM ĐIỂM SHADOW LEDGER...")
    
    if not os.path.exists(FILE_PATH):
        print("❌ Không tìm thấy Sổ cái Bóng đêm! Vui lòng chờ Bot chạy sinh ra file.")
        return

    # Đọc file CSV
    df = pd.read_csv(FILE_PATH)
    now = datetime.utcnow()
    updated_rows = 0

    # Lặp qua từng dòng hồ sơ
    for index, row in df.iterrows():
        try:
            trade_time = datetime.fromisoformat(row['Timestamp_UTC'].replace('Z', '+00:00'))
            symbol = row['Symbol']
            side = row['Side']
            
            # Kiểm tra xem các cột Outcome đã được điền chưa (Trống hoặc NaN)
            needs_1h = pd.isna(row['Outcome_1H_PnL']) and now >= trade_time + timedelta(hours=1)
            needs_6h = pd.isna(row['Outcome_6H_PnL']) and now >= trade_time + timedelta(hours=6)
            needs_12h = pd.isna(row['Outcome_12H_PnL']) and now >= trade_time + timedelta(hours=12)

            if needs_1h or needs_6h or needs_12h:
                # Quét Data 1H từ Binance (lấy dư ra 13 tiếng để đảm bảo đủ nến)
                start_ts = int(trade_time.timestamp() * 1000)
                end_ts = int((trade_time + timedelta(hours=13)).timestamp() * 1000)
                
                # Gọi API lấy lịch sử giá
                klines = client.futures_historical_klines(symbol, '1h', start_str=start_ts, end_str=end_ts)
                
                if len(klines) >= 2:
                    # Mức giá entry được tính bằng Giá Đóng Cửa (Close) của cây nến lúc Bot ra quyết định
                    entry_price = float(klines[0][4])
                    
                    if needs_1h and len(klines) > 1:
                        df.at[index, 'Outcome_1H_PnL'] = calculate_net_pnl(entry_price, float(klines[1][4]), side)
                    if needs_6h and len(klines) > 6:
                        df.at[index, 'Outcome_6H_PnL'] = calculate_net_pnl(entry_price, float(klines[6][4]), side)
                    if needs_12h and len(klines) > 12:
                        df.at[index, 'Outcome_12H_PnL'] = calculate_net_pnl(entry_price, float(klines[12][4]), side)
                        
                    print(f" ✅ Đã gắn nhãn thành công cho {side} {symbol} lúc {trade_time.strftime('%H:%M %d/%m')}")
                    updated_rows += 1
                
                # Tránh bị Binance Rate Limit ban IP
                time.sleep(0.2)
                
        except Exception as e:
            print(f" ⚠️ Lỗi khi xử lý dòng {index} ({row['Symbol']}): {e}")

    # Ghi đè lại file nếu có cập nhật
    if updated_rows > 0:
        df.to_csv(FILE_PATH, index=False)
        print(f"🎉 Hoàn tất! Đã chấm điểm và gắn nhãn cho {updated_rows} hồ sơ.")
    else:
        print("⚪ Chưa có hồ sơ nào đủ thời gian chín muồi để chấm điểm. Chờ đợi thêm...")

if __name__ == "__main__":
    run_backfiller()