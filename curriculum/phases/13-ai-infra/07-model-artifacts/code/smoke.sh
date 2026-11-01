#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
REG_NAME="cc-model-artifacts-registry"
LLAMA_NAME="cc-model-artifacts-llama"
REG_PORT=5077
LLAMA_PORT=18077
REG="127.0.0.1:$REG_PORT"
cleanup() {
  if command -v docker >/dev/null; then
    docker rm -f "$REG_NAME" "$LLAMA_NAME" >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

GGUF_REPO="bartowski/SmolLM2-135M-Instruct-GGUF"
GGUF_REV="09816acd5d99df7be770d85ea30822623dab342c"
GGUF_FILE="SmolLM2-135M-Instruct-Q8_0.gguf"
GGUF_SHA="5a1395716f7913741cc51d98581b9b1228d80987a9f7d3664106742eb06bba83"
BASE_REPO="HuggingFaceTB/SmolLM2-135M-Instruct"
BASE_REV="12fd25f77366fa6b3b4b768ec3050bf629380bac"
ST_REPO="EleutherAI/pythia-14m"
ST_REV="cf967c0a9a04383db6f7b1108d86b2962634b4ac"
ST_SHA="116a02532db461f91386a5b20f942ff2c8d4de7341e21b55caafc3d7b25f49a1"
PARAMS=134515008
TENSOR_BYTES=143025408
ST_PARAMS=14067712
LLAMA_IMAGE="ghcr.io/ggml-org/llama.cpp:server-b11706@sha256:f41d47eee678c02656eefc9ed8cce76cab584aa7032805eeef0e6639fcfda485"
REGISTRY_IMAGE="registry:2.8.3"
ORAS_VERSION="1.3.5"
ARTIFACT_TYPE="application/vnd.cncf.model.manifest.v1+json"
CONFIG_TYPE="application/vnd.cncf.model.config.v1+json"
WEIGHT_TYPE="application/vnd.cncf.model.weight.v1.raw"
WEIGHT_CONFIG_TYPE="application/vnd.cncf.model.weight.config.v1.raw"

mi() { python3 code/main.py "$@"; }
jget() {
  python3 - "$@" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2:]:
    value = value[int(key)] if isinstance(value, list) else value[key]
print(json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value)
PY
}
wait_http() {
  local url="$1" deadline=$((SECONDS + $2))
  while (( SECONDS < deadline )); do
    if curl -sf -o /dev/null "$url"; then return 0; fi
    sleep 1
  done
  return 1
}

echo "▸ modelinspect fetch: the GGUF at a pinned revision, checked against the pinned and the Hub's sha256"
mi fetch "$GGUF_REPO" "$GGUF_FILE" --revision "$GGUF_REV" --sha256 "$GGUF_SHA" --dest "$WORK/model" --json > "$WORK/fetch.json" 2> "$WORK/fetch.err" || fail "fetch $GGUF_FILE: $(cat "$WORK/fetch.err")"
[[ "$(jget "$WORK/fetch.json" commit)" == "$GGUF_REV" ]] || fail "Hub resolved another commit: $(cat "$WORK/fetch.json")"
[[ "$(jget "$WORK/fetch.json" digest)" == "sha256:$GGUF_SHA" ]] || fail "digest: $(cat "$WORK/fetch.json")"
sha256sum "$WORK/model/$GGUF_FILE" > "$WORK/sum.txt"
grep -q "^$GGUF_SHA " "$WORK/sum.txt" || fail "sha256sum disagrees: $(cat "$WORK/sum.txt")"
mi fetch "$BASE_REPO" config.json --revision "$BASE_REV" --dest "$WORK/model" --json > "$WORK/fetch-config.json" 2> "$WORK/fetch-config.err" || fail "fetch config.json: $(cat "$WORK/fetch-config.err")"
[[ "$(jget "$WORK/fetch-config.json" verified_against)" == *"git blob id"* ]] || fail "config.json should be verified by its git blob id: $(cat "$WORK/fetch-config.json")"
echo "  $GGUF_FILE $(jget "$WORK/fetch.json" size) bytes at $GGUF_REV, $(jget "$WORK/fetch.json" verified_against)"

