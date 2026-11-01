#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
NS="cc-services-dns"
K=(kubectl)
CLUSTER=""
cleanup() {
  if [[ -n "$CLUSTER" ]]; then
    "${K[@]}" delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT
MP() { python3 code/main.py "$@"; }
poll() {
  local what="$1" tries="$2"
  shift 2
  for _ in $(seq 1 "$tries"); do
    if "$@"; then return 0; fi
    sleep 1
  done
  fail "timed out after ${tries}s waiting for $what"
}
FX=code/fixtures/shop.json

echo "▸ Build It: the EndpointSlice controller keeps every pod but marks the unready one"
MP endpoints shop/web --from "$FX" --json > "$WORK/fx-endpoints.json"
python3 - "$WORK/fx-endpoints.json" <<'PY' || fail "fixture endpoints: $(cat "$WORK/fx-endpoints.json")"
import json, sys
slices = json.load(open(sys.argv[1]))["shop/web"]
assert len(slices) == 1, slices
s = slices[0]
assert [(p["name"], p["port"]) for p in s["ports"]] == [("http", 8080)], s["ports"]
got = [(e["address"], e["ready"]) for e in s["endpoints"]]
assert got == [("10.244.1.5", True), ("10.244.1.6", True), ("10.244.2.7", True), ("10.244.2.8", False)], got
print("  shop/web: named targetPort http -> 8080,", len(got), "endpoints,", sum(r for _, r in got), "ready")
PY

echo "▸ Build It: iptables render, then trace it back to three pods at one third each"
MP iptables --from "$FX" --cluster-cidr 10.244.0.0/16 --node kind-worker > "$WORK/fx-nat.txt"
grep -q -- "--probability 0.33333333349 -j KUBE-SEP-" "$WORK/fx-nat.txt" || fail "no 1/3 statistic rule in: $(cat "$WORK/fx-nat.txt")"
grep -q 'shop/empty:http has no endpoints" -m tcp --dport 80 -j REJECT' "$WORK/fx-nat.txt" || fail "no REJECT for a Service without endpoints"
MP trace --ruleset "$WORK/fx-nat.txt" --to 10.96.0.20:80 --simulate 3000 --seed 7 --json > "$WORK/fx-trace.json"
python3 - "$WORK/fx-trace.json" <<'PY' || fail "fixture trace: $(cat "$WORK/fx-trace.json")"
import json, sys
t = json.load(open(sys.argv[1]))
assert [h["probability"] for h in t["endpoints"]] == ["0.33333333349", "0.50000000000", None], t["endpoints"]
assert all(abs(h["share"] - 1 / 3) < 1e-6 for h in t["endpoints"]), t["endpoints"]
assert all(800 < n < 1200 for n in t["simulation"].values()), t["simulation"]
print("  shares", [round(h["share"], 3) for h in t["endpoints"]], "simulated", t["simulation"])
PY
set +e
MP trace --ruleset "$WORK/fx-nat.txt" --to 10.96.0.40:80 > "$WORK/fx-reject.txt"
CODE=$?
set -e
[[ "$CODE" == "1" ]] && grep -q "REJECT in filter/KUBE-SERVICES" "$WORK/fx-reject.txt" || fail "empty Service should trace to a REJECT (exit $CODE): $(cat "$WORK/fx-reject.txt")"
MP trace --ruleset code/fixtures/node-nat.txt --to 10.96.0.10:53 --proto udp > "$WORK/fx-node.txt" || fail "kube-dns trace on the node fixture: $(cat "$WORK/fx-node.txt")"
grep -q "DNAT 10.244.0.3:53" "$WORK/fx-node.txt" || fail "kube-dns trace: $(cat "$WORK/fx-node.txt")"

echo "▸ Build It: the nftables rendering reaches the same three endpoints through one map lookup"
MP nftables --from "$FX" --cluster-cidr 10.244.0.0/16 > "$WORK/fx.nft"
grep -q "numgen random mod 3 vmap" "$WORK/fx.nft" || fail "no numgen vmap in: $(cat "$WORK/fx.nft")"
MP trace --ruleset "$WORK/fx.nft" --to 10.96.0.20:80 --json > "$WORK/fx-nft-trace.json"
python3 -c 'import json,sys; t=json.load(open(sys.argv[1])); d=[h["destination"] for h in t["endpoints"]]; assert t["mode"]=="nftables" and d==["10.244.1.5:8080","10.244.1.6:8080","10.244.2.7:8080"], t; print("  nft:", d)' "$WORK/fx-nft-trace.json" || fail "nft trace: $(cat "$WORK/fx-nft-trace.json")"

echo "▸ Build It: DNS records, and what ndots:5 costs an external name"
MP dns web-headless --namespace shop --from "$FX" --json > "$WORK/fx-headless.json"
MP dns api.partner.example.com --namespace shop --from "$FX" --json > "$WORK/fx-external.json"
MP dns api.partner.example.com. --namespace shop --from "$FX" --json > "$WORK/fx-dotted.json"
python3 - "$WORK/fx-headless.json" "$WORK/fx-external.json" "$WORK/fx-dotted.json" <<'PY' || fail "fixture dns"
import json, sys
headless, external, dotted = (json.load(open(p)) for p in sys.argv[1:4])
assert sorted(a.split()[-1] for a in headless["answer"]) == ["10.244.1.5", "10.244.1.6", "10.244.2.7"], headless
assert (len(external["attempts"]), external["nxdomains"]) == (4, 3), external
assert len(dotted["attempts"]) == 1, dotted
print("  headless: 3 A records; api.partner.example.com: 4 names (3 NXDOMAIN) without the trailing dot, 1 with it")
PY

if ! command -v kubectl >/dev/null || ! command -v docker >/dev/null; then
  echo "  (kubectl or docker not installed; cluster steps skipped)"
  echo "SMOKE OK: orchestration/services-dns"
  exit 0
fi
kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if grep -qx kind-cc-smoke "$WORK/contexts.txt"; then K=(kubectl --context kind-cc-smoke); fi
if ! "${K[@]}" cluster-info > "$WORK/cluster-info.txt" 2>&1; then
  echo "  (no reachable cluster; cluster steps skipped)"
  echo "SMOKE OK: orchestration/services-dns"
  exit 0
fi

echo "▸ Use It: a 3-replica Deployment, one never-ready pod, a ClusterIP and a headless Service in $NS"
CLUSTER=1
"${K[@]}" delete namespace "$NS" --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1 || true
"${K[@]}" create namespace "$NS" >/dev/null || fail "create namespace $NS"
"${K[@]}" -n "$NS" apply -f code/fixtures/web.yaml >/dev/null || fail "apply code/fixtures/web.yaml"
"${K[@]}" -n "$NS" rollout status deployment/web --timeout=180s || fail "deployment web did not become ready: $("${K[@]}" -n "$NS" get pods -o wide 2>&1)"
"${K[@]}" -n "$NS" wait --for=jsonpath='{.status.phase}'=Running pod/web-unready --timeout=120s >/dev/null || fail "web-unready not running: $("${K[@]}" -n "$NS" describe pod web-unready 2>&1 | tail -20)"
slices_settled() {
  "${K[@]}" -n "$NS" get endpointslices -l kubernetes.io/service-name=web -o json > "$WORK/web-slices.json" 2>/dev/null || return 1
  python3 - "$WORK/web-slices.json" <<'PY'
import json, sys
endpoints = [e for s in json.load(open(sys.argv[1]))["items"] for e in (s.get("endpoints") or [])]
ready = [e for e in endpoints if (e.get("conditions") or {}).get("ready")]
sys.exit(0 if len(endpoints) == 4 and len(ready) == 3 else 1)
PY
}
poll "the web EndpointSlice to list 4 endpoints, 3 ready" 60 slices_settled
"${K[@]}" -n "$NS" get svc,endpointslices -o wide
"${K[@]}" -n "$NS" get svc,pods,endpointslices -o json > "$WORK/objects.json"
CIP="$("${K[@]}" -n "$NS" get svc web -o jsonpath='{.spec.clusterIP}')"
[[ -n "$CIP" ]] || fail "service web has no ClusterIP"

echo "▸ miniproxy's EndpointSlice controller agrees with the real one"
MP endpoints --from "$WORK/objects.json" --slices computed --json > "$WORK/computed.json"
MP endpoints --from "$WORK/objects.json" --slices input --json > "$WORK/real.json"
python3 - "$WORK/computed.json" "$WORK/real.json" "$NS" <<'PY' || fail "computed and real EndpointSlices differ"
import json, sys
computed, real = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
ns = sys.argv[3]
def view(slices):
    return sorted((e["address"], tuple(sorted((p["name"], p["port"]) for p in s["ports"])), e["ready"], e["serving"], e["terminating"]) for s in slices for e in s["endpoints"])
for key in (f"{ns}/web", f"{ns}/web-headless"):
    a, b = view(computed[key]), view(real[key])
    assert a == b, (key, a, b)
    print(f"  {key}: {len(a)} endpoints, {sum(1 for e in a if e[2])} ready, ports {a[0][1]}: identical")
PY

echo "▸ the proxy mode, and the node's real ruleset for $CIP:80"
"${K[@]}" -n kube-system get configmap kube-proxy -o jsonpath='{.data.config\.conf}' > "$WORK/kube-proxy.conf" 2>/dev/null || true
python3 - "$WORK/kube-proxy.conf" > "$WORK/mode.txt" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
mode = re.search(r'^mode:\s*"?([a-z]*)"?\s*$', text, re.M)
cidr = re.search(r'^clusterCIDR:\s*"?([0-9a-fA-F.:/,]+)"?\s*$', text, re.M)
print(mode.group(1) if mode and mode.group(1) else "iptables", cidr.group(1).split(",")[0] if cidr else "none")
PY
read -r MODE CIDR < "$WORK/mode.txt"
NODE="$("${K[@]}" get nodes -o jsonpath='{.items[0].metadata.name}')"
echo "  kube-proxy mode: $MODE, clusterCIDR: $CIDR, node: $NODE"
CIDR_ARGS=()
[[ "$CIDR" != "none" ]] && CIDR_ARGS=(--cluster-cidr "$CIDR")
if ! docker inspect "$NODE" >/dev/null 2>&1; then
  echo "  (node $NODE is not a local kind container; ruleset comparison skipped)"
elif [[ "$MODE" == "iptables" || "$MODE" == "nftables" ]]; then
  if [[ "$MODE" == "iptables" ]]; then
    dump_rules() { docker exec "$NODE" iptables-save -t nat > "$WORK/node-rules.txt"; }
    MP iptables --from "$WORK/objects.json" --slices input ${CIDR_ARGS[@]+"${CIDR_ARGS[@]}"} > "$WORK/mini-rules.txt"
  else
    dump_rules() { docker exec "$NODE" nft list table ip kube-proxy > "$WORK/node-rules.txt"; }
    MP nftables --from "$WORK/objects.json" --slices input ${CIDR_ARGS[@]+"${CIDR_ARGS[@]}"} > "$WORK/mini-rules.txt"
  fi
  node_programmed() {
    dump_rules || return 1
    MP trace --ruleset "$WORK/node-rules.txt" --to "$CIP:80" --json > "$WORK/node-trace.json" || return 1
    python3 -c 'import json,sys; sys.exit(0 if len(json.load(open(sys.argv[1]))["endpoints"]) == 3 else 1)' "$WORK/node-trace.json"
  }
  poll "kube-proxy to program 3 endpoints for $CIP:80" 45 node_programmed
  grep -F "$NS/web:http" "$WORK/node-rules.txt" > "$WORK/node-web.txt" || grep -F "$NS/web/" "$WORK/node-rules.txt" > "$WORK/node-web.txt" || true
  cat "$WORK/node-web.txt"
  MP trace --ruleset "$WORK/node-rules.txt" --to "$CIP:80"
  MP trace --ruleset "$WORK/mini-rules.txt" --to "$CIP:80" --json > "$WORK/mini-trace.json" || fail "miniproxy's own rules do not reach $CIP:80: $(cat "$WORK/mini-rules.txt")"
  python3 - "$WORK/node-trace.json" "$WORK/mini-trace.json" "$WORK/objects.json" "$MODE" "$NS" <<'PY' || fail "node ruleset and miniproxy disagree"
import base64, hashlib, json, sys
node, mini, objects = (json.load(open(p)) for p in sys.argv[1:4])
mode, ns = sys.argv[4], sys.argv[5]
pods = [p for p in objects["items"] if p["kind"] == "Pod" and p["metadata"].get("labels", {}).get("app") == "web"]
ready = {p["status"]["podIP"] + ":8080" for p in pods if p["status"].get("podIP") and any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))}
destinations = lambda t: {h["destination"] for h in t["endpoints"]}
assert len(ready) == 3, ready
assert destinations(node) == destinations(mini) == ready, (destinations(node), destinations(mini), ready)
assert len(node["endpoints"]) == len(mini["endpoints"]) == 3, (node["endpoints"], mini["endpoints"])
if mode == "iptables":
    sequence = lambda t: [round(float(h["probability"]), 5) if h["probability"] else None for h in t["endpoints"]]
    assert sequence(node) == sequence(mini) == [0.33333, 0.5, None], (sequence(node), sequence(mini))
    exact = [h["probability"] for h in node["endpoints"]] == [h["probability"] for h in mini["endpoints"]]
    print("  iptables: 3 KUBE-SEP chains on both sides, probabilities", sequence(node), "exact strings equal:", exact)
    expected = "KUBE-SVC-" + base64.b32encode(hashlib.sha256(f"{ns}/web:httptcp".encode()).digest()).decode()[:16]
    print("  node chain", node["path"][-1], "| base32(sha256(ns/name:port + proto)):", expected, "| miniproxy chain", mini["path"][-1])
