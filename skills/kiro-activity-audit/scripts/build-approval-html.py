#!/usr/bin/env python3
"""Sinh reports/bao-cao-chi-phi-va-muc-dich-su-dung.html TỪ bản .md cùng tên.

Trước đây .md và .html là hai tài liệu viết tay song song, phải sửa tay cả hai mỗi kỳ —
nguồn gây lệch số liệu giữa hai bản. Giờ markdown là nguồn duy nhất; HTML được sinh ra,
kèm 3 biểu đồ lấy số trực tiếp từ data/csv-metrics.json.

    python3 build-approval-html.py --workdir ./kiro-audit-<acct>            # ghi HTML
    python3 build-approval-html.py --md report.md --out report.html \
        --metrics data/csv-metrics.json [--css approval-style.css] [--dry-run]
"""
import argparse
import html
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CSS = HERE.parent / "templates" / "approval-style.css"

EXTRA_CSS = """
/* --- sinh bởi build-approval-html.py --- */
h3{font-size:14px;font-weight:700;margin:22px 0 10px}
h4{font-size:13px;font-weight:700;margin:0 0 8px}
ul,ol{margin:8px 0 12px 22px} li{margin:3px 0} p{margin:9px 0}
th.c,td.c{text-align:center} th.n,td.n{text-align:right}
.tick{font-size:17px;line-height:1;color:var(--dim)}
.box table{margin:10px 0}
@media print{.chart-wrap{break-inside:avoid}}
"""


# ------------------------------------------------------------ markdown → HTML
def inline(t: str) -> str:
    t = html.escape(t, quote=False)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])", r"<i>\1</i>", t)
    t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', t)
    t = t.replace("&lt;br&gt;", "<br>")
    return t.replace("☐", '<span class="tick">☐</span>')


def cells(line: str):
    s = line.strip()
    s = s[1:] if s.startswith("|") else s
    s = s[:-1] if s.endswith("|") else s
    return [c.strip() for c in s.split("|")]


def align(sep: str) -> str:
    s = sep.strip()
    if s.startswith(":") and s.endswith(":"):
        return "c"
    return "n" if s.endswith(":") else ""


def table(lines) -> str:
    head, al = cells(lines[0]), [align(c) for c in cells(lines[1])]
    al += [""] * (len(head) - len(al))
    cl = lambda i: f' class="{al[i]}"' if i < len(al) and al[i] else ""
    out = ["<table><thead><tr>"]
    out += [f"<th{cl(i)}>{inline(c)}</th>" for i, c in enumerate(head)]
    out.append("</tr></thead><tbody>")
    for ln in lines[2:]:
        row = cells(ln)
        total = bool(re.match(r"^\**\s*(TỔNG|Tổng)\b", row[0] if row else ""))
        out.append('<tr class="total-hi">' if total else "<tr>")
        out += [f"<td{cl(i)}>{inline(c)}</td>" for i, c in enumerate(row)]
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def kind(text: str) -> str:
    if "🔴" in text:
        return "bad"
    if "⚠" in text:
        return "warn"
    return "good" if ("✅" in text or "✓" in text) else "info"


BLOCK_START = re.compile(r"^(#{1,6}\s|>|\||-{3,}$|[-*]\s|\d+\.\s|<)")


def render(md: str) -> str:
    L, out, i = md.split("\n"), [], 0
    n = len(L)
    is_sep = lambda k: k < n and re.match(r"^\s*\|[\s:|-]+\|\s*$", L[k])
    while i < n:
        s = L[i].strip()
        if not s:
            i += 1
        elif re.match(r"^-{3,}$", s):
            out.append('<div class="space"></div>'); i += 1
        elif s.startswith(">"):
            blk = []
            while i < n:
                t = L[i].strip()
                if t.startswith(">"):
                    blk.append(re.sub(r"^>\s?", "", t)); i += 1
                elif not t and i + 1 < n and L[i + 1].strip().startswith(">"):
                    blk.append(""); i += 1
                else:
                    break
            inner = "\n".join(blk)
            body = re.sub(r"^<h\d>(.*?)</h\d>", r"<h4>\1</h4>", render(inner), count=1, flags=re.S)
            out.append(f'<div class="box {kind(inner)}">{body}</div>')
        elif s.startswith("|") and is_sep(i + 1):
            tb = [L[i], L[i + 1]]; i += 2
            while i < n and L[i].strip().startswith("|"):
                tb.append(L[i]); i += 1
            out.append(table(tb))
        elif (m := re.match(r"^(#{1,6})\s+(.*)$", s)):
            lvl, txt = len(m.group(1)), m.group(2); i += 1
            if lvl == 1:
                continue                      # H1 đã nằm trong doc-head
            mm = re.match(r"^(\d+)\.\s+(.*)$", txt)
            if lvl == 2 and mm:
                out.append(f'<h2><span class="pill">{mm.group(1)}</span><span>{inline(mm.group(2))}</span></h2>')
            else:
                out.append(f"<h{min(lvl, 4)}>{inline(txt)}</h{min(lvl, 4)}>")
        elif re.match(r"^([-*]|\d+\.)\s+", s):
            tag = "ol" if s[0].isdigit() else "ul"; items = []
            while i < n:
                t = L[i].strip()
                m2 = re.match(r"^(?:[-*]|\d+\.)\s+(.*)$", t)
                if m2:
                    items.append(m2.group(1)); i += 1
                elif t and items and not BLOCK_START.match(t):
                    items[-1] += " " + t; i += 1
                else:
                    break
            out.append(f"<{tag}>" + "".join(f"<li>{inline(x)}</li>" for x in items) + f"</{tag}>")
        elif s.startswith("<"):
            out.append(s); i += 1
        else:
            para = [s]; i += 1
            while i < n and L[i].strip() and not BLOCK_START.match(L[i].strip()):
                para.append(L[i].strip()); i += 1
            out.append(f"<p>{inline(' '.join(para))}</p>")
    return "\n".join(out)


