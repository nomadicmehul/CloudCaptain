#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
APP="cc-gitops"
NS="cc-gitops-reconciler"
ARGO_NS="cc-gitops-reconciler-argo"
CTX="${CC_SMOKE_CONTEXT:-kind-cc-smoke}"
RUN_ARGOCD="${CC_SMOKE_ARGOCD:-1}"
ARGOCD_VERSION="v3.5.6"
ARGOCD_MANIFEST_SHA256="d3d9d3555ad77eb4b6d27b1ec0d2af7978a91537e84d54631f983c227f0a3d31"
PIDS=()
NS_CREATED=""
ARGO_APPLIED=""
k() { kubectl --context "$CTX" "$@"; }
mg() { python3 code/main.py "$@"; }
G=(git -c user.name=cc-smoke -c user.email=smoke@example.invalid -c init.defaultBranch=main -c advice.detachedHead=false)
cleanup() {
  set +e
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do { kill "$pid"; wait "$pid"; } 2>/dev/null; done
  if [[ -n "$ARGO_APPLIED" ]]; then
    k -n argocd patch application "$APP" --type=merge -p '{"metadata":{"finalizers":null}}' >/dev/null 2>&1
    k -n argocd delete application "$APP" --ignore-not-found --wait=false >/dev/null 2>&1
    k -n argocd delete appproject "$APP" --ignore-not-found --wait=false >/dev/null 2>&1
    if [[ -f "$WORK/argocd-core.yaml" ]]; then k -n argocd delete -f "$WORK/argocd-core.yaml" --ignore-not-found --wait=false >/dev/null 2>&1; fi
    k delete namespace argocd --ignore-not-found --wait=false >/dev/null 2>&1
  fi
  if [[ -n "$NS_CREATED" ]]; then k delete namespace "$NS" "$ARGO_NS" --ignore-not-found --wait=false >/dev/null 2>&1; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

cat > "$WORK/same_objects.py" <<'PY'
import json
import sys


def documents(path):
    text, index, decoder, docs = open(path).read(), 0, json.JSONDecoder(), []
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            return docs
        value, index = decoder.raw_decode(text, index)
        docs.append(value)


def load(path):
    out = {}
    for data in documents(path):
        for item in data["items"] if data.get("kind") == "List" else [data]:
            meta = item.setdefault("metadata", {})
            for key in ("namespace", "creationTimestamp"):
                meta.pop(key, None)
            if not item.get("status"):
                item.pop("status", None)
            out["%s/%s" % (item["kind"], meta["name"])] = item
    return out


def differences(a, b, path=""):
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            yield from differences(a.get(key), b.get(key), "%s.%s" % (path, key) if path else key)
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for index, (x, y) in enumerate(zip(a, b)):
            yield from differences(x, y, "%s[%d]" % (path, index))
    elif a != b:
        yield "%s: minigitops %s, %s %s" % (path, json.dumps(a), sys.argv[3], json.dumps(b))


mine, theirs = load(sys.argv[1]), load(sys.argv[2])
if sorted(mine) != sorted(theirs):
    sys.exit("different objects: minigitops %s, %s %s" % (sorted(mine), sys.argv[3], sorted(theirs)))
problems = [line for name in sorted(mine) for line in differences(mine[name], theirs[name], name)]
if problems:
    sys.exit("\n".join(problems))
print("  %d objects, field for field identical: %s" % (len(mine), ", ".join(sorted(mine))))
PY

cat > "$WORK/check_status.py" <<'PY'
import json
import sys

try:
    status = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    raise SystemExit(1)
raise SystemExit(0 if eval(sys.argv[2], {"s": status}) else 1)
PY

cat > "$WORK/git_http.py" <<'PY'
import http.server
import os
import subprocess
import sys
import urllib.parse

root, host, port = sys.argv[1], sys.argv[2], int(sys.argv[3])


class GitHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def handle_git(self):
        url = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_PROJECT_ROOT": root, "GIT_HTTP_EXPORT_ALL": "1",
               "REQUEST_METHOD": self.command, "PATH_INFO": urllib.parse.unquote(url.path), "QUERY_STRING": url.query,
               "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)), "REMOTE_ADDR": self.client_address[0]}
        if self.headers.get("Content-Encoding"):
            env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
        if self.headers.get("Git-Protocol"):
            env["GIT_PROTOCOL"] = self.headers["Git-Protocol"]
        result = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True)
        head, separator, payload = result.stdout.partition(b"\r\n\r\n")
        if not separator:
            head, _, payload = result.stdout.partition(b"\n\n")
        status, headers = 200, []
        for line in head.decode("latin-1").splitlines():
            name, _, value = line.partition(":")
            if name.lower() == "status":
                status = int(value.strip().split()[0])
            elif name:
                headers.append((name, value.strip()))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = handle_git
    do_POST = handle_git

    def log_message(self, fmt, *args):
        sys.stderr.write("git-http %s %s\n" % (self.address_string(), fmt % args))