echo "▸ a pin that disagrees with the Hub downloads nothing and exits 1"
CODE=0
mi fetch "$GGUF_REPO" SmolLM2-135M-Instruct-Q4_K_M.gguf --revision "$GGUF_REV" --sha256 "$GGUF_SHA" --dest "$WORK/wrong" > /dev/null 2> "$WORK/wrong.err" || CODE=$?
[[ "$CODE" == "1" ]] && grep -q "nothing was downloaded" "$WORK/wrong.err" && [[ ! -e "$WORK/wrong/SmolLM2-135M-Instruct-Q4_K_M.gguf" ]] || fail "wrong pin: exit $CODE, $(cat "$WORK/wrong.err")"

echo "▸ modelinspect inspect: GGUF header, parameters and bytes per block format"
mi inspect "$WORK/model/$GGUF_FILE" --json --tensors > "$WORK/gguf.json" || fail "inspect gguf"
mi inspect "$WORK/model/$GGUF_FILE" > "$WORK/gguf.txt"
sed -n '1,9p' "$WORK/gguf.txt"
python3 - "$WORK/gguf.json" "$PARAMS" "$TENSOR_BYTES" <<'PY' || fail "GGUF inspection: $(head -c 2000 "$WORK/gguf.json")"
import json, sys
d = json.load(open(sys.argv[1]))
params, tensor_bytes = int(sys.argv[2]), int(sys.argv[3])
assert (d["format"], d["version"], d["file_type"]) == ("gguf", 3, "MOSTLY_Q8_0"), (d["format"], d["version"], d["file_type"])
assert d["parameters"] == params, d["parameters"]
assert d["tensor_bytes"] == tensor_bytes, d["tensor_bytes"]
assert d["tensor_count"] == 272 and d["dtypes"]["Q8_0"]["tensors"] == 211 and d["dtypes"]["F32"]["tensors"] == 61, d["dtypes"]
s = d["shape"]
got = (s["architecture"], s["layers"], s["embedding"], s["heads"], s["kv_heads"], s["key_dim"], s["context_length"], s["vocab"])
assert got == ("llama", 30, 576, 9, 3, 64, 8192, 49152), got
assert s["kv_bytes_per_token_f16"] == 23040, s
PY
mi memory "$WORK/model/$GGUF_FILE" --ctx 8192 --json > "$WORK/memory.json" || fail "memory"
[[ "$(jget "$WORK/memory.json" kv_cache bytes)" == "188743680" ]] || fail "KV cache at 8192 tokens should be 180 MiB: $(cat "$WORK/memory.json")"

echo "▸ the Hub's own GGUF summary (the model card numbers) agrees"
curl -sSf "https://huggingface.co/api/models/$GGUF_REPO/revision/$GGUF_REV?expand%5B%5D=gguf" -o "$WORK/api-gguf.json" || fail "Hub API for $GGUF_REPO"
python3 - "$WORK/api-gguf.json" "$WORK/gguf.json" <<'PY' || fail "Hub GGUF summary disagrees: $(head -c 600 "$WORK/api-gguf.json")"
import json, sys
hub = json.load(open(sys.argv[1]))["gguf"]
mine = json.load(open(sys.argv[2]))
assert hub["total"] == mine["parameters"], (hub["total"], mine["parameters"])
assert hub["architecture"] == mine["shape"]["architecture"] and hub["context_length"] == mine["shape"]["context_length"], hub
print("  Hub: %s parameters, %s, context %s" % (hub["total"], hub["architecture"], hub["context_length"]))
PY

echo "▸ the publisher's BF16 safetensors of the same model, inspected over HTTP Range without downloading it"
mi inspect "https://huggingface.co/$BASE_REPO/resolve/$BASE_REV/model.safetensors" --config "$WORK/model/config.json" --json > "$WORK/remote.json" 2> "$WORK/remote.err" || fail "remote inspect: $(cat "$WORK/remote.err")"
curl -sSf "https://huggingface.co/api/models/$BASE_REPO/revision/$BASE_REV?expand%5B%5D=safetensors" -o "$WORK/api-st.json" || fail "Hub API for $BASE_REPO"
python3 - "$WORK/remote.json" "$WORK/api-st.json" "$PARAMS" <<'PY' || fail "remote safetensors: $(head -c 1500 "$WORK/remote.json")"
import json, sys
d = json.load(open(sys.argv[1]))
hub = json.load(open(sys.argv[2]))["safetensors"]
params = int(sys.argv[3])
assert d["format"] == "safetensors" and d["parameters"] == params == hub["total"], (d["parameters"], hub)
assert list(d["dtypes"]) == ["BF16"] and d["tensor_bytes"] == 2 * params, d["dtypes"]
assert d["reads"]["bytes"] <= 1 << 20 and d["reads"]["bytes"] < d["file_size"] // 100, d["reads"]
assert d["shape"]["kv_bytes_per_token_f16"] == 23040, d["shape"]
print("  %d parameters in %d bytes, read %d bytes in %d request(s); same count as the GGUF" % (d["parameters"], d["file_size"], d["reads"]["bytes"], d["reads"]["requests"]))
PY

