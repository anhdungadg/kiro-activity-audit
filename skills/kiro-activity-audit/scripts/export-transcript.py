#!/usr/bin/env python3
"""
Trích transcript + credit usage từ session store local của Kiro CLI.

Session CLI được lưu tại ~/.kiro/sessions/<dirHash>/<sessionId>/messages.jsonl
(hoặc $KIRO_HOME/sessions/... nếu có set KIRO_HOME).

Script này đọc messages.jsonl và xuất:
  - <out>.md    transcript dạng markdown (user / assistant / tool calls)
  - <out>.json  bản raw đã chuẩn hoá, kèm thống kê credit
  - in ra terminal bảng credit theo từng lượt

ĐIỂM ĐÁNG CHÚ Ý: bản ghi `usage_summary` trong session store chứa
**credit tiêu thụ thật của từng lượt** (unit: "credit") — dữ liệu này KHÔNG có
trong S3 prompt-logs. Đây là nguồn duy nhất để biết chi phí ở mức per-turn
trước khi báo cáo CSV hằng ngày của Kiro được sinh ra.

VÍ DỤ:
    # Liệt kê session của thư mục hiện tại
    python3 export-transcript.py --list

    # Xuất session gần nhất của thư mục hiện tại
    python3 export-transcript.py --latest --out transcript/session

    # Xuất theo session id
    python3 export-transcript.py --session-id sess_056c7c8e-... --out transcript/session

    # Chỉ xem thống kê credit, không ghi file
    python3 export-transcript.py --latest --stats-only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Múi giờ hiển thị — đổi bằng biến môi trường KIRO_AUDIT_TZ (số giờ lệch UTC).
#   export KIRO_AUDIT_TZ=7    → GMT+7 (Việt Nam, mặc định)
#   export KIRO_AUDIT_TZ=8    → GMT+8 (Singapore, Malaysia)
#   export KIRO_AUDIT_TZ=0    → UTC
# Người dùng và người đọc report thường cùng múi giờ → hiển thị giờ địa phương.
# ---------------------------------------------------------------------------
TZ_OFFSET = float(os.environ.get("KIRO_AUDIT_TZ", "7"))
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET))
TZ_LABEL = f"GMT{'+' if TZ_OFFSET >= 0 else '-'}{abs(TZ_OFFSET):g}"
from pathlib import Path

TOOL_ICON = "→"


def kiro_home() -> Path:
    return Path(os.environ.get("KIRO_HOME") or (Path.home() / ".kiro"))


def find_sessions() -> list[dict]:
    """Tìm mọi session dir có messages.jsonl, kèm metadata nếu đọc được."""
    root = kiro_home() / "sessions"
    out: list[dict] = []
    if not root.is_dir():
        return out
    for msg in root.glob("*/*/messages.jsonl"):
        sess_dir = msg.parent
        meta: dict = {}
        sj = sess_dir / "session.json"
        if sj.is_file():
            try:
                meta = json.loads(sj.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        out.append({
            "id": sess_dir.name,
            "dirHash": sess_dir.parent.name,
            "path": sess_dir,
            "messages": msg,
            "mtime": msg.stat().st_mtime,
            "bytes": msg.stat().st_size,
            "cwd": meta.get("cwd") or meta.get("workingDirectory") or "",
            "title": meta.get("title") or meta.get("name") or "",
        })
    out.sort(key=lambda s: s["mtime"], reverse=True)
    return out


def pick_session(args) -> dict:
    sessions = find_sessions()
    if not sessions:
        sys.exit(f"[x] Không tìm thấy session nào trong {kiro_home() / 'sessions'}")

    if args.session_id:
        want = args.session_id
        for s in sessions:
            if s["id"] == want or s["id"].endswith(want) or want in s["id"]:
                return s
        sys.exit(f"[x] Không thấy session khớp '{want}'. Chạy --list để xem danh sách.")

    cwd = str(Path.cwd())
    same = [s for s in sessions if s["cwd"] and (s["cwd"] == cwd or cwd.startswith(s["cwd"]))]
    chosen = (same or sessions)[0]
    if not same:
        print(f"[!] Không có session nào khớp thư mục hiện tại — dùng session mới nhất "
              f"({chosen['id']})", file=sys.stderr)
    return chosen


def load_records(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def ts_fmt(raw: str | None) -> str:
    """ISO8601 -> 'dd/mm/YYYY HH:MM:SS' giờ địa phương (TZ_LABEL)."""
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")) \
                       .astimezone(LOCAL_TZ).strftime("%d/%m/%Y %H:%M:%S")
    except ValueError:
        return raw


def build(records: list[dict]) -> dict:
    """Chuẩn hoá thành danh sách turn + thống kê credit."""
    turns: list[dict] = []
    cur: dict | None = None
    credits: list[dict] = []
    tools: dict[str, int] = {}
    started = ended = None

    def new_turn(ts):
        return {"ts": ts, "user": [], "assistant": [], "tools": [],
                "credit": None, "elapsedMs": None, "status": None}

    for rec in records:
        p = rec.get("payload") or {}
        t = p.get("type")
        ts = rec.get("timestamp")
        if started is None and ts:
            started = ts
        if ts:
            ended = ts

        if t == "user":
            if cur is None or cur["user"]:
                cur = new_turn(ts)
                turns.append(cur)
            cur["user"].append(p.get("content") or "")
        elif t == "assistant":
            if cur is None:
                cur = new_turn(ts)
                turns.append(cur)
            c = p.get("content")
            if c:
                cur["assistant"].append(c)
        elif t == "tool_call":
            name = p.get("toolName") or p.get("actionType") or "?"
            tools[name] = tools.get(name, 0) + 1
            if cur is not None:
                cur["tools"].append(name)
        elif t == "usage_summary":
            used = 0.0
            for s in (p.get("promptTurnSummaries") or []):
                if s.get("unit") == "credit":
                    used += float(s.get("usage") or 0)
            entry = {"credit": used, "elapsedMs": p.get("elapsedTime"),
                     "status": p.get("status"), "ts": ts,
                     "usedTools": sorted({tool for s in (p.get("promptTurnSummaries") or [])
                                          for tool in (s.get("usedTools") or [])})}
            credits.append(entry)
            if cur is not None and cur["credit"] is None:
                cur["credit"] = used
                cur["elapsedMs"] = p.get("elapsedTime")
                cur["status"] = p.get("status")

    total = sum(c["credit"] for c in credits)
    return {
        "turns": turns,
        "credits": credits,
        "tools": dict(sorted(tools.items(), key=lambda x: -x[1])),
        "totalCredit": total,
        "turnsWithCredit": len(credits),
        "started": started,
        "ended": ended,
        "counts": {
            "userMessages": sum(1 for t in turns if t["user"]),
            "assistantMessages": sum(len(t["assistant"]) for t in turns),
            "toolCalls": sum(tools.values()),
        },
    }


def write_md(sess: dict, data: dict, out: Path) -> None:
    L: list[str] = []
    L.append(f"# Kiro CLI transcript — `{sess['id']}`\n")
    L.append(f"| | |\n|---|---|")
    L.append(f"| Session ID | `{sess['id']}` |")
    if sess["title"]:
        L.append(f"| Tiêu đề | {sess['title']} |")
    if sess["cwd"]:
        L.append(f"| Thư mục | `{sess['cwd']}` |")
    L.append(f"| Bắt đầu ({TZ_LABEL}) | {ts_fmt(data['started'])} |")
    L.append(f"| Kết thúc ({TZ_LABEL}) | {ts_fmt(data['ended'])} |")
    L.append(f"| Lượt user | {data['counts']['userMessages']} |")
    L.append(f"| Tin nhắn assistant | {data['counts']['assistantMessages']} |")
    L.append(f"| Tool call | {data['counts']['toolCalls']} |")
    L.append(f"| **Credit tiêu thụ** | **{data['totalCredit']:.2f}** "
             f"({data['turnsWithCredit']} lượt có ghi nhận) |")
    L.append(f"| Xuất lúc | {datetime.now(LOCAL_TZ).strftime('%d/%m/%Y %H:%M:%S')} {TZ_LABEL} |\n")

    L.append("> ⚠️ Transcript chứa nguyên văn prompt, phản hồi, và output của tool "
             "(gồm cả đường dẫn file và nội dung code). Không commit lên repo công khai.\n")

    if data["tools"]:
        L.append("## Tool được dùng\n")
        L.append("| Tool | Lần |\n|---|---:|")
        for k, v in data["tools"].items():
            L.append(f"| `{k}` | {v} |")
        L.append("")

    if data["credits"]:
        L.append("## Credit theo lượt\n")
        L.append(f"| # | Thời điểm {TZ_LABEL} | Credit | Thời gian | Trạng thái | Tool |\n"
                 "|---:|---|---:|---:|---|---|")
        for i, c in enumerate(data["credits"], 1):
            secs = f"{c['elapsedMs'] / 1000:.1f}s" if c.get("elapsedMs") else "—"
            tl = ", ".join(f"`{t}`" for t in c["usedTools"][:6]) or "—"
            L.append(f"| {i} | {ts_fmt(c['ts'])} | {c['credit']:.3f} | {secs} | "
                     f"{c.get('status') or '—'} | {tl} |")
        L.append(f"| | **Tổng** | **{data['totalCredit']:.3f}** | | | |\n")

    L.append("---\n\n## Hội thoại\n")
    for i, t in enumerate(data["turns"], 1):
        L.append(f"### Lượt {i} — {ts_fmt(t['ts'])} {TZ_LABEL}")
        if t["credit"] is not None:
            secs = f" · {t['elapsedMs'] / 1000:.1f}s" if t.get("elapsedMs") else ""
            L.append(f"*{t['credit']:.3f} credit{secs}*")
        L.append("")
        for u in t["user"]:
            L.append("**User:**\n")
            L.append("```text")
            L.append(u.rstrip())
            L.append("```\n")
        for a in t["assistant"]:
            L.append("**Assistant:**\n")
            L.append(a.rstrip() + "\n")
        if t["tools"]:
            uniq: list[str] = []
            for x in t["tools"]:
                if x not in uniq:
                    uniq.append(x)
            L.append(f"*{TOOL_ICON} tool: " + ", ".join(f"`{x}`" for x in uniq) + "*\n")

    out.write_text("\n".join(L), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Trích transcript + credit usage từ session store local của Kiro CLI.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--list", action="store_true", help="Liệt kê session rồi thoát")
    ap.add_argument("--latest", action="store_true", help="Dùng session mới nhất của thư mục hiện tại")
    ap.add_argument("--session-id", help="Chỉ định session id (khớp đầy đủ hoặc một phần)")
    ap.add_argument("--out", default="transcript/session", help="Tiền tố file xuất (mặc định transcript/session)")
    ap.add_argument("--stats-only", action="store_true", help="Chỉ in thống kê, không ghi file")
    args = ap.parse_args()

    if args.list:
        rows = find_sessions()
        if not rows:
            sys.exit(f"[x] Không có session nào trong {kiro_home() / 'sessions'}")
        print(f"{len(rows)} session trong {kiro_home() / 'sessions'}:\n")
        for s in rows[:40]:
            when = datetime.fromtimestamp(s["mtime"], timezone.utc).strftime("%Y-%m-%d %H:%M")
            print(f"  {s['id']}")
            print(f"      {when} UTC · {s['bytes'] / 1024:.0f} KB"
                  + (f" · {s['title']}" if s["title"] else "")
                  + (f"\n      cwd: {s['cwd']}" if s["cwd"] else ""))
        return

    if not (args.latest or args.session_id):
        sys.exit("[x] Cần --latest hoặc --session-id (hoặc --list để xem danh sách)")

    sess = pick_session(args)
    records = load_records(sess["messages"])
    data = build(records)

    print(f"[+] Session   : {sess['id']}")
    if sess["cwd"]:
        print(f"    cwd       : {sess['cwd']}")
    print(f"    bản ghi   : {len(records)}")
    print(f"    lượt user : {data['counts']['userMessages']}")
    print(f"    tool call : {data['counts']['toolCalls']}")
    print(f"    khoảng TG : {ts_fmt(data['started'])} → {ts_fmt(data['ended'])} {TZ_LABEL}")
    print(f"    CREDIT    : {data['totalCredit']:.3f} "
          f"({data['turnsWithCredit']} lượt có ghi nhận)")
    if data["credits"]:
        avg = data["totalCredit"] / len(data["credits"])
        mx = max(data["credits"], key=lambda c: c["credit"])
        print(f"    TB/lượt   : {avg:.3f} credit · cao nhất {mx['credit']:.3f}")
    if data["tools"]:
        top = ", ".join(f"{k}×{v}" for k, v in list(data["tools"].items())[:8])
        print(f"    tool      : {top}")

    if args.stats_only:
        return

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_md(sess, data, out.with_suffix(".md"))
    payload = {
        "sessionId": sess["id"],
        "cwd": sess["cwd"],
        "title": sess["title"],
        "started": data["started"],
        "ended": data["ended"],
        "exportedAt": datetime.now(LOCAL_TZ).isoformat(),
        "counts": data["counts"],
        "totalCredit": data["totalCredit"],
        "credits": data["credits"],
        "tools": data["tools"],
        "turns": data["turns"],
    }
    out.with_suffix(".json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[+] Đã ghi {out.with_suffix('.md')} và {out.with_suffix('.json')}")
    print("[!] Transcript chứa nguyên văn prompt và output tool — không commit lên repo công khai.")


if __name__ == "__main__":
    main()
