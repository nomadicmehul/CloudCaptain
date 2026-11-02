import copy
import io
import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

HERE = os.path.dirname(__file__)
FIXTURE = os.path.join(HERE, "..", "fixtures", "kind-node.json")
KUBE_BENCH = os.path.join(HERE, "..", "fixtures", "kube-bench-sample.json")
KUBELET_CONFIG = "/var/lib/kubelet/config.yaml"
APISERVER = "/etc/kubernetes/manifests/kube-apiserver.yaml"
NOW = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)

EXPECTED_ON_KIND = {
    "FAIL": {"1.1.12", "1.2.5", "1.2.15", "1.2.16", "1.2.17", "1.2.18", "1.2.19", "1.2.30", "1.3.2", "1.4.1", "4.1.1", "4.1.9"},
    "WARN": {"1.2.1", "1.2.3", "1.2.9", "1.2.11", "1.2.20", "1.2.27", "1.2.29", "1.3.1", "3.1.1", "3.1.2", "3.1.3", "3.2.1", "3.2.2",
             "4.2.9", "4.2.12", "4.2.13", "4.2.14", "legacy-4.2.6"},
}


def fixture():
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


def catalogue():
    return main.load_catalogue()


def run(data, **kwargs):
    return main.run_checks(catalogue(), main.Snapshot(data), **kwargs)


def states(report):
    return {r.check.id: r.state for r in report.results}


def result(report, check_id):
    return next(r for r in report.results if r.check.id == check_id)


def set_kubelet_config(data, text):
    data["files"][KUBELET_CONFIG]["content"] = text


def edit_kubelet_config(data, old, new):
    content = data["files"][KUBELET_CONFIG]["content"]
    assert content.count(old) == 1, old
    set_kubelet_config(data, content.replace(old, new))


def cli(*argv, runner=main.subprocess_runner):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(list(argv), runner=runner)
    return code, out.getvalue(), err.getvalue()


def write_json(directory, name, data):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    return path


class FakeNode:
    def __init__(self, files, processes, reachable=True):
        self.files = files
        self.processes = processes
        self.reachable = reachable
        self.calls = []

    def stat_line(self, path):
        entry = self.files[path]
        kind = {"dir": "directory", "file": "regular file"}[entry["type"]]
        return f"{entry['mode']}|{entry['owner']}|{entry['group']}|{kind}|{path}"

    def __call__(self, argv):
        self.calls.append(list(argv))
        assert argv[:3] == ["docker", "exec", "cc-lab-control-plane"], argv
        command = argv[3:]
        if not self.reachable:
            return main.CommandResult(1, "", "Error response from daemon: No such container: cc-lab-control-plane")
        if command == ["true"]:
            return main.CommandResult(0)
        if command[0] == "cat":
            entry = self.files.get(command[-1])
            return main.CommandResult(0, entry["content"]) if entry and "content" in entry else main.CommandResult(1, "", "No such file")
        if command[0] == "stat":
            path = command[-1]
            return main.CommandResult(0, self.stat_line(path) + "\n") if path in self.files else main.CommandResult(1, "", "No such file")
        if command[0] == "find":
            root = command[1]
            lines = [self.stat_line(p) for p in sorted(self.files) if p == root or p.startswith(root + "/")]
            return main.CommandResult(0 if lines else 1, "\n".join(lines) + "\n" if lines else "", "" if lines else "No such file")
        if command[:2] == ["sh", "-c"] and command[2] == main.PROC_SCRIPT:
            text = "".join(main.ARG_SEPARATOR.join(argv) + main.ARG_SEPARATOR + "\n" for argv in self.processes)
            return main.CommandResult(0, "\n" + text)
        return main.CommandResult(127, "", f"unexpected {command}")


def fake_node_from_fixture():
    data = fixture()
    files = {path: entry for path, entry in data["files"].items() if entry.get("exists")}
    for entry in data["trees"][main.PKI_DIR]:
        files[entry["path"]] = entry
    return FakeNode(files, list(data["processes"].values()))


