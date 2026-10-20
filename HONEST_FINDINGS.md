# Kết luận điều tra: Tại sao bot chưa có lãi — và tại sao không nên tune tiếp

*File JSON bằng chứng nằm trong `reports/`. Hoàn tất 2026-08-02. Mọi con số dưới đây đo bằng harness leak-free (`honest/`), purged walk-forward, sau phí, đối chiếu permutation null. File kết quả gốc được dẫn kèm từng mục.*

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

**Tầng 2 — chính harness v1 cũng dính (soi lại lần hai, lần này đi tìm lỗi chứ không đi xác nhận):**
1. `Taker Buy Base` (volume thô) + ~20 cột level không dừng (EMA, BB, MACD...) lọt qua bộ lọc feature → model **nhớ mặt symbol** thay vì học tín hiệu. Bỏ rò rỉ: excess SHORT +9.67 → +4.94bps. *Một nửa "kỹ năng" là memorization.*
2. Công thức effective-n sai (Kish thay vì Σw) → CI hẹp 2.5× so với sự thật
3. Purge giả định lưới nến không gap; maker sim bỏ sót stop cùng nến

Bài học vận hành: **rò rỉ không phải lỗi một lần — nó là áp lực thường trực.** Đến cả công cụ xây riêng để chống rò rỉ cũng rò. Cách duy nhất giữ được trung thực là mỗi lần đo lại phải cố tình đi tìm chỗ sai, và luôn có permutation null đứng cạnh để đối chiếu.

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

## Còn lại gì sau tất cả

Tín hiệu 15m thì không có. Nhưng câu hỏi "cái này có edge không" giờ trả lời được dứt điểm, trong khi hồi còn tin vào PF 2.69 thì không có cách nào biết.

Cụ thể là một bộ đo mà backtest không lừa được, một danh sách failure mode tự tay dẫm phải (leakage, overfit cổng, adverse selection, multiple testing), và một chuẩn nghiệm thu áp cho mọi ý tưởng sau này. Đúng cái chuẩn đó về sau loại basis carry, vol-target và cross-exchange chỉ trong ba ngày.

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

Cả hai cell đều mất đúng một ngày, vì `honest/` đã có sẵn null, hold-out, halves và cost stress. Kết quả âm rẻ như vậy thì mới đáng làm.

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

Ba họ ý tưởng trong một ngày, cả ba đóng như chiến lược mới. Đổi lại, cross-exchange trả cho CARRY-7d một bài kiểm tra độc lập đã đạt và một điểm yếu có tên. Hạn mức ≤3 họ/quý hết sạch từ đó, **không mở cell mới cho tới tháng 10.**

---

## Sổ finding (mã FA-*/R6 được trích trong code và config)

Mấy chỗ trong `honest/daily.py`, `run_carry_paper.py` và `carry_paper_config_v1.json` có trích mã finding. Ghi lại ở đây để đọc code không phải đoán:

- **FA-1 — nến ma của coin đã delist.** Binance vẫn trả kline cho symbol đã ngừng giao dịch: giá đứng im, volume 0. Không lọc thì backtest tự sinh lãi từ những ngày không thể khớp lệnh. Chặn bằng zombie guard trong `build_panel`: symbol-ngày chỉ hợp lệ khi volume > 0 **và** nằm trong khoảng có funding.
- **FA-2 / R6 — funding của lần settle 00:00 UTC thuộc về rổ nào.** Ngày t settle lúc 00:00, rebalance cũng ở 00:00. Ghi funding đó cho rổ *mới* thì luôn có lợi cho backtest một cách hệ thống, vì rổ mới được chọn dựa trên chính chuỗi funding vừa cộng. Quy tắc: settlement 00:00 thuộc về rổ giữ **trước** khi rebalance.
- **FA-3 — reindex không được forward-fill.** `fill_method=None` khi reindex panel. Mặc định pad sẽ kéo giá của ngày sống sang ngày chết, làm biến mất đúng cái mà FA-1 vừa chặn.

