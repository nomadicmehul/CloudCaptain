#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
NS="cc-gateway-api"
CLASS="cc-gateway-api"
CTX="kind-cc-smoke"
EG_NS="envoy-gateway-system"
GATEWAY_API_VERSION="v1.7.0"
ENVOY_GATEWAY_VERSION="v1.9.3"
HELM_VERSION="v4.3.1"
HELM="$WORK/bin/helm"
export HELM_CACHE_HOME="$WORK/helm/cache" HELM_CONFIG_HOME="$WORK/helm/config" HELM_DATA_HOME="$WORK/helm/data"
PIDS=()
CLUSTER=""
GW_CRDS_APPLIED=""
EG_CRDS_APPLIED=""
EG_INSTALLED=""
EG_NS_CREATED=""

k() { kubectl --context "$CTX" "$@"; }

cleanup() {
  set +e
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do { kill "$pid"; wait "$pid"; } 2>/dev/null; done
  if [[ -n "$CLUSTER" ]]; then
    k delete namespace "$NS" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1
    if ! k delete gatewayclass "$CLASS" --ignore-not-found --wait=true --timeout=60s >/dev/null 2>&1; then
      k patch gatewayclass "$CLASS" --type=merge -p '{"metadata":{"finalizers":null}}' >/dev/null 2>&1
    fi
    if [[ -n "$EG_INSTALLED" ]]; then
      "$HELM" uninstall eg -n "$EG_NS" --kube-context "$CTX" --timeout 120s >/dev/null 2>&1
      k delete mutatingwebhookconfiguration,validatingwebhookconfiguration,clusterrole,clusterrolebinding -l app.kubernetes.io/instance=eg --ignore-not-found >/dev/null 2>&1
    fi
    if [[ -n "$EG_NS_CREATED" ]]; then k delete namespace "$EG_NS" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1; fi
    if [[ -n "$EG_CRDS_APPLIED" ]]; then k delete -f "$WORK/eg-crds.yaml" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1; fi
    if [[ -n "$GW_CRDS_APPLIED" ]]; then k delete -f "$WORK/standard-install.yaml" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1; fi
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

wait_line() { for _ in $(seq 1 100); do grep -q "$2" "$1" 2>/dev/null && return 0; sleep 0.1; done; fail "no '$2' in $1: $(cat "$1" 2>/dev/null)"; }
free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'; }

cat > "$WORK/probe.py" <<'PY'
import json
import sys
import time
import urllib.error
import urllib.request

base, label, wait = sys.argv[1], sys.argv[2], float(sys.argv[3])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)


def get(path, host="app.shop.test", headers=None):
    request = urllib.request.Request(base + path, headers=dict({"Host": host}, **(headers or {})))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read().decode()


def seen(path, **kwargs):
    status, _, body = get(path, **kwargs)
    assert status == 200, (label, path, status, body[:300])
    return json.loads(body)


deadline = time.time() + wait
while True:
    try:
        status, _, body = get("/api/health")
        if status == 200 and json.loads(body).get("backend") == "green":
            break
        last = (status, body[:200])
    except Exception as exc:
        last = repr(exc)
    if time.time() > deadline:
        raise SystemExit(f"SMOKE FAIL: {label}: /api/health never answered from green: {last}")
    time.sleep(1)

health = seen("/api/health", headers={"x-version": "v2"})
assert health["backend"] == "green" and health["headers"].get("x-route") == "exact", (label, "Exact /api/health must beat PathPrefix /api with a header match", health)
users = seen("/api/users")
assert users["backend"] == "blue" and "x-route" not in users["headers"], (label, "plain PathPrefix /api", users)
v2 = seen("/api/users", headers={"x-version": "v2"})
assert v2["backend"] == "green" and "x-route" not in v2["headers"], (label, "the header match must beat the plain prefix", v2)
rewritten = seen("/v1/users?page=2")
assert (rewritten["backend"], rewritten["path"], rewritten["query"]) == ("blue", "/api/users", "page=2"), (label, "URLRewrite ReplacePrefixMatch", rewritten)
status, _, body = get("/apis")
assert status == 404, (label, "/apis must not match PathPrefix /api", status, body[:200])
status, headers, body = get("/old")
assert status == 301 and headers.get("location", "").endswith("/api/v1"), (label, "RequestRedirect", status, headers)
location = headers["location"]
status, _, body = get("/broken")
assert status == 500, (label, "a backendRef to a missing Service must answer 500", status, body[:200])
status, _, body = get("/", host="other.example")
assert status == 404, (label, "a route whose hostname does not intersect the listener must not serve", status, body[:200])
names = [seen("/canary")["backend"] for _ in range(200)]
green = names.count("green")
assert set(names) <= {"blue", "green"} and 6 <= green <= 40, (label, "90/10 split over 200 requests", green)
print(f"  {label}: exact beats prefix+header, header beats prefix, /apis 404, /old 301 {location}, /v1 -> /api, /broken 500, other.example 404, /canary green {green}/200")
PY

