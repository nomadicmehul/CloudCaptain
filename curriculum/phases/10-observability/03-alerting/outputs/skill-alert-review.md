---
name: alert-review
description: Review a Prometheus alerting rule and its Alertmanager route before it ships (symptom versus cause, for, keep_firing_for, labels, annotations, routing, grouping, timers, inhibition, silences), and explain why an alert did or did not page
version: 1.0.0
phase: observability
lesson: observability/alerting
tags: [alerting, prometheus, alertmanager, promtool, amtool, on-call, alert-fatigue, routing, inhibition, silences]
---

# Alert review

Use this in two situations: a pull request adds or changes an alerting rule or the Alertmanager configuration, or someone asks "why did this page me?" or "why did nobody get paged?". Never approve an alert because the expression looks right; prove when it fires, where it goes and what keeps it quiet, with a unit test and a routes test. Never debug a missing page by editing rules in production; find the stage that dropped it (rule state, send, route, grouping, mute, notification log, integration) first.

## Part A: before merge

### 1. Collect the facts

```bash
promtool check rules rules.yml
amtool check-config alertmanager.yml
P=http://prometheus:9090
A=http://alertmanager:9093
curl -sG $P/api/v1/query --data-urlencode 'query=EXPR' | python3 -m json.tool           # what the expression returns today, with labels
curl -sG $P/api/v1/query --data-urlencode 'query=count(EXPR)'                           # how many alerts it would create
curl -s  $P/api/v1/status/flags | python3 -c 'import json,sys; f=json.load(sys.stdin)["data"]; print(f["rules.alert.resend-delay"], f["rules.alert.for-grace-period"])'
```

Write down: the symptom the alert claims to detect, who acts on it and what they do, the evaluation interval of its group, the labels of the alert (expression labels plus rule labels plus `alertname`), and the receivers it should reach.

### 2. Check the rule

| Check | How | Problem if it fails | Fix |
|---|---|---|---|
| symptom, not cause | does a user notice when it fires? | pages for CPU, memory or one pod restarting that hurt nobody: fatigue | page on error ratio, latency or SLO burn; send causes to tickets or dashboards |
| actionable with a runbook | `runbook_url` annotation; the first step is something a human does now | the responder guesses; "wait and see" pages train people to ignore pages | write the runbook first; if the action is always the same, automate it |
| `for` set and justified | compare with the expression's natural noise (`max_over_time(EXPR[1d])` patterns) | `for: 0` pages on every blip; a long `for` delays real outages | at least two to five evaluations; longer for ticket severity |
| `keep_firing_for` on flappy conditions | does the condition dip for one evaluation during an incident? | resolve and re-page in the middle of one incident | `keep_firing_for` of a few evaluations to tens of minutes |
| no `$value` or timestamps in labels | read the `labels` block | every new value is a new alert: the old one resolves, the new one pages | values go in annotations only |
| label templates bounded | which expression labels end up in alert labels | per-pod or per-request labels create hundreds of alerts and groups | aggregate in the expression: `max by (service, cluster) (...)` |
| severity and owner labels | `severity` and `team` or `service` present | the route cannot send it anywhere specific: it lands in the default receiver | add the labels routing uses; check with step 3 |
| missing data covered | what happens when the metric disappears? | an alert on `rate(errors[5m]) > x` never fires when the target is down | an `up == 0` or `absent()` alert; remember `absent()` waits for the five-minute lookback without staleness markers |
| annotations render | `promtool test rules` with `exp_annotations` | `<error expanding template: ...>` in the page | fix the template; test it |

### 3. Check the route

```bash
amtool config routes test --config.file=alertmanager.yml --tree LABELS...
amtool config routes test --config.file=alertmanager.yml --verify.receivers=EXPECTED LABELS...    # exit 1 on a mismatch
python3 minialert.py routes test --config alertmanager.json --explain LABELS...                    # the walk, the group key, the timers
```

