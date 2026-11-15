"""Helpers for the build-a-paas checks: running tools, the cluster guard,
fixture repositories, HTTP through the kind node, and cleanup.

Everything the grader creates is named cc-grade-paas-* (namespaces, temp
dirs) or tinypaas/ccg-* (images of the grader's own apps) and is removed
when a check ends. Python 3.9+, standard library only.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

PREFIX = "cc-grade-paas"
HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
APPS = os.path.join(FIXTURES, "apps")
CASES = os.path.join(FIXTURES, "manifests", "cases.json")
EPOCH = 1767225600
TIMESTAMP_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class Fail(Exception):
    pass


@dataclass
class Proc:
    code: int
    out: str
    err: str


def run(args: List[str], timeout: float = 60, env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None, input: Optional[str] = None) -> Proc:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd, input=input)
    except FileNotFoundError:
        return Proc(127, "", f"{args[0]}: command not found")
    except PermissionError:
        return Proc(126, "", f"{args[0]}: permission denied")
    except subprocess.TimeoutExpired:
        return Proc(124, "", f"timed out after {timeout:g}s")
    return Proc(done.returncode, done.stdout, done.stderr)


RUNNER: Callable[..., Proc] = run
SLEEP: Callable[[float], None] = time.sleep
CLOCK: Callable[[], float] = time.monotonic


def call(args: List[str], timeout: float = 60, **kwargs: Any) -> Proc:
    return RUNNER(args, timeout=timeout, **kwargs)


def attempt(fn: Callable[..., str], *args: Any) -> Tuple[bool, str]:
    try:
        return True, fn(*args)
    except Fail as exc:
        return False, str(exc)


def tail(text: str, limit: int = 300) -> str:
    flat = " | ".join(line.strip() for line in (text or "").strip().splitlines() if line.strip())
    return flat if len(flat) <= limit else "..." + flat[-limit:]


def parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def poll(probe: Callable[[], Tuple[bool, Any]], timeout: float, interval: float = 1.0) -> Tuple[bool, Any]:
    deadline = CLOCK() + timeout
    while True:
        ok, value = probe()
        if ok or CLOCK() >= deadline:
            return ok, value
        SLEEP(interval)


def holds(probe: Callable[[], Tuple[bool, Any]], seconds: float, interval: float = 1.0) -> Tuple[bool, Any]:
    deadline = CLOCK() + seconds
    while True:
        ok, value = probe()
        if not ok or CLOCK() >= deadline:
            return ok, value
        SLEEP(interval)


def parse_timestamp(text: str) -> Tuple[int, int]:
    match = TIMESTAMP_RE.match(text or "")
    if not match:
        raise ValueError(f"not an RFC 3339 timestamp: {text!r}")
    fields = [int(match.group(i)) for i in range(1, 7)]
    seconds = calendar.timegm((*fields, 0, 0, 0))
    zone = match.group(8)
    if zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        seconds -= sign * (int(zone[1:3]) * 3600 + int(zone[4:6]) * 60)
    return seconds, int((match.group(7) or "").ljust(9, "0"))


def cpu_millis(quantity: Any) -> Optional[int]:
    text = str(quantity or "")
    try:
        return int(text[:-1]) if text.endswith("m") else int(round(float(text) * 1000))
    except ValueError:
        return None


MEMORY_UNITS = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "k": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "": 1}


def memory_bytes(quantity: Any) -> Optional[int]:
    match = re.match(r"^([0-9]+)(Ki|Mi|Gi|k|M|G)?$", str(quantity or ""))
    return int(match.group(1)) * MEMORY_UNITS[match.group(2) or ""] if match else None


def short_image(ref: str) -> str:
    for prefix in ("docker.io/library/", "docker.io/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def executable(project: str) -> str:
    path = os.path.join(os.path.abspath(project), "bin", "tinypaas")
    if not os.path.isfile(path):
        raise Fail("bin/tinypaas is missing: copy starter/bin/tinypaas into your project (the grader runs <project>/bin/tinypaas)")
    if not os.access(path, os.X_OK):
        raise Fail("bin/tinypaas is not executable: run chmod +x bin/tinypaas and commit it")
    return path


def manifest_cases() -> List[Dict[str, Any]]:
    with open(CASES, encoding="utf-8") as f:
        return json.load(f)["cases"]


def materialize(files: Dict[str, Any], dest: str) -> str:
    for rel, content in files.items():
        path = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content if isinstance(content, str) else json.dumps(content, indent=2) + "\n")
    os.makedirs(dest, exist_ok=True)
    return dest


def compare_case(case: Dict[str, Any], code: int, data: Any) -> List[str]:
    cid = case["id"]
    if not isinstance(data, dict) or not isinstance(data.get("valid"), bool) or not isinstance(data.get("errors"), list):
        return [f"case {cid}: --json must print an object with valid (true/false) and errors ([{{path, message}}])"]
    if not all(isinstance(e, dict) and isinstance(e.get("path"), str) for e in data["errors"]):
        return [f"case {cid}: every error needs a path string such as $.port"]
    problems = []
    if data["valid"] != case["valid"]:
        problems.append(f"case {cid}: expected valid={str(case['valid']).lower()}, got {str(data['valid']).lower()}")
    want_code = 0 if case["valid"] else 1
    if code != want_code:
        problems.append(f"case {cid}: exit {code}, expected {want_code}")
    got = {e["path"] for e in data["errors"]}
    want = set(case["paths"])
    if got != want:
        problems.append(f"case {cid}: expected paths {sorted(want)}, got {sorted(got)}")
    return problems


@dataclass
class Target:
    context: str
    cluster: str
    nodes: List[str]


_TARGET: Optional[Target] = None


def which(tool: str) -> bool:
    return shutil.which(tool) is not None


def target() -> Target:
    global _TARGET
    if _TARGET is not None:
        return _TARGET
    for tool, hint in (("docker", "install Docker"), ("kubectl", "install kubectl"), ("kind", "install kind (https://kind.sigs.k8s.io)"), ("git", "install git")):
        if not which(tool):
            raise Fail(f"{tool} is not on PATH: {hint}; this milestone needs docker, kind, kubectl and git")
    info = call(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30)
    if info.code != 0:
        raise Fail(f"the Docker daemon is not reachable: {tail(info.err)}")
    current = call(["kubectl", "config", "current-context"], timeout=20)
    context = current.out.strip()
    if current.code != 0 or not context:
        raise Fail("no current kubectl context: create one with kind create cluster --name tinypaas")
    allowed = os.environ.get("CC_GRADER_CONTEXT", "")
    if not context.startswith("kind-") and context != allowed:
        raise Fail(f"refusing to grade against context {context}: switch to a kind cluster (kubectl config use-context kind-<name>) or set CC_GRADER_CONTEXT={context} if it is a throwaway cluster")
    cluster = os.environ.get("CC_GRADER_KIND_CLUSTER") or (context[len("kind-"):] if context.startswith("kind-") else "")
    if not cluster:
        raise Fail(f"context {context} is not named kind-<cluster>: set CC_GRADER_KIND_CLUSTER to the kind cluster name")
    listed = call(["kind", "get", "nodes", "--name", cluster], timeout=30)
    nodes = [n.strip() for n in listed.out.splitlines() if n.strip()]
    if listed.code != 0 or not nodes:
        raise Fail(f"kind has no nodes for cluster {cluster}: create it with kind create cluster --name {cluster}")
    ready = call(["kubectl", "--context", context, "get", "--raw", "/readyz"], timeout=30)
    if ready.code != 0:
        raise Fail(f"the API server of {context} does not answer /readyz: {tail(ready.err)}")
    _TARGET = Target(context, cluster, nodes)
    return _TARGET


def reset_target() -> None:
    global _TARGET
    _TARGET = None
    _ROUTES.clear()


_ROUTES: Dict[str, str] = {}


def route_for(host: str, port: int) -> str:
    if host in _ROUTES:
        return _ROUTES[host]
    try:
        socket.create_connection((host, port), timeout=2).close()
        route = "direct"
    except ConnectionRefusedError:
        route = "direct"
    except OSError:
        route = "node"
    _ROUTES[host] = route
    return route


def fetch_direct(url: str, timeout: float) -> Tuple[Optional[int], str]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        return None, str(getattr(exc, "reason", exc))


def fetch_via_node(node: str, url: str, timeout: float) -> Tuple[Optional[int], str]:
    proc = call(["docker", "exec", node, "curl", "-sS", "-m", str(int(timeout)), "-w", "\n%{http_code}", url], timeout=timeout + 15)
    if proc.code != 0:
        return None, tail(proc.err or proc.out)
    body, _, status = proc.out.rpartition("\n")
    return (int(status), body) if status.isdigit() else (None, proc.out)


def fetch(url: str, timeout: float = 5) -> Tuple[Optional[int], str]:
    parsed = urlparse(url)
    if parsed.scheme != "http" or not parsed.hostname:
        return None, f"not an http:// URL: {url}"
    if route_for(parsed.hostname, parsed.port or 80) == "direct":
        return fetch_direct(url, timeout)
    return fetch_via_node(target().nodes[0], url, timeout)


class Session:
    def __init__(self, project: str, apps: Iterable[str]):
        self.tinypaas = executable(project)
        self.target = target()
        self.apps = list(apps)
        self.work = tempfile.mkdtemp(prefix=f"{PREFIX}-")
        self.home = os.path.join(self.work, "home")
        os.makedirs(self.home)
        self.commits = 0
        empty = os.path.join(self.work, "gitconfig")
        open(empty, "w").close()
        self.git_env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=empty,
                            GIT_AUTHOR_NAME="CloudCaptain Grader", GIT_AUTHOR_EMAIL="grader@cloudcaptain.invalid",
                            GIT_COMMITTER_NAME="CloudCaptain Grader", GIT_COMMITTER_EMAIL="grader@cloudcaptain.invalid")
        self.env = dict(os.environ, TINYPAAS_HOME=self.home, TINYPAAS_NAMESPACE_PREFIX=PREFIX,
                        TINYPAAS_CONTEXT=self.target.context, TINYPAAS_KIND_CLUSTER=self.target.cluster)

    def __enter__(self) -> "Session":
        for app in self.apps:
            self.delete_namespace(app, wait=True)
        return self

    def __exit__(self, *exc: Any) -> None:
        for app in self.apps:
            self.delete_namespace(app, wait=True)
            self.remove_images(app)
        shutil.rmtree(self.work, ignore_errors=True)

    def namespace(self, app: str) -> str:
        ns = f"{PREFIX}-{app}"
        if not ns.startswith(f"{PREFIX}-"):
            raise Fail(f"refusing to touch namespace {ns}")
        return ns

    def kubectl(self, *args: str, timeout: float = 60) -> Proc:
        return call(["kubectl", "--context", self.target.context, *args], timeout=timeout)

    def delete_namespace(self, app: str, wait: bool) -> None:
        ns = self.namespace(app)
        if self.kubectl("get", "namespace", ns, "-o", "name").code != 0:
            return
        self.kubectl("delete", "namespace", ns, "--ignore-not-found", f"--wait={'true' if wait else 'false'}", "--timeout=120s", timeout=150)

    def remove_images(self, app: str) -> None:
        mine = f"tinypaas/{app}:"
        listed = call(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"], timeout=30)
        refs = [r for r in listed.out.split() if short_image(r).startswith(mine)]
        if refs:
            call(["docker", "image", "rm", "-f", *refs], timeout=60)
        for node in self.target.nodes:
            for ref in self.node_images(node):
                if short_image(ref).startswith(mine):
                    call(["docker", "exec", node, "crictl", "rmi", ref], timeout=60)

    def node_images(self, node: str) -> List[str]:
        proc = call(["docker", "exec", node, "crictl", "images", "-o", "json"], timeout=60)
        data = parse_json(proc.out) if proc.code == 0 else None
        return [tag for image in (data or {}).get("images", []) for tag in image.get("repoTags") or []]

    def tp(self, args: List[str], timeout: float) -> Proc:
        return call([self.tinypaas, *args], timeout=timeout, env=self.env)

    def tp_ok(self, args: List[str], timeout: float, hint: str = "") -> Proc:
        proc = self.tp(args, timeout)
        if proc.code != 0:
            raise Fail(f"tinypaas {' '.join(self.short_args(args))} exited {proc.code}: {tail(proc.err or proc.out)}{hint}")
        return proc

    def tp_json(self, args: List[str], timeout: float, hint: str = "") -> Dict[str, Any]:
        proc = self.tp_ok(args, timeout, hint)
        data = parse_json(proc.out)
        if not isinstance(data, dict):
            raise Fail(f"tinypaas {' '.join(self.short_args(args))} must print one JSON object on stdout, got: {tail(proc.out)}")
        return data

    def short_args(self, args: List[str]) -> List[str]:
        return [os.path.basename(a) if a.startswith(self.work) else a for a in args]

    def git(self, repo: str, *args: str, timeout: float = 60) -> Proc:
        proc = call(["git", "-C", repo, *args], timeout=timeout, env=self.git_env)
        if proc.code != 0:
            raise Fail(f"git {' '.join(args[:2])} failed: {tail(proc.err or proc.out)}")
        return proc

    def make_app(self, app: str, base: str = "hello", overlay: Optional[str] = None, files: Optional[Dict[str, str]] = None, manifest: Optional[Dict[str, Any]] = None) -> str:
        repo = os.path.join(self.work, "apps", app)
        shutil.copytree(os.path.join(APPS, base), repo)
        if overlay:
            for name in os.listdir(os.path.join(APPS, overlay)):
                shutil.copy(os.path.join(APPS, overlay, name), os.path.join(repo, name))
        proc = call(["git", "init", "--quiet", "-b", "main", repo], timeout=30, env=self.git_env)
        if proc.code != 0:
            raise Fail(f"git init failed: {tail(proc.err)}")
        self.commit(repo, files or {}, f"Create {app}", dict(manifest or {}, name=app))
        return repo

    def commit(self, repo: str, files: Dict[str, str], message: str, manifest: Optional[Dict[str, Any]] = None) -> str:
        materialize(files, repo)
        if manifest:
            path = os.path.join(repo, "app.json")
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data.update(manifest)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
        self.commits += 1
        stamp = f"{EPOCH + self.commits} +0000"
        self.git_env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "--quiet", "--no-verify", "-m", message)
        return self.git(repo, "rev-parse", "HEAD").out.strip()

    def push(self, repo: str, remote: str, branch: str) -> Proc:
        proc = call(["git", "-C", repo, "push", remote, f"{branch}:{branch}"], timeout=600, env=self.git_env)
        if proc.code != 0:
            raise Fail(f"git push {branch} to the tinypaas remote failed: {tail(proc.err)}")
        return proc

    def url(self, app: str) -> str:
        data = self.tp_json(["url", app, "--json"], timeout=60)
        url = data.get("url")
        if not isinstance(url, str) or not url.startswith("http://"):
            raise Fail(f"tinypaas url {app} --json must print {{\"url\": \"http://...\"}}, got {data}")
        return url.rstrip("/")

    def wait_body(self, url: str, expected: str, timeout: float) -> None:
        def probe() -> Tuple[bool, Any]:
            status, body = fetch(url)
            return status == 200 and body.strip() == expected, (status, body)
        ok, (status, body) = poll(probe, timeout)
        if not ok:
            raise Fail(f"{url} did not answer 200 {expected!r} within {timeout:g}s; last answer: {status} {tail(body, 120)!r}")

    def deployment(self, app: str) -> Dict[str, Any]:
        ns = self.namespace(app)
        proc = self.kubectl("-n", ns, "get", "deployments", "-o", "json")
        items = (parse_json(proc.out) or {}).get("items", []) if proc.code == 0 else []
        named = [d for d in items if d["metadata"]["name"] == app]
        if named or len(items) == 1:
            return (named or items)[0]
        if not items:
            raise Fail(f"no Deployment in namespace {ns}: deploy into TINYPAAS_NAMESPACE_PREFIX-<app> ({ns})")
        raise Fail(f"{len(items)} Deployments in {ns} and none is named {app}")

    def ready_pods(self, deployment: Dict[str, Any]) -> List[str]:
        labels = deployment["spec"]["selector"].get("matchLabels", {})
        selector = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        proc = self.kubectl("-n", deployment["metadata"]["namespace"], "get", "pods", "-l", selector, "-o", "json")
        pods = (parse_json(proc.out) or {}).get("items", []) if proc.code == 0 else []
        return sorted(p["metadata"]["name"] for p in pods if is_ready(p))

    def image_tags(self, app: str) -> List[str]:
        listed = call(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"], timeout=30)
        mine = f"tinypaas/{app}:"
        return [short_image(r)[len(mine):] for r in listed.out.split() if short_image(r).startswith(mine)]


def is_ready(pod: Dict[str, Any]) -> bool:
    if pod["metadata"].get("deletionTimestamp"):
        return False
    conditions = pod.get("status", {}).get("conditions", [])
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def first_container(deployment: Dict[str, Any]) -> Dict[str, Any]:
    containers = deployment["spec"]["template"]["spec"].get("containers", [])
    if not containers:
        raise Fail("the Deployment has no containers")
    return containers[0]


def releases_of(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = data.get("releases")
    keys = ("number", "image", "config_hash", "time", "status")
    if not isinstance(items, list) or "current" not in data:
        raise Fail("tinypaas releases --json must print {\"current\": N, \"releases\": [...]}")
    for item in items:
        if not isinstance(item, dict) or any(k not in item for k in keys):
            raise Fail(f"every release needs {', '.join(keys)}; got {tail(json.dumps(item), 160)}")
        try:
            parse_timestamp(item["time"])
        except ValueError:
            raise Fail(f"release {item.get('number')} time {item['time']!r} is not RFC 3339 (2026-11-13T10:00:00Z)")
    numbers = [item["number"] for item in items]
    if numbers != list(range(1, len(items) + 1)):
        raise Fail(f"releases must be numbered 1, 2, 3, ... in order; got {numbers}")
    return items
