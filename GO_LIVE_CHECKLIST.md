# Khung quyết định vốn & quy trình go-live cho CARRY-7d

Đọc và **điền** file này trước ngày 60 (~02/10/2026). Con số vốn vào `execution_ceilings` v-tiếp-theo phải được quyết ở đây, trong lúc bình tĩnh — không phải trong hưng phấn ngày gate pass hay thất vọng ngày gate fail.

## Phần 1 — Con số thật từ backtest 600 ngày (đo 24/08/2026)

| Chỉ số | Backtest | Giả định lập kế hoạch live |
|---|---|---|
| Lợi nhuận năm | +32.8% | **+15%** (chiết khấu ~50%: fill thật, phí thật, decay) |
| Sharpe | 1.78 | ~1.0 |
| Max drawdown | −14.7% | **−30%** (giả định tệ gấp đôi) |
| Tháng tệ nhất | −6.4% | −13% |
| Tỷ lệ tháng dương | 75% | ~65% |
| **Chuỗi không có đỉnh mới dài nhất** | **89 ngày** | **có thể 4–6 tháng** |
| Ngày tệ nhất | −7.6% (−755bps) | −15% |

Dòng quan trọng nhất là **89 ngày underwater**: ngay trong backtest đẹp, có gần 3 tháng liên tục tài khoản không lập đỉnh mới. Live có thể lâu hơn. Ai không chịu được điều đó về tâm lý thì mọi con số khác vô nghĩa.

## Phần 2 — Bảng vốn (điền dòng bạn chọn)

Sàn cứng **không phải hằng số** — nó là `max(minNotional / weight nhỏ nhất)` trên sàn thật, và Binance đổi filter tùy lúc: 17/08 BTC minNotional $100 → sàn ~$1,800; 03/09 minNotional đã hạ còn $50 nhưng stepSize live là 0.001 BTC, nên sàn = 0.001 × giá BTC ÷ 0.0556 và **trôi theo giá BTC** — đo hai lần trong cùng ngày 03/09 ra $1,402 rồi $1,465 (đo bằng `python check_live_filters.py`, đọc `reports/live_filters_check.json`). Chạy lại lệnh đó **đúng ngày quyết định**; số kế hoạch vẫn giữ **$2,000** cho có biên. Dưới sàn thật, BTC rớt khỏi rổ và đây thành chiến lược khác chưa test.

| Vốn | Lãi kỳ vọng/năm (15%) | DD kế hoạch (−30%) | Tháng tệ nhất (−13%) |
|---|---|---|---|
| $2,000 | ~$300 | −$600 | −$260 |
| $3,000 | ~$450 | −$900 | −$390 |
| $5,000 | ~$750 | −$1,500 | −$650 |
| $10,000 | ~$1,500 | −$3,000 | −$1,300 |

Câu hỏi duy nhất cần trả lời trung thực: **cột thứ ba** — mất số đó, đời sống và tâm lý bạn có bình thường không? Chọn vốn theo cột DD, không theo cột lãi. Lưu ý lãi kỳ vọng $300/năm cho $2,000 là con số *khiêm tốn có chủ đích* — mục tiêu của giai đoạn đầu live là **chứng minh tracking với paper**, không phải kiếm tiền.

**Nhiễu lớn hơn lãi.** Paper 30 ngày có độ lệch chuẩn ngày 0.70% → 1σ tháng ≈ 3.8% vốn. Ở $2,000: lãi kỳ vọng tháng ~$25, dao động 1σ ~$76. Nghĩa là **tháng đỏ là chuyện bình thường**, và một tháng không nói lên điều gì. Backtest ghi 89 ngày dưới đỉnh dài nhất — trong tiền thì cảm giác đúng như vậy.

## Phần 3 — Điều kiện gate (đã cam kết từ 03/08, không đổi)

- [ ] ≥60 ngày paper (khuyến nghị 90) VÀ Sharpe > 0.5 VÀ tổng > 0 VÀ DD > −20%
- [ ] ≥20 lần testnet `COMPLETE` + reconcile exit 0, gồm ngày flip, ngày orphan, và 1 lần kill-switch giữa chừng xử lý theo runbook
- [ ] `carry_paper_incidents.md`: mọi incident đã đóng
- [ ] Signal-health canary (Bybit-funding weights) không cho thấy edge đang phai

**Đo bằng máy, không bằng mắt:** sáng **02/10/2026** (sau khi paper 07:05 book ngày 60) chạy `python gate_report.py` — từng điều kiện in PASS/FAIL/NOT-YET kèm số đo và ngưỡng, kết luận GO (exit 0) / NO-GO (1) / NOT-YET (2), ghi `reports/GATE_REPORT.md`. Hai bằng chứng vẫn cần **người**: một ngày flip và **một lần cố ý bật kill-switch giữa chừng** — đã đặt lịch ở Phần 8.

**Nếu ngày 60 fail:** chờ tới 90. **Nếu 90 fail:** dừng, không tune. Ghi vào HONEST_FINDINGS và giữ nguyên kỷ luật đã giúp project này trung thực tới giờ.

## Phần 4 — Quy trình phát hành ceilings v2 (ngày quyết định)

