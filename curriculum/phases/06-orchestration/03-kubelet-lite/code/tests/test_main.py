import http.server
import io
import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

WEB_YAML = """
# a web server that needs its docroot written by an init container
apiVersion: v1
kind: Pod
metadata:
  name: web
  namespace: demo
spec:
  restartPolicy: Always
  terminationGracePeriodSeconds: 5
  initContainers:
    - name: write-index
      image: python:3.12-alpine
      command: ["python3", "-c", "open('index.html', 'w').write('hi')"]
  containers:
    - name: web
      image: python:3.12-alpine
      command: [python3, -m, http.server, "8000"]
      env:
        - name: MODE
          value: "demo"
      ports:
        - name: http
          containerPort: 8000
      readinessProbe:
        httpGet:
          path: /index.html
          port: http
        periodSeconds: 2
        successThreshold: 2
      livenessProbe:
        tcpSocket:
          port: 8000
        initialDelaySeconds: 3
"""

WEB_JSON = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {"name": "web", "namespace": "demo"},
    "spec": {
        "terminationGracePeriodSeconds": 5,
        "restartPolicy": "Always",
        "containers": [
            {
                "name": "web",
                "image": "python:3.12-alpine",
                "command": ["python3", "-m", "http.server", "8000"],
                "env": [{"name": "MODE", "value": "demo"}],
                "ports": [{"name": "http", "containerPort": 8000}],
                "livenessProbe": {"tcpSocket": {"port": 8000}, "initialDelaySeconds": 3},
                "readinessProbe": {"httpGet": {"path": "/index.html", "port": "http"}, "periodSeconds": 2, "successThreshold": 2},
            }
        ],
        "initContainers": [
            {"name": "write-index", "image": "python:3.12-alpine", "command": ["python3", "-c", "open('index.html', 'w').write('hi')"]}
        ],
    },
}


class Clock:
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeRuntime:
    name = "fake"

    def __init__(self, clock, scripts=None):
        self.clock = clock
        self.scripts = scripts or {}
        self.containers = {}
        self.calls = []
        self.exec_results = {}
        self.removed = []

    def behaviour(self, name, attempt):
        script = self.scripts.get(name, {})
        if isinstance(script, list):
            script = script[min(attempt - 1, len(script) - 1)]
        return script

    def run_sandbox(self, pod):
        sandbox = f"sb-{pod.full_name}"
        self.calls.append(("run_sandbox", sandbox))
        return sandbox

    def address(self, sandbox):
        return "10.0.0.7"

    def start(self, sandbox, pod, container, attempt):
        behaviour = self.behaviour(container.name, attempt)
        if behaviour.get("fail_start"):
            raise main.StartError(behaviour["fail_start"])
        cid = f"{container.name}-{attempt}"
        run = behaviour.get("run")
        self.containers[cid] = {
            "name": container.name,
            "exit_at": None if run is None else self.clock.t + run,
            "code": behaviour.get("code", 0),
            "ignore_term": behaviour.get("ignore_term", False),
            "exited": None,
        }
        self.calls.append(("start", cid, self.clock.t))
        return cid

    def status(self, cid):
        container = self.containers[cid]
        if container["exited"] is None and container["exit_at"] is not None and self.clock.t >= container["exit_at"]:
            container["exited"] = container["code"]
        return container["exited"]

    def kill(self, cid, sig):
        self.calls.append(("kill", cid, int(sig), self.clock.t))
        container = self.containers[cid]
        if container["exited"] is not None:
            return
        if sig == signal.SIGKILL:
            container["exited"] = 137
        elif not container["ignore_term"]:
            container["exited"] = 143

    def exec_sync(self, cid, argv, timeout):
        self.calls.append(("exec", cid, tuple(argv), self.clock.t))
        handler = self.exec_results.get(self.containers[cid]["name"])
        return handler(list(argv), self.clock.t) if handler else (0, "")

    def logs(self, cid):
        return ""

    def remove(self, cid):
        self.removed.append(cid)

    def remove_sandbox(self, sandbox):
        self.removed.append(sandbox)


def pod_from(data, node="node1"):
    return main.parse_pod(data, "test.json", node)


def simple_pod(name="app", policy="Always", grace=30, container=None, init=None):
    spec = {"restartPolicy": policy, "terminationGracePeriodSeconds": grace, "containers": [container or {"name": "app", "image": "busybox"}]}
    if init:
        spec["initContainers"] = init
    return pod_from({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name}, "spec": spec})


def kubelet_for(scripts=None, base=10.0, cap=300.0, http=None, tcp=None, min_grace=2.0):
    clock = Clock()
    runtime = FakeRuntime(clock, scripts)
    prober = main.Prober(runtime, http=http or (lambda url, timeout: main.ProbeResult(True, "HTTP 200")), tcp=tcp or (lambda host, port, timeout: main.ProbeResult(True, "ok")))
    kubelet = main.Kubelet(runtime, "node1", base, cap, prober=prober, now=clock, min_grace=min_grace)
    return kubelet, runtime, clock


