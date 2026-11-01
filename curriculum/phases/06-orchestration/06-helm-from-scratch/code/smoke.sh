#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux (downloads the linux helm build and talks to the kind cluster)"
command -v kubectl >/dev/null || fail "needs kubectl and the kind cluster"
NS="cc-helm-from-scratch"
CHART="code/fixtures/webapp"
OVERRIDE="code/fixtures/override.yaml"
WORK="$(mktemp -d)"
MH=(python3 code/main.py)
CTX=()
HCTX=()
MCTX=()
NS_CREATED=""
export HELM_CACHE_HOME="$WORK/helm/cache" HELM_CONFIG_HOME="$WORK/helm/config" HELM_DATA_HOME="$WORK/helm/data"
k() { kubectl ${CTX[@]+"${CTX[@]}"} "$@"; }
h() { "$WORK/bin/helm" ${HCTX[@]+"${HCTX[@]}"} "$@"; }
mhk() { "${MH[@]}" "$@" --store secrets --applier kubectl -n "$NS" ${MCTX[@]+"${MCTX[@]}"}; }
cleanup() {
  if [[ -n "$NS_CREATED" ]]; then
    h uninstall cc-web -n "$NS" >/dev/null 2>&1 || true
    k delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

cat > "$WORK/same_docs.py" <<'PY'
import json, sys
sys.path.insert(0, "code")
import main
mini_text, helm_text = open(sys.argv[1]).read(), open(sys.argv[2]).read()
mini, helm = main.parse_documents(mini_text), main.parse_documents(helm_text)
label = sys.argv[3] or "(chart defaults)"
if mini != helm:
    names = lambda docs: [(d.get("kind"), (d.get("metadata") or {}).get("name")) for d in docs]
    print("minihelm:", names(mini), "\nhelm:    ", names(helm))
    for a, b in zip(mini, helm):
        if a != b:
            print("first difference\nminihelm:", json.dumps(a, sort_keys=True), "\nhelm:    ", json.dumps(b, sort_keys=True))
            break
    raise SystemExit(f"{label}: rendered documents differ")
print(f"  {label}: {len(mini)} documents equal{', byte-identical output' if mini_text == helm_text else ''}")
PY

echo "▸ download the pinned helm and verify its sha256"
HELM_VERSION="${HELM_VERSION:-v4.3.1}"
case "$(uname -m)" in
  x86_64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) fail "no helm build for $(uname -m)" ;;
esac
TARBALL="helm-${HELM_VERSION}-linux-${ARCH}.tar.gz"
curl -fsSL --retry 3 -o "$WORK/$TARBALL" "https://get.helm.sh/$TARBALL" || fail "download $TARBALL"
curl -fsSL --retry 3 -o "$WORK/$TARBALL.sha256sum" "https://get.helm.sh/$TARBALL.sha256sum" || fail "download $TARBALL.sha256sum"
(cd "$WORK" && sha256sum -c "$TARBALL.sha256sum" > "$WORK/sha.txt" 2>&1) || fail "checksum mismatch: $(cat "$WORK/sha.txt")"
mkdir -p "$WORK/bin"
tar -xzf "$WORK/$TARBALL" -C "$WORK"
mv "$WORK/linux-${ARCH}/helm" "$WORK/bin/helm"
"$WORK/bin/helm" version --short > "$WORK/helm-version.txt"
[[ "$(cat "$WORK/helm-version.txt")" == "$HELM_VERSION"* ]] || fail "expected helm $HELM_VERSION, got $(cat "$WORK/helm-version.txt")"
echo "  $(cat "$WORK/helm-version.txt"), $(cat "$WORK/sha.txt")"

echo "▸ minihelm template and helm template render the same documents for the lesson chart"
CASES=(
  ""
  "-f $OVERRIDE"
  "-f $OVERRIDE --set replicaCount=3,tier=backend --set-string image.tag=3.9"
  "--set service.enabled=false,env.REGION=us-east-1,ports[0].containerPort=9000,ports[0].name=web"
)
for case in "${CASES[@]}"; do
  read -ra ARGS <<<"$case"
  "${MH[@]}" template cc-web "$CHART" -n "$NS" ${ARGS[@]+"${ARGS[@]}"} > "$WORK/mini.yaml" 2> "$WORK/mini.err" || fail "minihelm template $case: $(cat "$WORK/mini.err")"
  h template cc-web "$CHART" -n "$NS" ${ARGS[@]+"${ARGS[@]}"} > "$WORK/helm.yaml" 2> "$WORK/helm.err" || fail "helm template $case: $(cat "$WORK/helm.err")"
  python3 "$WORK/same_docs.py" "$WORK/mini.yaml" "$WORK/helm.yaml" "$case" || fail "template mismatch for: $case"
