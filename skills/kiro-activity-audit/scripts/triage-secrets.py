#!/usr/bin/env python3
"""Phân loại các hit secret mà export_json.py báo — gắn người dùng, thời điểm, nguồn, mức độ.

KHÔNG in / KHÔNG lưu giá trị secret thật: mọi giá trị đều được che (4 ký tự đầu + độ dài).
Kết quả ghi ra data/secret-triage.json để đưa vào báo cáo.

    cd data/snapshot-XXXX && python3 ../../scripts/triage-secrets.py --out ../secret-triage.json
"""
import argparse
import base64
import csv
import glob
import gzip
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=int(os.environ.get("KIRO_AUDIT_TZ", "7"))))

PATS = {
    "AWS access key": r"AKIA[0-9A-Z]{16}",
    "AWS temp key": r"ASIA[0-9A-Z]{16}",
    "Private key": r"BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY",
    "Google API key": r"AIza[0-9A-Za-z_\-]{35}",
    "JWT token": r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
    "Slack/GitHub token": r"xox[baprs]-[0-9A-Za-z\-]{10,}|gh[pousr]_[0-9A-Za-z]{36}",
    "Secret assignment": r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|apikey|credential)\s*[:=]\s*[\"']?([A-Za-z0-9+/=_\-]{16,})",
    "Connection string": r"(?i)(mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|amqp)://([^\s\"']{8,})",
    "npm authToken": r"_authToken\s*=\s*([^\s\"']{12,})",
    "Presigned URL": r"X-Amz-Signature=[0-9a-f]{64}",
}
AWS_SECRET_NEAR = re.compile(r"(?i)(?:secret[_-]?access[_-]?key|secret[_-]?key|[\w.]*\.key|_key)[\"'\s]*[:=][\"'\s]*[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])")
PLACEHOLDER = re.compile(r"(?i)(x{6,}|\*{4,}|example|placeholder|your[_-]|changeme|<[^>]+>|\$\{|dummy|test123|sample)")


def mask(v: str) -> str:
    return f"{v[:4]}…({len(v)})"


# Che mọi thứ trông giống secret trong đoạn ngữ cảnh — kể cả secret KHÁC nằm cạnh secret được match
_CTX_SCRUB = [
    re.compile(r"((?i:password|passwd|pwd|secret|token|api[_-]?key|apikey|credential|clientid|client_id|appkey)[\w.\-]*\s*[:=]\s*[\"']?)([^\s\"'&,;}]{6,})"),
    re.compile(r"(://[^:/@\s]*:)([^@\s]+)(@)"),
    re.compile(r"()((?:AKIA|ASIA)[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{35}|eyJ[A-Za-z0-9_\-]{10,}(?:\.[A-Za-z0-9_\-]*){0,2}|glpat-[\w\-]{10,}|[A-Za-z0-9+/]{24,}={0,2})"),
]


def scrub(ctx: str) -> str:
    for rx in _CTX_SCRUB:
        ctx = rx.sub(lambda m: m.group(1) + "<SECRET>" + (m.group(3) if m.lastindex and m.lastindex >= 3 else ""), ctx)
    return ctx


def aws_account_from_key(kid: str) -> str:
    """Account ID mã hoá trong access key ID (AKIA/ASIA…). Thuật toán công khai."""
    try:
        b = base64.b32decode(kid[4:] + "=" * (-len(kid[4:]) % 8))
        z = int.from_bytes(b[:6], "big")
        return f"{(z & 0x7FFFFFFFFF80) >> 7:012d}"
    except Exception:
        return "?"


def jwt_claims(tok: str) -> dict:
    try:
        p = tok.split(".")[1]
        c = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:
        return {}
    out = {k: c[k] for k in ("iss", "azp", "role", "typ") if k in c}
    for k in ("exp", "iat"):
        if isinstance(c.get(k), (int, float)):
            out[k] = datetime.fromtimestamp(c[k], TZ).strftime("%d/%m/%Y %H:%M")
    if isinstance(c.get("exp"), (int, float)):
        out["expired"] = c["exp"] < datetime.now(timezone.utc).timestamp()
    return out


def email_map(root: str) -> dict:
    m = {}
    for f in glob.glob(os.path.join(root, "user-activity-reports", "**", "*.csv"), recursive=True):
        with open(f, encoding="utf-8", errors="replace") as fh:
            for r in csv.DictReader(fh):
                uid, em = r.get("UserId"), r.get("User_Email")
                if uid and em:
                    m[uid] = em
                    m[uid.split(".")[-1]] = em
    return m


