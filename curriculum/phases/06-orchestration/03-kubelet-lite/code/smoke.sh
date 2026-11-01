#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
NS="cc-kubelet-lite"
NODE="cc-smoke-control-plane"
STATIC="/etc/kubernetes/manifests/cc-static.yaml"
MIRROR="cc-static-$NODE"
K=(kubectl --context kind-cc-smoke)
KUBELET_PID=""
DOCKER_KUBELET_PID=""
CLUSTER=""
STATIC_WRITTEN=""

stop_kubelet() {
  local pid="$1"
  [[ -n "$pid" ]] || return 0
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 100); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
  kill -KILL "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}
cleanup() {
  stop_kubelet "$KUBELET_PID"
  stop_kubelet "$DOCKER_KUBELET_PID"
  pkill -KILL -f "$WORK/" 2>/dev/null || true
  if command -v docker >/dev/null; then
    LEFT="$(docker ps -aq --filter label=minikubelet.pod 2>/dev/null || true)"
    if [[ -n "$LEFT" ]]; then docker rm -f $LEFT >/dev/null 2>&1 || true; fi
  fi
  if [[ -n "$STATIC_WRITTEN" ]]; then
    docker exec "$NODE" rm -f "$STATIC" >/dev/null 2>&1 || true
    "${K[@]}" wait --for=delete "pod/$MIRROR" -n "$NS" --timeout=90s >/dev/null 2>&1 || true
  fi
  if [[ -n "$CLUSTER" ]]; then
    "${K[@]}" delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

poll() {
  local limit=$((SECONDS + $1))
  shift
  until "$@" >/dev/null 2>&1; do
    (( SECONDS < limit )) || return 1
    sleep 0.5
  done
}

wait_exit() {
  local pid="$1" code
  for _ in $(seq 1 150); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
  kill -0 "$pid" 2>/dev/null && fail "minikubelet (pid $pid) did not exit within 30s of SIGTERM"
  set +e
  wait "$pid"
  code=$?
  set -e
  [[ "$code" == "0" ]] || fail "minikubelet exited $code"
}

cat > "$WORK/check.py" <<'PY'
import json
import sys


def items(path):
    try:
        with open(path) as handle:
            return json.load(handle)["items"]
    except (OSError, ValueError, KeyError):
        return []


def pod(path, name):
    return next((p for p in items(path) if p["metadata"]["name"] == name), None)


mode, path, name = sys.argv[1:4]
found = pod(path, name)
if mode == "ready":
    ok = found is not None and any(c["type"] == "Ready" and c["status"] == "True" for c in found["status"]["conditions"])
elif mode == "restarts":
    ok = found is not None and found["status"]["containerStatuses"][0]["restartCount"] >= int(sys.argv[4])
elif mode == "gone":
    ok = found is None and items(path) != []
else:
    raise SystemExit("unknown mode " + mode)
sys.exit(0 if ok else 1)
PY

PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
mkdir -p "$WORK/manifests" "$WORK/docroot"
cat > "$WORK/manifests/web.yaml" <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: web
  namespace: demo
spec:
  terminationGracePeriodSeconds: 2
  initContainers:
    - name: write-index
      image: python:3.12-alpine
      command: ["python3", "-c", "import os, time; time.sleep(1); open(os.environ['DOCROOT'] + '/index.html', 'w').write('hello from init')"]
      env:
        - name: DOCROOT
          value: "$WORK/docroot"
  containers:
    - name: web
      image: python:3.12-alpine
      command: ["python3", "-m", "http.server", "$PORT", "--bind", "127.0.0.1", "--directory", "$WORK/docroot"]
      ports:
        - name: http
          containerPort: $PORT
      readinessProbe:
        httpGet:
          path: /index.html
          port: http
        initialDelaySeconds: 1
        periodSeconds: 1
        successThreshold: 2
EOF
cat > "$WORK/manifests/crash.json" <<'EOF'
{"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "crash", "namespace": "demo"},
 "spec": {"restartPolicy": "Always", "containers": [{"name": "crash", "image": "python:3.12-alpine",
   "command": ["python3", "-c", "import sys; print('crashing', flush=True); sys.exit(1)"]}]}}
EOF
cat > "$WORK/manifests/stubborn.yaml" <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: stubborn
  namespace: demo
spec:
  terminationGracePeriodSeconds: 1
  containers:
    - name: stubborn
      image: python:3.12-alpine
      command: ["python3", "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ignoring SIGTERM', flush=True); time.sleep(600)"]
EOF

echo "▸ validate: three manifests (YAML subset and JSON) parse; the probe line reads like kubectl describe"
python3 code/main.py validate "$WORK/manifests" --node smoke > "$WORK/validate.txt" 2>&1 || fail "validate: $(cat "$WORK/validate.txt")"
grep -qE "Readiness: +http-get http://:$PORT/index.html delay=1s timeout=1s period=1s #success=2 #failure=3" "$WORK/validate.txt" || fail "probe description: $(cat "$WORK/validate.txt")"
grep -q "^demo/web-smoke  from web.yaml" "$WORK/validate.txt" || fail "static pod naming: $(cat "$WORK/validate.txt")"

echo "▸ run --runtime process: init container, readiness probe, CrashLoopBackOff with a 0.5s base"
python3 code/main.py run --manifests "$WORK/manifests" --runtime process --node smoke --sync 0.2 --backoff-base 0.5 --backoff-max 4 \
  --status-file "$WORK/status.json" --events "$WORK/events.jsonl" --work "$WORK/kubelet" --quiet 2> "$WORK/kubelet.err" &
KUBELET_PID=$!
poll 30 python3 "$WORK/check.py" ready "$WORK/status.json" web-smoke || fail "web-smoke never became Ready: $(python3 code/main.py get pods -f "$WORK/status.json" 2>&1) $(cat "$WORK/kubelet.err")"
python3 -c 'import sys, urllib.request; body = urllib.request.urlopen(sys.argv[1], timeout=5).read().decode(); assert body == "hello from init", body' "http://127.0.0.1:$PORT/index.html" || fail "web did not serve the file its init container wrote"
poll 40 python3 "$WORK/check.py" restarts "$WORK/status.json" crash-smoke 5 || fail "crash-smoke did not reach 5 restarts: $(python3 code/main.py get pods -f "$WORK/status.json" 2>&1)"
table_has() { python3 code/main.py get pods -f "$WORK/status.json" > "$WORK/table.txt" && grep -qE "$1" "$WORK/table.txt"; }
poll 15 table_has "^demo +crash-smoke +0/1 +CrashLoopBackOff +[0-9]+" || fail "get pods should show CrashLoopBackOff: $(cat "$WORK/table.txt")"
grep -qE "^demo +web-smoke +1/1 +Running +0 " "$WORK/table.txt" || fail "get pods should show web-smoke 1/1 Running: $(cat "$WORK/table.txt")"
cat "$WORK/table.txt"
python3 - "$WORK/events.jsonl" <<'PY' || fail "event log: $(cat "$WORK/events.jsonl")"
import json, sys
events = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
def find(reason, container):
    return [e for e in events if e["reason"] == reason and e["container"] == container]
init_exit, web_start = find("Exited", "write-index"), find("Started", "web")
assert init_exit and init_exit[0]["exitCode"] == 0, init_exit
assert web_start and events.index(init_exit[0]) < events.index(web_start[0]), (init_exit, web_start)
assert find("Ready", "web"), "web never reported Ready"
delays = [e["delay"] for e in find("BackOff", "crash")]
assert delays[:4] == [0.5, 1.0, 2.0, 4.0], delays
crash = [e for e in events if e["container"] == "crash" and e["reason"] in ("Exited", "Started")]
gaps = [round(b["t"] - a["t"], 2) for a, b in zip(crash, crash[1:]) if a["reason"] == "Exited" and b["reason"] == "Started"]
assert gaps[0] < 0.3, gaps
assert all(gap >= delay - 0.05 for gap, delay in zip(gaps[1:], delays)), (gaps, delays)
assert gaps[1] < gaps[2] < gaps[3] < gaps[4], gaps
assert all(e["exitCode"] == 1 for e in find("Exited", "crash")), find("Exited", "crash")
print("  init exited 0 before web started; back-off delays", delays[:4], "measured restart gaps", gaps[:5])
PY

echo "▸ removing a manifest stops its pod: SIGTERM, then SIGKILL after the 2s minimum grace"
rm "$WORK/manifests/stubborn.yaml"
poll 15 python3 "$WORK/check.py" gone "$WORK/status.json" stubborn-smoke || fail "stubborn-smoke was not removed: $(python3 code/main.py get pods -f "$WORK/status.json" 2>&1)"
python3 - "$WORK/events.jsonl" <<'PY' || fail "graceful stop events: $(grep stubborn "$WORK/events.jsonl")"
import json, sys
events = [json.loads(line) for line in open(sys.argv[1]) if line.strip() and '"stubborn"' in line]
by = {e["reason"]: e for e in events}
assert by["Killing"]["message"] == "Stopping container stubborn", by["Killing"]
assert "GraceExpired" in by, [e["reason"] for e in events]
assert by["Exited"]["exitCode"] == 137, by["Exited"]
waited = by["Exited"]["t"] - by["Killing"]["t"]
assert waited >= 1.9, waited
print("  Killing -> SIGKILL -> exit 137 after %.1fs (terminationGracePeriodSeconds 1, raised to the 2s minimum)" % waited)
PY

echo "▸ SIGTERM to minikubelet stops every pod and leaves no processes behind"
kill -TERM "$KUBELET_PID"
wait_exit "$KUBELET_PID"
KUBELET_PID=""
python3 - "$WORK/events.jsonl" <<'PY' || fail "shutdown events"
import json, sys
events = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
removed = sorted(e["pod"] for e in events if e["reason"] == "PodRemoved")
assert removed == ["demo/crash-smoke", "demo/stubborn-smoke", "demo/web-smoke"], removed
web_exit = [e for e in events if e["reason"] == "Exited" and e["container"] == "web"]
assert web_exit and web_exit[-1]["exitCode"] == 143, web_exit
print("  web exited 143 on SIGTERM; pods removed:", ", ".join(removed))
PY
if pgrep -f "$WORK/docroot" > "$WORK/left.txt"; then fail "processes left behind: $(cat "$WORK/left.txt")"; fi

if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  echo "▸ run --runtime docker: a pause sandbox, two containers sharing localhost, PID 1 ignoring SIGTERM"
  docker pull -q registry.k8s.io/pause:3.10 >/dev/null || fail "pull registry.k8s.io/pause:3.10"
  docker pull -q python:3.12-alpine >/dev/null || fail "pull python:3.12-alpine"
  mkdir -p "$WORK/dmanifests"
  cat > "$WORK/dmanifests/dweb.yaml" <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: dweb
  namespace: demo
spec:
  terminationGracePeriodSeconds: 2
  containers:
    - name: web
      image: python:3.12-alpine
      command: ["python3", "-m", "http.server", "8000"]
      readinessProbe:
        httpGet:
          path: /
          port: 8000
        periodSeconds: 1
    - name: sidecar
      image: python:3.12-alpine
      command: ["sh", "-c", "until wget -qO- http://127.0.0.1:8000/ >/dev/null 2>&1; do sleep 0.5; done; echo reached-web-over-localhost; exec sleep 300"]
EOF
  python3 code/main.py run --manifests "$WORK/dmanifests" --runtime docker --node smoke --sync 0.5 \
    --status-file "$WORK/dstatus.json" --events "$WORK/devents.jsonl" --quiet 2> "$WORK/dkubelet.err" &
  DOCKER_KUBELET_PID=$!
  poll 90 python3 "$WORK/check.py" ready "$WORK/dstatus.json" dweb-smoke || fail "dweb-smoke never became Ready: $(python3 code/main.py get pods -f "$WORK/dstatus.json" 2>&1) $(cat "$WORK/dkubelet.err")"
  SIDE="$(docker ps -q --filter label=minikubelet.container=sidecar --filter label=minikubelet.pod=demo/dweb-smoke)"
  SANDBOX="$(docker ps -q --filter label=minikubelet.role=sandbox --filter label=minikubelet.pod=demo/dweb-smoke)"
  [[ -n "$SIDE" && -n "$SANDBOX" ]] || fail "expected a sandbox and a sidecar container: $(docker ps --filter label=minikubelet.pod)"
  sidecar_reached() { docker logs "$SIDE" > "$WORK/sidecar.log" 2>&1 && grep -q reached-web-over-localhost "$WORK/sidecar.log"; }
  poll 20 sidecar_reached || fail "the sidecar never reached web on 127.0.0.1: $(cat "$WORK/sidecar.log")"
  NETMODE="$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$SIDE")"
  [[ "$NETMODE" == container:* ]] || fail "sidecar should join the sandbox's network namespace, got $NETMODE"
  echo "  sandbox $(docker inspect -f '{{.Config.Image}}' "$SANDBOX"), sidecar network $NETMODE, reached web over localhost"
  kill -TERM "$DOCKER_KUBELET_PID"
  wait_exit "$DOCKER_KUBELET_PID"
  DOCKER_KUBELET_PID=""
  python3 - "$WORK/devents.jsonl" <<'PY' || fail "docker shutdown events: $(cat "$WORK/devents.jsonl")"
