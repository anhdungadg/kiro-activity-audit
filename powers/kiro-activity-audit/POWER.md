---
name: "kiro-activity-audit"
displayName: "Kiro Activity Audit"
description: "Read-only audit of Kiro Enterprise activity logs in S3 for any AWS account: cost per user, users blocked by credit quota, model mix, safe right-sizing, leaked credentials in prompt logs (always redacted), and purpose of use — with technical and approval reports."
keywords: ["kiro audit", "kiro activity", "kiro cost", "kiro credits", "kiro subscription", "prompt logs", "kiro-activity bucket", "leaked credentials"]
author: "VNM Kiro audit"
---

# Kiro Activity Audit

## Overview

Audit bucket log Kiro Enterprise (`kiro-activity-<account>-<region>-<suffix>`) cho **bất kỳ AWS account nào**.
MCP server `kiro-audit` đóng gói toàn bộ script và **tự che mọi giá trị secret** trước khi trả về —
model không bao giờ thấy credential thật, kể cả khi đọc log.

Kết quả: chi phí đã xác minh từ CSV chính thức, **người đã bị chặn** vì hết credit, 3 phương án giảm chi phí,
phân loại credential bị lộ theo mức độ, danh sách cần trao đổi về mục đích sử dụng, và 2 báo cáo HTML.

**Chỉ đọc trên AWS:** server chỉ gọi `aws sts get-caller-identity`, `aws s3 ls`, `aws s3 sync|cp` (tải xuống).

## Onboarding

### Yêu cầu
- `uv` (để chạy `uvx`): `curl -LsSf https://astral.sh/uv/install.sh | sh` hoặc `brew install uv`
- AWS CLI v2 và một profile **đọc được bucket** — ưu tiên ReadOnly. Kiểm tra: `aws s3 ls s3://<bucket>/ --profile <p>`
- `bash`, `python3` ≥ 3.10
- Truy cập được repo chứa server (xem `mcp.json`)

### Thông tin cần hỏi người dùng
| Thông tin | Ví dụ |
|---|---|
| bucket | `kiro-activity-123456789012-us-east-1-ab` |
| profile | `readonly` |
| tên tổ chức (hiển thị trong báo cáo) | `ACME` |
| thư mục làm việc | `~/audits/kiro-audit-123456789012` (mặc định `./kiro-audit-<account>`) |
| regex dự án công ty (chỉ khi cần đánh giá mục đích) | `acme\|\bacm-\|com\.acme\.` |

Múi giờ hiển thị: biến `KIRO_AUDIT_TZ` trong `mcp.json` (mặc định 7 = Việt Nam).

## Available MCP Servers

### kiro-audit

| Tool | Việc | Thời gian |
|---|---|---|
| `audit_check_access(bucket, profile)` | xác minh danh tính, prefix cấp đầu, biến thể A/B, có `reports/` không | giây |
| `audit_sync_start(bucket, profile, workdir?, csv_only?, jobs?, days?, region?)` | **job nền**: snapshot mới + sync delta | CSV: giây · đầy đủ: 30–40 phút với ~150k object |
| `audit_job_status(workdir, job_id?)` | trạng thái job, log đã che | giây |
| `audit_job_cancel(workdir, job_id)` | dừng job local | giây |
| `audit_cost(workdir, min_active_days?, crosscheck?, top?)` | chi phí, **người bị chặn**, hệ số model, 3 phương án | ~1 phút |
| `audit_scan_start(workdir, bucket?, work_pattern?, since?)` | **job nền**: metrics + phân loại secret (+ mục đích) | vài phút |
| `audit_secret_summary(workdir, min_severity?, previous_triage?)` | secret theo mức critical/high/medium/low/info, đánh dấu giá trị **xuất hiện lại** | giây |
| `audit_purpose_summary(workdir)` | người cần trao đổi + 4 giới hạn bắt buộc | giây |
| `audit_build_reports(workdir, bucket, customer, profile?)` | 2 HTML + kiểm tra chart và secret thô | giây |

## Workflow

```
1. audit_check_access(bucket, profile)
     → cảnh báo nếu role Admin; biến thể B (không có prompt-logs) thì bỏ bước 5–6
2. audit_sync_start(bucket, profile, workdir, csv_only=True)     # CSV trước — có số chi phí ngay
3. audit_cost(workdir)                                            # báo cáo sơ bộ cho người dùng trong lúc chờ
4. audit_sync_start(bucket, profile, workdir)                     # đầy đủ, chạy nền
   audit_job_status(workdir) … lặp cho tới done=True, prefixFail=0
5. audit_scan_start(workdir, bucket, work_pattern?)               # chạy nền
   audit_job_status(workdir) … tới done=True
6. audit_secret_summary(workdir [, previous_triage])
   audit_purpose_summary(workdir)                                 # nếu có work_pattern
7. audit_build_reports(workdir, bucket, customer)
```

Kỳ sau chỉ cần lặp từ bước 2 — sync chỉ tải phần mới.

Chi tiết từng phần: đọc steering khi cần
- `workflow.md` — đọc kết quả từng tool và ra kết luận
- `pitfalls.md` — **bắt buộc đọc trước khi khuyến nghị**: các bẫy đã gặp thật
- `secret-handling.md` — phân mức credential, cách báo cáo không lộ giá trị
- `data-sources.md` — cấu trúc bucket, schema CSV/prompt-logs, bảng giá
- `reporting.md` — cấu trúc báo cáo kỹ thuật và bản trình phê duyệt, giới hạn bắt buộc

## Best Practices

- **Không bao giờ** in, lưu, hay trích giá trị secret; không thử dùng khoá bị lộ — giả định còn hiệu lực.
- Mở đầu câu trả lời bằng việc cấp thiết nhất: secret `critical` → người **đã bị chặn** → chi phí.
- Thứ tự xử lý vượt hạn mức: **đổi model ($0)** → overage có trần → nâng gói.
- Không đề nghị huỷ license chỉ vì im lặng vài ngày; không hạ gói người có < 10 ngày hoạt động.
- Mục đích sử dụng chỉ là tín hiệu để trao đổi — **không xử lý hồi tố** khi chưa có hướng dẫn và thông báo.
- Thư mục `workdir/data/snapshot-*` chứa source code, prompt nguyên văn và **credential thật** —
  `data/.gitignore` được tạo tự động; không commit, không chia sẻ.

## Troubleshooting

| Lỗi | Nguyên nhân / xử lý |
|---|---|
| `audit_check_access` → `canList: false`, AccessDenied | profile không có quyền đọc bucket, hoặc bucket policy phía account chủ chưa cấp |
| Sync `prefixFail > 0` | mạng chập chờn — gọi lại `audit_sync_start`, chỉ tải phần thiếu |
| Sync chạy rất lâu, file không tăng | đang liệt kê một ngày rất lớn; xem `logTail`. Không dùng `aws s3 sync` cả bucket |
| Không có CSV | User activity report là tuỳ chọn **riêng** với prompt logging trong Kiro Console; profile mới cần chờ chu kỳ ghi hằng ngày |
| `audit_cost` báo thiếu cột model | CSV thêm model mới — script tự đọc mọi cột `*_messages`; nếu vẫn lỗi, kiểm tra header CSV |
| Server không khởi động | kiểm tra `uv --version`, quyền truy cập repo trong `mcp.json`, và `aws --version` |