# ------------------------------------------------------------ biểu đồ
def rows(metrics, limit=24):
    out = []
    for u in sorted(metrics["users"], key=lambda u: -u["credits"])[:limit]:
        m = u.get("models") or {}
        tot = sum(m.values()) or 1
        opus = sum(v for k, v in m.items() if "opus" in k.lower())
        out.append(dict(e=u["email"].split("@")[0] + "@", cr=round(u["credits"], 2),
                        msg=u["messages"], opus=round(opus / tot * 100, 1),
                        pct=round(u["projectedPctOfLimit"], 1), proj=int(u["projectedMonthly"]),
                        hit=str(u["credits"] >= 4940).lower()))
    return out


CHART_HTML = """
<h2><span class="pill">BĐ</span><span>Biểu đồ — {n} nhân viên dùng nhiều nhất</span></h2>
<div class="g2">
  <div class="chart-wrap"><div class="cap">Credit thực dùng {d} ngày — đỏ: đã chạm hạn mức 5.000</div>
    <div class="cbox sm"><canvas id="c1"></canvas></div></div>
  <div class="chart-wrap"><div class="cap">Dự phóng tháng so với hạn mức 5.000 credit</div>
    <div class="cbox sm"><canvas id="c2"></canvas></div></div>
</div>
<div class="chart-wrap"><div class="cap">Credit/tin nhắn và tỷ lệ dùng Opus — cơ sở đề nghị đổi model
  (đỏ: trên 1,0 credit/tin nhắn)</div><div class="cbox"><canvas id="c3"></canvas></div></div>
"""

CHART_JS = r"""
const F={dim:'#6b7688',line:'rgba(221,226,234,.9)',line2:'#c3cbd8',blue:'#1e5fa8',green:'#1a7f47',amber:'#9a6208',red:'#b3261e'};
const A=(h,o)=>{const n=parseInt(h.slice(1),16);return `rgba(${n>>16&255},${n>>8&255},${n&255},${o})`;};
const ROWS=__ROWS__;
if(window.Chart){Object.assign(Chart.defaults,{color:F.dim,maintainAspectRatio:false});
  Chart.defaults.font.family='-apple-system,"Segoe UI",Roboto,Arial,sans-serif';Chart.defaults.font.size=11;
  Chart.defaults.plugins.tooltip.backgroundColor='rgba(26,31,43,.94)';Chart.defaults.plugins.tooltip.padding=10;
  Chart.defaults.plugins.legend.labels.boxWidth=11;}
const G={grid:{color:F.line,drawTicks:false},border:{color:F.line2}}, NG={grid:{display:false},border:{color:F.line2}};
const mk=(id,cfg)=>{const el=document.getElementById(id);if(el&&window.Chart)new Chart(el,cfg);};
const col=(f)=>({backgroundColor:ROWS.map(r=>A(f(r),.8)),borderColor:ROWS.map(f),borderWidth:1,borderRadius:2});
const T=(t)=>({display:true,text:t,font:{size:10,weight:'600'}});
mk('c1',{type:'bar',data:{labels:ROWS.map(r=>r.e),datasets:[{data:ROWS.map(r=>r.cr),
  ...col(r=>r.hit?F.red:r.cr>3750?F.amber:F.green)}]},
  options:{indexAxis:'y',plugins:{legend:{display:false},tooltip:{callbacks:{
    label:c=>` ${c.parsed.x.toFixed(2)} credit`,
    afterLabel:c=>{const r=ROWS[c.dataIndex];return [`${r.msg} tin nhắn`,`${(r.cr/r.msg).toFixed(3)} credit/tin`].concat(r.hit?['ĐÃ CHẠM HẠN MỨC']:[]);}}}},
  scales:{x:{...G,beginAtZero:true,suggestedMax:5400,title:T('CREDIT THỰC DÙNG __DAYS__ NGÀY')},y:NG}}});
mk('c2',{type:'bar',data:{labels:ROWS.map(r=>r.e),datasets:[{data:ROWS.map(r=>r.proj),
  ...col(r=>r.pct>=100?F.red:r.pct>=75?F.amber:F.green)}]},
  options:{indexAxis:'y',plugins:{legend:{display:false},tooltip:{callbacks:{
    label:c=>` ${c.parsed.x.toLocaleString('vi-VN')} credit/tháng`,
    afterLabel:c=>{const p=ROWS[c.dataIndex].pct;return `${p.toFixed(0)}% hạn mức — `+(p>=100?'sẽ bị chặn':p>=75?'at-risk':'an toàn');}}}},
  scales:{x:{...G,beginAtZero:true,title:T('CREDIT/THÁNG NGOẠI SUY — hạn mức 5.000'),
    ticks:{callback:v=>v.toLocaleString('vi-VN')}},y:NG}}});
mk('c3',{type:'bar',data:{labels:ROWS.map(r=>r.e),datasets:[
  {label:'Credit / tin nhắn',data:ROWS.map(r=>+(r.cr/r.msg).toFixed(3)),...col(r=>r.cr/r.msg>1?F.red:F.amber),yAxisID:'y',order:2},
  {label:'% tin nhắn dùng Opus',type:'line',data:ROWS.map(r=>r.opus),borderColor:F.blue,backgroundColor:A(F.blue,.1),
   borderWidth:2,fill:true,pointRadius:4,pointBackgroundColor:F.blue,tension:.2,yAxisID:'y1',order:1}]},
  options:{plugins:{legend:{position:'top',align:'end'}},scales:{x:NG,
    y:{...G,position:'left',beginAtZero:true,title:{...T('CREDIT / TIN NHẮN'),color:F.amber}},
    y1:{position:'right',grid:{display:false},border:{color:F.line2},beginAtZero:true,max:100,
        title:{...T('% DÙNG OPUS'),color:F.blue},ticks:{callback:v=>v+'%'}}}}});
"""


