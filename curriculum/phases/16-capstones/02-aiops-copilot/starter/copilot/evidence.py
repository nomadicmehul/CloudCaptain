from __future__ import annotations

from typing import Any, Dict

from .kube import Kube


def collect(kube: Kube, namespace: str) -> Dict[str, Any]:
    raise NotImplementedError("milestone evidence (copilot collect): return {\"namespace\": ..., \"evidence\": [...]}")
