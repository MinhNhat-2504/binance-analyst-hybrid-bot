# SHORT + BTC hedge: protocol thăm dò và xác nhận tiến tới Q1/2027

Viết ngày 20/10/2026 **trước khi chạy** `explore_short_btc_hedge.py`. Ý tưởng đã
được ghi 26/09 trong `RESEARCH_PREREG_Q4_2026.md`, sau khi nhìn phân rã hai chân
CARRY-7d. Theo yêu cầu nghiên cứu ngay, mọi kết quả lịch sử dưới đây được dán
nhãn **exploratory**. Hai universe lịch sử đã được xem qua ở báo cáo tách chân;
không được gọi universe thứ hai là holdout sạch hay dùng kết quả để sửa v1.

## Một quy tắc cố định cho phép thăm dò

- Giá, funding, zombie guard và cửa sổ 600 ngày lấy từ `honest.daily.build_panel`.
- Signal là tổng funding đã settle 7 ngày, chọn 20% mã cao nhất để **short**.
  Chân short ban đầu có gross 0,5, cân đều. Không chọn chân long altcoin.
- Beta từng mã so với BTC = covariance / variance của return ngày trong 90 ngày
  đã biết ở cuối ngày quyết định, kẹp [0,3]. Thiếu 90 quan sát thì mã chưa
  đủ điều kiện. BTC hedge = âm của beta dollar của rổ short; ghép chung nếu BTC
  cũng nằm trong rổ short. Chuẩn hóa lại gross tuyệt đối về 1, rồi dịch weight
  sang ngày giao dịch kế tiếp. Không đổi hướng, cửa sổ, cap hay universe sau
  khi đọc kết quả.
- Phí 10 bps/chân trên turnover; kiểm tra stress 20 bps. Tính funding cho cả
  chân short và BTC. So với CARRY-7d v1 trong **cùng ngày có thể giao dịch**.
- Báo cáo Sharpe, return, drawdown, turnover, beta ex ante còn lại, hai nửa và
  khối sau 03/04/2026. Dùng bootstrap cặp khối tròn 22 ngày, 999 mẫu, seed 0
  cho chênh lệch Sharpe so với v1; chỉ là khoảng bất định mô tả vì mẫu đã lộ.

## Ranh giới kết luận

Chạy một lần trên 42 mã discovery và 33 mã rời đã có cache. Cả hai là **mẫu
khám phá đã bị nhìn**, không mở thêm grid, null hay kết luận xác nhận nếu kết
quả đẹp. Chưa có quyền tạo targets, thay paper config, hoặc bật live.

Xác nhận thật phải dùng dữ liệu phát sinh **sau 20/10/2026** theo protocol v2
được khóa trước khi kỳ shadow bắt đầu: chạy song song sổ bóng v1 và ứng viên,
ghi mỗi ngày kể cả ngày xấu, đo lợi nhuận ròng, drawdown và chi phí khớp thực
tế ít nhất 90 ngày. Nếu không đủ thời gian trong Q1, tiếp tục quan sát; không
chọn lọc một giai đoạn đẹp để tuyên bố thắng.
