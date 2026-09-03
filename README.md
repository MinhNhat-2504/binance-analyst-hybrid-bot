# Binance Analyst

Bot giao dịch trên Binance Futures. Repo đổi hướng giữa chừng nên cái tên cũ dễ gây hiểu nhầm, kể ngắn để ai mới vào đỡ mất thời gian.

Ban đầu đây là bot scalping khung 15 phút: XGBoost + LSTM + một gating network quyết định lúc nào nghe model nào. Backtest ra profit factor 2.2–2.7. Chạy thật thì tháng này qua tháng khác không có lãi. Đến đó thì có hai lựa chọn, tune tiếp hoặc đi kiểm tra xem cái backtest kia có thật không. Tôi chọn cách thứ hai và bỏ mấy tuần dựng riêng một bộ đo: purged walk-forward theo thời gian thật, permutation null, tính đủ phí, không lọc mẫu bằng thông tin tương lai.

Kết quả không dễ chịu. Số đẹp là sản phẩm của leakage trong pipeline train, cụ thể là lọc mẫu bằng nhãn tương lai, purge sai đơn vị, và một cột forward-return lọt vào feature list khi chạy lại cell. Đo lại tử tế thì "kỹ năng" của lệnh SHORT chỉ còn khoảng +4.9bps so với nhiễu (p=0.19), LONG còn +0.85bps (p=0.476), tức không phân biệt được với tung đồng xu. Phí round-trip rẻ nhất mà vẫn khả thi là 12bps. Profit factor thật, đo trên 4.110 lệnh shadow: 0.999.

Bất đẳng thức `4.9 < 12` là toàn bộ câu chuyện. Bot không thua vì thiếu gate hay sai stop, nó thua vì tín hiệu không đủ thông tin để trả phí, và không có cách tune nào tạo ra thông tin từ nhiễu. Toàn bộ khám nghiệm nằm trong [HONEST_FINDINGS.md](HONEST_FINDINGS.md). Nếu chỉ đọc một file ở repo này thì đọc file đó.

Bot 15m giờ nằm nguyên trong `archive/legacy_15m/`, kể cả notebook còn bug. Xóa đi thì câu chuyện mất một nửa.

## Cái thay thế nó: CARRY-7d

Không train model nào. Mỗi ngày xếp hạng 42 perpetual USDT-M theo tổng funding rate 7 ngày gần nhất, short nhóm 20% cao nhất (phe long đang chen chúc và phải trả tiền để giữ vị thế), long nhóm 20% thấp nhất, chia đều, gross 0.5 mỗi bên, cân lại lúc 00:00 UTC, khớp ở giá open ngày kế tiếp, tính 10bps mỗi chân. Hết. Luật viết ra vừa một đoạn văn và nằm trong `carry_paper_config_v1.json` đúng như vậy.

Số đo trong `reports/carry_holdout_report.json`:

| | Discovery (42 đồng, 592 ngày) | Hold-out (33 đồng tách rời, 587 ngày) |
|---|---:|---:|
| Sharpe @ 10bps/chân | 1.78 | 1.85 |
| Lợi nhuận/năm | +32.8% | +38.4% |
| Drawdown sâu nhất | −14.7% | −12.5% |
| Sharpe khi phí gấp đôi (20bps) | 1.20 | 1.31 |
| p-value (permutation null) | 0.005 | 0.005 |

Chia đôi mẫu discovery ra 1.77 và 1.80 nên nó không sống nhờ một giai đoạn may mắn. Có một kiểm tra nữa đáng nói: lấy weight tính từ funding Binance áp lên **giá Bybit**, Sharpe còn 1.61 và 1.70, tức giá không phải artifact của một sàn. Nhưng chiều ngược lại thì hỏng, weight tính từ funding Bybit chỉ ra 1.09 và −0.35. Nghĩa là edge sống nhờ chất lượng thông tin *trong funding của Binance*, không phải "funding nói chung". Đó là điểm yếu đã biết và đã đặt tên.

Vài hướng khác cũng đã đo rồi đóng: rebalance mỗi 8h (Sharpe 0.84 so với 1.85 của daily), spot–perp basis carry (funding thu về chưa bao giờ vượt phí 4 chân), vol-targeting overlay (không nhất quán giữa hai universe và ngày tệ nhất luôn tệ hơn). Kết quả âm ghi lại đầy đủ, cùng chỗ với kết quả dương.

## Đang ở đâu (04/09/2026)

Nói trước cho gọn: **chưa có gì chứng minh bot kiếm được tiền.** Đang có 30 ngày paper, không phải một track record.

