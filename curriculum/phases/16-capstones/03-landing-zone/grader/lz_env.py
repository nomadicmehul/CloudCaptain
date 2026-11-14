"""Processes, working copies and the local moto server the landing-zone checks share."""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

import lz_aws

MOTO_IMAGE = "motoserver/moto:5.2.5"
PREFIX = "cc-grade-lz-"
REGION = lz_aws.REGION
COPY_IGNORE = (".terraform", ".lz", ".git", "*.tfstate", "*.tfstate.backup", "*.tfplan", "__pycache__", "errored.tfstate")


class SetupError(Exception):
    pass


@dataclass
class Result:
    code: int
    out: str
    err: str
    timed_out: bool = False
    seconds: float = 0.0

    def tail(self, lines: int = 12) -> str:
        text = "\n".join(part for part in (self.err.strip(), self.out.strip()) if part)
        nonblank = [line.strip(" │╷╵") for line in text.splitlines() if line.strip(" │╷╵")]
        errors = [i for i, line in enumerate(nonblank) if line.startswith("Error:")]
        kept = nonblank[errors[0]:errors[0] + lines] if errors else nonblank[-lines:]
        return " | ".join(kept)[-1500:] or "(no output)"


Runner = Callable[[Sequence[str], Optional[str], Optional[Dict[str, str]], float], Result]


def run(argv: Sequence[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, timeout: float = 60.0) -> Result:
    started = time.monotonic()
    try:
        done = subprocess.run(list(argv), cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return Result(127, "", f"{argv[0]}: command not found", False, time.monotonic() - started)
    except PermissionError:
        return Result(126, "", f"{argv[0]}: permission denied (is it executable? chmod +x {argv[0]})", False, time.monotonic() - started)
    except subprocess.TimeoutExpired as expired:
        out = expired.stdout.decode("utf-8", "replace") if isinstance(expired.stdout, bytes) else (expired.stdout or "")
        err = expired.stderr.decode("utf-8", "replace") if isinstance(expired.stderr, bytes) else (expired.stderr or "")
        return Result(124, out, err + f"\n(timed out after {timeout:.0f}s)", True, time.monotonic() - started)
    return Result(done.returncode, done.stdout, done.stderr, False, time.monotonic() - started)


def parse_json_output(text: str) -> Optional[object]:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        pass
    lines = stripped.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].lstrip().startswith(("{", "[")):
            try:
                return json.loads("\n".join(lines[index:]))
            except ValueError:
                continue
    return None


def plugin_cache() -> str:
    path = os.environ.get("TF_PLUGIN_CACHE_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "cloudcaptain-grader", "tofu-plugins")
    os.makedirs(path, exist_ok=True)
    return path


def tofu_env(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.update({
        "TF_PLUGIN_CACHE_DIR": plugin_cache(),
        "TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE": "1",
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
        "CHECKPOINT_DISABLE": "1",
        "NO_COLOR": "1",
    })
    return env


def aws_env(endpoint: str, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    endpoint = lz_aws.require_local(endpoint)
    env = {k: v for k, v in tofu_env(base).items() if not k.startswith("AWS_")}
    env.update({
        "LZ_AWS_ENDPOINT": endpoint,
        "AWS_ENDPOINT_URL": endpoint,
        "AWS_ACCESS_KEY_ID": lz_aws.ACCESS_KEY,
        "AWS_SECRET_ACCESS_KEY": lz_aws.SECRET_KEY,
        "AWS_REGION": REGION,
        "AWS_DEFAULT_REGION": REGION,
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    })
    return env


class Workspace:
    def __init__(self, project: str) -> None:
        self.root = tempfile.mkdtemp(prefix=PREFIX, dir=os.environ.get("CC_GRADER_WORK") or None)
        self.project = project

    def copy(self, name: str = "project") -> str:
        target = os.path.join(self.root, name)
        shutil.copytree(self.project, target, symlinks=True, ignore=shutil.ignore_patterns(*COPY_IGNORE))
        return target

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def wait_until(predicate: Callable[[], bool], timeout: float, interval: float = 0.25, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> bool:
    deadline = clock() + timeout
    while True:
        if predicate():
            return True
        if clock() >= deadline:
            return False
        sleep(interval)


class Moto:
    def __init__(self, runner: Runner = run, which: Callable[[str], Optional[str]] = shutil.which, environ: Optional[Dict[str, str]] = None) -> None:
        self.runner = runner
        self.which = which
        self.environ = os.environ if environ is None else environ
        self.endpoint = ""
        self.container = ""

    def start(self) -> str:
        if self.endpoint:
            return self.endpoint
        given = self.environ.get("LZ_AWS_ENDPOINT", "").strip()
        if given:
            try:
                endpoint = lz_aws.require_local(given)
            except lz_aws.EndpointError as error:
                raise SetupError(str(error)) from None
            if not lz_aws.Client(endpoint).is_moto():
                raise SetupError(f"LZ_AWS_ENDPOINT={endpoint} does not answer like moto (GET /moto-api/data.json): start moto in server mode or unset LZ_AWS_ENDPOINT so the grader starts {MOTO_IMAGE} itself")
            self.endpoint = endpoint
            return endpoint
        if not self.which("docker"):
            raise SetupError(f"Docker is not installed: the grader runs moto from {MOTO_IMAGE}. Install Docker, or run moto yourself (pip install 'moto[server]==5.2.5'; moto_server -p 5000) and set LZ_AWS_ENDPOINT=http://127.0.0.1:5000")
        name = f"{PREFIX}moto-{os.getpid()}"
        self.runner(["docker", "rm", "-f", name], None, None, 30)
        started = self.runner(["docker", "run", "-d", "--rm", "--name", name, "--label", "cloudcaptain.grader=landing-zone", "-p", "127.0.0.1::5000", MOTO_IMAGE], None, None, 600)
        if started.code != 0:
            raise SetupError(f"could not start {MOTO_IMAGE}: {started.tail()}")
        self.container = name
        atexit.register(self.stop)
        port = self.runner(["docker", "port", name, "5000/tcp"], None, None, 30)
        mapped = [line.rsplit(":", 1)[-1] for line in port.out.splitlines() if ":" in line]
        if port.code != 0 or not mapped:
            self.stop()
            raise SetupError(f"docker port {name} 5000/tcp: {port.tail()}")
        endpoint = f"http://127.0.0.1:{mapped[0].strip()}"
        if not wait_until(lz_aws.Client(endpoint).is_moto, 90, 0.5):
            logs = self.runner(["docker", "logs", "--tail", "20", name], None, None, 30)
            self.stop()
            raise SetupError(f"moto did not answer on {endpoint} within 90s: {logs.tail()}")
        self.endpoint = endpoint
        return endpoint

    def reset(self) -> None:
        lz_aws.Client(self.start()).moto_reset()

    def stop(self) -> None:
        if self.container:
            self.runner(["docker", "rm", "-f", self.container], None, None, 60)
            self.container = ""
            self.endpoint = ""


_SHARED: List[Moto] = []


def shared_moto() -> Moto:
    if not _SHARED:
        _SHARED.append(Moto())
    return _SHARED[0]
