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

| 03/04→14/08, 10bps/leg | Carry 8h | Carry daily |
|---|---:|---:|
| Sharpe | 0.84 | 1.85 |
| Tổng lợi nhuận | +5.9% | +15.8% |
| Max drawdown | -11.3% | -6.4% |
| Cost drag annualized | -14.7% | -9.7% |
| HAC CI mean (bps/period) | [-4.8, +8.1] | [-9.3, +32.8] |

Turnover 8h chỉ tăng khoảng 1.51x, không phải 3x, vì rank funding-7d gần như không đổi giữa hai settlement. Tuy vậy hiệu quả vẫn thua daily rõ ràng; latency thêm một bar 8h còn làm Sharpe tăng từ 0.84 lên 1.13. Double-holdout 8h có association p=0.2875 và net-profit block p=0.302; ở stress 20bps, mean chỉ còn +0.30bps/bar và net-profit p tăng lên 0.452. Bằng chứng này đóng giả thuyết “có thông tin ở scale 8h cần rebalance nhanh để thu hoạch”. Không chọn biến thể khác, không promote, và không thay đổi route daily đang paper.

Sau report đầu, protocol chỉ được harden theo hai điểm không tune signal: thêm null block-bootstrap một phía cho mean **net sau phí**, và bắt buộc refresh/kiểm tra funding tail để không âm thầm bỏ 40 bar cuối. Null circular-shift cũ vẫn được giữ và ghi đúng nhãn là test signal-association với cost path cố định.

## Hai hướng mở rộng đã đo (16/08/2026): cả hai đóng

Đo trong lúc chờ paper CARRY-7d, trên cùng harness, grid đăng ký trước, discovery 42 + hold-out 33, 600 ngày. Không đụng paper.

### Spot–perp basis carry (cash-and-carry) — ĐÓNG vì phí và regime

Long spot / short perp top-k theo funding 7d, hold H ngày; k∈{5,10}, H∈{1,3,7}; phí 4 chân 19bps/đơn vị turnover (spot 10+2, perp 5+2), stress ×1.5. File: `honest/basis.py`, `run_basis_lab.py`, `basis_lab_report.json`.

| | Funding thu được (trần, trước phí) | Phí | Kết quả tốt nhất |
|---|---|---|---|
| Discovery | +3.8…+5.2%/năm notional | 10.7…35%/năm | k10-H7: −6.9% notional, Sharpe −4.5 |
| Hold-out | +2.9…+5.8%/năm | 9…29%/năm | k10-H7: −5.6%, Sharpe −2.8 |
| Market-carry (không chọn lọc) | +2.3…+2.7% | 4.8…6.0% | âm cả hai universe |

Kết luận không cần p-value: chân funding thu được **chưa bao giờ** vượt phí 4 chân, và ROE sau chia capital (1.5×) thua cả gửi USDT 5–8%. Đây là chuyện *regime* (funding 2025–26 thấp) + *phí spot 10bps*, không phải chuyện chọn đúng đồng. Trigger để xem lại: khi funding trung bình thị trường > ~15%/năm kéo dài (kiểu 2021, đầu 2024). Bài học kỹ thuật: null hoán-vị-cột **sai** cho chiến lược một chiều (null được short cả đồng funding âm → trả funding → sụp giả); null đúng là chọn ngẫu nhiên *trong* nhóm đủ điều kiện.

### Vol-targeting overlay cho CARRY-7d — ĐÓNG vì không có timing value nhất quán

Scale gross = target_vol / vol thực 20 ngày (lag 1), floor 0.25, cap 2.0; target ∈ {10%, 15%}. So với **đòn bẩy cố định cùng gross trung bình** để tách timing khỏi leverage. File: `run_carry_voltarget.py`, `carry_voltarget_report.json`.

| | dSharpe vs const | dMaxDD | dWorst-day | Halves |
|---|---|---|---|---|
| Discovery VT-10/15 | −0.03 / −0.05 | 0 / −0.2pp | **−96 / −152bps (tệ hơn)** | h1 tốt hơn, h2 kém hơn |
| Hold-out VT-10/15 | +0.33 / +0.30 | +0.9 / +1.2pp | **−79 / −126bps (tệ hơn)** | h1 tốt hơn, h2 kém hơn |

Không nhất quán giữa universe, và **ngày tệ nhất luôn tệ hơn** ở cả 4 cell: vol-target nâng scale trong lúc yên rồi bị squeeze đập đúng lúc đang ở gross cao — failure mode kinh điển của vol-targeting cho chiến lược kiểu short-vol. Nếu muốn vol thấp hơn, đòn bẩy cố định 0.65× cho cùng Sharpe, đơn giản hơn, rẻ hơn. Không đưa vào paper-v2.

**Điểm chung của hai kết quả:** harness `honest/` giờ trả lời một ý tưởng mới trong ~1 ngày với cùng chuẩn (null + hold-out + halves + cost stress). Đó là lý do kết quả âm cũng có giá trị — chúng rẻ và dứt điểm.

### Cross-exchange (Binance vs Bybit, 577 ngày chung; OKX chỉ có ~90 ngày funding public nên bỏ) — 16/08/2026

File: `honest/crossex.py`, `run_crossex_lab.py`, `crossex_lab_report.json`.

**B. Chênh funding giữa sàn — ĐÓNG.** Trần |funding Binance − Bybit| chỉ 3.5–5.9%/năm notional, thấp hơn phí 2 chân 28bps/vòng; 8/8 cell âm cả hai universe. Hai sàn đã arbitrage funding với nhau — không còn gì để thu.

**A. Universality của CARRY-7d — kết quả quan trọng nhất hôm nay, đọc bằng bảng 2×2:**

| weight từ funding → áp lên giá | Discovery | Hold-out |
|---|---|---|
| Binance → Binance | 1.75 | 1.85 |
| **Binance → Bybit** | **1.61** | **1.70** |
| Bybit → Binance | 1.09 | −0.35 |
| Bybit → Bybit | 1.07 | −0.29 |

Funding hai sàn tương quan 0.93, chọn trùng ~57% tên, nhưng chỉ **xếp hạng theo funding Binance** mới mang tín hiệu. Hai hệ quả:
1. *Tốt:* chiến lược không phụ thuộc sàn thực thi — weight Binance áp lên giá Bybit vẫn giữ ~90% Sharpe. Giá không phải artifact; có thể chạy trên Bybit nếu cần (phí/thanh khoản khác).
2. *Điểm yếu có tên:* edge sống nhờ **chất lượng thông tin trong funding Binance** (sàn có OI alt lớn nhất → funding phản ánh crowding thật), không phải "funding nói chung". Nếu Binance đổi cơ chế funding (cap, chu kỳ 4h…) hoặc dòng tiền dịch sàn, tín hiệu có thể phai. Không phải refutation; là rủi ro cấu trúc cần theo dõi. Canary đề xuất: theo dõi định kỳ hiệu năng "Bybit-funding weights" — nếu nó bắt đầu ngang Binance, tín hiệu đang lan rộng (tốt); nếu Binance tụt về mức Bybit, tín hiệu đang phai.

**Tổng kết ngày 16/08:** 3 họ ý tưởng đo trong một ngày (basis carry, vol-target, cross-exchange), cả 3 đóng như chiến lược mới; nhưng cross-exchange cho CARRY-7d một bài kiểm tra độc lập đạt (thực thi được trên sàn khác) và một điểm yếu được gọi tên. Hạn mức ≤3 họ/quý đã dùng hết — **không mở cell mới cho tới tháng 10.**
