#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
NS="cc-model-rollout"
CTX="${CC_SMOKE_CONTEXT:-kind-cc-smoke}"
ROLLOUTS_VERSION="v1.10.2"
# argo-rollouts-checksums.txt covers only the kubectl plugin; these two sha256 were computed from the v1.10.2 release assets and match GitHub's asset digests
ROLLOUTS_INSTALL_SHA256=b8280994791eaee7eb91c8b84450efcc6e0292fca5aafc18c1b0ebc5d18dbefe
ROLLOUTS_NS_INSTALL_SHA256=bb14ecec25afb7880500c58cf51ac8b15f513305d2756e21e63d03be9cb9906c
CRD_LABEL="cc-smoke-lesson=model-rollout"
F="code/fixtures"
V1_WEIGHTS="1.5,-2.0,0.25"
V2_WEIGHTS="1.4,-2.1,0.2"
V3_WEIGHTS="-1.5,-2.0,0.25"
PIDS=()
CLUSTER=""
NS_CREATED=""
CRDS_INSTALLED=""

k() { kubectl --context "$CTX" "$@"; }

cleanup() {
  set +e
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do { kill "$pid"; wait "$pid"; } 2>/dev/null; done
  if [[ -n "$CLUSTER" && -n "$NS_CREATED" ]]; then
    k -n "$NS" delete rollouts.argoproj.io,analysisruns.argoproj.io --all --wait=true --timeout=60s >/dev/null 2>&1
    k delete namespace "$NS" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1
  fi
  if [[ -n "$CLUSTER" && -n "$CRDS_INSTALLED" ]]; then
    k delete crd -l "$CRD_LABEL" --ignore-not-found --wait=true --timeout=60s >/dev/null 2>&1
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

sha256_of() { if command -v sha256sum >/dev/null; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi; }
wait_line() { for _ in $(seq 1 150); do grep -q "$2" "$1" 2>/dev/null && return 0; sleep 0.1; done; fail "no '$2' in $1: $(cat "$1" 2>/dev/null)"; }
url_of() { sed -n 's/.* listening on //p' "$1" | head -1; }
free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'; }
start() { local log="$WORK/$1.log"; shift; python3 code/main.py "$@" > "$log" 2>&1 & PIDS+=("$!"); }

field() {
  python3 - "$@" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2:]:
    value = value[int(key)] if isinstance(value, list) else value[key]
print(json.dumps(value) if isinstance(value, (dict, list)) or value is None else value)
PY
}

echo "▸ minirollout plan: the traffic each analysis gets and how long it needs for 150 samples per side"
python3 code/main.py plan "$F/plan-shadow.json" --rps 60 --baseline-quality 0.97 --json > "$WORK/plan.json" 2>&1 || fail "plan: $(cat "$WORK/plan.json")"
python3 - "$WORK/plan.json" <<'PY' || fail "plan report: $(cat "$WORK/plan.json")"
import json, sys
report = json.load(open(sys.argv[1]))
rows = [(r["mode"], r["candidate_rps"], r["seconds_to_min_samples"]) for r in report["rows"] if "mode" in r]
assert rows == [("shadow", 60.0, 2.5), ("served", 15.0, 10.0), ("served", 30.0, 5.0)], rows
assert report["samples_needed"] == 147 and not report["warnings"], report
print(f"  shadow, 25% and 50% analyses reach 150 samples in {rows[0][2]}s, {rows[1][2]}s and {rows[2][2]}s at 60 req/s; 147 per side detect a 0.10 quality drop")
PY
python3 code/main.py plan "$F/plan-shadow.json" --rps 60 --baseline-quality 0.95 > "$WORK/plan95.txt" 2>&1 || fail "plan: $(cat "$WORK/plan95.txt")"
grep -q "warning: min-samples 150 is below the 180 per side" "$WORK/plan95.txt" || fail "plan should warn that 150 samples cannot detect the drop from 0.95: $(cat "$WORK/plan95.txt")"

