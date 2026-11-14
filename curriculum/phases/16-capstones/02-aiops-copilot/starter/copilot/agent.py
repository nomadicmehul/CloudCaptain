from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict

from .config import Config

API_VERSION = "2026-10-21"


class ModelError(RuntimeError):
    pass


def post_messages(config: Config, headers: Dict[str, str], body: Dict[str, Any], timeout: float = 600.0) -> Dict[str, Any]:
    request = urllib.request.Request(f"{config.model_url}/v1/messages", data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise ModelError(f"model API returned HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:400]}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ModelError(f"cannot reach {config.model_url}: {exc}") from exc


def run_agent(question: str, namespace: str, config: Config, max_turns: int, token_budget: int) -> Dict[str, Any]:
    raise NotImplementedError("milestone agent-loop (copilot ask): run the Messages API loop and return answer, stop_reason, turns, tool_calls, proposals, usage")
