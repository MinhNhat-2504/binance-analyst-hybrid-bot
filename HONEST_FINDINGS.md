# Kết luận điều tra: Tại sao bot chưa có lãi — và tại sao không nên tune tiếp

*Hoàn tất 2026-08-02. Mọi con số dưới đây đo bằng harness leak-free (`honest/`), purged walk-forward, sau phí, đối chiếu permutation null. File kết quả gốc được dẫn kèm từng mục.*

---

## Câu trả lời ngắn

**Bot chưa có lãi vì tín hiệu entry không chứa đủ thông tin để trả phí giao dịch — và không có cách tune nào tạo ra thông tin từ nhiễu.**

- Kỹ năng chọn lệnh thật của SHORT: **~+4.9bps** so với nhiễu (p=0.19, chưa đạt ý nghĩa thống kê)
- Phí khả thi thấp nhất (maker entry + taker stop): **~12bps**
- LONG: **+0.85bps** so với nhiễu (p=0.476) — không phân biệt được với tung đồng xu

Bất đẳng thức `4.9 < 12` là toàn bộ vấn đề. Mọi thứ khác (stop, gate, threshold, maker, horizon) là hạ nguồn của nó.

---

## Mọi cánh cửa đã được ĐO — không phải đoán

| Cửa | Kết quả tốt nhất | File | Phán quyết |
|---|---|---|---|
| Signal hiện tại (131 feature sạch, 540d, 8 folds, 20 perms) | SHORT −6.16bps, excess +4.94, p=0.19 | `honest_harness_CLEAN.json` | ĐÓNG |
| Giảm phí bằng maker (mô phỏng khớp trên range nến thật) | −1.08bps, 12/12 cấu hình âm | `execution_test_off8.json` | ĐÓNG |
| Horizon dài hơn (3h→48h, chạy trên feature CÒN rò rỉ = chặn trên) | +1.16bps @ 24h, p=0.077 (sàn perms) | `edge_sweep_horizon.log` | ĐÓNG |
| Phí rẻ hơn nữa (`maker_both` 6bps) | +4.82bps NHƯNG bất khả thi: 99.7% lệnh thoát bằng stop = bắt buộc taker | `edge_sweep_cost.json` | HƯ CẤU |
| Thông tin mới: funding rate + cross-sectional rank (ablation cùng rows/folds) | excess +4.94 → **+3.59** (delta −1.35) | `signal_v2_ablation.json` | ĐÓNG |
| Sửa exit engine (stop 0.2×ATR → hợp lý) | Thu hồi ~5bps tự gây thương tích → về ~0 | phân tích ledger | Đáng làm CHỈ KHI có edge |

**Điểm dừng có nguyên tắc:** mỗi cell sweep thêm đều ăn mòn giá trị thống kê của toàn bộ — quét đủ nhiều cell rồi giữ cell đẹp nhất *chính là* cơ chế đã tạo ra PF 2.69 giả. Các cửa trên là toàn bộ các hướng có cơ chế (mechanism) rõ ràng. Chúng đều đóng.

## Tại sao trước đây tưởng có lãi: chuỗi rò rỉ 2 tầng

**Tầng 1 — notebook cũ (`Model_Training_Lab.ipynb`), 3 lỗi critical:**
1. Lọc sample bằng kết quả tương lai (`TBM_Label != 0`) — train trên universe mà runtime không bao giờ thấy
2. Purge 24 *dòng* trên frame 10-symbol xen kẽ ≈ 36 phút, trong khi label kéo dài 3 giờ
3. `Forward_Return_20` (shift −20 = return 5h tương lai) rò vào feature list khi re-run cell

→ Sinh ra PF 2.25–2.69 giả trong `model_calibrations.json`. PF thực đo trên 4110 lệnh shadow: **0.999**.

**Tầng 2 — chính harness v1 cũng dính (bị bắt bởi kiểm toán đối nghịch 21 agent):**
1. `Taker Buy Base` (volume thô) + ~20 cột level không dừng (EMA, BB, MACD...) lọt qua bộ lọc feature → model **nhớ mặt symbol** thay vì học tín hiệu. Bỏ rò rỉ: excess SHORT +9.67 → +4.94bps. *Một nửa "kỹ năng" là memorization.*
2. Công thức effective-n sai (Kish thay vì Σw) → CI hẹp 2.5× so với sự thật
3. Purge giả định lưới nến không gap; maker sim bỏ sót stop cùng nến

