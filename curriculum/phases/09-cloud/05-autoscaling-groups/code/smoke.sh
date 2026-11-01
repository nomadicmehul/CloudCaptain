#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

LESSON_DIR="$PWD"
MINIASG=(python3 "$LESSON_DIR/code/main.py")
FIX="$LESSON_DIR/code/fixtures"
MOTO_VERSION="5.2.5"
GROUP="cc-autoscaling-groups"
WORK="$(mktemp -d)"
MOTO_PID=""
fail() {
  echo "SMOKE FAIL: $*" >&2
  if [[ -f "$WORK/moto.log" ]]; then echo "--- moto_server log (last 20 lines)" >&2; tail -20 "$WORK/moto.log" >&2; fi
  exit 1
}
cleanup() {
  if [[ -n "$MOTO_PID" ]]; then
    kill "$MOTO_PID" 2>/dev/null || true
    wait "$MOTO_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT
mini() {
  local out="$1"
  shift
  local code=0
  "${MINIASG[@]}" "$@" --json > "$out" 2> "$out.err" || code=$?
  echo "$code"
}

echo "▸ miniasg: scaling policies replayed against the decisions the AWS user guide documents"
for s in step-scaling step-warmup target-tracking simple-cooldown; do
  CODE="$(mini "$WORK/$s.json" run "$s")"
  [[ "$CODE" == "0" ]] || fail "miniasg run $s exited $CODE: $(cat "$WORK/$s.json.err")"
done
python3 - "$WORK" "$FIX/expected-decisions.json" <<'PY' || fail "policy decisions differ from the documented ones (see above)"
import json, os, sys
work, expected = sys.argv[1], json.load(open(sys.argv[2]))
def summary(name):
    return json.load(open(os.path.join(work, name + ".json")))["summary"]
for name in ("step-scaling", "step-warmup", "target-tracking"):
    got = [[d["datapoint"], d["from"], d["to"]] for d in summary(name)["decisions"]]
    assert got == expected[name], (name, got, expected[name])
    print(f"  {name:<16} " + ", ".join(f"datapoint {i}: {a} -> {b}" for i, a, b in got))
times = [d["t"] for d in summary("simple-cooldown")["decisions"]]
assert times == expected["simple-cooldown-times"], times
print(f"  {'simple-cooldown':<16} scale-outs at t={times} (60 s launch + 300 s cooldown apart), then MaxSize")
events = json.load(open(os.path.join(work, "step-warmup.json")))["events"]
assert any(e["kind"] == "blocked" for e in events), "scale-in during warmup should be blocked"
events = json.load(open(os.path.join(work, "target-tracking.json")))["events"]
assert any(e["kind"] == "no-change" and "would push the metric above" in e["message"] for e in events), "target tracking should refuse a scale-in that overshoots the target"
PY

echo "▸ miniasg: zones, health checks, lifecycle hooks and instance refresh"
for s in spread self-heal az-outage lifecycle-hook refresh refresh-surge refresh-broken; do
  CODE="$(mini "$WORK/$s.json" run "$s")"
  [[ "$CODE" == "0" ]] || fail "miniasg run $s exited $CODE: $(cat "$WORK/$s.json.err")"
done
CODE="$(mini "$WORK/refresh-broken-ec2.json" run refresh-broken --health-check-type EC2)"
[[ "$CODE" == "0" ]] || fail "miniasg run refresh-broken --health-check-type EC2 exited $CODE"
python3 - "$WORK" <<'PY' || fail "a capacity, health or refresh scenario changed behaviour (see above)"
import json, os, sys
work = sys.argv[1]
def load(name):
    return json.load(open(os.path.join(work, name + ".json")))
spread = load("spread")["summary"]
assert spread["zones"] == {"us-east-1a": 0, "us-east-1b": 1, "us-east-1c": 1} and spread["versions"] == {"1": 1, "2": 1}, spread
heal = load("self-heal")
marked = {e["message"].split()[0]: e["t"] for e in heal["events"] if e["kind"] == "unhealthy"}
assert sorted(marked.values()) == [300, 480, 700, 900], marked
outage = load("az-outage")
recovered = [c for c in outage["checkpoints"] if c["after"] == "zone-recover"][0]
assert recovered["zones"] == {"us-east-1a": 2, "us-east-1b": 2, "us-east-1c": 2} and recovered["min_serving"] == 6 and recovered["peak"] == 7, recovered
hook = load("lifecycle-hook")
assert [a["StatusCode"] for a in hook["activities"]].count("Cancelled") == 1, "one launch abandoned by the hook timeout"
assert any("Terminating:Wait" in e["message"] for e in hook["events"]), "the terminate hook should hold the instance"
refresh, surge, broken, ec2 = (load(n)["summary"] for n in ("refresh", "refresh-surge", "refresh-broken", "refresh-broken-ec2"))
assert refresh["refresh"]["status"] == "Successful" and (refresh["min_serving"], refresh["peak_instances"]) == (3, 4), refresh
assert surge["refresh"]["status"] == "Successful" and (surge["min_serving"], surge["peak_instances"]) == (4, 6), surge
assert broken["refresh"]["status"] == "Failed" and broken["min_serving"] == 3 and broken["versions"] == {"1": 4}, broken
assert ec2["refresh"]["status"] == "Successful" and ec2["serving"] == 0, ec2
print("  spread 0/1/1 after scale-in; unhealthy at t=300 (stopped), 480 (ELB after 120 s grace), 700, 900")
print("  zone outage then recovery: 2/2/2 again, never below 6 serving, peak 7 during AZRebalance")
print("  refresh: MinHealthy 90 keeps 3 of 4 serving; 100/150 peaks at 6; a broken version fails under ELB checks (3 serving) and 'succeeds' under EC2 checks with 0 serving")
PY

echo "▸ install moto[server]==$MOTO_VERSION into a venv and start moto_server"
if ! python3 -m venv "$WORK/venv" > "$WORK/venv.log" 2>&1; then
  echo "  (python3 -m venv not available; moto comparison skipped)"
  echo "SMOKE OK: cloud/autoscaling-groups (miniasg only)"
  exit 0
fi
PACKAGES=("moto[server]==$MOTO_VERSION")
if command -v aws >/dev/null; then AWS_BIN="$(command -v aws)"; else PACKAGES+=("awscli"); AWS_BIN="$WORK/venv/bin/aws"; fi
"$WORK/venv/bin/pip" install -q --disable-pip-version-check "${PACKAGES[@]}" > "$WORK/pip.log" 2>&1 || fail "pip install ${PACKAGES[*]}: $(tail -20 "$WORK/pip.log")"
"$WORK/venv/bin/python" -c 'import moto; print("  moto", moto.__version__)'
"$AWS_BIN" --version 2>&1 | sed 's/^/  /'
PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
"$WORK/venv/bin/moto_server" -H 127.0.0.1 -p "$PORT" > "$WORK/moto.log" 2>&1 &
MOTO_PID=$!
ENDPOINT="http://127.0.0.1:$PORT"
for _ in $(seq 1 150); do curl -sf -o /dev/null "$ENDPOINT/moto-api/data.json" && break; sleep 0.2; done
curl -sf -o /dev/null "$ENDPOINT/moto-api/data.json" || fail "moto_server did not answer on $ENDPOINT"

touch "$WORK/aws-config" "$WORK/aws-credentials"
export AWS_CONFIG_FILE="$WORK/aws-config" AWS_SHARED_CREDENTIALS_FILE="$WORK/aws-credentials"
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1 AWS_PAGER="" AWS_EC2_METADATA_DISABLED=true
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required AWS_RESPONSE_CHECKSUM_VALIDATION=when_required
unset AWS_PROFILE AWS_SESSION_TOKEN 2>/dev/null || true
AWSCLI=("$AWS_BIN" --endpoint-url "$ENDPOINT")
run() { "$@" > "$WORK/last.out" 2> "$WORK/last.err" || fail "$*: $(cat "$WORK/last.out" "$WORK/last.err")"; }
describe() { run "${AWSCLI[@]}" autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$GROUP"; cp "$WORK/last.out" "$WORK/$1.json"; }
member() {
  python3 - "$WORK/$1.json" "$2" <<'PY'
import json, sys
group = json.load(open(sys.argv[1]))["AutoScalingGroups"][0]
print(sorted(i["InstanceId"] for i in group["Instances"])[int(sys.argv[2])])
PY
}

cat > "$WORK/compare.py" <<'PY'
import json, sys
mirror, index, moto_file = json.load(open(sys.argv[1])), int(sys.argv[2]), sys.argv[3]
group = json.load(open(moto_file))["AutoScalingGroups"][0]
live = [i for i in group["Instances"] if i["LifecycleState"] in ("Pending", "Pending:Wait", "InService")]
zones = {}
for i in live:
    zones[i["AvailabilityZone"]] = zones.get(i["AvailabilityZone"], 0) + 1
point = mirror["checkpoints"][index]
mine = {"desired": point["desired"], "instances": point["instances"]}
theirs = {"desired": group["DesiredCapacity"], "instances": len(live)}
assert mine == theirs, f"after {point['after']}: miniasg {mine}, moto {theirs}"
spread = {z: n for z, n in point["zones"].items() if n}
print(f"  {point['after']:<41} desired {theirs['desired']}, {theirs['instances']} instance(s) in both | zones miniasg {spread} moto {zones}")
PY

echo "▸ the same operations in miniasg first: its prediction for each step"
cat > "$WORK/mirror-ops.json" <<EOF
{
  "description": "the operations the smoke runs against moto",
  "group": {"AutoScalingGroupName": "$GROUP", "MinSize": 1, "MaxSize": 6, "DesiredCapacity": 3, "AvailabilityZones": ["us-east-1a", "us-east-1b", "us-east-1c"], "HealthCheckGracePeriod": 60},
  "events": [
    {"at": 300, "op": "set-desired-capacity", "DesiredCapacity": 5},
    {"at": 600, "op": "set-desired-capacity", "DesiredCapacity": 9},
    {"at": 900, "op": "set-instance-health", "InstanceId": "i-00000000000000004", "HealthStatus": "Unhealthy"},
    {"at": 1200, "op": "terminate-instance-in-auto-scaling-group", "InstanceId": "i-00000000000000001", "ShouldDecrementDesiredCapacity": true},
    {"at": 1500, "op": "update-auto-scaling-group", "MaxSize": 3},
    {"at": 1800, "op": "stop-instance", "InstanceId": "i-00000000000000003"}
  ],
  "until": 2400
}
EOF
CODE="$(mini "$WORK/mirror.json" run "$WORK/mirror-ops.json")"
[[ "$CODE" == "1" ]] || fail "miniasg should reject DesiredCapacity 9 above MaxSize 6 and exit 1, got $CODE: $(cat "$WORK/mirror.json.err")"
python3 - "$WORK/mirror.json" <<'PY' || fail "mirror run: $(head -c 2000 "$WORK/mirror.json")"
import json, sys
doc = json.load(open(sys.argv[1]))
rejected = doc["summary"]["rejected"]
assert len(rejected) == 1 and rejected[0]["code"] == "ValidationError" and "above max value 6" in rejected[0]["message"], rejected
print("  " + " -> ".join(f"{c['after']}: {c['instances']}" for c in doc["checkpoints"]))
print(f"  rejected: {rejected[0]['code']}: {rejected[0]['message']}")
PY

echo "▸ Use It: a VPC with one subnet per zone, a launch template and an Auto Scaling group in moto"
run "${AWSCLI[@]}" ec2 create-vpc --cidr-block 10.40.0.0/16 --query Vpc.VpcId --output text
VPC="$(cat "$WORK/last.out")"
SUBNETS=""
n=1
for zone in us-east-1a us-east-1b us-east-1c; do
  run "${AWSCLI[@]}" ec2 create-subnet --vpc-id "$VPC" --cidr-block "10.40.$n.0/24" --availability-zone "$zone" --query Subnet.SubnetId --output text
  SUBNETS="$SUBNETS,$(cat "$WORK/last.out")"
  n=$((n + 1))
done
SUBNETS="${SUBNETS#,}"
run "${AWSCLI[@]}" ec2 describe-images --owners amazon --query 'Images[0].ImageId' --output text
AMI="$(cat "$WORK/last.out")"
[[ "$AMI" == ami-* ]] || fail "moto returned no AMI: $AMI"
run "${AWSCLI[@]}" ec2 create-launch-template --launch-template-name "$GROUP" --launch-template-data "{\"ImageId\":\"$AMI\",\"InstanceType\":\"t3.micro\"}"
run "${AWSCLI[@]}" autoscaling create-auto-scaling-group --auto-scaling-group-name "$GROUP" \
  --launch-template "LaunchTemplateName=$GROUP,Version=\$Latest" --min-size 1 --max-size 6 --desired-capacity 3 \
  --vpc-zone-identifier "$SUBNETS" --health-check-type EC2 --health-check-grace-period 60
describe m0
python3 "$WORK/compare.py" "$WORK/mirror.json" 0 "$WORK/m0.json" || fail "create: counts differ"

echo "▸ AZ spread: miniasg plan reads moto's describe output and says what AWS would do with it"
"${MINIASG[@]}" plan --describe "$WORK/m0.json" --json > "$WORK/plan0.json" || fail "miniasg plan: $(cat "$WORK/plan0.json")"
python3 - "$WORK/plan0.json" <<'PY' || fail "plan on moto's group: $(head -c 1500 "$WORK/plan0.json")"
import json, sys
p = json.load(open(sys.argv[1]))
zones = ["us-east-1a", "us-east-1b", "us-east-1c"]
actual = [p["zones"].get(z, 0) for z in zones]
settled = [p["settled"]["zones"][z] for z in zones]
assert p["settled"]["instances"] == p["desired"] == 3, p["settled"]
assert max(settled) - min(settled) <= 1, settled
base, extra = divmod(3, 3)
ranked = sorted(range(3), key=lambda k: (-actual[k], k))
target = {k: base + (1 if r < extra else 0) for r, k in enumerate(ranked)}
moves = sum(max(0, target[k] - actual[k]) for k in range(3))
assert p["rebalance_moves"] == moves, (p["rebalance_moves"], moves)
if p["balanced_now"]:
    print(f"  moto spread {actual} is balanced; miniasg agrees")
else:
    print(f"  moto placed {actual} across {zones} (it launches every instance into the first subnet);")
    print(f"  AWS launches into the zone with the fewest instances; miniasg plans {moves} AZRebalance launch(es) and settles at {settled}")
PY

echo "▸ set-desired-capacity 5: both launch two; miniasg puts them in the two emptiest zones"
run "${AWSCLI[@]}" autoscaling set-desired-capacity --auto-scaling-group-name "$GROUP" --desired-capacity 5
describe m1
python3 "$WORK/compare.py" "$WORK/mirror.json" 1 "$WORK/m1.json" || fail "set-desired-capacity 5: counts differ"

echo "▸ set-desired-capacity 9 above MaxSize 6: AWS and miniasg answer ValidationError"
RC=0
"${AWSCLI[@]}" autoscaling set-desired-capacity --auto-scaling-group-name "$GROUP" --desired-capacity 9 > "$WORK/nine.out" 2>&1 || RC=$?
describe m2a
NINE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["AutoScalingGroups"][0]["DesiredCapacity"])' "$WORK/m2a.json")"
if [[ "$RC" != "0" ]]; then
  grep -q "ValidationError" "$WORK/nine.out" || fail "moto refused desired 9 without a ValidationError: $(cat "$WORK/nine.out")"
  echo "  moto refused it too: $(grep -o 'ValidationError.*' "$WORK/nine.out" | head -c 160)"
else
  "${MINIASG[@]}" plan --describe "$WORK/m2a.json" --json > "$WORK/plan9.json" || fail "miniasg plan on desired 9"
  grep -q "ValidationError" "$WORK/plan9.json" || fail "miniasg plan should flag desired $NINE above max: $(head -c 800 "$WORK/plan9.json")"
  echo "  (moto $MOTO_VERSION accepted it: desired is now $NINE, above MaxSize 6; miniasg plan flags the state as one AWS rejects)"
  run "${AWSCLI[@]}" autoscaling set-desired-capacity --auto-scaling-group-name "$GROUP" --desired-capacity 5
fi
describe m2
python3 "$WORK/compare.py" "$WORK/mirror.json" 2 "$WORK/m2.json" || fail "after the rejected change: counts differ"

echo "▸ set-instance-health Unhealthy: miniasg plans the replacement; applying it to moto gives the same group"
VICTIM="$(member m2 0)"
run "${AWSCLI[@]}" autoscaling set-instance-health --instance-id "$VICTIM" --health-status Unhealthy
describe m3a
"${MINIASG[@]}" plan --describe "$WORK/m3a.json" --json > "$WORK/plan3.json" || fail "miniasg plan after set-instance-health"
STILL="$(python3 - "$WORK/m3a.json" "$WORK/plan3.json" "$VICTIM" <<'PY'
import json, sys
group = json.load(open(sys.argv[1]))["AutoScalingGroups"][0]
plan, victim = json.load(open(sys.argv[2])), sys.argv[3]
still = [i for i in group["Instances"] if i["InstanceId"] == victim]
if still:
    assert still[0]["HealthStatus"] == "Unhealthy", still
    actions = [(a["action"], a["instance"]) for a in plan["first_pass"]]
    assert actions[0] == ("terminate", victim) and [a for a, _ in actions].count("launch") == 1, actions
    assert plan["settled"]["instances"] == 5, plan["settled"]
    print("yes")
else:
    print("no")
PY
)" || fail "plan after set-instance-health: $(head -c 1500 "$WORK/plan3.json")"
if [[ "$STILL" == "yes" ]]; then
  echo "  (moto marks $VICTIM Unhealthy and keeps it in service; AWS's ReplaceUnhealthy process terminates and replaces it)"
  echo "  miniasg first pass: terminate $VICTIM, launch 1; applying that with terminate-instance-in-auto-scaling-group --no-should-decrement-desired-capacity"
  run "${AWSCLI[@]}" autoscaling terminate-instance-in-auto-scaling-group --instance-id "$VICTIM" --no-should-decrement-desired-capacity
  grep -q "Terminating EC2 instance: $VICTIM" "$WORK/last.out" || fail "terminate activity: $(cat "$WORK/last.out")"