class YamlAndFlagTests(unittest.TestCase):
    def test_the_kubeadm_manifest_parses_into_the_container_command(self):
        pod = main.parse_yaml(fixture()["files"][APISERVER]["content"])
        argv = main.container_argv(pod)
        self.assertEqual(argv[0], "kube-apiserver")
        self.assertIn("--authorization-mode=Node,RBAC", argv)
        self.assertIn("--runtime-config=", argv)
        self.assertEqual(pod["status"], {})

    def test_flags_parse_like_pflag(self):
        flags = main.parse_flags(["kubelet", "--config=/x.yaml", "--v", "2", "--anonymous-auth", "positional", "--runtime-config=", "--x=1", "--x=2"])
        self.assertEqual(flags["--config"], "/x.yaml")
        self.assertEqual(flags["--v"], "2")
        self.assertEqual(flags["--anonymous-auth"], "true")
        self.assertEqual(flags["--runtime-config"], "")
        self.assertEqual(flags["--x"], "2")

    def test_go_durations(self):
        self.assertEqual(main.parse_duration("0s"), 0)
        self.assertEqual(main.parse_duration("0"), 0)
        self.assertEqual(main.parse_duration("4h0m0s"), 4 * 3600)
        self.assertEqual(main.parse_duration("1m30s"), 90)
        self.assertEqual(main.parse_duration("300ms"), 0.3)
        self.assertIsNone(main.parse_duration("5"))
        self.assertIsNone(main.parse_duration("forever"))


class CatalogueTests(unittest.TestCase):
    def test_every_check_is_valid_data_with_a_cis_section(self):
        cat = catalogue()
        self.assertEqual(cat.benchmark, "CIS Kubernetes Benchmark v1.12.0")
        ids = [c.id for c in cat.checks]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 90)
        for check in cat.checks:
            self.assertTrue(main.SECTION_RE.match(check.id) or check.group == "legacy", check.id)
            self.assertIn(check.target, main.TARGETS)
            self.assertTrue(check.remediation, check.id)
            self.assertTrue(check.title.endswith(("(Automated)", "(Manual)")), check.id)
        self.assertEqual(ids, sorted(ids, key=main.section_key))

    def test_a_broken_catalogue_is_refused_with_the_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_json(tmp, "checks.json", {"groups": {"1.2": {"title": "API Server", "target": "master"}},
                                                   "checks": [{"id": "1.2.15", "title": "t", "audit": {"component": "kube-apiserver", "flag": "--profiling"}, "expect": {"op": "equals"}}]})
            with self.assertRaises(main.BenchError) as caught:
                main.load_catalogue(path)
            self.assertIn("expect.op", str(caught.exception))
            code, _, err = cli("checks", "--checks", path)
            self.assertEqual(code, 2)
            self.assertIn("1.2.15", err)