echo "▸ Use It: hf download by revision, the safetensors library and gguf-dump in a venv"
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install -q --disable-pip-version-check 'huggingface_hub==2.2.0' 'safetensors==0.8.0' 'numpy==2.2.6' 'gguf==0.17.1' > "$WORK/pip.log" 2>&1 || fail "pip install: $(tail -20 "$WORK/pip.log")"
HF_HUB_DISABLE_PROGRESS_BARS=1 "$WORK/venv/bin/hf" download "$ST_REPO" model.safetensors config.json --revision "$ST_REV" --local-dir "$WORK/pythia" > "$WORK/hf.log" 2>&1 || fail "hf download: $(tail -20 "$WORK/hf.log")"
mi digest "$WORK/pythia/model.safetensors" --expect "$ST_SHA" > "$WORK/pythia-digest.txt" || fail "hf download produced another file: $(cat "$WORK/pythia-digest.txt")"
mi inspect "$WORK/pythia/model.safetensors" --json > "$WORK/pythia.json" || fail "inspect pythia"
"$WORK/venv/bin/python" - "$WORK/pythia/model.safetensors" "$WORK/pythia.json" "$ST_PARAMS" <<'PY' || fail "safetensors library disagrees with modelinspect"
import json, math, sys
from safetensors import safe_open
path, mine, expected = sys.argv[1], json.load(open(sys.argv[2])), int(sys.argv[3])
with safe_open(path, framework="numpy") as handle:
    names = list(handle.keys())
    total = sum(math.prod(handle.get_slice(name).get_shape()) for name in names)
assert total == mine["parameters"] == expected, (total, mine["parameters"], expected)
assert len(names) == mine["tensor_count"], (len(names), mine["tensor_count"])
assert mine["shape"]["layers"] == 6 and list(mine["dtypes"]) == ["F16"], mine["shape"]
print("  pythia-14m: %d parameters in %d tensors (safetensors library and modelinspect)" % (total, len(names)))
PY
"$WORK/venv/bin/gguf-dump" --json "$WORK/model/$GGUF_FILE" > "$WORK/dump.json" 2> "$WORK/dump.err" || fail "gguf-dump: $(tail -20 "$WORK/dump.err")"
python3 - "$WORK/dump.json" "$WORK/gguf.json" <<'PY' || fail "gguf-dump disagrees with modelinspect"
import json, math, sys
dump, mine = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
theirs = dump["tensors"]
assert dump["metadata"]["GGUF.kv_count"]["value"] == mine["metadata_count"], (dump["metadata"]["GGUF.kv_count"], mine["metadata_count"])
assert set(theirs) == {t["name"] for t in mine["tensors"]}, "tensor names differ"
for tensor in mine["tensors"]:
    other = theirs[tensor["name"]]
    assert [int(x) for x in other["shape"]] == tensor["shape"] and other["type"] == tensor["dtype"], (tensor, other)
total = sum(math.prod(int(x) for x in t["shape"]) for t in theirs.values())
assert total == mine["parameters"], (total, mine["parameters"])
print("  gguf-dump: %d tensors, %d metadata keys, %d parameters, every shape and type identical" % (len(theirs), mine["metadata_count"], total))
PY

if ! command -v docker >/dev/null; then
  echo "  (docker not installed; registry, oras and llama.cpp checks skipped)"
  echo "SMOKE OK: ai-infra/model-artifacts (modelinspect and Hub checks only)"
  exit 0
fi

echo "▸ a local registry ($REGISTRY_IMAGE) on $REG"
docker rm -f "$REG_NAME" "$LLAMA_NAME" >/dev/null 2>&1 || true
docker run -d --name "$REG_NAME" -p "$REG:5000" "$REGISTRY_IMAGE" > /dev/null || fail "start $REGISTRY_IMAGE"
wait_http "http://$REG/v2/" 60 || fail "registry did not answer: $(docker logs "$REG_NAME" 2>&1 | tail -20)"