http.server.ThreadingHTTPServer((host, port), GitHandler).serve_forever()
PY

echo "▸ minigitops render on the fixture overlay: prefix, labels, image tag and patch"
mg render --dir code/fixtures/app --path overlays/prod --json > "$WORK/render-dir.json" 2> "$WORK/render-dir.err" || fail "render --dir: $(cat "$WORK/render-dir.err")"
python3 - "$WORK/render-dir.json" <<'PY' || fail "rendered fixture: $(cat "$WORK/render-dir.json")"
import json, sys
items = {"%s/%s" % (i["kind"], i["metadata"]["name"]): i for i in json.load(open(sys.argv[1]))["items"]}
assert sorted(items) == ["ConfigMap/prod-legacy-flags", "ConfigMap/prod-web-config", "Deployment/prod-web", "Job/prod-migrate", "Service/prod-web"], sorted(items)
web = items["Deployment/prod-web"]
container = web["spec"]["template"]["spec"]["containers"][0]
assert (web["spec"]["replicas"], container["image"]) == (2, "registry.k8s.io/pause:3.9"), (web["spec"]["replicas"], container["image"])
assert container["envFrom"] == [{"configMapRef": {"name": "prod-web-config"}}] and container["env"] == [{"name": "MODE", "value": "prod"}], container
assert web["metadata"]["labels"]["team"] == "shop" and "team" not in web["spec"]["selector"]["matchLabels"], web["metadata"]
print("  5 objects; prod-web: 2 replicas of registry.k8s.io/pause:3.9, envFrom prod-web-config, MODE=prod")
PY

echo "▸ minigitops demo: the whole loop on the in-memory cluster"
mg demo --json > "$WORK/demo.json" 2> "$WORK/demo.err" || fail "demo: $(cat "$WORK/demo.err")"
python3 - "$WORK/demo.json" <<'PY' || fail "demo assertions: $(head -c 3000 "$WORK/demo.json")"
import json, sys
result = json.load(open(sys.argv[1]))
steps = result["steps"]
assert [s["operation"] for s in steps] == ["initial sync", "new revision", "new revision", "self-heal"], steps
assert [s["image"].rsplit(":", 1)[1] for s in steps] == ["3.9", "3.10", "3.10", "3.10"], steps
assert [s["legacy_flags"] for s in steps] == [True, True, False, False], steps
assert all((s["sync"], s["health"], s["replicas"]) == ("Synced", "Healthy", 2) for s in steps), steps
print("  initial sync, new tag, prune, self-heal: every step ends Synced/Healthy with 2 replicas")
PY

if ! command -v git >/dev/null; then
  echo "  (git not installed; Git and cluster checks skipped)"
  echo "SMOKE OK: cicd/gitops-reconciler (render and demo only)"
  exit 0
fi