done

echo "▸ the chart helm create writes renders the same in both"
h create "$WORK/starter" > /dev/null
for case in "" "--set ingress.enabled=true" "--set autoscaling.enabled=true,serviceAccount.create=false" "--set httpRoute.enabled=true,fullnameOverride=edge"; do
  read -ra ARGS <<<"$case"
  "${MH[@]}" template cc-starter "$WORK/starter" -n "$NS" ${ARGS[@]+"${ARGS[@]}"} > "$WORK/mini.yaml" 2> "$WORK/mini.err" || fail "minihelm cannot render the helm create chart ($case): $(cat "$WORK/mini.err")"
  h template cc-starter "$WORK/starter" -n "$NS" ${ARGS[@]+"${ARGS[@]}"} > "$WORK/helm.yaml" 2> "$WORK/helm.err" || fail "helm template starter $case: $(cat "$WORK/helm.err")"
  python3 "$WORK/same_docs.py" "$WORK/mini.yaml" "$WORK/helm.yaml" "helm create ${case:-(defaults)}" || fail "helm create chart mismatch for: $case"
done

echo "▸ a required value set to null fails both with the chart's message"
set +e
"${MH[@]}" template cc-web "$CHART" --set team=null > /dev/null 2> "$WORK/mini.err"; MINI_CODE=$?
h template cc-web "$CHART" --set team=null > /dev/null 2> "$WORK/helm.err"; HELM_CODE=$?
set -e
[[ "$MINI_CODE" != "0" && "$HELM_CODE" != "0" ]] || fail "both should fail (minihelm $MINI_CODE, helm $HELM_CODE)"
grep -q "team is required: pass --set team=NAME" "$WORK/mini.err" || fail "minihelm message: $(cat "$WORK/mini.err")"
grep -q "team is required: pass --set team=NAME" "$WORK/helm.err" || fail "helm message: $(cat "$WORK/helm.err")"
echo "  minihelm: $(head -1 "$WORK/mini.err")"
echo "  helm:     $(head -1 "$WORK/helm.err")"

echo "▸ cluster: a namespace of our own"
kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if grep -qx kind-cc-smoke "$WORK/contexts.txt"; then
  CTX=(--context kind-cc-smoke)
  HCTX=(--kube-context kind-cc-smoke)
  MCTX=(--kube-context kind-cc-smoke)
fi
k get nodes -o name > "$WORK/nodes.txt" 2>&1 || fail "cluster not reachable: $(cat "$WORK/nodes.txt")"
k delete namespace "$NS" --ignore-not-found --wait=true --timeout=120s >/dev/null
k create namespace "$NS" >/dev/null
NS_CREATED=1

echo "▸ helm install --dry-run=server renders against the API server and stores nothing"
h install cc-dry "$CHART" -n "$NS" --dry-run=server > "$WORK/dry.txt" 2>&1 || fail "dry run: $(cat "$WORK/dry.txt")"
grep -q "kind: Deployment" "$WORK/dry.txt" || fail "dry run should print the manifest: $(head -20 "$WORK/dry.txt")"
k get secrets -n "$NS" -l owner=helm,name=cc-dry -o name > "$WORK/dry-secrets.txt"
[[ ! -s "$WORK/dry-secrets.txt" ]] || fail "a dry run must not write a release record: $(cat "$WORK/dry-secrets.txt")"

echo "▸ helm: upgrade --install with -f, upgrade with only --set, rollback to 1"
cm_message() { k get configmap "$1-webapp-config" -n "$NS" -o jsonpath='{.data.MESSAGE}'; }
replicas() { k get deployment "$1-webapp" -n "$NS" -o jsonpath='{.spec.replicas}'; }
h upgrade --install cc-web "$CHART" -n "$NS" -f "$OVERRIDE" > "$WORK/h1.txt" 2>&1 || fail "helm install: $(cat "$WORK/h1.txt")"
[[ "$(cm_message cc-web)" == "installed with -f" ]] || fail "revision 1 should carry the -f message, got $(cm_message cc-web)"
h upgrade cc-web "$CHART" -n "$NS" --set replicaCount=2 > "$WORK/h2.txt" 2>&1 || fail "helm upgrade: $(cat "$WORK/h2.txt")"
[[ "$(cm_message cc-web)" == "hello from minihelm" ]] || fail "an upgrade with only --set drops the earlier -f values, got $(cm_message cc-web)"
[[ "$(replicas cc-web)" == "2" ]] || fail "replicas after upgrade: $(replicas cc-web)"
h rollback cc-web 1 -n "$NS" > "$WORK/h3.txt" 2>&1 || fail "helm rollback: $(cat "$WORK/h3.txt")"
[[ "$(cm_message cc-web)" == "installed with -f" && "$(replicas cc-web)" == "1" ]] || fail "rollback should restore revision 1 (message $(cm_message cc-web), replicas $(replicas cc-web))"
k rollout status deployment/cc-web-webapp -n "$NS" --timeout=120s > /dev/null || fail "cc-web did not roll out: $(k get pods -n "$NS" -o wide)"
h history cc-web -n "$NS" -o json > "$WORK/helm-history.json"
python3 - "$WORK/helm-history.json" <<'PY' || fail "helm history: $(cat "$WORK/helm-history.json")"
import json, sys
rows = json.load(open(sys.argv[1]))
got = [(r["revision"], r["status"], r["description"]) for r in rows]
assert got == [(1, "superseded", "Install complete"), (2, "superseded", "Upgrade complete"), (3, "deployed", "Rollback to 1")], got
print("  helm history:", got)
PY

