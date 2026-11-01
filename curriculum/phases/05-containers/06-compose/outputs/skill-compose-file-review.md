---
name: compose-file-review
description: Review a compose file as a dependency graph before it is merged or run: validate it, check every edge and healthcheck, check every service, find Swarm leftovers and obsolete keys, and prove it with up --wait and down -v
version: 1.0.0
phase: containers
lesson: containers/compose
tags: [docker, compose, yaml, healthcheck, depends_on, review, local-dev]
---

# Compose file review

Use this when a pull request adds or changes a `compose.yaml`, when `docker compose up` behaves differently on two machines, or before a compose file becomes the way a team runs a stack.

## 1. Validate the text

```bash
docker compose -f compose.yaml config > /dev/null          # schema and YAML errors with the path, e.g. services.web.ports must be a list
docker compose -f compose.yaml config --services           # the node list
python3 minicompose.py -f compose.yaml config              # or: the lesson tool, which also reports keys it ignores
grep -nP '\t' compose.yaml && echo "tabs found"            # tabs are never valid YAML indentation
grep -nE '^\s*-\s+[0-9]+:[0-9]+\s*$' compose.yaml          # unquoted ports: quote them
```

Stop here if config fails; nothing below is meaningful on an invalid file.

## 2. Draw the graph

```bash
docker compose -f compose.yaml config --format json | python3 -c '
import json, sys
d = json.load(sys.stdin)
for name, s in sorted(d["services"].items()):
    deps = {k: v.get("condition", "service_started") for k, v in (s.get("depends_on") or {}).items()}
    print(f"{name:<12} healthcheck={\"yes\" if s.get(\"healthcheck\") else \"no\":<4} depends_on={deps}")'
```

For every edge, decide:

| Edge | Verdict |
|---|---|
| `service_started` to a database, cache, broker or anything with a port | Wrong. Change to `service_healthy` and add a healthcheck to the dependency that proves readiness (`pg_isready`, `redis-cli ping`, an HTTP 200), with `start_period` for slow starters. |
| `service_healthy` to a service without a `healthcheck:` | Fails at `up` unless the image carries a `HEALTHCHECK`; ask for the check to be in the file so the review can see it. |
| `service_completed_successfully` to a migration or seed job | Correct. Make sure the job exits non-zero on failure and is not `restart: always`. |
| An edge to a service behind a `profiles:` list | `up` without the profile fails. Either move the dependency out of the profile or put the dependent in it too. |
| A cycle | No valid start order. One of the edges is a lie; usually a web service "depending" on a worker that calls it back. |

Healthchecks themselves: `interval` and `timeout` in seconds, `retries` times `interval` is the time until `unhealthy`, `start_period` covers the slow start. A check that hits the network (`curl http://other-service`) tests the other service, not this one; keep checks local.

## 3. Check every node

```bash
docker compose -f compose.yaml config --images             # one line per image; every tag should be pinned
```

| Look at | Accept | Flag |
|---|---|---|
| `image:` | A tag or digest the team controls (`postgres:17.2`, `ghcr.io/org/api@sha256:...`) | `latest`, no tag, or `build:` in a file used outside local development |
| `ports:` | Quoted strings, `"127.0.0.1:5432:5432"` for databases that only the host should reach | Unquoted `5432:5432`, databases bound on all interfaces, two services on one host port |
| `environment:` | Non-secret settings; secrets via `env_file:` that is in `.gitignore`, or `secrets:` | Passwords and tokens inline; `${VAR}` without a default or `:?` message |
| `volumes:` | Named volumes for data (`dbdata:/var/lib/postgresql/data`, declared top-level); bind mounts only for source code in dev | Bind mounts of data directories; absolute host paths that exist on one laptop; a named volume used but not declared |
| `restart:` | `unless-stopped` for long-running services, nothing for one-shot jobs | `always` on a job that is expected to exit; `on-failure` masking a crash loop |
| `networks:` | The default network, or declared networks that separate a database from a public proxy | `network_mode: host` without a reason; services that cannot reach what they depend on |
| `command:` | List form (`["python3", "-m", "app"]`) | Shell form that swallows signals (see the PID 1 lesson) |
| `healthcheck:` | List form with `CMD` or `CMD-SHELL`, short `interval`, `start_period` for slow starters | `test` that depends on tools the image does not have (`curl` in alpine, `wget` in distroless) |

## 4. Leftovers from other tools

```bash
grep -nE '^\s*version:' compose.yaml                     # obsolete top-level key; Compose warns and ignores it
grep -nE '^\s*deploy:|^\s*placement:|update_config|restart_policy' compose.yaml
grep -nE '^\s*links:|volumes_from' compose.yaml          # legacy; use networks and named volumes
```

`deploy:` comes from Docker Swarm, which is in maintenance mode. `docker compose` honours `replicas` and `resources` and silently drops `placement`, `update_config`, `mode` and `restart_policy`. Ask for the Swarm-only keys to be removed and for `restart:` to be set at the service level instead of `deploy.restart_policy`.

## 5. Run it once

```bash
docker compose -f compose.yaml up -d --wait --wait-timeout 120 || { docker compose ps -a; docker compose logs --tail 50; }
docker compose ps                                        # every service running, every healthcheck healthy
docker inspect -f '{{json .State.Health}}' <project>-db-1 | python3 -m json.tool | head -30    # how long until healthy; tune start_period
docker compose down -v --remove-orphans                  # must leave nothing: docker ps -a --filter label=com.docker.compose.project=<project>
```

If `--wait` times out, read the health log before touching `depends_on`. If `down` leaves containers, a service was renamed; `--remove-orphans` finds them by label.

## 6. Report

State: whether `config` passes; the graph as a list of edges with their conditions and the verdict on each; the per-service findings in the order of the table above with the line number; the Swarm or legacy keys to remove; the measured time to healthy for each dependency and the `start_period` you recommend; and the exact `up` and `down` commands you ran with their result. Do not approve a file whose database edge is `service_started`.
