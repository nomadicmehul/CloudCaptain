from __future__ import annotations

import re

REDACTED = "[REDACTED]"
SECRET_NAME = r"[A-Za-z0-9_.-]*(?:passw(?:or)?d|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|credential|authorization|auth)[A-Za-z0-9_.-]*"
KEY_VALUE = re.compile(r"(?i)\b(" + SECRET_NAME + r")(\"?'?\s*[:=]\s*\"?'?)((?:bearer|basic|token)\s+)?([^\s\"',;]+)")
ENV_PAIR = re.compile(r"(?i)(\"name\"\s*:\s*\"" + SECRET_NAME + r"\"\s*,\s*\"value\"\s*:\s*\")([^\"]*)(\")")
BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{6,}")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
WRAPPER_TAG = re.compile(r"(?i)<(/?)(untrusted_data)")
SECRET_KINDS = {"secret", "secrets"}


def redact(text: str) -> str:
    text = PRIVATE_KEY.sub(REDACTED, text)
    text = ENV_PAIR.sub(lambda m: m.group(1) + REDACTED + m.group(3), text)
    text = KEY_VALUE.sub(lambda m: m.group(1) + m.group(2) + (m.group(3) or "") + REDACTED, text)
    text = BEARER.sub(lambda m: m.group(1) + " " + REDACTED, text)
    return JWT.sub(REDACTED, text)


def wrap(source: str, text: str) -> str:
    safe = WRAPPER_TAG.sub(lambda m: "&lt;" + m.group(1) + m.group(2), text)
    return f'<untrusted_data source="{source}">\n{safe}\n</untrusted_data>'


def truncate(text: str, limit: int = 20000) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n[truncated {len(text) - limit} characters]"
