---
name: tool-release-checklist
description: Release an internal Python CLI so that someone else can install it: check pyproject.toml, derive the version from the tag, build reproducibly, verify the wheel, lock dependencies with hashes, install into a clean environment, pick the shipping form, publish without stored tokens, and report
version: 1.0.0
phase: scripting
lesson: scripting/packaging-tools
tags: [python, packaging, wheel, pip, uv, pipx, zipapp, reproducible-builds, supply-chain, release]
---

# Tool release checklist

Use this when a script is becoming a tool other people install, before tagging a release, or when an install works on one machine and not on another. Run every command from the repository root on a clean checkout of the commit you intend to release.

## 1. The project table

```bash
python3 -c 'import tomllib,json; print(json.dumps(tomllib.load(open("pyproject.toml","rb")), indent=1))' | head -60
git status --porcelain                                 # must be empty: a dirty tree is not a release
```

| Check | Accept | Stop and fix |
|---|---|---|
| `[build-system]` | `requires` names one backend with a range (`flit_core>=3.12,<5`, `hatchling>=1.27`), `build-backend` set | Missing table (pip falls back to legacy setuptools), unbounded `requires` |
| `name` | Lower case, hyphens, unique on the index you publish to | A name that differs from the import package for no reason; a name close to a popular package |
| `version` | `dynamic = ["version"]` with a VCS plugin, or one static value | The version written in two places (`__version__` and `pyproject.toml`) by hand |
| `requires-python` | The oldest version you test in CI | Missing, or lower than CI covers |
| `dependencies` | Ranges the code really supports (`>=1.1,<2`) | Exact pins (`==`) in a library or tool's metadata; that is the lock's job |
| `license` | An SPDX expression string (`"MIT"`) | The `{ text = ... }` table form, which is deprecated |
| `[project.scripts]` | `name = "package.module:function"` for every command | A `scripts/` directory of shell wrappers copied by hand |
| `readme` | Exists and renders | A path to a file that is not committed |

## 2. One version, from the tag

```bash
git describe --tags --long --dirty --match 'v[0-9]*'   # v1.2.0-0-gabc1234 on a release commit
python3 -m setuptools_scm 2>/dev/null || python3 shipit.py version --json    # or: hatch version, uv version
grep -rn "__version__ *=" src/ | head                  # must agree with the tag or be derived from metadata
```

Distance 0 and no `-dirty` is a release. Anything else gives a development version such as `1.2.1.dev3+gabc1234`, which PyPI refuses because of the `+` label. If `__version__` disagrees with the tag, replace it with `importlib.metadata.version("<name>")`.

## 3. Build twice, compare

```bash
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
python3 -m build --outdir dist-a . && python3 -m build --outdir dist-b .
sha256sum dist-a/* dist-b/*                             # each pair must be equal
```

If the hashes differ, the backend is leaking time or file order into the archive: confirm `SOURCE_DATE_EPOCH` is exported, upgrade the backend, and check for generated files (version files written at build time with a timestamp).

## 4. Look inside the wheel

```bash
WHEEL=$(ls dist-a/*.whl)
python3 -m zipfile -l "$WHEEL"                          # only your package and one .dist-info/
unzip -p "$WHEEL" '*.dist-info/METADATA' | sed -n '1,/^$/p'
unzip -p "$WHEEL" '*.dist-info/entry_points.txt'
python3 -m twine check dist-a/*                         # metadata and README render on PyPI
```

Flag: tests, fixtures, `.env` files or `__pycache__` in the archive; a missing `Requires-Dist` for an import the code makes; a `Requires-Python` the CI matrix does not cover; a platform tag other than `py3-none-any` for pure-Python code.

## 5. Lock what it needs, with hashes

```bash
uv pip compile pyproject.toml --generate-hashes -o requirements.lock      # or pip-compile --generate-hashes, or uv lock
grep -c -- '--hash=sha256:' requirements.lock                             # one or more per pinned package
```

The lock is for the environments you control (CI, the release runner, the zipapp); the wheel keeps ranges so it can coexist with other tools.

## 6. Install it the way a user will

```bash
python3 -m venv /tmp/release-check && . /tmp/release-check/bin/activate
pip install --require-hashes --no-deps -r requirements.lock
pip install --no-deps "$WHEEL"
pip check                                               # no missing or conflicting dependency
<command> --version                                     # prints the tagged version
<command> --help >/dev/null && echo help-ok
pip show -f <name> | sed -n '/^Files:/,$p' | head -30   # the files RECORD promised, plus the launcher
deactivate; rm -rf /tmp/release-check
```

## 7. Choose the shipping form

| Who installs it, where | Ship | Command for them |
|---|---|---|
| Engineers on their laptops | Wheel on an index | `pipx install <name>==X.Y.Z` or `uv tool install <name>==X.Y.Z` |
| CI jobs | Wheel plus the lock | `pip install --require-hashes -r requirements.lock` then the wheel with `--no-deps` |
| Hosts with Python but no pip, pure-Python dependencies | Zipapp built from the lock | `python3 <name>.pyz` |
| Hosts without Python, or compiled dependencies and no build tools | PyInstaller binary per OS and CPU | `./<name>`; build on the oldest glibc you support |
| A container image | Wheel installed in the image build with the lock | Pin the base image by digest |

## 8. Publish without a stored token

```yaml
permissions:
  id-token: write
steps:
  - uses: actions/download-artifact@v4          # the dist/ built, hashed and tested earlier
  - uses: pypa/gh-action-pypi-publish@release/v1
```

Register the repository, workflow file and environment as a trusted publisher on PyPI (or your private index's equivalent), delete any old API token secret, and upload exactly the files from step 3. Tag first, build from the tag, publish last.

## 9. Report

State: the commit and tag; the version and how it was derived; the two build hashes and whether they matched; the wheel's file list and METADATA fields that changed since the last release; the lock's package count and whether every pin has a hash; the clean-install result (`--version`, `pip check`); the shipping form chosen for each audience; and the publish method. Do not release when the tree is dirty, the builds differ, the version disagrees with the tag, or any pin lacks a hash.
