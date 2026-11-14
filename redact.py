"""把凭据从台账、日志、管理接口和导出里去掉。

只处理键名和文本形态。不连接数据库，也不认识账号文件。
"""
from __future__ import annotations

import re

REDACTED = "«redacted»"

# 这些键整段丢掉，不把值写成占位符后再落盘（占位符旁边往往还留着结构）。
SECRET_KEYS = frozenset({
    "cookies", "cookie", "cookie_header", "authorization", "api_key",
    "apikey", "password", "passwd", "proxy", "proxy_password",
    "token", "access_token", "refresh_token", "secret", "private_key",
    "set-cookie", "x-api-key",
})

_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL | re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]{8,}")
_M2A = re.compile(r"\bm2a_[A-Za-z0-9]{8,}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\b")
_SK = re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9]{16,}\b")
_PROXY_URL = re.compile(
    r"(?i)\b(?:https?|socks5h?|socks4)://[^\s\"']+:[^\s\"']+@[^\s\"']+"
)
_COOKIE_PAIR = re.compile(
    r"(?i)\b(?:hatch_sess|hatch_gw|hatch_vml|hatch_native_auth_device)=[^;\s,]+"
)
_KV = re.compile(
    r"(?i)\b(cookies?|cookie_header|authorization|api[_-]?key|password|passwd|"
    r"proxy(?:_password)?|tokens?|secrets?|private_key|access_token|refresh_token)\b"
    r"(\s*[:=]\s*)([\"']?)"
    r"([^\s,;\"'}{]{4,})"
)


def redact_text(value: str) -> str:
    """替换正文里的 Bearer、PEM、m2a_ 服务密钥和带名字的凭据。"""
    if not value:
        return value
    value = _PEM.sub(REDACTED, value)
    value = _BEARER.sub("Bearer " + REDACTED, value)
    value = _M2A.sub(REDACTED, value)
    value = _JWT.sub(REDACTED, value)
    value = _SK.sub(REDACTED, value)
    value = _PROXY_URL.sub(REDACTED, value)
    value = _COOKIE_PAIR.sub(REDACTED, value)

    def _kv(match: re.Match) -> str:
        return match.group(1) + match.group(2) + match.group(3) + REDACTED

    return _KV.sub(_kv, value)


def scrub(value):
    """丢掉凭据字段，并扫描剩下的字符串。列表和字典原样递归。"""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if str(key).lower() in SECRET_KEYS:
                continue
            cleaned[str(key)] = scrub(item)
        return cleaned
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
