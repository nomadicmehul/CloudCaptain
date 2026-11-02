#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
NS="cc-network-policy"
TOOLS="cc-network-policy-tools"
CTX="${CC_SMOKE_CONTEXT:-kind-cc-smoke}"
L="code/fixtures/lab"
IMAGE="python:3.12-alpine"
PROBE_IMAGE="busybox:1.37"
EXT_NAMES=(cc-network-policy-ext-a cc-network-policy-ext-b)
SOURCES=("$NS/frontend" "$NS/api" "$NS/db" "$TOOLS/scanner")
EXTERNAL_ARGS=()
CLUSTER=""
DOCKER_EXT=""
SERVE_PID=""
k() { kubectl --context "$CTX" "$@"; }
cleanup() {
  if [[ -n "$SERVE_PID" ]]; then kill "$SERVE_PID" >/dev/null 2>&1 || true; fi
  if [[ -n "$DOCKER_EXT" ]]; then docker rm -f "${EXT_NAMES[@]}" >/dev/null 2>&1 || true; fi
  if [[ -n "$CLUSTER" ]]; then k delete namespace "$NS" "$TOOLS" --ignore-not-found --wait=false >/dev/null 2>&1 || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT
MP() { python3 code/main.py "$@"; }
lab_files() {
  local step="$1" out=() name
  while IFS= read -r name; do [[ -n "$name" ]] && out+=(-f "$L/$name"); done < <(python3 -c 'import json,sys; [print(s["apply"]) for s in json.load(open(sys.argv[1]))["steps"][: int(sys.argv[2]) + 1] if s["apply"]]' "$L/expected.json" "$step")
  printf '%s\n' ${out[@]+"${out[@]}"}
}
same_allowed() {
  python3 - "$1" "$L/expected.json" "$2" <<'PY'
import json, sys
predicted, expected = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
step = expected["steps"][int(sys.argv[3])]
targets = {c["to"] for c in predicted["cells"]}
key = lambda c: f"{c['from']} -> {c['to']} {c['port']}/{c['protocol']}"
got = sorted(key(c) for c in predicted["cells"] if c["allowed"])
if step["allowed"] == "*":
    want = sorted(key(c) for c in predicted["cells"])
else:
    want = sorted(k for k in step["allowed"] if k.split(" -> ")[1].split(" ")[0] in targets)
if got != want:
    print("  predicted:", got)
    print("  expected: ", want)
    sys.exit(1)
print(f"  {len(got)} of {len(predicted['cells'])} connections allowed, as expected.json says for: {step['name']}")
PY
}

echo "▸ Build It: mininetpol predicts every lab step from the fixture cluster"
STEPS="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["steps"]))' "$L/expected.json")"
FIXTURE_ARGS=(--namespace "$NS" --namespace "$TOOLS" --port 8080 --port 9090 --port 5432 --external ext-a=172.18.0.11:8080 --external ext-b=172.18.0.12:8080 --dns --json)
for i in $(seq 0 $((STEPS - 1))); do
  FILES=()
  while IFS= read -r arg; do [[ -n "$arg" ]] && FILES+=("$arg"); done < <(lab_files "$i")
  MP matrix -f "$L/pods.json" -f "$L/namespaces.json" ${FILES[@]+"${FILES[@]}"} "${FIXTURE_ARGS[@]}" > "$WORK/fixture-$i.json" || fail "matrix on fixture step $i: $(cat "$WORK/fixture-$i.json")"
  same_allowed "$WORK/fixture-$i.json" "$i" || fail "fixture step $i differs from expected.json"
done

echo "▸ Build It: explain names the blocking side, lint finds every trap"
CODE=0
MP explain -f "$L/pods.json" -f "$L/namespaces.json" -f "$L/01-default-deny.json" -f "$L/02-api-allow-frontend.json" --from frontend --to api --port http > "$WORK/explain.txt" || CODE=$?
[[ "$CODE" == "1" ]] && grep -q "change the egress side of $NS/frontend" "$WORK/explain.txt" || fail "explain frontend -> api after step 2 (exit $CODE): $(cat "$WORK/explain.txt")"
sed 's/^/  /' "$WORK/explain.txt"
CODE=0
MP lint -f "$L/pods.json" -f "$L/namespaces.json" -f code/fixtures/traps.json > "$WORK/lint.txt" || CODE=$?
[[ "$CODE" == "1" ]] || fail "lint on traps.json should exit 1, got $CODE: $(cat "$WORK/lint.txt")"
for code in egress-not-isolated podselector-missing or-peers peer-selects-nothing named-port-missing ipblock-pods ignored-rules selects-nothing dns-egress; do
  grep -q ": $code: " "$WORK/lint.txt" || fail "lint did not report $code: $(cat "$WORK/lint.txt")"
done
echo "  $(tail -1 "$WORK/lint.txt")"

echo "▸ Build It: serve and probe on real sockets"
python3 - > "$WORK/ports.txt" <<'PY'
import socket
held = []
for _ in range(3):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    held.append(s)
print(" ".join(str(s.getsockname()[1]) for s in held))
for s in held:
    s.close()
PY
read -r P1 P2 CLOSED < "$WORK/ports.txt"
MP serve --host 127.0.0.1 --port "$P1" --port "$P2" > "$WORK/serve.log" 2>&1 &
SERVE_PID=$!
for _ in $(seq 1 50); do grep -q "listening" "$WORK/serve.log" && break; sleep 0.1; done
grep -q "listening on 127.0.0.1:$P1/TCP, 127.0.0.1:$P2/TCP" "$WORK/serve.log" || fail "serve did not start: $(cat "$WORK/serve.log")"
python3 - "$P1" "$P2" "$CLOSED" > "$WORK/local-predicted.json" <<'PY'
import json, sys
p1, p2, closed = (int(x) for x in sys.argv[1:4])
cell = lambda to, port, allowed: {"from": "local/probe", "to": to, "kind": "pod", "address": "127.0.0.1", "port": port, "protocol": "TCP", "state": "allowed" if allowed else "blocked", "allowed": allowed}
print(json.dumps({"cells": [cell("local/a", p1, True), cell("local/a", p2, True), cell("local/b", closed, False)]}))
PY
MP probe --from local/probe --timeout 1 "$WORK/local-predicted.json" > "$WORK/local-measured.json" || fail "probe on localhost: $(cat "$WORK/local-measured.json")"
MP compare "$WORK/local-predicted.json" "$WORK/local-measured.json" > "$WORK/local-compare.txt" || fail "localhost probe should match: $(cat "$WORK/local-compare.txt") $(cat "$WORK/local-measured.json")"
echo "  $(head -1 "$WORK/local-compare.txt")"
kill "$SERVE_PID" >/dev/null 2>&1 || true
wait "$SERVE_PID" 2>/dev/null || true
SERVE_PID=""

if ! command -v kubectl >/dev/null || ! command -v docker >/dev/null; then
  echo "  (kubectl or docker not installed; cluster steps skipped)"
  echo "SMOKE OK: security/network-policy (mininetpol only)"
  exit 0
fi
kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if ! grep -qx "$CTX" "$WORK/contexts.txt" || ! k cluster-info > "$WORK/cluster-info.txt" 2>&1; then
  echo "  (context $CTX not found or not reachable; cluster steps skipped)"
  echo "SMOKE OK: security/network-policy (mininetpol only)"
  exit 0
fi

echo "▸ Use It: what enforces NetworkPolicy on $CTX"
NODE="$(k get nodes -o jsonpath='{.items[0].metadata.name}')"
KINDNET="$(k -n kube-system get daemonset kindnet -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
echo "  node $NODE, kindnet image: ${KINDNET:-none (another network plugin)}"
if docker inspect "$NODE" >/dev/null 2>&1 && docker exec "$NODE" nft list tables > "$WORK/nft-tables.txt" 2>&1; then
  if grep -q "kindnet-network-policies" "$WORK/nft-tables.txt"; then
    echo "  nftables: table inet kindnet-network-policies present (kube-network-policies, NFQUEUE)"
  else
    echo "  (no kindnet-network-policies table yet; it may appear only once a policy selects a pod)"
  fi
else
  echo "  (cannot read the node's nftables; enforcement is judged by measurement only)"
fi

echo "▸ Use It: four pods running mininetpol serve, two containers outside the cluster"
CLUSTER=1
k delete namespace "$NS" "$TOOLS" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1 || true
for n in "$NS" "$TOOLS"; do
  k create namespace "$n" >/dev/null || fail "create namespace $n"
  for _ in $(seq 1 30); do k -n "$n" get serviceaccount default >/dev/null 2>&1 && break; sleep 1; done
  k -n "$n" create configmap mininetpol --from-file=main.py=code/main.py >/dev/null || fail "configmap in $n"
done
k apply -f "$L/workloads.json" >/dev/null || fail "apply workloads.json"
for n in "$NS" "$TOOLS"; do
  k -n "$n" wait --for=condition=Ready pod --all --timeout=180s >/dev/null || fail "pods in $n not ready: $(k -n "$n" get pods -o wide 2>&1; k -n "$n" describe pods 2>&1 | tail -30)"
done
k get pods -n "$NS" -o wide
k get pods -n "$TOOLS" -o wide
NET="$(docker inspect -f '{{json .NetworkSettings.Networks}}' "$NODE" 2>/dev/null | python3 -c 'import json,sys; print(next(iter(json.load(sys.stdin)), ""))' 2>/dev/null || true)"
EXT_SUBNET=""
if [[ -n "$NET" ]]; then
  DOCKER_EXT=1
  docker rm -f "${EXT_NAMES[@]}" >/dev/null 2>&1 || true
  for name in "${EXT_NAMES[@]}"; do
    docker run -d --name "$name" --network "$NET" -v "$PWD/code:/app:ro" "$IMAGE" python3 /app/main.py serve --port 8080 >/dev/null || fail "docker run $name"
  done
  ext_ip() { docker inspect -f '{{json .NetworkSettings.Networks}}' "$1" | python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]]["IPAddress"])' "$NET"; }
  EXT_A="$(ext_ip "${EXT_NAMES[0]}")"
  EXT_B="$(ext_ip "${EXT_NAMES[1]}")"
  EXT_SUBNET="$(docker network inspect "$NET" -f '{{json .IPAM.Config}}' | python3 -c 'import ipaddress,json,sys; print(next((c["Subnet"] for c in json.load(sys.stdin) if ipaddress.ip_network(c["Subnet"]).version == 4), ""))')"
  [[ -n "$EXT_A" && -n "$EXT_B" && -n "$EXT_SUBNET" ]] || fail "external containers on $NET: a=$EXT_A b=$EXT_B subnet=$EXT_SUBNET"
  EXTERNAL_ARGS=(--external "ext-a=$EXT_A:8080" --external "ext-b=$EXT_B:8080")
  python3 - "$L/05-api-egress-external.json" "$WORK/05-api-egress-external.json" "$EXT_SUBNET" "$EXT_B" "$EXT_A" <<'PY' || fail "rewrite the ipBlock policy for $EXT_SUBNET"