echo "▸ minigateway status: the conditions a controller would write"
python3 code/main.py status code/fixtures/routes.json --json > "$WORK/status.json"
python3 - "$WORK/status.json" <<'PY' || fail "minigateway status: $(cat "$WORK/status.json")"
import json, sys
report = json.load(open(sys.argv[1]))
found = {(r["name"], c["type"]): (c["status"], c["reason"]) for r in report["routes"] for p in r["status"]["parents"] for c in p["conditions"]}
assert found[("app", "Accepted")] == ("True", "Accepted"), found
assert found[("app", "ResolvedRefs")] == ("True", "ResolvedRefs"), found
assert found[("broken", "ResolvedRefs")] == ("False", "BackendNotFound"), found
assert found[("elsewhere", "Accepted")] == ("False", "NoMatchingListenerHostname"), found
print("  app Accepted/ResolvedRefs, broken BackendNotFound, elsewhere NoMatchingListenerHostname")
PY

echo "▸ minigateway route: exact beats a prefix with more header matches, on paper"
python3 code/main.py route code/fixtures/routes.json --host app.shop.test --path /api/health -H 'x-version: v2' > "$WORK/route.txt"
grep -q "winner: shop/app rule 0 (beats the next candidate on exact path)" "$WORK/route.txt" || fail "route decision: $(cat "$WORK/route.txt")"
python3 code/main.py table code/fixtures/routes.json > "$WORK/table.txt"
head -3 "$WORK/table.txt"

echo "▸ local data plane: two echo backends behind minigateway serve"
python3 code/main.py backend --name blue --port 0 > "$WORK/blue.log" 2>&1 &
PIDS+=("$!")
python3 code/main.py backend --name green --port 0 > "$WORK/green.log" 2>&1 &
PIDS+=("$!")
wait_line "$WORK/blue.log" "listening on"
wait_line "$WORK/green.log" "listening on"
BLUE="$(sed -n 's/.*listening on //p' "$WORK/blue.log")"
GREEN="$(sed -n 's/.*listening on //p' "$WORK/green.log")"
python3 code/main.py serve code/fixtures/routes.json --backend "shop/blue=$BLUE" --backend "shop/green=$GREEN" --bind 80=0 --seed 7 > "$WORK/gateway.log" 2>&1 &
PIDS+=("$!")
wait_line "$WORK/gateway.log" "url="
GATEWAY_URL="$(sed -n 's/.*url=//p' "$WORK/gateway.log")"
python3 "$WORK/probe.py" "$GATEWAY_URL" minigateway 10 || fail "local data plane; gateway log: $(cat "$WORK/gateway.log")"

kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if ! command -v kubectl >/dev/null || ! grep -qx "$CTX" "$WORK/contexts.txt"; then
  echo "  (no $CTX context; Gateway API and Envoy Gateway steps skipped)"
  echo "SMOKE OK: orchestration/gateway-api"
  exit 0
fi
k cluster-info > "$WORK/cluster-info.txt" 2>&1 || fail "context $CTX is not reachable: $(cat "$WORK/cluster-info.txt")"
CLUSTER=1

echo "▸ Gateway API $GATEWAY_API_VERSION standard channel CRDs, server-side apply"
if k get crd gateways.gateway.networking.k8s.io >/dev/null 2>&1; then
  echo "  (Gateway API CRDs already installed; using and keeping them)"
