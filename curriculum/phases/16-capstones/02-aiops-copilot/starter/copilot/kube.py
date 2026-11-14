from __future__ import annotations

import json
import subprocess
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

Runner = Callable[[Sequence[str], float], Tuple[int, str, str]]


class KubeError(RuntimeError):
    pass


def subprocess_runner(argv: Sequence[str], timeout: float = 60.0) -> Tuple[int, str, str]:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:.0f}s"
    return done.returncode, done.stdout, done.stderr


class Kube:
    def __init__(self, context: Optional[str] = None, runner: Runner = subprocess_runner) -> None:
        self.context = context
        self.runner = runner

    def argv(self, *args: str) -> List[str]:
        return ["kubectl", *(["--context", self.context] if self.context else []), *args]

    def run(self, *args: str, timeout: float = 60.0) -> Tuple[int, str, str]:
        return self.runner(self.argv(*args), timeout)

    def get_json(self, *args: str) -> Dict[str, Any]:
        code, out, err = self.run(*args, "-o", "json")
        if code != 0:
            raise KubeError(f"kubectl {' '.join(args)}: {(err or out).strip()[:300]}")
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise KubeError(f"kubectl {' '.join(args)} did not return JSON: {exc}") from exc

    def items(self, *args: str) -> List[Dict[str, Any]]:
        return list(self.get_json(*args).get("items") or [])
