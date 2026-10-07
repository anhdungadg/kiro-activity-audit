---
name: kiro-activity-audit
description: Audit Kiro Enterprise activity logs in an S3 bucket (prompt-logs + user activity CSV) for any AWS account - cost per user, credit quota blocking, model mix, safe right-sizing, leaked credentials in prompts, and purpose of use - then produce a technical dashboard and an approval report. Use when asked to audit, analyze, or report on Kiro usage, Kiro credits/cost/subscriptions, a kiro-activity-* bucket, or Kiro prompt logs.
---

# Kiro Activity Audit

Audit log hoạt động Kiro Enterprise cho **bất kỳ AWS account nào**, chỉ đọc. Kết quả: số liệu chi phí đã xác minh,
danh sách người bị chặn vì hết credit, phương án giảm chi phí, phân loại credential bị lộ (đã che), đánh giá
mục đích sử dụng, và hai báo cáo (kỹ thuật + trình phê duyệt).

## Đầu vào - hỏi người dùng nếu chưa có

| Biến | Ví dụ | Ghi chú |
|---|---|---|
| `BUCKET` | `kiro-activity-123456789012-us-east-1-ab` | không có `s3://` |
| `PROFILE` | `readonly` | AWS CLI profile đọc được bucket. **Ưu tiên profile ReadOnly** |
| `CUSTOMER` | `ACME` | tên hiển thị trong báo cáo |
| `KIRO_AUDIT_TZ` | `7` | múi giờ hiển thị (7 = VN, 8 = SG) |
| `WORK_PATTERN` | `acme\|\bacm-\|com\.acme\.` | regex nhận diện dự án công ty - chỉ cần cho bước mục đích sử dụng |

`ACCOUNT` suy từ tên bucket. `WORKDIR` mặc định `./kiro-audit-<ACCOUNT>`.

## Vị trí script

Script nằm cạnh file này. Đặt biến `K` trước mọi lệnh:

```bash
K=$(ls -d ~/.kiro/skills/kiro-activity-audit .kiro/skills/kiro-activity-audit 2>/dev/null | head -1)
ls "$K/scripts"
```

## Quy tắc an toàn - bắt buộc

1. **Chỉ đọc.** Chỉ dùng `aws s3 ls|sync|cp` và `aws sts get-caller-identity`. Không ghi/xoá gì trên bucket,
   không đổi cấu hình bucket, không gọi API ghi nào.
2. **Không bao giờ in, lưu, hay trích dẫn giá trị secret.** Không grep raw log ra terminal. Dùng
   `triage-secrets.py` (tự che). Không thử dùng khoá bị lộ. Xem `references/secret-handling.md`.
3. Raw snapshot chứa source code, prompt nguyên văn, và credential thật → tạo `data/.gitignore` chặn
   `snapshot-*/`, `*.json.gz`, `metrics.json`, `secret-triage.json`, `purpose*.json`. Không commit, không chia sẻ.
4. Báo cáo chứa email nhân viên + đánh giá cá nhân → ghi nhãn lưu hành hạn chế.
5. **Không xử lý hồi tố** nếu tổ chức chưa ban hành hướng dẫn và chưa thông báo thu thập log.

## Quy trình

### Bước 0 - Khảo sát (1 phút)

```bash
aws sts get-caller-identity --profile "$PROFILE"
aws s3 ls "s3://$BUCKET/" --profile "$PROFILE"
aws s3 ls "s3://$BUCKET/prompt-logs/AWSLogs/$ACCOUNT/KiroLogs/" --profile "$PROFILE"   # không đệ quy
```

- Có `prompt-logs/` → **biến thể A** (làm đủ các bước).
- Chỉ có `user-activity-reports/` → **biến thể B**: bỏ bước 3–4, nêu rõ không đánh giá được mục đích sử dụng.
- Có `reports/` → nguồn đối chiếu độc lập, dùng cho `scenarios.py --crosscheck`.

**Không** chạy `aws s3 ls --recursive` hay `aws s3 sync` cả bucket - timeout từ ~150k object.

### Bước 1 - Sync snapshot

```bash
export BUCKET PROFILE KIRO_AUDIT_TZ WORKDIR=${WORKDIR:-./kiro-audit-$ACCOUNT}
"$K/scripts/sync-snapshot.sh"                 # CSV trước, rồi prompt-logs theo ngày, 6 luồng song song
WORKDIR=$(cd "$WORKDIR" && pwd)               # tuyệt đối - các bước sau có cd
SNAP="$WORKDIR/$(cat "$WORKDIR/data/.latest-snapshot")"
# Chỉ cần chi phí:  CSV_ONLY=1 "$K/scripts/sync-snapshot.sh"
```

Lần đầu lấy `DAYS=30` ngày gần nhất; các lần sau chỉ tải từ ngày snapshot trước − 1.
Có prefix FAIL thì chạy lại - chỉ tải phần thiếu. Lần đầu với ~150k object mất khoảng 30–40 phút;
**chạy nền** và báo người dùng thời gian dự kiến.

### Bước 2 - Chi phí (quan trọng nhất, chạy trước)

```bash
cd "$WORKDIR"
python3 "$K/scripts/analyze-csv.py"     --snapshot "$SNAP" --json data/csv-metrics.json   # bảng thô từng user
python3 "$K/scripts/monthly-rollup.py"  --snapshot "$SNAP" --json data/monthly-metrics.json   # theo THÁNG, không ngoại suy
python3 "$K/scripts/model-efficiency.py" --snapshot "$SNAP" --json data/model-metrics.json   # hệ số model + user BỊ CHẶN
python3 "$K/scripts/scenarios.py" --monthly data/monthly-metrics.json --model data/model-metrics.json \
        --json data/scenarios.json [--crosscheck <file>]                                   # 3 phương án
```

