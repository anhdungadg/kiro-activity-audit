# Báo cáo - hai đối tượng, hai tài liệu

| | Báo cáo kỹ thuật | Báo cáo trình phê duyệt |
|---|---|---|
| Người đọc | team kỹ thuật / bảo mật | trưởng bộ phận ra quyết định |
| File | `reports/kiro-activity-report.{md,html}` | `reports/bao-cao-chi-phi-va-muc-dich-su-dung.{md,html}` |
| Sinh bằng | `build-dashboard.py` (HTML từ JSON) | viết `.md` → `build-approval-html.py` |
| Style | dashboard tối, nhiều chart | văn bản chính thức, nền sáng, in PDF (`Cmd+P`), ô chữ ký |

**Không viết số vào HTML bằng tay.** `build-dashboard.py` sinh cả hai HTML thẳng từ `data/*-metrics.json`.
Nếu cần bản phê duyệt viết tay (có phần sự cố bảo mật, diễn giải riêng), viết `.md` rồi
`build-approval-html.py --workdir .` - 3 chart lấy số từ `csv-metrics.json`.
Bảng 47 dòng user: **sinh bằng script** từ `csv-metrics.json`, đừng gõ.

## Cấu trúc bản trình phê duyệt

1. **Khung cảnh báo đầu trang** - việc cấp thiết nhất (sự cố bảo mật / người bị chặn), kèm thời hạn.
2. **Nếu kỳ trước có khuyến nghị sai - nói ngay ở đầu**, nêu rõ cái gì sai và vì sao.
3. **Tóm tắt** - bảng nội dung · số liệu · **độ tin cậy** từng dòng; danh sách đề nghị A/B/C… kèm tác động $/tháng, $/năm và ưu tiên.
4. **Từng đề nghị một mục**: bằng chứng → việc cần làm (ai, khi nào) → tác động chi phí → điều kiện chưa xác minh.
5. **Độ tin cậy của số liệu** - bảng nguồn × mức tin cậy. Nói thẳng phần nào là dự phóng.
6. **Bảng ý kiến phê duyệt** - mỗi dòng một việc, cột ☐ Đồng ý / ☐ Không đồng ý; bảng phương án chi phí; ô chữ ký.
7. **Phụ lục phương pháp** + nhãn **lưu hành hạn chế** (email nhân viên, đánh giá cá nhân, thông tin sự cố).

Quy tắc:
- Các đề nghị **chồng lấn** thì nói rõ, không cộng dồn tiết kiệm.
- Mỗi con số có thể truy về một file JSON hoặc một lệnh.
- **Không** chứa giá trị secret - chỉ loại, hệ thống, người, thời hạn.
- Nếu có phần đánh giá mục đích sử dụng, **bắt buộc** nêu nổi bật 4 giới hạn:
  1. Không có dấu hiệu công ty ≠ dùng cá nhân (POC, học công cụ, đối tác). Biến thể B: **không có dữ liệu** để đánh giá.
  2. Công cụ mới triển khai - thử nghiệm giai đoạn đầu là bình thường.
  3. Phương pháp không đủ tin cậy để xử lý: người không mở file trong workspace gần như không để lại dấu vết.
  4. Đây là giám sát qua nội dung → cần chính sách + thông báo trước, **không xử lý hồi tố**.

## Đề nghị cố định nên cân nhắc mỗi kỳ

- Đổi model trước khi nâng gói ($0) · overage có trần làm lưới an toàn · nâng gói là cuối cùng.
- Gói thấp làm mặc định cho người mới; rà soát hằng tháng (vượt 75% → nâng; dưới 20% hai tháng liền → hạ).
- **Không** huỷ license chỉ vì im lặng vài ngày.
- Ban hành hướng dẫn sử dụng + thông báo thu thập log + quét secret tự động.
- Lifecycle policy; kiểm tra bucket policy / versioning / public access block **từ account chủ sở hữu**.
- Phân tích `by_user_analytic` để đo giá trị thu được, không chỉ chi phí.

## Verify trước khi coi là xong

```bash
# 1. canvas id ↔ lệnh khởi tạo chart
for f in reports/*.html; do
  a=$(grep -o 'canvas id="[^"]*"' $f | sort); b=$(grep -o "mk('[^']*'" $f | sed "s/mk('/canvas id=\"/;s/'$/\"/" | sort)
  [ "$a" = "$b" ] && echo "✓ $f" || echo "✗ $f"; done
# 2. JS chạy được, không dataset undefined/NaN - xem tests/check_charts.py trong repo
# 3. Không còn secret thô
grep -cE '(AKIA|ASIA)[0-9A-Z]{16}|eyJhbGciOiJ[A-Za-z0-9_-]{20,}|glpat-[A-Za-z0-9_-]{10}' reports/* data/*.json
# 4. Không còn tên/account của khách hàng trước
grep -ril '<tên-khách-cũ>\|<account-cũ>' reports/ && echo "✗ lọt dữ liệu khách cũ"
```