echo "▸ a bare repository is the Git host, a working clone is the developer"
mkdir -p "$WORK/git" "$WORK/src"
"${G[@]}" init --quiet --bare "$WORK/git/app.git" || fail "git init --bare"
"${G[@]}" -C "$WORK/src" init --quiet || fail "git init"
cp -R code/fixtures/app/. "$WORK/src/"
commit() {
  "${G[@]}" -C "$WORK/src" add -A >/dev/null &&
    "${G[@]}" -C "$WORK/src" commit --quiet -m "$1" >/dev/null &&
    "${G[@]}" -C "$WORK/src" push --quiet "$WORK/git/app.git" HEAD:main >/dev/null 2>&1 &&
    "${G[@]}" -C "$WORK/src" rev-parse HEAD
}
REV1="$(commit "initial app")" || fail "first commit"
REPO="$WORK/git/app.git"
echo "  main is at ${REV1:0:7}"

echo "▸ render from the Git revision equals render from the working tree"
mg render --repo "$REPO" --path overlays/prod --cache "$WORK/cache-render.git" --json > "$WORK/render-git.json" 2> "$WORK/render-git.err" || fail "render --repo: $(cat "$WORK/render-git.err")"
python3 "$WORK/same_objects.py" "$WORK/render-git.json" "$WORK/render-dir.json" "render --dir" || fail "render from Git and from the directory differ"

if ! command -v kubectl >/dev/null; then
  echo "  (kubectl not installed; cluster checks skipped)"
  echo "SMOKE OK: cicd/gitops-reconciler (minigitops only)"
  exit 0
fi
kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if ! grep -qx "$CTX" "$WORK/contexts.txt"; then
  echo "  (context $CTX not found; the cluster checks only run against the throwaway kind cluster)"
  echo "SMOKE OK: cicd/gitops-reconciler (minigitops only)"
  exit 0
fi

echo "▸ kubectl kustomize renders the same overlay to the same objects"
k kustomize "$WORK/src/overlays/prod" > "$WORK/kustomize.yaml" 2> "$WORK/kustomize.err" || fail "kubectl kustomize: $(cat "$WORK/kustomize.err")"
k create --dry-run=client -o json -f "$WORK/kustomize.yaml" > "$WORK/kustomize.json" 2> "$WORK/kustomize-json.err" || fail "kubectl create --dry-run=client on the kustomize output: $(cat "$WORK/kustomize-json.err")"
python3 "$WORK/same_objects.py" "$WORK/render-git.json" "$WORK/kustomize.json" "kustomize" || fail "minigitops render and kubectl kustomize disagree (see above); kustomize output: $(head -c 2500 "$WORK/kustomize.yaml")"

k delete namespace "$NS" "$ARGO_NS" --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1 || true
NS_CREATED=1
k create namespace "$NS" >/dev/null || fail "create namespace $NS"
k create namespace "$ARGO_NS" >/dev/null || fail "create namespace $ARGO_NS"
for _ in $(seq 1 30); do k get serviceaccount default -n "$NS" >/dev/null 2>&1 && k get serviceaccount default -n "$ARGO_NS" >/dev/null 2>&1 && break; sleep 1; done
COMMON=(--repo "$REPO" --path overlays/prod --app "$APP" --namespace "$NS" --context "$CTX" --cache "$WORK/cache.git" --health-timeout 180)

echo "▸ status before the first sync: OutOfSync, every resource Missing, exit 1"
CODE=0
mg status "${COMMON[@]}" --json > "$WORK/status0.json" 2> "$WORK/status0.err" || CODE=$?
[[ "$CODE" == "1" ]] || fail "status on an empty namespace should exit 1, got $CODE: $(cat "$WORK/status0.err")"
python3 - "$WORK/status0.json" <<'PY' || fail "status before sync: $(head -c 3000 "$WORK/status0.json")"
import json, sys
s = json.load(open(sys.argv[1]))
assert (s["sync"]["status"], s["health"]["status"]) == ("OutOfSync", "Missing"), (s["sync"], s["health"])
assert sorted((r["kind"], r["name"], r["health"]) for r in s["resources"]) == [("ConfigMap", "prod-legacy-flags", "Missing"), ("ConfigMap", "prod-web-config", "Missing"), ("Deployment", "prod-web", "Missing"), ("Service", "prod-web", "Missing")], s["resources"]
assert s["hooks"] == ["PreSync Job/prod-migrate"], s["hooks"]
print("  4 resources Missing, 1 PreSync hook, revision %s" % s["revision"][:7])
PY

