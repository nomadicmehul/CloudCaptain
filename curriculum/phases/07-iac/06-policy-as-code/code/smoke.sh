#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
PLANGUARD="$PWD/code/main.py"
RULES="$PWD/code/rules.json"
POLICY="$PWD/code/policy"
FIXTURES="$PWD/code/fixtures"
CONFTEST_VERSION="0.71.1"
OPA_VERSION="1.21.4"
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
mkdir -p "$WORK/bin" "$WORK/dl"
export PATH="$WORK/bin:$PATH"

sha256_of() { if command -v sha256sum >/dev/null; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi; }

cat > "$WORK/expected.json" <<'EOF'
[
  ["deny", "require-owner-tag", "terraform_data.cache"],
  ["deny", "require-env-tag", "terraform_data.api"],
  ["deny", "forbid-huge-sizes", "terraform_data.cache"],
  ["deny", "disk-limit", "terraform_data.cache"],
  ["deny", "no-world-writable-files", "local_file.scratch"],
  ["deny", "protect-audit-log", "terraform_data.audit_log"],
  ["deny", "no-replace-stateful", "terraform_data.db"],
  ["deny", "module-source-allow-list", "module.legacy"],
  ["warn", "require-cost-center", "terraform_data.cache"],
  ["warn", "name-convention", "terraform_data.api"]
]
EOF
echo "[]" > "$WORK/expected-clean.json"

cat > "$WORK/compare.py" <<'EOF'
import json
import sys


def planguard_set(path):
    with open(path) as handle:
        return {(v["level"], v["rule"], v["address"]) for v in json.load(handle)["violations"]}


def conftest_set(path):
    found = set()
    with open(path) as handle:
        results = json.load(handle)
    for result in results:
        for level, key in (("deny", "failures"), ("warn", "warnings")):
            for item in result.get(key) or []:
                meta = item.get("metadata") or {}
                found.add((level, meta.get("rule"), meta.get("address")))
    return found


def opa_set(path):
    with open(path) as handle:
        value = json.load(handle)["result"][0]["expressions"][0]["value"]
    return {(level, item["rule"], item["address"]) for level in ("deny", "warn") for item in value.get(level) or []}


tool, mine_path, theirs_path, expected_path = sys.argv[1:5]
mine = planguard_set(mine_path)
theirs = {"conftest": conftest_set, "opa": opa_set}[tool](theirs_path)
with open(expected_path) as handle:
    expected = {tuple(item) for item in json.load(handle)}
if mine != theirs:
    sys.exit(f"planguard and {tool} disagree\n  only planguard: {sorted(mine - theirs)}\n  only {tool}: {sorted(theirs - mine)}")
if mine != expected:
    sys.exit(f"both engines agree but not with the expected set\n  missing: {sorted(expected - mine)}\n  extra: {sorted(mine - expected)}")
print(f"  planguard == {tool}: {len(mine)} finding(s)")
for level, rule, address in sorted(mine):
    print(f"    {level:<4} {rule:<26} {address}")
EOF

run_planguard() {
  local plan="$1" out="$2" want="$3" code=0
  python3 "$PLANGUARD" "$plan" --rules "$RULES" --json > "$out" 2> "$out.err" || code=$?
  [[ "$code" == "$want" ]] || fail "planguard on $plan exited $code, want $want: $(cat "$out" "$out.err")"
}

run_conftest() {
  local plan="$1" out="$2" want="$3" code=0
  conftest test --parser json --policy "$POLICY" --output json "$plan" > "$out" 2> "$out.err" || code=$?
  [[ "$code" == "$want" ]] || fail "conftest on $plan exited $code, want $want: $(cat "$out" "$out.err")"
}

run_opa() {
  local plan="$1" out="$2"
  opa eval --format json --data "$POLICY" --input "$plan" '{"deny": data.main.deny, "warn": data.main.warn}' > "$out" 2> "$out.err" || fail "opa eval on $plan: $(cat "$out" "$out.err")"
}