class EvaluationTests(unittest.TestCase):
    def test_the_kind_fixture_gives_exactly_the_expected_states(self):
        report = run(fixture())
        got = states(report)
        self.assertEqual({i for i, s in got.items() if s == "FAIL"}, EXPECTED_ON_KIND["FAIL"])
        self.assertEqual({i for i, s in got.items() if s == "WARN"}, EXPECTED_ON_KIND["WARN"])
        self.assertEqual(report.totals(), {"PASS": 61, "WARN": 18, "FAIL": 12})
        self.assertEqual(report.drift, [])
        self.assertEqual(report.snapshot.problems, [])

    def test_an_unset_flag_is_judged_by_its_documented_default(self):
        profiling = result(run(fixture()), "1.2.15")
        self.assertEqual(profiling.state, "FAIL")
        self.assertEqual(profiling.value, "true")
        self.assertIn("--profiling not set; the default is true", profiling.evidence[0])
        data = fixture()
        data["files"][APISERVER]["content"] = data["files"][APISERVER]["content"].replace("    - --allow-privileged=true\n", "    - --allow-privileged=true\n    - --profiling=false\n")
        self.assertEqual(states(run(data))["1.2.15"], "PASS")

    def test_unscored_checks_warn_instead_of_failing_and_manual_checks_always_warn(self):
        report = run(fixture())
        self.assertEqual(result(report, "1.2.1").state, "WARN")
        self.assertFalse(result(report, "1.2.1").check.scored)
        manual = result(report, "3.2.2")
        self.assertEqual(manual.state, "WARN")
        self.assertTrue(manual.evidence[0].startswith("manual: "))

    def test_has_matches_list_elements_not_substrings(self):
        data = fixture()
        data["files"][APISERVER]["content"] = data["files"][APISERVER]["content"].replace("--authorization-mode=Node,RBAC", "--authorization-mode=RBAC,NodeX")
        got = states(run(data))
        self.assertEqual(got["1.2.7"], "FAIL")
        self.assertEqual(got["1.2.8"], "PASS")

    def test_the_etcd_ca_must_differ_from_the_api_server_client_ca(self):
        data = fixture()
        manifest = "/etc/kubernetes/manifests/etcd.yaml"
        data["files"][manifest]["content"] = data["files"][manifest]["content"].replace("--trusted-ca-file=/etc/kubernetes/pki/etcd/ca.crt", "--trusted-ca-file=/etc/kubernetes/pki/ca.crt")
        unique = result(run(data), "2.7")
        self.assertEqual(unique.state, "WARN")
        self.assertIn("not unique", unique.evidence[-1])

    def test_a_running_process_is_used_when_there_is_no_manifest(self):
        data = fixture()
        del data["files"][APISERVER]
        report = run(data)
        self.assertEqual(states(report)["1.2.15"], "FAIL")
        self.assertIn("running process", result(report, "1.2.15").evidence[0])
        self.assertEqual(states(report)["1.1.1"], "FAIL")
        self.assertIn("not found", result(report, "1.1.1").evidence[0])

    def test_drift_between_manifest_and_process_is_reported(self):
        data = fixture()
        data["processes"]["kube-apiserver"] = data["processes"]["kube-apiserver"] + ["--profiling=false"]
        found = main.drift(main.Snapshot(data))
        self.assertEqual(found, [{"component": "kube-apiserver", "flags": ["--profiling"], "manifest": {"--profiling": None}, "process": {"--profiling": "false"}}])


class KubeletTests(unittest.TestCase):
    def test_a_kubelet_flag_overrides_the_config_file(self):
        data = fixture()
        data["processes"]["kubelet"].append("--anonymous-auth=true")
        anonymous = result(run(data), "4.2.1")
        self.assertEqual((anonymous.state, anonymous.value), ("FAIL", "true"))
        self.assertIn("a flag overrides the config file", anonymous.evidence[0])

    def test_without_a_config_file_the_flag_defaults_apply(self):
        data = fixture()
        data["processes"]["kubelet"] = [arg for arg in data["processes"]["kubelet"] if not arg.startswith("--config=")]
        data["files"][KUBELET_CONFIG] = {"exists": False}
        got = run(data)
        self.assertEqual((result(got, "4.2.1").state, result(got, "4.2.1").value), ("FAIL", "true"))
        self.assertEqual((result(got, "4.2.2").state, result(got, "4.2.2").value), ("FAIL", "AlwaysAllow"))
        self.assertEqual((result(got, "4.2.4").state, result(got, "4.2.4").value), ("WARN", "10255"))
        self.assertEqual(result(got, "4.1.9").state, "PASS")

    def test_a_config_file_the_kubelet_does_not_load_is_ignored(self):
        data = fixture()
        data["processes"]["kubelet"] = [arg for arg in data["processes"]["kubelet"] if not arg.startswith("--config=")]
        got = run(data)
        anonymous = result(got, "4.2.1")
        self.assertEqual((anonymous.state, anonymous.value), ("FAIL", "true"))
        self.assertIn("runs without --config", anonymous.evidence[0])
        self.assertEqual(result(got, "4.2.2").value, "AlwaysAllow")
        self.assertEqual(result(got, "4.1.9").state, "FAIL")

    def test_a_missing_key_takes_the_config_file_default_not_the_flag_default(self):
        data = fixture()
        edit_kubelet_config(data, "  anonymous:\n    enabled: false\n", "")
        anonymous = result(run(data), "4.2.1")
        self.assertEqual((anonymous.state, anonymous.value), ("PASS", "false"))
        self.assertIn("config file default is false", anonymous.evidence[0])

    def test_zero_streaming_timeout_in_the_file_means_the_default_but_a_zero_flag_means_zero(self):
        data = fixture()
        timeout = result(run(data), "4.2.5")
        self.assertEqual((timeout.state, timeout.value), ("PASS", "4h0m0s"))
        self.assertIn("replaces with its default", timeout.evidence[0])
        data["processes"]["kubelet"].append("--streaming-connection-idle-timeout=0")
        self.assertEqual(result(run(data), "4.2.5").state, "WARN")

    def test_pids_limit_of_minus_one_is_not_a_limit(self):
        data = fixture()
        edit_kubelet_config(data, "rotateCertificates: true\n", "podPidsLimit: -1\nrotateCertificates: true\n")
        self.assertEqual(result(run(data), "4.2.13").state, "WARN")
        edit_kubelet_config(data, "podPidsLimit: -1\n", "podPidsLimit: 4096\n")
        self.assertEqual(result(run(data), "4.2.13").state, "PASS")

    def test_a_kubelet_config_in_json_is_read_too(self):
        data = fixture()
        set_kubelet_config(data, json.dumps({"kind": "KubeletConfiguration", "authentication": {"anonymous": {"enabled": True}}, "readOnlyPort": 10255}))
        got = run(data)
        self.assertEqual(result(got, "4.2.1").state, "FAIL")
        self.assertEqual(result(got, "4.2.4").value, "10255")