echo "▸ minigitops sync: PreSync hook, then wave -1, then wave 0, server-side apply as field manager minigitops"
mg sync "${COMMON[@]}" --prune --json > "$WORK/sync1.json" 2> "$WORK/sync1.err" || fail "first sync: $(cat "$WORK/sync1.err") $(head -c 4000 "$WORK/sync1.json")"
python3 - "$WORK/sync1.json" <<'PY' || fail "first sync result: $(head -c 4000 "$WORK/sync1.json")"
import json, sys
s = json.load(open(sys.argv[1]))
steps = [(r["step"], "%s/%s" % (r["kind"], r["name"]), r["action"]) for r in s["operation"]["resources"]]
assert steps == [("PreSync", "Job/prod-migrate", "hook created"), ("PreSync", "Job/prod-migrate", "hook Healthy"),
                 ("wave -1", "ConfigMap/prod-web-config", "created"), ("wave 0", "ConfigMap/prod-legacy-flags", "created"),
                 ("wave 0", "Service/prod-web", "created"), ("wave 0", "Deployment/prod-web", "created")], steps
drift = ["%s/%s %s" % (r["kind"], r["name"], d) for r in s["resources"] for d in r["diff"]]
assert (s["operation"]["phase"], s["sync"]["status"], s["health"]["status"]) == ("Succeeded", "Synced", "Healthy"), (s["operation"]["message"], s["sync"], s["health"], drift)
print("  " + "; ".join("%s %s %s" % step for step in steps))
PY

echo "▸ the live objects carry the tracking label, the Apply entry of minigitops, and the hook Job completed"
k get deployment prod-web -n "$NS" -o json > "$WORK/deploy1.json" || fail "get deployment prod-web"
python3 - "$WORK/deploy1.json" "$APP" <<'PY' || fail "live Deployment: $(head -c 3000 "$WORK/deploy1.json")"
import json, sys
d, app = json.load(open(sys.argv[1])), sys.argv[2]
labels = d["metadata"]["labels"]
assert labels.get("app.kubernetes.io/instance") == app and labels.get("team") == "shop", labels
entries = [(m["manager"], m["operation"]) for m in d["metadata"]["managedFields"]]
assert ("minigitops", "Apply") in entries, entries
container = d["spec"]["template"]["spec"]["containers"][0]
assert (d["spec"]["replicas"], container["image"]) == (2, "registry.k8s.io/pause:3.9"), (d["spec"]["replicas"], container["image"])
print("  managedFields: %s; defaulted by the API server and ignored by the diff: strategy=%s, imagePullPolicy=%s" % (entries, d["spec"]["strategy"]["type"], container["imagePullPolicy"]))
PY
[[ "$(k get job prod-migrate -n "$NS" -o jsonpath='{.status.succeeded}')" == "1" ]] || fail "the PreSync hook Job should have succeeded: $(k get job prod-migrate -n "$NS" -o wide 2>&1)"

echo "▸ right after the sync, status is Synced and Healthy: defaulted fields, status and managedFields are not drift"
mg status "${COMMON[@]}" > "$WORK/status1.txt" 2>&1 || fail "status after sync should exit 0: $(cat "$WORK/status1.txt")"
grep -q "^Sync:      Synced$" "$WORK/status1.txt" || fail "status after sync: $(cat "$WORK/status1.txt")"

echo "▸ minigitops watch: poll main every 2s with prune and self-heal"
mg watch "${COMMON[@]}" --prune --self-heal --interval 2 --status-file "$WORK/watch.json" > "$WORK/watch.log" 2>&1 &
PIDS+=("$!")
wait_status() {
  for _ in $(seq 1 "$2"); do
    if python3 "$WORK/check_status.py" "$WORK/watch.json" "$1"; then return 0; fi
    sleep 1
  done
  fail "timed out waiting for $3; watch log: $(tail -40 "$WORK/watch.log")"
}
wait_status "s['revision'] == '$REV1' and s['sync']['status'] == 'Synced'" 60 "the watcher's first reconcile"

