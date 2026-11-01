#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
LESSON="$PWD"
MAIN="$PWD/code/main.py"
EXAMPLES="$PWD/code/examples"
WORK="$(mktemp -d)"
NS="cc-pulumi-crossplane"
PULUMI_VERSION="v3.273.0"
PULUMI_RANDOM_VERSION="4.21.4"
XP_CLI_VERSION="v2.5.0"
XP_CORE_VERSION="v2.4.3"
PT_IMAGE="xpkg.crossplane.io/crossplane-contrib/function-patch-and-transform:v0.11.2"
case "$(uname -m)" in
  x86_64) PULUMI_ARCH="x64"; PULUMI_SHA256="ec08f86671fd58f0a186540e1cee00066393beaf5c6716b0075f690919800432"; XP_ARCH="linux_amd64"; XP_SHA256="065dd6b40c9ea5fc71e49900d5c924cc0bec7f58375922ddbde6b402f292347e" ;;
  aarch64|arm64) PULUMI_ARCH="arm64"; PULUMI_SHA256="c2e1b600b37d9d84551259b6f391f5035e336dfe63c49412f22d855a91995ed5"; XP_ARCH="linux_arm64"; XP_SHA256="2ae3bfe43206e47454c2634dace1427bd35794f616619e8f34ce23ce5b811dfd" ;;
  *) fail "unsupported architecture $(uname -m)" ;;
