#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
LESSON="$PWD"
MG="$LESSON/code/main.py"
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
command -v git >/dev/null || fail "git is not installed"
command -v python3 >/dev/null || fail "python3 is not installed"
git --version

export GIT_CONFIG_GLOBAL="$WORK/gitconfig" GIT_CONFIG_NOSYSTEM=1
: > "$GIT_CONFIG_GLOBAL"
git config --global init.defaultBranch main
git config --global init.defaultRefFormat files
git config --global advice.addEmbeddedRepo false
git config --global advice.detachedHead false
git config --global commit.gpgsign false
git config --global tag.gpgsign false
git config --global gc.auto 0
EMPTY_TREE=4b825dc642cb6eb9a060e54bf8d69288fbee4904
HELLO=ce013625030ba8dba906f756967f9e9ca394464a

mg() { python3 "$MG" "$@"; }
newrepo() { git init -q --object-format=sha1 "$1"; }
who() {
  export GIT_AUTHOR_NAME="$1" GIT_AUTHOR_EMAIL="$2" GIT_AUTHOR_DATE="$3"
  export GIT_COMMITTER_NAME="$4" GIT_COMMITTER_EMAIL="$5" GIT_COMMITTER_DATE="$6"
}

cat > "$WORK/check.py" <<'PY'
import hashlib
import json
import os
import stat
import subprocess
import sys
import zlib


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True).stdout