`carry_paper_config_v1.json` bị khóa bằng hash nên không sửa được nữa (đúng như thiết kế) — mã trích trong đó giờ tra ở mục này.

---

## Hai bộ đo có mâu thuẫn không? (25/09/2026) — không, chúng hỏi hai câu khác nhau

`honest/` nói CARRY-7d có p=0.005 trên 592 ngày. `clean_research/` nói CI 95% của các khối sau cutoff 03/04 cắt 0 và double hold-out có shift p=0.18. Đọc README cũ thì tưởng hai pipeline kết luận ngược nhau. `reconcile_evaluators.py` dựng lại chuỗi return ngày của `honest/` rồi cắt đúng mốc 03/04 và tính đúng HAC CI mà `clean_research/` dùng:

| Khối (10 bps/leg) | `honest/` cắt lại | `clean_research/` báo cáo |
|---|---|---|
| Discovery trước cutoff | +9.87 bps/ngày, CI [+2.2, +17.5], n=472 | +9.96, CI [+2.0, +17.9], n=449 |
| Discovery sau cutoff | +5.59, CI [−16.3, +27.5], n=120 | +4.97, CI [−17.0, +27.0], n=119 |
| Hold-out sau cutoff | +14.76, CI [−8.2, +37.7], n=120 | +13.66, CI [−9.2, +36.6], n=119 |

Cùng số tới sai số làm tròn. Không có mâu thuẫn về **đo lường**. Khác biệt nằm ở **câu hỏi**:

- `honest/` hỏi: Sharpe trên toàn bộ 592 ngày có tách được khỏi null hoán vị cột không? Có (p=0.005). Null đó có Sharpe trung bình −0.73 vì nó vẫn trả phí, nên bài kiểm này đo *kỹ năng chọn tên so với chọn ngẫu nhiên cùng chi phí*.
- `clean_research/` hỏi: mỗi khối 120 ngày sau cutoff, đứng riêng, có mean > 0 với CI HAC không? Không. Nhưng với độ lệch chuẩn ngày ~96 bps, một khối 120 ngày có nửa bề rộng CI kỳ vọng ~17 bps, còn edge kỳ vọng chỉ ~9 bps/ngày. **Kể cả khi edge là thật 100%, xác suất một khối 120 ngày tự chứng minh được là 18%.** Khối 592 ngày cũng chỉ đạt 65%. Muốn 80% cần ~850 ngày, mà lịch sử perp có ý nghĩa không dài đến thế.

Ba hệ quả phải nói thẳng:

1. Bằng chứng cho CARRY-7d là **t ≈ 2.3 trên ~590 ngày, lặp lại trên hai universe rời nhau**. Thật, nhưng không áp đảo. Sharpe 1.78 là ước lượng điểm; khoảng tin cậy của nó rộng.
2. Điều kiện "Sharpe > 0.5 ở ngày 60" của kỳ paper **về mặt thống kê gần như không phân biệt được gì**: 60 ngày có nửa CI ~24 bps. Paper là bài kiểm *tracking* (code chạy đúng, fill đúng, không lệch backtest), không phải bài kiểm *edge*. Cổng ngày 60/90 nên đọc đúng nghĩa đó.
3. `clean_research/` nghiêm hơn vì nó đòi từng khối tự đứng, và với chuỗi nhiễu thế này thì không khối ngắn nào đứng nổi, dù edge có thật hay không. Không sửa pipeline nào cả; chỉ sửa cách đọc.

File: `reconcile_evaluators.py`, `reports/evaluator_reconciliation.json`.

---

## Rổ CARRY-7d nghiêng về các đồng funding 4 giờ (đo 25/09/2026, phần ledger)

