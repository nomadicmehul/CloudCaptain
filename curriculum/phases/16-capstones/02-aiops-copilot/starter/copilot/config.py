from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from typing import List, Mapping, Optional

DEFAULT_MODEL_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"


class Refused(Exception):
    pass


@dataclass
class Config:
    allowed: List[str] = field(default_factory=list)
    context: Optional[str] = None
    home: str = ".copilot"
    model_url: str = DEFAULT_MODEL_URL
    model_url_set: bool = False
    model: str = DEFAULT_MODEL
    api_key: Optional[str] = None
    effort: str = DEFAULT_EFFORT

    def allows(self, namespace: Optional[str]) -> bool:
        return bool(namespace) and any(fnmatch.fnmatchcase(str(namespace), pattern) for pattern in self.allowed)

    def require(self, namespace: Optional[str]) -> str:
        if not self.allows(namespace):
            allowed = ", ".join(self.allowed) or "nothing (COPILOT_ALLOWED_NAMESPACES is empty)"
            raise Refused(f"namespace {namespace!r} is not allowed; COPILOT_ALLOWED_NAMESPACES allows {allowed}")
        return str(namespace)


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    env = os.environ if env is None else env
    allowed = [part.strip() for part in env.get("COPILOT_ALLOWED_NAMESPACES", "").split(",") if part.strip()]
    url = env.get("COPILOT_MODEL_URL", "").strip()
    return Config(
        allowed=allowed,
        context=env.get("COPILOT_CONTEXT") or None,
        home=env.get("COPILOT_HOME") or ".copilot",
        model_url=(url or DEFAULT_MODEL_URL).rstrip("/"),
        model_url_set=bool(url),
        model=env.get("COPILOT_MODEL") or DEFAULT_MODEL,
        api_key=env.get("ANTHROPIC_API_KEY") or None,
        effort=env.get("COPILOT_EFFORT") or DEFAULT_EFFORT,
    )
