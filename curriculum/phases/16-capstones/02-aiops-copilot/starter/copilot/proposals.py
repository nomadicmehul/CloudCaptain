from __future__ import annotations

from typing import Any, Dict

from .config import Config
from .kube import Kube


def apply(kube: Kube, config: Config, proposal_id: str) -> Dict[str, Any]:
    raise NotImplementedError("milestone guardrails (copilot apply --approve): run one stored proposal")
