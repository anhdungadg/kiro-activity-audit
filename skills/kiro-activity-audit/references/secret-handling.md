# Xử lý secret trong log Kiro

Prompt-logs chứa **nguyên văn** nội dung người dùng dán vào chat **và** nội dung file đang mở trong editor
(inline completion gửi `leftContext`/`rightContext`). Credential thật sẽ có ở đó - vấn đề là bao nhiêu.

## Quy tắc tuyệt đối

1. **Không in giá trị secret** ra terminal, chat, file, hay báo cáo. Chỉ in: loại, 4 ký tự đầu + độ dài,
   tên khoá, người dùng, thời điểm, nguồn, hạn dùng, account AWS.
2. **Không grep raw log** để "xem thử". Dùng `triage-secrets.py` - nó che trước khi in.
3. **Không thử dùng** khoá để kiểm tra còn sống. Giả định còn hiệu lực.
4. Raw snapshot `data/snapshot-*/` **luôn chứa secret thật** - không commit, không chia sẻ, không đưa vào ticket.
5. File số liệu (`metrics.json`, `secret-triage.json`) đã che - nhưng vẫn có tên hệ thống, tên người → lưu hành hạn chế.

## Quy trình

```bash
cd "$SNAP"
python3 "$K/scripts/export_json.py" --out ../metrics.json --bucket "$BUCKET"   # in "⚠️ SECRET: n lớp CÓ HIT"
python3 "$K/scripts/triage-secrets.py" --out ../secret-triage.json         # phân loại, đã che
```

`triage-secrets.py` cho mỗi giá trị khác nhau:

| Trường | Ý nghĩa |
|---|---|
| `class` | AWS access key · AWS temp key · Google API key · JWT · Secret assignment · Connection string · npm authToken · Presigned URL · Private key · Slack/GitHub token |
| `value` | `ABCD…(độ dài)` - đã che |
| `users`, `first`, `last`, `count` | ai, khi nào (giờ địa phương), bao nhiêu lần |
| `sources` | `chat-gõ/dán` · `file-tự-đính-kèm` · `inline-file-đang-sửa` - phân biệt **người dán** với **IDE tự gửi** |
| `awsAccount` | account suy từ access key ID (thuật toán công khai) |
| `secretKeyNearby` | có chuỗi 40 ký tự gắn với khoá `secret*`/`*.key` trong ±2.500 ký tự → **cặp khoá đầy đủ** |
| `exp`, `expired`, `iss` | với JWT - đọc thời hạn trong mã |
| `expires`, `expired` | với presigned URL - từ `X-Amz-Date` + `X-Amz-Expires` |
| `scheme`, `host`, `hasPassword` | với connection string |
| `keyName`, `near` | với secret assignment - ngữ cảnh đã che mọi secret lân cận |

## Phân mức

| Mức | Tiêu chí | Ví dụ đã gặp |
|---|---|---|
| 🔴 **Nghiêm trọng** | Không có hạn **và** (production **hoặc** cặp khoá đầy đủ **hoặc** quyền cao) | Mật khẩu Postgres prod tài khoản owner trong Helm values · AWS SES key pair dài hạn · ~13 khoá API đối tác trong `application.properties` |
| 🔴 **Cao** | Không có hạn, chưa rõ môi trường | GitLab `glpat-` trong `.npmrc` (và **xuất hiện lại sau khi đã báo** → có thể chưa thu hồi) |
| 🟠 Cao | Khoá ứng dụng / khoá ký | Keycloak client secret · `JWT_SECRET` · k8s Secret base64 |
| 🟠 Cần xác minh | Khoá thuộc account AWS **không có trong danh sách đã biết** | presigned URL ký bằng AKIA của account lạ |
| 🟡 Thấp | Đã hết hạn | JWT `expired: true` · presigned URL hết hạn · cookie phiên |
| ✓ Không phải vấn đề | Không có mật khẩu, hoặc localhost/non-prod không kèm credential | `postgresql://localhost:5432/db` |
| ✓ Loại trừ | Nằm trong **output do AI sinh** (completion), không phải input người dùng | Google API key trong gợi ý code |

Đọc theo thứ tự: AWS key có `secretKeyNearby: true` → connection string `hasPassword: true` có host chứa
`prod` → token không hạn (`glpat-`, `npm _authToken`) → secret assignment → phần còn lại.

**So với kỳ trước:** nếu một giá trị đã báo kỳ trước **xuất hiện lại** (`last` sau ngày báo) → nhiều khả năng
**chưa được thu hồi**. Nêu riêng.

## Trong báo cáo

- Bảng: mức · nội dung (loại + hệ thống, **không có giá trị**) · nhân viên · thời hạn.
- Việc cần làm theo thứ tự, mỗi việc ghi **bộ phận chịu trách nhiệm** (DBA, admin account AWS, chủ hệ thống tích hợp…).
- Nêu rõ: *"Không kiểm tra khoá nào còn hoạt động - giả định còn hiệu lực."*
- Nêu nguyên nhân gốc, **không đổ lỗi cá nhân**: nhân viên không biết file đang mở bị ghi lại, và chưa có
  hướng dẫn nào cấm dán file cấu hình vào chat.
- Đề nghị cố định: ban hành hướng dẫn (danh sách không mở/dán: `.env`, `.npmrc`, `application*.properties`,
  k8s Secret, Helm values có credential, chuỗi kết nối DB, `curl` có token) + thông báo thu thập log +
  bật quét secret tự động.