else
  echo "  moto replaced $VICTIM itself"
fi
describe m3
python3 - "$WORK/m3.json" "$VICTIM" <<'PY' || fail "$VICTIM should be gone and replaced: $(head -c 1500 "$WORK/m3.json")"
import json, sys
group = json.load(open(sys.argv[1]))["AutoScalingGroups"][0]
ids = [i["InstanceId"] for i in group["Instances"]]
assert sys.argv[2] not in ids and len(ids) == 5 and all(i["HealthStatus"] == "Healthy" for i in group["Instances"]), group["Instances"]
PY
python3 "$WORK/compare.py" "$WORK/mirror.json" 3 "$WORK/m3.json" || fail "after the unhealthy replacement: counts differ"

echo "▸ terminate-instance-in-auto-scaling-group --should-decrement-desired-capacity: desired 4, no replacement"
run "${AWSCLI[@]}" autoscaling terminate-instance-in-auto-scaling-group --instance-id "$(member m3 0)" --should-decrement-desired-capacity
grep -q "shrinking the capacity from 5 to 4" "$WORK/last.out" || echo "  (moto's activity cause: $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["Activity"]["Cause"])' "$WORK/last.out"))"
describe m4
python3 "$WORK/compare.py" "$WORK/mirror.json" 4 "$WORK/m4.json" || fail "terminate with decrement: counts differ"

