# Runbook vận hành executor CARRY-7d

Đọc file này **trước** khi chạy `--execute` lần đầu, và đọc lại **ngay** khi thấy một status không phải `COMPLETE`.

Nguyên tắc duy nhất cần nhớ: engine chỉ tự thanh lý (flatten) khi **vị thế thật sự sai so với contract**. Mọi bất thường khác — hết hạn kill-switch, mất market data, lệnh chờ lạ, slippage, lỗi ghi audit — nó **dừng và giao book cho bạn** thay vì bán tháo. Nghĩa là: một số status dưới đây **để lại vị thế lệch trên sàn có chủ đích**, và người phải quyết định tiếp là bạn.

## Điều kiện tiên quyết

- **Tài khoản testnet riêng**, không dùng chung với thứ gì khác. Engine coi *mọi* lệnh chờ trên toàn tài khoản là lý do halt (đúng thiết kế) — tài khoản dùng chung sẽ halt liên tục.
- Chế độ **one-way** (`--set-one-way-mode` một lần). Hedge mode bị từ chối.
- Chỉ đặt `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET`. Không đặt `BINANCE_LIVE_*` cho tới khi có ceilings v2.

## Chu trình một ngày bình thường

```
python export_carry_targets.py                       # weight hôm nay từ paper state
python run_testnet_execution.py --plan               # xem legs / skips / drift TRƯỚC khi mở khóa
python run_testnet_execution.py --release-kill-switch "rehearsal 2026-08-16" --authorize-budget-usd 500
python run_testnet_execution.py --execute --budget-usd 500 --confirm-testnet I_ACCEPT_TESTNET_ORDERS
python reconcile_paper_vs_testnet.py                 # exit 0 = khớp contract; 2 = lệch; 3 = có hand-off state
```

Kill-switch tự **bật lại** sau mỗi run thành công. Release hết hạn sau 15 phút và bị bind vào đúng `target_id` + budget — release cho ngày hôm qua không dùng được cho hôm nay.

`--plan` chạy đủ drift gate, min-notional, orphan close với quote thật nhưng **không đặt lệnh, không cancel, không cần release**. Nếu `--plan` báo abort thì `--execute` cũng sẽ abort — sửa nguyên nhân trước.

## Đọc status — và làm gì

| Status | Vị thế trên sàn | Nghĩa là | Việc của bạn |
|---|---|---|---|
| `COMPLETE` | = contract, đã verify | Ngày bình thường | Chạy reconcile; xong |
| `DRY_RUN` | không đổi | `--plan` | Không có gì |
| `FAILED` (trước khi có lệnh) | không đổi | Abort ở giai đoạn plan: drift, thiếu reference, target cũ, budget không khớp release, hedge mode… | Đọc message, sửa nguyên nhân, chạy lại. Không có gì trên sàn để dọn |
| **`HALTED_MID_BOOK`** | **một phần book, lệch** | Đang đặt lệnh thì: kill-switch hết hạn / bị bật tay, market data mất trước POST, lệnh chờ lạ xuất hiện, slippage vượt ngưỡng, hoặc không ghi được kill-switch sau khi verify | **Xem mục "Được giao book lệch"** |
| `HALTED_AUDIT_UNAVAILABLE` | như trên | Như trên nhưng do sqlite từ chối ghi. Terminal state nằm trong `.execution/testnet_execution.sqlite3.sidecar.jsonl` | Đọc sidecar → mục "Được giao book lệch" |
| `HALTED_CANCEL_FAILED` | lệch **và có thể còn lệnh chờ** | Halt, và cancel lệnh chờ cũng thất bại | Vào sàn **hủy lệnh chờ bằng tay trước**, rồi mục "Được giao book lệch" |
| `EXTERNAL_POSITION_DRIFT` | book của mình đúng, một symbol dust bị ADL/liquidation ngoài | Không phải lỗi mình; engine chỉ cancel lệnh chờ | Xem symbol drift trong audit; thường không cần làm gì; ghi chú |
| `VERIFICATION_UNAVAILABLE` | có thể đúng, **chưa verify được** | Quote timeout / quote = 0 sau khi fill xong | Chạy reconcile với `--run-id`; nếu chưa có `after_orders` thì **so tay** với `execution_contract` trong audit |
| **`MISMATCH`** | **đã flatten** (nếu flatten thành công) | Vị thế thật sự sai contract → engine đã thanh lý | Xem `target_verification` để biết symbol nào sai; điều tra fill; **không** chạy lại cho tới khi hiểu |
| **`UNRESOLVED_EXPOSURE`** | **lệch, flatten thất bại** | Muốn thanh lý mà sàn không cho (reject/lỗi) | **Khẩn**: vào sàn đóng tay theo `emergency_flatten_unresolved` snapshot |
| `INTERRUPTED` | tùy thời điểm | Ctrl+C trước khi có lệnh | Như `FAILED` |

