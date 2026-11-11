#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
NS="cc-golden-path"
CTX="${CC_SMOKE_CONTEXT:-kind-cc-smoke}"
IMAGE="cc-golden-path-checkout:smoke"
CONTAINER="cc-golden-path-checkout"
COPIER_VERSION="9.19.1"
LESSON="$(pwd)"
NS_CREATED=""
IMAGE_BUILT=""
export GIT_AUTHOR_NAME="CC Smoke" GIT_AUTHOR_EMAIL="smoke@example.invalid" GIT_COMMITTER_NAME="CC Smoke" GIT_COMMITTER_EMAIL="smoke@example.invalid"
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
cleanup() {
  set +e
  if [[ -n "$IMAGE_BUILT" ]]; then
    docker rm -f "$CONTAINER" >/dev/null 2>&1
    docker rmi -f "$IMAGE" >/dev/null 2>&1
  fi
  if [[ -n "$NS_CREATED" ]]; then kubectl --context "$CTX" delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1; fi
  rm -rf "$WORK"
}
trap cleanup EXIT
sc() { python3 "$LESSON/code/main.py" "$@"; }

cat > "$WORK/same_tree.py" <<'PY'
import difflib
import os
import sys


def tree(root):
    out = {}
    for current, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in names:
            path = os.path.join(current, name)
            with open(path, "rb") as handle:
                out[os.path.relpath(path, root)] = (handle.read(), bool(os.stat(path).st_mode & 0o111))
    return out


left, right = tree(sys.argv[1]), tree(sys.argv[2])
if sorted(left) != sorted(right):
    sys.exit("different files: only scaffolder %s, only copier %s" % (sorted(set(left) - set(right)), sorted(set(right) - set(left))))
problems = []
for path in sorted(left):
    if left[path] != right[path]:
        diff = difflib.unified_diff(left[path][0].decode(errors="replace").splitlines(), right[path][0].decode(errors="replace").splitlines(), "scaffolder/" + path, "copier/" + path, lineterm="")
        problems.append("\n".join(list(diff)[:40]) or "%s: executable bit differs" % path)
if problems:
    sys.exit("\n".join(problems))
print("  %d files, byte for byte identical" % len(left))
PY

echo "▸ scaffolder demo: generate 1.0.0, change the service, release 1.1.0, update"
sc demo --json > "$WORK/demo.json" 2> "$WORK/demo.err" || fail "demo: $(cat "$WORK/demo.err")"
python3 - "$WORK/demo.json" <<'PY' || fail "demo result: $(head -c 3000 "$WORK/demo.json")"
import json, sys
d = json.load(open(sys.argv[1]))
actions = {c["path"]: c["action"] for c in d["update"]["changes"] if c["action"] != "unchanged"}
assert actions == {".github/workflows/ci.yaml": "updated", "Dockerfile": "conflict", "README.md": "merged", "app/main.py": "kept", "catalog-info.yaml": "updated", "k8s/deployment.yaml": "merged"}, actions
assert d["deployment_keeps_flag"] and d["deployment_has_probes"], d
print("  " + ", ".join("%s %s" % (a, p) for p, a in sorted(actions.items())))
PY

echo "▸ the template is a Git repository with two releases, v1.0.0 and v1.1.0"
TPL="$WORK/service-template"
cp -R code/template "$TPL"
git -C "$TPL" -c init.defaultBranch=main init --quiet
git -C "$TPL" add --all
git -C "$TPL" commit --quiet --message "Release 1.0.0"
git -C "$TPL" tag v1.0.0
git -C "$TPL" apply "$LESSON/code/fixtures/template-v1.1.0.patch" || fail "the v1.1.0 patch does not apply to code/template"
git -C "$TPL" add --all
git -C "$TPL" commit --quiet --message "Release 1.1.0"
git -C "$TPL" tag v1.1.0
echo "  tags: $(git -C "$TPL" tag --list | tr '\n' ' ')"

echo "▸ scaffolder questions: typed parameters read from copier.yml"
sc questions --template "$TPL" --ref v1.1.0 --json > "$WORK/questions.json" || fail "questions"
python3 - "$WORK/questions.json" <<'PY' || fail "questions: $(cat "$WORK/questions.json")"
import json, sys
q = {x["name"]: x["type"] for x in json.load(open(sys.argv[1]))}
assert q == {"service_name": "str", "description": "str", "owner": "str", "port": "int", "replicas": "int", "exposure": "str",
             "hostname": "str", "cpu_request": "str", "database": "bool", "environments": "yaml"}, q
print("  " + ", ".join("%s:%s" % kv for kv in q.items()))
PY

