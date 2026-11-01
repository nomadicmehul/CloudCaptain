#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
DW="$PWD/code/main.py"
FIX="$PWD/code/tests/fixtures"
WORK="$(mktemp -d)"
CTX=kind-cc-smoke
NS=cc-drift-detection
NS_CREATED=0
cleanup() {
  if [[ "$NS_CREATED" == "1" ]]; then
    kubectl --context "$CTX" delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

dw() {
  local rc=0
  python3 "$DW" "$@" > "$WORK/dw.out" 2> "$WORK/dw.err" || rc=$?
  echo "$rc"
}
tofu_refresh_rc() {
  local rc=0
  tofu plan -refresh-only -detailed-exitcode -input=false -no-color > "$WORK/tofu.out" 2>&1 || rc=$?
  echo "$rc"
}
tofu_plan_rc() {
  local rc=0
  tofu plan -detailed-exitcode -input=false -no-color > "$WORK/tofu.out" 2>&1 || rc=$?
  echo "$rc"
}
dw_report() { echo "rc=$1 stdout: $(cat "$WORK/dw.out") stderr: $(cat "$WORK/dw.err")"; }

echo "▸ driftwatch classifies the fixture plans (no tools needed)"
RC=$(dw report --refresh-plan "$FIX/refresh_only.json" --plan "$FIX/plan.json" --config "$FIX/main.tf.json" --stack fixture --json)
[[ "$RC" == "2" ]] || fail "fixture drift should exit 2: $(dw_report "$RC")"
python3 - "$WORK/dw.out" <<'PY' || fail "fixture classification: $(cat "$WORK/dw.out")"
import json, sys
text = open(sys.argv[1]).read()
d = json.loads(text)
got = {f["address"]: f["category"] for f in d["findings"]}
assert got == {
    "google_compute_instance.worker": "replace-on-apply",
    "local_file.motd": "deleted-outside",
    "kubernetes_config_map_v1.flags": "changed-outside",
    "kubernetes_secret_v1.db": "changed-outside",
    "kubernetes_config_map_v1.routing": "ignored",
}, got
assert [p["address"] for p in d["pending"]] == ["local_file.banner"], d["pending"]
assert "hunter2" not in text and "rotated-by-hand" not in text, "sensitive value leaked"
print("  5 findings in 4 categories, 1 unapplied change, no sensitive value in the report")
PY

if ! command -v tofu >/dev/null; then
  echo "  (tofu not installed; Use It skipped)"
  echo "SMOKE OK: iac/drift-detection (fixtures only)"
  exit 0
fi

echo "▸ a local_file stack: apply, then a clean refresh-only plan in tofu and in driftwatch"
STACK="$WORK/motd"
mkdir -p "$STACK" && cd "$STACK"
cat > main.tf.json <<'EOF'
{
  "terraform": {"required_providers": {"local": {"source": "hashicorp/local", "version": ">= 2.4"}}},
  "resource": {
    "local_file": {
      "motd": {"filename": "${path.module}/out/motd.txt", "content": "hello from tofu\n"}
    }
  }
}
EOF
tofu init -input=false -no-color > "$WORK/init.txt" 2>&1 || fail "tofu init: $(tail -20 "$WORK/init.txt")"
tofu apply -auto-approve -input=false -no-color > "$WORK/apply.txt" 2>&1 || fail "tofu apply: $(tail -20 "$WORK/apply.txt")"
[[ "$(cat out/motd.txt)" == "hello from tofu" ]] || fail "apply should write out/motd.txt, got: $(cat out/motd.txt)"
RC=$(tofu_refresh_rc)
[[ "$RC" == "0" ]] || fail "a fresh stack should have no drift (tofu exit $RC): $(tail -20 "$WORK/tofu.out")"
RC=$(dw run --chdir "$STACK" --stack motd --json)
[[ "$RC" == "0" ]] || fail "driftwatch on a clean stack: $(dw_report "$RC")"
python3 - "$WORK/dw.out" <<'PY' || fail "clean report: $(cat "$WORK/dw.out")"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["findings"] == [] and d["pending"] == [], d
assert d["tofu_exit"] == 0 and d["fingerprint"] == "clean", d
print("  tofu 0, driftwatch 0, fingerprint clean")
PY

echo "▸ edit the file by hand: both exit 2, and local_file reports an edit as a deletion"
printf 'edited by hand\n' > out/motd.txt
RC=$(tofu_refresh_rc)
[[ "$RC" == "2" ]] || fail "tofu plan -refresh-only -detailed-exitcode should exit 2 after an edit, got $RC: $(tail -20 "$WORK/tofu.out")"
grep -q "local_file.motd has been deleted" "$WORK/tofu.out" || fail "tofu should report local_file.motd as deleted: $(tail -30 "$WORK/tofu.out")"
RC=0
GITHUB_STEP_SUMMARY="$WORK/summary.md" python3 "$DW" run --chdir "$STACK" --stack motd --json --markdown "$WORK/edit.md" --step-summary > "$WORK/dw.out" 2> "$WORK/dw.err" || RC=$?
[[ "$RC" == "2" ]] || fail "driftwatch after an edit: $(dw_report "$RC")"
python3 - "$WORK/dw.out" "$WORK/fp-edit" <<'PY' || fail "edit report: $(cat "$WORK/dw.out")"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["tofu_exit"] == 2, d["tofu_exit"]
f = d["findings"]
assert [(x["address"], x["category"], x["next_apply"]) for x in f] == [("local_file.motd", "deleted-outside", "create")], f
open(sys.argv[2], "w").write(d["key"] + " " + d["fingerprint"])
print("  local_file.motd: deleted-outside, next apply create; key", d["key"], "fingerprint", d["fingerprint"])
PY
grep -q "deleted outside" "$WORK/edit.md" || fail "Markdown report: $(cat "$WORK/edit.md")"
grep -q "<!-- driftwatch-" "$WORK/edit.md" || fail "Markdown report has no dedupe marker: $(cat "$WORK/edit.md")"
cmp -s "$WORK/edit.md" "$WORK/summary.md" || fail "--step-summary should append the same report"

echo "▸ apply restores the file; delete it by hand: deleted-outside again, same key and fingerprint"
tofu apply -auto-approve -input=false -no-color > "$WORK/apply.txt" 2>&1 || fail "tofu apply: $(tail -20 "$WORK/apply.txt")"
[[ "$(cat out/motd.txt)" == "hello from tofu" ]] || fail "apply should restore the content, got: $(cat out/motd.txt)"
rm out/motd.txt
RC=$(tofu_refresh_rc)
[[ "$RC" == "2" ]] || fail "tofu should see the deletion, exit $RC: $(tail -20 "$WORK/tofu.out")"
RC=$(dw run --chdir "$STACK" --stack motd --json)
[[ "$RC" == "2" ]] || fail "driftwatch after a deletion: $(dw_report "$RC")"
python3 - "$WORK/dw.out" "$WORK/fp-edit" <<'PY' || fail "delete report: $(cat "$WORK/dw.out")"
import json, sys
d = json.load(open(sys.argv[1]))
assert [(x["address"], x["category"]) for x in d["findings"]] == [("local_file.motd", "deleted-outside")], d["findings"]
assert open(sys.argv[2]).read() == d["key"] + " " + d["fingerprint"], "same drift should keep the same issue key and fingerprint"
print("  same key and fingerprint as the edit: the nightly job would update one issue")
PY

echo "▸ tofu apply -refresh-only accepts the deletion: no drift left, the missing file is now an unapplied change"
tofu apply -refresh-only -auto-approve -input=false -no-color > "$WORK/apply.txt" 2>&1 || fail "tofu apply -refresh-only: $(tail -20 "$WORK/apply.txt")"
tofu state list > "$WORK/state.txt" 2>&1 || fail "tofu state list: $(cat "$WORK/state.txt")"
! grep -q "local_file.motd" "$WORK/state.txt" || fail "apply -refresh-only should drop the deleted file from state: $(cat "$WORK/state.txt")"
RC=$(tofu_refresh_rc)
[[ "$RC" == "0" ]] || fail "after accepting the drift a refresh-only plan should exit 0, got $RC: $(tail -20 "$WORK/tofu.out")"
RC=$(dw run --chdir "$STACK" --stack motd --json)
[[ "$RC" == "0" ]] || fail "driftwatch after apply -refresh-only: $(dw_report "$RC")"
python3 - "$WORK/dw.out" <<'PY' || fail "pending report: $(cat "$WORK/dw.out")"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["findings"] == [], d["findings"]
assert [(p["address"], p["action"]) for p in d["pending"]] == [("local_file.motd", "create")], d["pending"]
print("  no drift; local_file.motd is an unapplied create")
PY
RC=$(dw run --chdir "$STACK" --stack motd --fail-on-pending)
[[ "$RC" == "2" ]] || fail "--fail-on-pending should exit 2: $(dw_report "$RC")"
RC=$(tofu_plan_rc)
[[ "$RC" == "2" ]] || fail "a normal plan should still want to create the file (exit 2), got $RC: $(tail -20 "$WORK/tofu.out")"

if ! command -v kubectl >/dev/null || ! kubectl --context "$CTX" get --raw /readyz >/dev/null 2>&1; then
  echo "  (no cluster at context $CTX; the kubernetes provider part is skipped)"
  echo "SMOKE OK: iac/drift-detection"
  exit 0
fi

echo "▸ two ConfigMaps owned by tofu on kind, one with ignore_changes on its labels"
NS_CREATED=1
kubectl --context "$CTX" create namespace "$NS" > "$WORK/ns.txt" 2>&1 || fail "create namespace: $(cat "$WORK/ns.txt")"
kubectl config view --raw > "$WORK/kubeconfig" || fail "kubectl config view failed"
K8S="$WORK/flags"
mkdir -p "$K8S" && cd "$K8S"
cat > main.tf.json <<EOF
{
  "terraform": {"required_providers": {"kubernetes": {"source": "hashicorp/kubernetes", "version": ">= 2.30, < 4.0"}}},
  "provider": {"kubernetes": {"config_path": "$WORK/kubeconfig", "config_context": "$CTX"}},
  "resource": {
    "kubernetes_config_map_v1": {
      "flags": {
        "metadata": {"name": "flags", "namespace": "$NS"},
        "data": {"color": "blue"}
      },
      "routing": {
        "metadata": {"name": "routing", "namespace": "$NS", "labels": {"app": "shop"}},
        "data": {"upstream": "v1"},
        "lifecycle": {"ignore_changes": ["metadata[0].labels"]}
      }
    }
  }
}
EOF
tofu init -input=false -no-color > "$WORK/init.txt" 2>&1 || fail "tofu init (kubernetes provider): $(tail -20 "$WORK/init.txt")"
tofu apply -auto-approve -input=false -no-color > "$WORK/apply.txt" 2>&1 || fail "tofu apply: $(tail -20 "$WORK/apply.txt")"
RC=$(dw run --chdir "$K8S" --stack flags --json)
[[ "$RC" == "0" ]] || fail "driftwatch right after apply: $(dw_report "$RC")"

echo "▸ kubectl patch and kubectl label behind tofu's back: changed-outside with the diff, and ignored"
kubectl --context "$CTX" -n "$NS" patch configmap flags --type merge -p '{"data":{"color":"red"}}' > "$WORK/k.txt" 2>&1 || fail "kubectl patch: $(cat "$WORK/k.txt")"
kubectl --context "$CTX" -n "$NS" label configmap routing owner=oncall > "$WORK/k.txt" 2>&1 || fail "kubectl label: $(cat "$WORK/k.txt")"
RC=$(tofu_refresh_rc)
[[ "$RC" == "2" ]] || fail "tofu should see the patch, exit $RC: $(tail -30 "$WORK/tofu.out")"
grep -q "kubernetes_config_map_v1.flags has changed" "$WORK/tofu.out" || fail "tofu should report flags as changed: $(tail -40 "$WORK/tofu.out")"
RC=$(dw run --chdir "$K8S" --stack flags --json)
[[ "$RC" == "2" ]] || fail "driftwatch after kubectl patch: $(dw_report "$RC")"
python3 - "$WORK/dw.out" <<'PY' || fail "kubernetes drift report: $(cat "$WORK/dw.out")"
import json, sys
d = json.load(open(sys.argv[1]))
f = {x["address"]: x for x in d["findings"]}
flags = f["kubernetes_config_map_v1.flags"]
assert flags["category"] == "changed-outside" and flags["next_apply"] == "update", flags
diffs = {x["path"]: (x["before"], x["after"]) for x in flags["diffs"]}
assert diffs.get("data.color") == ("blue", "red"), diffs
routing = f["kubernetes_config_map_v1.routing"]
assert routing["category"] == "ignored" and routing["next_apply"] == "no-op", routing
assert routing["ignored_by"] == ["metadata[0].labels"], routing["ignored_by"]
assert any(x["path"] == "metadata[0].labels.owner" for x in routing["diffs"]), routing["diffs"]
print("  flags: data.color blue -> red, next apply update; routing: ignored by metadata[0].labels")
PY

echo "▸ tofu apply reverts flags and keeps the label ignore_changes protects"
tofu apply -auto-approve -input=false -no-color > "$WORK/apply.txt" 2>&1 || fail "tofu apply: $(tail -20 "$WORK/apply.txt")"
COLOR=$(kubectl --context "$CTX" -n "$NS" get configmap flags -o jsonpath='{.data.color}')
[[ "$COLOR" == "blue" ]] || fail "apply should revert data.color to blue, got $COLOR"
OWNER=$(kubectl --context "$CTX" -n "$NS" get configmap routing -o jsonpath='{.metadata.labels.owner}')
[[ "$OWNER" == "oncall" ]] || fail "ignore_changes should keep the hand-added label, got '$OWNER'"

echo "▸ only ignored drift left: tofu exits 2, driftwatch exits 0, --strict exits 2"
kubectl --context "$CTX" -n "$NS" label configmap routing tier=gold > "$WORK/k.txt" 2>&1 || fail "kubectl label: $(cat "$WORK/k.txt")"
RC=$(tofu_refresh_rc)
[[ "$RC" == "2" ]] || fail "tofu counts every difference in a refresh-only plan, expected 2, got $RC: $(tail -20 "$WORK/tofu.out")"
RC=$(dw run --chdir "$K8S" --stack flags --json)
[[ "$RC" == "0" ]] || fail "ignored drift alone should exit 0: $(dw_report "$RC")"
python3 - "$WORK/dw.out" <<'PY' || fail "ignored-only report: $(cat "$WORK/dw.out")"
import json, sys
d = json.load(open(sys.argv[1]))
assert [(x["address"], x["category"]) for x in d["findings"]] == [("kubernetes_config_map_v1.routing", "ignored")], d["findings"]
assert d["tofu_exit"] == 2, d["tofu_exit"]
print("  tofu said 2, driftwatch 0: the only drift is one ignore_changes accepts")
PY
RC=$(dw run --chdir "$K8S" --stack flags --strict)
[[ "$RC" == "2" ]] || fail "--strict should count ignored drift: $(dw_report "$RC")"

tofu destroy -auto-approve -input=false -no-color > "$WORK/destroy.txt" 2>&1 || fail "tofu destroy: $(tail -20 "$WORK/destroy.txt")"
echo "SMOKE OK: iac/drift-detection"
