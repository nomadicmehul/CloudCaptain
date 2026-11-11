#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
NS="cc-platform-api"
CTX="kind-cc-smoke"
LABEL="cloudcaptain.io/smoke=platform-api"
SCORE_K8S_VERSION="0.22.0"
SCORE_K8S_SHA256_AMD64="d32f190d6278943d85cfecfacdee31146ff56b5fa148d6fe2850da1fa237d2f0"
SCORE_K8S_SHA256_ARM64="b5c34a9aeed687e029ba8d347827ae862a63fcfd99bdeca9d65cf3faa07f8b86"
GATEWAY_API_VERSION="v1.7.0"
SAMPLES=(code/fixtures/storefront.json code/fixtures/orders.json)
CLUSTER=""
GW_CRDS_APPLIED=""

k() { kubectl --context "$CTX" "$@"; }

cleanup() {
  set +e
  if [[ -n "$CLUSTER" ]]; then
    k delete namespace "$NS" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1
    k delete crd -l "$LABEL" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1
    if [[ -n "$GW_CRDS_APPLIED" ]]; then
      k delete -f "$WORK/standard-install.yaml" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1
    fi
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "▸ PyYAML 6.0.3 in a venv with pinned hashes (to read score-k8s's YAML)"
cat > "$WORK/requirements.txt" <<'EOF'
pyyaml==6.0.3 \
    --hash=sha256:ba1cc08a7ccde2d2ec775841541641e4548226580ab850948cbfda66a1befcdc \
    --hash=sha256:9149cad251584d5fb4981be1ecde53a1ca46c891a79788c0df828d2f166bda28 \
    --hash=sha256:41715c910c881bc081f1e8872880d3c650acf13dfa8214bad49ed4cede7c34ea \
    --hash=sha256:8dc52c23056b9ddd46818a57b78404882310fb473d63f17b07d5c40421e47f8e
EOF
python3 -m venv "$WORK/venv" > "$WORK/venv.txt" 2>&1 || fail "python3 -m venv: $(cat "$WORK/venv.txt")"
"$WORK/venv/bin/pip" install --quiet --disable-pip-version-check --require-hashes --only-binary :all: -r "$WORK/requirements.txt" > "$WORK/pip.txt" 2>&1 \
  || fail "pip install pyyaml (cp312 manylinux and musllinux wheels for x86_64 and aarch64 are pinned; python3 is $(python3 --version)): $(cat "$WORK/pip.txt")"
PYY="$WORK/venv/bin/python"

echo "▸ miniplatform validate: the samples pass, every planted mistake fails with its own message"
python3 code/main.py validate "${SAMPLES[@]}" > "$WORK/valid.txt" 2>&1 || fail "samples should be valid: $(cat "$WORK/valid.txt")"
rc=0
python3 code/main.py validate --json code/fixtures/invalid.json > "$WORK/invalid.json" 2>&1 || rc=$?
[[ "$rc" == "1" ]] || fail "validate on invalid.json should exit 1, got $rc: $(cat "$WORK/invalid.json")"
python3 - "$WORK/invalid.json" <<'PY' || fail "validation messages: $(cat "$WORK/invalid.json")"
import json, sys
fixtures = {i["metadata"]["name"]: i["metadata"]["annotations"]["cc.test/expect"] for i in json.load(open("code/fixtures/invalid.json"))["items"]}
results = json.load(open(sys.argv[1]))["results"]
assert len(results) == len(fixtures), (len(results), len(fixtures))
for r in results:
    assert not r["valid"] and any(fixtures[r["name"]] in e for e in r["errors"]), r
print(f"  {len(results)} invalid Workloads rejected, each with its expected message")
PY

echo "▸ miniplatform render: two Workloads become 17 objects; the YAML and JSON outputs hold the same objects"
python3 code/main.py render "${SAMPLES[@]}" --summary > "$WORK/summary.txt" 2> "$WORK/render-warnings.txt" || fail "render --summary: $(cat "$WORK/render-warnings.txt")"
tail -1 "$WORK/summary.txt"
grep -q "uses the development password seed" "$WORK/render-warnings.txt" || fail "expected the dev-seed warning on stderr: $(cat "$WORK/render-warnings.txt")"
python3 code/main.py render "${SAMPLES[@]}" -o yaml > "$WORK/rendered.yaml" 2>/dev/null
python3 code/main.py render "${SAMPLES[@]}" -o json > "$WORK/rendered.json" 2>/dev/null
"$PYY" - "$WORK/rendered.yaml" "$WORK/rendered.json" <<'PY' || fail "YAML and JSON renders differ"
import json, sys, yaml
from_yaml = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
from_json = json.load(open(sys.argv[2]))["items"]
assert from_yaml == from_json, "the YAML render parses to different objects than the JSON render"
assert len(from_json) == 17, len(from_json)
print(f"  {len(from_json)} objects, identical after parsing both outputs")
PY

echo "▸ miniplatform convert: v1alpha1 -> v1beta1 -> v1alpha1 is lossless, and so is the other direction"
python3 code/main.py convert code/fixtures/storefront.json --to v1beta1 > "$WORK/storefront.v1beta1.json"
python3 code/main.py convert "$WORK/storefront.v1beta1.json" --to v1alpha1 > "$WORK/storefront.v1alpha1.json"
python3 code/main.py convert code/fixtures/storefront-autoscaled.json --to v1alpha1 > "$WORK/autoscaled.v1alpha1.json"
python3 code/main.py convert "$WORK/autoscaled.v1alpha1.json" --to v1beta1 > "$WORK/autoscaled.v1beta1.json"
python3 - "$WORK" <<'PY' || fail "conversion round trips"
import json, sys
work = sys.argv[1]
load = lambda p: json.load(open(p))
assert load(f"{work}/storefront.v1alpha1.json") == load("code/fixtures/storefront.json")
assert load(f"{work}/autoscaled.v1beta1.json") == load("code/fixtures/storefront-autoscaled.json")
kept = load(f"{work}/autoscaled.v1alpha1.json")["metadata"]["annotations"]["platform.cloudcaptain.io/v1beta1-spec"]
print(f"  both round trips equal their input; v1alpha1 kept {kept} in an annotation")
PY

echo "▸ miniplatform diff: what a spec change does to the generated objects"
rc=0
python3 code/main.py diff code/fixtures/orders.json code/fixtures/orders-next.json > "$WORK/diff.txt" 2>/dev/null || rc=$?
[[ "$rc" == "1" ]] || fail "diff should exit 1 when objects change, got $rc: $(cat "$WORK/diff.txt")"
grep -q "~ Deployment/orders   \[restarts pods\]" "$WORK/diff.txt" || fail "diff should mark the pod restart: $(cat "$WORK/diff.txt")"
grep -q "spec.rules\[0\].matches\[0\].path.value" "$WORK/diff.txt" || fail "diff should show the route path: $(cat "$WORK/diff.txt")"
tail -1 "$WORK/diff.txt"
rc=0
python3 code/main.py diff code/fixtures/storefront.json code/fixtures/storefront-autoscaled.json > "$WORK/diff-hpa.txt" 2>/dev/null || rc=$?
[[ "$rc" == "1" ]] && grep -q "resets the Deployment to 1 replica (from 2)" "$WORK/diff-hpa.txt" || fail "replicas to HPA warning: $(cat "$WORK/diff-hpa.txt")"
echo "  replicas -> autoscaling warns that removing spec.replicas resets the Deployment to 1"

echo "▸ score-k8s $SCORE_K8S_VERSION from GitHub releases, sha256 pinned and checked against checksums.txt"
case "$(uname -m)" in
  x86_64) ARCH=amd64; WANT="$SCORE_K8S_SHA256_AMD64" ;;
  aarch64|arm64) ARCH=arm64; WANT="$SCORE_K8S_SHA256_ARM64" ;;
  *) fail "unsupported architecture $(uname -m)" ;;
