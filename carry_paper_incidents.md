
## 2026-08-15T18:05:10+00:00 — plan_refused

```
{
  "target_id": "a58ad70191e0aa2e431ab039",
  "error": "RuntimeError: stale target: intended execution is 42.09h old (limit 6.00h)"
}
```

_Xử lý theo EXECUTION_RUNBOOK.md, ghi quyết định vào đây, rồi xóa `.execution/ATTENTION`._

**Xử lý (2026-08-16):** Không phải lỗi executor — chốt an toàn "target > 6h" hoạt động đúng. Nguyên nhân gốc là (1) exporter đọc weight *đã book* của paper (trễ 1-2 ngày so với ngày signal mới nhất) và (2) bug sổ sách trong `run_carry_paper.py`: `last_signal_day` ghi ngày *thử* thay vì ngày *đã book* sau khi `break`. Đã sửa cả hai: exporter recompute weight cho ngày signal mới nhất bằng đúng `target_weights` + mask của paper và cross-check với paper khi ngày đó đã book; paper ghi `last_signal_day` = ngày book cuối. Không PnL nào đổi. Marker đã xóa; runs tiếp tục.

## 2026-08-17T00:21:15+00:00 — run_needs_review

```
{
  "target_id": "e6bedfe4c489276a7e64551d",
  "engine_status": "FAILED",
  "engine_detail": "incomplete target; refuse partial portfolio: BTCUSDT:below_min_notional",
  "reconcile_exit": 0,
  "reconcile_tail": "No execution run exists for frozen target e6bedfe4c489276a7e64551d.\n"
}
```

_Xử lý theo EXECUTION_RUNBOOK.md, ghi quyết định vào đây, rồi xóa `.execution/ATTENTION`._

**Xử lý (2026-08-24):** `run_needs_review` 17/08 = engine từ chối cả danh mục vì BTC $27.8 < min-notional $100 tại budget $500 — 0 lệnh đặt, hành vi đúng. Phát hiện: vốn tối thiểu đủ rổ ~$1,800. Ceiling testnet nâng 500→2000 (reviewed, chưa có COMPLETE contract nào trước đó nên không vỡ reconcile). Marker đã xóa; runs tiếp tục từ 25/08 07:20. Lỗ hổng phụ: ATTENTION chặn runs cả tuần vì không ai xem — đúng thiết kế fail-safe, nhưng ghi nhận cần thói quen liếc `.execution/`.

## 2026-09-03T12:10:00+00:00 — stale_lock_and_empty_attention (01/09 10:22 local)

```
{
  "lock": {"pid": 27844, "started_utc": "2026-09-01T03:22:11+00:00"},
  "attention_marker": "0 bytes, same timestamp as the lock",
  "audit_db": "no run row on 2026-09-01 (killed before the engine started)",
  "task_scheduler": "run-if-missed fired at 10:22 after a wake; exit 0xC000013A on the paper task 31/08 shows the same signature",
  "exchange_check_2026-09-03": {"open_orders": 0, "positions": 17, "gross_usd": 2008.1, "reconcile_exit": 0}
}
```

**Xử lý (2026-09-03):** Tiến trình bị kill giữa chừng (mã 0xC000013A = CTRL_C/CTRL_CLOSE) ngay khi crash-guard đang ghi marker → marker rỗng, lock không được nhả, engine chưa kịp chạy nên sàn không bị đụng (0 lệnh mở, reconcile 0 so với contract 27/08). Nguyên nhân khả dĩ nhất: task chạy ở chế độ tương tác nên mỗi lần chạy bật một cửa sổ console; đóng cửa sổ đó = CTRL_CLOSE → Python nhận KeyboardInterrupt. Sửa: task chạy ẩn qua `run_hidden.vbs` (không còn cửa sổ để đóng); crash-guard ghi marker atomic (temp+rename) để không bao giờ để lại marker rỗng. Marker và lock đã xóa; runs tiếp tục từ 04/09 07:20. Hệ quả: 28/08–03/09 không có run testnet (7 ngày), 3/20 COMPLETE giữ nguyên.
