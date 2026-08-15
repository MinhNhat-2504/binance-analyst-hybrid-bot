
## 2026-08-15T18:05:10+00:00 — plan_refused

```
{
  "target_id": "a58ad70191e0aa2e431ab039",
  "error": "RuntimeError: stale target: intended execution is 42.09h old (limit 6.00h)"
}
```

_Xử lý theo EXECUTION_RUNBOOK.md, ghi quyết định vào đây, rồi xóa `.execution/ATTENTION`._

**Xử lý (2026-08-16):** Không phải lỗi executor — chốt an toàn "target > 6h" hoạt động đúng. Nguyên nhân gốc là (1) exporter đọc weight *đã book* của paper (trễ 1-2 ngày so với ngày signal mới nhất) và (2) bug sổ sách trong `run_carry_paper.py`: `last_signal_day` ghi ngày *thử* thay vì ngày *đã book* sau khi `break`. Đã sửa cả hai: exporter recompute weight cho ngày signal mới nhất bằng đúng `target_weights` + mask của paper và cross-check với paper khi ngày đó đã book; paper ghi `last_signal_day` = ngày book cuối. Không PnL nào đổi. Marker đã xóa; runs tiếp tục.
