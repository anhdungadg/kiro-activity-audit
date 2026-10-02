#!/usr/bin/env python3
"""Kiểm tra mọi file HTML báo cáo: canvas ↔ chart khớp, JS chạy được, không dataset rỗng/undefined/NaN,
không còn secret thô. Cần `node` cho bước chạy JS (bỏ qua nếu không có).

    python3 tests/check_charts.py reports/*.html
"""
import re
import shutil
import subprocess
import sys
import tempfile

RAW = re.compile(r"(AKIA|ASIA)[0-9A-Z]{16}|eyJhbGciOiJ[A-Za-z0-9_-]{20,}|glpat-[A-Za-z0-9_-]{10}|AIza[0-9A-Za-z_-]{35}")
STUB = r"""
let made=[];globalThis.window={Chart:function(){}};
globalThis.document={getElementById:(id)=>({id}),querySelectorAll:()=>[],querySelector:()=>null,addEventListener:()=>{}};
globalThis.addEventListener=()=>{};
globalThis.Chart=function(e,c){made.push([e&&e.id,c]);};window.Chart=Chart;
Chart.defaults={font:{},plugins:{legend:{labels:{}},tooltip:{}},animation:{},scale:{grid:{}},elements:{}};
Chart.register=()=>{};
"""
CHECK = r"""
const e=[];made.forEach(([id,c])=>((c.data||{}).datasets||[]).forEach(d=>{
 if(!d.data||!d.data.length)e.push(id+' rỗng');
 else if(d.data.some(v=>v===undefined||(typeof v==='number'&&Number.isNaN(v))))e.push(id+' undefined/NaN');}));
console.log(JSON.stringify({charts:made.length,errors:e}));
"""


def check(path: str) -> list[str]:
    h = open(path, encoding="utf-8", errors="replace").read()
    errs = []
    can = set(re.findall(r'canvas id="([^"]+)"', h))
    mk = set(re.findall(r"(?:mk|new Chart)\(\s*(?:document\.getElementById\()?['\"]([^'\"]+)['\"]", h))
    if can - mk:
        errs.append(f"canvas không có chart: {sorted(can - mk)}")
    if RAW.search(h):
        errs.append(f"{len(RAW.findall(h))} secret thô")
    node = shutil.which("node")
    scripts = re.findall(r"<script>(.*?)</script>", h, re.S)
    if node and scripts:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(STUB + scripts[-1] + CHECK)
        r = subprocess.run([node, f.name], capture_output=True, text=True, timeout=60)
        if r.returncode:
            errs.append("JS lỗi: " + r.stderr.strip().splitlines()[-1][:200])
        else:
            import json
            o = json.loads(r.stdout.strip().splitlines()[-1])
            errs += o["errors"]
            if o["charts"] != len(can):
                errs.append(f"{o['charts']} chart khởi tạo / {len(can)} canvas")
    return errs


if __name__ == "__main__":
    bad = 0
    for p in sys.argv[1:]:
        e = check(p)
        print(("✓ " if not e else "✗ ") + p + ("" if not e else " — " + "; ".join(e)))
        bad += bool(e)
    sys.exit(1 if bad else 0)