class FileTests(unittest.TestCase):
    def test_modes_are_a_bitmask_not_a_number(self):
        self.assertTrue(main.mode_within("600", "600"))
        self.assertTrue(main.mode_within("400", "600"))
        self.assertFalse(main.mode_within("640", "600"))
        self.assertFalse(main.mode_within("644", "600"))
        self.assertTrue(main.mode_within("700", "700"))
        self.assertFalse(main.mode_within("1700", "700"))

    def test_one_bad_key_in_the_pki_tree_is_named_in_the_evidence(self):
        data = fixture()
        for entry in data["trees"][main.PKI_DIR]:
            if entry["path"].endswith("/etcd/server.key"):
                entry["mode"] = "644"
        keys = result(run(data), "1.1.21")
        self.assertEqual(keys.state, "WARN")
        self.assertEqual(keys.evidence[0], "/etc/kubernetes/pki/etcd/server.key: mode 644")
        self.assertIn("of 11 entries pass", keys.evidence[-1])

    def test_missing_files_follow_each_checks_rule(self):
        data = fixture()
        for path in main.KUBELET_SERVICE_FILES:
            data["files"][path] = {"exists": False}
        got = states(run(data))
        self.assertEqual(got["4.1.1"], "FAIL")
        self.assertEqual(got["4.1.2"], "PASS")

    def test_the_service_file_is_the_first_candidate_that_exists(self):
        data = fixture()
        data["files"]["/etc/systemd/system/kubelet.service.d/10-kubeadm.conf"] = {"exists": False}
        data["files"]["/etc/systemd/system/kubelet.service"]["mode"] = "600"
        service = result(run(data), "4.1.1")
        self.assertEqual(service.state, "PASS")
        self.assertIn("/etc/systemd/system/kubelet.service: mode 600", service.evidence[0])


class TargetTests(unittest.TestCase):
    def worker(self):
        data = fixture()
        for component in main.CONTROL_PLANE:
            data["processes"].pop(component)
            data["files"].pop(main.MANIFESTS[component])
        return data

    def test_a_worker_node_runs_only_node_checks(self):
        report = run(self.worker())
        self.assertEqual({r.check.target for r in report.results}, {"node"})
        self.assertEqual(len(report.skipped), 91 - len(report.results))

    def test_asking_for_a_target_the_node_does_not_run_is_an_error(self):
        with self.assertRaises(main.BenchError) as caught:
            run(self.worker(), targets=["master"])
        self.assertIn("kube-apiserver", str(caught.exception))

    def test_check_selection_takes_ids_and_groups(self):
        report = run(fixture(), selection=["1.2", "4.2.1"])
        ids = [r.check.id for r in report.results]
        self.assertIn("1.2.15", ids)
        self.assertIn("4.2.1", ids)
        self.assertNotIn("4.2.10", ids)
        self.assertNotIn("1.1.1", ids)


