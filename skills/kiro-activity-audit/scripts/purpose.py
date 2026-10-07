#!/usr/bin/env python3
"""Đánh giá mục đích sử dụng theo từng người: file/dự án nào được mở, câu hỏi nhắc tới gì.

Chỉ là TÍN HIỆU để biết cần trao đổi với ai - KHÔNG đủ làm căn cứ xử lý (xem giới hạn bên dưới).

Tổ chức tự cung cấp pattern - script không hardcode tên công ty nào:
    --work      regex nhận diện việc công ty  (vd: 'acme|\\bacm-|com\\.acme\\.')
    --personal  regex nhận diện việc cá nhân  (mặc định: trading bot, forex, crypto exchange…)

Ví dụ:
    python3 purpose.py --root data/snapshot-XXXX --work 'acme|acm-' --since 2026-09-10 --json data/purpose.json

Giới hạn PHẢI nêu khi báo cáo:
  1. Từ khoá nhiễu ("english", "crypto" xuất hiện trong lệnh/thư viện bình thường) - chỉ kết luận khi
     TÊN DỰ ÁN (thư mục gốc) và nội dung cùng chỉ về một hướng.
  2. Không có file path ≠ bất thường (một số client không gửi ngữ cảnh file).
  3. Chưa có hướng dẫn sử dụng / chưa thông báo thu thập log → không xử lý hồi tố.
"""
import argparse
import csv
import glob
import gzip
import json
import os
import re
from collections import Counter, defaultdict

PERSONAL_DEFAULT = (r"(?i)forex|\bmt[45]\b|metatrader|\.mq[45]\b|expert.?advisor|binance|bybit|okx\b|"
                    r"futures.?bot|trading.?bot|ielts|toeic|vocab")
SKIP_DIRS = {"Users", "home", "src", "main", "java", "com", "vn", "app", "apps", "lib", "libs",
             "components", "features", "services", "utils", "Documents", "Desktop", "Downloads",
             "Library", "tests", "test", "web", "server", "projects", "project", "workspace",
             "workspaces", "code", "repos", "repo", "git", "github", "Development", "dev", "WorkingCopy",
             "work", "data", "api", "packages", "modules"}


def email_map(root):
    m = {}
    for f in glob.glob(os.path.join(root, "user-activity-reports", "**", "*.csv"), recursive=True):
        with open(f, encoding="utf-8", errors="replace") as fh:
            for r in csv.DictReader(fh):
                if r.get("UserId") and r.get("User_Email"):
                    m[r["UserId"].split(".")[-1]] = r["User_Email"]
    return m


def project_of(path: str) -> str:
    """Thư mục gốc có nghĩa đầu tiên - xấp xỉ tên dự án."""
    parts = [p for p in re.split(r"[\\/]", path) if p]
    if path.startswith("/Users/") or path.startswith("/home/"):
        parts = parts[2:]                       # bỏ /Users/<username>
    for p in parts:
        if p not in SKIP_DIRS and not p.startswith("."):
            return p
    return parts[0] if parts else "?"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, help="thư mục snapshot")
    ap.add_argument("--work", required=True, help="regex việc công ty")
    ap.add_argument("--personal", default=PERSONAL_DEFAULT)
    ap.add_argument("--since", help="YYYY-MM-DD - chỉ xét file log từ ngày này (theo đường dẫn log UTC)")
    ap.add_argument("--json")
    a = ap.parse_args()
    WORK, PERS = re.compile(a.work, re.I), re.compile(a.personal)
    em = email_map(a.root)

    files = defaultdict(set)
    opens = defaultdict(Counter)
    projects = defaultdict(Counter)
    stat = defaultdict(lambda: {"prompts": 0, "work": 0, "personal": 0})
    kw = defaultdict(Counter)
    for f in glob.glob(os.path.join(a.root, "prompt-logs", "**", "*.json.gz"), recursive=True):
        if a.since:
            m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", f)
            if m and "-".join(m.groups()) < a.since:
                continue
        try:
            recs = json.load(gzip.open(f)).get("records", [])
        except Exception:
            continue
        for r in recs:
            q = r.get("generateAssistantResponseEventRequest") or r.get("generateCompletionsEventRequest")
            if not q:
                continue
            uid = (q.get("userId") or "").split(".")[-1]
            u = em.get(uid, uid[:8])
            names = []
            p = q.get("prompt")
            if p is not None:
                s = stat[u]
                s["prompts"] += 1
                s["work"] += bool(WORK.search(p))
                hit = PERS.findall(p)
                s["personal"] += bool(hit)
                for h in hit:
                    kw[u][h.lower()] += 1
                names = re.findall(r'<file name="([^"]+)"', p)
            if q.get("fileName"):
                names.append(q["fileName"])
            for n in names:
                files[u].add(n)
                opens[u][n] += 1
                projects[u][project_of(n)] += 1

    out = {}
    for u in sorted(set(stat) | set(files), key=lambda x: -stat[x]["prompts"]):
        s, fs = stat[u], files[u]
        fw = sum(1 for x in fs if WORK.search(x))
        fp = sum(1 for x in fs if PERS.search(x))
        n = max(s["prompts"], 1)
        tot_open = sum(opens[u].values())
        work_open = sum(c for k, c in opens[u].items() if WORK.search(k))
        share = work_open / tot_open if tot_open else None
        # Gắn cờ khi CẢ HAI hướng cùng chỉ về việc cá nhân:
        #   (a) dưới 20% lượt mở file thuộc dự án công ty, VÀ
        #   (b) có tín hiệu cá nhân trong câu hỏi (>0,5%) hoặc trong tên file
        flag = share is not None and share < 0.2 and (s["personal"] / n > 0.005 or fp > 0)
        out[u] = {"prompts": s["prompts"], "workPromptPct": round(s["work"] / n * 100, 1),
                  "personalPromptPct": round(s["personal"] / n * 100, 1),
                  "files": len(fs), "workFiles": fw, "personalFiles": fp,
                  "workOpenSharePct": None if share is None else round(share * 100, 1),
                  "topProjects": projects[u].most_common(6), "personalKeywords": kw[u].most_common(5),
                  "flag": flag, "noFileContext": len(fs) == 0}
    print(f"{'user':34} {'prompt':>6} {'%CV':>5} {'%riêng':>7} {'file':>5} {'%mởCV':>6}  dự án chính")
    for u, v in out.items():
        top = ", ".join(f"{k}:{c}" for k, c in v["topProjects"][:3])
        ws = "-" if v["workOpenSharePct"] is None else f"{v['workOpenSharePct']:.0f}"
        print(f"{u[:34]:34} {v['prompts']:6} {v['workPromptPct']:5.0f} {v['personalPromptPct']:7.1f} "
              f"{v['files']:5} {ws:>6}  {top}{'  ← XEM' if v['flag'] else ''}")
    flagged = [u for u, v in out.items() if v["flag"]]
    print(f"\n[i] {len(flagged)} người cần xem kỹ: {', '.join(flagged) or '-'}")
    print("[i] Không có file path ≠ bất thường. Chỉ dùng để biết cần trao đổi với ai, không xử lý hồi tố.")
    if a.json:
        json.dump(out, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"[+] Ghi {a.json}")


if __name__ == "__main__":
    main()