`measure_funding_interval.py --ledger` đọc `/fapi/v1/fundingInfo` và 52 ngày paper. Sáu đồng discovery đang settle mỗi 4 giờ (ENA, JTO, ORDI, PYTH, TAO, TIA; hold-out có thêm 7). Trong 52 ngày paper: có ít nhất một đồng 4h trong rổ **100% số ngày**, chúng chiếm **17.9% tổng số chỗ** dù chỉ là 6/42 tên (14%), và gần như chỉ ở bên short: PYTH short 33/52 ngày, ORDI 31, TAO 30, JTO 22, ENA 16, TIA 16. Long chỉ 10 lần-ngày trên cả sáu.

Đọc thẳng: Binance chuyển một hợp đồng sang 4h đúng khi funding của nó cực đoan, và xếp hạng theo tổng funding 7 ngày đưa đúng những tên đó lên đầu bên short. Không phải lỗi, không tune. Nhưng đây là nơi paper và live sẽ lệch nhau (paper giả định 3 kỳ settle/ngày ở quy tắc ranh giới funding), và là nơi một cú squeeze đánh trúng nhất. Phần backtest loại 6 tên này chạy ngày 01/10 cùng cell 1 theo lịch pre-reg.

File: `reports/funding_interval_report.json`.

---

## +8% paper là chân long thắng trong một đợt alt tăng, không phải carry đang hoạt động (25/09/2026)

`analyze_paper_attribution.py` dựng lại PnL 52 ngày paper theo từng đồng, đúng công thức của executor (tổng theo đồng khớp ledger từng ngày, sai số < 1e-5). Kết quả:

| | |
|---|---:|
| Tổng (cộng dồn số học) | +8.03% |
| Chân long | **+22.60%** (giá +22.98, funding +0.36) |
| Chân short | **−14.57%** (giá −14.16, funding +0.51) |
| Funding, cả hai chân | +0.87% ≈ 11% tổng |
| Phí | −1.66% ≈ −21% tổng |
| Top 1 (WLDUSDT, long 38/38 ngày) | 17% tổng lãi gộp |
| Top 3 (WLD, BCH, APT, đều long) | 45%; **bỏ 3 tên này: −6.38%** |
| Ngày đẹp nhất | 33.5% tổng |
| Ngày trung vị | −2.1 bps; 50% số ngày dương |
| Đồng âm | 21/39; nặng nhất ENA, DOGE, UNI, PYTH, BTC (toàn bên short) |

Đọc thẳng: trong 52 ngày này chiến lược kiếm tiền nhờ **nắm long một nhóm alt đang tăng** (WLD, BCH, APT, SEI, ARB, INJ), còn chân short — cái mà luận điểm "phe long chen chúc phải trả funding" đặt cược vào — lỗ 14.6% vì giá của nhóm crowded-long không sập mà đi ngang hoặc tăng. Funding chỉ đóng góp 0.9 điểm. Backtest 600 ngày ghi funding chiếm ~26% PnL discovery và chân giá là phần chính, nên tỉ lệ này không lạ; nhưng mức tập trung thì đáng lo: một quý mà bỏ ba tên là âm.

Không tune, không đổi gì. Ghi để đọc con số +8% cho đúng: nó là bằng chứng code chạy đúng và tracking được, **không phải** bằng chứng carry có lãi. Cùng với canary ba tuần nghiêng Bybit và phân tích power (khối 60 ngày chỉ 18% cơ hội tự chứng minh), câu hỏi ngày 90 vẫn mở như ngày đầu. File: `reports/paper_attribution.json`, `reports/paper_attribution_by_symbol.csv`.

---

## Chân short là toàn bộ edge; chân long lỗ trong 600 ngày (đo 26/09/2026)

Câu hỏi đặt ra sau phân bổ paper (long +22.6%, short −14.6% trong 52 ngày): chân nào mới thật sự kiếm tiền? `analyze_backtest_legs.py` tách PnL của chiến lược đang chạy theo hai chân trên cả hai universe, cùng cửa sổ 600 ngày và cùng phí:

