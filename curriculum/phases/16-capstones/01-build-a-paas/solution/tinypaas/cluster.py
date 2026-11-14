from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .render import NAME_LABEL, namespace_name
from .runner import PaasError, Result, Runner, must

DEFAULT_PREFIX = "tinypaas"


class Cluster:
    def __init__(self, run: Runner, context: Optional[str] = None, kind_name: Optional[str] = None, prefix: str = DEFAULT_PREFIX):
        self.run = run
        self._context = context
        self._kind_name = kind_name
        self.prefix = prefix

    @classmethod
    def from_env(cls, run: Runner, environ: Mapping[str, str]) -> "Cluster":
        return cls(run, environ.get("TINYPAAS_CONTEXT") or None, environ.get("TINYPAAS_KIND_CLUSTER") or None,
                   environ.get("TINYPAAS_NAMESPACE_PREFIX") or DEFAULT_PREFIX)

    @property
    def context(self) -> str:
        if self._context is None:
            result = self.run(["kubectl", "config", "current-context"], timeout=20)
            if not result.ok or not result.out.strip():
                raise PaasError("no kubectl context: create a cluster with kind create cluster --name tinypaas")
            self._context = result.out.strip()
        return self._context

    @property
    def kind_name(self) -> str:
        if self._kind_name is None:
            if not self.context.startswith("kind-"):
                raise PaasError(f"context {self.context} is not a kind cluster; set TINYPAAS_KIND_CLUSTER to the kind cluster name")
            self._kind_name = self.context[len("kind-"):]
        return self._kind_name

    def namespace(self, app: str) -> str:
        return namespace_name(self.prefix, app)

    def kubectl(self, *args: str, input: Optional[str] = None, timeout: float = 60) -> Result:
        return self.run(["kubectl", "--context", self.context, *args], input=input, timeout=timeout)

    def get_json(self, *args: str) -> Dict[str, Any]:
        return json.loads(must(self.kubectl("get", *args, "-o", "json"), f"kubectl get {' '.join(args)}").out)

    def apply(self, document: Dict[str, Any]) -> None:
        must(self.kubectl("apply", "-f", "-", input=json.dumps(document)), "kubectl apply")

    def ensure_namespace(self, document: Dict[str, Any]) -> None:
        self.apply(document)

    def rollout(self, ns: str, app: str, timeout: int) -> Tuple[bool, str]:
        result = self.kubectl("-n", ns, "rollout", "status", f"deployment/{app}", f"--timeout={timeout}s", timeout=timeout + 30)
        return result.ok, (result.err or result.out).strip()

    def scale(self, ns: str, app: str, replicas: int) -> None:
        must(self.kubectl("-n", ns, "scale", f"deployment/{app}", f"--replicas={replicas}"), "kubectl scale")

    def node_address(self) -> str:
        nodes = self.get_json("nodes")["items"]
        for node in nodes:
            for address in node.get("status", {}).get("addresses", []):
                if address.get("type") == "InternalIP":
                    return address["address"]
        raise PaasError("no node has an InternalIP")

    def node_port(self, ns: str, app: str) -> int:
        result = self.kubectl("-n", ns, "get", "service", app, "-o", "json")
        if not result.ok:
            raise PaasError(f"{app} has no Service in {ns}: deploy it first")
        for port in json.loads(result.out)["spec"].get("ports", []):
            if port.get("nodePort"):
                return int(port["nodePort"])
        raise PaasError(f"service {app} in {ns} has no nodePort")

    def pods(self, ns: str, app: str) -> List[str]:
        items = self.get_json("pods", "-n", ns, "-l", f"{NAME_LABEL}={app}")["items"]
        return sorted(p["metadata"]["name"] for p in items if not p["metadata"].get("deletionTimestamp"))

    def pod_logs(self, ns: str, pod: str, since: str) -> str:
        return must(self.kubectl("-n", ns, "logs", pod, "--timestamps", f"--since={since}", "--all-containers"), f"kubectl logs {pod}").out
