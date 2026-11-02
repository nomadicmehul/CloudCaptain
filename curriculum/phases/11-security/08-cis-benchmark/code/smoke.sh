#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
NODE="${CC_SMOKE_NODE:-cc-smoke-control-plane}"
CTX="${CC_SMOKE_CONTEXT:-kind-cc-smoke}"
NS="cc-cis-benchmark"
KB_VERSION="0.16.0"
KB_BENCHMARK="cis-1.12"
KB_IMAGE="docker.io/aquasec/kube-bench:v${KB_VERSION}"
KB_DIR="/opt/cc-kube-bench"
F="code/fixtures"
NODE_DIR_CREATED=""
NS_CREATED=""
k() { kubectl --context "$CTX" "$@"; }
mb() { python3 code/main.py "$@"; }
cleanup() {
  if [[ -n "$NODE_DIR_CREATED" ]]; then docker exec "$NODE" rm -rf "$KB_DIR" >/dev/null 2>&1 || true; fi
  if [[ -n "$NS_CREATED" ]]; then k delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

flip_snapshot() {
  python3 - "$1" "$2" "${3:-}" <<'PY'
import json, sys
sys.path.insert(0, "code")
import main
source, target, config_out = sys.argv[1:]
data = json.load(open(source))
path = main.Snapshot(data).resolve("kubelet-config")
entry = data["files"].get(path) or {}
if "content" not in entry:
    raise SystemExit(f"no kubelet config content at {path} in {source}")
config = main.parse_document(entry["content"])
config.setdefault("authentication", {}).setdefault("anonymous", {})["enabled"] = True
entry["content"] = json.dumps(config, indent=2)
data["processes"]["kubelet"] = data["processes"]["kubelet"] + ["--read-only-port=10255"]
json.dump(data, open(target, "w"), indent=2)
if config_out:
    open(config_out, "w").write(entry["content"] + "\n")
PY
}
assert_flip() {
  python3 - "$1" "$2" <<'PY'
import json, sys
diff = json.load(open(sys.argv[1]))
changed = {c["id"]: (c["old"], c["new"]) for c in diff["changed"]}
want = {"4.2.1": ("PASS", "FAIL"), "4.2.4": ("PASS", "WARN")}
if changed != want:
    raise SystemExit(f"{sys.argv[2]}: expected {want}, diff says {changed}")
print(f"  {sys.argv[2]}: 4.2.1 PASS -> FAIL, 4.2.4 PASS -> WARN, nothing else changed ({diff['same']} same)")
PY
}
junit_matches() {
  python3 - "$1" "$2" <<'PY'
import json, sys
import xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
totals = json.load(open(sys.argv[2]))["totals"]
got = (len(root.findall(".//testcase")), len(root.findall(".//failure")), len(root.findall(".//skipped")))
want = (sum(totals.values()), totals["FAIL"], totals["WARN"])
if got != want:
    raise SystemExit(f"JUnit testcases/failures/skipped {got} != JSON totals {want}")
print(f"  JUnit: {got[0]} testcases, {got[1]} failures, {got[2]} skipped")
PY
}

echo "▸ minibench on the kind-shaped fixture: 61 PASS, 12 FAIL, 18 WARN and exit 1"
CODE=0
mb run "$F/kind-node.json" --json > "$WORK/fixture.json" || CODE=$?
[[ "$CODE" == "1" ]] || fail "minibench run on the fixture should exit 1 (FAILs present), got $CODE: $(head -c 400 "$WORK/fixture.json")"
python3 - "$WORK/fixture.json" <<'PY'
import json, sys
totals = json.load(open(sys.argv[1]))["totals"]
if totals != {"PASS": 61, "WARN": 18, "FAIL": 12}:
    raise SystemExit(f"fixture totals {totals}")
print(f"  {totals}")
PY
mb run "$F/kind-node.json" --junit --fail-on none > "$WORK/fixture.xml" || fail "minibench --junit failed"
junit_matches "$WORK/fixture.xml" "$WORK/fixture.json"

echo "▸ a flipped kubelet flag and config key in a copy of the fixture flip 4.2.1 and 4.2.4, and diff calls it a regression"
flip_snapshot "$F/kind-node.json" "$WORK/fixture-flipped.json" || fail "could not flip the fixture"
mb run "$WORK/fixture-flipped.json" --json --fail-on none > "$WORK/fixture-flipped-report.json" || fail "run on the flipped fixture"
CODE=0
mb diff "$WORK/fixture.json" "$WORK/fixture-flipped-report.json" --json > "$WORK/fixture-diff.json" || CODE=$?
[[ "$CODE" == "1" ]] || fail "diff should exit 1 on a regression, got $CODE: $(cat "$WORK/fixture-diff.json")"
assert_flip "$WORK/fixture-diff.json" "fixture" || fail "fixture flip: $(cat "$WORK/fixture-diff.json")"

echo "▸ bad input exits 2 with the reason"
echo '{"kind": "Pod"}' > "$WORK/not-a-snapshot.json"
CODE=0
mb run "$WORK/not-a-snapshot.json" > /dev/null 2> "$WORK/bad.err" || CODE=$?
[[ "$CODE" == "2" ]] && grep -q "not a minibench snapshot" "$WORK/bad.err" || fail "a non-snapshot should exit 2: code $CODE, $(cat "$WORK/bad.err")"

if ! command -v docker >/dev/null || ! docker inspect "$NODE" >/dev/null 2>&1; then
  echo "  (docker or the kind node container $NODE not available; node, kube-bench and Job checks skipped)"
  echo "SMOKE OK: security/cis-benchmark (fixture only)"
  exit 0
fi

HAVE_CTX=""
if command -v kubectl >/dev/null; then
  kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
  if grep -qx "$CTX" "$WORK/contexts.txt"; then HAVE_CTX=1; fi
fi

echo "▸ minibench collect --node docker:$NODE (stat, cat of manifests and the kubelet config, /proc/*/cmdline; read-only)"
mb collect --node "docker:$NODE" -o "$WORK/node.json" 2> "$WORK/collect.err" || fail "collect: $(cat "$WORK/collect.err")"
sed 's/^/  /' "$WORK/collect.err"
python3 - "$WORK/node.json" <<'PY'
import json, sys
sys.path.insert(0, "code")
import main
data = json.load(open(sys.argv[1]))
missing = [c for c in ("kube-apiserver", "kube-controller-manager", "kube-scheduler", "etcd", "kubelet") if c not in data["processes"]]
if missing:
    raise SystemExit(f"processes not found on the node: {missing}; found {sorted(data['processes'])}")
for path in main.MANIFESTS.values():
    if "content" not in data["files"].get(path, {}):
        raise SystemExit(f"manifest {path} not read: {data['files'].get(path)}")
leaked = [p for p, e in data["files"].items() if "content" in e and p not in main.MANIFESTS.values() and p != main.Snapshot(data).resolve("kubelet-config")]
if leaked:
    raise SystemExit(f"collect copied contents it must not: {leaked}")
if any(marker in json.dumps(data) for marker in ("client-key-data", "BEGIN RSA PRIVATE KEY", "BEGIN EC PRIVATE KEY", "BEGIN PRIVATE KEY")):
    raise SystemExit("the snapshot contains key material")
tree = data["trees"].get(main.PKI_DIR) or []
if len(tree) < 10:
    raise SystemExit(f"PKI tree has only {len(tree)} entries")
print(f"  kubelet: {' '.join(data['processes']['kubelet'])}")
PY
CODE=0
mb run "$WORK/node.json" --json > "$WORK/mini.json" || CODE=$?
[[ "$CODE" == "0" || "$CODE" == "1" ]] || fail "minibench run on the node exited $CODE: $(head -c 400 "$WORK/mini.json")"
mb run "$WORK/node.json" --fail-on none > "$WORK/mini.txt" || fail "minibench text report"
tail -n 1 "$WORK/mini.txt" | sed 's/^/  /'
python3 - "$WORK/mini.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
if report["problems"]:
    raise SystemExit(f"minibench could not parse something: {report['problems']}")
if report["drift"]:
    raise SystemExit(f"manifests and running processes disagree: {report['drift']}")
if report["skipped"]:
    raise SystemExit(f"a control-plane node should run every target, skipped: {report['skipped'][:5]}")
print(f"  no drift between manifests and processes; {len(report['results'])} checks evaluated")
PY
mb run "$WORK/node.json" --junit --fail-on none > "$WORK/mini.xml" || fail "minibench --junit on the node"
junit_matches "$WORK/mini.xml" "$WORK/mini.json"

echo "▸ kube-bench v$KB_VERSION: download the release and verify it against the project's checksum file"
case "$(uname -m)" in
  x86_64) KB_ARCH=amd64; KB_SHA256=82dbc7e598740dc9344d41f8ad0b8210d57c4c00bdb2c5f1d8a69a2b98baddcf ;;
  aarch64|arm64) KB_ARCH=arm64; KB_SHA256=64500561f5fcaa3f86fe951ed26bbfc28f7bbf3d2eac13843abfd2924955d10b ;;
  *) fail "no kube-bench build pinned for $(uname -m)" ;;
