
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

## 2026-09-04T00:21:58+00:00 — plan_refused

```
{
  "target_id": "c37a9415ab50042e584a9dbb",
  "error": "RuntimeError: BTCUSDT: cannot split order into exchange-valid notionals"
}
```

_Xử lý theo EXECUTION_RUNBOOK.md, ghi quyết định vào đây, rồi xóa `.execution/ATTENTION`._

**Xử lý (2026-09-04):** Task chạy đúng giờ (fix `run_hidden.vbs` hôm qua có tác dụng), nhưng plan bị từ chối trước khi mở khóa nên **0 lệnh trên sàn**, sàn không bị đụng. Nguyên nhân là va chạm số học lần đầu gặp: BTCUSDT lần đầu vào rổ ở budget $2000. Weight nhỏ nhất 5,56% → $105.60; trần một lệnh `max_order_notional_usd=100` → $97.48; sàn min-notional của BTC trên demo → $56.86. Chia đôi thì mảnh sau chỉ $8.12, dưới sàn; muốn cả hai mảnh hợp lệ cần $113.72 mà lệnh chỉ có $105.60. Một lệnh nguyên thì vượt trần. Bế tắc, engine từ chối cả danh mục — hành vi đúng theo thiết kế "không chạy thiếu chân", nhưng mất nguyên ngày.

Sửa trong `execution/engine.py::_append_split`: khi không thể chia thành các mảnh hợp lệ, gửi **một lệnh duy nhất**, và chỉ khi khối lượng **nhỏ hơn 2 lần min-notional của sàn**. Lý do chọn ngưỡng đó: trần một lệnh là *ưu tiên của mình* về chống trượt giá, còn min-notional là *luật cứng của sàn*; khi hai cái đá nhau thì luật sàn thắng, nhưng lối thoát phải bị chặn bằng một con số do sàn đặt chứ không do mình đặt, để nó không biến thành cách lách trần. Lệnh vượt xa trần vẫn bị chia bình thường, không chia được thì vẫn từ chối. Hai test khóa đúng hai nhánh đó (`test_uncuttable_leg_becomes_one_order_bounded_by_two_exchange_minimums`, `test_a_leg_far_above_the_cap_is_still_refused_not_sent_whole`). Toàn bộ 166 test xanh. Marker đã xóa, runs tiếp tục từ 05/09 07:20.

## 2026-09-05T00:23:35+00:00 — run_needs_review

```
{
  "target_id": "58984bd557cba89ba34b82ff",
  "engine_status": "UNRESOLVED_EXPOSURE",
  "engine_detail": "network failure calling /fapi/v1/order: HTTPSConnectionPool(host='demo-fapi.binance.com', port=443): Max retries exceeded with url: /fapi/v1/order?symbol=SUIUSDT&orderId=1065808868&recvWindow=5000&timestamp=1788567719640&signature=f4763f08b491117e32b5299ffb9135ea59a11f0e4c59afafcce59966f007c88a (Caused by SSLError(SSLEOFError(8, 'EOF occurred in violation of protocol (_ssl.c:2427)')))",
  "reconcile_exit": 3,
  "reconcile_tail": "{\n  \"ATTENTION\": \"target has a run that ended in a hand-off state; positions may be live and unhedged\",\n  \"sidecar\": \"no sidecar (audit DB accepted the terminal row)\",\n  \"target_id\": \"58984bd557cba89ba34b82ff\",\n  \"runs_with_open_exposure\": [\n    {\n      \"run_id\": \"56188bafbe02451395e7043b7998553c\",\n      \"started_utc\": \"2026-09-05T00:21:13.811+00:00\",\n      \"finished_utc\": \"2026-09-05T00:23:35.127+00:00\",\n      \"status\": \"UNRESOLVED_EXPOSURE\",\n      \"message\": \"network failure calling /fapi/v1/order: HTTPSConnectionPool(host='demo-fapi.binance.com', port=443): Max retries exceeded with url: /fapi/v1/order?symbol=SUIUSDT&orderId=1065808868&recvWindow=5000&timestamp=1788567719640&signature=f4763f08b491117e32b5299ffb9135ea59a11f0e4c59afafcce59966f007c88a (Caused by SSLError(SSLEOFError(8, 'EOF occurred in violation of protocol (_ssl.c:2427)')))\"\n    }\n  ],\n  \"positions_held_at_handoff\": {\n    \"positions\": [],\n    \"open_orders\": []\n  },\n  \"action\": \"inspect the exchange, decide flatten/keep manually, then reconcile with --run-id if a later COMPLETE run exists\"\n}\n"
}
```