echo "▸ update-auto-scaling-group --max-size 3: the implicit SetDesiredCapacity lowers desired to the new max"
run "${AWSCLI[@]}" autoscaling update-auto-scaling-group --auto-scaling-group-name "$GROUP" --max-size 3
describe m5
python3 "$WORK/compare.py" "$WORK/mirror.json" 5 "$WORK/m5.json" || fail "update max-size: counts differ"

echo "▸ ec2 terminate-instances on a member, outside the group: the group launches a replacement"
GONE="$(member m5 0)"
run "${AWSCLI[@]}" ec2 terminate-instances --instance-ids "$GONE"
describe m6
python3 - "$WORK/m6.json" "$GONE" <<'PY' || fail "$GONE should have been replaced: $(head -c 1500 "$WORK/m6.json")"
import json, sys
group = json.load(open(sys.argv[1]))["AutoScalingGroups"][0]
assert sys.argv[2] not in [i["InstanceId"] for i in group["Instances"]], group["Instances"]
PY
python3 "$WORK/compare.py" "$WORK/mirror.json" 6 "$WORK/m6.json" || fail "replacement after ec2 terminate-instances: counts differ"

echo "▸ describe-scaling-activities: moto's list next to the activities miniasg recorded for the same operations"
run "${AWSCLI[@]}" autoscaling describe-scaling-activities --auto-scaling-group-name "$GROUP"
cp "$WORK/last.out" "$WORK/activities.json"
python3 - "$WORK/activities.json" "$WORK/mirror.json" <<'PY' || fail "activities"
import json, sys
moto = json.load(open(sys.argv[1]))["Activities"]
mine = json.load(open(sys.argv[2]))["activities"]
causes = " ".join(a["Cause"] for a in mine)
for sentence in ("a user request created an AutoScalingGroup changing the desired capacity from 0 to 3.",
                 "a user request explicitly set group desired capacity changing the desired capacity from 3 to 5.",
                 "an instance was taken out of service in response to a user health-check.",
                 "was taken out of service in response to a user request, shrinking the capacity from 5 to 4.",
                 "an instance was taken out of service in response to an EC2 health check indicating it has been terminated or stopped."):
    assert sentence in causes, sentence
