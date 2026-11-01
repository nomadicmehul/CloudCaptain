#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
MG="$PWD/code/main.py"
FIXTURES="$PWD/code/fixtures"
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

write_config() { mkdir -p "$1" && cp "$FIXTURES/demo/main.tf.json" "$1/"; }
write_cycle() { mkdir -p "$1" && cp "$FIXTURES/cycle/main.tf.json" "$1/"; }

cat > "$WORK/check.py" <<'PY'
import json
import re
import sys

QUOTED = r'"((?:[^"\\]|\\.)*)"'
EDGE = re.compile(r"^\s*" + QUOTED + r"\s*->\s*" + QUOTED)
NODE = re.compile(r"^\s*" + QUOTED + r"\s*(\[|;|$)")
RESOURCE = re.compile(r"^[a-z][a-z0-9_]*\.[A-Za-z_][A-Za-z0-9_-]*$")
PLUMBING = {"local", "var", "output", "data", "module", "path", "provider", "meta", "root", "terraform"}
LINE = re.compile(r"^(?P<addr>\S+?)(?P<deposed> \(deposed object [0-9a-f]+\))?: (?P<what>Creating\.\.\.|Creation complete|Destroying\.\.\.|Destruction complete|Modifying\.\.\.|Modifications complete)")


def clean(name):
    name = name.replace('\\"', '"')
    if name.startswith("[root] "):
        name = name[len("[root] "):]
    stripped = re.sub(r"\s+\([^)]*\)$", "", name)
    return stripped if is_resource(stripped) else name


def is_resource(name):
    return bool(RESOURCE.match(name)) and name.split(".")[0] not in PLUMBING


def resource_edges(path):
    adjacency = {}
    for line in open(path, encoding="utf-8"):
        match = EDGE.match(line)
        if match:
            node, dep = clean(match.group(1)), clean(match.group(2))
            adjacency.setdefault(node, set()).add(dep)
            adjacency.setdefault(dep, set())
            continue
        match = NODE.match(line)
        if match:
            adjacency.setdefault(clean(match.group(1)), set())
    closure = {}
    for start in sorted(node for node in adjacency if is_resource(node)):
        seen, stack = set(), list(adjacency[start])
        while stack:
            node = stack.pop()
            if node == start or node in seen:
                continue
            seen.add(node)
            stack.extend(adjacency.get(node, ()))
        closure[start] = {node for node in seen if is_resource(node)}
    return {node: {dep for dep in found if not any(dep in closure[other] for other in found if other != dep)} for node, found in closure.items()}


def events(path):
    found = []
    for line in open(path, encoding="utf-8"):
        match = LINE.match(line.strip())
        if match:
            found.append((match.group("addr"), bool(match.group("deposed")), match.group("what")))
    return found


def index(found, address, what, deposed=False):
    for position, event in enumerate(found):
        if event == (address, deposed, what):
            return position
    raise SystemExit(f"no line '{address}{' (deposed object ...)' if deposed else ''}: {what}' in the log")


def peak(found):
    level = top = 0
    for _, _, what in found:
        if what == "Creating...":
            level += 1
            top = max(top, level)
        elif what == "Creation complete":
            level -= 1
    return top


command = sys.argv[1]
if command == "graph":
    mini, real = resource_edges(sys.argv[2]), resource_edges(sys.argv[3])
    if not mini or not real:
        raise SystemExit(f"no resource nodes parsed (minigraph {len(mini)}, tofu {len(real)})")
    if set(mini) != set(real):
        raise SystemExit(f"resource nodes differ: minigraph {sorted(mini)} vs tofu {sorted(real)}")
    if mini != real:
        diff = {node: (sorted(mini[node]), sorted(real[node])) for node in mini if mini[node] != real[node]}
        raise SystemExit(f"edges differ (minigraph, tofu): {diff}")
    print(f"  {len(mini)} resources, {sum(len(v) for v in mini.values())} edges after reduction, identical: " + ", ".join(f"{n} -> {d}" for n in sorted(mini) for d in sorted(mini[n])))
elif command == "apply-order":
    deps, found, parallelism = resource_edges(sys.argv[2]), events(sys.argv[3]), int(sys.argv[4])
    for node, found_deps in deps.items():
        for dep in found_deps:
            if not index(found, dep, "Creation complete") < index(found, node, "Creating..."):
                raise SystemExit(f"{node} started before {dep} finished")
    top = peak(found)
    if top > parallelism or (parallelism == 1 and top != 1) or (parallelism >= 4 and top < 2):
        raise SystemExit(f"{top} creates in flight with -parallelism={parallelism}")
    print(f"  every edge respected; at most {top} creates in flight with -parallelism={parallelism}")