`python status.py` sáng nay in ra đúng thế này:

```
CARRY-7d status  2026-09-03 21:06 UTC  (04:06 local)
  paper   day 30/60 (target 90)  equity 1.0151 (+1.51%)  sharpe +1.43  maxDD -2.5%  last booked 2026-09-01
  testnet COMPLETE 3/20 needed  last run 2026-08-27  missed (machine off?) 7: 2026-08-30, ... 2026-09-03
  markers none - next scheduled run will proceed
  canary  2026-09-03 (1d ago)  Binance +1.11 vs Bybit +1.26  funding +5.3%/yr  clear
  fills   90 legs  shortfall vs paper open: mean -5.2bps  p90 +62.6bps  (paper assumes ~10)
```

Paper trading từ 03/08, hôm nay là ngày 30/60. Equity +1.51%, Sharpe 1.43, drawdown sâu nhất −2.5%. Nhìn thì ổn, nhưng độ lệch chuẩn ngày là 0.70%, nên 1σ của một tháng đã khoảng 3.8%. Con số +1.51% kia gần như không nói lên điều gì.

Từ 25/08 có thêm một vòng diễn tập tự động mỗi sáng, đặt đúng cái rổ đó lên Binance testnet qua engine thật trong `execution/` rồi tự đối chiếu lại. Mới được 3 lần COMPLETE trên 20 lần cần, 90 chân lệnh đã khớp. Thiếu vì tuần 28/08–03/09 mất trắng: task Windows chạy ở chế độ tương tác nên mỗi sáng bật một cửa sổ console, đóng cửa sổ đó là Python ăn CTRL_CLOSE và chết giữa chừng, để lại lock cùng một marker rỗng, và cơ chế fail-safe chặn luôn mấy ngày sau. Sàn không bị đụng, nhưng mất 7 ngày trong đúng cái tháng cần đủ 20 run. Đã sửa, ghi trong [carry_paper_incidents.md](carry_paper_incidents.md).

Chất lượng khớp lệnh so với giá open mà paper giả định: trung bình −5.2bps, tức đang tốt hơn giả định. Nhưng đuôi p90 lên tới +62.6bps, đang theo dõi.

Cổng ngày 60 rơi vào **02/10/2026**. `gate_report.py` chấm 13 điều kiện bằng máy rồi in GO / NO-GO / NOT-YET kèm số đo từng dòng. Sáng nay: 9 PASS, 4 NOT-YET, thiếu 30 ngày paper và 17 lần testnet. Nếu ngày 60 trượt thì chờ tới ngày 90, trượt tiếp thì dừng hẳn chứ không tune. Ngưỡng khóa bằng sha256 trong config và executor từ chối chạy nếu file bị sửa. Nghe cực đoan, nhưng một record 60 ngày của cái luật bị chỉnh giữa chừng thì chẳng chứng minh được gì, và tune-giữa-chừng đúng là cơ chế đã tạo ra PF 2.69 giả hồi trước.

Một chi tiết đang canh: canary hằng tuần so Sharpe của weight tính từ funding Binance với weight từ funding Bybit. Ngày 24/08 là 1.60 so với 1.35, ngày 03/09 thành 1.11 so với 1.26. Chưa đủ để báo động, nhưng khoảng cách đã đảo chiều, mà edge của chiến lược này vốn nằm ở chỗ đó.

## Chạy

Không cần API key, mọi thứ dùng public endpoint của Binance.

```bash
pip install -r requirements.txt
```

```bash
python run_honest_harness.py --quick   # đo lại tín hiệu 15m cũ (spoiler: không có edge)
python run_daily_lab.py                # lab chiến lược khung ngày, mỗi cell so với null riêng
python run_carry_holdout.py            # kiểm carry trên universe tách biệt
python run_carry_paper.py              # book paper hằng ngày, quên vài hôm nó tự bù
pytest                                 # 164 test
```

Vận hành hằng ngày thì không phải gõ gì, ba task Windows do `INSTALL_TASKS.bat` cài lo phần đó. Khi cần nhìn:

```bash
python status.py             # toàn cảnh trong 6 dòng
python gate_report.py        # từng điều kiện của cổng ngày 60
python check_live_filters.py # sàn thật nhận rổ này ở vốn tối thiểu bao nhiêu
```

Muốn tự đặt lệnh testnet bằng tay thì `export_carry_targets.py` xuất weight hôm nay, `run_testnet_execution.py --plan` cho xem trước từng leg mà không đụng sàn, quy trình đầy đủ nằm trong [EXECUTION_RUNBOOK.md](EXECUTION_RUNBOOK.md).