import ipaddress, json, sys
source, target, subnet, ext_b, ext_a = sys.argv[1:6]
doc = json.load(open(source))
block = doc["spec"]["egress"][0]["to"][0]["ipBlock"]
block["cidr"], block["except"] = subnet, [f"{ext_b}/32"]
network = ipaddress.ip_network(subnet)
assert ipaddress.ip_address(ext_a) in network and ipaddress.ip_address(ext_b) in network, (subnet, ext_a, ext_b)
json.dump(doc, open(target, "w"), indent=2)
PY
  echo "  docker network $NET ($EXT_SUBNET): ext-a $EXT_A, ext-b $EXT_B"
else
  echo "  (the node is not a Docker container on a known network; external columns skipped)"
fi

MATRIX_ARGS=(--namespace "$NS" --namespace "$TOOLS" --port 8080 --port 9090 --port 5432 ${EXTERNAL_ARGS[@]+"${EXTERNAL_ARGS[@]}"} --dns)
predict() {
  k get pods,networkpolicies -A -o json > "$WORK/live.json"
  k get namespaces -o json > "$WORK/namespaces.json"
  MP matrix -f "$WORK/live.json" -f "$WORK/namespaces.json" "${MATRIX_ARGS[@]}" --json > "$WORK/predicted.json"
}
probe_all() {
  rm -f "$WORK"/measured-*.json
  local pids=() src pid rc=0
  for src in "${SOURCES[@]}"; do
    k -n "${src%%/*}" exec -i "${src#*/}" -- python3 /opt/mininetpol/main.py probe --from "$src" --timeout 1.5 - < "$WORK/predicted.json" > "$WORK/measured-${src#*/}.json" 2> "$WORK/measured-${src#*/}.err" &
    pids+=("$!")
  done
  for pid in "${pids[@]}"; do wait "$pid" || rc=1; done
  return "$rc"
}
converge() {
  local deadline=$((SECONDS + 75)) tries=0
  while :; do
    tries=$((tries + 1))
    predict
    if probe_all && MP compare "$WORK/predicted.json" "$WORK"/measured-*.json > "$WORK/compare.txt" 2>&1; then
      echo "  real network: $(head -1 "$WORK/compare.txt") (measurement $tries)"
      return 0
    fi
    if (( SECONDS >= deadline )); then return 1; fi
    sleep 2
  done
}
everything_open() {
  python3 - "$WORK"/measured-*.json <<'PY'
import json, sys
cells = [c for path in sys.argv[1:] for c in json.load(open(path)).get("cells", [])]
sys.exit(0 if cells and all(c.get("allowed") for c in cells if c.get("allowed") is not None) else 1)
PY
}
check_policy_types() {
  local file="$1" name
  name="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["metadata"]["name"])' "$file")"
  k -n "$NS" get networkpolicy "$name" -o json > "$WORK/server-policy.json" || fail "get networkpolicy $name"
  MP lint -f "$file" --json > "$WORK/mini-policy.json" 2>&1 || true
  python3 - "$WORK/server-policy.json" "$WORK/mini-policy.json" "$name" <<'PY' || fail "policyTypes of $name: server and mininetpol differ"
import json, sys
server, mini, name = json.load(open(sys.argv[1])), json.load(open(sys.argv[2])), sys.argv[3]
stored = server["spec"].get("policyTypes")
defaulted = mini["policies"][0]["policyTypes"]
assert stored == defaulted, (stored, defaulted)
print(f"  {name}: policyTypes {stored} on the API server and in mininetpol")
PY
}

