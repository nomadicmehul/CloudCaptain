#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
F="code/fixtures"
NS="cc-chaos-experiments"
CTX="${CC_SMOKE_CONTEXT:-kind-cc-smoke}"
NETNS="cc-chaos-srv"
VETH_H="cc-chaos-h"
VETH_S="cc-chaos-s"
HOST_IP="10.208.0.1"
SRV_IP="10.208.0.2"
HELM_VERSION="v4.3.1"
CHAOS_MESH_VERSION="2.8.7"
CHAOS_MESH_CHART_SHA256="c963e0f4ed2e30c1c7e236477f1c31fcf988b171991801bbe392546948436d38"
CHAOS_MESH_BUDGET_S="${CC_SMOKE_CHAOS_MESH_BUDGET_S:-150}"
CM_NS="cc-chaos-mesh"
CM_RELEASE="cc-chaos-mesh"
HELM="$WORK/bin/helm"
export HELM_CACHE_HOME="$WORK/helm/cache" HELM_CONFIG_HOME="$WORK/helm/config" HELM_DATA_HOME="$WORK/helm/data"
SUDO=""
[[ "$(id -u)" == "0" ]] || SUDO="sudo"
PIDS=()
DECOY_PID=""
NETNS_CREATED=""
NS_CREATED=""
CM_INSTALLED=""

k() { kubectl --context "$CTX" "$@"; }
mc() { python3 code/main.py "$@"; }
sudo_ok() { [[ -z "$SUDO" ]] || sudo -n true 2>/dev/null; }

cleanup() {
  set +e
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; done
  [[ -n "$DECOY_PID" ]] && kill "$DECOY_PID" 2>/dev/null
  pkill -f "main.py burn --seconds" 2>/dev/null
  if [[ -n "$NETNS_CREATED" && -e "/run/netns/$NETNS" ]]; then
    for pid in $($SUDO ip netns pids "$NETNS" 2>/dev/null); do $SUDO kill -9 "$pid" 2>/dev/null; done
    $SUDO ip netns del "$NETNS" 2>/dev/null
  fi
  [[ -n "$NETNS_CREATED" ]] && $SUDO ip link del "$VETH_H" 2>/dev/null
  if [[ -n "$CM_INSTALLED" ]]; then
    k -n "$NS" delete podchaos,networkchaos --all --timeout=30s >/dev/null 2>&1
    k -n "$NS" get podchaos,networkchaos -o jsonpath='{range .items[*]}{.kind}{" "}{.metadata.name}{"\n"}{end}' > "$WORK/leftover.txt" 2>/dev/null
    while read -r kind name; do
      [[ -n "$name" ]] && k -n "$NS" patch "$kind" "$name" --type=merge -p '{"metadata":{"finalizers":null}}' >/dev/null 2>&1
    done < "$WORK/leftover.txt"
    [[ -x "$HELM" ]] && "$HELM" uninstall "$CM_RELEASE" -n "$CM_NS" --kube-context "$CTX" --timeout 90s >/dev/null 2>&1
    k delete mutatingwebhookconfiguration,validatingwebhookconfiguration,clusterrole,clusterrolebinding -l "app.kubernetes.io/instance=$CM_RELEASE" --ignore-not-found >/dev/null 2>&1
    k get crd -o name 2>/dev/null | grep '\.chaos-mesh\.org$' > "$WORK/crds.txt"
    mapfile -t CRDS < "$WORK/crds.txt"
    [[ "${#CRDS[@]}" -gt 0 ]] && k delete "${CRDS[@]}" --timeout=90s >/dev/null 2>&1
    k delete namespace "$CM_NS" --ignore-not-found --wait=false >/dev/null 2>&1
  fi
  [[ -n "$NS_CREATED" ]] && k delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1
  $SUDO rm -rf "$WORK"
}
trap cleanup EXIT

field() {
  python3 - "$@" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2:]:
    value = value[int(key)] if isinstance(value, list) else value[key]
