import base64
import contextlib
import io
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FUNCTION = os.path.join(HERE, "..", "fixtures", "function")
REPORTS = os.path.join(HERE, "..", "fixtures", "cloudwatch-reports.txt")


def fixture_lines():
    with open(REPORTS, encoding="utf-8") as handle:
        return handle.read().splitlines()


def event(**fields):
    return json.dumps(fields).encode("utf-8")


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def http(method, url, body=None, headers=None):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


class PureTests(unittest.TestCase):
    def test_billed_duration_rounds_up_to_the_millisecond_and_includes_init(self):
        self.assertEqual(main.billed_ms(250.06, 100.77), 351)
        self.assertEqual(main.billed_ms(12.40), 13)
        self.assertEqual(main.billed_ms(2.0), 2)
        self.assertEqual(main.billed_ms(133.61, 80.00), 214)

    def test_report_line_has_init_duration_only_on_a_cold_start_and_status_on_a_reset(self):
        cold = main.report_line("r1", 250.06, 1024, 350, 100.77)
        warm = main.report_line("r2", 12.4, 1024, 351)
        timeout = main.report_line("r3", 3016.78, 128, 31, 84.0, "timeout")
        self.assertEqual(cold, "REPORT RequestId: r1\tDuration: 250.06 ms\tBilled Duration: 351 ms\tMemory Size: 1024 MB\tMax Memory Used: 350 MB\tInit Duration: 100.77 ms")
        self.assertNotIn("Init Duration", warm)
        self.assertTrue(timeout.endswith("Init Duration: 84.00 ms\tStatus: timeout"))
        self.assertIn("Billed Duration: 3101 ms", timeout)

    def test_parse_log_line_reads_lambda_rie_and_json_report_formats(self):
        parsed = [main.parse_log_line(line) for line in fixture_lines()]
        reports = [p for p in parsed if p and p["kind"] == "report"]
        self.assertEqual(len(reports), 7)
        crash = next(r for r in reports if r["request_id"].startswith("5b866fb1"))
        self.assertEqual((crash["duration_ms"], crash["billed_ms"], crash["init_ms"], crash["status"], crash["error_type"]), (133.61, 214.0, 80.0, "error", "Runtime.ExitError"))
        rie = next(r for r in reports if r["request_id"].startswith("8f6a2c1e"))
        self.assertEqual((rie["init_ms"], rie["duration_ms"], rie["memory_mb"]), (0.41, 61.37, 3008.0))
        structured = next(r for r in reports if r["request_id"].startswith("6d0c9b3a"))
        self.assertEqual((structured["duration_ms"], structured["max_memory_mb"]), (4.12, 70.0))
        self.assertNotIn("init_ms", structured)
        init = next(p for p in parsed if p and p["kind"] == "init_report")
        self.assertEqual((init["init_ms"], init["phase"], init["status"]), (1236.04, "init", "timeout"))
        self.assertIsNone(main.parse_log_line("START RequestId: x Version: $LATEST"))

    def test_summarize_counts_cold_starts_percentiles_and_init_failures(self):
        summary = main.summarize(main.parse_log_line(line) for line in fixture_lines() if main.parse_log_line(line))
        self.assertEqual((summary["invocations"], summary["cold_starts"]), (7, 4))
        self.assertEqual(summary["init_ms"]["p50"], 80.0)
        self.assertEqual(summary["init_ms"]["p95"], 100.77)
        self.assertEqual(summary["billed_ms_total"], 351 + 13 + 11 + 3101 + 214 + 62 + 5)
        self.assertEqual(summary["statuses"], {"success": 5, "timeout": 1, "error": 1})
        self.assertEqual(summary["init_failures"], {"init:timeout:-": 1})

    def test_placement_prefers_warm_then_reset_then_cold_and_throttles_on_concurrency(self):
        envs = [main.EnvView(1, "idle", 10.0), main.EnvView(2, "idle", 20.0), main.EnvView(3, "reset", 30.0), main.EnvView(4, "busy", 40.0)]
        self.assertEqual((main.place(envs, None, 10).action, main.place(envs, None, 10).env_id), ("warm", 2))
        envs.append(main.EnvView(5, "idle", 1.0, provisioned=True))
        self.assertEqual(main.place(envs, None, 10).env_id, 5)
        self.assertEqual(main.place([main.EnvView(3, "reset", 1.0)], None, 10).action, "reinit")
        self.assertEqual(main.place([main.EnvView(4, "busy", 1.0), main.EnvView(6, "initializing", 1.0)], None, 10).action, "cold")
        throttled = main.place([main.EnvView(4, "busy", 1.0)], 1, 10)
        self.assertEqual((throttled.action, throttled.reason), ("throttle", "ReservedFunctionConcurrentInvocationLimitExceeded"))
        self.assertEqual(main.place([], 0, 10).action, "throttle")
        self.assertEqual(main.place([main.EnvView(4, "busy", 1.0)], None, 1).reason, "ConcurrentInvocationLimitExceeded")

    def test_platform_errors_use_lambdas_types_and_wording(self):
        self.assertEqual(main.timeout_error(3, "abc"), {"errorType": "Sandbox.Timedout", "errorMessage": "RequestId: abc Error: Task timed out after 3.00 seconds"})
        self.assertEqual(main.exit_error(3, "abc")["errorMessage"], "RequestId: abc Error: Runtime exited with error: exit status 3")
        self.assertEqual(main.exit_error(-9)["errorMessage"], "Error: Runtime exited with error: signal: killed")
        self.assertEqual(main.exit_error(0)["errorMessage"], "Error: Runtime exited without providing a reason")
        self.assertEqual(main.normalize_error_type("Runtime.ImportModuleError"), "Runtime.ImportModuleError")
        self.assertEqual(main.normalize_error_type("ValueError"), "Runtime.Unknown")
        self.assertEqual(main.normalize_error_type("Function.x"), "Function.Unknown")
        self.assertEqual(main.throttle_body("ReservedFunctionConcurrentInvocationLimitExceeded")["message"], "Rate Exceeded.")

    def test_function_names_arns_and_qualifiers(self):
        self.assertEqual(main.split_function_name("function"), ("function", "$LATEST"))
        self.assertEqual(main.split_function_name("checkout:live"), ("checkout", "live"))
        self.assertEqual(main.split_function_name("arn:aws:lambda:us-east-1:000000000000:function:checkout"), ("checkout", "$LATEST"))
        self.assertEqual(main.split_function_name("arn:aws:lambda:us-east-1:000000000000:function:checkout:7"), ("checkout", "7"))

    def test_peak_rss_is_read_from_vmhwm_in_a_proc_tree(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "4242"))
            with open(os.path.join(root, "4242", "status"), "w", encoding="utf-8") as handle:
                handle.write("Name:\tpython3\nVmPeak:\t  90000 kB\nVmHWM:\t   33793 kB\nVmRSS:\t   30000 kB\n")
            self.assertEqual(main.peak_rss_mb(4242, root), 34)
            self.assertIsNone(main.peak_rss_mb(1, root))


