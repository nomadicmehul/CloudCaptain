from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from . import hooks, images, logs, manifest, releases, render
from .cluster import Cluster
from .runner import PaasError, Runner, run

DEFAULT_TIMEOUT = 180


def say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def emit(args: argparse.Namespace, data: Any, text: str) -> None:
    print(json.dumps(data, indent=2) if getattr(args, "json", False) else text)


class Paas:
    def __init__(self, data_dir: str, run: Runner = run, environ: Optional[Dict[str, str]] = None):
        self.environ = dict(os.environ if environ is None else environ)
        self.data_dir = os.path.abspath(data_dir)
        self.run = run
        self.store = releases.Store(self.data_dir)
        self.cluster = Cluster.from_env(run, self.environ)

    def base_image(self) -> str:
        return self.environ.get("TINYPAAS_BASE_IMAGE") or images.DEFAULT_BASE_IMAGE

    def build(self, app_dir: str) -> Dict[str, Any]:
        return images.build(app_dir, self.run, self.cluster.kind_name, self.base_image(), log=say)

    def replicas(self, state: Dict[str, Any], release: Dict[str, Any]) -> int:
        return int(state["formation"].get("web") or release["manifest"]["replicas"])

    def apply(self, state: Dict[str, Any], release: Dict[str, Any]) -> None:
        app = state["app"]
        ns = self.cluster.namespace(app)
        self.cluster.ensure_namespace(render.namespace(ns))
        self.cluster.apply(render.release_objects(app, ns, release, self.replicas(state, release)))

    def release(self, state: Dict[str, Any], release: Dict[str, Any], timeout: int) -> Dict[str, Any]:
        app = state["app"]
        ns = self.cluster.namespace(app)
        say(f"-----> Releasing v{release['number']} of {app} ({release['image']}) into {ns}")
        self.apply(state, release)
        ok, message = self.cluster.rollout(ns, app, timeout)
        previous = releases.current(state)
        if ok:
            release["status"] = releases.SUCCEEDED
            state["current"] = release["number"]
            self.store.save(state)
            say(f"-----> v{release['number']} is live")
            return release
        release["status"] = releases.FAILED
        self.store.save(state)
        if previous is not None:
            say(f"-----> v{release['number']} did not become ready ({message}); restoring v{previous['number']}")
            self.apply(state, previous)
            self.cluster.rollout(ns, app, timeout)
        raise PaasError(f"release v{release['number']} of {app} failed its rollout: {message}")

    def deploy(self, app_dir: str, timeout: int, expect_app: Optional[str]) -> Dict[str, Any]:
        report = manifest.validate(app_dir)
        if not report["valid"]:
            raise PaasError("app.json is invalid:\n" + manifest.describe_errors(report["errors"]))
        if expect_app and report["manifest"]["name"] != expect_app:
            raise PaasError(f"app.json names {report['manifest']['name']}, but this remote deploys {expect_app}")
        build = self.build(app_dir)
        state = self.store.load(build["app"])
        config = dict(build["manifest"]["env"])
        config.update(state["config"])
        release = self.store.add_release(state, build, config, f"Deploy {build['commit'][:images.SHORT_SHA]}")
        self.store.save(state)
        return self.release(state, release, timeout)

    def config_set(self, app: str, pairs: List[str], timeout: int) -> Dict[str, Any]:
        updates = parse_pairs(pairs)
        state = self.existing(app)
        live = releases.require_current(state)
        state["config"].update(updates)
        config = dict(live["config"])
        config.update(updates)
        release = self.store.add_release(state, live, config, "Set " + ", ".join(sorted(updates)))
        self.store.save(state)
        return self.release(state, release, timeout)

    def scale(self, app: str, formation: str, timeout: int) -> int:
        process, _, count = formation.partition("=")
        if process != "web" or not count.isdigit() or not 0 <= int(count) <= 10:
            raise PaasError(f"expected web=N with N between 0 and 10, got {formation}")
        state = self.existing(app)
        releases.require_current(state)
        state["formation"]["web"] = int(count)
        self.store.save(state)
        ns = self.cluster.namespace(app)
        self.cluster.scale(ns, app, int(count))
        ok, message = self.cluster.rollout(ns, app, timeout)
        if not ok:
            raise PaasError(f"scaled {app} to web={count}, but not every pod became ready: {message}")
        return int(count)

    def rollback(self, app: str, to: Optional[int], timeout: int) -> Dict[str, Any]:
        state = self.existing(app)
        target = releases.rollback_target(state, to)
        release = self.store.add_release(state, target, target["config"], f"Rollback to v{target['number']}", rollback_of=target["number"])
        state["config"] = {k: v for k, v in target["config"].items() if target["manifest"]["env"].get(k) != v}
        self.store.save(state)
        return self.release(state, release, timeout)

    def url(self, app: str) -> str:
        self.existing(app)
        ns = self.cluster.namespace(app)
        return f"http://{self.cluster.node_address()}:{self.cluster.node_port(ns, app)}"

    def logs(self, app: str, since: str) -> List[Dict[str, str]]:
        if not logs.valid_since(since):
            raise PaasError(f"--since takes a duration such as 30s, 5m or 1h, got {since}")
        self.existing(app)
        ns = self.cluster.namespace(app)
        return logs.merge({pod: self.cluster.pod_logs(ns, pod, since) for pod in self.cluster.pods(ns, app)})

    def remote_add(self, app: str, bare_path: str) -> Dict[str, str]:
        if not manifest.NAME_RE.match(app):
            raise PaasError(f"{app} is not a valid app name")
        env = {key: self.environ[key] for key in ("PATH", "TINYPAAS_CONTEXT", "TINYPAAS_KIND_CLUSTER", "TINYPAAS_NAMESPACE_PREFIX", "TINYPAAS_BASE_IMAGE") if self.environ.get(key)}
        env["TINYPAAS_HOME"] = self.data_dir
        tinypaas = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "tinypaas")
        hook = hooks.install(bare_path, hooks.post_receive(sys.executable, tinypaas, app, env), self.run)
        state = self.store.load(app)
        state["remote"] = os.path.abspath(bare_path)
        self.store.save(state)
        return {"app": app, "remote": os.path.abspath(bare_path), "hook": hook}

    def existing(self, app: str) -> Dict[str, Any]:
        if not self.store.exists(app):
            raise PaasError(f"tinypaas knows no app called {app}: deploy it first")
        return self.store.load(app)


