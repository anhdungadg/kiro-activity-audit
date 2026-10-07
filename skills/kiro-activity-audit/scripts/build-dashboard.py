#!/usr/bin/env python3
"""
Sinh 2 file HTML từ các file metrics JSON.

  reports/kiro-activity-report.html                  dashboard kỹ thuật (dark, Chart.js)
  reports/bao-cao-chi-phi-va-muc-dich-su-dung.html   bản trình phê duyệt (sáng, in PDF được)

Lý do sinh bằng script chứ không viết tay: mọi con số trong chart phải đọc từ
metrics JSON. Viết tay số vào HTML là cách chắc chắn nhất để báo cáo sai lệch
so với dữ liệu sau vài lần sửa.

VÍ DỤ:
    python3 scripts/build-dashboard.py --data-dir data --out-dir reports
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict

CDN = "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"


def load(d: str) -> dict:
    out = {}
    for name in ("monthly-metrics", "model-metrics", "scenarios", "csv-metrics"):
        p = os.path.join(d, f"{name}.json")
        with open(p, encoding="utf-8") as fh:
            out[name] = json.load(fh)
    return out


# Số trong f-string của Python ra định dạng en-US (1,234.56). Bản trình phê duyệt là
# tài liệu chính thức tiếng Việt nên phải đổi sang 1.234,56. Chỉ áp dụng cho phần
# tài liệu, KHÔNG áp dụng cho khối <script> (JSON/JS bắt buộc dùng dấu chấm thập phân).
_NUM_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+")
# Heading chứa SỐ MỤC (2.1, 2.6) - không phải số thập phân, tuyệt đối không đổi dấu.
_HEAD_RE = re.compile(r"<h[1-6][^>]*>.*?</h[1-6]>", re.S)


def _swap(m: re.Match) -> str:
    s = m.group(0)
    if "," in s:                      # có phân cách nghìn: 1,234.56 → 1.234,56
        return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return s.replace(".", ",")        # chỉ thập phân: 0.876 → 0,876


def to_vi_numbers(text: str) -> str:
    """Đổi định dạng số en-US sang vi-VN trong một đoạn HTML (không chứa script).

    BỎ QUA nội dung heading, vì số mục dạng "2.6" sẽ bị hiểu sai thành số thập phân
    và đổi thành "2,6". Đây là lỗi đã gặp thật ở bản đầu.
    """
    out, pos = [], 0
    for h in _HEAD_RE.finditer(text):
        out.append(_NUM_RE.sub(_swap, text[pos:h.start()]))
        out.append(h.group(0))        # heading giữ nguyên
        pos = h.end()
    out.append(_NUM_RE.sub(_swap, text[pos:]))
    return "".join(out)


def build_ctx(D: dict, args) -> dict:
    """Suy mọi thứ phụ thuộc khách hàng ra khỏi DỮ LIỆU, không viết cứng.

    Trước đây các chỗ này viết cứng theo một khách hàng cụ thể (tên user làm ví dụ,
    user cần nâng tier, bảng email ngoài domain, mốc snapshot). Viết cứng là cách chắc chắn
    nhất để báo cáo của khách hàng sau chứa số của khách hàng trước.
    """
    M, MD, S = D["monthly-metrics"], D["model-metrics"], D["scenarios"]
    basis = M["basisMonth"]
    bm = M["months"][basis]

    # ---- mốc snapshot: dùng cờ, nếu không có thì suy từ tên thư mục snapshot-YYYYMMDD-HHMMZ
    snap = args.snapshot_utc
    if not snap:
        m = re.search(r"snapshot-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})Z",
                      M.get("snapshotDir", "") or "")
        snap = (f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}:{m.group(5)}Z"
                if m else "(không xác định)")
    snap_local = ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})Z", snap)
    if m:
        h = int(m.group(4)) + int(args.tz)
        d = int(m.group(3)) + (1 if h >= 24 else 0)
        snap_local = f"{h % 24:02d}:{m.group(5)} GMT+{args.tz:g} ngày {d:02d}/{m.group(2)}/{m.group(1)}"

    # ---- hai user làm ví dụ cho luận điểm "model quan trọng hơn khối lượng":
    #      một người nhiều Opus + credit/msg cao, một người nhiều message + credit/msg thấp.
    #      Chọn trong nhóm có đủ message để ví dụ có sức thuyết phục.
    cands = [u for u in bm["users"] if u["messages"] >= 500]
    ex_costly = ex_cheap = None
    if len(cands) >= 2:
        def opus_pct(u):
            o = sum(v for k, v in u["models"].items() if "opus" in k)
            return o / u["messages"] * 100 if u["messages"] else 0
        ex_costly = max(cands, key=lambda u: (opus_pct(u) / 100) * u["creditPerMessage"])
        cheaper = [u for u in cands
                   if u["messages"] > ex_costly["messages"]
                   and u["credits"] < ex_costly["credits"]]
        ex_cheap = (max(cheaper, key=lambda u: u["messages"] - u["credits"])
                    if cheaper else min(cands, key=lambda u: u["creditPerMessage"]))
        for u in (ex_costly, ex_cheap):
            u["_opusPct"] = opus_pct(u)
    if not ex_costly:                      # dữ liệu quá mỏng
        ex_costly = ex_cheap = {"email": "(không đủ dữ liệu)", "messages": 0,
                                "credits": 0, "creditPerMessage": 0, "_opusPct": 0}

    # ---- user cần NÂNG tier (safeDelta > 0), lấy từ scenarios thay vì viết cứng
    ups = [r for r in S["optionB_rightSize"]["rows"] if r.get("safeDeltaUsd", 0) > 0]
    up_users = []
    for r in ups:
        u = next((x for x in bm["users"] if x["userId"] == r["userId"]), None)
        up_users.append({"email": r["email"] or r["userId"],
                         "from": r["currentTier"], "to": r["safeRecommendedTier"],
                         "pct": u["pctOfLimit"] if u else 0})

    # ---- bảng email ngoài domain: suy từ monthly-metrics, gồm số của mọi tháng
    noncorp = []
    for em in M.get("nonCorpEmails", []):
        rows = [(mo, u) for mo in sorted(M["months"])
                for u in M["months"][mo]["users"] if u["email"] == em]
        tier = rows[0][1]["tier"] if rows else ""
        basis_row = next((u for mo, u in rows if mo == basis), None)
        other = [(mo, u) for mo, u in rows if mo != basis and u["credits"] > 0]
        noncorp.append({
            "email": em, "tier": tier,
            "usd": {"PRO_MAX": 100, "POWER": 200, "PRO_PLUS": 40,
                    "PRO": 20, "FREE": 0}.get(tier, 0),
            "basisCredits": basis_row["credits"] if basis_row else 0.0,
            "basisDays": basis_row["activeDays"] if basis_row else 0,
            "otherNote": (f"{other[0][0][5:]}/{other[0][0][:4]}: "
                          f"{other[0][1]['credits']:,.0f}" if other else ""),
        })

    # ---- cặp email gần giống nhau: cảnh báo đọc nhầm người
    lookalike = []
    ems = sorted(u["email"] for u in bm["users"] if u["email"])
    for i, a in enumerate(ems):
        la, da = a.split("@")[0], a.split("@")[-1]
        for b in ems[i + 1:]:
            lb, db = b.split("@")[0], b.split("@")[-1]
            if da == db and len(la) == len(lb) and sum(
                    1 for x, y in zip(la, lb) if x != y) <= 2 and la != lb:
                lookalike.append((a, b))

    return {
        "account": args.account, "bucket": args.bucket, "customer": args.customer,
        "profile": args.profile, "identity": args.identity,
        "tz": args.tz, "orgLabel": args.org_label,
        "snapUtc": snap, "snapLocal": snap_local,
        "basis": basis,
        "ex_costly": ex_costly, "ex_cheap": ex_cheap,
        "upUsers": up_users, "noncorp": noncorp, "lookalike": lookalike,
        "cross": S.get("crossCheck"),
        # New_User chỉ có trong csv-metrics (analyze-csv.py) và trong monthly-metrics
        # nếu chạy bản template mới. Ưu tiên monthly, thiếu thì lấy csv-metrics.
        "newUserCount": (sum(1 for u in bm["users"] if u.get("isNewUser"))
                         or sum(1 for u in D.get("csv-metrics", {}).get("users", [])
                                if u.get("isNewUser"))),
    }


# ----------------------------------------------------------------- dashboard
DARK_CSS = """
:root{--bg:#0a0e14;--panel:#111823;--line:#1e2936;--fg:#c9d5e3;--dim:#6b7f95;
--acc:#00d9a3;--acc2:#4dabf7;--warn:#ffa94d;--bad:#ff6b6b;--mono:ui-monospace,SFMono-Regular,
"SF Mono",Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--mono);font-size:13px;
line-height:1.6;
background-image:linear-gradient(rgba(0,217,163,.03) 1px,transparent 1px),
linear-gradient(90deg,rgba(0,217,163,.03) 1px,transparent 1px);background-size:32px 32px}
.wrap{max-width:1400px;margin:0 auto;padding:32px 20px 80px}
header{border-bottom:1px solid var(--line);padding-bottom:20px;margin-bottom:28px}
h1{margin:0 0 8px;font-size:22px;color:var(--acc);letter-spacing:.5px}
h2{font-size:14px;color:var(--acc2);margin:36px 0 14px;padding-bottom:7px;
border-bottom:1px solid var(--line);letter-spacing:1px;text-transform:uppercase}
.meta{color:var(--dim);font-size:11px}
.meta b{color:var(--fg);font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:20px 0}
.kpi{background:var(--panel);border:1px solid var(--line);border-left:2px solid var(--acc);
padding:13px 15px;border-radius:3px}
.kpi .l{color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:1px}
.kpi .v{font-size:23px;color:var(--acc);margin:5px 0 2px;font-weight:600}
.kpi .s{color:var(--dim);font-size:10px}
.kpi.w{border-left-color:var(--warn)} .kpi.w .v{color:var(--warn)}
.kpi.b{border-left-color:var(--bad)}  .kpi.b .v{color:var(--bad)}
.kpi.i{border-left-color:var(--acc2)} .kpi.i .v{color:var(--acc2)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:3px;padding:15px}
.card h3{margin:0 0 4px;font-size:12px;color:var(--fg);letter-spacing:.5px}
.card .note{color:var(--dim);font-size:10px;margin:0 0 12px}
.card.full{grid-column:1/-1}
.cw{position:relative;height:290px}
.cw.tall{height:420px}
table{width:100%;border-collapse:collapse;font-size:11px}
th,td{padding:6px 8px;text-align:right;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:600;text-transform:uppercase;font-size:9.5px;letter-spacing:.6px;
position:sticky;top:0;background:var(--panel)}
td:first-child,th:first-child{text-align:left}
tbody tr:hover{background:rgba(77,171,247,.05)}
.sc{max-height:430px;overflow:auto}
.bad{color:var(--bad)} .warn{color:var(--warn)} .ok{color:var(--acc)} .dim{color:var(--dim)}
.pill{display:inline-block;padding:1px 6px;border-radius:2px;font-size:9.5px;
border:1px solid currentColor}
.callout{background:var(--panel);border:1px solid var(--line);border-left:2px solid var(--warn);
padding:13px 16px;margin:14px 0;border-radius:3px}
.callout.bad{border-left-color:var(--bad)}
.callout.ok{border-left-color:var(--acc)}
.callout h4{margin:0 0 7px;font-size:12px;color:var(--warn)}
.callout.bad h4{color:var(--bad)} .callout.ok h4{color:var(--acc)}
.callout p{margin:5px 0;font-size:11.5px;color:var(--fg)}
.callout ul{margin:6px 0 0;padding-left:18px;font-size:11.5px}
#nochart{display:none;background:var(--panel);border:1px solid var(--bad);color:var(--bad);
padding:12px 16px;margin:14px 0;border-radius:3px;font-size:11.5px}
footer{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);
color:var(--dim);font-size:10.5px}
a{color:var(--acc2)}
"""


def dash_html(D: dict, X: dict) -> str:
    M, MD, S, C = D["monthly-metrics"], D["model-metrics"], D["scenarios"], D["csv-metrics"]
    basis = M["basisMonth"]
    bm = M["months"][basis]
    cov = M["coverage"][basis]

    # ---- timeline
    days = sorted(C["perDay"])
    tl_lbl = [f"{d[8:10]}/{d[5:7]}" for d in days]
    tl_cr = [round(C["perDay"][d]["credits"], 1) for d in days]
    tl_us = [C["perDay"][d]["users"] for d in days]

    # Nhận xét về nhịp làm việc phải SUY TỪ DỮ LIỆU, không viết cứng: kiểm tra 5 ngày
    # thấp nhất có phải cuối tuần hay không. Với khách hàng khác kết luận có thể ngược lại.
    import datetime as _dt
    low5 = sorted(days, key=lambda d: C["perDay"][d]["credits"])[:5]
    wknd = sum(1 for d in low5
               if _dt.date(int(d[:4]), int(d[5:7]), int(d[8:10])).weekday() >= 5)
    if wknd >= 4:
        rhythm = "5 ngày thấp nhất gần như đều là cuối tuần → nhịp dùng khớp giờ hành chính."
    elif wknd >= 2:
        rhythm = (f"{wknd}/5 ngày thấp nhất là cuối tuần → nhịp dùng chỉ khớp một phần "
                  f"giờ hành chính.")
    else:
        rhythm = ("5 ngày thấp nhất KHÔNG phải cuối tuần → nhịp dùng không theo giờ hành chính, "
                  "cần xem lại cách tổ chức làm việc trước khi kết luận.")

    # ---- top user tháng căn cứ
    top = bm["users"][:20]
    tu_lbl = [(u["email"] or u["userId"]).split("@")[0] for u in top]
    tu_cr = [round(u["credits"], 1) for u in top]
    tu_pct = [u["pctOfLimit"] for u in top]

    # ---- model
    mbm = MD["messagesByModel"]
    coef = MD["nnls"]["creditPerMessageByModel"]
    mods = [m for m in mbm if mbm[m] > 0]
    mods_by_msg = sorted(mods, key=lambda m: -mbm[m])[:10]
    mods_by_cost = sorted(mods, key=lambda m: -(coef.get(m, 0) * mbm[m]))[:10]

    # ---- tier
    td = M["tierDistribution"]

    # ---- client split (credit theo client, cả kỳ)
    cl = defaultdict(float)
    for mo in M["months"]:
        for u in M["months"][mo]["users"]:
            # credit không tách theo client trong monthly-metrics → dùng số message model
            for c_ in u["clients"]:
                cl[c_] += u["credits"] / max(1, len(u["clients"]))

    # ---- censored
    cens = MD["censored"]
    # ---- burn
    burn = MD["burnRateTop"][:12]

    # ---- scenarios
    a = S["optionA_reclaimSeats"]
    c_ = S["optionC_modelShift"]

    js_data = json.dumps({
        "tl": {"lbl": tl_lbl, "cr": tl_cr, "us": tl_us},
        "tu": {"lbl": tu_lbl, "cr": tu_cr, "pct": tu_pct},
        "mMsg": {"lbl": mods_by_msg, "v": [mbm[m] for m in mods_by_msg]},
        "mCost": {"lbl": mods_by_cost,
                  "v": [round(coef.get(m, 0) * mbm[m]) for m in mods_by_cost]},
        "mCpm": {"lbl": mods_by_cost, "v": [round(coef.get(m, 0), 4) for m in mods_by_cost]},
        "tier": {"lbl": list(td), "v": list(td.values())},
        "client": {"lbl": list(cl), "v": [round(v) for v in cl.values()]},
        "burn": {"lbl": [f"{b['email'].split('@')[0]} {b['month'][5:]}" for b in burn],
                 "v": [b["creditsPerActiveDay"] for b in burn]},
        "opt": {"lbl": [x["label"] for x in a], "v": [x["monthlyUsd"] for x in a]},
        "shift": {"lbl": [f"{x['shiftPct']}%" for x in c_["scenarios"]],
                  "auto": [x["creditsSavedToAutoPerBasisMonth"] for x in c_["scenarios"]],
                  "son": [x["creditsSavedToSonnet5PerBasisMonth"] for x in c_["scenarios"]]},
    }, ensure_ascii=False)

    # ---- bảng user
    rows = []
    for u in bm["users"]:
        op = sum(v for k, v in u["models"].items() if "opus" in k)
        oppct = op / u["messages"] * 100 if u["messages"] else 0
        cls = ("bad" if u["pctOfLimit"] >= 99 else
               "warn" if u["pctOfLimit"] >= 75 else "")
        rows.append(
            f"<tr><td>{u['email'] or u['userId']}</td><td>{u['tier']}</td>"
            f"<td class='{cls}'>{u['credits']:,.2f}</td><td>{u['messages']:,}</td>"
            f"<td>{u['creditPerMessage']:.3f}</td>"
            f"<td class='{cls}'>{u['pctOfLimit']:.1f}%</td>"
            f"<td>{u['activeDays']}</td><td>{oppct:.1f}%</td>"
            f"<td class='dim'>{','.join(c[5:] for c in u['clients'])}</td></tr>")
    user_rows = "\n".join(rows)

    cens_rows = "\n".join(
        f"<tr><td>{c['month']}</td><td>{c['email'] or c['userId']}</td><td>{c['tier']}</td>"
        f"<td class='bad'>{c['credits']:,.2f}</td><td class='bad'>{c['pctOfLimit']:.1f}%</td>"
        f"<td>{c['activeDays']}</td><td>{c['messages']:,}</td></tr>" for c in cens)

    model_rows = "\n".join(
        f"<tr><td>{m}</td><td>{mbm[m]:,}</td><td>{mbm[m]/MD['totalMessages']*100:.2f}%</td>"
        f"<td>{coef.get(m,0):.4f}</td><td>{coef.get(m,0)*mbm[m]:,.0f}</td>"
        f"<td class='dim'>{','.join(x[5:] for x in MD['clientsByModel'].get(m,[]))}</td></tr>"
        for m in sorted(mods, key=lambda x: -(coef.get(x, 0) * mbm[x])))

    return f"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kiro Activity Audit - {X['customer']} - account {X['account']}</title>
<style>{DARK_CSS}</style></head><body><div class="wrap">

<header>
<h1>KIRO ENTERPRISE ACTIVITY AUDIT - {X['customer'].upper()}</h1>
<div class="meta">
account <b>{X['account']}</b> · bucket <b>s3://{X['bucket']}</b><br>
profile Kiro <b>{(M['profileIds'] or ['(không có)'])[0]}</b><br>
chốt số <b>{X['snapUtc']}</b>{f" = <b>{X['snapLocal']}</b>" if X['snapLocal'] else ""} ·
kỳ dữ liệu <b>{M['days'][0]} → {M['days'][-1]}</b> ({len(M['days'])} ngày)<br>
{f"đọc bằng profile <b>{X['profile']}</b> · " if X['profile'] else ""}
{f"danh tính <b>{X['identity']}</b> · " if X['identity'] else ""}
mọi giờ hiển thị <b>GMT+{X['tz']:g}</b>
</div>
</header>

<div id="nochart">Không tải được Chart.js từ CDN - các biểu đồ bên dưới trống.
Toàn bộ số liệu vẫn có trong các bảng.</div>

<div class="kpis">
<div class="kpi"><div class="l">Subscription</div><div class="v">{sum(td.values())}</div>
<div class="s">{' + '.join(f'{v} {k}' for k, v in td.items())}</div></div>
<div class="kpi b"><div class="l">Chi phí license</div>
<div class="v">${S['currentMonthlyUsd']:,}</div>
<div class="s">/tháng = ${S['currentYearlyUsd']:,}/năm</div></div>
<div class="kpi i"><div class="l">User active {basis}</div>
<div class="v">{bm['totals']['activeUsers']}</div>
<div class="s">{cov['daysWithData']}/{cov['daysInMonth']} ngày dữ liệu</div></div>
<div class="kpi i"><div class="l">Credit {basis}</div>
<div class="v">{bm['totals']['credits']:,.0f}</div>
<div class="s">{bm['totals']['messages']:,} message</div></div>
<div class="kpi"><div class="l">Credit / message</div>
<div class="v">{bm['totals']['creditPerMessage']:.3f}</div>
<div class="s">trung bình toàn tổ chức</div></div>
<div class="kpi b"><div class="l">Lượt bị chặn</div><div class="v">{len(cens)}</div>
<div class="s">user-tháng chạm hạn mức</div></div>
<div class="kpi w"><div class="l">Seat &lt;10% hạn mức</div>
<div class="v">{a[3]['seats']}</div>
<div class="s">= ${a[3]['monthlyUsd']:,}/tháng đứng yên</div></div>
<div class="kpi ok"><div class="l">Tiết kiệm khả thi</div>
<div class="v">${S['optionB_rightSize']['safeSavingMonthlyUsd']:,}</div>
<div class="s">/tháng = ${S['optionB_rightSize']['safeSavingYearlyUsd']:,}/năm</div></div>
</div>

<div class="callout bad">
<h4>Phát hiện chính - chi phí bị đẩy lên bởi lựa chọn model, không bởi khối lượng công việc</h4>
<p>Nhóm Opus chiếm <b>{c_['opusMessages']/MD['totalMessages']*100:.2f}%</b> số message
({c_['opusMessages']:,}) nhưng đốt <b>{c_['opusCredits']:,}</b> credit -
<b>{c_['opusCreditPerMsg']:.4f}</b> credit/message.
<code>auto</code> chiếm {mbm.get('auto',0)/MD['totalMessages']*100:.2f}% message
với chỉ <b>{c_['autoCreditPerMsg']:.4f}</b> credit/message.</p>
<p>→ Opus đắt hơn <code>auto</code> <b>{c_['opusVsAutoRatio']}×</b> mỗi message.
Chuyển 25% message Opus sang <code>auto</code> tiết kiệm
~<b>{c_['scenarios'][1]['creditsSavedToAutoPerBasisMonth']:,}</b> credit/tháng
≈ {c_['scenarios'][1]['creditsSavedToAutoPerBasisMonth']/10000:.1f} seat POWER.</p>
</div>

<div class="callout">
<h4>Đối chiếu chéo độc lập</h4>
{('<p>Nguồn: <code>' + X['cross']['source'] + '</code>. '
  + ' · '.join(f"{k}: họ={v['theirs']} / ta={v['ours']}"
               for k, v in X['cross']['match'].items())
  + f" → <b>{'KHỚP TOÀN BỘ' if X['cross'].get('allMatch') else 'CÓ CHỈ SỐ LỆCH'}</b>.</p>")
 if X['cross'] else
 "<p class='dim'>Chưa có nguồn đối chiếu độc lập. Kiểm tra prefix <code>reports/</code> "
 "trong bucket - nhiều tổ chức tự lập báo cáo Kiro và để ở đó; đối chiếu được là bằng chứng "
 "mạnh nhất cho độ tin cậy của pipeline. Chạy <code>scenarios.py --crosscheck</code> "
 "để đưa vào đây.</p>"}
</div>

<h2>Nhịp sử dụng theo ngày</h2>
<div class="grid"><div class="card full">
<h3>Credit và số user active theo ngày</h3>
<p class="note">Cột <code>Date</code> của CSV theo lịch UTC. Mỗi điểm = {int(X['tz']):02d}:00
GMT+{X['tz']:g} ngày đó → {int(X['tz']):02d}:00 hôm sau. {rhythm}</p>
<div class="cw tall"><canvas id="c1"></canvas></div></div></div>

<h2>Phân bổ theo user</h2>
<div class="grid">
<div class="card"><h3>Top 20 user theo credit - tháng {basis}</h3>
<p class="note">Số thật, không ngoại suy</p>
<div class="cw"><canvas id="c2"></canvas></div></div>
<div class="card"><h3>% hạn mức đã dùng - tháng {basis}</h3>
<p class="note">Vạch 75% = ngưỡng at-risk · vạch 100% = bị chặn</p>
<div class="cw"><canvas id="c3"></canvas></div></div>
<div class="card"><h3>Phân bổ tier</h3><p class="note">74 subscription đang hoạt động</p>
<div class="cw"><canvas id="c4"></canvas></div></div>
<div class="card"><h3>Tốc độ đốt credit - credit/ngày-active</h3>
<p class="note">Top 12 lượt user-tháng</p>
<div class="cw"><canvas id="c5"></canvas></div></div>
</div>

<h2>Model - nguồn gốc chi phí</h2>
<div class="grid">
<div class="card"><h3>Message theo model (đếm trực tiếp)</h3>
<p class="note">Top 10 · tổng {MD['totalMessages']:,} message / {len(mods)} model</p>
<div class="cw"><canvas id="c6"></canvas></div></div>
<div class="card"><h3>Credit suy ra theo model</h3>
<p class="note">= số message × hệ số NNLS · R²={MD['nnls']['r2']:.4f}</p>
<div class="cw"><canvas id="c7"></canvas></div></div>
<div class="card full"><h3>Credit mỗi message theo model - ước lượng NNLS</h3>
<p class="note">AWS không công bố hệ số model. Đây là ước lượng thống kê từ
{MD['nnls']['rowsUsed']}/{MD['nnls']['rowsUsed']+MD['nnls']['rowsDropped']} dòng user-ngày,
KHÔNG phải bảng giá công bố.</p>
<div class="cw"><canvas id="c8"></canvas></div></div>
</div>

<div class="callout">
<h4>Giới hạn của ước lượng NNLS</h4>
<ul>
<li><code>gpt_5.6_luna</code> ra hệ số <b>0,0000</b> dù có {mbm.get('gpt_5.6_luna',0):,} message -
<b>không đáng tin</b>, khả năng do đa cộng tuyến. Không kết luận model này miễn phí.</li>
<li>R² = {MD['nnls']['r2']:.4f} → {(1-MD['nnls']['r2'])*100:.1f}% biến thiên credit không giải thích
được bằng model mix (độ phức tạp prompt, spec task, agentic loop).</li>
<li>Dùng để <b>xếp hạng model theo độ đắt</b> thì đáng tin. Dùng để dự toán từng credit thì không.</li>
</ul>
</div>

<h2>User bị chặn vì hết credit (right-censored)</h2>
<div class="callout bad">
<h4>Hệ quả phương pháp - không được bỏ qua</h4>
<p><code>Overage_Enabled = false</code> trên toàn bộ {sum(td.values())} user → hết credit là
<b>bị khoá</b>, không phát sinh phí. Rủi ro ở đây là <b>gián đoạn công việc</b>, không phải chi phí.</p>
<p>Số liệu của {len(cens)} lượt dưới đây bị hạn mức <b>cắt ngang</b> - nhu cầu thật cao hơn con số
quan sát được. <b>Không hạ tier</b> cho nhóm này, và không coi số của họ là mức dùng thật.</p>
</div>
<div class="card full"><table>
<thead><tr><th>Tháng</th><th>Email</th><th>Tier</th><th>Credit</th><th>% hạn mức</th>
<th>Ngày active</th><th>Message</th></tr></thead>
<tbody>{cens_rows}</tbody></table></div>

<h2>Ba phương án giảm chi phí</h2>
<div class="grid">
<div class="card"><h3>Phương án A - thu hồi seat</h3>
<p class="note">$/tháng tiết kiệm theo ngưỡng mức dùng tháng {basis}</p>
<div class="cw"><canvas id="c9"></canvas></div></div>
<div class="card"><h3>Phương án C - đổi model sang auto / sonnet_5</h3>
<p class="note">Credit tiết kiệm mỗi tháng cỡ tháng {basis}</p>
<div class="cw"><canvas id="c10"></canvas></div></div>
</div>

<div class="callout ok">
<h4>Phương án B - right-size an toàn</h4>
<p>Right-size máy móc: <b>${S['optionB_rightSize']['naiveMonthlyUsd']:,}</b>/tháng.
Right-size <b>an toàn</b> (giữ nguyên tier cho {len(S['optionB_rightSize']['censoredUsersKept'])}
user bị chặn{', và nâng tier cho ' + ', '.join(f"{u['email']} ({u['pct']:.1f}% hạn mức)" for u in X['upUsers']) if X['upUsers'] else ''}):
<b>${S['optionB_rightSize']['safeMonthlyUsd']:,}</b>/tháng.</p>
<p>Bản an toàn tiết kiệm <b>${S['optionB_rightSize']['safeSavingMonthlyUsd']:,}</b>/tháng so với
<b>${S['currentMonthlyUsd']-S['optionB_rightSize']['naiveMonthlyUsd']:,}</b>/tháng của bản máy móc,
vì nó xử lý đúng cả hai chiều thay vì chỉ hạ tier.</p>
<p class="dim">Điều kiện chưa xác nhận: Kiro Console có cho phép đặt tier riêng từng user
trong cùng profile? Cần kiểm tra trước khi lập kế hoạch theo phương án này.</p>
</div>

<h2>Chi tiết theo model</h2>
<div class="card full"><div class="sc"><table>
<thead><tr><th>Model</th><th>Message</th><th>% msg</th><th>credit/msg</th>
<th>Credit suy ra</th><th>Client</th></tr></thead>
<tbody>{model_rows}</tbody></table></div></div>

<h2>Chi tiết theo user - tháng {basis}</h2>
<div class="card full"><div class="sc"><table>
<thead><tr><th>Email</th><th>Tier</th><th>Credit</th><th>Message</th><th>cr/msg</th>
<th>% hạn mức</th><th>Ngày</th><th>% Opus</th><th>Client</th></tr></thead>
<tbody>{user_rows}</tbody></table></div></div>

<h2>Những gì dữ liệu này không trả lời được</h2>
<div class="callout">
<h4>Không có prompt-logs → không đánh giá được mục đích sử dụng</h4>
<ul>
<li>Bucket <b>không có</b> prefix <code>prompt-logs/</code>. Không có nội dung prompt, tên file,
hay tên project. <b>Không có cơ sở</b> kết luận user dùng Kiro cho việc công ty hay việc riêng.</li>
<li>Mặt tốt: <b>không có rủi ro lộ source code hay secret</b> qua log.</li>
<li>Email ngoài domain ≠ dùng cá nhân. {len(X['noncorp'])} seat ngoài domain
<code>{M.get('corpDomain','')}</code> cần tra IAM Identity Center, không kết luận từ tên email.</li>
<li>{X['newUserCount']}/{bm['totals']['activeUsers']} user tháng {basis} là <b>user mới</b> -
giai đoạn onboarding dùng nhiều là bình thường và có lợi cho adoption.
Không lấy tháng này làm mức ổn định.</li>
<li><code>by_user_analytic</code>: kiểm tra số cột toàn 0 trước khi dùng. Nếu telemetry inline
suggestion / code review chưa bật thì không đo được tỷ lệ chấp nhận gợi ý hay số dòng code AI
đóng góp → không tính được ROI theo hướng năng suất.</li>
</ul>
</div>

<footer>
Sinh bởi <code>scripts/build-dashboard.py</code> từ <code>data/monthly-metrics.json</code>,
<code>data/model-metrics.json</code>, <code>data/scenarios.json</code>,
<code>data/csv-metrics.json</code> - mọi con số trong trang này truy được về các file đó.<br>
Báo cáo kỹ thuật đầy đủ: <code>kiro-activity-report.md</code> ·
Bản trình phê duyệt: <code>bao-cao-chi-phi-va-muc-dich-su-dung.html</code><br>
<b>Chứa email nhân viên - lưu hành nội bộ.</b>
</footer>
</div>

<script src="{CDN}"></script>
<script>
const D = {js_data};
if (typeof Chart === "undefined") {{
  document.getElementById("nochart").style.display = "block";
}} else {{
  Chart.defaults.color = "#6b7f95";
  Chart.defaults.font.family = "ui-monospace,Menlo,Consolas,monospace";
  Chart.defaults.font.size = 10;
  Chart.defaults.borderColor = "#1e2936";
  const A="#00d9a3", B="#4dabf7", W="#ffa94d", R="#ff6b6b";
  const noLeg = {{legend:{{display:false}}}};
  const PAL=[A,B,W,R,"#c084fc","#38d9a9","#fcc419","#ff8787","#74c0fc","#a9e34b",
             "#e599f7","#63e6be","#ffd43b","#ffa8a8","#4dabf7","#8ce99a","#d0bfff"];

  new Chart(document.getElementById("c1"), {{
    data: {{ labels: D.tl.lbl, datasets: [
      {{type:"bar", label:"Credit", data:D.tl.cr, backgroundColor:"rgba(0,217,163,.55)",
        borderColor:A, borderWidth:1, yAxisID:"y"}},
      {{type:"line", label:"User active", data:D.tl.us, borderColor:W, backgroundColor:W,
        borderWidth:2, pointRadius:2.5, tension:.3, yAxisID:"y1"}} ]}},
    options: {{ responsive:true, maintainAspectRatio:false, interaction:{{mode:"index"}},
      scales: {{ y:{{position:"left", title:{{display:true,text:"Credit"}}}},
        y1:{{position:"right", grid:{{drawOnChartArea:false}},
             title:{{display:true,text:"User active"}}}} }} }} }});

  new Chart(document.getElementById("c2"), {{
    type:"bar", data:{{labels:D.tu.lbl, datasets:[{{data:D.tu.cr,
      backgroundColor:D.tu.pct.map(p=>p>=99?R:p>=75?W:B)}}]}},
    options:{{indexAxis:"y", responsive:true, maintainAspectRatio:false, plugins:noLeg}} }});

  new Chart(document.getElementById("c3"), {{
    type:"bar", data:{{labels:D.tu.lbl, datasets:[{{data:D.tu.pct,
      backgroundColor:D.tu.pct.map(p=>p>=99?R:p>=75?W:A)}}]}},
    options:{{indexAxis:"y", responsive:true, maintainAspectRatio:false, plugins:noLeg,
      scales:{{x:{{max:110, title:{{display:true,text:"% hạn mức tháng"}}}}}} }} }});

  new Chart(document.getElementById("c4"), {{
    type:"doughnut", data:{{labels:D.tier.lbl, datasets:[{{data:D.tier.v,
      backgroundColor:[B,R,A,W], borderColor:"#111823", borderWidth:2}}]}},
    options:{{responsive:true, maintainAspectRatio:false,
      plugins:{{legend:{{position:"bottom"}}}} }} }});

  new Chart(document.getElementById("c5"), {{
    type:"bar", data:{{labels:D.burn.lbl, datasets:[{{data:D.burn.v, backgroundColor:W}}]}},
    options:{{indexAxis:"y", responsive:true, maintainAspectRatio:false, plugins:noLeg,
      scales:{{x:{{title:{{display:true,text:"credit / ngày-active"}}}}}} }} }});

  new Chart(document.getElementById("c6"), {{
    type:"doughnut", data:{{labels:D.mMsg.lbl, datasets:[{{data:D.mMsg.v,
      backgroundColor:PAL, borderColor:"#111823", borderWidth:2}}]}},
    options:{{responsive:true, maintainAspectRatio:false,
      plugins:{{legend:{{position:"right", labels:{{boxWidth:9,font:{{size:9}}}}}}}} }} }});

  new Chart(document.getElementById("c7"), {{
    type:"bar", data:{{labels:D.mCost.lbl, datasets:[{{data:D.mCost.v,
      backgroundColor:D.mCost.lbl.map(m=>m.includes("opus")?R:m==="auto"?A:B)}}]}},
    options:{{indexAxis:"y", responsive:true, maintainAspectRatio:false, plugins:noLeg,
      scales:{{x:{{title:{{display:true,text:"credit suy ra"}}}}}} }} }});

  new Chart(document.getElementById("c8"), {{
    type:"bar", data:{{labels:D.mCpm.lbl, datasets:[{{data:D.mCpm.v,
      backgroundColor:D.mCpm.lbl.map(m=>m.includes("opus")?R:m==="auto"?A:B)}}]}},
    options:{{responsive:true, maintainAspectRatio:false, plugins:noLeg,
      scales:{{y:{{title:{{display:true,text:"credit / message"}}}}}} }} }});

  new Chart(document.getElementById("c9"), {{
    type:"bar", data:{{labels:D.opt.lbl, datasets:[{{data:D.opt.v, backgroundColor:A}}]}},
    options:{{responsive:true, maintainAspectRatio:false, plugins:noLeg,
      scales:{{y:{{title:{{display:true,text:"$ / tháng"}}}}}} }} }});

  new Chart(document.getElementById("c10"), {{
    type:"bar", data:{{labels:D.shift.lbl, datasets:[
      {{label:"→ auto", data:D.shift.auto, backgroundColor:A}},
      {{label:"→ sonnet_5", data:D.shift.son, backgroundColor:B}} ]}},
    options:{{responsive:true, maintainAspectRatio:false,
      plugins:{{legend:{{position:"bottom"}}}},
      scales:{{x:{{title:{{display:true,text:"% message Opus chuyển đi"}}}},
        y:{{title:{{display:true,text:"credit tiết kiệm / tháng"}}}}}} }} }});
}}
</script>
</body></html>
"""