print(json.dumps(value) if isinstance(value, (dict, list)) else value)
PY
}
free_port() { python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'; }
edit_json() {
  python3 - "$@" <<'PY'
import json, sys
source, target, expression = sys.argv[1:4]
doc = json.load(open(source))
exec(expression, {"doc": doc})
json.dump(doc, open(target, "w"), indent=2)
PY
}

echo "▸ minichaos simulate: every fixture reaches its expected verdict and exit code"
for entry in "pod-delete held 0" "netem-latency aborted 1" "process-cpu held 0" "podchaos-experiment held 0" "wide-pod-delete refused 2"; do
  read -r name want want_code <<< "$entry"
  code=0
  mc simulate "$F/$name.json" --json > "$WORK/sim-$name.json" 2> "$WORK/sim-$name.err" || code=$?
  verdict="$(field "$WORK/sim-$name.json" verdict 2>/dev/null || echo "?")"
  [[ "$verdict" == "$want" && "$code" == "$want_code" ]] || fail "simulate $name: verdict $verdict exit $code, want $want exit $want_code: $(cat "$WORK/sim-$name.json" "$WORK/sim-$name.err")"
  echo "  $name: $verdict (exit $code): $(field "$WORK/sim-$name.json" reason)"
done
edit_json "$F/pod-delete.json" "$WORK/force.json" 'doc["method"]["faults"][0]["grace_period_s"] = 0'
code=0
mc simulate "$WORK/force.json" --json > "$WORK/sim-force.json" || code=$?
[[ "$code" == "1" && "$(field "$WORK/sim-force.json" verdict)" == "deviated" ]] || fail "a forced delete should deviate in the simulator: $(cat "$WORK/sim-force.json")"
echo "  grace_period_s 0: deviated, $(field "$WORK/sim-force.json" reason)"

echo "▸ minichaos validate: refusals that need no cluster, and the exact commands"
edit_json "$F/pod-delete.json" "$WORK/kube-system.json" 'doc["method"]["faults"][0].update(namespace="kube-system", selector="k8s-app=kube-dns"); doc["blast_radius"]["namespaces"].append("kube-system")'
edit_json "$F/netem-latency.json" "$WORK/host-netem.json" 'doc["method"]["faults"][0].pop("netns"); doc["method"]["faults"][0]["device"] = "eth0"'
for exp in kube-system host-netem; do
  code=0
  mc validate "$WORK/$exp.json" > "$WORK/validate-$exp.txt" 2>&1 || code=$?
  [[ "$code" == "2" ]] || fail "validate $exp should refuse with exit 2 (got $code): $(cat "$WORK/validate-$exp.txt")"
  grep "refused:" "$WORK/validate-$exp.txt" | sed 's/^/  /' || true
done
grep -q "runs cluster add-ons" "$WORK/validate-kube-system.txt" || fail "kube-system refusal: $(cat "$WORK/validate-kube-system.txt")"
grep -q "shapes the host's own eth0" "$WORK/validate-host-netem.txt" || fail "host netem refusal: $(cat "$WORK/validate-host-netem.txt")"
mc validate "$F/netem-latency.json" --commands > "$WORK/validate-netem.txt" || fail "validate netem: $(cat "$WORK/validate-netem.txt")"
grep -qF "ip netns exec $NETNS tc qdisc add dev $VETH_S root netem delay 200ms loss 5%" "$WORK/validate-netem.txt" || fail "netem command: $(cat "$WORK/validate-netem.txt")"

if [[ "$(uname -s)" == "Linux" ]] && command -v ip >/dev/null && command -v tc >/dev/null && command -v curl >/dev/null && sudo_ok; then
  echo "▸ network namespace $NETNS joined by a veth pair: the server inside, the load generator outside"
  if [[ -e "/run/netns/$NETNS" ]]; then $SUDO ip netns del "$NETNS"; fi
  $SUDO ip link del "$VETH_H" 2>/dev/null || true
  NETNS_CREATED=1
  $SUDO ip netns add "$NETNS"
  $SUDO ip link add "$VETH_H" type veth peer name "$VETH_S"
  $SUDO ip link set "$VETH_S" netns "$NETNS"
  $SUDO ip addr add "$HOST_IP/30" dev "$VETH_H"
  $SUDO ip link set "$VETH_H" up
  $SUDO ip netns exec "$NETNS" ip addr add "$SRV_IP/30" dev "$VETH_S"
  $SUDO ip netns exec "$NETNS" ip link set "$VETH_S" up
  $SUDO ip netns exec "$NETNS" ip link set lo up
  mkdir -p "$WORK/www"
  echo ok > "$WORK/www/index.html"
  $SUDO ip netns exec "$NETNS" python3 -m http.server 8080 --bind "$SRV_IP" --directory "$WORK/www" > "$WORK/http.log" 2>&1 &
  PIDS+=("$!")
  for _ in $(seq 1 50); do curl -fsS -m 1 -o /dev/null "http://$SRV_IP:8080/" 2>/dev/null && break; sleep 0.2; done
  curl -fsS -m 2 -o /dev/null "http://$SRV_IP:8080/" || fail "the server in $NETNS is not reachable at $SRV_IP:8080: $(cat "$WORK/http.log")"

  echo "▸ sch_netem: the kernel module behind tc netem"
  netem_ok() { $SUDO ip netns exec "$NETNS" tc qdisc add dev lo root netem delay 1ms >/dev/null 2>&1 && $SUDO ip netns exec "$NETNS" tc qdisc del dev lo root >/dev/null 2>&1; }
  NETEM=""
  if netem_ok; then
    NETEM=1
  else
    $SUDO modprobe sch_netem > "$WORK/modprobe.txt" 2>&1 || true
    if netem_ok; then
      NETEM=1
    elif command -v apt-get >/dev/null; then
      echo "  sch_netem is not loadable ($(cat "$WORK/modprobe.txt")); installing linux-modules-extra-$(uname -r)"
      timeout 60 $SUDO apt-get update -q > "$WORK/apt.txt" 2>&1 || true
      timeout 120 $SUDO apt-get install -y -q "linux-modules-extra-$(uname -r)" >> "$WORK/apt.txt" 2>&1 || true
      $SUDO modprobe sch_netem >> "$WORK/modprobe.txt" 2>&1 || true
      if netem_ok; then NETEM=1; fi
    fi
  fi
  if [[ -n "$NETEM" ]]; then
    echo "  netem available on kernel $(uname -r)"
  else
    echo "  (sch_netem is not available on kernel $(uname -r): $(tail -2 "$WORK/modprobe.txt" 2>/dev/null); netem checks skipped)"
  fi

  if [[ -n "$NETEM" ]]; then
    echo "▸ Use It: tc netem by hand inside $NETNS, timed with curl"
    T_BEFORE="$(curl -fsS -m 10 -o /dev/null -w '%{time_total}' "http://$SRV_IP:8080/")" || fail "curl before netem"
    $SUDO ip netns exec "$NETNS" tc qdisc add dev "$VETH_S" root netem delay 200ms loss 5% || fail "tc qdisc add netem on $VETH_S"
    $SUDO ip netns exec "$NETNS" tc -s qdisc show dev "$VETH_S" > "$WORK/qdisc.txt" || fail "tc -s qdisc show"
    grep -q "qdisc netem" "$WORK/qdisc.txt" && grep -q "delay 200" "$WORK/qdisc.txt" || fail "tc qdisc show should list netem with delay 200ms: $(cat "$WORK/qdisc.txt")"
    head -1 "$WORK/qdisc.txt" | sed 's/^/  /'
    T_DURING="$(curl -fsS -m 10 -o /dev/null -w '%{time_total}' "http://$SRV_IP:8080/")" || fail "curl with netem on"
    $SUDO ip netns exec "$NETNS" tc qdisc del dev "$VETH_S" root || fail "tc qdisc del on $VETH_S"
    T_AFTER="$(curl -fsS -m 10 -o /dev/null -w '%{time_total}' "http://$SRV_IP:8080/")" || fail "curl after netem"
    python3 -c 'import sys; b, d, a = map(float, sys.argv[1:]); assert d >= 0.38 and b < 0.15 and a < 0.15, (b, d, a)' "$T_BEFORE" "$T_DURING" "$T_AFTER" \
      || fail "curl time_total before/during/after netem should be fast/about 0.4 s/fast, got $T_BEFORE/$T_DURING/$T_AFTER"
    echo "  curl time_total: before $T_BEFORE s, with netem $T_DURING s (SYN-ACK and response each delayed), after $T_AFTER s"
  fi

  PORT="$(free_port)"
  SRC="http://127.0.0.1:$PORT/metrics"
  echo "▸ minichaos load against $SRV_IP:8080, its SLI on $SRC"
  python3 code/main.py load --url "http://$SRV_IP:8080/" --concurrency 8 --rate 40 --timeout 2 --metrics-port "$PORT" --json > "$WORK/load.json" 2> "$WORK/load.err" &
  LOAD_PID="$!"
  PIDS+=("$LOAD_PID")
  for _ in $(seq 1 50); do curl -fsS -m 1 -o /dev/null "http://127.0.0.1:$PORT/healthz" 2>/dev/null && break; sleep 0.2; done
  curl -fsS -m 2 "$SRC" > "$WORK/load-metrics.txt" || fail "load generator metrics: $(cat "$WORK/load.err")"
  grep -q '^minichaos_load_requests_total{outcome="ok"}' "$WORK/load-metrics.txt" || fail "load metrics: $(cat "$WORK/load-metrics.txt")"
  mc measure "$F/netem-latency.json" --source "$SRC" > "$WORK/measure-netns.txt" || fail "steady state before the runner experiments: $(cat "$WORK/measure-netns.txt")"
  sed 's/^/  /' "$WORK/measure-netns.txt"

  if [[ -n "$NETEM" ]]; then
    echo "▸ minichaos run netem-latency: 200 ms and 5% loss break p95, the abort fires early, the rollback removes the qdisc"
    code=0
    $SUDO python3 code/main.py run "$F/netem-latency.json" --source "$SRC" --json > "$WORK/netem-journal.json" 2> "$WORK/netem.err" || code=$?
    python3 - "$WORK/netem-journal.json" "$code" <<'PY' || fail "netem experiment (exit $code): $(cat "$WORK/netem-journal.json" "$WORK/netem.err")"
import json, sys
j = json.load(open(sys.argv[1]))
code = int(sys.argv[2])
assert code == 1 and j["verdict"] == "aborted", (code, j["verdict"], j["reason"])
assert "p95_latency_s" in j["reason"], j["reason"]
early = j["aborted_at_s"] - j["injected_at_s"]
assert early < 30, ("the abort should come long before the 60 s duration", early)
assert j["rollback_ok"] and "tc qdisc del" in j["faults"][0]["revert"], j["faults"]
assert j["recovered_after_s"] is not None, "the steady state should return after the rollback"
during = [m for m in j["measurements"] if m["phase"] == "during"]
worst = max(r["value"] for m in during for r in m["readings"] if r["sli"] == "p95_latency_s" and r["value"] is not None)
assert worst > 0.15, worst
print(f"  aborted {early:.1f} s after injection of a 60 s fault, worst p95 {worst:.3f} s, recovered {j['recovered_after_s']:g} s after the rollback")
print(f"  rollback: {j['faults'][0]['revert']}")
PY
    $SUDO ip netns exec "$NETNS" tc qdisc show dev "$VETH_S" > "$WORK/qdisc-after.txt"
    ! grep -q netem "$WORK/qdisc-after.txt" || fail "netem is still on $VETH_S after the rollback: $(cat "$WORK/qdisc-after.txt")"
    echo "  tc qdisc show dev $VETH_S: $(head -1 "$WORK/qdisc-after.txt")"
  fi

  echo "▸ minichaos run process-cpu: kill a decoy process and burn two cores while the server keeps answering"
  bash -c 'exec -a cc-chaos-decoy sleep 300' &
  DECOY_PID="$!"
  for _ in $(seq 1 30); do grep -qa cc-chaos-decoy "/proc/$DECOY_PID/cmdline" 2>/dev/null && break; sleep 0.1; done
  grep -qa cc-chaos-decoy "/proc/$DECOY_PID/cmdline" || fail "the decoy did not start"
  code=0
  mc run "$F/process-cpu.json" --source "$SRC" --json > "$WORK/proc-journal.json" 2> "$WORK/proc.err" || code=$?
  python3 - "$WORK/proc-journal.json" "$code" "$DECOY_PID" <<'PY' || fail "process-cpu experiment (exit $code): $(cat "$WORK/proc-journal.json" "$WORK/proc.err")"
import json, sys
j = json.load(open(sys.argv[1]))
code, decoy = int(sys.argv[2]), sys.argv[3]
assert code == 0 and j["verdict"] == "held", (code, j["verdict"], j["reason"])
kill, burn = j["faults"]
assert kill["targets"] == [f"{decoy} cc-chaos-decoy 300"], kill["targets"]
assert decoy in kill["result"], kill["result"]
assert burn["result"].startswith("started 2 burner(s)") and burn["revert"] == "stopped 2 burner(s)", burn
print(f"  held: {kill['result']}; {burn['result'].split(';')[0]}, then {burn['revert']}")
PY
  STATE="$(ps -o stat= -p "$DECOY_PID" 2>/dev/null || true)"
  [[ -z "$STATE" || "$STATE" == Z* ]] || fail "the decoy $DECOY_PID survived the process-kill fault (state $STATE)"
  wait "$DECOY_PID" 2>/dev/null || true
  DECOY_PID=""
  if pgrep -af "main.py burn --seconds" > "$WORK/burners.txt"; then fail "burners still running after the rollback: $(cat "$WORK/burners.txt")"; fi

  kill "$LOAD_PID" 2>/dev/null || true
  wait "$LOAD_PID" 2>/dev/null || true
  python3 - "$WORK/load.json" <<'PY' || fail "load generator report: $(cat "$WORK/load.json" "$WORK/load.err")"
import json, sys
report = json.load(open(sys.argv[1]))
assert report["ok"] > 100, report
print(f"  load generator: {report['ok']} ok, {report['errors']} errors, reasons {report['reasons']}")
PY
else
  echo "  (Linux with ip, tc, curl and passwordless sudo needed; network namespace, netem and process experiments skipped)"
fi

if ! command -v kubectl >/dev/null; then
  echo "  (kubectl not installed; cluster checks skipped)"
  echo "SMOKE OK: observability/chaos-experiments (cluster checks skipped)"
  exit 0
fi
kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if ! grep -qx "$CTX" "$WORK/contexts.txt"; then
  echo "  (context $CTX not found; this smoke deletes pods, so it only runs against the throwaway kind cluster)"
  echo "SMOKE OK: observability/chaos-experiments (cluster checks skipped)"
  exit 0
fi

pod_names() {
  k -n "$NS" get pods -l app=web --field-selector=status.phase=Running -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.deletionTimestamp}{"\n"}{end}' > "$WORK/pods-raw.txt" || fail "list the web pods"
  awk 'NF == 1 {print $1}' "$WORK/pods-raw.txt" | sort > "$1"
}
wait_gone() {
  for _ in $(seq 1 60); do
    k -n "$NS" get pod "$1" -o name > "$WORK/gone.txt" 2>/dev/null || return 0
    [[ -s "$WORK/gone.txt" ]] || return 0
    sleep 1
  done
  return 1
}
one_replaced() {
  python3 - "$1" "$2" "$3" <<'PY'
import sys
before = set(open(sys.argv[1]).read().split())
after = set(open(sys.argv[2]).read().split())
expected_gone = sys.argv[3]
gone, new = before - after, after - before
assert len(before) == 3 and len(after) == 3, (sorted(before), sorted(after))
assert len(gone) == 1 and len(new) == 1, ("exactly one pod should be replaced", sorted(gone), sorted(new))
assert not expected_gone or gone == {expected_gone}, (expected_gone, sorted(gone))
print(f"  replaced {gone.pop()} with {new.pop()}; 3 Running")
PY
}