echo "▸ oras $ORAS_VERSION, checksum verified against the pinned and the published value"
case "$(uname -m)" in
  x86_64) ORAS_ARCH=amd64; ORAS_SHA="f48a73ea6d026a862c3919fc773da9a8c86fc259c2c737d5baed3b8b9614ad22" ;;
  aarch64) ORAS_ARCH=arm64; ORAS_SHA="d5d4e5afd5c0781c93eda5ea1976e654e1fdee202c1ddaeaf5f91a8c4fd67d46" ;;
  *) fail "no pinned oras checksum for $(uname -m)" ;;
esac
ORAS_TGZ="oras_${ORAS_VERSION}_linux_${ORAS_ARCH}.tar.gz"
mkdir -p "$WORK/bin"
curl -sSfL -o "$WORK/$ORAS_TGZ" "https://github.com/oras-project/oras/releases/download/v$ORAS_VERSION/$ORAS_TGZ" || fail "download $ORAS_TGZ"
curl -sSfL -o "$WORK/oras-checksums.txt" "https://github.com/oras-project/oras/releases/download/v$ORAS_VERSION/oras_${ORAS_VERSION}_checksums.txt" || fail "download oras checksums"
echo "$ORAS_SHA  $WORK/$ORAS_TGZ" | sha256sum -c - > /dev/null || fail "$ORAS_TGZ does not match the pinned sha256"
grep -q "^$ORAS_SHA  $ORAS_TGZ$" "$WORK/oras-checksums.txt" || fail "published checksum differs from the pinned one: $(grep "$ORAS_TGZ" "$WORK/oras-checksums.txt")"
tar -xzf "$WORK/$ORAS_TGZ" -C "$WORK/bin" oras
ORAS="$WORK/bin/oras"
"$ORAS" version > "$WORK/oras-version.txt"
head -1 "$WORK/oras-version.txt"

echo "▸ the ModelPack config modelinspect would push, then oras push with the same config and media types"
mi push "$REG/models/smollm2:q8_0" "$WORK/model/$GGUF_FILE" "$WORK/model/config.json" --revision "$GGUF_REV" --dry-run --config-out "$WORK/model/modelpack-config.json" > "$WORK/dry.json" || fail "dry run"
(cd "$WORK/model" && "$ORAS" push --plain-http "$REG/models/smollm2:q8_0-oras" \
  --artifact-type "$ARTIFACT_TYPE" \
  --config "modelpack-config.json:$CONFIG_TYPE" \
  "$GGUF_FILE:$WEIGHT_TYPE" \
  "config.json:$WEIGHT_CONFIG_TYPE") > "$WORK/oras-push.log" 2>&1 || fail "oras push: $(tail -20 "$WORK/oras-push.log")"
curl -sSf -H "Accept: application/vnd.oci.image.manifest.v1+json" -D "$WORK/oras-manifest.headers" -o "$WORK/oras-manifest.json" "http://$REG/v2/models/smollm2/manifests/q8_0-oras" || fail "GET the oras manifest"
ORAS_DIGEST="$(python3 -c 'import hashlib, sys; print("sha256:" + hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$WORK/oras-manifest.json")"
grep -qi "^docker-content-digest: $ORAS_DIGEST" "$WORK/oras-manifest.headers" || fail "registry digest header does not match the manifest bytes: $(cat "$WORK/oras-manifest.headers")"
"$ORAS" manifest fetch --plain-http "$REG/models/smollm2:q8_0-oras" > "$WORK/oras-manifest.cli.json" || fail "oras manifest fetch"
python3 - "$WORK/oras-manifest.json" "$WORK/oras-manifest.cli.json" <<'PY' || fail "oras manifest fetch and the registry disagree"
import json, sys
assert json.load(open(sys.argv[1])) == json.load(open(sys.argv[2]))
PY
echo "  oras pushed models/smollm2:q8_0-oras = $ORAS_DIGEST"