esac
mkdir -p "$WORK/dl" "$WORK/bin" "$WORK/score"
TARBALL="score-k8s_${SCORE_K8S_VERSION}_linux_${ARCH}.tar.gz"
BASE="https://github.com/score-spec/score-k8s/releases/download/$SCORE_K8S_VERSION"
curl -fsSL --retry 3 -o "$WORK/dl/$TARBALL" "$BASE/$TARBALL" || fail "download $TARBALL"
curl -fsSL --retry 3 -o "$WORK/dl/checksums.txt" "$BASE/checksums.txt" || fail "download checksums.txt"
LISTED="$(awk -v f="$TARBALL" '$2 == f {print $1}' "$WORK/dl/checksums.txt")"
GOT="$(sha256sum "$WORK/dl/$TARBALL" | awk '{print $1}')"
[[ "$GOT" == "$WANT" && "$LISTED" == "$WANT" ]] || fail "score-k8s checksum: pinned $WANT, checksums.txt $LISTED, downloaded $GOT"
tar -xzf "$WORK/dl/$TARBALL" -C "$WORK/bin" score-k8s
SCORE="$WORK/bin/score-k8s"
"$SCORE" --version > "$WORK/score-version.txt" 2>&1 || fail "score-k8s --version: $(cat "$WORK/score-version.txt")"
head -1 "$WORK/score-version.txt"