_Xử lý theo EXECUTION_RUNBOOK.md, ghi quyết định vào đây, rồi xóa `.execution/ATTENTION`._

**Xử lý (2026-09-25, muộn 20 ngày):** Sàn đã kiểm trực tiếp: **0 vị thế, 0 lệnh chờ, unrealized 0**. Rổ không bị bỏ dở — engine đã tự thanh lý sạch ở lần thử flatten thứ 3 (8 lệnh `emergency_flatten`, tất cả FILLED), chỉ là nó *không tự công nhận* việc đó. Chuỗi sự kiện: một lỗi SSL EOF thoáng qua khi đọc trạng thái lệnh SUIUSDT (lệnh đã được sàn nhận, chỉ không đọc được) → engine coi là lỗi chung sau khi đã có lệnh → leo thang flatten → cancel-all thất bại vì cùng đợt mất mạng → đọc vị thế lần 1 mất mạng, lần 2 lỗi −1021 (lệch giờ) → lần 3 đọc được và đóng hết 8 vị thế → hết số lần thử, bước xác nhận chỉ nằm ở đầu lần thử *kế tiếp* nên tài khoản phẳng vẫn bị ghi `UNRESOLVED_EXPOSURE`.

Ba lỗi chồng nhau, sửa cả ba (`execution/binance_futures.py`, `execution/engine.py`): (1) client tự thử lại 3 lần có backoff cho GET/DELETE — không bao giờ cho POST đặt lệnh — và tự đồng bộ giờ rồi gửi lại khi gặp −1021; (2) lệnh **đã đặt được nhưng không đọc được trạng thái** giờ là `HALTED_MID_BOOK` (giữ rổ, chỉ hủy lệnh chờ), không còn là flatten — "không biết" khác "sai"; (3) flatten thành công ở lần thử cuối được công nhận là `emergency_flatten_verified` → status `FAILED` thay vì `UNRESOLVED_EXPOSURE`. 12 test mới, tổng 189 xanh.

Bài học vận hành đắt nhất không phải code: marker ATTENTION nằm trong thư mục ẩn và **không ai nhìn suốt 20 ngày**, mất 20 ngày testnet trong đúng tháng cần 20 lần COMPLETE. Từ giờ task paper mỗi sáng chạy `notify_markers.py`: có marker là có file `BINANCE BOT - CAN XEM.txt` trên Desktop, hết marker file tự biến mất. Marker đã xóa; runs tiếp tục từ 26/09 07:20 — ngày đầu sẽ là dựng lại cả rổ từ 0 (~34 chân).

**Canary (xem cùng lúc):** 06/09 báo ALERT (Binance +0.84 so với Bybit +1.11); 13/09 và 20/09 hết ALERT theo luật nhưng khoảng cách tiếp tục âm (−0.28, −0.41): trên cửa sổ 180 ngày, Sharpe theo weight Binance tụt 1.60 → 1.02 còn theo weight Bybit tăng 1.35 → 1.43. Đọc thẳng: chưa phải bằng chứng edge Binance mất, vì paper (dùng đúng weight Binance) đang +8.14% ở ngày 52; 180 ngày trộn cả mùa hè. Nhưng đây là điểm yếu đã đặt tên từ 16/08 và nó đang đi đúng hướng xấu. Quyết định: không có tiền thật nên không phải tiêu chí dừng; ghi nhận, giữ canary hằng tuần, `gate_report.py` vẫn đọc 4 dòng gần nhất. Marker `canary_ALERT` từ giờ phản ánh *tuần mới nhất* (tuần sạch tự xóa), lịch sử nằm trong `canary_log.csv`. Marker đã xóa.

## 2026-09-28T00:21:41+00:00 — plan_refused