else:
    print("  nftables: numgen over 3 endpoint chains on both sides")
print("  same DNAT targets:", sorted(ready))
print("  masquerade on the node:", node["masquerade"][:1], "| miniproxy:", mini["masquerade"][:1])
PY
else
  echo "  (kube-proxy mode $MODE; neither iptables nor nftables, ruleset comparison skipped)"
fi

echo "▸ Use It: a pod resolves the Service names through CoreDNS with ndots:5"
"${K[@]}" -n kube-system get configmap coredns -o jsonpath='{.data.Corefile}' > "$WORK/Corefile" 2>/dev/null || true
grep -E "kubernetes cluster.local|forward|cache" "$WORK/Corefile" || echo "  (CoreDNS Corefile not readable)"
PROBE='
import json, os, socket
ns = os.environ["NS"]
def addrs(name):
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(name, 80, socket.AF_INET, socket.SOCK_STREAM)})
    except socket.gaierror as exc:
        return ["error: %s" % exc]
out = {"resolv": open("/etc/resolv.conf").read()}
for name in ("web", "web-headless", "web." + ns, "web.%s.svc.cluster.local." % ns):
    out[name] = addrs(name)
print(json.dumps(out))
'
"${K[@]}" -n "$NS" run dnsprobe --image=python:3.12-alpine --restart=Never --env="NS=$NS" --command -- python3 -c "$PROBE" >/dev/null || fail "kubectl run dnsprobe"
"${K[@]}" -n "$NS" wait --for=jsonpath='{.status.phase}'=Succeeded pod/dnsprobe --timeout=120s >/dev/null || fail "dnsprobe did not succeed: $("${K[@]}" -n "$NS" logs dnsprobe 2>&1 | tail -5) $("${K[@]}" -n "$NS" describe pod dnsprobe 2>&1 | tail -10)"
"${K[@]}" -n "$NS" logs dnsprobe > "$WORK/probe.json"
DNSIP="$("${K[@]}" -n kube-system get svc kube-dns -o jsonpath='{.spec.clusterIP}')"
python3 - "$WORK/probe.json" "$CIP" "$DNSIP" "$NS" "$WORK/resolv.conf" <<'PY' || fail "pod DNS: $(cat "$WORK/probe.json")"
import json, sys
sys.path.insert(0, "code")
import main
probe = json.load(open(sys.argv[1]))
cip, dns_ip, ns, out = sys.argv[2:6]
open(out, "w").write(probe["resolv"])
conf = main.parse_resolv_conf(probe["resolv"])
assert conf["ndots"] == 5, conf
assert conf["search"][:3] == main.search_domains(ns), conf
assert conf["nameservers"] == [dns_ip], conf
for name in ("web", "web." + ns, f"web.{ns}.svc.cluster.local."):
    assert probe[name] == [cip], (name, probe[name], cip)
