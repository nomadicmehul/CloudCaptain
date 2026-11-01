---
name: state-lock-incident
description: Resolve OpenTofu/Terraform state lock and state write incidents (stuck locks, errored.tfstate, refused or lost writes) without overwriting someone else's apply
version: 1.0.0
phase: iac
lesson: iac/remote-state-locking
tags: [opentofu, terraform, iac, state, locking, incident, s3, http-backend]
---

# State lock incident

Use this when a run fails with `Error acquiring the state lock` or `Error releasing the state lock`, when an apply leaves `errored.tfstate`, when a backend refuses a state write (409, `cannot import state with lineage`, `newer state with serial`), or when resources exist in the cloud but not in state after two runs overlapped. Never start with `force-unlock`, `-lock=false` or `state push -force`.

## 1. Freeze and collect

Stop new applies on the stack (pause the pipeline, tell the channel). Then record, without changing anything:

```bash
tofu state pull > /tmp/state-now.tfstate              # works while locked; it does not take the lock
jq '{serial, lineage, terraform_version, resources: (.resources | length)}' /tmp/state-now.tfstate
ls -l errored.tfstate 2>/dev/null                      # in the working directory of the failed run, if you have it
```

From the error, copy the lock info verbatim: `ID`, `Who`, `Operation`, `Created`, `Version`. For S3 with `use_lockfile`, the same JSON is in `s3://BUCKET/KEY.tflock`; for DynamoDB it is the item whose `LockID` is `BUCKET/KEY`; for statesrv, `statesrv status ADDRESS` and `statesrv events ADDRESS`.

## 2. Is the holder alive?

Match `Who` and `Created` to a process: a CI job (runner name, pipeline started at that time), a person (`user@laptop`), or an automation (drift job, Atlantis, a scheduled workflow).

| Evidence | Holder state | Action |
|---|---|---|
| The CI job or process is still running and producing output | alive | wait; rerun yours with `-lock-timeout=10m` |
| The job was cancelled, timed out, or its runner or pod no longer exists | dead | step 3 |
| A person, reachable | unknown | ask them to finish or Ctrl-C (which releases the lock) |
| A person, unreachable, `Created` hours ago, no audit-log writes since | probably dead | step 3, and note the risk in the report |
| `Operation` is a plan or refresh and its job is gone | dead, no writes pending | step 3 |
| Cannot tell | unknown | do not unlock; escalate to the stack owner |

## 3. Release a dead holder's lock, by ID

```bash
tofu force-unlock LOCK_ID          # prompts; -force skips the prompt in automation
```

Run it from a checkout of the same stack, initialised against the same backend. If no client can run, delete the lock record itself (the `.tflock` object, the DynamoDB item, `statesrv force-unlock --data DIR NAME`) and write down that you did. Immediately run `tofu plan` and read it: a dead apply may have changed resources it never recorded.

## 4. Recover an `errored.tfstate`

An apply that could not write its state saved it locally. The changes in it already happened.

```bash
jq '{serial, lineage}' errored.tfstate
tofu state pull | jq '{serial, lineage}'
```

| Comparison | Meaning | Action |
|---|---|---|
| same lineage, and no version was written after the failed run started (backend history, bucket object versions, audit log) | the only missing write is yours | `tofu state push errored.tfstate`, then `tofu plan` must be clean or show only intended changes |
| same lineage, and a version was written after the failed run started | forked state | do not push, even if `tofu` would accept the serial; go to step 5 |
| different lineage | the file belongs to another state | stop; find which stack it belongs to |

## 5. Reconcile a forked or lost write

Two writers built on the same read; one of them is missing from state.

```bash
tofu state pull > current.tfstate
jq -r '.resources[] | "\(.module // "root") \(.type).\(.name)"' current.tfstate | sort > current.txt
jq -r '.resources[] | "\(.module // "root") \(.type).\(.name)"' errored.tfstate | sort > other.txt
diff current.txt other.txt
```

For each resource only in the other version: if it exists in the cloud, bring it back with an `import` block (or `tofu import`), never by pushing the other file. For each resource only in the current version that no longer exists, let a refresh drop it or `tofu state rm` it. Finish when `tofu plan` shows only changes someone intends. If a whole version must be restored, download it (an old bucket object version, `statesrv pull ADDRESS --version N`), set `serial` to the current serial plus one with the lineage unchanged, `tofu state push` it under the lock, and plan immediately. Pushing is safer than copying an object version over the key by hand, because the push goes through the lock and the lineage and serial checks.

## 6. Close the gap that allowed it

- Locking off: add `lock_address`/`unlock_address` (HTTP), `use_lockfile = true` (S3), or the backend's equivalent; remove `-lock=false` from every script.
- Overlapping pipelines: serialise applies per stack (a concurrency group in CI) and set `-lock-timeout=5m`.
- Runners killed mid-apply: give the job a timeout longer than the longest apply, and handle cancellation with SIGINT so the client releases the lock.
- No history: enable bucket versioning before the next incident, not after.

## 7. Report

State: the stack and backend, the timeline (each lock, write and unlock with time and holder), the lock ID and holder you released and the evidence the holder was dead, whether `errored.tfstate` was pushed or reconciled, the serial before and after, resources imported or removed, the final `tofu plan` summary line, and the guard from step 6 that was missing. Never paste state contents; it contains secrets.
