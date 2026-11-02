import io
import json
import os
import random
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
SAMPLE = os.path.join(FIXTURES, "sample.prom")
OPENMETRICS = os.path.join(FIXTURES, "openmetrics.txt")
BAD = os.path.join(FIXTURES, "bad-histogram.prom")
CONFIG = os.path.join(FIXTURES, "prometheus.json")


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def labels(**items):
    return main.freeze(items)


class Clock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeTargets:
    def __init__(self, pages):
        self.pages = pages

    def __call__(self, url, timeout):
        page = self.pages.get(url)
        if page is None:
            raise main.ScrapeError(f'Get "{url}": connection refused')
        if isinstance(page, tuple):
            return page
        return main.TEXT_CONTENT_TYPE, page.encode()


def demo_config(**job):
    doc = {"job_name": "demo", "static_configs": [{"targets": ["a:80"]}]}
    doc.update(job)
    return main.load_config({"global": {"scrape_interval": "15s", "scrape_timeout": "10s"}, "scrape_configs": [doc]})


def scraper_for(pages, clock=None, **job):
    targets, _ = main.discover(demo_config(**job))
    db = main.TSDB()
    return main.Scraper(db, targets, FakeTargets(pages), clock or Clock()), db, targets[0]


class ExpositionTests(unittest.TestCase):
    def test_text_format_families_escapes_and_timestamps(self):
        exposition = main.parse_exposition(read(SAMPLE))
        families = {f.name: f for f in exposition.families}
        self.assertEqual([(f.name, f.type, len(f.samples)) for f in exposition.families], [
            ("http_requests_total", "counter", 2),
            ("msdos_file_access_time_seconds", "gauge", 1),
            ("metric_without_timestamp_and_labels", "gauge", 1),
            ("something_weird", "untyped", 1),
            ("http_request_duration_seconds", "histogram", 8),
            ("rpc_duration_seconds", "summary", 7),
        ])
        self.assertEqual(families["http_requests_total"].help, "Total HTTP requests.\nThe second line has a backslash: \\")
        self.assertEqual(families["http_requests_total"].samples[1].timestamp_ms, 1395066363000)
        msdos = families["msdos_file_access_time_seconds"].samples[0]
        self.assertEqual(msdos.labels, {"path": "C:\\DIR\\FILE.TXT", "error": 'Cannot find file:\n"FILE.TXT"'})
        weird = families["something_weird"].samples[0]
        self.assertEqual((weird.value, weird.timestamp_ms), (float("inf"), -3982045))
        self.assertEqual(main.validate(exposition), [])

    def test_le_and_quantile_are_normalised_like_prometheus_3(self):
        exposition = main.parse_exposition(read(SAMPLE))
        buckets = [s.labels["le"] for s in exposition.families[4].samples if s.name.endswith("_bucket")]
        self.assertEqual(buckets, ["0.05", "0.1", "0.2", "0.5", "1.0", "+Inf"])
        untyped = main.parse_exposition('weird{le="1"} 3\n')
        self.assertEqual(untyped.samples()[0].labels, {"le": "1"})
        cases = {1: "1.0", 0: "0.0", -1: "-1.0", 10: "10.0", 0.25: "0.25", 1e6: "1e+06", 123456.0: "123456.0",
                 100000.0: "100000.0", 1e-05: "1e-05", 0.0001: "0.0001", float("inf"): "+Inf", float("-inf"): "-Inf"}
        for value, text in cases.items():
            self.assertEqual(main.format_openmetrics_float(value), text, value)
        self.assertEqual([main.format_value(v) for v in (100.0, 1e-07, 2.5, -0.0, float("nan"))], ["100", "0.0000001", "2.5", "-0", "NaN"])

    def test_openmetrics_counters_created_exemplars_and_seconds(self):
        exposition = main.parse_exposition(read(OPENMETRICS), main.OPENMETRICS)
        families = {f.name: f for f in exposition.families}
        self.assertEqual(families["process_cpu_seconds"].type, "counter")
        self.assertEqual([s.name for s in families["process_cpu_seconds"].samples], ["process_cpu_seconds_total", "process_cpu_seconds_created"])
        self.assertEqual(families["process_cpu_seconds"].unit, "seconds")
        self.assertIn('"quoted"', families["process_cpu_seconds"].help)
        bucket = families["request_duration_seconds"].samples[2]
        self.assertEqual((bucket.labels["le"], bucket.value), ("1.0", 11.0))
        self.assertEqual(bucket.exemplar.labels, {"trace_id": "0d482-ac43e"})
        self.assertEqual((bucket.exemplar.value, bucket.exemplar.timestamp), (0.52, 1520879607.789))
        self.assertEqual(families["build"].type, "info")
        self.assertEqual(main.detect_format(read(OPENMETRICS)), main.OPENMETRICS)
        stamped = main.parse_exposition("# TYPE g gauge\ng 1 1520879607.789\n# EOF\n", main.OPENMETRICS)
        self.assertEqual(stamped.samples()[0].timestamp_ms, 1520879607789)

    def test_openmetrics_is_strict(self):
        broken = {
            "# TYPE g gauge\ng 1\n": "must end with # EOF",
            "# TYPE g gauge\ng 1\n\n# EOF\n": "empty lines",
            "# TYPE a gauge\na 1\n# TYPE b gauge\nb 1\na 2\n# EOF\n": "contiguous",
            "# TYPE c counter\nc 1\n# EOF\n": "needs one of the suffixes",
            "# a free comment\n# EOF\n": "free comments",
            "# TYPE g gauge\ng 1\n# EOF\ng 2\n": "after # EOF",
            '# TYPE g gauge\ng{a="x\\ty"} 1\n# EOF\n': "invalid escape",
            '# TYPE g gauge\ng 1 # {trace_id="x"} 1\n# EOF\n': "exemplars are only allowed",
        }
        for text, words in broken.items():
            with self.assertRaises(main.ParseError, msg=text) as caught:
                main.parse_exposition(text, main.OPENMETRICS)
            self.assertIn(words, str(caught.exception))

    def test_text_format_errors_name_the_line(self):
        broken = {
            "ok 1\nmissing_value\n": ("line 2", "a value"),
            'bad{a=b} 1\n': ("line 1", "quoted value"),
            'bad{a="b} 1\n': ("line 1", "unterminated"),
            "bad 1 2 3\n": ("line 1", "after the timestamp"),
            "bad one\n": ("line 1", "invalid value"),
            "bad 1 # {a=\"b\"} 1\n": ("line 1", "exemplars need the OpenMetrics format"),
            "# TYPE x counter\n# TYPE x gauge\n": ("line 2", "second TYPE"),
            "x 1\n# TYPE x counter\n": ("line 2", "TYPE after its samples"),
            '# TYPE x weird\n': ("line 1", "invalid metric type"),
            'dup{a="1",a="2"} 1\n': ("line 1", "duplicate label name"),
            'x 1_000\n': ("line 1", "invalid value"),
        }
        for text, (line, words) in broken.items():
            with self.assertRaises(main.ParseError, msg=text) as caught:
                main.parse_exposition(text)
            self.assertTrue(str(caught.exception).startswith(line), str(caught.exception))
            self.assertIn(words, str(caught.exception))

    def test_validate_reports_broken_histograms_and_counters(self):
        problems = main.validate(main.parse_exposition(read(BAD)))
        self.assertEqual(len(problems), 4, problems)
        self.assertIn('has no le="+Inf" bucket', problems[0])
        self.assertIn("buckets are cumulative", problems[1])
        self.assertIn("_count is 3 but the +Inf bucket holds 2", problems[2])
        self.assertIn("counter jobs_failed_total is negative", problems[3])
        duplicate = main.validate(main.parse_exposition('x{a="1"} 1\nx{a="1"} 2\n'))
        self.assertEqual(duplicate, ['line 2: duplicate series x{a="1"} (first on line 1)'])

    def test_lint_reports_what_promtool_check_metrics_reports(self):
        problems = main.lint(main.parse_exposition(main.render_demo(10)))
        self.assertEqual(problems, [
            ("cc_build_info", "no help text"),
            ("cc_cache_latency_milliseconds", 'use base unit "seconds" instead of "milliseconds"'),
            ("cc_jobs_processed", 'counter metrics should have "_total" suffix'),
        ])
        messy = "\n".join([
            "# HELP fooBar_total x", "# TYPE fooBar_total counter", 'fooBar_total{someLabel="1"} 1',
            "# HELP a:b x", "# TYPE a:b gauge", "a:b 1",
            "# HELP thing_count x", "# TYPE thing_count gauge", 'thing_count{le="1"} 1',
            "# HELP size_gauge x", "# TYPE size_gauge gauge", "size_gauge 1",
            "# HELP latency_ms x", "# TYPE latency_ms gauge", "latency_ms 1",
            "# HELP same x", "# TYPE same gauge", 'same{a="1"} 1', 'same{a="1"} 2',
            "# HELP queue_length_total x", "# TYPE queue_length_total gauge", "queue_length_total 1",
        ]) + "\n"
        found = main.lint(main.parse_exposition(messy))
        self.assertEqual(found, sorted(found))
        self.assertIn(("fooBar_total", "metric names should be written in 'snake_case' not 'camelCase'"), found)
        self.assertIn(("fooBar_total", "label names should be written in 'snake_case' not 'camelCase'"), found)
        self.assertIn(("a:b", "metric names should not contain ':'"), found)
        self.assertIn(("thing_count", 'non-histogram and non-summary metrics should not have "_count" suffix'), found)
        self.assertIn(("thing_count", 'non-histogram metrics should not have "le" label'), found)
        self.assertIn(("size_gauge", "metric name should not include type 'gauge'"), found)
        self.assertIn(("latency_ms", "metric names should not contain abbreviated units"), found)
        self.assertIn(("same", "metric not unique"), found)
        self.assertIn(("queue_length_total", 'non-counter metrics should not have "_total" suffix'), found)


