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
python3 "$MINITF" plan | grep -q "Plan: 2 to add, 0 to change, 0 to destroy." || fail "initial plan"
python3 "$MINITF" apply >/dev/null
[[ "$(cat site/index.html)" == "hello" ]] || fail "apply wrote the file"
python3 "$MINITF" plan | grep -q "No changes" || fail "second plan is a no-op"
sed -i.bak 's/hello/hello v2/' main.json
python3 "$MINITF" plan | grep -q "~ local_file.index" || fail "content change plans an update"
python3 "$MINITF" apply >/dev/null
rm site/index.html
python3 "$MINITF" plan | grep -q "drift" || fail "manual deletion is drift"
python3 "$MINITF" apply >/dev/null
sed -i.bak 's#site/index.html#site/home.html#' main.json
python3 "$MINITF" plan | grep -q -- "-/+ local_file.index" || fail "path change forces replacement"
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

echo "▸ Use It with $TF and the local provider"
mkdir -p "$WORK/tf" && cd "$WORK/tf"
cat > main.tf <<'EOF'
resource "local_file" "index" {
  filename = "${path.module}/site/index.html"
  content  = "hello"
}
EOF
$TF init -input=false -no-color >/dev/null
$TF plan -input=false -no-color | grep -q "1 to add, 0 to change, 0 to destroy" || fail "$TF initial plan"
$TF apply -input=false -auto-approve -no-color >/dev/null
[[ "$(cat site/index.html)" == "hello" ]] || fail "$TF apply wrote the file"
$TF plan -input=false -no-color | grep -q "No changes" || fail "$TF second plan is a no-op"
sed -i.bak 's/"hello"/"hello v2"/' main.tf
$TF plan -input=false -no-color | grep -q "1 to change" || fail "$TF content change is an in-place update"
rm site/index.html
$TF plan -input=false -no-color | grep -q "1 to add" || fail "$TF detects drift after manual deletion"
$TF state list | grep -q "local_file.index" || fail "$TF state list"
python3 - <<'PY' || fail "state file shape"
import json
s = json.load(open("terraform.tfstate"))
assert s["serial"] >= 1 and any(r["type"] == "local_file" for r in s["resources"]), s
PY
echo "SMOKE OK: iac/state-reconciler ($TF)"