else
  curl -fsSL -o "$WORK/standard-install.yaml" "https://github.com/kubernetes-sigs/gateway-api/releases/download/$GATEWAY_API_VERSION/standard-install.yaml" || fail "download standard-install.yaml"
  GW_CRDS_APPLIED=1
  k apply --server-side -f "$WORK/standard-install.yaml" > "$WORK/crds.txt" 2>&1 || fail "apply Gateway API CRDs: $(cat "$WORK/crds.txt")"
  k wait --for=condition=Established --timeout=60s crd/gatewayclasses.gateway.networking.k8s.io crd/gateways.gateway.networking.k8s.io crd/httproutes.gateway.networking.k8s.io crd/referencegrants.gateway.networking.k8s.io >/dev/null || fail "Gateway API CRDs not established"
  echo "  $(grep -c customresourcedefinition "$WORK/crds.txt" || true) CRDs applied, Established"
fi

if k get deployment envoy-gateway -n "$EG_NS" >/dev/null 2>&1; then
  echo "  (Envoy Gateway already running in $EG_NS; using and keeping it)"
else
  echo "▸ helm $HELM_VERSION from get.helm.sh, checksum verified"
  case "$(uname -m)" in x86_64) ARCH=amd64 ;; aarch64|arm64) ARCH=arm64 ;; *) fail "unsupported architecture $(uname -m)" ;; esac
  mkdir -p "$WORK/bin" "$WORK/dl"
  TARBALL="helm-$HELM_VERSION-linux-$ARCH.tar.gz"
  curl -fsSL -o "$WORK/dl/$TARBALL" "https://get.helm.sh/$TARBALL" || fail "download $TARBALL"
  curl -fsSL -o "$WORK/dl/$TARBALL.sha256sum" "https://get.helm.sh/$TARBALL.sha256sum" || fail "download $TARBALL.sha256sum"
  WANT="$(awk '{print $1}' "$WORK/dl/$TARBALL.sha256sum")"
  GOT="$(sha256sum "$WORK/dl/$TARBALL" | awk '{print $1}')"
  [[ -n "$WANT" && "$WANT" == "$GOT" ]] || fail "helm checksum mismatch: want $WANT got $GOT"
  tar -xzf "$WORK/dl/$TARBALL" -C "$WORK/dl"
  mv "$WORK/dl/linux-$ARCH/helm" "$HELM"
  echo "  $("$HELM" version --short)"

  echo "▸ Envoy Gateway $ENVOY_GATEWAY_VERSION: chart from oci://docker.io/envoyproxy/gateway-helm, its own CRDs, then the controller"
  "$HELM" pull oci://docker.io/envoyproxy/gateway-helm --version "$ENVOY_GATEWAY_VERSION" --untar --untardir "$WORK/chart" > "$WORK/pull.txt" 2>&1 || fail "helm pull: $(cat "$WORK/pull.txt")"
  python3 - "$WORK/chart/gateway-helm/crds" "$WORK/eg-crds.yaml" <<'PY' || fail "could not extract Envoy Gateway's own CRDs from the chart"
import os, re, sys
root, out = sys.argv[1], sys.argv[2]
keep = []
for folder, _, files in sorted(os.walk(root)):
    for name in sorted(files):
        if not name.endswith((".yaml", ".yml")):
            continue
        with open(os.path.join(folder, name), encoding="utf-8") as handle:
            text = handle.read()
        for doc in re.split(r"(?m)^---\s*$", text):
            group = re.search(r"(?m)^  group: ['\"]?([\w.-]+)", doc)
            if "kind: CustomResourceDefinition" in doc and group and not group.group(1).startswith("gateway.networking."):
                keep.append(doc.strip())
with open(out, "w", encoding="utf-8") as handle:
    handle.write("\n---\n".join(keep) + "\n" if keep else "")