esac
CTX_ARGS=()
NS_CREATED=""
k() { kubectl ${CTX_ARGS[@]+"${CTX_ARGS[@]}"} "$@"; }
cleanup() {
  if [[ -n "$NS_CREATED" ]]; then
    k delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  if command -v docker >/dev/null; then
    docker network ls -q --filter name=crossplane-render- 2>/dev/null > "$WORK/xp-networks" || true
    xargs -r docker network rm < "$WORK/xp-networks" >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT
mkdir -p "$WORK/bin" "$WORK/xp"

cat > "$WORK/check.py" <<'PY'
import json
import sys

COMPOSITION_RESOURCE_NAME = "crossplane.io/composition-resource-name"


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def short(urn):
    return urn.rsplit("::", 1)[-1]


def resources(export):
    return ((export.get("deployment") or {}).get("resources")) or []


def graph(export):
    out = {}
    for entry in resources(export):
        if entry["type"].startswith("pulumi:"):
            continue
        out[short(entry["urn"])] = {"type": entry["type"], "dependencies": sorted(short(d) for d in entry.get("dependencies") or [])}
    return out


def steps(preview):
    return {short(s["urn"]): (s["op"], s["urn"].split("::")[2]) for s in preview.get("steps") or [] if not s["urn"].split("::")[2].startswith("pulumi:")}


def first_preview(real_path, mini_path):
    real, mini = load(real_path), load(mini_path)
    real_creates = {name: rtype for name, (op, rtype) in steps(real).items() if op == "create"}
    mini_creates = {name: rtype for name, (op, rtype) in steps(mini).items() if op == "create"}
    assert real_creates == mini_creates, (real_creates, mini_creates)
    assert sorted(real_creates) == ["pet", "port", "suffix"], real_creates
    assert real["changeSummary"].get("create") == mini["changeSummary"].get("create") == 4, (real["changeSummary"], mini["changeSummary"])
    print("  both previews: + 4 to create (the stack and %s)" % ", ".join(sorted(real_creates)))


def outputs(path, suffix_length):
    data = load(path)
    pet, suffix = data["name"].rsplit("-", 1)
    assert len(pet.split("-")) == 2 and all(word.isalpha() for word in pet.split("-")), data
    assert len(suffix) == int(suffix_length) and suffix == suffix.lower() and suffix.isalnum(), data
    assert 8000 <= int(data["port"]) <= 8999, data
    print("  %s: name=%s port=%s" % (path.rsplit("/", 1)[-1], data["name"], data["port"]))


def same_graph(real_path, mini_path):
    real, mini = graph(load(real_path)), graph(load(mini_path))
    assert real == mini, (real, mini)
    assert real["port"]["dependencies"] == ["pet", "suffix"], real
    print("  same resource graph: " + "; ".join("%s <- %s" % (name, ",".join(node["dependencies"]) or "-") for name, node in sorted(real.items())))


def second_preview(path):
    found = steps(load(path))
    assert found.get("suffix", ("missing",))[0] == "replace", found
    assert found.get("port", ("missing",))[0] in ("replace", "create-replacement", "update"), found
    assert found.get("pet", ("same",))[0] == "same", found
    print("  %s: suffix %s, port %s, pet unchanged" % (path.rsplit("/", 1)[-1], found["suffix"][0], found["port"][0]))


def pet_kept(before_path, after_path):
    before, after = load(before_path), load(after_path)
    assert before["name"].rsplit("-", 1)[0] == after["name"].rsplit("-", 1)[0], (before, after)
    assert len(after["name"].rsplit("-", 1)[1]) == 8, after
    print("  pet kept (%s), suffix replaced: %s -> %s" % (after["name"].rsplit("-", 1)[0], before["name"], after["name"]))


def empty(path):
    left = [entry["urn"] for entry in resources(load(path)) if entry.get("custom") and not entry["type"].startswith("pulumi:providers:")]
    assert left == [], left


def offline_render(path):
    objects = load(path)
    xr, deployment, service = objects
    assert [deployment["kind"], service["kind"]] == ["Deployment", "Service"], objects
    assert deployment["spec"]["replicas"] == 2, deployment["spec"]
    assert deployment["spec"]["template"]["spec"]["containers"][0]["image"] == "registry.k8s.io/pause:3.10"
    assert service["spec"]["selector"] == {"app": "checkout-web"}, service["spec"]
    assert deployment["metadata"]["labels"]["team"] == "payments"
    assert xr["status"]["conditions"][0]["reason"] == "Creating", xr
    print("  composed: Deployment (2 replicas, app=checkout-web), Service; XR Ready=False Creating")


def for_create(path, out):
    items = []
    for obj in load(path)[1:]:
        obj["metadata"].pop("ownerReferences", None)
        items.append(obj)
    with open(out, "w", encoding="utf-8") as handle:
        json.dump({"apiVersion": "v1", "kind": "List", "items": items}, handle)


def observed(path, out):
    docs = []
    for item in load(path)["items"]:
        meta = item["metadata"]
        keep = {key: meta[key] for key in ("name", "namespace", "labels", "annotations") if key in meta}
        docs.append({"apiVersion": item["apiVersion"], "kind": item["kind"], "metadata": keep, "spec": item.get("spec", {}), "status": item.get("status", {})})
    assert sorted(d["metadata"]["annotations"][COMPOSITION_RESOURCE_NAME] for d in docs) == ["deployment", "service"], docs
    with open(out, "w", encoding="utf-8") as handle:
        handle.write("\n".join("---\n" + json.dumps(doc) for doc in docs) + "\n")


def condition(xr):
    return next(c for c in (xr.get("status") or {}).get("conditions") or [] if c.get("type") == "Ready")


def observed_render(path, cluster_ip):
    objects = load(path)
    xr = objects[0]
    assert xr["status"]["address"] == cluster_ip, xr["status"]
    assert xr["status"]["availableReplicas"] == 2, xr["status"]
    assert condition(xr)["status"] == "True", xr["status"]
    assert all("name" in obj["metadata"] for obj in objects[1:]), objects
    print("  status flowed back: address=%s availableReplicas=2, XR Ready=True" % cluster_ip)


def compare_render(mini_path, real_path, with_status):
    import yaml
    mini = load(mini_path)
    with open(real_path, encoding="utf-8") as handle:
        real = [doc for doc in yaml.safe_load_all(handle) if doc]
    real_xr = next(doc for doc in real if doc.get("kind") == "App")

    def by_name(objects):
        return {((obj.get("metadata") or {}).get("annotations") or {}).get(COMPOSITION_RESOURCE_NAME): obj for obj in objects if obj.get("kind") != "App"}

    mine, theirs = by_name(mini[1:]), by_name(real)
    assert sorted(mine) == sorted(theirs) == ["deployment", "service"], (sorted(mine), sorted(theirs))
    for name in mine:
        a, b = mine[name], theirs[name]
        assert (a["apiVersion"], a["kind"]) == (b["apiVersion"], b["kind"]), (a, b)
        assert b["metadata"]["labels"].get("crossplane.io/composite") == "checkout", b["metadata"]
        if with_status == "spec":
            assert a["spec"] == b["spec"], (name, a["spec"], b["spec"])
            assert a["metadata"]["labels"]["team"] == b["metadata"]["labels"]["team"], (a["metadata"], b["metadata"])
    if with_status == "spec":
        assert mine["deployment"]["metadata"]["annotations"]["platform.example.org/description"] == theirs["deployment"]["metadata"]["annotations"]["platform.example.org/description"]
    assert condition(mini[0])["status"] == condition(real_xr)["status"], (mini[0]["status"], real_xr.get("status"))
    if with_status == "status":
        for key in ("address", "availableReplicas"):
            assert mini[0]["status"][key] == real_xr["status"][key], (key, mini[0]["status"], real_xr["status"])
    print("  minixp == crossplane composition render: %s, Ready=%s" % ("same composed specs and labels" if with_status == "spec" else "same composed resources and XR status", condition(real_xr)["status"]))


COMMANDS = {
    "first-preview": first_preview, "outputs": outputs, "same-graph": same_graph, "second-preview": second_preview,
    "pet-kept": pet_kept, "empty": empty, "offline-render": offline_render, "for-create": for_create,
    "observed": observed, "observed-render": observed_render, "compare-render": compare_render,
}
COMMANDS[sys.argv[1]](*sys.argv[2:])
PY
check() { python3 "$WORK/check.py" "$@"; }

echo "▸ minixp renders the composition offline as a pure function"
python3 "$MAIN" xp render "$EXAMPLES/xp/xr.json" "$EXAMPLES/xp/composition.json" > "$WORK/xp/table.txt" || fail "minixp render failed"
grep -q "Ready=False Creating: Unready resources: deployment, service" "$WORK/xp/table.txt" || fail "minixp table: $(cat "$WORK/xp/table.txt")"
python3 "$MAIN" xp render "$EXAMPLES/xp/xr.json" "$EXAMPLES/xp/composition.json" --json > "$WORK/xp/mini-offline.json"
python3 "$MAIN" xp render "$EXAMPLES/xp/xr.json" "$EXAMPLES/xp/composition.json" --json > "$WORK/xp/mini-offline-2.json"
cmp -s "$WORK/xp/mini-offline.json" "$WORK/xp/mini-offline-2.json" || fail "two renders of the same inputs differ"
check offline-render "$WORK/xp/mini-offline.json" || fail "offline render content"

echo "▸ minipulumi runs the example program: preview, up, config change, destroy"
MINI="$WORK/mini"
mkdir -p "$MINI"
cp -R "$EXAMPLES/pulumi-app/." "$MINI/"
mp() { local cmd="$1"; shift; python3 "$MAIN" pulumi "$cmd" --cwd "$MINI" "$@"; }
mp preview > "$WORK/mini-preview.txt"
grep -q "+ 4 to create" "$WORK/mini-preview.txt" || fail "minipulumi preview: $(cat "$WORK/mini-preview.txt")"
grep -q "name: \[unknown\]" "$WORK/mini-preview.txt" || fail "preview should show unknown outputs: $(cat "$WORK/mini-preview.txt")"
mp up > "$WORK/mini-up.txt"
grep -q "+ 4 created" "$WORK/mini-up.txt" || fail "minipulumi up: $(cat "$WORK/mini-up.txt")"
mp output --json > "$WORK/mini-out1.json"
check outputs "$WORK/mini-out1.json" 6 || fail "minipulumi outputs"
printf 'config:\n  ccsmoke:suffixLength: "8"\n' > "$MINI/Pulumi.dev.yaml"
mp preview --json > "$WORK/mini-preview2.json"
check second-preview "$WORK/mini-preview2.json" || fail "minipulumi second preview"
mp up > /dev/null
mp output --json > "$WORK/mini-out2.json"
check pet-kept "$WORK/mini-out1.json" "$WORK/mini-out2.json" || fail "minipulumi replacement kept the wrong resources"
mp destroy > "$WORK/mini-destroy.txt"
grep -q -- "- 4 deleted" "$WORK/mini-destroy.txt" || fail "minipulumi destroy: $(cat "$WORK/mini-destroy.txt")"
mp export > "$WORK/mini-export-empty.json"
check empty "$WORK/mini-export-empty.json" || fail "minipulumi destroy left resources"

echo "▸ install the Pulumi CLI $PULUMI_VERSION (checksum) and the Python SDK in a venv"
PULUMI_TGZ="pulumi-${PULUMI_VERSION}-linux-${PULUMI_ARCH}.tar.gz"
curl -fsSL --retry 3 -o "$WORK/$PULUMI_TGZ" "https://github.com/pulumi/pulumi/releases/download/${PULUMI_VERSION}/${PULUMI_TGZ}" || fail "download $PULUMI_TGZ"
echo "${PULUMI_SHA256}  $WORK/$PULUMI_TGZ" | sha256sum -c - >/dev/null || fail "$PULUMI_TGZ checksum mismatch"
tar -xzf "$WORK/$PULUMI_TGZ" -C "$WORK/bin"
export PATH="$WORK/bin/pulumi:$WORK/bin:$PATH"
export PULUMI_HOME="$WORK/pulumi-home" PULUMI_CONFIG_PASSPHRASE="cc-smoke" PULUMI_SKIP_UPDATE_CHECK="true"
[[ "$(pulumi version)" == "$PULUMI_VERSION" ]] || fail "pulumi version is $(pulumi version), expected $PULUMI_VERSION"
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install -q --disable-pip-version-check "pulumi==${PULUMI_VERSION#v}" "pulumi-random==${PULUMI_RANDOM_VERSION}" > "$WORK/pip.log" 2>&1 || fail "pip install: $(tail -20 "$WORK/pip.log")"

echo "▸ Use It: pulumi preview, up and stack output on a local backend"
REAL="$WORK/real"
mkdir -p "$REAL" "$WORK/backend"
cp "$EXAMPLES/pulumi-app/__main__.py" "$REAL/"
cat > "$REAL/Pulumi.yaml" <<EOF
name: ccsmoke
runtime:
  name: python
  options:
    toolchain: pip
    virtualenv: $WORK/venv
EOF
cd "$REAL"
pulumi login "file://$WORK/backend" > /dev/null
pulumi stack init dev --non-interactive > /dev/null
pulumi preview --json --non-interactive > "$WORK/real-preview1.json" 2> "$WORK/real-preview1.err" || fail "pulumi preview: $(tail -20 "$WORK/real-preview1.err")"
python3 "$MAIN" pulumi preview --cwd "$REAL" --json > "$WORK/mini-real-preview1.json"
check first-preview "$WORK/real-preview1.json" "$WORK/mini-real-preview1.json" || fail "first previews differ"
pulumi up --yes --non-interactive --skip-preview > "$WORK/real-up1.txt" 2>&1 || fail "pulumi up: $(tail -20 "$WORK/real-up1.txt")"
grep -qE "\+ 4 created" "$WORK/real-up1.txt" || fail "pulumi up summary: $(tail -20 "$WORK/real-up1.txt")"
pulumi stack output --json > "$WORK/real-out1.json"
check outputs "$WORK/real-out1.json" 6 || fail "pulumi stack output"

echo "▸ minipulumi on the same program and config produces the same resource graph"
python3 "$MAIN" pulumi up --cwd "$REAL" > /dev/null
pulumi stack export > "$WORK/real-export1.json"
python3 "$MAIN" pulumi export --cwd "$REAL" > "$WORK/mini-export1.json"
check same-graph "$WORK/real-export1.json" "$WORK/mini-export1.json" || fail "resource graphs differ"

echo "▸ pulumi config set: suffix and port are replaced, pet is kept, in both engines"
pulumi config set suffixLength 8 > /dev/null
grep -q "suffixLength" "$REAL/Pulumi.dev.yaml" || fail "pulumi config set did not write Pulumi.dev.yaml: $(cat "$REAL/Pulumi.dev.yaml")"
pulumi preview --json --non-interactive > "$WORK/real-preview2.json" 2> "$WORK/real-preview2.err" || fail "pulumi preview 2: $(tail -20 "$WORK/real-preview2.err")"
check second-preview "$WORK/real-preview2.json" || fail "pulumi second preview"
python3 "$MAIN" pulumi preview --cwd "$REAL" --json > "$WORK/mini-real-preview2.json"
check second-preview "$WORK/mini-real-preview2.json" || fail "minipulumi second preview on the shared config"
pulumi up --yes --non-interactive --skip-preview > "$WORK/real-up2.txt" 2>&1 || fail "pulumi up 2: $(tail -20 "$WORK/real-up2.txt")"
pulumi stack output --json > "$WORK/real-out2.json"
check pet-kept "$WORK/real-out1.json" "$WORK/real-out2.json" || fail "pulumi replacement"
python3 "$MAIN" pulumi output --cwd "$REAL" --json > "$WORK/mini-real-out1.json"
python3 "$MAIN" pulumi up --cwd "$REAL" > /dev/null
python3 "$MAIN" pulumi output --cwd "$REAL" --json > "$WORK/mini-real-out2.json"
check pet-kept "$WORK/mini-real-out1.json" "$WORK/mini-real-out2.json" || fail "minipulumi replacement on the shared config"

echo "▸ destroy in both engines leaves nothing behind"
pulumi destroy --yes --non-interactive --skip-preview > "$WORK/real-destroy.txt" 2>&1 || fail "pulumi destroy: $(tail -20 "$WORK/real-destroy.txt")"
grep -q "4 deleted" "$WORK/real-destroy.txt" || fail "pulumi destroy summary: $(tail -20 "$WORK/real-destroy.txt")"
pulumi stack export > "$WORK/real-export-empty.json"
check empty "$WORK/real-export-empty.json" || fail "pulumi destroy left resources"
python3 "$MAIN" pulumi destroy --cwd "$REAL" > /dev/null
python3 "$MAIN" pulumi export --cwd "$REAL" > "$WORK/mini-real-export-empty.json"
check empty "$WORK/mini-real-export-empty.json" || fail "minipulumi destroy left resources"
pulumi stack rm --yes dev > /dev/null
cd "$LESSON"

if ! command -v kubectl >/dev/null || ! kubectl cluster-info >/dev/null 2>&1; then
  echo "  (no reachable cluster; applying the rendered resources skipped)"
  echo "SMOKE OK: iac/pulumi-crossplane (no cluster)"
  exit 0
fi
if kubectl config get-contexts -o name 2>/dev/null | grep -qx kind-cc-smoke; then CTX_ARGS=(--context kind-cc-smoke); fi

echo "▸ the rendered Deployment and Service run on kind; their observed state flows back into the XR"
k delete namespace "$NS" --ignore-not-found --wait=true --timeout=60s >/dev/null 2>&1 || true
k create namespace "$NS" >/dev/null
NS_CREATED=1
check for-create "$WORK/xp/mini-offline.json" "$WORK/xp/create.json"
k create -f "$WORK/xp/create.json" >/dev/null || fail "kubectl create of the rendered resources"
DEPLOY="$(k get deployment -n "$NS" -l crossplane.io/composite=checkout -o jsonpath='{.items[0].metadata.name}')"
[[ "$DEPLOY" == checkout-* ]] || fail "generateName should give checkout-*, got '$DEPLOY'"
k rollout status deployment/"$DEPLOY" -n "$NS" --timeout=180s >/dev/null || fail "rendered deployment did not become available"
CLUSTER_IP="$(k get service -n "$NS" -l crossplane.io/composite=checkout -o jsonpath='{.items[0].spec.clusterIP}')"
[[ -n "$CLUSTER_IP" ]] || fail "service has no clusterIP"
k get deployment,service -n "$NS" -l crossplane.io/composite=checkout -o json > "$WORK/xp/kube.json"
check observed "$WORK/xp/kube.json" "$WORK/xp/observed.yaml" || fail "observed resources"
python3 "$MAIN" xp render "$EXAMPLES/xp/xr.json" "$EXAMPLES/xp/composition.json" --observed "$WORK/xp/observed.yaml" --json > "$WORK/xp/mini-observed.json" || fail "minixp render with observed resources"
check observed-render "$WORK/xp/mini-observed.json" "$CLUSTER_IP" || fail "status did not flow back"

XP=""
if ! command -v docker >/dev/null || ! docker info >/dev/null 2>&1; then
  echo "  (docker not available; crossplane composition render comparison skipped)"
elif curl -fsSL --retry 3 -o "$WORK/bin/crossplane" "https://cli.crossplane.io/stable/${XP_CLI_VERSION}/bin/${XP_ARCH}/crossplane"; then
  echo "${XP_SHA256}  $WORK/bin/crossplane" | sha256sum -c - >/dev/null || fail "crossplane CLI checksum mismatch"
  chmod +x "$WORK/bin/crossplane"
  XP="$WORK/bin/crossplane"
else
  echo "  (crossplane CLI $XP_CLI_VERSION could not be downloaded; crossplane composition render comparison skipped)"
fi
if [[ -n "$XP" ]]; then
  echo "▸ Use It: crossplane composition render (CLI $XP_CLI_VERSION, engine image $XP_CORE_VERSION) agrees with minixp"
  if docker pull -q "xpkg.crossplane.io/crossplane/crossplane:${XP_CORE_VERSION}" >/dev/null 2>&1 && docker pull -q "$PT_IMAGE" >/dev/null 2>&1; then
    XP_ARGS=(--crossplane-version="$XP_CORE_VERSION" --timeout=3m)
    "$XP" composition render "$EXAMPLES/xp/xr.json" "$EXAMPLES/xp/composition.json" "$EXAMPLES/xp/functions.yaml" "${XP_ARGS[@]}" > "$WORK/xp/real-offline.yaml" 2> "$WORK/xp/real-offline.err" || fail "crossplane composition render: $(tail -20 "$WORK/xp/real-offline.err")"
    "$WORK/venv/bin/python" "$WORK/check.py" compare-render "$WORK/xp/mini-offline.json" "$WORK/xp/real-offline.yaml" spec || fail "offline renders differ: $(cat "$WORK/xp/real-offline.yaml")"
    "$XP" composition render "$EXAMPLES/xp/xr.json" "$EXAMPLES/xp/composition.json" "$EXAMPLES/xp/functions.yaml" --observed-resources="$WORK/xp/observed.yaml" "${XP_ARGS[@]}" > "$WORK/xp/real-observed.yaml" 2> "$WORK/xp/real-observed.err" || fail "crossplane composition render with observed resources: $(tail -20 "$WORK/xp/real-observed.err")"
    "$WORK/venv/bin/python" "$WORK/check.py" compare-render "$WORK/xp/mini-observed.json" "$WORK/xp/real-observed.yaml" status || fail "observed renders differ: $(cat "$WORK/xp/real-observed.yaml")"
  else
    echo "  (could not pull the Crossplane engine or function image; crossplane composition render comparison skipped)"
  fi
fi

echo "SMOKE OK: iac/pulumi-crossplane"