class RelabelTests(unittest.TestCase):
    def rule(self, **doc):
        return main.RelabelRule.from_dict(doc)

    def test_replace_uses_go_template_rules(self):
        rule = self.rule(source_labels=["__address__"], regex=r"(?P<host>[^:]+):(\d+)", target_label="where", replacement="${host}-$2")
        self.assertEqual(main.relabel({"__address__": "web:8080"}, [rule])["where"], "web-8080")
        greedy = self.rule(source_labels=["a"], regex="(.*)", target_label="b", replacement="$1x")
        self.assertNotIn("b", main.relabel({"a": "v"}, [greedy]))
        braced = self.rule(source_labels=["a"], regex="(.*)", target_label="b", replacement="${1}x $$1")
        self.assertEqual(main.relabel({"a": "v"}, [braced])["b"], "vx $1")
        joined = self.rule(source_labels=["a", "missing", "c"], target_label="j")
        self.assertEqual(main.relabel({"a": "1", "c": "3"}, [joined])["j"], "1;;3")
        unanchored = self.rule(source_labels=["a"], regex="b", target_label="hit", replacement="yes")
        self.assertNotIn("hit", main.relabel({"a": "abc"}, [unanchored]))
        constant = self.rule(target_label="env", replacement="prod")
        self.assertEqual(main.relabel({}, [constant]), {"env": "prod"})

    def test_empty_replacement_deletes_the_label(self):
        rule = self.rule(source_labels=["drop_me"], regex=".*", target_label="drop_me", replacement="")
        self.assertEqual(main.relabel({"drop_me": "x", "keep": "y"}, [rule]), {"keep": "y"})

    def test_keep_drop_labelmap_labeldrop_hashmod_and_equality(self):
        keep = self.rule(source_labels=["__name__"], regex="cc_.*", action="keep")
        drop = self.rule(source_labels=["__name__"], regex="cc_debug_.*", action="drop")
        self.assertIsNone(main.relabel({"__name__": "go_goroutines"}, [keep]))
        self.assertIsNone(main.relabel({"__name__": "cc_debug_x"}, [keep, drop]))
        self.assertIsNotNone(main.relabel({"__name__": "cc_requests_total"}, [keep, drop]))
        labelmap = self.rule(action="labelmap", regex="__meta_kubernetes_pod_label_(.+)")
        mapped = main.relabel({"__meta_kubernetes_pod_label_app": "web", "__meta_kubernetes_namespace": "prod"}, [labelmap])
        self.assertEqual(mapped["app"], "web")
        self.assertNotIn("namespace", mapped)
        dropped = main.relabel({"a": "1", "pod_uid": "x", "b": "2"}, [self.rule(action="labeldrop", regex="pod_uid|b")])
        self.assertEqual(dropped, {"a": "1"})
        kept = main.relabel({"a": "1", "b": "2"}, [self.rule(action="labelkeep", regex="a")])
        self.assertEqual(kept, {"a": "1"})
        shard = self.rule(source_labels=["__address__"], action="hashmod", modulus=4, target_label="__tmp_shard")
        values = {main.relabel({"__address__": f"10.0.0.{n}:9100"}, [shard])["__tmp_shard"] for n in range(40)}
        self.assertEqual(values, {"0", "1", "2", "3"})
        self.assertEqual(main.relabel({"__address__": "a:1"}, [shard]), main.relabel({"__address__": "a:1"}, [shard]))
        same = self.rule(source_labels=["a"], target_label="b", action="keepequal")
        self.assertIsNotNone(main.relabel({"a": "x", "b": "x"}, [same]))
        self.assertIsNone(main.relabel({"a": "x", "b": "y"}, [same]))
        lowered = main.relabel({"a": "MiXed"}, [self.rule(source_labels=["a"], target_label="a", action="lowercase")])
        self.assertEqual(lowered["a"], "mixed")

    def test_invalid_rules_are_rejected_with_the_reason(self):
        with self.assertRaisesRegex(main.InputError, "unknown relabel action"):
            self.rule(action="rename")
        with self.assertRaisesRegex(main.InputError, "requires 'target_label'"):
            self.rule(source_labels=["a"])
        with self.assertRaisesRegex(main.InputError, "invalid relabel regex"):
            self.rule(action="drop", regex="(")
        with self.assertRaisesRegex(main.InputError, "unknown relabel field"):
            self.rule(action="drop", regexp="x")