echo "▸ decode helm's newest release Secret by hand and with minihelm decode"
k get secret sh.helm.release.v1.cc-web.v3 -n "$NS" -o json > "$WORK/helm-secret.json"
k get secret sh.helm.release.v1.cc-web.v3 -n "$NS" -o jsonpath='{.data.release}' > "$WORK/release.b64"
base64 -d "$WORK/release.b64" | base64 -d | gunzip > "$WORK/helm-v3.json"
"${MH[@]}" decode "$WORK/helm-secret.json" > "$WORK/helm-v3-minihelm.json" || fail "minihelm decode of helm's Secret"
python3 - "$WORK/helm-secret.json" "$WORK/helm-v3.json" "$WORK/helm-v3-minihelm.json" <<'PY' || fail "decoding helm's Secret"
import json, sys
secret, by_hand, by_minihelm = (json.load(open(path)) for path in sys.argv[1:4])
assert secret["type"] == "helm.sh/release.v1", secret["type"]
labels = secret["metadata"]["labels"]
assert {k: labels[k] for k in ("name", "owner", "status", "version")} == {"name": "cc-web", "owner": "helm", "status": "deployed", "version": "3"}, labels
assert by_hand == by_minihelm, "base64 -d | base64 -d | gunzip and minihelm decode disagree"
print("  type helm.sh/release.v1, labels", sorted(labels), "; the record has", sorted(by_hand))
PY

echo "▸ minihelm: the same three steps against the cluster, records in real Secrets"
mhk upgrade --install cc-mini "$CHART" -f "$OVERRIDE" > "$WORK/m1.txt" 2>&1 || fail "minihelm install: $(cat "$WORK/m1.txt")"
[[ "$(cm_message cc-mini)" == "installed with -f" ]] || fail "minihelm revision 1 message: $(cm_message cc-mini)"
mhk upgrade cc-mini "$CHART" --set replicaCount=2 > "$WORK/m2.txt" 2>&1 || fail "minihelm upgrade: $(cat "$WORK/m2.txt")"
[[ "$(cm_message cc-mini)" == "hello from minihelm" && "$(replicas cc-mini)" == "2" ]] || fail "minihelm upgrade should match helm (message $(cm_message cc-mini), replicas $(replicas cc-mini))"
mhk rollback cc-mini 1 > "$WORK/m3.txt" 2>&1 || fail "minihelm rollback: $(cat "$WORK/m3.txt")"
[[ "$(cm_message cc-mini)" == "installed with -f" && "$(replicas cc-mini)" == "1" ]] || fail "minihelm rollback should restore revision 1"
mhk history cc-mini --json > "$WORK/mini-history.json" || fail "minihelm history"
k get secret sh.helm.release.v1.cc-mini.v3 -n "$NS" -o json > "$WORK/mini-secret.json" || fail "minihelm did not write sh.helm.release.v1.cc-mini.v3"
"${MH[@]}" decode "$WORK/mini-secret.json" > "$WORK/mini-v3.json"
"${MH[@]}" template cc-web "$CHART" -n "$NS" -f "$OVERRIDE" > "$WORK/expected-web.yaml"
"${MH[@]}" template cc-mini "$CHART" -n "$NS" -f "$OVERRIDE" > "$WORK/expected-mini.yaml"
python3 - "$WORK/helm-v3.json" "$WORK/mini-v3.json" "$WORK/expected-web.yaml" "$WORK/expected-mini.yaml" "$OVERRIDE" "$NS" <<'PY' || fail "helm's record and minihelm's record disagree"
import base64, json, sys
sys.path.insert(0, "code")
import main
helm, mini = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
expected = {"cc-web": main.parse_documents(open(sys.argv[3]).read()), "cc-mini": main.parse_documents(open(sys.argv[4]).read())}
override = main.jsonable(main.load_values_file(sys.argv[5]))
for who, record, name in (("helm", helm, "cc-web"), ("minihelm", mini, "cc-mini")):
    info = record["info"]
    assert (record["name"], record["version"], record["namespace"]) == (name, 3, sys.argv[6]), (who, record["name"], record["version"], record["namespace"])
    assert (info["status"], info["description"], info.get("rollback_revision")) == ("deployed", "Rollback to 1", 1), (who, info)
    assert record.get("config") == override, (who, record.get("config"))
    assert main.parse_documents(record["manifest"]) == expected[name], (who, "manifest documents differ")
    sources = [line for line in record["manifest"].splitlines() if line.startswith("# Source:")]
    assert sources == ["# Source: webapp/templates/configmap.yaml", "# Source: webapp/templates/service.yaml", "# Source: webapp/templates/deployment.yaml"], (who, sources)
