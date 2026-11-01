---
name: asg-review
description: Review an EC2 Auto Scaling group, before an incident or during one, by checking what counts as healthy, how capacity is spread and terminated, who writes desired capacity, how the scaling policies read their metrics, and whether lifecycle hooks and instance refresh can fail safely
version: 1.0.0
phase: cloud
lesson: cloud/autoscaling-groups
tags: [aws, ec2, autoscaling, health-checks, target-tracking, step-scaling, lifecycle-hooks, instance-refresh, availability-zones, review]
---

# Auto Scaling group review

Use this for a pre-launch or periodic review of an Auto Scaling group, and during incidents such as "the group keeps replacing instances", "it scaled in during the peak", "it terminated healthy instances", "the release went out and nothing serves" or "desired capacity keeps changing". During an incident, do not change desired capacity, terminate instances or start a refresh until step 4 has told you who else is writing to the group: a second writer will undo your change and hide the evidence.

The model: the group is a loop that compares desired capacity with the instances that count (`Pending`, `Pending:Wait`, `InService` and healthy) and launches or terminates the difference, zone by zone. Health checks decide what counts. You, scaling policies and scheduled actions decide desired capacity.

## 1. Collect the state

```bash
ASG=<group-name>
aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$ASG" > asg.json
aws autoscaling describe-policies --auto-scaling-group-name "$ASG" > policies.json
aws autoscaling describe-scaling-activities --auto-scaling-group-name "$ASG" --max-items 50 > activities.json
aws autoscaling describe-scheduled-actions --auto-scaling-group-name "$ASG" > scheduled.json
aws autoscaling describe-lifecycle-hooks --auto-scaling-group-name "$ASG" > hooks.json
aws autoscaling describe-instance-refreshes --auto-scaling-group-name "$ASG" --max-records 5 > refreshes.json
aws autoscaling describe-warm-pool --auto-scaling-group-name "$ASG" > warm-pool.json
aws cloudwatch describe-alarms --alarm-name-prefix "TargetTracking-$ASG" > tt-alarms.json
python3 miniasg.py plan --describe asg.json          # what the loop will do next with this exact state
```

Summarise in one line each: `MinSize`/`DesiredCapacity`/`MaxSize`, instance count per zone and lifecycle state, `HealthCheckType` and `HealthCheckGracePeriod`, `DefaultInstanceWarmup`, `DefaultCooldown`, `TerminationPolicies`, `SuspendedProcesses`, `TargetGroupARNs` or `LoadBalancerNames`, and the launch template name and version (`$Latest`, `$Default` or a number).

## 2. What counts as healthy

```bash
jq -r '.AutoScalingGroups[0] | "\(.HealthCheckType) grace=\(.HealthCheckGracePeriod) warmup=\(.DefaultInstanceWarmup) targets=\(.TargetGroupARNs | length)"' asg.json
jq -r '.AutoScalingGroups[0].Instances[] | "\(.InstanceId) \(.AvailabilityZone) \(.LifecycleState) \(.HealthStatus) v\(.LaunchTemplate.Version // "-") protected=\(.ProtectedFromScaleIn)"' asg.json
aws elbv2 describe-target-health --target-group-arn <target-group-arn> --query 'TargetHealthDescriptions[].[Target.Id,TargetHealth.State,TargetHealth.Reason]' --output text
```

| Finding | Risk | Change |
|---|---|---|
| `HealthCheckType` is `EC2` and the group is attached to a target group | an instance that boots but does not serve stays in service; a broken release passes an instance refresh | switch to `ELB` (`update-auto-scaling-group --health-check-type ELB`) after checking the target group's health check path and thresholds |
| grace period shorter than the measured time from launch to the first passing load balancer check | new instances are replaced while still booting, in a loop | set the grace period to that time plus a margin, or use a launch lifecycle hook and a grace period near 0 |
| grace period far longer than boot time | broken instances serve errors for the whole period before replacement | shorten it to boot time plus margin |
| `DefaultInstanceWarmup` unset | policies and refreshes fall back to `EstimatedInstanceWarmup`, the grace period or `DefaultCooldown`, which rarely match warm-up time | set it to the time from `InService` to steady metrics |
| targets `unhealthy` while instances are `Healthy` in the group | the group is not asking the load balancer, or the grace period has not ended | confirm `HealthCheckType` and the instance's `InService` time |
| `ReplaceUnhealthy` or `HealthCheck` in `SuspendedProcesses` | nothing is replaced, by design or by a forgotten change | find out who suspended it (CloudTrail `SuspendProcesses`) before resuming |