print(f"  {len(keep)} Envoy Gateway CRDs from crds/; the Gateway API CRDs stay those of the standard install" if keep else "  (no crds/ in the chart; its templates carry the CRDs)")
PY
  if [[ -s "$WORK/eg-crds.yaml" ]]; then
    EG_CRDS_APPLIED=1
    k apply --server-side -f "$WORK/eg-crds.yaml" > "$WORK/eg-crds.txt" 2>&1 || fail "apply Envoy Gateway CRDs: $(cat "$WORK/eg-crds.txt")"
  fi
  k get namespace "$EG_NS" >/dev/null 2>&1 || EG_NS_CREATED=1
  EG_INSTALLED=1
  "$HELM" install eg "$WORK/chart/gateway-helm" -n "$EG_NS" --create-namespace --skip-crds --kube-context "$CTX" --timeout 5m > "$WORK/helm.txt" 2>&1 || fail "helm install: $(cat "$WORK/helm.txt")"
  k wait --for=condition=Available deployment/envoy-gateway -n "$EG_NS" --timeout=240s >/dev/null || fail "envoy-gateway not Available: $(k get pods -n "$EG_NS" -o wide 2>&1)"
fi

echo "▸ GatewayClass, Gateway, three HTTPRoutes and two echo backends (running this lesson's main.py) in $NS"
k apply -f - > /dev/null <<EOF
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: $CLASS
spec:
  controllerName: gateway.envoyproxy.io/gatewayclass-controller
EOF
k wait --for=condition=Accepted "gatewayclass/$CLASS" --timeout=60s >/dev/null || fail "GatewayClass not accepted: $(k get gatewayclass "$CLASS" -o yaml 2>&1 | tail -20)"
k create namespace "$NS" >/dev/null
k create configmap minigateway -n "$NS" --from-file=main.py=code/main.py >/dev/null
python3 - "$NS" "$CLASS" code/fixtures/routes.json code/fixtures/backends.json > "$WORK/cluster.json" <<'PY'
import copy, json, sys
ns, gateway_class, files = sys.argv[1], sys.argv[2], sys.argv[3:]
items = []
for name in files:
    with open(name, encoding="utf-8") as handle:
        items += [copy.deepcopy(item) for item in json.load(handle)["items"]]
for item in items:
    item["metadata"]["namespace"] = ns
    if item["kind"] == "Gateway":
        item["spec"]["gatewayClassName"] = gateway_class
print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}))
PY
k apply -f "$WORK/cluster.json" > "$WORK/apply.txt" 2>&1 || fail "apply: $(cat "$WORK/apply.txt")"
k rollout status deployment/blue -n "$NS" --timeout=180s >/dev/null || fail "blue not ready: $(k get pods -n "$NS" -o wide 2>&1)"
k rollout status deployment/green -n "$NS" --timeout=180s >/dev/null || fail "green not ready: $(k get pods -n "$NS" -o wide 2>&1)"

echo "▸ route status: Envoy Gateway and minigateway (fed the live objects) write the same conditions"
python3 - "$CTX" "$NS" "$WORK" <<'PY' || fail "route status differs"
import json, subprocess, sys, time
ctx, ns, work = sys.argv[1:4]
EXPECTED = {
    ("app", "Accepted"): ("True", "Accepted"),
    ("app", "ResolvedRefs"): ("True", "ResolvedRefs"),
    ("broken", "ResolvedRefs"): ("False", "BackendNotFound"),
    ("elsewhere", "Accepted"): ("False", "NoMatchingListenerHostname"),
}


def kubectl(*args):
    return subprocess.run(["kubectl", "--context", ctx, *args], capture_output=True, text=True, check=True).stdout


def conditions(routes, controller=None):
    found = {}
    for item in routes:
        name = item["metadata"]["name"] if "metadata" in item else item["name"]
        for parent in (item.get("status") or {}).get("parents") or []:
            if controller and parent.get("controllerName") != controller:
                continue
            for c in parent.get("conditions") or []:
                found[(name, c["type"])] = (c["status"], c["reason"])
    return found


deadline = time.time() + 120
while True:
    real = conditions(json.loads(kubectl("get", "httproutes.gateway.networking.k8s.io", "-n", ns, "-o", "json"))["items"], "gateway.envoyproxy.io/gatewayclass-controller")
    if all(real.get(key) == want for key, want in EXPECTED.items()) or time.time() > deadline:
        break
    time.sleep(2)