echo "▸ four stub model versions: v1 (champion), v2 (retrained, as good), v3 (a feature whose sign flipped), v4 (as good as v2, 120 ms slower)"
start v1 model --version v1 --weights="$V1_WEIGHTS"
start v2 model --version v2 --weights="$V2_WEIGHTS"
start v3 model --version v3 --weights="$V3_WEIGHTS"
start v4 model --version v4 --weights="$V2_WEIGHTS" --latency-ms 120
for v in v1 v2 v3 v4; do wait_line "$WORK/$v.log" "listening on"; done
V1="$(url_of "$WORK/v1.log")"
V2="$(url_of "$WORK/v2.log")"
V3="$(url_of "$WORK/v3.log")"
V4="$(url_of "$WORK/v4.log")"
for pair in "v1 $V1" "v2 $V2" "v3 $V3" "v4 $V4"; do
  set -- $pair
  curl -fsS "$2/healthz" > "$WORK/health.json" || fail "$1 does not answer on $2"
  [[ "$(field "$WORK/health.json" version)" == "$1" ]] || fail "$2 is not model $1: $(cat "$WORK/health.json")"
done

echo "▸ the router with v1 as primary, and a load generator (60 req/s, 400 users) that posts feedback for every answer"
start router router --primary "v1=$V1"
wait_line "$WORK/router.log" "listening on"
ROUTER="$(url_of "$WORK/router.log")"
start load load --url "$ROUTER" --rps 60 --concurrency 8 --users 400 --feedback
wait_line "$WORK/load.log" "sending"
curl -fsS -D "$WORK/headers.txt" -o "$WORK/body.json" -H 'Content-Type: application/json' -d '{"features": [0.5, -0.5]}' "$ROUTER/predict" || fail "the router does not answer"
grep -qi '^x-model-version: v1' "$WORK/headers.txt" && grep -qi '^x-route: primary' "$WORK/headers.txt" || fail "router response headers: $(cat "$WORK/headers.txt")"

echo "▸ shadow: v3 receives a copy of every request, the caller still gets v1's answer byte for byte"
python3 - "$ROUTER" "$V1" "$V3" <<'PY' || fail "shadow traffic changed what callers saw"
import sys, time
sys.path.insert(0, "code")
import main
router, v1, v3 = sys.argv[1:4]
config = lambda **extra: main.http_json("POST", f"{router}/admin/config", dict({"primary": {"name": "v1", "url": v1}}, **extra))
status, body, _ = config(shadow={"name": "v3", "url": v3}, shadow_percent=100)
assert status == 200, body
dropped_before = body["mirror_dropped"]
examples = main.make_dataset(seed=99)[:200]
for i, example in enumerate(examples):
    request = {"id": example.id, "features": list(example.features)}
    s1, routed, headers = main.http_json("POST", f"{router}/predict", request, {"X-User-Id": f"shadow-{i}", "X-Request-Id": f"shadow-check-{i}"})
    s2, direct, _ = main.http_json("POST", f"{v1}/predict", request)
    assert (s1, s2, headers["x-model-version"], headers["x-route"]) == (200, 200, "v1", "primary"), (s1, s2, headers)
    assert routed == direct, (i, routed, direct)
deadline = time.time() + 30
while True:
    shadow = [s for s in main.fetch_samples(router, "v3", "shadow") if s.request_id.startswith("shadow-check-")]
    dropped = main.http_json("GET", f"{router}/admin/config")[1]["mirror_dropped"] - dropped_before
    if len(shadow) + dropped >= 200 or time.time() > deadline:
        break
    time.sleep(0.2)
assert len(shadow) >= 150 and len(shadow) + dropped >= 200, (len(shadow), dropped)
disagree = sum(1 for s in shadow if s.agree is False) / len(shadow)
assert 0.2 < disagree < 0.5, disagree
status, body, _ = config(shadow={"name": "v9", "url": "http://127.0.0.1:9"}, shadow_percent=100)
s, answer, headers = main.http_json("POST", f"{router}/predict", {"features": [1.0, 1.0]})
assert (s, headers["x-model-version"]) == (200, "v1"), (s, answer)
assert config()[0] == 200
print(f"  200 answers identical to v1 called directly; v3 shadowed {len(shadow)} of them ({dropped} dropped) and disagreed on {disagree:.0%}; a dead shadow changes nothing")
PY

