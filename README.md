# Binance Analyst

Bot giao dịch crypto trên Binance Futures. Repo này đổi hướng giữa chừng, và cái tên "hybrid bot" giờ chỉ còn đúng một nửa — kể lại cho ai mới vào đỡ bỡ ngỡ:

Ban đầu đây là bot scalping khung 15 phút, chạy cả cụm XGBoost + LSTM + gating network, backtest ra profit factor 2.2–2.7 nhìn rất sướng mắt. Nhưng chạy thật thì mãi không có lãi. Sau khi dựng lại hệ thống đo lường tử tế (walk-forward có purge theo thời gian thật, so với permutation null, tính đủ phí) thì vỡ lẽ: mấy con số đẹp kia là do leakage trong pipeline huấn luyện, còn tín hiệu thật chỉ đáng ~5bps trong khi phí round-trip đã 12-13bps. Tức là chưa đánh đã thua. Chi tiết vụ "khám nghiệm tử thi" này nằm trong [HONEST_FINDINGS.md](HONEST_FINDINGS.md) — nếu chỉ đọc một file trong repo thì đọc file đó.

Hướng đi hiện tại đơn giản hơn nhiều: **funding carry khung ngày (CARRY-7d)**. Xếp hạng ~40 đồng theo tổng funding rate 7 ngày, short nhóm funding cao nhất (phe long đang chen chúc và phải trả phí), long nhóm funding thấp nhất, cân lại danh mục mỗi ngày. Không train model nào cả. Đây là chiến lược đầu tiên của repo qua được toàn bộ khâu kiểm: backtest 600 ngày Sharpe ~1.8, giữ nguyên trên universe hold-out gồm 33 đồng hoàn toàn khác, sống sót khi nhân đôi phí, không dựa vào một symbol hay vài ngày may mắn nào (ngày trung vị dương, không đồng nào chiếm quá 15% lợi nhuận).

## Trạng thái hiện tại (08/2026)

Đang **paper trade**, bắt đầu từ 03/08/2026. Live đang tắt hoàn toàn (`live_enabled=false`, vốn cấp = 0).

Điều kiện để cân nhắc bật live được ghi sẵn trong `carry_paper_config_v1.json`: tối thiểu 60 ngày paper (mục tiêu 90), Sharpe > 0.5, tổng lãi dương, drawdown không quá -20%. File config này bị khóa bằng hash — executor từ chối chạy nếu file bị sửa. Nghe hơi cực đoan nhưng lý do đơn giản: record 60 ngày của một rule bị chỉnh giữa chừng thì chẳng chứng minh được gì, và tune-giữa-chừng chính là cách repo này từng tự lừa mình.

## Chạy

Cài đặt gọn (không cần tensorflow như bản cũ):

```bash
pip install numpy pandas scipy scikit-learn xgboost pyarrow
```

Không cần API key — mọi thứ dùng public endpoint của Binance (klines + funding rate).

```bash
# Đo lại xem tín hiệu 15m cũ có edge không (spoiler: không)
python run_honest_harness.py --quick

# Lab chiến lược khung ngày: momentum / trend / carry, mỗi cái so với null riêng
python run_daily_lab.py

# Kiểm tra hold-out của carry trên universe tách biệt
python run_carry_holdout.py

# Paper trading hằng ngày (chạy trễ hay quên vài ngày cũng được, nó tự bù)
python run_carry_paper.py
python run_carry_paper.py --status   # xem đang ở ngày bao nhiêu, lãi lỗ ra sao
```

## Cấu trúc

- `honest/` — bộ đo lường: fetch data có cache, feature sạch (chặn cột không dừng), triple-barrier label tính đủ phí, purged walk-forward, permutation null, mô phỏng khớp lệnh maker/taker, và lab chiến lược khung ngày. Đây là phần đáng giá nhất của repo; muốn thử ý tưởng gì thì bắt nó chạy qua đây trước khi tin.
- `run_daily_lab.py` / `run_carry_holdout.py` — grid chiến lược daily đã đăng ký trước + kiểm hold-out.
- `run_carry_paper.py` + `carry_paper_config_v1.json` — executor paper trading, fill tại open ngày kế tiếp, có khóa chống tune.
- `run_clean_oos.py` — pipeline OOS độc lập (một nhánh kiểm tra song song, kết luận tương tự).
- `binance-analyst.ipynb`, `Model_Training_Lab.ipynb` — bot 15m cũ và notebook train. Giữ lại để tham khảo, **đừng tin số trong đó** — xem HONEST_FINDINGS.md để biết vì sao.

## Bài học rút ra (tóm tắt cho đỡ đọc file dài)

1. Backtest đẹp mà không có permutation null đối chứng thì chưa nói lên điều gì.
2. Purge theo số dòng trên dataframe nhiều symbol xen kẽ là sai đơn vị — phải purge theo thời gian thật.
3. Phí 13bps nghe nhỏ nhưng giết chết mọi tín hiệu yếu ở khung 15 phút.
4. Sàn trả nến "ma" cho coin đã delist (giá đứng im, volume 0) — không lọc là backtest tự bịa lãi.
5. Kiểm tra độ tập trung lợi nhuận theo symbol và theo ngày trước khi tin bất kỳ Sharpe nào.

## Lưu ý

Repo phục vụ nghiên cứu và học tập. Crypto rủi ro cao, funding carry vẫn có thể sập khi thị trường squeeze — tự chịu trách nhiệm nếu bật live.

## Kết quả cell carry 8h (15/08/2026): đóng hướng này

Cell 8h đã được đăng ký trước và chạy bằng cùng snapshot, universe, cutoff và chi phí với arm daily; dữ liệu đã refresh tới 15/08 thay vì âm thầm dừng ở 01/08. Ở double-holdout replay sau 03/04, 8h đạt Sharpe 0.84, tổng lợi nhuận +5.9%, max drawdown -11.3% và HAC CI của mean [-4.8, +8.1] bps/bar. Arm daily cùng snapshot đạt Sharpe 1.85, +15.8%, drawdown -6.4%. Chi phí 8h cao hơn vì turnover tăng khoảng 1.51x; latency thêm 8h lại cải thiện Sharpe từ 0.84 lên 1.13, không ủng hộ giả thuyết có thông tin cần thu hoạch ở scale 8h. Double-holdout 8h có association p=0.2875 và net-profit p=0.302, nên point estimate dương không phải edge đã xác nhận.

Kết luận: **không promote và không tune tiếp cell 8h**. Đây là kết quả âm có giá trị; route CARRY-7d daily đang paper không bị thay đổi. `permutation_p` trong report là null về liên kết signal–outcome và cố định cost path; `net_profit_block_p` là null riêng cho lợi nhuận ròng sau phí. Runner từ chối chạy nếu funding tail hoặc bar cache bị stale thay vì âm thầm cắt phần cuối.
