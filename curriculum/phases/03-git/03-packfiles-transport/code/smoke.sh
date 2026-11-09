#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
LESSON="$PWD"
WORK="$(mktemp -d)"
SERVER_PID=""
cleanup() {
  if [[ -n "$SERVER_PID" ]]; then kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT
command -v git >/dev/null || fail "git is not installed"
command -v python3 >/dev/null || fail "python3 is not installed"
git --version
[[ -x "$(git --exec-path)/git-http-backend" ]] || fail "git-http-backend is not in $(git --exec-path)"

export GIT_CONFIG_GLOBAL="$WORK/gitconfig" GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
unset GIT_TRACE_PACKET GIT_TRACE GIT_TRACE_CURL
: > "$GIT_CONFIG_GLOBAL"
git config --global init.defaultBranch main
git config --global init.defaultRefFormat files
git config --global user.name Dev
git config --global user.email dev@example.invalid
git config --global advice.detachedHead false
git config --global commit.gpgsign false
git config --global tag.gpgsign false
git config --global gc.auto 0
git config --global maintenance.auto false
git config --global fetch.writeCommitGraph false
git config --global protocol.version 2

mp() { python3 "$LESSON/code/main.py" "$@"; }
at() { export GIT_AUTHOR_DATE="$1 +0000" GIT_COMMITTER_DATE="$1 +0000"; }

cat > "$WORK/gen.py" <<'PY'
import hashlib
import os
import sys

root, version = sys.argv[1], int(sys.argv[2])
os.makedirs(os.path.join(root, "docs"), exist_ok=True)
os.makedirs(os.path.join(root, "assets"), exist_ok=True)
with open(os.path.join(root, "numbers.txt"), "w") as handle:
    handle.write("".join(f"{n}\n" for n in range(1, version * 400 + 1)))
with open(os.path.join(root, "VERSION"), "w") as handle:
    handle.write(f"v{version}\n")
with open(os.path.join(root, "docs", "guide.md"), "w") as handle:
    handle.write("".join(f"Step {n}: run the release checklist item {n} for build {min(n, version)}.\n" for n in range(1, 120)))
seed = "frame-%d" % (version // 3)
blob = b"".join(hashlib.sha256(f"{seed}-{i}".encode()).digest() for i in range(1024))
with open(os.path.join(root, "assets", "frame.bin"), "wb") as handle:
    handle.write(blob)
script = os.path.join(root, "release.sh")
with open(script, "w") as handle:
    handle.write(f"#!/bin/sh\necho release {version}\n")
os.chmod(script, 0o755)
PY

cat > "$WORK/check.py" <<'PY'
import json
import re
import subprocess
import sys

TRACE = re.compile(r"packet:\s+(?P<who>[\w.-]+)(?P<dir>[<>]) ?(?P<text>.*)$")
SECTIONS = re.compile(r"^(acknowledgments|shallow-info|packfile|ready|NAK|ACK [0-9a-f]{40}|shallow [0-9a-f]{40}|unshallow [0-9a-f]{40})$")


def die(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def objects(mine, theirs, label):
    listing = {}
    for name, repo in (("minipack", mine), ("git", theirs)):
        listing[name] = sorted(git(repo, "rev-list", "--objects", "--all", "--missing=print").splitlines())
    if listing["minipack"] != listing["git"]:
        a, b = set(listing["minipack"]), set(listing["git"])
        die(f"{label}: rev-list --objects --all differs: only minipack {sorted(a - b)[:8]}, only git {sorted(b - a)[:8]}")
    missing = sum(1 for line in listing["git"] if line.startswith("?"))
    promised = f", {missing} of them promised blobs that are absent on purpose" if missing else ""
    print(f"  {label}: rev-list --objects --all --missing=print identical, {len(listing['git'])} objects{promised}")
    return missing


def refs(mine, theirs, label):
    found = {}
    for name, repo in (("minipack", mine), ("git", theirs)):
        rows = git(repo, "for-each-ref", "--format=%(objectname) %(refname) %(symref)").splitlines()
        found[name] = sorted(tuple(row.split(" ")) for row in rows)
        found[name].append(("HEAD", git(repo, "symbolic-ref", "HEAD").strip()))
    if found["minipack"] != found["git"]:
        die(f"{label}: refs differ: minipack {found['minipack']}, git {found['git']}")
    print(f"  {label}: {len(found['git']) - 1} refs and HEAD -> {found['git'][-1][1]} identical")


def shallow(mine, theirs, label):
    def read(repo):
        try:
            with open(f"{repo}/.git/shallow", encoding="utf-8") as handle:
                return sorted(line.strip() for line in handle if line.strip())
        except FileNotFoundError:
            return []
    a, b = read(mine), read(theirs)
    if a != b:
        die(f"{label}: .git/shallow differs: minipack {a}, git {b}")
    print(f"  {label}: .git/shallow identical ({len(a)} boundary commits)")


def parse_trace(path, who=""):
    requests, current, stage, received = [], None, "caps", []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = TRACE.search(line.rstrip("\n"))
            if not match:
                continue
            name, direction, text = match.group("who"), match.group("dir"), match.group("text").rstrip()
            if direction == "<":
                if name == who and SECTIONS.match(text):
                    received.append(text)
                continue
            if not who:
                if name == "git" or not text.startswith("command="):
                    continue
                who = name
            if name != who:
                continue
            if text.startswith("command="):
                current = {"command": text[8:], "caps": [], "args": []}
                requests.append(current)
                stage = "caps"
            elif current is None:
                continue
            elif text == "0001":
                stage = "args"
            elif text == "0000":
                current = None
            else:
                current[stage].append(text)
    if not requests:
        die(f"{path}: no protocol v2 requests in the trace")
    return who, requests, received


def keywords(args):
    seen = []
    for arg in args:
        word = arg.split(" ")[0]
        if word != "thin-pack" and word not in seen:
            seen.append(word)
    return seen


def trace(mine_path, theirs_path, label):
    _, mine, mine_got = parse_trace(mine_path, "minipack")
    who, theirs, theirs_got = parse_trace(theirs_path)
    if [r["command"] for r in mine] != [r["command"] for r in theirs]:
        die(f"{label}: commands differ: minipack {[r['command'] for r in mine]}, git ({who}) {[r['command'] for r in theirs]}")
    for a, b in zip(mine, theirs):
        caps_a, caps_b = sorted(c.split("=")[0] for c in a["caps"]), sorted(c.split("=")[0] for c in b["caps"])
        args_a, args_b = set(a["args"]) - {"thin-pack"}, set(b["args"]) - {"thin-pack"}
        if caps_a != caps_b:
            die(f"{label} {a['command']}: capability lines differ: minipack {caps_a}, git {caps_b}")
        if args_a != args_b:
            die(f"{label} {a['command']}: arguments differ: only minipack {sorted(args_a - args_b)}, only git {sorted(args_b - args_a)}")
        if keywords(a["args"]) != keywords(b["args"]):
            die(f"{label} {a['command']}: argument order differs: minipack {keywords(a['args'])}, git {keywords(b['args'])}")
    if mine_got != theirs_got:
        die(f"{label}: response sections differ: minipack read {mine_got}, git ({who}) read {theirs_got}")
    summary = "; ".join(f"{r['command']}: {' '.join(keywords(r['args']))}" for r in theirs)
    print(f"  {label}: same requests as {who} in GIT_TRACE_PACKET ({summary}); sections {[s.split(' ')[0] for s in theirs_got]}")


def fetch_trace(mine_path, theirs_path):
    _, mine, mine_got = parse_trace(mine_path, "minipack")
    who, theirs, theirs_got = parse_trace(theirs_path)
    for name, requests, got in (("minipack", mine, mine_got), (who, theirs, theirs_got)):
        fetches = [r for r in requests if r["command"] == "fetch"]
        haves = sum(1 for r in fetches for a in r["args"] if a.startswith("have "))
        acks = sum(1 for line in got if line.startswith("ACK "))
        if not fetches or not haves or not acks or got[-1:] != ["packfile"] or "ready" not in got:
            die(f"{name}: an incremental fetch with unpushed local commits should send haves, read ACK lines and ready, then a packfile; requests {requests}, sections {got}")
        rounds = ", ".join(f"{sum(1 for a in r['args'] if a.startswith('have '))} haves{' + done' if 'done' in r['args'] else ''}" for r in fetches)
        print(f"  {name}: {len(fetches)} fetch request(s) ({rounds}); read {acks} ACK, ready, packfile")


def same_bytes(a, b, label):
    with open(a, "rb") as one, open(b, "rb") as two:
        left, right = one.read(), two.read()
    if left != right:
        first = next((i for i, (x, y) in enumerate(zip(left, right)) if x != y), min(len(left), len(right)))
        die(f"{label}: {a} ({len(left)} bytes) and {b} ({len(right)} bytes) differ from byte {first}")
    print(f"  {label}: byte-identical ({len(left)} bytes)")


def verify(git_out, mine_json, label, want_deltas):
    rows, chains = {}, {}
    with open(git_out, encoding="utf-8") as handle:
        for line in handle:
            fields = line.split()
            if len(fields) >= 5 and re.match(r"^[0-9a-f]{40}$", fields[0]):
                depth, base = (int(fields[5]), fields[6]) if len(fields) >= 7 else (0, "")
                rows[fields[0]] = (fields[1], int(fields[2]), int(fields[3]), int(fields[4]), depth, base)
            match = re.match(r"^chain length = (\d+): (\d+) objects?$", line.strip())
            if match:
                chains[match.group(1)] = int(match.group(2))
    report = load(mine_json)
    mine = {r["oid"]: (r["type"], r["size"], r["packed"], r["offset"], r["depth"], r["base"]) for r in report["objects"]}
    if not report["ok"]:
        die(f"{label}: minipack verify-pack reports {report['problems']}")
    if mine != rows:
        diff = sorted(oid for oid in set(mine) | set(rows) if mine.get(oid) != rows.get(oid))[:5]
        die(f"{label}: verify-pack -v rows differ for {[(oid, mine.get(oid), rows.get(oid)) for oid in diff]}")
    if dict(report["chains"]) != chains:
        die(f"{label}: chain histograms differ: minipack {report['chains']}, git {chains}")
    deltas = sum(chains.values())
    if want_deltas and not deltas:
        die(f"{label}: expected delta entries, git verify-pack shows none")
    print(f"  {label}: {len(rows)} rows (id, type, size, packed size, offset, depth, base) and chains {chains} identical to git verify-pack -v")


def pack_objects(repo, git_out, report_json):
    report = load(report_json)
    listed = set()
    with open(git_out, encoding="utf-8") as handle:
        for line in handle:
            fields = line.split()
            if fields and re.match(r"^[0-9a-f]{40}$", fields[0]):
                listed.add(fields[0])
    reachable = {line.split(" ")[0] for line in git(repo, "rev-list", "--objects", "--all").splitlines()}
    if listed != reachable:
        die(f"pack-objects: the pack holds {len(listed)} objects, rev-list --objects --all lists {len(reachable)}: only pack {sorted(listed - reachable)[:5]}, only rev-list {sorted(reachable - listed)[:5]}")
    if report["objects"] != len(listed) or not report["deltas"]:
        die(f"pack-objects report {report} does not match {len(listed)} objects with deltas")
    print(f"  {report['delta_kind']}-delta pack: {len(listed)} objects = rev-list --objects --all, {report['deltas']} deltas, {report['bytes']} bytes")


def ls_remote(git_out, mine_json):
    with open(git_out, encoding="utf-8") as handle:
        theirs = sorted(tuple(line.rstrip("\n").split("\t")) for line in handle if line.strip())
    mine = []
    for ref in load(mine_json)["refs"]:
        mine.append((ref["oid"], ref["name"]))
        if ref["peeled"]:
            mine.append((ref["peeled"], ref["name"] + "^{}"))
    if sorted(mine) != theirs:
        die(f"ls-remote differs: minipack {sorted(mine)}, git {theirs}")
    print(f"  {len(theirs)} lines identical, peeled tags included")


def batch(mine, theirs):
    listing = {}
    for name, repo in (("minipack", mine), ("git", theirs)):
        listing[name] = sorted(git(repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype) %(objectsize)").splitlines())
    if listing["minipack"] != listing["git"]:
        die(f"unpack-objects: objects differ: only minipack {sorted(set(listing['minipack']) - set(listing['git']))[:5]}, only git {sorted(set(listing['git']) - set(listing['minipack']))[:5]}")
    print(f"  {len(listing['git'])} objects with the same id, type and size as in Git's pack")


COMMANDS = {"objects": objects, "refs": refs, "shallow": shallow, "trace": trace, "fetch-trace": fetch_trace, "same-bytes": same_bytes,
            "pack-objects": pack_objects, "ls-remote": ls_remote, "batch": batch}

if __name__ == "__main__":
    if sys.argv[1] == "verify":
        verify(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5] == "deltas")
    else:
        COMMANDS[sys.argv[1]](*sys.argv[2:])
PY
check() { python3 "$WORK/check.py" "$@"; }

echo "▸ a repository with 6 commits, a merge, a branch, annotated and lightweight tags, growing text and a binary that changes"
W="$WORK/work"
git init -q --object-format=sha1 "$W"
for i in 1 2 3; do
  python3 "$WORK/gen.py" "$W" "$i"; git -C "$W" add -A; at $((1700000000 + i * 1000)); git -C "$W" commit -qm "release $i"
done
git -C "$W" tag -a v1 -m "version 1" HEAD~1
git -C "$W" checkout -qb topic
echo "topic notes" > "$W/docs/topic.md"; git -C "$W" add -A; at 1700003500; git -C "$W" commit -qm "topic: notes"
git -C "$W" checkout -q main
for i in 4 5; do
  python3 "$WORK/gen.py" "$W" "$i"; git -C "$W" add -A; at $((1700000000 + i * 1000)); git -C "$W" commit -qm "release $i"
done
git -C "$W" tag light HEAD~1
at 1700006000; git -C "$W" merge -q --no-ff -m "merge topic" topic
git -C "$W" tag -a v2 -m "version 2" HEAD
git -C "$W" log --oneline --graph --all > "$WORK/graph.txt"; cat "$WORK/graph.txt"
mkdir "$WORK/srv"
git clone -q --bare "$W" "$WORK/srv/app.git"
git -C "$WORK/srv/app.git" config uploadpack.allowFilter true

echo "▸ minipack serve: git http-backend behind a local CGI wrapper on 127.0.0.1"
mp serve "$WORK/srv" --port 0 --url-file "$WORK/url" --verbose 2> "$WORK/serve.log" > "$WORK/serve.out" &
SERVER_PID=$!
for _ in $(seq 1 100); do [[ -s "$WORK/url" ]] && break; kill -0 "$SERVER_PID" 2>/dev/null || fail "minipack serve exited: $(cat "$WORK/serve.log")"; sleep 0.1; done
[[ -s "$WORK/url" ]] || fail "minipack serve did not write its URL: $(cat "$WORK/serve.log")"
URL="$(head -n 1 "$WORK/url")/app.git"
git ls-remote "$URL" > "$WORK/ls-git.txt" 2> "$WORK/ls-git.err" || fail "git ls-remote $URL: $(cat "$WORK/ls-git.err") $(cat "$WORK/serve.log")"
echo "  $URL answers git ls-remote"

echo "▸ ls-remote: minipack ls-refs (peel, symrefs) against git ls-remote"
mp ls-remote "$URL" --json > "$WORK/ls-mp.json" 2> "$WORK/ls-mp.err" || fail "minipack ls-remote: $(cat "$WORK/ls-mp.err")"
check ls-remote "$WORK/ls-git.txt" "$WORK/ls-mp.json" || fail "ls-remote comparison"

clone_pair() {
  local name="$1"; shift
  local mp_opts=() git_opts=()
  case "$name" in
    full) ;;
    shallow) mp_opts=(--depth 2); git_opts=(--depth 2) ;;
    blobless) mp_opts=(--filter blob:none); git_opts=(--filter=blob:none) ;;
  esac
  mp clone "$URL" "$WORK/mp-$name" ${mp_opts[@]+"${mp_opts[@]}"} --trace "$WORK/trace-mp-$name.txt" --json > "$WORK/clone-mp-$name.json" 2> "$WORK/clone-mp-$name.err" \
    || fail "minipack clone ($name): $(cat "$WORK/clone-mp-$name.err") $(tail -n 5 "$WORK/serve.log")"
  GIT_TRACE_PACKET="$WORK/trace-git-$name.txt" git clone -q --no-checkout ${git_opts[@]+"${git_opts[@]}"} "$URL" "$WORK/git-$name" 2> "$WORK/clone-git-$name.err" \
    || fail "git clone ($name): $(cat "$WORK/clone-git-$name.err")"
  python3 -c 'import json, sys; s = json.load(open(sys.argv[1])); p = s["pack"]; print("  minipack pack: %d objects %s, %d deltas, %d bytes, %s" % (p["objects"], p["by_type"], p["deltas"], p["bytes"], p["storage"]))' "$WORK/clone-mp-$name.json"
  check objects "$WORK/mp-$name" "$WORK/git-$name" "$name" > "$WORK/objects-$name.txt" || fail "$name clone objects"
  cat "$WORK/objects-$name.txt"
  check refs "$WORK/mp-$name" "$WORK/git-$name" "$name" || fail "$name clone refs"
  check shallow "$WORK/mp-$name" "$WORK/git-$name" "$name" || fail "$name clone shallow file"
  git -C "$WORK/mp-$name" fsck --strict --no-progress > "$WORK/fsck-$name.txt" 2>&1 || fail "git fsck --strict on minipack's $name clone: $(cat "$WORK/fsck-$name.txt")"
  echo "  $name: git fsck --strict accepts minipack's clone"
  check trace "$WORK/trace-mp-$name.txt" "$WORK/trace-git-$name.txt" "$name" || fail "$name pkt-line exchange"
}