class TargetTests(unittest.TestCase):
    def test_discovery_relabels_defaults_instance_and_drops(self):
        active, dropped = main.discover(main.read_config(CONFIG))
        self.assertEqual([t.labels for t in active], [
            {"env": "lab", "instance": "127.0.0.1:18481", "job": "demo", "port": "18481", "team": "payments"},
            {"env": "lab", "instance": "127.0.0.1:18482", "job": "demo", "port": "18482", "team": "payments"},
            {"instance": "127.0.0.1:18481", "job": "demo-honor"},
        ])
        self.assertEqual(active[0].url, "http://127.0.0.1:18481/metrics")
        self.assertEqual(active[0].discovered["__scrape_interval__"], "2s")
        self.assertEqual(active[0].discovered["__meta_team"], "payments")
        self.assertEqual([(job, labels["__address__"]) for job, labels in dropped], [("demo", "127.0.0.1:9")])
        document = main.targets_document(active, dropped)["data"]
        self.assertEqual(len(document["activeTargets"]), 3)
        self.assertEqual(document["droppedTargets"][0]["discoveredLabels"]["env"], "skip")

    def test_config_durations_and_errors(self):
        self.assertEqual([main.parse_duration(t) for t in ("15s", "1m30s", "500ms", "2h", "15d", "0")], [15000, 90000, 500, 7200000, 1296000000, 0])
        self.assertEqual([main.format_duration(ms) for ms in (2000, 60000, 90000, 500, 1296000000, 31536000000)], ["2s", "1m", "1m30s", "500ms", "15d", "1y"])
        job = demo_config(scrape_interval="5s")[0]
        self.assertEqual((job.interval_ms, job.timeout_ms), (5000, 5000))
        with self.assertRaisesRegex(main.InputError, "scrape timeout greater than scrape interval"):
            demo_config(scrape_interval="5s", scrape_timeout="6s")
        with self.assertRaisesRegex(main.InputError, "multiple scrape configs"):
            main.load_config({"scrape_configs": [{"job_name": "a"}, {"job_name": "a"}]})
        with self.assertRaisesRegex(main.InputError, "not a Prometheus duration"):
            main.parse_duration("5 minutes")
        warnings = []
        main.load_config({"scrape_configs": [{"job_name": "k", "kubernetes_sd_configs": []}]}, warnings.append)
        self.assertIn("kubernetes_sd_configs", warnings[0])
        targets, _ = main.discover(demo_config(metrics_path="/probe", params={"module": ["http_2xx"]}))
        self.assertEqual(targets[0].url, "http://a:80/probe?module=http_2xx")


