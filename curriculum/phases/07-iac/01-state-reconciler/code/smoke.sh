#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
MINITF="$PWD/code/main.py"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "▸ minitf end to end (Step 5 of Build It)"
cd "$WORK"
cat > main.json <<'EOF'
{"resources": [
  {"type": "local_dir",  "name": "site",  "path": "site"},
  {"type": "local_file", "name": "index", "path": "site/index.html", "content": "hello"}
]}
EOF
# out_has PATTERN MESSAGE CMD...: run CMD into a file, then grep the file. grep -q on a live pipe can
# exit before the writer finishes, and under pipefail the writer's SIGPIPE (exit 141) fails a correct run.
out_has() {
  local pattern="$1" message="$2"; shift 2
  "$@" > "$WORK/out.txt" 2>&1 || fail "$message: $* exited non-zero: $(tail -30 "$WORK/out.txt")"
  grep -q -- "$pattern" "$WORK/out.txt" || fail "$message: $(tail -30 "$WORK/out.txt")"
}
out_has "Plan: 2 to add, 0 to change, 0 to destroy." "initial plan" python3 "$MINITF" plan
python3 "$MINITF" apply >/dev/null
[[ "$(cat site/index.html)" == "hello" ]] || fail "apply wrote the file"
out_has "No changes" "second plan is a no-op" python3 "$MINITF" plan
sed -i.bak 's/hello/hello v2/' main.json
out_has "~ local_file.index" "content change plans an update" python3 "$MINITF" plan
python3 "$MINITF" apply >/dev/null
rm site/index.html
out_has "drift" "manual deletion is drift" python3 "$MINITF" plan
python3 "$MINITF" apply >/dev/null
sed -i.bak 's#site/index.html#site/home.html#' main.json
out_has "-/+ local_file.index" "path change forces replacement" python3 "$MINITF" plan
python3 "$MINITF" apply >/dev/null
[[ -f site/home.html && ! -e site/index.html ]] || fail "replace moved the file"
python3 "$MINITF" destroy >/dev/null
[[ ! -e site ]] || fail "destroy removed everything"
echo "  minitf: plan/apply/drift/replace/destroy OK"

TF=""
if command -v tofu >/dev/null; then TF=tofu; elif command -v terraform >/dev/null; then TF=terraform; fi
if [[ -z "$TF" ]]; then
  echo "  (neither tofu nor terraform installed; Use It skipped)"
  echo "SMOKE OK: iac/state-reconciler (minitf only)"
  exit 0
fi

# grep -q on a live pipe can exit before the writer finishes; under pipefail the writer's SIGPIPE
# (exit 141) then fails a correct plan. Write the plan to a file and grep that instead.
tf_plan_has() {
  $TF plan -input=false -no-color > "$WORK/plan.txt" 2>&1 || fail "$TF plan exited non-zero: $(tail -30 "$WORK/plan.txt")"
  grep -q "$1" "$WORK/plan.txt" || fail "$2: $(tail -30 "$WORK/plan.txt")"
}
echo "▸ Use It with $TF and the local provider"
mkdir -p "$WORK/tf" && cd "$WORK/tf"
cat > main.tf <<'EOF'
resource "local_file" "index" {
  filename = "${path.module}/site/index.html"
  content  = "hello"
}
EOF
$TF init -input=false -no-color > "$WORK/init.txt" 2>&1 || fail "$TF init: $(tail -30 "$WORK/init.txt")"
tf_plan_has "1 to add, 0 to change, 0 to destroy" "$TF initial plan"
$TF apply -input=false -auto-approve -no-color >/dev/null
[[ "$(cat site/index.html)" == "hello" ]] || fail "$TF apply wrote the file"
tf_plan_has "No changes" "$TF second plan is a no-op"
sed -i.bak 's/"hello"/"hello v2"/' main.tf
tf_plan_has "1 to add, 0 to change, 1 to destroy" "$TF content change should plan as a replacement (every local_file attribute forces replacement)"
rm site/index.html
tf_plan_has "1 to add" "$TF detects drift after manual deletion"
out_has "local_file.index" "$TF state list" $TF state list
python3 - <<'PY' || fail "state file shape"
import json
s = json.load(open("terraform.tfstate"))
assert s["serial"] >= 1 and any(r["type"] == "local_file" for r in s["resources"]), s
PY
echo "SMOKE OK: iac/state-reconciler ($TF)"
