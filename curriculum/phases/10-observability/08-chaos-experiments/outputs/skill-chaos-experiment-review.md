---
name: chaos-experiment-review
description: Review a chaos experiment before it runs (measured steady state, falsifiable hypothesis, blast radius with its rounding, abort conditions, verified rollback, environment), watch it while it runs, and confirm the rollback and the recovery afterwards, ending with a verdict and its evidence
version: 1.0.0
phase: observability
lesson: observability/chaos-experiments
tags: [chaos-engineering, sre, slo, game-day, kubernetes, chaos-mesh, litmuschaos, chaos-toolkit, netem, fault-injection]
---

# Chaos experiment review

Use this when someone proposes a chaos experiment or a game day, when a scheduled experiment (`minichaos`, Chaos Mesh, LitmusChaos, Chaos Toolkit) is about to run, or when one has just ended with a verdict nobody can explain. Do not inject anything until steps 1 to 5 pass. If at any point the real SLO starts burning faster than the experiment allows, press the abort button first and investigate afterwards.

## 1. Refuse what is missing

Read the experiment file (or the ticket) and fill in this table. Any row marked missing blocks the run.

| Part | What good looks like | Why it blocks |
|---|---|---|
| steady state | an error ratio and a latency percentile from counters and histograms the users' requests pass through, per window, with a minimum request count | without it there is nothing to compare the fault with; CPU, pod counts and "dashboards look green" do not qualify |
| hypothesis | one sentence with numbers that one measurement can falsify: "with one of three pods deleted, every 3 s window keeps errors at or under 5% and p95 at or under 250 ms, and the Deployment is 3/3 within 60 s" | "the system is resilient" can never fail, so it can never teach |
| method | one fault (or a deliberate combination) with target, size and duration | several unrelated faults at once make the result uninterpretable |
| blast radius | allowed namespaces or network namespaces, a percentage rounded down, an absolute cap, a maximum duration | the tool's own selection may round up: 34% of 3 pods is 2 pods in Chaos Mesh's `fixed-percent` |
| abort conditions | thresholds looser than the hypothesis that stop early after N consecutive windows, an abort on lost measurements, a human abort button with a named owner | without them the experiment runs to the end whatever it costs |
| rollback | an undo for every fault that runs on every exit path and is verified; for faults that outlive the controller, an expiry (`duration` in Chaos Mesh, a self-terminating worker, a namespace that can be deleted) | an undo that only exists in someone's terminal fails exactly when that terminal hangs |

## 2. Check the environment

```bash
kubectl config current-context                                      # the cluster you think it is
kubectl get pods -n "$NS" -l "$SELECTOR" -o wide                    # the targets exist, are Running and spread over nodes
kubectl get pdb -n "$NS"                                            # a PodDisruptionBudget the fault would violate
kubectl get deployment -n "$NS" "$DEPLOY" -o jsonpath='{.spec.replicas} {.status.readyReplicas}{"\n"}'
```

| Question | Go | No-go |
|---|---|---|
| Which environment? | staging, or production after the same experiment held in staging | production first |
| Error budget left? | the SLO's budget for the window is not exhausted and not burning fast (no burn-rate alert pending) | budget exhausted: the release freeze covers experiments too |
| Incident or change in progress? | none, and no deploy scheduled during the run | any open incident, a rollout in progress, a change freeze |
| Who knows? | on-call is informed, the incident channel has a start and end message, the abort-button owner is not the operator | on-call finds out from an alert |
| Is there traffic? | the request rate gives at least `min_requests` per window | night-time traffic that turns every window into no data |

## 3. Resolve targets and rehearse the commands

```bash
python3 minichaos.py validate experiment.json --resolve --commands --context "$CTX"   # targets, refusals, the exact commands
ip netns exec "$NETNS" tc qdisc show dev "$DEV"                                      # netem: the root qdisc must be a default (handle 0:)
kubectl get -f chaos.json -o yaml 2>/dev/null; kubectl get crd | grep chaos-mesh.org   # Chaos Mesh: CRDs installed, no leftover object
kubectl -n chaos-mesh get deploy chaos-controller-manager                             # the controller that removes finalizers is running
```

