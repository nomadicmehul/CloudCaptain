---
name: scheduled-job-review
description: Review a scheduled job (crontab line, /etc/cron.d file, systemd timer or Kubernetes CronJob) for the expression and its dialect, time zone and daylight-saving behaviour, overlap and locking, missed runs and deadlines, jitter, timeouts, idempotence and visibility, then prove each answer on the box
version: 1.0.0
phase: scripting
lesson: scripting/job-scheduling
tags: [cron, crontab, systemd, timers, kubernetes, cronjob, flock, dst, scheduling, review]
---

# Scheduled job review

Use this before merging a new crontab entry, timer or CronJob, when a job ran twice or not at all, when two copies overlapped, after a daylight-saving change, or when a fleet-wide job overloads a shared service. Do not edit the schedule during triage: first record what it says and what the scheduler did.

## 1. Collect the job as the scheduler sees it

```bash
crontab -l -u USER                                   # user crontab: five fields + command
cat /etc/crontab /etc/cron.d/* 2>/dev/null           # system crontabs: five fields + user + command
systemctl cat NAME.timer NAME.service                # timer and the service it starts, with drop-ins
systemctl list-timers --all | grep NAME              # NEXT, LEFT, LAST, PASSED
kubectl get cronjob NAME -o yaml                     # schedule, timeZone, concurrencyPolicy, startingDeadlineSeconds, suspend
timedatectl show -p Timezone                         # the zone cron and timers use when the job names none
```

Record: the scheduler (cronie, Debian cron, systemd, Kubernetes CronJob, a library such as croniter or APScheduler), the expression, the zone it is evaluated in, and the command.

## 2. Expression and dialect

```bash
python3 minicron.py explain 'EXPR' --dialect vixie     # cronie, Debian cron
python3 minicron.py explain 'EXPR' --dialect robfig    # Kubernetes CronJob, croniter
systemd-analyze calendar --iterations=5 'ONCALENDAR'   # timers: the normalized form and the next elapses
```

| Finding | Risk | Fix |
|---|---|---|
| both day-of-month and day-of-week restricted (`0 9 1 * 1`) | OR, not AND: runs on the 1st and every Monday | first-weekday logic: `1-7` plus a weekday test in the command, or `OnCalendar=Mon *-*-1..7` |
| `*/N` in a day field together with a restricted other day field | Vixie cron uses AND, Kubernetes and croniter use OR | avoid steps in day fields when both are used; `explain` with both dialects must agree |
| six fields | a seconds field (Quartz, Spring) or a cron.d line pasted into a user crontab | five fields in crontab and CronJob; seconds only in `OnCalendar=` |
| `CRON_TZ=` or `TZ=` inside a CronJob `schedule` | rejected by the API server | `spec.timeZone: Europe/Berlin` |
| `29-31` in day-of-month, `0 0 30 2 *` | silently skips short months or never runs | `OnCalendar=*-*~01` for the last day, or a check in the command |
| unescaped `%` in a crontab command | cron turns it into a newline | `\%`, or move the command into a script |

## 3. Time zone and daylight saving

List the next runs across the next transitions and read them:

```bash
python3 minicron.py next 'EXPR' --tz ZONE --from 2026-03-27T00:00Z -n 6     # spring, Europe
python3 minicron.py next 'EXPR' --tz ZONE --from 2026-10-24T00:00Z -n 6     # autumn, Europe
TZ=ZONE systemd-analyze calendar --iterations=6 --base-time='2026-03-27 00:00 UTC' 'ONCALENDAR'
```

| Scheduler | Time in the skipped hour | Time in the repeated hour |
|---|---|---|
| cronie, Debian cron, fixed minute and hour | runs once after the change | once |
| cronie, Debian cron, `*` in minute or hour | not run | runs again |
| systemd `OnCalendar=` | not run | first occurrence only |
| croniter-based schedulers | runs once at the end of the gap | twice |
| Kubernetes CronJob with `timeZone` | test it on your version with a schedule inside the transition hour | test it |