echo "▸ sticky canary: v2 at 30%, three requests per user, every user sees one version"
python3 - "$ROUTER" "$V1" "$V2" <<'PY' || fail "canary assignment is not sticky"
import sys, time
sys.path.insert(0, "code")
import main
router, v1, v2 = sys.argv[1:4]
start = time.time() - 1
status, body, _ = main.http_json("POST", f"{router}/admin/config", {"primary": {"name": "v1", "url": v1}, "canary": {"name": "v2", "url": v2}, "weight": 30})
assert status == 200, body
users = [f"sticky-{i}" for i in range(300)]
for round_ in range(3):
    for user in users:
        status, _, _ = main.http_json("POST", f"{router}/predict", {"features": [0.3, 0.1]}, {"X-User-Id": user, "X-Request-Id": f"sticky-{round_}-{user}"})
        assert status == 200
seen = {}
for sample in main.fetch_samples(router, None, "served", start, time.time() + 1):
    if sample.request_id.startswith("sticky-"):
        seen.setdefault(sample.user, set()).add(sample.version)
assert len(seen) == 300 and all(len(v) == 1 for v in seen.values()), {u: v for u, v in seen.items() if len(v) != 1}
canary = {u for u, v in seen.items() if v == {"v2"}}
assert canary == {u for u in users if main.assign(u, "v2", 30) == main.CANARY}
assert 60 <= len(canary) <= 120, len(canary)
assert main.http_json("POST", f"{router}/admin/config", {"primary": {"name": "v1", "url": v1}})[0] == 200
print(f"  {len(canary)} of 300 users on v2, each user on one version for all three requests")
PY

cat > "$WORK/registry.json" <<'EOF'
{"models": {"churn": {"aliases": {"champion": "1"}, "versions": {"1": {"tags": {}}}}}}
EOF
local_run() {
  local name="$1" plan="$2" candidate="$3" code=0
  python3 code/main.py run "$plan" --router "$ROUTER" --candidate "$candidate" --registry "$WORK/registry.json" --model churn \
    --tick 0.5 --analysis-timeout 90 --state "$WORK/state-$name.json" > "$WORK/run-$name.txt" 2>&1 || code=$?
  grep -E "^step |analysis step .*(promote|rollback)|^promoted|^rolled back|^registry|^analysis step .* no decision" "$WORK/run-$name.txt" | sed 's/^/    /' || true
  return "$code"
}

echo "▸ minirollout run (shadow plan): v2 passes the shadow, 25% and 50% analyses, becomes primary, churn@champion moves to 2"
local_run v2-shadow "$F/plan-shadow.json" "v2=$V2" || fail "v2 should be promoted: $(cat "$WORK/run-v2-shadow.txt")"
[[ "$(field "$WORK/registry.json" models churn aliases champion)" == "2" ]] || fail "champion alias: $(cat "$WORK/registry.json")"
curl -fsS "$ROUTER/admin/config" > "$WORK/config.json"
[[ "$(field "$WORK/config.json" primary name)" == "v2" && "$(field "$WORK/config.json" canary)" == "null" ]] || fail "router after promotion: $(cat "$WORK/config.json")"

echo "▸ minirollout run (shadow plan): v3 is rolled back by the shadow analysis before a single user is served by it"
CODE=0
local_run v3-shadow "$F/plan-shadow.json" "v3=$V3" || CODE=$?
[[ "$CODE" == "1" ]] || fail "v3 should be rolled back (exit 1), got $CODE: $(cat "$WORK/run-v3-shadow.txt")"
[[ "$(field "$WORK/state-v3-shadow.json" failed_step)" == "1" && "$(field "$WORK/state-v3-shadow.json" exposure served_requests)" == "0" ]] || fail "v3 shadow state: $(cat "$WORK/state-v3-shadow.json")"
grep -q "disagreement fail" "$WORK/run-v3-shadow.txt" || fail "the shadow rollback should name disagreement: $(cat "$WORK/run-v3-shadow.txt")"
[[ "$(field "$WORK/registry.json" models churn aliases champion)" == "2" && "$(field "$WORK/registry.json" models churn versions 3 tags rollout)" == "rolled-back" ]] || fail "registry after rollback: $(cat "$WORK/registry.json")"