esac
TARBALL="kube-bench_${KB_VERSION}_linux_${KB_ARCH}.tar.gz"
SUMS="kube-bench_${KB_VERSION}_checksums.txt"
BASE="https://github.com/aquasecurity/kube-bench/releases/download/v${KB_VERSION}"
curl -fsSL --retry 3 -o "$WORK/$TARBALL" "$BASE/$TARBALL" || fail "download $BASE/$TARBALL"
curl -fsSL --retry 3 -o "$WORK/$SUMS" "$BASE/$SUMS" || fail "download $BASE/$SUMS"
grep " ${TARBALL}\$" "$WORK/$SUMS" > "$WORK/kb.sha256" || fail "$TARBALL is not listed in $SUMS: $(cat "$WORK/$SUMS")"
(cd "$WORK" && sha256sum -c kb.sha256 > "$WORK/sha.out" 2>&1) || fail "checksum file mismatch: $(cat "$WORK/sha.out")"
echo "$KB_SHA256  $WORK/$TARBALL" | sha256sum -c - > "$WORK/sha-pin.out" 2>&1 || fail "pinned sha256 mismatch: $(cat "$WORK/sha-pin.out")"
mkdir -p "$WORK/kb"
tar -xzf "$WORK/$TARBALL" -C "$WORK/kb"
[[ -f "$WORK/kb/kube-bench" && -f "$WORK/kb/cfg/$KB_BENCHMARK/node.yaml" ]] || fail "unexpected tarball layout: $(ls -R "$WORK/kb" | head -20)"
echo "  $(cat "$WORK/sha.out")"