import json, sys
events = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
for name in ("web", "sidecar"):
    exits = [e["exitCode"] for e in events if e["reason"] == "Exited" and e["container"] == name]
    assert exits == [137], (name, exits)
    assert any(e["reason"] == "GraceExpired" and e["container"] == name for e in events), name
print("  both containers ignored SIGTERM as PID 1 and were killed after the grace period (exit 137)")
PY
  LEFT="$(docker ps -aq --filter label=minikubelet.pod)"
  [[ -z "$LEFT" ]] || fail "containers left behind: $LEFT"
else
  echo "  (docker not available; docker runtime step skipped)"
fi

kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if command -v kubectl >/dev/null && grep -qx kind-cc-smoke "$WORK/contexts.txt" && "${K[@]}" get --raw /readyz >/dev/null 2>&1; then
  CLUSTER=1
  "${K[@]}" create namespace "$NS" --dry-run=client -o yaml > "$WORK/ns.yaml"
  "${K[@]}" apply -f "$WORK/ns.yaml" >/dev/null || fail "create namespace $NS"
  mkdir -p "$WORK/cluster" "$WORK/static"
  cat > "$WORK/cluster/crash.yaml" <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: cc-crash
spec:
  restartPolicy: Always
  terminationGracePeriodSeconds: 1
  containers:
    - name: app
      image: alpine:3.22
      command: ["sh", "-c", "echo crashing; exit 1"]
