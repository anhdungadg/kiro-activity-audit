# Những thứ dễ sai — đã gặp thật qua các lần audit

Đọc trước khi đưa ra bất kỳ khuyến nghị nào. Mỗi dòng là một lỗi đã xảy ra thật.

## Dữ liệu và nguồn

| Bẫy | Thực tế |
|---|---|
| Tưởng prompt-logs có dữ liệu chi phí | Không có field credit/token/cost nào. Credit chỉ có trong CSV `user_report/` |
| Tưởng có AWS CLI cho Kiro | `aws kiro`, `aws q`, `aws codewhisperer`, `aws user-subscriptions` đều không tồn tại. Số subscription xem ở Kiro Console |
| Mặc định bucket nào cũng có `prompt-logs/` | Có bucket **chỉ có CSV** (biến thể B). Kiểm tra biến thể trước, rồi mới chọn bước |
| Tưởng CSV chưa có là do cấu hình sai | Kiro ghi CSV theo lịch hằng ngày (~02:00 UTC). Profile mới tạo thì chu kỳ chưa chạy. Prompt logging và User activity report là **hai tuỳ chọn riêng** |
| Đếm usage từ prompt-logs | Có thể bỏ sót tới 27,6% message. `Total_Messages` trong CSV mới là nguồn có thẩm quyền. Inline completion **không** tính vào `Total_Messages` nhưng vẫn sinh credit |
| Đọc thiếu CSV | Từ một thời điểm CSV tách theo `Client_Type` (`KIRO_IDE`, `KIRO_CLI`, `PLUGIN`) — 1–3 file/ngày. Đọc thiếu = thiếu người |
| Gộp `by_user_analytic/` với `user_report/` | Schema khác hẳn (không có cột chi phí, `Date` dạng `MM-DD-YYYY`). Từng gặp 42/44 cột toàn 0. `Chat_MessagesSent` ≠ `Total_Messages` (lệch 4,8×). **Không gộp hai nguồn** |
| Bỏ qua prefix `reports/` trong bucket | Nhiều tổ chức tự lập báo cáo để ở đó — **nguồn đối chiếu độc lập tốt nhất**. Truyền qua `scenarios.py --crosscheck` |
| Tưởng "ngày" trong CSV = ngày làm việc | `Date` theo lịch UTC. Với múi giờ +7, việc làm 00:00–07:00 giờ địa phương bị tính vào **ngày hôm trước** |
| Coi mọi ngày thiếu file là ngày không ai làm | Từng gặp `by_user_analytic` thiếu hẳn 1 ngày trong khi `user_report` cùng ngày có hoạt động |
| Index trực tiếp `record[...]['messageMetadata']` | Crash `KeyError` với biến thể schema. Dùng `.get()` |
| Chốt số liệu không ghi mốc snapshot | Bucket ghi live. Luôn ghi mốc snapshot trong mọi báo cáo |

## Tải dữ liệu

| Bẫy | Thực tế |
|---|---|
| `aws s3 sync` cả bucket | Từ ~150k object, bước **liệt kê** `prompt-logs/` bị read-timeout — từng chạy >20 phút rồi chết mà chưa tải được file nào. Dùng `scripts/sync-snapshot.sh` (theo ngày, song song) |
| `aws s3 cp --recursive --exclude "*/*"` để lấy file cấp đầu | Vẫn liệt kê toàn bộ cây bên dưới → timeout. Dùng `aws s3 ls` không đệ quy (có delimiter) |
| Sync vào thư mục trống mỗi lần | Chậm gấp nhiều lần. Copy snapshot cũ rồi sync delta (script đã làm) |
| Chỉ cần chi phí mà vẫn sync cả prompt-logs | `CSV_ONLY=1` — vài giây thay vì hàng chục phút |

## Chi phí và gói

| Bẫy | Thực tế |
|---|---|
| Ngoại suy chia **ngày lịch** rồi nhân **ngày làm việc** | Trộn đơn vị. Cuối tuần thấp hơn ngày làm việc tới 58× → hạ thấp ngoại suy ~29% |
| Ngoại suy khi dữ liệu trải nhiều tháng | `analyze-csv.py` so credit N ngày với hạn mức 1 tháng → ra 200% vô nghĩa. Dùng `monthly-rollup.py` để kết luận |
| Đề xuất đổi gói khi mới có 1–5 ngày dữ liệu | Từng đề nghị nâng gói ở ngày 1 (161%), ngày 2 về 98% → rút lại. Từng xếp một người vào nhóm "dùng ít, an toàn để hạ" — 3 tuần sau người đó **bị chặn** (13,80 → 5.003 credit). Hạ gói chỉ khi **≥ ~2 tuần hoạt động** |
| Khuyến nghị huỷ license vì "im lặng vài ngày" | Từng đề nghị huỷ 4 người — **cả 4 dùng lại**. "Im lặng" không phân biệt được nghỉ phép, chuyển việc, hay **đã bị chặn vì hết credit**. Bỏ hẳn loại khuyến nghị này |
| Hạ gói cho user đã bị chặn | Số của họ là **dữ liệu bị cắt** (right-censored) — nhu cầu thật cao hơn. `scenarios.py` giữ nguyên gói cho nhóm này |
| Coi "bị chặn" là rủi ro dự báo | Đã xảy ra thật: mẫu nhận diện là credit tích luỹ dừng ±10 quanh hạn mức rồi **0,00 mọi ngày sau**. `model-efficiency.py` tự phát hiện |
| Quên hạn mức reset theo tháng | Hạn mức tính theo **tháng dương lịch** (người bị chặn dừng ở 5.001 tính từ ngày 01) |
| Đề xuất nâng gói trước | **Đổi model** thường giải quyết được với $0 — credit/message chênh tới 10× giữa người dùng `auto` và người dùng Opus. Thứ tự: đổi model → overage có trần → nâng gói |
| Mặc định overage đắt hơn nâng gói | Overage $0,04/credit. Hoà vốn với nâng PRO_MAX→POWER ở **2.500 credit vượt/tháng**. Tính cả hai trước khi đề xuất |
| Right-size coi là tiết kiệm ổn định | Từng đảo chiều 3 lần trong 3 kỳ (tiết kiệm → tiết kiệm → tốn thêm) khi người dùng còn tăng. Chỉ hành động trên phần **bền với mọi cách tính** |
| Ước lượng credit rồi trình như số liệu | Từng ước 230–320, số thật 90,76 (lệch 3×). Luôn chờ CSV |