echo "▸ kube-bench on $NODE as a binary: --benchmark $KB_BENCHMARK --targets master,controlplane,etcd,node --json"
NODE_DIR_CREATED=1
docker exec "$NODE" rm -rf "$KB_DIR"
docker exec "$NODE" mkdir -p "$KB_DIR"
docker cp "$WORK/kb/kube-bench" "$NODE:$KB_DIR/kube-bench" > "$WORK/cp.log" 2>&1 || fail "docker cp kube-bench: $(cat "$WORK/cp.log")"
docker cp "$WORK/kb/cfg" "$NODE:$KB_DIR/cfg" > "$WORK/cp.log" 2>&1 || fail "docker cp cfg: $(cat "$WORK/cp.log")"
docker exec "$NODE" "$KB_DIR/kube-bench" run --targets master,controlplane,etcd,node --benchmark "$KB_BENCHMARK" --config-dir "$KB_DIR/cfg" --json > "$WORK/kb.json" 2> "$WORK/kb.err" \
  || fail "kube-bench in the node failed: $(tail -n 20 "$WORK/kb.err")"
python3 - "$WORK/kb.json" <<'PY'
import sys
sys.path.insert(0, "code")
import main
name, results = main.load_results(sys.argv[1])
counts = {}
for item in results.values():
    counts[item["state"]] = counts.get(item["state"], 0) + 1