def parse_pairs(pairs: List[str]) -> Dict[str, str]:
    updates = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not manifest.ENV_KEY_RE.match(key):
            raise PaasError(f"expected KEY=VALUE with an uppercase KEY, got {pair}")
        updates[key] = value
    return updates


def release_line(release: Dict[str, Any], live: Optional[int]) -> str:
    mark = "*" if release["number"] == live else " "
    return f"{mark} v{release['number']:<4} {release['status']:<10} {release['time']}  {release['image']:<40} {release['description']}"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tinypaas", description="A tiny Heroku-style PaaS on a kind cluster.")
    p.add_argument("--data-dir", help="where .tinypaas/ state lives (default: $TINYPAAS_HOME or your home directory)")
    sub = p.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str, json_flag: bool = True, timeout: bool = False) -> argparse.ArgumentParser:
        cmd = sub.add_parser(name, help=help_text)
        if json_flag:
            cmd.add_argument("--json", action="store_true", help="print JSON")
        if timeout:
            cmd.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds to wait for the rollout")
        return cmd

    command("validate", "check app.json").add_argument("app_dir")
    command("build", "build and load the image for HEAD").add_argument("app_dir")
    deploy = command("deploy", "build, release and roll out HEAD", timeout=True)
    deploy.add_argument("app_dir")
    deploy.add_argument("--app", help="refuse to deploy unless app.json names this app")
    command("url", "print the app's URL").add_argument("app")
    remote = command("remote", "git remotes that deploy on push")
    remote_sub = remote.add_subparsers(dest="remote_command", required=True)
    remote_add = remote_sub.add_parser("add", help="create a bare repository whose main branch deploys")
    remote_add.add_argument("app")
    remote_add.add_argument("path")
    remote_add.add_argument("--json", action="store_true")
    config = command("config", "app configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_set = config_sub.add_parser("set", help="set KEY=VALUE and release")
    config_set.add_argument("app")
    config_set.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    config_set.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    config_set.add_argument("--json", action="store_true")
    config_get = config_sub.add_parser("get", help="show the live configuration")
    config_get.add_argument("app")
    config_get.add_argument("--json", action="store_true")
    scale = command("scale", "set the number of web processes", timeout=True)
    scale.add_argument("app")
    scale.add_argument("formation", metavar="web=N")
    command("releases", "list releases").add_argument("app")
    rollback = command("rollback", "release an earlier release again", timeout=True)
    rollback.add_argument("app")
    rollback.add_argument("--to", type=int, help="release number (default: the last good one before the current)")
    logs_cmd = command("logs", "logs from every pod, merged by time")
    logs_cmd.add_argument("app")
    logs_cmd.add_argument("--since", default="5m")
    return p


def dispatch(paas: Paas, args: argparse.Namespace) -> int:
    if args.command == "validate":
        report = manifest.validate(args.app_dir)
        data = {k: report[k] for k in ("app_dir", "valid", "errors")}
        text = "app.json is valid" if report["valid"] else "app.json is invalid:\n" + manifest.describe_errors(report["errors"])
        emit(args, data, text)
        return 0 if report["valid"] else 1
    if args.command == "build":
        build = paas.build(args.app_dir)
        emit(args, {k: build[k] for k in ("app", "image", "commit", "digest")}, build["image"])
        return 0
    if args.command == "deploy":
        release = paas.deploy(args.app_dir, args.timeout, args.app)
        emit(args, {"app": release["manifest"]["name"], "release": release["number"], "image": release["image"], "status": release["status"]}, f"v{release['number']} {release['status']}")
        return 0
    if args.command == "url":
        url = paas.url(args.app)
        emit(args, {"app": args.app, "url": url}, url)
        return 0
    if args.command == "remote":
        result = paas.remote_add(args.app, args.path)
        emit(args, result, f"git remote ready: git push {result['remote']} main")
        return 0
    if args.command == "config" and args.config_command == "set":
        release = paas.config_set(args.app, args.pairs, args.timeout)
        emit(args, {"app": args.app, "release": release["number"], "status": release["status"]}, f"v{release['number']} {release['status']}")
        return 0
    if args.command == "config":
        live = releases.require_current(paas.existing(args.app))
        emit(args, {"app": args.app, "config": live["config"]}, "\n".join(f"{k}={v}" for k, v in sorted(live["config"].items())))
        return 0
    if args.command == "scale":
        count = paas.scale(args.app, args.formation, args.timeout)
        emit(args, {"app": args.app, "web": count}, f"{args.app}: web={count}")
        return 0
    if args.command == "releases":
        state = paas.existing(args.app)
        data = {"app": args.app, "current": state["current"], "releases": [releases.public(r) for r in state["releases"]]}
        emit(args, data, "\n".join(release_line(r, state["current"]) for r in reversed(state["releases"])))
        return 0
    if args.command == "rollback":
        release = paas.rollback(args.app, args.to, args.timeout)
        emit(args, {"app": args.app, "release": release["number"], "rollback_of": release["rollback_of"], "status": release["status"]}, f"v{release['number']} restores v{release['rollback_of']}")
        return 0
    lines = paas.logs(args.app, args.since)
    emit(args, {"app": args.app, "since": args.since, "lines": lines}, "\n".join(f"{e['time']} {e['pod']} {e['line']}" for e in lines))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    data_dir = args.data_dir or os.environ.get("TINYPAAS_HOME") or os.path.expanduser("~")
    try:
        return dispatch(Paas(data_dir), args)
    except PaasError as exc:
        say(f"tinypaas: {exc}")
        return 1