print(f"  moto returned {len(moto)} activit{'y' if len(moto) == 1 else 'ies'}; miniasg recorded {len(mine)}, for example:")
print("    " + [a["Cause"] for a in mine if "from 3 to 5" in a["Cause"]][0])
PY

echo "▸ put-scaling-policy in moto, then replay the documented metric series through the policies moto returns"
run "${AWSCLI[@]}" autoscaling update-auto-scaling-group --auto-scaling-group-name "$GROUP" --max-size 20
run "${AWSCLI[@]}" autoscaling put-scaling-policy --auto-scaling-group-name "$GROUP" --policy-name cpu50 --policy-type TargetTrackingScaling \
  --estimated-instance-warmup 300 --target-tracking-configuration '{"PredefinedMetricSpecification":{"PredefinedMetricType":"ASGAverageCPUUtilization"},"TargetValue":50.0}'
cp "$WORK/last.out" "$WORK/put-tt.out"
run "${AWSCLI[@]}" autoscaling put-scaling-policy --auto-scaling-group-name "$GROUP" --policy-name step-out --policy-type StepScaling \
  --adjustment-type PercentChangeInCapacity --estimated-instance-warmup 300 \
  --step-adjustments MetricIntervalLowerBound=0,MetricIntervalUpperBound=10,ScalingAdjustment=0 MetricIntervalLowerBound=10,MetricIntervalUpperBound=20,ScalingAdjustment=10 MetricIntervalLowerBound=20,ScalingAdjustment=30