echo "▸ kind ($CTX): web (three nginx:1.30-alpine replicas behind a Service) and the cc-load generator in $NS"
k delete namespace "$NS" --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1 || true
NS_CREATED=1
k create namespace "$NS" >/dev/null || fail "create namespace $NS"
k -n "$NS" create configmap minichaos --from-file=main.py=code/main.py >/dev/null || fail "configmap"
k -n "$NS" apply -f "$F/k8s/web.yaml" > "$WORK/apply-web.txt" 2>&1 || fail "apply web.yaml: $(cat "$WORK/apply-web.txt")"
k -n "$NS" rollout status deployment/web --timeout=180s > /dev/null || fail "web not ready: $(k -n "$NS" get pods -o wide 2>&1; k -n "$NS" describe pods 2>&1 | tail -30)"
k -n "$NS" apply -f "$F/k8s/load-pod.yaml" > "$WORK/apply-load.txt" 2>&1 || fail "apply load-pod.yaml: $(cat "$WORK/apply-load.txt")"
k -n "$NS" wait --for=condition=Ready pod/cc-load --timeout=180s > /dev/null || fail "cc-load not ready: $(k -n "$NS" describe pod cc-load 2>&1 | tail -30; k -n "$NS" logs cc-load 2>&1 | tail -20)"
k get --raw "/api/v1/namespaces/$NS/pods/cc-load:9100/proxy/metrics" > "$WORK/pod-metrics.txt" || fail "the API server proxy to cc-load:9100/metrics"
grep -q '^minichaos_load_requests_total{outcome="ok"}' "$WORK/pod-metrics.txt" || fail "cc-load metrics: $(cat "$WORK/pod-metrics.txt")"