echo "▸ minirollout run (the Argo Rollout's own steps, no shadow): v3 is caught by user feedback at step 2, after real users met it"
CODE=0
local_run v3-canary "$F/rollout.json" "v3=$V3" || CODE=$?
[[ "$CODE" == "1" ]] || fail "v3 should be rolled back (exit 1), got $CODE: $(cat "$WORK/run-v3-canary.txt")"
LOCAL_V3_STEP="$(field "$WORK/state-v3-canary.json" failed_step)"
EXPOSED="$(field "$WORK/state-v3-canary.json" exposure served_requests)"
[[ "$LOCAL_V3_STEP" == "1" && "$EXPOSED" -gt 0 ]] || fail "v3 canary state: $(cat "$WORK/state-v3-canary.json")"
grep -q "quality fail" "$WORK/run-v3-canary.txt" || fail "the canary rollback should name quality: $(cat "$WORK/run-v3-canary.txt")"
echo "  v3 served $EXPOSED requests to $(field "$WORK/state-v3-canary.json" exposure users) users before the rollback; with the shadow plan it served 0"

echo "▸ minirollout run: v4 answers correctly but 120 ms slower; the Mann-Whitney test rolls it back"
CODE=0
local_run v4-canary "$F/rollout.json" "v4=$V4" || CODE=$?
[[ "$CODE" == "1" ]] && grep -q "latency fail" "$WORK/run-v4-canary.txt" || fail "v4 should be rolled back on latency (exit $CODE): $(cat "$WORK/run-v4-canary.txt")"
curl -fsS "$ROUTER/admin/config" > "$WORK/config.json"
[[ "$(field "$WORK/config.json" primary name)" == "v2" ]] || fail "v2 should still be primary: $(cat "$WORK/config.json")"

echo "▸ oracle: scipy (hash-pinned) recomputes Mann-Whitney U and the z-tests on the samples the router recorded"
if python3 -c 'import platform, sys; sys.exit(0 if sys.version_info[:2] == (3, 12) and platform.libc_ver()[0] == "glibc" else 1)'; then
  python3 -m venv "$WORK/oracle"
  "$WORK/oracle/bin/pip" install --quiet --disable-pip-version-check --retries 3 --require-hashes --only-binary=:all: -r code/oracle-requirements.txt > "$WORK/pip.txt" 2>&1 || fail "pip install scipy: $(tail -20 "$WORK/pip.txt")"
  curl -fsS "$ROUTER/samples?role=served" > "$WORK/samples.json" || fail "GET /samples"
  "$WORK/oracle/bin/python" - "$WORK/samples.json" <<'PY' || fail "scipy and minirollout disagree"
import json, math, sys
sys.path.insert(0, "code")
import main
import numpy as np
from scipy import stats
samples = [main.Sample.from_json(s) for s in json.load(open(sys.argv[1]))["samples"]]
by = lambda version: [s for s in samples if s.version == version]
latency = lambda rows: [s.latency_ms for s in rows if s.ok]
v2, v3, v4 = by("v2"), by("v3"), by("v4")
half = len(v2) // 2
for name, cand, prim in (("v4 vs v2", latency(v4), latency(v2)), ("v2 vs v2", latency(v2[:half]), latency(v2[half:]))):
    ours = main.mann_whitney_greater(cand, prim)
    theirs = stats.mannwhitneyu(cand, prim, alternative="greater", method="asymptotic", use_continuity=True)
    assert math.isclose(ours.statistic, float(theirs.statistic), rel_tol=1e-12), (name, ours.statistic, theirs.statistic)
    assert math.isclose(ours.p_value, float(theirs.pvalue), rel_tol=1e-6, abs_tol=1e-300), (name, ours.p_value, theirs.pvalue)
    print(f"  Mann-Whitney {name}: U={ours.statistic:.1f} p={ours.p_value:.3g} (scipy {float(theirs.pvalue):.3g})")
votes = lambda rows: [s.feedback for s in rows if s.feedback is not None]
c, p = votes(v3), votes(v2)
ours = main.two_proportion_greater(c.count(False), len(c), p.count(False), len(p))
chi2, pvalue, _, _ = stats.chi2_contingency(np.array([[c.count(False), c.count(True)], [p.count(False), p.count(True)]]), correction=False)
assert math.isclose(ours.z ** 2, chi2, rel_tol=1e-9), (ours.z ** 2, chi2)
assert math.isclose(2 * main.norm_sf(abs(ours.z)), pvalue, rel_tol=1e-6, abs_tol=1e-300), (ours.p_value, pvalue)
print(f"  quality v3 vs v2: z={ours.z:.2f}, z^2={ours.z ** 2:.2f}, scipy chi2={chi2:.2f}")
PY
else
  echo "  (scipy's pinned wheels need CPython 3.12 on glibc; oracle skipped, the unit tests compare with values scipy computed)"