elif command == "destroy-order":
    deps, found = resource_edges(sys.argv[2]), events(sys.argv[3])
    for node, found_deps in deps.items():
        for dep in found_deps:
            if not index(found, node, "Destruction complete") < index(found, dep, "Destroying..."):
                raise SystemExit(f"{dep} destroyed before its dependent {node}")
    print(f"  every edge walked backwards ({sum(len(v) for v in deps.values())} edges)")
elif command == "cbd":
    found = events(sys.argv[2])
    retire = index(found, "local_file.versioned", "Destroying...", deposed=True)
    if not index(found, "local_file.versioned", "Creating...") < retire:
        raise SystemExit("the old local_file.versioned was destroyed before its replacement was created")
    if not index(found, "local_file.pointer", "Creation complete") < retire:
        raise SystemExit("the old local_file.versioned was destroyed before its dependent local_file.pointer moved to the new one")
    print("  new object created, dependent replaced, deposed object destroyed last")
elif command == "plan-json":
    docs = {"minigraph": json.load(open(sys.argv[2])), "tofu": json.load(open(sys.argv[3]))}
    addresses = {}
    for name, doc in docs.items():
        changes = {item["address"]: item["change"] for item in doc["resource_changes"]}
        addresses[name] = set(changes)
        assert all(change["actions"] == ["create"] for change in changes.values()), (name, changes)
        assert changes["terraform_data.release"]["after_unknown"].get("input") is True, (name, changes["terraform_data.release"])
        assert changes["local_file.config"]["after_unknown"].get("content") is True, (name, changes["local_file.config"])
        assert not changes["local_file.index"]["after_unknown"].get("content"), (name, changes["local_file.index"])
        assert changes["local_file.index"]["after"]["content"] == "see ./out/config.txt\n", (name, changes["local_file.index"])
        assert changes["local_file.config"]["after"]["filename"] == "./out/config.txt", (name, changes["local_file.config"])
    assert addresses["minigraph"] == addresses["tofu"], addresses
    print("  both: terraform_data.release.input and local_file.config.content unknown, local_file.index.content known")
elif command == "cycle":
    named = {}
    for name, path in (("minigraph", sys.argv[2]), ("tofu", sys.argv[3])):
        text = open(path, encoding="utf-8").read()
        if "Cycle" not in text:
            raise SystemExit(f"{name}: no cycle error in: {text}")
        named[name] = set(re.findall(r"terraform_data\.[a-z_]+", text[text.index("Cycle"):]))
    expected = {"terraform_data.a", "terraform_data.b", "terraform_data.c"}
    if not named["minigraph"] == named["tofu"] == expected:
        raise SystemExit(f"cycle members differ: {named}")
    print(f"  both name {sorted(expected)} and leave terraform_data.d out")
else:
    raise SystemExit(f"unknown check {command}")
PY
CHECK() { python3 "$WORK/check.py" "$@"; }

echo "▸ minigraph validate, order and graph (no providers, nothing applied)"
write_config "$WORK/mg"
python3 "$MG" -chdir="$WORK/mg" validate > "$WORK/mg-validate.txt" || fail "validate: $(cat "$WORK/mg-validate.txt")"
grep -q "Success! The configuration is valid." "$WORK/mg-validate.txt" || fail "validate output: $(cat "$WORK/mg-validate.txt")"
python3 "$MG" -chdir="$WORK/mg" --json order > "$WORK/mg-order.json"
python3 - "$WORK/mg-order.json" <<'PY' || fail "order: $(cat "$WORK/mg-order.json")"
import json, sys
d = json.load(open(sys.argv[1]))
assert len(d["waves"]) == 4, d["waves"]
assert len(d["waves"][0]) == 6, d["waves"][0]
assert d["critical_path"] == ["random_pet.name", "terraform_data.release", "local_file.config", "local_file.index"], d["critical_path"]
print("  4 waves, critical path", " -> ".join(d["critical_path"]))
PY
python3 "$MG" -chdir="$WORK/mg" graph > "$WORK/mg.dot"
grep -q '"local_file.index" -> "time_sleep.warm_a" \[style = "dashed"\]' "$WORK/mg.dot" || fail "graph should draw depends_on edges: $(cat "$WORK/mg.dot")"

echo "▸ minigraph plan: values that depend on new resources are known after apply"
python3 "$MG" -chdir="$WORK/mg" plan > "$WORK/mg-plan.txt"
grep -q "(known after apply)" "$WORK/mg-plan.txt" || fail "plan should show unknowns: $(cat "$WORK/mg-plan.txt")"
grep -q "Plan: 10 to add, 0 to change, 0 to destroy." "$WORK/mg-plan.txt" || fail "plan summary: $(cat "$WORK/mg-plan.txt")"
python3 "$MG" -chdir="$WORK/mg" --json plan > "$WORK/mg-plan.json"

