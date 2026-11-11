#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
F="code/fixtures"
CTX="${CC_SMOKE_CONTEXT:-kind-cc-smoke}"
NS="cc-service-catalog"
NS_CREATED=""
CATALOG_MODEL_SHA512="sha512-DnMlbWySe5UpCTXePmWdPy70yVMgUjtYy9ZIN0TLvqsqk02n8GHTwZsqDs/7eQARB5jcssa2GtdiOxPh37mpgw=="
YAML_SHA512="sha512-3NxN8+78OdzbT7C/WjGsyfPAtJaN3FNDsWxv7Y7mcDsT/oOmgW8BpyQQFFBnvZE3j9Y2Sdz1ULFLezL7Eb2yFw=="
cleanup() {
  if [[ -n "$NS_CREATED" ]]; then
    kubectl --context "$CTX" delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

mc() { python3 code/main.py "$@"; }
exit_of() {
  local out="$1"
  shift
  local code=0
  "$@" > "$out" 2> "$out.err" || code=$?
  echo "$code"
}
check() {
  local what="$1"
  shift
  python3 - "$@" > "$WORK/check.txt" 2>&1 || fail "$what: $(cat "$WORK/check.txt")"
  sed 's/^/  /' "$WORK/check.txt"
}

echo "▸ minicatalog validate: the root Location pulls in every repository's catalog-info.yaml and all entities are accepted"
CODE="$(exit_of "$WORK/valid.json" mc validate "$F/repos/catalog/all.yaml" --json)"
[[ "$CODE" == "0" ]] || fail "validate on the valid catalog exit $CODE: $(head -c 3000 "$WORK/valid.json") $(cat "$WORK/valid.json.err")"
check "valid catalog" "$WORK/valid.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
assert doc["summary"] == {"files": 8, "entities": 28, "accepted": 28, "rejected": 0}, doc["summary"]
assert [s["path"] for s in doc["skipped"]] == ["fraud-ml/catalog-info.yaml"], doc["skipped"]
kinds = sorted({e["data"]["kind"] for e in doc["entities"]})
print("%d files from one root Location, %d entities of kinds %s; the optional fraud-ml target is skipped" % (len(doc["files"]), len(doc["entities"]), ", ".join(kinds)))
PY

echo "▸ minicatalog validate: every broken fixture is rejected for exactly the field that was planted"
CODE="$(exit_of "$WORK/broken.json" mc validate "$F/broken" --json)"
[[ "$CODE" == "1" ]] || fail "validate on the broken fixtures should exit 1, got $CODE: $(cat "$WORK/broken.json.err")"
check "broken fixtures" "$WORK/broken.json" "$F/expected/broken-fields.json" <<'PY'
import json, sys
doc, expected = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
found = {"%s#%d" % (e["source"], e["doc"]): [f["field"] for f in e["findings"] if f["severity"] == "error"] for e in doc["entities"]}
assert found == {k: [v] for k, v in expected.items()}, {k: (found.get(k), expected.get(k)) for k in set(found) | set(expected) if found.get(k) != [expected.get(k)]}
print("%d entities rejected, each for one field: %s" % (len(found), ", ".join(sorted(set(expected.values())))))
PY

echo "▸ installing the oracle: @backstage/catalog-model 1.10.2 and yaml 2.9.1 with npm ci (every tarball checked against the lock file's sha512)"
command -v node >/dev/null || fail "node is not installed; the smoke needs Node 22 for @backstage/catalog-model"
command -v npm >/dev/null || fail "npm is not installed"
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
[[ "$NODE_MAJOR" -ge 22 ]] || fail "Node $NODE_MAJOR found; Node 22 or later is required"
mkdir -p "$WORK/oracle"
cp code/oracle/package.json code/oracle/package-lock.json code/oracle/backstage-check.js "$WORK/oracle/"
check "lock file pins" "$WORK/oracle/package-lock.json" "$CATALOG_MODEL_SHA512" "$YAML_SHA512" <<'PY'
import json, sys
packages = json.load(open(sys.argv[1]))["packages"]
model, yaml = packages["node_modules/@backstage/catalog-model"], packages["node_modules/yaml"]
assert (model["version"], model["integrity"]) == ("1.10.2", sys.argv[2]), model
assert (yaml["version"], yaml["integrity"]) == ("2.9.1", sys.argv[3]), yaml
print("lock file: %d packages, @backstage/catalog-model %s and yaml %s pinned by sha512" % (len(packages) - 1, model["version"], yaml["version"]))
PY
(cd "$WORK/oracle" && npm ci --ignore-scripts --no-audit --no-fund --loglevel=error) > "$WORK/npm.log" 2>&1 || fail "npm ci: $(tail -20 "$WORK/npm.log")"

echo "▸ Backstage's own policies and kind validators on the same files: same YAML, same verdicts, same fields"
oracle() {
  local report="$1" out="$2"
  python3 - "$report" > "$WORK/args.txt" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
print(doc["base"])
for path in doc["files"]:
    print(path)
PY
  local args=()
  while IFS= read -r line; do
    args+=("$line")
  done < "$WORK/args.txt"
  node "$WORK/oracle/backstage-check.js" "${args[@]}" > "$out" 2> "$out.err" || fail "backstage-check.js: $(cat "$out.err")"
}
oracle "$WORK/valid.json" "$WORK/bs-valid.json"
oracle "$WORK/broken.json" "$WORK/bs-broken.json"
check "verdicts" "$WORK/valid.json" "$WORK/bs-valid.json" "$WORK/broken.json" "$WORK/bs-broken.json" <<'PY'
import json, sys
pairs = [(json.load(open(sys.argv[i])), json.load(open(sys.argv[i + 1]))) for i in (1, 3)]
print("oracle: " + ", ".join("%s %s" % kv for kv in pairs[0][1]["versions"].items()))
for mine, theirs in pairs:
    backstage = {(d["source"], d["doc"]): d for d in theirs["documents"]}
    ours = {(e["source"], e["doc"]): e for e in mine["entities"]}
    assert set(backstage) == set(ours), (sorted(set(backstage) ^ set(ours)))
    for key, entity in sorted(ours.items()):
        verdict = backstage[key]
        assert verdict.get("data") == entity["data"] or verdict["field"] == "<root>", ("YAML parsed differently", key, verdict.get("data"), entity["data"])
        assert verdict["accepted"] == entity["accepted"], ("verdict differs", key, verdict, entity["findings"])
        if not entity["accepted"]:
            fields = [f["field"] for f in entity["findings"] if f["severity"] == "error"]
            assert fields == [verdict["field"]], ("field differs", key, fields, verdict)
    rejected = [d for d in theirs["documents"] if not d["accepted"]]
    print("%d documents: Backstage accepts %d and rejects %d, minicatalog agrees on every verdict and field" % (len(ours), len(ours) - len(rejected), len(rejected)))
    for d in rejected[:4]:
        print("    %s#%d %s: %s" % (d["source"], d["doc"], d["field"], d["error"][:110]))
PY

echo "▸ relations: minicatalog's two-way relations equal the ones Backstage's compiled catalog model derives from the same entities"
CODE="$(exit_of "$WORK/relations.json" mc relations "$F/repos" --json)"
[[ "$CODE" == "0" ]] || fail "relations exit $CODE: $(cat "$WORK/relations.json.err")"
check "relations" "$WORK/relations.json" "$WORK/bs-valid.json" <<'PY'
import collections, json, sys
mine, theirs = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
ours = {(r["source"], r["type"], r["target"]) for r in mine["relations"]}
backstage = {tuple(r) for r in theirs["relations"]}
assert ours == backstage, {"only minicatalog": sorted(ours - backstage), "only Backstage": sorted(backstage - ours)}
assert [(d["ref"], d["field"]) for d in mine["dropped"]] == [(d["ref"], d["field"]) for d in theirs["dropped"]], (mine["dropped"], theirs["dropped"])
counts = collections.Counter(t for _, t, _ in ours)
print("%d relations identical, by type: %s" % (len(ours), ", ".join("%s %d" % kv for kv in sorted(counts.items()))))
print("both drop %s %s (an owner must be a Group or User)" % (theirs["dropped"][0]["ref"], theirs["dropped"][0]["field"]))
PY

echo "▸ graph queries: blast radius of payments-db, gaps and the production-readiness scorecard match the expected fixtures"
CODE="$(exit_of "$WORK/blast.json" mc blast-radius resource:payments-db "$F/repos" --json)"
[[ "$CODE" == "0" ]] || fail "blast-radius exit $CODE: $(cat "$WORK/blast.json.err")"
CODE="$(exit_of "$WORK/gaps.json" mc gaps "$F/repos" --json)"
[[ "$CODE" == "1" ]] || fail "gaps should exit 1 (three components lack an owner), got $CODE: $(cat "$WORK/gaps.json.err")"
CODE="$(exit_of "$WORK/score.json" mc scorecard "$F/repos" --rules "$F/scorecard.json" --json)"
[[ "$CODE" == "0" ]] || fail "scorecard exit $CODE: $(cat "$WORK/score.json.err")"
CODE="$(exit_of "$WORK/gate.txt" mc scorecard "$F/repos" --rules "$F/scorecard.json" --min-level silver)"
[[ "$CODE" == "1" ]] || fail "--min-level silver should fail on ledger and the unowned components, got $CODE: $(cat "$WORK/gate.txt")"
check "queries" "$WORK/blast.json" "$F/expected/blast-radius-payments-db.json" "$WORK/gaps.json" "$F/expected/gaps.json" "$WORK/score.json" "$F/expected/scorecard.json" <<'PY'
import json, sys
docs = [json.load(open(p)) for p in sys.argv[1:]]
for got, want in zip(docs[::2], docs[1::2]):
    assert got == want, (got, want)
blast, gaps, score = docs[0], docs[2], docs[4]
print("payments-db down: %s; notify %s" % (", ".join("%s (depth %d)" % (i["ref"], i["depth"]) for i in blast["impacted"]), ", ".join(blast["teams"])))
print("gaps: " + "; ".join("%s: %s" % (team, ", ".join(i["ref"] for i in items)) for team, items in gaps["teams"].items()))
print("scorecard: " + ", ".join("%s %s %s%%" % (e["ref"].split("/")[-1], e["level"] or "none", e["score"]) for e in score["entities"]))
PY

echo "▸ lint against last night's export: broken references, the dropped owner and two orphans"
CODE="$(exit_of "$WORK/lint.json" mc lint "$F/repos" --previous "$F/previous.json" --json)"
[[ "$CODE" == "1" ]] || fail "lint should exit 1, got $CODE: $(cat "$WORK/lint.json" "$WORK/lint.json.err")"
check "lint" "$WORK/lint.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
assert [(b["ref"], b["target"]) for b in doc["broken_references"]] == [("component:default/notifications", "group:default/comms-team"), ("component:payments/receipts-worker", "group:payments/payments-team")], doc["broken_references"]
assert [(o["ref"], o["status"]) for o in doc["orphans"]] == [("component:default/legacy-billing", "orphan"), ("component:default/old-reporting", "orphan")], doc["orphans"]
for o in doc["orphans"]:
    print("%s: %s" % (o["ref"], o["message"]))
PY

echo "▸ k8s-owners on a recorded kubectl List: owned, unlabelled, unknown component and ownerless workloads"
CODE="$(exit_of "$WORK/k8s-recorded.json" mc k8s-owners "$F/repos" --objects "$F/k8s/deployments.json" --json)"
[[ "$CODE" == "1" ]] || fail "k8s-owners should exit 1 for unowned workloads, got $CODE: $(cat "$WORK/k8s-recorded.json.err")"
check "k8s-owners (recorded)" "$WORK/k8s-recorded.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
statuses = {w["object"].split("/")[-1]: w["status"] for w in doc["workloads"]}
assert statuses == {"checkout-web": "owned", "payments-api": "owned", "notifications": "no-owner", "search-v1": "no-component", "debug-shell": "unlabelled"}, statuses
print(", ".join("%s %s" % kv for kv in sorted(statuses.items())))
PY

echo "▸ on kind: the same Deployments in namespace $NS, found by the backstage.io/kubernetes-id label the Backstage Kubernetes plugin uses"
if command -v kubectl >/dev/null && kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null && grep -qx "$CTX" "$WORK/contexts.txt"; then
  kubectl --context "$CTX" delete namespace "$NS" --ignore-not-found --wait=true --timeout=90s > /dev/null || fail "could not remove a leftover $NS namespace"
  kubectl --context "$CTX" create namespace "$NS" > "$WORK/ns.txt" 2>&1 || fail "create namespace: $(cat "$WORK/ns.txt")"
  NS_CREATED=1
  kubectl --context "$CTX" -n "$NS" apply -f "$F/k8s/workloads.yaml" > "$WORK/apply.txt" 2>&1 || fail "apply: $(cat "$WORK/apply.txt")"
  kubectl --context "$CTX" -n "$NS" get deployments -o json > "$WORK/live.json" || fail "kubectl get deployments failed"
  kubectl --context "$CTX" -n "$NS" get deployments -l backstage.io/kubernetes-id=payments-api -o jsonpath='{.items[*].metadata.name}' > "$WORK/selected.txt" || fail "label selector query failed"
  [[ "$(cat "$WORK/selected.txt")" == "payments-api" ]] || fail "label selector should select payments-api only, got: $(cat "$WORK/selected.txt")"
  CODE="$(exit_of "$WORK/k8s-live.json" mc k8s-owners "$F/repos" --objects "$WORK/live.json" --json)"
  [[ "$CODE" == "1" ]] || fail "k8s-owners on the live cluster should exit 1, got $CODE: $(cat "$WORK/k8s-live.json.err")"
  check "k8s-owners (live)" "$WORK/k8s-live.json" "$WORK/k8s-recorded.json" <<'PY'
import json, sys
live, recorded = (json.load(open(p)) for p in sys.argv[1:3])
assert live == recorded, (live, recorded)
print("the live cluster gives the same mapping as the recording: %d workloads, %d owned" % (len(live["workloads"]), sum(w["status"] == "owned" for w in live["workloads"])))
PY
else
  echo "  (kubectl context $CTX not available; cluster check skipped)"
fi

echo "SMOKE OK: platform/service-catalog"