class CollectTests(unittest.TestCase):
    def test_collect_through_docker_exec_rebuilds_the_fixture_results(self):
        node = fake_node_from_fixture()
        data = main.collect(main.make_source("docker:cc-lab-control-plane", node), NOW)
        self.assertEqual(states(run(data)), states(run(fixture())))
        self.assertIn(["docker", "exec", "cc-lab-control-plane", "stat", "-c", main.STAT_FORMAT, "--", APISERVER], node.calls)
        self.assertEqual(data["processes"]["kubelet"][0], "/usr/bin/kubelet")
        self.assertEqual(data["collected_at"], "2026-11-02T08:00:00Z")

    def test_collect_never_copies_kubeconfig_or_key_contents(self):
        node = fake_node_from_fixture()
        node.files["/etc/kubernetes/admin.conf"]["content"] = "client-key-data: c2VjcmV0"
        data = main.collect(main.make_source("docker:cc-lab-control-plane", node), NOW)
        self.assertNotIn("content", data["files"]["/etc/kubernetes/admin.conf"])
        self.assertNotIn("c2VjcmV0", json.dumps(data))
        with_content = sorted(p for p, e in data["files"].items() if "content" in e)
        self.assertEqual(with_content, sorted(list(main.MANIFESTS.values()) + [KUBELET_CONFIG]))

    def test_an_unreachable_node_is_an_error_not_an_empty_snapshot(self):
        node = FakeNode({}, [], reachable=False)
        code, _, err = cli("collect", "--node", "docker:cc-lab-control-plane", runner=node)
        self.assertEqual(code, 2)
        self.assertIn("No such container", err)

    def test_tree_source_reads_a_copied_node_filesystem(self):
        with tempfile.TemporaryDirectory() as root:
            manifests = os.path.join(root, "etc/kubernetes/manifests")
            os.makedirs(manifests)
            with open(os.path.join(manifests, "kube-apiserver.yaml"), "w", encoding="utf-8") as handle:
                handle.write(fixture()["files"][APISERVER]["content"])
            os.chmod(os.path.join(manifests, "kube-apiserver.yaml"), 0o644)
            os.makedirs(os.path.join(root, "proc/42"))
            with open(os.path.join(root, "proc/42/cmdline"), "wb") as handle:
                handle.write(b"/usr/bin/kubelet\0--config=/var/lib/kubelet/config.yaml\0")
            data = main.collect(main.make_source(f"root:{root}"), NOW)
            self.assertEqual(data["processes"]["kubelet"], ["/usr/bin/kubelet", "--config=/var/lib/kubelet/config.yaml"])
            self.assertEqual(data["files"][APISERVER]["mode"], "644")
            report = run(data, selection=["1.1.1", "1.2.15"])
            self.assertEqual(states(report), {"1.1.1": "FAIL", "1.2.15": "FAIL"})

    def test_bad_node_names_are_explained(self):
        with self.assertRaises(main.BenchError) as caught:
            main.make_source("ssh:node-1")
        self.assertIn("docker:CONTAINER", str(caught.exception))


