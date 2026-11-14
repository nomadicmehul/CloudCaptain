# Grader fixture apps

- `hello/`: a stdlib Python web app with no Dockerfile, so tinypaas generates one. `/` returns `body.txt`, `/healthz` returns 200 while `health.txt` says `ok`, `/pod` returns the pod name, `/env` returns the environment as JSON, and every request except `/healthz` logs `request path=... marker=... pod=...` to stdout.
- `custom/`: an overlay on `hello/` with its own `Dockerfile` (label `cc.fixture=custom`) and no `web` command.
- `site/`: a static site (`"static": "public"`) with no Dockerfile.
- `unready/`: an overlay on `hello/` whose `health.txt` says `fail`, so the readiness probe never passes.

The checks copy these into a temporary git repository, set `name` in `app.json` to a grader-owned app name (`ccg-*`) and commit with fixed dates.