| Khối | Discovery: long / short (%/năm) | Hold-out: long / short (%/năm) |
|---|---|---|
| Toàn bộ | **−7.2 / +40.0** | **−15.3 / +53.7** |
| Nửa đầu | −2.2 / +31.5 | +2.6 / +36.8 |
| Nửa sau | −14.1 / +49.1 | −31.2 / +66.4 |
| Trước 03/04/2026 | −23.3 / +59.3 | −21.0 / +55.3 |
| **Sau 03/04/2026** | **+59.1 / −38.6** | +10.7 / +43.2 |

Sharpe riêng chân short: +0.99 discovery, +1.40 hold-out, dương ở cả hai nửa. Chân long: −0.19 và −0.38. Trong chân short, funding chỉ +2.5 đến +2.9%/năm; **+44 đến +57%/năm là giá**: nhóm crowded-long thua thị trường. Đó là cơ chế thật của CARRY-7d. Chân long chỉ là hedge beta, và lịch sử cho thấy nó trả tiền để làm việc đó.

Hệ quả, nói thẳng:

1. **Tháng 9 paper lãi nhờ chân yếu, chân mạnh đang lỗ.** Từ 03/04, chân short trên discovery lỗ −38.6%/năm (giá −35%), chân long lãi +59%. Cùng hình với 52 ngày paper. Đây không phải nhiễu ngắn, nó kéo dài từ tháng 4, và trùng với canary ba tuần nghiêng Bybit. Trên hold-out thì chân short vẫn dương sau cutoff (+43%), nên chưa thể kết luận edge chết; nhưng edge trên đúng 42 tên đang paper thì đã âm gần sáu tháng.
2. **Chân long là nguồn giảm kết quả lịch sử.** Nếu chân long lỗ 7 đến 15%/năm chỉ để giữ beta trung tính, câu hỏi cho Q1/2027 là short-only + hedge BTC. Quy tắc cố định được thăm dò ngày 20/10 theo yêu cầu riêng; kết quả ở cuối tài liệu thua v1 trên cả hai tập đã lộ, nên không trở thành ứng viên thay thế.
3. **Không tune, không bỏ chân nào.** Bỏ chân long sau khi thấy nó lỗ là đúng cái repo này từng làm sai; và một rổ short-only trong đợt alt tăng như 6 tháng qua sẽ lỗ nặng hơn nhiều so với −14.6%.

File: `analyze_backtest_legs.py`, `reports/backtest_legs.json`.

## 20/10/2026 — đối soát funding toàn kỳ và độ nhạy phí của paper

Đối soát funding theo mốc settlement thật bằng `audit_paper_funding.py` đã tải
đủ và lưu cache có cửa sổ cố định cho **77/77 ngày paper** (03/08–18/10).
Chênh lệch cộng dồn do gán settlement theo trọng số cũ/mới là **−0,70 bps**;
lợi nhuận ghi sổ +10,894% thành +10,886%. Chạy lại offline cho cùng kết quả.
Đây chỉ sửa phân bổ funding theo thời điểm settle, chưa sửa giá khớp hay phí.
File: `reports/paper_funding_audit.json`.

Ảnh chụp ledger 76 ngày trước đợt đối soát ghi +10.92% ở 10 bps/chân. Giữ nguyên toàn bộ vị thế và
giá đã book, chỉ cộng thêm phí theo turnover, lợi nhuận giả định còn +8.22%
ở 20 bps, +3.02% ở 40 bps và +0.51% ở 50 bps/chân. Đây là phân tích độ nhạy
trên đúng 76 ngày đã thấy, không dự báo phí hoặc lợi nhuận tương lai.

## 19/10/2026 — LOWVOL-XS dừng ở discovery

