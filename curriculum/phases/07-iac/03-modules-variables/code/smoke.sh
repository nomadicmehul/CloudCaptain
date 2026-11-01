#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
MINIMOD="$PWD/code/main.py"
FIXTURES="$PWD/code/fixtures"
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

cat > "$WORK/expected.txt" <<'TXT'
local_file.index
module.web["blog"].local_file.page[0]
module.web["blog"].local_file.page[1]
module.web["docs"].local_file.page[0]
module.web["docs"].local_file.page[1]
TXT

drop_moved() {
  python3 - "$FIXTURES/renamed/main.tf.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    config = json.load(handle)
config.pop("moved")
with open("main.tf.json", "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=2)
PY
}

echo "▸ minimod expands the fixture into namespaced addresses (Build It)"
cp -R "$FIXTURES/site" "$WORK/mm"
cd "$WORK/mm"
python3 "$MINIMOD" addresses -var-file=prod.tfvars > addresses.txt 2>&1 || fail "minimod addresses: $(cat addresses.txt)"
diff "$WORK/expected.txt" addresses.txt > diff.txt || fail "minimod addresses differ from the expected set: $(cat diff.txt)"

echo "▸ minimod rejects invalid values in the root and in a child instance"
if python3 "$MINIMOD" plan -var env=qa > qa.txt 2>&1; then fail "minimod accepted env=qa: $(cat qa.txt)"; fi
grep -q "Invalid value for variable" qa.txt || fail "minimod root validation summary: $(cat qa.txt)"
grep -q "The env variable must be dev or prod." qa.txt || fail "minimod root validation message: $(cat qa.txt)"
if python3 "$MINIMOD" plan -var 'sites={"Blog": "x", "blog": "y"}' > child.txt 2>&1; then fail "minimod accepted the key Blog: $(cat child.txt)"; fi
grep -q 'module.web\["Blog"\].var.name' child.txt || fail "minimod child validation names the instance: $(cat child.txt)"

echo "▸ minimod outputs, apply, and a rename with and without moved"
python3 "$MINIMOD" outputs -var-file=prod.tfvars > outputs.txt 2>&1 || fail "minimod outputs: $(cat outputs.txt)"
grep -q "token = <sensitive>" outputs.txt || fail "minimod hides the sensitive output: $(cat outputs.txt)"
grep -q 'index_file = "./out/index.txt"' outputs.txt || fail "minimod index_file output: $(cat outputs.txt)"
python3 "$MINIMOD" apply -var-file=prod.tfvars > apply.txt 2>&1 || fail "minimod apply: $(cat apply.txt)"
python3 "$MINIMOD" plan -var-file=prod.tfvars > plan2.txt 2>&1 || fail "minimod second plan: $(cat plan2.txt)"
grep -q "No changes" plan2.txt || fail "minimod second plan is not empty: $(cat plan2.txt)"
drop_moved
python3 "$MINIMOD" plan -var-file=prod.tfvars > rename.txt 2>&1 || fail "minimod rename plan: $(cat rename.txt)"
grep -q "Plan: 5 to add, 0 to change, 5 to destroy." rename.txt || fail "minimod rename without moved: $(cat rename.txt)"
cp "$FIXTURES/renamed/main.tf.json" main.tf.json
python3 "$MINIMOD" plan -var-file=prod.tfvars > moved.txt 2>&1 || fail "minimod moved plan: $(cat moved.txt)"
grep -q "Plan: 0 to add, 0 to change, 0 to destroy." moved.txt || fail "minimod moved plan summary: $(cat moved.txt)"
moves="$(grep -c 'has moved to' moved.txt || true)"
[[ "$moves" == "5" ]] || fail "minimod should report 5 moves, got $moves: $(cat moved.txt)"
echo "  minimod: addresses, validation, outputs, apply and moved OK"

TF=""
if command -v tofu >/dev/null; then TF=tofu; elif command -v terraform >/dev/null; then TF=terraform; fi
if [[ -z "$TF" ]]; then
  echo "  (neither tofu nor terraform installed; Use It skipped)"
  echo "SMOKE OK: iac/modules-variables (minimod only)"
  exit 0
fi

echo "▸ $TF init records the local module"
cp -R "$FIXTURES/site" "$WORK/tf"
cd "$WORK/tf"
$TF init -input=false -no-color > init.txt 2>&1 || fail "$TF init: $(cat init.txt)"
[[ -f .terraform/modules/modules.json ]] || fail "$TF init wrote no .terraform/modules/modules.json: $(cat init.txt)"
grep -q "modules/web" .terraform/modules/modules.json || fail "modules.json does not record modules/web: $(cat .terraform/modules/modules.json)"

echo "▸ the same validations fail in $TF"
if $TF plan -input=false -no-color -var env=qa > qa.txt 2>&1; then fail "$TF accepted env=qa: $(cat qa.txt)"; fi
grep -q "Invalid value for variable" qa.txt || fail "$TF root validation summary: $(cat qa.txt)"
grep -q "The env variable must be dev or prod." qa.txt || fail "$TF root validation message: $(cat qa.txt)"
if $TF plan -input=false -no-color -var 'sites={"Blog"="x", "blog"="y"}' > child.txt 2>&1; then fail "$TF accepted the key Blog: $(cat child.txt)"; fi
grep -q "Invalid value for variable" child.txt || fail "$TF child validation summary: $(cat child.txt)"
grep -q "lowercase letters" child.txt || fail "$TF child validation message: $(cat child.txt)"

echo "▸ $TF apply; state list equals minimod addresses"
$TF apply -input=false -auto-approve -no-color -var-file=prod.tfvars > apply.txt 2>&1 || fail "$TF apply: $(tail -30 apply.txt)"
$TF state list > state.txt 2>&1 || fail "$TF state list: $(cat state.txt)"
python3 "$MINIMOD" addresses -var-file=prod.tfvars > mm.txt 2>&1 || fail "minimod addresses: $(cat mm.txt)"
diff <(sort state.txt) <(sort mm.txt) > diff.txt || fail "$TF state list and minimod addresses differ: $(cat diff.txt)"
diff <(sort state.txt) <(sort "$WORK/expected.txt") > diff.txt || fail "$TF state list differs from the expected set: $(cat diff.txt)"
[[ "$(cat out/prod-site-docs-1.txt)" == "$(printf '# prod-site-docs\nHello from the docs')" ]] || fail "$TF wrote unexpected content: $(cat out/prod-site-docs-1.txt)"

echo "▸ outputs agree, and both hide the sensitive one"
$TF output -json > tf-outputs.json 2> tf-outputs.err || fail "$TF output -json: $(cat tf-outputs.err)"
python3 "$MINIMOD" outputs --json -var-file=prod.tfvars > mm-outputs.json 2> mm-outputs.err || fail "minimod outputs --json: $(cat mm-outputs.err)"
python3 - <<'PY' || fail "output values differ: $(cat tf-outputs.json) vs $(cat mm-outputs.json)"
import json
with open("tf-outputs.json") as handle:
    tf = json.load(handle)
with open("mm-outputs.json") as handle:
    mm = json.load(handle)
assert set(tf) == set(mm), (sorted(tf), sorted(mm))
for name in tf:
    assert tf[name]["value"] == mm[name]["value"], (name, tf[name]["value"], mm[name]["value"])
    assert bool(tf[name].get("sensitive")) == mm[name]["sensitive"], name
PY
$TF output -no-color > tf-outputs.txt 2>&1 || fail "$TF output: $(cat tf-outputs.txt)"
grep -q "token = <sensitive>" tf-outputs.txt || fail "$TF output hides the token: $(cat tf-outputs.txt)"
echo 'local.prefix' | $TF console -var-file=prod.tfvars > console.txt 2>&1 || fail "$TF console: $(cat console.txt)"
grep -q "prod-site" console.txt || fail "$TF console local.prefix: $(cat console.txt)"
python3 "$MINIMOD" eval 'local.prefix' -var-file=prod.tfvars > eval.txt 2>&1 || fail "minimod eval: $(cat eval.txt)"
grep -q '"prod-site"' eval.txt || fail "minimod eval local.prefix: $(cat eval.txt)"

echo "▸ a rename without moved destroys and creates in both"
drop_moved
$TF init -input=false -no-color > init-rename.txt 2>&1 || fail "$TF init for the renamed module call: $(tail -30 init-rename.txt)"
$TF plan -input=false -no-color -var-file=prod.tfvars > rename.txt 2>&1 || fail "$TF rename plan: $(tail -30 rename.txt)"
grep -q "Plan: 5 to add, 0 to change, 5 to destroy." rename.txt || fail "$TF rename without moved: $(grep 'Plan:' rename.txt || tail -20 rename.txt)"
python3 "$MINIMOD" plan -var-file=prod.tfvars --state terraform.tfstate > mm-rename.txt 2>&1 || fail "minimod rename plan on $TF state: $(cat mm-rename.txt)"
grep -q "Plan: 5 to add, 0 to change, 5 to destroy." mm-rename.txt || fail "minimod rename on $TF state: $(cat mm-rename.txt)"

echo "▸ moved blocks: 0 to add, 0 to destroy, the same five moves in both"
cp "$FIXTURES/renamed/main.tf.json" main.tf.json
$TF plan -input=false -no-color -var-file=prod.tfvars -out=moved.plan > moved.txt 2>&1 || fail "$TF moved plan: $(tail -30 moved.txt)"
grep -q "has moved to" moved.txt || fail "$TF plan shows no moves: $(cat moved.txt)"
grep -Eq "Plan: 0 to add, 0 to change, 0 to destroy|No changes" moved.txt || fail "$TF moved plan summary: $(grep 'Plan:' moved.txt || tail -20 moved.txt)"
$TF show -json moved.plan > moved.json 2> show.err || fail "$TF show -json: $(cat show.err)"
python3 "$MINIMOD" plan --json -var-file=prod.tfvars --state terraform.tfstate > mm-moved.json 2> mm-moved.err || fail "minimod moved plan on $TF state: $(cat mm-moved.err)"
python3 - <<'PY' || fail "moves differ between $TF and minimod"
import json
with open("moved.json") as handle:
    plan = json.load(handle)
with open("mm-moved.json") as handle:
    mm = json.load(handle)
changes = plan.get("resource_changes", [])
actions = {change["address"]: change["change"]["actions"] for change in changes}
assert all(action == ["no-op"] for action in actions.values()), actions
tf_moves = {(change["previous_address"], change["address"]) for change in changes if change.get("previous_address")}
mm_moves = {(move["from"], move["to"]) for move in mm["moves"]}
assert len(tf_moves) == 5 and tf_moves == mm_moves, (sorted(tf_moves), sorted(mm_moves))
assert mm["summary"]["add"] == 0 and mm["summary"]["destroy"] == 0 and mm["summary"]["change"] == 0, mm["summary"]
print("  moves:", ", ".join(sorted(f"{a} -> {b}" for a, b in tf_moves)))
PY

echo "▸ apply the moves; the new addresses match minimod"
$TF apply -input=false -auto-approve -no-color moved.plan > apply2.txt 2>&1 || fail "$TF apply moved.plan: $(tail -30 apply2.txt)"
$TF state list > state2.txt 2>&1 || fail "$TF state list: $(cat state2.txt)"
python3 "$MINIMOD" addresses -var-file=prod.tfvars > mm2.txt 2>&1 || fail "minimod addresses on the renamed config: $(cat mm2.txt)"
diff <(sort state2.txt) <(sort mm2.txt) > diff.txt || fail "$TF state list after moves differs from minimod: $(cat diff.txt)"
grep -qx 'module.site\["blog"\].local_file.page\[1\]' state2.txt || fail "$TF state has no module.site instances: $(cat state2.txt)"
grep -qx 'local_file.home' state2.txt || fail "$TF state has no local_file.home: $(cat state2.txt)"
$TF state mv -dry-run 'module.site["docs"]' 'module.web["docs"]' > mv.txt 2>&1 || fail "$TF state mv -dry-run: $(cat mv.txt)"
grep -q "module.site" mv.txt || fail "$TF state mv -dry-run output: $(cat mv.txt)"
$TF state list > state3.txt 2>&1
diff state2.txt state3.txt > diff.txt || fail "state mv -dry-run changed the state: $(cat diff.txt)"
echo "SMOKE OK: iac/modules-variables ($TF)"