echo "▸ a commit that changes the image tag is picked up and rolled out"
sed -i.bak 's/newTag: "3.9"/newTag: "3.10"/' "$WORK/src/overlays/prod/kustomization.yaml" && rm "$WORK/src/overlays/prod/kustomization.yaml.bak"
REV2="$(commit "bump web to pause 3.10")" || fail "second commit"
wait_status "s['revision'] == '$REV2' and s['sync']['status'] == 'Synced' and s['health']['status'] == 'Healthy'" 150 "revision ${REV2:0:7} Synced/Healthy"
IMAGE="$(k get deployment prod-web -n "$NS" -o jsonpath='{.spec.template.spec.containers[0].image}')"
[[ "$IMAGE" == "registry.k8s.io/pause:3.10" ]] || fail "the new tag should be live, got $IMAGE"
grep -q "syncing (new revision)" "$WORK/watch.log" || fail "the watch log should show a sync for the new revision: $(tail -20 "$WORK/watch.log")"
grep -q 'containers\[name=web\].image: Git "registry.k8s.io/pause:3.10", live "registry.k8s.io/pause:3.9"' "$WORK/watch.log" || fail "the watch log should show the image diff: $(tail -20 "$WORK/watch.log")"
echo "  ${REV2:0:7} is live: $IMAGE"

echo "▸ a commit that deletes legacy-flags.yaml prunes the ConfigMap by its tracking label"
sed -i.bak '/legacy-flags.yaml/d' "$WORK/src/base/kustomization.yaml" && rm "$WORK/src/base/kustomization.yaml.bak" "$WORK/src/base/legacy-flags.yaml"
REV3="$(commit "remove legacy feature flags")" || fail "third commit"
wait_status "s['revision'] == '$REV3' and s['sync']['status'] == 'Synced'" 150 "revision ${REV3:0:7} Synced"
for _ in $(seq 1 30); do [[ -z "$(k get configmap prod-legacy-flags -n "$NS" --ignore-not-found -o name)" ]] && break; sleep 1; done
[[ -z "$(k get configmap prod-legacy-flags -n "$NS" --ignore-not-found -o name)" ]] || fail "prod-legacy-flags should have been pruned"
grep -q "ConfigMap/prod-legacy-flags *pruned" "$WORK/watch.log" || fail "the watch log should show the prune: $(tail -20 "$WORK/watch.log")"
echo "  ConfigMap/prod-legacy-flags pruned"

echo "▸ kubectl scale by hand is drift: the watcher reverts it (self-heal)"
k scale deployment prod-web -n "$NS" --replicas=5 >/dev/null || fail "kubectl scale"
k wait --for=jsonpath='{.spec.replicas}'=2 deployment/prod-web -n "$NS" --timeout=120s >/dev/null || fail "self-heal should set replicas back to 2: $(tail -30 "$WORK/watch.log")"
wait_status "s['sync']['status'] == 'Synced' and [h['reason'] for h in s['history']][-1:] == ['self-heal']" 60 "a self-heal sync in the history"
grep -q "spec.replicas: Git 2, live 5" "$WORK/watch.log" || fail "the watch log should show the replicas drift: $(tail -20 "$WORK/watch.log")"
echo "  replicas 5 -> 2; history: $(python3 -c 'import json,sys; print(", ".join(h["reason"] for h in json.load(open(sys.argv[1]))["history"]))' "$WORK/watch.json")"
kill "${PIDS[0]}" 2>/dev/null || true
wait "${PIDS[0]}" 2>/dev/null || true
PIDS=()