EOF
  cat > "$WORK/cluster/ready.yaml" <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: cc-ready
spec:
  terminationGracePeriodSeconds: 1
  containers:
    - name: app
      image: alpine:3.22
      command: ["sleep", "3600"]
      readinessProbe:
        exec:
          command: ["cat", "/tmp/ready"]
        periodSeconds: 1
        failureThreshold: 1
EOF
  cat > "$WORK/static/cc-static.yaml" <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: cc-static
  namespace: $NS
spec:
  terminationGracePeriodSeconds: 1
  containers:
    - name: cc-static-app
      image: alpine:3.22
      command: ["sh", "-c", "echo static pod started; exec sleep 3600"]
EOF
  echo "▸ Use It: minikubelet parses the same manifests the cluster runs"
  python3 code/main.py validate "$WORK/cluster" "$WORK/static" --node "$NODE" --json > "$WORK/cluster-validate.json" || fail "validate cluster manifests"
  python3 -c 'import json, sys; names = sorted(p["name"] for p in json.load(open(sys.argv[1]))); assert names == ["cc-crash-" + sys.argv[2], "cc-ready-" + sys.argv[2], "cc-static-" + sys.argv[2]], names' "$WORK/cluster-validate.json" "$NODE" || fail "validated names"
  "${K[@]}" apply -n "$NS" -f "$WORK/cluster/crash.yaml" -f "$WORK/cluster/ready.yaml" >/dev/null || fail "apply crash and ready pods"

  echo "▸ Use It: the kind node's control plane runs as static pods with mirror pods"
  docker exec "$NODE" ls /etc/kubernetes/manifests > "$WORK/node-manifests.txt" || fail "cannot list the node's manifests"
  for f in etcd.yaml kube-apiserver.yaml kube-controller-manager.yaml kube-scheduler.yaml; do
    grep -qx "$f" "$WORK/node-manifests.txt" || fail "$f missing from /etc/kubernetes/manifests: $(cat "$WORK/node-manifests.txt")"
  done
  docker exec "$NODE" cat /var/lib/kubelet/config.yaml > "$WORK/kubelet-config.yaml" || fail "cannot read the node's kubelet config"
  grep -q "staticPodPath: /etc/kubernetes/manifests" "$WORK/kubelet-config.yaml" || fail "staticPodPath not found in the kubelet config"
  "${K[@]}" get pod -n kube-system "kube-apiserver-$NODE" -o json > "$WORK/apiserver.json" || fail "no kube-apiserver mirror pod"
  python3 - "$WORK/apiserver.json" "$NODE" <<'PY' || fail "mirror pod fields: $(cat "$WORK/apiserver.json")"