compare_all() {
  local plan="$1" name="$2" expected="$3" want="$4"
  run_planguard "$plan" "$WORK/pg-$name.json" "$want"
  run_conftest "$plan" "$WORK/ct-$name.json" "$want"
  run_opa "$plan" "$WORK/opa-$name.json"
  python3 "$WORK/compare.py" conftest "$WORK/pg-$name.json" "$WORK/ct-$name.json" "$expected" || fail "conftest comparison on $name: $(cat "$WORK/ct-$name.json")"
  python3 "$WORK/compare.py" opa "$WORK/pg-$name.json" "$WORK/opa-$name.json" "$expected" || fail "opa comparison on $name: $(cat "$WORK/opa-$name.json")"
}

echo "▸ planguard on the committed fixtures: deny exits 1, clean exits 0"
python3 "$PLANGUARD" "$FIXTURES/plan.json" --rules "$RULES" > "$WORK/fixture.txt" && fail "the violating fixture should exit 1"
cat "$WORK/fixture.txt"
grep -q "8 denied, 2 warning(s): apply is blocked." "$WORK/fixture.txt" || fail "fixture summary line: $(cat "$WORK/fixture.txt")"
python3 "$PLANGUARD" "$FIXTURES/plan-clean.json" --rules "$RULES" > "$WORK/clean.txt" || fail "the clean fixture should pass: $(cat "$WORK/clean.txt")"
python3 "$PLANGUARD" "$FIXTURES/plan.json" --rules "$RULES" --markdown > "$WORK/summary.md" || true
grep -q "| deny | \`no-replace-stateful\` | \`terraform_data.db\` | replace |" "$WORK/summary.md" || fail "markdown summary: $(cat "$WORK/summary.md")"

echo "▸ download conftest v$CONFTEST_VERSION and opa v$OPA_VERSION and verify their sha256"
case "$(uname -s)" in
  Linux) CT_OS=Linux; OPA_OS=linux ;;
  Darwin) CT_OS=Darwin; OPA_OS=darwin ;;
  *) fail "unsupported OS $(uname -s)" ;;
esac
case "$(uname -m)" in
  x86_64|amd64) CT_ARCH=x86_64; OPA_ARCH=amd64 ;;
  arm64|aarch64) CT_ARCH=arm64; OPA_ARCH=arm64 ;;
  *) fail "unsupported architecture $(uname -m)" ;;
esac
CT_ASSET="conftest_${CONFTEST_VERSION}_${CT_OS}_${CT_ARCH}.tar.gz"
CT_BASE="https://github.com/open-policy-agent/conftest/releases/download/v${CONFTEST_VERSION}"
curl -fsSL --retry 3 -o "$WORK/dl/$CT_ASSET" "$CT_BASE/$CT_ASSET" || fail "download $CT_BASE/$CT_ASSET"
curl -fsSL --retry 3 -o "$WORK/dl/checksums.txt" "$CT_BASE/checksums.txt" || fail "download $CT_BASE/checksums.txt"
WANT="$(awk -v f="$CT_ASSET" '$2 == f {print $1}' "$WORK/dl/checksums.txt")"
GOT="$(sha256_of "$WORK/dl/$CT_ASSET")"
[[ -n "$WANT" && "$WANT" == "$GOT" ]] || fail "conftest checksum mismatch: want '$WANT' got '$GOT'"
tar -xzf "$WORK/dl/$CT_ASSET" -C "$WORK/bin" conftest
OPA_ASSET="opa_${OPA_OS}_${OPA_ARCH}_static"
[[ "$OPA_OS" == "darwin" && "$OPA_ARCH" == "amd64" ]] && OPA_ASSET="opa_darwin_amd64"
OPA_BASE="https://github.com/open-policy-agent/opa/releases/download/v${OPA_VERSION}"
curl -fsSL --retry 3 -o "$WORK/bin/opa" "$OPA_BASE/$OPA_ASSET" || fail "download $OPA_BASE/$OPA_ASSET"
curl -fsSL --retry 3 -o "$WORK/dl/opa.sha256" "$OPA_BASE/$OPA_ASSET.sha256" || fail "download $OPA_BASE/$OPA_ASSET.sha256"
WANT="$(awk '{print $1; exit}' "$WORK/dl/opa.sha256")"
GOT="$(sha256_of "$WORK/bin/opa")"
[[ -n "$WANT" && "$WANT" == "$GOT" ]] || fail "opa checksum mismatch: want '$WANT' got '$GOT'"
chmod +x "$WORK/bin/conftest" "$WORK/bin/opa"
conftest --version > "$WORK/versions.txt"
opa version >> "$WORK/versions.txt"
head -3 "$WORK/versions.txt"