echo "▸ minigraph apply -parallelism=1: one node at a time, every edge respected"
python3 "$MG" -chdir="$WORK/mg" apply -parallelism=1 > "$WORK/mg-apply1.log" 2>&1 || fail "apply: $(cat "$WORK/mg-apply1.log")"
CHECK apply-order "$WORK/mg.dot" "$WORK/mg-apply1.log" 1 || fail "minigraph apply order: $(cat "$WORK/mg-apply1.log")"
grep -q "Apply complete! Resources: 10 added, 0 changed, 0 destroyed." "$WORK/mg-apply1.log" || fail "apply summary: $(cat "$WORK/mg-apply1.log")"
PET="$(python3 "$MG" -chdir="$WORK/mg" show | python3 -c 'import json,sys; print(json.load(sys.stdin)["outputs"]["pet"])')" || fail "minigraph show has no pet output"
[[ "$(cat "$WORK/mg/out/config.txt")" == "release=$PET" ]] || fail "config.txt should carry the pet name $PET: $(cat "$WORK/mg/out/config.txt")"

echo "▸ minigraph destroy: the same edges walked backwards"
python3 "$MG" -chdir="$WORK/mg" destroy -parallelism=4 > "$WORK/mg-destroy.log" 2>&1 || fail "destroy: $(cat "$WORK/mg-destroy.log")"
CHECK destroy-order "$WORK/mg.dot" "$WORK/mg-destroy.log" || fail "minigraph destroy order: $(cat "$WORK/mg-destroy.log")"
grep -q "Destroy complete! Resources: 10 destroyed." "$WORK/mg-destroy.log" || fail "destroy summary: $(cat "$WORK/mg-destroy.log")"

echo "▸ minigraph apply -parallelism=4: independent nodes overlap"
python3 "$MG" -chdir="$WORK/mg" apply -parallelism=4 > "$WORK/mg-apply4.log" 2>&1 || fail "apply: $(cat "$WORK/mg-apply4.log")"
CHECK apply-order "$WORK/mg.dot" "$WORK/mg-apply4.log" 4 || fail "minigraph parallel apply: $(cat "$WORK/mg-apply4.log")"

echo "▸ minigraph create_before_destroy: +/-, the new object first, the old one last"
sed -i.bak 's#/v1.txt#/v2.txt#' "$WORK/mg/main.tf.json"
python3 "$MG" -chdir="$WORK/mg" plan > "$WORK/mg-cbd-plan.txt"
grep -qF -- "+/- local_file.versioned" "$WORK/mg-cbd-plan.txt" || fail "plan should mark create-before-destroy: $(cat "$WORK/mg-cbd-plan.txt")"
grep -q "Plan: 2 to add, 0 to change, 2 to destroy." "$WORK/mg-cbd-plan.txt" || fail "cbd plan summary: $(cat "$WORK/mg-cbd-plan.txt")"
python3 "$MG" -chdir="$WORK/mg" apply > "$WORK/mg-cbd.log" 2>&1 || fail "cbd apply: $(cat "$WORK/mg-cbd.log")"
CHECK cbd "$WORK/mg-cbd.log" || fail "minigraph cbd order: $(cat "$WORK/mg-cbd.log")"
[[ -f "$WORK/mg/out/v2.txt" && ! -e "$WORK/mg/out/v1.txt" ]] || fail "v2.txt should replace v1.txt: $(ls "$WORK/mg/out")"

echo "▸ minigraph rejects a cycle and prints its path"
write_cycle "$WORK/mg-cycle"
if python3 "$MG" -chdir="$WORK/mg-cycle" plan > "$WORK/mg-cycle.out" 2>&1; then fail "a cyclic config should not plan: $(cat "$WORK/mg-cycle.out")"; fi
grep -q "Cycle: terraform_data.a -> terraform_data.c -> terraform_data.b -> terraform_data.a" "$WORK/mg-cycle.out" || fail "cycle path: $(cat "$WORK/mg-cycle.out")"
echo "  $(cat "$WORK/mg-cycle.out")"

if ! command -v tofu >/dev/null; then
  echo "  (tofu not installed; Use It skipped)"
  echo "SMOKE OK: iac/dependency-graph (minigraph only)"
  exit 0
fi

echo "▸ Use It: tofu init with the local, random and time providers"
write_config "$WORK/tf"
cd "$WORK/tf"
tofu init -input=false -no-color > init.log 2>&1 || fail "tofu init: $(cat init.log)"
tofu validate -no-color > validate.log 2>&1 || fail "tofu validate: $(cat validate.log)"