class ScrapeTests(unittest.TestCase):
    def test_scrape_appends_samples_and_the_report_series(self):
        scraper, db, target = scraper_for({"http://a:80/metrics": 'requests_total{code="200"} 5\nrequests_total{code="500"} 1\n'})
        report = scraper.scrape(target)
        self.assertTrue(report.up)
        self.assertEqual((report.scraped, report.post_relabel, report.series_added), (2, 2, 2))
        names = sorted({dict(s.labels)["__name__"] for s in db.series.values()})
        self.assertEqual(names, sorted(["requests_total"] + list(main.REPORT_METRICS)))
        up = db.instant(main.parse_selector('up{job="demo",instance="a:80"}'), report.timestamp_ms)
        self.assertEqual(up[0][2], 1.0)

    def test_honor_labels_false_renames_conflicts_to_exported(self):
        page = 'build_info{job="batch",instance="x",exported_job="old"} 1\n'
        scraper, db, target = scraper_for({"http://a:80/metrics": page})
        scraper.scrape(target)
        series = [dict(s.labels) for s in db.series.values() if dict(s.labels)["__name__"] == "build_info"][0]
        self.assertEqual(series["job"], "demo")
        self.assertEqual(series["instance"], "a:80")
        self.assertEqual(series["exported_instance"], "x")
        self.assertEqual(series["exported_job"], "old")
        self.assertEqual(series["exported_exported_job"], "batch")

    def test_honor_labels_true_keeps_the_scraped_values(self):
        scraper, db, target = scraper_for({"http://a:80/metrics": 'build_info{job="batch"} 1\n'}, honor_labels=True)
        scraper.scrape(target)
        series = [dict(s.labels) for s in db.series.values() if dict(s.labels)["__name__"] == "build_info"][0]
        self.assertEqual((series["job"], series["instance"]), ("batch", "a:80"))

    def test_metric_relabel_drop_changes_post_relabel_count(self):
        page = "keep_total 1\ndebug_a 1\ndebug_b 2\n"
        rules = [{"source_labels": ["__name__"], "regex": "debug_.*", "action": "drop"}]
        scraper, db, target = scraper_for({"http://a:80/metrics": page}, metric_relabel_configs=rules)
        report = scraper.scrape(target)
        self.assertEqual((report.scraped, report.post_relabel), (3, 1))
        self.assertEqual(db.select(main.parse_selector('{__name__=~"debug_.*"}')), [])

    def test_a_series_that_disappears_gets_a_staleness_marker(self):
        clock = Clock()
        pages = {"http://a:80/metrics": 'queue_depth{queue="batch"} 17\nqueue_depth{queue="default"} 4\n'}
        scraper, db, target = scraper_for(pages, clock)
        scraper.scrape(target)
        clock.sleep(15)
        pages["http://a:80/metrics"] = 'queue_depth{queue="default"} 4\n'
        report = scraper.scrape(target)
        self.assertEqual(report.stale, 1)
        batch = db.select(main.parse_selector('queue_depth{queue="batch"}'))[0]
        self.assertEqual([bits for _, bits in batch.samples()][-1], main.STALE_NAN)
        now = report.timestamp_ms + 1000
        self.assertEqual(db.instant(main.parse_selector('queue_depth{queue="batch"}'), now), [])
        self.assertEqual(len(db.instant(main.parse_selector('queue_depth{queue="default"}'), now)), 1)
        self.assertEqual(len(db.instant(main.parse_selector('queue_depth{queue="batch"}'), report.timestamp_ms - 1)), 1)

    def test_explicit_timestamps_are_honoured_and_not_marked_stale(self):
        clock = Clock()
        pages = {"http://a:80/metrics": "federated 3 1699999990000\n"}
        scraper, db, target = scraper_for(pages, clock)
        scraper.scrape(target)
        pages["http://a:80/metrics"] = ""
        report = scraper.scrape(target)
        self.assertEqual(report.stale, 0)
        federated = db.select(main.parse_selector("federated"))[0]
        self.assertEqual(federated.samples(), [(1699999990000, main.float_bits(3.0))])

    def test_a_failed_scrape_reports_up_zero_and_marks_everything_stale(self):
        clock = Clock()
        pages = {"http://a:80/metrics": "a 1\nb 2\n"}
        scraper, db, target = scraper_for(pages, clock)
        scraper.scrape(target)
        clock.sleep(15)
        del pages["http://a:80/metrics"]
        report = scraper.scrape(target)
        self.assertFalse(report.up)
        self.assertIn("connection refused", report.error)
        self.assertEqual((report.stale, report.scraped), (2, 0))
        self.assertEqual(target.health, "down")
        values = {dict(l)["__name__"]: v for l, _, v in db.instant(main.parse_selector('{job="demo"}'), report.timestamp_ms)}
        self.assertEqual(values["up"], 0.0)
        self.assertNotIn("a", values)

    def test_prometheus_3_rejects_unknown_content_types_unless_a_fallback_is_set(self):
        pages = {"http://a:80/metrics": ("application/json", b"a 1\n")}
        scraper, _, target = scraper_for(pages)
        report = scraper.scrape(target)
        self.assertFalse(report.up)
        self.assertIn('unsupported Content-Type "application/json"', report.error)
        scraper, _, target = scraper_for(pages, fallback_scrape_protocol="PrometheusText0.0.4")
        self.assertTrue(scraper.scrape(target).up)
        scraper, _, target = scraper_for({"http://a:80/metrics": ("", b"a 1\n")})
        self.assertIn("blank Content-Type", scraper.scrape(target).error)
        scraper, _, target = scraper_for({"http://a:80/metrics": ("application/openmetrics-text; version=1.0.0", b"a 1\n")})
        self.assertIn("# EOF", scraper.scrape(target).error)

    def test_a_scrape_slower_than_the_timeout_fails(self):
        clock = Clock()

        def slow(url, timeout):
            clock.sleep(timeout + 0.5)
            return main.TEXT_CONTENT_TYPE, b"a 1\n"

        targets, _ = main.discover(demo_config(scrape_timeout="2s"))
        report = main.Scraper(main.TSDB(), targets, slow, clock).scrape(targets[0])
        self.assertEqual((report.up, report.error), (False, "context deadline exceeded"))
        self.assertGreater(report.duration, 2)

    def test_run_scrapes_each_target_on_its_interval(self):
        clock = Clock()
        doc = {"scrape_configs": [
            {"job_name": "fast", "scrape_interval": "5s", "static_configs": [{"targets": ["f:1"]}]},
            {"job_name": "slow", "scrape_interval": "15s", "static_configs": [{"targets": ["s:1"]}]},
        ]}
        targets, _ = main.discover(main.load_config(doc))
        pages = {"http://f:1/metrics": "x 1\n", "http://s:1/metrics": "y 1\n"}
        db = main.TSDB()
        reports = []
        total = main.Scraper(db, targets, FakeTargets(pages), clock).run(duration=30, sleep=clock.sleep, report=reports.append)
        jobs = [r.target.job for r in reports]
        self.assertEqual((total, jobs.count("fast"), jobs.count("slow")), (8, 6, 2))
        fast = db.select(main.parse_selector('x{job="fast"}'))[0].samples()
        self.assertEqual([b - a for (a, _), (b, _) in zip(fast, fast[1:])], [5000] * 5)


