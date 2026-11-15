"""Milestone checks for capstones/build-a-paas.

One function per milestone in ../lesson.json: milestone `some-id` is checked by
`check_some_id(project)`, which gets the project folder and returns
(passed, detail). The grader drives <project>/bin/tinypaas only through its
documented interface (subcommands, --json output, TINYPAAS_* variables) and
looks at the cluster with kubectl and docker. Python 3.9+, standard library only.

Safety: cluster checks run only when the current kubectl context starts with
kind- (or equals $CC_GRADER_CONTEXT), touch only namespaces named
cc-grade-paas-* and images named tinypaas/ccg-*, and delete them when they end.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import paas_kit as kit  # noqa: E402
from paas_kit import Fail, Session  # noqa: E402

Result = Tuple[bool, str]
DEPLOY_TIMEOUT = "180"
TOKEN = "cc-grade-7f3a91c2"
MARKER_RE = re.compile(r"marker=(ccg-\d{3})\b.*\bpod=(\S+)")


def check_app_manifest(project: str) -> Result:
    return kit.attempt(app_manifest, project)


def check_build_image(project: str) -> Result:
    return kit.attempt(build_image, project)


def check_deploy(project: str) -> Result:
    return kit.attempt(deploy, project)


def check_git_push(project: str) -> Result:
    return kit.attempt(git_push, project)


def check_config_scale(project: str) -> Result:
    return kit.attempt(config_scale, project)


def check_releases_rollback(project: str) -> Result:
    return kit.attempt(releases_rollback, project)


def check_logs(project: str) -> Result:
    return kit.attempt(logs, project)


def app_manifest(project: str) -> str:
    tinypaas = kit.executable(project)
    cases = kit.manifest_cases()
    work = tempfile.mkdtemp(prefix=f"{kit.PREFIX}-")
    env = dict(os.environ, TINYPAAS_HOME=work, TINYPAAS_NAMESPACE_PREFIX=kit.PREFIX)
    try:
        problems: List[str] = []
        for case in cases:
            folder = kit.materialize(case["files"], os.path.join(work, case["id"]))
            proc = kit.call([tinypaas, "validate", folder, "--json"], timeout=30, env=env)
            data = kit.parse_json(proc.out)
            if data is None:
                raise Fail(f"tinypaas validate <dir> --json printed no JSON for case {case['id']} (exit {proc.code}): {kit.tail(proc.err or proc.out)}")
            problems.extend(kit.compare_case(case, proc.code, data))
        if problems:
            raise Fail(f"{len(problems)} wrong verdict(s): " + "; ".join(problems[:3]) + " (cases: grader/fixtures/manifests/cases.json)")
        valid = sum(1 for c in cases if c["valid"])
        return f"{len(cases)} manifests: {valid} accepted, {len(cases) - valid} rejected with exactly the expected JSON paths"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def verify_build(session: Session, app: str, commit: str, data: Dict[str, Any]) -> str:
    image, digest = data.get("image"), data.get("digest")
    if not isinstance(image, str) or not isinstance(digest, str):
        raise Fail(f"tinypaas build --json must print {{\"image\": ..., \"digest\": ...}}; got {kit.tail(json.dumps(data), 160)}")
    name, _, tag = kit.short_image(image).rpartition(":")
    if name != f"tinypaas/{app}":
        raise Fail(f"{app} was tagged {image}; the repository must be tinypaas/{app}")
    if len(tag) < 7 or not commit.startswith(tag):
        raise Fail(f"tag {tag} of {image} is not the short sha of the commit it was built from ({commit[:7]})")
    inspect = kit.call(["docker", "image", "inspect", "--format", "{{.Id}}", image], timeout=30)
    if inspect.code != 0:
        raise Fail(f"{image} is not in the local Docker image store: {kit.tail(inspect.err)}")
    image_id = inspect.out.strip()
    if not kit.SHA256_RE.match(digest) or digest != image_id:
        raise Fail(f"digest {digest} of {image} should be its content digest as docker image inspect --format '{{{{.Id}}}}' prints it ({image_id})")
    for node in session.target.nodes:
        if f"tinypaas/{app}:{tag}" not in {kit.short_image(r) for r in session.node_images(node)}:
            raise Fail(f"{image} is not in the containerd store of kind node {node}: load it with kind load docker-image {image} --name {session.target.cluster}")
    return f"tinypaas/{app}:{tag}"


def build_image(project: str) -> str:
    apps = [("ccg-hello", "hello", None), ("ccg-custom", "hello", "custom"), ("ccg-site", "site", None)]
    with Session(project, [a for a, _, _ in apps]) as s:
        built = {}
        repos = {}
        for app, base, overlay in apps:
            repos[app] = s.make_app(app, base, overlay)
            commit = s.git(repos[app], "rev-parse", "HEAD").out.strip()
            data = s.tp_json(["build", repos[app], "--json"], timeout=600)
            built[app] = verify_build(s, app, commit, data)
        label = kit.call(["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", built["ccg-custom"]], timeout=30)
        if (kit.parse_json(label.out) or {}).get("cc.fixture") != "custom":
            raise Fail("ccg-custom has its own Dockerfile (LABEL cc.fixture=custom) but its image lacks that label: build with the app's Dockerfile when one exists")
        second = s.commit(repos["ccg-hello"], {"body.txt": "second commit\n"}, "Change the body")
        again = verify_build(s, "ccg-hello", second, s.tp_json(["build", repos["ccg-hello"], "--json"], timeout=600))
        if again == built["ccg-hello"]:
            raise Fail(f"a new commit must give a new tag, but both builds produced {again}")
        return f"built and loaded into kind: {', '.join(built.values())}; the next commit became {again}"


def deploy(project: str) -> str:
    app = "ccg-deploy"
    with Session(project, [app]) as s:
        repo = s.make_app(app, files={"body.txt": "deployed by tinypaas\n"})
        s.tp_ok(["deploy", repo, "--timeout", DEPLOY_TIMEOUT], timeout=600)
        url = s.url(app)
        s.wait_body(url, "deployed by tinypaas", 90)
        container = kit.first_container(s.deployment(app))
        readiness = (container.get("readinessProbe") or {}).get("httpGet") or {}
        if readiness.get("path") != "/healthz":
            raise Fail(f"the live Deployment needs readinessProbe.httpGet.path /healthz (app.json health), found {readiness.get('path')!r}")
        if not container.get("livenessProbe"):
            raise Fail("the live Deployment has no livenessProbe: render one from app.json health")
        limits = (container.get("resources") or {}).get("limits") or {}
        if kit.cpu_millis(limits.get("cpu")) != 100 or kit.memory_bytes(limits.get("memory")) != 64 * 1024 ** 2:
            raise Fail(f"resources.limits should be cpu 100m and memory 64Mi from app.json, found {limits}")
        return f"{url} answers 200 with the app's body; readiness /healthz, liveness and limits cpu=100m memory=64Mi are live"


def git_push(project: str) -> str:
    app = "ccg-push"
    with Session(project, [app]) as s:
        repo = s.make_app(app, files={"body.txt": "pushed v1\n"})
        bare = os.path.join(s.work, "remotes", f"{app}.git")
        s.tp_ok(["remote", "add", app, bare, "--json"], timeout=60)
        hook = os.path.join(bare, "hooks", "post-receive")
        if not os.access(hook, os.X_OK):
            raise Fail(f"tinypaas remote add {app} <path> must create a bare repository at <path> with an executable hooks/post-receive")
        s.push(repo, bare, "main")
        url = s.url(app)
        s.wait_body(url, "pushed v1", 120)
        s.commit(repo, {"body.txt": "pushed v2\n"}, "Second release")
        s.push(repo, bare, "main")
        s.wait_body(url, "pushed v2", 120)
        before = s.deployment(app)
        s.git(repo, "checkout", "--quiet", "-b", "feature")
        feature = s.commit(repo, {"body.txt": "pushed from a feature branch\n"}, "Feature work")
        s.push(repo, bare, "feature")

        def unchanged() -> Tuple[bool, Any]:
            status, body = kit.fetch(url)
            now = s.deployment(app)
            same = now["metadata"]["generation"] == before["metadata"]["generation"]
            return same and status == 200 and body.strip() == "pushed v2", (status, body, same)
        ok, seen = kit.holds(unchanged, 8)
        if not ok:
            raise Fail(f"a push to feature changed the app (answer {seen[0]} {kit.tail(seen[1], 80)!r}, Deployment unchanged: {seen[2]}): only pushes to main may deploy")
        if any(feature.startswith(tag) for tag in s.image_tags(app)):
            raise Fail("a push to feature built an image: the hook must ignore every ref except refs/heads/main")
        return "pushes to main deployed v1 then v2 within the deadline; a push to feature built and deployed nothing"


def config_scale(project: str) -> str:
    app = "ccg-config"
    with Session(project, [app]) as s:
        repo = s.make_app(app)
        s.tp_ok(["deploy", repo, "--timeout", DEPLOY_TIMEOUT], timeout=600, hint=" (config and scale need a deployed app)")
        s.tp_ok(["config", "set", app, f"CC_GRADE_TOKEN={TOKEN}"], timeout=600)
        url = s.url(app)

        def env_has_token() -> Tuple[bool, Any]:
            status, body = kit.fetch(url + "/env")
            env = kit.parse_json(body) if status == 200 else None
            return isinstance(env, dict) and env.get("CC_GRADE_TOKEN") == TOKEN, (status, body)
        ok, (status, body) = kit.poll(env_has_token, 90)
        if not ok:
            raise Fail(f"after config set, {url}/env should show CC_GRADE_TOKEN={TOKEN}; last answer {status} {kit.tail(body, 120)!r}")
        s.tp_ok(["scale", app, "web=3"], timeout=600)

        def three_ready() -> Tuple[bool, Any]:
            deployment = s.deployment(app)
            pods = s.ready_pods(deployment)
            return deployment["spec"].get("replicas") == 3 and len(pods) == 3, (deployment["spec"].get("replicas"), pods)
        ok, (replicas, pods) = kit.poll(three_ready, 120)
        if not ok:
            raise Fail(f"scale {app} web=3 should give replicas 3 and 3 ready pods; found replicas {replicas} and {len(pods)} ready")
        return "/env shows the value set with config set; web=3 gave 3 ready pods"


def latest(data: Dict[str, Any]) -> Dict[str, Any]:
    items = kit.releases_of(data)
    if not items:
        raise Fail("tinypaas releases --json lists no releases after a deploy")
    return items[-1]


def releases_rollback(project: str) -> str:
    app = "ccg-release"
    with open(os.path.join(kit.APPS, "unready", "health.txt"), encoding="utf-8") as f:
        unhealthy = f.read()
    with Session(project, [app]) as s:
        listing = ["releases", app, "--json"]
        repo = s.make_app(app, files={"body.txt": "release one\n"}, manifest={"env": {"GREETING": "one"}})
        s.tp_ok(["deploy", repo, "--timeout", DEPLOY_TIMEOUT], timeout=600)
        s.commit(repo, {"body.txt": "release two\n"}, "Release two", {"env": {"GREETING": "two"}})
        s.tp_ok(["deploy", repo, "--timeout", DEPLOY_TIMEOUT], timeout=600)
        data = s.tp_json(listing, timeout=60)
        items = kit.releases_of(data)
        if len(items) != 2 or any(r["status"] != "succeeded" for r in items) or data["current"] != 2:
            raise Fail(f"after two good deploys expected releases 1 and 2 succeeded and current 2; got {[(r['number'], r['status']) for r in items]}, current {data['current']}")
        one, two = items
        if one["config_hash"] == two["config_hash"] or one["image"] == two["image"]:
            raise Fail("release 2 changed the image and GREETING in app.json, so both image and config_hash must differ from release 1")

        s.commit(repo, {"body.txt": "release three\n", "health.txt": unhealthy}, "Release three never gets ready")
        broken = s.tp(["deploy", repo, "--timeout", "40"], timeout=600)
        if broken.code == 0:
            raise Fail("deploying a commit whose /healthz fails must exit non-zero once --timeout passes")
        data = s.tp_json(listing, timeout=60)
        three = latest(data)
        if three["number"] != 3 or three["status"] != "failed" or data["current"] != 2:
            raise Fail(f"the unready release should be v3 with status failed and current still 2; got v{three['number']} {three['status']}, current {data['current']}")
        url = s.url(app)
        s.wait_body(url, "release two", 60)
        ok, (status, body) = kit.holds(lambda: _answer(url, "release two"), 3)
        if not ok:
            raise Fail(f"while v3 failed, {url} must keep serving release two; it answered {status} {kit.tail(body, 80)!r}")

        s.tp_ok(["rollback", app, "--to", "1", "--json"], timeout=600)
        data = s.tp_json(listing, timeout=60)
        four = latest(data)
        if (four["number"], four["status"], data["current"]) != (4, "succeeded", 4):
            raise Fail(f"rollback --to 1 should create v4 (succeeded, current); got v{four['number']} {four['status']}, current {data['current']}")
        if kit.short_image(four["image"]) != kit.short_image(one["image"]) or four["config_hash"] != one["config_hash"]:
            raise Fail("v4 must restore v1 exactly: the same image and the same config_hash")
        s.wait_body(url, "release one", 90)
        status, body = kit.fetch(url + "/env")
        if (kit.parse_json(body) or {}).get("GREETING") != "one":
            raise Fail(f"after rollback --to 1, GREETING must be v1's value 'one'; /env answered {status} {kit.tail(body, 120)!r}")
        live = kit.short_image(kit.first_container(s.deployment(app)).get("image", ""))
        if live != kit.short_image(one["image"]):
            raise Fail(f"after rollback --to 1 the live Deployment runs {live}, not v1's {one['image']}")

        s.tp_ok(["rollback", app, "--json"], timeout=600)
        five = latest(s.tp_json(listing, timeout=60))
        if kit.short_image(five["image"]) != kit.short_image(two["image"]) or five["config_hash"] != two["config_hash"]:
            raise Fail(f"rollback without --to from v4 should restore v2 (the last succeeded release before the current one, skipping failed v3); v5 runs {five['image']}")
        s.wait_body(url, "release two", 90)
        return "v3 failed readiness and v2 kept serving; rollback --to 1 restored v1's image, config and body as v4; rollback restored v2 as v5"


def _answer(url: str, expected: str) -> Tuple[bool, Any]:
    status, body = kit.fetch(url)
    return status == 200 and body.strip() == expected, (status, body)


def logs(project: str) -> str:
    app = "ccg-logs"
    markers = [f"ccg-{i:03d}" for i in range(40)]
    with Session(project, [app]) as s:
        repo = s.make_app(app, manifest={"replicas": 2})
        s.tp_ok(["deploy", repo, "--timeout", DEPLOY_TIMEOUT], timeout=600)

        def two_ready() -> Tuple[bool, Any]:
            ready = s.ready_pods(s.deployment(app))
            return len(ready) == 2, ready
        ok, pods = kit.poll(two_ready, 90)
        if not ok:
            raise Fail(f"app.json says replicas: 2 but {len(pods)} pod(s) are ready: deploy must honour replicas")
        url = s.url(app)

        def both_pods_answer() -> Tuple[bool, Any]:
            seen = set()
            for _ in range(10):
                status, body = kit.fetch(url + "/pod")
                if status == 200:
                    seen.add(body.strip())
            return seen >= set(pods), seen
        ok, seen = kit.poll(both_pods_answer, 60)
        if not ok:
            raise Fail(f"only {sorted(seen)} answered {url}/pod within 60s although {pods} are ready: check the Service selector")
        for marker in markers:
            status, body = kit.fetch(f"{url}/?marker={marker}")
            if status != 200:
                raise Fail(f"{url}/?marker={marker} answered {status} {kit.tail(body, 80)!r}")

        def collect() -> Tuple[bool, Any]:
            data = s.tp_json(["logs", app, "--since", "5m", "--json"], timeout=120)
            lines = data.get("lines")
            if not isinstance(lines, list):
                raise Fail("tinypaas logs --json must print {\"lines\": [{\"time\", \"pod\", \"line\"}, ...]}")
            found = {}
            for entry in lines:
                match = MARKER_RE.search(entry.get("line", "")) if isinstance(entry, dict) else None
                if match:
                    found[match.group(1)] = (entry.get("pod"), match.group(2))
            return len(found) == len(markers), (lines, found)
        ok, (lines, found) = kit.poll(collect, 45, interval=2)
        missing = [m for m in markers if m not in found]
        if missing:
            raise Fail(f"{len(missing)} of {len(markers)} request markers are missing from tinypaas logs (first: {missing[0]}); read every pod of the app")
        stamps = []
        for entry in lines:
            if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in ("time", "pod", "line")):
                raise Fail(f"every log entry needs time, pod and line strings; got {kit.tail(json.dumps(entry), 120)}")
            try:
                stamps.append(kit.parse_timestamp(entry["time"]))
            except ValueError as exc:
                raise Fail(f"log time {entry['time']!r} is not RFC 3339: {exc}")
        if stamps != sorted(stamps):
            raise Fail("log lines are not ordered by time: parse the timestamps (RFC 3339 trims trailing zeros, so string order is wrong)")
        mislabelled = [(m, pod, writer) for m, (pod, writer) in sorted(found.items()) if pod != writer]
        if mislabelled:
            marker, pod, writer = mislabelled[0]
            raise Fail(f"{len(mislabelled)} log entries carry the wrong pod: {marker} was written by {writer} but labelled {pod!r}")
        served = {writer for _, writer in found.values()}
        if served != set(pods):
            raise Fail(f"the marked requests were served by {sorted(served)} only, but the app's ready pods are {pods}: check that the Service selects every pod of the app")
        return f"all {len(markers)} markers from both pods ({', '.join(pods)}) came back in time order"