echo "▸ tofu graph and minigraph graph have the same resource edges"
tofu graph > tofu.dot 2> graph.err || fail "tofu graph: $(cat graph.err)"
CHECK graph "$WORK/mg.dot" tofu.dot || fail "graphs differ; tofu graph was: $(cat tofu.dot)"
if command -v dot >/dev/null; then
  dot -Tsvg tofu.dot -o graph.svg
  grep -q "<svg" graph.svg || fail "dot did not render an SVG"
  echo "  rendered graph.svg with $(dot -V 2>&1)"
else
  echo "  (graphviz dot not installed; SVG rendering skipped)"
fi

echo "▸ tofu plan marks the same values known after apply"
tofu plan -input=false -no-color -out=plan.out > plan.txt 2>&1 || fail "tofu plan: $(cat plan.txt)"
grep -q "(known after apply)" plan.txt || fail "tofu plan should show unknowns: $(cat plan.txt)"
grep -q "Plan: 10 to add, 0 to change, 0 to destroy." plan.txt || fail "tofu plan summary: $(cat plan.txt)"
tofu show -json plan.out > tofu-plan.json
CHECK plan-json "$WORK/mg-plan.json" tofu-plan.json || fail "plan JSON comparison"

echo "▸ tofu apply -parallelism=1"
tofu apply -input=false -no-color -auto-approve -parallelism=1 > apply1.log 2>&1 || fail "tofu apply: $(cat apply1.log)"
CHECK apply-order "$WORK/mg.dot" apply1.log 1 || fail "tofu apply order: $(cat apply1.log)"
TF_PET="$(tofu output -raw pet)" || fail "tofu output -raw pet"
[[ "$(cat out/config.txt)" == "release=$TF_PET" ]] || fail "tofu config.txt should carry $TF_PET: $(cat out/config.txt)"

echo "▸ tofu destroy walks the edges backwards"
tofu destroy -input=false -no-color -auto-approve -parallelism=4 > destroy.log 2>&1 || fail "tofu destroy: $(cat destroy.log)"
CHECK destroy-order "$WORK/mg.dot" destroy.log || fail "tofu destroy order: $(cat destroy.log)"
grep -q "Destroy complete! Resources: 10 destroyed." destroy.log || fail "tofu destroy summary: $(cat destroy.log)"

echo "▸ tofu apply -parallelism=4"
tofu apply -input=false -no-color -auto-approve -parallelism=4 > apply4.log 2>&1 || fail "tofu apply: $(cat apply4.log)"
CHECK apply-order "$WORK/mg.dot" apply4.log 4 || fail "tofu parallel apply: $(cat apply4.log)"

echo "▸ tofu create_before_destroy"
sed -i.bak 's#/v1.txt#/v2.txt#' main.tf.json
tofu plan -input=false -no-color > cbd-plan.txt 2>&1 || fail "tofu cbd plan: $(cat cbd-plan.txt)"
grep -qF -- "+/-" cbd-plan.txt || fail "tofu plan should mark create-before-destroy: $(cat cbd-plan.txt)"
grep -q "local_file.versioned must be replaced" cbd-plan.txt || fail "tofu cbd plan: $(cat cbd-plan.txt)"
grep -q "Plan: 2 to add, 0 to change, 2 to destroy." cbd-plan.txt || fail "tofu cbd plan summary: $(cat cbd-plan.txt)"
tofu apply -input=false -no-color -auto-approve > cbd.log 2>&1 || fail "tofu cbd apply: $(cat cbd.log)"
CHECK cbd cbd.log || fail "tofu cbd order: $(cat cbd.log)"
[[ -f out/v2.txt && ! -e out/v1.txt ]] || fail "tofu: v2.txt should replace v1.txt: $(ls out)"

echo "▸ tofu rejects the same cycle with the same resources named"
write_cycle "$WORK/tf-cycle"
cd "$WORK/tf-cycle"
tofu init -input=false -no-color > init.log 2>&1 || fail "tofu init (cycle dir): $(cat init.log)"
if tofu plan -input=false -no-color > "$WORK/tf-cycle.out" 2>&1; then fail "tofu should reject the cycle: $(cat "$WORK/tf-cycle.out")"; fi
CHECK cycle "$WORK/mg-cycle.out" "$WORK/tf-cycle.out" || fail "cycle reports differ: $(cat "$WORK/tf-cycle.out")"

tofu -version > "$WORK/version.txt"
echo "SMOKE OK: iac/dependency-graph ($(head -1 "$WORK/version.txt"))"
