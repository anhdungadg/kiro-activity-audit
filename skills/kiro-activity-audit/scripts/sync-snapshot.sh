#!/usr/bin/env bash
# Tạo snapshot mới và sync delta từ bucket log Kiro (CHỈ ĐỌC từ S3).
#
# Vì sao không `aws s3 sync` cả bucket: từ ~150k object, bước LIỆT KÊ prefix prompt-logs/ bị
# read-timeout. Script này:
#   1. copy snapshot gần nhất (để sync chỉ tải phần mới)
#   2. sync user-activity-reports/ (CSV chi phí, vài giây)
#   3. sync prompt-logs/ THEO TỪNG NGÀY, song song, từ ngày snapshot trước − 1
#
# Biến môi trường (BUCKET, PROFILE bắt buộc):
#   BUCKET=kiro-activity-<acct>-<region>-xx  PROFILE=<aws-profile>  WORKDIR=./kiro-audit-<acct>
#   ACCOUNT  (mặc định: suy ra từ tên bucket)   REGION (mặc định us-east-1 - vùng của prefix log)
#   JOBS=6   CSV_ONLY=1   DAYS=30 (số ngày lấy khi chưa có snapshot nào)
#
# Ví dụ:
#   BUCKET=kiro-activity-123456789012-us-east-1-ab PROFILE=readonly WORKDIR=~/audit-123456789012 sync-snapshot.sh
set -eu

: "${BUCKET:?Thiếu BUCKET}"
: "${PROFILE:?Thiếu PROFILE}"
ACCOUNT="${ACCOUNT:-$(echo "$BUCKET" | grep -oE '[0-9]{12}' | head -1)}"
: "${ACCOUNT:?Không suy ra được ACCOUNT từ tên bucket - đặt ACCOUNT=}"
REGION="${REGION:-us-east-1}"
JOBS="${JOBS:-6}"
WORKDIR="${WORKDIR:-./kiro-audit-$ACCOUNT}"
BASE="prompt-logs/AWSLogs/$ACCOUNT/KiroLogs"

mkdir -p "$WORKDIR/data"
cd "$WORKDIR"
[ -f data/.gitignore ] || cat > data/.gitignore <<'EOF'
# Raw log Kiro: source code, prompt nguyên văn, CREDENTIAL THẬT - không commit, không chia sẻ
snapshot-*/
*.json.gz
metrics.json
secret-triage.json
purpose*.json
.latest-snapshot
EOF
PREV=$(ls -1d data/snapshot-* 2>/dev/null | tail -1 || true)
SNAP="data/snapshot-$(date -u +%Y%m%d-%H%MZ)"
[ -n "$PREV" ] && { echo "[+] copy $PREV → $SNAP"; cp -R "$PREV" "$SNAP"; } || mkdir -p "$SNAP"
echo "$SNAP" > data/.latest-snapshot

# AWS CLI mặc định 10 request song song/lệnh - object log rất nhỏ (~4 KB) nên nút cổ chai là số request
CFG=$(mktemp); [ -f ~/.aws/config ] && cp ~/.aws/config "$CFG"
python3 - "$CFG" "$PROFILE" <<'PY'
import configparser, sys
c = configparser.RawConfigParser(); c.read(sys.argv[1]); sec = f"profile {sys.argv[2]}"
if c.has_section(sec):
    c.set(sec, "s3", "\nmax_concurrent_requests = 64\nmax_queue_size = 10000")
    c.write(open(sys.argv[1], "w"))
PY
export AWS_CONFIG_FILE="$CFG"
trap 'rm -f "$CFG"' EXIT

echo "[+] sync CSV (user-activity-reports/)"
aws s3 sync "s3://$BUCKET/user-activity-reports/" "$SNAP/user-activity-reports/" \
  --profile "$PROFILE" --only-show-errors
echo "    $(find "$SNAP/user-activity-reports" -name '*.csv' | wc -l | tr -d ' ') CSV"
[ "${CSV_ONLY:-0}" = 1 ] && { echo "[✓] CSV_ONLY - xong: $SNAP"; exit 0; }

# ngày bắt đầu = ngày của snapshot trước − 1 (snapshot đặt tên theo UTC: snapshot-YYYYMMDD-HHMMZ)
if [ -n "$PREV" ]; then
  D0=$(basename "$PREV" | sed -E 's/snapshot-([0-9]{8}).*/\1/')
  START=$(python3 -c "import datetime as d;print((d.datetime.strptime('$D0','%Y%m%d').date()-d.timedelta(days=1)).isoformat())")
else
  START=$(python3 -c "import datetime as d;print((d.date.today()-d.timedelta(days=${DAYS:-30})).isoformat())")
fi
END=$(python3 -c "import datetime as d;print(d.datetime.now(d.timezone.utc).date().isoformat())")
echo "[+] sync prompt-logs theo ngày: $START → $END · $JOBS luồng"

LIST=$(mktemp); DONE=$(mktemp)
python3 - "$START" "$END" "$BASE" "$REGION" > "$LIST" <<'PY'
import sys, datetime as dt
a, b, base, reg = dt.date.fromisoformat(sys.argv[1]), dt.date.fromisoformat(sys.argv[2]), sys.argv[3], sys.argv[4]
d = a
while d <= b:
    for api in ("GenerateAssistantResponse", "GenerateCompletions"):
        print(f"{base}/{api}/{reg}/{d:%Y/%m/%d}/")
    d += dt.timedelta(days=1)
PY
# các file validation nằm thẳng dưới KiroLogs/ (không theo ngày). KHÔNG dùng cp --recursive
# (sẽ liệt kê cả 150k+ object bên dưới) - `s3 ls` không đệ quy dùng delimiter nên chỉ trả cấp đầu.
aws s3 ls "s3://$BUCKET/$BASE/" --profile "$PROFILE" | awk '$1 != "PRE" {print $4}' | while read -r f; do
  [ -n "$f" ] && [ ! -f "$SNAP/$BASE/$f" ] && \
    aws s3 cp "s3://$BUCKET/$BASE/$f" "$SNAP/$BASE/$f" --profile "$PROFILE" --only-show-errors
done || true

# worker riêng - `xargs -I{}` trên macOS giới hạn độ dài lệnh thay thế (~255 byte)
WORKER=$(mktemp)
cat > "$WORKER" <<'EOW'
p="$1"
for t in 1 2 3; do
  aws s3 sync "s3://$BUCKET/$p" "$SNAP/$p" --profile "$PROFILE" --only-show-errors --cli-read-timeout 120 \
    && { echo "OK $p" >> "$DONE"; exit 0; }
  sleep 5
done
echo "FAIL $p" >> "$DONE"
EOW
export BUCKET PROFILE SNAP DONE AWS_CONFIG_FILE
xargs -P "$JOBS" -n 1 bash "$WORKER" < "$LIST"
rm -f "$WORKER"

ok=$(grep -c '^OK' "$DONE" || true); fail=$(grep -c '^FAIL' "$DONE" || true)
echo "[+] prefix: $ok OK · $fail FAIL / $(wc -l < "$LIST" | tr -d ' ')"
grep '^FAIL' "$DONE" || true
rm -f "$LIST" "$DONE"
echo "[✓] $SNAP · $(find "$SNAP" -type f | wc -l | tr -d ' ') file · $(du -sh "$SNAP" | cut -f1)"
[ "$fail" = 0 ] || { echo "[!] Có prefix lỗi - chạy lại script (sẽ chỉ tải phần thiếu)"; exit 1; }