fi

if ! command -v kubectl >/dev/null; then
  echo "  (kubectl not installed; the kind and Argo Rollouts steps are skipped)"
  echo "SMOKE OK: mlops/model-rollout (local only)"
  exit 0
fi
kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if ! grep -qx "$CTX" "$WORK/contexts.txt"; then
  echo "  (context $CTX not found; the kind and Argo Rollouts steps are skipped)"
  echo "SMOKE OK: mlops/model-rollout (local only)"
  exit 0
fi
k cluster-info > "$WORK/cluster-info.txt" 2>&1 || fail "context $CTX is not reachable: $(cat "$WORK/cluster-info.txt")"
CLUSTER=1
for pid in ${PIDS[@]+"${PIDS[@]}"}; do { kill "$pid"; wait "$pid"; } 2>/dev/null || true; done
PIDS=()

echo "▸ Argo Rollouts $ROLLOUTS_VERSION: CRDs from install.yaml, the namespaced controller from namespace-install.yaml, both checked against pinned sha256"
BASE="https://github.com/argoproj/argo-rollouts/releases/download/$ROLLOUTS_VERSION"
curl -fsSL --retry 3 -o "$WORK/install.yaml" "$BASE/install.yaml" || fail "download $BASE/install.yaml"
curl -fsSL --retry 3 -o "$WORK/namespace-install.yaml" "$BASE/namespace-install.yaml" || fail "download $BASE/namespace-install.yaml"
GOT="$(sha256_of "$WORK/install.yaml")"
[[ "$GOT" == "$ROLLOUTS_INSTALL_SHA256" ]] || fail "install.yaml checksum mismatch: want $ROLLOUTS_INSTALL_SHA256 got $GOT"
GOT="$(sha256_of "$WORK/namespace-install.yaml")"
[[ "$GOT" == "$ROLLOUTS_NS_INSTALL_SHA256" ]] || fail "namespace-install.yaml checksum mismatch: want $ROLLOUTS_NS_INSTALL_SHA256 got $GOT"
python3 - "$WORK" <<'PY' || fail "could not split the Argo Rollouts manifests"
import re, sys
work = sys.argv[1]


def docs(path):
    return [d for d in re.split(r"(?m)^---[ \t]*$", open(path, encoding="utf-8").read()) if re.search(r"(?m)^kind:", d)]


crds = [d for d in docs(f"{work}/install.yaml") if re.search(r"(?m)^kind: CustomResourceDefinition[ \t]*$", d)]
names = sorted(re.search(r"(?m)^  name: (\S+)", d).group(1) for d in crds)
assert names == ["analysisruns.argoproj.io", "analysistemplates.argoproj.io", "clusteranalysistemplates.argoproj.io", "experiments.argoproj.io", "rollouts.argoproj.io"], names
controller = [d for d in docs(f"{work}/namespace-install.yaml") if not re.search(r"(?m)^kind: Cluster", d)]
kinds = sorted(re.search(r"(?m)^kind: (\S+)", d).group(1) for d in controller)
assert "Deployment" in kinds and "Role" in kinds and "RoleBinding" in kinds, kinds
with open(f"{work}/crds.yaml", "w", encoding="utf-8") as handle:
    handle.write("\n---\n".join(crds))
with open(f"{work}/controller.yaml", "w", encoding="utf-8") as handle:
    handle.write("\n---\n".join(controller))
with open(f"{work}/crd-names.txt", "w", encoding="utf-8") as handle:
    handle.write("\n".join(names) + "\n")
print(f"  {len(crds)} CRDs; controller objects: {', '.join(kinds)} (the aggregate ClusterRoles are left out)")
PY
k delete namespace "$NS" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1 || true
NS_CREATED=1
k create namespace "$NS" >/dev/null || fail "create namespace $NS"
if k get crd rollouts.argoproj.io >/dev/null 2>&1; then
  echo "  (Argo Rollouts CRDs already installed; using and keeping them)"