echo "▸ opa check: the Rego policy compiles"
opa check "$POLICY" > "$WORK/opa-check.txt" 2>&1 || fail "opa check: $(cat "$WORK/opa-check.txt")"

echo "▸ the committed fixtures: planguard, conftest and opa agree"
compare_all "$FIXTURES/plan.json" fixture "$WORK/expected.json" 1
compare_all "$FIXTURES/plan-clean.json" fixture-clean "$WORK/expected-clean.json" 0

if ! command -v tofu >/dev/null; then
  echo "  (tofu not installed; the real-plan comparison is skipped)"
  echo "SMOKE OK: iac/policy-as-code (fixtures only)"
  exit 0
fi

echo "▸ a real OpenTofu plan: baseline config (local, random, terraform_data, one module)"
INFRA="$WORK/infra"
mkdir -p "$INFRA/modules/web" "$INFRA/vendor/legacy"
cd "$INFRA"
cat > modules/web/main.tf <<'EOF'
resource "terraform_data" "page" {
  input = {
    kind    = "page"
    size    = "small"
    disk_gb = 5
    tags    = { owner = "storefront", env = "prod", cost_center = "cc-42" }
  }
}
EOF
cat > vendor/legacy/main.tf <<'EOF'
resource "terraform_data" "thing" {
  input = {
    kind    = "vm"
    size    = "small"
    disk_gb = 10
    tags    = { owner = "platform", env = "dev", cost_center = "cc-01" }
  }
}
EOF
cat > providers.tf <<'EOF'
terraform {
  required_providers {
    local  = { source = "hashicorp/local" }
    random = { source = "hashicorp/random" }
  }
}
EOF
cat > main.tf <<'EOF'
resource "random_pet" "suffix" {}

resource "terraform_data" "db" {
  input = {
    kind    = "database"
    size    = "medium"
    disk_gb = 100
    tags    = { owner = "payments", env = "prod", cost_center = "cc-42" }
  }
  triggers_replace = ["engine-16"]
}

resource "terraform_data" "audit_log" {
  input = {
    kind    = "bucket"
    size    = "small"
    disk_gb = 50
    tags    = { owner = "security", env = "prod", cost_center = "cc-07" }
  }
}

resource "terraform_data" "web" {
  input = {
    kind    = "vm"
    name    = "web-1"
    size    = "small"
    disk_gb = 20
    tags    = { owner = "storefront", env = "prod", cost_center = "cc-42" }
  }
}

resource "local_file" "motd" {
  filename        = "${path.module}/out/motd.txt"
  content         = "hello from ${random_pet.suffix.id}"
  file_permission = "0644"
}

module "web" {
  source = "./modules/web"
}
EOF
tofu init -input=false -no-color > "$WORK/init1.txt" 2>&1 || fail "tofu init: $(cat "$WORK/init1.txt")"
tofu plan -input=false -no-color -out=base.tfplan > "$WORK/plan1.txt" 2>&1 || fail "tofu plan (baseline): $(cat "$WORK/plan1.txt")"
tofu show -json base.tfplan > base.json 2> "$WORK/show-base.err" || fail "tofu show -json base.tfplan: $(cat "$WORK/show-base.err")"
compare_all "$INFRA/base.json" base "$WORK/expected-clean.json" 0
python3 "$PLANGUARD" --show base.tfplan --rules "$RULES" > "$WORK/show-base.txt" || fail "planguard --show on the baseline: $(cat "$WORK/show-base.txt")"
grep -q "policy passed" "$WORK/show-base.txt" || fail "planguard --show summary: $(cat "$WORK/show-base.txt")"
tofu apply -input=false -no-color base.tfplan > "$WORK/apply1.txt" 2>&1 || fail "tofu apply (baseline): $(cat "$WORK/apply1.txt")"
echo "  baseline plan passed all three engines and was applied"

echo "▸ the change: replace the db, delete the audit log, untagged and oversized resources, a 0777 file, an unknown name, a vendored module"
cat > main.tf <<'EOF'
resource "random_pet" "suffix" {}

