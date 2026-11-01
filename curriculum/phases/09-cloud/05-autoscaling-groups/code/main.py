from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

EPOCH = datetime(2026, 11, 1, 9, 0, 0, tzinfo=timezone.utc)
SCENARIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios")
LIVE = ("Pending", "Pending:Wait", "InService")
GOING = ("Terminating", "Terminating:Wait")
LAUNCHING = "autoscaling:EC2_INSTANCE_LAUNCHING"
TERMINATING = "autoscaling:EC2_INSTANCE_TERMINATING"
HIGH_DATAPOINTS = 3
LOW_DATAPOINTS = 15
REFRESH_PATIENCE = 3600
SETTLE_LIMIT = 6 * 3600
CPU_CEILING = 100.0
COMPARE: Dict[str, Callable[[float, float], bool]] = {
    "GreaterThanThreshold": lambda value, threshold: value > threshold,
    "GreaterThanOrEqualToThreshold": lambda value, threshold: value >= threshold,
    "LessThanThreshold": lambda value, threshold: value < threshold,
    "LessThanOrEqualToThreshold": lambda value, threshold: value <= threshold,
}
TERMINATION_POLICIES = ("Default", "OldestInstance", "NewestInstance", "OldestLaunchTemplate", "OldestLaunchConfiguration", "ClosestToNextInstanceHour")
ADJUSTMENT_TYPES = ("ChangeInCapacity", "ExactCapacity", "PercentChangeInCapacity")


class AsgError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class InputError(Exception):
    pass


def plain(cause: str) -> str:
    return re.sub(r"At \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ", "", cause).replace("  ", " ")