echo "▸ full clone: minipack clone vs git clone --no-checkout"
clone_pair full
grep -q '"storage": "loose"' "$WORK/clone-mp-full.json" || fail "a default minipack clone should unpack into loose objects: $(head -c 400 "$WORK/clone-mp-full.json")"

echo "▸ shallow clone: --depth 2 from a merge commit, so the boundary has two commits"
clone_pair shallow
[[ "$(grep -c . "$WORK/mp-shallow/.git/shallow")" -ge 2 ]] || fail "expected at least two shallow boundary commits: $(cat "$WORK/mp-shallow/.git/shallow")"

echo "▸ blob-less partial clone: --filter blob:none"
clone_pair blobless
MISSING="$(grep -oE '[0-9]+ of them promised' "$WORK/objects-blobless.txt" | tr -dc '0-9' || true)"
[[ "${MISSING:-0}" -gt 0 ]] || fail "the blob-less clone should have promised (missing) blobs: $(cat "$WORK/objects-blobless.txt")"
ls "$WORK"/mp-blobless/.git/objects/pack/*.promisor > /dev/null 2>&1 || fail "minipack's partial clone has no .promisor pack: $(ls "$WORK/mp-blobless/.git/objects/pack")"
for key in remote.origin.promisor remote.origin.partialclonefilter; do
  a="$(git -C "$WORK/mp-blobless" config --get "$key" || true)"; b="$(git -C "$WORK/git-blobless" config --get "$key" || true)"
  [[ "$a" == "$b" && -n "$a" ]] || fail "$key: minipack '$a', git '$b'"
done
echo "  remote.origin.promisor and partialclonefilter set as git sets them; .promisor next to the pack"
VERSION_BLOB="$(git -C "$W" rev-parse HEAD:VERSION)"
GOT="$(git -C "$WORK/mp-blobless" cat-file -p "$VERSION_BLOB" 2> "$WORK/lazy.err")" || fail "lazy fetch of a promised blob through minipack's partial clone: $(cat "$WORK/lazy.err")"
[[ "$GOT" == "v5" ]] || fail "lazily fetched VERSION blob says '$GOT', expected v5"
echo "  git cat-file on a promised blob fetched it on demand from the remote: VERSION = $GOT"

echo "▸ incremental fetch after a push: want/have negotiation with ACK and ready, only new objects in the pack"
python3 "$WORK/gen.py" "$W" 6; git -C "$W" add -A; at 1700007000; git -C "$W" commit -qm "release 6"
git -C "$W" tag -a v3 -m "version 3" HEAD
git -C "$W" push -q "$WORK/srv/app.git" main v3 2> "$WORK/push.err" || fail "git push: $(cat "$WORK/push.err")"
for clone in mp-full git-full; do
  TREE="$(git -C "$WORK/$clone" rev-parse 'HEAD^{tree}')"; TIP="$(git -C "$WORK/$clone" rev-parse HEAD)"
  for i in $(seq 1 24); do at $((1700010000 + i)); TIP="$(git -C "$WORK/$clone" commit-tree -p "$TIP" -m "local work $i" "$TREE")"; done
  git -C "$WORK/$clone" update-ref refs/heads/local "$TIP"
done
echo "  24 unpushed local commits on refs/heads/local in both clones, more than minipack's first batch of 16 haves"
git -C "$WORK/mp-full" rev-list --objects --all > "$WORK/before.txt"
mp fetch "$WORK/mp-full" --trace "$WORK/trace-mp-fetch.txt" --json > "$WORK/fetch-mp.json" 2> "$WORK/fetch-mp.err" || fail "minipack fetch: $(cat "$WORK/fetch-mp.err")"
GIT_TRACE_PACKET="$WORK/trace-git-fetch.txt" git -C "$WORK/git-full" fetch -q origin 2> "$WORK/fetch-git.err" || fail "git fetch: $(cat "$WORK/fetch-git.err")"
check objects "$WORK/mp-full" "$WORK/git-full" "after fetch" || fail "objects after fetch"
check refs "$WORK/mp-full" "$WORK/git-full" "after fetch" || fail "refs after fetch"
git -C "$WORK/mp-full" rev-list --objects --all > "$WORK/after.txt"
python3 - "$WORK/fetch-mp.json" "$WORK/before.txt" "$WORK/after.txt" <<'PY' || fail "fetch pack size: $(cat "$WORK/fetch-mp.json")"
import json, sys
summary = json.load(open(sys.argv[1]))
new = len(set(open(sys.argv[3]).read().split("\n")) - set(open(sys.argv[2]).read().split("\n")))
got = summary["pack"]["objects"]
assert got == new, (got, new)
print(f"  minipack's fetch pack: {got} objects = exactly the objects the clone did not have; rounds {summary['rounds']}")
PY
check fetch-trace "$WORK/trace-mp-fetch.txt" "$WORK/trace-git-fetch.txt" || fail "fetch negotiation traces"
git -C "$WORK/mp-full" fsck --strict --no-progress > "$WORK/fsck-fetch.txt" 2>&1 || fail "git fsck after minipack fetch: $(cat "$WORK/fsck-fetch.txt")"

echo "▸ minipack pack-objects: git index-pack computes the same .idx, git verify-pack accepts the pack"
for kind in ofs ref; do
  extra=(); [[ "$kind" == "ref" ]] && extra=(--ref-delta)
  mp pack-objects --repo "$W" --all --out "$WORK/pk-$kind" ${extra[@]+"${extra[@]}"} --json > "$WORK/pk-$kind.json" 2> "$WORK/pk-$kind.err" || fail "minipack pack-objects ($kind): $(cat "$WORK/pk-$kind.err")"
  PACK="$(ls "$WORK/pk-$kind"/pack-*.pack)"
  mkdir "$WORK/gi-$kind"; cp "$PACK" "$WORK/gi-$kind/"
  git index-pack --strict "$WORK/gi-$kind/$(basename "$PACK")" > "$WORK/gi-$kind.out" 2>&1 || fail "git index-pack --strict rejects minipack's $kind pack: $(cat "$WORK/gi-$kind.out")"
  check same-bytes "${PACK%.pack}.idx" "$WORK/gi-$kind/$(basename "${PACK%.pack}").idx" "$kind pack: minipack .idx vs git index-pack .idx" || fail ".idx differs ($kind)"
  git verify-pack -v "${PACK%.pack}.idx" > "$WORK/gv-$kind.txt" 2>&1 || fail "git verify-pack rejects minipack's $kind pack: $(tail -n 5 "$WORK/gv-$kind.txt")"
  check pack-objects "$W" "$WORK/gv-$kind.txt" "$WORK/pk-$kind.json" || fail "pack-objects ($kind) content"
  mp verify-pack -v "${PACK%.pack}.idx" --json > "$WORK/mv-$kind.json" 2> "$WORK/mv-$kind.err" || fail "minipack verify-pack ($kind): $(cat "$WORK/mv-$kind.err")"
  check verify "$WORK/gv-$kind.txt" "$WORK/mv-$kind.json" "$kind pack" deltas || fail "verify-pack -v ($kind)"
done
git init -q --bare "$WORK/chk.git"
cp "$WORK"/pk-ofs/pack-* "$WORK/chk.git/objects/pack/"
git -C "$W" for-each-ref --format='%(objectname) %(refname)' > "$WORK/work-refs.txt"
while read -r oid ref; do git -C "$WORK/chk.git" update-ref "$ref" "$oid"; done < "$WORK/work-refs.txt"
git -C "$WORK/chk.git" fsck --strict --no-dangling --no-progress > "$WORK/fsck-chk.txt" 2>&1 || fail "git fsck on a repository made of minipack's pack: $(cat "$WORK/fsck-chk.txt")"
check objects "$WORK/chk.git" "$W" "bare repository built from minipack's pack vs the source" || fail "objects in the repository from minipack's pack"

echo "▸ packs Git wrote: minipack computes the same .idx, the same verify-pack rows, and unpacks them"
GPACK="$(ls "$WORK"/git-full/.git/objects/pack/pack-*.pack | head -n 1)"
mp index-pack "$GPACK" -o "$WORK/git-clone-pack.idx" > "$WORK/mi.out" 2>&1 || fail "minipack index-pack on git clone's pack: $(cat "$WORK/mi.out")"
check same-bytes "$WORK/git-clone-pack.idx" "${GPACK%.pack}.idx" "git clone's received pack: minipack .idx vs git's" || fail ".idx of git clone's pack"
cp -R "$W" "$WORK/work-ref"
git -C "$WORK/work-ref" -c repack.useDeltaBaseOffset=false repack -adfq
GPACK="$(ls "$WORK"/work-ref/.git/objects/pack/pack-*.pack)"
mp index-pack "$GPACK" -o "$WORK/git-ref-pack.idx" > "$WORK/mi2.out" 2>&1 || fail "minipack index-pack on a REF_DELTA pack: $(cat "$WORK/mi2.out")"
check same-bytes "$WORK/git-ref-pack.idx" "${GPACK%.pack}.idx" "git repack REF_DELTA pack: minipack .idx vs git's" || fail ".idx of git's REF_DELTA pack"
git verify-pack -v "${GPACK%.pack}.idx" > "$WORK/gv-git.txt" 2>&1 || fail "git verify-pack on its own pack: $(tail -n 3 "$WORK/gv-git.txt")"
mp verify-pack -v "${GPACK%.pack}.idx" --json > "$WORK/mv-git.json" 2> "$WORK/mv-git.err" || fail "minipack verify-pack on git's pack: $(cat "$WORK/mv-git.err")"
check verify "$WORK/gv-git.txt" "$WORK/mv-git.json" "git's REF_DELTA pack" deltas || fail "verify-pack -v on git's pack"
git init -q "$WORK/unpacked"
mp unpack-objects "$GPACK" --repo "$WORK/unpacked" > "$WORK/unpack.out" 2>&1 || fail "minipack unpack-objects: $(cat "$WORK/unpack.out")"
check batch "$WORK/unpacked" "$WORK/work-ref" || fail "objects unpacked from git's pack"

echo "▸ minipack clone --keep-pack: the received pack is kept with an .idx git index-pack agrees with"
mp clone "$URL" "$WORK/mp-kept" --keep-pack > "$WORK/kept.out" 2>&1 || fail "minipack clone --keep-pack: $(cat "$WORK/kept.out")"
KPACK="$(ls "$WORK"/mp-kept/.git/objects/pack/pack-*.pack)"
git index-pack -o "$WORK/kept-git.idx" "$KPACK" > "$WORK/kept-gi.out" 2>&1 || fail "git index-pack on the kept pack: $(cat "$WORK/kept-gi.out")"
check same-bytes "${KPACK%.pack}.idx" "$WORK/kept-git.idx" "kept clone pack: minipack .idx vs git index-pack" || fail ".idx of the kept pack"
git -C "$WORK/mp-kept" fsck --strict --no-progress > "$WORK/fsck-kept.txt" 2>&1 || fail "git fsck on the --keep-pack clone: $(cat "$WORK/fsck-kept.txt")"
git -C "$WORK/mp-kept" checkout -q main 2> "$WORK/checkout.err" || fail "git checkout in minipack's clone: $(cat "$WORK/checkout.err")"
[[ "$(cat "$WORK/mp-kept/VERSION")" == "v6" ]] || fail "checked-out VERSION is $(cat "$WORK/mp-kept/VERSION"), expected v6"
echo "  git fsck and git checkout work in minipack's clone (VERSION = v6)"

echo "SMOKE OK: git/packfiles-transport"