def drive(kubelet, clock, desired, seconds, step=1.0):
    pods = {pod.key: pod for pod in desired}
    steps = int(round(seconds / step))
    for _ in range(steps):
        kubelet.sync(pods)
        clock.advance(step)


def events(kubelet, reason=None, container=None):
    return [e for e in kubelet.events if (reason is None or e.reason == reason) and (container is None or e.container == container)]


def only(kubelet):
    return main.pod_object(next(iter(kubelet.pods.values())), "fake")


class ManifestTests(unittest.TestCase):
    def test_yaml_subset_and_json_load_to_the_same_pod_and_hash(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        with open(os.path.join(folder, "web.yaml"), "w") as handle:
            handle.write(WEB_YAML)
        with open(os.path.join(folder, "web.json"), "w") as handle:
            json.dump(WEB_JSON, handle)
        from_yaml = main.load_manifest(os.path.join(folder, "web.yaml"), "node1")
        from_json = main.load_manifest(os.path.join(folder, "web.json"), "node1")
        self.assertEqual(from_yaml.config_hash, from_json.config_hash)
        self.assertEqual(from_yaml.full_name, "web-node1")
        self.assertEqual(from_yaml.key, "demo/web-node1")
        self.assertEqual(from_yaml.init_containers, from_json.init_containers)
        self.assertEqual(from_yaml.containers, from_json.containers)
        web = from_yaml.containers[0]
        self.assertEqual(web.argv(), ["python3", "-m", "http.server", "8000"])
        self.assertEqual(web.env, {"MODE": "demo"})
        self.assertEqual(web.probes["readiness"].port, 8000)
        self.assertEqual(web.probes["readiness"].success_threshold, 2)
        self.assertEqual(web.probes["liveness"].handler, "tcpSocket")
        self.assertEqual(from_yaml.grace_period, 5.0)
        self.assertNotEqual(main.load_manifest(os.path.join(folder, "web.yaml"), "node2").config_hash, from_yaml.config_hash)

    def test_manifest_errors_name_the_field_and_the_fix(self):
        def pod(spec, kind="Pod"):
            return {"apiVersion": "v1", "kind": kind, "metadata": {"name": "p"}, "spec": spec}

        app = {"name": "app", "image": "busybox"}
        cases = [
            (pod({"containers": [app]}, kind="Deployment"), "kind must be Pod"),
            (pod({"containers": []}), "at least one container"),
            (pod({"containers": [app], "restartPolicy": "Sometimes"}), "restartPolicy must be one of"),
            (pod({"containers": [dict(app, readinessProbe={"exec": {"command": ["true"]}, "httpGet": {"port": 80}})]}), "exactly one of exec, httpGet, tcpSocket"),
            (pod({"containers": [dict(app, livenessProbe={"exec": {"command": ["true"]}, "successThreshold": 2})]}), "successThreshold: must be 1 for liveness"),
            (pod({"containers": [dict(app, readinessProbe={"httpGet": {"port": "web"}})]}), "named ports: none"),
            (pod({"containers": [app, app]}), "used twice"),
            (pod({"containers": [app], "initContainers": [dict(app, name="init", readinessProbe={"exec": {"command": ["true"]}})]}), "cannot have probes"),
            (pod({"containers": [dict(app, env=[{"name": "A", "valueFrom": {"secretKeyRef": {}}}])]}), "cannot read ConfigMaps or Secrets"),
            (pod({"containers": [dict(app, env=[{"name": "PORT", "value": 8000}])]}), "quote numbers"),
            (pod({"containers": [dict(app, readinessProbe={"exec": {"command": ["true"]}, "periodSeconds": 0})]}), "periodSeconds: must be an integer >= 1"),
            (pod({"containers": [app], "initContainers": [dict(app, name="side", restartPolicy="Always")]}), "sidecar init containers"),
        ]
        for data, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(main.ManifestError) as caught:
                    pod_from(data)
                self.assertIn(message, str(caught.exception))

    def test_probe_describe_uses_kubectl_describe_format(self):
        pod = simple_pod(container={
            "name": "app",
            "image": "busybox",
            "ports": [{"name": "http", "containerPort": 8080}],
            "livenessProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "initialDelaySeconds": 3, "periodSeconds": 3},
            "readinessProbe": {"exec": {"command": ["cat", "/tmp/healthy"]}, "initialDelaySeconds": 5, "periodSeconds": 5},
            "startupProbe": {"tcpSocket": {"port": 8080}, "failureThreshold": 30, "periodSeconds": 90},
        })
        probes = pod.containers[0].probes
        self.assertEqual(probes["liveness"].describe(), "http-get http://:8080/healthz delay=3s timeout=1s period=3s #success=1 #failure=3")
        self.assertEqual(probes["readiness"].describe(), "exec [cat /tmp/healthy] delay=5s timeout=1s period=5s #success=1 #failure=3")
        self.assertEqual(probes["startup"].describe(), "tcp-socket :8080 delay=0s timeout=1s period=1m30s #success=1 #failure=30")

    def test_manifest_directory_skips_hidden_files_and_reports_bad_and_duplicate_pods(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        files = {
            "a-web.yaml": WEB_YAML,
            "b-copy.json": json.dumps(WEB_JSON),
            ".web.yaml.swp": "garbage",
            "notes.txt": "not a manifest",
            "broken.yaml": "apiVersion: v1\nkind: Pod\nmetadata:\n  name: x\n   bad: indent\n",
        }
        for name, text in files.items():
            with open(os.path.join(folder, name), "w") as handle:
                handle.write(text)
        pods, errors = main.load_manifests(folder, "n")
        self.assertEqual(list(pods), ["demo/web-n"])
        self.assertEqual(sorted(errors), ["b-copy.json", "broken.yaml"])
        self.assertIn("already defined in a-web.yaml", errors["b-copy.json"])
        self.assertIn("broken.yaml:5: bad indentation", errors["broken.yaml"])


class BackoffTests(unittest.TestCase):
    def test_schedule_starts_immediately_then_doubles_from_the_base_up_to_the_cap(self):
        self.assertEqual(main.backoff_schedule(10, 300, 8), [0, 10, 20, 40, 80, 160, 300, 300])
        self.assertEqual(main.backoff_schedule(0.5, 4, 6), [0, 0.5, 1, 2, 4, 4])

    def test_backoff_resets_after_running_cleanly_for_twice_the_cap(self):
        backoff = main.Backoff(10, 300)
        backoff.next("c", 0, 0)
        backoff.next("c", 1, 11)
        self.assertEqual(backoff.delay("c"), 20)
        self.assertEqual(backoff.remaining("c", 20, 25), 15)
        self.assertIsNone(backoff.remaining("c", 11 + 601, 11 + 601))
        self.assertEqual(backoff.next("c", 11 + 601, 11 + 601), 10)


class SyncLoopTests(unittest.TestCase):
    def test_init_containers_run_in_order_before_app_containers_start(self):
        pod = simple_pod(init=[{"name": "init-a", "image": "busybox"}, {"name": "init-b", "image": "busybox"}])
        kubelet, runtime, clock = kubelet_for({"init-a": {"run": 2}, "init-b": {"run": 1}})
        kubelet.sync({pod.key: pod})
        first = only(kubelet)
        self.assertEqual(main.status_reason(first), "Init:0/2")
        self.assertEqual(first["status"]["containerStatuses"][0]["state"]["waiting"]["reason"], "PodInitializing")
        self.assertEqual(first["status"]["conditions"][1], {"type": "Initialized", "status": "False", "reason": "ContainersNotInitialized", "message": "containers with incomplete status: [init-a init-b]"})
        clock.advance(2)
        kubelet.sync({pod.key: pod})
        self.assertEqual(main.status_reason(only(kubelet)), "Init:1/2")
        clock.advance(1)
        kubelet.sync({pod.key: pod})
        final = only(kubelet)
        self.assertEqual([call[1] for call in runtime.calls if call[0] == "start"], ["init-a-1", "init-b-1", "app-1"])
        self.assertEqual(final["status"]["phase"], "Running")
        self.assertEqual(main.status_reason(final), "Running")
        self.assertEqual(final["status"]["conditions"][1], {"type": "Initialized", "status": "True"})
        self.assertEqual(runtime.calls[0], ("run_sandbox", "sb-app-node1"))

    def test_failed_init_container_is_retried_with_backoff_unless_restart_policy_is_never(self):
        init = [{"name": "migrate", "image": "busybox"}]
        always = simple_pod(init=init)
        kubelet, runtime, clock = kubelet_for({"migrate": {"run": 0, "code": 1}})
        drive(kubelet, clock, [always], 15)
        starts = [call[2] - 1000 for call in runtime.calls if call[0] == "start"]
        self.assertEqual(starts, [0, 1, 12])
        self.assertNotIn("app-1", [call[1] for call in runtime.calls if call[0] == "start"])
        clock.advance(-1)
        kubelet.sync({always.key: always})
        state = only(kubelet)
        self.assertEqual(state["status"]["phase"], "Pending")
        self.assertIn(main.status_reason(state), ("Init:CrashLoopBackOff", "Init:Error"))
        never = simple_pod(policy="Never", init=init)
        kubelet, runtime, clock = kubelet_for({"migrate": {"run": 0, "code": 1}})
        drive(kubelet, clock, [never], 10)
        state = only(kubelet)
        self.assertEqual(state["status"]["phase"], "Failed")
        self.assertEqual(main.status_reason(state), "Init:Error")
        self.assertEqual([call[1] for call in runtime.calls if call[0] == "start"], ["migrate-1"])

    def test_restart_policy_decides_whether_an_exited_container_comes_back(self):
        expectations = {
            ("Always", 0): (True, "Running"),
            ("Always", 1): (True, "Running"),
            ("OnFailure", 0): (False, "Succeeded"),
            ("OnFailure", 1): (True, "Running"),
            ("Never", 0): (False, "Succeeded"),
            ("Never", 1): (False, "Failed"),
        }
        for (policy, code), (restarted, phase) in expectations.items():
            with self.subTest(policy=policy, code=code):
                pod = simple_pod(policy=policy)
                kubelet, runtime, clock = kubelet_for({"app": [{"run": 1, "code": code}, {"run": None}]})
                drive(kubelet, clock, [pod], 4)
                status = only(kubelet)
                self.assertEqual("app-2" in [call[1] for call in runtime.calls if call[0] == "start"], restarted)
                self.assertEqual(status["status"]["phase"], phase)
                if not restarted:
                    self.assertEqual(main.status_reason(status), "Completed" if code == 0 else "Error")

    def test_crashloop_backoff_grows_and_reads_like_kubectl(self):
        pod = simple_pod(name="crash")
        kubelet, runtime, clock = kubelet_for({"app": {"run": 0, "code": 1}})
        drive(kubelet, clock, [pod], 80)
        exits = [e.at for e in events(kubelet, "Exited")]
        starts = [e.at for e in events(kubelet, "Started")]
        gaps = [start - exited for exited, start in zip(exits, starts[1:])]
        self.assertEqual(gaps[:4], [0, 10, 20, 40])
        self.assertEqual([e.delay for e in events(kubelet, "BackOff")], [10, 20, 40, 80])
        self.assertEqual(events(kubelet, "BackOff")[0].message, "Back-off restarting failed container app in pod crash-node1")
        status = only(kubelet)
        container = status["status"]["containerStatuses"][0]
        self.assertEqual(container["state"]["waiting"]["reason"], "CrashLoopBackOff")
        self.assertTrue(container["state"]["waiting"]["message"].startswith("back-off 80s restarting failed container=app pod=crash-node1_default("))
        self.assertEqual(container["lastState"]["terminated"]["exitCode"], 1)
        self.assertEqual(container["restartCount"], 4)
        self.assertEqual(status["status"]["phase"], "Running")
        self.assertEqual(main.status_reason(status), "CrashLoopBackOff")
        self.assertEqual(main.table_rows([status], clock.t)[0]["restarts"], 4)

    def test_readiness_gates_ready_without_restarting_and_uses_the_pod_ip_and_named_port(self):
        answers = iter([False, True, True, True, False, False, True, True])
        urls = []

        def http(url, timeout):
            urls.append(url)
            ok = next(answers)
            return main.ProbeResult(ok, "HTTP 200" if ok else "HTTP probe failed with statuscode: 503")

        pod = simple_pod(container={
            "name": "web",
            "image": "x",
            "ports": [{"name": "http", "containerPort": 8000}],
            "readinessProbe": {"httpGet": {"path": "/ready", "port": "http"}, "initialDelaySeconds": 2, "periodSeconds": 1, "successThreshold": 2, "failureThreshold": 2},
        })
        kubelet, runtime, clock = kubelet_for(http=http)
        readiness = []
        for _ in range(10):
            kubelet.sync({pod.key: pod})
            readiness.append(only(kubelet)["status"]["containerStatuses"][0]["ready"])
            clock.advance(1)
        self.assertEqual(urls[0], "http://10.0.0.7:8000/ready")
        self.assertEqual(readiness, [False, False, False, False, True, True, True, False, False, True])
        self.assertEqual(len(urls), 8)
        self.assertEqual(only(kubelet)["status"]["containerStatuses"][0]["restartCount"], 0)
        unhealthy = events(kubelet, "Unhealthy")
        self.assertEqual(len(unhealthy), 3)
        self.assertEqual(unhealthy[0].message, "Readiness probe failed: HTTP probe failed with statuscode: 503")

    def test_liveness_failures_kill_the_container_with_sigterm_and_it_restarts(self):
        pod = simple_pod(grace=10, container={"name": "app", "image": "x", "livenessProbe": {"exec": {"command": ["cat", "/tmp/alive"]}, "periodSeconds": 1, "failureThreshold": 3}})
        kubelet, runtime, clock = kubelet_for()
        runtime.exec_results["app"] = lambda argv, now: (0, "") if now < 1003 else (1, "cat: can't open '/tmp/alive': No such file or directory")
        drive(kubelet, clock, [pod], 7)
        self.assertEqual(len(events(kubelet, "Unhealthy")), 3)
        self.assertEqual(events(kubelet, "Unhealthy")[0].message, "Liveness probe failed: cat: can't open '/tmp/alive': No such file or directory")
        self.assertEqual(events(kubelet, "Killing")[0].message, "Container app failed liveness probe, will be restarted")
        self.assertIn(("kill", "app-1", int(signal.SIGTERM), 1005.0), runtime.calls)
        self.assertEqual(events(kubelet, "Exited")[0].exit_code, 143)
        status = only(kubelet)["status"]["containerStatuses"][0]
        self.assertEqual(status["restartCount"], 1)
        self.assertEqual(status["lastState"]["terminated"]["exitCode"], 143)

    def test_startup_probe_holds_back_liveness_and_readiness_and_can_kill(self):
        container = {
            "name": "app",
            "image": "x",
            "startupProbe": {"exec": {"command": ["startup"]}, "periodSeconds": 1, "failureThreshold": 5},
            "livenessProbe": {"exec": {"command": ["live"]}, "periodSeconds": 1},
            "readinessProbe": {"exec": {"command": ["ready"]}, "periodSeconds": 1},
        }
        pod = simple_pod(container=container)
        kubelet, runtime, clock = kubelet_for()
        runtime.exec_results["app"] = lambda argv, now: (0, "") if argv != ["startup"] or now >= 1003 else (1, "not yet")
        drive(kubelet, clock, [pod], 6)
        execs = [(call[2][0], call[3] - 1000) for call in runtime.calls if call[0] == "exec"]
        first_other = min(t for name, t in execs if name != "startup")
        self.assertEqual(first_other, 3)
        self.assertEqual([t for name, t in execs if name == "startup"], [1, 2, 3])
        self.assertTrue(only(kubelet)["status"]["containerStatuses"][0]["ready"])
        pod = simple_pod(container=dict(container, startupProbe={"exec": {"command": ["startup"]}, "periodSeconds": 1, "failureThreshold": 2}))
        kubelet, runtime, clock = kubelet_for()
        runtime.exec_results["app"] = lambda argv, now: (1, "") if argv == ["startup"] else (0, "")
        drive(kubelet, clock, [pod], 3)
        self.assertEqual(events(kubelet, "Killing")[0].message, "Container app failed startup probe, will be restarted")
        self.assertNotIn("live", [call[2][0] for call in runtime.calls if call[0] == "exec"])

    def test_graceful_stop_sends_sigterm_then_sigkill_when_the_grace_period_expires(self):
        pod = simple_pod(grace=5)
        kubelet, runtime, clock = kubelet_for({"app": {"run": None, "ignore_term": True}})
        drive(kubelet, clock, [pod], 2)
        drive(kubelet, clock, [], 8)
        kills = [(call[2], call[3] - 1000) for call in runtime.calls if call[0] == "kill"]
        self.assertEqual(kills, [(int(signal.SIGTERM), 2), (int(signal.SIGKILL), 7)])
        self.assertEqual(events(kubelet, "Killing")[0].message, "Stopping container app")
        self.assertEqual(events(kubelet, "Exited")[0].exit_code, 137)
        self.assertEqual(kubelet.pods, {})
        self.assertIn("sb-app-node1", runtime.removed)
        polite, runtime, clock = kubelet_for()
        drive(polite, clock, [pod], 2)
        drive(polite, clock, [], 3)
        self.assertEqual([call[2] for call in runtime.calls if call[0] == "kill"], [int(signal.SIGTERM)])
        self.assertEqual(events(polite, "Exited")[0].exit_code, 143)

    def test_short_grace_is_raised_to_the_minimum_and_pre_stop_runs_before_sigterm(self):
        pod = simple_pod(grace=0, container={"name": "app", "image": "x", "lifecycle": {"preStop": {"exec": {"command": ["sh", "-c", "sleep 1"]}}}})
        kubelet, runtime, clock = kubelet_for({"app": {"run": None, "ignore_term": True}})
        drive(kubelet, clock, [pod], 1)
        drive(kubelet, clock, [], 4)
        kills = [(call[2], call[3] - 1000) for call in runtime.calls if call[0] == "kill"]
        self.assertEqual(kills, [(int(signal.SIGTERM), 1), (int(signal.SIGKILL), 3)])
        pod = simple_pod(grace=10, container={"name": "app", "image": "x", "lifecycle": {"preStop": {"exec": {"command": ["drain"]}}}})
        kubelet, runtime, clock = kubelet_for()
        drive(kubelet, clock, [pod], 1)
        drive(kubelet, clock, [], 2)
        ops = [call[0] for call in runtime.calls if call[0] in ("exec", "kill")]
        self.assertEqual(ops, ["exec", "kill"])

    def test_changed_manifest_replaces_the_pod_and_removed_manifest_deletes_it(self):
        v1 = simple_pod(container={"name": "app", "image": "app:1"})
        v2 = simple_pod(container={"name": "app", "image": "app:2"})
        self.assertNotEqual(v1.config_hash, v2.config_hash)
        kubelet, runtime, clock = kubelet_for()
        drive(kubelet, clock, [v1], 2)
        drive(kubelet, clock, [v2], 3)
        self.assertEqual(kubelet.pods[v2.key].spec.config_hash, v2.config_hash)
        self.assertEqual([call[1] for call in runtime.calls if call[0] == "start"], ["app-1", "app-1"])
        self.assertEqual(events(kubelet, "Killing")[0].message, "Stopping container app")
        self.assertEqual(only(kubelet)["spec"]["containers"][0]["image"], "app:2")
        drive(kubelet, clock, [], 2)
        self.assertEqual(kubelet.pods, {})
        self.assertEqual(len(events(kubelet, "PodRemoved")), 2)


class StatusTests(unittest.TestCase):
    def test_pod_object_has_the_api_shape_and_static_pod_annotations(self):
        pod = simple_pod(name="web")
        kubelet, runtime, clock = kubelet_for()
        drive(kubelet, clock, [pod], 2)
        obj = json.loads(json.dumps(main.pod_list(kubelet)))
        self.assertEqual(obj["kind"], "List")
        item = obj["items"][0]
        self.assertEqual(item["metadata"]["name"], "web-node1")
        self.assertEqual(item["metadata"]["annotations"]["kubernetes.io/config.source"], "file")
        self.assertEqual(item["metadata"]["annotations"]["kubernetes.io/config.hash"], pod.config_hash)
        self.assertEqual(item["spec"]["nodeName"], "node1")
        status = item["status"]["containerStatuses"][0]
        self.assertEqual(set(status), {"name", "image", "ready", "started", "restartCount", "state", "lastState", "containerID"})
        self.assertEqual(status["containerID"], "fake://app-1")
        self.assertIn("running", status["state"])
        self.assertEqual([c["type"] for c in item["status"]["conditions"]], ["PodReadyToStartContainers", "Initialized", "ContainersReady", "Ready"])
        self.assertTrue(all(c["status"] == "True" for c in item["status"]["conditions"]))

    def test_status_reason_matches_the_kubectl_printer_on_api_objects(self):
        def pod(phase, containers, init=None, deleting=False, ready=False):
            obj = {"metadata": {"name": "p"}, "spec": {}, "status": {"phase": phase, "containerStatuses": containers, "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}
            if init is not None:
                obj["status"]["initContainerStatuses"] = init
                obj["spec"]["initContainers"] = [{"name": str(i)} for i in range(len(init))]
            if deleting:
                obj["metadata"]["deletionTimestamp"] = "2026-11-01T10:00:00Z"
            return obj

        running = {"ready": True, "state": {"running": {}}}
        done = {"ready": False, "state": {"terminated": {"exitCode": 0, "reason": "Completed"}}}
        crash = {"ready": False, "state": {"waiting": {"reason": "CrashLoopBackOff"}}, "lastState": {"terminated": {"exitCode": 1}}}
        cases = [
            (pod("Running", [crash]), "CrashLoopBackOff"),
            (pod("Running", [running]), "Running"),
            (pod("Succeeded", [done]), "Completed"),
            (pod("Running", [done, running], ready=True), "Running"),
            (pod("Running", [done, running], ready=False), "NotReady"),
            (pod("Failed", [{"state": {"terminated": {"exitCode": 2}}}]), "ExitCode:2"),
            (pod("Pending", [], init=[done, {"state": {"waiting": {"reason": "PodInitializing"}}}]), "Init:1/2"),
            (pod("Pending", [], init=[{"state": {"terminated": {"exitCode": 1, "reason": "Error"}}}]), "Init:Error"),
            (pod("Pending", [], init=[{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}]), "Init:CrashLoopBackOff"),
            (pod("Running", [running], deleting=True), "Terminating"),
            (pod("Pending", [{"state": {"waiting": {"reason": "ContainerCreating"}}}]), "ContainerCreating"),
        ]
        for obj, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(main.status_reason(obj), expected)

    def test_table_and_age_formatting_follow_kubectl(self):
        self.assertEqual([main.human_age(s) for s in (5, 119, 125, 600, 3 * 3600 + 300, 50 * 3600, 30 * 86400)], ["5s", "119s", "2m5s", "10m", "3h5m", "2d2h", "30d"])
        pod = {
            "metadata": {"name": "crash-node1", "namespace": "demo", "creationTimestamp": "2026-11-01T10:00:00Z"},
            "spec": {"containers": [{"name": "app"}, {"name": "side"}]},
            "status": {"phase": "Running", "containerStatuses": [
                {"name": "app", "ready": False, "restartCount": 3, "state": {"waiting": {"reason": "CrashLoopBackOff"}}},
                {"name": "side", "ready": True, "restartCount": 0, "state": {"running": {}}},
            ]},
        }
        now = main.parse_time("2026-11-01T10:02:05Z")
        table = main.render_table(main.table_rows([pod], now)).splitlines()
        self.assertEqual(table[0].split(), ["NAMESPACE", "NAME", "READY", "STATUS", "RESTARTS", "AGE"])
        self.assertEqual(table[1].split(), ["demo", "crash-node1", "1/2", "CrashLoopBackOff", "3", "2m5s"])
        self.assertEqual(main.render_table([]), "No resources found")


class ProberTests(unittest.TestCase):
    def test_http_and_tcp_probes_against_a_local_server(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                codes = {"/ok": 200, "/fail": 503, "/moved": 302}
                code = codes.get(self.path, 404)
                self.send_response(code)
                if code == 302:
                    self.send_header("Location", "/ok")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        self.assertTrue(main.http_get(f"http://127.0.0.1:{port}/ok", 2).ok)
        self.assertTrue(main.http_get(f"http://127.0.0.1:{port}/moved", 2).ok)
        failed = main.http_get(f"http://127.0.0.1:{port}/fail", 2)
        self.assertEqual((failed.ok, failed.detail), (False, "HTTP probe failed with statuscode: 503"))
        self.assertTrue(main.tcp_connect("127.0.0.1", port, 2).ok)
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        free = closed.getsockname()[1]
        closed.close()
        refused = main.http_get(f"http://127.0.0.1:{free}/", 1)
        self.assertFalse(refused.ok)
        self.assertTrue(refused.detail.startswith(f'Get "http://127.0.0.1:{free}/":'))
        self.assertFalse(main.tcp_connect("127.0.0.1", free, 1).ok)

    def test_exec_probe_failure_detail_is_the_command_output(self):
        class Runtime:
            def exec_sync(self, cid, argv, timeout):
                return {"ok": (0, "fine"), "bad": (1, "cat: can't open '/tmp/ready'"), "quiet": (2, ""), "slow": (-1, "")}[argv[0]]

        prober = main.Prober(Runtime())
        probe = lambda name: main.Probe("readiness", "exec", command=[name, "x"])
        self.assertTrue(prober.run(probe("ok"), "c", "").ok)
        self.assertEqual(prober.run(probe("bad"), "c", "").detail, "cat: can't open '/tmp/ready'")
        self.assertEqual(prober.run(probe("quiet"), "c", "").detail, "command [quiet x] exited with 2")
        self.assertEqual(prober.run(probe("slow"), "c", "").detail, "command [slow x] timed out after 1s")


class RuntimeTests(unittest.TestCase):
    def wait_exit(self, runtime, cid, seconds=10.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            code = runtime.status(cid)
            if code is not None:
                return code
            time.sleep(0.02)
        self.fail(f"{cid} still running after {seconds}s")

    def test_process_runtime_runs_real_processes_and_signals_the_whole_group(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        runtime = main.ProcessRuntime(root)
        pod = simple_pod()
        sandbox = runtime.run_sandbox(pod)
        py = sys.executable

        def spec(name, code):
            return main.ContainerSpec(name, "ignored", [py, "-c", code], env={"GREETING": "hello"})

        exits = runtime.start(sandbox, pod, spec("exits", "import os, sys; print(os.environ['GREETING']); sys.exit(3)"), 1)
        self.assertEqual(self.wait_exit(runtime, exits), 3)
        self.assertEqual(runtime.logs(exits).strip(), "hello")
        sleeper = runtime.start(sandbox, pod, spec("sleeper", "import time; print('up', flush=True); time.sleep(30)"), 1)
        self.assertIsNone(runtime.status(sleeper))
        code, output = runtime.exec_sync(sleeper, [py, "-c", "import os; print(os.environ['GREETING'], os.getcwd() == os.path.realpath('.'))"], 5)
        self.assertEqual((code, output), (0, "hello True"))
        runtime.kill(sleeper, signal.SIGTERM)
        self.assertEqual(self.wait_exit(runtime, sleeper), 143)
        stubborn = runtime.start(sandbox, pod, spec("stubborn", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(30)"), 1)
        deadline = time.time() + 10
        while "ready" not in runtime.logs(stubborn) and time.time() < deadline:
            time.sleep(0.02)
        runtime.kill(stubborn, signal.SIGTERM)
        time.sleep(0.3)
        self.assertIsNone(runtime.status(stubborn))
        runtime.kill(stubborn, signal.SIGKILL)
        self.assertEqual(self.wait_exit(runtime, stubborn), 137)
        with self.assertRaises(main.StartError):
            runtime.start(sandbox, pod, main.ContainerSpec("missing", "x", ["/nonexistent/binary"]), 1)
        with self.assertRaises(main.StartError):
            runtime.start(sandbox, pod, main.ContainerSpec("noentry", "x"), 1)
        for cid in (exits, sleeper, stubborn):
            runtime.remove(cid)

    def test_docker_runtime_builds_a_pause_sandbox_and_cri_like_commands(self):
        calls = []
        replies = {"inspect": "false 137\n"}

        def runner(args, timeout=None):
            calls.append(list(args))
            if args[1] == "inspect" and "{{.State.Running}}" in args[3]:
                return main.CommandResult(0, replies["inspect"])
            if args[1] == "inspect":
                return main.CommandResult(0, "172.17.0.5\n")
            if args[1] == "exec" and timeout == 1:
                return main.CommandResult(-1, "", "timed out")
            return main.CommandResult(0, "ok\n")

        runtime = main.DockerRuntime(runner)
        pod = simple_pod(name="web")
        sandbox = runtime.run_sandbox(pod)
        self.assertEqual(sandbox, f"mk_default_web-node1_{pod.config_hash[:8]}")
        self.assertEqual(calls[1][:6], ["docker", "run", "-d", "--name", sandbox, "--ipc"])
        self.assertEqual(calls[1][-1], main.PAUSE_IMAGE)
        self.assertEqual(runtime.address(sandbox), "172.17.0.5")
        container = main.ContainerSpec("web", "python:3.12-alpine", ["python3", "-m", "http.server"], ["8000"], {"MODE": "x"})
        cid = runtime.start(sandbox, pod, container, 2)
        run = calls[-1]
        self.assertEqual(cid, f"{sandbox}_web_2")
        self.assertIn(f"container:{sandbox}", run)
        self.assertEqual(run[run.index("--network") + 1], f"container:{sandbox}")
        self.assertEqual(run[run.index("--entrypoint") + 1], "python3")
        self.assertEqual(run[run.index("python:3.12-alpine"):], ["python:3.12-alpine", "-m", "http.server", "8000"])
        self.assertIn("MODE=x", run)
        self.assertEqual(runtime.status(cid), 137)
        replies["inspect"] = "true 0\n"
        self.assertIsNone(runtime.status(cid))
        runtime.kill(cid, signal.SIGTERM)
        self.assertEqual(calls[-1], ["docker", "kill", "--signal", "SIGTERM", cid])
        self.assertEqual(runtime.exec_sync(cid, ["cat", "/tmp/ready"], 1)[0], -1)


class CliTests(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_validate_get_and_backoff_commands(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        with open(os.path.join(folder, "web.yaml"), "w") as handle:
            handle.write(WEB_YAML)
        code, out, err = self.run_main(["validate", folder, "--node", "kind-control-plane", "--json"])
        self.assertEqual(code, 0, err)
        summary = json.loads(out)[0]
        self.assertEqual(summary["name"], "web-kind-control-plane")
        self.assertEqual(summary["containers"][0]["probes"]["readiness"], "http-get http://:8000/index.html delay=0s timeout=1s period=2s #success=2 #failure=3")
        code, out, err = self.run_main(["validate", folder, "--node", "n"])
        self.assertRegex(out, r"Readiness: +http-get http://:8000/index.html delay=0s")
        with open(os.path.join(folder, "bad.yaml"), "w") as handle:
            handle.write("kind: Pod\nmetadata:\n\tname: x\n")
        code, out, err = self.run_main(["validate", os.path.join(folder, "bad.yaml")])
        self.assertEqual(code, 2)
        self.assertIn("bad.yaml:3: tabs are not allowed", err)
        status = os.path.join(folder, "pods.json")
        with open(status, "w") as handle:
            json.dump({"kind": "List", "items": [{"metadata": {"name": "c", "namespace": "ns", "creationTimestamp": "2026-11-01T10:00:00Z"}, "spec": {"containers": [{"name": "a"}]}, "status": {"phase": "Running", "containerStatuses": [{"ready": False, "restartCount": 2, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}]}, handle)
        code, out, err = self.run_main(["get", "pods", "-f", status, "--json"])
        self.assertEqual(code, 0, err)
        self.assertEqual({k: v for k, v in json.loads(out)[0].items() if k != "age"}, {"namespace": "ns", "name": "c", "ready": "0/1", "status": "CrashLoopBackOff", "restarts": 2})
        code, out, err = self.run_main(["backoff", "--base", "10", "--max", "300", "--restarts", "7", "--json"])
        self.assertEqual(json.loads(out), [0, 10, 20, 40, 80, 160, 300])

    def test_run_with_the_process_runtime_end_to_end(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        manifests = os.path.join(folder, "manifests")
        os.makedirs(manifests)
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "job", "namespace": "demo"},
            "spec": {
                "restartPolicy": "Never",
                "initContainers": [{"name": "prepare", "image": "x", "command": [sys.executable, "-c", "open('ready.txt', 'w').write('ok')"]}],
                "containers": [{"name": "work", "image": "x", "command": [sys.executable, "-c", "print(open('ready.txt').read())"]}],
            },
        }
        with open(os.path.join(manifests, "job.json"), "w") as handle:
            json.dump(pod, handle)
        events_path = os.path.join(folder, "events.jsonl")
        status_path = os.path.join(folder, "status.json")
        code, out, err = self.run_main(["run", "--manifests", manifests, "--node", "t", "--duration", "2.5", "--sync", "0.1", "--json", "--quiet", "--work", os.path.join(folder, "work"), "--events", events_path, "--status-file", status_path])
        self.assertEqual(code, 0, err)
        snapshot = json.loads(out)["items"][0]
        self.assertEqual(snapshot["metadata"]["name"], "job-t")
        self.assertEqual(snapshot["status"]["phase"], "Succeeded")
        self.assertEqual(main.status_reason(snapshot), "Completed")
        with open(events_path) as handle:
            logged = [json.loads(line) for line in handle]
        order = [(e["reason"], e["container"]) for e in logged if e["reason"] in ("Started", "Exited")]
        self.assertEqual(order, [("Started", "prepare"), ("Exited", "prepare"), ("Started", "work"), ("Exited", "work")])
        self.assertEqual([e["exitCode"] for e in logged if e["reason"] == "Exited"], [0, 0])
        logs = [name for name in os.listdir(os.path.join(folder, "work", f"demo_job-t_{snapshot['metadata']['uid'][:8]}")) if name.endswith(".log")]
        self.assertEqual(sorted(logs), ["prepare-1.log", "work-1.log"])
        with open(status_path) as handle:
            self.assertEqual(json.load(handle)["items"], [])
        code, out, err = self.run_main(["run", "--manifests", os.path.join(folder, "missing")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
