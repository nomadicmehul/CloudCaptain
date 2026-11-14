from __future__ import annotations

from typing import Any, Dict

from .config import Config
from .kube import Kube


def build_report(kube: Kube, config: Config, namespace: str) -> Dict[str, Any]:
    raise NotImplementedError("milestone report (copilot report): summary, impact, timeline, hypotheses, actions, evidence")


def render_markdown(report: Dict[str, Any]) -> str:
    raise NotImplementedError("milestone report (copilot report --format md)")