Bài học vận hành: **rò rỉ không phải lỗi một lần — nó là áp lực thường trực.** Đến cả công cụ xây riêng để chống rò rỉ cũng rò. Chỉ có kiểm toán đối nghịch + permutation null mới giữ được sự trung thực.

## Khuyến nghị

### Dừng phát triển signal stack này
- **Không deploy live.** Không tune thêm threshold/gate/stop/model trên cùng nguồn tín hiệu — mọi cải thiện quan sát được từ đây gần như chắc chắn là overfit.
- Bộ artifact `xgb_v8_*`, `expert_models_v8.pkl`, `model_calibrations.json`, 11 tầng gate, ~15 file config: xây trên tín hiệu không tồn tại. Đề nghị archive.

### Giữ lại tài sản thật: `honest/`
Rig đo này tái dùng được cho **bất kỳ** chiến lược nào sau này:
- `honest/data.py` — fetch + cache klines
- `honest/features.py` — feature sạch, chặn level không dừng
- `honest/labels.py` — triple barrier net-of-cost, không filter tương lai, horizon gap-safe
- `honest/cv.py` — purged walk-forward theo wall-clock + `assert_no_leakage()` mỗi fold
- `honest/evaluate.py` — threshold trên inner-train, CI theo effective-n, **permutation null**
- `honest/execution.py` — mô phỏng maker/taker + adverse selection
- `honest/funding.py` — funding rate + cross-sectional features

Chuẩn nghiệm thu cho mọi ý tưởng mới: `python run_honest_harness.py` — **vượt null (p<0.05) VÀ CI dưới của net > 0** thì mới bàn tiếp.

### Nếu muốn thử lại — phải khác về LOẠI, không phải về độ
1. **Khung thời gian dài hơn hẳn** (daily/weekly): phí 12bps không còn chi phối, nhưng cạnh tranh với beta của thị trường
2. **Nguồn thông tin khác hẳn**: on-chain, event-driven, order-book microstructure (cần hạ tầng khác)
3. **Chấp nhận kết luận thị trường**: TA 15m trên các đồng major đã được price-in hiệu quả — kết quả âm này *nhất quán* với giả thuyết thị trường hiệu quả ở khung đó

## Điều project này đã làm được

Câu hỏi "chiến lược này có edge không?" đã được trả lời **đúng phương pháp và dứt điểm** — điều mà đa số bot retail không bao giờ đạt tới, vì backtest của họ nói dối và họ không có công cụ để biết. Bạn giờ có:
- Một rig đo lường mà backtest không lừa được
- Danh mục failure mode đã trải nghiệm thực tế (leakage, overfit gate, adverse selection, multiple testing)
- Một tiêu chuẩn nghiệm thu rõ ràng cho mọi ý tưởng tương lai

Đó là nền tảng thật. Tín hiệu thì chưa có — nhưng giờ bạn sẽ *biết ngay* khi nào nó có.

---

## Phụ lục 15/08/2026 — giả thuyết rebalance carry mỗi 8h

Một cell mới đã được đăng ký trước để kiểm tra giả thuyết funding settle mỗi 8h có thể làm daily rebalance bỏ phí thông tin. So sánh công bằng trên cùng snapshot và double holdout cho kết quả:

| 03/04→31/07, 10bps/leg | Carry 8h | Carry daily |
|---|---:|---:|
| Sharpe | 1.11 | 2.05 |
| Tổng lợi nhuận | +7.6% | +16.5% |
| Max drawdown | -11.3% | -6.4% |
| Cost drag annualized | -14.7% | -9.6% |
| HAC CI mean (bps/period) | [-4.8, +9.3] | [-9.2, +36.6] |

Turnover 8h chỉ tăng khoảng 1.53x, không phải 3x, vì rank funding-7d gần như không đổi giữa hai settlement. Tuy vậy hiệu quả vẫn thua daily rõ ràng; latency thêm một bar 8h còn làm Sharpe tăng từ 1.11 lên 1.50. Bằng chứng này đóng giả thuyết “có thông tin ở scale 8h cần rebalance nhanh để thu hoạch”. Không chọn biến thể khác, không promote, và không thay đổi route daily đang paper.

Sau report đầu, protocol chỉ được harden theo hai điểm không tune signal: thêm null block-bootstrap một phía cho mean **net sau phí**, và bắt buộc refresh/kiểm tra funding tail để không âm thầm bỏ 40 bar cuối. Null circular-shift cũ vẫn được giữ và ghi đúng nhãn là test signal-association với cost path cố định.