run "${AWSCLI[@]}" autoscaling put-scaling-policy --auto-scaling-group-name "$GROUP" --policy-name step-in --policy-type StepScaling \
  --adjustment-type PercentChangeInCapacity --estimated-instance-warmup 300 \
  --step-adjustments MetricIntervalLowerBound=-10,MetricIntervalUpperBound=0,ScalingAdjustment=0 MetricIntervalLowerBound=-20,MetricIntervalUpperBound=-10,ScalingAdjustment=-10 MetricIntervalUpperBound=-20,ScalingAdjustment=-30
run "${AWSCLI[@]}" autoscaling put-scaling-policy --auto-scaling-group-name "$GROUP" --policy-name add2 --policy-type SimpleScaling \
  --adjustment-type ChangeInCapacity --scaling-adjustment 2 --cooldown 300
run "${AWSCLI[@]}" autoscaling describe-policies --auto-scaling-group-name "$GROUP"
cp "$WORK/last.out" "$WORK/policies.json"
for s in step-scaling target-tracking simple-cooldown; do
  CODE="$(mini "$WORK/replay-$s.json" run "$s" --policies "$WORK/policies.json")"
  [[ "$CODE" == "0" ]] || fail "miniasg run $s --policies (moto's describe-policies) exited $CODE: $(cat "$WORK/replay-$s.json.err")"