def build(MD, OUT, CSS, METRICS, dry=False):
    md = MD.read_text(encoding="utf-8")
    met = json.loads(METRICS.read_text(encoding="utf-8"))
    grab = lambda k: (re.search(rf"\*\*{k}:\*\*\s*(.+)", md) or [None, ""])[1].strip()
    title = re.search(r"^#\s+(.+)$", md, re.M).group(1).strip()

    body_md = re.sub(r"\A#[^\n]*\n(?:\s*\n)?(?:\*\*[^\n]*\n)+", "", md, count=1)
    body = render(body_md)
    rs, nd = rows(met), len(met["days"])
    chart = CHART_HTML.format(n=len(rs), d=nd)
    anchor = '<h2><span class="pill">3</span>'
    body = body.replace(anchor, chart + anchor, 1) if anchor in body else body + chart

    js_rows = "[\n" + ",\n".join(
        "{{e:'{e}',cr:{cr},msg:{msg},opus:{opus},pct:{pct},proj:{proj},hit:{hit}}}".format(**r) for r in rs
    ) + "]"
    js = CHART_JS.replace("__ROWS__", js_rows).replace("__DAYS__", str(nd))

    meta = "".join(f"<dt>{k}</dt><dd>{inline(v)}</dd>" for k, v in [
        ("Trình", grab("Trình")), ("Phạm vi", grab("Phạm vi")),
        ("Ngày lập", grab("Ngày lập")), ("Múi giờ", grab("Múi giờ")),
        ("Người lập", grab("Người lập"))] if v)

    doc = f"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title.title())}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
{CSS.read_text(encoding="utf-8")}{EXTRA_CSS}</style>
</head>
<body>
<div class="page">
<div class="doc-head">
  <div class="org">Báo cáo nội bộ &nbsp;·&nbsp; Trình Trưởng bộ phận phê duyệt</div>
  <h1>{html.escape(title)}</h1>
  <dl class="meta-grid">{meta}</dl>
</div>
{body}
</div>
<!-- sinh tự động bởi scripts/build-approval-html.py — KHÔNG sửa tay, sửa file .md -->
<script>{js}</script>
</body>
</html>
"""
    stats = dict(tables=doc.count("<table>"), boxes=doc.count('class="box '),
                 h2=doc.count("<h2>"), canvas=doc.count("<canvas"), rows=len(rs), bytes=len(doc.encode()))
    if not dry:
        OUT.write_text(doc, encoding="utf-8")
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workdir", help="thư mục audit; mặc định đọc reports/bao-cao-*.md + data/csv-metrics.json trong đó")
    ap.add_argument("--md"); ap.add_argument("--out"); ap.add_argument("--metrics")
    ap.add_argument("--css", default=str(DEFAULT_CSS))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    w = Path(a.workdir or ".")
    md = Path(a.md or w / "reports" / "bao-cao-chi-phi-va-muc-dich-su-dung.md")
    out = Path(a.out or md.with_suffix(".html"))
    met = Path(a.metrics or w / "data" / "csv-metrics.json")
    for f in (md, met, Path(a.css)):
        if not f.is_file():
            raise SystemExit(f"[x] Không thấy {f}")
    st = build(md, out, Path(a.css), met, a.dry_run)
    print(("[dry-run] " if a.dry_run else f"[+] Ghi {out} · ") +
          " · ".join(f"{k}={v}" for k, v in st.items()))
