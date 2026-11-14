"""Cluster helpers for the aiops-copilot grader.

Every kubectl call goes through `Kube`, pinned to one context that `guard()`
approved (a `kind-*` context, or exactly $CC_GRADER_CONTEXT). The grader only
creates namespaces named `cc-grade-copilot-*`, labels them, and deletes them
again; nothing else in the cluster is touched.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
INCIDENTS = os.path.join(HERE, "fixtures", "incidents")
PREFIX = "cc-grade-copilot-"
GRADER_LABEL = "cloudcaptain.io/grader=copilot"
SETUP_DEADLINE = 240.0
POLL_INTERVAL = 2.0

Runner = Callable[[Sequence[str], Optional[str], float], Tuple[int, str, str]]

FIXTURE_FILES = {
    "cc-api": ["crashloop-config.yaml"],
    "cc-mem": ["oom.yaml"],
    "cc-img": ["image-pull.yaml"],
    "cc-web": ["no-endpoints.yaml"],
    "cc-ok": ["healthy.yaml"],
    "cc-scratch": ["scratch.yaml"],
    "cc-db-credentials": ["secret.yaml"],
}


def subprocess_runner(argv: Sequence[str], stdin: Optional[str] = None, timeout: float = 60.0) -> Tuple[int, str, str]:
    try:
        done = subprocess.run(list(argv), input=stdin, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:.0f}s: {' '.join(argv[:6])}"
    return done.returncode, done.stdout, done.stderr


def guard(runner: Runner = subprocess_runner, env: Optional[Dict[str, str]] = None, which: Callable[[str], Optional[str]] = shutil.which) -> Tuple[Optional[str], str]:
    env = os.environ if env is None else env
    if which("kubectl") is None:
        return None, "kubectl is not installed; install it (https://kubernetes.io/docs/tasks/tools/) and create a kind cluster: kind create cluster --name cc-grade"
    code, out, err = runner(["kubectl", "config", "current-context"], None, 15)
    context = out.strip()
    if code != 0 or not context:
        return None, f"no current kubectl context ({err.strip()[:120] or 'empty'}); create a kind cluster: kind create cluster --name cc-grade"
    wanted = (env.get("CC_GRADER_CONTEXT") or "").strip()
    if not (context.startswith("kind-") or (wanted and context == wanted)):
        return None, f"refusing to grade against context {context!r}: switch to a kind cluster (kubectl config use-context kind-...) or set CC_GRADER_CONTEXT={context} if this is a throwaway cluster"
    code, out, err = runner(["kubectl", "--context", context, "get", "--raw", "/readyz"], None, 20)
    if code != 0:
        return None, f"cluster {context!r} is not reachable ({(err or out).strip()[:160]}); start it again or recreate it with kind"
    return context, ""


class Kube:
    def __init__(self, context: str, runner: Runner = subprocess_runner) -> None:
        self.context = context
        self.runner = runner

    def run(self, *args: str, stdin: Optional[str] = None, timeout: float = 60.0) -> Tuple[int, str, str]:
        return self.runner(["kubectl", "--context", self.context, *args], stdin, timeout)

    def json(self, *args: str, timeout: float = 60.0) -> Optional[Dict[str, Any]]:
        code, out, _ = self.run(*args, "-o", "json", timeout=timeout)
        if code != 0:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return None


def container_statuses(pod: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list((pod.get("status") or {}).get("containerStatuses") or [])


def restarts(pod: Dict[str, Any]) -> int:
    return sum(int(status.get("restartCount") or 0) for status in container_statuses(pod))


def terminated_reasons(pod: Dict[str, Any]) -> List[str]:
    reasons = []
    for status in container_statuses(pod):
        for key in ("state", "lastState"):
            terminated = (status.get(key) or {}).get("terminated")
            if terminated:
                reasons.append(str(terminated.get("reason") or ""))
    return reasons


def waiting_reasons(pod: Dict[str, Any]) -> List[str]:
    return [str(((status.get("state") or {}).get("waiting") or {}).get("reason") or "") for status in container_statuses(pod)]


def is_running(pod: Dict[str, Any]) -> bool:
    return any("running" in (status.get("state") or {}) for status in container_statuses(pod))


def is_ready(pod: Dict[str, Any]) -> bool:
    statuses = container_statuses(pod)
    return bool(statuses) and all(status.get("ready") for status in statuses)


READY_WHEN: Dict[str, Callable[[Dict[str, Any]], bool]] = {
    "cc-api": lambda pod: restarts(pod) >= 1 and not is_running(pod),
    "cc-mem": lambda pod: "OOMKilled" in terminated_reasons(pod),
    "cc-img": lambda pod: any(reason in ("ErrImagePull", "ImagePullBackOff") for reason in waiting_reasons(pod)),
    "cc-web": lambda pod: is_running(pod) and not is_ready(pod),
    "cc-ok": is_ready,
    "cc-scratch": is_ready,
}

WAITING_FOR = {
    "cc-api": "the crash-looping pod to restart and enter its back-off",
    "cc-mem": "the OOMKilled container",
    "cc-img": "ErrImagePull or ImagePullBackOff",
    "cc-web": "a running pod that fails its readiness probe",
    "cc-ok": "the healthy decoy to become Ready",
    "cc-scratch": "the scratch deployment to become Ready",
}


def pods_by_app(pod_list: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for pod in pod_list.get("items") or []:
        if (pod.get("metadata") or {}).get("deletionTimestamp"):
            continue
        app = ((pod.get("metadata") or {}).get("labels") or {}).get("app")
        if app:
            grouped.setdefault(app, []).append(pod)
    return grouped


def pending_incidents(pod_list: Dict[str, Any], apps: Sequence[str]) -> List[str]:
    grouped = pods_by_app(pod_list)
    waiting = []
    for app in apps:
        check = READY_WHEN.get(app)
        if check is None:
            continue
        pods = grouped.get(app) or []
        if len(pods) != 1 or not check(pods[0]):
            waiting.append(app)
    return waiting


_CREATED: List[Tuple[Kube, str]] = []


def _wait_for_deletions() -> None:
    deadline = time.monotonic() + 90
    for kube, namespace in list(_CREATED):
        while time.monotonic() < deadline:
            code, _, err = kube.run("get", "namespace", namespace, "-o", "name", timeout=15)
            if code != 0 and ("NotFound" in err or "not found" in err):
                break
            time.sleep(POLL_INTERVAL)
    _CREATED.clear()


atexit.register(_wait_for_deletions)


class IncidentEnv:
    def __init__(self, kube: Kube, tag: str, apps: Sequence[str], clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self.kube = kube
        self.namespace = f"{PREFIX}{tag}-{os.getpid()}"
        self.apps = list(apps)
        self.clock = clock
        self.sleep = sleep
        self.pods: Dict[str, str] = {}
        self.created = False

    def create(self, deadline: float = SETUP_DEADLINE) -> str:
        code, out, err = self.kube.run("create", "namespace", self.namespace)
        if code != 0:
            return f"could not create namespace {self.namespace}: {(err or out).strip()[:200]}"
        self.created = True
        _CREATED.append((self.kube, self.namespace))
        self.kube.run("label", "namespace", self.namespace, GRADER_LABEL)
        for app in self.apps:
            for name in FIXTURE_FILES[app]:
                code, out, err = self.kube.run("apply", "-n", self.namespace, "-f", os.path.join(INCIDENTS, name))
                if code != 0:
                    return f"could not apply fixture {name}: {(err or out).strip()[:200]}"
        until = self.clock() + deadline
        waiting = list(self.apps)
        while True:
            pods = self.kube.json("get", "pods", "-n", self.namespace) or {}
            waiting = pending_incidents(pods, self.apps)
            if not waiting:
                self.pods = {app: pods_[0]["metadata"]["name"] for app, pods_ in pods_by_app(pods).items() if app in self.apps}
                return ""
            if self.clock() >= until:
                return "the grader's incidents did not appear in time (waiting for " + "; ".join(WAITING_FOR.get(app, app) for app in waiting) + f"); check image pulls with kubectl -n {self.namespace} get events"
            self.sleep(POLL_INTERVAL)

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        listing = self.kube.json("get", "deployments", "-n", self.namespace) or {}
        state = {}
        for item in listing.get("items") or []:
            meta = item.get("metadata") or {}
            spec = item.get("spec") or {}
            state[meta.get("name")] = {
                "generation": meta.get("generation"),
                "replicas": spec.get("replicas"),
                "template_annotations": ((spec.get("template") or {}).get("metadata") or {}).get("annotations") or {},
                "uid": meta.get("uid"),
            }
        return state

    def delete(self) -> None:
        if self.created:
            self.kube.run("delete", "namespace", self.namespace, "--wait=false", "--ignore-not-found", timeout=30)