resource "random_pet" "api" {}

resource "terraform_data" "db" {
  input = {
    kind    = "database"
    size    = "medium"
    disk_gb = 100
    tags    = { owner = "payments", env = "prod", cost_center = "cc-42" }
  }
  triggers_replace = ["engine-17"]
}

resource "terraform_data" "web" {
  input = {
    kind    = "vm"
    name    = "web-1"
    size    = "medium"
    disk_gb = 20
    tags    = { owner = "storefront", env = "prod", cost_center = "cc-42" }
  }
}

resource "terraform_data" "cache" {
  input = {
    kind    = "cache"
    size    = "xlarge"
    disk_gb = 1000
    tags    = { env = "dev" }
  }
}

resource "terraform_data" "api" {
  input = {
    kind    = "vm"
    name    = "api-${random_pet.api.id}"
    size    = "small"
    disk_gb = 20
    tags    = { owner = "storefront", env = "qa", cost_center = "cc-42" }
  }
}

resource "local_file" "motd" {
  filename        = "${path.module}/out/motd.txt"
  content         = "hello from ${random_pet.suffix.id}"
  file_permission = "0644"
}

resource "local_file" "scratch" {
  filename = "${path.module}/out/scratch.txt"
  content  = "tmp"
}

module "web" {
  source = "./modules/web"
}

module "legacy" {
  source = "./vendor/legacy"
}
EOF
tofu init -input=false -no-color > "$WORK/init2.txt" 2>&1 || fail "tofu init (change): $(cat "$WORK/init2.txt")"
tofu plan -input=false -no-color -out=change.tfplan > "$WORK/plan2.txt" 2>&1 || fail "tofu plan (change): $(cat "$WORK/plan2.txt")"
tofu show -json change.tfplan > change.json 2> "$WORK/show-change.err" || fail "tofu show -json change.tfplan: $(cat "$WORK/show-change.err")"
python3 - "$INFRA/change.json" <<'PY' || fail "plan JSON shape: the facts the rules rely on"
import json, sys
plan = json.load(open(sys.argv[1]))
by = {rc["address"]: rc for rc in plan["resource_changes"]}
assert by["terraform_data.db"]["change"]["actions"] == ["delete", "create"], by["terraform_data.db"]["change"]["actions"]
assert by["terraform_data.audit_log"]["change"]["actions"] == ["delete"], by["terraform_data.audit_log"]["change"]["actions"]
assert by["local_file.scratch"]["change"]["after"]["file_permission"] == "0777", by["local_file.scratch"]["change"]["after"]
assert by["terraform_data.api"]["change"]["after_unknown"]["input"]["name"] is True, by["terraform_data.api"]["change"]["after_unknown"]
assert plan["configuration"]["root_module"]["module_calls"]["legacy"]["source"] == "./vendor/legacy"
print("  plan facts: db replaced, audit_log deleted, scratch defaults to 0777, api name known after apply")
PY
compare_all "$INFRA/change.json" change "$WORK/expected.json" 1
python3 "$PLANGUARD" --show change.tfplan --rules "$RULES" --json > "$WORK/show-change.json" && fail "planguard --show on the change should exit 1"
python3 - "$WORK/show-change.json" "$WORK/pg-change.json" <<'PY' || fail "--show and the JSON file should give the same report"
import json, sys
a, b = (json.load(open(p)) for p in sys.argv[1:3])
assert a["violations"] == b["violations"], (a["violations"], b["violations"])
PY

echo "▸ the gate: apply runs only when the policy passes"
if python3 "$PLANGUARD" change.json --rules "$RULES" > "$WORK/gate.txt"; then
  tofu apply -input=false -no-color change.tfplan > "$WORK/apply2.txt" 2>&1
fi
tofu state list > "$WORK/state.txt"
grep -qx "terraform_data.audit_log" "$WORK/state.txt" || fail "the blocked plan was applied anyway: $(cat "$WORK/state.txt")"
grep -q "apply is blocked" "$WORK/gate.txt" || fail "gate output: $(cat "$WORK/gate.txt")"
echo "  apply skipped; terraform_data.audit_log is still in state"

echo "SMOKE OK: iac/policy-as-code"
