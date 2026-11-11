import contextlib
import io
import json
import os
import sys
import tempfile
import tracemalloc
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

RECORDS = [
    {"ts": "2026-11-10T10:00:01Z", "method": "GET", "path": "/api/users", "status": 200, "bytes": 512, "ms": 12.5, "user": "alice", "tags": ["eu"], "upstream": {"host": "a", "retries": 0}},
    {"ts": "2026-11-10T10:00:02Z", "method": "POST", "path": "/api/orders", "status": 503, "bytes": 0, "ms": 230.0, "user": None, "tags": [], "upstream": {"host": "b", "retries": 2}},
    {"ts": "2026-11-10T10:00:03Z", "method": "GET", "path": "/", "status": 404, "bytes": 90, "ms": 3.25, "user": "bob", "tags": ["us", "bot"], "upstream": {"host": "a", "retries": 1}},
    {"ts": "2026-11-10T10:00:04Z", "method": "GET", "path": "/api/users", "status": 500, "bytes": 77, "ms": 99.0, "tags": ["eu"], "upstream": {"host": "b", "retries": 0}},
]
JSONL = "".join(json.dumps(record) + "\n" for record in RECORDS)
CSV = "ts,method,path,status,bytes\n" + "".join(f"{r['ts']},{r['method']},{r['path']},{r['status']},{r['bytes']}\n" for r in RECORDS)


def jq(program, value, **variables):
    return list(main.JqProgram(program, variables).run(value, variables))


