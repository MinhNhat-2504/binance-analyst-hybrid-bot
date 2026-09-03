# Đăng ký trước nghiên cứu Q4/2026 (khóa ngày 04/09/2026)

Ngân sách quý: **≤3 hướng**, đúng như kỷ luật đã ghi trong `HONEST_FINDINGS.md`. Ba cell dưới đây chọn ra từ 6 ý tưởng, chấm hai lượt cách nhau vài ngày: lượt đầu chỉ đi tìm chỗ có thể rò rỉ hoặc trùng với cái đã đóng, lượt sau chỉ hỏi một câu — cái nào thực sự đổi được P&L trong một quý. File này viết **trước khi nhìn bất kỳ kết quả nào** và không được sửa sau khi bắt đầu chạy — sửa grid, đổi ngưỡng, thêm biến thể sau khi thấy số là chính xác cái đã làm hỏng bot 15m cũ (PF 2.69 giả).

**Không cell nào được chạy trước 01/10/2026.** Tháng 9 dành cho vận hành: đủ 20 lần testnet COMPLETE và cổng ngày 60.

**Không cell nào được đụng vào CARRY-7d đang chạy.** `carry_paper_config_v1.json` khóa hash tới hết kỳ paper. Cell thắng cũng chỉ được đăng ký thành *config v2 ứng viên* với paper period riêng 60 ngày, không thay thế v1 giữa chừng.

---

## Cell 1 — CARRY-7d-HYST: dải trễ (hysteresis) trên tư cách thành viên rổ

**Không phải hướng mới, là nghiên cứu thực thi trên chiến lược đang có.** Nó chiếm slot vì đây là thứ khả năng cao nhất đổi được tiền thật trong quý.

**Vấn đề đo được:** v1 trả 10.7 pp/năm (discovery) / 11.1 pp/năm (hold-out) tiền phí ở mức 10 bps/leg, và tăng gấp đôi ở stress 20 bps. Phân tích mô tả trên panel cache 600 ngày (làm 03/09, **chưa đánh giá PnL ứng viên nào**): turnover 0.291/ngày là 99% do đảo thành viên rổ; 66% lần thoát bên short xảy ra khi tên đó vẫn nằm trong 10 phân vị quanh biên 0.80; tương quan rank ngày-qua-ngày là 0.947.

**Giả thuyết:** giữ một tên đã vào rổ cho tới khi rank vượt `0.80 − h` (short) / `0.20 + h` (long), thay vì thoát ngay khi rớt khỏi biên. Thông tin funding của tên ở phân vị 0.75 gần như bằng tên ở 0.81, nhưng vòng lặp mua-bán tốn 20 bps.

**Grid: đúng 2 điểm.** `h ∈ {0.05, 0.10}`. v1 (h=0) là mốc so sánh, không phải một cell. Bonferroni: p < 0.025/cell. **Nếu cả hai qua, lấy h=0.05** (lệch v1 ít hơn) — quy tắc phá hòa ghi trước, không được chọn cái đẹp hơn.

**Null:** (1) column-permutation chạy *qua chính bộ dựng weight có hysteresis* (giữ nguyên turnover và cost profile của luật mới); (2) **paired circular block bootstrap** khối 22 ngày, 999 lần, trên chuỗi hiệu `r_v2 − r_v1` — đây mới là kiểm định quan trọng vì hai bên dùng chung ~90% vị thế; (3) placebo "blind delay" khớp turnover: nếu không thắng nổi placebo thì dải trễ chỉ là *giao dịch ít đi*, không phải dùng thông tin rank.

**Qua cổng khi và chỉ khi tất cả:** turnover ≤ 0.75× v1 · lãi ròng năm hơn v1 ≥ +2.0 pp (discovery) và ≥ 0 (hold-out) và ≥ +4.0 pp ở stress 20 bps · Sharpe không thấp hơn v1 quá 0.05 trên cả hai universe · paired bootstrap p < 0.025 (discovery), < 0.10 (hold-out) · column null p < 0.025 cả hai · maxDD và ngày tệ nhất không xấu hơn v1 quá 1.0 pp / 100 bps · thắng placebo.

**Giết ngay khi:** ở cost = 0 mà Sharpe(h=0.05) thấp hơn v1 quá 0.15 (dải ăn mất tín hiệu nhanh hơn tiết kiệm phí) · turnover giảm < 25% ở h=0.10 · hold-out thua ở cả hai h · dấu hiệu số đảo giữa hai nửa · thua placebo.

**Ràng buộc vốn phải nói trước:** rổ sẽ phình từ 8–9 lên 10–12 tên mỗi bên, đẩy sàn vốn tối thiểu lên **~$3,000**. Ở $2,000 cell này *không triển khai được* dù có thắng. Con số đó phải vào bảng vốn của `GO_LIVE_CHECKLIST.md` nếu nó qua cổng.

