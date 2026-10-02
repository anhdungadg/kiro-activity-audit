# kiro-activity-audit

Audit log hoạt động **Kiro Enterprise** cho bất kỳ AWS account nào — **chỉ đọc**, mọi secret **tự che**.

- Chi phí từng người từ CSV chính thức, **người đã bị chặn** vì hết credit, hệ số credit theo model
- 3 phương án giảm chi phí: thu hồi seat · hạ gói an toàn · đổi model
- Phân loại credential bị lộ trong prompt-logs (mật khẩu DB, AWS key, token…) theo mức độ — **không bao giờ in giá trị**
- Đánh giá mục đích sử dụng (tín hiệu để trao đổi, không phải căn cứ xử lý)
- Báo cáo kỹ thuật + bản trình phê duyệt (HTML, in PDF được)

Một repo, hai cách dùng — cùng một bộ script:

| | **Skill** | **Power** (+ MCP server) |
|---|---|---|
| Dùng trong | Kiro CLI và Kiro IDE | Kiro IDE (cài qua Powers panel); dùng được trong CLI sau khi cài |
| Agent làm việc qua | lệnh shell chạy script | 9 tool MCP có kiểu rõ ràng |
| Che secret | script tự che; phụ thuộc agent tuân thủ quy tắc | **server che mọi output** — model không thấy giá trị thật |
| Việc chạy lâu | agent tự chạy nền | job nền + `audit_job_status` |
| Cài | copy 1 thư mục | Powers panel → Add Custom Power |

## Yêu cầu

- AWS CLI v2 + profile **đọc được bucket** log Kiro (ưu tiên ReadOnly):
  `aws s3 ls s3://<bucket>/ --profile <p>`
- `python3` ≥ 3.10, `bash`
- Power: thêm [`uv`](https://docs.astral.sh/uv/) (`brew install uv`)

## Cài Skill

```bash
git clone <URL-repo> ~/src/kiro-activity-audit
mkdir -p ~/.kiro/skills
ln -s ~/src/kiro-activity-audit/skills/kiro-activity-audit ~/.kiro/skills/kiro-activity-audit
# cập nhật về sau: cd ~/src/kiro-activity-audit && git pull
```

Dùng: mở `kiro-cli chat` (hoặc Kiro IDE) và nói, ví dụ:
*"Audit Kiro cho bucket kiro-activity-123456789012-us-east-1-ab, profile readonly, tên tổ chức ACME"*.
Kiro tự nạp skill theo mô tả trong `SKILL.md`.

## Cài Power

1. Sửa `powers/kiro-activity-audit/mcp.json` → `args` trỏ tới URL repo **mà người dùng truy cập được**
   (`git+https://<forgejo>/<owner>/kiro-activity-audit.git`, có thể ghim `@<tag>`).
2. Kiro IDE → biểu tượng **Powers** → **Add Custom Power** → **Local Directory** → chọn
   `powers/kiro-activity-audit/` (hoặc nhập URL repo nếu dùng nguồn Git).
3. Mở power → **Try Power**, nói: *"Audit Kiro cho bucket …, profile …"*.

### Chỉ dùng MCP server (không qua Powers panel)

Thêm vào `~/.kiro/settings/mcp.json`:

```json
{
  "mcpServers": {
    "kiro-audit": {
      "command": "uvx",
      "args": ["--from", "git+https://<forgejo>/<owner>/kiro-activity-audit.git", "kiro-audit-mcp"],
      "env": { "KIRO_AUDIT_TZ": "7" },
      "autoApprove": ["audit_check_access", "audit_job_status", "audit_secret_summary", "audit_purpose_summary"]
    }
  }
}
```

## Cấu trúc

```
kiro-activity-audit/
├── skills/kiro-activity-audit/      ← NGUỒN DUY NHẤT của script + tài liệu
│   ├── SKILL.md                     quy trình cho agent
│   ├── scripts/                     13 script (sync, chi phí, secret, mục đích, báo cáo)
│   ├── references/                  pitfalls · secret-handling · data-sources · reporting
│   └── templates/approval-style.css
├── powers/kiro-activity-audit/
│   ├── POWER.md · mcp.json
│   └── steering/                    workflow.md + bản sao references (tools/sync-power-steering.sh)
├── src/kiro_audit/                  MCP server (server.py) + bộ che secret (redact.py)
├── pyproject.toml                   đóng gói server, kèm scripts/references/templates của skill
├── tools/sync-power-steering.sh
└── tests/
    ├── test_repo.py                 không cần AWS: compile, steering đồng bộ, che secret, không lọt dữ liệu khách, MCP stdio
    ├── e2e_mcp.py                   chạy thật trên một bucket qua MCP (chỉ đọc)
    └── check_charts.py              kiểm tra HTML báo cáo
```

## Phát triển

```bash
# sửa references → đồng bộ sang steering của power
tools/sync-power-steering.sh

# test không cần AWS
uv run --no-project --with 'mcp>=1.20,<2' python3 tests/test_repo.py

# test thật (chỉ đọc; tải CSV + 2 ngày log gần nhất)
uv build
E2E_BUCKET=<bucket> E2E_PROFILE=<profile> E2E_WORK='<regex-dự-án-công-ty>' \
E2E_SERVER="uvx --no-cache --from $PWD/dist/kiro_audit_mcp-0.1.0-py3-none-any.whl kiro-audit-mcp" \
uv run --no-project --with 'mcp>=1.20,<2' python3 tests/e2e_mcp.py /tmp/kiro-e2e
rm -rf /tmp/kiro-e2e      # chứa log thật, có thể có credential
```

**Phát hành:** tăng `version` trong `pyproject.toml` và `src/kiro_audit/__init__.py`, tag `vX.Y.Z`.
`uvx` cache theo version — không tăng version thì người dùng có thể chạy bản cũ.

**Trước khi commit:** `tests/test_repo.py` bước 4 phải đạt — repo **không được** chứa tên, email,
account, hay hệ thống của bất kỳ tổ chức nào đã audit.

## An toàn

- Server chỉ gọi `aws sts get-caller-identity`, `aws s3 ls`, `aws s3 sync|cp` (tải xuống). Không có lệnh ghi lên AWS.
- Tham số `bucket`/`profile`/`region`/`job_id` được kiểm tra bằng regex; không có lệnh nào chạy qua shell.
- Thư mục làm việc (`kiro-audit-<account>/data/snapshot-*`) chứa source code, prompt nguyên văn và
  **credential thật**. `data/.gitignore` được tạo tự động. Không commit, không chia sẻ, xoá khi xong.
- Báo cáo chứa email nhân viên và đánh giá cá nhân — lưu hành hạn chế.