echo "▸ without a watcher the same drift is reported (exit 1) and one sync takes the field back with --force-conflicts"
k scale deployment prod-web -n "$NS" --replicas=4 >/dev/null || fail "kubectl scale"
k get deployment prod-web -n "$NS" -o json > "$WORK/deploy-scaled.json"
CODE=0
mg status "${COMMON[@]}" > "$WORK/status2.txt" 2>&1 || CODE=$?
[[ "$CODE" == "1" ]] || fail "status with drift should exit 1, got $CODE: $(cat "$WORK/status2.txt")"
grep -q "spec.replicas: Git 2, live 4" "$WORK/status2.txt" || fail "status should print the drift: $(cat "$WORK/status2.txt")"
mg sync "${COMMON[@]}" --prune --json > "$WORK/sync2.json" 2> "$WORK/sync2.err" || fail "sync after drift: $(cat "$WORK/sync2.err") $(head -c 3000 "$WORK/sync2.json")"
k get deployment prod-web -n "$NS" -o json > "$WORK/deploy2.json"
python3 - "$WORK/deploy-scaled.json" "$WORK/deploy2.json" <<'PY' || fail "field ownership of spec.replicas"
import json, sys
def owners(path):
    d = json.load(open(path))
    return d["spec"]["replicas"], sorted("%s/%s%s" % (m["manager"], m["operation"], "/" + m["subresource"] if m.get("subresource") else "")
                                         for m in d["metadata"]["managedFields"] if "f:replicas" in (m.get("fieldsV1") or {}).get("f:spec", {}))
before, after = owners(sys.argv[1]), owners(sys.argv[2])
assert after[0] == 2 and "minigitops/Apply" in after[1], after
print("  spec.replicas owners after kubectl scale: %s; after the sync: %s" % (before[1], after[1]))
PY

if [[ "$RUN_ARGOCD" != "1" ]]; then
  echo "  (CC_SMOKE_ARGOCD=$RUN_ARGOCD; Argo CD part skipped)"
  echo "SMOKE OK: cicd/gitops-reconciler (minigitops on $CTX)"
  exit 0
fi
if k get crd applications.argoproj.io >/dev/null 2>&1 || k get namespace argocd >/dev/null 2>&1; then
  echo "  (Argo CD or an argocd namespace already exists in $CTX; this smoke installs its own copy only on a clean cluster, Argo CD part skipped)"
  echo "SMOKE OK: cicd/gitops-reconciler (minigitops on $CTX)"
  exit 0
fi
if ! command -v docker >/dev/null || ! command -v curl >/dev/null; then
  echo "  (docker or curl not installed; the Git server address on the kind network cannot be found, Argo CD part skipped)"
  echo "SMOKE OK: cicd/gitops-reconciler (minigitops on $CTX)"
  exit 0
fi

echo "▸ the same bare repository over smart HTTP (git http-backend), on the kind network gateway"
NODE="$(k get nodes -o jsonpath='{.items[0].metadata.name}')"
docker inspect "$NODE" --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{"\n"}}{{end}}' > "$WORK/gateway.txt" || fail "docker inspect $NODE"
GW="$(awk '/^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ {print; exit}' "$WORK/gateway.txt")"
[[ -n "$GW" ]] || fail "no IPv4 gateway for $NODE: $(cat "$WORK/gateway.txt")"
PORT="$(python3 -c 'import socket, sys; s = socket.socket(); s.bind((sys.argv[1], 0)); print(s.getsockname()[1]); s.close()' "$GW")"
python3 "$WORK/git_http.py" "$WORK/git" "$GW" "$PORT" 2> "$WORK/git-http.log" &
PIDS+=("$!")
URL="http://$GW:$PORT/app.git"
for _ in $(seq 1 50); do git ls-remote "$URL" refs/heads/main > "$WORK/ls-remote.txt" 2>/dev/null && break; sleep 0.2; done
grep -q "^$REV3" "$WORK/ls-remote.txt" || fail "git ls-remote $URL: $(cat "$WORK/ls-remote.txt") $(cat "$WORK/git-http.log")"
cat > "$WORK/probe.json" <<EOF
{"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "cc-git-probe", "namespace": "$ARGO_NS"},
 "spec": {"restartPolicy": "Never", "terminationGracePeriodSeconds": 0, "containers": [{"name": "probe", "image": "alpine:3.22",
  "command": ["wget", "-q", "-O", "-", "$URL/info/refs?service=git-upload-pack"]}]}}
