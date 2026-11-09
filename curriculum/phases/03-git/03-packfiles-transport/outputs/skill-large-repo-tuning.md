---
name: large-repo-tuning
description: Make clones and fetches of a large Git repository fast and correct for CI, release jobs and developers by measuring what is transferred, choosing depth, filters, sparse checkout or LFS per consumer, tuning the server, and verifying the result in a packet trace
version: 1.0.0
phase: git
lesson: git/packfiles-transport
tags: [git, packfile, shallow-clone, partial-clone, sparse-checkout, git-lfs, ci, performance]
---

# Large repository tuning

Use this when someone says "clones are slow", "CI spends minutes on checkout", "the repository is huge", "git describe fails in CI" or "the Git server is overloaded". A clone is a ref listing, a want and have negotiation and one packfile; every fix below changes which objects go into that pack or how cheaply the server finds them. Measure before changing anything, and change one consumer at a time.

## 1. Measure

Run these against a fresh clone and against the server's bare repository if you can reach it.

```bash
URL=https://git.example.com/org/repo.git
time GIT_TRACE_PACKET=$PWD/clone.trace git clone --no-checkout "$URL" probe 2> clone.err
grep -E 'packet: +clone>' clone.trace | sed 's/.*packet: *//' | grep -cE '^want '      # how many tips were wanted
grep -E 'packet: +clone>' clone.trace | sed 's/.*packet: *//' | grep -E '^(deepen|filter|done)'
git ls-remote "$URL" > refs.txt && wc -l < refs.txt                                       # ref count (pull refs, tags, branches)
git -C probe count-objects -vH                                                            # size-pack is what every full clone downloads
git -C probe rev-list --objects --all \
  | git -C probe cat-file --batch-check='%(objecttype) %(objectname) %(objectsize) %(rest)' \
  | sort -k3 -n | tail -20 > largest.txt                                                  # largest blobs anywhere in history
git -C probe log --oneline | wc -l
```

On the server (or ask the hosting admin):

```bash
git -C repo.git config --get uploadpack.allowFilter         # true, or partial clones fall back to full
ls repo.git/objects/pack/*.bitmap repo.git/objects/pack/multi-pack-index 2>/dev/null
ls repo.git/objects/info/commit-graph* 2>/dev/null
git -C repo.git count-objects -v                            # many loose objects or packs means no maintenance
```

Write down: total clone time and how it splits (waiting for the server to count and compress, transfer, `Resolving deltas` on the client, checkout), `size-pack`, the ref count, the twenty largest blobs with their paths, and whether filters, bitmaps and a commit-graph exist on the server.

## 2. Classify each consumer

| Consumer | What it reads | Default choice |
|---|---|---|
| CI build and test of one commit | the tree of one commit | `--depth 1` (the `actions/checkout` default), or `--filter=blob:none` plus sparse checkout in a monorepo |
| Release, versioning or changelog job | all commits and tags since the last release | `--filter=blob:none` (every commit and tag, blobs on demand) or `fetch-depth: 0`; never a guessed depth |
| Job that reads old file contents (`log -p`, `blame`, history-wide linters) | blobs across history | a full clone, cached; avoid `blob:none`, which fetches blobs one command at a time |
| Developer laptop | full history, blame, bisect | `--filter=blob:none`, plus `sparse-checkout set --cone DIR` in a monorepo |
| Mirror, backup, migration | everything | `git clone --mirror`, refreshed with `git remote update --prune` |
| Deployment that needs files only | one tree | `git archive` from the server, or a shallow clone of a tag |

## 3. Decide from the symptom

| Symptom | Mechanism | Fix |
|---|---|---|
| Clone is far larger than the working tree | large blobs anywhere in history are reachable | keep new ones out with LFS (`git lfs track`); rewrite history (`git lfs migrate import --everything` or `git filter-repo`) only with a coordinated re-clone |
| Server CPU high during `Counting objects` | reachability computed by walking every commit | reachability bitmaps (`git repack -adb`, `git multi-pack-index write --bitmap`), `git maintenance start`; hosted products do this |
| Clients spend minutes in `Resolving deltas` | the receiver must inflate, patch and hash every object | ask for less: depth or filter; give runners more cores (`pack.threads`) |
| `warning: filtering not recognized by server, ignoring` | the server does not advertise `filter` | set `uploadpack.allowFilter=true` on the server |
| `git describe` or `merge-base` fails, or a tag is missing | the commits are below `.git/shallow` | `fetch-depth: 0`, `git fetch --unshallow --tags`, or a blob-less clone instead of a shallow one |
| A cached shallow workspace re-downloads a lot on `fetch` | the server cannot find common commits below the boundary | keep the workspace unshallow, or re-clone shallow instead of fetching |
| A partial-clone job makes hundreds of requests | each command fetches its missing blobs lazily | full clone for that job, `--filter=blob:limit=1m`, or prefetch with `git backfill` where available |
| Trace shows `# service=` followed by ref lines with capabilities after a NUL | protocol v0: a proxy dropped the `Git-Protocol` header, or the server is old | pass the header through the proxy; upgrade the server |
| Thousands of `want` lines or a huge ls-refs answer | the clone wants every ref, or the server advertises pull refs | `--single-branch`, `--no-tags` where tags are not needed, narrower refspecs |

## 4. Apply

GitHub Actions:

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 1                 # build jobs
- uses: actions/checkout@v4
  with:
    filter: blob:none              # release jobs: every commit and tag, blobs on demand
    fetch-depth: 0
- uses: actions/checkout@v4
  with:
    sparse-checkout: |
      services/api
      libs/common
```

Plain Git:

```bash
git clone --depth 1 --branch "$TAG" "$URL" build
git clone --filter=blob:none "$URL" dev
git clone --filter=blob:none --sparse "$URL" mono && git -C mono sparse-checkout set --cone services/api libs/common
git -C dev fetch --unshallow --tags                 # turn a shallow clone into a full one
git lfs install --local && git lfs track "*.psd" "*.mp4" "*.onnx" && git add .gitattributes
```

Server:

```bash
git -C repo.git config uploadpack.allowFilter true
git -C repo.git repack -adb --write-midx            # or: git -C repo.git maintenance start
git -C repo.git commit-graph write --reachable
```

## 5. Verify

- Re-run the clone with `GIT_TRACE_PACKET` and confirm the request carries what you intended (`deepen 1`, `filter blob:none`, the expected number of `want` lines) and that the server advertised the capability.
- Compare `size-pack` and the time split with the measurements from step 1.
- In the new clone: `git fsck` is clean; for partial clones `git rev-list --objects --all --missing=print | grep -c '^?'` shows the expected missing blobs; the job's own commands (`describe`, `log`, the build) succeed.
- For LFS: `git cat-file -p HEAD:path/to/file` prints a pointer (`version https://git-lfs.github.com/spec/v1`, `oid sha256:...`, `size ...`), and `git lfs ls-files` lists the file.

## 6. Report

Post: the repository and its measurements (size-pack, ref count, the largest blobs with paths, server settings found), each consumer with the option chosen and why in one sentence, the before and after numbers (clone time and bytes per consumer), the trace lines that prove the request changed, anything rejected (for example a history rewrite) with the reason, and the follow-ups that need someone else (server settings, LFS migration, proxy configuration).
