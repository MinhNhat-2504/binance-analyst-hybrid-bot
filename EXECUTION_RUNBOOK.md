# Runbook vận hành executor CARRY-7d

Đọc file này **trước** khi chạy `--execute` lần đầu, và đọc lại **ngay** khi thấy một status không phải `COMPLETE`.

Nguyên tắc duy nhất cần nhớ: engine chỉ tự thanh lý (flatten) khi **vị thế thật sự sai so với contract**. Mọi bất thường khác — hết hạn kill-switch, mất market data, lệnh chờ lạ, slippage, lỗi ghi audit — nó **dừng và giao book cho bạn** thay vì bán tháo. Nghĩa là: một số status dưới đây **để lại vị thế lệch trên sàn có chủ đích**, và người phải quyết định tiếp là bạn.

## Điều kiện tiên quyết

- **Tài khoản testnet riêng**, không dùng chung với thứ gì khác. Engine coi *mọi* lệnh chờ trên toàn tài khoản là lý do halt (đúng thiết kế) — tài khoản dùng chung sẽ halt liên tục.
- Chế độ **one-way** (`--set-one-way-mode` một lần). Hedge mode bị từ chối.
- Chỉ đặt `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET`. Không đặt `BINANCE_LIVE_*` cho tới khi có ceilings v2.

## Chế độ tự động (khuyến nghị cho 60 ngày paper)

`run_carry_testnet_daily.py` (task `carry_testnet_task.bat`, chạy sau task paper) làm trọn chu trình dưới đây **không cần bạn**: export → plan → tự release kill-switch cho đúng target hôm nay → execute → tự engage lại → reconcile. Ngày bình thường: im lặng, một dòng vào `carry_testnet_log.csv`. Bất kỳ điều gì khác: tạo file **`.execution/ATTENTION`** + ghi `carry_paper_incidents.md` + **từ chối chạy ngày hôm sau** cho tới khi bạn xử lý theo bảng status bên dưới và xóa marker. Việc của bạn: thỉnh thoảng xem thư mục `.execution/` có file `ATTENTION` không. Không có = mọi thứ ổn.

Nó **tự tắt** ngày `execution_ceilings` khai live > 0 — không cần nhớ tắt khi lên live.

## Chu trình một ngày bình thường (chạy tay, nếu muốn)

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
| `FAILED` | **tùy** — xem `orders_started` trong audit/sidecar | Hoặc abort ở giai đoạn plan (drift, thiếu reference, target cũ, budget không khớp, hedge mode) → không có gì trên sàn; **hoặc** lỗi sau khi có lệnh mà engine đã flatten thành công → sàn flat nhưng bạn đã trả phí một vòng | Đọc message. Nếu có snapshot `emergency_flatten_verified` → đã thanh lý, điều tra nguyên nhân trước khi chạy lại. Nếu không → chỉ là abort plan, sửa rồi chạy lại |
| `RUNNING` (không đổi sau nhiều phút) | **không rõ** | Process chết giữa chừng (mất điện, taskkill). Reconciler coi đây là exposure | Vào sàn xem thật; xử lý như `HALTED_MID_BOOK` |
| **`HALTED_MID_BOOK`** | **một phần book, lệch** | Đang đặt lệnh thì: kill-switch hết hạn / bị bật tay, market data mất trước POST, lệnh chờ lạ xuất hiện, slippage vượt ngưỡng, hoặc không ghi được kill-switch sau khi verify | **Xem mục "Được giao book lệch"** |
| `HALTED_AUDIT_UNAVAILABLE` | như trên | Như trên nhưng do sqlite từ chối ghi giữa chừng. Nếu DB hồi lại kịp lúc ghi kết thúc thì row status có trong DB; nếu không, terminal state + book đang giữ nằm trong `.execution/testnet_execution.sqlite3.sidecar.jsonl` (dòng cuối). Reconciler tự báo file này nếu tồn tại | Đọc DB **rồi** sidecar → mục "Được giao book lệch" |
| `EXTERNAL_DRIFT_CANCEL_FAILED` | book mình đúng, dust bị ADL, **và có thể còn lệnh chờ** | Như `EXTERNAL_POSITION_DRIFT` nhưng cancel lệnh chờ thất bại | Vào sàn **hủy lệnh chờ tay**; sau đó thường không cần gì thêm |
| `HALTED_CANCEL_FAILED` | lệch **và có thể còn lệnh chờ** | Halt, và cancel lệnh chờ cũng thất bại | Vào sàn **hủy lệnh chờ bằng tay trước**, rồi mục "Được giao book lệch" |
| `EXTERNAL_POSITION_DRIFT` | book của mình đúng, một symbol dust bị ADL/liquidation ngoài | Không phải lỗi mình; engine chỉ cancel lệnh chờ | Xem symbol drift trong audit; thường không cần làm gì; ghi chú |
| `VERIFICATION_UNAVAILABLE` | có thể đúng, **chưa verify được** | Quote timeout / quote = 0 sau khi fill xong | Chạy reconcile với `--run-id`; nếu chưa có `after_orders` thì **so tay** với `execution_contract` trong audit |
| **`MISMATCH`** | **đã flatten** (nếu flatten thành công) | Vị thế thật sự sai contract → engine đã thanh lý | Xem `target_verification` để biết symbol nào sai; điều tra fill; **không** chạy lại cho tới khi hiểu |
| **`UNRESOLVED_EXPOSURE`** | **lệch, flatten thất bại** | Muốn thanh lý mà sàn không cho (reject/lỗi) | **Khẩn**: vào sàn đóng tay theo `emergency_flatten_unresolved` snapshot |
| `INTERRUPTED` | tùy thời điểm | Ctrl+C. Trước khi có lệnh → không đổi. Sau khi có lệnh → engine đã cố flatten (xem snapshot `emergency_flatten_*`) | Như `FAILED`: xem `orders_started` + snapshot flatten để biết sàn có flat không |

