#!/usr/bin/env python3
"""
Xuất metrics.json từ snapshot Kiro prompt-logs - dùng cho HTML dashboard.

Account-agnostic: mọi giá trị đều suy ra từ dữ liệu, không hardcode.
Chạy từ TRONG thư mục snapshot (nơi có prompt-logs/ và user-activity-reports/).

VÍ DỤ:
    cd data/snapshot-20260831-1414Z
    python3 ../../scripts/export_json.py
    python3 ../../scripts/export_json.py --out ../metrics.json
    python3 ../../scripts/export_json.py --bucket kiro-activity-123456789012-us-east-1-xx
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Múi giờ hiển thị - đổi bằng biến môi trường KIRO_AUDIT_TZ (số giờ lệch UTC).
#   export KIRO_AUDIT_TZ=7    → GMT+7 (Việt Nam, mặc định)
#   export KIRO_AUDIT_TZ=8    → GMT+8 (Singapore, Malaysia)
#   export KIRO_AUDIT_TZ=0    → UTC
# Người dùng và người đọc report thường cùng múi giờ → hiển thị giờ địa phương.
# ---------------------------------------------------------------------------
TZ_OFFSET = float(os.environ.get("KIRO_AUDIT_TZ", "7"))
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET))
TZ_LABEL = f"GMT{'+' if TZ_OFFSET >= 0 else '-'}{abs(TZ_OFFSET):g}"


def vn(iso: str) -> str:
    """ISO8601 UTC -> 'dd/mm HH:MM:SS' giờ địa phương (TZ_LABEL)."""
    return datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone(LOCAL_TZ).strftime('%d/%m %H:%M:%S')

# --------------------------------------------------------------------------- #
# Pattern quét dữ liệu nhạy cảm - 16 lớp
# --------------------------------------------------------------------------- #
SENSITIVE_PATTERNS: dict[str, str] = {
    "AWS access key": r"AKIA[0-9A-Z]{16}",
    "AWS temp key": r"ASIA[0-9A-Z]{16}",
    "Private key": r"BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY",
    "Google API key": r"AIza[0-9A-Za-z_\-]{35}",
    "Firebase/GCP service account": r"\"private_key_id\"|\"client_email\".*iam\.gserviceaccount",
    "JWT token": r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
    "Slack/GitHub token": r"xox[baprs]-[0-9A-Za-z\-]{10,}|gh[pousr]_[0-9A-Za-z]{36}",
    "Secret assignment": r"(?i)(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|credential)\s*[:=]\s*[\"']?[A-Za-z0-9+/=_\-]{16,}",
    "Connection string": r"(?i)(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|amqp)://[^\s\"']{8,}",
    "AWS ARN": r"arn:aws[\w-]*:[^\s\"']+",
    "S3 URI": r"s3://[a-z0-9.\-/]+",
    "IPv4 address": r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b",
    "AWS account ID": r"\b\d{12}\b",
    "ECR registry host": r"[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com",
    "Credential VAR name": r"\b[A-Z][A-Z0-9_]*(?:ACCESS_KEY_ID|SECRET_ACCESS_KEY|PASSWORD|TOKEN|SECRET|API_KEY|CREDENTIALS)\b",
    "K8s internal DNS": r"[a-z0-9.\-{}]+\.svc\.cluster\.local",
}

# 9 lớp đầu là secret VALUE - có hit nghĩa là sự cố thật
CRITICAL = list(SENSITIVE_PATTERNS)[:9]

# Từ khoá stack công nghệ - mở rộng tuỳ dự án
TECH_TERMS = [
    "cassandra", "medusa", "backup", "purge", "kubernetes", "sidecar", "registry",
    "argocd", "namespace", "CronJob", "ECR", "helm", "Taskfile", "karpenter",
    "values.yaml", "kubectl", "nodepool", "StatefulSet", "terraform", "temporal",
    "firestore", "cloudinary", "nextjs", "typescript", "spring",
]


def load_records(root: str = ".") -> tuple[list[dict], list[dict], list[str], int]:
    """Đọc mọi *.json.gz và file validation. Trả về (chat, inline, validation, sốFile)."""
    chat: list[dict] = []
    inline: list[dict] = []
    validation: list[str] = []
    files = 0

    for path in sorted(glob.glob(os.path.join(root, "**", "*"), recursive=True)):
        if not os.path.isfile(path):
            continue
        if path.endswith(".json.gz"):
            files += 1
            try:
                with gzip.open(path) as fh:
                    payload = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[!] Bỏ qua {path}: {exc}", file=sys.stderr)
                continue
            for rec in payload.get("records", []):
                if "generateAssistantResponseEventRequest" in rec:
                    chat.append(rec)
                elif "generateCompletionsEventRequest" in rec:
                    inline.append(rec)
        elif "KiroLogs" in path and not path.endswith((".py", ".json", ".txt", ".md", ".html")):
            files += 1
            validation.append(open(path, encoding="utf-8", errors="replace").read())

    return chat, inline, validation, files


def build_blob(chat: list[dict], inline: list[dict]) -> str:
    """Gộp toàn bộ nội dung prompt + code context + completion để quét."""
    parts: list[str] = []
    for rec in chat:
        parts.append(rec["generateAssistantResponseEventRequest"].get("prompt") or "")
    for rec in inline:
        req = rec["generateCompletionsEventRequest"]
        res = rec.get("generateCompletionsEventResponse") or {}
        parts.append(req.get("leftContext") or "")
        parts.append(req.get("rightContext") or "")
        for comp in (res.get("completions") or []):
            parts.append(comp.get("content") if isinstance(comp, dict) else str(comp))
    return "\n".join(parts)


_REDACT = [
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{35}|glpat-[\w\-]{10,}"
               r"|eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]*(?:\.[A-Za-z0-9_\-]*)?"   # JWT, kể cả bị cắt cụt
               r"|xox[baprs]-[0-9A-Za-z\-]{10,}|gh[pousr]_[0-9A-Za-z]{36}|X-Amz-Signature=[0-9a-f]{64}"),
    # key=value với tên khoá nhạy cảm → che phần value
    re.compile(r"(?i)((?:password|passwd|pwd|secret|token|api[_-]?key|apikey|credential|_authToken|[\w.]*\.key)"
               r"[\w.\-]*\\?[\"']?\s*[:=]\s*\\?[\"']?)([A-Za-z0-9+/=_\-.]{12,})"),
    # user:password@host trong connection string
    re.compile(r"(://[^:/@\s\"]+:)([^@\s\"]+)(@)"),
]


def redact(s: str) -> str:
    s = _REDACT[0].sub(lambda m: _mask(m.group(0)), s)
    s = _REDACT[1].sub(lambda m: m.group(1) + "<REDACTED>", s)
    return _REDACT[2].sub(lambda m: m.group(1) + "<REDACTED>" + m.group(3), s)


def _mask(v: str) -> str:
    """Che giá trị secret: chỉ giữ 4 ký tự đầu + độ dài. Không bao giờ lưu giá trị thật."""
    return f"{v[:4]}…({len(v)})"


def scan_sensitive(blob: str) -> dict:
    found: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    clean: list[str] = []
    for name, pat in SENSITIVE_PATTERNS.items():
        hits = re.findall(pat, blob)
        vals = sorted({h if isinstance(h, str) else "|".join(x for x in h if x) for h in hits})
        if vals:
            counts[name] = len(vals)
            # Lớp CRITICAL là giá trị secret thật → che trước khi ghi ra metrics.json / dashboard
            found[name] = [_mask(v) for v in vals[:20]] if name in CRITICAL else vals[:20]
        else:
            clean.append(name)
    critical_hits = {k: v for k, v in found.items() if k in CRITICAL}
    return {"found": found, "distinct": counts, "clean": clean, "criticalHits": critical_hits}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="Thư mục snapshot (mặc định thư mục hiện tại)")
    ap.add_argument("--out", default="metrics.json", help="File xuất (mặc định metrics.json)")
    ap.add_argument("--bucket", help="Tên bucket, chỉ để ghi vào metadata")
    ap.add_argument("--snapshot", help="Nhãn thời điểm snapshot (mặc định: giờ hiện tại UTC)")
    args = ap.parse_args()

    chat, inline, validation, files = load_records(args.root)
    if not chat and not inline:
        sys.exit(f"[x] Không tìm thấy record nào trong {os.path.abspath(args.root)}\n"
                 f"    Bạn có đang ở trong thư mục snapshot (chứa prompt-logs/) không?")

    # ---- account & bucket suy ra từ đường dẫn ----
    account = ""
    for path in glob.glob(os.path.join(args.root, "**", "AWSLogs", "*"), recursive=True):
        base = os.path.basename(path)
        if re.fullmatch(r"\d{12}", base):
            account = base
            break

    # ---- timestamps ----
    stamps: list[tuple[str, str]] = []
    for rec in chat:
        stamps.append((rec["generateAssistantResponseEventRequest"]["timeStamp"], "chat"))
    for rec in inline:
        stamps.append((rec["generateCompletionsEventRequest"]["timeStamp"], "inline"))
    for text in validation:
        m = re.search(r"Timestamp: (\S+)", text)
        if m:
            stamps.append((m.group(1), "validation"))

    per_minute: dict[str, dict[str, int]] = defaultdict(
        lambda: {"chat": 0, "inline": 0, "validation": 0})
    per_hour: Counter = Counter()
    for ts, kind in stamps:
        local = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(LOCAL_TZ)
        per_minute[local.strftime("%d/%m %H:%M")][kind] += 1
        if kind != "validation":
            per_hour[local.strftime("%d/%m %Hh")] += 1

    # ---- per-user ----
    users: dict[str, dict] = defaultdict(lambda: {
        "chat": 0, "inline": 0, "prompts": 0, "models": Counter(),
        "first": "z", "last": "", "files": Counter()})

    for rec in chat:
        q = rec["generateAssistantResponseEventRequest"]
        u = users[q["userId"]]
        u["chat"] += 1
        u["models"][q.get("modelId")] += 1
        u["first"] = min(u["first"], q["timeStamp"])
        u["last"] = max(u["last"], q["timeStamp"])
        prompt = q.get("prompt") or ""
        if prompt:
            u["prompts"] += 1
        for block in re.findall(r"<OPEN-EDITOR-FILES>(.*?)</OPEN-EDITOR-FILES>", prompt, re.S):
            for fn in re.findall(r'<file name="([^"]+)"', block):
                u["files"][fn] += 1

    for rec in inline:
        q = rec["generateCompletionsEventRequest"]
        u = users[q["userId"]]
        u["inline"] += 1
        u["first"] = min(u["first"], q["timeStamp"])
        u["last"] = max(u["last"], q["timeStamp"])

    identity_store = ""
    for uid in users:
        if "." in uid:
            identity_store = uid.split(".", 1)[0]
            break

    # ---- editor files toàn cục ----
    open_files: Counter = Counter()
    active_files: Counter = Counter()
    for rec in chat:
        prompt = rec["generateAssistantResponseEventRequest"].get("prompt") or ""
        for block in re.findall(r"<OPEN-EDITOR-FILES>(.*?)</OPEN-EDITOR-FILES>", prompt, re.S):
            for fn in re.findall(r'<file name="([^"]+)"', block):
                open_files[fn] += 1
        for block in re.findall(r"<ACTIVE-EDITOR-FILE>(.*?)</ACTIVE-EDITOR-FILE>", prompt, re.S):
            for fn in re.findall(r'<file name="([^"]+)"', block):
                active_files[fn] += 1

    # ---- chat flags ----
    plens = [len(r["generateAssistantResponseEventRequest"].get("prompt") or "") for r in chat]
    nonempty = [n for n in plens if n] or [0]
    def _resp(rec: dict) -> dict:
        return rec.get("generateAssistantResponseEventResponse") or {}

    empty_resp = sum(1 for r in chat if not _resp(r).get("assistantResponse"))
    null_conv = sum(1 for r in chat
                    if (_resp(r).get("messageMetadata") or {}).get("conversationId") is None)
    no_meta = sum(1 for r in chat if "messageMetadata" not in _resp(r))

    # ---- inline context ----
    lc = [len(r["generateCompletionsEventRequest"].get("leftContext") or "") for r in inline] or [0]
    rc = [len(r["generateCompletionsEventRequest"].get("rightContext") or "") for r in inline] or [0]
    ncomp = Counter(len((r.get("generateCompletionsEventResponse") or {}).get("completions") or [])
                    for r in inline)

    # ---- prompt riêng biệt ----
    seen: set[str] = set()
    topics: list[dict] = []
    for rec in sorted(chat, key=lambda r: r["generateAssistantResponseEventRequest"]["timeStamp"]):
        q = rec["generateAssistantResponseEventRequest"]
        text = (q.get("prompt") or "").split("<EnvironmentContext>")[0].strip()
        if not text or text[:80] in seen:
            continue
        seen.add(text[:80])
        topics.append({"ts": q["timeStamp"], "tsLocal": vn(q["timeStamp"]), "userId": q["userId"],
                       "model": q.get("modelId"), "promptLen": len(q["prompt"]),
                       "text": text[:400]})

    blob = build_blob(chat, inline)
    keywords = {t: len(re.findall(re.escape(t), blob, re.I)) for t in TECH_TERMS}

    out = {
        "meta": {
            "snapshot": args.snapshot or datetime.now(timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "snapshotLocal": datetime.now(LOCAL_TZ).strftime("%d/%m/%Y %H:%M") +  " " + TZ_LABEL,
            "timezone": f"{TZ_LABEL} - mọi mốc thời gian trong file này hiển thị theo giờ địa phương",
            "sourceAccount": account,
            "bucket": args.bucket or "",
            "identityStoreId": identity_store,
            "logFiles": files,
        },
        "totals": {
            "chat": len(chat), "inline": len(inline), "validation": len(validation),
            "requests": len(chat) + len(inline),
            "records": len(chat) + len(inline) + len(validation),
        },
        "users": {
            uid: {
                "chat": u["chat"], "inline": u["inline"],
                "total": u["chat"] + u["inline"], "prompts": u["prompts"],
                "from": vn(u["first"]), "to": vn(u["last"]),
                "models": dict(u["models"]), "files": len(u["files"]),
                "topFiles": dict(u["files"].most_common(15)),
            } for uid, u in users.items()
        },
        "userCount": len(users),
        "timeRange": [min(s for s, _ in stamps), max(s for s, _ in stamps)] if stamps else [],
        "timeRangeLocal": [vn(min(s for s, _ in stamps)), vn(max(s for s, _ in stamps))] if stamps else [],
        "models": dict(Counter(r["generateAssistantResponseEventRequest"].get("modelId")
                               for r in chat)),
        "perMinute": {k: per_minute[k] for k in sorted(per_minute)},
        "perHour": dict(sorted(per_hour.items())),
        "chatFlags": {
            "emptyPrompt": plens.count(0),
            "nonEmptyPrompt": len([n for n in plens if n]),
            "emptyResponse": empty_resp,
            "nullConversationId": null_conv,
            "noMessageMetadata": no_meta,
            "codeRefs": sum(len(_resp(r).get("codeReferenceEvents") or []) for r in chat),
            "webLinks": sum(len(_resp(r).get("supplementaryWebLinksEvent") or []) for r in chat),
            "promptLen": {"min": min(nonempty), "max": max(nonempty),
                          "avg": sum(nonempty) // len(nonempty)},
        },
        "inlineFiles": dict(Counter(r["generateCompletionsEventRequest"].get("fileName")
                                    for r in inline)),
        "context": {
            "leftMin": min(lc), "leftMax": max(lc),
            "leftAvg": sum(lc) // len(lc), "leftTotal": sum(lc),
            "rightMin": min(rc), "rightMax": max(rc),
            "rightAvg": sum(rc) // len(rc), "rightTotal": sum(rc),
        },
        "completionsPerReq": {str(k): v for k, v in sorted(ncomp.items())},
        "openFiles": dict(open_files.most_common()),
        "activeFiles": dict(active_files.most_common()),
        "keywords": {k: v for k, v in sorted(keywords.items(), key=lambda x: -x[1]) if v},
        "topics": topics,
        "contentChars": len(blob),
        "sensitive": scan_sensitive(blob),
    }

    # Che mọi giá trị secret xuất hiện ở BẤT KỲ đâu trong output (kể cả trích đoạn prompt),
    # vì metrics.json được build-html-data.py nhúng vào dashboard.
    text = redact(json.dumps(out, indent=2, ensure_ascii=False))
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text)

    print(f"[+] Ghi {args.out} ({os.path.getsize(args.out):,} bytes)")
    print(f"    account   : {account or '(không suy ra được)'}")
    print(f"    identStore: {identity_store or '(không có)'}")
    print(f"    records   : {out['totals']['records']} "
          f"(chat {len(chat)} · inline {len(inline)} · validation {len(validation)})")
    print(f"    user      : {len(users)}")
    print(f"    nội dung  : {len(blob):,} ký tự")
    if stamps:
        print(f"    thời gian : {vn(min(s for s, _ in stamps))} → "
              f"{vn(max(s for s, _ in stamps))} ({TZ_LABEL})")
    crit = out["sensitive"]["criticalHits"]
    if crit:
        print(f"    ⚠️  SECRET  : {len(crit)} lớp CÓ HIT - {', '.join(crit)}")
        print("        → kiểm tra ngay, đây có thể là sự cố lộ credential")
    else:
        print(f"    secret    : 0 hit trên {len(CRITICAL)} lớp nguy hiểm ✓")


if __name__ == "__main__":
    main()