**Kiểm tra rẻ nhất, ~5 phút, làm đầu tiên:** chạy v1 và h=0.10 ở cost 0 và cost 10 bps trên discovery, in turnover + lãi ròng. Nếu net(v2) ≤ net(v1) ngay ở discovery, hoặc turnover giảm < 25%, cell chết mà không tốn slot. **Cấm** chạy hold-out hoặc h=0.05 trước.

---

## Cell 2 — LOWVOL-XS: cross-section theo biến động, có hedge beta BTC

**Hướng mới #1.** Đây là cell *đa dạng hóa*: nó tồn tại để giảm phụ thuộc vào một nguồn edge duy nhất, không phải để thay carry.

**Giả thuyết:** trong các perp đã trưởng thành, nhóm biến động cao nhất mang tính "vé số", đông retail, và cho lợi nhuận điều chỉnh rủi ro *thấp hơn* nhóm biến động thấp — **sau khi bỏ beta BTC của rổ**. Long 20% dưới, short 20% trên, 0.5 gross mỗi bên, cộng một chân hedge BTC bằng `−Σ(wᵢ·βᵢ)` với β là OLS 90 ngày, kẹp [0,3].

**Hedge không phải tùy chọn.** Một rổ low-vol không hedge thì về cấu trúc là short beta, và trên mẫu 600 ngày mà BTC-HOLD có Sharpe −0.48 thì phiên bản không hedge sẽ trông "tốt" vì lý do chẳng liên quan gì đến hiện tượng đang xét.

**Grid: 4 cell cố định.** Ước lượng vol ∈ {độ lệch chuẩn log-return, Parkinson range} × cửa sổ ∈ {30d, 90d}. q=0.2, gross 0.5/bên, beta 90d, 10 bps/leg là **hằng số**, không phải trục grid. Bonferroni p < 0.0125.

**Null:** column-permutation ma trận tín hiệu vol (200 lần, seed 0) đẩy qua đúng bộ dựng weight *bao gồm cả chân hedge*. Kiểm tra bắt buộc trước khi đọc p-value: turnover của null phải nằm trong 10% turnover của chiến lược — nếu không, null đang thua vì phí chứ không phải vì thiếu kỹ năng.

**Qua cổng khi và chỉ khi tất cả:** Sharpe ròng discovery > 0.70 ở 10 bps với p < 0.0125 · hold-out (33 tên rời) Sharpe > 0.40, p < 0.05 · cả hai nửa 300 ngày dương · Sharpe > 0.35 ở stress 20 bps · **ρ với PnL ngày của CARRY-7d < 0.40** trên cả hai universe **và** alpha t-stat (HAC 10 lag) > 2.0 so với carry · blend cố định 70/30 nâng Sharpe 600 ngày ≥ +0.10 trên cả hai universe mà không làm xấu ngày tệ nhất.

**Giết khi:** phiên bản hedge thua phiên bản không hedge (nghĩa là "hiện tượng" chỉ là beta) · ρ với carry > 0.40 (thì đây là carry đội lốt: funding cao và vol cao thường là cùng một nhóm tên) · hold-out trượt · blend làm ngày tệ nhất xấu hơn.

**Kỳ vọng trung thực:** spread gộp 6–12%/năm sau hedge, trừ 2–4%/năm phí → ròng 3–8%/năm, Sharpe đứng riêng 0.3–0.6. **Xác suất qua hết cổng ~20%.** Giá trị của nó nếu qua nằm ở blend, không ở việc chạy riêng.

---

## Cell 3 — SMART-VS-CROWD-7d: phân kỳ giữa tài khoản lớn và đám đông

**Hướng mới #2.** Dùng dữ liệu Binance công bố mà funding *không nhìn thấy*.

**Giả thuyết:** Binance công bố hai thước đo dân số khác nhau — tỷ lệ long/short theo **đầu người** (retail chiếm đa số) và tỷ lệ long/short theo **vị thế của nhóm 20% tài khoản ký quỹ lớn nhất** (trọng số theo notional). Funding chỉ thấy giá thanh toán của mất cân bằng tổng; nó không thấy *ai* đứng bên nào. Tín hiệu `D = trung bình 7 ngày của [ln(top_trader_ratio) − ln(account_ratio)]`. Long nhóm 20% D cao nhất, short nhóm thấp nhất.

