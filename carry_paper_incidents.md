
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
