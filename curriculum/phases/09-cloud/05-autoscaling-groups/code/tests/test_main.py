import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
ZONES = ["us-east-1a", "us-east-1b", "us-east-1c"]


def iid(n):
    return f"i-{n:017x}"


def group(**fields):
    doc = {"AutoScalingGroupName": "web", "MinSize": 1, "MaxSize": 6, "DesiredCapacity": 3, "AvailabilityZones": ZONES}
    doc.update(fields)
    return doc


def scenario(events=(), until=None, **fields):
    doc = {"group": group(**fields), "events": list(events)}
    if until is not None:
        doc["until"] = until
    return doc


def run(doc, **kwargs):
    return main.run_scenario(doc, **kwargs)


def builtin(name, **kwargs):
    return main.run_scenario(main.load_scenario(name), **kwargs)


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


def checkpoint_after(asg, op):
    return [c for c in asg.checkpoints if c["after"] == op]


class ScalingMathTests(unittest.TestCase):
    def test_target_tracking_rounds_up_in_both_directions(self):
        self.assertEqual(main.target_tracking_capacity(2, 75, 50), 3)
        self.assertEqual(main.target_tracking_capacity(4, 75, 50), 6)
        self.assertEqual(main.target_tracking_capacity(4, 40, 50), 4)
        self.assertEqual(main.target_tracking_capacity(4, 35, 50), 3)
        self.assertEqual(main.target_tracking_capacity(4, 50, 50), 4)

    def test_percent_change_rounding_follows_the_user_guide_table(self):
        self.assertEqual(main.round_percent_change(12.7), 12)
        self.assertEqual(main.round_percent_change(0.67), 1)
        self.assertEqual(main.round_percent_change(-0.58), -1)
        self.assertEqual(main.round_percent_change(-6.67), -6)
        self.assertEqual(main.round_percent_change(0), 0)

    def test_adjustment_types_and_min_adjustment_magnitude(self):
        self.assertEqual(main.adjusted_capacity("ChangeInCapacity", 3, 5), 8)
        self.assertEqual(main.adjusted_capacity("ExactCapacity", 3, 5), 5)
        self.assertEqual(main.adjusted_capacity("PercentChangeInCapacity", 10, 10), 11)
        self.assertEqual(main.adjusted_capacity("PercentChangeInCapacity", 11, 30), 14)
        self.assertEqual(main.adjusted_capacity("PercentChangeInCapacity", 14, -10), 13)
        self.assertEqual(main.adjusted_capacity("PercentChangeInCapacity", 4, 25, min_magnitude=2), 6)
        with self.assertRaises(main.InputError):
            main.adjusted_capacity("Doubling", 3, 2)

    def test_step_bounds_are_inclusive_below_the_breach_and_exclusive_above(self):
        steps = [main.Step(0, 10, 0), main.Step(10, 20, 10), main.Step(20, None, 30)]
        self.assertEqual(main.matching_step(steps, 50, 60, True).adjustment, 10)
        self.assertEqual(main.matching_step(steps, 50, 70, True).adjustment, 30)
        self.assertEqual(main.matching_step(steps, 50, 69.9, True).adjustment, 10)
        inward = [main.Step(-10, 0, 0), main.Step(-20, -10, -10), main.Step(None, -20, -30)]
        self.assertEqual(main.matching_step(inward, 50, 40, False).adjustment, -10)
        self.assertEqual(main.matching_step(inward, 50, 30, False).adjustment, -30)
        self.assertEqual(main.matching_step(inward, 50, 50, False).adjustment, 0)

    def test_step_validation_rejects_gaps_and_missing_open_ends(self):
        with self.assertRaisesRegex(main.InputError, "gap"):
            main.validate_steps([main.Step(0, 10, 1), main.Step(20, None, 2)])
        with self.assertRaisesRegex(main.InputError, "null upper bound"):
            main.validate_steps([main.Step(0, 10, 1), main.Step(10, 20, 2)])
        with self.assertRaisesRegex(main.InputError, "null lower bound"):
            main.validate_steps([main.Step(-20, -10, -1), main.Step(-10, 0, 0)])
        main.validate_steps([main.Step(0, 10, 0), main.Step(10, None, 1)])