echo "▸ the same two Workloads through score-k8s (via miniplatform to-score) and miniplatform: shared fields agree"
python3 code/main.py to-score "${SAMPLES[@]}" --out "$WORK/score" > /dev/null
( cd "$WORK/score" && "$SCORE" init --no-sample && "$SCORE" generate orders.score.yaml storefront.score.yaml --namespace "$NS" ) > "$WORK/score.log" 2>&1 || fail "score-k8s generate: $(cat "$WORK/score.log")"
python3 code/main.py render "${SAMPLES[@]}" -o json --namespace "$NS" > "$WORK/mini.json" 2>/dev/null
"$PYY" - "$WORK/score/manifests.yaml" "$WORK/mini.json" <<'PY' || fail "score-k8s and miniplatform disagree on a shared field"
import json, sys, yaml

score = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
mini = json.load(open(sys.argv[2]))["items"]
outputs = {"score-k8s": score, "miniplatform": mini}


def by(objs, kind, name=None):
    return [o for o in objs if o["kind"] == kind and (name is None or o["metadata"]["name"] == name)]


def one(objs, kind, name):
    found = by(objs, kind, name)
    assert len(found) == 1, (kind, name, sorted(f"{o['kind']}/{o['metadata']['name']}" for o in objs))
    return found[0]


