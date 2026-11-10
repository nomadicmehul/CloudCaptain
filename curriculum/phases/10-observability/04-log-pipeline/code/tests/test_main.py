import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

NS = main.NS
T0 = main.parse_rfc3339("2026-11-02T09:00:00Z")
SEQ = re.compile(r"\bseq\"?\s*[:=]\s*(\d+)")


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def run(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class Recorder:
    def __init__(self, statuses=None, store=None):
        self.statuses = list(statuses or [])
        self.store = store
        self.calls = []

    def __call__(self, url, data, headers, timeout):
        self.calls.append((url, json.loads(data)))
        if self.statuses:
            status = self.statuses.pop(0)
            if isinstance(status, Exception):
                raise status
            return status, "simulated"
        if self.store is not None:
            return self.store.push(json.loads(data))
        return 204, ""


class TempDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="minilog-test-")
        self.warnings = []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.dir, name)

    def write(self, name, *lines, mode="a"):
        with open(self.path(name), mode, encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")

    def source(self, pattern="app.log*", **kwargs):
        return main.Source(self.path(pattern), {"app": "test"}, kwargs.pop("stages", []), **kwargs)

    def tailer(self, *sources, positions=None):
        return main.Tailer(list(sources) or [self.source()], positions, warn=self.warnings.append)

    def texts(self, reads):
        return [line.text for _, lines in reads for line in lines]


class TailTests(TempDir):
    def test_complete_lines_are_read_once_and_a_partial_line_waits_for_its_newline(self):
        self.write("app.log", "one", "two")
        with open(self.path("app.log"), "a") as handle:
            handle.write("thr")
        tail = self.tailer()
        self.assertEqual(self.texts(tail.poll()), ["one", "two"])
        self.assertEqual(self.texts(tail.poll()), [])
        with open(self.path("app.log"), "a") as handle:
            handle.write("ee\n")
        reads = tail.poll()
        self.assertEqual(self.texts(reads), ["three"])
        self.assertEqual(reads[0][1][0].end, os.path.getsize(self.path("app.log")))
        tail.close_all()

    def test_positions_are_keyed_by_device_and_inode_and_resume_after_a_restart(self):
        self.write("app.log", "a1", "a2")
        positions = self.path("positions.json")
        tail = self.tailer(positions=positions)
        tail.poll()
        for state in tail.states.values():
            state.committed = state.offset
        tail.save()
        tail.close_all()
        st = os.stat(self.path("app.log"))
        saved = load_json(positions)["files"]
        self.assertEqual(list(saved), [f"{st.st_dev}:{st.st_ino}"])
        self.assertEqual(saved[f"{st.st_dev}:{st.st_ino}"]["offset"], st.st_size)
        self.write("app.log", "a3")
        again = self.tailer(positions=positions)
        self.assertEqual(self.texts(again.poll()), ["a3"])
        again.close_all()

    def test_rename_rotation_finishes_the_renamed_file_then_reads_the_new_one(self):
        self.write("app.log", "1", "2")
        tail = self.tailer()
        self.assertEqual(self.texts(tail.poll()), ["1", "2"])
        late = open(self.path("app.log"), "a")
        os.replace(self.path("app.log"), self.path("app.log.1"))
        late.write("3\n")
        late.close()
        self.write("app.log", "4", "5")
        self.assertEqual(sorted(self.texts(tail.poll())), ["3", "4", "5"])
        self.assertEqual(tail.stats["renamed"], 1)
        self.assertEqual(tail.stats["files"], 2)
        origins = {s.path: s.origin for s in tail.states.values()}
        self.assertEqual(origins[self.path("app.log.1")], self.path("app.log"))
        tail.close_all()

    def test_a_file_renamed_out_of_the_glob_is_drained_through_the_open_descriptor(self):
        self.write("app.log", "1")
        tail = self.tailer(self.source("app.log"))
        tail.poll()
        self.write("app.log", "2")
        with open(self.path("app.log"), "a") as handle:
            handle.write("no newline")
        os.replace(self.path("app.log"), self.path("old.log"))
        self.assertEqual(self.texts(tail.poll()), ["2", "no newline"])
        self.assertEqual(tail.stats["closed"], 1)
        self.assertEqual(len(tail.retired), 1)
        self.assertEqual(tail.states, {})

    def test_copytruncate_resumes_in_the_copy_at_the_old_offset_without_duplicates(self):
        self.write("app.log", *[f"entry {i} " + "x" * 40 for i in range(1, 4)])
        tail = self.tailer()
        self.assertEqual(len(self.texts(tail.poll())), 3)
        self.write("app.log", "entry 4 unread before the copy " + "y" * 40)
        shutil.copyfile(self.path("app.log"), self.path("app.log.1"))
        os.truncate(self.path("app.log"), 0)
        self.write("app.log", "entry 5 after truncation " + "z" * 40)
        texts = self.texts(tail.poll())
        self.assertEqual(sorted(t.split()[1] for t in texts), ["4", "5"])
        self.assertEqual((tail.stats["truncated"], tail.stats["copies"], tail.stats["lost_bytes"]), (1, 1, 0))
        copy = next(s for s in tail.states.values() if s.path.endswith(".1"))
        self.assertEqual(copy.origin, self.path("app.log"))
        tail.close_all()

    def test_a_copy_seen_before_the_truncate_is_held_until_the_original_is_truncated(self):
        self.write("app.log", *[f"line {i} " + "q" * 60 for i in range(1, 3)])
        tail = self.tailer()
        tail.poll()
        self.write("app.log", "line 3 " + "q" * 60)
        shutil.copyfile(self.path("app.log"), self.path("app.log.1"))
        self.assertEqual(self.texts(tail.poll()), ["line 3 " + "q" * 60])
        self.assertEqual(tail.stats["pending_copies"], 1)
        os.truncate(self.path("app.log"), 0)
        self.write("app.log", "line 4 new content")
        self.assertEqual(self.texts(tail.poll()), ["line 4 new content"])
        self.assertEqual(tail.stats["copies"], 1)
        tail.close_all()

    def test_truncation_without_a_matching_copy_warns_about_the_lost_bytes(self):
        self.write("app.log", "first line long enough to fingerprint " + "w" * 40)
        tail = self.tailer(self.source("app.log"))
        tail.poll()
        self.write("app.log", "written but never read before the truncate")
        tail.states[next(iter(tail.states))].size = os.path.getsize(self.path("app.log"))
        os.truncate(self.path("app.log"), 0)
        self.write("app.log", "after")
        self.assertEqual(self.texts(tail.poll()), ["after"])
        self.assertEqual(tail.stats["lost_bytes"], len("written but never read before the truncate\n"))
        self.assertIn("truncated", self.warnings[0])
        tail.close_all()

    def test_a_stored_position_whose_fingerprint_no_longer_matches_is_a_new_file(self):
        self.write("app.log", "brand new content on a recycled inode")
        st = os.stat(self.path("app.log"))
        positions = self.path("positions.json")
        stale = {"path": self.path("app.log"), "origin": self.path("app.log"), "offset": 20, "prefix": "b2xkIGNvbnRlbnQ="}
        with open(positions, "w") as handle:
            json.dump({"version": 1, "files": {f"{st.st_dev}:{st.st_ino}": stale}}, handle)
        tail = self.tailer(positions=positions)
        self.assertEqual(self.texts(tail.poll()), ["brand new content on a recycled inode"])
        self.assertEqual(tail.stats["truncated"], 1)
        tail.close_all()

    def test_copytruncate_while_the_shipper_is_down_resumes_from_the_copy(self):
        lines = [f"entry {i} " + "c" * 50 for i in range(1, 5)]
        self.write("app.log", *lines[:2])
        positions = self.path("positions.json")
        tail = self.tailer(positions=positions)
        tail.poll()
        for state in tail.states.values():
            state.committed = state.offset
        tail.save()
        tail.close_all()
        self.write("app.log", lines[2])
        shutil.copyfile(self.path("app.log"), self.path("app.log.1"))
        os.truncate(self.path("app.log"), 0)
        self.write("app.log", lines[3])
        again = self.tailer(positions=positions)
        self.assertEqual(sorted(self.texts(again.poll())), [lines[2], lines[3]])
        again.close_all()

    def test_compressed_rotations_and_directories_are_ignored(self):
        self.write("app.log", "live")
        self.write("app.log.2.gz", "binary")
        os.mkdir(self.path("app.log.d"))
        tail = self.tailer()
        self.assertEqual(self.texts(tail.poll()), ["live"])
        tail.close_all()

    def test_fingerprints_compare_as_prefixes(self):
        old = b"x" * 100
        self.assertEqual(main.compare_prefix(old[:70], old), "same")
        self.assertEqual(main.compare_prefix(old[:10], old), "maybe")
        self.assertEqual(main.compare_prefix(b"y" + old[:69], old), "different")
        self.assertEqual(main.compare_prefix(b"", old), "different")
        self.assertEqual(main.compare_prefix(b"short and complete", b"short"), "same")


def shipper(test, config, transport=None, store=None, clock=None, positions=None, **kwargs):
    tail = main.Tailer(config.sources, positions, warn=test.warnings.append)
    pusher = main.Pusher(["http://loki.test"], transport=transport or Recorder(store=store), sleep=lambda s: None, warn=test.warnings.append)
    return main.Shipper(config, tail, pusher, clock=clock or Clock(), wall=lambda: 7 * NS, warn=test.warnings.append, **kwargs)


class MultilineTests(TempDir):
    def config(self, **multiline):
        source = self.source(firstline=re.compile("^ts="), **multiline)
        return main.Config({"job": "t"}, [source], filename_label=False, batch_wait=0)

    def test_a_traceback_becomes_one_entry_when_the_next_firstline_arrives(self):
        self.write("app.log", "ts=1 boom", "Traceback (most recent call last):", '  File "x.py", line 1', "ValueError: bad", "ts=2 ok")
        store = main.Store()
        ship = shipper(self, self.config(), store=store)
        ship.cycle()
        rows = store.read(next(iter(store.streams.values())), 0, 10**20, main.new_qstats())
        self.assertEqual([r[1] for r in rows], ["ts=1 boom\nTraceback (most recent call last):\n  File \"x.py\", line 1\nValueError: bad"])
        self.assertEqual(ship.tailer.states[next(iter(ship.tailer.states))].block[0].text, "ts=2 ok")
        ship.stop()

    def test_a_pending_block_flushes_after_max_wait_and_is_never_committed_before(self):
        self.write("app.log", "ts=1 first", "ts=2 second", "  continued")
        positions = self.path("positions.json")
        clock = Clock(0.0)
        store = main.Store()
        ship = shipper(self, self.config(max_wait=2.0), store=store, clock=clock, positions=positions)
        ship.cycle()
        ship.stop()
        state = load_json(positions)["files"]
        self.assertEqual(list(state.values())[0]["offset"], len("ts=1 first\n"))
        again = shipper(self, self.config(max_wait=2.0), store=store, clock=clock, positions=positions)
        again.cycle()
        clock.now = 2.5
        again.cycle()
        rows = store.read(next(iter(store.streams.values())), 0, 10**20, main.new_qstats())
        self.assertEqual([r[1] for r in rows], ["ts=1 first", "ts=2 second\n  continued"])
        again.stop()

    def test_max_lines_starts_a_new_block(self):
        self.write("app.log", "ts=1", "a", "b", "c")
        store = main.Store()
        ship = shipper(self, self.config(max_lines=2), store=store)
        ship.cycle(final=True)
        rows = store.read(next(iter(store.streams.values())), 0, 10**20, main.new_qstats())
        self.assertEqual(sorted(r[1] for r in rows), ["b\nc", "ts=1\na"])


class ParserTests(unittest.TestCase):
    def test_json_flattens_objects_sanitizes_keys_keeps_number_text_and_skips_arrays(self):
        line = '{"status": 502, "took": 1.50, "ok": false, "req": {"user-agent": "curl", "2xx": "no"}, "1st": "x", "tags": ["a"], "none": null}'
        self.assertEqual(main.flat_json(line), {"status": "502", "took": "1.50", "ok": "false", "req_user_agent": "curl", "req_2xx": "no", "_1st": "x"})
        with self.assertRaises(ValueError):
            main.flat_json("[1, 2]")
        with self.assertRaises(ValueError):
            main.flat_json("not json")

    def test_logfmt_follows_lokis_decoder_on_quotes_escapes_bare_keys_and_garbage(self):
        line = 'ts=2026 level=info msg="charge \\"ok\\"" empty= bare  url=/a?b=c weird"key=1 status=200 path=/x'
        self.assertEqual(main.logfmt_pairs(line), [("ts", "2026"), ("level", "info"), ("msg", 'charge "ok"'), ("empty", ""), ("bare", ""), ("status", "200"), ("path", "/x")])
        self.assertEqual(main.flat_logfmt("a=1 b= c=x\nTraceback (most recent call last):"), {"a": "1", "c": "x"})

    def test_regexp_parser_needs_named_groups_and_accepts_go_style_names(self):
        with self.assertRaises(main.InputError):
            main.compile_named(r"(\d+)", "regexp")
        regex = main.compile_named(r"status=(?<status>\d+)", "regexp")
        self.assertEqual(regex.search("x status=503 y").group("status"), "503")


class StageTests(TempDir):
    def test_json_timestamp_labels_and_structured_metadata_build_the_entry(self):
        self.write("app.log", json.dumps({"ts": "2026-11-02T09:00:00.250Z", "level": "error", "trace_id": "abc", "user_id": "u1"}))
        stages = [main.check_stage(s, "t") for s in ({"type": "json"}, {"type": "timestamp", "source": "ts", "format": "RFC3339"}, {"type": "labels", "names": ["level"]}, {"type": "structured_metadata", "names": ["trace_id"]})]
        recorder = Recorder()
        ship = shipper(self, main.Config({"job": "t"}, [self.source(stages=stages)], batch_wait=0), transport=recorder)
        ship.cycle()
        stream = recorder.calls[0][1]["streams"][0]
        self.assertEqual(stream["stream"], {"app": "test", "filename": self.path("app.log"), "job": "t", "level": "error"})
        self.assertEqual(stream["values"][0][0], str(T0 + 250 * 10**6))
        self.assertEqual(stream["values"][0][2], {"trace_id": "abc"})
        ship.stop()

    def test_drop_and_limit_stages_skip_entries_but_still_commit_their_offsets(self):
        self.write("app.log", "level=debug n=1", "level=info n=2", "level=info n=3", "level=info n=4")
        stages = [main.check_stage(s, "t") for s in ({"type": "logfmt"}, {"type": "drop", "source": "level", "value": "debug"}, {"type": "limit", "rate": 1, "burst": 2})]
        recorder = Recorder()
        positions = self.path("positions.json")
        ship = shipper(self, main.Config({}, [self.source(stages=stages)], batch_wait=0), transport=recorder, positions=positions)
        ship.cycle()
        ship.stop()
        lines = [v[1] for v in recorder.calls[0][1]["streams"][0]["values"]]
        self.assertEqual(lines, ["level=info n=2", "level=info n=3"])
        self.assertEqual((ship.stats["dropped_by_stage"], ship.stats["dropped_by_limit"]), (1, 1))
        self.assertEqual(list(load_json(positions)["files"].values())[0]["offset"], os.path.getsize(self.path("app.log")))

    def test_unbounded_labels_are_flagged_in_the_config_and_when_values_pile_up(self):
        config_path = self.path("pipeline.json")
        with open(config_path, "w") as handle:
            json.dump({"max_label_values": 3, "sources": [{"path": "app.log*", "labels": {"app": "x"}, "stages": [{"type": "logfmt"}, {"type": "labels", "names": ["user_id"]}]}]}, handle)
        config = main.load_config(config_path)
        self.assertTrue(any("user_id" in w and "unbounded" in w for w in config.warnings))
        self.write("app.log", *[f"user_id=u{i}" for i in range(5)])
        ship = shipper(self, config)
        ship.cycle(final=True)
        self.assertTrue(any("more than 3 distinct values" in w for w in self.warnings))
        ship.stop()

    def test_config_errors_are_input_errors(self):
        for doc in ({"sources": []}, {"sources": [{"path": "x", "labels": {"bad-name": "v"}}]}, {"sources": [{"path": "x", "stages": [{"type": "regex", "expression": "(x)"}]}]}, {"sources": [{"path": "x", "stages": [{"type": "timestamp", "source": "ts", "format": "ISO"}]}]}):
            with open(self.path("c.json"), "w") as handle:
                json.dump(doc, handle)
            with self.assertRaises(main.InputError):
                main.load_config(self.path("c.json"))


class ShutdownTests(TempDir):
    def test_a_shutdown_during_retries_leaves_the_batch_uncommitted_so_it_is_read_again(self):
        self.write("app.log", "one", "two")
        positions = self.path("positions.json")
        stop = {"flag": False}

        def failing(url, data, headers, timeout):
            stop["flag"] = True
            return 503, "overloaded"

        config = main.Config({}, [self.source()], batch_wait=0)
        tail = main.Tailer(config.sources, positions, warn=self.warnings.append)
        pusher = main.Pusher(["http://loki"], transport=failing, sleep=lambda s: None, warn=self.warnings.append, should_stop=lambda: stop["flag"])
        ship = main.Shipper(config, tail, pusher, clock=Clock(), wall=lambda: NS, warn=self.warnings.append)
        ship.cycle()
        ship.stop()
        self.assertEqual(ship.stats["interrupted_batches"], 1)
        self.assertEqual(list(load_json(positions)["files"].values())[0]["offset"], 0)
        store = main.Store()
        again = shipper(self, config, store=store, positions=positions)
        again.cycle()
        again.stop()
        self.assertEqual(store.stats["entries"], 2)


class PushTests(unittest.TestCase):
    def test_push_body_groups_entries_by_stream_and_carries_metadata_as_a_third_value(self):
        body = main.push_body([main.Entry({"b": "2", "a": "1"}, 5, "x", {}), main.Entry({"a": "1", "b": "2"}, 6, "y", {"trace_id": "t"}), main.Entry({"a": "9"}, 7, "z", {})])
        self.assertEqual(body, {"streams": [{"stream": {"a": "1", "b": "2"}, "values": [["5", "x"], ["6", "y", {"trace_id": "t"}]]}, {"stream": {"a": "9"}, "values": [["7", "z"]]}]})
        self.assertEqual(main.push_url("http://127.0.0.1:3100"), "http://127.0.0.1:3100/loki/api/v1/push")
        self.assertEqual(main.push_url("http://x/custom/push"), "http://x/custom/push")

    def test_5xx_429_and_connection_errors_are_retried_with_doubling_backoff(self):
        sleeps = []
        recorder = Recorder([503, ConnectionRefusedError("refused"), 429, 204])
        pusher = main.Pusher(["http://loki"], transport=recorder, sleep=sleeps.append, min_backoff=0.5, max_backoff=1.5, warn=lambda m: None)
        self.assertEqual(pusher.push({"streams": []}, 3), "ok")
        self.assertEqual(sleeps, [0.5, 1.0, 1.5])
        self.assertEqual(pusher.stats["http://loki/loki/api/v1/push"]["retries"], 3)

    def test_other_4xx_is_not_retried_and_retries_give_up_at_the_limit(self):
        warnings = []
        pusher = main.Pusher(["http://loki"], transport=Recorder([400]), sleep=lambda s: None, warn=warnings.append)
        self.assertEqual(pusher.push({"streams": []}, 1), "failed")
        self.assertIn("rejected", warnings[0])
        gives_up = main.Pusher(["http://loki"], transport=Recorder([500] * 5), sleep=lambda s: None, max_retries=2, warn=warnings.append)
        self.assertEqual(gives_up.push({"streams": []}, 1), "failed")
        self.assertEqual(gives_up.stats["http://loki/loki/api/v1/push"]["retries"], 2)
        self.assertIn("gave up", warnings[-1])


def stored(entries, chunk_bytes=64 * 1024):
    store = main.Store(chunk_bytes=chunk_bytes)
    streams = {}
    for labels, ts, line, *meta in entries:
        streams.setdefault(json.dumps(labels, sort_keys=True), {"stream": labels, "values": []})["values"].append([str(ts), line] + meta)
    status, message = store.push({"streams": list(streams.values())})
    assert status == 204, message
    return store


def query(store, text, start=T0, end=T0 + 3600 * NS, **params):
    doc = main.execute(store, {"query": text, "start": str(start), "end": str(end), "direction": "forward", "limit": "5000", **params}, instant=False)
    return doc["data"]


def instant(store, text, at):
    return main.execute(store, {"query": text, "time": str(at)}, instant=True)["data"]["result"]


CHECKOUT = {"app": "checkout", "job": "t"}
PAYMENTS = {"app": "payments", "job": "t"}


class StoreTests(unittest.TestCase):
    def test_streams_are_indexed_by_labels_and_chunks_are_pruned_by_time(self):
        store = stored([(CHECKOUT, T0 + i * NS, f"line {i} " + "p" * 30) for i in range(40)] + [(PAYMENTS, T0, "pay")], chunk_bytes=200)
        self.assertEqual(store.postings["app"]["checkout"], {main.label_string(CHECKOUT)})
        self.assertGreater(len(store.streams[main.label_string(CHECKOUT)].chunks), 3)
        data = query(store, '{app="checkout"}', start=T0 + 35 * NS, end=T0 + 40 * NS)
        self.assertEqual(len(data["result"][0]["values"]), 5)
        stats = data["stats"]["minilog"]
        self.assertEqual(stats["streams_matched"], 1)
        self.assertGreater(stats["chunks_pruned"], stats["chunks_scanned"])
        self.assertGreater(store.describe()["compression_ratio"], 1)

    def test_identical_timestamp_and_line_is_dropped_so_a_resent_batch_is_harmless(self):
        store = stored([(CHECKOUT, T0, "a"), (CHECKOUT, T0, "b")])
        body = {"streams": [{"stream": CHECKOUT, "values": [[str(T0), "a"], [str(T0), "b"], [str(T0 + 1), "a"]]}]}
        self.assertEqual(store.push(body), (204, ""))
        self.assertEqual(store.stats["duplicates_dropped"], 2)
        self.assertEqual(len(query(store, '{app="checkout"}')["result"][0]["values"]), 3)

    def test_entries_too_far_behind_the_newest_in_their_stream_are_rejected(self):
        store = stored([(CHECKOUT, T0 + 7200 * NS, "new")])
        status, message = store.push({"streams": [{"stream": CHECKOUT, "values": [[str(T0), "old"], [str(T0 + 7000 * NS), "fine"]]}]})
        self.assertEqual(status, 400)
        self.assertIn("entry too far behind", message)
        self.assertEqual(store.stats["entries"], 2)

    def test_push_validation_names_the_problem(self):
        store = main.Store()
        self.assertEqual(store.push({"stream": []})[0], 400)
        self.assertIn("not a valid label name", store.push({"streams": [{"stream": {"bad-name": "x"}, "values": []}]})[1])
        self.assertIn("must be", store.push({"streams": [{"stream": CHECKOUT, "values": [["soon", "x"]]}]})[1])


def sample_store():
    entries = []
    for i in range(1, 31):
        level = "error" if i % 10 == 0 else "info"
        line = json.dumps({"level": level, "status": 500 + i % 3 if level == "error" else 200, "path": f"/api/{i}", "seq": i, "app": "dup", "trace_id": f"t{i}"})
        entries.append(({**CHECKOUT, "level": level}, T0 + i * NS + NS // 4, line, {"trace_id": f"t{i}"}))
    for i in range(1, 11):
        entries.append((PAYMENTS, T0 + i * NS + NS // 2, f'ts={i} level={"error" if i % 5 == 0 else "info"} seq={i} msg="m {i}" status={502 if i % 5 == 0 else 200}'))
    return stored(entries)


class LogQLTests(unittest.TestCase):
    def setUp(self):
        self.store = sample_store()

    def values(self, text, **params):
        return sorted((int(v[0]), v[1]) for s in query(self.store, text, **params)["result"] for v in s["values"])

    def test_a_selector_needs_a_matcher_that_rejects_the_empty_string_and_regex_matchers_are_anchored(self):
        for bad in ('{app=~".*"}', '{app!="x"}'):
            with self.assertRaises(main.QueryError) as caught:
                query(self.store, bad)
            self.assertIn("empty-compatible", str(caught.exception))
        self.assertEqual(len(self.values('{app=~"check"}')), 0)
        self.assertEqual(len(self.values('{app=~"check.*"}')), 30)
        self.assertEqual(len(self.values('{app=~"check.*|pay.*", level!="error"}')), 37)

    def test_line_filters_chain_and_a_regex_line_filter_is_unanchored(self):
        self.assertEqual(len(self.values('{app="payments"} |= "level=error"')), 2)
        self.assertEqual(len(self.values('{app="payments"} != "level=error" |~ "seq=1[0-9]?"')), 1)
        self.assertEqual(len(self.values('{app="payments"} !~ `status=50\\d`')), 8)

    def test_json_then_numeric_label_filter_drops_missing_labels_and_keeps_unparseable_with_an_error(self):
        self.assertEqual([SEQ.search(l).group(1) for _, l in self.values('{app="checkout"} | json | status >= 501')], ["10", "20"])
        self.assertEqual(len(self.values('{app="payments"} | logfmt | nonexistent > 1')), 0)
        result = query(self.store, '{app="payments"} | logfmt | msg > 1')["result"]
        self.assertTrue(all(s["stream"]["__error__"] == "LabelFilterErr" for s in result))
        self.assertEqual(len(self.values('{app="payments"} | logfmt | msg > 1 | __error__=""')), 0)

    def test_a_parsed_key_that_collides_with_a_stream_label_or_metadata_gets_the_extracted_suffix(self):
        streams = query(self.store, '{app="checkout"} | json | seq == 10')["result"]
        labels = streams[0]["stream"]
        self.assertEqual((labels["level"], labels["level_extracted"], labels["app"], labels["app_extracted"]), ("error", "error", "checkout", "dup"))
        self.assertEqual((labels["trace_id"], labels["trace_id_extracted"]), ("t10", "t10"))

    def test_line_format_renders_labels_and_a_missing_label_is_empty(self):
        lines = [l for _, l in self.values('{app="checkout"} | json | level="error" | line_format "{{.status}} {{ .path }} [{{.nope}}]"')]
        self.assertEqual(lines, ["501 /api/10 []", "502 /api/20 []", "500 /api/30 []"])
        with self.assertRaises(main.QueryError):
            query(self.store, '{app="checkout"} | line_format "{{ .a | upper }}"')

    def test_count_over_time_uses_a_left_open_window_and_rate_divides_by_the_range(self):
        at = T0 + 10 * NS + NS // 4
        per_trace = instant(self.store, 'count_over_time({app="checkout", level="info"}[5s])', at)
        self.assertEqual(sorted((r["metric"]["trace_id"], r["value"][1]) for r in per_trace), [("t6", "1"), ("t7", "1"), ("t8", "1"), ("t9", "1")])
        result = instant(self.store, 'sum(count_over_time({app="checkout"}[5s]))', at)
        self.assertEqual(result[0]["value"][1], "5")
        rate = instant(self.store, 'sum(rate({app="checkout"}[10s]))', T0 + 10 * NS + NS // 4)
        self.assertAlmostEqual(float(rate[0]["value"][1]), 1.0)
        bytes_total = instant(self.store, 'sum(bytes_over_time({app="payments"} |= "seq=10 " [1h]))', T0 + 3600 * NS)
        self.assertEqual(bytes_total[0]["value"][1], str(len('ts=10 level=error seq=10 msg="m 10" status=502')))

    def test_sum_by_and_without_group_series_and_range_queries_step(self):
        result = instant(self.store, 'sum by (app) (count_over_time({job="t"}[1h]))', T0 + 3600 * NS)
        self.assertEqual({r["metric"]["app"]: r["value"][1] for r in result}, {"checkout": "30", "payments": "10"})
        result = instant(self.store, 'sum without (app, filename) (count_over_time({app="payments"}[1h]))', T0 + 3600 * NS)
        self.assertEqual(result[0]["metric"], {"job": "t"})
        matrix = query(self.store, 'count(sum by (level) (count_over_time({app="checkout"}[10s])))', start=T0, end=T0 + 30 * NS, step="10s")["result"]
        self.assertEqual(matrix[0]["values"], [[T0 // NS + 10, "1"], [T0 // NS + 20, "2"], [T0 // NS + 30, "2"]])

    def test_a_metric_query_over_entries_with_a_parser_error_fails(self):
        with self.assertRaises(main.QueryError) as caught:
            instant(self.store, 'sum(count_over_time({app="payments"} | json [1h]))', T0 + 3600 * NS)
        self.assertIn("pipeline error: 'JSONParserErr'", str(caught.exception))
        ok = instant(self.store, 'sum(count_over_time({app="payments"} | json | __error__="" [1h]))', T0 + 3600 * NS)
        self.assertEqual(ok, [])

    def test_limit_and_direction_pick_the_oldest_or_newest_entries(self):
        forward = self.values('{app="checkout"}', limit="2")
        backward = self.values('{app="checkout"}', limit="2", direction="backward")
        self.assertEqual([SEQ.search(l).group(1) for _, l in forward], ["1", "2"])
        self.assertEqual([SEQ.search(l).group(1) for _, l in backward], ["29", "30"])

    def test_parse_errors_name_the_problem(self):
        cases = {'{app="x"': "expected", 'sum({app="x"})': "range function", '{app="x"} | pattern "<_>"': "not part of minilog", '{app="x"} | a > 1 or b > 2': "`or`", '{app="x"} |~ "("': "invalid regular expression", 'rate({app="x"}[5q])': "expected"}
        for text, fragment in cases.items():
            with self.assertRaises(main.QueryError) as caught:
                main.parse_query(text)
            self.assertIn(fragment, str(caught.exception), text)


class HttpTests(TempDir):
    def test_the_store_speaks_lokis_push_and_query_api_over_http(self):
        store = main.Store()
        server = main.start_server(store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            pusher = main.Pusher([base], sleep=lambda s: None)
            self.assertEqual("ok", pusher.push(main.push_body([main.Entry(CHECKOUT, T0 + NS, "GET /api 500", {"trace_id": "t1"})]), 1))
            doc = main.http_get_json(base, "/loki/api/v1/query_range", [("query", '{app="checkout"} | trace_id="t1"'), ("start", str(T0)), ("end", str(T0 + 60 * NS))])
            self.assertEqual(doc["data"]["result"][0]["values"], [[str(T0 + NS), "GET /api 500"]])
            self.assertEqual(main.http_get_json(base, "/loki/api/v1/label/app/values", [])["data"], ["checkout"])
            series = main.http_get_json(base, "/loki/api/v1/series", [("match[]", '{job="t"}')])["data"]
            self.assertEqual(series, [CHECKOUT])
            with self.assertRaises(main.QueryError):
                main.http_get_json(base, "/loki/api/v1/query", [("query", '{app="checkout"}')])
            request = urllib.request.Request(base + main.DEFAULT_PUSH_PATH, data=b"\x00", headers={"Content-Type": "application/x-protobuf"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 415)
        finally:
            server.shutdown()
            server.server_close()

    def test_cardinality_report_counts_values_per_label_and_warns(self):
        series = [{"app": "a", "user_id": f"u{i}"} for i in range(30)]
        report = main.cardinality_report(series, 25)
        self.assertEqual([(r["name"], r["values"]) for r in report["labels"]], [("user_id", 30), ("app", 1)])
        self.assertIn("user_id", report["warnings"][0])


class EndToEndTests(TempDir):
    def test_generate_rotates_both_ways_and_ship_once_loses_and_duplicates_nothing(self):
        checkout = main.generate(self.path("logs/checkout.log"), "json", 60, start=T0, rotate_every=25, rotate="rename")
        payments = main.generate(self.path("logs/payments.log"), "logfmt", 40, start=T0, rotate_every=15, rotate="copytruncate", traceback_every=20)
        self.assertEqual((checkout["rotations"], payments["rotations"], payments["tracebacks"]), (2, 2, [20, 40]))
        self.assertEqual(checkout["files"], ["checkout.log", "checkout.log.1", "checkout.log.2"])
        with open(self.path("logs/checkout.log.1")) as handle:
            self.assertEqual(SEQ.search(handle.readlines()[-1]).group(1), "51")
        config = {"labels": {"job": "t"}, "sources": [
            {"path": "logs/checkout.log*", "labels": {"app": "checkout"}, "stages": [{"type": "json"}, {"type": "drop", "source": "level", "value": "debug"}, {"type": "timestamp", "source": "ts", "format": "RFC3339"}]},
            {"path": "logs/payments.log*", "labels": {"app": "payments"}, "multiline": {"firstline": "^ts="}, "stages": [{"type": "regex", "expression": "^ts=(?P<ts>\\S+)"}, {"type": "timestamp", "source": "ts", "format": "RFC3339"}]},
        ]}
        with open(self.path("pipeline.json"), "w") as handle:
            json.dump(config, handle)
        store = main.Store()
        ship = shipper(self, main.load_config(self.path("pipeline.json")), store=store)
        ship.cycle(final=True)
        ship.stop()
        lines = [v[1] for s in query(store, '{job="t"}', end=T0 + 600 * NS)["result"] for v in s["values"]]
        seqs = {app: sorted(int(SEQ.search(l).group(1)) for l in lines if ("seq\":" in l) == (app == "checkout")) for app in ("checkout", "payments")}
        self.assertEqual(seqs["checkout"], [s for s in range(1, 61) if s not in checkout["debug"]])
        self.assertEqual(seqs["payments"], list(range(1, 41)))
        traces = [l for l in lines if "Traceback" in l]
        self.assertEqual(len(traces), 2)
        self.assertTrue(all(l.count("\n") == 6 for l in traces))
        self.assertEqual(store.stats["duplicates_dropped"], 0)

    def test_cli_exit_codes(self):
        self.assertEqual(run(["ship", "--config", self.path("missing.json"), "--dry-run"])[0], 2)
        code, _, err = run(["query", '{app=~".*"}', "--url", "http://127.0.0.1:9"])
        self.assertEqual(code, 1)
        code, _, err = run(["query", '{app="x"', "--url", "http://127.0.0.1:9"])
        self.assertEqual(code, 2)
        self.assertIn("parse error", err)
        code, out, _ = run(["generate", self.path("g.log"), "--count", "3", "--start", "2026-11-02T09:00:00Z", "--json"])
        self.assertEqual((code, json.loads(out)["written"]), (0, 3))
        with open(self.path("g.log")) as handle:
            self.assertEqual(json.loads(handle.readline())["ts"], "2026-11-02T09:00:00.250Z")


if __name__ == "__main__":
    unittest.main()