import json, sys
pod = json.load(open(sys.argv[1]))
ann = pod["metadata"]["annotations"]
assert ann["kubernetes.io/config.source"] == "file", ann
assert ann["kubernetes.io/config.mirror"] == ann["kubernetes.io/config.hash"], ann
owners = [(o["kind"], o["name"]) for o in pod["metadata"].get("ownerReferences", [])]
assert owners == [("Node", sys.argv[2])], owners
print("  kube-apiserver-%s: config.source=file, config.mirror == config.hash, owner Node/%s" % (sys.argv[2], sys.argv[2]))
PY

  echo "▸ Use It: a static pod written into the node's manifest directory gets a mirror pod"
  docker exec -i "$NODE" tee "$STATIC" < "$WORK/static/cc-static.yaml" >/dev/null || fail "write $STATIC"
  STATIC_WRITTEN=1
  poll 90 "${K[@]}" get pod "$MIRROR" -n "$NS" || fail "mirror pod $MIRROR never appeared: $(docker exec "$NODE" journalctl -u kubelet --no-pager -n 30 2>&1)"
  "${K[@]}" wait --for=condition=Ready "pod/$MIRROR" -n "$NS" --timeout=120s >/dev/null || fail "$MIRROR not Ready: $("${K[@]}" describe pod "$MIRROR" -n "$NS")"
  "${K[@]}" get pod "$MIRROR" -n "$NS" -o json > "$WORK/mirror.json" || fail "get $MIRROR"
  python3 - "$WORK/mirror.json" "$NODE" <<'PY' || fail "static mirror pod fields"