class CapacityTests(unittest.TestCase):
    def test_create_spreads_instances_over_the_zones(self):
        asg, summary = run(scenario(until=120))
        self.assertEqual(summary["zones"], {"us-east-1a": 1, "us-east-1b": 1, "us-east-1c": 1})
        self.assertIn("a user request created an AutoScalingGroup changing the desired capacity from 0 to 3.", asg.activities[0]["Cause"])
        self.assertEqual(asg.activities[0]["Description"], f"Launching a new EC2 instance: {iid(1)}")
        self.assertEqual(asg.activities[0]["StatusCode"], "Successful")

    def test_scale_out_goes_to_the_zone_with_fewest_instances(self):
        asg, summary = run(scenario([{"at": 300, "op": "set-desired-capacity", "DesiredCapacity": 5}], 600))
        self.assertEqual(summary["zones"], {"us-east-1a": 2, "us-east-1b": 2, "us-east-1c": 1})
        launch = [a for a in asg.activities if a["Description"].endswith(iid(4))][0]
        self.assertIn("a user request explicitly set group desired capacity changing the desired capacity from 3 to 5.", launch["Cause"])
        self.assertIn("increasing the capacity from 3 to 5.", launch["Cause"])

    def test_scale_in_takes_from_the_fullest_zone_then_the_oldest_version(self):
        asg, summary = builtin("spread")
        terminated = [i.id for i in asg.instances if i.state == "Terminated"]
        self.assertEqual(terminated, [iid(1), iid(2), iid(3), iid(4)])
        self.assertEqual(summary["zones"], {"us-east-1a": 0, "us-east-1b": 1, "us-east-1c": 1})
        self.assertEqual(summary["versions"], {"1": 1, "2": 1})

    def test_zone_balance_beats_the_termination_policy(self):
        for policy, expected in (("NewestInstance", "i-b-new"), ("OldestInstance", "i-b-old")):
            asg = main.Asg(main.Group(desired=3, zones=ZONES[:2], termination_policies=[policy]), create=False)
            asg.instances = [main.Instance(name, zone, "1", launched, state="InService", ec2_state="running", in_service_at=launched + 60)
                             for name, zone, launched in (("i-a-newest", "us-east-1a", 1000), ("i-a-oldest", "us-east-1a", -500), ("i-b-old", "us-east-1b", 0), ("i-b-new", "us-east-1b", 500), ("i-b-mid", "us-east-1b", 200))]
            asg.group.desired = 5
            asg.now = 2000
            asg.set_desired_capacity(4)
            asg.reconcile()
            victims = [i.id for i in asg.instances if i.state == "Terminating"]
            self.assertEqual(victims, [expected], policy)

    def test_protected_instances_are_skipped_for_scale_in(self):
        doc = scenario([{"at": 120, "op": "set-instance-protection", "InstanceId": iid(1), "ProtectedFromScaleIn": True},
                        {"at": 130, "op": "set-desired-capacity", "DesiredCapacity": 1}], 400)
        asg, summary = run(doc)
        self.assertEqual([i.id for i in asg.live()], [iid(1)])

    def test_desired_outside_min_and_max_is_a_validation_error(self):
        with self.assertRaisesRegex(main.AsgError, "Desired capacity:9 must be between the specified min size:1 and max size:6"):
            run(scenario(DesiredCapacity=9))
        asg, summary = run(scenario([{"at": 100, "op": "set-desired-capacity", "DesiredCapacity": 9}], 200))
        self.assertEqual(summary["rejected"][0]["code"], "ValidationError")
        self.assertIn("above max value 6", summary["rejected"][0]["message"])
        self.assertEqual(summary["desired"], 3)

    def test_update_clamps_desired_into_the_new_range(self):
        asg, summary = run(scenario([{"at": 100, "op": "update-auto-scaling-group", "MaxSize": 2},
                                     {"at": 300, "op": "update-auto-scaling-group", "MinSize": 4, "MaxSize": 6}], 600))
        self.assertEqual(summary["desired_history"], [0, 3, 2, 4])
        self.assertEqual(summary["instances"], 4)

    def test_terminate_with_and_without_decrement(self):
        asg, summary = run(scenario([{"at": 100, "op": "terminate-instance-in-auto-scaling-group", "InstanceId": iid(1), "ShouldDecrementDesiredCapacity": False},
                                     {"at": 300, "op": "terminate-instance-in-auto-scaling-group", "InstanceId": iid(2), "ShouldDecrementDesiredCapacity": True}], 600))
        self.assertEqual([c["instances"] for c in asg.checkpoints], [3, 3, 2])
        self.assertEqual(summary["desired_history"], [0, 3, 2])
        cause = [a for a in asg.activities if a["Description"] == f"Terminating EC2 instance: {iid(2)}"][0]["Cause"]
        self.assertIn("in response to a user request, shrinking the capacity from 3 to 2.", cause)