Write down the worst-case number of targets the tool will select (with its rounding) next to the number the blast radius allows. If the tool rounds up and the limit rounds down, the experiment must be refused, not "close enough". For netem, refuse a device that already carries a configured qdisc (handle other than `0:`): the rollback `tc qdisc del ... root` would delete it as well. For pod faults, note the grace period the tool uses: Chaos Mesh's `pod-kill` (`gracePeriod`) and Litmus' `pod-delete` (`FORCE=true`) skip `preStop` by default, which is a different and harsher fault than a graceful deletion.

## 4. Measure the steady state now

```bash
python3 minichaos.py measure experiment.json --context "$CTX"     # one window, exit 0 only if every SLI and check is ok
curl -sG "$PROM/api/v1/query" --data-urlencode 'query=sum(rate(http_requests_total{code=~"5.."}[1m])) / sum(rate(http_requests_total[1m]))'
```

If the measurement is not `ok`, stop: the verdict would be `not-run` anyway, and a fault on top of an existing problem only deepens it. If it is `ok` but close to the threshold, the hypothesis has no headroom and the result will be noise; widen the window or fix the service first.

## 5. Agree on the abort

Name the abort-button owner and the mechanism (`touch /tmp/abort` for `minichaos run --abort-file /tmp/abort`, Ctrl-C, `kubectl delete -f chaos.json`, `kubectl annotate ... experiment.chaos-mesh.org/pause=true`). Check that the abort conditions are looser than the hypothesis and tighter than the SLO's pain threshold, and that losing the measurement aborts.

## 6. Run and watch

```bash
python3 minichaos.py run experiment.json --context "$CTX" --abort-file /tmp/abort --journal journal.json
kubectl get pods -n "$NS" -l "$SELECTOR" -w                                    # in a second terminal
```

| Signal during the run | Action |
|---|---|
| the experiment's own abort rule fires | let it roll back; do not restart it until the cause is understood |
| a real SLO alert or a customer report that the experiment does not measure | press the abort button; the experiment's SLI is missing something |
| probe errors or no data | abort if it lasts: you are injecting blind |
| a dependency outside the blast radius degrades | abort; the blast radius was wrong |
| the operator wants to "just try one more thing" | no: a new fault is a new experiment with its own review |

## 7. Confirm the rollback and the recovery

```bash
python3 -c 'import json; j = json.load(open("journal.json")); print(j["verdict"], j["rollback_ok"], j["recovered_after_s"]); [print(f["type"], "->", f["revert"]) for f in j["faults"]]'
ip netns exec "$NETNS" tc qdisc show dev "$DEV"                                 # no netem
kubectl get deployment -n "$NS" "$DEPLOY"                                        # READY equals the desired count
kubectl get podchaos,networkchaos -A                                             # no leftover chaos objects
pgrep -af 'burn --seconds' || echo "no burners"
```

If `rollback_ok` is false, run the manual command the journal names now, then verify again. Do not close the experiment while any fault is still in place.

## 8. Decide what the verdict means

| Verdict | Meaning | Next |
|---|---|---|
| `held` | the steady state survived the fault and returned within `recovery_s` | schedule it as a regression experiment; consider a slightly larger blast radius in staging |
| `deviated` | the hypothesis broke below the abort threshold | open a ticket with the journal; the finding is the first breached window and what the system did |
| `aborted` | an abort rule, lost measurements or a human stopped it | review it like an incident: why did the system fail this badly, and was the abort fast enough |
| `inconclusive` | nothing broke, but windows had no data | add traffic or lengthen the window, then rerun; do not report it as held |
| `not-run` | the steady state was already broken | fix or wait; the experiment did its job by refusing |
| `refused` | outside the blast radius | shrink the fault or justify a larger radius in review |
| `failed` | a fault could not be injected | fix the tooling (permissions, `sch_netem` missing, controller down); nothing was learned about the system |

## Report

State: the experiment title and environment; the hypothesis with its numbers; the fault, its targets and the blast radius with the worst-case count against the allowed count; the steady-state measurement before; the verdict; the first breached window and the worst value of each SLI during the fault; whether and when an abort fired and why; the rollback result and its verification (`rollback_ok`, the commands you checked with); the recovery time; and the follow-up (a ticket for a weakness, a scheduled regression run, or the change to the experiment). Never report `held` for a run with no-data windows, and never report an experiment as finished while `rollback_ok` is false.