**Dữ liệu:** kho lưu trữ miễn phí `data.binance.vision/data/futures/um/daily/metrics/` — mỗi ngày một file CSV 288 dòng 5 phút, **đã xác minh 03/09 là không thiếu ngày** từ 2021-12-01 (hoặc ngày niêm yết) tới 2026-09-02. Ba tỷ lệ này **chỉ Binance có** ở dạng này — trùng khớp với phát hiện 16/08 rằng edge nằm trong thông tin của Binance. `collect_daily_snapshots.py` (chạy từ 04/09) không đóng góp lịch sử ở đây; vai trò của nó là **bằng chứng hằng ngày rằng REST khớp kho lưu trữ**, để chuỗi sau này nối liền được.

**Grid: 4 cell cố định.** D làm mượt 7d · D làm mượt 30d · **RESIDUAL** (rank của D hồi quy lên rank funding-7d mỗi ngày, dựng weight từ phần dư) · **CROWD-ONLY** (chỉ rank đám đông, fade retail) làm đối chứng quy kết. Bonferroni p < 0.0125.

**Null:** column-permutation trên ma trận D; với RESIDUAL thì hoán vị cột thô *rồi mới* hồi quy lại lên rank funding thật trong từng lần hoán vị (giữ điều kiện hóa funding, chỉ phá phần D). Cộng một null đặc thù: **hoán vị cặp (top, crowd)** — chỉ xáo cột top-trader, giữ cột đám đông. Chiến lược sống sót null này mới thật sự đang khai thác *phân kỳ*; không thì nó chỉ là fade đám đông, mà cái đó funding đã định giá rồi.

**Qua cổng khi và chỉ khi tất cả:** cell thô Sharpe > 1.0, p < 0.01, p_net < 0.05 · **RESIDUAL Sharpe > 0.7, p < 0.05** (không có điều này thì đây là funding đội tên khác, CARRY-7d đã sở hữu) · null cặp p < 0.05 · hold-out > 0.7 (thô) và > 0.5 (residual) · hai nửa đều dương · stress 20 bps net > 0 · blend 50/50 với carry nâng Sharpe ≥ +0.20 trên cả hai universe.

**Giết ngay khi:** Sharpe gộp (cost 0) < 0.5 · tương quan Spearman rank(D) với rank(funding-7d) > 0.6 · tương quan rank giữa hai tỷ lệ > 0.9 (không còn phân kỳ nào để giao dịch) · lệch kho lưu trữ so với REST > 1% trên > 5% số ngày-tên · Binance ngừng công bố → chiến lược mất tín hiệu, **cấm** thay bằng proxy nếu không đăng ký lại.

**Nếu CROWD-ONLY qua mà các cell phân kỳ trượt:** ghi "fade đám đông trùng với funding", đóng, không đăng ký mới.

---

## Những gì đã cân nhắc và **không** chọn

| Đề xuất | Điểm | Lý do loại |
|---|---|---|
| OI-BUILD-7d (fade tăng open interest) | 22 | Tín hiệu chu kỳ đòn bẩy có thật nhưng nhỏ, bị 13–18%/năm turnover ăn hết — đúng câu chuyện bot 15m ở tốc độ ngày |
| LISTING-AGE (trôi giá sau niêm yết) | 22 | Không triển khai được trên engine hiện tại: universe động, không có chặn lỗ theo tên, đuôi squeeze có thể vượt 100% notional |
| CARRY-7d-T08/T16 (đổi giờ rebalance) | 21 | Kỳ vọng ~0 theo thiết kế; qua cổng cũng chỉ mua thêm rủi ro vận hành ở giờ xấu hơn |

## Ràng buộc chung cho cả ba cell

1. **Không cell nào được chạy trước 01/10/2026.**
2. Discovery = 42 tên `run_daily_lab.UNIVERSE`; hold-out = 33 tên rời `run_carry_holdout.HOLDOUT_UNIVERSE`, **chạy đúng một lần**, sau khi kết luận discovery đã viết ra.
3. Cửa sổ 600 ngày cố định, min 400 ngày, zombie guard như hiện tại.
4. Mọi kết quả — kể cả và **đặc biệt là** kết quả âm — ghi vào `HONEST_FINDINGS.md` kèm file JSON trong `reports/`.
5. Không cell nào được sửa `carry_paper_config_v1.json`, ceilings, hay lịch chạy. Cell thắng đi vào paper period riêng của nó.
6. Sửa grid/ngưỡng/universe sau khi thấy kết quả = **hủy cell**, không phải "tinh chỉnh".

*Một ghi chú robustness ngoài ngân sách 3 hướng (không tốn slot vì nó kiểm tra chiến lược đang chạy, không tìm cái mới): 6 đồng trong universe trả funding mỗi 4 giờ (TIA, ENA, JTO, PYTH, TAO, ORDI) trong khi paper giả định 3 kỳ/ngày, và xếp hạng theo tổng funding 7 ngày về mặt cơ học ưu ái nhóm 4h. Câu hỏi: CARRY-7d có sống sót khi loại nhóm 4h không? Chạy cùng lúc với cell 1.*