echo "▸ minichaos measure: the steady state through the API server's pod proxy"
code=1
for _ in $(seq 1 8); do
  code=0
  mc measure "$F/pod-delete.json" --context "$CTX" > "$WORK/measure-kind.txt" 2>&1 || code=$?
  [[ "$code" == "0" ]] && break
  sleep 1
done
[[ "$code" == "0" ]] || fail "steady state on kind: $(cat "$WORK/measure-kind.txt"; k -n "$NS" logs cc-load 2>&1 | tail -10)"
sed 's/^/  /' "$WORK/measure-kind.txt"

echo "▸ minichaos validate --resolve: fixed-percent 34 rounds up to 2 of the 3 real pods, the blast radius allows 1"
code=0
mc validate "$F/wide-pod-delete.json" --resolve --context "$CTX" > "$WORK/wide.txt" 2>&1 || code=$?
[[ "$code" == "2" ]] && grep -q "would affect 2 of 3 pods" "$WORK/wide.txt" || fail "wide pod-delete should be refused against the cluster (exit $code): $(cat "$WORK/wide.txt")"
grep "refused:" "$WORK/wide.txt" | sed 's/^/  /' || true

echo "▸ minichaos run pod-delete: one of three pods deleted gracefully, errors stay under 5%, the Deployment replaces it"
pod_names "$WORK/pods-before.txt"
code=0
mc run "$F/pod-delete.json" --context "$CTX" --journal "$WORK/pd-journal.json" > "$WORK/pd.txt" 2>&1 || code=$?
sed 's/^/  /' "$WORK/pd.txt"
DELETED="$(python3 - "$WORK/pd-journal.json" "$code" <<'PY'
import json, sys
j = json.load(open(sys.argv[1]))
assert int(sys.argv[2]) == 0 and j["verdict"] == "held", (sys.argv[2], j["verdict"], j["reason"])
result = j["faults"][0]["result"]
assert result.startswith("deleted ") and "," not in result, result
print(result.split(" ", 1)[1])
PY
)" || fail "pod-delete experiment (exit $code): $(cat "$WORK/pd.txt")"
grep -qx "$DELETED" "$WORK/pods-before.txt" || fail "minichaos deleted $DELETED, which was not one of the Running web pods: $(cat "$WORK/pods-before.txt")"
wait_gone "$DELETED" || fail "$DELETED is still there 60 s after the experiment"
k -n "$NS" rollout status deployment/web --timeout=90s > /dev/null || fail "web did not return to 3 ready"
pod_names "$WORK/pods-after.txt"
one_replaced "$WORK/pods-before.txt" "$WORK/pods-after.txt" "$DELETED" || fail "pods before/after: $(cat "$WORK/pods-before.txt") / $(cat "$WORK/pods-after.txt")"