def stamp(seconds: int) -> str:
    return (EPOCH + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def clock(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def target_tracking_capacity(current: int, metric: float, target: float) -> int:
    if target <= 0:
        raise InputError(f"TargetValue must be positive, got {target}")
    return math.ceil(round(current * metric / target, 6))


def round_percent_change(delta: float) -> int:
    delta = round(delta, 6)
    if delta >= 1:
        return math.floor(delta)
    if delta > 0:
        return 1
    if delta > -1 and delta < 0:
        return -1
    if delta <= -1:
        return math.ceil(delta)
    return 0


def adjusted_capacity(adjustment_type: str, current: int, adjustment: float, min_magnitude: Optional[int] = None) -> int:
    if adjustment_type == "ExactCapacity":
        return int(adjustment)
    if adjustment_type == "ChangeInCapacity":
        return current + int(adjustment)
    if adjustment_type == "PercentChangeInCapacity":
        delta = round_percent_change(current * adjustment / 100)
        if min_magnitude and 0 < abs(delta) < min_magnitude:
            delta = min_magnitude if delta > 0 else -min_magnitude
        return current + delta
    raise InputError(f"unknown AdjustmentType {adjustment_type!r} (one of {', '.join(ADJUSTMENT_TYPES)})")


@dataclass
class Step:
    lower: Optional[float]
    upper: Optional[float]
    adjustment: float

    def describe(self, above: bool = True) -> str:
        low = "-inf" if self.lower is None else f"{self.lower:g}"
        high = "+inf" if self.upper is None else f"{self.upper:g}"
        interval = f"[{low}, {high})" if above else f"({low}, {high}]"
        return f"{interval} -> {self.adjustment:+g}"


def validate_steps(steps: Sequence[Step]) -> None:
    if not steps:
        raise InputError("a StepScaling policy needs at least one StepAdjustment")
    if any(s.lower is None and s.upper is None for s in steps):
        raise InputError("a step adjustment cannot have both bounds null")
    ordered = sorted(steps, key=lambda s: -math.inf if s.lower is None else s.lower)
    for left, right in zip(ordered, ordered[1:]):
        if left.upper is None or right.lower is None or left.upper != right.lower:
            raise InputError(f"step adjustments overlap or leave a gap: {left.describe()} then {right.describe()}")
    if ordered[0].lower is not None and ordered[0].lower < 0:
        raise InputError("a negative lower bound needs a step adjustment with a null lower bound")
    if ordered[-1].upper is not None and ordered[-1].upper > 0:
        raise InputError("a positive upper bound needs a step adjustment with a null upper bound")


def matching_step(steps: Sequence[Step], threshold: float, value: float, scale_out: bool) -> Optional[Step]:
    diff = value - threshold
    above = diff > 0 or (diff == 0 and scale_out)
    for step in steps:
        if above:
            fits = (step.lower is None or diff >= step.lower) and (step.upper is None or diff < step.upper)
        else:
            fits = (step.lower is None or diff > step.lower) and (step.upper is None or diff <= step.upper)
        if fits:
            return step
    return None


def even_targets(total: int, counts: Dict[str, int], zones: Sequence[str]) -> Dict[str, int]:
    if not zones:
        return {}
    base, extra = divmod(total, len(zones))
    ranked = sorted(zones, key=lambda z: (-counts.get(z, 0), zones.index(z)))
    return {zone: base + (1 if rank < extra else 0) for rank, zone in enumerate(ranked)}


def moves_to_balance(total: int, counts: Dict[str, int], zones: Sequence[str]) -> int:
    targets = even_targets(total, counts, zones)
    return sum(max(0, targets[z] - counts.get(z, 0)) for z in zones)


def version_key(version: str) -> Tuple[int, str]:
    return (int(version), version) if version.isdigit() else (sys.maxsize, version)


@dataclass
class Instance:
    id: str
    zone: str
    version: str
    launched_at: int
    purpose: str = "capacity"
    state: str = "Pending"
    health: str = "Healthy"
    ec2_state: str = "pending"
    elb_ok: bool = True
    protected: bool = False
    ready_at: int = 0
    in_service_at: Optional[int] = None
    wait_until: Optional[int] = None
    wait_result: Optional[str] = None
    gone_at: Optional[int] = None
    unhealthy_reason: str = ""
    activity: Optional[Dict[str, Any]] = None


def seconds_to_billing_hour(instance: Instance, now: int) -> int:
    return 3600 - (now - instance.launched_at) % 3600


def narrow(pool: List[Instance], policy: str, current_version: str, now: int) -> List[Instance]:
    if policy == "OldestInstance":
        oldest = min(i.launched_at for i in pool)
        return [i for i in pool if i.launched_at == oldest]
    if policy == "NewestInstance":
        newest = max(i.launched_at for i in pool)
        return [i for i in pool if i.launched_at == newest]
    if policy == "ClosestToNextInstanceHour":
        closest = min(seconds_to_billing_hour(i, now) for i in pool)
        return [i for i in pool if seconds_to_billing_hour(i, now) == closest]
    if policy in ("OldestLaunchTemplate", "OldestLaunchConfiguration"):
        outdated = [i for i in pool if i.version != current_version] or pool
        oldest = min(version_key(i.version) for i in outdated)
        return [i for i in outdated if version_key(i.version) == oldest]
    if policy == "Default":
        outdated = [i for i in pool if i.version != current_version]
        if outdated:
            oldest = min(version_key(i.version) for i in outdated)
            pool = [i for i in outdated if version_key(i.version) == oldest]
        return narrow(pool, "ClosestToNextInstanceHour", current_version, now)
    raise InputError(f"unknown termination policy {policy!r} (one of {', '.join(TERMINATION_POLICIES)})")


def pick_victim(pool: List[Instance], policies: Sequence[str], current_version: str, now: int) -> Instance:
    for policy in list(policies) + ["Default"]:
        pool = narrow(pool, policy, current_version, now)
        if len(pool) == 1:
            return pool[0]
    return min(pool, key=lambda i: i.id)


@dataclass
class Group:
    name: str = "web"
    min_size: int = 1
    max_size: int = 6
    desired: int = 2
    zones: List[str] = field(default_factory=lambda: ["us-east-1a", "us-east-1b", "us-east-1c"])
    version: str = "1"
    health_check_type: str = "EC2"
    grace_period: int = 0
    default_cooldown: int = 300
    default_warmup: Optional[int] = None
    termination_policies: List[str] = field(default_factory=lambda: ["Default"])
    boot_seconds: int = 60
    drain_seconds: int = 30
    broken_versions: List[str] = field(default_factory=list)


def validate_group(group: Group) -> None:
    if not group.zones:
        raise AsgError("ValidationError", "At least one Availability Zone or VPC Subnet is required.")
    if group.min_size < 0 or group.max_size < group.min_size:
        raise AsgError("ValidationError", f"max size {group.max_size} must be at least min size {group.min_size}")
    if not group.min_size <= group.desired <= group.max_size:
        raise AsgError("ValidationError", f"Desired capacity:{group.desired} must be between the specified min size:{group.min_size} and max size:{group.max_size}")
    if group.health_check_type not in ("EC2", "ELB"):
        raise AsgError("ValidationError", f"HealthCheckType must be EC2 or ELB, got {group.health_check_type}")
    for policy in group.termination_policies:
        if policy not in TERMINATION_POLICIES:
            raise AsgError("ValidationError", f"unknown termination policy {policy} (one of {', '.join(TERMINATION_POLICIES)})")


@dataclass
class Alarm:
    name: str
    threshold: float
    comparison: str = "GreaterThanThreshold"
    evaluation_periods: int = 1


@dataclass
class Policy:
    name: str
    kind: str
    target: float = 0.0
    disable_scale_in: bool = False
    warmup: Optional[int] = None
    adjustment_type: str = "ChangeInCapacity"
    adjustment: float = 0
    steps: List[Step] = field(default_factory=list)
    min_magnitude: Optional[int] = None
    cooldown: Optional[int] = None
    alarm: Optional[Alarm] = None
    high_points: int = HIGH_DATAPOINTS
    low_points: int = LOW_DATAPOINTS
    history: List[Optional[float]] = field(default_factory=list)
    state: str = "INSUFFICIENT_DATA"
    note: str = ""


@dataclass
class Hook:
    name: str
    transition: str
    heartbeat_timeout: int = 3600
    default_result: str = "ABANDON"
    respond_after: Optional[int] = None
    respond_with: str = "CONTINUE"


@dataclass
class Metric:
    values: List[float]
    period: int = 60
    mode: str = "observed"
    start: int = 60
    series: Dict[str, List[float]] = field(default_factory=dict)

    def raw(self, policy: str, index: int) -> Optional[float]:
        values = self.series.get(policy, self.values)
        return values[index] if index < len(values) else None


@dataclass
class Refresh:
    id: str
    version: str
    previous: str
    min_healthy: int
    max_healthy: int
    warmup: int
    original: List[str]
    pending: List[str]
    started_at: int
    last_progress: int
    status: str = "InProgress"
    reason: str = ""
    extra: int = 0
    replaced: int = 0


class Asg:
    def __init__(self, group: Group, policies: Sequence[Policy] = (), hooks: Sequence[Hook] = (), metric: Optional[Metric] = None, tick: int = 10, create: bool = True):
        validate_group(group)
        self.group = group
        self.policies = list(policies)
        self.hooks = list(hooks)
        self.metric = metric
        self.tick = tick
        self.now = 0
        self.instances: List[Instance] = []
        self.activities: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []
        self.checkpoints: List[Dict[str, Any]] = []
        self.rejected: List[Dict[str, Any]] = []
        self.decisions: List[Dict[str, Any]] = []
        self.down_zones: set = set()
        self.refresh: Optional[Refresh] = None
        self.allowance = 0
        self.rebalance_batch = 0
        self.owed = 0
        self.cooldown_until: Optional[int] = None
        self.cooldown_pending: Optional[int] = None
        self.cause = ""
        self.counter = 0
        self.instance_counter = 0
        self.desired_history: List[Tuple[int, int]] = [(0, group.desired)]
        self.peak = 0
        self.min_serving: Optional[int] = None
        self.reached = False
        self.window_peak = 0
        self.window_min: Optional[int] = None
        if create:
            self.desired_history = [(0, 0)]
            target, group.desired = group.desired, 0
            self.log("create", f"group {group.name}: min {group.min_size}, max {group.max_size}, zones {', '.join(group.zones)}, health check {group.health_check_type} (grace {group.grace_period}s)")
            self.set_desired(target, "a user request created an AutoScalingGroup")

    def log(self, kind: str, message: str) -> None:
        self.events.append({"t": self.now, "kind": kind, "message": message})

    def next_id(self) -> int:
        self.counter += 1
        return self.counter

    def activity(self, description: str, cause: str, zone: str = "", status: str = "InProgress", message: str = "") -> Dict[str, Any]:
        n = self.next_id()
        record: Dict[str, Any] = {
            "ActivityId": f"{n:08x}-0000-4000-8000-{n:012x}",
            "AutoScalingGroupName": self.group.name,
            "Description": description,
            "Cause": cause,
            "StartTime": stamp(self.now),
            "EndTime": stamp(self.now) if status != "InProgress" else None,
            "StatusCode": status,
            "Progress": 0 if status == "InProgress" else 100,
            "Details": json.dumps({"Availability Zone": zone}) if zone else "{}",
        }
        if message:
            record["StatusMessage"] = message
        self.activities.append(record)
        return record

    def finish(self, record: Optional[Dict[str, Any]], status: str = "Successful", message: str = "") -> None:
        if record is None or record["StatusCode"] != "InProgress":
            return
        record.update(StatusCode=status, EndTime=stamp(self.now), Progress=100)
        if message:
            record["StatusMessage"] = message

    def set_desired(self, value: int, sentence: str) -> bool:
        old = self.group.desired
        if value == old:
            return False
        self.group.desired = value
        self.cause = f"At {stamp(self.now)} {sentence} changing the desired capacity from {old} to {value}."
        self.desired_history.append((self.now, value))
        self.log("desired", f"{old} -> {value}: {sentence}")
        return True

    def live(self) -> List[Instance]:
        return [i for i in self.instances if i.state in LIVE]

    def find(self, instance_id: str) -> Instance:
        for instance in self.instances:
            if instance.id == instance_id and instance.state != "Terminated":
                return instance
        raise AsgError("ValidationError", f"Instance Id not found - No managed instance found for instance ID: {instance_id}")

    def in_grace(self, instance: Instance) -> bool:
        return instance.in_service_at is None or self.now - instance.in_service_at < self.group.grace_period

    def effective_warmup(self, policy: Optional[Policy] = None) -> int:
        if policy is not None and policy.warmup is not None:
            return policy.warmup
        if self.group.default_warmup is not None:
            return self.group.default_warmup
        return self.group.default_cooldown

    def ready(self, warmup: int) -> List[Instance]:
        return [i for i in self.instances if i.state == "InService" and i.health == "Healthy" and i.in_service_at is not None and self.now >= i.in_service_at + warmup]

    def warming(self, warmup: int) -> List[Instance]:
        return [i for i in self.instances if i.state in ("Pending", "Pending:Wait") or (i.state == "InService" and i.in_service_at is not None and self.now < i.in_service_at + warmup)]

    def zone_counts(self, instances: Optional[Sequence[Instance]] = None) -> Dict[str, int]:
        counts = {zone: 0 for zone in self.group.zones}
        for instance in self.live() if instances is None else instances:
            counts[instance.zone] = counts.get(instance.zone, 0) + 1
        return counts

    def in_cooldown(self) -> bool:
        return self.cooldown_pending is not None or (self.cooldown_until is not None and self.now < self.cooldown_until)

    def launch_version(self) -> str:
        if self.refresh is not None and self.refresh.status == "InProgress":
            return self.refresh.version
        return self.group.version

    def hook_for(self, transition: str) -> Optional[Hook]:
        for hook in self.hooks:
            if hook.transition == transition:
                return hook
        return None

    def set_desired_capacity(self, value: int, honor_cooldown: bool = False) -> None:
        if value > self.group.max_size:
            raise AsgError("ValidationError", f"New SetDesiredCapacity value {value} is above max value {self.group.max_size} for the AutoScalingGroup.")
        if value < self.group.min_size:
            raise AsgError("ValidationError", f"New SetDesiredCapacity value {value} is below min value {self.group.min_size} for the AutoScalingGroup.")
        if honor_cooldown and self.in_cooldown():
            raise AsgError("ScalingActivityInProgress", f"group {self.group.name} is in its cooldown period")
        self.set_desired(value, "a user request explicitly set group desired capacity")

    def update_group(self, min_size: Optional[int] = None, max_size: Optional[int] = None, desired: Optional[int] = None, **settings: Any) -> None:
        low = self.group.min_size if min_size is None else min_size
        high = self.group.max_size if max_size is None else max_size
        if low > high:
            raise AsgError("ValidationError", f"min size {low} is greater than max size {high}")
        if desired is not None and not low <= desired <= high:
            raise AsgError("ValidationError", f"Desired capacity:{desired} must be between the specified min size:{low} and max size:{high}")
        target = desired if desired is not None else min(max(self.group.desired, low), high)
        self.group.min_size, self.group.max_size = low, high
        for key, value in settings.items():
            if value is not None:
                setattr(self.group, key, value)
        validate_group(Group(**dict(vars(self.group), desired=target)))
        self.log("update", f"min {low}, max {high}" + "".join(f", {k} {v}" for k, v in settings.items() if v is not None))
        self.set_desired(target, f"a user request update of AutoScalingGroup constraints to min: {low}, max: {high}, desired: {target}")

    def set_instance_health(self, instance_id: str, status: str, respect_grace: bool = False) -> None:
        instance = self.find(instance_id)
        if status not in ("Healthy", "Unhealthy"):
            raise AsgError("ValidationError", "Valid instance health states are: [Healthy, Unhealthy]")
        if status == "Unhealthy" and respect_grace and self.in_grace(instance):
            self.log("health", f"{instance.id}: Unhealthy ignored, still inside the {self.group.grace_period}s grace period")
            return
        if status == "Unhealthy":
            self.mark_unhealthy(instance, "a user health-check")
        else:
            instance.health = "Healthy"
            instance.unhealthy_reason = ""

    def terminate_instance(self, instance_id: str, decrement: bool) -> None:
        instance = self.find(instance_id)
        if instance.state in GOING:
            raise AsgError("ValidationError", f"instance {instance_id} is already terminating")
        before = self.group.desired
        if decrement:
            if before - 1 < self.group.min_size:
                raise AsgError("ValidationError", f"Currently, desiredSize equals minSize ({self.group.min_size}). Terminating instance without replacement will violate group's min size constraint.")
            self.group.desired = before - 1
            self.desired_history.append((self.now, self.group.desired))
            self.log("desired", f"{before} -> {self.group.desired}: terminate-instance-in-auto-scaling-group --should-decrement-desired-capacity")
        tail = f", shrinking the capacity from {before} to {self.group.desired}." if decrement else "."
        self.terminate(instance, f"At {stamp(self.now)} instance {instance.id} was taken out of service in response to a user request{tail}", "user request" + (", desired decremented" if decrement else ", replacement follows"))

    def stop_instance(self, instance_id: str) -> None:
        instance = self.find(instance_id)
        instance.ec2_state = "stopped"
        self.log("ec2", f"{instance.id} stopped outside the group (EC2 state stopped)")

    def set_elb_health(self, instance_id: str, healthy: bool) -> None:
        instance = self.find(instance_id)
        instance.elb_ok = healthy
        self.log("elb", f"{instance.id} load balancer target {'healthy' if healthy else 'unhealthy'}")

    def zone_outage(self, zone: str) -> None:
        self.down_zones.add(zone)
        for instance in self.live():
            if instance.zone == zone:
                instance.ec2_state = "stopped"
        self.log("zone", f"{zone} unavailable: its instances stop running and launches there fail")

    def zone_recover(self, zone: str) -> None:
        self.down_zones.discard(zone)
        self.log("zone", f"{zone} available again")

    def set_protection(self, instance_id: str, protected: bool) -> None:
        self.find(instance_id).protected = protected
        self.log("protect", f"{instance_id} ProtectedFromScaleIn={protected}")

    def complete_lifecycle_action(self, instance_id: str, result: str) -> None:
        instance = self.find(instance_id)
        if instance.state not in ("Pending:Wait", "Terminating:Wait"):
            raise AsgError("ValidationError", f"No active Lifecycle Action found with instance ID {instance_id}")
        if result not in ("CONTINUE", "ABANDON"):
            raise AsgError("ValidationError", "LifecycleActionResult must be CONTINUE or ABANDON")
        instance.wait_until, instance.wait_result = self.now, result

    def start_instance_refresh(self, version: Optional[str] = None, min_healthy: int = 90, max_healthy: int = 100, warmup: Optional[int] = None, skip_matching: bool = False) -> Refresh:
        if self.refresh is not None and self.refresh.status == "InProgress":
            raise AsgError("InstanceRefreshInProgress", f"An Instance Refresh is already in progress and blocks the execution of this Instance Refresh ({self.refresh.id}).")
        if not 0 <= min_healthy <= 100 or not 100 <= max_healthy <= 200 or max_healthy - min_healthy > 100:
            raise AsgError("ValidationError", f"MinHealthyPercentage {min_healthy} must be 0-100, MaxHealthyPercentage {max_healthy} must be 100-200, and they may differ by at most 100")
        target = version or self.group.version
        if warmup is None:
            warmup = self.group.default_warmup if self.group.default_warmup is not None else self.group.grace_period
        live = self.live()
        pending = [i.id for i in live if not skip_matching or i.version != target]
        n = self.next_id()
        self.refresh = Refresh(f"{n:08x}-refresh", target, self.group.version, min_healthy, max_healthy, warmup, [i.id for i in live], pending, self.now, self.now)
        self.log("refresh", f"start: version {target}, {len(pending)} instance(s) to replace, MinHealthyPercentage {min_healthy}, MaxHealthyPercentage {max_healthy}, warmup {warmup}s")
        return self.refresh

    def mark_unhealthy(self, instance: Instance, reason: str) -> None:
        instance.health = "Unhealthy"
        instance.unhealthy_reason = reason
        self.log("unhealthy", f"{instance.id} ({instance.zone}) marked Unhealthy: {reason}")

    def launch(self, count: int, cause: str = "", purpose: str = "") -> List[Instance]:
        launched: List[Instance] = []
        start = len(self.live())
        for _ in range(count):
            counts = self.zone_counts()
            zone = None
            for candidate in sorted(self.group.zones, key=lambda z: (counts.get(z, 0), self.group.zones.index(z))):
                if candidate in self.down_zones:
                    self.log("launch", f"attempt in {candidate} failed (zone unavailable); trying the next zone with the fewest instances")
                    self.activity("Launching a new EC2 instance.  Status Reason: launch failed, Availability Zone unavailable", f"At {stamp(self.now)} an attempt to launch in {candidate} failed; trying another Availability Zone.", candidate, "Failed", f"{candidate} cannot launch instances")
                    continue
                zone = candidate
                break
            if zone is None:
                self.log("error", "no Availability Zone can launch an instance")
                break
            kind = purpose or ("replace" if self.owed > 0 else "capacity")
            if kind == "replace":
                self.owed -= 1
            self.instance_counter += 1
            instance = Instance(f"i-{self.instance_counter:017x}", zone, self.launch_version(), self.now, kind, ready_at=self.now + self.group.boot_seconds)
            self.instances.append(instance)
            launched.append(instance)
            sentence = cause or f"At {stamp(self.now)} an instance was started in response to a difference between desired and actual capacity, increasing the capacity from {start} to {start + count}."
            prefix = self.cause + "  " if self.cause and not cause and kind == "capacity" else ""
            instance.activity = self.activity(f"Launching a new EC2 instance: {instance.id}", prefix + sentence, zone)
            self.log("launch", f"{instance.id} in {zone} (version {instance.version}, {kind})")
        return launched

    def terminate(self, instance: Instance, cause: str, reason: str) -> None:
        instance.state = "Terminating"
        instance.gone_at = self.now + self.group.drain_seconds
        instance.activity = self.activity(f"Terminating EC2 instance: {instance.id}", cause, instance.zone)
        self.log("terminate", f"{instance.id} in {instance.zone} (version {instance.version}): {reason}")

    def enter_service(self, instance: Instance) -> None:
        instance.state = "InService"
        instance.in_service_at = self.now
        instance.wait_until = instance.wait_result = None
        instance.elb_ok = instance.version not in self.group.broken_versions
        self.finish(instance.activity)
        self.log("in-service", f"{instance.id} ({instance.zone}, version {instance.version}) InService")

    def wait(self, instance: Instance, hook: Hook, state: str) -> None:
        instance.state = state
        if hook.respond_after is not None and hook.respond_after < hook.heartbeat_timeout:
            instance.wait_until, instance.wait_result = self.now + hook.respond_after, hook.respond_with
        else:
            instance.wait_until, instance.wait_result = self.now + min(hook.heartbeat_timeout, 48 * 3600), None
        self.log("hook", f"{instance.id} {state} for lifecycle hook {hook.name} (heartbeat timeout {hook.heartbeat_timeout}s, default result {hook.default_result})")

    def advance_lifecycle(self) -> None:
        for instance in list(self.instances):
            if instance.state == "Pending" and self.now >= instance.ready_at:
                if instance.ec2_state == "pending":
                    instance.ec2_state = "running"
                hook = self.hook_for(LAUNCHING)
                if hook is not None:
                    self.wait(instance, hook, "Pending:Wait")
                else:
                    self.enter_service(instance)
            elif instance.state in ("Pending:Wait", "Terminating:Wait") and instance.wait_until is not None and self.now >= instance.wait_until:
                self.finish_wait(instance)
            elif instance.state == "Terminating" and instance.gone_at is not None and self.now >= instance.gone_at:
                hook = self.hook_for(TERMINATING)
                if hook is not None and instance.wait_until is None:
                    self.wait(instance, hook, "Terminating:Wait")
                else:
                    self.gone(instance)

    def finish_wait(self, instance: Instance) -> None:
        transition = LAUNCHING if instance.state == "Pending:Wait" else TERMINATING
        hook = self.hook_for(transition)
        default = hook.default_result if hook is not None else "CONTINUE"
        result = instance.wait_result or default
        how = "completed" if instance.wait_result else "timed out"
        self.log("hook", f"{instance.id} lifecycle action {how} with {result}")
        if instance.state == "Terminating:Wait":
            self.gone(instance)
        elif result == "CONTINUE":
            self.enter_service(instance)
        else:
            self.finish(instance.activity, "Cancelled", f"Instance failed to complete user's Lifecycle Action: Lifecycle Action Completed with {result} Result.")
            instance.wait_until = self.now
            instance.state = "Terminating"
            instance.gone_at = self.now
            self.gone(instance)

    def gone(self, instance: Instance) -> None:
        instance.state = "Terminated"
        instance.ec2_state = "terminated"
        self.finish(instance.activity)
        self.log("terminated", f"{instance.id} ({instance.zone}) Terminated")

    def check_health(self) -> None:
        for instance in self.instances:
            if instance.state != "InService" or instance.health != "Healthy":
                continue
            if instance.ec2_state != "running":
                self.mark_unhealthy(instance, "an EC2 health check indicating it has been terminated or stopped")
            elif self.group.health_check_type == "ELB" and not instance.elb_ok and not self.in_grace(instance):
                self.mark_unhealthy(instance, "an ELB system health check failure")

    def replace_unhealthy(self) -> None:
        unhealthy = sorted((i for i in self.instances if i.state == "InService" and i.health == "Unhealthy"), key=lambda i: i.id)
        urgent = [i for i in unhealthy if "ELB" not in i.unhealthy_reason]
        gradual = [i for i in unhealthy if "ELB" in i.unhealthy_reason]
        warmup = self.group.default_warmup or 0
        in_flight = len([i for i in self.instances if i.purpose == "replace" and i in self.warming(warmup)])
        budget = max(1, self.group.desired * 10 // 100) - in_flight - self.owed
        for instance in urgent + gradual[: max(0, budget)]:
            self.owed += 1
            self.terminate(instance, f"At {stamp(self.now)} an instance was taken out of service in response to {instance.unhealthy_reason}.", f"replace unhealthy ({instance.unhealthy_reason})")

    def step_refresh(self) -> None:
        refresh = self.refresh
        if refresh is None or refresh.status != "InProgress":
            return
        live = self.live()
        old = [i for i in live if i.id in refresh.pending]
        fresh = [i for i in live if i.id not in refresh.original]
        in_flight = [i for i in fresh if i.state != "InService" or i.health != "Healthy" or i.in_service_at is None or self.now < i.in_service_at + refresh.warmup]
        if not old and not in_flight and not refresh.extra:
            refresh.status = "Successful"
            self.group.version = refresh.version
            self.log("refresh", f"Successful: {refresh.replaced} instance(s) replaced, group now launches version {refresh.version}")
            return
        if self.now - refresh.last_progress > REFRESH_PATIENCE:
            refresh.status = "Failed"
            refresh.reason = f"no replacement became healthy and warmed up within {REFRESH_PATIENCE}s; {len(old)} instance(s) not replaced"
            self.allowance -= refresh.extra
            refresh.extra = 0
            self.log("refresh", f"Failed: {refresh.reason}")
            return
        if in_flight or self.owed or any(i.state in GOING for i in self.instances):
            return
        if refresh.extra:
            for victim in self.choose_victims(refresh.extra, old):
                self.retire(refresh, victim)
            self.allowance -= refresh.extra
            refresh.extra = 0
            return
        desired = self.group.desired
        if refresh.max_healthy > 100 or refresh.min_healthy == 100:
            batch = min(len(old), max(1, desired * refresh.max_healthy // 100 - desired))
            refresh.extra = batch
            self.allowance += batch
            self.log("refresh", f"launch {batch} new instance(s) before terminating old ones (MaxHealthyPercentage {refresh.max_healthy})")
            return
        healthy = len(self.ready(refresh.warmup))
        batch = min(len(old), max(1, healthy - math.ceil(desired * refresh.min_healthy / 100)))
        self.log("refresh", f"terminate {batch} old instance(s), keeping at least {math.ceil(desired * refresh.min_healthy / 100)} of {desired} healthy where possible")
        for victim in self.choose_victims(batch, old):
            self.retire(refresh, victim)

    def retire(self, refresh: Refresh, victim: Instance) -> None:
        refresh.pending.remove(victim.id)
        refresh.replaced += 1
        refresh.last_progress = self.now
        self.terminate(victim, f"At {stamp(self.now)} an instance was taken out of service in response to an instance refresh.", f"instance refresh to version {refresh.version}")

    def quiet(self) -> bool:
        busy = any(i.state in ("Pending", "Pending:Wait") + GOING for i in self.instances)
        refreshing = self.refresh is not None and self.refresh.status == "InProgress"
        return not busy and not self.allowance and not self.owed and not refreshing and len(self.live()) == self.group.desired

    def rebalance(self) -> None:
        if self.rebalance_batch:
            if any(i.state in ("Pending", "Pending:Wait") for i in self.instances):
                return
            surplus = len(self.live()) - self.group.desired
            for victim in self.choose_victims(surplus):
                self.terminate(victim, f"At {stamp(self.now)} an instance was taken out of service to aid in balancing the group's zones.", "AZRebalance, after the new instance is InService")
            self.allowance -= self.rebalance_batch
            self.rebalance_batch = 0
            return
        if not self.quiet():
            return
        zones = [z for z in self.group.zones if z not in self.down_zones]
        counts = self.zone_counts()
        usable = {z: counts.get(z, 0) for z in zones}
        stranded = sum(n for z, n in counts.items() if z not in zones)
        if not usable or (max(usable.values()) - min(usable.values()) <= 1 and not stranded):
            return
        moves = moves_to_balance(len(self.live()) - stranded, usable, zones)
        limit = self.group.max_size + max(1, self.group.max_size // 10)
        batch = min(moves, max(0, limit - len(self.live())))
        if batch <= 0:
            return
        self.log("rebalance", f"zones {usable} are unbalanced: launch {batch} before terminating (limit {limit} = max {self.group.max_size} + margin)")
        self.allowance += batch
        self.rebalance_batch = batch
        self.launch(batch, f"At {stamp(self.now)} an instance was launched to aid in balancing the group's zones.", "rebalance")

    def choose_victims(self, count: int, candidates: Optional[Sequence[Instance]] = None) -> List[Instance]:
        chosen: List[Instance] = []
        for _ in range(count):
            remaining = [i for i in self.live() if i not in chosen]
            pool = [i for i in (candidates if candidates is not None else remaining) if i in remaining and not i.protected and i.health == "Healthy"]
            if not pool:
                break
            counts = self.zone_counts(remaining)
            top = max(counts[i.zone] for i in pool)
            pool = [i for i in pool if counts[i.zone] == top]
            chosen.append(pick_victim(pool, self.group.termination_policies, self.group.version, self.now))
        return chosen

    def converge(self) -> None:
        live = self.live()
        want = self.group.desired + self.allowance
        if len(live) < want:
            self.launch(want - len(live))
        elif len(live) > want:
            before = len(live)
            for victim in self.choose_victims(before - want):
                prefix = self.cause + "  " if self.cause else ""
                self.terminate(victim, f"{prefix}At {stamp(self.now)} an instance was taken out of service in response to a difference between desired and actual capacity, shrinking the capacity from {before} to {want}.  At {stamp(self.now)} instance {victim.id} was selected for termination.", f"scale-in {before} -> {want}")
        if len(self.live()) == self.group.desired and not self.owed:
            self.cause = ""

    def settle_cooldown(self) -> None:
        if self.cooldown_pending is None:
            return
        if any(i.state in ("Pending", "Pending:Wait") + GOING for i in self.instances):
            return
        self.cooldown_until = self.now + self.cooldown_pending
        self.log("cooldown", f"scaling activity finished; simple scaling policies wait {self.cooldown_pending}s, until {clock(self.cooldown_until)}")
        self.cooldown_pending = None

    def datapoint(self, policy: Policy, raw: float) -> Optional[float]:
        if self.metric is None or self.metric.mode == "observed":
            return float(raw)
        serving = len(self.ready(self.effective_warmup(policy)))
        if serving == 0:
            return None
        return min(CPU_CEILING, round(raw / serving, 2))

    def evaluate_policies(self) -> None:
        if self.metric is None or not self.policies or self.now < self.metric.start or (self.now - self.metric.start) % self.metric.period:
            return
        index = (self.now - self.metric.start) // self.metric.period
        tracking = [p for p in self.policies if p.kind == "TargetTrackingScaling" and self.metric.raw(p.name, index) is not None]
        if tracking:
            self.track(tracking, index)
        for policy in self.policies:
            raw = self.metric.raw(policy.name, index)
            if policy.kind in ("StepScaling", "SimpleScaling") and raw is not None:
                self.alarm_policy(policy, raw, index)

    def note(self, policy: Policy, kind: str, text: str) -> None:
        if policy.note != text:
            policy.note = text
            self.log(kind, f"{policy.name}: {text}")

    def decide(self, policy: Policy, index: int, value: Optional[float], new: int, sentence: str, formula: str) -> None:
        old = self.group.desired
        if self.set_desired(new, sentence):
            policy.note = ""
            self.decisions.append({"t": self.now, "datapoint": index, "policy": policy.name, "metric": value, "from": old, "to": new, "formula": formula})
            self.log("policy", f"{policy.name}: {formula}")

    def track(self, tracking: List[Policy], index: int) -> None:
        out: List[Tuple[int, Policy, float, str]] = []
        inward: List[Tuple[int, Policy, float, str]] = []
        for policy in tracking:
            value = self.datapoint(policy, self.metric.raw(policy.name, index) or 0.0)
            policy.history.append(value)
            if value is None:
                self.note(policy, "alarm", "INSUFFICIENT_DATA: no instance has finished warming up, so there is no metric to act on")
                continue
            current = len(self.ready(self.effective_warmup(policy)))
            proposed = target_tracking_capacity(current, value, policy.target)
            formula = f"ceil({current} x {value:g} / {policy.target:g}) = {proposed}"
            if self.breached(policy.history, policy.high_points, lambda v: v > policy.target):
                out.append((proposed, policy, value, formula))
            elif not policy.disable_scale_in and self.breached(policy.history, policy.low_points, lambda v: v < policy.target):
                inward.append((proposed, policy, value, formula))
        if out:
            proposed, policy, value, formula = max(out, key=lambda o: o[0])
            new = min(self.group.max_size, proposed)
            alarm = f"TargetTracking-{self.group.name}-AlarmHigh-{policy.name}"
            if new > self.group.desired:
                self.decide(policy, index, value, new, f"a monitor alarm {alarm} in state ALARM triggered policy {policy.name}", f"{formula}, desired {self.group.desired} -> {new}")
            else:
                self.note(policy, "no-change", f"{formula}: desired {self.group.desired} already counts the instances still warming up")
            return
        voters = [p for p in tracking if not p.disable_scale_in]
        if inward and len(inward) == len(voters):
            proposed, policy, value, formula = max(inward, key=lambda o: o[0])
            warming = self.warming(max(self.effective_warmup(p) for p in voters))
            if warming:
                self.note(policy, "blocked", f"scale-in blocked: {len(warming)} instance(s) still launching or warming up")
                return
            new = max(self.group.min_size, proposed)
            alarm = f"TargetTracking-{self.group.name}-AlarmLow-{policy.name}"
            if new < self.group.desired:
                self.decide(policy, index, value, new, f"a monitor alarm {alarm} in state ALARM triggered policy {policy.name}", f"{formula}, desired {self.group.desired} -> {new}")
            else:
                self.note(policy, "no-change", f"{formula}: removing an instance would push the metric above {policy.target:g}")

    @staticmethod
    def breached(history: Sequence[Optional[float]], points: int, test: Callable[[float], bool]) -> bool:
        recent = list(history[-points:])
        return len(recent) == points and all(v is not None and test(v) for v in recent)

    def alarm_policy(self, policy: Policy, raw: float, index: int) -> None:
        alarm = policy.alarm
        if alarm is None:
            raise InputError(f"policy {policy.name} needs an alarm (Threshold, ComparisonOperator) in the scenario's alarms")
        value = self.datapoint(policy, raw)
        policy.history.append(value)
        state = "ALARM" if self.breached(policy.history, alarm.evaluation_periods, lambda v: COMPARE[alarm.comparison](v, alarm.threshold)) else "OK"
        if state != policy.state:
            policy.state = state
            self.log("alarm", f"{alarm.name} -> {state} (value {value}, threshold {alarm.threshold:g}, {alarm.comparison})")
        if state != "ALARM" or value is None:
            return
        scale_out = alarm.comparison.startswith("Greater")
        sentence = f"a monitor alarm {alarm.name} in state ALARM triggered policy {policy.name}"
        if policy.kind == "SimpleScaling":
            if self.in_cooldown():
                until = "the current activity finishes" if self.cooldown_pending is not None else clock(self.cooldown_until or 0)
                self.note(policy, "cooldown", f"in cooldown until {until}; alarm still in ALARM")
                return
            current = len([i for i in self.live() if i.state == "InService"])
            new = max(self.group.min_size, min(self.group.max_size, adjusted_capacity(policy.adjustment_type, current, policy.adjustment, policy.min_magnitude)))
            if new != self.group.desired:
                self.decide(policy, index, value, new, sentence, f"{policy.adjustment_type} {policy.adjustment:+g} on {current}, desired {self.group.desired} -> {new}")
                self.cooldown_pending = policy.cooldown if policy.cooldown is not None else self.group.default_cooldown
            return
        step = matching_step(policy.steps, alarm.threshold, value, scale_out)
        if step is None:
            self.note(policy, "no-change", f"value {value:g} falls in no step adjustment")
            return
        warmup = self.effective_warmup(policy)
        current = len(self.ready(warmup))
        if scale_out:
            proposed = adjusted_capacity(policy.adjustment_type, current, step.adjustment, policy.min_magnitude)
            new = min(self.group.max_size, proposed)
            formula = f"step {step.describe()} on current capacity {current} = {proposed}"
            if new > self.group.desired:
                self.decide(policy, index, value, new, sentence, f"{formula}, desired {self.group.desired} -> {new}")
            else:
                self.note(policy, "no-change", f"{formula}: desired {self.group.desired} already includes it")
            return
        warming = self.warming(warmup)
        if warming:
            self.note(policy, "blocked", f"scale-in blocked: {len(warming)} instance(s) still launching or warming up")
            return
        base = current + len([i for i in self.instances if i.state in GOING])
        proposed = adjusted_capacity(policy.adjustment_type, base, step.adjustment, policy.min_magnitude)
        new = max(self.group.min_size, proposed)
        formula = f"step {step.describe(False)} on current capacity {base} = {proposed}"
        if new < self.group.desired:
            self.decide(policy, index, value, new, sentence, f"{formula}, desired {self.group.desired} -> {new}")
        else:
            self.note(policy, "no-change", f"{formula}: nothing more to remove")

    def sample(self) -> None:
        total = len(self.live())
        serving = len([i for i in self.instances if i.state == "InService" and i.ec2_state == "running" and i.elb_ok])
        self.peak = max(self.peak, total)
        self.window_peak = max(self.window_peak, total)
        if serving >= self.group.desired:
            self.reached = True
        if self.reached:
            self.min_serving = serving if self.min_serving is None else min(self.min_serving, serving)
            self.window_min = serving if self.window_min is None else min(self.window_min, serving)

    def reconcile(self) -> None:
        self.advance_lifecycle()
        self.evaluate_policies()
        self.check_health()
        self.replace_unhealthy()
        self.step_refresh()
        self.rebalance()
        self.converge()
        self.settle_cooldown()
        self.sample()

    def checkpoint(self, label: str) -> None:
        live = self.live()
        self.checkpoints.append({
            "t": self.now,
            "after": label,
            "desired": self.group.desired,
            "instances": len(live),
            "in_service": len([i for i in live if i.state == "InService"]),
            "zones": self.zone_counts(),
            "min_serving": self.window_min,
            "peak": self.window_peak,
        })
        self.window_peak = 0
        self.window_min = None

    def apply(self, event: Dict[str, Any]) -> None:
        op = event.get("op", "")
        handlers: Dict[str, Callable[[], Any]] = {
            "set-desired-capacity": lambda: self.set_desired_capacity(int(event["DesiredCapacity"]), bool(event.get("HonorCooldown", False))),
            "update-auto-scaling-group": lambda: self.update_group(event.get("MinSize"), event.get("MaxSize"), event.get("DesiredCapacity"), health_check_type=event.get("HealthCheckType"), grace_period=event.get("HealthCheckGracePeriod"), version=event.get("LaunchTemplateVersion"), default_warmup=event.get("DefaultInstanceWarmup"), termination_policies=event.get("TerminationPolicies")),
            "set-instance-health": lambda: self.set_instance_health(event["InstanceId"], event["HealthStatus"], bool(event.get("ShouldRespectGracePeriod", False))),
            "terminate-instance-in-auto-scaling-group": lambda: self.terminate_instance(event["InstanceId"], bool(event.get("ShouldDecrementDesiredCapacity", False))),
            "set-instance-protection": lambda: self.set_protection(event["InstanceId"], bool(event.get("ProtectedFromScaleIn", True))),
            "complete-lifecycle-action": lambda: self.complete_lifecycle_action(event["InstanceId"], event.get("LifecycleActionResult", "CONTINUE")),
            "start-instance-refresh": lambda: self.start_instance_refresh(event.get("Version"), **refresh_preferences(event.get("Preferences") or {})),
            "stop-instance": lambda: self.stop_instance(event["InstanceId"]),
            "elb-health": lambda: self.set_elb_health(event["InstanceId"], bool(event.get("Healthy", False))),
            "zone-outage": lambda: self.zone_outage(event["Zone"]),
            "zone-recover": lambda: self.zone_recover(event["Zone"]),
        }
        if op not in handlers:
            raise InputError(f"unknown op {op!r} at t={event.get('at')} (one of {', '.join(sorted(handlers))})")
        self.log("op", f"{op} " + " ".join(f"{k}={v}" for k, v in event.items() if k not in ("op", "at")))
        try:
            handlers[op]()
        except KeyError as missing:
            raise InputError(f"op {op} at t={event.get('at')} is missing {missing}")
        except AsgError as err:
            self.rejected.append({"t": self.now, "op": op, "code": err.code, "message": err.message})
            self.log("error", f"{op}: An error occurred ({err.code}): {err.message}")

    def run(self, events: Sequence[Dict[str, Any]], until: int) -> None:
        queue = sorted(enumerate(events), key=lambda pair: (int(pair[1].get("at", 0)), pair[0]))
        label = "create"
        while True:
            due = [event for _, event in queue if int(event.get("at", 0)) <= self.now]
            if due:
                queue = [(n, event) for n, event in queue if int(event.get("at", 0)) > self.now]
                self.checkpoint(label)
                label = ", ".join(event["op"] for event in due)
                for event in due:
                    self.apply(event)
            self.reconcile()
            if self.now >= until and not queue:
                break
            self.now += self.tick
        self.checkpoint(label)

    def settle(self, limit: int = SETTLE_LIMIT) -> int:
        started = self.now
        quiet_ticks = 0
        while self.now - started < limit:
            self.reconcile()
            quiet_ticks = quiet_ticks + 1 if self.quiet() else 0
            if quiet_ticks >= 2:
                break
            self.now += self.tick
        return self.now - started

    def summary(self) -> Dict[str, Any]:
        live = self.live()
        versions: Dict[str, int] = {}
        for instance in live:
            versions[instance.version] = versions.get(instance.version, 0) + 1
        return {
            "desired": self.group.desired,
            "desired_history": [d for _, d in self.desired_history],
            "instances": len(live),
            "zones": self.zone_counts(),
            "versions": versions,
            "launched": len([i for i in self.instances]),
            "terminated": len([i for i in self.instances if i.state == "Terminated"]),
            "peak_instances": self.peak,
            "min_serving": self.min_serving,
            "serving": len([i for i in live if i.state == "InService" and i.ec2_state == "running" and i.elb_ok]),
            "refresh": None if self.refresh is None else {"status": self.refresh.status, "replaced": self.refresh.replaced, "reason": self.refresh.reason},
            "rejected": self.rejected,
            "decisions": self.decisions,
        }


def refresh_preferences(prefs: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "min_healthy": int(prefs.get("MinHealthyPercentage", 90)),
        "max_healthy": int(prefs.get("MaxHealthyPercentage", 100)),
        "warmup": prefs.get("InstanceWarmup"),
        "skip_matching": bool(prefs.get("SkipMatching", False)),
    }


def number(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    return float(value)


def group_from(doc: Dict[str, Any]) -> Group:
    template = doc.get("LaunchTemplate") or {}
    version = str(doc.get("LaunchTemplateVersion") or template.get("Version") or "1")
    if version.startswith("$"):
        versions = [str((i.get("LaunchTemplate") or {}).get("Version", "1")) for i in doc.get("Instances") or []]
        version = max(versions, key=version_key) if versions else "1"
    try:
        return Group(
            name=str(doc.get("AutoScalingGroupName", "web")),
            min_size=int(doc["MinSize"]),
            max_size=int(doc["MaxSize"]),
            desired=int(doc.get("DesiredCapacity", doc["MinSize"])),
            zones=list(doc.get("AvailabilityZones") or []),
            version=version,
            health_check_type=str(doc.get("HealthCheckType", "EC2")),
            grace_period=int(doc.get("HealthCheckGracePeriod", 0) or 0),
            default_cooldown=int(doc.get("DefaultCooldown", 300) or 300),
            default_warmup=None if doc.get("DefaultInstanceWarmup") is None else int(doc["DefaultInstanceWarmup"]),
            termination_policies=list(doc.get("TerminationPolicies") or ["Default"]),
            boot_seconds=int(doc.get("BootSeconds", 60)),
            drain_seconds=int(doc.get("DrainSeconds", 30)),
            broken_versions=[str(v) for v in doc.get("BrokenVersions") or []],
        )
    except KeyError as missing:
        raise InputError(f"the group needs {missing} (the describe-auto-scaling-groups field names)")
    except (TypeError, ValueError) as err:
        raise InputError(f"bad group field: {err}")


def policies_from(items: Sequence[Dict[str, Any]], alarms: Dict[str, Dict[str, Any]]) -> List[Policy]:
    policies: List[Policy] = []
    for item in items:
        name = str(item.get("PolicyName", ""))
        kind = str(item.get("PolicyType", "SimpleScaling"))
        warmup = item.get("EstimatedInstanceWarmup")
        policy = Policy(name=name, kind=kind, warmup=None if warmup is None else int(warmup))
        if kind == "TargetTrackingScaling":
            config = item.get("TargetTrackingConfiguration") or {}
            if "TargetValue" not in config:
                raise InputError(f"policy {name}: TargetTrackingConfiguration needs TargetValue")
            policy.target = float(config["TargetValue"])
            policy.disable_scale_in = bool(config.get("DisableScaleIn", False))
        elif kind in ("StepScaling", "SimpleScaling"):
            policy.adjustment_type = str(item.get("AdjustmentType", "ChangeInCapacity"))
            if policy.adjustment_type not in ADJUSTMENT_TYPES:
                raise InputError(f"policy {name}: unknown AdjustmentType {policy.adjustment_type}")
            policy.min_magnitude = None if item.get("MinAdjustmentMagnitude") is None else int(item["MinAdjustmentMagnitude"])
            policy.adjustment = float(item.get("ScalingAdjustment", 0) or 0)
            policy.cooldown = None if item.get("Cooldown") is None else int(item["Cooldown"])
            if kind == "StepScaling":
                policy.steps = [Step(number(s.get("MetricIntervalLowerBound")), number(s.get("MetricIntervalUpperBound")), float(s["ScalingAdjustment"])) for s in item.get("StepAdjustments") or []]
                validate_steps(policy.steps)
            spec = alarms.get(name)
            if spec is None:
                raise InputError(f"policy {name} is {kind}: add an alarm for it under \"alarms\" (Threshold, ComparisonOperator, EvaluationPeriods)")
            comparison = str(spec.get("ComparisonOperator", "GreaterThanThreshold"))
            if comparison not in COMPARE:
                raise InputError(f"alarm for {name}: unknown ComparisonOperator {comparison}")
            policy.alarm = Alarm(str(spec.get("AlarmName", f"{name}-alarm")), float(spec["Threshold"]), comparison, int(spec.get("EvaluationPeriods", 1)))
        else:
            raise InputError(f"policy {name}: unsupported PolicyType {kind} (TargetTrackingScaling, StepScaling, SimpleScaling)")
        policies.append(policy)
    return policies


def hooks_from(items: Sequence[Dict[str, Any]]) -> List[Hook]:
    hooks = []
    for item in items:
        transition = str(item.get("LifecycleTransition", LAUNCHING))
        if transition not in (LAUNCHING, TERMINATING):
            raise InputError(f"LifecycleTransition must be {LAUNCHING} or {TERMINATING}")
        timeout = int(item.get("HeartbeatTimeout", 3600))
        if not 30 <= timeout <= 7200:
            raise InputError(f"HeartbeatTimeout must be 30-7200 seconds, got {timeout}")
        hooks.append(Hook(str(item["LifecycleHookName"]), transition, timeout, str(item.get("DefaultResult", "ABANDON")), None if item.get("RespondAfter") is None else int(item["RespondAfter"]), str(item.get("RespondWith", "CONTINUE"))))
    return hooks


def build(doc: Dict[str, Any]) -> Tuple[Asg, List[Dict[str, Any]], int]:
    if "group" not in doc:
        raise InputError("a scenario needs a \"group\" object (describe-auto-scaling-groups field names)")
    group = group_from(doc["group"])
    policies = policies_from(doc.get("policies") or [], doc.get("alarms") or {})
    hooks = hooks_from(doc.get("hooks") or [])
    metric = None
    if doc.get("metric"):
        spec = doc["metric"]
        if spec.get("mode", "observed") not in ("observed", "demand"):
            raise InputError("metric.mode must be observed (values are the metric) or demand (values are total load in percent of one instance)")
        series = {str(name): [float(v) for v in values] for name, values in (spec.get("series") or {}).items()}
        metric = Metric([float(v) for v in spec.get("values") or []], int(spec.get("period", 60)), str(spec.get("mode", "observed")), int(spec.get("start", spec.get("period", 60))), series)
    asg = Asg(group, policies, hooks, metric, int(doc.get("tick", 10)))
    events = list(doc.get("events") or [])
    until = int(doc.get("until", max([int(e.get("at", 0)) for e in events] + [0]) + 900))
    return asg, events, until


def load_json(path: str) -> Any:
    try:
        if path == "-":
            return json.load(sys.stdin)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as err:
        raise InputError(f"{path}: not valid JSON ({err})")
    except OSError as err:
        raise InputError(f"{path}: {err.strerror}")


def scenario_names() -> List[str]:
    if not os.path.isdir(SCENARIO_DIR):
        return []
    return sorted(name[:-5] for name in os.listdir(SCENARIO_DIR) if name.endswith(".json"))


def load_scenario(name: str) -> Dict[str, Any]:
    if name.endswith(".json") or os.sep in name or name == "-":
        return load_json(name)
    path = os.path.join(SCENARIO_DIR, f"{name}.json")
    if not os.path.exists(path):
        raise InputError(f"unknown scenario {name!r}; built-in: {', '.join(scenario_names())}, or pass a .json file")
    return load_json(path)


def merge_policies(own: Sequence[Dict[str, Any]], described: Any) -> List[Dict[str, Any]]:
    items = described.get("ScalingPolicies", []) if isinstance(described, dict) else list(described)
    by_name = {str(item.get("PolicyName")): item for item in items}
    if not own:
        return list(items)
    names = [str(p.get("PolicyName")) for p in own]
    missing = [n for n in names if n not in by_name]
    if missing:
        raise InputError(f"the describe-policies output has no policy named {', '.join(missing)} (it has {', '.join(sorted(by_name)) or 'none'})")
    return [by_name[n] for n in names]


def run_scenario(doc: Dict[str, Any], policies_doc: Optional[Dict[str, Any]] = None, health_check_type: Optional[str] = None) -> Tuple[Asg, Dict[str, Any]]:
    doc = json.loads(json.dumps(doc))
    if policies_doc is not None:
        doc["policies"] = merge_policies(doc.get("policies") or [], policies_doc)
    if health_check_type:
        doc["group"]["HealthCheckType"] = health_check_type
    asg, events, until = build(doc)
    asg.run(events, until)
    return asg, asg.summary()


def instance_from(item: Dict[str, Any], group: Group) -> Instance:
    template = item.get("LaunchTemplate") or {}
    state = str(item.get("LifecycleState", "InService"))
    instance = Instance(str(item["InstanceId"]), str(item.get("AvailabilityZone", group.zones[0])), str(template.get("Version", group.version)), 0, state=state, health=str(item.get("HealthStatus", "Healthy")), ec2_state="running" if state in LIVE else "terminated", protected=bool(item.get("ProtectedFromScaleIn", False)), in_service_at=-10 ** 6)
    if instance.health == "Unhealthy":
        instance.unhealthy_reason = "a health check (HealthStatus Unhealthy in the describe output)"
    return instance


def plan(doc: Dict[str, Any], name: Optional[str] = None) -> Dict[str, Any]:
    groups = doc.get("AutoScalingGroups") if isinstance(doc, dict) and "AutoScalingGroups" in doc else [doc]
    if name:
        groups = [g for g in groups if g.get("AutoScalingGroupName") == name]
    if not groups:
        raise InputError(f"no Auto Scaling group {name or ''} in the describe output")
    item = groups[0]
    group = group_from(item)
    if group.desired > group.max_size or group.desired < group.min_size:
        note = f"DesiredCapacity {group.desired} is outside min {group.min_size} / max {group.max_size}: AWS rejects that with a ValidationError, so this state came from an emulator or a race"
        group.desired = min(max(group.desired, group.min_size), group.max_size)
    else:
        note = ""
    asg = Asg(group, create=False)
    asg.instances = [instance_from(i, group) for i in item.get("Instances") or []]
    actual = asg.zone_counts()
    live_before = len(asg.live())
    start = len(asg.activities)
    asg.reconcile()
    first = [{"action": "launch" if a["Description"].startswith("Launching") else "terminate", "instance": a["Description"].rsplit(" ", 1)[-1], "zone": json.loads(a["Details"]).get("Availability Zone", ""), "cause": a["Cause"]} for a in asg.activities[start:] if a["StatusCode"] != "Failed"]
    asg.now += asg.tick
    took = asg.settle()
    settled = asg.zone_counts()
    zones = group.zones
    return {
        "group": group.name,
        "desired": group.desired,
        "min": group.min_size,
        "max": group.max_size,
        "instances": live_before,
        "zones": actual,
        "unhealthy": sorted(i["InstanceId"] for i in item.get("Instances") or [] if i.get("HealthStatus") == "Unhealthy"),
        "first_pass": first,
        "balanced_now": max(actual.values()) - min(actual.values()) <= 1 if actual else True,
        "rebalance_moves": moves_to_balance(group.desired, {z: actual.get(z, 0) for z in zones}, zones),
        "settled": {"instances": len(asg.live()), "zones": settled, "seconds": took},
        "note": note,
        "activities": asg.activities,
    }


def format_event(event: Dict[str, Any]) -> str:
    return f"{clock(event['t'])}  {event['kind']:<11} {event['message']}"


def zones_text(counts: Dict[str, int]) -> str:
    return " ".join(f"{zone[-2:] if len(zone) > 2 else zone}={n}" for zone, n in counts.items())


def cmd_list(args: argparse.Namespace) -> int:
    rows = []
    for name in scenario_names():
        doc = load_json(os.path.join(SCENARIO_DIR, f"{name}.json"))
        rows.append({"name": name, "description": doc.get("description", "")})
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        width = max([len(r["name"]) for r in rows] + [8])
        for row in rows:
            print(f"{row['name']:<{width}}  {row['description']}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    doc = load_scenario(args.scenario)
    policies_doc = load_json(args.policies) if args.policies else None
    asg, summary = run_scenario(doc, policies_doc, args.health_check_type)
    if args.json:
        print(json.dumps({"scenario": args.scenario, "description": doc.get("description", ""), "summary": summary, "checkpoints": asg.checkpoints, "events": asg.events, "activities": asg.activities}, indent=2))
        return 1 if summary["rejected"] else 0
    print(f"scenario {args.scenario}: {doc.get('description', '')}")
    for event in asg.events:
        print(format_event(event))
    print("\ncheckpoints")
    print(f"  {'time':<8}  {'after':<42} {'desired':>7} {'live':>5} {'min-serving':>11} {'peak':>5}  zones")
    for c in asg.checkpoints:
        serving = "-" if c["min_serving"] is None else str(c["min_serving"])
        print(f"  {clock(c['t']):<8}  {c['after'][:42]:<42} {c['desired']:>7} {c['instances']:>5} {serving:>11} {c['peak']:>5}  {zones_text(c['zones'])}")
    print("\nsummary")
    for key in ("desired_history", "instances", "zones", "versions", "peak_instances", "min_serving", "serving", "refresh"):
        print(f"  {key}: {json.dumps(summary[key])}")
    for err in summary["rejected"]:
        print(f"  rejected at {clock(err['t'])}: {err['op']}: {err['code']}: {err['message']}")
    return 1 if summary["rejected"] else 0


def cmd_plan(args: argparse.Namespace) -> int:
    result = plan(load_json(args.describe), args.name)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"group {result['group']}: desired {result['desired']} (min {result['min']}, max {result['max']}), {result['instances']} instance(s): {zones_text(result['zones'])}")
    if result["note"]:
        print(f"  note: {result['note']}")
    if result["unhealthy"]:
        print(f"  unhealthy: {', '.join(result['unhealthy'])}")
    print("first reconcile pass:")
    for action in result["first_pass"] or [{"action": "nothing", "instance": "", "zone": "", "cause": "desired and actual already match"}]:
        print(f"  {action['action']:<9} {action['instance']:<21} {action['zone']:<11} {action['cause']}")
    if not result["balanced_now"]:
        print(f"zones are unbalanced: AZRebalance needs {result['rebalance_moves']} launch(es) in the emptier zones, each followed by a termination in the fullest")
    settled = result["settled"]
    print(f"settled after {settled['seconds']}s: {settled['instances']} instance(s): {zones_text(settled['zones'])}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="miniasg", description="An Auto Scaling group as a reconciler: capacity, zones, health, scaling policies, lifecycle hooks and instance refresh on a virtual clock.")
    sub = parser.add_subparsers(dest="command")
    listing = sub.add_parser("list", help="list the built-in scenarios")
    listing.add_argument("--json", action="store_true")
    run = sub.add_parser("run", help="run a scenario (built-in name or .json file) and print its event log")
    run.add_argument("scenario")
    run.add_argument("--policies", help="aws autoscaling describe-policies output to use instead of the scenario's policies")
    run.add_argument("--health-check-type", choices=["EC2", "ELB"], help="override the group's HealthCheckType")
    run.add_argument("--json", action="store_true")
    planner = sub.add_parser("plan", help="read describe-auto-scaling-groups output and say what the group will do next")
    planner.add_argument("--describe", required=True, help="aws autoscaling describe-auto-scaling-groups output, or - for stdin")
    planner.add_argument("--name", help="the group to plan when the output holds several")
    planner.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return int(exit_.code or 0)
    if args.command is None:
        parser.print_help()
        return 2
    try:
        return {"list": cmd_list, "run": cmd_run, "plan": cmd_plan}[args.command](args)
    except InputError as err:
        print(f"miniasg: {err}", file=sys.stderr)
        return 2
    except AsgError as err:
        print(f"miniasg: An error occurred ({err.code}): {err.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