echo "▸ invalid answers are all reported, exit 2, nothing written"
CODE=0
sc new "$WORK/bad" --template "$TPL" -d service_name=Bad_Name -d port=80 -d exposure=world --trust > "$WORK/bad.out" 2> "$WORK/bad.err" || CODE=$?
[[ "$CODE" == "2" ]] || fail "invalid answers should exit 2, got $CODE: $(cat "$WORK/bad.err")"
for want in "service_name: 'Bad_Name' must be a DNS label" "owner: required" "port: 80 must be between 1024 and 65535" "exposure: 'world' is not one of internal, public"; do
  grep -qF "$want" "$WORK/bad.err" || fail "missing problem '$want' in: $(cat "$WORK/bad.err")"
done
[[ ! -e "$WORK/bad" ]] || fail "nothing should be written for invalid answers"
echo "  $(grep -c '^  ' "$WORK/bad.err") problems reported"

echo "▸ copier $COPIER_VERSION in a venv, every wheel pinned by sha256"
python3 -m venv "$WORK/venv" || fail "python3 -m venv"
"$WORK/venv/bin/python" -m pip install --quiet --disable-pip-version-check --require-hashes --only-binary=:all: -r code/smoke-requirements.txt > "$WORK/pip.log" 2>&1 || fail "pip install copier: $(tail -20 "$WORK/pip.log")"
COPIER="$WORK/venv/bin/copier"
GOT_VERSION="$("$WORK/venv/bin/python" -c 'import importlib.metadata as m; print(m.version("copier"))')"
[[ "$GOT_VERSION" == "$COPIER_VERSION" ]] || fail "copier version is $GOT_VERSION"
echo "  copier $GOT_VERSION"

render_both() {
  local name="$1" ref="$2"
  shift 2
  local args=()
  for pair in "$@"; do args+=(-d "$pair"); done
  sc new "$WORK/sc-$name" --template "$TPL" --ref "$ref" "${args[@]}" --trust --json > "$WORK/sc-$name.json" 2> "$WORK/sc-$name.err" || fail "scaffolder new $name: $(cat "$WORK/sc-$name.err")"
  "$COPIER" copy --quiet --trust --defaults --vcs-ref "$ref" "${args[@]}" "$TPL" "$WORK/cp-$name" > "$WORK/cp-$name.log" 2>&1 || fail "copier copy $name: $(cat "$WORK/cp-$name.log")"
}

echo "▸ same answers, same tree: defaults (internal, no database)"
render_both plain v1.0.0 service_name=checkout owner=payments
python3 "$WORK/same_tree.py" "$WORK/sc-plain" "$WORK/cp-plain" || fail "scaffolder and copier differ for the default answers"

echo "▸ same answers, same tree: public route, database, three environments, a description that needs quoting"
render_both full v1.0.0 service_name=orders owner=shop exposure=public database=true port=9090 replicas=3 \
  "environments=[dev, staging, production]" 'description=Orders: carts & "checkouts" <fast>'
python3 "$WORK/same_tree.py" "$WORK/sc-full" "$WORK/cp-full" || fail "scaffolder and copier differ for the full answers"
[[ -f "$WORK/sc-full/k8s/httproute.yaml" && ! -e "$WORK/sc-plain/k8s/httproute.yaml" ]] || fail "httproute.yaml should exist only when exposure=public"

echo "▸ the post-generation tasks made one commit in both"
for dir in sc-plain cp-plain sc-full cp-full; do
  grep -qxF '    cloudcaptain.io/template-version: "v1.0.0"' "$WORK/$dir/catalog-info.yaml" || fail "$dir: catalog-info.yaml lacks the template-version annotation: $(cat "$WORK/$dir/catalog-info.yaml")"
  SUBJECT="$(git -C "$WORK/$dir" log --format=%s)"
  [[ "$SUBJECT" == Scaffold\ *\ from\ the\ service\ template ]] || fail "$dir: unexpected history: $SUBJECT"
  [[ -z "$(git -C "$WORK/$dir" status --porcelain)" ]] || fail "$dir: working tree not clean after generation"
done
echo "  $(git -C "$WORK/sc-full" log --format=%s)"

echo "▸ the generated service's own tests pass"
cp -R "$WORK/sc-full" "$WORK/run-tests"
(cd "$WORK/run-tests" && python3 -m unittest discover -s tests -t . > "$WORK/svc-tests.log" 2>&1) || fail "generated tests: $(cat "$WORK/svc-tests.log")"
grep -q "^OK" "$WORK/svc-tests.log" || fail "generated tests: $(cat "$WORK/svc-tests.log")"
echo "  $(grep '^Ran' "$WORK/svc-tests.log")"