else
  CRDS_INSTALLED=1
  k apply --server-side -f "$WORK/crds.yaml" > "$WORK/crds.txt" 2>&1 || fail "apply CRDs: $(tail -20 "$WORK/crds.txt")"
  mapfile -t CRD_NAMES < "$WORK/crd-names.txt"
  k label crd "${CRD_NAMES[@]}" "$CRD_LABEL" --overwrite >/dev/null || fail "label CRDs"
  for crd in "${CRD_NAMES[@]}"; do
    for _ in $(seq 1 60); do
      [[ "$(k get crd "$crd" -o jsonpath='{.status.conditions[?(@.type=="Established")].status}' 2>/dev/null)" == "True" ]] && break
      sleep 1
    done
    [[ "$(k get crd "$crd" -o jsonpath='{.status.conditions[?(@.type=="Established")].status}')" == "True" ]] || fail "CRD $crd not established"
  done
fi
k -n "$NS" apply -f "$WORK/controller.yaml" > "$WORK/controller.txt" 2>&1 || fail "apply the controller: $(cat "$WORK/controller.txt")"
k -n "$NS" rollout status deployment/argo-rollouts --timeout=180s >/dev/null || fail "the Argo Rollouts controller is not ready: $(k -n "$NS" get pods -o wide 2>&1)"

echo "▸ namespace $NS: model code in a ConfigMap, the model Service, the AnalysisTemplate (web provider on the load generator's /compare)"
k -n "$NS" create configmap minirollout-code --from-file=main.py=code/main.py >/dev/null || fail "create the code ConfigMap"
python3 - "$F" "$WORK" "$NS" "$V1_WEIGHTS" "$V2_WEIGHTS" "$V3_WEIGHTS" <<'PY'
import json, sys
fixtures, work, ns, w1, w2, w3 = sys.argv[1:7]
rollout = json.load(open(f"{fixtures}/rollout.json"))
for version, weights, stable in (("v1", w1, "v0"), ("v2", w2, "v1"), ("v3", w3, "v2")):
    doc = json.loads(json.dumps(rollout))
    env = {"MODEL_VERSION": version, "MODEL_WEIGHTS": weights}
    for item in doc["spec"]["template"]["spec"]["containers"][0]["env"]:
        item["value"] = env[item["name"]]
    for step in doc["spec"]["strategy"]["canary"]["steps"]:
        for arg in step.get("analysis", {}).get("args", []):
            arg["value"] = {"stable": stable, "canary": version}.get(arg["name"], arg["value"])
    json.dump(doc, open(f"{work}/rollout-{version}.json", "w"), indent=2)
template = json.load(open(f"{fixtures}/analysis-template.json"))
for metric in template["spec"]["metrics"]:
    metric["provider"]["web"]["url"] = metric["provider"]["web"]["url"].replace(".cc-model-rollout.svc", f".{ns}.svc")
json.dump(template, open(f"{work}/analysis-template.json", "w"), indent=2)
base = {"apiVersion": "v1", "kind": "List", "items": [
    {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "model"}, "spec": {"selector": {"app": "model"}, "ports": [{"port": 8080, "targetPort": 8080}]}},
    {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "loadgen"}, "spec": {"selector": {"app": "loadgen"}, "ports": [{"port": 9090, "targetPort": 9090}]}}]}
json.dump(base, open(f"{work}/base.json", "w"), indent=2)
loadgen = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "loadgen"}, "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "loadgen"}},
    "template": {"metadata": {"labels": {"app": "loadgen"}}, "spec": {"containers": [{"name": "load", "image": "python:3.12-alpine",
        "command": ["python3", "/app/main.py", "load", "--url", f"http://model.{ns}.svc.cluster.local:8080", "--rps", "50", "--concurrency", "8",
                    "--users", "400", "--record", "--listen", "0.0.0.0:9090"],
        "ports": [{"containerPort": 9090}], "readinessProbe": {"httpGet": {"path": "/healthz", "port": 9090}, "periodSeconds": 2},
        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}}, "volumeMounts": [{"name": "code", "mountPath": "/app"}]}],
        "volumes": [{"name": "code", "configMap": {"name": "minirollout-code"}}]}}}}
