#!/usr/bin/env python3
"""Kiểm tra repo không cần AWS:

  1. mọi script Python biên dịch được, script shell đúng cú pháp
  2. steering của power trùng references của skill
  3. bộ che secret che đúng, không phá chuỗi thường
  4. không còn dữ liệu của tổ chức cụ thể trong repo
  5. MCP server khởi động qua stdio và liệt kê đủ tool

    uv run --with 'mcp>=1.20,<2' python3 tests/test_repo.py
"""
import asyncio
import filecmp
import os
import py_compile
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SK = ROOT / "skills" / "kiro-activity-audit"
PW = ROOT / "powers" / "kiro-activity-audit"
sys.path.insert(0, str(ROOT / "src"))
fails = []


def ok(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        fails.append(msg)


print("1. script")
for p in sorted((SK / "scripts").glob("*.py")) + sorted((ROOT / "src" / "kiro_audit").glob("*.py")):
    try:
        py_compile.compile(str(p), doraise=True)
        ok(True, p.name)
    except py_compile.PyCompileError as e:
        ok(False, f"{p.name}: {e}")
for p in sorted((SK / "scripts").glob("*.sh")) + sorted((ROOT / "tools").glob("*.sh")):
    ok(subprocess.run(["bash", "-n", str(p)]).returncode == 0, f"{p.name} (bash -n)")

print("2. steering ↔ references")
for f in ("pitfalls", "secret-handling", "data-sources", "reporting"):
    ok(filecmp.cmp(SK / "references" / f"{f}.md", PW / "steering" / f"{f}.md", shallow=False),
       f"{f}.md giống nhau (nếu lệch: tools/sync-power-steering.sh)")
for ref in re.findall(r"`([\w\-]+\.md)`", (PW / "POWER.md").read_text()):
    if ref != "POWER.md":
        ok((PW / "steering" / ref).is_file(), f"POWER.md nhắc tới steering/{ref}")

print("3. redact")
from kiro_audit.redact import redact  # noqa: E402

fake_akid = "AKIA" + "Q" * 16
fake_secret = "aB3dEfGhIjKlMnOpQrStUvWxYz0123456789/+Ab"
cases = {
    f"aws.ses.id={fake_akid}\naws.ses.key={fake_secret}": [fake_akid, fake_secret],
    "postgres://owner:hunter2@db.prod.example:5432/x": ["hunter2"],
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcDEF123_-": ["eyJzdWIiOiIxIn0"],
    "//registry/:_authToken=glpat-ABCDEFGHIJKLMNOPQRST": ["glpat-ABCDEFGHIJKLMNOPQRST"],
    "JWT_SECRET: \"dev-secret-change-me-please-123\"": ["dev-secret-change-me-please-123"],
}
for src, secrets in cases.items():
    out = redact(src)
    ok(not any(s in out for s in secrets), f"che: {src.splitlines()[0][:40]}…")
keep = "commit 3f2a9c1e5b7d4f6a8c0e2b4d6f8a0c2e4b6d8f0a · 47 user · $4.700/tháng · s3://bucket/key"
ok(redact(keep) == keep, "giữ nguyên chuỗi thường (git SHA, số, đường dẫn)")

print("4. không lọt dữ liệu tổ chức cụ thể")
# Danh sách trong repo CHỈ là mock (tổ chức/account/user giả) để bước kiểm tra luôn chạy.
# Từ khoá thật của các tổ chức đã audit (tên, account ID, directory ID, username...)
# KHÔNG được commit: đặt trong tests/banned-terms.local.txt (đã .gitignore, mỗi dòng
# một regex, dòng bắt đầu bằng # bị bỏ qua) hoặc biến môi trường KIRO_AUDIT_BANNED
# (phân cách bằng dấu |). Hai nguồn này được cộng thêm vào danh sách mock.
MOCK_BANNED = [
    "acme-?corp", "globex", "initech",            # tên tổ chức giả
    "111122223333", "999988887777",               # AWS account ID giả
    "d-9999999999",                               # Identity Center directory ID giả
    "jdoe-mock", "asmith-mock",                   # username giả
]
extra = []
local = ROOT / "tests" / "banned-terms.local.txt"
if local.is_file():
    extra += [l.strip() for l in local.read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]
extra += [t for t in os.environ.get("KIRO_AUDIT_BANNED", "").split("|") if t.strip()]
banned = re.compile("|".join(MOCK_BANNED + extra), re.I)
print(f"  · {len(MOCK_BANNED)} từ khoá mock + {len(extra)} từ khoá local")
hits = []
for p in ROOT.rglob("*"):
    if p.is_file() and ".git" not in p.parts and p.suffix in (".py", ".sh", ".md", ".json", ".toml", ".css"):
        if p.name in ("test_repo.py", local.name):
            continue
        for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if banned.search(line):
                hits.append(f"{p.relative_to(ROOT)}:{i}")
ok(not hits, "0 hit" if not hits else f"{len(hits)} hit: {', '.join(hits[:8])}")

print("5. MCP server")


async def mcp_check():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable, args=["-m", "kiro_audit.server"],
                                   env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name: t for t in (await s.list_tools()).tools}
            want = {"audit_check_access", "audit_sync_start", "audit_scan_start", "audit_job_status",
                    "audit_job_cancel", "audit_cost", "audit_secret_summary", "audit_purpose_summary",
                    "audit_build_reports"}
            ok(want <= set(tools), f"{len(tools)} tool: {', '.join(sorted(tools))}")
            ro = [n for n, t in tools.items() if t.annotations and t.annotations.readOnlyHint]
            ok(set(ro) >= {"audit_check_access", "audit_job_status", "audit_secret_summary"}, f"readOnlyHint: {sorted(ro)}")
            res = await s.call_tool("audit_check_access", {"bucket": "bad bucket; rm -rf /", "profile": "x"})
            ok(res.isError, "từ chối tên bucket độc hại")


try:
    asyncio.run(mcp_check())
except ModuleNotFoundError:
    ok(False, "thiếu thư viện mcp - chạy bằng: uv run --with 'mcp>=1.20,<2' python3 tests/test_repo.py")

print(f"\n{'✗ ' + str(len(fails)) + ' lỗi' if fails else '✓ tất cả đạt'}")
sys.exit(1 if fails else 0)