def container(workload):
    containers = workload["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1, containers
    return containers[0]


def port_number(c, port):
    return port if isinstance(port, int) else next(p["containerPort"] for p in c.get("ports", []) if p.get("name") == port)


def routes_to(objs, service):
    return [r for r in by(objs, "HTTPRoute") if any(b["name"] == service for rule in r["spec"]["rules"] for b in rule["backendRefs"])]


notes = []
compared = 0
for name in ("orders", "storefront"):
    deploy = {tool: one(objs, "Deployment", name) for tool, objs in outputs.items()}
    c = {tool: container(d) for tool, d in deploy.items()}
    for field in ("name", "image", "command", "args", "resources"):
        assert c["score-k8s"].get(field) == c["miniplatform"].get(field), (name, field, c["score-k8s"].get(field), c["miniplatform"].get(field))
        compared += 1
    for probe in ("readinessProbe", "livenessProbe"):
        got = {tool: (x[probe]["httpGet"]["path"], port_number(x, x[probe]["httpGet"]["port"])) for tool, x in c.items()}
        assert got["score-k8s"] == got["miniplatform"], (name, probe, got)
        compared += 1
    env = {tool: {e["name"]: e for e in x.get("env", [])} for tool, x in c.items()}
    assert sorted(env["score-k8s"]) == sorted(env["miniplatform"]), (name, sorted(env["score-k8s"]), sorted(env["miniplatform"]))
    for var in sorted(env["score-k8s"]):
        pair = {tool: e[var] for tool, e in env.items()}
        if any("valueFrom" in v for v in pair.values()):
            for tool, v in pair.items():
                ref = v["valueFrom"]["secretKeyRef"]
                assert ref["key"] == "password" and by(outputs[tool], "Secret", ref["name"]), (tool, var, v)
        elif var.endswith("_HOST"):
            for tool, v in pair.items():
                assert by(outputs[tool], "Service", v["value"]), (tool, var, v)
            notes.append(f"{name} {var}: score-k8s {pair['score-k8s']['value']}, miniplatform {pair['miniplatform']['value']} (both a Service in their own output)")
        elif var.endswith("_URL"):
            for v in pair.values():
                assert v["value"].startswith(f"http://{var[:-4].lower()}:"), (var, pair)
            notes.append(f"{name} {var}: score-k8s {pair['score-k8s']['value']}, miniplatform {pair['miniplatform']['value']} (service-port outputs the target port)")
        elif var in ("DB_NAME", "DB_USER"):
            notes.append(f"{name} {var}: score-k8s {pair['score-k8s']['value']} (random, kept in .score-k8s/state.yaml), miniplatform {pair['miniplatform']['value']}")
        else:
            assert pair["score-k8s"]["value"] == pair["miniplatform"]["value"], (name, var, pair)
        compared += 1
    for tool, objs in outputs.items():
        service = one(objs, "Service", name)
        labels = deploy[tool]["spec"]["template"]["metadata"]["labels"]
        assert all(labels.get(k) == v for k, v in service["spec"]["selector"].items()), (tool, name, "Service selector does not select the pods")
    ports = {tool: [(p["port"], port_number(c[tool], p["targetPort"]), p.get("protocol", "TCP"), p.get("name")) for p in one(objs, "Service", name)["spec"]["ports"]] for tool, objs in outputs.items()}
    assert ports["score-k8s"] == ports["miniplatform"], (name, ports)
    compared += 1
    route = {}
    for tool, objs in outputs.items():
        found = routes_to(objs, name)
        assert len(found) == 1, (tool, name, found)
        route[tool] = found[0]["spec"]
    for field in ("hostnames", "rules"):
        assert route["score-k8s"][field] == route["miniplatform"][field], (name, field, route["score-k8s"][field], route["miniplatform"][field])
        compared += 1
    notes.append(f"{name} HTTPRoute parentRefs: score-k8s {route['score-k8s']['parentRefs']}, miniplatform {route['miniplatform']['parentRefs']}")
    notes.append(f"{name} Deployment spec.replicas: score-k8s {deploy['score-k8s']['spec'].get('replicas', 'unset')}, miniplatform {deploy['miniplatform']['spec'].get('replicas', 'unset (the HPA owns it)')}")
    notes.append(f"{name} pod securityContext: score-k8s {deploy['score-k8s']['spec']['template']['spec'].get('securityContext', 'none')}, miniplatform runAsNonRoot={deploy['miniplatform']['spec']['template']['spec']['securityContext']['runAsNonRoot']}")
for tool, objs in outputs.items():
    sts = by(objs, "StatefulSet")
    assert len(sts) == 1 and "postgres" in container(sts[0])["image"], (tool, sts)
    assert one(objs, "Service", sts[0]["spec"]["serviceName"])["spec"]["ports"][0]["port"] == 5432, tool
only_mini = sorted({o["kind"] for o in mini} - {o["kind"] for o in score})
only_score = sorted({o["kind"] for o in score} - {o["kind"] for o in mini})
assert only_mini == ["HorizontalPodAutoscaler", "NetworkPolicy", "PodDisruptionBudget", "ServiceAccount"], only_mini
assert only_score == [], only_score
print(f"  {compared} shared fields equal (image, command, resources, probes, env, Service ports, route hosts and rules); both run postgres in a StatefulSet")
print(f"  kinds only miniplatform renders: {', '.join(only_mini)}")
print("  documented differences:")
for note in notes:
    print(f"    {note}")
PY

kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if ! command -v kubectl >/dev/null || ! grep -qx "$CTX" "$WORK/contexts.txt"; then
  echo "  (no $CTX context; CRD, CEL, dry-run and apply steps on kind skipped)"
  echo "SMOKE OK: platform/platform-api"
  exit 0
fi
k cluster-info > "$WORK/cluster-info.txt" 2>&1 || fail "context $CTX is not reachable: $(cat "$WORK/cluster-info.txt")"
CLUSTER=1

established() {
  local state=""
  for _ in $(seq 1 60); do
    state="$(k get crd "$1" -o jsonpath='{.status.conditions[?(@.type=="Established")].status}' 2>/dev/null || true)"
    [[ "$state" == "True" ]] && return 0
    sleep 1
  done
  fail "CRD $1 not Established after 60s: $(k get crd "$1" -o jsonpath='{.status.conditions}' 2>&1)"
}

k delete namespace "$NS" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1 || true
k create namespace "$NS" >/dev/null
k label namespace "$NS" "$LABEL" >/dev/null

echo "▸ Gateway API $GATEWAY_API_VERSION standard channel CRDs (installed only if absent, labelled, removed in the trap)"
if k get crd httproutes.gateway.networking.k8s.io >/dev/null 2>&1; then
  echo "  (Gateway API CRDs already installed; using and keeping them)"
else
  curl -fsSL --retry 3 -o "$WORK/standard-install.yaml" "https://github.com/kubernetes-sigs/gateway-api/releases/download/$GATEWAY_API_VERSION/standard-install.yaml" || fail "download standard-install.yaml"
  GW_CRDS_APPLIED=1
  k apply --server-side -f "$WORK/standard-install.yaml" > "$WORK/gw-crds.txt" 2>&1 || fail "apply Gateway API CRDs: $(cat "$WORK/gw-crds.txt")"
  k label -f "$WORK/standard-install.yaml" "$LABEL" --overwrite > "$WORK/gw-label.txt" 2>&1 || fail "label Gateway API objects: $(cat "$WORK/gw-label.txt")"
  for crd in gatewayclasses gateways httproutes referencegrants; do established "$crd.gateway.networking.k8s.io"; done
  echo "  $(grep -c customresourcedefinition "$WORK/gw-crds.txt" || true) CRDs applied and Established"
fi

echo "▸ the Workload CRD from 'miniplatform crd' (v1beta1, OpenAPI schema plus CEL rules)"
python3 code/main.py crd > "$WORK/crd.json"
python3 - "$WORK/crd.json" "$LABEL" <<'PY'
import json, sys
path, label = sys.argv[1], sys.argv[2]
crd = json.load(open(path))
key, value = label.split("=")
crd["metadata"]["labels"][key] = value
json.dump(crd, open(path, "w"))
PY
k apply -f "$WORK/crd.json" > "$WORK/crd.txt" 2>&1 || fail "the API server refused the Workload CRD (schema or CEL cost): $(cat "$WORK/crd.txt")"
established workloads.platform.cloudcaptain.io

echo "▸ validation parity: miniplatform validate and kubectl apply --dry-run=server on the same Workloads"
python3 - "$CTX" "$NS" "$WORK" <<'PY' || fail "validation parity"
import json, subprocess, sys
ctx, ns, work = sys.argv[1:4]
valid = [
    json.load(open("code/fixtures/orders.json")),
    json.loads(subprocess.run([sys.executable, "code/main.py", "convert", "code/fixtures/storefront.json", "--to", "v1beta1"], capture_output=True, text=True, check=True).stdout),
]
for obj in valid:
    obj["metadata"].setdefault("annotations", {}).update({"cc.test/expect": "", "cc.test/kubernetes": "accept"})
cases = valid + json.load(open("code/fixtures/invalid.json"))["items"]
print(f"  {'workload':<46} {'miniplatform':<13} kubernetes")
for obj in cases:
    name = obj["metadata"]["name"]
    notes = obj["metadata"]["annotations"]
    token, want_kube = notes["cc.test/expect"], notes["cc.test/kubernetes"]
    path = f"{work}/wl-{name}.json"
    with open(path, "w") as handle:
        json.dump(obj, handle)
    mini = subprocess.run([sys.executable, "code/main.py", "validate", path], capture_output=True, text=True)
    kube = subprocess.run(["kubectl", "--context", ctx, "-n", ns, "apply", "--dry-run=server", "--validate=strict", "-f", path], capture_output=True, text=True)
    got_mini = "accept" if mini.returncode == 0 else "reject"
    got_kube = "accept" if kube.returncode == 0 else "reject"
    assert got_mini == ("reject" if token else "accept"), (name, mini.stdout, mini.stderr)
    assert not token or token in mini.stdout, (name, token, mini.stdout)
    assert got_kube == want_kube, (name, "kubernetes", got_kube, kube.stdout, kube.stderr)
    assert got_kube == "accept" or token in kube.stderr, (name, "kubernetes message", token, kube.stderr)
    print(f"  {name:<46} {got_mini:<13} {got_kube}{'  (the CRD cannot see this rule)' if got_mini != got_kube else ''}")
PY

echo "▸ the Gateway API's own CEL rules: a redirect together with backendRefs is refused before any controller sees it"
cat > "$WORK/bad-route.json" <<EOF
{"apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute", "metadata": {"name": "bad", "namespace": "$NS"},
 "spec": {"parentRefs": [{"name": "cc-platform-api"}], "rules": [{"filters": [{"type": "RequestRedirect", "requestRedirect": {"statusCode": 301}}], "backendRefs": [{"name": "storefront", "port": 80}]}]}}
EOF
rc=0
k apply --dry-run=server -f "$WORK/bad-route.json" > "$WORK/bad-route.txt" 2>&1 || rc=$?
[[ "$rc" != "0" ]] && grep -q "RequestRedirect" "$WORK/bad-route.txt" || fail "the HTTPRoute CRD should refuse a redirect with backendRefs: $(cat "$WORK/bad-route.txt")"
echo "  refused: $(grep -o 'RequestRedirect[^"]*backendRefs' "$WORK/bad-route.txt" | head -1)"

