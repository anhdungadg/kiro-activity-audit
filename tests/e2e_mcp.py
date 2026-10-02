#!/usr/bin/env python3
"""End-to-end trên một bucket thật, qua MCP stdio — CHỈ ĐỌC trên AWS.

    E2E_BUCKET=kiro-activity-<acct>-us-east-1-xx E2E_PROFILE=<p> E2E_WORK='acme|acm-' \
    E2E_SERVER='uvx --from dist/kiro_audit_mcp-0.1.0-py3-none-any.whl kiro-audit-mcp' \
    uv run --no-project --with 'mcp>=1.20,<2' python3 tests/e2e_mcp.py /tmp/kiro-e2e

Lấy CSV đầy đủ + prompt-logs của 2 ngày gần nhất (DAYS=1), rồi chạy mọi tool. Kiểm tra:
không tool nào trả về secret thô, báo cáo sinh ra không có secret thô, chart khớp.
"""
import asyncio
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

RAW = re.compile(r"(AKIA|ASIA)[0-9A-Z]{16}|eyJhbGciOiJ[A-Za-z0-9_-]{20,}\.|glpat-[A-Za-z0-9_-]{10}|AIza[0-9A-Za-z_-]{35}"
                 r"|X-Amz-Signature=[0-9a-f]{64}")
B, P = os.environ["E2E_BUCKET"], os.environ["E2E_PROFILE"]
WORK = os.environ.get("E2E_WORK")
WD = str(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/kiro-e2e").resolve())
server = shlex.split(os.environ.get("E2E_SERVER", f"{sys.executable} -m kiro_audit.server"))
leaks, fails = [], []


async def call(s, name, args):
    t0 = time.time()
    r = await s.call_tool(name, args)
    txt = "".join(getattr(c, "text", "") for c in r.content)
    if RAW.search(txt):
        leaks.append(name)
    data = None
    try:
        data = json.loads(txt)
    except Exception:
        pass
    print(f"  {'✗' if r.isError else '✓'} {name} ({time.time()-t0:.1f}s)")
    if r.isError:
        fails.append(f"{name}: {txt[:300]}")
    return data, txt


async def wait(s, label, limit_min):
    t0 = time.time()
    while True:
        st, _ = await call(s, "audit_job_status", {"workdir": WD})
        if st and not st.get("running"):
            print(f"    {label}: xong sau {st.get('elapsedMin')} phút · files={st.get('files')} · fail={st.get('prefixFail')}")
            return st
        if time.time() - t0 > limit_min * 60:
            fails.append(f"{label} quá {limit_min} phút")
            return st
        await asyncio.sleep(20)


async def main():
    env = {**os.environ, "KIRO_AUDIT_TZ": "7"}
    params = StdioServerParameters(command=server[0], args=server[1:], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            a, _ = await call(s, "audit_check_access", {"bucket": B, "profile": P})
            print("    ", {k: a.get(k) for k in ("variant", "topPrefixes", "hasReportsPrefix", "warning", "promptLogApis")})

            await call(s, "audit_sync_start", {"bucket": B, "profile": P, "workdir": WD, "days": 1})
            st = await wait(s, "sync", 30)
            if not st or not st.get("done") or st.get("prefixFail"):
                fails.append(f"sync chưa xong: {st}")

            c, _ = await call(s, "audit_cost", {"workdir": WD})
            if c and "totals" in c:
                print(f"    cost: {c['totals']} · ${c['currentMonthlyUsd']}/tháng · blocked={len(c['blockedUsers'])} "
                      f"· safe right-size ${c['optionB_rightSize'].get('safeSavingYearlyUsd')}/năm "
                      f"· thin={len(c['optionB_rightSize'].get('thinDataUsersKept', []))}")

            args = {"workdir": WD, "bucket": B}
            if WORK:
                args["work_pattern"] = WORK
            await call(s, "audit_scan_start", args)
            await wait(s, "scan", 30)

            sec, _ = await call(s, "audit_secret_summary", {"workdir": WD, "min_severity": "high"})
            if sec and "counts" in sec:
                print(f"    secret: {sec['counts']} · người critical/high: {len(sec['peopleWithCriticalOrHigh'])}")
            if WORK:
                pu, _ = await call(s, "audit_purpose_summary", {"workdir": WD})
                if pu and "flagged" in pu:
                    print(f"    purpose: {pu['users']} người · gắn cờ {len(pu['flagged'])}")

            rep, _ = await call(s, "audit_build_reports", {"workdir": WD, "bucket": B, "customer": "E2E", "profile": P})
            if rep:
                for f, ck in rep.get("checks", {}).items():
                    print(f"    {f}: {ck}")
                    if ck["rawSecretHits"] or ck["canvasWithoutChart"]:
                        fails.append(f"report {f}: {ck}")

    for f in list(Path(WD, "reports").glob("*.html")) + list(Path(WD, "data").glob("*.json")):
        if RAW.search(f.read_text(errors="replace")):
            leaks.append(str(f))
    print("\nLỘ SECRET:", leaks or "0")
    print("LỖI:", fails or "0")
    sys.exit(1 if (leaks or fails) else 0)


asyncio.run(main())
