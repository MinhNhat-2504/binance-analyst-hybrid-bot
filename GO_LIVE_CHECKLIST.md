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

Sàn cứng: **$2,000** — dưới mức này BTC không mua nổi lệnh tối thiểu $100 và rổ thành chiến lược khác chưa test (phát hiện 17/08).

| Vốn | Lãi kỳ vọng/năm (15%) | DD kế hoạch (−30%) | Tháng tệ nhất (−13%) |
|---|---|---|---|
| $2,000 | ~$300 | −$600 | −$260 |
| $3,000 | ~$450 | −$900 | −$390 |
| $5,000 | ~$750 | −$1,500 | −$650 |
| $10,000 | ~$1,500 | −$3,000 | −$1,300 |

Câu hỏi duy nhất cần trả lời trung thực: **cột thứ ba** — mất số đó, đời sống và tâm lý bạn có bình thường không? Chọn vốn theo cột DD, không theo cột lãi. Lưu ý lãi kỳ vọng $300/năm cho $2,000 là con số *khiêm tốn có chủ đích* — mục tiêu của giai đoạn đầu live là **chứng minh tracking với paper**, không phải kiếm tiền.

## Phần 3 — Điều kiện gate (đã cam kết từ 03/08, không đổi)

- [ ] ≥60 ngày paper (khuyến nghị 90) VÀ Sharpe > 0.5 VÀ tổng > 0 VÀ DD > −20%
- [ ] ≥20 lần testnet `COMPLETE` + reconcile exit 0, gồm ngày flip, ngày orphan, và 1 lần kill-switch giữa chừng xử lý theo runbook
- [ ] `carry_paper_incidents.md`: mọi incident đã đóng
- [ ] Signal-health canary (Bybit-funding weights) không cho thấy edge đang phai

**Nếu ngày 60 fail:** chờ tới 90. **Nếu 90 fail:** dừng, không tune. Ghi vào HONEST_FINDINGS và giữ nguyên kỷ luật đã giúp project này trung thực tới giờ.

## Phần 4 — Quy trình phát hành ceilings v2 (ngày quyết định)

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

## Phần 7 — Lộ trình tăng vốn (nếu mọi thứ sạch)

Tháng 1 live: vốn khởi điểm. Mỗi tháng sạch (tracking ≤1%/tháng, không incident mở): +50% vốn, trần tạm $10,000 cho tới khi có ≥6 tháng live. Không bao giờ tăng sau một tháng thua để "gỡ".
