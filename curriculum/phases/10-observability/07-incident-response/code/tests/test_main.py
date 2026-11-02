import copy
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from http.server import ThreadingHTTPServer
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
CONFIG_PATH = os.path.join(FIXTURES, "config.json")
SCENARIO_PATH = os.path.join(FIXTURES, "scenario.json")
GK_CHECKOUT = '{}:{alertname="CheckoutHighErrorRate", service="checkout"}'
GK_SEARCH = '{}:{alertname="SearchLatencyHigh", service="search"}'
T0 = main.parse_time("2026-11-02T10:00:00Z")


def config_doc():
    with open(CONFIG_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def service(doc=None):
    return main.IncidentService(main.load_config(doc or config_doc()))


def alert(instance, starts, status="firing", ends="0001-01-01T00:00:00Z", name="CheckoutHighErrorRate", svc="checkout", severity="critical", **labels):
    merged = {"alertname": name, "service": svc, "severity": severity, "instance": instance}
    merged.update(labels)
    return {"status": status, "labels": merged, "annotations": {}, "startsAt": starts, "endsAt": ends, "generatorURL": "", "fingerprint": ""}


def webhook(key, alerts, status="firing", **common):
    labels = dict(alerts[0]["labels"])
    labels.update(common)
    return {"version": "4", "groupKey": key, "status": status, "receiver": "miniincident", "alerts": alerts,
            "groupLabels": {"alertname": labels["alertname"], "service": labels["service"]}, "commonLabels": labels, "commonAnnotations": {}}


def run(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


def kinds(incident):
    return [e.kind for e in sorted(incident.timeline, key=lambda e: (e.at, e.seq))]


class TimeTests(unittest.TestCase):
    def test_times_parse_the_way_alertmanager_writes_them(self):
        self.assertEqual(main.parse_time("2026-11-02T10:00:00Z"), T0)
        self.assertAlmostEqual(main.parse_time("2026-11-02T10:00:00.123456789Z"), T0 + 0.123456789, places=6)
        self.assertEqual(main.parse_time("2026-11-02T11:00:00+01:00"), T0)
        self.assertIsNone(main.parse_time("0001-01-01T00:00:00Z"))
        with self.assertRaises(main.InputError):
            main.parse_time("yesterday")

    def test_durations_and_relative_times(self):
        self.assertEqual(main.parse_duration("1h30m"), 5400)
        self.assertEqual(main.parse_duration("7d"), 604800)
        self.assertEqual(main.parse_duration(90), 90)
        self.assertEqual(main.parse_when("+5m", T0), T0 + 300)
        self.assertEqual(main.human(420), "7m 00s")
        self.assertEqual(main.human(5400), "1h 30m")
        with self.assertRaises(main.InputError):
            main.parse_duration("5 minutes")


class KeyTests(unittest.TestCase):
    def test_group_key_is_the_route_path_plus_sorted_group_labels(self):
        self.assertEqual(main.group_key("{}", {"service": "checkout", "alertname": "CheckoutHighErrorRate"}), GK_CHECKOUT)
        self.assertEqual(main.group_key('{}/{team="payments"}', {"alertname": "X"}), '{}/{team="payments"}:{alertname="X"}')

    def test_dedup_key_is_the_sha256_alertmanager_sends_to_pagerduty(self):
        self.assertEqual(main.dedup_key(GK_CHECKOUT), hashlib.sha256(GK_CHECKOUT.encode()).hexdigest())
        self.assertEqual(main.dedup_key(GK_CHECKOUT), "05e94e9578802de292b3aef8763c31312779b7ce186396f6b2796fa4b6ca4129")


class SeverityTests(unittest.TestCase):
    def test_matrix_and_alert_labels(self):
        config = main.load_config(config_doc())
        self.assertEqual(main.classify(config, "outage", "many"), "SEV1")
        self.assertEqual(main.classify(config, "degraded", "few"), "SEV3")
        self.assertEqual(main.severity_from_labels(config, {"severity": "critical"}), ("SEV2", "alert severity critical"))
        self.assertEqual(main.severity_from_labels(config, {"severity": "critical", "impact": "outage", "scope": "all"})[0], "SEV1")
        self.assertEqual(main.severity_from_labels(config, {})[0], "SEV3")
        with self.assertRaises(main.InputError):
            main.classify(config, "meltdown", "all")

    def test_config_errors_name_the_problem(self):
        doc = config_doc()
        doc["severity_matrix"]["cells"]["outage"]["all"] = "SEV9"
        with self.assertRaisesRegex(main.InputError, "cells.outage.all"):
            main.load_config(doc)
        doc = config_doc()
        doc["escalation_policies"]["checkout"]["levels"][0]["targets"] = ["schedule:nope"]
        with self.assertRaisesRegex(main.InputError, "no schedule 'nope'"):
            main.load_config(doc)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.config = main.load_config(config_doc())
        self.primary = self.config.schedules["checkout-primary"]

    def test_follow_the_sun_layers_cover_their_hours(self):
        self.assertEqual(main.on_call(self.primary, main.parse_time("2026-11-03T03:00:00Z")).user, "ravi")
        self.assertEqual(main.on_call(self.primary, main.parse_time("2026-11-03T09:00:00Z")).user, "bob")
        self.assertEqual(main.on_call(self.primary, main.parse_time("2026-11-03T20:00:00Z")).user, "dana")
        self.assertEqual(main.on_call(self.primary, main.parse_time("2026-10-27T09:00:00Z")).user, "alice")

    def test_override_beats_every_layer_and_rotation_counts_from_the_anchor(self):
        current = main.on_call(self.primary, T0)
        self.assertEqual((current.user, current.source), ("alice", "override"))
        secondary = self.config.schedules["checkout-secondary"]
        self.assertEqual(main.on_call(secondary, main.parse_time("2026-11-02T08:59:00Z")).user, "erin")
        self.assertEqual(main.on_call(secondary, main.parse_time("2026-11-02T09:00:00Z")).user, "frank")
        self.assertIsNone(main.on_call(secondary, main.parse_time("2026-10-01T00:00:00Z")).user)

    def test_rendered_schedule_shows_gaps(self):
        doc = config_doc()
        doc["schedules"]["checkout-primary"]["layers"] = doc["schedules"]["checkout-primary"]["layers"][:2]
        schedule = main.load_config(doc).schedules["checkout-primary"]
        segments = main.render_schedule(schedule, main.parse_time("2026-11-03T00:00:00Z"), main.parse_time("2026-11-04T00:00:00Z"))
        self.assertEqual([(s["user"], main.iso(s["start"])) for s in segments],
                         [("ravi", "2026-11-03T00:00:00Z"), ("bob", "2026-11-03T08:00:00Z"), (None, "2026-11-03T16:00:00Z")])
        code, out, _ = run(["oncall", "--config", CONFIG_PATH, "--schedule", "checkout-primary", "--from", "2026-11-03T00:00:00Z", "--to", "+1d"])
        self.assertEqual(code, 0, out)

    def test_local_time_restrictions_move_with_daylight_saving(self):
        try:
            main.zone("Europe/Berlin")
        except main.InputError:
            self.skipTest("no tz database on this machine")
        doc = config_doc()
        doc["schedules"]["checkout-primary"]["layers"][1]["timezone"] = "Europe/Berlin"
        doc["schedules"]["checkout-primary"]["layers"][1]["restrictions"] = [{"from": "09:00", "to": "17:00"}]
        schedule = main.load_config(doc).schedules["checkout-primary"]
        winter = main.on_call(schedule, main.parse_time("2027-03-26T15:30:00Z"))
        summer = main.on_call(schedule, main.parse_time("2027-03-30T15:30:00Z"))
        self.assertEqual(winter.source, "layer emea")
        self.assertEqual((summer.user, summer.source), (None, "gap"))


class WebhookTests(unittest.TestCase):
    def test_one_incident_per_group_key_with_adds_and_repeats(self):
        svc = service()
        first = svc.receive_alertmanager(webhook(GK_CHECKOUT, [alert("a", "2026-11-02T09:58:00Z")]), T0)
        added = svc.receive_alertmanager(webhook(GK_CHECKOUT, [alert("a", "2026-11-02T09:58:00Z"), alert("b", "2026-11-02T10:01:00Z")]), T0 + 120)
        repeat = svc.receive_alertmanager(webhook(GK_CHECKOUT, [alert("a", "2026-11-02T09:58:00Z"), alert("b", "2026-11-02T10:01:00Z")]), T0 + 180)
        other = svc.receive_alertmanager(webhook(GK_SEARCH, [alert("s", "2026-11-02T09:59:00Z", name="SearchLatencyHigh", svc="search", severity="warning")]), T0 + 60)
        self.assertEqual([r["action"] for r in (first, added, repeat, other)], ["opened", "updated", "repeat", "opened"])
        self.assertEqual([i.id for i in svc.incidents], ["INC-1", "INC-2"])
        incident = svc.incidents[0]
        self.assertEqual((len(incident.alerts), incident.repeats, incident.notifications["alertmanager"]), (2, 1, 3))
        self.assertEqual(incident.impact_start, T0 - 120)
        self.assertEqual(kinds(incident)[:4], ["alert", "opened", "page", "alert.added"])

    def test_resolve_uses_the_alerts_end_time_and_reopen_links_the_old_incident(self):
        svc = service()
        svc.receive_alertmanager(webhook(GK_CHECKOUT, [alert("a", "2026-11-02T09:58:00Z")]), T0)
        result = svc.receive_alertmanager(webhook(GK_CHECKOUT, [alert("a", "2026-11-02T09:58:00Z", "resolved", "2026-11-02T10:20:00Z")], "resolved"), T0 + 1500)
        self.assertEqual(result["action"], "resolved")
        self.assertEqual(svc.incidents[0].resolved_at, T0 + 1200)
        again = svc.receive_alertmanager(webhook(GK_CHECKOUT, [alert("a", "2026-11-02T10:30:00Z")]), T0 + 1800)
        self.assertEqual(again["incident"], "INC-2")
        self.assertEqual(svc.incidents[1].related, ["INC-1"])
        ignored = svc.receive_alertmanager(webhook(GK_SEARCH, [alert("s", "2026-11-02T09:00:00Z", "resolved", "2026-11-02T09:10:00Z", name="SearchLatencyHigh", svc="search")], "resolved"), T0)
        self.assertEqual(ignored["action"], "ignored")

    def test_not_a_webhook_is_rejected(self):
        with self.assertRaisesRegex(main.InputError, "groupKey"):
            service().receive_alertmanager({"alerts": []}, T0)


class PagerDutyTests(unittest.TestCase):
    def test_events_v2_with_the_group_dedup_key_land_on_the_same_incident(self):
        svc = service()
        svc.receive_alertmanager(webhook(GK_CHECKOUT, [alert("a", "2026-11-02T09:58:00Z")]), T0)
        event = {"routing_key": "example", "event_action": "trigger", "dedup_key": main.dedup_key(GK_CHECKOUT),
                 "payload": {"summary": "checkout errors", "source": "Alertmanager", "severity": "critical", "component": "checkout"}}
        self.assertEqual(svc.receive_pagerduty(event, T0 + 1)["dedup_key"], main.dedup_key(GK_CHECKOUT))
        svc.receive_pagerduty({"routing_key": "example", "event_action": "acknowledge", "dedup_key": main.dedup_key(GK_CHECKOUT), "client": "phone"}, T0 + 60)
        self.assertEqual(len(svc.incidents), 1)
        incident = svc.incidents[0]
        self.assertEqual((incident.status, incident.notifications), ("acknowledged", {"alertmanager": 1, "pagerduty": 2}))
        self.assertNotIn("commander", incident.roles)

    def test_trigger_without_a_key_gets_one_and_bad_events_are_refused(self):
        svc = service()
        reply = svc.receive_pagerduty({"routing_key": "k", "event_action": "trigger", "payload": {"summary": "disk full", "source": "db-1", "severity": "error"}}, T0)
        self.assertEqual(len(reply["dedup_key"]), 32)
        self.assertEqual(svc.incidents[0].severity, "SEV3")
        with self.assertRaisesRegex(main.InputError, "payload.severity"):
            svc.receive_pagerduty({"routing_key": "k", "event_action": "trigger", "payload": {"summary": "x"}}, T0)
        with self.assertRaisesRegex(main.InputError, "event_action"):
            svc.receive_pagerduty({"routing_key": "k", "event_action": "page"}, T0)


class EscalationTests(unittest.TestCase):
    def test_a_clock_jump_pages_each_level_at_its_due_time(self):
        svc = service()
        svc.receive_alertmanager(webhook(GK_CHECKOUT, [alert("a", "2026-11-02T09:58:00Z")]), T0)
        events = svc.tick(T0 + 20 * 60)
        self.assertEqual([(e["at"] - T0, e["level"]) for e in events], [(300, 2), (900, 3)])
        self.assertEqual([(p["user"], p["at"] - T0) for p in svc.pages], [("alice", 0), ("frank", 300), ("maria", 900)])

    def test_acknowledgement_stops_escalation_and_repeat_then_exhausts(self):
        svc = service()
        svc.receive_alertmanager(webhook(GK_CHECKOUT, [alert("a", "2026-11-02T09:58:00Z")]), T0)
        svc.tick(T0 + 299)
        svc.command("alice", "/inc ack", T0 + 299)
        self.assertEqual(svc.tick(T0 + 7200), [])
        self.assertEqual(len(svc.pages), 1)
        other = service()
        other.receive_alertmanager(webhook(GK_CHECKOUT, [alert("a", "2026-11-02T09:58:00Z")]), T0)
        events = other.tick(T0 + 7200)
        self.assertEqual([e.get("level", "exhausted") for e in events], [2, 3, 1, 2, 3, "exhausted"])
        self.assertEqual(other.incidents[0].escalation.state, "exhausted")
        self.assertIn("escalation exhausted with nobody acknowledging", other.warnings(other.incidents[0], T0 + 7200))

    def test_a_schedule_gap_escalates_at_once_and_minor_severities_do_not_page(self):
        doc = config_doc()
        doc["schedules"]["checkout-primary"]["overrides"] = []
        doc["schedules"]["checkout-primary"]["layers"] = doc["schedules"]["checkout-primary"]["layers"][:1]
        svc = service(doc)
        svc.receive_alertmanager(webhook(GK_CHECKOUT, [alert("a", "2026-11-02T09:58:00Z")]), T0)
        svc.tick(T0)
        self.assertEqual([p["user"] for p in svc.pages], ["frank"])
        self.assertIn("page.gap", kinds(svc.incidents[0]))
        quiet = service()
        quiet.receive_alertmanager(webhook(GK_SEARCH, [alert("s", "2026-11-02T09:59:00Z", name="SearchLatencyHigh", svc="search", severity="warning")]), T0)
        self.assertEqual((quiet.pages, quiet.incidents[0].escalation.state), ([], "not paged"))
        quiet.command("grace", "/inc sev INC-1 SEV2", T0 + 60)
        self.assertEqual([p["user"] for p in quiet.pages], ["ivan"])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.svc = service()
        self.svc.receive_alertmanager(webhook(GK_CHECKOUT, [alert("a", "2026-11-02T09:58:00Z")]), T0)

    def test_ack_makes_the_first_responder_commander_and_handoffs_are_recorded(self):
        self.assertIn("acknowledged by frank", self.svc.command("@frank", "/inc ack INC-1", T0 + 420))
        incident = self.svc.incidents[0]
        self.assertEqual(incident.roles, {"commander": "frank"})
        self.svc.command("frank", "/inc role INC-1 commander @alice", T0 + 900)
        self.assertEqual(incident.handoffs, [{"role": "commander", "from": "frank", "to": "alice", "at": T0 + 900}])
        self.assertEqual(main.incident_times(incident)["time_to_acknowledge"], 420)

    def test_severity_raises_through_the_matrix_and_warnings_follow_the_contract(self):
        self.svc.command("frank", "/inc ack", T0 + 60)
        self.svc.command("frank", "/inc sev impact=outage scope=all", T0 + 120)
        incident = self.svc.incidents[0]
        self.assertEqual(incident.severity, "SEV1")
        self.svc.command("frank", "/inc role ops @frank", T0 + 130)
        warnings = self.svc.warnings(incident, T0 + 2400)
        self.assertTrue(any("needs comms" in w for w in warnings), warnings)
        self.assertTrue(any("split them" in w for w in warnings), warnings)
        self.assertTrue(any("status page update overdue by 10m 00s" in w for w in warnings), warnings)

    def test_status_updates_drive_the_status_page(self):
        self.svc.command("erin", '/inc status INC-1 investigating "Checkout is failing for some customers"', T0 + 300)
        page = self.svc.status_page()
        self.assertEqual(page["components"], {"Checkout": "partial_outage", "Search": "operational"})
        self.assertEqual(page["incidents"][0]["state"], "investigating")
        self.svc.command("erin", "/inc resolve INC-1 rollback finished", T0 + 1200)
        page = self.svc.status_page()
        self.assertEqual((page["components"]["Checkout"], page["incidents"][0]["state"]), ("operational", "resolved"))

    def test_command_errors_say_what_to_type(self):
        self.svc.command("bob", '/inc open "Payments provider timeouts" service=search sev=SEV2', T0)
        with self.assertRaisesRegex(main.InputError, "which incident.*INC-1, INC-2"):
            self.svc.command("bob", "/inc ack", T0)
        with self.assertRaisesRegex(main.InputError, "unknown role"):
            self.svc.command("bob", "/inc role INC-1 scribe @bob", T0)
        with self.assertRaisesRegex(main.InputError, "unknown command 'reboot'"):
            self.svc.command("bob", "/inc reboot INC-1", T0)
        self.assertIn("/inc open", self.svc.command("bob", "/inc help", T0))


class DeployTests(unittest.TestCase):
    def test_deploys_before_and_during_an_incident_join_its_timeline(self):
        svc = service()
        svc.record_deploy({"service": "checkout", "version": "v2.14.0", "actor": "ci", "at": "2026-11-02T09:52:00Z"}, T0 - 480)
        svc.record_deploy({"service": "checkout", "version": "v2.10.0", "actor": "ci", "at": "2026-11-02T08:00:00Z"}, T0)
        svc.record_deploy({"service": "search", "version": "v9", "actor": "ci", "at": "2026-11-02T09:55:00Z"}, T0)
        svc.receive_alertmanager(webhook(GK_CHECKOUT, [alert("a", "2026-11-02T09:58:00Z")]), T0)
        svc.record_deploy({"service": "checkout", "version": "v2.13.4", "actor": "bob", "kind": "rollback"}, T0 + 600)
        texts = [e.text for e in svc.incidents[0].timeline if e.kind == "deploy"]
        self.assertEqual(texts, ["deploy checkout v2.14.0 by ci (6m 00s before the first alert)", "rollback checkout v2.13.4 by bob (during the incident)"])


class MetricsAndPostmortemTests(unittest.TestCase):
    def setUp(self):
        with open(SCENARIO_PATH, encoding="utf-8") as handle:
            self.scenario = json.load(handle)
        self.svc = service()
        self.log = main.replay(self.svc, self.scenario)

    def test_scenario_metrics(self):
        stats = main.metrics(self.svc.incidents)
        self.assertEqual((stats["incidents"], stats["resolved"], stats["resolved_without_ack"], stats["escalations"]), (2, 2, 1, 1))
        self.assertEqual(stats["mtta"], {"count": 1, "mean": 420.0, "median": 420.0})
        self.assertEqual(stats["mttr"], {"count": 2, "mean": 1275.0, "median": 1275.0})
        self.assertEqual(stats["mttd"]["mean"], 120.0)
        self.assertEqual(stats["by_severity"]["SEV1"]["mttr"]["mean"], 1440.0)
        self.assertEqual([p["user"] for p in self.svc.pages], ["alice", "frank"])

    def test_postmortem_has_every_section_in_order_and_leaves_the_judgement_to_people(self):
        text = main.postmortem(self.svc, self.svc.get("INC-1"), T0 + 3000)
        positions = [text.index(f"## {name}\n") for name in main.POSTMORTEM_SECTIONS]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("| Time to acknowledge | 7m 00s |", text)
        self.assertIn("checkout v2.14.0 was deployed 6m 00s before the first alert", text)
        self.assertIn("| FILL IN | | | |", text)
        self.assertIn('uses "human error"', text)
        self.assertIn("Severity: SEV1 at its highest", text)

    def test_state_round_trip_keeps_everything(self):
        restored = main.IncidentService.from_json(json.loads(json.dumps(self.svc.to_json())))
        self.assertEqual(main.metrics(restored.incidents), main.metrics(self.svc.incidents))
        self.assertEqual(main.postmortem(restored, restored.get("INC-1"), T0), main.postmortem(self.svc, self.svc.get("INC-1"), T0))


class CliTests(unittest.TestCase):
    def test_replay_postmortem_metrics_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state.json")
            code, out, err = run(["replay", SCENARIO_PATH, "--config", CONFIG_PATH, "--state", state, "--json"])
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["metrics"]["mtta"]["mean"], 420.0)
            code, out, _ = run(["postmortem", "--state", state, "--incident", "INC-2"])
            self.assertEqual(code, 0)
            self.assertIn("It resolved before anyone acknowledged it", out)
            code, out, _ = run(["metrics", "--state", state])
            self.assertIn("MTTR  mean  21m 15s", out)
            code, _, err = run(["postmortem", "--state", state, "--incident", "INC-9"])
            self.assertEqual(code, 2)
            self.assertIn("no incident INC-9", err)
        code, out, _ = run(["oncall", "--config", CONFIG_PATH, "--at", "2026-11-02T10:00:00Z", "--json"])
        self.assertEqual([r["user"] for r in json.loads(out)], ["alice", "frank", "ivan"])
        code, out, _ = run(["severity", "--config", CONFIG_PATH, "--impact", "outage", "--scope", "many"])
        self.assertTrue(out.startswith("SEV1"), out)
        code, _, err = run(["replay", os.path.join(FIXTURES, "missing.json"), "--config", CONFIG_PATH])
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.clock = main.Clock(T0)
        self.svc = service()
        handler = main.make_handler(self.svc, self.clock, threading.Lock(), None)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read().decode()
                return response.status, (json.loads(raw) if "json" in response.headers.get("Content-Type", "") else raw)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_webhook_clock_command_and_postmortem_over_http(self):
        self.assertEqual(self.call("/alertmanager", webhook(GK_CHECKOUT, [alert("a", "2026-11-02T09:58:00Z")]))[1]["action"], "opened")
        code, reply = self.call("/v2/enqueue", {"routing_key": "k", "event_action": "trigger", "dedup_key": main.dedup_key(GK_CHECKOUT),
                                                "payload": {"summary": "s", "source": "Alertmanager", "severity": "critical"}})
        self.assertEqual((code, reply["status"]), (202, "success"))
        self.assertEqual(self.call("/clock", {"now": "+6m"})[1]["escalations"][0]["level"], 2)
        self.assertEqual(self.call("/commands", {"user": "frank", "text": "/inc ack INC-1"})[0], 200)
        incident = self.call("/incidents/INC-1")[1]
        self.assertEqual((incident["status"], incident["times"]["time_to_acknowledge"]), ("acknowledged", 360.0))
        self.assertEqual([p["user"] for p in incident["pages"]], ["alice", "frank"])
        code, text = self.call("/incidents/INC-1/postmortem")
        self.assertIn("## Action items", text)
        code, error = self.call("/clock", {"now": "2026-11-01T00:00:00Z"})
        self.assertEqual(code, 400)
        self.assertIn("only moves forward", error["error"])
        self.assertEqual(self.call("/oncall?schedule=checkout-primary&at=2026-11-03T20:00:00Z")[1][0]["user"], "dana")


if __name__ == "__main__":
    unittest.main()
