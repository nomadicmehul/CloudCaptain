# Build a Tiny PaaS: reference solution

Finish the capstone before reading this. CI grades this folder on every change
to prove the grader can be passed with full marks.

`bin/tinypaas` is a Python 3.9+ program that uses only the standard library and
calls `git`, `docker`, `kind` and `kubectl` through `subprocess`.

| File | Milestone | What it does |
|---|---|---|
| `tinypaas/manifest.py` | app-manifest | every `app.json` rule, each error with a JSON path; normalizes the manifest with defaults |
| `tinypaas/images.py` | build-image | `git archive` of the commit, generated Dockerfiles (Python, static), `docker build`, digest, `kind load docker-image` |
| `tinypaas/render.py` | deploy | Namespace, Deployment (probes, limits, `maxUnavailable: 0`, release annotations) and NodePort Service |
| `tinypaas/cluster.py` | deploy, config-scale, logs | the kubectl calls: apply, rollout status, scale, node IP and nodePort, pods and logs |
| `tinypaas/hooks.py` | git-push | the bare repository and the `post-receive` hook that deploys `main` only |
| `tinypaas/releases.py` | releases-rollback, config-scale | the release store under `$TINYPAAS_HOME/.tinypaas/apps/<app>.json`, config hashes, rollback targets |
| `tinypaas/logs.py` | logs | RFC 3339 parsing and the time-ordered merge across pods |
| `tinypaas/cli.py` | all | argparse subcommands, `--json` output, and the deploy, config, scale and rollback flows |
| `tests/` | all | unit tests for the pure parts: `python3 -m unittest discover -s solution/tests` |

A release moves through `pending` to `succeeded` or `failed`. When a rollout
times out, `cli.Paas.release` marks it failed and re-applies the current
release, so the half-started pod is removed and the old pods keep serving.
