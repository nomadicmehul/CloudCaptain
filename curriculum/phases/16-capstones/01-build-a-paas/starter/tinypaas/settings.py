from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


class PaasError(Exception):
    pass


def run(args: List[str], input: Optional[str] = None, timeout: float = 120, env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None) -> Result:
    try:
        done = subprocess.run(args, input=input, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)
    except FileNotFoundError:
        return Result(127, "", f"{args[0]}: command not found")
    except subprocess.TimeoutExpired:
        return Result(124, "", f"{args[0]} timed out after {timeout:g}s")
    return Result(done.returncode, done.stdout, done.stderr)


@dataclass
class Settings:
    data_dir: str
    context: Optional[str]
    kind_cluster: Optional[str]
    namespace_prefix: str

    @classmethod
    def load(cls, data_dir: Optional[str], environ: Mapping[str, str]) -> "Settings":
        return cls(
            data_dir=os.path.abspath(data_dir or environ.get("TINYPAAS_HOME") or os.path.expanduser("~")),
            context=environ.get("TINYPAAS_CONTEXT") or None,
            kind_cluster=environ.get("TINYPAAS_KIND_CLUSTER") or None,
            namespace_prefix=environ.get("TINYPAAS_NAMESPACE_PREFIX") or "tinypaas",
        )

    @property
    def state_dir(self) -> str:
        return os.path.join(self.data_dir, ".tinypaas")

    def namespace(self, app: str) -> str:
        return f"{self.namespace_prefix}-{app}"

    def current_context(self) -> str:
        if self.context:
            return self.context
        result = run(["kubectl", "config", "current-context"], timeout=20)
        if not result.ok or not result.out.strip():
            raise PaasError("no kubectl context: create a cluster with kind create cluster --name tinypaas")
        return result.out.strip()

    def kind_cluster_name(self) -> str:
        if self.kind_cluster:
            return self.kind_cluster
        context = self.current_context()
        if not context.startswith("kind-"):
            raise PaasError(f"context {context} is not a kind cluster; set TINYPAAS_KIND_CLUSTER")
        return context[len("kind-"):]

    def kubectl(self, *args: str, input: Optional[str] = None, timeout: float = 60) -> Result:
        return run(["kubectl", "--context", self.current_context(), *args], input=input, timeout=timeout)