class ReportTests(unittest.TestCase):
    def test_json_report_carries_value_evidence_and_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = cli("run", write_json(tmp, "node.json", fixture()), "--json")
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual(report["totals"], {"PASS": 61, "WARN": 18, "FAIL": 12})
        anonymous = next(r for r in report["results"] if r["id"] == "4.2.1")
        self.assertEqual(anonymous["value"], "false")
        self.assertEqual(anonymous["evidence"], ["/var/lib/kubelet/config.yaml: authentication.anonymous.enabled: false"])

    def test_junit_has_one_case_per_check_with_failures_and_skips(self):
        xml = main.render_junit(run(fixture()))
        root = ET.fromstring(xml)
        cases = root.findall(".//testcase")
        self.assertEqual(len(cases), 91)
        self.assertEqual(len(root.findall(".//failure")), 12)
        self.assertEqual(len(root.findall(".//skipped")), 18)
        self.assertEqual(root.get("failures"), "12")
        profiling = next(c for c in cases if c.get("name").startswith("1.2.15 "))
        self.assertIn("--profiling=false", profiling.find("failure").get("message"))

    def test_fail_on_controls_the_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_json(tmp, "node.json", fixture())
            self.assertEqual(cli("run", path)[0], 1)
            self.assertEqual(cli("run", path, "--fail-on", "none")[0], 0)
            self.assertEqual(cli("run", path, "--check", "4.2.1,4.2.2")[0], 0)
            self.assertEqual(cli("run", path, "--check", "4.2.13", "--fail-on", "warn")[0], 1)
            self.assertEqual(cli("run", os.path.join(tmp, "missing.json"))[0], 2)
            self.assertEqual(cli("run", write_json(tmp, "other.json", {"kind": "Pod"}))[0], 2)


class DiffTests(unittest.TestCase):
    def test_a_flipped_kubelet_setting_shows_up_as_a_regression(self):
        flipped = fixture()
        flipped["processes"]["kubelet"].append("--read-only-port=10255")
        edit_kubelet_config(flipped, "  anonymous:\n    enabled: false\n", "  anonymous:\n    enabled: true\n")
        with tempfile.TemporaryDirectory() as tmp:
            before = write_json(tmp, "before.json", run(fixture()).to_json())
            after = write_json(tmp, "after.json", run(flipped).to_json())
            code, out, _ = cli("diff", before, after, "--json")
            self.assertEqual(code, 1)
            changed = {c["id"]: (c["old"], c["new"]) for c in json.loads(out)["changed"]}
            self.assertEqual(changed, {"4.2.1": ("PASS", "FAIL"), "4.2.4": ("PASS", "WARN")})
            self.assertEqual(cli("diff", after, before)[0], 0)

    def test_kube_bench_json_is_compared_by_section_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            mine = write_json(tmp, "mine.json", run(fixture()).to_json())
            code, out, _ = cli("diff", mine, KUBE_BENCH, "--common", "--fail-on", "any", "--json")
            self.assertEqual(code, 0)
            result = json.loads(out)
            self.assertEqual((result["old"], result["new"], result["same"]), ("minibench", "kube-bench", 4))
            self.assertEqual([i["id"] for i in result["only_new"]], ["1.1.9"])
            self.assertEqual(cli("diff", mine, KUBE_BENCH, "--fail-on", "any")[0], 1)

    def test_a_disagreement_with_kube_bench_prints_both_sides_evidence(self):
        with open(KUBE_BENCH, encoding="utf-8") as handle:
            theirs = json.load(handle)
        theirs["Controls"][0]["tests"][1]["results"][0]["status"] = "PASS"
        with tempfile.TemporaryDirectory() as tmp:
            mine = write_json(tmp, "mine.json", run(fixture()).to_json())
            noisy = os.path.join(tmp, "kube-bench.log")
            with open(noisy, "w", encoding="utf-8") as handle:
                handle.write("I1102 08:00:00.000000 1 util.go:93] ps - proc\n" + json.dumps(theirs) + "\n")
            code, out, _ = cli("diff", mine, noisy, "--common", "--fail-on", "any")
        self.assertEqual(code, 1)
        self.assertIn("1.2.15    FAIL -> PASS", out)
        self.assertIn("old: kube-apiserver (manifest /etc/kubernetes/manifests/kube-apiserver.yaml): --profiling not set; the default is true", out)
        self.assertIn("new: expected: '--profiling' is present", out)

    def test_diff_refuses_files_that_are_neither_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = cli("diff", write_json(tmp, "a.json", {"x": 1}), KUBE_BENCH)
        self.assertEqual(code, 2)
        self.assertIn("neither a minibench report", err)


if __name__ == "__main__":
    unittest.main()
