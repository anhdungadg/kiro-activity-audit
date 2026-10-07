"""Che giá trị secret trong MỌI chuỗi trước khi trả về cho model.

Đây là lớp bảo vệ thứ hai: script đã tự che, nhưng mọi stdout/stderr/log mà server trả về
vẫn đi qua đây - model không bao giờ thấy giá trị thật kể cả khi một script có lỗi.
"""
import re

_TOKEN = re.compile(
    r"(?:AKIA|ASIA)[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_\-]{35}"
    r"|glpat-[\w\-]{10,}"
    r"|gh[pousr]_[0-9A-Za-z]{36}|xox[baprs]-[0-9A-Za-z\-]{10,}"
    r"|eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]*(?:\.[A-Za-z0-9_\-]*)?"
    r"|X-Amz-Signature=[0-9a-f]{64}|X-Amz-Security-Token=[^&\s\"']+"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
)
_KV = re.compile(
    r"(?i)((?:password|passwd|pwd|secret|token|api[_-]?key|apikey|credential|_authToken|[\w.]*\.key)"
    r"[\w.\-]*\\?[\"']?\s*[:=]\s*\\?[\"']?)([A-Za-z0-9+/=_\-.]{12,})"
)
_URL_PW = re.compile(r"(://[^:/@\s\"']+:)([^@\s\"']+)(@)")
_AWS_SECRET = re.compile(r"(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=])")


def mask(v: str) -> str:
    return f"{v[:4]}…({len(v)})"


def redact(s: str) -> str:
    if not s:
        return s
    s = _TOKEN.sub(lambda m: mask(m.group(0)), s)
    s = _KV.sub(lambda m: m.group(1) + "<REDACTED>", s)
    s = _URL_PW.sub(lambda m: m.group(1) + "<REDACTED>" + m.group(3), s)
    # chuỗi 40 ký tự có cả hoa, thường, số - dạng AWS secret key
    s = _AWS_SECRET.sub(
        lambda m: "<REDACTED-40>" if (re.search(r"[a-z]", m.group(0)) and re.search(r"[A-Z]", m.group(0))
                                      and re.search(r"[0-9/+]", m.group(0))) else m.group(0), s)
    return s
