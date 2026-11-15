---
name: build-a-paas-review
description: Review a deploy platform or pipeline (a homegrown PaaS, a buildpack setup, an internal developer platform, or a CI job that runs kubectl) for per-commit images, release records, safe rollouts, rollback, config handling, git-push deploys and logs, with concrete commands and a fix list
version: 1.0.0
phase: capstones
lesson: capstones/build-a-paas
tags: [platform-engineering, paas, deployments, rollback, releases, kubernetes, kind, git-hooks, twelve-factor]
---

# Deploy platform review

Use this when someone asks "is our deploy path safe?", before a platform goes to more teams, or after an incident where a deploy broke production or could not be rolled back. The output is a scored table of properties with evidence and the three fixes that matter most.

Do not review from the README alone: run the commands below against a real app on a throwaway cluster (`kind create cluster --name review`). Do not change production while reviewing.

The model: a platform turns a commit into an image (build), pairs it with config into a numbered record (release), and asks the cluster to converge on it (run). Every property below is a question about one of those three stages.

## 1. Find the three stages

Read the platform's entry point (CLI, pipeline file, hook) and write down:

| Stage | Question | Where to look |
|---|---|---|
| build | what is the input: the commit, or the working tree? | `git archive`, `docker build .`, buildpack invocation |
| build | how is the image named? | the tag: commit sha, semver, or `latest` |
| release | where is the record kept and what does it hold? | a state file, ConfigMap, annotations, CI run history |
| run | what does it apply and what does it wait for? | `kubectl apply`, Helm, Argo CD; `rollout status`, a timeout |

If any answer is "nowhere", that stage is missing; note it and continue.

## 2. Check each property with a command

| Property | Pass when | Check |
|---|---|---|
| per-commit image | tag or label names the commit; uncommitted edits are not built | change a file without committing, build, inspect `org.opencontainers.image.revision` and the tag |
| content address | the release records the image digest, not only the tag | `docker image inspect --format '{{.Id}}'` equals the recorded digest |
| validation | a bad manifest fails before anything is built, with the field named | break `port` and `health`; expect two errors with paths |
| probes and limits | readiness and liveness on a health path; CPU and memory limits | `kubectl get deploy <app> -o jsonpath='{.spec.template.spec.containers[0].readinessProbe}'` |
| safe rollout | `maxUnavailable: 0` (or a PodDisruptionBudget) and a readiness probe | deploy a commit whose health check fails; the old version must keep answering |
| failure is recorded | a timed-out rollout is marked failed and exits non-zero | read the release list after the bad deploy |
| rollback | restores image and config exactly, as a new record | roll back, compare image and config hash, curl the app |
| config | config changes are releases; values survive the next deploy | set a value, deploy a new commit, read it back |
| git-push deploy | only the deploy branch deploys; hook output reaches the pusher | push a feature branch; nothing changes |
| logs | every pod, timestamped, ordered by parsed time | scale to 2, send marked requests, read the merged log |

Record evidence for each row: the command and the relevant output line.

## 3. Decide

| Finding | Severity |
|---|---|
| a failing release replaces healthy pods (no readiness gate, or `maxUnavailable` above 0 with no probe) | high: one bad commit is an outage |
| images tagged `latest` or by branch name | high: you cannot tell or restore what ran |
| no release record or rollback only by re-running an old pipeline | high: rollback depends on CI being up and the old commit still building |
| config edited in the cluster by hand | medium: the next deploy silently reverts it |
| logs only per pod | medium: slows every incident |
| validation missing | medium: errors surface as CrashLoopBackOff instead of a message |
| feature branches deploy | depends: fine for preview environments with their own namespace, wrong for production |

## Report

1. A table with one row per property from Step 2: pass, fail or not testable, and the evidence.
2. The release path drawn as build, release, run with the tool used at each stage.
3. The three fixes with the largest effect, each with the exact change (a manifest field, a flag, a hook line) and the command that proves it worked.