echo "▸ render into $NS, kubectl apply --dry-run=server, then for real"
python3 code/main.py render "${SAMPLES[@]}" -o json --namespace "$NS" --gateway "$NS/cc-platform-api" > "$WORK/objects.json" 2>/dev/null
k get gatewayclasses.gateway.networking.k8s.io -o json > "$WORK/classes.json" 2>/dev/null || echo '{"items": []}' > "$WORK/classes.json"
CLASS="$(python3 - "$WORK/classes.json" <<'PY'
import json, sys
for item in json.load(open(sys.argv[1])).get("items", []):
    if any(c.get("type") == "Accepted" and c.get("status") == "True" for c in (item.get("status") or {}).get("conditions", [])):
        print(item["metadata"]["name"])
        break
PY
)"
k apply -n "$NS" -f - > /dev/null <<EOF
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: cc-platform-api
spec:
  gatewayClassName: ${CLASS:-cc-platform-api-no-controller}
  listeners:
    - name: http
      port: 80
      protocol: HTTP
      allowedRoutes:
        namespaces:
          from: Same
EOF
k apply --dry-run=server -f "$WORK/objects.json" > "$WORK/dry-run.txt" 2>&1 || fail "server dry run: $(cat "$WORK/dry-run.txt")"
echo "  dry run: $(grep -c 'server dry run' "$WORK/dry-run.txt" || true) objects accepted by admission"
k apply -f "$WORK/objects.json" > "$WORK/apply.txt" 2>&1 || fail "apply: $(cat "$WORK/apply.txt")"
k rollout status deployment/storefront -n "$NS" --timeout=180s > /dev/null || fail "storefront not ready: $(k get pods -n "$NS" -o wide 2>&1; k describe pods -n "$NS" -l app.kubernetes.io/name=storefront 2>&1 | tail -30)"
k rollout status deployment/orders -n "$NS" --timeout=180s > /dev/null || fail "orders not ready: $(k get pods -n "$NS" -o wide 2>&1; k describe pods -n "$NS" -l app.kubernetes.io/name=orders 2>&1 | tail -30)"
k rollout status statefulset/orders-db -n "$NS" --timeout=180s > /dev/null || fail "orders-db not ready: $(k get pods -n "$NS" -o wide 2>&1; k logs -n "$NS" statefulset/orders-db 2>&1 | tail -20)"
echo "  Deployments storefront and orders Ready (non-root, read-only root filesystem), StatefulSet orders-db Ready"