import json, sys
pod = json.load(open(sys.argv[1]))
ann = pod["metadata"]["annotations"]
assert pod["status"]["phase"] == "Running", pod["status"]["phase"]
assert pod["spec"]["nodeName"] == sys.argv[2], pod["spec"]["nodeName"]
assert ann["kubernetes.io/config.source"] == "file" and ann["kubernetes.io/config.mirror"] == ann["kubernetes.io/config.hash"], ann
print("  mirror pod %s Running on %s, config.hash %s" % (pod["metadata"]["name"], sys.argv[2], ann["kubernetes.io/config.hash"]))
PY
  python3 code/main.py get pods -f "$WORK/mirror.json" > "$WORK/mirror-table.txt" 2>&1 || fail "minikubelet get pods on the mirror pod: $(cat "$WORK/mirror-table.txt")"
  grep -qE "^$NS +$MIRROR +1/1 +Running" "$WORK/mirror-table.txt" || fail "minikubelet's printer on the real mirror pod: $(cat "$WORK/mirror-table.txt")"
  CID1=""
  if docker exec "$NODE" sh -c 'command -v crictl' >/dev/null 2>&1; then
    docker exec "$NODE" crictl ps --name cc-static-app -q > "$WORK/cid1.txt" || fail "crictl ps in the node"
    CID1="$(head -n 1 "$WORK/cid1.txt")"
    [[ -n "$CID1" ]] || fail "crictl ps shows no cc-static-app container"
    docker exec "$NODE" crictl logs "$CID1" > "$WORK/static.log" 2>&1
    grep -q "static pod started" "$WORK/static.log" || fail "crictl logs: $(cat "$WORK/static.log")"
    echo "  crictl ps: container ${CID1:0:13} running, crictl logs: $(cat "$WORK/static.log")"
  else
    echo "  (crictl not installed in the node; container checks skipped)"
  fi

  echo "▸ deleting the mirror pod does not stop a static pod; the kubelet recreates the mirror"
  UID1="$("${K[@]}" get pod "$MIRROR" -n "$NS" -o jsonpath='{.metadata.uid}')" || fail "read the mirror pod uid"
  "${K[@]}" delete pod "$MIRROR" -n "$NS" --wait=false >/dev/null || fail "delete the mirror pod"
  mirror_replaced() { local uid; uid="$("${K[@]}" get pod "$MIRROR" -n "$NS" -o jsonpath='{.metadata.uid}')" || return 1; [[ -n "$uid" && "$uid" != "$UID1" ]]; }
  poll 90 mirror_replaced || fail "the kubelet did not recreate the mirror pod"
  if [[ -n "$CID1" ]]; then
    docker exec "$NODE" crictl ps --name cc-static-app -q > "$WORK/cid2.txt" || fail "crictl ps in the node"
    [[ "$(head -n 1 "$WORK/cid2.txt")" == "$CID1" ]] || fail "the static pod's container changed ($CID1 -> $(cat "$WORK/cid2.txt")); deleting a mirror pod must not restart it"
    echo "  new mirror pod uid, same container ${CID1:0:13}"
  fi

  echo "▸ removing the manifest file stops the static pod and its mirror pod goes away"
  docker exec "$NODE" rm -f "$STATIC" || fail "remove $STATIC"
  "${K[@]}" wait --for=delete "pod/$MIRROR" -n "$NS" --timeout=90s >/dev/null || fail "mirror pod still present after removing $STATIC"
  STATIC_WRITTEN=""
  if [[ -n "$CID1" ]]; then
    container_gone() { docker exec "$NODE" crictl ps --name cc-static-app -q > "$WORK/cid3.txt" && [[ ! -s "$WORK/cid3.txt" ]]; }
    poll 30 container_gone || fail "cc-static-app still running in the node: $(cat "$WORK/cid3.txt")"
  fi

  same_status() {
    "${K[@]}" get pod "$1" -n "$NS" -o json > "$WORK/one.json" || return 1
    "${K[@]}" get pod "$1" -n "$NS" --no-headers > "$WORK/one.txt" || return 1
    python3 code/main.py get pods -f "$WORK/one.json" --json > "$WORK/one-mini.json" || return 1
    python3 -c 'import json, sys; mini = json.load(open(sys.argv[1]))[0]; cols = open(sys.argv[2]).read().split(); sys.exit(0 if cols[1:3] == [mini["ready"], mini["status"]] and mini["status"] in sys.argv[3].split("|") else 1)' "$WORK/one-mini.json" "$WORK/one.txt" "$2"
  }

  echo "▸ Use It: a failing readiness probe keeps a pod NotReady without restarting it"
  "${K[@]}" wait --for=jsonpath='{.status.phase}'=Running pod/cc-ready -n "$NS" --timeout=120s >/dev/null || fail "cc-ready not Running: $("${K[@]}" describe pod cc-ready -n "$NS")"
  unhealthy() {
    "${K[@]}" get events -n "$NS" --field-selector involvedObject.name=cc-ready,reason=Unhealthy -o json > "$WORK/unhealthy.json" || return 1
    python3 -c 'import json, sys; sys.exit(0 if any("Readiness probe failed" in e.get("message", "") for e in json.load(open(sys.argv[1]))["items"]) else 1)' "$WORK/unhealthy.json"
  }
  poll 60 unhealthy || fail "no Unhealthy event for cc-ready: $(cat "$WORK/unhealthy.json")"
  [[ "$("${K[@]}" get pod cc-ready -n "$NS" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')" == "False" ]] || fail "cc-ready should not be Ready yet"
  "${K[@]}" describe pod cc-ready -n "$NS" > "$WORK/describe.txt" || fail "describe cc-ready"
  PROBE_LINE="$(python3 -c 'import json, sys; print(next(p for p in json.load(open(sys.argv[1])) if p["name"].startswith("cc-ready"))["containers"][0]["probes"]["readiness"])' "$WORK/cluster-validate.json")"
  grep -qF "$PROBE_LINE" "$WORK/describe.txt" || fail "kubectl describe should show the probe as minikubelet prints it ($PROBE_LINE): $(grep -i readiness "$WORK/describe.txt")"
  echo "  kubectl describe: Readiness: $PROBE_LINE"
  poll 20 same_status cc-ready Running || fail "minikubelet vs kubectl STATUS for cc-ready: $(cat "$WORK/one.txt") / $(cat "$WORK/one-mini.json")"
  "${K[@]}" exec cc-ready -n "$NS" -- touch /tmp/ready || fail "kubectl exec touch /tmp/ready"
  "${K[@]}" wait --for=condition=Ready pod/cc-ready -n "$NS" --timeout=60s >/dev/null || fail "cc-ready did not become Ready after touch /tmp/ready"
  [[ "$("${K[@]}" get pod cc-ready -n "$NS" -o jsonpath='{.status.containerStatuses[0].restartCount}')" == "0" ]] || fail "a readiness failure must not restart the container"
  poll 20 same_status cc-ready Running || fail "minikubelet vs kubectl STATUS for cc-ready after Ready"
  echo "  NotReady while /tmp/ready was missing, Ready after touch, restartCount 0; kubectl and minikubelet print: $(cat "$WORK/one.txt")"

  echo "▸ Use It: a crashing pod reaches CrashLoopBackOff with restartCount >= 2 and phase Running"
  crashloop() {
    "${K[@]}" get pod cc-crash -n "$NS" -o json > "$WORK/crash.json" || return 1
    python3 -c 'import json, sys; c = json.load(open(sys.argv[1]))["status"]["containerStatuses"][0]; sys.exit(0 if c["restartCount"] >= 2 and (c["state"].get("waiting") or {}).get("reason") == "CrashLoopBackOff" else 1)' "$WORK/crash.json"
  }
  poll 150 crashloop || fail "cc-crash did not reach CrashLoopBackOff with 2 restarts: $("${K[@]}" describe pod cc-crash -n "$NS")"
  python3 - "$WORK/crash.json" <<'PY' || fail "crash pod status: $(cat "$WORK/crash.json")"