done
python3 - "$WORK" "$FIX/expected-decisions.json" <<'PY' || fail "replay with moto's policy definitions"
import json, os, sys
work, expected = sys.argv[1], json.load(open(sys.argv[2]))
def summary(name):
    return json.load(open(os.path.join(work, f"replay-{name}.json")))["summary"]
for name in ("step-scaling", "target-tracking"):
    got = [[d["datapoint"], d["from"], d["to"]] for d in summary(name)["decisions"]]
    assert got == expected[name], (name, got)
assert [d["t"] for d in summary("simple-cooldown")["decisions"]] == expected["simple-cooldown-times"]
policies = {p["PolicyName"]: p for p in json.load(open(os.path.join(work, "policies.json")))["ScalingPolicies"]}
print(f"  moto returned {sorted(policies)}; with those definitions miniasg reproduces every documented decision")
put = open(os.path.join(work, "put-tt.out")).read().strip()
try:
    alarms = json.loads(put).get("Alarms", []) if put else []
except ValueError:
    alarms = []
if alarms:
    print("  moto returned target tracking alarms:", [a["AlarmName"] for a in alarms])
else:
    print("  (moto creates no CloudWatch alarms for target tracking; AWS returns TargetTracking-GROUP-AlarmHigh-... and -AlarmLow-...)")