## 3. Capacity, zones and termination

```bash
jq -r '[.AutoScalingGroups[0].Instances[] | select(.LifecycleState == "InService") | .AvailabilityZone] | group_by(.) | map("\(.[0])=\(length)") | join(" ")' asg.json
jq -r '.AutoScalingGroups[0] | "zones=\(.AvailabilityZones | join(",")) policies=\(.TerminationPolicies | join(",")) suspended=\([.SuspendedProcesses[].ProcessName] | join(","))"' asg.json
```

| Finding | Meaning | Change |
|---|---|---|
| zones differ by more than one instance and nothing is launching | `AZRebalance` is suspended, a zone cannot launch the instance type, or the group is at max with no margin | read the latest `Failed` launch activities for capacity errors; add instance types (mixed instances policy) or zones |
| desired equals `MaxSize` | scale-out has no room; rebalancing can only use its 10% or one-instance margin | raise `MaxSize` if the policy keeps asking for more (look for repeated policy activities that did not change desired) |
| desired equals `MinSize` during a peak | a policy or schedule drove it there; see step 4 | |
| `OldestInstance` or `NewestInstance` expected to pick a specific instance | zone balance is applied first; the policy only chooses inside the fullest zone | use scale-in protection for instances that must stay |
| many protected instances | scale-in and refreshes may stall waiting for them | review `ProtectedFromScaleIn` and the refresh's `ScaleInProtectedInstances` preference |

## 4. Who writes desired capacity

Read the first sentence of each activity's `Cause`; it names the writer:

```bash
jq -r '.Activities[] | "\(.StartTime) \(.StatusCode) \(.Description) || \(.Cause | split(".  ")[0])"' activities.json | head -40
jq -r '.ScheduledUpdateGroupActions[] | "\(.ScheduledActionName) \(.Recurrence // .StartTime) min=\(.MinSize) max=\(.MaxSize) desired=\(.DesiredCapacity)"' scheduled.json
```

| Cause starts with | Writer |
|---|---|
| `a user request explicitly set group desired capacity` | someone or something calling `set-desired-capacity` (check CloudTrail for the principal: a script, a pipeline, cluster-autoscaler) |
| `a user request update of AutoScalingGroup constraints` | `update-auto-scaling-group`, often infrastructure as code reapplying a fixed desired capacity |
| `a scheduled action update of AutoScalingGroup constraints` | a scheduled action |
| `a monitor alarm ... triggered policy ...` | a scaling policy; the alarm name says which (`TargetTracking-...-AlarmHigh` or `-AlarmLow`, or your own) |
| `an instance was taken out of service in response to an ELB system health check failure` / `... EC2 health check ...` / `... a user health-check` | health replacement, not a capacity change |
| `... to aid in balancing the group's zones` | `AZRebalance` |
| `... in response to an instance refresh` | an instance refresh |

Two writers that alternate (a schedule and a policy, or Terraform with a fixed `desired_capacity` and a policy) are a finding on their own: decide which one owns the number and remove the other, for example with `lifecycle { ignore_changes = [desired_capacity] }` in Terraform or OpenTofu.

## 5. Scaling policies

```bash
jq -r '.ScalingPolicies[] | "\(.PolicyName) \(.PolicyType) warmup=\(.EstimatedInstanceWarmup) target=\(.TargetTrackingConfiguration.TargetValue) disableScaleIn=\(.TargetTrackingConfiguration.DisableScaleIn) adj=\(.AdjustmentType) cooldown=\(.Cooldown)"' policies.json
jq -r '.MetricAlarms[] | "\(.AlarmName) \(.ComparisonOperator) \(.Threshold) periods=\(.EvaluationPeriods)x\(.Period)s \(.StateValue)"' tt-alarms.json
python3 miniasg.py run target-tracking --policies policies.json     # replay a trace through the group's own definitions
```