Verdict: any job with a fixed time between 01:00 and 03:00 local time in a zone with DST is a finding unless it is idempotent and both outcomes are acceptable. Prefer UTC for machine work, or move the time out of the window.

## 4. Overlap and locking

| Scheduler | Check | Safe answer |
|---|---|---|
| cron | is the command wrapped in `flock -n LOCKFILE` | yes, `-E 75` to tell a skipped run from a failure; the lock file is never deleted |
| cron with a PID file | does the script check that the PID is alive and handle a stale file | replace with `flock`; the kernel drops the lock when the holder dies |
| systemd | timer starts a `.service` | systemd never starts a second copy of an active service; check `Type=oneshot` so "active" means "still running" |
| Kubernetes | `concurrencyPolicy` | `Forbid` for anything that writes shared state; `Replace` only if a newer run makes the old one worthless; `Allow` (the default) needs a reason |

Measure the real duration against the period: `journalctl -u NAME.service -o short-precise | grep -E 'Started|Finished|Deactivated'`, `kubectl get jobs -o custom-columns=NAME:.metadata.name,START:.status.startTime,DONE:.status.completionTime`. A job whose 95th percentile run time exceeds half the period will skip or pile up.

## 5. Missed runs and deadlines

| Scheduler | Default | Knob |
|---|---|---|
| cron | missed runs are lost | anacron for daily and slower jobs, or a wrapper that compares the last success with now |
| systemd | lost unless `Persistent=true` | `Persistent=true` runs once at activation; `ls -l /var/lib/systemd/timers/` shows the stamp files |
| Kubernetes | the most recent missed schedule runs once | `startingDeadlineSeconds` drops runs that are too late; a resume after `suspend: true` counts as missed runs |

Decide per job: skip (a sync that will run again soon), run once (a nightly backup), run all (a backfill of per-day partitions, which belongs in a workflow tool, not cron). Then simulate it: `python3 minicron.py simulate --jobs jobs.json --from ... --to ... --down FROM/TO`.

## 6. Jitter, timeouts, idempotence

- Fleet jobs at a round time (`0 0 * * *` on many hosts) need jitter: `RandomizedDelaySec=` with `FixedRandomDelay=yes` on timers, `minicron run --jitter N` or `sleep $(( $(hostname | cksum | cut -d' ' -f1) % 300 ))` in cron.
- Every job needs a timeout: `timeout 3600` in cron, `TimeoutStartSec=` on a `Type=oneshot` service (it is still starting while it runs), `RuntimeMaxSec=` on a long-running one, `activeDeadlineSeconds` on the Job. Without one, a hung run blocks every later run under `Forbid`.
- Every job must be idempotent: CronJobs are documented as "approximately once", DST and catch-up can add or drop a run, and humans rerun jobs by hand.

## 7. Visibility

| Scheduler | Where runs are recorded | Check |
|---|---|---|
| cron | syslog or the `cron` unit's journal, mail to `MAILTO` | is output redirected to a log the team reads, or to `/dev/null` |
| systemd | `journalctl -u NAME.service`, `systemctl status NAME.timer` | `OnFailure=` points at an alerting unit |
| Kubernetes | Jobs, pod logs, events (`JobAlreadyActive`, `SawCompletedJob`) | `failedJobsHistoryLimit` above 0, alert on `kube_cronjob_status_last_successful_time` or a dead man's switch |

Every scheduled job needs an alert on "has not succeeded for N periods", not only on failure: a job that never starts produces no failure.

## Report

State: scheduler and zone; the expression, its normalized form and the next five runs; the dialect check result; the DST verdict with the two transition dates checked; overlap policy and lock mechanism with the measured run time against the period; missed-run behaviour and deadline; jitter, timeout and idempotence; where runs are recorded and which alert fires when the job stops running. List each change as the exact line before and after. Never report "added a lock file" without naming the mechanism (`flock`, not a PID file) and confirming the file is never deleted.