class HealthTests(unittest.TestCase):
    def test_a_stopped_instance_is_replaced_at_once_even_inside_the_grace_period(self):
        doc = scenario([{"at": 70, "op": "stop-instance", "InstanceId": iid(1)}], 300, HealthCheckType="ELB", HealthCheckGracePeriod=600)
        asg, summary = run(doc)
        replaced = asg.instances[3]
        self.assertEqual((replaced.zone, replaced.launched_at, replaced.purpose), ("us-east-1a", 70, "replace"))
        self.assertEqual(summary["instances"], 3)

    def test_elb_failures_wait_for_the_grace_period(self):
        asg, summary = builtin("self-heal")
        marked = {e["message"].split()[0]: e["t"] for e in asg.events if e["kind"] == "unhealthy"}
        self.assertEqual(marked[iid(1)], 300)
        self.assertEqual(marked[iid(4)], 360 + 120)
        self.assertEqual(marked[iid(2)], 700)
        self.assertEqual(marked[iid(3)], 900)
        self.assertEqual(summary["zones"], {"us-east-1a": 1, "us-east-1b": 1, "us-east-1c": 1})

    def test_ec2_health_checks_ignore_the_load_balancer(self):
        doc = scenario([{"at": 100, "op": "elb-health", "InstanceId": iid(1), "Healthy": False}], 600, HealthCheckType="EC2")
        asg, summary = run(doc)
        self.assertFalse([e for e in asg.events if e["kind"] == "unhealthy"])
        self.assertEqual(summary["serving"], 2)

    def test_set_instance_health_can_respect_the_grace_period(self):
        doc = scenario([{"at": 90, "op": "set-instance-health", "InstanceId": iid(1), "HealthStatus": "Unhealthy", "ShouldRespectGracePeriod": True}], 300, HealthCheckGracePeriod=300)
        asg, summary = run(doc)
        self.assertEqual(asg.instances[0].health, "Healthy")
        self.assertTrue(any("grace period" in e["message"] for e in asg.events if e["kind"] == "health"))

    def test_elb_replacements_are_limited_to_ten_percent_of_desired(self):
        events = [{"at": 120, "op": "elb-health", "InstanceId": iid(n), "Healthy": False} for n in range(1, 6)]
        asg, summary = run(scenario(events, 900, MaxSize=20, DesiredCapacity=20, HealthCheckType="ELB"))
        first = [e["t"] for e in asg.events if e["kind"] == "terminate"]
        self.assertEqual(first[:2], [120, 120])
        self.assertGreater(first[2], 120)
        self.assertEqual(summary["instances"], 20)

    def test_a_zone_outage_moves_capacity_and_rebalancing_launches_first(self):
        asg, summary = builtin("az-outage")
        outage, recovery = checkpoint_after(asg, "zone-outage")[0], checkpoint_after(asg, "zone-recover")[0]
        self.assertEqual(outage["zones"], {"us-east-1a": 3, "us-east-1b": 0, "us-east-1c": 3})
        self.assertEqual(recovery["zones"], {"us-east-1a": 2, "us-east-1b": 2, "us-east-1c": 2})
        self.assertEqual((recovery["min_serving"], recovery["peak"]), (6, 7))
        self.assertTrue(any(a["StatusCode"] == "Failed" and "us-east-1b" in a["Details"] for a in asg.activities))


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.expected = fixture("expected-decisions.json")

    def decisions(self, summary):
        return [[d["datapoint"], d["from"], d["to"]] for d in summary["decisions"]]

    def test_step_scaling_matches_the_user_guide_example(self):
        asg, summary = builtin("step-scaling")
        self.assertEqual(self.decisions(summary), self.expected["step-scaling"])
        self.assertEqual(summary["desired_history"][1:], [10, 11, 14, 13, 10])

    def test_step_warmup_does_not_add_twice_and_blocks_scale_in(self):
        asg, summary = builtin("step-warmup")
        self.assertEqual(self.decisions(summary), self.expected["step-warmup"])
        self.assertTrue(any(e["kind"] == "blocked" and e["t"] == 600 + 4 * 60 for e in asg.events))

    def test_target_tracking_scales_out_fast_and_in_slowly(self):
        asg, summary = builtin("target-tracking")
        self.assertEqual(self.decisions(summary), self.expected["target-tracking"])
        self.assertTrue(any(e["kind"] == "no-change" and "would push the metric above 50" in e["message"] for e in asg.events))

    def test_simple_scaling_waits_for_the_launch_and_the_cooldown(self):
        asg, summary = builtin("simple-cooldown")
        self.assertEqual([d["t"] for d in summary["decisions"]], self.expected["simple-cooldown-times"])
        self.assertEqual(summary["desired"], 10)

    def test_honor_cooldown_rejects_a_manual_change_during_cooldown(self):
        doc = main.load_scenario("simple-cooldown")
        doc["events"] = [{"at": 700, "op": "set-desired-capacity", "DesiredCapacity": 5, "HonorCooldown": True}]
        asg, summary = run(doc)
        self.assertEqual(summary["rejected"][0]["code"], "ScalingActivityInProgress")

    def test_scale_out_if_any_tracking_policy_wants_it_in_only_if_all_do(self):
        def two_policies(cpu, rps):
            doc = scenario(until=600 + 20 * 60, MinSize=2, MaxSize=10, DesiredCapacity=4)
            doc["policies"] = [{"PolicyName": name, "PolicyType": "TargetTrackingScaling", "EstimatedInstanceWarmup": 0, "TargetTrackingConfiguration": {"TargetValue": target}}
                               for name, target in (("cpu", 50.0), ("rps", 100.0))]
            doc["metric"] = {"mode": "observed", "start": 600, "series": {"cpu": [cpu] * 20, "rps": [rps] * 20}}
            return self.decisions(run(doc)[1])
        self.assertEqual(two_policies(20, 100), [])
        self.assertEqual(two_policies(20, 50), [[14, 4, 2]])
        self.assertEqual(two_policies(20, 300), [[2, 4, 10]])

    def test_policies_from_describe_policies_output(self):
        described = fixture("describe-policies-moto.json")
        _, step = builtin("step-scaling", policies_doc=described)
        _, tracking = builtin("target-tracking", policies_doc=described)
        self.assertEqual(self.decisions(step), self.expected["step-scaling"])
        self.assertEqual(self.decisions(tracking), self.expected["target-tracking"])
        with self.assertRaisesRegex(main.InputError, "no policy named add2"):
            builtin("simple-cooldown", policies_doc=described)