EOF
k apply -f "$WORK/probe.json" >/dev/null || fail "create the probe pod"
k wait --for=jsonpath='{.status.phase}'=Succeeded pod/cc-git-probe -n "$ARGO_NS" --timeout=90s >/dev/null || fail "a pod cannot fetch $URL: $(k logs cc-git-probe -n "$ARGO_NS" 2>&1) $(k get pod cc-git-probe -n "$ARGO_NS" -o wide 2>&1)"
k logs cc-git-probe -n "$ARGO_NS" > "$WORK/probe.txt" 2>&1
grep -q "service=git-upload-pack" "$WORK/probe.txt" || fail "unexpected answer from $URL: $(head -c 300 "$WORK/probe.txt")"
k delete pod cc-git-probe -n "$ARGO_NS" --wait=false >/dev/null
echo "  $URL answers git clients on the runner and inside the cluster"

echo "▸ Argo CD $ARGOCD_VERSION core install (manifest pinned by sha256, ApplicationSet controller left out)"
curl -fsSL --retry 3 -o "$WORK/core-install.yaml" "https://raw.githubusercontent.com/argoproj/argo-cd/$ARGOCD_VERSION/manifests/core-install.yaml" || fail "download core-install.yaml"
GOT="$(sha256sum "$WORK/core-install.yaml" | awk '{print $1}')"
[[ "$GOT" == "$ARGOCD_MANIFEST_SHA256" ]] || fail "core-install.yaml sha256 is $GOT, expected $ARGOCD_MANIFEST_SHA256"
python3 - "$WORK/core-install.yaml" "$WORK/argocd-core.yaml" <<'PY' || fail "filter the Argo CD manifest"
import re, sys
kept, dropped = [], []
for doc in re.split(r"(?m)^---[ \t]*$", open(sys.argv[1], encoding="utf-8").read()):
    if not doc.strip():
        continue
    kind = re.search(r"(?m)^kind: *(\S+)", doc)
    meta = re.search(r"(?ms)^metadata:\n(.*?)(?=^\S)", doc + "\nend:")
    name = re.search(r"(?m)^  name: *(\S+)", meta.group(1)) if meta else None
    if not (kind and name):
        raise SystemExit("cannot read kind and metadata.name of a document: " + doc[:200])
    if kind.group(1) != "CustomResourceDefinition" and name.group(1).startswith("argocd-applicationset-controller"):
        dropped.append("%s/%s" % (kind.group(1), name.group(1)))
    else:
        kept.append(doc.strip("\n"))
open(sys.argv[2], "w", encoding="utf-8").write("\n---\n".join(kept) + "\n")
print("  %d objects applied; left out %s" % (len(kept), ", ".join(dropped)))
PY
ARGO_APPLIED=1
k create namespace argocd >/dev/null || fail "create namespace argocd"
k -n argocd apply --server-side --force-conflicts -f "$WORK/argocd-core.yaml" > "$WORK/argocd-apply.txt" 2>&1 || fail "apply Argo CD: $(tail -20 "$WORK/argocd-apply.txt")"
k wait --for=condition=Established --timeout=60s crd/applications.argoproj.io crd/appprojects.argoproj.io >/dev/null || fail "Argo CD CRDs not Established"
for workload in deployment/argocd-redis deployment/argocd-repo-server statefulset/argocd-application-controller; do
  k -n argocd rollout status "$workload" --timeout=300s > "$WORK/rollout.txt" 2>&1 || fail "$workload did not become ready: $(cat "$WORK/rollout.txt") $(k -n argocd get pods -o wide 2>&1)"
done
echo "  redis, repo-server and application-controller are ready"

echo "▸ an Application with automated sync, prune and self-heal on the same repository"
cat > "$WORK/application.yaml" <<EOF
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: $APP
  namespace: argocd
spec:
  sourceRepos:
    - $URL
  destinations:
    - server: https://kubernetes.default.svc
      namespace: $ARGO_NS
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: $APP
  namespace: argocd