ENFORCED=1
for i in $(seq 0 $((STEPS - 1))); do
  APPLY="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["steps"][int(sys.argv[2])]["apply"] or "")' "$L/expected.json" "$i")"
  NAME="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["steps"][int(sys.argv[2])]["name"])' "$L/expected.json" "$i")"
  echo "▸ step $i: $NAME"
  if [[ -n "$APPLY" ]]; then
    FILE="$L/$APPLY"
    [[ -f "$WORK/$APPLY" ]] && FILE="$WORK/$APPLY"
    k apply -f "$FILE" >/dev/null || fail "apply $FILE"
    check_policy_types "$FILE"
  fi
  if [[ "$ENFORCED" == "1" ]]; then
    if ! converge; then
      if [[ "$i" == "1" ]] && everything_open; then
        ENFORCED=0
        echo "  (every connection still succeeds under a default deny: this cluster's network plugin does not enforce NetworkPolicy."
        echo "   kind releases before v0.24.0 shipped kindnet without it. The comparison with the real network is skipped; use a"
        echo "   separate cluster with disableDefaultCNI and Calico or Cilium rather than replacing the shared cluster's plugin.)"
      else
        fail "step $i: the real network disagrees with mininetpol after 75s: $(cat "$WORK/compare.txt"); probe errors: $(cat "$WORK"/measured-*.err 2>/dev/null)"
      fi
    fi
  else
    predict
  fi
  same_allowed "$WORK/predicted.json" "$i" || fail "step $i: mininetpol on the live objects differs from expected.json"
  MP matrix -f "$WORK/live.json" -f "$WORK/namespaces.json" "${MATRIX_ARGS[@]}" > "$WORK/matrix.txt" || fail "matrix text"
  sed -n '/^allowed ports/,$p' "$WORK/matrix.txt" | sed 's/^/  /'