echo "▸ the HPA owns orders' replicas: the manifest has none, the HPA raises the Deployment to minReplicas"
READY=""
for _ in $(seq 1 75); do
  READY="$(k get deployment orders -n "$NS" -o jsonpath='{.spec.replicas}/{.status.readyReplicas}' 2>/dev/null || true)"
  [[ "$READY" == "2/2" ]] && break
  sleep 2
done
[[ "$READY" == "2/2" ]] || fail "orders should reach 2/2 replicas from the HPA's minReplicas, got $READY: $(k describe hpa orders -n "$NS" 2>&1 | tail -15)"
echo "  orders spec.replicas/readyReplicas = $READY"

echo "▸ HTTPRoute admitted by the API server; its status depends on a controller owning the GatewayClass"
k get httproute storefront -n "$NS" -o json > "$WORK/route.json" || fail "HTTPRoute storefront missing"
if [[ -n "$CLASS" ]]; then
  ACCEPTED=""
  for _ in $(seq 1 60); do
    ACCEPTED="$(k get httproute storefront -n "$NS" -o jsonpath='{.status.parents[0].conditions[?(@.type=="Accepted")].status}' 2>/dev/null || true)"
    [[ "$ACCEPTED" == "True" ]] && break
    sleep 2
  done
  [[ "$ACCEPTED" == "True" ]] || fail "HTTPRoute not Accepted by the controller of GatewayClass $CLASS: $(k get httproute storefront -n "$NS" -o jsonpath='{.status}' 2>&1)"
  echo "  GatewayClass $CLASS: HTTPRoute storefront Accepted=True"