class ChunkTests(unittest.TestCase):
    def test_varints_and_crc32c_match_go_and_the_published_check_value(self):
        self.assertEqual([main.encode_varint(x).hex() for x in (0, -1, 1, -2, 63, -64, 64)], ["00", "01", "02", "03", "7e", "7f", "8001"])
        self.assertEqual(main.encode_uvarint(300).hex(), "ac02")
        for value in (0, 1, -1, 300, -300, 2 ** 62, -(2 ** 62)):
            data = main.encode_varint(value)
            self.assertEqual(main.read_varint_from(main.Cursor(data).byte), value)
        self.assertEqual(main.crc32c(b"123456789"), 0xE3069283)
        self.assertEqual(main.crc32c(b""), 0)

    def test_gorilla_round_trips_irregular_timestamps_and_special_values(self):
        rng = random.Random(7)
        chunk, points, t = main.XORChunk(), [], 1_700_000_000_000
        specials = [main.STALE_NAN, main.NORMAL_NAN, main.float_bits(float("inf")), main.float_bits(-0.0)]
        for i in range(120):
            t += rng.choice([15000, 15000, 15001, 14999, 30000, 1, 8193, 70000, 10 ** 9])
            bits = specials[i % 4] if i % 17 == 0 else main.float_bits(rng.choice([1.0, 2.5, float(i), -3.25, 1e300, 0.0]))
            points.append((t, bits))
            chunk.append(t, bits)
        self.assertEqual(main.decode_chunk(chunk.to_bytes()), points)

    def test_a_regular_constant_series_costs_two_bits_per_sample(self):
        chunk = main.XORChunk()
        costs = [chunk.append(1000 + 15000 * i, main.float_bits(1.0)) for i in range(120)]
        self.assertEqual(costs[0], (16, 64))
        self.assertTrue(all(cost == (1, 1) for cost in costs[2:]))
        self.assertLessEqual(len(chunk.to_bytes()), 50)

    def test_delta_of_delta_buckets_use_prometheus_bit_widths(self):
        def cost_of(dod):
            chunk = main.XORChunk()
            chunk.append(0, 0)
            chunk.append(15000, 0)
            return chunk.append(30000 + dod, 0)[0]

        self.assertEqual([cost_of(d) for d in (0, 1, -1, 8192, -8191, 8193, -8192, 65536, 65537, 524288, 524289)],
                         [1, 16, 16, 16, 16, 20, 20, 20, 24, 24, 68])

    def test_xor_reuses_the_previous_window_when_it_fits(self):
        chunk = main.XORChunk()
        chunk.append(0, main.float_bits(10.0))
        first = chunk.append(1000, main.float_bits(11.0))[1]
        second = chunk.append(2000, main.float_bits(10.0))[1]
        self.assertEqual(first, 1 + 1 + 5 + 6 + (64 - main.leading_zeros(main.float_bits(10.0) ^ main.float_bits(11.0)) - main.trailing_zeros(main.float_bits(10.0) ^ main.float_bits(11.0))))
        self.assertLess(second, first)

    def test_series_cut_a_new_chunk_every_120_samples(self):
        series = main.Series(1, labels(__name__="x"))
        for i in range(250):
            series.append(i * 1000, main.float_bits(float(i)))
        self.assertEqual([c.count for c in series.all_chunks()], [120, 120, 10])
        self.assertEqual(len(series.samples()), 250)
        self.assertEqual(series.samples(100_000, 102_000), [(t * 1000, main.float_bits(float(t))) for t in (100, 101, 102)])


class WALTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wal_dir = os.path.join(self.tmp.name, "wal")

    def tearDown(self):
        self.tmp.cleanup()

    def test_records_round_trip_across_pages_and_segments(self):
        series = [(n, labels(__name__="m", user=f"u{n:05d}")) for n in range(1, 3001)]
        samples = [(n, 1_700_000_000_000 + n, main.float_bits(n / 3)) for n in range(1, 3001)]
        wal = main.WAL(self.wal_dir, segment_size=64 * 1024)
        big_series, big_samples = main.encode_series_record(series), main.encode_samples_record(samples)
        self.assertGreater(len(big_series), main.PAGE_SIZE)
        for _ in range(3):
            wal.log(big_series)
            wal.log(big_samples)
        wal.close()
        replay = main.read_wal(self.wal_dir)
        self.assertIsNone(replay.problem)
        self.assertGreater(replay.segments, 1)
        self.assertEqual(replay.records, [big_series, big_samples] * 3)
        self.assertEqual(main.decode_record(big_series), (main.RECORD_SERIES, series))
        self.assertEqual(main.decode_record(big_samples), (main.RECORD_SAMPLES, samples))
        for _, path in main.wal_segments(self.wal_dir):
            self.assertLessEqual(os.path.getsize(path), 64 * 1024 + main.PAGE_SIZE)

    def test_a_torn_tail_is_ignored_on_replay_and_truncated_on_repair(self):
        db = main.TSDB.open(self.tmp.name, writable=True)
        for i in range(5):
            db.commit([(labels(__name__="x"), i * 1000, main.float_bits(float(i)))])
        db.wal.close()
        segment = main.wal_segments(self.wal_dir)[-1][1]
        size = os.path.getsize(segment)
        with open(segment, "r+b") as handle:
            handle.truncate(size - 3)
        reopened = main.TSDB.open(self.tmp.name)
        self.assertIn("torn record", reopened.replay_problem)
        self.assertEqual(len(next(iter(reopened.series.values())).samples()), 4)
        repaired = main.TSDB.open(self.tmp.name, writable=True)
        self.assertIn("truncated", repaired.replay_problem)
        repaired.commit([(labels(__name__="x"), 9000, main.float_bits(9.0))])
        repaired.wal.close()
        clean = main.TSDB.open(self.tmp.name)
        self.assertEqual(clean.replay_problem, "")
        self.assertEqual([t for t, _ in next(iter(clean.series.values())).samples()], [0, 1000, 2000, 3000, 9000])

    def test_a_flipped_byte_is_a_checksum_mismatch(self):
        wal = main.WAL(self.wal_dir)
        wal.log(main.encode_samples_record([(1, 0, 0)]))
        wal.log(main.encode_samples_record([(1, 1, 0)]))
        wal.close()
        segment = main.wal_segments(self.wal_dir)[0][1]
        with open(segment, "r+b") as handle:
            data = bytearray(handle.read())
            data[-1] ^= 0xFF
            handle.seek(0)
            handle.write(bytes(data))
        replay = main.read_wal(self.wal_dir)
        self.assertEqual(len(replay.records), 1)
        self.assertEqual(replay.problem.reason, "checksum mismatch")

    def test_reopening_replays_series_and_samples_and_starts_a_new_segment(self):
        db = main.TSDB.open(self.tmp.name, writable=True)
        db.commit([(labels(__name__="a", k="1"), 1000, main.float_bits(1.0)), (labels(__name__="b"), 1000, main.STALE_NAN)])
        db.wal.close()
        again = main.TSDB.open(self.tmp.name, writable=True)
        self.assertEqual(sorted(again.refs), [labels(__name__="a", k="1"), labels(__name__="b")])
        self.assertEqual(again.series[again.refs[labels(__name__="b")]].samples(), [(1000, main.STALE_NAN)])
        again.commit([(labels(__name__="c"), 2000, main.float_bits(2.0))])
        again.wal.close()
        self.assertEqual(len(main.wal_segments(self.wal_dir)), 2)
        self.assertEqual(again.refs[labels(__name__="c")], 3)


