# Nguồn dữ liệu trong bucket log Kiro

Bucket do Kiro Console cấu hình, thường tên `kiro-activity-<account>-<region>-<suffix>`.

```
s3://<bucket>/
├── prompt-logs/AWSLogs/<account>/KiroLogs/              ← chỉ có nếu bật Prompt logging (biến thể A)
│   ├── <uuid>                                            ← file validation "Access validation successful."
│   ├── GenerateAssistantResponse/<region>/YYYY/MM/DD/HH/*.json.gz   ← chat
│   └── GenerateCompletions/<region>/YYYY/MM/DD/HH/*.json.gz         ← inline completion
├── user-activity-reports/AWSLogs/<account>/KiroLogs/
│   ├── user_report/<region>/YYYY/MM/DD/00/{KIRO_IDE,KIRO_CLI,PLUGIN}_<account>_user_report_YYYYMMDD0000.csv  ← CHI PHÍ
│   └── by_user_analytic/<region>/YYYY/MM/DD/00/<account>_by_user_analytic_YYYYMMDD0000_report.csv     ← năng suất
└── reports/                                              ← (có thể có) báo cáo tổ chức tự lập — nguồn đối chiếu
```

- **Biến thể A** — có `prompt-logs/`: phân tích được nội dung, secret, mục đích sử dụng.
- **Biến thể B** — chỉ CSV: chỉ có chi phí. Không có rủi ro lộ nội dung, nhưng **không đánh giá được mục đích** — nêu rõ.

```bash
aws s3 ls s3://<bucket>/ --profile <p>                                       # có prompt-logs/ không?
aws s3 ls s3://<bucket>/prompt-logs/AWSLogs/<acct>/KiroLogs/ --profile <p>    # không đệ quy — nhanh
```

## CSV `user_report/` — nguồn chi phí có thẩm quyền

Một dòng = một user × một ngày (lịch **UTC**) × một `Client_Type`. Header thật (2026-09):

```
Date,UserId,Client_Type,Chat_Conversations,Credits_Used,Overage_Cap,Overage_Credits_Used,
Overage_Enabled,ProfileId,Subscription_Tier,Total_Messages,New_User,User_Email,Usage_Limit,
auto_messages,claude_opus_5_messages,claude_sonnet_5_messages,gpt_5.6_terra_messages,…
```

| Cột | Ghi chú |
|---|---|
| `Date` | lịch UTC — với +7, việc 00:00–07:00 giờ VN rơi vào ngày trước |
| `UserId` | `<identityStoreId>.<uuid>` — khoá nối với prompt-logs (phần sau dấu chấm) |
| `User_Email` | có sẵn → thường **không cần** `map-users.py` |
| `Credits_Used` | credit thật, phân số bước 0,01 |
| `Subscription_Tier` | `FREE` · `PRO` · `PRO_PLUS` · `PRO_MAX` · `POWER` |
| `Usage_Limit` | hạn mức tháng của gói |
| `Overage_Enabled` | `false` → hết credit là **bị chặn**, không tính phí |
| `<model>_messages` | số message theo model — **cột thay đổi theo thời gian** khi có model mới. Không hardcode tên cột |
| `Total_Messages` | **không** gồm inline completion |

Ghi lúc ~09:00 giờ VN (02:00 UTC) cho ngày hôm trước.

## CSV `by_user_analytic/` — năng suất (chưa dùng cho chi phí)

46 cột, `Date` dạng `MM-DD-YYYY`. `Chat_AICodeLines`, `Dev_AcceptedLines`, `Dev_GeneratedLines`,
`CodeFix_*`, `CodeReview_*`, `DocGeneration_*`… Dữ liệu để trả lời *"Kiro có đáng tiền không"*.
**Cộng tổng từng cột trước khi dùng** — từng gặp 42/44 cột toàn 0 (telemetry chưa bật).
Không gộp với `user_report`.

## Prompt-logs

`.json.gz` = `{"records": [...]}`. Hai loại record:

**Chat** — `generateAssistantResponseEventRequest`: `prompt`, `chatTriggerType`, `userId`, `timeStamp`, `modelId`.
`prompt` chứa cả phần Kiro tự đính kèm: `<EnvironmentContext>`, `<OPEN-EDITOR-FILES>`, `<ACTIVE-EDITOR-FILE>`,
steering. Câu hỏi thật thường chỉ vài chục ký tự; prompt dài nhất từng gặp ~800 KB → **context tự động là chi phí ẩn**.
~70% record có `prompt` rỗng (vòng lặp tool). `conversationId` thường null, `assistantResponse` thường rỗng →
không dựng lại được hội thoại.

**Inline completion** — `generateCompletionsEventRequest`: `fileName`, `leftContext`, `rightContext`, `userId`,
`timeStamp`; response `completions[].content`. **`leftContext`/`rightContext` = nội dung file đang sửa** —
nguồn lộ secret lớn nhất (người dùng không gõ gì cả).

Biến thể schema: một số record thiếu `messageMetadata`, `customizationArn`. Luôn `.get()`.

## Khối lượng tham chiếu (một tổ chức ~47 người)

| Thời điểm | Object | Nội dung |
|---|---:|---:|
| Ngày 1 | 176 | — |
| Ngày 10 | 31.959 | 101 MB |
| Ngày 30 | 158.700 | 641 MB |

~5.000–6.500 object/ngày làm việc. Đề nghị lifecycle (Glacier IR 30 ngày, xoá 90 ngày).
**Không SSE-KMS** nếu dùng Kiro User Activity Dashboard của Cloud Intelligence Dashboards.

## Bảng giá (kiểm tra lại kiro.dev/pricing)

| Gói | $/tháng | Credit/tháng |
|---|---:|---:|
| FREE | 0 | 50 |
| PRO | 20 | 1.000 |
| PRO_PLUS | 40 | 2.000 |
| PRO_MAX | 100 | 5.000 |
| POWER | 200 | 10.000 |

Overage $0,04/credit. Hệ số model: `auto` 1×, Sonnet ~1,3×, Opus không công bố —
`model-efficiency.py` ước lượng từ dữ liệu (từng đo được Opus ≈ 5,3× `auto`).
Bảng giá nằm trong dict `TIERS` của `analyze-csv.py`, `monthly-rollup.py`, `scenarios.py` — đổi giá thì sửa cả ba.