done

echo "▸ explain on the live objects: api to db on postgres"
CODE=0
MP explain -f "$WORK/live.json" -f "$WORK/namespaces.json" --from "$NS/api" --to "$NS/db" --port postgres --json > "$WORK/explain.json" || CODE=$?
[[ "$CODE" == "1" ]] || fail "api -> db should be denied (exit $CODE): $(cat "$WORK/explain.json")"
python3 - "$WORK/explain.json" <<'PY' || fail "explain api -> db: $(cat "$WORK/explain.json")"
import json, sys
v = json.load(open(sys.argv[1]))
assert v["port"] == 5432 and v["egress"]["allowed"] is False and v["ingress"]["allowed_by"] == "db-allow ingress[0]", v
print(f"  denied by api's egress (isolated by {', '.join(v['egress']['isolated_by'])}); db's ingress allows it through {v['ingress']['allowed_by']}")
PY
k -n "$NS" describe networkpolicy db-allow > "$WORK/describe.txt" || fail "describe db-allow"
grep -q "$TOOLS" "$WORK/describe.txt" || fail "describe should show the namespace selector: $(cat "$WORK/describe.txt")"
sed -n '/Spec:/,$p' "$WORK/describe.txt" | head -20 | sed 's/^/  /'

if [[ "$ENFORCED" == "1" ]]; then
  echo "▸ labels are the identity: a throwaway pod with app=frontend gets the frontend's access, app=intruder does not"
  API_IP="$(k -n "$NS" get pod api -o jsonpath='{.status.podIP}')"
  final_phase() {
    local phase=""
    for _ in $(seq 1 90); do
      phase="$(k -n "$NS" get pod "$1" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
      if [[ "$phase" == "Succeeded" || "$phase" == "Failed" ]]; then break; fi
      sleep 1
    done
    echo "$phase"
  }
  for label in frontend intruder; do
    k -n "$NS" run "cc-probe-$label" --image="$PROBE_IMAGE" --restart=Never --labels="app=$label" -- sh -c "for i in 1 2 3 4 5; do nc -z -w 2 $API_IP 8080 && exit 0; sleep 1; done; exit 1" >/dev/null || fail "run cc-probe-$label"
  done
  GOOD="$(final_phase cc-probe-frontend)"
  BAD="$(final_phase cc-probe-intruder)"
  [[ "$GOOD" == "Succeeded" && "$BAD" == "Failed" ]] || fail "throwaway pods: app=frontend $GOOD (want Succeeded), app=intruder $BAD (want Failed); $(k -n "$NS" logs cc-probe-frontend 2>&1) $(k -n "$NS" logs cc-probe-intruder 2>&1)"
  echo "  app=frontend: $GOOD, app=intruder: $BAD"
  k -n "$NS" delete pod cc-probe-frontend cc-probe-intruder --wait=false >/dev/null 2>&1 || true