## Được giao book lệch (`HALTED_*`)

Bạn đang cầm một danh mục market-neutral **chưa hoàn thành** — có thể net long hoặc net short. Đây không phải khẩn cấp cấp giây (không có lệnh chờ nếu status không phải `CANCEL_FAILED`), nhưng cũng không được để qua đêm mà không quyết định.

1. **Xem chính xác đang giữ gì**: `python reconcile_paper_vs_testnet.py` — exit 3 và in `positions_held_at_handoff`. Nếu là `HALTED_AUDIT_UNAVAILABLE`, đọc dòng cuối `*.sidecar.jsonl`. Đối chiếu với vị thế trên sàn — sidecar/audit là ghi nhận, sàn là sự thật.
2. **Đọc message** trong audit `execution_runs.message` để biết *tại sao* halt. Ba nhóm:
   - *Bạn/TTL bật kill-switch*: chủ ý. Quyết định ở bước 3.
   - *Market data / slippage*: thị trường xấu lúc đó. Chờ ổn rồi bước 3.
   - *Lệnh chờ lạ*: **ai đặt?** Nếu không phải bạn → tài khoản đang bị dùng chung hoặc có process khác — dừng mọi thứ, tìm nguồn.
3. **Chọn một trong hai, không có lựa chọn thứ ba**:
   - **Hoàn thành**: release lại (target_id + budget y hệt) và `--execute` lại **cùng target**. Engine đọc vị thế hiện tại và chỉ đặt phần còn thiếu — không đặt lại phần đã có. Đây là đường mặc định nếu nguyên nhân đã hết.
   - **Đóng hết**: nếu không tin được trạng thái, không hiểu nguyên nhân, hoặc target đã quá cũ (>6h) → đóng tay trên sàn (reduce-only, market), rồi ghi lại vào audit bằng tay/ghi chú. Ngày mai chạy target mới từ đầu.
   - **Không được**: để nguyên và "xem sao". Book lệch = đang beta thị trường, không phải chiến lược.
4. **Ghi lại** vào `carry_paper_incidents.md` (tạo nếu chưa có): ngày, status, nguyên nhân, quyết định. 60 ngày paper cần record này để biết executor có đáng tin không.

## Những thứ engine cố tình KHÔNG làm

- Không tự chạy lại sau halt. Mọi lần chạy đều cần release mới.
- Không tự nâng budget. `--budget-usd` chỉ được **≤** ceiling trong `execution_ceilings_v1.json` (hiện testnet 500, live **0**). Đổi file đó = đổi hash mọi contract tương lai = sự kiện có review, không phải knob.
- Không tự quyết "lỗi nhỏ thì kệ". Mọi status không phải COMPLETE đều cần bạn đọc.

## Trước khi bàn chuyện live

Không phải checklist code. Checklist **record**:
- ≥60 ngày paper qua gate trong `carry_paper_config_v1.json`.
- ≥20 lần testnet `COMPLETE` + reconcile exit 0, gồm ít nhất một ngày có flip, một ngày có orphan close, và **một lần bạn cố tình bật kill-switch giữa chừng** rồi xử lý theo mục trên mà không hoảng.
- File `carry_paper_incidents.md` có nội dung và mọi incident đều đóng.
- `execution_ceilings_v2.json` với số live **có review**, không phải copy số testnet.