echo "▸ modelinspect push to the same repository: every blob already exists, nothing is uploaded"
mi push "$REG/models/smollm2:q8_0" "$WORK/model/$GGUF_FILE" "$WORK/model/config.json" --revision "$GGUF_REV" --plain-http --json > "$WORK/mi-push.json" 2> "$WORK/mi-push.err" || fail "modelinspect push: $(cat "$WORK/mi-push.err")"
mi push "$REG/models/smollm2:q8_0-again" "$WORK/model/$GGUF_FILE" "$WORK/model/config.json" --revision "$GGUF_REV" --plain-http --json > "$WORK/mi-push-again.json" || fail "second modelinspect push"
MI_DIGEST="$(jget "$WORK/mi-push.json" digest)"
python3 - "$WORK/mi-push.json" "$WORK/mi-push-again.json" "$WORK/dry.json" <<'PY' || fail "modelinspect push: $(cat "$WORK/mi-push.json")"
import json, sys
first, again, dry = (json.load(open(p)) for p in sys.argv[1:4])
assert first["uploaded"] == [] and len(first["skipped"]) == 3 and first["uploaded_bytes"] == 0, (first["uploaded"], first["skipped"])
assert first["digest"] == again["digest"] == dry["digest"], (first["digest"], again["digest"], dry["digest"])
print("  0 bytes uploaded, 3 blobs already present; manifest %s is reproducible" % first["digest"])
PY

echo "▸ both manifests reference the same blob digests, and the GGUF layer is the Hub's LFS sha256"
curl -sSf -H "Accept: application/vnd.oci.image.manifest.v1+json" -o "$WORK/mi-manifest.json" "http://$REG/v2/models/smollm2/manifests/$MI_DIGEST" || fail "GET modelinspect's manifest"
python3 - "$WORK/oras-manifest.json" "$WORK/mi-manifest.json" "$GGUF_SHA" "$ARTIFACT_TYPE" "$CONFIG_TYPE" "$ORAS_DIGEST" "$MI_DIGEST" <<'PY' || fail "manifests disagree: oras $(cat "$WORK/oras-manifest.json") modelinspect $(cat "$WORK/mi-manifest.json")"
import json, sys
oras, mine = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
gguf_sha, artifact_type, config_type, oras_digest, mi_digest = sys.argv[3:8]
for manifest in (oras, mine):
    assert manifest["artifactType"] == artifact_type and manifest["config"]["mediaType"] == config_type, manifest
assert oras["config"]["digest"] == mine["config"]["digest"], (oras["config"], mine["config"])
assert sorted(l["digest"] for l in oras["layers"]) == sorted(l["digest"] for l in mine["layers"]), "layer digests differ"
weights = [l for l in mine["layers"] if l["annotations"]["org.opencontainers.image.title"].endswith(".gguf")]
assert weights[0]["digest"] == "sha256:" + gguf_sha, weights
assert oras_digest != mi_digest, "the manifests were expected to differ as documents"
print("  config %s and %d layers identical; manifests differ (%s vs %s): annotations %s" % (mine["config"]["digest"][:19], len(mine["layers"]), oras_digest[:19], mi_digest[:19], sorted((oras.get("annotations") or {}).keys())))
PY

echo "▸ pull across tools: oras pulls modelinspect's artifact, modelinspect pulls oras's, both by digest"
"$ORAS" pull --plain-http -o "$WORK/from-oras" "$REG/models/smollm2@$MI_DIGEST" > "$WORK/oras-pull.log" 2>&1 || fail "oras pull: $(tail -20 "$WORK/oras-pull.log")"
sha256sum "$WORK/from-oras/$GGUF_FILE" > "$WORK/from-oras.sum"
grep -q "^$GGUF_SHA " "$WORK/from-oras.sum" || fail "oras pulled other bytes: $(cat "$WORK/from-oras.sum")"
mi pull "$REG/models/smollm2@$ORAS_DIGEST" --dest "$WORK/served" --plain-http --json > "$WORK/mi-pull.json" 2> "$WORK/mi-pull.err" || fail "modelinspect pull: $(cat "$WORK/mi-pull.err")"
python3 - "$WORK/mi-pull.json" "$GGUF_SHA" "$GGUF_REV" <<'PY' || fail "modelinspect pull: $(cat "$WORK/mi-pull.json")"
import json, sys
d, gguf_sha, revision = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3]
config = d["modelConfig"]
assert config["config"] == {"format": "gguf", "paramSize": "134.5M", "quantization": "q8_0"}, config
assert config["descriptor"]["revision"] == revision, config["descriptor"]
assert {f["file"] for f in d["files"]} == {"SmolLM2-135M-Instruct-Q8_0.gguf", "config.json"}, d["files"]
assert any(f["digest"] == "sha256:" + gguf_sha for f in d["files"]), d["files"]
print("  pulled %s: %s, paramSize %s, revision %s" % (d["digest"][:19], ", ".join(f["file"] for f in d["files"]), config["config"]["paramSize"], revision[:7]))
PY

