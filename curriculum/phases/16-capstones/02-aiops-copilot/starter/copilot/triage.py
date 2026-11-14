from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from .kube import Kube


def classify(items: Sequence[Dict[str, Any]]) -> Tuple[str, List[str], str]:
    raise NotImplementedError("milestone triage (copilot triage): return (category, citations, reason)")


def triage(kube: Kube, namespace: str) -> Dict[str, Any]:
    raise NotImplementedError("milestone triage (copilot triage): return {\"namespace\": ..., \"incidents\": [...]}")