class TSDBTests(unittest.TestCase):
    def store(self):
        db = main.TSDB()
        rows = []
        for job, instance in (("api", "a:1"), ("api", "b:1"), ("web", "c:1")):
            for code in ("200", "500"):
                rows.append((labels(__name__="http_requests_total", job=job, instance=instance, code=code), 1000, main.float_bits(1.0)))
        db.commit(rows)
        return db

    def test_select_intersects_postings_then_applies_regex_matchers(self):
        db = self.store()
        self.assertEqual(len(db.postings[("job", "api")]), 4)
        self.assertEqual(len(db.select(main.parse_selector('http_requests_total{job="api"}'))), 4)
        self.assertEqual(len(db.select(main.parse_selector('{job="api",code=~"5.."}'))), 2)
        self.assertEqual(len(db.select(main.parse_selector('http_requests_total{job!="api",instance!~"a.*"}'))), 2)
        self.assertEqual(len(db.select(main.parse_selector('{__name__="http_requests_total",missing=""}'))), 6)
        with self.assertRaisesRegex(main.InputError, "at least one matcher"):
            main.parse_selector('{job=~".*",instance!="x"}')
        with self.assertRaisesRegex(main.InputError, "matcher"):
            main.parse_selector("up{job}")

    def test_instant_queries_use_the_lookback_window(self):
        db = main.TSDB()
        db.commit([(labels(__name__="g"), 1000, main.float_bits(5.0))])
        self.assertEqual(db.instant(main.parse_selector("g"), 1000 + main.LOOKBACK_MS - 1)[0][2], 5.0)
        self.assertEqual(db.instant(main.parse_selector("g"), 1000 + main.LOOKBACK_MS), [])
        self.assertEqual(db.instant(main.parse_selector("g"), 999), [])

    def test_out_of_order_and_duplicate_samples_are_rejected(self):
        db = main.TSDB()
        key = labels(__name__="x")
        db.commit([(key, 2000, main.float_bits(1.0))])
        result = db.commit([(key, 1000, main.float_bits(1.0)), (key, 2000, main.float_bits(2.0)), (key, 2000, main.float_bits(1.0)), (key, 3000, main.float_bits(3.0))])
        self.assertEqual((result.appended, result.out_of_order, result.duplicates), (1, 1, 1))

    def test_retention_drops_whole_chunks_and_empty_series(self):
        db = main.TSDB()
        for i in range(300):
            rows = [(labels(__name__="alive"), i * 1000, main.float_bits(1.0))]
            if i < 100:
                rows.append((labels(__name__="gone"), i * 1000, main.float_bits(1.0)))
            db.commit(rows)
        chunks, series = db.apply_retention(250_000)
        self.assertEqual((chunks, series), (3, 1))
        alive = db.series[db.refs[labels(__name__="alive")]]
        self.assertEqual(alive.samples()[0][0], 240_000)
        self.assertNotIn(("__name__", "gone"), db.postings)

    def test_cardinality_ranks_metrics_labels_and_flags_unbounded_labels(self):
        db = main.TSDB()
        rows = [(labels(__name__="logins_total", user_id=f"u{n}", instance=i), 0, 0) for n in range(30) for i in ("a", "b")]
        rows += [(labels(__name__="up", instance=i), 0, 0) for i in ("a", "b")]
        rows += [(labels(__name__="sessions", session="s1"), 0, 0)]
        db.commit(rows)
        report = db.cardinality(top=3)
        self.assertEqual(report["total_series"], 63)
        self.assertEqual(report["metrics"], [{"name": "logins_total", "series": 60}, {"name": "up", "series": 2}, {"name": "sessions", "series": 1}])
        self.assertEqual(report["labels"][0], {"label": "user_id", "values": 30})
        self.assertEqual([o["label"] for o in report["offenders"]], ["user_id", "session"])
        self.assertEqual(report["offenders"][0]["series_per_value"], 2.0)
        self.assertEqual(report["offenders"][0]["metrics"], ["logins_total"])