```
{
  "target_id": "03e01c1373443c72bf237d08",
  "error": "RuntimeError: SEIUSDT: frozen reference drift 356.83bps exceeds per-symbol limit 300.00bps"
}
```

_Xử lý theo EXECUTION_RUNBOOK.md, ghi quyết định vào đây, rồi xóa `.execution/ATTENTION`._

**Xử lý (2026-10-03, muộn 5 ngày):** Plan bị từ chối trước khi mở khóa: SEIUSDT chạy 356 bps giữa giá tham chiếu paper (00:00 UTC) và giá lúc chạy (00:20), vượt ngưỡng 300 bps của drift gate. **Không lệnh nào được đặt**, sàn giữ nguyên rổ của run COMPLETE 26/09. Drift gate làm đúng việc. Lỗi là ở chỗ khác: một lần từ chối không đặt lệnh lại ghi marker ATTENTION, và marker đó chặn năm buổi sáng sạch (29/09 tới 03/10), đúng kiểu hỏng của tháng 9.

Sửa trong `run_carry_testnet_daily.py`: tách hai loại marker. ATTENTION (chặn) chỉ còn cho những gì có thể để lại vị thế lệch trên sàn: trạng thái hand-off sau khi đã đặt lệnh, DD guard, lock kẹt, crash, không bật lại được kill-switch. Từ chối trước khi đặt lệnh (plan refused, export thất bại, không đọc được tài khoản) giờ ghi `.execution/NOTICE`: không chặn, đếm số lần liên tiếp, hiện trên Desktop và `status.py`, tự xóa khi có ngày COMPLETE; healthcheck báo khi lặp 3 ngày liền. Ghi NOTICE là best-effort (test đĩa đầy bắt được lỗi crash ở đây). 5 test mới. Marker đã xóa; runs tiếp tục từ 04/10 07:20.

## 2026-10-11 — testnet không khởi động dù máy thức; thước đo tracking sai phương pháp

**Xử lý (2026-10-12):** Hai phát hiện khi kiểm cổng ngày 12/10, không cái nào để lại vị thế lệch (sàn khớp contract, reconcile 0 ở cả 10 lần COMPLETE).

(1) Sáng 11/10 máy thức đúng giờ (wake 07:19:47) nhưng vòng testnet không chạy, không log, không marker. Nguyên nhân: ba file `.bat` cùng redirect vào `run_daily_task.log`, mà `cmd` mở file redirect không cho tiến trình khác ghi. Stage paper hôm đó bị máy ngủ giữa chừng (07:05:36 tới 07:19:47) nên vẫn giữ file lúc 07:20; `.bat` testnet bị từ chối mở file và Python không hề khởi động. Đã tái hiện bằng thí nghiệm ("being used by another process"). Sửa: mỗi stage redirect vào file riêng (`run_daily_<stage>.log`), dòng tóm tắt do Python tự ghi; stage testnet chờ tối đa 15 phút nếu stage paper còn chạy (marker `.execution/paper_stage.running`, quá 1 giờ coi là chết); `run_daily.py` giữ power request `SYSTEM_REQUIRED` để máy không idle-sleep giữa lúc chạy (07:24:44 hôm đó là "Sleep Reason: System Idle").

(2) `track_paper_vs_testnet.py` cũ so paper (mark-to-market) với income **đã chốt** của sàn theo ngày, nên lãi của vị thế giữ nhiều ngày dồn vào ngày đóng: báo lệch +4.35% ngân sách sau 9 ngày, có ngày lệch $66 trên rổ $2000. Viết lại: so thay đổi equity giữa hai lần chạy liên tiếp (chuỗi DD guard đã ghi sẵn) với pnl paper cùng ngày, chỉ tính khoảng sạch (run COMPLETE, 22 tới 26 giờ). Kết quả thật trên 3 ngày sạch 07 tới 09/10: +$38.96 / +$22.48 / +$7.23 so với paper +$37.88 / +$26.88 / +$10.76, lệch cộng dồn −0.34% ngân sách (−11 bps/ngày). Hai thước đo fill quality và tracking giờ chạy trong chuỗi hằng ngày sau vòng testnet; trước đó cổng chấm hai điều kiện này bằng số liệu của ba ngày tháng 8.
