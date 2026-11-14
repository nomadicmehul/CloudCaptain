from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Sequence


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


Runner = Callable[..., Result]


def run(args: Sequence[str], input: Optional[str] = None, timeout: float = 120, env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None) -> Result:
    try:
        done = subprocess.run(list(args), input=input, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)
    except FileNotFoundError:
        return Result(127, "", f"{args[0]}: command not found")
    except subprocess.TimeoutExpired:
        return Result(124, "", f"{' '.join(args[:3])} timed out after {timeout:g}s")
    return Result(done.returncode, done.stdout, done.stderr)


def must(result: Result, what: str) -> Result:
    if not result.ok:
        raise PaasError(f"{what} failed (exit {result.code}): {result.err.strip() or result.out.strip()}")
    return result