def die(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def log(git_file, mg_file, *expected):
    with open(git_file, encoding="utf-8") as handle:
        rows = [line.split("\x1f") for line in handle.read().splitlines()]
    mine = load(mg_file)
    if len(rows) != len(mine):
        die(f"git log has {len(rows)} commits, minigit log has {len(mine)}")
    for row, commit in zip(rows, mine):
        oid, tree, parents, an, ae, at, ai, cn, ce, ct, ci, subject = row
        want = [oid, tree, parents, an, ae, at, ai.split()[-1], cn, ce, ct, ci.split()[-1], subject]
        got = [commit["oid"], commit["tree"], " ".join(commit["parents"]), commit["author"]["name"], commit["author"]["email"], str(commit["author"]["time"]),
               commit["author"]["tz"], commit["committer"]["name"], commit["committer"]["email"], str(commit["committer"]["time"]), commit["committer"]["tz"],
               commit["message"].split("\n")[0]]
        if got != want:
            die(f"commit {oid}: git log says {want}, minigit says {got}")
        print(f"  {oid[:7]} {an} <{ae}> {ai} | committer {cn} {ci} | {subject}")
    authors = [f"{r[3]}|{r[5]}|{r[6].split()[-1]}" for r in rows]
    if expected and authors != list(expected):
        die(f"authors and dates: expected {list(expected)}, git log shows {authors}")


def blobs(repo, rev):
    checked = 0
    for item in filter(None, git(repo, "ls-tree", "-r", "-z", rev).split(b"\0")):
        meta, path = item.split(b"\t", 1)
        mode, _, oid = meta.decode().split()
        if mode == "160000":
            continue
        full = os.path.join(repo, os.fsdecode(path))
        expected = os.fsencode(os.readlink(full)) if mode == "120000" else open(full, "rb").read()
        if git(repo, "cat-file", "blob", oid) != expected:
            die(f"git cat-file blob {oid} differs from {os.fsdecode(path)}")
        if mode == "100755" and not os.stat(full).st_mode & stat.S_IXUSR:
            die(f"{os.fsdecode(path)} is 100755 in the tree but not executable on disk")
        checked += 1
    print(f"  {checked} blobs byte-identical to the work tree (symlink blobs = link targets)")


def digest(root):
    out = {}

    def walk(folder, prefix):
        for name in sorted(os.listdir(folder)):
            full, rel = os.path.join(folder, name), prefix + name
            info = os.lstat(full)
            if stat.S_ISLNK(info.st_mode):
                out[rel] = ["link", os.readlink(full)]
            elif stat.S_ISDIR(info.st_mode):
                out[rel + "/"] = ["dir"]
                walk(full, rel + "/")
            else:
                with open(full, "rb") as handle:
                    out[rel] = ["file", bool(info.st_mode & stat.S_IXUSR), hashlib.sha256(handle.read()).hexdigest()]

    walk(root, "")
    return out


def samedir(a, b):
    left, right = digest(a), digest(b)
    if left != right:
        keys = sorted(k for k in set(left) | set(right) if left.get(k) != right.get(k))
        die(f"checkouts differ at {keys}: minigit {[left.get(k) for k in keys]} git archive {[right.get(k) for k in keys]}")
    print(f"  {len(left)} paths identical (type, executable bit, content or link target)")


def lstree(repo, rev, mg_file):
    theirs = set()
    for item in filter(None, git(repo, "ls-tree", "-r", "-z", rev).split(b"\0")):
        meta, path = item.split(b"\t", 1)
        theirs.add(tuple(meta.decode().split()) + (os.fsdecode(path),))
    mine = {(e["mode"], e["type"], e["oid"], e["path"]) for e in load(mg_file)}
    if mine != theirs:
        die(f"ls-tree differs: only git {sorted(theirs - mine)}, only minigit {sorted(mine - theirs)}")
    print(f"  ls-tree -r: {len(mine)} identical entries, modes {sorted({e[0] for e in mine})}")


def reflog(git_file, mg_file):
    with open(git_file, encoding="utf-8") as handle:
        theirs = [tuple(line.split("\x1f", 1)) for line in handle.read().splitlines()]
    mine = [(r["new"], r["message"]) for r in load(mg_file)]
    if mine != theirs:
        die(f"reflog differs: git {theirs}, minigit {mine}")
    for oid, message in theirs:
        print(f"  {oid[:7]} {message}")


def lines(git_file, mg_file):
    with open(git_file, encoding="utf-8") as a, open(mg_file, encoding="utf-8") as b:
        theirs, mine = sorted(a.read().splitlines()), sorted(b.read().splitlines())
    if theirs != mine:
        die(f"git: {theirs[:5]}... minigit: {mine[:5]}...; only git {sorted(set(theirs) - set(mine))}, only minigit {sorted(set(mine) - set(theirs))}")
    print(f"  {len(mine)} objects: identical ID, type and size")


def counts(git_file, mg_file):
    with open(git_file, encoding="utf-8") as handle:
        theirs = dict(line.split(": ", 1) for line in handle.read().splitlines())
    mine = load(mg_file)
    for key in ("count", "size", "in-pack", "packs"):
        if int(theirs[key]) != mine[key]:
            die(f"count-objects {key}: git {theirs[key]}, minigit {mine[key]}")
    print(f"  count {mine['count']}, size {mine['size']} KiB, in-pack {mine['in-pack']}, packs {mine['packs']}")


def loose(path, expected):
    with open(path, "rb") as handle:
        raw = zlib.decompress(handle.read())
    if raw != expected.encode().decode("unicode_escape").encode("latin-1"):
        die(f"{path} decompresses to {raw!r}")
    print(f"  zlib.decompress({os.path.basename(os.path.dirname(path))}/{os.path.basename(path)[:8]}...) = {raw!r}")


def field(path, *keys):
    value = load(path)
    for key in keys:
        value = value[int(key)] if isinstance(value, list) else value[key]
    print(json.dumps(value) if isinstance(value, (dict, list)) else value)


COMMANDS = {"log": log, "blobs": blobs, "samedir": samedir, "lstree": lstree, "reflog": reflog, "lines": lines, "counts": counts, "loose": loose, "field": field}
COMMANDS[sys.argv[1]](*sys.argv[2:])
PY
check() { python3 "$WORK/check.py" "$@"; }
LOGFMT='--format=%H%x1f%T%x1f%P%x1f%an%x1f%ae%x1f%at%x1f%ai%x1f%cn%x1f%ce%x1f%ct%x1f%ci%x1f%s'

echo "▸ minigit alone builds a repository: files, an executable, a symlink, nested directories, binary data, three commits, an annotated tag"
M="$WORK/mini"
mg init "$M" > "$WORK/init.txt" || fail "minigit init: $(cat "$WORK/init.txt")"
mkdir -p "$M/bin" "$M/src/app/util" "$M/docs/v1"
printf '# demo\n' > "$M/README.md"
printf '#!/bin/sh\necho run\n' > "$M/bin/run.sh"
chmod 755 "$M/bin/run.sh"
printf 'guide v1\n' > "$M/docs/v1/guide.md"
ln -s v1/guide.md "$M/docs/latest"
printf 'print("hi")\n' > "$M/src/app/main.py"
printf 'X = 1\n' > "$M/src/app/util/helpers.py"
printf 'bin\000ary\377\n' > "$M/data.bin"
who "Ada Lovelace" ada@example.invalid "2026-01-05T09:00:00+01:00" "Grace Hopper" grace@example.invalid "2026-01-05T09:30:00+01:00"
mg -C "$M" commit -m "Add the skeleton" > "$WORK/c.txt" || fail "commit 1: $(cat "$WORK/c.txt")"
cat "$WORK/c.txt"
printf 'more\n' >> "$M/README.md"
who "Linus Torvalds" linus@example.invalid "2026-02-01T08:00:00-05:00" "Linus Torvalds" linus@example.invalid "2026-02-01T08:05:00-05:00"
mg -C "$M" commit -m "Extend the README" > "$WORK/c.txt" || fail "commit 2: $(cat "$WORK/c.txt")"
cat "$WORK/c.txt"
mkdir -p "$M/docs/v2"
printf 'guide v2\n' > "$M/docs/v2/guide.md"
rm "$M/docs/latest"
ln -s v2/guide.md "$M/docs/latest"
who "Margaret Hamilton" margaret@example.invalid "@1772000000 +0530" "Margaret Hamilton" margaret@example.invalid "@1772000100 +0530"
mg -C "$M" commit -m "Point latest at v2" > "$WORK/c.txt" || fail "commit 3: $(cat "$WORK/c.txt")"
cat "$WORK/c.txt"
mg -C "$M" tag -a v1.0 -m "First release" > "$WORK/tag.txt" || fail "tag: $(cat "$WORK/tag.txt")"
find "$M/.git" -type f | sed "s|$M/||" | sort > "$WORK/files.txt"
echo "  $(grep -c '^.git/objects/' "$WORK/files.txt") loose objects, refs: $(grep '^.git/refs/' "$WORK/files.txt" | tr '\n' ' ')"

echo "▸ git fsck --strict accepts every object minigit wrote"
CODE=0
git -C "$M" fsck --strict --no-progress > "$WORK/fsck-mini.txt" 2>&1 || CODE=$?
[[ "$CODE" == "0" ]] || fail "git fsck --strict exit $CODE: $(cat "$WORK/fsck-mini.txt")"
if grep -Eq '^(error|warning|missing|broken|dangling)' "$WORK/fsck-mini.txt"; then fail "git fsck reported: $(cat "$WORK/fsck-mini.txt")"; fi
echo "  git fsck --strict: exit 0, $(wc -l < "$WORK/fsck-mini.txt" | tr -d ' ') lines of output"

echo "▸ git log shows the three commits with the authors, Unix times and zones minigit recorded"
git -C "$M" log "$LOGFMT" > "$WORK/git-log.txt" || fail "git log failed"
mg -C "$M" log --json > "$WORK/mg-log.json" || fail "minigit log: $(cat "$WORK/mg-log.json")"
check log "$WORK/git-log.txt" "$WORK/mg-log.json" "Margaret Hamilton|1772000000|+0530" "Linus Torvalds|1769950800|-0500" "Ada Lovelace|1767600000|+0100" || fail "log comparison"

echo "▸ git cat-file returns byte-identical content; modes are 100755 and 120000 where they should be"
check blobs "$M" HEAD || fail "blob comparison"
git -C "$M" cat-file -p HEAD:data.bin > "$WORK/data.out"
cmp "$M/data.bin" "$WORK/data.out" || fail "HEAD:data.bin differs from the file"
git -C "$M" ls-tree -r HEAD > "$WORK/lstree.txt"
grep -q $'^100755 blob [0-9a-f]*\tbin/run.sh$' "$WORK/lstree.txt" || fail "bin/run.sh should be 100755: $(cat "$WORK/lstree.txt")"
grep -q $'^120000 blob [0-9a-f]*\tdocs/latest$' "$WORK/lstree.txt" || fail "docs/latest should be 120000: $(cat "$WORK/lstree.txt")"
[[ "$(git -C "$M" cat-file -p HEAD:docs/latest)" == "v2/guide.md" ]] || fail "symlink blob should hold the link target"
[[ "$(git -C "$M" cat-file -t v1.0)" == "tag" ]] || fail "v1.0 should be an annotated tag object"
git -C "$M" cat-file -p v1.0 > "$WORK/tag-body.txt"
grep -qx "object $(git -C "$M" rev-parse HEAD)" "$WORK/tag-body.txt" || fail "tag should point at HEAD: $(cat "$WORK/tag-body.txt")"
mg -C "$M" ls-tree -r --json HEAD > "$WORK/mg-lstree.json"
check lstree "$M" HEAD "$WORK/mg-lstree.json" || fail "ls-tree comparison"

echo "▸ rev-parse, reflog and count-objects agree between git and minigit"
REVS=(HEAD HEAD~2 'HEAD^{tree}' 'v1.0^{commit}' 'v1.0^{}' HEAD:src/app 'main@{1}' 'HEAD@{2}')
git -C "$M" rev-parse "${REVS[@]}" > "$WORK/git-revs.txt" || fail "git rev-parse failed"
mg -C "$M" rev-parse "${REVS[@]}" > "$WORK/mg-revs.txt" 2> "$WORK/mg-revs.err" || fail "minigit rev-parse: $(cat "$WORK/mg-revs.err")"
cmp -s "$WORK/git-revs.txt" "$WORK/mg-revs.txt" || fail "rev-parse differs: git $(tr '\n' ' ' < "$WORK/git-revs.txt") minigit $(tr '\n' ' ' < "$WORK/mg-revs.txt")"
echo "  ${#REVS[@]} revisions resolve to the same IDs"
git -C "$M" reflog show --format='%H%x1f%gs' HEAD > "$WORK/git-reflog.txt"
mg -C "$M" reflog --json > "$WORK/mg-reflog.json"
check reflog "$WORK/git-reflog.txt" "$WORK/mg-reflog.json" || fail "reflog comparison"
git -C "$M" count-objects -v > "$WORK/git-count.txt"
mg -C "$M" count-objects --json > "$WORK/mg-count.json"
check counts "$WORK/git-count.txt" "$WORK/mg-count.json" || fail "count-objects comparison"

echo "▸ minigit checkout of HEAD~1 equals git archive of HEAD~1"
mg -C "$M" checkout HEAD~1 "$WORK/co-mini" > "$WORK/co.txt" || fail "minigit checkout: $(cat "$WORK/co.txt")"
cat "$WORK/co.txt"
mkdir "$WORK/co-git"
git -C "$M" archive HEAD~1 > "$WORK/head1.tar"
tar -x -C "$WORK/co-git" -f "$WORK/head1.tar"
check samedir "$WORK/co-mini" "$WORK/co-git" || fail "checkout comparison"

echo "▸ the work tree is exactly HEAD: git reset rebuilds the index from minigit's tree and git status is clean"
git -C "$M" reset -q
git -C "$M" status --porcelain > "$WORK/status.txt"
[[ ! -s "$WORK/status.txt" ]] || fail "git status should be clean: $(cat "$WORK/status.txt")"
mg -C "$M" fsck > "$WORK/mg-fsck-mini.txt" || fail "minigit fsck on its own repository: $(cat "$WORK/mg-fsck-mini.txt")"
tail -1 "$WORK/mg-fsck-mini.txt"

echo "▸ git builds a repository (merge, annotated tag, embedded repository, symlink); minigit reads every object and computes the same IDs"
G="$WORK/real"
newrepo "$G"
newrepo "$G/vendor/lib"
printf 'lib\n' > "$G/vendor/lib/lib.txt"
who "Ada Lovelace" ada@example.invalid "2026-03-01T10:00:00+01:00" "Ada Lovelace" ada@example.invalid "2026-03-01T10:00:00+01:00"
git -C "$G/vendor/lib" add -A
git -C "$G/vendor/lib" commit -q -m "lib"
mkdir -p "$G/d/e"
printf 'a\n' > "$G/a.txt"
printf '#!/bin/sh\n' > "$G/run.sh"
chmod 755 "$G/run.sh"
ln -s a.txt "$G/link"
printf 'deep\n' > "$G/d/e/deep.txt"
printf '\000\001\002' > "$G/data.bin"
git -C "$G" add -A
git -C "$G" commit -q -m "one"
git -C "$G" checkout -q -b side
printf 'side\n' > "$G/side.txt"
git -C "$G" add side.txt
who "Linus Torvalds" linus@example.invalid "2026-03-02T09:00:00-08:00" "Linus Torvalds" linus@example.invalid "2026-03-02T09:00:00-08:00"
git -C "$G" commit -q -m "side work"
git -C "$G" checkout -q main
printf 'a2\n' >> "$G/a.txt"
git -C "$G" commit -q -am "two"
git -C "$G" merge -q --no-ff side -m "Merge side"
git -C "$G" tag -a v1 -m "release"
git -C "$G" cat-file --batch-all-objects --batch-check > "$WORK/git-objects.txt"
cut -d' ' -f1 "$WORK/git-objects.txt" | mg -C "$G" cat-file --batch-check > "$WORK/mg-objects.txt" || fail "minigit batch-check failed"
check lines "$WORK/git-objects.txt" "$WORK/mg-objects.txt" || fail "batch-check comparison"
mg -C "$G" fsck --json > "$WORK/mg-fsck.json" || fail "minigit fsck on git's repository: $(cat "$WORK/mg-fsck.json")"
[[ "$(check field "$WORK/mg-fsck.json" reachable)" == "$(wc -l < "$WORK/git-objects.txt" | tr -d ' ')" ]] || fail "every object should be reachable: $(cat "$WORK/mg-fsck.json")"
echo "  minigit fsck: ok=$(check field "$WORK/mg-fsck.json" ok), reachable=$(check field "$WORK/mg-fsck.json" reachable), problems=$(check field "$WORK/mg-fsck.json" problems)"
GT="$(git -C "$G" write-tree)"
MT="$(mg -C "$G" write-tree)"
[[ "$GT" == "$MT" ]] || fail "same content, different tree: git $GT minigit $MT"
echo "  write-tree of the same work tree: $MT from both (gitlink vendor/lib included)"
[[ "$(git -C "$G" hash-object data.bin)" == "$(mg -C "$G" hash-object data.bin)" ]] || fail "hash-object of data.bin differs"
TREE="$(git -C "$G" rev-parse 'HEAD^{tree}')"
GC="$(git -C "$G" commit-tree "$TREE" -p HEAD -m "same input")"
MC="$(mg -C "$G" commit-tree "$TREE" -p HEAD -m "same input")"
[[ "$GC" == "$MC" ]] || fail "commit-tree with identical input: git $GC minigit $MC"
echo "  commit-tree with the same tree, parent, identities, dates and message: $MC from both"
git -C "$G" log --first-parent --format=%H > "$WORK/git-first.txt"
mg -C "$G" log --json > "$WORK/mg-first.json"
python3 -c 'import json,sys; print("\n".join(c["oid"] for c in json.load(open(sys.argv[1]))))' "$WORK/mg-first.json" > "$WORK/mg-first.txt"
cmp -s "$WORK/git-first.txt" "$WORK/mg-first.txt" || fail "first-parent log differs: git $(tr '\n' ' ' < "$WORK/git-first.txt") minigit $(tr '\n' ' ' < "$WORK/mg-first.txt")"
[[ "$(mg -C "$G" rev-parse 'HEAD^2')" == "$(git -C "$G" rev-parse side)" ]] || fail "HEAD^2 should be the side branch"
mg -C "$G" ls-tree -r --json HEAD > "$WORK/mg-real-lstree.json"
check lstree "$G" HEAD "$WORK/mg-real-lstree.json" || fail "ls-tree comparison on git's repository"
mg -C "$G" cat-file -p v1 --json > "$WORK/mg-tag.json"
[[ "$(check field "$WORK/mg-tag.json" object)" == "$(git -C "$G" rev-parse HEAD)" ]] || fail "minigit should read the tag's object: $(cat "$WORK/mg-tag.json")"

echo "▸ the sorting rule: a directory sorts as if its name ended in /, and an empty directory is not stored"
T="$WORK/sort"
newrepo "$T"
mkdir -p "$T/foo" "$T/empty"
printf 'b\n' > "$T/foo/bar.txt"
printf 'c\n' > "$T/foo.c"
printf 'd\n' > "$T/foo-bar"
printf 'e\n' > "$T/foo0"
git -C "$T" add -A
GT="$(git -C "$T" write-tree)"
MT="$(mg -C "$T" write-tree)"
[[ "$GT" == "$MT" ]] || fail "sorted tree: git $GT minigit $MT"
git -C "$T" ls-tree "$GT" > "$WORK/sort-ls.txt"
cut -f2 "$WORK/sort-ls.txt" | tr '\n' ' ' > "$WORK/sort-names.txt"
[[ "$(cat "$WORK/sort-names.txt")" == "foo-bar foo.c foo foo0 " ]] || fail "Git's order should be foo-bar foo.c foo foo0, got $(cat "$WORK/sort-names.txt")"
echo "  git write-tree = minigit write-tree = $MT; order: $(cat "$WORK/sort-names.txt")(empty/ left out by both)"
MK="$(mg -C "$T" write-tree --keep-empty-dirs)"
{ cat "$WORK/sort-ls.txt"; printf '040000 tree %s\tempty\n' "$EMPTY_TREE"; } > "$WORK/sort-mktree.txt"
GK="$(git -C "$T" mktree < "$WORK/sort-mktree.txt")"
[[ "$MK" == "$GK" ]] || fail "tree with an empty directory: minigit $MK git mktree $GK"
echo "  with --keep-empty-dirs: $MK, equal to git mktree with an empty-tree entry"
LC_ALL=C sort -t $'\t' -k2,2 "$WORK/sort-ls.txt" > "$WORK/naive.txt"
NAIVE="$(mg -C "$T" mktree --as-given < "$WORK/naive.txt")"
[[ "$NAIVE" != "$GT" ]] || fail "a plain name sort should give a different tree ID"
CODE=0
git -C "$T" fsck --no-progress > "$WORK/fsck-sort.txt" 2>&1 || CODE=$?
grep -q "tree $NAIVE: treeNotSorted" "$WORK/fsck-sort.txt" || fail "git fsck should name $NAIVE as treeNotSorted (exit $CODE): $(cat "$WORK/fsck-sort.txt")"
echo "  plain name order ($(cut -f2 "$WORK/naive.txt" | tr '\n' ' ')) gives $NAIVE; git fsck exit $CODE: $(grep treeNotSorted "$WORK/fsck-sort.txt")"

echo "▸ Use It: objects, refs and the reflog by hand with git's plumbing, cross-checked with minigit"
P="$WORK/plumbing"
newrepo "$P"
[[ "$(cat "$P/.git/HEAD")" == "ref: refs/heads/main" ]] || fail "HEAD of a new repository: $(cat "$P/.git/HEAD")"
B="$(printf 'hello\n' | git -C "$P" hash-object -w --stdin)"
[[ "$B" == "$HELLO" ]] || fail "hash-object of hello: $B"
SHA="$(printf 'blob 6\000hello\n' | sha1sum | cut -d' ' -f1)"
[[ "$SHA" == "$HELLO" ]] || fail "sha1sum of the framed blob: $SHA"
check loose "$P/.git/objects/${HELLO:0:2}/${HELLO:2}" 'blob 6\x00hello\n' || fail "loose object layout"
[[ "$(git -C "$P" cat-file -t ce01)" == "blob" && "$(git -C "$P" cat-file -s ce01)" == "6" ]] || fail "cat-file -t/-s"
git -C "$P" update-index --add --cacheinfo "100644,$B,hello.txt"
PT="$(git -C "$P" write-tree)"
C1="$(git -C "$P" commit-tree "$PT" -m first)"
[[ "$(mg -C "$P" commit-tree "$PT" -m first)" == "$C1" ]] || fail "commit-tree differs for identical input"
git -C "$P" update-ref refs/heads/main "$C1"
C2="$(git -C "$P" commit-tree "$PT" -p "$C1" -m second)"
git -C "$P" update-ref -m "manual: second" refs/heads/main "$C2" "$C1"
if git -C "$P" update-ref refs/heads/main "$C1" "$C1" 2> "$WORK/stale-git.txt"; then fail "git update-ref with a stale old value should fail"; fi
CODE=0
mg -C "$P" update-ref refs/heads/main "$C1" "$C1" 2> "$WORK/stale-mg.txt" || CODE=$?
[[ "$CODE" == "2" ]] && grep -q "but expected" "$WORK/stale-mg.txt" || fail "minigit update-ref with a stale old value: exit $CODE $(cat "$WORK/stale-mg.txt")"
echo "  compare-and-swap refused by both: $(head -1 "$WORK/stale-git.txt")"
mg -C "$P" update-ref -m "minigit: topic" refs/heads/topic "$C1"
[[ "$(git -C "$P" rev-parse topic)" == "$C1" ]] || fail "git should read the ref minigit wrote"
git -C "$P" reflog show --format='%gs' topic > "$WORK/topic-log.txt"
grep -qx "minigit: topic" "$WORK/topic-log.txt" || fail "git should read minigit's reflog line: $(cat "$WORK/topic-log.txt")"
mg -C "$P" symbolic-ref HEAD refs/heads/topic
[[ "$(git -C "$P" symbolic-ref HEAD)" == "refs/heads/topic" && "$(git -C "$P" rev-parse HEAD)" == "$C1" ]] || fail "symbolic-ref written by minigit"
git -C "$P" symbolic-ref HEAD refs/heads/main
git -C "$P" pack-refs --all
[[ ! -e "$P/.git/refs/heads/main" ]] || fail "pack-refs --all should remove the loose ref file"
[[ "$(mg -C "$P" rev-parse main)" == "$C2" ]] || fail "minigit should fall back to packed-refs"
git -C "$P" show-ref > "$WORK/git-show-ref.txt"
mg -C "$P" show-ref > "$WORK/mg-show-ref.txt"
cmp -s "$WORK/git-show-ref.txt" "$WORK/mg-show-ref.txt" || fail "show-ref differs: git $(cat "$WORK/git-show-ref.txt") minigit $(cat "$WORK/mg-show-ref.txt")"
git -C "$P" fsck --strict --no-progress > "$WORK/fsck-p.txt" 2>&1 || fail "git fsck on the plumbing repository: $(cat "$WORK/fsck-p.txt")"
echo "  refs from packed-refs: $(tr '\n' ' ' < "$WORK/mg-show-ref.txt")"
git -C "$P" gc -q
git -C "$P" count-objects -v > "$WORK/git-count-p.txt"
mg -C "$P" count-objects --json > "$WORK/mg-count-p.json"
check counts "$WORK/git-count-p.txt" "$WORK/mg-count-p.json" || fail "count-objects after gc"
CODE=0
mg -C "$P" cat-file -t main > /dev/null 2> "$WORK/packed.err" || CODE=$?
[[ "$CODE" == "2" ]] && grep -q "packfile" "$WORK/packed.err" || fail "after gc minigit should name the packfile: exit $CODE $(cat "$WORK/packed.err")"
echo "  after git gc: $(cat "$WORK/packed.err")"

echo "▸ SHA-256 object format: same IDs from git and minigit, and git accepts a SHA-256 repository minigit wrote"
if git init -q --object-format=sha256 "$WORK/s256" 2> "$WORK/s256.err"; then
  printf 'hi\n' > "$WORK/s256/hi"
  GH="$(git -C "$WORK/s256" hash-object hi)"
  MH="$(mg -C "$WORK/s256" hash-object hi)"
  [[ "$GH" == "$MH" && "${#MH}" == "64" ]] || fail "SHA-256 blob ID: git $GH minigit $MH"
  git -C "$WORK/s256" add hi
  [[ "$(git -C "$WORK/s256" write-tree)" == "$(mg -C "$WORK/s256" write-tree)" ]] || fail "SHA-256 tree IDs differ"
  mg init --object-format sha256 "$WORK/m256" > /dev/null
  printf 'x\n' > "$WORK/m256/x"
  mg -C "$WORK/m256" commit -m "sha256 root" > /dev/null || fail "minigit commit in a SHA-256 repository"
  git -C "$WORK/m256" fsck --strict --no-progress > "$WORK/fsck-256.txt" 2>&1 || fail "git fsck on minigit's SHA-256 repository: $(cat "$WORK/fsck-256.txt")"
  [[ "$(git -C "$WORK/m256" rev-parse --show-object-format)" == "sha256" && "$(git -C "$WORK/m256" rev-parse HEAD)" == "$(mg -C "$WORK/m256" rev-parse HEAD)" ]] || fail "SHA-256 HEAD differs"
  echo "  blob $MH; commit $(mg -C "$WORK/m256" rev-parse HEAD)"
else
  echo "  (this git cannot create SHA-256 repositories: $(cat "$WORK/s256.err"); skipped)"
fi

echo "SMOKE OK: git/object-store"