Chạy đủ bốn ô đã đăng ký trong `RESEARCH_PREREG_Q4_2026.md` trên 42 mã discovery,
panel cache 599 ngày kết thúc 31/07/2026, phí 10 bps/chân và 200 lần null cho
mỗi ô. Hedge BTC được ước lượng bằng OLS 90 ngày, kẹp beta [0,3], thêm vào
rổ trước khi shift sang ngày giao dịch. Turnover trung bình của null nằm trong
10% turnover thật ở cả bốn ô, nên p-value có thể đọc.

| Ô | Sharpe | p-value null | alpha t so với carry | Kết luận |
|---|---:|---:|---:|---|
| Log-return 30d | 0.53 | 0.139 | 0.70 | Trượt |
| Log-return 90d | 0.40 | 0.234 | 0.39 | Trượt |
| Parkinson 30d | 0.82 | 0.060 | 0.88 | Trượt p < 0.0125, alpha > 2 và ổn định hai nửa |
| Parkinson 90d | 0.31 | 0.294 | 0.08 | Trượt |

Cả bốn phiên bản có Sharpe *sau hedge* thấp hơn bản không hedge, một điều kiện
dừng khác đã viết trước. Không có nominee nên hold-out 33 mã **không được mở**.
File: `run_lowvol_xs_lab.py`, `reports/lowvol_xs_discovery.json`.

## 20/10/2026 — SMART-VS-CROWD dừng ở cổng discovery đã đăng ký

Đã tải archive cho đủ 42 mã discovery trong đúng cửa sổ giá cố định 599 ngày
(10/12/2024–31/07/2026). Có 41 mã đạt ít nhất 400 ngày khớp và giao dịch được;
MKR bị loại theo ngưỡng này. Ngày 29/08/2025 archive chỉ có 285 thay vì 288
bản ghi 5 phút trên toàn thị trường; ngày đó để thiếu. Các ngày archive thiếu
sau khi TON ngừng giao dịch cũng để thiếu, không tự điền. Tổng cộng 24.479
ngày-mã hợp lệ. Đối chiếu archive[D] với REST[D+1] trên 40 mã còn giao dịch
ở cuối panel, 30 ngày/mã gần đây: tỷ lệ top trader, tỷ lệ tài khoản toàn thị
trường và open interest đều có **0/1.200** điểm lệch quá 1%. TON không còn
REST hiện hành nên không nằm trong phép đối chiếu gần đây; lịch sử TON vẫn chịu
quy tắc thiếu dữ liệu và zombie guard. File nguồn:
`reports/smart_vs_crowd_source_check.json`.

Chạy đúng hai cửa sổ D thô đã khóa, 7 và 30 ngày, long 20% D cao nhất, short
20% thấp nhất, 0,5 gross mỗi bên, khớp ngày kế tiếp. Phí giả định 10 bps/chân.

| Discovery | Sharpe gộp | Sharpe ròng | Lãi ròng/năm | Blend 50/50 tăng Sharpe so với carry |
|---|---:|---:|---:|---:|
| D7 | −0,63 | −0,95 | −13,00% | −0,84 |
| D30 | +0,26 | +0,13 | +1,82% | −0,31 |

Cả hai Sharpe gộp đều dưới **0,5**, đúng tiêu chí giết sớm đã đăng ký. D30
còn có nửa sau âm. RESIDUAL và CROWD-ONLY chỉ được tính mô tả trong cùng lượt
discovery (Sharpe ròng +0,32 và +0,70), **chưa có kiểm định null**, không phải
ứng viên. Dừng cell ở `STOP_CHEAP_GATE`; không chạy permutation null và
**không mở hold-out 33 mã**. Không đổi chiều tín hiệu, ngưỡng hay universe sau
khi thấy kết quả. File: `run_smart_vs_crowd_lab.py`,
`reports/smart_vs_crowd_discovery.json`.

