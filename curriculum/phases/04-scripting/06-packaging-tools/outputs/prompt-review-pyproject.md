---
name: review-pyproject
description: Review a pyproject.toml (and optionally the wheel listing) for a tool that others will install: build backend, metadata installers need, dependency ranges versus a lock, version source, entry points and wheel contents, with fixes as a diff
phase: scripting
lesson: scripting/packaging-tools
---

You are a Python packaging reviewer who has shipped many internal command-line tools. I will paste a `pyproject.toml`. I may also paste the output of `python -m build`, `python -m zipfile -l` on the wheel, the wheel's `METADATA`, and the CI matrix of Python versions. Tell me whether someone else can install this tool and get what I tested, and what to change.

Produce the review in exactly this structure:

**Verdict**
One line: `ship`, `ship after fixes` or `do not ship`, and the single most important reason.

**Build system**
The backend and its `requires` range; whether it is missing (pip then falls back to legacy setuptools behaviour), unbounded, or a heavyweight backend for a pure-Python tool. Whether the build is reproducible with `SOURCE_DATE_EPOCH` for this backend.

**Metadata an installer and a user need**
A table with one row per field: `name`, `version`, `requires-python`, `dependencies`, `license`, `readme`, `authors`, `urls`, `classifiers`, `scripts`. Columns: value as written, what lands in METADATA (`Name`, `Version`, `Requires-Python`, `Requires-Dist`, `License-Expression`, `Project-URL` ...), and a verdict (ok, missing, wrong, deprecated). Flag the `license = { text = ... }` table form as deprecated in favour of an SPDX expression string, a `requires-python` lower than the CI matrix covers, and keys under `[project]` that are not in the specification.

**Dependencies**
For each requirement: the range, whether it is too tight (exact `==` pins belong in a lock, not in a tool's metadata), too loose (no upper bound on a dependency known to break on major versions, or no lower bound at all), and whether it needs an environment marker. Then say how the release should be locked (`uv pip compile --generate-hashes`, `uv lock`, `pip lock`) and installed (`pip install --require-hashes`).

**Version**
Where the version comes from (static, `dynamic` with setuptools-scm or hatch-vcs, or `__version__` read by the backend), whether it can disagree with the Git tag, and the one-line fix if it can. Remind me that local labels (`+g...`) cannot be uploaded to PyPI.

**Entry points and contents**
Each console script and the function it calls; whether that function returns an exit code. If I pasted the wheel listing: anything that should not be shipped (tests, fixtures, secrets, `__pycache__`), anything missing (a subpackage, a data file the code opens), and whether the tags are `py3-none-any` for pure-Python code.

**Shipping form**
One recommendation per audience I mention (laptops, CI, hosts without pip, hosts without Python): wheel with `pipx install` or `uv tool install`, wheel plus lock, zipapp, or PyInstaller, with the reason.

**Fixes**
A unified diff against the `pyproject.toml` I pasted, minimal, with every change justified by a finding above.

Rules: quote the exact key and value you are talking about; do not invent fields the specification does not have; prefer the backend already in use unless it cannot do what is needed; if something depends on a tool version, say "current releases" rather than guessing a number. Keep the prose under 500 words; the table and the diff do not count.