import json, sys
pod = json.load(open(sys.argv[1]))
c = pod["status"]["containerStatuses"][0]
assert pod["status"]["phase"] == "Running", pod["status"]["phase"]
assert c["lastState"]["terminated"]["exitCode"] == 1, c["lastState"]
print("  phase Running, state.waiting.reason CrashLoopBackOff, restartCount %d, lastState exit code 1" % c["restartCount"])
print("  " + c["state"]["waiting"].get("message", ""))
PY
  "${K[@]}" logs cc-crash -n "$NS" --previous > "$WORK/previous.log" 2>&1 || true
  grep -q crashing "$WORK/previous.log" || fail "kubectl logs --previous: $(cat "$WORK/previous.log")"
  "${K[@]}" get events -n "$NS" --field-selector involvedObject.name=cc-crash,reason=BackOff -o json > "$WORK/backoff.json" || fail "get BackOff events"
  python3 -c 'import json, sys; m = [e["message"] for e in json.load(open(sys.argv[1]))["items"]]; assert any("Back-off restarting failed container" in x for x in m), m; print("  event:", m[0])' "$WORK/backoff.json" || fail "no BackOff event for cc-crash"
  poll 60 same_status cc-crash "CrashLoopBackOff|Error" || fail "minikubelet vs kubectl STATUS for cc-crash: $(cat "$WORK/one.txt") / $(cat "$WORK/one-mini.json")"
  echo "  kubectl and minikubelet print: $(cat "$WORK/one.txt")"

  "${K[@]}" delete namespace "$NS" --wait=false >/dev/null
  CLUSTER=""
else
  echo "  (no kind-cc-smoke cluster reachable; Use It cluster steps skipped)"
fi

echo "SMOKE OK: orchestration/kubelet-lite"