## Bảo mật và secret

| Bẫy | Thực tế |
|---|---|
| **"Kiro chỉ gửi đường dẫn file đang mở, không gửi nội dung"** | **SAI.** Inline completion gửi `leftContext`/`rightContext` = nội dung file đang sửa; chat có thể đính kèm nội dung file. Từng thấy mật khẩu DB production trong 7 file Helm values bị ghi 125 lần **chỉ vì file đang mở** |
| Thấy tên `.env` trong log là báo động | Phân biệt **tên file** (trong tag `<file name="…">`) với **nội dung**. Đừng báo động sai, nhưng kiểm tra cả nội dung |
| Grep raw log ra terminal để xem secret | Output đi vào transcript/log của chính phiên audit. **Không bao giờ** in giá trị — dùng `triage-secrets.py` (tự che) |
| Công cụ audit tự lưu secret | Từng có `metrics.json` lưu nguyên văn secret và dashboard nhúng lại. Che **trước khi ghi** (đã sửa trong `export_json.py`) |
| Regex AWS secret chỉ tìm `secret_access_key` | Bỏ sót cặp `aws.ses.id` + `aws.ses.key`. `triage-secrets.py` tìm chuỗi 40 ký tự gắn với bất kỳ khoá `*.key`/`secret*` trong ±2.500 ký tự |
| Base64 trong k8s Secret là an toàn | Base64 không phải mã hoá. Coi là lộ |
| Báo lộ cả key do AI tự sinh | Từng gặp Google API key nằm trong **completion do model sinh**. Kiểm tra nguồn trước khi kết luận |
| Kiểm tra key còn sống bằng cách gọi thử | **Không.** Ngoài phạm vi và không phù hợp. Giả định còn hiệu lực cho tới khi chủ hệ thống thay |
| Coi 0 hit ở các kỳ đầu là an toàn | 4 kỳ đầu sạch, kỳ 5 có hit, kỳ 7 có 113 giá trị / 21 người. Quy mô tăng theo số người và khối lượng log |
| Khuyến nghị SSE-KMS cho an toàn hơn | **Làm hỏng** Kiro User Activity Dashboard (Cloud Intelligence Dashboards) — chỉ hỗ trợ không mã hoá hoặc SSE-S3 |

## Mục đích sử dụng

| Bẫy | Thực tế |
|---|---|
| Quét từ khoá tên công ty | Âm tính giả: 4 người bị chấm 0 nhưng đường dẫn file xác nhận làm việc công ty |
| Từ khoá "english", "crypto" | Nhiễu (lệnh "reply in English", thư viện mã hoá). Chỉ kết luận khi **tên dự án + nội dung** cùng hướng — `purpose.py` gắn cờ theo tỷ lệ lượt mở file |
| Không có file path = đáng ngờ | Không. Một số client không gửi ngữ cảnh file. Người tiêu credit thứ 2 từng có 0 file path. Phải hỏi trực tiếp |
| Xử lý ngay khi thấy dùng ngoài mục đích | Khi chưa có hướng dẫn và chưa thông báo thu thập log → **không xử lý hồi tố** |
| Hai email gần giống nhau | Từng gặp cặp kiểu `abc@`/`bac@`, `x1993@`/`x5693@` — người khác nhau, credit chênh 4×. Luôn đối chiếu `UserId` |
| Email ngoài domain công ty = dùng sai | Có thể là nhà thầu hợp lệ. Đề nghị tra IAM Identity Center |

## Báo cáo

| Bẫy | Thực tế |
|---|---|
| Viết số vào HTML bằng tay | Số lệch khỏi dữ liệu sau vài lần sửa, và số của **khách hàng trước** lọt sang khách sau. Sinh HTML bằng script; grep tên khách cũ phải ra 0 hit |
| Hardcode tên model/màu theo số user | Dashboard từng vỡ khi số user 5 → 22 (`UC[5]` undefined) và khi xuất hiện model Opus mới (NaN). Luôn verify chart sau khi dữ liệu đổi quy mô |
| Đổi định dạng số vi-VN bằng regex cả file | Số mục heading `2.6` thành `2,6`; số viết sẵn `5.000` bị đổi hai lần |
| Gửi báo cáo kỹ thuật cho cấp quản lý | Hai đối tượng, hai tài liệu. Xem `reporting.md` |
| Diễn đạt "an toàn để phê duyệt ngay" | Từng dùng cho khuyến nghị về sau sai. Nêu độ tin cậy theo **từng** đề nghị |
| Hiển thị thời gian UTC | Che mất nhịp làm việc thật. Hiển thị giờ địa phương (`KIRO_AUDIT_TZ`) |
