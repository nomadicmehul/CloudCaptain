import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import ThreadingHTTPServer
from io import StringIO
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
CONFIG = os.path.join(FIXTURES, "alertmanager.json")
SCENARIO = os.path.join(FIXTURES, "scenario.json")
ROUTE_CASES = os.path.join(FIXTURES, "route-cases.json")
RULES = os.path.join(FIXTURES, "rules.json")
RULES_TEST = os.path.join(FIXTURES, "rules-test.json")
RULES_TEST_WRONG = os.path.join(FIXTURES, "rules-test-wrong.json")


def read(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def config(raw=None):
    return main.load_config(raw if raw is not None else read(CONFIG))


def run(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


def simple_config(**route):
    raw = {
        "route": dict({"receiver": "hook", "group_by": ["alertname"], "group_wait": "10s", "group_interval": "1m", "repeat_interval": "1h"}, **route),
        "receivers": [{"name": "hook", "webhook_configs": [{"url": "http://hook.invalid/"}]}],
    }
    return raw


def alert(name, **labels):
    return {"labels": dict(labels, alertname=name), "annotations": {"summary": name}}


SIM = main.simulate(config(), read(SCENARIO))


def notes(result=SIM):
    return [(n["at"], n["receiver"], n["payload"]["status"], n["payload"]["notification_reason"], len(n["payload"]["alerts"])) for n in result["notifications"]]


class ParsingTests(unittest.TestCase):
    def test_durations_parse_and_format_like_prometheus(self):
        self.assertEqual(main.parse_duration("1h30m"), 5400)
        self.assertEqual(main.parse_duration("500ms"), 0.5)
        self.assertEqual(main.parse_duration("0"), 0)
        self.assertEqual(main.format_duration(5400), "1h30m")
        self.assertEqual(main.format_duration(0), "0s")
        for bad in ("", "5", "1m1h", "-5m", "five"):
            with self.assertRaises(main.InputError, msg=bad):
                main.parse_duration(bad)

    def test_fingerprint_matches_prometheus_common_test_vectors(self):
        self.assertEqual(int(main.fingerprint({}), 16), 14695981039346656037)
        self.assertEqual(int(main.fingerprint({"name": "garland, briggs", "fear": "love is not enough"}), 16), 5799056148416392346)
        self.assertEqual(main.fingerprint({"alertname": "X", "service": "s"}), "86192aebb5111ed0")
        self.assertEqual(main.fingerprint({"a": "1", "b": "2"}), main.fingerprint({"b": "2", "a": "1"}))
        self.assertNotEqual(main.fingerprint({"a": "1b"}), main.fingerprint({"a1": "b"}))

    def test_matchers_are_anchored_and_treat_a_missing_label_as_empty(self):
        team = main.parse_matcher('team=~"db|storage"')
        self.assertTrue(team.matches({"team": "storage"}))
        self.assertFalse(team.matches({"team": "dbx"}))
        self.assertTrue(main.parse_matcher("env!=prod").matches({}))
        self.assertTrue(main.parse_matcher('env=""').matches({"team": "db"}))
        self.assertTrue(main.parse_matcher('cluster!~"eu.*"').matches({"cluster": "us1"}))
        self.assertEqual(str(main.parse_matcher('summary="say \\"hi\\""')), 'summary="say \\"hi\\""')
        for bad in ("team", 'team=~"("', 'team="db', "1x=y"):
            with self.assertRaises(main.InputError, msg=bad):
                main.parse_matcher(bad)

    def test_time_round_trips_through_rfc3339(self):
        unix = main.parse_time("2026-11-02T09:00:00Z")
        self.assertEqual(main.rfc3339(unix), "2026-11-02T09:00:00Z")
        self.assertEqual(main.parse_time("2026-11-02T10:00:00+01:00"), unix)
        self.assertEqual(main.rfc3339(unix + 0.25), "2026-11-02T09:00:00.25Z")
        self.assertEqual(main.rfc3339(None), main.ZERO_TIME)
        with self.assertRaises(main.InputError):
            main.parse_time("yesterday")


class ConfigTests(unittest.TestCase):
    def test_children_inherit_receiver_grouping_and_timers(self):
        cfg = config()
        pager, db_pager = cfg.route.routes[1], cfg.route.routes[1].routes[0]
        self.assertEqual((pager.group_by, pager.group_wait, pager.group_interval, pager.repeat_interval), (["alertname", "service"], 2, 6, 9))
        self.assertEqual((db_pager.receiver, db_pager.repeat_interval), ("db-pager", 9))
        self.assertEqual(cfg.route.routes[2].group_by, ["alertname", "cluster"])
        self.assertEqual(db_pager.path(), "route.routes[1].routes[0]")
        self.assertEqual(db_pager.key(), '{}/{severity="page"}/{team="db"}')
        self.assertEqual(config(simple_config(group_wait=None)).route.group_wait, main.DEFAULT_GROUP_WAIT)

    def test_invalid_configurations_are_rejected_with_alertmanagers_reason(self):
        cases = {
            "undefined receiver": lambda raw: raw["route"]["routes"][0].update(receiver="nobody"),
            "default receiver": lambda raw: raw["route"].pop("receiver"),
            "must not have any matchers": lambda raw: raw["route"].update(matchers=['team="db"']),
            "wildcard group_by": lambda raw: raw["route"].update(group_by=["...", "alertname"]),
            "not unique": lambda raw: raw["receivers"].append({"name": "pager"}),
            "group_interval cannot be zero": lambda raw: raw["route"].update(group_interval="0"),
            "url": lambda raw: raw["receivers"][0]["webhook_configs"][0].pop("url"),
        }
        for reason, change in cases.items():
            raw = read(CONFIG)
            change(raw)
            with self.assertRaises(main.ConfigError, msg=reason) as caught:
                main.load_config(raw)
            self.assertIn(reason, str(caught.exception))

    def test_webhooks_send_resolved_by_default_and_legacy_match_still_works(self):
        raw = simple_config(routes=[{"match": {"severity": "page"}, "match_re": {"team": "db|storage"}, "receiver": "hook"}])
        raw["receivers"].append({"name": "mail", "email_configs": [{"to": "x@example.com"}]})
        cfg = config(raw)
        self.assertTrue(cfg.receivers["hook"].integrations[0].send_resolved)
        self.assertFalse(cfg.receivers["mail"].integrations[0].send_resolved)
        self.assertEqual([str(m) for m in cfg.route.routes[0].matchers], ['severity="page"', 'team=~"db|storage"'])


class RoutingTests(unittest.TestCase):
    def test_route_table_matches_what_amtool_answers(self):
        cfg = config()
        for case in read(ROUTE_CASES):
            with self.subTest(labels=case["labels"]):
                self.assertEqual(",".join(r.receiver for r in cfg.route.match(case["labels"])), case["receivers"])

    def test_explain_shows_stop_continue_and_the_group_key(self):
        result = main.routes_test(config(), {"alertname": "DiskFull", "severity": "page", "team": "db", "cluster": "eu1"})
        trace = "\n".join(result["trace"])
        self.assertIn("continue: true on route.routes[1]", trace)
        self.assertIn('no match (alertname="DiskFull" fails alertname="Watchdog")', trace)
        self.assertEqual(result["receivers"], ["db-pager", "db-chat"])
        self.assertEqual(result["routes"][1]["group_key"], '{}/{team=~"db|storage"}:{alertname="DiskFull", cluster="eu1"}')
        stopped = main.routes_test(config(), {"alertname": "Watchdog", "severity": "page"})
        self.assertIn("stop: route.routes[0] matched and continue is false", "\n".join(stopped["trace"]))

    def test_group_by_wildcard_puts_every_label_set_in_its_own_group(self):
        cfg = config(simple_config(group_by=["..."]))
        dispatcher = main.Dispatcher(cfg)
        dispatcher.post([alert("A", instance="a"), alert("A", instance="b"), alert("A", instance="a")], 0)
        self.assertEqual(len(dispatcher.groups), 2)

    def test_routes_test_cli_verifies_receivers(self):
        code, out, _ = run(["routes", "test", "--config", CONFIG, "--verify.receivers=pager", "alertname=QueueFull", "severity=page", "team=storage"])
        self.assertEqual(code, 1)
        self.assertIn("pager,db-chat", out)
        self.assertIn("WARNING: Expected receivers did not match", out)
        code, out, _ = run(["routes", "test", "--config", CONFIG, "--verify.receivers=pager,db-chat", "alertname=QueueFull", "severity=page", "team=storage"])
        self.assertEqual((code, out.strip()), (0, "pager,db-chat"))
        code, _, err = run(["routes", "test", "--config", CONFIG, 'team=~"db"'])
        self.assertEqual(code, 2)
        self.assertIn("key=value", err)


class DispatcherTests(unittest.TestCase):
    def test_fixture_scenario_sends_the_twelve_notifications_alertmanager_sent(self):
        self.assertEqual(len(SIM["notifications"]), 12)
        self.assertEqual(notes()[:3], [(2.0, "pager", "firing", "first notification", 1), (2.0, "default-chat", "firing", "first notification", 1), (6.0, "db-pager", "firing", "first notification", 1)])
        self.assertIn((8.0, "pager", "firing", "new alerts added", 2), notes())
        self.assertIn((24.0, "pager", "resolved", "all alerts resolved", 1), notes())
        self.assertIn((32.0, "pager", "firing", "some alerts resolved", 2), notes())
        self.assertNotIn("blackhole", {n[1] for n in notes()})
        self.assertEqual(SIM["min_margin"], 1.0)

    def test_inhibition_needs_equal_labels_and_never_mutes_the_source(self):
        snapshot = {a["labels"]["alertname"] + a["labels"].get("instance", ""): a for a in SIM["snapshots"][0]["alerts"]}
        cluster_down = main.fingerprint(read(SCENARIO)["alerts"]["G"]["labels"])
        for name in ("DiskFullpg-0", "SlowQueriespg-0", "HighErrorRatecheckout-0", "HighErrorRatecheckout-1"):
            self.assertEqual(snapshot[name]["status"]["inhibitedBy"], [cluster_down], name)
        self.assertEqual(snapshot["ClusterDown"]["status"]["state"], "active")
        dispatcher = main.Dispatcher(config())
        dispatcher.post([alert("ClusterDown", cluster="us1", severity="page"), alert("DiskFull", cluster="eu1", severity="page")], 0)
        self.assertIsNone(dispatcher.inhibited_by(dict(alert("DiskFull", cluster="eu1", severity="page")["labels"]), 1))
        self.assertIsNotNone(dispatcher.inhibited_by(dict(alert("DiskFull", cluster="us1", severity="page")["labels"]), 1))

    def test_inhibited_alerts_are_not_sent_and_come_back_after_the_source_resolves(self):
        db = [n for n in notes() if n[1] == "db-pager"]
        self.assertEqual(db, [(6.0, "db-pager", "firing", "first notification", 1), (24.0, "db-pager", "firing", "repeat interval elapsed", 1)])
        self.assertTrue(any(e["kind"] == "inhibited" and e["at"] == 12.0 for e in SIM["events"]))

    def test_silence_mutes_until_expired_then_the_group_reports_new_alerts(self):
        cert = [n for n in notes() if n[1] == "default-chat"]
        self.assertEqual(cert, [(2.0, "default-chat", "firing", "first notification", 1), (20.0, "default-chat", "firing", "new alerts added", 2)])
        silenced = [e["at"] for e in SIM["events"] if e["kind"] == "silenced"]
        self.assertEqual(silenced, [8.0, 14.0, 14.0])
        self.assertEqual(SIM["snapshots"][1]["alerts"][0]["status"]["silencedBy"], [])

    def test_silence_validation_and_expiry(self):
        dispatcher = main.Dispatcher(config())
        with self.assertRaises(main.InputError):
            dispatcher.add_silence("s", [], 0, 10)
        with self.assertRaises(main.InputError):
            dispatcher.add_silence("s", [main.parse_matcher("a=b")], 10, 10)
        dispatcher.add_silence("s", [main.parse_matcher("a=b")], 0, 100)
        self.assertEqual(dispatcher.silenced_by({"a": "b"}, 50), ["s"])
        dispatcher.expire_silence("s", 60)
        self.assertEqual(dispatcher.silenced_by({"a": "b"}, 61), [])
        with self.assertRaises(main.InputError):
            dispatcher.expire_silence("s", 70)
        with self.assertRaises(main.InputError):
            dispatcher.expire_silence("missing", 70)

    def test_needs_update_reasons(self):
        entry = main.LogEntry({"a", "b"}, set(), 100)
        self.assertEqual(main.needs_update(None, {"a"}, set(), 60, 110, True), main.REASON_FIRST)
        self.assertEqual(main.needs_update(None, set(), {"a"}, 60, 110, True), main.REASON_NONE)
        self.assertEqual(main.needs_update(entry, {"a", "b", "c"}, set(), 60, 110, True), main.REASON_NEW_ALERTS)
        self.assertEqual(main.needs_update(entry, {"a"}, {"b"}, 60, 110, True), main.REASON_NEW_RESOLVED)
        self.assertEqual(main.needs_update(entry, {"a"}, {"b"}, 60, 110, False), main.REASON_NONE)
        self.assertEqual(main.needs_update(entry, set(), {"a", "b"}, 60, 110, True), main.REASON_ALL_RESOLVED)
        self.assertEqual(main.needs_update(entry, {"a", "b"}, set(), 60, 110, True), main.REASON_NONE)
        self.assertEqual(main.needs_update(entry, {"a", "b"}, set(), 60, 161, True), main.REASON_REPEAT)

    def test_same_alert_posted_again_is_deduplicated_and_keeps_its_start(self):
        dispatcher = main.Dispatcher(config(simple_config()))
        dispatcher.post([alert("A")], 0)
        dispatcher.flush_due(10)
        dispatcher.post([alert("A")], 30)
        dispatcher.flush_due(70)
        self.assertEqual(len(dispatcher.notifications), 1)
        self.assertEqual(dispatcher.alerts[main.fingerprint(alert("A")["labels"])].starts_at, 0)
        self.assertEqual(dispatcher.events[-1]["kind"], "dedup")

    def test_alert_that_started_long_ago_skips_group_wait(self):
        dispatcher = main.Dispatcher(config(simple_config()))
        dispatcher.post([dict(alert("Old"), startsAt=0.0)], 100)
        self.assertEqual(dispatcher.next_flush(), 100)
        dispatcher.post([alert("New")], 100)
        self.assertEqual(sorted(g.next_flush for g in dispatcher.groups.values()), [100, 110])

    def test_resolve_timeout_resolves_an_alert_that_stops_being_sent(self):
        dispatcher = main.Dispatcher(config(simple_config()))
        dispatcher.post([alert("A")], 0)
        dispatcher.flush_due(10)
        dispatcher.flush_due(310)
        self.assertEqual([n["payload"]["status"] for n in dispatcher.notifications], ["firing", "resolved"])
        self.assertEqual(dispatcher.notifications[1]["payload"]["notification_reason"], main.REASON_ALL_RESOLVED)
        self.assertEqual(dispatcher.groups, {})

    def test_payload_is_alertmanager_webhook_version_4(self):
        payload = SIM["notifications"][3]["payload"]
        self.assertEqual(set(payload), {"receiver", "status", "alerts", "notification_reason", "groupLabels", "commonLabels", "commonAnnotations", "routeLabels", "externalURL", "version", "groupKey", "truncatedAlerts"})
        self.assertEqual(payload["version"], "4")
        first = payload["alerts"][0]
        self.assertEqual(first["endsAt"], main.ZERO_TIME)
        self.assertEqual(first["startsAt"], "2026-11-02T09:00:04Z")
        self.assertEqual(first["fingerprint"], main.fingerprint(first["labels"]))
        resolved = [n["payload"] for n in SIM["notifications"] if n["payload"]["status"] == "resolved"][0]
        self.assertEqual(resolved["alerts"][0]["endsAt"], "2026-11-02T09:00:21Z")
        self.assertEqual(main.common_map([{"a": "1", "b": "2"}, {"a": "1", "b": "3"}]), {"a": "1"})

    def test_max_alerts_truncates_and_send_resolved_false_drops_resolved(self):
        raw = simple_config()
        raw["receivers"][0]["webhook_configs"][0].update(max_alerts=1, send_resolved=False)
        dispatcher = main.Dispatcher(config(raw))
        dispatcher.post([alert("A", instance="a"), alert("A", instance="b")], 0)
        dispatcher.flush_due(10)
        payload = dispatcher.notifications[0]["payload"]
        self.assertEqual((len(payload["alerts"]), payload["truncatedAlerts"]), (1, 1))
        dispatcher.post([dict(alert("A", instance="a"), endsAt=20.0), dict(alert("A", instance="b"), endsAt=20.0)], 20)
        dispatcher.flush_due(70)
        self.assertEqual(len(dispatcher.notifications), 1)
        self.assertIn("send_resolved is false", dispatcher.events[-2]["text"])

    def test_stretching_every_time_and_timer_only_scales_the_notifications(self):
        stretch = lambda text: main.format_duration(main.parse_duration(text) * 3)
        raw, scenario = read(CONFIG), read(SCENARIO)

        def stretch_route(route):
            for key in ("group_wait", "group_interval", "repeat_interval"):
                if key in route:
                    route[key] = stretch(route[key])
            for child in route.get("routes") or []:
                stretch_route(child)

        stretch_route(raw["route"])
        raw["global"]["resolve_timeout"] = stretch(raw["global"]["resolve_timeout"])
        for step in scenario["steps"]:
            step["at"] = stretch(step["at"])
            if "silence" in step:
                step["silence"]["duration"] = stretch(step["silence"]["duration"])
        scenario["until"] = stretch(scenario["until"])
        slow = main.simulate(config(raw), scenario)
        self.assertEqual([(n[0] / 3,) + n[1:] for n in notes(slow)], notes())
        self.assertEqual(slow["min_margin"], 3.0)
        self.assertEqual([len(s["alerts"]) for s in slow["snapshots"]], [len(s["alerts"]) for s in SIM["snapshots"]])

    def test_scenario_errors_name_the_step(self):
        with self.assertRaises(main.InputError) as caught:
            main.load_scenario({"alerts": {}, "steps": [{"at": "1s", "post": ["X"]}]})
        self.assertIn("step 1: unknown alert 'X'", str(caught.exception))
        with self.assertRaises(main.InputError):
            main.load_scenario({"steps": [{"at": "1s", "post": [], "resolve": []}]})


class RuleTests(unittest.TestCase):
    def rule(self, expr, hold="0", keep="0", **extra):
        path = os.path.join(self.tmp, "r.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"groups": [{"name": "g", "rules": [dict({"alert": "A", "expr": expr, "for": hold, "keep_firing_for": keep}, **extra)]}]}, handle)
        return main.load_rules(path)[0][1][0]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def states(self, rule, values, interval=60.0):
        store = main.SeriesStore()
        for i, value in enumerate(values):
            if value is not None:
                store.add({"__name__": "x"}, i * interval, value)
        out = []
        for i in range(len(values)):
            rule.evaluate(store, i * interval, {})
            alerts = list(rule.active.values())
            out.append(alerts[0].display() if alerts else "-")
        return out

    def test_for_holds_pending_until_the_condition_was_true_that_long(self):
        rule = self.rule("x > 1", hold="2m")
        self.assertEqual(self.states(rule, [0, 5, 5, 5, 0]), ["-", "pending", "pending", "firing", "resolved"])

    def test_a_dip_while_pending_resets_the_for_clock(self):
        rule = self.rule("x > 1", hold="2m")
        self.assertEqual(self.states(rule, [5, 5, 0, 5, 5, 5]), ["pending", "pending", "-", "pending", "pending", "firing"])

    def test_keep_firing_for_bridges_a_gap_but_not_a_long_one(self):
        rule = self.rule("x > 1", keep="2m")
        self.assertEqual(self.states(rule, [5, 0, 5, 0, 0, 0]), ["firing", "firing (kept)", "firing", "firing (kept)", "firing (kept)", "resolved"])

    def test_resolved_alerts_are_kept_fifteen_minutes_then_forgotten(self):
        rule = self.rule("x > 1")
        values = [5, 0] + [0] * 16
        states = self.states(rule, values)
        self.assertEqual(states[1:17], ["resolved"] * 16)
        self.assertEqual(states[17], "-")

    def test_labels_and_annotations_are_templated_per_alert(self):
        rule = self.rule('max by (queue) (depth) > 100', labels={"team": "{{ $labels.queue }}-owners"}, annotations={"summary": "{{ $labels.queue }} has {{ $value | humanize }} waiting", "v": '{{ printf "%.1f" $value }}'})
        store = main.SeriesStore()
        store.add({"__name__": "depth", "queue": "emails", "instance": "a"}, 0, 1500)
        store.add({"__name__": "depth", "queue": "emails", "instance": "b"}, 0, 20)
        rule.evaluate(store, 0, {})
        only = list(rule.active.values())[0]
        self.assertEqual(only.labels, {"queue": "emails", "team": "emails-owners", "alertname": "A"})
        self.assertEqual(only.annotations, {"summary": "emails has 1.5k waiting", "v": "1500.0"})

    def test_absent_fires_after_a_stale_marker_but_not_during_lookback(self):
        rule = self.rule('absent(up{job="checkout"})')
        store = main.SeriesStore()
        store.add({"__name__": "up", "job": "checkout"}, 0, 1)
        rule.evaluate(store, 240, {})
        self.assertEqual(rule.active, {})
        rule.evaluate(store, 300, {})
        self.assertEqual(list(rule.active.values())[0].labels, {"job": "checkout", "alertname": "A"})
        store.add({"__name__": "up", "job": "checkout"}, 360, None)
        self.assertEqual(store.latest([main.Matcher("job", "=", "checkout")], 360), [])

    def test_sends_respect_the_resend_delay_and_set_ends_at_ahead(self):
        rule = self.rule("x > 1")
        store = main.SeriesStore()
        sent = []
        for i, value in enumerate([5, 5, 5, 0, 0]):
            store.add({"__name__": "x"}, i * 60.0, value)
            rule.evaluate(store, i * 60.0, {})
            sent.extend((s["at"], s["status"], s["endsAt"]) for s in rule.sends(i * 60.0, 60))
        self.assertEqual(sent, [(0.0, "firing", 240.0), (120.0, "firing", 360.0), (180.0, "resolved", 180.0)])

    def test_rule_files_reject_what_promtool_rejects(self):
        bad = {
            "one of 'record' or 'alert'": {"alert": "A", "record": "b", "expr": "x"},
            "field 'expr' must be set": {"alert": "A"},
            "in recording rule": {"record": "job:x:sum", "expr": "sum(x)", "for": "1m"},
            'function "nosuch" not defined': {"alert": "A", "expr": "x", "annotations": {"s": "{{ nosuch $value }}"}},
            "not in minialert's PromQL subset": {"alert": "A", "expr": "rate(x[5m]) > 1"},
        }
        for reason, entry in bad.items():
            path = os.path.join(self.tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"groups": [{"name": "g", "rules": [entry]}]}, handle)
            with self.assertRaises(main.InputError, msg=reason) as caught:
                main.load_rules(path)
            self.assertIn(reason, str(caught.exception))


class ExpressionAndTemplateTests(unittest.TestCase):
    def test_series_notation_expands_like_promtool(self):
        self.assertEqual(main.expand_values("0+10x3 _ _x2 stale 5-1x2 1e3"), [0.0, 10.0, 20.0, 30.0, "_", "_", "_", "stale", 5.0, 4.0, 3.0, 1000.0])
        self.assertEqual(main.parse_series_spec('up{job="a", instance="b\\"c"}'), {"__name__": "up", "job": "a", "instance": 'b"c'})
        with self.assertRaises(main.InputError):
            main.expand_values("1x")

    def test_expression_subset_filters_and_aggregates(self):
        store = main.SeriesStore()
        store.add({"__name__": "q", "queue": "a", "i": "1"}, 0, 150)
        store.add({"__name__": "q", "queue": "a", "i": "2"}, 0, 50)
        store.add({"__name__": "q", "queue": "b", "i": "1"}, 0, 10)
        result = main.evaluate(main.parse_expr("max by (queue) (q) > 100"), store, 60)
        self.assertEqual(result, [({"queue": "a"}, 150.0)])
        self.assertEqual(main.evaluate(main.parse_expr("q{i=\"1\"} * 2 > bool 50"), store, 60), [({"i": "1", "queue": "a"}, 1.0), ({"i": "1", "queue": "b"}, 0.0)])
        self.assertEqual(main.evaluate(main.parse_expr("count_over_time(q{queue=\"b\"}[5m])"), store, 60), [({"i": "1", "queue": "b"}, 1.0)])
        for bad in ("1 > 2", "q[5m]", "rate(q[5m])"):
            with self.assertRaises(main.InputError, msg=bad):
                main.evaluate(main.parse_expr(bad), store, 60)

    def test_templates_follow_go_text_template_and_prometheus_functions(self):
        labels = {"job": "checkout", "instance": "a"}
        self.assertEqual(main.expand_template("{{ $labels.job }} fails {{ $value | humanizePercentage }}", labels, 0.08), "checkout fails 8%")
        self.assertEqual(main.expand_template("{{ .Labels.instance }} {{ .Value }}", labels, 0.5), "a 0.5")
        self.assertEqual(main.expand_template('{{ printf "%.2f" $value }} {{ humanize 1234567 }} {{ toUpper "x" }}', labels, 2.0 / 3), "0.67 1.235M X")
        self.assertEqual(main.expand_template("a  {{- $labels.job -}}  b", labels, 0), "acheckoutb")
        self.assertEqual(main.expand_template("{{ $labels.missing }}|{{ $nope }}", labels, 0), '<error expanding template: undefined variable "$nope">')

    def test_go_float_formatting_matches_strconv(self):
        for value, text in ((150.0, "150"), (0.1, "0.1"), (1e21, "1e+21"), (1e-05, "1e-05"), (123456.0, "123456"), (1234567.0, "1.234567e+06"), (float("nan"), "NaN"), (float("-inf"), "-Inf")):
            self.assertEqual(main.go_float(value), text)


class UnitTestFileTests(unittest.TestCase):
    def test_fixture_tests_pass_and_the_wrong_ones_fail_like_promtool(self):
        code, out, _ = run(["rules", "test", RULES_TEST])
        self.assertEqual((code, out.splitlines()[-1]), (0, "  SUCCESS"))
        code, out, _ = run(["rules", "test", RULES_TEST_WRONG])
        self.assertEqual(code, 1)
        self.assertIn("alertname: HighErrorRate, time: 3m", out)
        self.assertIn("alertname: InstanceDown, time: 11m", out)

    def test_emitted_expectations_cover_every_evaluation_and_pass_themselves(self):
        tmp = tempfile.mkdtemp()
        try:
            for name in ("rules.json", "rules-test.json"):
                shutil.copy(os.path.join(FIXTURES, name), tmp)
            out_path = os.path.join(tmp, "every-minute.json")
            code, _, err = run(["rules", "test", os.path.join(tmp, "rules-test.json"), "--emit-expected", out_path])
            self.assertEqual(code, 0)
            emitted = read(out_path)
            self.assertEqual(emitted["rule_files"], ["rules.json"])
            self.assertEqual(len(emitted["tests"][0]["alert_rule_test"]), 31 * 4)
            cases = sum(len(t["alert_rule_test"]) for t in emitted["tests"])
            self.assertIn(f"{cases} alert_rule_test case(s)", err)
            failures, _ = main.run_unit_tests(out_path)
            self.assertEqual(failures, [])
        finally:
            shutil.rmtree(tmp)

    def test_eval_prints_the_lifecycle_with_kept_and_resolved_states(self):
        result = main.rules_eval(RULES_TEST, None, None)
        high = [(t["at"], t["state"]) for t in result["transitions"] if t["alert"] == "HighErrorRate"]
        self.assertEqual(high, [(180, "pending"), (480, "firing"), (660, "firing (kept)"), (720, "firing"), (900, "firing (kept)"), (1500, "resolved")])
        code, out, _ = run(["rules", "eval", RULES_TEST, "--test", "a stale marker makes absent() true at once"])
        self.assertEqual(code, 0)
        self.assertRegex(out, r"6m  TargetMissing +firing")
        code, _, err = run(["rules", "eval", RULES_TEST, "--test", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("no test named 'nope'", err)


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.sent = []
        self.live = main.LiveAlertmanager(config(simple_config()), clock=lambda: self.now[0], send=lambda url, body: self.sent.append((url, body)) or (200, ""))

    def test_live_alertmanager_sends_webhooks_when_the_clock_reaches_a_flush(self):
        self.live.post_alerts([{"labels": {"alertname": "A"}}])
        self.live.flush()
        self.assertEqual(self.sent, [])
        self.now[0] += 10
        self.live.flush()
        self.assertEqual([(url, body["status"], body["groupKey"]) for url, body in self.sent], [("http://hook.invalid/", "firing", '{}:{alertname="A"}')])
        with self.assertRaises(main.InputError):
            self.live.post_alerts({"labels": {}})

    def test_http_api_accepts_alerts_and_silences_and_reports_suppression(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), main.api_handler(self.live))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            self.assertEqual(main.http_request("GET", base + "/-/ready")[0], 200)
            self.assertEqual(main.http_request("POST", base + "/api/v2/alerts", [{"labels": {"alertname": "A", "team": "db"}}])[0], 200)
            status, text = main.http_request("POST", base + "/api/v2/silences", {"matchers": [{"name": "team", "value": "db", "isRegex": False, "isEqual": True}], "startsAt": main.rfc3339(1000.0), "endsAt": main.rfc3339(1600.0), "createdBy": "t", "comment": "c"})
            self.assertEqual(status, 200)
            ident = json.loads(text)["silenceID"]
            alerts = json.loads(main.http_request("GET", base + "/api/v2/alerts")[1])
            self.assertEqual((alerts[0]["status"]["state"], alerts[0]["status"]["silencedBy"]), ("suppressed", [ident]))
            self.assertEqual(main.http_request("DELETE", base + f"/api/v2/silence/{ident}")[0], 200)
            alerts = json.loads(main.http_request("GET", base + "/api/v2/alerts")[1])
            self.assertEqual(alerts[0]["status"]["state"], "active")
            self.assertEqual(main.http_request("POST", base + "/api/v2/alerts", {"not": "a list"})[0], 400)
            self.assertEqual(main.http_request("POST", base + "/api/v2/silences", {"matchers": [], "endsAt": main.rfc3339(10.0)})[0], 400)
            self.assertEqual(main.http_request("GET", base + "/api/v1/alerts")[0], 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_receiver_records_each_post_as_a_json_line(self):
        tmp = tempfile.mkdtemp()
        out_path = os.path.join(tmp, "hooks.jsonl")
        server = main.start_receiver("127.0.0.1:0", out_path)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            self.assertEqual(main.http_request("POST", base + "/pager", {"groupKey": "k"})[0], 200)
            self.assertEqual(json.loads(main.http_request("GET", base + "/")[1]), {"received": 1})
            hooks = main.read_jsonl(out_path)
            self.assertEqual((hooks[0]["path"], hooks[0]["body"]), ("/pager", {"groupKey": "k"}))
        finally:
            server.shutdown()
            server.server_close()
            shutil.rmtree(tmp)


class DriveAndCompareTests(unittest.TestCase):
    def test_drive_plays_steps_on_time_and_uses_amtool_for_silences(self):
        clock = [500.0]
        calls, commands = [], []

        def request(method, url, body=None):
            calls.append((round(clock[0] - 500.0, 3), method, url.split("9093")[1]))
            return 200, "[]" if method == "GET" else ""

        def fake_run(argv, **kwargs):
            commands.append(argv)
            return SimpleNamespace(returncode=0, stdout="3f1c0e6a-silence\n", stderr="")

        def sleep(seconds):
            clock[0] += seconds

        record = main.drive(read(SCENARIO), "http://127.0.0.1:9093/", "amtool --alertmanager.url=http://127.0.0.1:9093", clock=lambda: clock[0], sleep=sleep, request=request, run=fake_run)
        self.assertEqual(record["silences"], {"search-maintenance": "3f1c0e6a-silence"})
        self.assertEqual(commands[0][:4], ["amtool", "--alertmanager.url=http://127.0.0.1:9093", "silence", "add"])
        self.assertIn('service="search"', commands[0])
        self.assertEqual(commands[1][-3:], ["silence", "expire", "3f1c0e6a-silence"])
        self.assertEqual(calls[0], (0.0, "POST", "/api/v2/alerts"))
        self.assertEqual([c[0] for c in calls if c[1] == "GET"], [13.0, 33.0])
        self.assertEqual(clock[0] - 500.0, 34.0)

    def test_drive_reports_an_api_error(self):
        with self.assertRaises(main.InputError) as caught:
            main.drive({"alerts": {}, "steps": [{"at": "0s", "post": [alert("A")]}]}, "http://x", clock=lambda: 0.0, sleep=lambda s: None, request=lambda m, u, b=None: (400, "bad"))
        self.assertIn("answered 400: bad", str(caught.exception))

    def driven_from(self, result, t0=5000.0):
        driven = {"t0": t0, "silences": {"search-maintenance": "uuid-1"}, "snapshots": copy.deepcopy(result["snapshots"])}
        for snap in driven["snapshots"]:
            for a in snap["alerts"]:
                a["status"]["silencedBy"] = ["uuid-1" if s == "search-maintenance" else s for s in a["status"]["silencedBy"]]
        hooks = [{"at": t0 + n["at"] + 0.2, "body": n["payload"]} for n in result["notifications"]]
        return driven, hooks

    def test_compare_accepts_an_identical_run_within_tolerance(self):
        driven, hooks = self.driven_from(SIM)
        lines, problems = main.compare_runs(SIM, driven, hooks)
        self.assertEqual(problems, [])
        self.assertEqual(len([l for l in lines if l.lstrip()[:1].isdigit()]), 12)

    def test_compare_reports_missing_late_and_different_notifications(self):
        driven, hooks = self.driven_from(SIM)
        dropped = hooks.pop(0)
        hooks[0] = dict(hooks[0], at=hooks[0]["at"] + 3)
        hooks[1] = copy.deepcopy(hooks[1])
        hooks[1]["body"]["status"] = "resolved"
        driven["snapshots"][0]["alerts"][0]["status"]["inhibitedBy"] = []
        _, problems = main.compare_runs(SIM, driven, hooks)
        text = "\n".join(problems)
        self.assertIn("missing: " + dropped["body"]["receiver"], text)
        self.assertIn("time (", text)
        self.assertIn("status", text)
        self.assertIn("snapshot cluster-down alert", text)

    def test_compare_cli_exit_codes(self):
        tmp = tempfile.mkdtemp()
        try:
            driven, hooks = self.driven_from(SIM)
            paths = {name: os.path.join(tmp, name) for name in ("sim.json", "driven.json", "hooks.jsonl")}
            with open(paths["sim.json"], "w", encoding="utf-8") as handle:
                json.dump(SIM, handle)
            with open(paths["driven.json"], "w", encoding="utf-8") as handle:
                json.dump(driven, handle)
            with open(paths["hooks.jsonl"], "w", encoding="utf-8") as handle:
                handle.write("".join(json.dumps(h) + "\n" for h in hooks[:-1]))
            code, out, _ = run(["compare", "--simulated", paths["sim.json"], "--driven", paths["driven.json"], "--webhook", paths["hooks.jsonl"]])
            self.assertEqual(code, 1)
            self.assertIn("1 mismatch(es)", out)
        finally:
            shutil.rmtree(tmp)


class CliTests(unittest.TestCase):
    def test_check_config_reports_what_it_found(self):
        code, out, _ = run(["check-config", CONFIG])
        self.assertEqual(code, 0)
        self.assertIn("SUCCESS", out)
        self.assertIn(" - 1 inhibit rules", out)
        self.assertIn(" - 5 receivers", out)
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "bad.json")
            raw = read(CONFIG)
            raw["route"]["routes"][0]["receiver"] = "storage-chat"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(raw, handle)
            code, out, _ = run(["check-config", path])
            self.assertEqual(code, 1)
            self.assertIn('FAILED: undefined receiver "storage-chat" used in route', out)
        finally:
            shutil.rmtree(tmp)

    def test_simulate_text_and_json(self):
        code, out, _ = run(["simulate", SCENARIO, "--config", CONFIG])
        self.assertEqual(code, 0)
        self.assertIn("12.0s  NOTIFY pager {alertname=\"ClusterDown\"} firing (first notification)", out)
        self.assertIn("summary: 12 notification(s) to db-chat, db-pager, default-chat, pager", out)
        code, out, _ = run(["simulate", SCENARIO, "--config", CONFIG, "--json"])
        self.assertEqual(json.loads(out)["notifications"], json.loads(json.dumps(SIM["notifications"])))

    def test_unreadable_or_invalid_input_exits_2(self):
        code, _, err = run(["simulate", "/no/such/file.json", "--config", CONFIG])
        self.assertEqual(code, 2)
        self.assertIn("cannot read /no/such/file.json", err)
        code, _, err = run(["rules", "check", RULES, CONFIG])
        self.assertEqual(code, 2)
        self.assertIn('a rule file is an object with a list of "groups"', err)


if __name__ == "__main__":
    unittest.main()