| Check | Problem if it fails | Fix |
|---|---|---|
| reaches the owner's receiver, nowhere else | a page in a channel nobody reads, or a duplicate page via `continue: true` | reorder routes or add matchers; `continue` only where both receivers should act |
| not caught by an earlier sibling without `continue` | the first match wins and the intended route is never tried | move the specific route above the general one |
| `group_by` on the labels a responder works on | `instance` in `group_by` turns an outage into one page per pod; `...` the same | `alertname` plus `cluster` or `service` |
| timers fit the severity | `group_wait` of minutes delays pages; `repeat_interval` of minutes re-pages all night | pages: `group_wait` 30s, `group_interval` 5m, `repeat_interval` 4h or more |
| inhibited during a known cause | symptoms page while the cause is already firing | an inhibition rule from the cause to the symptoms with `equal` on the shared label (cluster, service) |
| inhibition does not hide the cause | a broad target matcher with no `equal` mutes unrelated alerts everywhere | always `equal`; never inhibit the source's own severity without it |
| `send_resolved` intended | a pager integration that never resolves incidents, or a chat flooded with resolved messages | set it per integration |

### 4. Prove it

Add a `promtool test rules` case for each of: the condition true for less than `for` (no alert), longer than `for` (alert with full labels and annotations), a one-evaluation dip while firing (still firing if `keep_firing_for` is set), recovery (no alert after `keep_firing_for`), and missing data. Add each new label set with its expected receivers to the routes-test table that CI runs with `--verify.receivers`.

```bash
promtool test rules rules-test.yml
python3 minialert.py rules eval rules-test.json --show-sends      # the state at every evaluation and what Prometheus sends
python3 minialert.py simulate scenario.json --config alertmanager.json    # groups, mutes and notifications for a burst
```

### 5. Decide

| Finding | Decision |
|---|---|
| cause-based page with no user impact | block, or downgrade to ticket severity |
| no runbook or no owner label | block for pages; allow tickets with a follow-up |
| no `for` on a noisy expression | block |
| `$value` or an unbounded label in alert labels | block |
| route reaches the wrong or two receivers by accident | block until the routes test names the intended receivers |
| missing inhibition for a known cause | request the rule; allow if the alert is new and low volume |

## Part B: "why did (not) this page?"

Walk the pipeline in order and stop at the first stage that explains it:

```bash
curl -sG $P/api/v1/query --data-urlencode 'query=ALERTS{alertname="NAME"}' --data-urlencode "time=T"     # pending or firing at T?
curl -sG $P/api/v1/query --data-urlencode 'query=ALERTS_FOR_STATE{alertname="NAME"}' --data-urlencode "time=T"
curl -s  $A/api/v2/alerts | python3 -c 'import json,sys; [print(a["labels"]["alertname"], a["status"], [r["name"] for r in a["receivers"]]) for a in json.load(sys.stdin)]'
amtool --alertmanager.url=$A silence query
amtool config routes test --config.file=alertmanager.yml --tree LABELS...
docker logs alertmanager 2>&1 | grep -E 'level=(ERROR|WARN)'                                                  # failed integrations
```

| What you see | Meaning | Next |
|---|---|---|
| `ALERTS` shows `pending` only, never `firing` | the condition never held for `for` without a miss | `max_over_time`/`min_over_time` of the expression; add `keep_firing_for` or smooth the expression |
| firing in Prometheus, absent in Alertmanager | not sent: `alerting.alertmanagers` wrong, network, or the alert was relabelled away | Prometheus `/api/v1/alertmanagers` and the notifier metrics `prometheus_notifications_*` |
| in Alertmanager, `state: suppressed`, `inhibitedBy` set | a source alert with equal labels is firing | is that source real? is `equal` too loose? |
| `state: suppressed`, `silencedBy` set | a silence matches | `amtool silence query`; expire it if it is too broad |
| active, but the receivers are not the expected ones | routing | `amtool config routes test --tree` with the alert's exact labels |
| active, right receiver, no notification | grouping or the notification log: the group was already notified and nothing is new | `group_interval`, `repeat_interval`; a muted alert that unmuted is not news |
| notification sent, nobody paged | the integration failed or `send_resolved`/`max_alerts` dropped it | Alertmanager logs, `alertmanager_notifications_failed_total` |
| pages for a resolved incident | `keep_firing_for` too long, or the expression's window (`[1h]`) keeps the condition true | shorter windows or a multiwindow rule |

## Report

State: the alert (rule name, group, expression, `for`, `keep_firing_for`, labels, annotations); the symptom and the owner; every failed check from the tables with the evidence (a promtool case, an amtool line, an API response at a named time); the receivers and the group key for its typical label set; the inhibition and silences that apply; the changes made; the unit-test and routes-test cases that now guard it. For Part B, name the stage that dropped or produced the page, the evidence, and the change that prevents it next time. Never write "should page now" without a promtool case that fires and a routes test that names the receiver.