PY

echo "▸ put-lifecycle-hook with no timeout or result: the defaults match the API reference and miniasg"
run "${AWSCLI[@]}" autoscaling put-lifecycle-hook --auto-scaling-group-name "$GROUP" --lifecycle-hook-name cc-defaults --lifecycle-transition autoscaling:EC2_INSTANCE_LAUNCHING
run "${AWSCLI[@]}" autoscaling describe-lifecycle-hooks --auto-scaling-group-name "$GROUP"
python3 - "$WORK/last.out" "$LESSON_DIR/code" <<'PY' || fail "lifecycle hook defaults: $(cat "$WORK/last.out")"
import json, sys
sys.path.insert(0, sys.argv[2])
import main
hook = [h for h in json.load(open(sys.argv[1]))["LifecycleHooks"] if h["LifecycleHookName"] == "cc-defaults"][0]
mine = main.hooks_from([{"LifecycleHookName": "cc-defaults"}])[0]
assert (hook["HeartbeatTimeout"], hook["DefaultResult"]) == (mine.heartbeat_timeout, mine.default_result) == (3600, "ABANDON"), hook
print(f"  HeartbeatTimeout {hook['HeartbeatTimeout']}, DefaultResult {hook['DefaultResult']} in moto and miniasg")
PY
run "${AWSCLI[@]}" autoscaling delete-lifecycle-hook --auto-scaling-group-name "$GROUP" --lifecycle-hook-name cc-defaults

echo "▸ start-instance-refresh against moto"
RC=0
AWS_MAX_ATTEMPTS=1 "${AWSCLI[@]}" autoscaling start-instance-refresh --auto-scaling-group-name "$GROUP" --preferences MinHealthyPercentage=90 > "$WORK/refresh.out" 2>&1 || RC=$?
if [[ "$RC" == "0" ]]; then
  echo "  moto accepted it: $(head -c 200 "$WORK/refresh.out")"
else
  echo "  (moto $MOTO_VERSION does not implement instance refresh; miniasg's refresh is checked against the documented behaviour above)"
fi

echo "SMOKE OK: cloud/autoscaling-groups"