echo "▸ Chaos Mesh $CHAOS_MESH_VERSION: the same pod deletion as a PodChaos, measured by minichaos (elapsed ${SECONDS} s)"
CM_READY=""
if [[ -n "${CC_SMOKE_SKIP_CHAOS_MESH:-}" ]]; then
  echo "  (CC_SMOKE_SKIP_CHAOS_MESH is set; PodChaos comparison skipped)"
elif (( SECONDS > CHAOS_MESH_BUDGET_S )); then
  echo "  (the smoke is at ${SECONDS} s, over its ${CHAOS_MESH_BUDGET_S} s budget for installing Chaos Mesh; PodChaos comparison skipped)"
elif k get crd podchaos.chaos-mesh.org >/dev/null 2>&1; then
  echo "  (Chaos Mesh CRDs already exist in $CTX; not installing over another installation, PodChaos comparison skipped)"
else
  case "$(uname -m)" in x86_64) ARCH=amd64 ;; aarch64|arm64) ARCH=arm64 ;; *) fail "unsupported architecture $(uname -m)" ;; esac
  mkdir -p "$WORK/bin" "$WORK/dl"
  TARBALL="helm-$HELM_VERSION-linux-$ARCH.tar.gz"
  curl -fsSL --retry 3 -o "$WORK/dl/$TARBALL" "https://get.helm.sh/$TARBALL" || fail "download $TARBALL"
  curl -fsSL --retry 3 -o "$WORK/dl/$TARBALL.sha256sum" "https://get.helm.sh/$TARBALL.sha256sum" || fail "download $TARBALL.sha256sum"
  WANT="$(awk '{print $1}' "$WORK/dl/$TARBALL.sha256sum")"
  GOT="$(sha256sum "$WORK/dl/$TARBALL" | awk '{print $1}')"
  [[ -n "$WANT" && "$WANT" == "$GOT" ]] || fail "helm checksum mismatch: want $WANT got $GOT"
  tar -xzf "$WORK/dl/$TARBALL" -C "$WORK/dl"
  mv "$WORK/dl/linux-$ARCH/helm" "$HELM"
  echo "  helm $("$HELM" version --short), sha256 $GOT"
  CHART="$WORK/dl/chaos-mesh-$CHAOS_MESH_VERSION.tgz"
  curl -fsSL --retry 3 -o "$CHART" "https://charts.chaos-mesh.org/chaos-mesh-$CHAOS_MESH_VERSION.tgz" || fail "download the Chaos Mesh chart"
  GOT="$(sha256sum "$CHART" | awk '{print $1}')"
  [[ "$GOT" == "$CHAOS_MESH_CHART_SHA256" ]] || fail "Chaos Mesh chart checksum mismatch: want $CHAOS_MESH_CHART_SHA256 (the digest in charts.chaos-mesh.org/index.yaml) got $GOT"
  echo "  chart chaos-mesh-$CHAOS_MESH_VERSION.tgz, sha256 $GOT"
  CM_INSTALLED=1
  if "$HELM" install "$CM_RELEASE" "$CHART" -n "$CM_NS" --create-namespace --kube-context "$CTX" --timeout 120s \
      --set chaosDaemon.runtime=containerd --set chaosDaemon.socketPath=/run/containerd/containerd.sock \
      --set controllerManager.replicaCount=1 --set dashboard.create=false --set dnsServer.create=false > "$WORK/helm-cm.txt" 2>&1; then
    if k -n "$CM_NS" rollout status deployment/chaos-controller-manager --timeout=150s > "$WORK/cm-rollout.txt" 2>&1; then
      for _ in $(seq 1 30); do
        if k apply --dry-run=server -f "$F/podchaos.json" > "$WORK/cm-dry.txt" 2>&1; then CM_READY=1; break; fi
        sleep 2
      done
      [[ -n "$CM_READY" ]] || echo "  (the Chaos Mesh admission webhook never accepted a dry run: $(tail -3 "$WORK/cm-dry.txt"); PodChaos comparison skipped)"
    else
      echo "  (chaos-controller-manager not ready within 150 s: $(k -n "$CM_NS" get pods -o wide 2>&1 | tail -5); PodChaos comparison skipped)"
    fi
  else
    echo "  (helm install of Chaos Mesh failed: $(tail -5 "$WORK/helm-cm.txt"); PodChaos comparison skipped)"
  fi