class DemoAndCLITests(unittest.TestCase):
    def test_demo_page_has_known_series_and_hides_one_on_request(self):
        exposition = main.parse_exposition(main.render_demo(12.5))
        self.assertEqual(len(exposition.samples()), 69)
        self.assertEqual(main.validate(exposition), [])
        values = {main.render_series(dict(s.labels, __name__=s.name)): s.value for s in exposition.samples()}
        self.assertEqual(values['cc_http_requests_total{code="200", method="GET"}'], 125)
        self.assertEqual(values['cc_queue_depth{queue="email \\"bulk\\""}'], 0)
        hidden = main.parse_exposition(main.render_demo(12.5, {"batch"}))
        self.assertEqual(len(hidden.samples()), 68)

    def test_scraping_the_demo_server_over_http(self):
        server = main.make_demo_server("127.0.0.1", 0, main.DemoApp())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
                self.assertEqual(response.headers["Content-Type"], main.TEXT_CONTENT_TYPE)
            doc = {"global": {"scrape_interval": "1s"}, "scrape_configs": [{"job_name": "demo", "static_configs": [{"targets": [f"127.0.0.1:{port}", "127.0.0.1:1"]}],
                   "metric_relabel_configs": [{"source_labels": ["__name__"], "regex": "cc_debug_.*", "action": "drop"}]}]}
            targets, _ = main.discover(main.load_config(doc))
            db = main.TSDB()
            scraper = main.Scraper(db, targets)
            live, dead = scraper.scrape(targets[0]), scraper.scrape(targets[1])
            self.assertEqual((live.up, live.scraped, live.post_relabel), (True, 69, 65))
            self.assertFalse(dead.up)
            with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/hide?queue=batch", method="POST"), timeout=5) as response:
                self.assertEqual(response.status, 200)
            again = scraper.scrape(targets[0])
            self.assertEqual((again.scraped, again.stale), (68, 1))
        finally:
            server.shutdown()
            server.server_close()

    def test_cli_parse_lint_targets_and_encode(self):
        code, out, _ = run("parse", SAMPLE, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["families"][4]["samples"][4]["labels"]["le"], "1.0")
        self.assertEqual(run("parse", BAD)[0], 1)
        code, _, err = run("parse", os.path.join(FIXTURES, "missing.prom"))
        self.assertEqual(code, 2)
        self.assertIn("No such file", err)
        with tempfile.NamedTemporaryFile("w", suffix=".prom", delete=False) as handle:
            handle.write(main.render_demo(1))
        try:
            code, out, _ = run("lint", handle.name)
            self.assertEqual(code, 1)
            self.assertEqual(out.splitlines()[0], "cc_build_info no help text")
        finally:
            os.unlink(handle.name)
        code, out, _ = run("targets", "--config", CONFIG, "--json")
        self.assertEqual(len(json.loads(out)["data"]["droppedTargets"]), 1)
        code, out, _ = run("encode", "0:1", "15000:1", "30000:1", "45001:2", "--json")
        doc = json.loads(out)
        self.assertEqual([r["timestamp_bits"] for r in doc["samples"]], [8, 16, 1, 16])
        self.assertEqual(doc["decoded"][-1], [45001, "2"])
        self.assertEqual(run("encode", "10:1", "5:1")[0], 2)

    def test_cli_reads_a_data_directory_like_the_http_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = main.TSDB.open(tmp, writable=True)
            db.commit([(labels(__name__="up", job="demo", instance="a:1"), 1_700_000_000_000, main.float_bits(1.0)),
                       (labels(__name__="gone", job="demo"), 1_700_000_000_000, main.STALE_NAN)])
            db.wal.close()
            code, out, _ = run("query", "--data", tmp, '{job="demo"}', "--time", "1700000001", "--json")
            result = json.loads(out)["data"]["result"]
            self.assertEqual((code, result), (0, [{"metric": {"__name__": "up", "instance": "a:1", "job": "demo"}, "value": [1700000001.0, "1"]}]))
            code, out, _ = run("series", "--data", tmp, "--json")
            self.assertEqual(len(json.loads(out)["data"]), 2)
            code, out, _ = run("dump", "--data", tmp, "--match", "gone", "--json")
            self.assertEqual(json.loads(out)[0]["samples"], [[1_700_000_000_000, "NaN", True]])
            code, out, _ = run("cardinality", "--data", tmp, "--json")
            self.assertEqual(json.loads(out)["total_series"], 2)
            self.assertEqual(run("chunks", "--data", tmp)[0], 0)
            code, _, err = run("query", "--data", os.path.join(tmp, "nowhere"), "up")
            self.assertEqual(code, 2)
            self.assertIn("no wal/ directory", err)


if __name__ == "__main__":
    unittest.main()