## Phần thực thi

Chỗ này tốn công hơn dự tính, vì câu hỏi trong đầu luôn là: nếu ngày gate pass thật thì có dám giao tiền cho đoạn code này không.

Live không phải một cái cờ bật/tắt, nó bị chặn ở hai tầng. Tầng file: `execution_ceilings_v1.json` khai `testnet: 2000, live: 0`, và hash của file đó đóng băng vào mọi execution contract; CLI chỉ được hạ budget xuống dưới trần, không bao giờ nâng. Tầng code: `ABSOLUTE_GROSS_UPPER_BOUND_USD = 5000` trong `execution/contracts.py`, không file JSON nào vượt qua được. Muốn có live thì phải sửa file, đổi hash mọi contract tương lai, và làm đỏ một test viết riêng để reo chuông đúng lúc đó. Là một sự kiện được review, không phải một cái nút.

Engine còn có kill-switch tự bật lại sau mỗi run (release hết hạn sau 15 phút và bind vào đúng target và budget của ngày), drift gate so vị thế thật với contract, verifier làm việc trong không gian quantity chứ không phải notional, audit sqlite, và DD guard tự dừng trước khi đặt lệnh nếu equity tụt 20% ngân sách so với đỉnh. Nguyên tắc xuyên suốt: engine chỉ tự thanh lý khi vị thế thật sự sai so với contract, mọi bất thường khác thì nó dừng và giao book lại cho người chứ không bán tháo.

Có chuyện chỉ chạy thật mới biết. Run không người trực đầu tiên từ chối cả danh mục vì min-notional của BTC là $100 mà weight nhỏ nhất của rổ chỉ 5,56%, nên ở budget $500 chân BTC chỉ có $27,8. Engine không chạy thiếu chân, nó bỏ nguyên ngày, và đó là hành vi đúng. Từ đó mới có `check_live_filters.py`, và hóa ra sàn vốn tối thiểu không phải hằng số: đo ngày 03/09 ra $1.402 rồi $1.465 trong cùng một buổi, vì nó trôi theo giá BTC và stepSize. Dưới sàn thì BTC rớt khỏi rổ, và lúc đó đây là một chiến lược khác chưa ai test.

## Cấu trúc

- `honest/` — bộ đo: fetch có cache, feature chặn cột không dừng, label triple-barrier tính đủ phí, purged walk-forward theo wall-clock, permutation null, mô phỏng khớp maker/taker. Phần đáng giá nhất của repo. Ý tưởng mới phải qua được nó rồi mới được tin.
- `run_daily_lab.py`, `run_carry_holdout.py` — grid chiến lược daily đăng ký trước, và kiểm hold-out.
- `run_carry_paper.py` + `carry_paper_config_v1.json` — executor paper, fill tại open ngày kế tiếp, khóa hash chống tune.
- `execution/` — engine đặt lệnh và các khóa an toàn nói ở trên.
- `reports/` — JSON kết quả của mọi lab đã chạy, tức bằng chứng cho HONEST_FINDINGS.md.
- `archive/legacy_15m/` — bot cũ giữ nguyên vẹn để tham khảo. Đừng tin số trong đó.
- [EXECUTION_RUNBOOK.md](EXECUTION_RUNBOOK.md), [GO_LIVE_CHECKLIST.md](GO_LIVE_CHECKLIST.md), [RESEARCH_PREREG_Q4_2026.md](RESEARCH_PREREG_Q4_2026.md) — vận hành, khung quyết định vốn, và 3 cell nghiên cứu đăng ký trước cho tháng 10. Repo tự giới hạn 3 hướng mới mỗi quý.

## Vài thứ học được, trả bằng thời gian thật

1. Backtest không có permutation null đối chứng thì chưa nói lên điều gì.
2. Purge theo số dòng trên frame nhiều symbol xen kẽ là sai đơn vị, phải purge theo thời gian thật.
3. Sàn trả nến "ma" cho coin đã delist, giá đứng im và volume 0. Không lọc là backtest tự bịa ra lãi.
4. Chính cái harness viết ra để chống leakage cũng leak. Cách duy nhất giữ được trung thực là mỗi lần đo lại phải cố tình đi tìm chỗ sai.

Nói trước cho rõ: repo này làm để nghiên cứu và học, không phải lời khuyên đầu tư và cũng không phải để ai clone về chạy tiền thật. Crypto rủi ro cao, funding carry vẫn ăn đòn nặng được khi thị trường squeeze. Ai bật live thì tự chịu.
