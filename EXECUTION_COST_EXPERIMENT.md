# Thử nghiệm chi phí khớp lệnh trên tài khoản Demo Futures riêng

Viết 20/10/2026. Tài khoản testnet của bot đang tích lũy 20 phiên COMPLETE;
**không** gửi lệnh thử vào tài khoản đó, không đổi `carry_paper_config_v1.json`,
`execution_ceilings_v1.json` hay `order_style=MARKET` của vòng hằng ngày.

## Bằng chứng hiện có

- `reports/execution_fee_audit.json`: 338/338 lệnh của 13 phiên COMPLETE,
  406 trades, 0% maker, hoa hồng 4,16 bps theo notional.
- `execution_quality.csv`: 338 lệnh có open ngày; shortfall +8,99 bps nếu mỗi
  lệnh một phiếu, +9,17 bps theo notional. Tổng hoa hồng + shortfall cùng mốc
  open paper là **13,32 bps**, cao hơn giả định 10 bps/chân.
- `reports/execution_opportunity.json`: phí maker trên mọi mã đã khớp là
  2,00 bps. Ngay cả giả định cực tốt là **100%** notional được maker khớp ở
  **cùng giá** market cũ, chỉ tiết kiệm tối đa 2,16 bps phí và tổng vẫn là
  11,17 bps. Phải có thêm cải thiện giá ít nhất 1,17 bps mới xuống 10 bps;
  xác suất khớp, vị trí hàng đợi, adverse selection, chờ và fallback chưa đo.

## Protocol thử riêng, khóa trước lệnh đầu tiên

1. Dùng API key **khác** của tài khoản Binance Demo Futures riêng, trong file
   `.env.maker_probe` (gitignore đã bao phủ), gồm đúng ba dòng:

   ```text
   PROBE_API_KEY=<điền trên máy, không gửi trong chat>
   PROBE_API_SECRET=<điền trên máy, không gửi trong chat>
   ISOLATED_DEMO_ACCOUNT=1
   ```

   `python probe_maker_account.py --credentials .env.maker_probe` là cổng chỉ
   đọc: so key với bot chính, kiểm tra đúng host demo, tài khoản phẳng, không
   có lệnh chờ và one-way mode. Chưa có key riêng thì dừng tại cổng này.

2. Dùng cùng một danh sách mã thanh khoản tốt và cỡ lệnh đủ min-notional cho
   cả hai kiểu `MARKET` và `MAKER_THEN_MARKET` đã có trong engine. Ghi quyết
   định kiểu lệnh *trước* khi nhìn giá ngày thử; chạy trên tài khoản riêng,
   đóng sạch trước và sau mỗi cặp thử. Maker chờ tối đa 10 giây rồi hủy và
   market phần còn lại; mọi trạng thái không xác định phải dừng theo runbook.
3. Tối thiểu 10 ngày khác nhau và 100 lệnh mỗi kiểu. So chi phí thực bằng
   `commission + signed(fill / mid_lúc_gửi - 1)` theo notional **và** bằng
   `commission + signed(fill / open_paper - 1)`; đo tỷ lệ maker khớp, lệnh
   không khớp, fallback, thời gian chờ, độ lệch giá sau chờ và mọi lần halt.
   So sánh theo ngày và cỡ lệnh, không chọn riêng mã/ngày đẹp.
4. Chỉ xem xét ứng viên v2 nếu giảm chi phí all-in ít nhất **2 bps/chân**,
   khoảng tin cậy bootstrap theo ngày 95% cho chênh lệch nằm dưới 0, và không
   có thêm vị thế/lệnh treo hoặc run halt. Mẫu nhỏ hơn chỉ là pilot vận hành.

Chưa chạy lệnh thử vì API key của tài khoản riêng chưa được cấu hình. Báo cáo
phí hiện tại chỉ xác định **trần tiết kiệm từ phí**, không đo được hiệu quả
thực của maker. Khi có key riêng, preflight trên phải qua trước mọi lệnh.