Đọc và kết luận theo thứ tự:
1. **Ai đã bị chặn** (`model-metrics.json → censored`): credit tích luỹ dừng quanh hạn mức rồi 0 mọi ngày sau.
   Đây là sự kiện đã xảy ra, không phải dự báo. Hạn mức reset theo tháng dương lịch.
2. **Đổi model** (phương án C): credit/message chênh tới 10× giữa người dùng `auto` và Opus - thường giải quyết
   người vượt hạn mức với $0.
3. **Overage có trần vs nâng gói**: hoà vốn ở 2.500 credit vượt/tháng (PRO_MAX → POWER).
4. **Hạ gói** (phương án B, đã giữ nguyên người bị chặn và người < `--min-active-days` ngày hoạt động).
5. **Không** đề nghị huỷ license chỉ vì im lặng vài ngày.

`analyze-csv.py` ngoại suy - chỉ để tham khảo; kết luận bằng `monthly-rollup.py` + `scenarios.py`.

### Bước 3 - Secret (biến thể A)

```bash
cd "$SNAP"
python3 "$K/scripts/export_json.py" --out ../metrics.json --bucket "$BUCKET"   # metrics + quét 16 lớp (đã che)
python3 "$K/scripts/triage-secrets.py" --out ../secret-triage.json             # phân loại từng giá trị (đã che)
cd - >/dev/null
```

Nếu có `⚠️ SECRET: n lớp CÓ HIT` → **ưu tiên cao nhất**, đưa lên đầu báo cáo. Phân mức theo
`references/secret-handling.md`. So với `secret-triage.json` kỳ trước: giá trị cũ **xuất hiện lại** → có thể chưa thu hồi.

### Bước 4 - Mục đích sử dụng (biến thể A, chỉ khi được yêu cầu)

```bash
python3 "$K/scripts/purpose.py" --root "$SNAP" --work "$WORK_PATTERN" --json data/purpose.json [--since YYYY-MM-DD]
```

Chỉ gắn cờ khi **dưới 20% lượt mở file thuộc dự án công ty VÀ có tín hiệu cá nhân**. Kết quả là danh sách
**cần trao đổi**, không phải căn cứ xử lý. Bắt buộc nêu 4 giới hạn trong `references/reporting.md`.

### Bước 5 - Báo cáo

```bash
python3 "$K/scripts/build-dashboard.py" --data-dir data --out-dir reports \
  --account "$ACCOUNT" --bucket "$BUCKET" --customer "$CUSTOMER" --profile "$PROFILE"
```

Sinh `reports/kiro-activity-report.html` (kỹ thuật) và `reports/bao-cao-chi-phi-va-muc-dich-su-dung.html`
(trình phê duyệt). Nếu cần bản phê duyệt có phần sự cố bảo mật / diễn giải riêng: viết
`reports/bao-cao-chi-phi-va-muc-dich-su-dung.md` theo cấu trúc trong `references/reporting.md`, rồi:

```bash
python3 "$K/scripts/build-approval-html.py" --workdir .
```

**Không viết số vào HTML bằng tay.** Verify theo cuối `references/reporting.md` (chart, JS, secret thô = 0,
không lọt tên khách hàng trước).

### Bước 6 - Để lần sau chạy lại nhanh

Tạo `$WORKDIR/README.md` (kết quả chính + việc tồn đọng + mốc snapshot) và `$WORKDIR/RUNBOOK.md`
(các lệnh trên với giá trị thật). Nếu muốn Kiro tự nhớ bối cảnh: steering trong `.kiro/steering/` - giữ dưới ~1.500 từ.

## Trả lời người dùng

- Ngôn ngữ của người dùng. Dẫn mọi con số kèm nguồn (file JSON / lệnh).
- Mở đầu bằng việc cấp thiết nhất (secret nghiêm trọng → người bị chặn → chi phí).
- Nói rõ phần nào là dự phóng, phần nào đã xác minh; phần nào **không kiểm tra** (vd: khoá còn sống hay không).
- Nếu kỳ trước có khuyến nghị sai, nói ra.
- Kết thúc bằng việc tồn đọng có mốc thời gian.

## Tham chiếu

- `references/pitfalls.md` - **đọc trước khi khuyến nghị**: mọi bẫy đã gặp thật
- `references/secret-handling.md` - phân mức credential, cách báo cáo không lộ giá trị
- `references/data-sources.md` - cấu trúc bucket, schema CSV và prompt-logs, bảng giá
- `references/reporting.md` - cấu trúc 2 báo cáo, giới hạn bắt buộc, verify

## Script

| Script | Việc |
|---|---|
| `sync-snapshot.sh` | snapshot mới, sync delta theo ngày, song song |
| `analyze-csv.py` | bảng chi phí thô từng user (ngoại suy theo ngày làm việc) |
| `monthly-rollup.py` | gộp theo tháng dương lịch, seat idle, email ngoài domain |
| `model-efficiency.py` | hệ số credit/model (NNLS), **user bị chặn**, tốc độ đốt credit |
| `scenarios.py` | 3 phương án: thu hồi seat / hạ gói an toàn / đổi model |
| `export_json.py` | `metrics.json` từ prompt-logs + quét secret (output đã che) |
| `triage-secrets.py` | phân loại từng secret: người, thời điểm, nguồn, hạn, account (đã che) |
| `purpose.py` | dự án/file theo người, gắn cờ cần trao đổi |
| `build-dashboard.py` | 2 HTML từ JSON |
| `build-approval-html.py` | HTML bản phê duyệt từ `.md` + 3 chart |
| `analyze.py`, `map-users.py`, `export-transcript.py` | phụ trợ: in nhanh, map userId→email (khi CSV thiếu email), credit từ session local |
