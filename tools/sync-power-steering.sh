#!/usr/bin/env bash
# Một nguồn duy nhất: references/ của skill → steering/ của power.
# Chạy sau mỗi lần sửa references; tests/test_repo.py kiểm tra hai nơi giống nhau.
set -eu
cd "$(dirname "$0")/.."
for f in pitfalls secret-handling data-sources reporting; do
  cp "skills/kiro-activity-audit/references/$f.md" "powers/kiro-activity-audit/steering/$f.md"
done
echo "✓ steering đã đồng bộ"