own_changes() { python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import main; main.own_changes(sys.argv[2], sys.argv[3] == "yes")' "$LESSON/code" "$1" "$2"; }

for scenario in clean conflict; do
  echo "▸ update 1.0.0 -> 1.1.0 ($scenario): the service's own changes survive and the template changes land"
  render_both "$scenario" v1.0.0 service_name=checkout owner=payments
  for dir in "sc-$scenario" "cp-$scenario"; do
    own_changes "$WORK/$dir" "$([[ "$scenario" == conflict ]] && echo yes || echo no)"
    git -C "$WORK/$dir" commit --quiet --all --message "own changes"
  done
  CODE=0
  (cd "$WORK/sc-$scenario" && sc update --ref v1.1.0 --trust --json > "$WORK/update-$scenario.json" 2> "$WORK/update-$scenario.err") || CODE=$?
  WANT="$([[ "$scenario" == conflict ]] && echo 1 || echo 0)"
  [[ "$CODE" == "$WANT" ]] || fail "scaffolder update ($scenario) should exit $WANT, got $CODE: $(cat "$WORK/update-$scenario.err")"
  CCODE=0
  (cd "$WORK/cp-$scenario" && "$COPIER" update --quiet --trust --defaults --vcs-ref v1.1.0 > "$WORK/copier-update-$scenario.log" 2>&1) || CCODE=$?
  [[ "$CCODE" == "0" ]] || fail "copier update ($scenario) exit $CCODE: $(cat "$WORK/copier-update-$scenario.log")"
  python3 "$WORK/same_tree.py" "$WORK/sc-$scenario" "$WORK/cp-$scenario" || fail "scaffolder and copier updates differ ($scenario)"
  git -C "$WORK/cp-$scenario" status --porcelain > "$WORK/cp-status-$scenario.txt"
  python3 - "$WORK/update-$scenario.json" "$WORK/cp-status-$scenario.txt" "$scenario" "$WORK/sc-$scenario" <<'PY' || fail "update result ($scenario): $(head -c 3000 "$WORK/update-$scenario.json") copier status: $(cat "$WORK/cp-status-$scenario.txt")"
import json, os, sys
result, status_lines, scenario, root = json.load(open(sys.argv[1])), open(sys.argv[2]).read().splitlines(), sys.argv[3], sys.argv[4]
read = lambda p: open(os.path.join(root, p)).read()
assert "FEATURE_FLAGS" in read("k8s/deployment.yaml") and "readinessProbe" in read("k8s/deployment.yaml"), "deployment.yaml"
assert '"/version"' in read("app/main.py") and "## Runbook" in read("README.md") and "docker build -t checkout ." in read("README.md"), "own changes"
assert "actions/setup-python" in read(".github/workflows/ci.yaml"), "ci.yaml"
assert '    cloudcaptain.io/template-version: "v1.1.0"\n' in read("catalog-info.yaml"), read("catalog-info.yaml")
assert "_commit: v1.1.0\n" in read(".copier-answers.yml") and "cpu_request: 100m\n" in read(".copier-answers.yml"), read(".copier-answers.yml")
unmerged = sorted(line[3:] for line in status_lines if line.startswith("UU "))
if scenario == "conflict":
    assert result["conflicts"] == ["Dockerfile"] and unmerged == ["Dockerfile"], (result["conflicts"], unmerged)
    assert read("Dockerfile").startswith("<<<<<<< before updating\nFROM python:3.13-slim-trixie\n=======\nFROM python:3.14-slim\n>>>>>>> after updating\n"), read("Dockerfile")
else:
    assert result["conflicts"] == [] and unmerged == [], (result["conflicts"], unmerged)
    assert read("Dockerfile").startswith("FROM python:3.14-slim\n"), read("Dockerfile")
print("  scaffolder: %s; copier marks unmerged: %s" % (", ".join("%s %s" % (c["action"], c["path"]) for c in result["changes"] if c["action"] != "unchanged"), unmerged or "none"))
PY
done

if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  echo "▸ docker build of the generated Dockerfile (template 1.1.0), run it, read /healthz and the HEALTHCHECK"
  IMAGE_BUILT=1
  docker build --quiet -t "$IMAGE" "$WORK/sc-clean" > "$WORK/build.log" 2>&1 || fail "docker build: $(tail -30 "$WORK/build.log")"
  [[ "$(docker image inspect "$IMAGE" --format '{{.Config.User}}')" == "10001" ]] || fail "the image should run as user 10001"
  docker run -d --name "$CONTAINER" -p 127.0.0.1::8080 "$IMAGE" > /dev/null || fail "docker run"
  HOSTPORT="$(docker port "$CONTAINER" 8080/tcp | awk -F: '/^127\.0\.0\.1:/ {print $2; exit}')"
  [[ -n "$HOSTPORT" ]] || fail "no published port: $(docker port "$CONTAINER" 2>&1)"
  BODY=""
  for _ in $(seq 1 60); do
    BODY="$(curl -fsS "http://127.0.0.1:$HOSTPORT/healthz" 2>/dev/null || true)"
    [[ -n "$BODY" ]] && break
    sleep 1
  done
  python3 -c 'import json, sys; assert json.loads(sys.argv[1]) == {"status": "ok"}, sys.argv[1]' "$BODY" || fail "/healthz answered '$BODY'; logs: $(docker logs "$CONTAINER" 2>&1 | tail -20)"
  HEALTH=""
  for _ in $(seq 1 60); do
    HEALTH="$(docker inspect "$CONTAINER" --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}')"
    [[ "$HEALTH" == "healthy" ]] && break
    sleep 1
  done
  [[ "$HEALTH" == "healthy" ]] || fail "HEALTHCHECK status is '$HEALTH': $(docker inspect "$CONTAINER" --format '{{json .State.Health}}')"
  echo "  127.0.0.1:$HOSTPORT/healthz -> $BODY; HEALTHCHECK $HEALTH; user $(docker image inspect "$IMAGE" --format '{{.Config.User}}')"
  docker rm -f "$CONTAINER" >/dev/null
else
  echo "  (docker not available; image build skipped)"
fi

if ! command -v kubectl >/dev/null; then
  echo "  (kubectl not installed; server-side dry run skipped)"
  echo "SMOKE OK: platform/golden-path (scaffolder, copier and docker; no cluster)"
  exit 0
fi
kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if ! grep -qx "$CTX" "$WORK/contexts.txt"; then
  echo "  (context $CTX not found; the server-side dry run only runs against the throwaway kind cluster)"
  echo "SMOKE OK: platform/golden-path (scaffolder, copier and docker; no cluster)"
  exit 0
fi

echo "▸ kubectl apply --dry-run=server on the generated manifests"
k() { kubectl --context "$CTX" "$@"; }
k delete namespace "$NS" --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1 || true
NS_CREATED=1
k create namespace "$NS" >/dev/null || fail "create namespace $NS"
MANIFESTS=(-f "$WORK/sc-full/k8s/deployment.yaml" -f "$WORK/sc-full/k8s/service.yaml" -f "$WORK/sc-clean/k8s/deployment.yaml")
if k get crd httproutes.gateway.networking.k8s.io >/dev/null 2>&1; then
  MANIFESTS+=(-f "$WORK/sc-full/k8s/httproute.yaml")
  WANT_KINDS="Deployment Deployment HTTPRoute Service"
else
  echo "  (Gateway API CRDs not installed in $CTX; httproute.yaml left out of the dry run)"
  WANT_KINDS="Deployment Deployment Service"
fi
k apply -n "$NS" --dry-run=server -o json "${MANIFESTS[@]}" > "$WORK/dry-run.json" 2> "$WORK/dry-run.err" || fail "server dry run: $(cat "$WORK/dry-run.err")"
python3 - "$WORK/dry-run.json" "$WANT_KINDS" <<'PY' || fail "dry run result: $(head -c 3000 "$WORK/dry-run.json")"
import json, sys
text, index, decoder, items = open(sys.argv[1]).read(), 0, json.JSONDecoder(), []
while index < len(text):
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        break
    value, index = decoder.raw_decode(text, index)
    items += value["items"] if value.get("kind") == "List" else [value]
kinds = sorted(i["kind"] for i in items)
assert kinds == sys.argv[2].split(), kinds
for d in (i for i in items if i["kind"] == "Deployment"):
    c = d["spec"]["template"]["spec"]["containers"][0]
    assert d["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"] is True, d["spec"]["template"]["spec"]
    assert c["securityContext"]["readOnlyRootFilesystem"] is True and d["spec"]["strategy"]["type"] == "RollingUpdate", c
    print("  %s: %d replicas, port %s, probes %s, defaulted by the API server: strategy %s, imagePullPolicy %s" % (
        d["metadata"]["name"], d["spec"]["replicas"], c["ports"][0]["containerPort"], "readinessProbe" in c, d["spec"]["strategy"]["type"], c["imagePullPolicy"]))
PY

echo "SMOKE OK: platform/golden-path"