spec:
  project: $APP
  source:
    repoURL: $URL
    targetRevision: main
    path: overlays/prod
  destination:
    server: https://kubernetes.default.svc
    namespace: $ARGO_NS
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
EOF
k apply -f "$WORK/application.yaml" >/dev/null || fail "apply the Application"
argo_state() { k -n argocd get application "$APP" -o jsonpath='sync={.status.sync.status} health={.status.health.status} op={.status.operationState.phase} msg={.status.operationState.message} conditions={.status.conditions}' 2>&1; }
k -n argocd wait --for=jsonpath='{.status.sync.status}'=Synced "application/$APP" --timeout=240s >/dev/null || fail "Application never became Synced: $(argo_state)"
k -n argocd wait --for=jsonpath='{.status.health.status}'=Healthy "application/$APP" --timeout=240s >/dev/null || fail "Application never became Healthy: $(argo_state)"
k -n argocd get application "$APP" -o json > "$WORK/application.json"
k get deployment prod-web -n "$ARGO_NS" -o json > "$WORK/argo-deploy.json" || fail "Argo CD did not create prod-web"
k get deployment prod-web -n "$NS" -o json > "$WORK/mini-deploy.json"
python3 - "$WORK/application.json" "$WORK/argo-deploy.json" "$WORK/mini-deploy.json" "$REV3" "$APP" "$ARGO_NS" <<'PY' || fail "Argo CD result: $(argo_state)"
import json, sys
app, argo, mini = (json.load(open(p)) for p in sys.argv[1:4])
revision, name, namespace = sys.argv[4:7]
assert app["status"]["sync"]["revision"] == revision, (app["status"]["sync"], revision)
assert app["status"]["operationState"]["phase"] == "Succeeded", app["status"]["operationState"]
resources = sorted("%s/%s" % (r["kind"], r["name"]) for r in app["status"].get("resources", []) if r["kind"] != "Job")
assert resources == ["ConfigMap/prod-web-config", "Deployment/prod-web", "Service/prod-web"], resources
tracking = argo["metadata"].get("annotations", {}).get("argocd.argoproj.io/tracking-id", "")
assert tracking.startswith(name + ":apps/Deployment:") and tracking.endswith("/prod-web"), tracking
def shape(d):
    c = d["spec"]["template"]["spec"]["containers"][0]
    return d["spec"]["replicas"], c["image"], c.get("env"), c.get("envFrom"), d["metadata"]["labels"].get("team")
assert shape(argo) == shape(mini), (shape(argo), shape(mini))
managers = sorted({"%s/%s" % (m["manager"], m["operation"]) for m in argo["metadata"]["managedFields"]})
print("  Synced/Healthy at %s; resources %s; tracking-id %s" % (revision[:7], resources, tracking))
print("  same live shape as minigitops: replicas=%s image=%s env=%s envFrom=%s team=%s" % shape(argo))
print("  field managers on Argo CD's Deployment: %s" % managers)
PY
[[ "$(k get job prod-migrate -n "$ARGO_NS" -o jsonpath='{.status.succeeded}')" == "1" ]] || fail "Argo CD should have run the PreSync hook Job: $(k get jobs -n "$ARGO_NS" 2>&1)"

echo "▸ Argo CD self-heals the same kubectl scale"
k scale deployment prod-web -n "$ARGO_NS" --replicas=5 >/dev/null || fail "kubectl scale in $ARGO_NS"
k wait --for=jsonpath='{.spec.replicas}'=2 deployment/prod-web -n "$ARGO_NS" --timeout=180s >/dev/null || fail "Argo CD did not revert replicas: $(argo_state)"
k -n argocd wait --for=jsonpath='{.status.sync.status}'=Synced "application/$APP" --timeout=120s >/dev/null || fail "Application not Synced after self-heal: $(argo_state)"
INITIATED="$(k -n argocd get application "$APP" -o jsonpath='{.status.operationState.operation.initiatedBy.automated}')"
[[ "$INITIATED" == "true" ]] || fail "the last operation should be automated, initiatedBy.automated=$INITIATED: $(argo_state)"
echo "  replicas 5 -> 2 by an automated sync; $(argo_state | cut -c1-120)"

echo "SMOKE OK: cicd/gitops-reconciler"