def jq_stream(program, text, *flags):
    out, err = io.StringIO(), io.StringIO()
    code = main.main(["jq", *flags, program], stdin=io.StringIO(text), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def awk(program, text, *flags):
    out, err = io.StringIO(), io.StringIO()
    code = main.main(["awk", *flags, program], stdin=io.StringIO(text), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class TrickleStream:
    def __init__(self, text, size):
        self.text = text
        self.size = size
        self.position = 0

    def read(self, requested=-1):
        piece = self.text[self.position:self.position + min(self.size, requested if requested > 0 else self.size)]
        self.position += len(piece)
        return piece


class GeneratedStream:
    def __init__(self, lines):
        self.lines = iter(lines)
        self.pending = ""

    def read(self, size=-1):
        while len(self.pending) < size:
            line = next(self.lines, None)
            if line is None:
                break
            self.pending += line
        chunk, self.pending = self.pending[:size], self.pending[size:]
        return chunk


class CountingSink:
    def __init__(self):
        self.writes = 0
        self.last = ""

    def write(self, text):
        self.writes += 1
        self.last = text

    def flush(self):
        pass


def generated_records(count):
    for index in range(count):
        yield json.dumps({"id": index, "status": 500 if index % 10 == 0 else 200, "path": f"/api/items/{index % 50}", "padding": "x" * 60}) + "\n"


class JqPathsAndPipes(unittest.TestCase):
    def test_paths_index_nested_objects_and_arrays_and_are_null_safe(self):
        value = {"a": {"b": [10, 20, 30]}, "n": None}
        self.assertEqual(jq(".a.b[0]", value), [10])
        self.assertEqual(jq(".a.b[-1]", value), [30])
        self.assertEqual(jq(".a.b[7]", value), [None])
        self.assertEqual(jq(".n.x.y", value), [None])
        self.assertEqual(jq('.["a"].b[1:]', value), [[20, 30]])

    def test_iterate_pipe_and_comma_produce_a_stream_of_outputs(self):
        value = {"items": [{"n": 1}, {"n": 2}, {"n": 3}]}
        self.assertEqual(jq(".items[] | .n", value), [1, 2, 3])
        self.assertEqual(jq(".items[0].n, .items[2].n", value), [1, 3])
        self.assertEqual(jq("[.items[] | .n * 10]", value), [[10, 20, 30]])
        self.assertEqual(jq("{a: 1} | .[]", value), [1])

    def test_select_map_and_object_construction(self):
        self.assertEqual(jq("[.[] | select(.status >= 500) | .path]", RECORDS), [["/api/orders", "/api/users"]])
        self.assertEqual(jq("map({path, code: .status})[0]", RECORDS), [{"path": "/api/users", "code": 200}])
        self.assertEqual(jq("{(.method): .status}", RECORDS[1]), [{"POST": 503}])
        self.assertEqual(jq("{a: (1, 2)}", None), [{"a": 1}, {"a": 2}])

    def test_comparison_uses_jq_type_order_and_arithmetic_follows_jq_types(self):
        mixed = [{"a": 1}, [1], "b", 2, True, False, None]
        self.assertEqual(jq("sort", mixed), [[None, False, True, 2, "b", [1], {"a": 1}]])
        self.assertEqual(jq("[1 < 2, \"a\" < \"b\", null < false, [1, 2] < [1, 3], 1 == 1.0]", None), [[True, True, True, True, True]])
        self.assertEqual(jq('[null + 1, "a" + "b", [1] + [2], {a: 1} + {b: 2}, [1, 2, 3] - [2], 7 % 3, 10 / 4]', None), [[1, "ab", [1, 2], {"a": 1, "b": 2}, [1, 3], 1, 2.5]])

    def test_builtins_length_keys_has_entries_group_and_sort(self):
        self.assertEqual(jq("[length, (.tags | length), (.path | length)]", RECORDS[0]), [[9, 1, 10]])
        self.assertEqual(jq(".upstream | keys", RECORDS[0]), [["host", "retries"]])
        self.assertEqual(jq('[has("user"), (.tags | has(0))]', RECORDS[3]), [[False, True]])
        self.assertEqual(jq(".upstream | to_entries | from_entries", RECORDS[0]), [{"host": "a", "retries": 0}])
        self.assertEqual(jq("group_by(.path) | map([.[0].path, length])", RECORDS), [[["/", 1], ["/api/orders", 1], ["/api/users", 2]]])
        self.assertEqual(jq("sort_by(.ms) | map(.ms)", RECORDS), [[3.25, 12.5, 99.0, 230.0]])
        self.assertEqual(jq("[min_by(.ms).path, max_by(.bytes).path, (map(.status) | unique)]", RECORDS), [["/", "/api/users", [200, 404, 500, 503]]])

    def test_alternative_if_interpolation_and_formats(self):
        self.assertEqual(jq('.user // "anonymous"', RECORDS[1]), ["anonymous"])
        self.assertEqual(jq('if .status >= 500 then "server" elif .status >= 400 then "client" else "ok" end', RECORDS[2]), ["client"])
        self.assertEqual(jq('"\\(.method) \\(.path) \\(.status)"', RECORDS[0]), ["GET /api/users 200"])
        self.assertEqual(jq("[.ts, .status, .user] | @csv", RECORDS[1]), ['"2026-11-10T10:00:02Z",503,'])
        self.assertEqual(jq("[.method, .path] | @tsv", RECORDS[0]), ["GET\t/api/users"])

    def test_variables_and_reduce(self):
        self.assertEqual(jq("reduce .[] as $r (0; . + $r.bytes)", RECORDS), [679])
        self.assertEqual(jq(".status as $s | .tags | map(\"\\($s)-\\(.)\")", RECORDS[2]), [["404-us", "404-bot"]])
        self.assertEqual(jq("$limit", None, limit="5"), ["5"])

    def test_runtime_errors_name_the_types_and_question_mark_suppresses_them(self):
        with self.assertRaisesRegex(main.JqError, 'Cannot index number with "x"'):
            jq(".status.x", RECORDS[0])
        with self.assertRaisesRegex(main.JqError, "Cannot iterate over null"):
            jq(".missing[]", RECORDS[0])
        with self.assertRaisesRegex(main.JqError, "cannot be added"):
            jq('.status + "x"', RECORDS[0])
        self.assertEqual(jq("[.status.x?]", RECORDS[0]), [[]])
        self.assertEqual(jq("[.[] | .status.x? // .path]", RECORDS[:2]), [["/api/users", "/api/orders"]])

    def test_syntax_errors_carry_line_and_column(self):
        with self.assertRaises(main.ProgramError) as caught:
            main.JqProgram(".a |\n  .b ||")
        self.assertEqual(caught.exception.line, 2)
        with self.assertRaisesRegex(main.ProgramError, "lenght/0 is not defined"):
            main.JqProgram(".a | lenght")
        with self.assertRaisesRegex(main.ProgramError, r"\$x is not defined"):
            main.JqProgram(".a | $x")
        with self.assertRaisesRegex(main.ProgramError, "update-assignment"):
            main.JqProgram(".a |= 1")


class JqStreaming(unittest.TestCase):
    def test_decoder_handles_values_split_across_tiny_reads(self):
        text = '{"a": [1, 2,\n 3]}  12345 "x y"\n[true, null]\n-7.5e2 {"b":{}}'
        values = list(main.iter_json_values(TrickleStream(text, 3), chunk_size=3))
        self.assertEqual(values, [{"a": [1, 2, 3]}, 12345, "x y", [True, None], -750.0, {"b": {}}])

    def test_invalid_input_raises_after_the_good_values(self):
        stream = main.iter_json_values(TrickleStream('{"a":1}\n{"a":2}\n{"a":\n', 4), chunk_size=4)
        self.assertEqual(next(stream), {"a": 1})
        self.assertEqual(next(stream), {"a": 2})
        with self.assertRaisesRegex(main.InputError, "invalid JSON input at byte"):
            next(stream)

    def test_cli_flags_raw_compact_slurp_and_null_input(self):
        self.assertEqual(jq_stream(".path", JSONL, "-r")[1].splitlines(), ["/api/users", "/api/orders", "/", "/api/users"])
        self.assertEqual(jq_stream(".upstream", JSONL, "-c")[1].splitlines()[0], '{"host":"a","retries":0}')
        self.assertEqual(jq_stream(".upstream", JSONL)[1].splitlines()[:2], ["{", '  "host": "a",'])
        self.assertEqual(jq_stream("map(.bytes) | add", JSONL, "-s")[1], "679\n")
        self.assertEqual(jq_stream("[range(3)]", "", "-n", "-c")[1], "[0,1,2]\n")
        self.assertEqual(jq_stream("1.5 * 2", "", "-n")[1], "3\n")

    def test_a_runtime_error_on_one_record_is_reported_and_the_stream_continues(self):
        code, out, err = jq_stream("if .status == 503 then .status.x else .path end", JSONL, "-r")
        self.assertEqual(code, main.EXIT_RUNTIME)
        self.assertEqual(out.splitlines(), ["/api/users", "/", "/api/users"])
        self.assertIn("error (at record 2)", err)
        self.assertEqual(err.count("\n"), 1)

    def test_streaming_keeps_memory_flat_while_slurp_does_not(self):
        sink = CountingSink()
        tracemalloc.start()
        try:
            code = main.run_jq(main.JqProgram("select(.status >= 500) | .path"), [("-", GeneratedStream(generated_records(30000)))], sink, io.StringIO(), raw=True)
            _, streaming_peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        input_bytes = sum(len(line) for line in generated_records(30000))
        self.assertEqual(code, main.EXIT_OK)
        self.assertEqual(sink.writes, 6000)
        self.assertGreater(input_bytes, 3_000_000)
        self.assertLess(streaming_peak, 1_000_000)
        tracemalloc.start()
        try:
            main.run_jq(main.JqProgram(".[] | select(.status >= 500) | .path"), [("-", GeneratedStream(generated_records(30000)))], CountingSink(), io.StringIO(), raw=True, slurp=True)
            _, slurp_peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertGreater(slurp_peak, input_bytes)


class AwkFieldsAndPatterns(unittest.TestCase):
    def test_default_splitting_on_runs_of_blanks_with_nf_nr_and_last_field(self):
        code, out, _ = awk("{ print NR, NF, $1, $NF }", "  a  b\tc \nsingle\n\n")
        self.assertEqual(code, 0)
        self.assertEqual(out, "1 3 a c\n2 1 single single\n3 0  \n")

    def test_separator_patterns_begin_end_and_associative_arrays(self):
        program = 'BEGIN { print "start" } NR > 1 && $4 >= 500 { bad[$3]++ } END { for (p in bad) print p, bad[p]; print "rows", NR - 1 }'
        code, out, _ = awk(program, CSV, "-F", ",")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "start")
        self.assertEqual(sorted(out.splitlines()[1:-1]), ["/api/orders 1", "/api/users 1"])
        self.assertEqual(out.splitlines()[-1], "rows 4")
        self.assertEqual(awk("$2 ~ /^P/ { print $3 }", CSV, "-F,")[1], "/api/orders\n")
        self.assertEqual(awk("/users/", CSV, "-F,")[1].count("\n"), 2)
        self.assertEqual(awk("{ print $2 }", "a::b\n", "-F", "::")[1], "b\n")
        self.assertEqual(awk("{ print $3 }", "a::b\n", "-F", ":+")[1], "\n")
        self.assertEqual(awk("{ print $2 }", "a::b\n", "-F", ":+")[1], "b\n")
        self.assertEqual(awk("{ print $2 }", "a\tb c\n", "-F", "\\t")[1], "b c\n")

    def test_numbers_print_as_integers_or_with_ofmt_and_strings_from_input_compare_numerically(self):
        self.assertEqual(awk("BEGIN { print 0.1 + 0.2, 3 / 2, 1e6, 2 ^ 10, -7 % 3, 1 / 3 }", "")[1], "0.3 1.5 1000000 1024 -1 0.333333\n")
        self.assertEqual(awk("$1 > $2 { print \"num\" } \"10\" > \"9\" { print \"never\" }", "10 9\n")[1], "num\n")
        self.assertEqual(awk('$1 == "1.0" { print "string" } $1 == 1 { print "number" }', "1.00\n")[1], "number\n")
        self.assertEqual(awk("{ x = $1; print (x < 9) }", "10\n")[1], "0\n")
        self.assertEqual(awk('BEGIN { if (u == 0 && u == "") print "both"; print length(u), u + 5 }', "")[1], "both\n0 5\n")

    def test_printf_and_sprintf_follow_c_formats(self):
        program = 'NR == 2 { printf "%-6s|%5d|%8.2f|%x|%c|%5.1f%%\\n", $2, $5, $5 / 3, $4, 65, 12.345; s = sprintf("%03d", 7); print s }'
        self.assertEqual(awk(program, CSV, "-F,")[1], "GET   |  512|  170.67|c8|A| 12.3%\n007\n")

    def test_assigning_a_field_rebuilds_the_record_with_ofs(self):
        self.assertEqual(awk('BEGIN { OFS = "-" } { $2 = "X"; print; print NF }', "a b c\n")[1], "a-X-c\n3\n")
        self.assertEqual(awk('BEGIN { OFS = "," } { $5 = "e"; print }', "a b\n")[1], "a,b,,,e\n")
        self.assertEqual(awk('BEGIN { OFS = "\\t" } { $1 = $1; print }', "a,b,c\n", "-F,")[1], "a\tb\tc\n")
        self.assertEqual(awk("{ NF = 2; print }", "a b c d\n")[1], "a b\n")
        self.assertEqual(awk('{ $0 = "x y z"; print NF, $3 }', "a\n")[1], "3 z\n")

    def test_string_builtins(self):
        program = '{ print length($0), substr($0, 3, 4), substr($0, 8), index($0, "lo"), toupper($1), tolower("MiX") }'
        self.assertEqual(awk(program, "hello world\n")[1], "11 llo  orld 4 HELLO mix\n")
        self.assertEqual(awk('{ n = split($0, p, "/"); print n, p[2], p[n] }', "/api/users/7\n")[1], "4 api 7\n")
        self.assertEqual(awk('{ n = gsub(/o/, "[&]"); print n, $0 }', "foo boo\n")[1], "4 f[o][o] b[o][o]\n")
        self.assertEqual(awk('{ sub(/o+/, "\\\\&", $2); print $2 }', "foo boo\n")[1], "b&\n")
        self.assertEqual(awk('{ if (match($0, /[0-9]+/)) print RSTART, RLENGTH, substr($0, RSTART, RLENGTH) }', "id=4521;\n")[1], "4 4 4521\n")

    def test_control_flow_next_exit_loops_and_delete(self):
        self.assertEqual(awk("NR == 2 { next } { print }", "a\nb\nc\n")[1], "a\nc\n")
        self.assertEqual(awk("NR == 2 { exit 3 } { print } END { print \"end\" }", "a\nb\nc\n")[:2], (3, "a\nend\n"))
        self.assertEqual(awk("BEGIN { for (i = 1; i <= 5; i++) { if (i == 2) continue; if (i == 4) break; s = s i } print s }", "")[1], "13\n")
        self.assertEqual(awk("BEGIN { while (n < 3) n++; print n, (n > 2 ? \"big\" : \"small\") }", "")[1], "3 big\n")
        self.assertEqual(awk('BEGIN { a["x"] = 1; a["y"] = 2; delete a["x"]; print length(a), ("x" in a), ("y" in a) }', "")[1], "1 0 1\n")
        self.assertEqual(awk("!seen[$0]++", "a\nb\na\nc\nb\n")[1], "a\nb\nc\n")

    def test_v_assignments_are_numeric_strings(self):
        self.assertEqual(awk("$1 > limit { print $1 }", "5\n50\n500\n", "-v", "limit=40")[1], "50\n500\n")
        self.assertEqual(awk("BEGIN { print greeting }", "", "-v", "greeting=a\\tb")[1], "a\tb\n")

    def test_syntax_and_runtime_errors_have_exit_codes_and_positions(self):
        code, _, err = awk("{ print $1 > \"out.txt\" }", "a\n")
        self.assertEqual(code, main.EXIT_USAGE)
        self.assertIn("output redirection", err)
        code, _, err = awk('{ print "unterminated }', "a\n")
        self.assertEqual(code, main.EXIT_USAGE)
        self.assertIn("syntax error at line 1, column 9: unterminated string", err)
        code, _, err = awk("{ print 1 / $1 }", "2\n0\n")
        self.assertEqual(code, main.EXIT_RUNTIME)
        self.assertIn("division by zero (at record 2)", err)
        self.assertEqual(awk("BEGIN { next }", "")[0], main.EXIT_RUNTIME)
        self.assertEqual(awk("{ print }", "", "-v", "1bad=x")[0], main.EXIT_USAGE)

    def test_awk_streams_lines_in_constant_memory(self):
        lines = (f"{index} {'GET' if index % 3 else 'POST'} /p/{index % 40} {200 + index % 4}\n" for index in range(40000))
        sink = CountingSink()
        tracemalloc.start()
        try:
            main.AwkProgram("$2 == \"POST\" { posts++; bytes[$3] += $4 } END { print posts, length(bytes) }").run([("-", lines)], sink)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(sink.last, "13334 40\n")
        self.assertLess(peak, 500_000)


class ExplainAndCli(unittest.TestCase):
    def test_explain_prints_the_jq_tree(self):
        out = io.StringIO()
        self.assertEqual(main.main(["explain", "jq", ".items[] | select(.n > 1)"], stdout=out, stderr=io.StringIO()), 0)
        self.assertEqual(out.getvalue().splitlines(), [
            "pipe",
            "  iterate",
            "    index",
            "      identity",
            '      literal "items"',
            '  call "select"',
            '    compare ">"',
            "      index",
            "        identity",
            '        literal "n"',
            "      literal 1",
        ])

    def test_explain_json_shows_awk_precedence_and_concatenation(self):
        out = io.StringIO()
        main.main(["explain", "--json", "awk", '$1 > 2 { print $1 " " $2 * 3 }'], stdout=out, stderr=io.StringIO())
        rule = json.loads(out.getvalue())["args"][0][0]
        self.assertEqual(rule["node"], "rule")
        self.assertEqual(rule["args"][0]["node"], "compare")
        printed = rule["args"][1]["args"][0][0]["args"][0][0]
        self.assertEqual(printed["node"], "concat")
        self.assertEqual(printed["args"][1]["node"], "arith")

    def test_files_stats_and_usage_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "access.jsonl")
            with open(path, "w") as handle:
                handle.write(JSONL)
            out, err = io.StringIO(), io.StringIO()
            self.assertEqual(main.main(["--stats", "jq", "-c", "select(.status >= 500) | .path", path, path], stdout=out, stderr=err), 0)
            self.assertEqual(out.getvalue().count("\n"), 4)
            stats = json.loads(err.getvalue().splitlines()[-1])
            self.assertEqual((stats["tool"], stats["records"], stats["outputs"]), ("jq", 8, 4))
            err = io.StringIO()
            self.assertEqual(main.main(["jq", ".", os.path.join(directory, "missing.jsonl")], stdout=io.StringIO(), stderr=err), main.EXIT_USAGE)
            self.assertIn("missing.jsonl", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main.main(["jq"], stdout=io.StringIO(), stderr=io.StringIO()), main.EXIT_USAGE)
        self.assertEqual(jq_stream(".[", "1")[0], main.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