echo "▸ llama.cpp serves the pulled file and reports the same model ($LLAMA_IMAGE)"
chmod a+rx "$WORK" "$WORK/served"
chmod a+r "$WORK/served/$GGUF_FILE"
docker pull -q "$LLAMA_IMAGE" > /dev/null || fail "pull $LLAMA_IMAGE"
docker run -d --name "$LLAMA_NAME" -p "127.0.0.1:$LLAMA_PORT:8080" -v "$WORK/served:/models:ro" "$LLAMA_IMAGE" \
  -m "/models/$GGUF_FILE" -c 2048 -np 1 --host 0.0.0.0 --port 8080 > /dev/null || fail "start llama.cpp"
wait_http "http://127.0.0.1:$LLAMA_PORT/health" 180 || fail "llama.cpp did not become ready: $(docker logs "$LLAMA_NAME" 2>&1 | tail -30)"
curl -sSf "http://127.0.0.1:$LLAMA_PORT/v1/models" -o "$WORK/v1-models.json" || fail "GET /v1/models"
python3 - "$WORK/v1-models.json" "$WORK/gguf.json" <<'PY' || fail "llama.cpp's report disagrees: $(cat "$WORK/v1-models.json")"
import json, sys
models, mine = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
def find(node):
    if isinstance(node, dict):
        if "n_params" in node:
            return node
        node = list(node.values())
    if isinstance(node, list):
        for item in node:
            found = find(item)
            if found:
                return found
    return None
meta = find(models)
assert meta is not None, "no meta with n_params in /v1/models"
assert meta["n_params"] == mine["parameters"], (meta["n_params"], mine["parameters"])
assert meta["size"] == mine["tensor_bytes"], (meta["size"], mine["tensor_bytes"])
assert meta["n_embd"] == mine["shape"]["embedding"] and meta["n_vocab"] == mine["shape"]["vocab"] and meta["n_ctx_train"] == mine["shape"]["context_length"], meta
print("  llama.cpp: n_params %d, size %d, n_embd %d, n_vocab %d, n_ctx_train %d" % (meta["n_params"], meta["size"], meta["n_embd"], meta["n_vocab"], meta["n_ctx_train"]))
PY
docker logs "$LLAMA_NAME" > "$WORK/llama.log" 2>&1 || true
python3 - "$WORK/llama.log" "$WORK/served/$GGUF_FILE" <<'PY' || fail "KV cache size disagrees with modelinspect memory"
import json, re, subprocess, sys
log = open(sys.argv[1], encoding="utf-8", errors="replace").read()
match = re.search(r"size\s*=\s*([0-9.]+)\s*MiB\s*\(\s*(\d+)\s*cells", log)
if not match:
    print("  (no KV cache size line in this llama.cpp build's log; comparison skipped)")
    raise SystemExit(0)
mib, cells = float(match.group(1)), int(match.group(2))
report = json.loads(subprocess.check_output([sys.executable, "code/main.py", "memory", sys.argv[2], "--ctx", str(cells), "--json"]))
mine = report["kv_cache"]["bytes"] / (1 << 20)
assert abs(mine - mib) < 0.01, (mine, mib, match.group(0))
print("  KV cache: llama.cpp %.2f MiB for %d cells, modelinspect %.2f MiB" % (mib, cells, mine))
PY
curl -sSf "http://127.0.0.1:$LLAMA_PORT/v1/chat/completions" -H 'Content-Type: application/json' \
  -d '{"messages": [{"role": "user", "content": "Say hello in three words."}], "max_tokens": 12, "temperature": 0}' -o "$WORK/chat.json" || fail "chat completion"
python3 - "$WORK/chat.json" <<'PY' || fail "chat completion: $(cat "$WORK/chat.json")"
import json, sys
d = json.load(open(sys.argv[1]))
text = d["choices"][0]["message"]["content"]
assert isinstance(text, str) and d["usage"]["completion_tokens"] >= 1, d
print("  the artifact pulled by digest answers: %r" % text.strip()[:60])
PY

echo "SMOKE OK: ai-infra/model-artifacts"