fi

if [[ -n "$CM_READY" ]]; then
  echo "  controller ready after ${SECONDS} s; running podchaos-experiment.json"
  pod_names "$WORK/cm-before.txt"
  code=0
  mc run "$F/podchaos-experiment.json" --context "$CTX" --journal "$WORK/cm-journal.json" > "$WORK/cm.txt" 2>&1 || code=$?
  sed 's/^/  /' "$WORK/cm.txt"
  python3 - "$WORK/cm-journal.json" "$WORK/pd-journal.json" "$code" <<'PY' || fail "PodChaos experiment (exit $code): $(cat "$WORK/cm.txt")"
import json, sys
theirs, mine = (json.load(open(p)) for p in sys.argv[1:3])
assert int(sys.argv[3]) == 0 and theirs["verdict"] == "held", (sys.argv[3], theirs["verdict"], theirs["reason"])
assert theirs["verdict"] == mine["verdict"], (theirs["verdict"], mine["verdict"])
assert "AllInjected" in theirs["faults"][0]["result"] and theirs["faults"][0]["revert"].startswith("deleted PodChaos"), theirs["faults"]
worst = lambda j: max((r["value"] or 0) for m in j["measurements"] if m["phase"] == "during" for r in m["readings"] if r["sli"] == "error_rate")
print(f"  same verdict: pod-delete {mine['verdict']} (worst error_rate {worst(mine):.3f}), PodChaos {theirs['verdict']} (worst error_rate {worst(theirs):.3f})")
PY
  if k -n "$NS" get podchaos cc-web-pod-kill -o name > "$WORK/podchaos-left.txt" 2>/dev/null && [[ -s "$WORK/podchaos-left.txt" ]]; then
    fail "the rollback should have deleted the PodChaos: $(cat "$WORK/podchaos-left.txt")"
  fi
  for _ in $(seq 1 60); do
    pod_names "$WORK/cm-after.txt"
    if [[ "$(wc -l < "$WORK/cm-after.txt")" -eq 3 ]] && [[ "$(comm -12 "$WORK/cm-before.txt" "$WORK/cm-after.txt" | wc -l)" -eq 2 ]]; then break; fi
    sleep 1
  done
  k -n "$NS" rollout status deployment/web --timeout=90s > /dev/null || fail "web did not return to 3 ready after the PodChaos"
  one_replaced "$WORK/cm-before.txt" "$WORK/cm-after.txt" "" || fail "PodChaos should replace exactly one pod: $(cat "$WORK/cm-before.txt") / $(cat "$WORK/cm-after.txt")"
fi

echo "SMOKE OK: observability/chaos-experiments"
