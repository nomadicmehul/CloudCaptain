---
name: release-checklist
description: Cut a SemVer release from Conventional Commits: lint the history, compute and justify the next version, review the changelog, create a signed annotated tag, verify what was published, and recover from release mistakes
version: 1.0.0
phase: cicd
lesson: cicd/release-versioning
tags: [release, semver, conventional-commits, changelog, git-tag, git-cliff, release-please, semantic-release]
---

# Release checklist

Use this when someone says "cut a release", "what version is next", "the changelog is wrong" or "we tagged the wrong commit". The version is computed from the commits since the last tag, never chosen by feel, and a published tag never moves.

## 1. Establish the facts

```bash
git fetch --tags --prune origin
git status --short --branch                      # clean tree, on the release branch, up to date
git describe --tags --abbrev=0                   # latest tag reachable from HEAD
git tag --list 'v*' --sort=-v:refname | head -5  # latest tags by version, not by date
git log --oneline "$(git describe --tags --abbrev=0)"..HEAD
```

Write down: the release branch, the latest stable tag, the latest pre-release tag if any, the tag prefix (`v`, `api/v`), and the bump settings the project uses (does a breaking change leave 0.x? does a feature bump the minor in 0.x?). If the project has a `cliff.toml`, `release-please-config.json` or `.releaserc`, those settings live there; read them, do not assume defaults.

## 2. Lint the commits

```bash
python3 minirelease.py log --strict             # or: npx commitlint --from "$(git describe --tags --abbrev=0)"
```

| Finding | Do |
|---|---|
| a non-conventional commit that changed behaviour | stop; get the author to describe it, and record it in the changelog by hand or reword before merging next time |
| a non-conventional merge commit | ignore; the merged commits carry the information |
| `fixup!` or `squash!` commits | the branch was not squashed; fix the history before releasing from it |
| a `refactor:` or `chore:` whose body describes a removed field, flag or endpoint | treat it as breaking; the commit lied, the release must not |
| `BREAKING CHANGE` written in lower case | not a breaking footer by the specification; treat it as breaking anyway and fix the commit-lint rule |

## 3. Compute the next version and say why

```bash
python3 minirelease.py next --json | python3 -m json.tool
git-cliff --bumped-version                       # must agree; if not, find the setting that differs
```

| Highest impact since the last stable tag | 1.0 and later | 0.y.z (y > 0) | 0.0.z |
|---|---|---|---|
| breaking change | major | minor (unless the project decided to leave 0.x) | patch |
| feature | minor | patch (unless features always bump minor) | patch |
| fix, perf, refactor, revert | patch | patch | patch |
| only docs, ci, chore, style, test, build | no release | no release | no release |

Then check the number against the public interface, not only the commits: diff the exported API (`git diff LAST_TAG -- src/**/__init__.py api/openapi.yaml`, the npm `exports`, the CLI `--help` output). A removed or renamed public name with no breaking commit means the number is wrong; fix it now.

## 4. Pre-releases, promotion and hotfixes

- Release candidate: `next --pre rc` gives `X.Y.Z-rc.N`, counting existing tags on that channel for the same core. Channels must sort upward (`alpha` < `beta` < `rc`); never go back.
- Promotion: release without `--pre` from the same commits; the stable version is computed from everything since the last stable tag.
- Hotfix of an old line: branch from the old tag (`git switch -c release/1.4 v1.4.0`), cherry-pick the `fix:` commit, release `1.4.1` from that branch with the tag prefix and the branch named in the release record. The main line keeps its own numbering.

## 5. Generate and review the changelog section

```bash
python3 minirelease.py changelog --bump --compare-url https://github.com/OWNER/REPO > /tmp/CHANGELOG.md
git-cliff --unreleased --bump --strip all
```

Review as a consumer: every breaking entry has an upgrade note (what to change, not only what changed), deprecations name the version that will remove them, security fixes name the advisory if there is one, and nothing internal (CI, refactors with no effect) is listed under Added. Edit the commit-to-section rules, not the generated file, when a category is wrong, or the next release repeats the mistake.

## 6. Commit, tag, push

```bash
python3 minirelease.py release --changelog CHANGELOG.md --commit --sign --dry-run
python3 minirelease.py release --changelog CHANGELOG.md --commit --sign
git -c gpg.ssh.allowedSignersFile=.github/allowed_signers tag -v "$(git describe --tags --abbrev=0)"
git push origin HEAD --follow-tags
```

The tag is annotated (`git cat-file -t TAG` prints `tag`), signed, created on the release commit that contains the changelog, and its message keeps the section headings (tag with `--cleanup=whitespace`). With release-please, merge the release pull request instead; with semantic-release, push to the release branch and let it tag.

## 7. Verify what was published

- The CI run triggered by the tag built from exactly `git rev-parse TAG^{commit}`; compare the commit in the build log or the provenance.
- The published artifact carries the version (`pip download PKG==X --no-deps`, `npm view PKG@X version`, the image label `org.opencontainers.image.version`).
- The GitHub or GitLab release text equals the changelog section.
- Consumers pinned with `^`, `~` or `~=` will pick this release up on their next lockfile refresh: for a minor or patch, that is the promise you just made.

## 8. When something went wrong

| Situation | Do |
|---|---|
| tagged the wrong commit, not pushed yet | `git tag -d TAG` and tag again |
| wrong tag already pushed, nothing published | delete it on the remote (`git push origin :refs/tags/TAG`), tell everyone who fetched it, tag again; prefer releasing the next patch if in doubt |
| artifact already published with the wrong content | never republish the same version; yank it on PyPI (project settings or the API) or run `npm deprecate PKG@X "reason"` and release the next patch |
| a breaking change shipped as a minor | release a patch that restores the old behaviour, then the major with the change and an upgrade note |
| changelog entry wrong after release | fix the file in a normal commit; do not rewrite the tag message |

## 9. Report

Post: the version and tag, the previous tag, the rule that decided the bump (one sentence, as `next --json` prints it), the commit the tag points to, whether the tag is signed and by which key, the changelog section, the CI run and artifact links with the verified commit, any commit that had to be reclassified by hand and why, and for a pre-release the channel and what blocks promotion.