## Được giao book lệch (`HALTED_*`)

Bạn đang cầm một danh mục market-neutral **chưa hoàn thành** — có thể net long hoặc net short. Đây không phải khẩn cấp cấp giây (không có lệnh chờ nếu status không phải `CANCEL_FAILED`), nhưng cũng không được để qua đêm mà không quyết định.

1. **Xem chính xác đang giữ gì**: `python reconcile_paper_vs_testnet.py` — exit 3 và in `positions_held_at_handoff`. Nếu là `HALTED_AUDIT_UNAVAILABLE`, đọc dòng cuối `*.sidecar.jsonl`. Đối chiếu với vị thế trên sàn — sidecar/audit là ghi nhận, sàn là sự thật.
2. **Đọc message** trong audit `execution_runs.message` để biết *tại sao* halt. Ba nhóm:
   - *Bạn/TTL bật kill-switch*: chủ ý. Quyết định ở bước 3.
   - *Market data / slippage*: thị trường xấu lúc đó. Chờ ổn rồi bước 3.
   - *Lệnh chờ lạ*: **ai đặt?** Nếu không phải bạn → tài khoản đang bị dùng chung hoặc có process khác — dừng mọi thứ, tìm nguồn.
3. **Chọn một trong hai, không có lựa chọn thứ ba**:
   - **Hoàn thành**: release lại (target_id + budget y hệt) và `--execute` lại **cùng target**. Engine đọc vị thế hiện tại và chỉ đặt phần còn thiếu — không đặt lại phần đã có (`HALTED_*` không bị guard "đã COMPLETE" chặn). Đây là đường mặc định nếu nguyên nhân đã hết. **Hai thứ hay chặn re-run**: target quá 6h (freshness), và **drift gate** — vài giờ sau halt, giá đã đi xa reference của target cũ, per-symbol 300bps / median 150bps sẽ từ chối. Chạy `--plan` trước; nếu drift chặn thì đường còn lại là **Đóng hết** rồi target mới ngày mai. Lưu ý budget tính lại từ equity hiện tại nên contract mới sẽ khác contract cũ một chút — bình thường.
   - **Đóng hết**: nếu không tin được trạng thái, không hiểu nguyên nhân, hoặc target đã quá cũ (>6h) → đóng tay trên sàn (reduce-only, market), rồi ghi lại vào audit bằng tay/ghi chú. Ngày mai chạy target mới từ đầu.
   - **Không được**: để nguyên và "xem sao". Book lệch = đang beta thị trường, không phải chiến lược.
4. **Ghi lại** vào `carry_paper_incidents.md` (tạo nếu chưa có): ngày, status, nguyên nhân, quyết định. 60 ngày paper cần record này để biết executor có đáng tin không.

## Những thứ engine cố tình KHÔNG làm

- Không tự chạy lại sau halt. Mọi lần chạy đều cần release mới.
- Không tự nâng budget. `--budget-usd` phải **bằng đúng** số đã release (`--authorize-budget-usd`), và cả hai phải ≤ ceiling trong `execution_ceilings_v1.json` (hiện testnet 500, live **0**). Đổi file đó = đổi hash mọi contract tương lai = sự kiện có review, không phải knob. Khi ra `execution_ceilings_v2.json`, giữ nguyên v1 để reconcile được các run cũ.
- Không tự quyết "lỗi nhỏ thì kệ". Mọi status không phải COMPLETE đều cần bạn đọc.

## Trước khi bàn chuyện live

Không phải checklist code. Checklist **record**:
- ≥60 ngày paper qua gate trong `carry_paper_config_v1.json`.
- ≥20 lần testnet `COMPLETE` + reconcile exit 0, gồm ít nhất một ngày có flip, một ngày có orphan close, và **một lần bạn cố tình bật kill-switch giữa chừng** rồi xử lý theo mục trên mà không hoảng.
- File `carry_paper_incidents.md` có nội dung và mọi incident đều đóng.
- `execution_ceilings_v2.json` với số live **có review**, không phải copy số testnet.