| Finding | Risk | Change |
|---|---|---|
| target tracking on a metric that saturates (CPU on a CPU-bound service) | each step under-reacts while the metric sits at its ceiling; recovery takes several warmups | a lower target, a request-count-per-target metric, or a step policy for the steep part |
| target tracking on a metric that is not proportional to load per instance (latency, a total queue length, total request count) | the formula sizes the group wrongly | a per-instance metric (queue length divided by instances, `ALBRequestCountPerTarget`) |
| no warmup anywhere | warming instances' low metrics drive scale-in decisions right after a scale-out | set `DefaultInstanceWarmup` |
| step adjustments with a gap or a bound on the wrong side of the threshold | some metric values match no step | rebuild the steps from the threshold; check them with `miniasg` |
| simple scaling policies | each step waits for the cooldown after the launch completes; slow under sustained load | replace with target tracking or step scaling |
| several target tracking policies | scale-out follows the most demanding, scale-in needs all of them | check that each metric is meaningful on its own |

## 6. Lifecycle hooks, instance refresh, warm pool

```bash
jq -r '.LifecycleHooks[] | "\(.LifecycleHookName) \(.LifecycleTransition) timeout=\(.HeartbeatTimeout) default=\(.DefaultResult)"' hooks.json
jq -r '.InstanceRefreshes[] | "\(.Status) \(.PercentageComplete)% min=\(.Preferences.MinHealthyPercentage) max=\(.Preferences.MaxHealthyPercentage) warmup=\(.Preferences.InstanceWarmup) skip=\(.Preferences.SkipMatching) rollback=\(.Preferences.AutoRollback) \(.StatusReason // "")"' refreshes.json
```

| Finding | Risk | Change |
|---|---|---|
| launch hook with `DefaultResult` `ABANDON` and a timeout close to the bootstrap time | slow boots are abandoned and relaunched in a loop | raise the timeout, record heartbeats from the bootstrap, or make the bootstrap call `complete-lifecycle-action` with `ABANDON` on failure so it fails fast |
| termination hook with a long timeout | scale-in and refreshes are slow; draining capacity is still billed | match the timeout to the drain time |
| refresh preferences without `AutoRollback` and an alarm specification | a bad release completes or stalls instead of rolling back | `--preferences '{"AutoRollback":true,"AlarmSpecification":{"Alarms":["<5xx alarm>"]}}'` |
| `MinHealthyPercentage` 90 on a small group | one instance at a time, but each batch removes more than 10% of capacity | use 100 with `MaxHealthyPercentage` above 100 to launch first |
| `SkipMatching` off on a refresh meant to finish a partial rollout | instances already on the target version are replaced again | turn it on |
| warm pool instances on an older template version | the next scale-out serves the old version | refresh after `InService` instances; check the pool's instances after the refresh |

## 7. Decision

| Situation | Verdict |
|---|---|
| `EC2` health checks with a load balancer, or no warmup set | fix before the next release; these turn a bad release into an outage |
| two writers of desired capacity | fix now; pick one owner |
| saturating or non-proportional metric in target tracking | fix before the next peak |
| zone imbalance with `Failed` launch activities | capacity problem in a zone: diversify instance types |
| hooks or refresh preferences without a failure path | fix before relying on automated rollouts |
| none of the above | the group is sound; record the review |

## 8. Report

State: the group's capacity line (min, desired, max, instances per zone); health check type, grace period and warmup, with the measured boot time; every writer of desired capacity seen in the last 50 activities and whether they conflict; each scaling policy with its metric, target or steps and the finding; hook and refresh settings with the failure path; `miniasg plan` output for the current state; and the changes, ordered by the decision table, each with the exact command or code change. Never list "increased MaxSize" or "terminated the bad instances" as a fix without the cause that made them necessary.