class LifecycleAndRefreshTests(unittest.TestCase):
    def test_launch_hook_holds_capacity_and_abandon_replaces(self):
        asg, summary = builtin("lifecycle-hook")
        third = asg.instances[2]
        self.assertEqual(third.state, "Terminated")
        self.assertEqual(asg.instances[3].launched_at, 660 + 300)
        self.assertEqual(len(asg.instances), 4)
        self.assertEqual(third.activity["StatusCode"], "Cancelled")

    def test_grace_period_starts_when_the_hook_completes(self):
        asg, _ = builtin("lifecycle-hook")
        self.assertEqual(asg.instances[0].in_service_at, 150)

    def test_terminate_hook_delays_termination(self):
        asg, _ = builtin("lifecycle-hook")
        waits = [e for e in asg.events if e["kind"] == "hook" and "Terminating:Wait" in e["message"]]
        self.assertEqual(len(waits), 1)
        gone = [e["t"] for e in asg.events if e["kind"] == "terminated" and e["message"].startswith(iid(1))]
        self.assertEqual(gone, [1500 + 30 + 120])

    def test_refresh_replaces_one_at_a_time_with_min_healthy_90(self):
        asg, summary = builtin("refresh")
        self.assertEqual(summary["refresh"]["status"], "Successful")
        self.assertEqual(summary["versions"], {"2": 4})
        self.assertEqual((summary["min_serving"], summary["peak_instances"]), (3, 4))

    def test_refresh_with_max_healthy_150_launches_before_terminating(self):
        asg, summary = builtin("refresh-surge")
        self.assertEqual(summary["refresh"]["status"], "Successful")
        self.assertEqual((summary["min_serving"], summary["peak_instances"]), (4, 6))

    def test_a_broken_version_fails_the_refresh_only_with_elb_health_checks(self):
        _, elb = builtin("refresh-broken")
        self.assertEqual(elb["refresh"]["status"], "Failed")
        self.assertEqual((elb["refresh"]["replaced"], elb["min_serving"], elb["versions"]), (1, 3, {"1": 4}))
        _, ec2 = builtin("refresh-broken", health_check_type="EC2")
        self.assertEqual(ec2["refresh"]["status"], "Successful")
        self.assertEqual((ec2["serving"], ec2["versions"]), (0, {"3": 4}))

    def test_refresh_preferences_are_validated(self):
        asg = main.Asg(main.Group(desired=2))
        with self.assertRaises(main.AsgError):
            asg.start_instance_refresh("2", min_healthy=50, max_healthy=200)
        asg.start_instance_refresh("2")
        with self.assertRaisesRegex(main.AsgError, "InstanceRefreshInProgress"):
            asg.start_instance_refresh("3")