if len(results) < 90:
    raise SystemExit(f"kube-bench reported only {len(results)} checks: {counts}")
print(f"  kube-bench: {len(results)} checks, {counts}")
PY

echo "▸ minibench and kube-bench agree on every section both implement"
CODE=0
mb diff "$WORK/mini.json" "$WORK/kb.json" --common --fail-on any > "$WORK/cmp.txt" || CODE=$?
mb diff "$WORK/mini.json" "$WORK/kb.json" --common --fail-on none --json > "$WORK/cmp.json" || fail "diff --json"
head -n 1 "$WORK/cmp.txt" | sed 's/^/  /'
if [[ "$CODE" != "0" ]]; then
  cat "$WORK/cmp.txt" >&2
  fail "minibench and kube-bench disagree on the sections above (old = minibench, new = kube-bench, with each side's evidence)"
fi
python3 - "$WORK/cmp.json" "$WORK/mini.json" <<'PY'
import json, sys
diff = json.load(open(sys.argv[1]))
mine = {r["id"] for r in json.load(open(sys.argv[2]))["results"]}
only_mine = {i["id"] for i in diff["only_old"]}
if only_mine != {"legacy-4.2.6"}:
    raise SystemExit(f"kube-bench should implement every minibench section except legacy-4.2.6; missing: {sorted(only_mine)}")
if diff["same"] != len(mine) - 1:
    raise SystemExit(f"expected {len(mine) - 1} sections in common, got {diff['same']}")
print(f"  {diff['same']} sections agree; kube-bench only: {', '.join(i['id'] for i in diff['only_new'])}")
PY

echo "▸ the kubelet's effective configuration: minibench's values equal configz from the API server's node proxy"
if [[ -n "$HAVE_CTX" ]]; then
  k get --raw "/api/v1/nodes/$NODE/proxy/configz" > "$WORK/configz.json" || fail "configz for $NODE"
  python3 - "$WORK/mini.json" "$WORK/configz.json" <<'PY'
import json, sys
sys.path.insert(0, "code")
import main
results = {r["id"]: r for r in json.load(open(sys.argv[1]))["results"]}
live = json.load(open(sys.argv[2]))["kubeletconfig"]
fields = {"4.2.1": "authentication.anonymous.enabled", "4.2.2": "authorization.mode", "4.2.3": "authentication.x509.clientCAFile",
          "4.2.4": "readOnlyPort", "4.2.5": "streamingConnectionIdleTimeout", "4.2.6": "makeIPTablesUtilChains",
          "4.2.10": "rotateCertificates", "4.2.13": "podPidsLimit", "4.2.14": "seccompDefault", "legacy-4.2.6": "protectKernelDefaults"}
bad = []
for check_id, key in fields.items():
    mine = results[check_id]["value"]
    theirs = main.dig(live, key)
    if mine is None:
        print(f"  {check_id:<13}{key:<36}minibench has no effective value; configz {theirs!r}")
        continue
    if theirs is None:
        theirs = "false" if mine in ("true", "false") else "0" if main.INT_RE.match(mine) else ""
    theirs = main.scalar_text(theirs)
    equal = main.parse_duration(mine) == main.parse_duration(theirs) if key == "streamingConnectionIdleTimeout" else mine.lower() == theirs.lower()
    print(f"  {check_id:<13}{key:<36}minibench {mine:<28}configz {theirs}")
    if not equal:
        bad.append(f"{key}: minibench {mine}, configz {theirs}")
if bad:
    raise SystemExit("effective kubelet values differ: " + "; ".join(bad))
PY
else
  echo "  (context $CTX not found; configz comparison and the Job skipped)"
fi