class FakeRuntimeClient:
    def __init__(self):
        self.calls = []

    def respond(self, request_id, body, invocation_id=""):
        self.calls.append(("response", request_id, body, invocation_id))
        return 202

    def error(self, request_id, payload, invocation_id=""):
        self.calls.append(("error", request_id, payload, invocation_id))
        return 202

    def init_error(self, payload):
        self.calls.append(("init_error", payload))
        return 202

    def next(self):
        raise main.RuntimeAPIError(500, b"shutting down")


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        sys.path.insert(0, self.tmp.name)
        self.headers = {"lambda-runtime-aws-request-id": "req-1", "lambda-runtime-invocation-id": "inv-1", "lambda-runtime-deadline-ms": "1000500", "lambda-runtime-invoked-function-arn": "arn:aws:lambda:us-east-1:000000000000:function:f"}

    def tearDown(self):
        sys.path.remove(self.tmp.name)
        self.tmp.cleanup()

    def module(self, source):
        name = "cc_mod_" + uuid.uuid4().hex[:8]
        with open(os.path.join(self.tmp.name, name + ".py"), "w", encoding="utf-8") as handle:
            handle.write(source)
        return name

    def test_load_handler_reports_the_python_runtime_error_types(self):
        with self.assertRaises(main.FaultError) as malformed:
            main.load_handler("handler")
        self.assertEqual(malformed.exception.error_type, "Runtime.MalformedHandlerName")
        self.assertIn("not enough values to unpack", malformed.exception.message)
        with self.assertRaises(main.FaultError) as missing:
            main.load_handler("cc_no_such_module_here.handler")
        self.assertEqual((missing.exception.error_type, missing.exception.message), ("Runtime.ImportModuleError", "Unable to import module 'cc_no_such_module_here': No module named 'cc_no_such_module_here'"))
        name = self.module("def other(event, context):\n    return 1\n")
        with self.assertRaises(main.FaultError) as not_found:
            main.load_handler(f"{name}.handler")
        self.assertEqual((not_found.exception.error_type, not_found.exception.message), ("Runtime.HandlerNotFound", f"Handler 'handler' missing on module '{name}'"))
        broken = self.module("def handler(event, context)\n    return 1\n")
        with self.assertRaises(main.FaultError) as syntax:
            main.load_handler(f"{broken}.handler")
        self.assertEqual(syntax.exception.error_type, "Runtime.UserCodeSyntaxError")
        with self.assertRaises(main.FaultError) as builtin:
            main.load_handler("sys.handler")
        self.assertEqual(builtin.exception.error_type, "Runtime.BuiltInModuleConflict")

    def test_error_payload_starts_the_stack_at_user_code(self):
        name = self.module("def handler(event, context):\n    raise ValueError('bad input')\n")
        handler = main.load_handler(f"{name}.handler")
        client = FakeRuntimeClient()
        outcome, payload = main.handle_invocation(client, handler, self.headers, b"{}", {}, io.StringIO())
        self.assertEqual(outcome, "error")
        self.assertEqual((payload["errorType"], payload["errorMessage"], payload["requestId"]), ("ValueError", "bad input", "req-1"))
        self.assertIn(name, payload["stackTrace"][0])
        self.assertNotIn("main.py", "".join(payload["stackTrace"]))
        self.assertEqual(client.calls[0][0:2], ("error", "req-1"))
        self.assertEqual(client.calls[0][3], "inv-1")

    def test_context_counts_down_from_the_deadline_header(self):
        client = FakeRuntimeClient()
        seen = {}

        def handler(event, context):
            seen.update(event=event, remaining=context.get_remaining_time_in_millis(), arn=context.invoked_function_arn, name=context.function_name)
            return {"ok": True}

        outcome, _ = main.handle_invocation(client, handler, self.headers, b'{"n": 1}', {"AWS_LAMBDA_FUNCTION_NAME": "f"}, io.StringIO(), clock=lambda: 1000.0)
        self.assertEqual(outcome, "success")
        self.assertEqual(seen, {"event": {"n": 1}, "remaining": 500, "arn": self.headers["lambda-runtime-invoked-function-arn"], "name": "f"})
        self.assertEqual(client.calls, [("response", "req-1", b'{"ok": true}', "inv-1")])

    def test_unmarshal_and_marshal_failures_are_runtime_errors(self):
        client = FakeRuntimeClient()
        outcome, payload = main.handle_invocation(client, lambda e, c: e, self.headers, b"not json", {}, io.StringIO())
        self.assertEqual((outcome, payload["errorType"]), ("error", "Runtime.UnmarshalError"))
        outcome, payload = main.handle_invocation(client, lambda e, c: {1, 2}, self.headers, b"{}", {}, io.StringIO())
        self.assertEqual((outcome, payload["errorType"]), ("error", "Runtime.MarshalError"))
        self.assertIn("not JSON serializable", payload["errorMessage"])

    def test_an_import_error_is_posted_to_init_error_and_the_runtime_exits_1(self):
        client = FakeRuntimeClient()
        out = io.StringIO()
        code = main.run_runtime(client, "cc_absent_module.handler", self.tmp.name, {}, out)
        self.assertEqual(code, 1)
        self.assertEqual(client.calls[0][0], "init_error")
        self.assertEqual(client.calls[0][1]["errorType"], "Runtime.ImportModuleError")
        self.assertEqual(client.calls[0][1]["requestId"], "")
        self.assertIn("[ERROR] Runtime.ImportModuleError: Unable to import module 'cc_absent_module'", out.getvalue())

    def test_bootstrap_refuses_to_run_without_a_runtime_api(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AWS_LAMBDA_RUNTIME_API", None)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(main.main(["bootstrap", "app.handler"]), 2)
        self.assertIn("AWS_LAMBDA_RUNTIME_API is not set", err.getvalue())


class PlatformCase(unittest.TestCase):
    def make(self, handler="app.handler", **overrides):
        self.sink = io.StringIO()
        config = main.FunctionConfig(handler=handler, task_root=FUNCTION, timeout=overrides.pop("timeout", 3.0), **overrides)
        self.platform = main.Platform(config, self.sink, memory_probe=lambda pid: 42)
        self.addCleanup(self.platform.close)
        return self.platform

    def lines(self, prefix):
        return [line for line in self.sink.getvalue().splitlines() if line.startswith(prefix)]


class LifecycleTests(PlatformCase):
    def test_first_invoke_is_cold_with_init_duration_and_the_second_reuses_the_process(self):
        platform = self.make()
        first = platform.invoke(event(name="cc"))
        second = platform.invoke(event(name="cc"))
        self.assertEqual(first.json(), {"message": "hello cc", "cold": True, "invocations": 1})
        self.assertEqual(second.json(), {"message": "hello cc", "cold": False, "invocations": 2})
        self.assertTrue(first.cold and first.init_ms > 0)
        self.assertFalse(second.cold)
        self.assertIsNone(second.init_ms)
        self.assertEqual(first.environment, second.environment)
        reports = self.lines("REPORT")
        self.assertEqual(len(reports), 2)
        self.assertIn("Init Duration", reports[0])
        self.assertNotIn("Init Duration", reports[1])
        self.assertIn("Max Memory Used: 42 MB", reports[1])
        self.assertEqual(first.billed_ms, main.billed_ms(first.duration_ms, first.init_ms))
        self.assertTrue(any("\t" + first.request_id + "\taction=hello cold=True" in line for line in self.sink.getvalue().splitlines()))

    def test_a_handler_error_is_a_200_with_the_error_body_and_the_environment_stays_warm(self):
        platform = self.make()
        platform.invoke(event(name="cc"))
        failed = platform.invoke(event(action="fail", reason="missing order id"))
        self.assertEqual(failed.status, 200)
        self.assertEqual(failed.headers["X-Amz-Function-Error"], "Unhandled")
        body = failed.json()
        self.assertEqual((body["errorType"], body["errorMessage"], body["requestId"]), ("ValueError", "bad input: missing order id", failed.request_id))
        after = platform.invoke(event(name="cc"))
        self.assertEqual(after.json()["cold"], False)
        self.assertEqual(after.json()["invocations"], 3)
        self.assertNotIn("Status:", self.lines("REPORT")[1])

    def test_a_timeout_kills_the_process_and_the_next_invoke_is_a_suppressed_init(self):
        platform = self.make(timeout=0.6)
        platform.invoke(event(name="cc"))
        timed_out = platform.invoke(event(action="sleep", seconds=5))
        self.assertEqual(timed_out.outcome, "timeout")
        self.assertEqual(timed_out.json(), {"errorType": "Sandbox.Timedout", "errorMessage": f"RequestId: {timed_out.request_id} Error: Task timed out after 0.60 seconds"})
        self.assertGreaterEqual(timed_out.duration_ms, 600)
        self.assertLess(timed_out.duration_ms, 1500)
        after = platform.invoke(event(name="cc"))
        self.assertEqual(after.json(), {"message": "hello cc", "cold": True, "invocations": 1})
        self.assertTrue(after.cold)
        self.assertIsNone(after.init_ms)
        reports = self.lines("REPORT")
        self.assertTrue(reports[1].endswith("Status: timeout"))
        self.assertNotIn("Init Duration", reports[2])
        self.assertEqual(len(self.lines("INIT_REPORT")), 0)
        self.assertTrue(any("Task timed out after 0.60 seconds" in line for line in self.sink.getvalue().splitlines()))

    def test_a_runtime_that_exits_mid_invoke_is_runtime_exit_error(self):
        platform = self.make()
        crashed = platform.invoke(event(action="exit", code=3))
        self.assertEqual(crashed.json()["errorType"], "Runtime.ExitError")
        self.assertIn("Runtime exited with error: exit status 3", crashed.json()["errorMessage"])
        self.assertTrue(self.lines("REPORT")[0].endswith("Status: error\tError Type: Runtime.ExitError"))
        self.assertTrue(platform.invoke(event(name="cc")).json()["cold"])

    def test_an_import_error_fails_init_twice_and_returns_the_runtimes_payload(self):
        platform = self.make(handler="broken.handler")
        result = platform.invoke(event(name="cc"))
        body = result.json()
        self.assertEqual(result.headers["X-Amz-Function-Error"], "Unhandled")
        self.assertEqual(body["errorType"], "Runtime.ImportModuleError")
        self.assertEqual(body["errorMessage"], "Unable to import module 'broken': No module named 'cc_dependency_not_in_the_package'")
        reports = self.lines("INIT_REPORT")
        self.assertEqual([("Phase: init" in r, "Phase: invoke" in r) for r in reports], [(True, False), (False, True)])
        self.assertTrue(all("Status: error\tError Type: Runtime.ImportModuleError" in r for r in reports))
        self.assertTrue(self.lines("REPORT")[0].endswith("Error Type: Runtime.ImportModuleError"))
        self.assertLess(self.sink.getvalue().index("Phase: init"), self.sink.getvalue().index("START RequestId"))

    def test_an_exception_at_import_keeps_its_python_type_but_the_platform_calls_it_unknown(self):
        platform = self.make(handler="needs_env.handler")
        body = platform.invoke(event()).json()
        self.assertEqual((body["errorType"], body["errorMessage"]), ("KeyError", "'DATABASE_URL'"))
        self.assertIn("Error Type: Runtime.Unknown", self.lines("INIT_REPORT")[0])
        fixed = self.make(handler="needs_env.handler", environment={"DATABASE_URL": "postgres://app:secret@db.internal:5432/orders"})
        self.assertEqual(fixed.invoke(event()).json(), {"database": "db.internal:5432/orders"})

    def test_a_missing_bootstrap_is_an_invalid_entrypoint(self):
        platform = self.make(command=["/nonexistent/cc/bootstrap"])
        body = platform.invoke(event()).json()
        self.assertEqual(body["errorType"], "Runtime.InvalidEntrypoint")
        self.assertIn("Couldn't find valid bootstrap(s)", body["errorMessage"])

    def test_an_oversized_response_is_refused_with_413_and_reported_as_a_function_error(self):
        platform = self.make(max_response_bytes=1000)
        body = platform.invoke(event(action="big", bytes=5000)).json()
        self.assertEqual(body, {"errorType": "Function.ResponseSizeTooLarge", "errorMessage": "Response payload size exceeded maximum allowed payload size (1000 bytes)."})
        self.assertTrue(wait_until(lambda: "HTTP 413" in self.sink.getvalue()))
        self.assertFalse(platform.invoke(event(name="cc")).cold)


class ConcurrencyTests(PlatformCase):
    def start_slow(self, platform, seconds=0.8):
        results = []
        thread = threading.Thread(target=lambda: results.append(platform.invoke(event(action="sleep", seconds=seconds))))
        thread.start()
        self.assertTrue(wait_until(lambda: any(env.current is not None for env in platform.envs)), "the slow invocation never reached the runtime")
        return thread, results

    def test_two_concurrent_invokes_get_two_environments(self):
        platform = self.make()
        thread, results = self.start_slow(platform)
        second = platform.invoke(event(name="cc"))
        thread.join()
        self.assertEqual(second.json()["cold"], True)
        self.assertNotEqual(second.environment, results[0].environment)
        self.assertEqual(len(platform.envs), 2)

    def test_reserved_concurrency_throttles_with_429_and_no_function_logs(self):
        platform = self.make(reserved_concurrency=1)
        thread, results = self.start_slow(platform)
        throttled = platform.invoke(event(name="cc"))
        thread.join()
        self.assertEqual(throttled.status, 429)
        self.assertEqual(throttled.headers["X-Amzn-ErrorType"], "TooManyRequestsException")
        self.assertEqual(throttled.json(), {"Reason": "ReservedFunctionConcurrentInvocationLimitExceeded", "Type": "User", "message": "Rate Exceeded."})
        self.assertNotIn(throttled.request_id, self.sink.getvalue())
        self.assertEqual(results[0].status, 200)

    def test_reserved_concurrency_zero_turns_the_function_off(self):
        platform = self.make(reserved_concurrency=0)
        self.assertEqual(platform.invoke(event()).status, 429)
        self.assertEqual(platform.envs, [])

    def test_provisioned_environments_are_initialised_before_the_first_invoke(self):
        platform = self.make()
        durations = platform.prewarm(1)
        self.assertTrue(durations[0] > 0)
        self.assertEqual(len(self.lines("INIT_REPORT Init Duration")), 1)
        result = platform.invoke(event(action="env", name="AWS_LAMBDA_INITIALIZATION_TYPE"))
        self.assertFalse(result.cold)
        self.assertIsNone(result.init_ms)
        self.assertEqual(result.json()["value"], "provisioned-concurrency")
        self.assertNotIn("Init Duration", self.lines("REPORT")[0])

    def test_an_environment_idle_longer_than_the_ttl_is_destroyed(self):
        platform = self.make(idle_ttl=0.2)
        first = platform.invoke(event(name="cc"))
        time.sleep(0.4)
        second = platform.invoke(event(name="cc"))
        self.assertTrue(second.cold)
        self.assertIsNotNone(second.init_ms)
        self.assertNotEqual(first.environment, second.environment)


@unittest.skipUnless(hasattr(signal, "SIGSTOP"), "freezing needs SIGSTOP and SIGCONT")
class FreezeTests(PlatformCase):
    def ticks(self, path):
        return os.path.getsize(path) if os.path.exists(path) else 0

    def test_an_idle_environment_is_frozen_so_background_threads_stop(self):
        platform = self.make()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ticks.txt")
            platform.invoke(event(action="background", path=path))
            self.assertTrue(wait_until(lambda: platform.envs[0].frozen))
            frozen_at = self.ticks(path)
            time.sleep(0.4)
            self.assertEqual(self.ticks(path), frozen_at)
            platform.invoke(event(action="sleep", seconds=0.4))
            self.assertGreater(self.ticks(path), frozen_at)

    def test_without_freezing_background_threads_keep_running_between_invokes(self):
        platform = self.make(freeze=False)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ticks.txt")
            platform.invoke(event(action="background", path=path))
            time.sleep(0.1)
            before = self.ticks(path)
            time.sleep(0.4)
            self.assertGreater(self.ticks(path), before)
            self.assertFalse(platform.envs[0].frozen)


class LogFormatTests(PlatformCase):
    def test_json_log_format_emits_platform_records_and_json_function_logs(self):
        platform = self.make(log_format="JSON")
        result = platform.invoke(event(name="cc"))
        records = [json.loads(line) for line in self.sink.getvalue().splitlines()]
        types = [r.get("type") or r.get("level") for r in records]
        self.assertEqual(types, ["platform.start", "INFO", "platform.report"])
        self.assertEqual(records[1]["requestId"], result.request_id)
        report = records[2]["record"]
        self.assertEqual((report["requestId"], report["status"]), (result.request_id, "success"))
        self.assertIn("initDurationMs", report["metrics"])
        summary = main.summarize(main.parse_log_line(line) for line in self.sink.getvalue().splitlines() if main.parse_log_line(line))
        self.assertEqual((summary["invocations"], summary["cold_starts"]), (1, 1))


class RuntimeAPIProtocolTests(PlatformCase):
    def test_be_the_runtime_yourself_over_http(self):
        platform = self.make(command=[sys.executable, "-c", "import time; time.sleep(30)"], timeout=5.0)
        results = []
        thread = threading.Thread(target=lambda: results.append(platform.invoke(event(name="by hand"), request_id="req-by-hand")))
        thread.start()
        self.assertTrue(wait_until(lambda: bool(platform.envs) and platform.envs[0].alive))
        base = f"http://127.0.0.1:{platform.envs[0].port}/2018-06-01/runtime"
        before = int(time.time() * 1000)
        status, headers, body = http("GET", base + "/invocation/next")
        self.assertEqual((status, json.loads(body)), (200, {"name": "by hand"}))
        self.assertEqual(headers["lambda-runtime-aws-request-id"], "req-by-hand")
        self.assertEqual(headers["lambda-runtime-invoked-function-arn"], "arn:aws:lambda:us-east-1:000000000000:function:minilambda")
        self.assertTrue(before + 4000 <= int(headers["lambda-runtime-deadline-ms"]) <= before + 5500)
        self.assertTrue(headers["lambda-runtime-trace-id"].startswith("Root=1-"))
        self.assertEqual(http("POST", base + "/init/error", b"{}")[0], 403)
        self.assertEqual(http("POST", base + "/invocation/someone-else/response", b'"x"')[0], 400)
        self.assertEqual(http("POST", base + "/invocation/req-by-hand/response", b'"x"', {"Lambda-Runtime-Invocation-Id": "stale"})[0], 400)
        self.assertEqual(http("POST", base + "/invocation/req-by-hand/response", b'{"answer": 42}', {"Lambda-Runtime-Invocation-Id": headers["lambda-runtime-invocation-id"]})[0], 202)
        self.assertEqual(http("POST", base + "/invocation/req-by-hand/response", b'"again"')[0], 403)
        thread.join(5)
        self.assertEqual(results[0].json(), {"answer": 42})
        self.assertEqual(results[0].request_id, "req-by-hand")


class InvokeAPITests(PlatformCase):
    def setUp(self):
        platform = self.make(name="checkout")
        self.server = main.InvokeServer(("127.0.0.1", 0), platform)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def url(self, name="function"):
        return f"{self.base}/2015-03-31/functions/{name}/invocations"

    def test_the_rie_path_and_the_aws_cli_path_both_invoke_with_a_log_tail(self):
        status, headers, body = http("POST", self.url(), event(name="cc"), {"X-Amz-Log-Type": "Tail"})
        self.assertEqual((status, json.loads(body)["message"]), (200, "hello cc"))
        log = base64.b64decode(headers["x-amz-log-result"]).decode()
        self.assertTrue(log.startswith("START RequestId: " + headers["x-amzn-requestid"]))
        self.assertIn("REPORT RequestId", log)
        self.assertEqual(headers["x-amz-executed-version"], "$LATEST")
        status, headers, _ = http("POST", self.url("arn:aws:lambda:us-east-1:000000000000:function:checkout"), event(name="cc"))
        self.assertEqual((status, headers["x-minilambda-cold"]), (200, "false"))

    def test_bad_json_unknown_functions_and_dry_runs(self):
        status, headers, body = http("POST", self.url(), b"{not json")
        self.assertEqual((status, headers["x-amzn-errortype"]), (400, "InvalidRequestContentException"))
        status, headers, body = http("POST", self.url("payments"), b"{}")
        self.assertEqual((status, headers["x-amzn-errortype"]), (404, "ResourceNotFoundException"))
        self.assertEqual(http("POST", self.url("checkout:7"), b"{}")[0], 404)
        self.assertEqual(http("POST", self.url(), b"{}", {"X-Amz-Invocation-Type": "DryRun"})[0], 204)
        self.assertEqual(self.platform.envs, [])

    def test_the_cli_client_prints_the_body_and_exits_1_on_a_function_error(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main.main(["invoke", "--url", self.base, "--payload", '{"name": "cli"}', "--json"]), 0)
            self.assertEqual(main.main(["invoke", "--url", self.base, "--payload", '{"action": "fail"}', "--json"]), 1)
        first, second = [json.loads(chunk) for chunk in out.getvalue().replace("}\n{", "}\x00{").split("\x00")]
        self.assertEqual((first["status"], first["body"]["message"], first["cold"]), (200, "hello cli", "true"))
        self.assertEqual((second["function_error"], second["body"]["errorType"]), ("Unhandled", "ValueError"))

    def test_state_lists_environments(self):
        http("POST", self.url(), b"{}")
        status, _, body = http("GET", self.base + "/minilambda/state")
        state = json.loads(body)
        self.assertEqual(state["function"], "arn:aws:lambda:us-east-1:000000000000:function:checkout")
        self.assertEqual([env["status"] for env in state["environments"]], ["idle"])


class CommandLineTests(unittest.TestCase):
    def test_report_command_summarises_a_log_file(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main.main(["report", REPORTS, "--json"]), 0)
        summary = json.loads(out.getvalue())
        self.assertEqual((summary["invocations"], summary["cold_starts"], summary["cold_start_rate"]), (7, 4, round(4 / 7, 4)))

    def test_bad_configuration_exits_2_with_the_reason(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main.main(["serve", "--handler", "app.handler", "--task-root", FUNCTION, "--timeout", "0"]), 2)
            self.assertEqual(main.main(["serve", "--handler", "app.handler", "--task-root", "/nonexistent/cc"]), 2)
            self.assertEqual(main.main(["serve", "--handler", "app.handler", "--task-root", FUNCTION, "--env", "NOEQUALS"]), 2)
        self.assertIn("--timeout must be between 0 and 900", err.getvalue())
        self.assertIn("is not a directory", err.getvalue())
        self.assertIn("KEY=VALUE", err.getvalue())


if __name__ == "__main__":
    unittest.main()
