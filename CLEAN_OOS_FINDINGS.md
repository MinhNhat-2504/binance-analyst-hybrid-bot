# Kết quả kiểm tra lợi nhuận với ledger và OOS sạch

Ngày khóa dữ liệu: 01/08/2026 UTC. Dữ liệu daily hoàn tất đến 31/07/2026, gồm 75
USDT-M perpetual symbols. Live trading không được bật hoặc thay đổi bởi nghiên cứu này.

## Kết luận

Chưa có đủ bằng chứng để kết luận dự án đã có lợi nhuận bền vững sau chi phí.

- Model Ridge huấn luyện lại bằng decision ledger sạch đã thất bại trên OOS.
- CARRY-7d có PnL replay dương sau phí, kể cả phí stress và entry chậm thêm một daily
  bar, nhưng CI 95% của các khối sau cutoff đều cắt 0 và double-holdout không thắng null.
- CARRY-7d chỉ được giữ ở `PAPER_SHADOW_ONLY`; `live_enabled=false`, vốn được cấp bằng 0.

## Nguyên nhân ledger cũ tạo cảm giác có lãi

`shadow_ledger_candidates_v4.csv` không phải tập huấn luyện alpha hợp lệ: nó là runtime
decision log ngắn, thiếu raw causal features và chứa nhiều outcome/execution columns.

| Cách tính | Số lệnh | Mean | Profit factor |
|---|---:|---:|---:|
| Raw paper rows | 130 | +3.35 bps/lệnh | 1.141 |
| Dedupe theo 15m + symbol + side | 127 | +3.75 bps/lệnh | 1.158 |
| Chỉ một vị thế mở mỗi symbol/side | 60 | **-4.70 bps/lệnh** | **0.803** |

Phần lợi nhuận raw chủ yếu đến từ các entry/label chồng lấn và chỉ phủ 5 ngày paper.
Khi đổi sang view có thể thực thi, tổng return là -2.82%.

## Model huấn luyện lại

Decision ledger mới dùng 34 feature allowlisted, quyết định sau daily close, vào từ open
kế tiếp, thoát sau một ngày, funding chỉ lấy settlement đúng khoảng giữ lệnh. Model chỉ
được chọn trên discovery symbols trước cutoff 03/04/2026; nhãn train bị purge khỏi mỗi
validation fold.

Ridge được chọn (`full_schema_ridge_a1000`) có median validation +9.94 bps/ngày, nhưng
không chuyển sang dữ liệu ngoài development:

| Partition, phí 10 bps/leg | Net bps/ngày | PF | Sharpe | Max DD |
|---|---:|---:|---:|---:|
| Symbol holdout trước cutoff | -11.56 | 0.782 | -1.68 | -47.32% |
| Discovery sau cutoff | **-31.94** | 0.567 | -3.23 | -35.46% |
| Symbol + time double holdout | **-15.46** | 0.732 | -2.18 | -26.61% |

Đây là dấu hiệu rõ của feature/model edge phụ thuộc universe và regime: validation trong
development nhìn tốt nhưng quan hệ learned không ổn định ngoài mẫu. Model artifact được
lưu để audit với stage `RESEARCH_ONLY`, không được nối vào bot.

## Nguồn tín hiệu mới: funding crowding CARRY-7d

Rule cố định: long tail 20% funding 7 ngày thấp, short tail funding cao; gross 1.0,
notional net 0; phí mở/đóng tính theo turnover và có terminal liquidation.

| Partition | Net @10 bps/leg | HAC 95% CI | Shift p | Net @20 bps/leg |
|---|---:|---:|---:|---:|
| Discovery trước cutoff | +9.96 bps/ngày | [+1.99, +17.93] | 0.0024 | +6.97 |
| Symbol holdout trước cutoff | +8.87 | [-0.32, +18.07] | 0.0545 | +5.68 |
| Discovery sau cutoff | +4.97 | [-17.05, +26.99] | 0.0323 | +2.13 |
| Double holdout sau cutoff | +13.66 | [-9.24, +36.56] | 0.1828 | +11.03 |

Entry chậm thêm một daily bar vẫn dương: +5.33 bps/ngày trên discovery-time replay và
+14.41 bps/ngày trên double holdout tại 10 bps/leg. Tuy nhiên các CI vẫn cắt 0; kết quả
dương chưa tách được khỏi regime chung ở double holdout.

Giai đoạn sau cutoff là OOS sạch về code/split, nhưng không còn là researcher-unseen:
carry và toàn lịch sử này đã được xem trong các thử nghiệm trước. Vì vậy chỉ dữ liệu paper
mới phát sinh sau lần khóa này mới là temporal confirmation thật.

Universe cũng được dựng từ các contract còn tồn tại ở thời điểm hiện tại, nên vẫn còn
survivorship bias; kết quả không đại diện đầy đủ cho các contract đã delist/thất bại.

## Những thay đổi an toàn đã áp dụng

- Feature schema versioned, deny-by-default; outcome/exit/legacy score không thể lọt vào X.
- Unique immutable decision IDs và snapshot hash.
- Next-open labels, funding coverage, strict settlement endpoints và kiểm tra daily gaps.
- Full outcome matrix, ngày inactive, phí entry/liquidation và gross/net constraints.
- Exact unique circular shifts thay cho permutation lặp giả độ phân giải.
- Walk-forward training theo timestamp với label-availability purge.
- Disjoint-symbol, later-time và double holdout; base/stress cost và delayed-entry replay.

## Gate tiếp theo

Giữ CARRY-7d ở paper shadow tối thiểu 60 ngày lịch và 40 ngày active, không đổi feature,
universe hay cost contract. Chỉ review live lại khi CI HAC 95% lower bound > 0, PnL vẫn
dương tại 20 bps/leg, shift p < 0.05 cho route đã khóa, max drawdown < 25%, và fill paper
xác nhận latency/spread/slippage thực tế.

Chạy lại:

```powershell
python -B -m pytest -q
python -B run_clean_oos.py --n-perm 999
```