echo "▸ flip 4.2.1 and 4.2.4 in a copy of the node's snapshot (the node is not touched)"
flip_snapshot "$WORK/node.json" "$WORK/node-flipped.json" "$WORK/kubelet-flipped.json" || fail "could not flip the node snapshot"
mb run "$WORK/node-flipped.json" --json --fail-on none > "$WORK/mini-flipped.json" || fail "run on the flipped snapshot"
CODE=0
mb diff "$WORK/mini.json" "$WORK/mini-flipped.json" --json > "$WORK/node-diff.json" || CODE=$?
[[ "$CODE" == "1" ]] || fail "diff should exit 1 on the flipped snapshot, got $CODE: $(cat "$WORK/node-diff.json")"
assert_flip "$WORK/node-diff.json" "node snapshot" || fail "node flip: $(cat "$WORK/node-diff.json")"

echo "▸ kube-bench on the same flipped kubelet config, at a scratch path its cfg points to: 4.2.1 fails there too"
python3 - "$WORK/kb/cfg" "$WORK/cfg-flipped" "$KB_DIR/kubelet-flipped.json" "$KB_BENCHMARK" <<'PY'
import os, shutil, sys
source, target, flipped, benchmark = sys.argv[1:]
text = open(os.path.join(source, "config.yaml")).read()
anchor = '      - "/etc/kubernetes/kubelet-config.yaml"\n'
if text.count(anchor) != 1:
    raise SystemExit(f"cfg/config.yaml has {text.count(anchor)} copies of the kubelet conf anchor; kube-bench changed its layout")
os.makedirs(target)
open(os.path.join(target, "config.yaml"), "w").write(text.replace(anchor, f'      - "{flipped}"\n' + anchor))
shutil.copytree(os.path.join(source, benchmark), os.path.join(target, benchmark))
PY
docker cp "$WORK/kubelet-flipped.json" "$NODE:$KB_DIR/kubelet-flipped.json" > "$WORK/cp.log" 2>&1 || fail "docker cp flipped config: $(cat "$WORK/cp.log")"
docker cp "$WORK/cfg-flipped" "$NODE:$KB_DIR/cfg-flipped" > "$WORK/cp.log" 2>&1 || fail "docker cp cfg-flipped: $(cat "$WORK/cp.log")"
docker exec "$NODE" "$KB_DIR/kube-bench" run --targets node --benchmark "$KB_BENCHMARK" --config-dir "$KB_DIR/cfg-flipped" --json > "$WORK/kb-flipped.json" 2> "$WORK/kb-flipped.err" \
  || fail "kube-bench on the flipped config: $(tail -n 20 "$WORK/kb-flipped.err")"
python3 - "$WORK/kb.json" "$WORK/kb-flipped.json" "$WORK/mini-flipped.json" <<'PY'
import sys
sys.path.insert(0, "code")
import main
real = main.load_results(sys.argv[1])[1]["4.2.1"]
flipped = main.load_results(sys.argv[2])[1]["4.2.1"]
mine = main.load_results(sys.argv[3])[1]["4.2.1"]
if (real["state"], flipped["state"], mine["state"]) != ("PASS", "FAIL", "FAIL"):
    raise SystemExit(f"4.2.1: kube-bench real {real}, kube-bench flipped {flipped}, minibench flipped {mine}")
print(f"  kube-bench 4.2.1: PASS on the real config, FAIL on the copy ({'; '.join(flipped['evidence'][1:2]) or flipped['evidence']})")
PY

if [[ -n "$HAVE_CTX" ]]; then
  echo "▸ kube-bench as a Job on $CTX ($KB_IMAGE, hostPID, read-only hostPath mounts, pinned to $NODE)"
  k delete namespace "$NS" --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1 || true
  NS_CREATED=1
  k create namespace "$NS" >/dev/null || fail "create namespace $NS"
  python3 - "$WORK/job.json" "$NS" "$NODE" "$KB_IMAGE" "$KB_BENCHMARK" <<'PY'