assert helm["chart"]["metadata"] == mini["chart"]["metadata"], (helm["chart"]["metadata"], mini["chart"]["metadata"])
assert helm["chart"]["values"] == mini["chart"]["values"], "chart default values differ"
files = lambda record: {t["name"]: base64.b64decode(t["data"]).decode() for t in record["chart"]["templates"]}
assert files(helm) == files(mini), (sorted(files(helm)), sorted(files(mini)))
print("  both: version 3, deployed, 'Rollback to 1', same config, chart metadata", helm["chart"]["metadata"]["name"] + "-" + helm["chart"]["metadata"]["version"], ", same values, same", len(files(helm)), "template files, same manifest documents")
PY

echo "▸ helm reads the release minihelm wrote"
h history cc-mini -n "$NS" -o json > "$WORK/helm-reads-mini.json" 2>&1 || fail "helm history could not read minihelm's records: $(cat "$WORK/helm-reads-mini.json")"
h get values cc-mini -n "$NS" -o json > "$WORK/helm-values-mini.json" 2>&1 || fail "helm get values cc-mini: $(cat "$WORK/helm-values-mini.json")"
h get manifest cc-mini -n "$NS" > "$WORK/helm-manifest-mini.yaml" 2>&1 || fail "helm get manifest cc-mini: $(cat "$WORK/helm-manifest-mini.yaml")"
python3 - "$WORK/helm-reads-mini.json" "$WORK/mini-history.json" "$WORK/helm-values-mini.json" "$WORK/helm-manifest-mini.yaml" "$WORK/expected-mini.yaml" "$OVERRIDE" <<'PY' || fail "helm's view of minihelm's release"
import json, sys
sys.path.insert(0, "code")
import main
by_helm, by_minihelm = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
pick = lambda rows: [(r["revision"], r["status"], r["chart"], r["app_version"], r["description"]) for r in rows]
assert pick(by_helm) == pick(by_minihelm), (pick(by_helm), pick(by_minihelm))
assert json.load(open(sys.argv[3])) == main.jsonable(main.load_values_file(sys.argv[6])), "helm get values"
assert main.parse_documents(open(sys.argv[4]).read()) == main.parse_documents(open(sys.argv[5]).read()), "helm get manifest"
print("  helm history cc-mini:", pick(by_helm))
PY

echo "▸ uninstall both: objects and release records are gone"
h uninstall cc-web -n "$NS" > "$WORK/hu.txt" 2>&1 || fail "helm uninstall: $(cat "$WORK/hu.txt")"
mhk uninstall cc-mini > "$WORK/mu.txt" 2>&1 || fail "minihelm uninstall: $(cat "$WORK/mu.txt")"
for _ in $(seq 1 60); do
  k get deployments,configmaps,services -n "$NS" -l app.kubernetes.io/name=webapp -o name > "$WORK/left.txt"
  [[ -s "$WORK/left.txt" ]] || break
  sleep 1
done
[[ ! -s "$WORK/left.txt" ]] || fail "objects left after uninstall: $(cat "$WORK/left.txt")"
k get secrets -n "$NS" -l owner=helm -o name > "$WORK/records.txt"
[[ ! -s "$WORK/records.txt" ]] || fail "release records left after uninstall: $(cat "$WORK/records.txt")"
echo "  $(cat "$WORK/hu.txt"); $(cat "$WORK/mu.txt")"

echo "SMOKE OK: orchestration/helm-from-scratch"
