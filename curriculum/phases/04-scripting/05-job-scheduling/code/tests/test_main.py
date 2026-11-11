import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

UTC = timezone.utc
BERLIN = main.load_zone("Europe/Berlin")
NEW_YORK = main.load_zone("America/New_York")
FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def at(text):
    return main.parse_instant(text, UTC)


def utc_list(runs):
    return [main.format_utc(r.instant) for r in runs]


def run_cli(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


class CronParserTests(unittest.TestCase):
    def test_fields_expand_lists_ranges_steps_and_names(self):
        expr = main.parse_cron("*/15 9-17/4 1,15 jan-mar MON-fri")
        self.assertEqual(expr.minutes, (0, 15, 30, 45))
        self.assertEqual(expr.hours, (9, 13, 17))
        self.assertEqual(expr.days, frozenset({1, 15}))
        self.assertEqual(expr.months, frozenset({1, 2, 3}))
        self.assertEqual(expr.weekdays, frozenset({1, 2, 3, 4, 5}))
        self.assertEqual(main.parse_cron("5/20 * * * 7").minutes, (5, 25, 45))
        self.assertEqual(main.parse_cron("0 0 * * 7").weekdays, frozenset({0}))
        self.assertEqual(main.parse_cron("0 0 * * */2").weekdays, frozenset({0, 2, 4, 6}))

    def test_macros_expand_and_reboot_has_no_next_run(self):
        self.assertEqual(main.parse_cron("@daily").fields, ("0", "0", "*", "*", "*"))
        self.assertEqual(main.parse_cron("@weekly").weekdays, frozenset({0}))
        self.assertEqual(main.parse_cron("@hourly").minutes, (0,))
        with self.assertRaisesRegex(main.ScheduleError, "no next run"):
            main.parse_cron("@reboot")

    def test_bad_expressions_name_the_field_and_the_reason(self):
        cases = {
            "60 * * * *": "minute: 60 is outside 0-59",
            "* 24 * * *": "hour: 24 is outside 0-23",
            "* * 0 * *": "day-of-month: 0 is outside 1-31",
            "* * * 5-2 *": "month: range 5-2 runs backwards",
            "*/0 * * * *": "step '0' must be a positive number",
            "* * * * funday": "day-of-week: 'funday' is not a number or a name",
            "0 30 2 * * *": "six fields",
            "CRON_TZ=UTC 0 * * * *": "--tz",
        }
        for text, message in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(main.ScheduleError) as caught:
                    main.parse_cron(text)
                self.assertIn(message, str(caught.exception))


class DayRuleTests(unittest.TestCase):
    def test_both_day_fields_restricted_means_or(self):
        expr = main.parse_cron("0 0 13 * 5")
        runs = main.next_runs(expr, UTC, at("2026-02-01T00:00Z"), 4)
        self.assertEqual(utc_list(runs), ["2026-02-06T00:00:00Z", "2026-02-13T00:00:00Z", "2026-02-20T00:00:00Z", "2026-02-27T00:00:00Z"])
        march = main.next_runs(expr, UTC, at("2026-03-01T00:00Z"), 3)
        self.assertEqual(utc_list(march), ["2026-03-06T00:00:00Z", "2026-03-13T00:00:00Z", "2026-03-20T00:00:00Z"])

    def test_one_day_field_star_means_only_the_other_counts(self):
        expr = main.parse_cron("0 0 * * 1")
        self.assertEqual(expr.day_rule, "and")
        runs = main.next_runs(expr, UTC, at("2026-01-01T00:00Z"), 2)
        self.assertEqual(utc_list(runs), ["2026-01-05T00:00:00Z", "2026-01-12T00:00:00Z"])

    def test_stepped_star_is_a_star_for_vixie_but_not_for_robfig(self):
        vixie = main.parse_cron("0 0 */2 * 1", "vixie")
        robfig = main.parse_cron("0 0 */2 * 1", "robfig")
        self.assertEqual(vixie.day_rule, "and")
        self.assertEqual(robfig.day_rule, "or")
        start = at("2026-01-01T00:00Z")
        self.assertEqual(utc_list(main.next_runs(vixie, UTC, start, 2)), ["2026-01-05T00:00:00Z", "2026-01-19T00:00:00Z"])
        self.assertEqual(utc_list(main.next_runs(robfig, UTC, start, 3)), ["2026-01-03T00:00:00Z", "2026-01-05T00:00:00Z", "2026-01-07T00:00:00Z"])
        self.assertTrue(main.explain_cron(vixie)["dialects_disagree"])
        self.assertFalse(main.explain_cron(main.parse_cron("0 0 1-7 * 1"))["dialects_disagree"])


class NextRunTests(unittest.TestCase):
    def test_weekday_business_hours_roll_over_the_weekend(self):
        expr = main.parse_cron("*/15 9-17 * * mon-fri")
        runs = main.next_runs(expr, UTC, at("2026-11-13T17:40Z"), 3)
        self.assertEqual(utc_list(runs), ["2026-11-13T17:45:00Z", "2026-11-16T09:00:00Z", "2026-11-16T09:15:00Z"])

    def test_next_is_strictly_after_the_base_and_leap_days_are_found(self):
        expr = main.parse_cron("0 12 * * *")
        self.assertEqual(utc_list(main.next_runs(expr, UTC, at("2026-05-01T12:00Z"), 1)), ["2026-05-02T12:00:00Z"])
        leap = main.next_runs(main.parse_cron("0 0 29 2 *"), UTC, at("2026-01-01T00:00Z"), 2)
        self.assertEqual(utc_list(leap), ["2028-02-29T00:00:00Z", "2032-02-29T00:00:00Z"])
        with self.assertRaisesRegex(main.ScheduleError, "never runs"):
            main.next_runs(main.parse_cron("0 0 30 2 *"), UTC, at("2026-01-01T00:00Z"), 1)

    def test_naive_from_times_are_wall_times_in_the_zone(self):
        self.assertEqual(main.parse_instant("2026-07-01T09:00", BERLIN), at("2026-07-01T07:00Z"))
        self.assertEqual(main.parse_instant("2026-07-01T09:00:00Z", BERLIN), at("2026-07-01T09:00Z"))
        with self.assertRaisesRegex(main.ScheduleError, "ISO 8601"):
            main.parse_instant("yesterday", UTC)


class DaylightSavingTests(unittest.TestCase):
    def test_berlin_spring_gap_fixed_time_job_runs_at_the_end_of_the_gap(self):
        expr = main.parse_cron("30 2 * * *")
        runs = main.next_runs(expr, BERLIN, at("2026-03-27T12:00Z"), 3)
        self.assertEqual(utc_list(runs), ["2026-03-28T01:30:00Z", "2026-03-29T01:00:00Z", "2026-03-30T00:30:00Z"])
        self.assertEqual(runs[1].local.strftime("%H:%M %Z"), "03:00 CEST")
        self.assertIn("gap", runs[1].note)
        skipped = main.next_runs(expr, BERLIN, at("2026-03-27T12:00Z"), 2, gap="skip")
        self.assertEqual(utc_list(skipped), ["2026-03-28T01:30:00Z", "2026-03-30T00:30:00Z"])

    def test_berlin_spring_gap_wildcard_job_skips_the_missing_hour(self):
        runs = main.next_runs(main.parse_cron("0 * * * *"), BERLIN, at("2026-03-28T23:30Z"), 3)
        self.assertEqual([r.local.strftime("%H:%M %Z") for r in runs], ["01:00 CET", "03:00 CEST", "04:00 CEST"])
        shifted = main.next_runs(main.parse_cron("*/15 2 * * *"), BERLIN, at("2026-03-28T22:00Z"), 2, gap="shift", fold="both")
        self.assertEqual(utc_list(shifted), ["2026-03-29T01:00:00Z", "2026-03-30T00:00:00Z"])

    def test_berlin_autumn_fold_fixed_runs_once_wildcard_runs_twice(self):
        fixed = main.next_runs(main.parse_cron("30 2 * * *"), BERLIN, at("2026-10-24T12:00Z"), 2)
        self.assertEqual(utc_list(fixed), ["2026-10-25T00:30:00Z", "2026-10-26T01:30:00Z"])
        both = main.next_runs(main.parse_cron("30 2 * * *"), BERLIN, at("2026-10-24T12:00Z"), 2, fold="both")
        self.assertEqual(utc_list(both), ["2026-10-25T00:30:00Z", "2026-10-25T01:30:00Z"])
        hourly = main.next_runs(main.parse_cron("0 * * * *"), BERLIN, at("2026-10-24T23:30Z"), 3)
        self.assertEqual([r.local.strftime("%H:%M %Z") for r in hourly], ["02:00 CEST", "02:00 CET", "03:00 CET"])

    def test_new_york_transitions_happen_on_other_dates_and_hours(self):
        fold = main.next_runs(main.parse_cron("30 1 * * *"), NEW_YORK, at("2026-10-31T12:00Z"), 3, gap="shift", fold="both")
        self.assertEqual([r.local.isoformat() for r in fold], ["2026-11-01T01:30:00-04:00", "2026-11-01T01:30:00-05:00", "2026-11-02T01:30:00-05:00"])
        gap = main.next_runs(main.parse_cron("30 2 * * *"), NEW_YORK, at("2026-03-07T12:00Z"), 2)
        self.assertEqual([r.local.isoformat() for r in gap], ["2026-03-08T03:00:00-04:00", "2026-03-09T02:30:00-04:00"])
        self.assertEqual(main.next_runs(main.parse_cron("30 2 * * *"), BERLIN, at("2026-03-07T12:00Z"), 1)[0].local.isoformat(), "2026-03-08T02:30:00+01:00")


class CalendarTests(unittest.TestCase):
    def test_normalized_form_matches_systemd_analyze(self):
        cases = {
            "Mon..Fri 09:00": "Mon..Fri *-*-* 09:00:00",
            "daily": "*-*-* 00:00:00",
            "weekly": "Mon *-*-* 00:00:00",
            "quarterly": "*-01,04,07,10-01 00:00:00",
            "*:0/15": "*-*-* *:00/15:00",
            "Mon,Tue,Wed 1:2": "Mon..Wed *-*-* 01:02:00",
            "Sun,Mon": "Mon,Sun *-*-* 00:00:00",
            "Sat..Sun": "Sat,Sun *-*-* 00:00:00",
            "*-*-3,1 0:0": "*-*-01,03 00:00:00",
            "*-*-* 0..23/6:00": "*-*-* 00..18/6:00:00",
            "*-*-* 1..3/1:00": "*-*-* 01..03:00:00",
            "*-02~03": "*-02~03 00:00:00",
            "*-*-* 02:30 Europe/Berlin": "*-*-* 02:30:00 Europe/Berlin",
            "2026..2027-*-* 0:0": "2026..2027-*-* 00:00:00",
        }
        for text, normalized in cases.items():
            with self.subTest(text=text):
                self.assertEqual(main.parse_calendar(text).normalized, normalized)

    def test_next_elapses_match_systemd_analyze(self):
        base = at("2026-01-01T00:00Z")
        cases = {
            "Fri *-*-13 00:00": ["2026-02-13T00:00:00Z", "2026-03-13T00:00:00Z"],
            "*-*~01": ["2026-01-31T00:00:00Z", "2026-02-28T00:00:00Z"],
            "*-02~03": ["2026-02-26T00:00:00Z", "2027-02-26T00:00:00Z"],
            "*-*-* *:*:0/20": ["2026-01-01T00:00:20Z", "2026-01-01T00:00:40Z"],
            "*-2-29 0:0": ["2028-02-29T00:00:00Z", "2032-02-29T00:00:00Z"],
            "Mon *-*-1..7": ["2026-01-05T00:00:00Z", "2026-02-02T00:00:00Z"],
            "*-*-* 02:30 America/New_York": ["2026-01-01T07:30:00Z", "2026-01-02T07:30:00Z"],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                spec = main.parse_calendar(text)
                zone = main.load_zone(spec.zone or "UTC")
                self.assertEqual(utc_list(main.next_runs(spec, zone, base, 2)), expected)

    def test_calendar_dst_default_is_skip_the_gap_and_first_of_the_fold(self):
        spec = main.parse_calendar("*-*-* 02:30")
        spring = main.next_runs(spec, BERLIN, at("2026-03-28T12:00Z"), 1)
        self.assertEqual(utc_list(spring), ["2026-03-30T00:30:00Z"])
        autumn = main.next_runs(main.parse_calendar("hourly"), BERLIN, at("2026-10-24T22:30Z"), 3)
        self.assertEqual([r.local.strftime("%H:%M %Z") for r in autumn], ["01:00 CEST", "02:00 CEST", "03:00 CET"])

    def test_invalid_calendar_specs_are_rejected(self):
        for text in ("Fri..Mon", "*-*-* 25:00", "*-*-* 1-3:00", "Funday 10:00", "10:00 Mon"):
            with self.subTest(text=text):
                with self.assertRaises(main.ScheduleError):
                    main.parse_calendar(text)

    def test_persistent_timer_catches_up_once_after_downtime(self):
        spec = main.parse_calendar("daily")
        activated = at("2026-05-04T08:00Z")
        state = main.timer_activation(spec, UTC, activated, at("2026-05-01T00:00Z"), persistent=True)
        self.assertEqual(state["missed"], 3)
        self.assertEqual(state["last_missed"], "2026-05-04T00:00:00Z")
        self.assertTrue(state["fires_on_activation"])
        self.assertEqual(state["next"], "2026-05-05T00:00:00Z")
        self.assertFalse(main.timer_activation(spec, UTC, activated, at("2026-05-01T00:00Z"), persistent=False)["fires_on_activation"])
        self.assertFalse(main.timer_activation(spec, UTC, activated, None, persistent=True)["fires_on_activation"])
        self.assertFalse(main.timer_activation(spec, UTC, activated, at("2026-05-04T00:00Z"), persistent=True)["fires_on_activation"])


class JitterTests(unittest.TestCase):
    def test_jitter_is_stable_bounded_and_spread(self):
        values = [main.jitter_seconds(f"host-{i} backup", 300) for i in range(200)]
        self.assertEqual(values, [main.jitter_seconds(f"host-{i} backup", 300) for i in range(200)])
        self.assertTrue(all(0 <= v < 300 for v in values))
        self.assertGreater(len(set(values)), 100)
        self.assertEqual(main.jitter_seconds("anything", 0), 0)


class SchedulerTests(unittest.TestCase):
    def job(self, **overrides):
        spec = {"name": "slow", "cron": "* * * * *", "duration": 100, "concurrency": "Forbid"}
        spec.update(overrides)
        return main.load_jobs([spec])

    def events(self, journal, kind):
        return [e["scheduled"][11:16] for e in journal if e["event"] == kind]

    def test_forbid_skips_runs_that_would_overlap(self):
        journal = main.simulate(self.job(), at("2026-01-01T10:00:30Z"), at("2026-01-01T10:04:30Z"))
        self.assertEqual(self.events(journal, "start"), ["10:01", "10:03"])
        self.assertEqual(self.events(journal, "skip"), ["10:02", "10:04"])
        self.assertEqual(self.events(journal, "finish"), ["10:01"])

    def test_allow_overlaps_and_replace_kills_the_previous_run(self):
        allow = main.simulate(self.job(concurrency="Allow"), at("2026-01-01T10:00:30Z"), at("2026-01-01T10:03:30Z"))
        self.assertEqual(self.events(allow, "start"), ["10:01", "10:02", "10:03"])
        replace = main.simulate(self.job(concurrency="Replace"), at("2026-01-01T10:00:30Z"), at("2026-01-01T10:03:30Z"))
        self.assertEqual(self.events(replace, "kill"), ["10:01", "10:02"])
        self.assertEqual(self.events(replace, "start"), ["10:01", "10:02", "10:03"])

    def test_missed_run_policies_after_downtime(self):
        down = [(at("2026-01-01T00:30Z"), at("2026-01-01T03:10Z"))]
        start, end = at("2026-01-01T00:00Z"), at("2026-01-01T03:30Z")
        results = {}
        for policy in ("skip", "once", "all"):
            jobs = main.load_jobs([{"name": "hourly", "cron": "0 * * * *", "duration": 60, "missed": policy}])
            journal = main.simulate(jobs, start, end, down)
            results[policy] = (self.events(journal, "start"), self.events(journal, "miss"))
        self.assertEqual(results["skip"], ([], ["01:00", "02:00", "03:00"]))
        self.assertEqual(results["once"], (["03:00"], ["01:00", "02:00"]))
        self.assertEqual(results["all"], (["01:00", "02:00", "03:00"], []))

    def test_starting_deadline_turns_a_late_catch_up_into_a_miss(self):
        jobs = main.load_jobs([{"name": "nightly", "cron": "0 1 * * *", "duration": 60, "missed": "once", "deadline": 3600}])
        late = main.simulate(jobs, at("2026-01-01T00:00Z"), at("2026-01-01T05:00Z"), [(at("2026-01-01T00:30Z"), at("2026-01-01T04:00Z"))])
        self.assertEqual(self.events(late, "start"), [])
        self.assertIn("starting deadline", [e for e in late if e["event"] == "miss"][0]["reason"])
        early = main.simulate(jobs, at("2026-01-01T00:00Z"), at("2026-01-01T05:00Z"), [(at("2026-01-01T00:30Z"), at("2026-01-01T01:30Z"))])
        self.assertEqual([(e["time"], e["event"]) for e in early if e["event"] == "start"], [("2026-01-01T01:30:00Z", "start")])

    def test_jitter_delays_starts_within_the_bound_and_fixture_loads(self):
        jobs = self.job(concurrency="Allow", jitter=40, duration=1)
        journal = main.simulate(jobs, at("2026-01-01T10:00:30Z"), at("2026-01-01T10:10:30Z"), seed="t")
        starts = [e for e in journal if e["event"] == "start"]
        self.assertEqual(len(starts), 10)
        for entry in starts:
            delay = (at(entry["time"]) - at(entry["scheduled"])).total_seconds()
            self.assertTrue(0 <= delay < 40, entry)
        with open(os.path.join(FIXTURES, "jobs.json"), encoding="utf-8") as handle:
            self.assertEqual(len(main.load_jobs(json.load(handle))), 5)
        with self.assertRaisesRegex(main.ScheduleError, "concurrency"):
            self.job(concurrency="Sometimes")


class LockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_flock_is_exclusive_until_released_and_records_the_owner(self):
        path = os.path.join(self.tmp.name, "job.lock")
        first = main.acquire_flock(path)
        self.assertIsNotNone(first)
        self.assertIsNone(main.acquire_flock(path))
        self.assertEqual(main.read_lock_owner(path)[0], os.getpid())
        os.close(first)
        second = main.acquire_flock(path)
        self.assertIsNotNone(second)
        os.close(second)
        self.assertTrue(os.path.exists(path))

    def test_pidfile_stale_detection(self):
        self.assertEqual(main.pidfile_state(4242, 1000, 1010, None, alive=lambda pid: True), "held")
        self.assertEqual(main.pidfile_state(4242, 1000, 1010, None, alive=lambda pid: False), "stale")
        self.assertEqual(main.pidfile_state(4242, 1000, 5000, 3600, alive=lambda pid: True), "stale")
        self.assertEqual(main.pidfile_state(None, None, 1010, None), "stale")
        path = os.path.join(self.tmp.name, "job.pid")
        with open(path, "w") as handle:
            handle.write("4242 1000\n")
        self.assertEqual(main.acquire_pidfile(path, None, alive=lambda pid: True), (False, "held by pid 4242"))
        acquired, note = main.acquire_pidfile(path, None, alive=lambda pid: False)
        self.assertTrue(acquired)
        self.assertIn("stale lock left by pid 4242", note)
        self.assertEqual(main.read_lock_owner(path)[0], os.getpid())

    def test_run_refuses_a_busy_lock_with_exit_75_and_journals_it(self):
        lock = os.path.join(self.tmp.name, "job.lock")
        journal = os.path.join(self.tmp.name, "runs.jsonl")
        held = main.acquire_flock(lock)
        self.addCleanup(os.close, held)
        code, out, err = run_cli(["run", "--name", "backup", "--lock", lock, "--journal", journal, "--", sys.executable, "-c", "print('ran')"])
        self.assertEqual(code, 75)
        self.assertIn("is busy", err)
        with open(journal) as handle:
            self.assertEqual(json.loads(handle.readline())["outcome"], "locked")

    def test_run_passes_the_exit_code_and_enforces_the_timeout(self):
        journal = os.path.join(self.tmp.name, "runs.jsonl")
        self.assertEqual(main.run_job([sys.executable, "-c", "raise SystemExit(3)"], "fails", journal=journal, err=StringIO()), 3)
        err = StringIO()
        self.assertEqual(main.run_job([sys.executable, "-c", "import time; time.sleep(30)"], "hangs", timeout=0.5, journal=journal, err=err), 124)
        self.assertIn("timed out", err.getvalue())
        with open(journal) as handle:
            outcomes = [json.loads(line)["outcome"] for line in handle]
        self.assertEqual(outcomes, ["failed", "timeout"])

    def test_child_inherits_the_flock_so_the_lock_outlives_a_killed_wrapper(self):
        lock = os.path.join(self.tmp.name, "job.lock")
        marker = os.path.join(self.tmp.name, "child-checked")
        probe = (
            "import fcntl, os, sys\n"
            "fd = os.open(sys.argv[1], os.O_RDWR)\n"
            "try:\n"
            "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    result = 'free'\n"
            "except OSError:\n"
            "    result = 'held'\n"
            "open(sys.argv[2], 'w').write(result)\n"
        )
        code = main.run_job([sys.executable, "-c", probe, lock, marker], "probe", lock=lock, err=StringIO())
        self.assertEqual(code, 0)
        with open(marker) as handle:
            self.assertEqual(handle.read(), "held")


class CliTests(unittest.TestCase):
    def test_next_json_has_utc_local_and_dst_notes(self):
        code, out, err = run_cli(["next", "30 2 * * *", "--tz", "Europe/Berlin", "--from", "2026-03-28T12:00Z", "-n", "2", "--json"])
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual((data["gap"], data["fold"]), ("shift", "first"))
        self.assertEqual(data["runs"][0]["utc"], "2026-03-29T01:00:00Z")
        self.assertEqual(data["runs"][0]["local"], "2026-03-29T03:00:00+02:00")
        self.assertIn("gap", data["runs"][0]["note"])

    def test_errors_go_to_stderr_with_exit_2_and_never_runs_is_exit_1(self):
        code, out, err = run_cli(["next", "61 * * * *"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("minute: 61 is outside 0-59", err)
        code, out, err = run_cli(["next", "* * * * *", "--tz", "Mars/Olympus"])
        self.assertEqual(code, 2)
        self.assertIn("unknown time zone", err)
        code, out, err = run_cli(["next", "0 0 31 4 *"])
        self.assertEqual((code, out), (1, ""))

    def test_calendar_text_mimics_systemd_analyze_and_reports_catch_up(self):
        code, out, _ = run_cli(["calendar", "Mon..Fri 09:00", "--base-time", "2026-11-11T08:00:00Z", "--iterations", "2", "--last-trigger", "2026-11-09T09:00:00Z", "--persistent"])
        self.assertEqual(code, 0)
        self.assertIn("Normalized form: Mon..Fri *-*-* 09:00:00", out)
        self.assertIn("    Next elapse: Wed 2026-11-11 09:00:00 UTC", out)
        self.assertIn("   Iteration #2: Thu 2026-11-12 09:00:00 UTC", out)
        self.assertIn("1 elapse(s) missed", out)

    def test_simulate_and_dry_run_are_machine_readable(self):
        code, out, _ = run_cli(["simulate", "--jobs", os.path.join(FIXTURES, "jobs.json"), "--from", "2026-03-27T23:00Z", "--to", "2026-03-28T04:00Z", "--down", "2026-03-28T00:10Z/2026-03-28T03:05Z", "--json"])
        self.assertEqual(code, 0)
        summary = json.loads(out)["summary"]
        self.assertEqual(summary["backup"]["start"], 1)
        self.assertEqual(summary["rollup"]["start"], 5)
        self.assertGreater(summary["sync"]["skip"], 0)
        code, out, _ = run_cli(["run", "--dry-run", "--json", "--jitter", "60", "--lock", "/nonexistent/x.lock", "--", "false"])
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertEqual(plan["command"], ["false"])
        self.assertTrue(0 <= plan["delay"] < 60)

    def test_explain_states_the_day_rule(self):
        code, out, _ = run_cli(["explain", "0 0 */2 * 1"])
        self.assertEqual(code, 0)
        self.assertIn("AND", out)
        self.assertIn("different days", out)


if __name__ == "__main__":
    unittest.main()
