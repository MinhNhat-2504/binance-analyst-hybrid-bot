# TỔNG QUAN CHỨC NĂNG HỆ THỐNG BAN - SUPREME ALPHA

## 🧠 MODULE 1: BỘ NÃO DỰ BÁO (THE PREDICTIVE BRAIN)

### 1. Cấu trúc Lõi kép (Dual-Core XGBoost V8)
* **Chức năng:** Thay vì dùng 1 mô hình dự đoán giá lên/xuống, bot dùng 2 mô hình độc lập (`model_long` và `model_short`). Một cái chuyên tìm kiếm cơ hội Mua (Long), một cái chuyên tìm cơ hội Bán (Short).
* **Điểm hay:** Khắc phục tình trạng "nhầm lẫn" của AI khi thị trường đi ngang. Bot đòi hỏi xác suất tự tin bất đối xứng (Long cần >75%, Short cần >85%) mới cho phép "bóp cò".

### 2. Bóc tách Tư duy AI (SHAP Insights - AI Explainability)
* **Chức năng:** Trích xuất tầm quan trọng của các đặc trưng (Feature Contributions/SHAP) ngay tại thời gian thực.
* **Điểm hay:** Bot không hành động như một "hộp đen". Nó sẽ nói cho người dùng biết tại sao nó vào lệnh (Ví dụ: *"Dòng tiền vĩ mô đang ủng hộ, nhưng biến động giá đang hơi bất lợi"*). Đây là tính năng "ăn tiền" nhất để thuyết phục khách hàng mua gói $50.

### 3. Cơ chế Học Trực tuyến (Online Learning - Tự tiến hóa)
* **Chức năng:** Mỗi khi một lệnh đóng lại (chốt lời hoặc cắt lỗ), bot lấy chính dữ liệu của lệnh đó để "huấn luyện lại" (retrain) mô hình XGBoost.
* **Điểm hay:** Bot liên tục "mọc thêm tế bào não mới", tự động rút kinh nghiệm từ sai lầm của chính mình để thích nghi với thị trường ngày mai.

---

## 🛡️ MODULE 2: HỆ THỐNG PHÒNG VỆ & THỜI THẾ (THE SHIELD)

### 4. Vệ sĩ Thiên nga đen (Autoencoder Kill-Switch)
* **Chức năng:** Dùng mạng nơ-ron Autoencoder để đo lường "Lỗi tái tạo" (MSE). Hoạt động khi thị trường xuất hiện mô hình giá chưa từng có trong lịch sử (đột ngột sập mạnh hoặc bơm thổi bất thường).
* **Điểm hay:** Nó tự động "cắt cầu dao" đứng ngoài thị trường hoặc đóng lệnh khẩn cấp để bảo vệ tài khoản, triệt tiêu rủi ro cháy tài khoản do thiên nga đen.

### 5. Trí tuệ Nhận diện Thời thế (HMM - Hidden Markov Model)
* **Chức năng:** Phân tích biến động và thanh khoản để xếp thị trường vào 3 trạng thái: Đình trệ (Sleep), Đi ngang (Sideway), hoặc Siêu sóng (Trending).
* **Điểm hay:** Bot tự động đổi luật chơi. Nếu Sideway, nó đánh nhanh rút gọn (hạ TP/SL). Nếu Trending (Up), nó áp dụng "Luật thép": Cấm tuyệt đối việc đánh ngược sóng (Short).

---

## 🚦 MODULE 3: HỆ THỐNG GÁC CỔNG ĐA TẦNG (THE GATEKEEPERS)
*Đây là màng lọc giúp bot hủy bỏ các tín hiệu "rác" dù AI đã báo tỷ lệ thắng cao.*

### 6. Gác cổng Nhân quả (Causal AI - Do-Calculus)
* **Chức năng:** Phân tích sự tương quan giữa Altcoin (như SOL, BNB) và Bitcoin (BTC).
* **Điểm hay:** Nếu AI báo SOL sẽ tăng, nhưng Causal AI phát hiện SOL tăng chỉ vì "bị BTC kéo lên" chứ bản thân SOL không có lực mua thực sự, bot sẽ Hủy lệnh để tránh bẫy "tương quan giả".