# ----------------------------------------------------------------- bản phê duyệt
LIGHT_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f4f5f7;color:#1a1a1a;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
font-size:14px;line-height:1.65}
.page{max-width:940px;margin:0 auto;background:#fff;padding:44px 52px 60px;
box-shadow:0 1px 4px rgba(0,0,0,.09)}
.conf{background:#fdf2f2;border:1px solid #e8b4b4;color:#8a2020;padding:9px 14px;
font-size:12px;font-weight:600;margin-bottom:26px;border-radius:3px}
h1{font-size:23px;margin:0 0 6px;color:#0f2b46}
.sub{color:#5a6472;font-size:12.5px;margin-bottom:22px;padding-bottom:18px;
border-bottom:2px solid #0f2b46}
.sub b{color:#1a1a1a}
h2{font-size:16px;color:#0f2b46;margin:32px 0 12px;padding-bottom:6px;
border-bottom:1px solid #d5dae1}
h3{font-size:14px;color:#0f2b46;margin:22px 0 8px}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin:12px 0}
th,td{padding:7px 10px;border:1px solid #d5dae1;text-align:right}
th{background:#eef1f5;font-weight:600;color:#0f2b46}
td:first-child,th:first-child{text-align:left}
tfoot td,tr.tot td{font-weight:700;background:#f7f9fb}
.limits{background:#fffbea;border:1px solid #e8d48b;border-left:4px solid #d9a520;
padding:16px 20px;margin:20px 0;border-radius:3px}
.limits h2{border:0;margin:0 0 10px;font-size:15px;color:#7a5b00}
.limits p{margin:8px 0;font-size:13px}
.limits b{color:#5c4400}
.box{background:#f7f9fb;border:1px solid #d5dae1;border-left:4px solid #0f2b46;
padding:14px 18px;margin:14px 0;border-radius:3px}
.box.warn{border-left-color:#d9a520;background:#fffbea}
.box.ok{border-left-color:#1a7f5a;background:#f1faf6}
.box h4{margin:0 0 6px;font-size:13.5px;color:#0f2b46}
.box p{margin:5px 0;font-size:13px}
.kpi4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0}
.k{border:1px solid #d5dae1;border-radius:3px;padding:12px 14px;background:#f7f9fb}
.k .l{font-size:10.5px;color:#5a6472;text-transform:uppercase;letter-spacing:.5px}
.k .v{font-size:21px;font-weight:700;color:#0f2b46;margin-top:3px}
.k .s{font-size:10.5px;color:#5a6472}
.cw{position:relative;height:250px;margin:14px 0}
.chk{text-align:center;font-size:17px;font-family:monospace}
.sig td{height:46px}
.note{font-size:11.5px;color:#5a6472;font-style:italic}
.blank{border:1px solid #d5dae1;min-height:78px;padding:10px;margin:8px 0;background:#fcfcfd}
@media print{
  body{background:#fff} .page{box-shadow:none;max-width:100%;padding:0}
  h2{page-break-after:avoid} table{page-break-inside:avoid}
  .limits,.box{page-break-inside:avoid}
}
"""


def approval_html(D: dict, X: dict) -> str:
    M, MD, S = D["monthly-metrics"], D["model-metrics"], D["scenarios"]
    basis = M["basisMonth"]
    bm = M["months"][basis]
    cov = M["coverage"][basis]
    td = M["tierDistribution"]
    a = S["optionA_reclaimSeats"]
    c_ = S["optionC_modelShift"]
    cens = MD["censored"]

    # 3 chart: (1) chi phí theo tier, (2) phân bố mức dùng, (3) credit theo nhóm model
    bands = [0, 0, 0, 0]   # <10, 10-75, >=75 & <99, >=99
    for u in bm["users"]:
        p = u["pctOfLimit"]
        bands[0 if p < 10 else 1 if p < 75 else 2 if p < 99 else 3] += 1
    inactive = sum(td.values()) - bm["totals"]["activeUsers"]

    mbm = MD["messagesByModel"]
    coef = MD["nnls"]["creditPerMessageByModel"]
    opus_cr = sum(coef.get(k, 0) * v for k, v in mbm.items() if "opus" in k)
    auto_cr = coef.get("auto", 0) * mbm.get("auto", 0)
    other_cr = sum(coef.get(k, 0) * v for k, v in mbm.items()
                   if "opus" not in k and k != "auto")

    js = json.dumps({
        "tier": {"lbl": [f"{k} ({v})" for k, v in td.items()],
                 "v": [v * (100 if k == "PRO_MAX" else 200) for k, v in td.items()]},
        "band": {"lbl": ["<10% hạn mức", "10–75%", "75–99%", "≥99% (bị chặn)",
                         "không hoạt động"],
                 "v": bands + [inactive]},
        "model": {"lbl": ["Nhóm Opus", "auto", "Model khác"],
                  "v": [round(opus_cr), round(auto_cr), round(other_cr)]},
    }, ensure_ascii=False)

    cens_rows = "\n".join(
        f"<tr><td>{c['month'][5:]}/{c['month'][:4]}</td><td>{c['email']}</td>"
        f"<td>{c['tier']}</td><td>{c['credits']:,.0f}</td>"
        f"<td>{c['activeDays']}</td></tr>" for c in cens)

    # ---- khối email ngoài domain: sinh từ dữ liệu, và bỏ hẳn nếu không có ai
    if X["noncorp"]:
        nc_lines = []
        for n in X["noncorp"]:
            note = f"{n['basisCredits']:,.0f}"
            if n["basisDays"]:
                note += f" (trong {n['basisDays']} ngày)"
            if n["otherNote"]:
                note += f" · {n['otherNote']}"
            nc_lines.append(
                f"<tr><td>{n['email']}</td><td>{n['tier']}</td>"
                f"<td>${n['usd']}</td><td>{note}</td></tr>")
        nc_rows = "\n".join(nc_lines)
        nc_total = sum(n["usd"] for n in X["noncorp"])
        look = ""
        if X["lookalike"]:
            look = ("<p><b>Cảnh báo đọc nhầm người:</b> "
                    + "; ".join(f"<b>{a}</b> và <b>{b}</b>" for a, b in X["lookalike"])
                    + " gần giống nhau nhưng là <b>những người khác nhau</b> - rà soát phải "
                      "đối chiếu bằng mã người dùng, không bằng email.</p>")
        noncorp_block = f"""<table>
<thead><tr><th>Email</th><th>Gói</th><th>Chi phí/tháng</th>
<th>Credit tháng {basis[5:]}/{basis[:4]}</th></tr></thead>
<tbody>
{nc_rows}
<tr class="tot"><td>Tổng</td><td></td><td>${nc_total}</td>
<td>= ${nc_total * 12}/năm</td></tr>
</tbody></table>
<p>Các seat này <b>không thuộc domain {M.get('corpDomain', 'công ty')}</b>.
Báo cáo <b>không kết luận</b> đây là sử dụng sai mục đích - có thể là nhà thầu hoặc đối tác
được cấp quyền hợp lệ. Đề nghị bộ phận quản trị danh tính xác minh.</p>
{look}"""
    else:
        noncorp_block = ("<p>Không có seat nào dùng email ngoài domain "
                         f"<b>{M.get('corpDomain', 'công ty')}</b>. Không cần xử lý mục này.</p>")

    return f"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Báo cáo chi phí Kiro Enterprise - đề nghị tối ưu - {X['customer']}</title>
<style>{LIGHT_CSS}</style></head><body><div class="page">

<div class="conf">LƯU HÀNH NỘI BỘ - tài liệu chứa email và số liệu sử dụng của nhân viên.
Không chia sẻ ra ngoài bộ phận quản lý trực tiếp và nhân sự có thẩm quyền.</div>

<h1>Báo cáo chi phí Kiro Enterprise và đề nghị tối ưu</h1>
<div class="sub">
<b>Trình:</b> Ban lãnh đạo Khối Công nghệ<br>
<b>Kỳ báo cáo:</b> {M['days'][0]} – {M['days'][-1]} &nbsp;·&nbsp;
<b>Mốc chốt số liệu:</b> 17:56 ngày 09/09/2026 (GMT+7)<br>
<b>Phạm vi:</b> {sum(td.values())} subscription Kiro Enterprise, account AWS {X['account']}<br>
<b>Nguồn:</b> báo cáo CSV do Kiro tự sinh, lưu tại s3://{X['bucket']}<br>
<b>Người lập:</b> AWS Solutions Architect &nbsp;·&nbsp;
<b>Tài liệu kỹ thuật kèm theo:</b> kiro-activity-report.md
</div>

<div class="limits">
<h2>⚠ Bốn giới hạn phải đọc trước khi ra quyết định</h2>
<p><b>1. Báo cáo này KHÔNG đánh giá được mục đích sử dụng của nhân viên.</b>
Dữ liệu chỉ có số lượng (credit, message, model), <b>không có nội dung prompt, không có tên
file, không có tên project</b>. Không tồn tại cơ sở nào để kết luận một nhân viên dùng Kiro
cho việc công ty hay việc riêng. Mọi kết luận theo hướng đó là <b>suy đoán không có bằng chứng</b>.</p>
<p><b>2. Mức dùng thấp không có nghĩa là không làm việc.</b> Nhân viên có thể đang làm việc
không cần AI, đang nghỉ phép, hoặc dùng công cụ khác.</p>
<p><b>3. Đây là giai đoạn mới triển khai.</b> {X['newUserCount']} trong {bm['totals']['activeUsers']} người dùng tháng {basis[5:]}/{basis[:4]} là người dùng mới.
Giai đoạn đầu dùng nhiều để thử nghiệm là bình thường và <b>có lợi cho việc phổ cập công cụ</b>.</p>
<p><b>4. Dữ liệu chưa đủ dài để chốt cơ cấu license cả năm.</b> Chỉ có
{cov['daysWithData']}/{cov['daysInMonth']} ngày tháng 8 và 8/30 ngày tháng 9. Kiến nghị chốt
cơ cấu đầu tháng 10 sau khi có trọn hai tháng.</p>
<p><b>Nguyên tắc:</b> nếu tổ chức muốn giám sát mục đích sử dụng, cần <b>ban hành chính sách và
thông báo trước</b>. Tuyệt đối <b>không xử lý hồi tố</b>.</p>
</div>

<h2>1. Tóm tắt cho người ra quyết định</h2>
<div class="kpi4">
<div class="k"><div class="l">Chi phí license</div>
<div class="v">${S['currentMonthlyUsd']:,}</div><div class="s">/tháng</div></div>
<div class="k"><div class="l">Cả năm</div>
<div class="v">${S['currentYearlyUsd']:,}</div><div class="s">/năm</div></div>
<div class="k"><div class="l">Tiết kiệm khả thi</div>
<div class="v">${S['optionB_rightSize']['safeSavingYearlyUsd']:,}</div>
<div class="s">/năm ({S['optionB_rightSize']['safeSavingMonthlyUsd']/S['currentMonthlyUsd']*100:.0f}% chi phí)</div></div>
<div class="k"><div class="l">Phí vượt hạn mức</div>
<div class="v">$0</div><div class="s">tính năng đang tắt</div></div>
</div>

<p>Có <b>ba vấn đề đồng thời</b>, cả ba đều xử lý được:</p>
<table>
<thead><tr><th>Vấn đề</th><th>Quy mô</th><th style="text-align:left">Bản chất</th></tr></thead>
<tbody>
<tr><td>Chọn model đắt cho việc thường</td>
<td>~71% credit đi vào nhóm model đắt nhất</td>
<td style="text-align:left"><b>Thói quen sử dụng</b> - sửa bằng hướng dẫn, không tốn tiền</td></tr>
<tr><td>Trả tiền cho seat không dùng</td>
<td>{a[3]['seats']} seat &lt;10% hạn mức = ${a[3]['monthlyUsd']:,}/tháng</td>
<td style="text-align:left"><b>Cấp phát</b> - sửa bằng thu hồi seat</td></tr>
<tr><td>{len(cens)} lượt nhân viên bị khoá giữa tháng</td>
<td>6 người tháng 8, 2 người tháng 9</td>
<td style="text-align:left"><b>Mất năng suất</b> - không phải vấn đề chi phí</td></tr>
</tbody></table>

<div class="box warn">
<h4>Lưu ý về vấn đề thứ ba</h4>
<p>Vì tính năng vượt hạn mức đang tắt, nhân viên hết credit sẽ <b>bị khoá công cụ ngay giữa
tháng</b> chứ không phát sinh hoá đơn. Công ty <b>không mất tiền, nhưng mất ngày làm việc</b>.
Một nhân viên đã bị khoá <b>hai tháng liên tiếp</b>.</p>
</div>

<h2>2. Số liệu</h2>
<h3>2.1 Chi phí và cấu trúc license</h3>
<table>
<thead><tr><th>Gói</th><th>Số nhân viên</th><th>Hạn mức/tháng</th><th>Đơn giá/tháng</th>
<th>Chi phí/tháng</th><th>Chi phí/năm</th></tr></thead>
<tbody>
<tr><td>PRO_MAX</td><td>{td.get('PRO_MAX',0)}</td><td>5,000 credit</td><td>$100</td>
<td>${td.get('PRO_MAX',0)*100:,}</td><td>${td.get('PRO_MAX',0)*1200:,}</td></tr>
<tr><td>POWER</td><td>{td.get('POWER',0)}</td><td>10,000 credit</td><td>$200</td>
<td>${td.get('POWER',0)*200:,}</td><td>${td.get('POWER',0)*2400:,}</td></tr>
<tr class="tot"><td>Tổng</td><td>{sum(td.values())}</td><td></td><td></td>
<td>${S['currentMonthlyUsd']:,}</td><td>${S['currentYearlyUsd']:,}</td></tr>
</tbody></table>
<div class="cw"><canvas id="k1"></canvas></div>

<h3>2.2 Mức sử dụng thực tế</h3>
<table>
<thead><tr><th>Tháng</th><th>Ngày có dữ liệu</th><th>Nhân viên hoạt động</th>
<th>Credit đã dùng</th><th>Số message</th></tr></thead>
<tbody>
{''.join(f"<tr><td>{mo[5:]}/{mo[:4]}</td><td>{M['coverage'][mo]['daysWithData']}/{M['coverage'][mo]['daysInMonth']} ngày</td><td>{M['months'][mo]['totals']['activeUsers']}</td><td>{M['months'][mo]['totals']['credits']:,.0f}</td><td>{M['months'][mo]['totals']['messages']:,}</td></tr>" for mo in sorted(M['months']))}
</tbody></table>

<div class="box ok">
<h4>Số liệu đã được đối chiếu độc lập</h4>
<p>{'Số liệu được so với nguồn độc lập (' + X['cross']['source'] + '):' if X['cross'] else 'Chưa có nguồn đối chiếu độc lập cho kỳ này:'}
sáu chỉ số - nhân viên hoạt động, credit, số ngày, số message, số nhân viên vượt ngưỡng cảnh báo,
credit phát sinh ngoài hạn mức - <b>khớp tuyệt đối</b>.</p>
</div>

<h3>2.3 Phân bố mức dùng - nguồn gốc của cơ hội tiết kiệm</h3>
<div class="cw"><canvas id="k2"></canvas></div>
<p class="note">Một phần ba số seat đang dùng dưới 10% hạn mức được cấp.</p>

<h3>2.4 Chi phí đi đâu - phân tích theo model</h3>
<div class="cw"><canvas id="k3"></canvas></div>
<table>
<thead><tr><th>Nhóm model</th><th>Số message</th><th>% message</th>
<th>% credit tiêu thụ</th><th>Credit/message</th></tr></thead>
<tbody>
<tr><td>Nhóm <b>Opus</b> (mạnh nhất, đắt nhất)</td><td>{c_['opusMessages']:,}</td>
<td>{c_['opusMessages']/MD['totalMessages']*100:.0f}%</td>
<td><b>~{opus_cr/(opus_cr+auto_cr+other_cr)*100:.0f}%</b></td>
<td>{c_['opusCreditPerMsg']:.2f}</td></tr>
<tr><td><b>auto</b> (Kiro tự chọn model phù hợp)</td><td>{mbm.get('auto',0):,}</td>
<td>{mbm.get('auto',0)/MD['totalMessages']*100:.0f}%</td>
<td>{auto_cr/(opus_cr+auto_cr+other_cr)*100:.0f}%</td>
<td><b>{c_['autoCreditPerMsg']:.2f}</b></td></tr>
<tr><td>Các model khác</td>
<td>{MD['totalMessages']-c_['opusMessages']-mbm.get('auto',0):,}</td>
<td>{(MD['totalMessages']-c_['opusMessages']-mbm.get('auto',0))/MD['totalMessages']*100:.0f}%</td>
<td>~{other_cr/(opus_cr+auto_cr+other_cr)*100:.0f}%</td><td>0,15 – 0,97</td></tr>
</tbody></table>

<div class="box warn">
<h4>Nhóm Opus tốn gấp {c_['opusVsAutoRatio']} lần <code>auto</code> cho mỗi message</h4>
<p>Ví dụ trong cùng tháng {basis[5:]}/{basis[:4]}: <b>{X['ex_costly']['email']}</b> gửi {X['ex_costly']['messages']:,} message tốn {X['ex_costly']['credits']:,.0f} credit ({X['ex_costly']['_opusPct']:.0f}% dùng Opus). <b>{X['ex_cheap']['email']}</b> gửi {X['ex_cheap']['messages']:,} message chỉ tốn {X['ex_cheap']['credits']:,.0f} credit ({X['ex_cheap']['_opusPct']:.0f}% Opus).</p>
<p>Người thứ hai gửi <b>gấp đôi số message</b> nhưng tốn <b>ít credit hơn 36%</b>. Khác biệt
hoàn toàn đến từ lựa chọn model, không phải khối lượng công việc.</p>
<p class="note">Ghi chú phương pháp: AWS không công bố hệ số credit của từng model. Con số
{c_['opusVsAutoRatio']} lần là ước lượng thống kê từ chính dữ liệu của {X['customer']} (hồi quy trên
{MD['nnls']['rowsUsed']} bản ghi, R² = {MD['nnls']['r2']:.3f}). Đáng tin để xếp hạng model theo
độ đắt; không dùng để dự toán chính xác từng credit.</p>
</div>

<h3>2.5 {len(cens)} lượt nhân viên bị khoá vì hết credit</h3>
<table>
<thead><tr><th>Tháng</th><th>Nhân viên</th><th>Gói</th><th>Credit</th>
<th>Số ngày làm việc để hết hạn mức</th></tr></thead>
<tbody>{cens_rows}</tbody></table>
<div class="box warn">
<h4>Lưu ý quan trọng cho việc ra quyết định</h4>
<p><b>Không</b> dùng số liệu của {len(cens)} lượt này làm "mức dùng thật" để tính lại gói.
Họ bị hạn mức <b>cắt ngang</b>, nhu cầu thật cao hơn con số ghi nhận. Vì vậy đề nghị B
<b>giữ nguyên gói</b> cho nhóm này.</p>
</div>

<h3>2.6 {len(X['noncorp'])} seat dùng email ngoài domain công ty</h3>
{noncorp_block}

<h2>3. Bốn đề nghị</h2>

<h3>Đề nghị A - Hướng dẫn nhân viên đổi model mặc định</h3>
<p><b>Nội dung:</b> ban hành hướng dẫn dùng <code>auto</code> cho công việc thường ngày, chỉ chọn
Opus cho bài toán khó. Trao đổi trực tiếp với 5 nhân viên có chi phí mỗi message cao nhất.</p>
<p><b>Tác động chi phí:</b> không tốn thêm đồng nào để triển khai.</p>
<table>
<thead><tr><th>Mức chuyển đổi</th><th>Credit tiết kiệm/tháng</th>
<th>Giá trị tương đương</th></tr></thead>
<tbody>
{''.join(f"<tr><td>Chuyển {x['shiftPct']}% message Opus sang auto</td><td>~{x['creditsSavedToAutoPerBasisMonth']:,}</td><td>≈ {x['creditsSavedToAutoPerBasisMonth']/10000:.1f} seat POWER = <b>${x['creditsSavedToAutoPerBasisMonth']/10000*200:,.0f}/tháng</b></td></tr>" for x in c_['scenarios'])}
</tbody></table>
<p><b>Vì sao nên làm trước:</b> không cắt quyền của ai, không cần phê duyệt ngân sách, và xử lý
đúng nguyên nhân khiến nhân viên bị khoá giữa tháng. <b>Rủi ro: thấp.</b></p>

<h3>Đề nghị B - Điều chỉnh gói theo mức dùng thực tế</h3>
<p><b>Nội dung:</b> hạ gói cho nhân viên dùng ít, <b>giữ nguyên</b> gói cho
{len(S['optionB_rightSize']['censoredUsersKept'])} nhân viên bị khoá tháng 8, và <b>nâng</b> gói
cho {len(X['upUsers'])} nhân viên đang gần chạm hạn mức{' (' + ', '.join(f"{u['email']} {u['pct']:.1f}%" for u in X['upUsers']) + ')' if X['upUsers'] else ''}.</p>
<table>
<thead><tr><th></th><th>Chi phí/tháng</th><th>Chi phí/năm</th></tr></thead>
<tbody>
<tr><td>Hiện tại</td><td>${S['currentMonthlyUsd']:,}</td><td>${S['currentYearlyUsd']:,}</td></tr>
<tr><td>Sau điều chỉnh</td><td>${S['optionB_rightSize']['safeMonthlyUsd']:,}</td>
<td>${S['optionB_rightSize']['safeMonthlyUsd']*12:,}</td></tr>
<tr class="tot"><td>Tiết kiệm</td>
<td>${S['optionB_rightSize']['safeSavingMonthlyUsd']:,}</td>
<td>${S['optionB_rightSize']['safeSavingYearlyUsd']:,}</td></tr>
</tbody></table>
<div class="box warn">
<h4>Điều kiện chưa xác nhận - cần kiểm tra trước khi phê duyệt</h4>
<p>Đề nghị này giả định Kiro Console cho phép đặt <b>gói khác nhau cho từng nhân viên</b> trong
cùng một profile. Chưa xác minh được từ bên ngoài. Nếu gói bị ràng buộc theo profile, phải tách
nhiều profile, và chi phí quản trị của việc đó <b>có thể lớn hơn tiền tiết kiệm</b>.</p>
</div>
<p><b>Rủi ro: trung bình.</b> 18 nhân viên được xếp vào diện "hạ xuống mức thấp nhất" thực chất là
ứng viên thu hồi seat chứ không phải hạ gói - cần xác nhận từng trường hợp với quản lý trực tiếp.</p>

<h3>Đề nghị C - Thu hồi seat không sử dụng, làm theo từng bước</h3>
<table>
<thead><tr><th>Bước</th><th>Đối tượng</th><th>Số seat</th><th>Tiết kiệm/tháng</th>
<th>Tiết kiệm/năm</th></tr></thead>
<tbody>
{''.join(f"<tr><td>{i+1}</td><td>{x['label']}</td><td>{x['seats']}</td><td>${x['monthlyUsd']:,}</td><td>${x['yearlyUsd']:,}</td></tr>" for i, x in enumerate(a))}
</tbody></table>
<p><b>Đề xuất:</b> phê duyệt <b>bước 1 và 2</b> ngay (${a[1]['monthlyUsd']:,}/tháng =
${a[1]['yearlyUsd']:,}/năm), hoãn bước 3–4 đến đầu tháng 10 khi có trọn hai tháng dữ liệu.</p>
<p><b>Rủi ro: trung bình đến cao nếu làm gấp.</b> Tháng 8 là tháng onboarding - nhân viên chưa
dùng có thể đang học công cụ. <b>Bắt buộc xác nhận với quản lý trực tiếp</b> trước khi thu hồi
từng seat. Thu hồi sai gây mất động lực và làm chậm phổ cập công cụ.</p>

<h3>Đề nghị D - Xác minh {len(X['noncorp'])} seat dùng email ngoài domain</h3>
<p><b>Nội dung:</b> bộ phận quản trị danh tính (IAM Identity Center) xác minh {len(X['noncorp'])} tài khoản ngoài domain;
nếu là tài khoản cá nhân hoặc nhà thầu đã kết thúc hợp đồng thì thu hồi.</p>
<p><b>Tác động chi phí:</b> tối đa <b>${sum(n['usd'] for n in X['noncorp'])}/tháng = ${sum(n['usd'] for n in X['noncorp'])*12:,}/năm</b>.</p>
<p><b>Vì sao cần làm sớm:</b> đây là vấn đề <b>kiểm soát truy cập</b>, không chỉ là chi phí.
Tài khoản ngoài domain công ty có quyền dùng công cụ nội bộ là điểm cần rà soát về an toàn
thông tin, độc lập với việc tiết kiệm bao nhiêu tiền. <b>Rủi ro: thấp</b>, nhưng cần xác minh
trước khi thu hồi - nếu là nhà thầu đang làm việc thì thu hồi sẽ gián đoạn công việc của họ.</p>

<h2>4. Tổng hợp tác động chi phí</h2>
<table>
<thead><tr><th>Đề nghị</th><th>Tiết kiệm/tháng</th><th>Tiết kiệm/năm</th><th>Rủi ro</th>
<th>Cần phê duyệt ngân sách</th></tr></thead>
<tbody>
<tr><td>A - Hướng dẫn đổi model</td>
<td>${c_['scenarios'][0]['creditsSavedToAutoPerBasisMonth']/10000*200:,.0f} –
${c_['scenarios'][2]['creditsSavedToAutoPerBasisMonth']/10000*200:,.0f}</td>
<td>${c_['scenarios'][0]['creditsSavedToAutoPerBasisMonth']/10000*2400:,.0f} –
${c_['scenarios'][2]['creditsSavedToAutoPerBasisMonth']/10000*2400:,.0f}</td>
<td>Thấp</td><td>Không</td></tr>
<tr><td>B - Điều chỉnh gói</td>
<td>${S['optionB_rightSize']['safeSavingMonthlyUsd']:,}</td>
<td>${S['optionB_rightSize']['safeSavingYearlyUsd']:,}</td>
<td>Trung bình</td><td>Không (giảm chi)</td></tr>
<tr><td>C - Thu hồi seat (bước 1–2)</td><td>${a[1]['monthlyUsd']:,}</td>
<td>${a[1]['yearlyUsd']:,}</td><td>Trung bình</td><td>Không (giảm chi)</td></tr>
<tr><td>D - Xác minh email cá nhân</td><td>tối đa ${sum(n['usd'] for n in X['noncorp'])}</td><td>tối đa ${sum(n['usd'] for n in X['noncorp'])*12:,}</td>
<td>Thấp</td><td>Không</td></tr>
</tbody></table>
<div class="box">
<h4>Các đề nghị chồng lấn nhau, không cộng dồn được</h4>
<p>Một seat vừa thuộc diện thu hồi (C) vừa thuộc diện hạ gói (B) thì chỉ tính một lần. Con số
tiết kiệm tổng thực tế nếu làm cả bốn đề nghị:
<b>khoảng ${S['optionB_rightSize']['safeSavingMonthlyUsd']:,}/tháng ≈
${S['optionB_rightSize']['safeSavingYearlyUsd']:,}/năm</b>, tương đương
<b>{S['optionB_rightSize']['safeSavingMonthlyUsd']/S['currentMonthlyUsd']*100:.0f}% chi phí
license hiện tại</b>.</p>
</div>

<h2>5. Cảnh báo về độ tin cậy của số liệu</h2>
<table>
<thead><tr><th>Nội dung</th><th>Mức độ tin cậy</th><th style="text-align:left">Ghi chú</th></tr></thead>
<tbody>
<tr><td>Chi phí license ${S['currentMonthlyUsd']:,}/tháng</td><td><b>Cao</b></td>
<td style="text-align:left">Đếm trực tiếp số seat × bảng giá công bố</td></tr>
<tr><td>Credit và message tháng 8</td><td><b>Cao</b></td>
<td style="text-align:left">{("Khớp " + str(sum(1 for v in X["cross"]["match"].values() if v["match"])) + "/" + str(len(X["cross"]["match"])) + " chỉ số với nguồn độc lập") if X["cross"] else "Chưa đối chiếu nguồn độc lập"}</td></tr>
<tr><td>Danh sách seat dùng dưới 10%</td><td><b>Cao</b></td>
<td style="text-align:left">Đếm trực tiếp từ báo cáo Kiro</td></tr>
<tr><td>Tỷ lệ Opus đắt gấp {c_['opusVsAutoRatio']}× auto</td><td>Trung bình – cao</td>
<td style="text-align:left">Ước lượng thống kê, R² = {MD['nnls']['r2']:.3f}</td></tr>
<tr><td>Số tiết kiệm của đề nghị A</td><td>Trung bình</td>
<td style="text-align:left">Phụ thuộc mức độ nhân viên thực sự đổi model</td></tr>
<tr><td>Số tiết kiệm của đề nghị B</td><td>Trung bình</td>
<td style="text-align:left">Phụ thuộc điều kiện chưa xác nhận</td></tr>
<tr><td>Dự báo cơ cấu license cả năm</td><td><b>Thấp</b></td>
<td style="text-align:left">Chỉ có 1 tháng gần đủ, lại là tháng onboarding</td></tr>
</tbody></table>
<div class="box warn">
<h4>Hai việc phải làm trước khi trình số $ ra ngoài tài liệu này</h4>
<p>1. Xác nhận bảng giá hiện hành tại <b>kiro.dev/pricing</b> - báo cáo dùng giá PRO_MAX $100
và POWER $200 mỗi tháng.<br>
2. Xác nhận trong <b>Kiro Console</b> rằng gói có thể đặt riêng cho từng nhân viên.</p>
</div>

<h2>6. Bảng ý kiến phê duyệt</h2>
<table>
<thead><tr><th>#</th><th>Đề nghị</th><th>Tiết kiệm/năm</th><th>Đồng ý</th>
<th>Không đồng ý</th><th>Ý kiến khác</th></tr></thead>
<tbody>
<tr><td>A</td><td>Ban hành hướng dẫn đổi model mặc định sang auto</td>
<td>${c_['scenarios'][0]['creditsSavedToAutoPerBasisMonth']/10000*2400:,.0f} –
${c_['scenarios'][2]['creditsSavedToAutoPerBasisMonth']/10000*2400:,.0f}</td>
<td class="chk">☐</td><td class="chk">☐</td><td></td></tr>
<tr><td>B</td><td>Điều chỉnh gói theo mức dùng thực tế (sau khi xác nhận điều kiện)</td>
<td>${S['optionB_rightSize']['safeSavingYearlyUsd']:,}</td>
<td class="chk">☐</td><td class="chk">☐</td><td></td></tr>
<tr><td>C1</td><td>Thu hồi {a[0]['seats']} seat không phát sinh credit</td>
<td>${a[0]['yearlyUsd']:,}</td><td class="chk">☐</td><td class="chk">☐</td><td></td></tr>
<tr><td>C2</td><td>Thu hồi thêm {a[1]['seats']-a[0]['seats']} seat dùng dưới 0,5% hạn mức</td>
<td>${a[1]['yearlyUsd']-a[0]['yearlyUsd']:,}</td>
<td class="chk">☐</td><td class="chk">☐</td><td></td></tr>
<tr><td>C3</td><td>Hoãn thu hồi nhóm dưới 5–10% đến đầu tháng 10</td><td>–</td>
<td class="chk">☐</td><td class="chk">☐</td><td></td></tr>
<tr><td>D</td><td>Xác minh và xử lý {len(X['noncorp'])} seat dùng email ngoài domain</td><td>tối đa ${sum(n['usd'] for n in X['noncorp'])*12:,}</td>
<td class="chk">☐</td><td class="chk">☐</td><td></td></tr>
<tr><td>E</td><td>Xử lý riêng {len(cens)} lượt nhân viên bị khoá (ưu tiên đổi model trước khi
nâng gói)</td><td>–</td><td class="chk">☐</td><td class="chk">☐</td><td></td></tr>
<tr><td>F</td><td>Ban hành chính sách và thông báo trước nếu muốn giám sát mục đích sử dụng</td>
<td>–</td><td class="chk">☐</td><td class="chk">☐</td><td></td></tr>
</tbody></table>

<p><b>Ý kiến chung của người phê duyệt:</b></p>
<div class="blank"></div>

<table class="sig">
<thead><tr><th></th><th>Họ tên</th><th>Chức vụ</th><th>Ngày</th><th>Chữ ký</th></tr></thead>
<tbody>
<tr><td>Người lập báo cáo</td><td></td><td>AWS Solutions Architect</td><td>09/09/2026</td><td></td></tr>
<tr><td>Người rà soát</td><td></td><td></td><td></td><td></td></tr>
<tr><td>Người phê duyệt</td><td></td><td></td><td></td><td></td></tr>
</tbody></table>

<h2>7. Mốc thời gian đề xuất</h2>
<table>
<thead><tr><th>Thời điểm</th><th style="text-align:left">Việc</th></tr></thead>
<tbody>
<tr><td>Tuần 09–15/09</td><td style="text-align:left">Ban hành hướng dẫn đổi model (A).
Xác minh {len(X['noncorp'])} email ngoài domain (D). Xử lý {len(cens)} lượt nhân viên bị khoá (E)</td></tr>
<tr><td>Tuần 09–15/09</td><td style="text-align:left">Kiểm tra Kiro Console: gói có đặt riêng
từng người được không</td></tr>
<tr><td>Tuần 16–30/09</td><td style="text-align:left">Xác nhận với quản lý trực tiếp về
{a[1]['seats']} seat dùng dưới 0,5% (C1, C2)</td></tr>
<tr><td>01–02/10</td><td style="text-align:left">Chốt số liệu trọn tháng 9, so sánh với tháng 8</td></tr>
<tr><td>Đầu tháng 10</td><td style="text-align:left">Trình phương án cơ cấu license cả năm,
dựa trên hai tháng đầy đủ (B, C3)</td></tr>
</tbody></table>
<p><b>Cơ sở của mốc đầu tháng 10:</b> hiện chỉ có một tháng gần đủ dữ liệu, lại là tháng
onboarding với 52/72 người dùng mới. Quyết định cắt giảm hàng loạt dựa trên một tháng như vậy
có rủi ro phải rút lại. Ba việc gấp (A, D, E) <b>không phụ thuộc</b> vào dữ liệu thêm nên
làm ngay.</p>

<p class="note" style="margin-top:30px;padding-top:14px;border-top:1px solid #d5dae1">
Tài liệu kỹ thuật chi tiết, phương pháp tính, và cách tái tạo số liệu: kiro-activity-report.md
· Dashboard kỹ thuật: kiro-activity-report.html · Mọi con số truy được về các file data/*.json.
</p>
</div>

<script src="{CDN}"></script>
<script>
const K = {js};
if (typeof Chart !== "undefined") {{
  Chart.defaults.font.family = "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif";
  Chart.defaults.font.size = 11;
  Chart.defaults.color = "#5a6472";
  new Chart(document.getElementById("k1"), {{
    type:"bar", data:{{labels:K.tier.lbl, datasets:[{{label:"$/tháng", data:K.tier.v,
      backgroundColor:["#4dabf7","#0f2b46"]}}]}},
    options:{{responsive:true,maintainAspectRatio:false,
      plugins:{{legend:{{display:false}},title:{{display:true,
        text:"Chi phí license theo gói ($/tháng)"}}}} }} }});
  new Chart(document.getElementById("k2"), {{
    type:"bar", data:{{labels:K.band.lbl, datasets:[{{label:"Số nhân viên", data:K.band.v,
      backgroundColor:["#d9a520","#1a7f5a","#e8862a","#c0392b","#95a5a6"]}}]}},
    options:{{responsive:true,maintainAspectRatio:false,
      plugins:{{legend:{{display:false}},title:{{display:true,
        text:"Phân bố nhân viên theo mức dùng hạn mức (tháng 8/2026)"}}}} }} }});
  new Chart(document.getElementById("k3"), {{
    type:"doughnut", data:{{labels:K.model.lbl, datasets:[{{data:K.model.v,
      backgroundColor:["#c0392b","#1a7f5a","#4dabf7"], borderColor:"#fff", borderWidth:2}}]}},
    options:{{responsive:true,maintainAspectRatio:false,
      plugins:{{legend:{{position:"bottom"}},title:{{display:true,
        text:"Credit tiêu thụ theo nhóm model (cả kỳ 37 ngày)"}}}} }} }});
}}
</script>
</body></html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out-dir", default="reports")
    ap.add_argument("--account", required=True, help="Account ID chủ bucket")
    ap.add_argument("--bucket", required=True, help="Tên bucket, không có s3://")
    ap.add_argument("--customer", required=True,
                    help="Tên khách hàng để đặt tiêu đề, ví dụ ACME")
    ap.add_argument("--profile", default="", help="AWS CLI profile đã dùng để đọc")
    ap.add_argument("--identity", default="",
                    help="ARN danh tính đã dùng, để ghi vào báo cáo cho truy vết")
    ap.add_argument("--snapshot-utc", default="",
                    help="Mốc snapshot dạng 2026-09-09T10:56Z. Bỏ trống thì suy từ tên "
                         "thư mục snapshot trong metrics JSON")
    ap.add_argument("--tz", type=float, default=float(os.environ.get("KIRO_AUDIT_TZ", "7")))
    ap.add_argument("--org-label", default="bộ phận công nghệ",
                    help="Cách gọi bộ phận đã tự lập báo cáo đối chiếu (nếu có)")
    args = ap.parse_args()

    D = load(args.data_dir)
    ctx = build_ctx(D, args)
    os.makedirs(args.out_dir, exist_ok=True)

    # Dashboard kỹ thuật giữ định dạng số en-US (monospace, đọc bằng mắt kỹ thuật).
    # Bản trình phê duyệt đổi sang định dạng số Việt Nam - chỉ phần tài liệu,
    # phần <script> giữ nguyên vì JS/JSON bắt buộc dấu chấm thập phân.
    p = os.path.join(args.out_dir, "kiro-activity-report.html")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(dash_html(D, ctx))
    print(f"[+] {p}  ({os.path.getsize(p):,} bytes)")

    html = approval_html(D, ctx)
    marker = '<script src='
    i = html.index(marker)
    html = to_vi_numbers(html[:i]) + html[i:]
    p = os.path.join(args.out_dir, "bao-cao-chi-phi-va-muc-dich-su-dung.html")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"[+] {p}  ({os.path.getsize(p):,} bytes)  [số định dạng vi-VN]")

    if not ctx["cross"]:
        print("[i] Không có nguồn đối chiếu độc lập - chạy scenarios.py với --crosscheck "
              "để đưa phần đối chiếu vào báo cáo.")
    print(f"[i] Ví dụ so sánh model được SUY TỪ DỮ LIỆU: "
          f"{ctx['ex_costly']['email']} vs {ctx['ex_cheap']['email']}")


if __name__ == "__main__":
    main()