`execution_quality.csv` cũ chỉ có 119 lệnh vì bộ dựng dùng cache 90 ngày bị
cũ và khóa trùng `(run_id,symbol,reason)` làm rơi lệnh chia nhỏ. Đã sửa để
dựng lại theo từng `order_id`, lấy open ở đúng dải ngày lịch sử, ghi file
nguyên tử và từ chối kết quả thiếu. `gate_report.py` giờ kiểm số lệnh COMPLETE
trong audit SQLite trước khi chấp nhận CSV. **338/338** lệnh hiện có giá open;
shortfall trung bình theo lệnh là **+8,99 bps** (cổng khóa ngưỡng ≤10 bps),
còn theo notional là **+9,17 bps**. Mốc mid lúc gửi lệnh cho slippage theo
notional +7,63 bps; không cộng thêm vào shortfall so với open.

Đối chiếu trực tiếp `userTrades` trên Binance Demo Futures với mọi orderId
thường trong **13 phiên COMPLETE**: khớp đủ 338/338 lệnh, 406 trades,
$16.897,62 notional. Hoa hồng ghi bằng USDT là $7,0221, **4,16 bps** theo
notional; tỷ trọng maker **0%**. Hoa hồng + shortfall so với open paper là
**13,32 bps** theo notional. Đây là phép đo sàn demo, không phải dự báo live.
File: `audit_execution_fees.py`, `reports/execution_fee_audit.json`.

Mức phí maker riêng của tài khoản demo trên toàn bộ mã đã giao dịch là 2 bps.
Ngay cả phản thực tế cực tốt là 100% notional maker ở **cùng giá** đã khớp,
chỉ tiết kiệm được 2,16 bps phí và all-in còn **11,17 bps**, vẫn vượt giả
định paper 10 bps/chân. Giá tốt hơn có thể giúp, nhưng chưa biết lệnh maker
có khớp hay không và chi phí fallback; không suy từ trần phí ra lợi nhuận.
Nhóm `close_orphan` có shortfall +17,19 bps theo notional, `rebalance`
+6,03 bps; đây là mô tả mẫu, không phải lý do tự đổi thứ tự lệnh. File:
`audit_execution_opportunity.py`, `reports/execution_opportunity.json` và
`EXECUTION_COST_EXPERIMENT.md`. Gate hiện vẫn dùng ngưỡng **shortfall theo
lệnh** đã khóa, nên PASS ở +8,99 bps; phép đo all-in 13,32 bps là cảnh báo
kinh tế bổ sung phải đọc trước quyết định vốn, không được âm thầm đổi gate.

## 20/10/2026 — SHORT + BTC hedge: thăm dò lịch sử, chưa có ứng viên

Theo yêu cầu kiểm tra sớm, đã viết quy tắc một ô trong
`RESEARCH_Q1_SHORT_BTC_PROTOCOL.md` trước khi chạy. Cả hai tập lịch sử đã
được xem phân rã hai chân, nên đây **không phải xác nhận OOS sạch**. Rổ short
20% funding cao, beta BTC OLS 90 ngày, kẹp [0,3], hedge BTC, chuẩn hóa gross
về 1, phí 10 bps/chân và lag một ngày:

| Cùng 508 ngày có beta | 42 mã discovery | 33 mã rời đã xem lịch sử |
|---|---:|---:|
| Sharpe v1 | +1,74 | +1,95 |
| Sharpe short + BTC | **+0,12** | **+1,62** |
| Lãi/năm v1 | +31,70% | +42,45% |
| Lãi/năm short + BTC | **+2,43%** | **+31,65%** |

Ứng viên thua v1 trên cả hai tập; discovery có nửa sau −8,95% và drawdown
−19,72% so với v1 −14,68%. Dừng hướng này ở quy tắc đã thử, không tìm thêm
beta window hay quy mô hedge sau khi thấy số. `explore_short_btc_hedge.py`
chỉ ghi `reports/short_btc_hedge_exploratory.json`; không tạo targets và không
đụng v1. Nếu Q1 có giả thuyết mới, phải đăng ký lại trên dữ liệu tương lai.
