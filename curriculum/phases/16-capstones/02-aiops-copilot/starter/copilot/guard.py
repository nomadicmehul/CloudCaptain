from __future__ import annotations


def redact(text: str) -> str:
    raise NotImplementedError("milestone guardrails: replace secret values with [REDACTED]")


def wrap(source: str, text: str) -> str:
    raise NotImplementedError("milestone guardrails: wrap tool output in <untrusted_data source=...> ... </untrusted_data>")