fi

echo "▸ the egress: [] trap, as the API server stores it"
python3 - code/fixtures/traps.json "$WORK/trap.json" "$WORK/invalid.json" "$NS" <<'PY'
import json, sys
traps, trap_path, invalid_path, ns = sys.argv[1:5]
items = json.load(open(traps))["items"]
trap = next(p for p in items if p["metadata"]["name"] == "deny-egress-typo")
json.dump(trap, open(trap_path, "w"), indent=2)
invalid = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "cc-invalid", "namespace": ns},
           "spec": {"podSelector": {}, "ingress": [{"from": [{"ipBlock": {"cidr": "10.0.0.0/8"}, "podSelector": {}}]}]}}
json.dump(invalid, open(invalid_path, "w"), indent=2)
PY
k apply --dry-run=server -o json -f "$WORK/trap.json" > "$WORK/trap-server.json" || fail "dry-run of the trap policy"
MP lint -f "$WORK/trap.json" --json > "$WORK/trap-lint.json" || true
python3 - "$WORK/trap-server.json" "$WORK/trap-lint.json" <<'PY' || fail "trap: $(cat "$WORK/trap-server.json") $(cat "$WORK/trap-lint.json")"
import json, sys
server, mini = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
assert server["spec"]["policyTypes"] == ["Ingress"], server["spec"]
assert mini["policies"][0]["policyTypes"] == ["Ingress"], mini["policies"]
assert any(f["code"] == "egress-not-isolated" for f in mini["findings"]), mini["findings"]
print("  API server: policyTypes ['Ingress'] for podSelector {} with egress []; mininetpol agrees and lint warns egress-not-isolated")
PY

echo "▸ an ipBlock and a podSelector in one peer: refused by both"
CODE=0
k apply --dry-run=server -f "$WORK/invalid.json" > "$WORK/invalid-server.txt" 2>&1 || CODE=$?
[[ "$CODE" != "0" ]] && grep -q "ipBlock" "$WORK/invalid-server.txt" || fail "the API server should refuse ipBlock with podSelector (exit $CODE): $(cat "$WORK/invalid-server.txt")"
CODE=0
MP lint -f "$WORK/invalid.json" > /dev/null 2> "$WORK/invalid-mini.txt" || CODE=$?
[[ "$CODE" == "2" ]] && grep -q "may not specify both ipBlock and another peer" "$WORK/invalid-mini.txt" || fail "mininetpol should refuse it with exit 2: $CODE $(cat "$WORK/invalid-mini.txt")"
echo "  kubectl:    $(tail -1 "$WORK/invalid-server.txt")"
echo "  mininetpol: $(cat "$WORK/invalid-mini.txt")"
if grep -q "may not specify both ipBlock and another peer" "$WORK/invalid-server.txt"; then echo "  same wording"; else echo "  (note: the API server words it differently in this release)"; fi

if [[ "$ENFORCED" == "1" ]]; then
  echo "SMOKE OK: security/network-policy"
else
  echo "SMOKE OK: security/network-policy (network plugin does not enforce NetworkPolicy; real-network comparison skipped)"
fi
