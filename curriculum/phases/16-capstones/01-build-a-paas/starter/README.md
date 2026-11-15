# Build a Tiny PaaS: starter

Copy this folder into a new repository and build `tinypaas` there:

```bash
cp -r curriculum/phases/16-capstones/01-build-a-paas/starter ~/tinypaas
cd ~/tinypaas && git init && git add -A && git commit -m "Start from the CloudCaptain starter"
kind create cluster --name tinypaas
```

## What is already here

- `bin/tinypaas`: the entry point the grader runs. Every subcommand and flag of the interface is parsed; each one prints `not implemented: milestone <id>` to stderr and exits 1. Replace the stubs in `tinypaas/cli.py`.
- `tinypaas/settings.py`: the `TINYPAAS_*` variables resolved for you (data directory, kubectl context, kind cluster name, namespace prefix), a `run()` wrapper for subprocesses and a `kubectl()` helper.
- `examples/hello/`: a stdlib web app to try your platform on (`/`, `/healthz`, `/env`, request logging). Commit it in its own repository before you build it: `tinypaas` builds commits.
- `tests/`: two tests of the CLI skeleton; add yours next to them.

You may rewrite all of it in another language. The grader only needs an executable `bin/tinypaas` that honours the interface below.

## The interface

| Command | With `--json` prints | Exit |
|---|---|---|
| `tinypaas validate <app-dir>` | `{"valid": bool, "errors": [{"path", "message"}]}` | 0 valid, 1 invalid |
| `tinypaas build <app-dir>` | `{"app", "image", "commit", "digest"}` | 0 |
| `tinypaas deploy <app-dir> [--timeout SECONDS] [--app NAME]` | `{"app", "release", "image", "status"}` | 0 live, 1 failed |
| `tinypaas url <app>` | `{"app", "url"}` | 0 |
| `tinypaas remote add <app> <bare-repo-path>` | `{"app", "remote"}` | 0 |
| `tinypaas config set <app> KEY=VALUE...` | `{"app", "release", "status"}` | 0 live, 1 failed |
| `tinypaas scale <app> web=N` | `{"app", "web"}` | 0 when N pods are ready |
| `tinypaas releases <app>` | `{"app", "current", "releases": [{"number", "image", "config_hash", "time", "status"}]}` | 0 |
| `tinypaas rollback <app> [--to N]` | `{"app", "release", "status"}` | 0 live, 1 failed |
| `tinypaas logs <app> [--since 5m]` | `{"app", "lines": [{"time", "pod", "line"}]}` | 0 |

Environment: `TINYPAAS_HOME` (state in `.tinypaas/` inside it), `TINYPAAS_CONTEXT` (default: current context), `TINYPAAS_KIND_CLUSTER` (default: the context without `kind-`), `TINYPAAS_NAMESPACE_PREFIX` (default `tinypaas`; the grader sets `cc-grade-paas`).

Cluster layout: app `<app>` runs as Deployment `<app>` in namespace `<prefix>-<app>` behind a NodePort Service; images are tagged `tinypaas/<app>:<short sha>` and loaded with `kind load docker-image`; the URL is `http://<node InternalIP>:<nodePort>`.

The full rules for `app.json`, and what each check asserts, are in the capstone page: https://cloudcaptain.io/learn/capstones/build-a-paas

## Milestones

| Id | Points | Build |
|---|---|---|
| `app-manifest` | 2 | `validate` with every error and its JSON path |
| `build-image` | 3 | one image per commit, tagged with the short sha, loaded into kind |
| `deploy` | 3 | Deployment with probes and limits, NodePort Service, `url` |
| `git-push` | 2 | `remote add`: a bare repository whose `post-receive` deploys `main` only |
| `config-scale` | 2 | `config set` as a release, `scale web=N` |
| `releases-rollback` | 2 | numbered releases, failed rollouts recorded, `rollback [--to N]` |
| `logs` | 1 | every pod's logs merged in time order |

Pass mark: 11 of 15.

## Grade it

From a CloudCaptain checkout, with your kind cluster as the current context:

```bash
bash <cloudcaptain>/curriculum/phases/16-capstones/01-build-a-paas/grader/grade.sh .
bash <cloudcaptain>/curriculum/phases/16-capstones/01-build-a-paas/grader/grade.sh . --milestone app-manifest
```

To grade every push on GitHub, copy `grader/workflow.yml` from the capstone to `.github/workflows/cloudcaptain-grade.yml` here.