import json, sys
path, namespace, node, image, benchmark = sys.argv[1:]
mounts = [("var-lib-etcd", "/var/lib/etcd", "/var/lib/etcd"), ("var-lib-kubelet", "/var/lib/kubelet", "/var/lib/kubelet"),
          ("etc-systemd", "/etc/systemd", "/etc/systemd"), ("lib-systemd", "/lib/systemd", "/lib/systemd/"),
          ("etc-kubernetes", "/etc/kubernetes", "/etc/kubernetes"), ("usr-bin", "/usr/bin", "/usr/local/mount-from-host/bin"),
          ("var-lib-cni", "/var/lib/cni", "/var/lib/cni"), ("etc-cni-netd", "/etc/cni/net.d", "/etc/cni/net.d/"), ("opt-cni-bin", "/opt/cni/bin", "/opt/cni/bin/")]
job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "kube-bench", "namespace": namespace, "labels": {"app": "cc-cis-benchmark"}},
       "spec": {"backoffLimit": 0, "ttlSecondsAfterFinished": 600, "template": {"metadata": {"labels": {"app": "cc-cis-benchmark"}}, "spec": {
           "hostPID": True, "restartPolicy": "Never", "nodeSelector": {"kubernetes.io/hostname": node}, "tolerations": [{"operator": "Exists"}],
           "containers": [{"name": "kube-bench", "image": image, "command": ["kube-bench", "run", "--targets", "master,controlplane,etcd,node", "--benchmark", benchmark, "--json"],
                           "volumeMounts": [{"name": n, "mountPath": m, "readOnly": True} for n, _, m in mounts]}],
           "volumes": [{"name": n, "hostPath": {"path": h}} for n, h, _ in mounts]}}}}
json.dump(job, open(path, "w"), indent=2)
PY
  k apply -f "$WORK/job.json" >/dev/null || fail "apply the kube-bench Job: $(cat "$WORK/job.json")"
  DEADLINE=$((SECONDS + 240))
  STATE=""
  while (( SECONDS < DEADLINE )); do
    SUCCEEDED="$(k -n "$NS" get job kube-bench -o jsonpath='{.status.succeeded}' 2>/dev/null || true)"
    FAILED="$(k -n "$NS" get job kube-bench -o jsonpath='{.status.failed}' 2>/dev/null || true)"
    if [[ "$SUCCEEDED" == "1" ]]; then STATE=ok; break; fi
    if [[ -n "$FAILED" && "$FAILED" != "0" ]]; then STATE=failed; break; fi
    sleep 3
  done
  if [[ "$STATE" != "ok" ]]; then
    k -n "$NS" describe job kube-bench > "$WORK/job-describe.txt" 2>&1 || true
    k -n "$NS" get pods -o wide > "$WORK/job-pods.txt" 2>&1 || true
    k -n "$NS" logs job/kube-bench > "$WORK/job-fail.log" 2>&1 || true
    fail "kube-bench Job did not succeed (${STATE:-timeout}): $(cat "$WORK/job-pods.txt"; tail -n 20 "$WORK/job-describe.txt"; tail -n 20 "$WORK/job-fail.log")"
  fi
  k -n "$NS" logs job/kube-bench > "$WORK/kb-job.log" || fail "logs of the kube-bench Job"
  CODE=0
  mb diff "$WORK/mini.json" "$WORK/kb-job.log" --common --fail-on any > "$WORK/cmp-job.txt" || CODE=$?
  head -n 1 "$WORK/cmp-job.txt" | sed 's/^/  /'
  if [[ "$CODE" != "0" ]]; then
    cat "$WORK/cmp-job.txt" >&2
    fail "minibench and the kube-bench Job disagree on the sections above"
  fi
  mb diff "$WORK/kb.json" "$WORK/kb-job.log" --fail-on none > "$WORK/kb-vs-job.txt" || true
  sed -n '1p;/->/p' "$WORK/kb-vs-job.txt" | sed 's/^/  binary vs Job: /'
fi

echo "SMOKE OK: security/cis-benchmark"