def texts(rec: dict):
    """(nguồn, văn bản) của một record."""
    if "generateAssistantResponseEventRequest" in rec:
        q = rec["generateAssistantResponseEventRequest"]
        p = q.get("prompt") or ""
        # Kiro nhúng file đang mở vào prompt — tách để biết secret do người dán hay do IDE tự đính kèm
        auto = "".join(re.findall(r"<(?:OPEN-EDITOR-FILES|ACTIVE-EDITOR-FILE|EnvironmentContext)[^>]*>.*?</(?:OPEN-EDITOR-FILES|ACTIVE-EDITOR-FILE|EnvironmentContext)>", p, re.S))
        typed = p
        for blk in re.findall(r"<(?:OPEN-EDITOR-FILES|ACTIVE-EDITOR-FILE|EnvironmentContext)[^>]*>.*?</(?:OPEN-EDITOR-FILES|ACTIVE-EDITOR-FILE|EnvironmentContext)>", p, re.S):
            typed = typed.replace(blk, "")
        yield q, "chat-gõ/dán", typed
        if auto:
            yield q, "file-tự-đính-kèm", auto
    elif "generateCompletionsEventRequest" in rec:
        q = rec["generateCompletionsEventRequest"]
        yield q, "inline-file-đang-sửa", (q.get("leftContext") or "") + "\n" + (q.get("rightContext") or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--out", default="../secret-triage.json")
    ap.add_argument("--since", help="chỉ xét record từ ngày này (YYYY-MM-DD, GMT+7)")
    a = ap.parse_args()

    em = email_map(a.root)
    hits = defaultdict(lambda: {"users": set(), "sources": set(), "first": None, "last": None, "count": 0})
    files = sorted(glob.glob(os.path.join(a.root, "prompt-logs", "**", "*.json.gz"), recursive=True))
    for path in files:
        try:
            recs = json.load(gzip.open(path)).get("records", [])
        except Exception:
            continue
        for rec in recs:
            for q, src, t in texts(rec):
                if not t:
                    continue
                for cls, pat in PATS.items():
                    for m in re.finditer(pat, t):
                        full = m.group(0)
                        val = m.group(m.lastindex) if m.lastindex else full
                        ctx = t[max(0, m.start() - 160): m.end() + 160]
                        if PLACEHOLDER.search(val):
                            continue
                        uid = q.get("userId") or ""
                        user = em.get(uid) or em.get(uid.split(".")[-1]) or uid.split(".")[-1][:8]
                        ts = q.get("timeStamp") or rec.get("eventTimestamp") or ""
                        if not ts:
                            mm = re.search(r"_(\d{12})_", os.path.basename(path))
                            ts = datetime.strptime(mm.group(1), "%Y%m%d%H%M").replace(tzinfo=timezone.utc).isoformat() if mm else ""
                        key = (cls, val)
                        h = hits[key]
                        h["count"] += 1
                        h["users"].add(user.split("@")[0] + "@")
                        h["sources"].add(src)
                        h["first"] = min(filter(None, [h["first"], ts])) if ts else h["first"]
                        h["last"] = max(filter(None, [h["last"], ts])) if ts else h["last"]
                        if "detail" not in h:
                            d = {}
                            if cls in ("AWS access key", "AWS temp key"):
                                d["awsAccount"] = aws_account_from_key(val)
                                d["secretKeyNearby"] = bool(AWS_SECRET_NEAR.search(t[max(0, m.start() - 2500): m.end() + 2500]))
                                d["kind"] = "long-term (IAM user)" if cls == "AWS access key" else "tạm thời (STS, tự hết hạn ≤36h)"
                            elif cls == "JWT token":
                                d.update(jwt_claims(val))
                            elif cls == "Secret assignment":
                                d["keyName"] = m.group(1).lower()
                                d["near"] = scrub(re.sub(re.escape(val), "<SECRET>", ctx[100:260])).replace("\n", " ")[:140]
                            elif cls == "Connection string":
                                rest = m.group(2)
                                d["scheme"] = m.group(1).lower()
                                d["hasPassword"] = bool(re.match(r"[^/@]*:[^/@]+@", rest))
                                d["host"] = re.sub(r"^[^@]*@", "", rest).split("/")[0].split("?")[0]
                            elif cls == "Presigned URL":
                                mx = re.search(r"X-Amz-Date=(\d{8}T\d{6}Z).*?X-Amz-Expires=(\d+)|X-Amz-Expires=(\d+).*?X-Amz-Date=(\d{8}T\d{6}Z)", ctx)
                                if mx:
                                    dt = mx.group(1) or mx.group(4)
                                    ex = int(mx.group(2) or mx.group(3))
                                    t0 = datetime.strptime(dt, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                                    d["expires"] = (t0 + timedelta(seconds=ex)).astimezone(TZ).strftime("%d/%m/%Y %H:%M")
                                    d["expired"] = t0 + timedelta(seconds=ex) < datetime.now(timezone.utc)
                                bk = re.search(r"https://([a-z0-9.\-]+)\.s3[.\-]", ctx)
                                d["bucket"] = bk.group(1) if bk else "?"
                            h["detail"] = d

    out = []
    for (cls, val), h in hits.items():
        loc = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ).strftime("%d/%m %H:%M") if s else ""
        if a.since and h["last"] and loc(h["last"]) and datetime.fromisoformat(h["last"].replace("Z", "+00:00")).astimezone(TZ).date().isoformat() < a.since:
            continue
        out.append({"class": cls, "value": mask(val), "count": h["count"], "users": sorted(h["users"]),
                    "sources": sorted(h["sources"]), "first": loc(h["first"]), "last": loc(h["last"]),
                    **h.get("detail", {})})
    out.sort(key=lambda x: (list(PATS).index(x["class"]), x["first"]))
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump({"generated": datetime.now(TZ).isoformat(timespec="seconds"), "files": len(files),
                   "hits": out}, fh, ensure_ascii=False, indent=1)

    print(f"[+] {len(files)} file · {len(out)} giá trị secret khác nhau → {a.out}")
    for x in out:
        extra = {k: v for k, v in x.items() if k not in ("class", "value", "count", "users", "sources", "first", "last")}
        print(f"  {x['class']:18} {x['value']:14} ×{x['count']:<4} {','.join(x['users']):22} "
              f"{x['first']}→{x['last']}  [{','.join(x['sources'])}]  {json.dumps(extra, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