live = kubectl("get", "gateways.gateway.networking.k8s.io,httproutes.gateway.networking.k8s.io,referencegrants.gateway.networking.k8s.io,services", "-n", ns, "-o", "json")
with open(f"{work}/live.json", "w", encoding="utf-8") as handle:
    handle.write(live)
mini = conditions(json.loads(subprocess.run([sys.executable, "code/main.py", "status", f"{work}/live.json", "--json"], capture_output=True, text=True, check=True).stdout)["routes"])
for key, want in EXPECTED.items():
    assert real.get(key) == want, ("Envoy Gateway", key, real.get(key), real)
    assert mini.get(key) == want, ("minigateway", key, mini.get(key), mini)
    print(f"  {key[0]:<10} {key[1]:<13} {want[0]:<5} {want[1]:<27} Envoy Gateway and minigateway agree")
PY
python3 code/main.py route "$WORK/live.json" --host app.shop.test --path /api/health -H 'x-version: v2' > "$WORK/live-route.txt"
grep -q "winner: $NS/app rule 0 (beats the next candidate on exact path)" "$WORK/live-route.txt" || fail "minigateway on the live objects: $(cat "$WORK/live-route.txt")"

echo "▸ the same requests through Envoy, via kubectl port-forward to the Service Envoy Gateway created"
SELECTOR="gateway.envoyproxy.io/owning-gateway-namespace=$NS,gateway.envoyproxy.io/owning-gateway-name=web"
PROXY=""
for _ in $(seq 1 90); do
  PROXY="$(k get svc -A -l "$SELECTOR" -o jsonpath='{.items[0].metadata.namespace} {.items[0].metadata.name}' 2>/dev/null || true)"
  DEPLOY="$(k get deployment -A -l "$SELECTOR" -o name 2>/dev/null || true)"
  [[ -n "$PROXY" && -n "$DEPLOY" ]] && break
  sleep 2
done
[[ -n "$PROXY" ]] || fail "Envoy Gateway created no Service for the Gateway: $(k get gateways.gateway.networking.k8s.io web -n "$NS" -o yaml 2>&1 | tail -30)"
PROXY_NS="${PROXY%% *}"
PROXY_SVC="${PROXY##* }"
k wait --for=condition=Available deployment -n "$PROXY_NS" -l "$SELECTOR" --timeout=180s >/dev/null || fail "Envoy proxy not Available: $(k get pods -n "$PROXY_NS" -o wide 2>&1)"
PF_PORT="$(free_port)"
k port-forward -n "$PROXY_NS" "svc/$PROXY_SVC" "$PF_PORT:80" --address 127.0.0.1 > "$WORK/port-forward.log" 2>&1 &
PIDS+=("$!")
wait_line "$WORK/port-forward.log" "Forwarding from"
python3 "$WORK/probe.py" "http://127.0.0.1:$PF_PORT" "envoy-gateway" 120 || fail "Envoy data plane; port-forward log: $(cat "$WORK/port-forward.log")"
echo "  Envoy Service $PROXY_NS/$PROXY_SVC; Gateway listener port 80"

if command -v ingress2gateway >/dev/null; then
  echo "▸ ingress2gateway turns an ingress-nginx Ingress (retired controller) into a Gateway and an HTTPRoute"
  cat > "$WORK/ingress.yaml" <<'EOF'
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: app
  namespace: shop
spec:
  ingressClassName: nginx
  rules:
    - host: app.shop.test
      http:
        paths:
          - path: /api
            pathType: Prefix
            backend:
              service:
                name: blue
                port:
                  number: 8080
EOF
  ingress2gateway print --providers=ingress-nginx --input-file "$WORK/ingress.yaml" > "$WORK/i2g.yaml" 2>&1 || fail "ingress2gateway: $(cat "$WORK/i2g.yaml")"
  grep -q "kind: HTTPRoute" "$WORK/i2g.yaml" || fail "ingress2gateway printed no HTTPRoute: $(cat "$WORK/i2g.yaml")"
else
  echo "  (ingress2gateway not installed; migration check skipped)"
fi

echo "SMOKE OK: orchestration/gateway-api"
