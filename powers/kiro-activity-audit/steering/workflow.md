# Đọc kết quả và ra kết luận

## audit_check_access
- `warning` có → nói với người dùng nên đổi sang profile ReadOnly (vẫn chạy được, server không ghi gì lên AWS).
- `variant` B → không có prompt-logs: bỏ quét secret và mục đích sử dụng; nêu rõ trong báo cáo là **không đánh giá được mục đích**.
- `hasReportsPrefix` → có báo cáo tổ chức tự lập. Tải 1 file về (người dùng tự chọn) rồi truyền vào `audit_cost(crosscheck=…)` —
  đối chiếu khớp là bằng chứng mạnh nhất cho độ tin cậy.

## audit_sync_start / audit_job_status
- Làm `csv_only=True` trước để có số chi phí sau vài giây, rồi mới sync đầy đủ.
- Sync đầy đủ lần đầu với ~150k object mất 30–40 phút — báo người dùng, rồi poll `audit_job_status` thưa (mỗi 1–2 phút).
- `done=True` và `prefixFail=0` mới coi là xong. `prefixFail>0` → gọi lại `audit_sync_start`.

## audit_cost — thứ tự đọc
1. `blockedUsers` — **đã xảy ra**, không phải dự báo. Ai, credit bao nhiêu, bao nhiêu ngày hoạt động.
   Hạn mức reset theo tháng dương lịch. `overageEnabled=false` → bị chặn, không phát sinh phí.
2. `nearLimit` — ≥ 80% hạn mức nhưng chưa chạm.
3. `modelCreditPerMessage` — hệ số credit/message từng model (NNLS, `modelFitR2`). Người có credit/message > 1,0
   trong nhóm vượt hạn mức là ứng viên **đổi model** — thường về dưới ngưỡng với $0.
4. `optionC_modelShift` — tiết kiệm khi dịch 10–50% message Opus sang `auto`/Sonnet.
5. `optionB_rightSize` — `safeSavingYearlyUsd` đã **giữ nguyên** người bị chặn (`censoredUsersKept`) và người
   < `minActiveDays` ngày (`thinDataUsersKept`). Chỉ đề nghị phần này.
6. `optionA_reclaimSeats` — seat < x% hạn mức. **Xác nhận với quản lý** trước khi thu hồi; không thu hồi chỉ vì im lặng vài ngày.
7. `nonCorpEmails`, `idleUsers` — email ngoài domain (có thể là nhà thầu hợp lệ), seat 0 credit.

Overage vs nâng gói: overage $0,04/credit; PRO_MAX ($100/5.000) → POWER ($200/10.000) chỉ rẻ hơn khi vượt > 2.500 credit/tháng.

## audit_secret_summary
- `critical` → đưa lên **đầu** câu trả lời và báo cáo, mỗi mục kèm việc cần làm + bộ phận chịu trách nhiệm:
  thay mật khẩu DB, vô hiệu khoá AWS + tra CloudTrail, thay khoá API đối tác, thu hồi token.
- `reappearedFromPrevious` → giá trị đã báo kỳ trước vẫn xuất hiện → **có thể chưa thu hồi**, nêu riêng.
- `sources` chứa `inline-file-đang-sửa` / `file-tự-đính-kèm` → người dùng **không dán**, IDE tự gửi nội dung file đang mở.
  Nêu nguyên nhân gốc là thiếu hướng dẫn, không đổ lỗi cá nhân.
- `low` (JWT/presigned URL hết hạn) → gộp một dòng.

## audit_purpose_summary
- `flagged` là danh sách **cần trao đổi**, không phải kết luận vi phạm. Luôn kèm 4 giới hạn trong `limits`.
- `noFileContext` không phải dấu hiệu bất thường.

## audit_build_reports
- `checks[*].rawSecretHits` phải = 0; `canvasWithoutChart` phải rỗng.
- Bản trình phê duyệt có phần sự cố bảo mật / diễn giải riêng: viết `reports/bao-cao-chi-phi-va-muc-dich-su-dung.md`
  theo `reporting.md`, gọi lại `audit_build_reports` — HTML sẽ sinh từ file `.md` đó.
