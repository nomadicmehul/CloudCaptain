#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
NAME="ccsmoke-$$"
PROXY_IMAGE="${PROXY_IMAGE:-python:3.12-alpine}"
AGENT_IMAGE="${AGENT_IMAGE:-curlimages/curl:8.11.1}"
KIND_NODE="cc-smoke-control-plane"
K8S_NS=""
HELPERS=()
cleanup() {
  for pid in ${HELPERS[@]+"${HELPERS[@]}"}; do kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; done
  if command -v docker >/dev/null; then
    docker rm -f "$NAME-target" "$NAME-lazy" >/dev/null 2>&1 || true
    bash code/main.sh down --name "$NAME" >/dev/null 2>&1 || true
  fi
  if [[ -n "$K8S_NS" ]]; then kubectl delete namespace "$K8S_NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

free_port() { python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'; }
wait_port() {
  python3 - "$1" <<'PY'
import socket, sys, time
deadline = time.time() + 10
while time.time() < deadline:
    try:
        socket.create_connection(("127.0.0.1", int(sys.argv[1])), 0.5).close()
        sys.exit(0)
    except OSError:
        time.sleep(0.1)
sys.exit(1)
PY
}

echo "▸ main.sh parses (bash -n)"
bash -n code/main.sh || fail "code/main.sh has a syntax error"

echo "▸ render: the fixture policy as the exact docker commands"
python3 code/main.py render --policy code/fixtures/policy-triage.json -- python3 -c 'print(1)' > "$WORK/render.txt"
sed 's/^/  /' "$WORK/render.txt" | cut -c1-150
[[ "$(wc -l < "$WORK/render.txt")" -eq 5 ]] || fail "render should print 5 commands"
grep -qx "docker network create --internal triage-net" "$WORK/render.txt" || fail "the agent network must be --internal"
grep -qx "docker network connect triage-net triage-proxy" "$WORK/render.txt" || fail "the proxy must join the agent network"
AGENT_LINE="$(tail -1 "$WORK/render.txt")"
for flag in "--network triage-net" "--read-only" "--tmpfs /tmp" "--cap-drop ALL" "--security-opt no-new-privileges" "--pids-limit 64" "--memory 256m" "--cpus 1" "--user 1000" "-e HTTPS_PROXY=http://triage-proxy:3128" "-v /home/me/.kube/agent-config:/home/agent/.kube/config:ro"; do
  grep -qF -- "$flag" <<<"$AGENT_LINE" || fail "agent command lacks $flag"
done

echo "▸ check: a default container against a hardened one (saved docker inspect)"
set +e
python3 code/main.py check --policy code/fixtures/policy-triage.json --inspect code/fixtures/inspect-default.json --json > "$WORK/drift.json"
CODE=$?
set -e
[[ "$CODE" == "1" ]] || fail "check should exit 1 on drift, got $CODE"
python3 - "$WORK/drift.json" <<'PY' || fail "drift report for the default container"
import json, sys
report = json.load(open(sys.argv[1]))
checks = [f["check"] for f in report["findings"]]
assert report["drift"] == 15, report["drift"]
assert checks.count("sensitive mount") == 3, checks
assert any("docker.sock" in f["actual"] for f in report["findings"]), report["findings"]
assert {"read-only rootfs", "capabilities dropped", "network", "user", "proxy environment"} <= set(checks), checks
print("  default container: %d drift findings, e.g. %s" % (report["drift"], ", ".join(checks[:5])))
PY
python3 code/main.py check --policy code/fixtures/policy-triage.json --inspect code/fixtures/inspect-hardened.json > "$WORK/hardened.txt" || fail "the hardened fixture should have no drift"
grep -q "0 drift finding(s)" "$WORK/hardened.txt" || fail "hardened fixture verdict"
sed 's/^/  /' "$WORK/hardened.txt"

echo "▸ proxy on localhost: one allowed host:port, everything else 403"
mkdir -p "$WORK/www"
echo "hello from the allowed host" > "$WORK/www/hello.txt"
TARGET_PORT="$(free_port)"
OTHER_PORT="$(free_port)"
PROXY_PORT="$(free_port)"
python3 -m http.server "$TARGET_PORT" --bind 127.0.0.1 --directory "$WORK/www" >/dev/null 2>&1 &
HELPERS+=("$!")
python3 code/main.py proxy --listen "127.0.0.1:$PROXY_PORT" --allow "127.0.0.1:$TARGET_PORT" --audit "$WORK/audit.json" 2> "$WORK/proxy.log" &
PROXY_PID=$!
HELPERS+=("$PROXY_PID")
wait_port "$TARGET_PORT" || fail "local http.server did not start"
wait_port "$PROXY_PORT" || { cat "$WORK/proxy.log"; fail "proxy did not start"; }
PROXY="http://127.0.0.1:$PROXY_PORT"
OUT="$(curl -s --noproxy '' -x "$PROXY" "http://127.0.0.1:$TARGET_PORT/hello.txt")"
[[ "$OUT" == "hello from the allowed host" ]] || fail "plain GET through the proxy, got: $OUT"
CODE="$(curl -s --noproxy '' -x "$PROXY" -o "$WORK/denied.json" -w '%{http_code}' "http://127.0.0.1:$OTHER_PORT/secret")"
[[ "$CODE" == "403" ]] || fail "GET to an unlisted port should be 403, got $CODE"
grep -q "127.0.0.1:$OTHER_PORT is not in the allow-list" "$WORK/denied.json" || fail "403 body should carry the reason"
OUT="$(curl -s --noproxy '' -p -x "$PROXY" "http://127.0.0.1:$TARGET_PORT/hello.txt")"
[[ "$OUT" == "hello from the allowed host" ]] || fail "CONNECT tunnel (curl -p) to the allowed host, got: $OUT"
set +e
curl -sS --noproxy '' -x "$PROXY" --max-time 10 https://example.com/ > /dev/null 2> "$WORK/connect-denied.txt"
CODE=$?
set -e
[[ "$CODE" != "0" ]] && grep -q "403" "$WORK/connect-denied.txt" || fail "CONNECT example.com:443 should be refused with 403 (curl $CODE: $(cat "$WORK/connect-denied.txt"))"
echo "  curl https://example.com/ -> $(cat "$WORK/connect-denied.txt")"
OUT="$(https_proxy="$PROXY" no_proxy='' python3 -c 'import urllib.request
try:
    urllib.request.urlopen("https://pypi.org/simple/", timeout=10)
except OSError as exc:
    print(exc)')"
grep -q "Tunnel connection failed: 403" <<<"$OUT" || fail "urllib (the triage agent's HTTP client) should honour https_proxy and get 403, got: $OUT"
echo "  urllib with https_proxy -> $OUT"
kill -TERM "$PROXY_PID"
wait "$PROXY_PID" || true
sed 's/^/  /' "$WORK/proxy.log"
grep -q "agentbox proxy stopped: 5 request(s), 2 allowed, 3 denied" "$WORK/proxy.log" || fail "proxy summary line"
python3 - "$WORK/audit.json" "$TARGET_PORT" "$OTHER_PORT" <<'PY' || fail "audit JSON"
import json, sys
audit = json.load(open(sys.argv[1]))
target, other = sys.argv[2], sys.argv[3]
assert audit["summary"]["requests"] == 5 and audit["summary"]["allowed"] == 2 and audit["summary"]["denied"] == 3, audit["summary"]
pairs = sorted((r["method"], r["host"], r["port"], r["status"]) for r in audit["requests"])
assert pairs == sorted([("GET", "127.0.0.1", int(target), 200), ("GET", "127.0.0.1", int(other), 403), ("CONNECT", "127.0.0.1", int(target), 200), ("CONNECT", "example.com", 443, 403), ("CONNECT", "pypi.org", 443, 403)]), pairs
allowed = audit["hosts"]["127.0.0.1:" + target]
assert allowed["allowed"] == 2 and allowed["bytes_in"] > 0, allowed
print("  audit: %(requests)d requests, %(allowed)d allowed, %(denied)d denied, %(bytes_out)d bytes out" % audit["summary"])
PY

if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  KIND=0
  if command -v kubectl >/dev/null && [[ "$(kubectl config current-context 2>/dev/null || true)" == "kind-cc-smoke" ]] && docker inspect "$KIND_NODE" >/dev/null 2>&1 && docker network inspect kind >/dev/null 2>&1; then KIND=1; fi
  ALLOW=(--allow "$NAME-target:8000" --allow api.anthropic.com)
  if [[ "$KIND" == "1" ]]; then ALLOW+=(--allow "$KIND_NODE:6443"); fi

  echo "▸ Use It: main.sh up: an --internal agent network, an egress network, the proxy on both"
  docker pull -q "$PROXY_IMAGE" >/dev/null
  docker pull -q "$AGENT_IMAGE" >/dev/null
  PROXY_IMAGE="$PROXY_IMAGE" bash code/main.sh up --name "$NAME" "${ALLOW[@]}"
  [[ "$(docker network inspect -f '{{.Internal}}' "$NAME-net")" == "true" ]] || fail "$NAME-net should be internal"
  docker run -d --rm --name "$NAME-target" --network "$NAME-egress" "$PROXY_IMAGE" sh -c 'mkdir -p /srv && echo "hello from the allowed host" > /srv/hello.txt && exec python3 -m http.server 8000 --directory /srv' >/dev/null
  READY=0
  for _ in $(seq 1 30); do
    if docker logs "$NAME-proxy" 2>&1 | grep -q "agentbox proxy listening" && docker exec "$NAME-proxy" python3 -c "import socket; socket.create_connection(('$NAME-target', 8000), 1).close()" 2>/dev/null; then READY=1; break; fi
    sleep 1
  done
  [[ "$READY" == "1" ]] || { docker logs "$NAME-proxy" 2>&1 | tail -5; fail "proxy or target container not ready within 30s"; }

  echo "▸ main.sh run: the agent container, and the same argv as render"
  printf 'apiVersion: v1\nkind: Config\nclusters: []\ncontexts: []\nusers: []\n' > "$WORK/agent-kubeconfig"
  chmod 644 "$WORK/agent-kubeconfig"
  python3 - "$WORK/policy.json" "$NAME" "$AGENT_IMAGE" "$WORK/agent-kubeconfig" <<'PY'
import json, sys
path, name, image, kubeconfig = sys.argv[1:]
json.dump({"name": name, "image": image, "allowed_hosts": [f"{name}-target:8000", "api.anthropic.com"], "kubeconfig": kubeconfig}, open(path, "w"), indent=2)
PY
  bash code/main.sh run --name "$NAME" --image "$AGENT_IMAGE" --kubeconfig "$WORK/agent-kubeconfig" --detach -- sleep 300 >/dev/null 2> "$WORK/run.err" || { cat "$WORK/run.err"; fail "main.sh run did not start the agent"; }
  python3 code/main.py render --policy "$WORK/policy.json" --json --detach sleep 300 > "$WORK/plan.json"
  python3 - "$WORK/run.err" "$WORK/plan.json" <<'PY' || fail "main.sh and render disagree on the agent's docker run argv"
import json, shlex, sys
line = next(l for l in open(sys.argv[1]) if l.startswith("+ "))
assert shlex.split(line[2:]) == json.load(open(sys.argv[2]))["agent"], (line, open(sys.argv[2]).read())
print("  main.sh run and agentbox render produce the same %d arguments" % len(shlex.split(line[2:])))
PY
  AGENT="$NAME-agent"
  ax() { docker exec "$AGENT" "$@"; }

  echo "▸ inside the sandbox: uid, capabilities, read-only root, tmpfs, read-only kubeconfig"
  [[ "$(ax id -u)" == "1000" ]] || fail "agent should run as uid 1000"
  ax grep -q "CapEff:.*0000000000000000" /proc/self/status || fail "CapEff should be 0 after --cap-drop ALL"
  ax grep -q "NoNewPrivs:.*1" /proc/self/status || fail "NoNewPrivs should be 1"
  if ax sh -c 'touch /etc/agent-was-here' 2>/dev/null; then fail "root filesystem should be read-only"; fi
  ax sh -c 'touch /tmp/scratch' || fail "/tmp should be a writable tmpfs"
  if ax sh -c 'echo x >> /home/agent/.kube/config' 2>/dev/null; then fail "kubeconfig mount should be read-only"; fi
  echo "  uid 1000, CapEff 0, NoNewPrivs 1, / read-only, /tmp writable, kubeconfig read-only"

  echo "▸ egress: allowed through the proxy, denied by the proxy, and no way around it"
  OUT="$(ax curl -s --max-time 10 "http://$NAME-target:8000/hello.txt")"
  [[ "$OUT" == "hello from the allowed host" ]] || fail "allowed host through the proxy, got: $OUT"
  CODE="$(ax curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://$NAME-target:9000/" || true)"
  [[ "$CODE" == "403" ]] || fail "unlisted port should be 403 from the proxy, got $CODE"
  CODE="$(ax curl -s -o /dev/null -w '%{http_connect}' --max-time 10 https://example.com/ || true)"
  [[ "$CODE" == "403" ]] || fail "CONNECT example.com:443 should be refused with 403, got $CODE"
  CODE="$(ax curl -s -o /dev/null -w '%{http_code}' --max-time 20 https://api.anthropic.com/v1/models || true)"
  [[ "$CODE" != "000" && -n "$CODE" ]] || fail "allowed HTTPS host api.anthropic.com should answer through the CONNECT tunnel (no outbound internet from $NAME-egress?), got $CODE"
  echo "  allowed http 200, unlisted port 403, CONNECT example.com 403, https://api.anthropic.com/v1/models -> $CODE (no key, so 401 is the expected answer)"
  if ax curl -s --noproxy '*' --max-time 5 "http://$NAME-target:8000/hello.txt" >/dev/null 2>&1; then fail "bypassing the proxy by name should fail on an internal network"; fi
  TARGET_IP="$(docker inspect -f "{{(index .NetworkSettings.Networks \"$NAME-egress\").IPAddress}}" "$NAME-target")"
  if ax curl -s --noproxy '*' --max-time 5 "http://$TARGET_IP:8000/hello.txt" >/dev/null 2>&1; then fail "bypassing the proxy by IP ($TARGET_IP) should fail on an internal network"; fi
  echo "  curl --noproxy '*' to $NAME-target and to $TARGET_IP: no route, as intended"

  echo "▸ agentbox check against the live container, then against a lazy one"
  python3 code/main.py check --policy "$WORK/policy.json" --container "$AGENT" | tee "$WORK/live-check.txt" | sed 's/^/  /'
  grep -q "0 drift finding(s)" "$WORK/live-check.txt" || fail "the container main.sh started should match the policy"
  docker run -d --rm --name "$NAME-lazy" --cap-add NET_ADMIN -v "$WORK/agent-kubeconfig:/home/agent/.kube/config" "$AGENT_IMAGE" sleep 300 >/dev/null
  set +e
  python3 code/main.py check --policy "$WORK/policy.json" --container "$NAME-lazy" --json > "$WORK/lazy.json"
  CODE=$?
  set -e
  [[ "$CODE" == "1" ]] || fail "check should exit 1 for the lazy container, got $CODE"
  python3 - "$WORK/lazy.json" <<'PY' || fail "drift findings for the lazy container"
import json, sys
checks = [f["check"] for f in json.load(open(sys.argv[1]))["findings"]]
for expected in ("read-only rootfs", "capabilities dropped", "no capabilities added", "no-new-privileges", "pids limit", "memory limit", "network", "user", "proxy environment", "tmpfs", "mount is writable"):
    assert expected in checks, (expected, checks)
print("  lazy container: %d findings: %s" % (len(checks), ", ".join(checks)))
PY

  if [[ "$KIND" == "1" ]]; then
    echo "▸ Kubernetes: a read-only ServiceAccount token, used from the sandbox through the proxy"
    K8S_NS="cc-smoke-sandbox-$$"
    kubectl create namespace "$K8S_NS" >/dev/null
    kubectl create serviceaccount triage-ro -n "$K8S_NS" >/dev/null
    kubectl create rolebinding triage-ro-view -n "$K8S_NS" --clusterrole=view --serviceaccount="$K8S_NS:triage-ro" >/dev/null
    SA="system:serviceaccount:$K8S_NS:triage-ro"
    [[ "$(kubectl auth can-i list pods -n "$K8S_NS" --as="$SA" || true)" == yes* ]] || fail "view should allow list pods"
    [[ "$(kubectl auth can-i get secrets -n "$K8S_NS" --as="$SA" || true)" == no* ]] || fail "view must not allow reading secrets"
    [[ "$(kubectl auth can-i delete pods -n "$K8S_NS" --as="$SA" || true)" == no* ]] || fail "view must not allow deletes"
    echo "  can-i list pods: yes   get secrets: no   delete pods: no"
    TOKEN="$(kubectl create token triage-ro -n "$K8S_NS" --duration=10m)"
    kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d > "$WORK/ca.crt"
    docker network connect kind "$NAME-proxy"
    API="https://$KIND_NODE:6443/api/v1/namespaces/$K8S_NS"
    kapi() { docker exec -e TOKEN="$TOKEN" -e CA_PEM="$(cat "$WORK/ca.crt")" "$AGENT" sh -c 'printf "%s\n" "$CA_PEM" > /tmp/ca.crt && curl -s -o /dev/null -w "%{http_code}" --max-time 15 --cacert /tmp/ca.crt -H "Authorization: Bearer $TOKEN" "$1"' sh "$1" || true; }
    CODE="$(kapi "$API/pods")"
    [[ "$CODE" == "200" ]] || fail "list pods with the view token through the proxy should be 200, got $CODE"
    CODE="$(kapi "$API/secrets")"
    [[ "$CODE" == "403" ]] || fail "list secrets with the view token should be 403 from the API server, got $CODE"
    echo "  from the sandbox via CONNECT $KIND_NODE:6443: pods 200, secrets 403 (RBAC, not the proxy)"
    cat > "$WORK/netpol.yaml" <<'EOF'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: triage-agent-egress
spec:
  podSelector:
    matchLabels: {app: triage-agent}
  policyTypes: [Egress]
  egress:
  - to:
    - podSelector:
        matchLabels: {app: egress-proxy}
    ports: [{protocol: TCP, port: 3128}]
  - to:
    - namespaceSelector:
        matchLabels: {kubernetes.io/metadata.name: kube-system}
      podSelector:
        matchLabels: {k8s-app: kube-dns}
    ports: [{protocol: UDP, port: 53}, {protocol: TCP, port: 53}]
EOF
    kubectl apply --dry-run=server -n "$K8S_NS" -f "$WORK/netpol.yaml" | tee "$WORK/netpol.txt" | sed 's/^/  /'
    grep -q "triage-agent-egress created (server dry run)" "$WORK/netpol.txt" || fail "the NetworkPolicy from Use It should pass server-side validation"
  else
    echo "  (kind cluster kind-cc-smoke not found; Kubernetes token and NetworkPolicy checks skipped)"
  fi

  echo "▸ main.sh audit: the proxy's JSON audit from inside its container"
  bash code/main.sh audit --name "$NAME" > "$WORK/docker-audit.json"
  python3 - "$WORK/docker-audit.json" "$NAME" "$KIND" "$KIND_NODE" <<'PY' || fail "proxy audit from the container"
import json, sys
audit, name, kind, node = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3], sys.argv[4]
seen = {(r["method"], r["host"], r["port"], r["status"]) for r in audit["requests"]}
expected = {("GET", f"{name}-target", 8000, 200), ("GET", f"{name}-target", 9000, 403), ("CONNECT", "example.com", 443, 403), ("CONNECT", "api.anthropic.com", 443, 200)}
if kind == "1":
    expected.add(("CONNECT", node, 6443, 200))
assert expected <= seen, sorted(expected - seen)
hosts = audit["hosts"]
assert hosts["api.anthropic.com:443"]["bytes_in"] > 0, hosts
print("  %(requests)d requests, %(allowed)d allowed, %(denied)d denied; " % audit["summary"] + ", ".join(sorted(hosts)))
PY
  docker rm -f "$NAME-target" "$NAME-lazy" >/dev/null
  bash code/main.sh down --name "$NAME" | sed 's/^/  /'
  for net in "$NAME-net" "$NAME-egress"; do
    if docker network inspect "$net" >/dev/null 2>&1; then fail "down should remove $net"; fi
  done
else
  echo "  (docker not available; the sandbox, live drift check and Kubernetes checks skipped)"
fi

echo "SMOKE OK: aiops/guardrails-sandboxes"