assert len(probe["web-headless"]) == 3, probe["web-headless"]
print("  resolv.conf:", " | ".join(probe["resolv"].strip().splitlines()))
print("  web ->", cip, " web-headless ->", probe["web-headless"])
PY

echo "▸ miniproxy dns gives the pod's answers from the same objects and resolv.conf"
MP dns web-headless --namespace "$NS" --from "$WORK/objects.json" --slices input --json > "$WORK/mini-headless.json" || fail "miniproxy dns web-headless"
MP dns "web.$NS" --resolv-conf "$WORK/resolv.conf" --from "$WORK/objects.json" --json > "$WORK/mini-web.json" || fail "miniproxy dns web.$NS"
python3 - "$WORK/mini-headless.json" "$WORK/mini-web.json" "$WORK/probe.json" "$CIP" <<'PY' || fail "miniproxy dns disagrees with the pod"
import json, sys
headless, web, probe = (json.load(open(p)) for p in sys.argv[1:4])
cip = sys.argv[4]
assert sorted(a.split()[-1] for a in headless["answer"]) == probe["web-headless"], (headless["answer"], probe["web-headless"])
assert [a["rcode"] for a in web["attempts"]] == ["NXDOMAIN", "NOERROR"], web["attempts"]
assert web["answer"][0].split()[-1] == cip, web["answer"]
print("  headless A records match the pod; web.<ns> costs", len(web["attempts"]), "names with this resolv.conf")
PY

echo "SMOKE OK: orchestration/services-dns"