json.dump(loadgen, open(f"{work}/loadgen.json", "w"), indent=2)
PY
k -n "$NS" apply -f "$WORK/base.json" > "$WORK/base.txt" 2>&1 || fail "apply Services: $(cat "$WORK/base.txt")"
k -n "$NS" apply -f "$WORK/analysis-template.json" > "$WORK/template.txt" 2>&1 || fail "apply AnalysisTemplate: $(cat "$WORK/template.txt")"

rollout_until() {
  python3 - "$CTX" "$NS" "$1" "$2" "$3" <<'PY'
import json, subprocess, sys, time
ctx, ns, want, previous, seconds = sys.argv[1:6]
deadline = time.time() + float(seconds)


def kubectl(*args):
    done = subprocess.run(["kubectl", "--context", ctx, "-n", ns, *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f"kubectl {' '.join(args)}: {done.stderr.strip()}")
    return json.loads(done.stdout)


while True:
    status = kubectl("get", "rollout", "model", "-o", "json").get("status") or {}
    phase, stable, current = status.get("phase"), status.get("stableRS"), status.get("currentPodHash")
    summary = f"phase={phase} step={status.get('currentStepIndex')} stableRS={stable} currentPodHash={current} abort={status.get('abort', False)} message={status.get('message', '')!r}"
    if want == "promoted" and phase == "Healthy" and stable and stable == current and stable != previous:
        print(stable)
        break
    if want == "aborted" and status.get("abort"):
        print(stable)
        break
    if want == "promoted" and status.get("abort"):
        raise SystemExit(f"the rollout aborted: {summary}")
    if any(c.get("reason") == "InconclusiveAnalysis" for c in status.get("pauseConditions") or []):
        raise SystemExit(f"an analysis was Inconclusive (too few samples?): {summary}")
    if time.time() > deadline:
        raise SystemExit(f"timed out waiting for {want}: {summary}")
    time.sleep(2)
PY
}

echo "▸ Rollout v1: the first revision skips the steps; then the in-cluster load generator starts (50 req/s, records samples, serves /compare)"
k -n "$NS" apply -f "$WORK/rollout-v1.json" > "$WORK/apply-v1.txt" 2>&1 || fail "apply Rollout v1: $(cat "$WORK/apply-v1.txt")"
STABLE_V1="$(rollout_until promoted none 240 2> "$WORK/until.err")" || fail "Rollout v1 never became Healthy: $(cat "$WORK/until.err"); $(k -n "$NS" get rollout,rs,pods -o wide 2>&1)"
k -n "$NS" apply -f "$WORK/loadgen.json" > "$WORK/loadgen.txt" 2>&1 || fail "apply loadgen: $(cat "$WORK/loadgen.txt")"
k -n "$NS" rollout status deployment/loadgen --timeout=180s >/dev/null || fail "loadgen not ready: $(k -n "$NS" get pods -o wide 2>&1)"
PF_PORT="$(free_port)"
k -n "$NS" port-forward svc/loadgen "$PF_PORT:9090" --address 127.0.0.1 > "$WORK/port-forward.log" 2>&1 &
PIDS+=("$!")
wait_line "$WORK/port-forward.log" "Forwarding from"
LOADGEN="http://127.0.0.1:$PF_PORT"
for _ in $(seq 1 30); do
  curl -fsS "$LOADGEN/stats?version=v1&window=10" > "$WORK/stats.json" 2>/dev/null && [[ "$(field "$WORK/stats.json" requests)" -ge 100 ]] && break
  sleep 1
done
[[ "$(field "$WORK/stats.json" requests)" -ge 100 ]] || fail "the load generator should see v1 answering: $(cat "$WORK/stats.json" 2>/dev/null)"
echo "  v1 ($STABLE_V1) answers $(field "$WORK/stats.json" requests) requests in 10s, positive feedback $(field "$WORK/stats.json" feedback_rate)"

echo "▸ Rollout v2: 25%, analysis, 50%, analysis; Argo Rollouts promotes it"
k -n "$NS" apply -f "$WORK/rollout-v2.json" > "$WORK/apply-v2.txt" 2>&1 || fail "apply Rollout v2: $(cat "$WORK/apply-v2.txt")"
STABLE_V2="$(rollout_until promoted "$STABLE_V1" 300 2> "$WORK/until.err")" || fail "Argo Rollouts did not promote v2: $(cat "$WORK/until.err"); $(k -n "$NS" get analysisruns -o wide 2>&1)"

echo "▸ Rollout v3: the sign-flipped model; Argo Rollouts aborts it"
k -n "$NS" apply -f "$WORK/rollout-v3.json" > "$WORK/apply-v3.txt" 2>&1 || fail "apply Rollout v3: $(cat "$WORK/apply-v3.txt")"
rollout_until aborted "$STABLE_V2" 240 > "$WORK/abort.txt" 2> "$WORK/until.err" || fail "Argo Rollouts did not abort v3: $(cat "$WORK/until.err"); $(k -n "$NS" get analysisruns -o wide 2>&1)"
k -n "$NS" wait --for=jsonpath='{.status.phase}'=Degraded rollout/model --timeout=60s >/dev/null || fail "an aborted Rollout should be Degraded: $(k -n "$NS" get rollout model -o jsonpath='{.status.phase} {.status.message}' 2>&1)"
k -n "$NS" get rollout model -o json > "$WORK/rollout-live.json"
[[ "$(field "$WORK/rollout-live.json" status stableRS)" == "$STABLE_V2" ]] || fail "v2 should stay stable: $(field "$WORK/rollout-live.json" status)"

echo "▸ Argo's AnalysisRuns and minirollout's verdicts on the same windows of the same samples"
k -n "$NS" get analysisruns -o json > "$WORK/analysisruns.json"
python3 code/main.py replay --source "$LOADGEN" --json > "$WORK/replay.json" 2> "$WORK/replay.err" || fail "minirollout replay: $(cat "$WORK/replay.err")"
python3 - "$WORK/analysisruns.json" "$WORK/replay.json" "$LOCAL_V3_STEP" <<'PY' || fail "Argo Rollouts and minirollout disagree: $(cat "$WORK/analysisruns.json" | head -c 4000)"
import json, sys
runs = json.load(open(sys.argv[1]))["items"]
rows = json.load(open(sys.argv[2]))
local_step = sys.argv[3]
argo = {}
for run in runs:
    args = {a["name"]: a.get("value") for a in run["spec"].get("args", [])}
    step = run["metadata"].get("labels", {}).get("step-index")
    argo[(args.get("canary"), step)] = run
    status = run.get("status") or {}
    print(f"  AnalysisRun {run['metadata']['name']}: canary {args.get('canary')} step-index {step}: {status.get('phase')} {status.get('message', '')}")
assert {k: (r.get("status") or {}).get("phase") for k, r in argo.items() if k[0] == "v2"} == {("v2", "1"): "Successful", ("v2", "3"): "Successful"}, sorted(argo)
v3 = {k: r for k, r in argo.items() if k[0] == "v3"}
assert list(v3) == [("v3", local_step)], (list(v3), local_step)
failed = v3[("v3", local_step)]["status"]
assert failed["phase"] == "Failed" and 'Metric "quality" assessed Failed due to failed (1) > failureLimit (0)' in failed.get("message", ""), failed
quality = next(m for m in failed["metricResults"] if m["name"] == "quality")
measured = json.loads(quality["measurements"][0]["value"])
mini = {}
for row in rows:
    mini.setdefault((row["candidate"], row["step"]), set()).add(row["decision"])
for key in sorted(mini):
    print(f"  minirollout replay, candidate {key[0]} step-index {key[1]}: {sorted(mini[key])}")
assert mini.get(("v2", "1")) == {"promote"} and mini.get(("v2", "3")) == {"promote"}, mini
assert mini.get(("v3", local_step)) == {"rollback"} and ("v3", "3") not in mini, mini
print(f"  Argo measured v3 feedback {measured['candidate']['feedback_rate']:.3f} vs v2 {measured['primary']['feedback_rate']:.3f} on {measured['candidate']['feedback']} answers")
print(f"  both promoted v2 and rolled back v3 at step-index {local_step}, the step where minirollout's own run stopped it locally")
PY
k -n "$NS" get rollout model -o jsonpath='{.status.phase}{"\n"}{.status.message}{"\n"}' > "$WORK/final.txt"
sed 's/^/  /' "$WORK/final.txt"
grep -q "RolloutAborted" "$WORK/final.txt" || fail "Rollout message: $(cat "$WORK/final.txt")"

echo "SMOKE OK: mlops/model-rollout"
