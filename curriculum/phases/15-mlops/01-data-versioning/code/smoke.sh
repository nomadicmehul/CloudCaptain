#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
LESSON="$PWD"
DVC_VERSION="3.67.1"
MOTO_VERSION="5.2.5"
BUCKET="cc-data-versioning"
WORK="$(mktemp -d)"
MOTO_PID=""
cleanup() {
  [[ -z "$MOTO_PID" ]] || kill "$MOTO_PID" 2> /dev/null || true
  [[ -z "$MOTO_PID" ]] || wait "$MOTO_PID" 2> /dev/null || true
  chmod -R u+w "$WORK" 2> /dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

for tool in git curl python3 find; do
  command -v "$tool" > /dev/null || fail "needs $tool"
done
python3 -c 'import sys, venv; assert sys.version_info >= (3, 9), sys.version' || fail "needs python3 >= 3.9 with venv"
unset PYTHONPATH PIP_REQUIRE_VIRTUALENV || true
export LC_ALL=C
export HOME="$WORK/home" XDG_CACHE_HOME="$WORK/home/.cache" XDG_CONFIG_HOME="$WORK/home/.config"
mkdir -p "$HOME"
export GIT_CONFIG_GLOBAL="$WORK/gitconfig" GIT_CONFIG_NOSYSTEM=1
git config --global user.name "Data Bot"
git config --global user.email "data-bot@example.invalid"
git config --global init.defaultBranch main
git config --global commit.gpgsign false
git config --global advice.detachedHead false
export DVC_NO_ANALYTICS=1 DVC_SITE_CACHE_DIR="$WORK/dvc-site"
export AWS_ACCESS_KEY_ID="AKIACCBOOTSTRAP0001" AWS_SECRET_ACCESS_KEY="bootstrap-$RANDOM-$RANDOM"
export AWS_DEFAULT_REGION=us-east-1 AWS_EC2_METADATA_DISABLED=true
export AWS_CONFIG_FILE="$WORK/no-aws-config" AWS_SHARED_CREDENTIALS_FILE="$WORK/no-aws-credentials"

minidvc() { python3 "$LESSON/code/main.py" "$@"; }
DVC="$WORK/venv/bin/dvc"
VPY="$WORK/venv/bin/python"
A="$WORK/A"
B="$WORK/B"

echo "▸ install DVC $DVC_VERSION with dvc-s3 and moto[server] $MOTO_VERSION from hash-pinned requirements"
# antlr4-python3-runtime 4.9.3 (required by hydra-core, a dvc dependency) exists only as an sdist: it is the one
# package built from source, with --no-build-isolation against the hash-pinned setuptools; everything else is a wheel.
python3 -m venv "$WORK/venv" || fail "python3 -m venv"
"$WORK/venv/bin/pip" install --quiet --disable-pip-version-check --retries 3 --require-hashes --only-binary=:all: \
  -r code/requirements-build.txt > "$WORK/pip.txt" 2>&1 || fail "pip install setuptools: $(cat "$WORK/pip.txt")"
"$WORK/venv/bin/pip" install --quiet --disable-pip-version-check --retries 3 --require-hashes --only-binary=:all: \
  --no-binary=antlr4-python3-runtime --no-build-isolation -r code/requirements-dvc.txt >> "$WORK/pip.txt" 2>&1 || fail "pip install dvc: $(cat "$WORK/pip.txt")"
[[ "$("$DVC" --version)" == "$DVC_VERSION" ]] || fail "unexpected dvc version: $("$DVC" --version)"
dvc() { "$DVC" "$@"; }
python3 -m venv "$WORK/moto" || fail "python3 -m venv moto"
"$WORK/moto/bin/pip" install --quiet --disable-pip-version-check --retries 3 --require-hashes --only-binary=:all: \
  -r code/requirements-moto.txt > "$WORK/pip-moto.txt" 2>&1 || fail "pip install moto: $(cat "$WORK/pip-moto.txt")"
"$WORK/moto/bin/python" -c 'import sys, moto; assert moto.__version__ == sys.argv[1], moto.__version__' "$MOTO_VERSION" || fail "unexpected moto version"

echo "▸ moto_server on 127.0.0.1 as the S3 remote, verifying SigV4 against IAM keys after 3 bootstrap calls"
PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
ENDPOINT="http://127.0.0.1:$PORT"
INITIAL_NO_AUTH_ACTION_COUNT=3 "$WORK/moto/bin/moto_server" -H 127.0.0.1 -p "$PORT" > "$WORK/moto.txt" 2>&1 &
MOTO_PID=$!
DEADLINE=$((SECONDS + 60))
until curl -sf -o "$WORK/moto-data.json" "$ENDPOINT/moto-api/data.json"; do
  kill -0 "$MOTO_PID" 2> /dev/null || fail "moto_server exited: $(tail -20 "$WORK/moto.txt")"
  [[ $SECONDS -lt $DEADLINE ]] || fail "moto_server did not answer on $ENDPOINT: $(tail -20 "$WORK/moto.txt")"
  sleep 0.3
done
python3 -c 'import json, sys; assert isinstance(json.load(open(sys.argv[1])), dict)' "$WORK/moto-data.json" || fail "port $PORT answers, but not as moto: $(head -c 300 "$WORK/moto-data.json")"
"$WORK/moto/bin/python" - "$ENDPOINT" > "$WORK/iam-key.txt" 2> "$WORK/iam.err" <<'PY' || fail "IAM bootstrap on moto: $(cat "$WORK/iam.err")"
import json, sys
import boto3
iam = boto3.client("iam", endpoint_url=sys.argv[1], region_name="us-east-1")
iam.create_user(UserName="data-bot")
key = iam.create_access_key(UserName="data-bot")["AccessKey"]
iam.put_user_policy(UserName="data-bot", PolicyName="s3-all",
                    PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"}]}))
print(key["AccessKeyId"], key["SecretAccessKey"])
PY
read -r AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY < "$WORK/iam-key.txt"
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
s3() {
  "$VPY" - "$ENDPOINT" "$BUCKET" "$@" <<'PY'
import sys
import botocore.session
endpoint, bucket, action = sys.argv[1:4]
client = botocore.session.get_session().create_client("s3", endpoint_url=endpoint, region_name="us-east-1")
if action == "mb":
    client.create_bucket(Bucket=bucket)
elif action == "keys":
    prefix = sys.argv[4] + "/files/md5/"
    keys = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys.extend(item["Key"][len(prefix):].replace("/", "", 1) for item in page.get("Contents", []))
    print("\n".join(sorted(keys)))
PY
}
s3 mb > "$WORK/mb.txt" 2>&1 || fail "create bucket with botocore: $(cat "$WORK/mb.txt")"
echo "  moto $MOTO_VERSION on port $PORT, IAM user data-bot with key $AWS_ACCESS_KEY_ID, bucket $BUCKET created"

gen() {
  python3 - "$1" "$2" <<'PY'
import random, shutil, sys
from pathlib import Path
root, variant = Path(sys.argv[1]) / "data", sys.argv[2]
for name in ("raw.csv", "features.bin", "images"):
    target = root / name
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
root.mkdir(parents=True, exist_ok=True)
rng = random.Random(15)
rows = ["id,feature,label"] + [f"{i},{rng.random():.6f},{rng.randint(0, 1)}" for i in range(4000)]
if variant in ("v2", "exp"):
    extra = random.Random(16 if variant == "v2" else 99)
    rows += [f"{i},{extra.random():.6f},{extra.randint(0, 1)}" for i in range(4000, 4300)]
(root / "raw.csv").write_text("\n".join(rows) + "\n")
(root / "features.bin").write_bytes(random.Random(1).randbytes(6 * 1024 * 1024))
images = root / "images"
for cls in ("class-a", "class-b"):
    for i in range(8):
        path = images / cls / f"img-{i:03d}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(random.Random(f"{cls}{i}").randbytes(4096))
shard = bytearray(random.Random(2).randbytes(3 * 1024 * 1024))
(images / "big").mkdir()
(images / "notes 1.txt").write_text("labelled by hand\n")
if variant in ("v2", "exp"):
    (images / "class-a" / "img-003.bin").unlink()
    (images / "class-c").mkdir()
    (images / "class-c" / "img-000.bin").write_bytes(random.Random("c0").randbytes(4096))
    shard[12345] ^= 0xFF
(images / "big" / "shard-0.bin").write_bytes(bytes(shard))
PY
}
data_tree() {
  python3 - "$1" <<'PY'
import hashlib, json, os, sys
root = os.path.join(sys.argv[1], "data")
tree = {}
for current, _, files in os.walk(root):
    for name in files:
        if name == ".gitignore" or name.endswith(".dvc"):
            continue
        full = os.path.join(current, name)
        tree[os.path.relpath(full, root)] = hashlib.md5(open(full, "rb").read()).hexdigest()
print(json.dumps(tree, sort_keys=True))
PY
}
cache_set() { (cd "$1/.dvc/cache/files/md5" && find . -type f | sed 's|^\./||; s|/||' | sort); }
same_pointers() {
  "$VPY" - "$A" "$B" <<'PY'
import sys, yaml
from pathlib import Path
a, b = Path(sys.argv[1]), Path(sys.argv[2])
for name in ("data/raw.csv.dvc", "data/features.bin.dvc", "data/images.dvc"):
    left, right = yaml.safe_load((a / name).read_text()), yaml.safe_load((b / name).read_text())
    assert left == right, (name, left, right)
    out = left["outs"][0]
    same = "byte-identical" if (a / name).read_bytes() == (b / name).read_bytes() else "same YAML, different bytes"
    print(f"  {name:24} {out['md5']:36} size {out['size']:>8} nfiles {out.get('nfiles', '-'):>2}  {same}")
PY
}
dataset() { mkdir -p "$WORK/seed-$1" && gen "$WORK/seed-$1" "$1"; }
use_seed() { gen "$1" "$2"; [[ "$(data_tree "$1")" == "$(data_tree "$WORK/seed-$2")" ]] || fail "generator is not deterministic for $2"; }

echo "▸ two identical Git repositories with a generated dataset (files, a directory, a 6 MiB and a 3 MiB file)"
for variant in v1 v2 exp; do dataset "$variant"; done
for repo in "$A" "$B"; do
  mkdir -p "$repo"
  git -C "$repo" init -q
  printf 'runs.txt\n' > "$repo/.gitignore"
  use_seed "$repo" v1
done
(cd "$A" && minidvc init > /dev/null) || fail "minidvc init"
(cd "$B" && dvc init -q && dvc config core.analytics false) || fail "dvc init"
minidvc -C "$A" remote add -d store "s3://$BUCKET/minidvc" --endpoint "$ENDPOINT" > /dev/null || fail "minidvc remote add"
(cd "$B" && dvc remote add -q -d store "s3://$BUCKET/dvc" && dvc remote modify store endpointurl "$ENDPOINT") || fail "dvc remote add"

echo "▸ add: minidvc and DVC record the same md5, size and nfiles, and fill the same cache objects"
minidvc -C "$A" --json add data/raw.csv data/features.bin data/images > "$WORK/add.json" || fail "minidvc add: $(cat "$WORK/add.json")"
(cd "$B" && dvc add -q data/raw.csv data/features.bin data/images) > "$WORK/dvc-add.txt" 2>&1 || fail "dvc add: $(cat "$WORK/dvc-add.txt")"
same_pointers > "$WORK/pointers.txt" 2>&1 || fail "pointer files differ: $(cat "$WORK/pointers.txt")"
cat "$WORK/pointers.txt"
cmp -s "$A/data/.gitignore" "$B/data/.gitignore" || fail "data/.gitignore differs: $(cat "$A/data/.gitignore") vs $(cat "$B/data/.gitignore")"
[[ "$(cache_set "$A")" == "$(cache_set "$B")" ]] || fail "cache objects differ: $(diff <(cache_set "$A") <(cache_set "$B") || true)"
DIR_MD5="$("$VPY" -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["outs"][0]["md5"])' "$B/data/images.dvc")"
cmp -s "$A/.dvc/cache/files/md5/${DIR_MD5:0:2}/${DIR_MD5:2}" "$B/.dvc/cache/files/md5/${DIR_MD5:0:2}/${DIR_MD5:2}" || fail ".dir manifests differ"
echo "  $(cache_set "$A" | wc -l | tr -d ' ') cache objects in both, .dir manifest $DIR_MD5 byte-identical"

echo "▸ status: an edited file is 'modified' in both, and minidvc refuses to overwrite it without --force"
for repo in "$A" "$B"; do cp "$repo/data/raw.csv" "$WORK/raw.bak"; echo "999,0.5,1" >> "$repo/data/raw.csv"; done
minidvc -C "$A" --json status > "$WORK/mini-status.json" || fail "minidvc status"
(cd "$B" && dvc status --json) > "$WORK/dvc-status.json" || fail "dvc status"
"$VPY" -c 'import json, sys; a, b = (json.load(open(p)) for p in sys.argv[1:3]); assert a == b == {"data/raw.csv.dvc": [{"changed outs": {"data/raw.csv": "modified"}}]}, (a, b)' \
  "$WORK/mini-status.json" "$WORK/dvc-status.json" || fail "status differs: $(cat "$WORK/mini-status.json" "$WORK/dvc-status.json")"
set +e
minidvc -C "$A" checkout > "$WORK/refuse.txt" 2>&1
CODE=$?
set -e
[[ "$CODE" == "1" ]] && grep -q "would be lost" "$WORK/refuse.txt" || fail "minidvc checkout should refuse unsaved work (exit $CODE): $(cat "$WORK/refuse.txt")"
for repo in "$A" "$B"; do cp "$WORK/raw.bak" "$repo/data/raw.csv"; done
minidvc -C "$A" status -q || fail "minidvc status should be clean again"
echo "  both: {\"data/raw.csv.dvc\": [{\"changed outs\": {\"data/raw.csv\": \"modified\"}}]}"

echo "▸ push v1 and v2 to moto: minidvc signs with its own SigV4, DVC uses botocore; moto checks both"
commit_all() { git -C "$1" add -A && git -C "$1" commit -q -m "$2" && { [[ -z "${3:-}" ]] || git -C "$1" tag "$3"; }; }
commit_all "$A" "data v1" v1
commit_all "$B" "data v1" v1
push_both() {
  minidvc -C "$A" --json push > "$WORK/push.json" 2>&1 || fail "minidvc push: $(cat "$WORK/push.json")"
  (cd "$B" && dvc push -q) > "$WORK/dvc-push.txt" 2>&1 || fail "dvc push: $(cat "$WORK/dvc-push.txt")"
}
set +e
AWS_SECRET_ACCESS_KEY="wrong-secret" minidvc -C "$A" push > "$WORK/wrong.txt" 2>&1
CODE=$?
set -e
[[ "$CODE" == "1" ]] && grep -q "403 SignatureDoesNotMatch" "$WORK/wrong.txt" || fail "a wrong secret should get 403 SignatureDoesNotMatch from moto (exit $CODE): $(cat "$WORK/wrong.txt")"
push_both
echo "  wrong secret: $(grep -o 'HTTP 403 SignatureDoesNotMatch' "$WORK/wrong.txt"); the IAM key is accepted from minidvc and from DVC"
for repo in "$A" "$B"; do
  use_seed "$repo" v2
done
minidvc -C "$A" add data/raw.csv data/images > /dev/null || fail "minidvc add v2"
(cd "$B" && dvc add -q data/raw.csv data/images) || fail "dvc add v2"
same_pointers > "$WORK/pointers.txt" 2>&1 || fail "v2 pointer files differ: $(cat "$WORK/pointers.txt")"
commit_all "$A" "data v2" v2
commit_all "$B" "data v2" v2
push_both
s3 keys minidvc > "$WORK/remote-mini.txt" || fail "list minidvc prefix"
s3 keys dvc > "$WORK/remote-dvc.txt" || fail "list dvc prefix"
[[ -s "$WORK/remote-mini.txt" ]] || fail "nothing under s3://$BUCKET/minidvc"
cmp -s "$WORK/remote-mini.txt" "$WORK/remote-dvc.txt" || fail "remote objects differ: $(diff "$WORK/remote-mini.txt" "$WORK/remote-dvc.txt" || true)"
[[ "$(cache_set "$A")" == "$(cat "$WORK/remote-mini.txt")" ]] || fail "the remote should hold exactly the cache: $(diff <(cache_set "$A") "$WORK/remote-mini.txt" || true)"
echo "  $(wc -l < "$WORK/remote-mini.txt" | tr -d ' ') objects under s3://$BUCKET/minidvc/files/md5/ and the same keys under s3://$BUCKET/dvc/files/md5/"

echo "▸ fresh clones pull v2, then v1 after checking out the older commit; DVC also pulls what minidvc pushed"
git clone -q "$A" "$WORK/A2"
git clone -q "$B" "$WORK/B2"
git clone -q "$A" "$WORK/A3"
git clone -q "$B" "$WORK/B3"
minidvc -C "$WORK/A2" --json pull > "$WORK/pull.json" 2>&1 || fail "minidvc pull: $(cat "$WORK/pull.json")"
(cd "$WORK/B2" && dvc pull -q) > "$WORK/dvc-pull.txt" 2>&1 || fail "dvc pull: $(cat "$WORK/dvc-pull.txt")"
(cd "$WORK/A3" && dvc pull -q) > "$WORK/dvc-pull3.txt" 2>&1 || fail "dvc pull in a minidvc repository: $(cat "$WORK/dvc-pull3.txt")"
minidvc -C "$WORK/B3" pull > "$WORK/pull3.txt" 2>&1 || fail "minidvc pull in a DVC repository: $(cat "$WORK/pull3.txt")"
V2="$(data_tree "$WORK/seed-v2")"
V1="$(data_tree "$WORK/seed-v1")"
for clone in A2 B2 A3 B3; do
  [[ "$(data_tree "$WORK/$clone")" == "$V2" ]] || fail "$clone after pull is not v2: $(data_tree "$WORK/$clone")"
done
git -C "$WORK/A2" checkout -q v1
git -C "$WORK/B2" checkout -q v1
minidvc -C "$WORK/A2" pull > "$WORK/pull.txt" 2>&1 || fail "minidvc pull at v1: $(cat "$WORK/pull.txt")"
(cd "$WORK/B2" && dvc pull -q) > "$WORK/dvc-pull.txt" 2>&1 || fail "dvc pull at v1: $(cat "$WORK/dvc-pull.txt")"
for clone in A2 B2; do
  [[ "$(data_tree "$WORK/$clone")" == "$V1" ]] || fail "$clone at tag v1 does not hold v1 data"
done
echo "  v2 in A2 (minidvc), B2 (dvc), A3 (dvc reading minidvc's push) and B3 (minidvc reading dvc's push); v1 in A2 and B2 after git checkout v1 + pull"

echo "▸ checkout from the local cache: git checkout v1 restores v1 data in both, git checkout main restores v2"
git -C "$A" checkout -q v1
git -C "$B" checkout -q v1
minidvc -C "$A" --json checkout > "$WORK/checkout.json" || fail "minidvc checkout"
(cd "$B" && dvc checkout -q) || fail "dvc checkout v1"
[[ "$(data_tree "$A")" == "$V1" && "$(data_tree "$B")" == "$V1" ]] || fail "checkout of v1 did not restore v1"
LINKS="$("$VPY" -c 'import json, sys; print(",".join(sorted({l for row in json.load(open(sys.argv[1])) for l in row["links"]})))' "$WORK/checkout.json")"
git -C "$A" checkout -q main
git -C "$B" checkout -q main
minidvc -C "$A" checkout > /dev/null || fail "minidvc checkout main"
(cd "$B" && dvc checkout -q) || fail "dvc checkout main"
[[ "$(data_tree "$A")" == "$V2" && "$(data_tree "$B")" == "$V2" ]] || fail "checkout of main did not restore v2"
echo "  minidvc linked with: $LINKS (reflink falls back to copy where the filesystem cannot clone)"

echo "▸ pipeline: both tools rerun only the stages whose inputs changed"
for repo in "$A" "$B"; do
  cat > "$repo/dvc.yaml" <<'EOF'
stages:
  prepare:
    cmd: tail -n +2 data/raw.csv > prepared.csv && echo prepare >> runs.txt
    deps:
    - data/raw.csv
    outs:
    - prepared.csv
  train:
    cmd: cat prepared.csv params.txt > model.txt && echo train >> runs.txt
    deps:
    - prepared.csv
    - params.txt
    outs:
    - model.txt
EOF
  printf 'lr=0.1\n' > "$repo/params.txt"
done
repro_both() {
  rm -f "$A/runs.txt" "$B/runs.txt"
  minidvc -C "$A" --json repro > "$WORK/repro.json" 2>&1 || fail "minidvc repro: $(cat "$WORK/repro.json")"
  (cd "$B" && dvc repro -q) > "$WORK/dvc-repro.txt" 2>&1 || fail "dvc repro: $(cat "$WORK/dvc-repro.txt")"
  local mini real
  mini="$( [[ -f "$A/runs.txt" ]] && tr '\n' ' ' < "$A/runs.txt" || true)"
  real="$( [[ -f "$B/runs.txt" ]] && tr '\n' ' ' < "$B/runs.txt" || true)"
  [[ "$mini" == "$1" && "$real" == "$1" ]] || fail "$2: expected stages '$1', minidvc ran '$mini', dvc ran '$real'"
  printf '  %-28s minidvc: %-16s dvc: %s\n' "$2" "${mini:-nothing}" "${real:-nothing}"
}
repro_both "prepare train " "first run"
repro_both "" "nothing changed"
printf 'lr=0.2\n' > "$A/params.txt"
printf 'lr=0.2\n' > "$B/params.txt"
repro_both "train " "params.txt changed"
"$VPY" - "$A/dvc.lock" "$B/dvc.lock" <<'PY' || fail "dvc.lock files differ: $(cat "$A/dvc.lock" "$B/dvc.lock")"
import sys, yaml
def view(path):
    data = yaml.safe_load(open(path))
    assert str(data["schema"]) == "2.0", data
    return {name: (stage["cmd"], {d["path"]: d["md5"] for d in stage["deps"]}, {o["path"]: o["md5"] for o in stage["outs"]}) for name, stage in data["stages"].items()}
a, b = view(sys.argv[1]), view(sys.argv[2])
assert a == b, (a, b)
print("  dvc.lock: same command, deps and outs md5 for " + ", ".join(sorted(a)))
PY
commit_all "$A" "pipeline"
commit_all "$B" "pipeline"
push_both

echo "▸ gc --all-commits --cloud: an experiment branch is deleted, both keep exactly the objects some commit references"
for repo in "$A" "$B"; do
  git -C "$repo" checkout -q -b exp
  use_seed "$repo" exp
done
minidvc -C "$A" add data/raw.csv data/images > /dev/null || fail "minidvc add exp"
(cd "$B" && dvc add -q data/raw.csv data/images) || fail "dvc add exp"
commit_all "$A" "experiment"
commit_all "$B" "experiment"
push_both
EXP_MD5="$("$VPY" -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["outs"][0]["md5"])' "$A/data/raw.csv.dvc")"
for repo in "$A" "$B"; do
  git -C "$repo" checkout -q main
done
minidvc -C "$A" checkout > /dev/null || fail "minidvc checkout after exp"
(cd "$B" && dvc checkout -q) || fail "dvc checkout after exp"
for repo in "$A" "$B"; do
  git -C "$repo" branch -q -D exp
  git -C "$repo" reflog expire --expire=now --all
  cache_set "$repo" | grep -qx "$EXP_MD5" || fail "the experiment object $EXP_MD5 should be cached before gc"
done
expected() {
  "$VPY" - "$1" <<'PY'
import subprocess, sys, yaml, json
from pathlib import Path
repo = Path(sys.argv[1])
git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout
keep = set()
for commit in git("rev-list", "--all").split():
    for path in git("ls-tree", "-r", "--name-only", commit).splitlines():
        if not (path.endswith(".dvc") or path == "dvc.lock"):
            continue
        data = yaml.safe_load(git("show", f"{commit}:{path}"))
        outs = [o for s in data.get("stages", {}).values() for o in s.get("outs", [])] if path == "dvc.lock" else data["outs"]
        for out in outs:
            keep.add(out["md5"])
            if out["md5"].endswith(".dir"):
                manifest = repo / ".dvc/cache/files/md5" / out["md5"][:2] / out["md5"][2:]
                keep.update(row["md5"] for row in json.loads(manifest.read_text()))
print("\n".join(sorted(keep)))
PY
}
expected "$A" > "$WORK/expected-a.txt" || fail "compute the referenced set for A"
expected "$B" > "$WORK/expected-b.txt" || fail "compute the referenced set for B"
cmp -s "$WORK/expected-a.txt" "$WORK/expected-b.txt" || fail "the two histories reference different objects"
BEFORE="$(cache_set "$A" | wc -l | tr -d ' ')"
minidvc -C "$A" --json gc --all-commits --cloud > "$WORK/gc.json" 2>&1 || fail "minidvc gc: $(cat "$WORK/gc.json")"
(cd "$B" && dvc gc -q -f --all-commits --cloud) > "$WORK/dvc-gc.txt" 2>&1 || fail "dvc gc: $(cat "$WORK/dvc-gc.txt")"
cache_set "$A" > "$WORK/cache-a.txt"
cache_set "$B" > "$WORK/cache-b.txt"
s3 keys minidvc > "$WORK/remote-mini.txt" || fail "list minidvc prefix after gc"
s3 keys dvc > "$WORK/remote-dvc.txt" || fail "list dvc prefix after gc"
for got in cache-a cache-b remote-mini remote-dvc; do
  cmp -s "$WORK/$got.txt" "$WORK/expected-a.txt" || fail "$got after gc is not the referenced set: $(diff "$WORK/expected-a.txt" "$WORK/$got.txt" || true)"
done
grep -qx "$EXP_MD5" "$WORK/cache-a.txt" && fail "the deleted branch's object survived gc"
echo "  $BEFORE objects before, $(wc -l < "$WORK/expected-a.txt" | tr -d ' ') after in both caches and both remote prefixes; the deleted branch's raw.csv ($EXP_MD5) is gone"
[[ "$(data_tree "$A")" == "$V2" ]] || fail "gc changed the workspace"

echo "SMOKE OK: mlops/data-versioning"
