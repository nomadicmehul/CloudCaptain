---
name: unit-file-review
description: Review a systemd unit file as a contract: dependencies and ordering, start and readiness, restart policy and rate limit, stop timeout, environment, hardening; then prove each clause on the box
version: 1.0.0
phase: linux
lesson: linux/systemd-unit-runner
tags: [linux, systemd, units, restart, dependencies, hardening, review]
---

# Unit file review

Use this before merging a `.service` file, a drop-in, or a Quadlet `.container` file, and when a service misbehaves at boot (`connection refused` right after reboot, `start-limit-hit`, 90-second stops, `203/EXEC`).

## 1. Read the unit as the manager sees it

```bash
systemctl cat NAME.service                      # the merged unit: file plus every drop-in, with paths
systemd-analyze verify /path/to/NAME.service    # parse errors, unknown keys, missing executables, missing units
systemctl show NAME.service -p Type -p Restart -p RestartUSec -p TimeoutStopUSec -p KillMode -p StartLimitBurst -p StartLimitIntervalUSec
```

Review the merged text, not the file in the repo. A drop-in with `ExecStart=` on its own line resets the list before adding a new command; that is the rule, not a bug.

## 2. Dependencies: two axes, checked separately

| Clause | Question | Red flag | Fix |
|---|---|---|---|
| `Requires=X` | Is there also `After=X`? | `Requires=` without `After=`: both start at once, and this unit is not held back if X fails | add `After=X` |
| `Wants=X` | Can this unit run without X? | `Wants=` on something it cannot live without | `Requires=` + `After=`, or `BindsTo=` if it must die when X dies |
| `After=network.target` | Does it need an address or DNS? | `network.target` means "network stack configured", not "online" | `Wants=network-online.target` + `After=network-online.target` |
| `After=X` on a `Type=simple` X | Does this unit need X to be ready, not just forked? | ordering on a service that reports no readiness | make X `Type=notify` (or `Type=exec` as a minimum) or add a readiness check in `ExecStartPre=` |
| oneshot prerequisites (migrations, mounts, key fetches) | Is the work its own unit? | migration glued into `ExecStartPre=` of the app | a `Type=oneshot` unit with `RemainAfterExit=yes`, `Wants=` + `After=` from the app |

Confirm the order the manager actually computed: `systemctl list-dependencies NAME.service` (pulled in) and `systemctl list-dependencies --after NAME.service` (ordering). After a boot, `systemd-analyze critical-chain NAME.service` shows which `After=` chain decided the start time.

## 3. Start and readiness

- `Type=`: `simple` is active at fork. `exec` waits for `execve` to succeed (catches a missing binary synchronously). `notify` waits for `READY=1`, which is the only one that means "ready". `oneshot` finishes when the process exits; pair with `RemainAfterExit=yes` if other units `After=` it.
- `ExecStartPre=`: each line must exit 0 or the unit fails; a `-` prefix tolerates failure. No shell unless you write `/bin/sh -c`.
- Environment: the service does not get your shell's environment. `PATH` is minimal, `HOME` may be unset. Every variable the program needs goes in `Environment=` or `EnvironmentFile=`; `-/path` makes the file optional. Secrets do not belong in `Environment=` (readable with `systemctl show -p Environment`); use `LoadCredential=` or a root-only `EnvironmentFile=`.
- `WorkingDirectory=`, `User=`, `Group=`: a missing directory or user is exit status 200/CHDIR or 217/USER in `systemctl status`; `203/EXEC` is a missing or non-executable binary.

## 4. Restart contract

| Key | Check |
|---|---|
| `Restart=` | `on-failure` for daemons. `always` restarts even after exit 0 and after a `kill -TERM`; use it only when exiting is never correct. `no` for anything a human starts on purpose. Not allowed: `always` on `Type=oneshot`. |
| `RestartSec=` | The default is 100 ms, which is a tight loop against a dependency that is down. 2 to 10 s is usual for network services. |
| `StartLimitIntervalSec=`, `StartLimitBurst=` (in `[Unit]`) | Defaults 10 s and 5. With `RestartSec=1` a crashing service hits the limit in about 5 s and stays `failed (Result: start-limit-hit)` until `systemctl reset-failed`. Decide deliberately: a longer `RestartSec` or a larger window for services whose dependency may take a minute to come back. |
| Clean exits | Exit 0 and death by `SIGHUP`, `SIGINT`, `SIGTERM`, `SIGPIPE` do not trigger `on-failure`. Add `SuccessExitStatus=` for codes the program uses for "done". |

Read the journal before touching the policy: `journalctl -u NAME -b | grep -E 'status=|signal=|Scheduled restart'`. The exit status of the attempts is the bug; the rate limit is the symptom.

## 5. Stop contract

- Default stop: `SIGTERM` to every process in the unit's control group (`KillMode=control-group`), wait `TimeoutStopSec=` (default 90 s), then `SIGKILL`, result `timeout`.
- Check that the program handles `SIGTERM` (the PID 1 lesson). Measure: `time systemctl stop NAME`. Over a second with nothing to flush means the handler is missing, not that the timeout is too short.
- `TimeoutStopSec=` should be the real drain time plus margin, not 90 s by default and not 1 s to make deploys fast.
- `ExecStopPost=` runs after any end, including failures, with `$SERVICE_RESULT`, `$EXIT_CODE`, `$EXIT_STATUS`; use it for cleanup, never for "restart" logic.
- `KillMode=process` or `KillMode=none` are red flags: children survive the stop.

## 6. Hardening

```bash
systemd-analyze security NAME.service      # exposure score 0 (tight) to 10 (none); each line names a key
```

Minimum for a network service that is not root-only: `NoNewPrivileges=yes`, `ProtectSystem=strict` (+ `ReadWritePaths=` for its state dir), `ProtectHome=yes`, `PrivateTmp=yes`, `CapabilityBoundingSet=` down to what it uses (`CAP_NET_BIND_SERVICE` for ports under 1024, with `AmbientCapabilities=` to grant it), `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, `DynamicUser=yes` when it keeps no state. Re-run `systemd-analyze security` and record the score before and after.

## 7. Prove it

```bash
sudo systemctl daemon-reload && sudo systemctl restart NAME
systemctl status NAME --no-pager                        # Active:, Main PID, the CGroup tree, the last journal lines
journalctl -u NAME -b -o short-precise | tail -20
sudo kill -TERM "$(systemctl show NAME -p MainPID --value)"; sleep 2; systemctl show NAME -p ActiveState -p Result -p NRestarts
time sudo systemctl stop NAME; systemctl show NAME -p Result     # success, not timeout
sudo systemctl start NAME
```

For a reboot-sensitive unit, reboot a test node and run `systemd-analyze critical-chain NAME` and `journalctl -b -u NAME -u DEPENDENCY`.

## Report

State: the dependency axes (`After=`/`Requires=`/`Wants=` per dependency and whether readiness is real), `Type=`, the restart policy with `RestartSec` and the start limit as a sentence ("restarts every 5 s, gives up after 5 attempts in 60 s"), the measured stop time and `Result`, the environment and secrets handling, the `systemd-analyze security` score before and after, and the exact lines changed. Do not report "added Restart=always" as a fix for a crash.