class PlanTests(unittest.TestCase):
    def test_plan_replaces_the_unhealthy_instance_and_rebalances_moto_placement(self):
        result = main.plan(fixture("describe-asg-moto.json"))
        self.assertEqual(result["zones"], {"us-east-1a": 5, "us-east-1b": 0, "us-east-1c": 0})
        self.assertEqual(result["first_pass"][0]["action"], "terminate")
        self.assertEqual(result["first_pass"][0]["instance"], result["unhealthy"][0])
        self.assertEqual([a["action"] for a in result["first_pass"]], ["terminate", "launch"])
        self.assertFalse(result["balanced_now"])
        self.assertEqual(result["rebalance_moves"], 3)
        self.assertEqual(result["settled"], {"instances": 5, "zones": {"us-east-1a": 2, "us-east-1b": 2, "us-east-1c": 1}, "seconds": result["settled"]["seconds"]})

    def test_plan_on_a_settled_group_does_nothing(self):
        doc = {"AutoScalingGroups": [dict(group(DesiredCapacity=3), Instances=[
            {"InstanceId": f"i-{n}", "AvailabilityZone": z, "LifecycleState": "InService", "HealthStatus": "Healthy"} for n, z in enumerate(ZONES)])]}
        result = main.plan(doc)
        self.assertEqual(result["first_pass"], [])
        self.assertTrue(result["balanced_now"])

    def test_plan_flags_a_desired_capacity_aws_would_reject(self):
        doc = {"AutoScalingGroups": [dict(group(DesiredCapacity=9), Instances=[])]}
        result = main.plan(doc)
        self.assertIn("ValidationError", result["note"])
        self.assertEqual(result["settled"]["instances"], 6)


class CliTests(unittest.TestCase):
    def test_list_shows_every_builtin_scenario(self):
        code, out, _ = run_cli(["list", "--json"])
        self.assertEqual(code, 0)
        names = {row["name"] for row in json.loads(out)}
        self.assertTrue({"spread", "self-heal", "az-outage", "target-tracking", "step-scaling", "refresh-broken"} <= names)

    def test_run_json_has_summary_checkpoints_and_activities(self):
        code, out, _ = run_cli(["run", "step-scaling", "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["summary"]["desired_history"][-1], 10)
        self.assertEqual(set(doc["activities"][0]) >= {"ActivityId", "Description", "Cause", "StatusCode", "StartTime"}, True)

    def test_rejected_operation_exits_1_and_bad_input_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ops.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(scenario([{"at": 60, "op": "set-desired-capacity", "DesiredCapacity": 9}], 120), handle)
            code, out, _ = run_cli(["run", path])
            self.assertEqual(code, 1)
            self.assertIn("ValidationError", out)
            bad = os.path.join(tmp, "bad.json")
            with open(bad, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            code, _, err = run_cli(["run", bad])
            self.assertEqual(code, 2)
            self.assertIn("not valid JSON", err)
        code, _, err = run_cli(["run", "no-such-scenario"])
        self.assertEqual(code, 2)
        self.assertIn("unknown scenario", err)

    def test_plan_cli_text_output(self):
        code, out, _ = run_cli(["plan", "--describe", os.path.join(FIXTURES, "describe-asg-moto.json")])
        self.assertEqual(code, 0)
        self.assertIn("AZRebalance needs 3 launch(es)", out)
        self.assertIn("1a=2 1b=2 1c=1", out)


if __name__ == "__main__":
    unittest.main()