0. **Pre-flight (không cần key), chạy đúng ngày quyết định:** `python check_live_filters.py` → yêu cầu 0 đồng trong universe ở trạng thái khác TRADING/PERPETUAL trên live (03/09: MKRUSDT và TONUSDT đang SETTLING — zombie guard của paper đã loại chúng, nhưng phải xác nhận lại), sàn vốn live ≤ ceiling định chọn, mọi khác biệt filter live-vs-demo đã đọc qua. Sau đó smoke test tài khoản live bằng key **read-only** tạo trước ≥1 tuần: futures đã bật, one-way mode, multi-asset off, 0 lệnh chờ / 0 vị thế, key không có quyền rút.
1. Điền Phần 2, chọn số vốn. Ngủ một đêm. Đọc lại.
2. Sửa `execution_ceilings_v1.json`: `live: <số đã chọn>`, thêm note ngày + lý do + link tới file này. (Đổi file = đổi hash mọi contract tương lai — commit riêng, message rõ.)
3. `pytest` — test `test_shipped_ceilings_keep_live_policy_unconstructible` sẽ **đỏ** vì live không còn 0: đó là chuông báo đúng thiết kế. Cập nhật test đó thành phiên bản khẳng định số mới. Lưu ý: script testnet không người trực **tự tắt** từ thời điểm này (đúng thiết kế).
4. Tạo API key **live** trên tài khoản Binance **riêng, trống, chỉ dành cho bot** (điều kiện bắt buộc: executor coi mọi lệnh chờ toàn tài khoản là lý do halt). Set `BINANCE_LIVE_API_KEY/SECRET` (không bỏ vào `.env.testnet` — loader cố tình không đọc live). Bật one-way mode.
5. Nạp đúng số vốn đã chọn, không hơn.

## Phần 5 — Ngày live đầu tiên (thủ công 100%, không unattended)

```
python export_carry_targets.py
python run_live_execution.py --plan --budget-usd <vốn>
# đọc từng leg. Có gì lạ -> dừng, hỏi lại.
python run_live_execution.py --release-kill-switch "go-live day 1" --authorize-budget-usd <vốn>
python run_live_execution.py --execute --budget-usd <vốn> --confirm-live I_AUTHORIZE_REAL_MONEY_ORDERS --acknowledge-max-loss <35% vốn>
python reconcile_paper_vs_testnet.py --audit .execution/live_execution.sqlite3
```

Tuần 1–2 live: chạy tay mỗi sáng, so PnL live với paper **mỗi ngày** (`analyze_execution_quality.py`). Chỉ bàn chuyện tự động hóa live sau ≥10 ngày tracking sạch.

## Phần 6 — Tiêu chí DỪNG live (viết trước, tuân theo sau)

- DD thực tế chạm **−20%** → về 0, xem lại toàn bộ. (Kế hoạch chịu được −30% nhưng dừng ở −20 để còn biên xử lý.)
- **Tracking error**: live lệch paper >1%/tuần kéo dài 3 tuần → dừng, tìm nguyên nhân (phí? fill? signal phai?).
- Signal-health canary báo Binance-edge tụt về mức Bybit → dừng nạp thêm, xem xét thoát.
- Bất kỳ `HALTED_*`/`MISMATCH` nào chưa hiểu rõ nguyên nhân → không chạy tiếp cho tới khi hiểu.
- **Bản tự động của dòng đầu:** vòng lặp không người trực có *DD guard* — trước khi đặt bất kỳ lệnh nào, nếu equity tụt ≥20% **ngân sách** so với đỉnh (lưu ở `.execution/equity_hwm_<env>.json`) thì bật kill-switch, ghi `DD_GUARD_HALT` + ATTENTION và **không rebalance**. Nó không tự thanh lý: thanh lý là quyết định của người.

## Phần 7 — Lộ trình tăng vốn (nếu mọi thứ sạch)

Tháng 1 live: vốn khởi điểm. Mỗi tháng sạch (tracking ≤1%/tháng, không incident mở): +50% vốn, trần tạm $10,000 cho tới khi có ≥6 tháng live. Không bao giờ tăng sau một tháng thua để "gỡ".

## Phần 8 — Lộ trình 04/09 → 02/10 (việc có ngày)

| Khi nào | Việc | Ai làm |
|---|---|---|
| mỗi sáng 07:05 / 07:20 | máy cắm điện, nắp mở. Cần thêm **17 lần COMPLETE trong 28 ngày** | máy tự chạy; bạn liếc `python status.py` vài ngày một lần |
| ~20/09 | mở tài khoản (hoặc sub-account) Binance **riêng cho bot**, bật futures, tạo key **read-only** trước, chưa nạp tiền | bạn |
| T7 26/09 | **diễn tập kill-switch giữa chừng** trên testnet: chạy tay, bật kill-switch lúc đang đặt lệnh, xử lý `HALTED_*` theo runbook, ghi rồi đóng incident | bạn + runbook |
| 02/10 sáng | `python gate_report.py` → đọc verdict. Nếu GO: sang Phần 4 và **ngủ một đêm** trước khi sửa ceilings | bạn |
| tháng 10 | thiết kế **live runner không người trực** (cùng cấu trúc self-release như testnet, DD guard chuyển vào engine, cờ `unattended_live` riêng). Chỉ build sau ≥10 ngày live chạy tay sạch — **tháng 10 là tháng chạy tay**, nói trước để không ảo tưởng "bật là xong" | Claude + bạn |
| tháng 10 | nghiên cứu Q4 theo `RESEARCH_PREREG_Q4_2026.md` (đã đăng ký trước, không thêm cell giữa chừng) | Claude |

Ghi trước một nguồn lệch paper-vs-live đã biết, để sau này không đổ lỗi sai: 6 đồng trong universe trả funding **mỗi 4 giờ** (TIA, ENA, JTO, PYTH, TAO, ORDI) trong khi paper giả định 3 kỳ/ngày, và PYTH/TAO/ORDI hay nằm bên short. Không phải lỗi, không tune — nó là một cell robustness trong pre-reg Q4.