### 7. Gác cổng Tin tức Vĩ mô (NLP FinBERT Gatekeeper)
* **Chức năng:** Liên tục cào (crawl) tin tức từ Cointelegraph RSS, đưa vào mô hình ngôn ngữ lớn FinBERT để chấm điểm cảm xúc (Sentiment Score).
* **Điểm hay:** Nếu thị trường đang ngập tràn tin FUD (tin xấu tột độ, score < -0.15), nó sẽ cấm đánh Long. Ngược lại, nếu FOMO tột độ, cấm đánh Short.

### 8. Tín hiệu Alpha Cấp cao (Mempool & On-chain Sniping)
* **Chức năng:** Không đợi giá chạy trên biểu đồ, bot quét trực tiếp các giao dịch khổng lồ (>10 triệu USD) đang "chờ duyệt" trên mạng lưới blockchain (Uniswap/DEX).
* **Điểm hay:** Nếu bắt được dòng tiền cá mập, nó ép bot phải "Front-run" (chạy trước), đẩy xác suất tự tin lên 99% để vào lệnh ngay lập tức trước khi đám đông nhận ra.

### 9. Mắt thần Sổ lệnh (Level-2 Order Book OFI)
* **Chức năng:** Đọc độ sâu sổ lệnh (20 mức giá gần nhất) trên Binance để tính toán sự mất cân bằng luồng lệnh (Order Flow Imbalance - OFI).
* **Điểm hay:** Khám phá ra các "Bức tường chặn giá". Ví dụ AI báo Long, nhưng Bot soi thấy cá mập đang treo lệnh Bán khổng lồ cản đường (OFI <= -0.30), nó sẽ Hủy lệnh ngay.

---

## 💼 MODULE 4: QUẢN TRỊ DANH MỤC & THỰC THI (PORTFOLIO & EXECUTION)

### 10. Tổng tư lệnh Danh mục (GNN Meta-Agent)
* **Chức năng:** Tính toán ma trận tương quan (Correlation Matrix) giữa 5 đồng coin.
* **Điểm hay:** Nếu AI báo Long cả BTC và ETH, nhưng Meta-Agent thấy 2 con này đang chạy giống hệt nhau (>75%), nó sẽ chủ động chia đôi vốn giải ngân, ngăn chặn tình trạng "chết chùm" nếu phân tích sai.

### 11. Quản lý Vốn Động (Dynamic Tiered Kelly)
* **Chức năng:** Không đánh một số tiền cố định. Dựa vào độ tự tin của AI để phân bổ vốn:
  * Thăm dò ($20)
  * Khá tự tin ($35)
  * Tất tay ($50)

### 12. Thực thi Thông minh chống Trượt giá (Smart Limit Iceberg)
* **Chức năng:** Thay vì phang lệnh Market trượt giá nặng, bot tự động "xé nhỏ" vốn làm 4 phần, đặt lệnh Limit rải đều quanh mức giá Mid-price trong vòng 45 giây. Nếu sát giờ chưa khớp mới dùng Market.

### 13. Thoát lệnh Đa chiều (Dynamic Exits)
* **Chức năng:** Kết hợp 3 loại thoát lệnh: Chốt lời cứng (Take Profit), Cắt lỗ cứng (Stop Loss), và Khóa lãi động (Trailing Stop). Mức tỷ lệ được điều chỉnh riêng biệt cho tính cách của từng đồng coin (VD: SOL biến động mạnh sẽ có SL/TP rộng hơn BTC).

---

## 📡 MODULE 5: HỆ THỐNG THƯƠNG MẠI HÓA (TELEGRAM ROUTING)

### 14. Phân luồng Tín hiệu (Basic vs Standard Routing)
* **Chức năng:** Tự động phân loại và bắn tín hiệu về 2 kênh Telegram riêng biệt (Free và VIP) tùy thuộc vào giá trị của thông tin và loại tài sản.