else
  PARENTS="$(k get httproute storefront -n "$NS" -o jsonpath='{.status.parents}' 2>/dev/null || true)"
  [[ -z "$PARENTS" || "$PARENTS" == "[]" ]] || fail "no controller owns the class, yet status.parents is $PARENTS"
  echo "  (no GatewayClass is accepted by a controller on this cluster: the route is stored and validated, status.parents stays empty, as the gateway-api lesson explains)"
fi

echo "▸ dependencies became wiring: env vars and NetworkPolicies, checked from inside the pods"
inpod() { k exec -n "$NS" "deploy/$1" -- python -c "$2" "${@:3}"; }
CONNECT='import socket, sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3).close(); print("open")'
allowed() {
  local out=""
  for _ in $(seq 1 20); do
    out="$(inpod "$1" "$CONNECT" "$2" "$3" 2>&1 || true)"
    [[ "$out" == "open" ]] && return 0
    sleep 3
  done
  fail "$1 -> $2:$3 should be allowed: $out"
}
STATUS="$(inpod storefront 'import os, urllib.request; print(urllib.request.urlopen(os.environ["ORDERS_URL"] + "/", timeout=5).status)' 2>&1 || true)"
for _ in $(seq 1 20); do
  [[ "$STATUS" == "200" ]] && break
  sleep 3
  STATUS="$(inpod storefront 'import os, urllib.request; print(urllib.request.urlopen(os.environ["ORDERS_URL"] + "/", timeout=5).status)' 2>&1 || true)"
done
[[ "$STATUS" == "200" ]] || fail "storefront -> \$ORDERS_URL should answer 200: $STATUS"
echo "  storefront -> \$ORDERS_URL ($(inpod storefront 'import os; print(os.environ["ORDERS_URL"])')): HTTP 200"
DB_HOST="$(inpod orders 'import os; print(os.environ["DB_HOST"])')"
allowed orders "$DB_HOST" 5432
echo "  orders -> \$DB_HOST ($DB_HOST):5432 open"
BLOCKED=""
for _ in $(seq 1 15); do
  OUT="$(inpod storefront "$CONNECT" "$DB_HOST" 5432 2>&1 || true)"
  if [[ "$OUT" != "open" ]]; then BLOCKED=1; break; fi
  sleep 3
done
if [[ -n "$BLOCKED" ]]; then
  echo "  storefront -> $DB_HOST:5432 blocked by NetworkPolicy orders-db"
else
  echo "  (storefront -> $DB_HOST:5432 still open after 45s: this cluster's network plugin does not enforce NetworkPolicy)"
fi

echo "▸ kubectl diff agrees with miniplatform diff about the next version of orders"
python3 code/main.py render code/fixtures/storefront.json code/fixtures/orders-next.json -o json --namespace "$NS" --gateway "$NS/cc-platform-api" > "$WORK/next.json" 2>/dev/null
rc=0
k diff -f "$WORK/next.json" > "$WORK/kubectl-diff.txt" 2>&1 || rc=$?
[[ "$rc" == "1" ]] || fail "kubectl diff should exit 1 for a changed image, got $rc: $(cat "$WORK/kubectl-diff.txt")"
grep -q "python:3.13-alpine" "$WORK/kubectl-diff.txt" && grep -q "/api/v2/orders" "$WORK/kubectl-diff.txt" || fail "kubectl diff should show the image and route path: $(cat "$WORK/kubectl-diff.txt")"
echo "  kubectl diff shows the image and the route path change that miniplatform diff predicted"

echo "SMOKE OK: platform/platform-api"
