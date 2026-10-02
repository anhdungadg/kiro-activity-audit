"""MCP server: audit log Kiro Enterprise — CHỈ ĐỌC trên AWS, mọi output đã che secret.

Tool:
  audit_check_access   — xác minh profile + khảo sát bucket (biến thể A/B, có reports/ không)
  audit_sync_start     — job nền: snapshot mới, sync delta (CSV + prompt-logs theo ngày)
  audit_scan_start     — job nền: metrics.json + phân loại secret (+ mục đích sử dụng nếu có pattern)
  audit_job_status     — trạng thái job nền
  audit_cost           — chi phí, người bị chặn, 3 phương án (chạy ngay, ~1 phút)
  audit_secret_summary — tóm tắt secret theo mức độ (đọc kết quả scan)
  audit_purpose_summary— người cần trao đổi về mục đích sử dụng (đọc kết quả scan)
  audit_build_reports  — sinh 2 báo cáo HTML + kiểm tra

Lệnh AWS duy nhất được gọi: `aws sts get-caller-identity`, `aws s3 ls`, và (trong sync-snapshot.sh)
`aws s3 sync|cp` từ bucket XUỐNG máy. Không có lệnh ghi nào lên AWS.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .redact import redact

PKG = Path(__file__).resolve().parent
SCRIPTS = Path(os.environ.get("KIRO_AUDIT_SCRIPTS") or PKG / "scripts")
if not SCRIPTS.is_dir():  # chạy từ source checkout (chưa build wheel)
    SCRIPTS = PKG.parents[1] / "skills" / "kiro-activity-audit" / "scripts"
PY = sys.executable

BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")
PROFILE_RE = re.compile(r"^[\w.\-+@]{1,128}$")
RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
LOCAL = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

mcp = FastMCP("kiro-audit", instructions=(
    "Audit Kiro Enterprise activity logs. Read-only on AWS. Never ask for or reveal secret values; "
    "all tool output is already redacted. Order: audit_check_access → audit_sync_start → "
    "audit_job_status (poll) → audit_cost → audit_scan_start → audit_secret_summary → audit_build_reports. "
    "Report urgent items first: critical secrets, then users already blocked by quota, then cost."))


# ------------------------------------------------------------------ helpers
def _check(bucket: str | None = None, profile: str | None = None) -> None:
    if bucket is not None and not BUCKET_RE.match(bucket):
        raise ValueError(f"Tên bucket không hợp lệ: {bucket!r} (không kèm s3://)")
    if profile is not None and not PROFILE_RE.match(profile):
        raise ValueError(f"Tên profile không hợp lệ: {profile!r}")


def _account(bucket: str) -> str:
    m = re.search(r"\d{12}", bucket)
    if not m:
        raise ValueError("Không suy ra được account 12 số từ tên bucket")
    return m.group(0)


def _workdir(workdir: str | None, bucket: str | None = None) -> Path:
    if workdir:
        p = Path(workdir).expanduser()
    elif bucket:
        p = Path.cwd() / f"kiro-audit-{_account(bucket)}"
    else:
        raise ValueError("Cần workdir")
    return p.resolve()


def _latest_snapshot(wd: Path) -> Path:
    f = wd / "data" / ".latest-snapshot"
    if f.is_file():
        p = wd / f.read_text().strip()
        if p.is_dir():
            return p
    snaps = sorted((wd / "data").glob("snapshot-*"))
    if not snaps:
        raise FileNotFoundError(f"Chưa có snapshot trong {wd}/data — chạy audit_sync_start trước")
    return snaps[-1]


def _run(args: list[str], cwd: Path | None = None, timeout: int = 900, env: dict | None = None) -> dict:
    t0 = time.time()
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, **(env or {})})
        return {"ok": r.returncode == 0, "code": r.returncode, "seconds": round(time.time() - t0, 1),
                "stdout": redact(r.stdout[-6000:]), "stderr": redact(r.stderr[-3000:])}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": None, "seconds": timeout, "stdout": "", "stderr": f"timeout sau {timeout}s"}


def _load(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def _jobs_dir(wd: Path) -> Path:
    d = wd / "data" / ".jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _start_job(wd: Path, kind: str, cmd: list[str], env: dict, cwd: Path) -> dict:
    jid = f"{kind}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    jd = _jobs_dir(wd)
    log = jd / f"{jid}.log"
    with open(log, "w") as fh:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                             env={**os.environ, **env}, start_new_session=True)
    meta = {"id": jid, "kind": kind, "pid": p.pid, "started": time.time(), "log": str(log), "workdir": str(wd)}
    (jd / f"{jid}.json").write_text(json.dumps(meta))
    return meta


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # zombie của chính server → đã xong
        r = os.waitpid(pid, os.WNOHANG)
        return r == (0, 0)
    except ChildProcessError:
        return True


# ------------------------------------------------------------------ tools
@mcp.tool(annotations=RO)
def audit_check_access(bucket: str, profile: str) -> dict:
    """Xác minh AWS profile đọc được bucket log Kiro và khảo sát cấu trúc (không đệ quy, nhanh).

    Trả về: danh tính gọi (cảnh báo nếu là role Admin), các prefix cấp đầu, biến thể A (có prompt-logs)
    hay B (chỉ CSV), có prefix reports/ (nguồn đối chiếu độc lập) không, số file validation.
    """
    _check(bucket, profile)
    acct = _account(bucket)
    who = _run(["aws", "sts", "get-caller-identity", "--profile", profile, "--output", "json"], timeout=60)
    ident = {}
    if who["ok"]:
        ident = json.loads(who["stdout"] or "{}")
    top = _run(["aws", "s3", "ls", f"s3://{bucket}/", "--profile", profile], timeout=120)
    prefixes = re.findall(r"PRE (\S+)/", top["stdout"])
    out: dict[str, Any] = {
        "account": acct, "bucket": bucket, "callerArn": ident.get("Arn"), "callerAccount": ident.get("Account"),
        "canList": top["ok"], "topPrefixes": prefixes,
        "variant": "A (có prompt-logs)" if "prompt-logs" in prefixes else "B (chỉ CSV)",
        "hasReportsPrefix": "reports" in prefixes,
    }
    if not top["ok"]:
        out["error"] = top["stderr"][-800:]
    arn = (ident.get("Arn") or "").lower()
    if re.search(r"admin|fullaccess|poweruser", arn):
        out["warning"] = "Đang dùng role có quyền ghi (Admin). Nên dùng profile ReadOnly cho audit."
    if "prompt-logs" in prefixes:
        v = _run(["aws", "s3", "ls", f"s3://{bucket}/prompt-logs/AWSLogs/{acct}/KiroLogs/", "--profile", profile],
                 timeout=120)
        out["promptLogApis"] = re.findall(r"PRE (\S+)/", v["stdout"])
        out["validationFiles"] = len([l for l in v["stdout"].splitlines() if not l.strip().startswith("PRE")])
    return out


@mcp.tool(annotations=LOCAL)
def audit_sync_start(bucket: str, profile: str, workdir: str | None = None, csv_only: bool = False,
                     jobs: int = 6, days: int = 30, region: str = "us-east-1") -> dict:
    """Bắt đầu job nền: tạo snapshot mới trong <workdir>/data/ và sync delta từ bucket (chỉ tải xuống).

    csv_only=True chỉ tải CSV chi phí (vài giây). Đầy đủ có thể mất 30–40 phút với ~150k object —
    poll bằng audit_job_status. Lần đầu lấy `days` ngày gần nhất; lần sau chỉ tải từ ngày snapshot trước − 1.
    """
    _check(bucket, profile)
    if not re.match(r"^[a-z]{2}(-[a-z]+)+-\d$", region):
        raise ValueError("region không hợp lệ")
    wd = _workdir(workdir, bucket)
    wd.mkdir(parents=True, exist_ok=True)
    env = {"BUCKET": bucket, "PROFILE": profile, "WORKDIR": str(wd), "ACCOUNT": _account(bucket),
           "REGION": region, "JOBS": str(max(1, min(int(jobs), 16))), "DAYS": str(max(1, min(int(days), 400))),
           "CSV_ONLY": "1" if csv_only else "0"}
    meta = _start_job(wd, "sync", ["bash", str(SCRIPTS / "sync-snapshot.sh")], env, wd)
    return {"jobId": meta["id"], "workdir": str(wd), "csvOnly": csv_only,
            "hint": "Gọi audit_job_status(workdir, job_id) để theo dõi."}


@mcp.tool(annotations=LOCAL)
def audit_scan_start(workdir: str, bucket: str | None = None, work_pattern: str | None = None,
                     since: str | None = None) -> dict:
    """Bắt đầu job nền quét prompt-logs của snapshot mới nhất: metrics.json + phân loại secret
    (data/secret-triage.json, đã che). Nếu có work_pattern (regex dự án công ty) thì chạy thêm
    đánh giá mục đích sử dụng (data/purpose.json). Vài phút cho ~150k object.
    """
    if bucket:
        _check(bucket)
    if work_pattern:
        re.compile(work_pattern)
    if since and not re.match(r"^\d{4}-\d{2}-\d{2}$", since):
        raise ValueError("since phải dạng YYYY-MM-DD")
    wd = _workdir(workdir)
    snap = _latest_snapshot(wd)
    if not (snap / "prompt-logs").is_dir():
        return {"skipped": True, "reason": "Snapshot không có prompt-logs/ (biến thể B) — không có gì để quét."}
    steps = [[PY, str(SCRIPTS / "export_json.py"), "--root", ".", "--out", str(wd / "data" / "metrics.json")]
             + (["--bucket", bucket] if bucket else []),
             [PY, str(SCRIPTS / "triage-secrets.py"), "--root", ".", "--out", str(wd / "data" / "secret-triage.json")]]
    if work_pattern:
        steps.append([PY, str(SCRIPTS / "purpose.py"), "--root", ".", "--work", work_pattern,
                      "--json", str(wd / "data" / "purpose.json")] + (["--since", since] if since else []))
    # chuỗi lệnh chạy tuần tự trong một tiến trình con, không qua shell
    runner = [PY, "-c",
              "import json,subprocess,sys\n"
              "for c in json.loads(sys.argv[1]):\n"
              "    print('[step]', ' '.join(c[1:2]), flush=True)\n"
              "    r = subprocess.run(c)\n"
              "    if r.returncode: sys.exit(r.returncode)\n",
              json.dumps(steps)]
    meta = _start_job(wd, "scan", runner, {"KIRO_AUDIT_TZ": os.environ.get("KIRO_AUDIT_TZ", "7")}, snap)
    return {"jobId": meta["id"], "snapshot": snap.name, "steps": len(steps)}


@mcp.tool(annotations=RO)
def audit_job_status(workdir: str, job_id: str | None = None) -> dict:
    """Trạng thái job nền (sync/scan). Không truyền job_id → job mới nhất. Log đã che secret."""
    wd = _workdir(workdir)
    jd = _jobs_dir(wd)
    metas = sorted(jd.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if job_id:
        if not re.match(r"^[\w\-]+$", job_id):
            raise ValueError("job_id không hợp lệ")
        metas = [jd / f"{job_id}.json"]
    if not metas or not metas[-1].is_file():
        return {"error": "Không có job"}
    m = json.loads(metas[-1].read_text())
    log = Path(m["log"]).read_text(errors="replace") if Path(m["log"]).is_file() else ""
    running = _alive(m["pid"])
    tail = [l for l in log.splitlines() if l.strip()][-25:]
    st = {"id": m["id"], "kind": m["kind"], "running": running,
          "elapsedMin": round((time.time() - m["started"]) / 60, 1), "logTail": redact("\n".join(tail))}
    if m["kind"] == "sync":
        st["prefixFail"] = log.count("FAIL prompt-logs")
        st["done"] = "[✓]" in log and not running
        try:
            snap = _latest_snapshot(wd)
            st["snapshot"] = snap.name
            st["files"] = sum(1 for _ in snap.rglob("*") if _.is_file())
        except FileNotFoundError:
            pass
    else:
        st["done"] = not running and (wd / "data" / "secret-triage.json").is_file()
    return st


@mcp.tool(annotations=LOCAL)
def audit_job_cancel(workdir: str, job_id: str) -> dict:
    """Dừng một job nền đang chạy (chỉ dừng tiến trình local, không ảnh hưởng AWS)."""
    if not re.match(r"^[\w\-]+$", job_id):
        raise ValueError("job_id không hợp lệ")
    m = json.loads((_jobs_dir(_workdir(workdir)) / f"{job_id}.json").read_text())
    try:
        os.killpg(m["pid"], signal.SIGTERM)
        return {"cancelled": True}
    except ProcessLookupError:
        return {"cancelled": False, "reason": "đã kết thúc"}


@mcp.tool(annotations=LOCAL)
def audit_cost(workdir: str, min_active_days: int = 10, crosscheck: str | None = None, top: int = 60) -> dict:
    """Chi phí từ CSV của snapshot mới nhất: bảng từng user, NGƯỜI ĐÃ BỊ CHẶN vì hết credit, hệ số credit
    theo model, và 3 phương án (thu hồi seat / hạ gói an toàn / đổi model). Ghi data/*-metrics.json.

    min_active_days: không đề nghị hạ gói người có ít ngày hoạt động hơn (mặc định 10).
    crosscheck: đường dẫn file JSON báo cáo độc lập (prefix reports/ của bucket) để đối chiếu.
    """
    wd = _workdir(workdir)
    snap = _latest_snapshot(wd)
    d = wd / "data"
    tz = {"KIRO_AUDIT_TZ": os.environ.get("KIRO_AUDIT_TZ", "7")}
    runs = {
        "analyze-csv": _run([PY, str(SCRIPTS / "analyze-csv.py"), "--snapshot", str(snap),
                             "--json", str(d / "csv-metrics.json")], env=tz),
        "monthly-rollup": _run([PY, str(SCRIPTS / "monthly-rollup.py"), "--snapshot", str(snap),
                                "--json", str(d / "monthly-metrics.json")], env=tz),
        "model-efficiency": _run([PY, str(SCRIPTS / "model-efficiency.py"), "--snapshot", str(snap),
                                  "--json", str(d / "model-metrics.json")], env=tz),
    }
    sc = [PY, str(SCRIPTS / "scenarios.py"), "--monthly", str(d / "monthly-metrics.json"),
          "--model", str(d / "model-metrics.json"), "--json", str(d / "scenarios.json"),
          "--min-active-days", str(int(min_active_days))]
    if crosscheck:
        sc += ["--crosscheck", str(Path(crosscheck).expanduser())]
    runs["scenarios"] = _run(sc, env=tz)
    failed = {k: v["stderr"][-1500:] or v["stdout"][-1500:] for k, v in runs.items() if not v["ok"]}
    if failed:
        return {"error": "Script lỗi", "details": failed}

    C, M, S, Mo = (_load(d / f) for f in ("csv-metrics.json", "monthly-metrics.json",
                                         "scenarios.json", "model-metrics.json"))
    users = sorted(C["users"], key=lambda u: -u["credits"])
    blocked = {c["email"] or c["userId"] for c in Mo.get("censored", [])}
    return {
        "snapshot": snap.name, "csvDays": [C["days"][0], C["days"][-1], len(C["days"])],
        "basisMonth": S["basisMonth"], "basisCoverage": S["basisCoverage"],
        "totals": C["totals"], "currentMonthlyUsd": S["currentMonthlyUsd"], "currentYearlyUsd": S["currentYearlyUsd"],
        "blockedUsers": Mo.get("censored", []),
        "nearLimit": [{"email": u["email"], "credits": round(u["credits"], 2), "pct": round(u["credits"] / u["usageLimit"] * 100, 1)}
                      for u in users if u["email"] not in blocked and u["usageLimit"] and u["credits"] / u["usageLimit"] >= 0.8],
        "modelCreditPerMessage": Mo.get("nnls", {}).get("creditPerMessageByModel"),
        "modelFitR2": Mo.get("nnls", {}).get("r2"),
        "optionA_reclaimSeats": S["optionA_reclaimSeats"],
        "optionB_rightSize": {k: v for k, v in S["optionB_rightSize"].items() if k != "rows"},
        "optionC_modelShift": S["optionC_modelShift"],
        "verification": S.get("verification"), "crossCheck": S.get("crossCheck"),
        "nonCorpEmails": (M or {}).get("nonCorpEmails"),
        "idleUsers": (M or {}).get("idleUsersInBasisMonth"),
        "users": [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in u.items()
                   if k in ("email", "tier", "credits", "messages", "creditPerMessage", "projectedPctOfLimit",
                            "workdaysObserved", "firstDay", "status")} for u in users[:max(1, int(top))]],
        "files": [str(d / f) for f in ("csv-metrics.json", "monthly-metrics.json", "model-metrics.json", "scenarios.json")],
        "notes": ["Kết luận bằng monthly-rollup/scenarios; analyze-csv là ngoại suy — chỉ tham khảo.",
                  "Người bị chặn: dữ liệu bị cắt — không hạ gói. Thử đổi model trước khi nâng gói.",
                  "Không đề nghị huỷ license chỉ vì im lặng vài ngày."],
    }


def _severity(h: dict) -> str:
    c, host = h["class"], (h.get("host") or "").lower()
    if c in ("Private key", "Slack/GitHub token", "npm authToken"):
        return "critical"
    if c == "AWS access key":
        return "critical" if h.get("secretKeyNearby") else "high"
    if c == "Connection string":
        if not h.get("hasPassword"):
            return "info"
        return "critical" if re.search(r"prod(?!uct)", host) else "high"
    if c in ("JWT token", "Presigned URL"):
        return "low" if h.get("expired") else "high"
    if c == "AWS temp key":
        return "low"
    if c == "Secret assignment":
        return "high"
    return "medium"


@mcp.tool(annotations=RO)
def audit_secret_summary(workdir: str, min_severity: str = "high", previous_triage: str | None = None) -> dict:
    """Tóm tắt credential bị lộ (từ data/secret-triage.json, đã che) theo mức độ.

    min_severity: critical | high | medium | low | info — mức thấp nhất liệt kê chi tiết.
    previous_triage: secret-triage.json của kỳ trước → đánh dấu giá trị XUẤT HIỆN LẠI (có thể chưa thu hồi).
    """
    wd = _workdir(workdir)
    t = _load(wd / "data" / "secret-triage.json")
    if not t:
        return {"error": "Chưa có data/secret-triage.json — chạy audit_scan_start"}
    order = ["critical", "high", "medium", "low", "info"]
    lim = order.index(min_severity) if min_severity in order else 1
    prev = set()
    if previous_triage:
        pt = _load(Path(previous_triage).expanduser())
        prev = {(h["class"], h["value"]) for h in (pt or {}).get("hits", [])}
    by = {s: [] for s in order}
    for h in t["hits"]:
        h = {**h, "severity": _severity(h)}
        if (h["class"], h["value"]) in prev:
            h["reappeared"] = True
        by[h["severity"]].append(h)
    people = sorted({u for s in order[:2] for h in by[s] for u in h["users"]})
    return {
        "generated": t.get("generated"), "filesScanned": t.get("files"), "distinctValues": len(t["hits"]),
        "counts": {s: len(v) for s, v in by.items()},
        "peopleWithCriticalOrHigh": people,
        "details": {s: [{k: v for k, v in h.items() if k != "near"} for h in by[s]] for s in order[:lim + 1]},
        "reappearedFromPrevious": [h for s in order for h in by[s] if h.get("reappeared")],
        "rules": ["Không thử dùng khoá. Giả định còn hiệu lực cho tới khi chủ hệ thống thay.",
                  "Báo cáo chỉ nêu loại, hệ thống, người, thời hạn — không bao giờ nêu giá trị.",
                  "Nguyên nhân gốc thường là thiếu hướng dẫn + file đang mở bị IDE tự gửi, không phải cố ý."],
    }


@mcp.tool(annotations=RO)
def audit_purpose_summary(workdir: str) -> dict:
    """Người cần trao đổi về mục đích sử dụng (từ data/purpose.json). Chỉ là tín hiệu — KHÔNG phải căn cứ xử lý."""
    p = _load(_workdir(workdir) / "data" / "purpose.json")
    if p is None:
        return {"error": "Chưa có data/purpose.json — chạy audit_scan_start với work_pattern"}
    flagged = {u: v for u, v in p.items() if v.get("flag")}
    return {"users": len(p), "flagged": flagged,
            "noFileContext": [u for u, v in p.items() if v.get("noFileContext")],
            "limits": ["Không có dấu hiệu công ty ≠ dùng cá nhân (POC, học công cụ, đối tác).",
                       "Công cụ mới triển khai — thử nghiệm ban đầu là bình thường.",
                       "Người không mở file trong workspace gần như không để lại dấu vết; không có file path ≠ bất thường.",
                       "Giám sát qua nội dung cần chính sách + thông báo trước — không xử lý hồi tố."]}


@mcp.tool(annotations=LOCAL)
def audit_build_reports(workdir: str, bucket: str, customer: str, profile: str = "") -> dict:
    """Sinh reports/kiro-activity-report.html + reports/bao-cao-chi-phi-va-muc-dich-su-dung.html từ data/*.json.
    Nếu có reports/bao-cao-chi-phi-va-muc-dich-su-dung.md (bản viết tay) thì sinh HTML từ file đó thay thế.
    Kiểm tra: canvas↔chart khớp, không còn secret thô trong báo cáo.
    """
    _check(bucket, profile or None)
    wd = _workdir(workdir)
    out = wd / "reports"
    out.mkdir(exist_ok=True)
    r = _run([PY, str(SCRIPTS / "build-dashboard.py"), "--data-dir", str(wd / "data"), "--out-dir", str(out),
              "--account", _account(bucket), "--bucket", bucket, "--customer", customer[:80]]
             + (["--profile", profile] if profile else []),
             env={"KIRO_AUDIT_TZ": os.environ.get("KIRO_AUDIT_TZ", "7")})
    res: dict[str, Any] = {"dashboard": r}
    md = out / "bao-cao-chi-phi-va-muc-dich-su-dung.md"
    if md.is_file():
        res["approvalFromMarkdown"] = _run([PY, str(SCRIPTS / "build-approval-html.py"), "--workdir", str(wd)])
    checks = {}
    raw = re.compile(r"(AKIA|ASIA)[0-9A-Z]{16}|eyJhbGciOiJ[A-Za-z0-9_-]{20,}|glpat-[A-Za-z0-9_-]{10}|AIza[0-9A-Za-z_-]{35}")
    for f in out.glob("*.html"):
        h = f.read_text(encoding="utf-8", errors="replace")
        can = sorted(re.findall(r'canvas id="([^"]+)"', h))
        mk = sorted(set(re.findall(r"(?:mk|new Chart)\(\s*(?:document\.getElementById\()?['\"]([^'\"]+)['\"]", h)))
        checks[f.name] = {"bytes": len(h), "canvas": len(can), "canvasWithoutChart": sorted(set(can) - set(mk)),
                          "rawSecretHits": len(raw.findall(h))}
    res["checks"] = checks
    res["reports"] = [str(p) for p in out.glob("*.html")]
    return res


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
