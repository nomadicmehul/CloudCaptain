import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
DEFAULT_PROFILE = os.path.join(FIXTURES, "seccomp-docker-default-excerpt.json")
TINY_PROFILE = os.path.join(FIXTURES, "seccomp-tiny.json")
TRACE = os.path.join(FIXTURES, "strace-python-print.log")
INSPECT_DEFAULT = os.path.join(FIXTURES, "inspect-default.json")
INSPECT_HARDENED = os.path.join(FIXTURES, "inspect-hardened.json")
POD_VIOLATING = os.path.join(FIXTURES, "pod-violating.json")
POD_RESTRICTED = os.path.join(FIXTURES, "pod-restricted.json")
DEPLOYMENT = os.path.join(FIXTURES, "deployment.json")
DOCKER_DEFAULT = main.parse_caps("docker-default")

MESSY_TRACE = """strace: Process 4242 attached
4241  12:00:01.000100 execve("/usr/bin/sh", ["sh", "-c", "mkdir /tmp/x"], 0x7ffd /* 5 vars */) = 0
[pid  4241] brk(NULL)                     = 0x5555
[pid  4241] clone(child_stack=NULL, flags=CLONE_CHILD_CLEARTID|SIGCHLD <unfinished ...>
[pid  4242] execve("/bin/mkdir", ["mkdir", "/tmp/x"], 0x5555 /* 5 vars */) = 0
[pid  4241] <... clone resumed>, child_tidptr=0x7f00) = 4242
[pid  4242] mkdir("/tmp/x", 0777)         = -1 EPERM (Operation not permitted)
[pid  4242] write(2, "mkdir: cannot create", 20) = 20
[pid  4242] exit_group(1)                 = ?
[pid  4242] +++ exited with 1 +++
--- SIGCHLD {si_signo=SIGCHLD, si_code=CLD_EXITED, si_pid=4242, si_uid=0, si_status=1} ---
0.000120 wait4(-1, [{WIFEXITED(s) && WEXITSTATUS(s) == 1}], 0, NULL) = 4242
syscall_0x1ff(0x1, 0x2)                   = -1 ENOSYS (Function not implemented)
exit_group(1)                             = ?
+++ exited with 1 +++
"""


def run_main(argv, runner=None, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        if stdin is None:
            code = main.main(argv, runner)
        else:
            with mock.patch("sys.stdin", io.StringIO(stdin)):
                code = main.main(argv, runner)
    return code, out.getvalue(), err.getvalue()


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def by_check(findings):
    return {f.check: f for f in findings}


class SeccompProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = main.load_profile(DEFAULT_PROFILE)

    def test_parse_profile_reads_defaults_arches_and_rules(self):
        self.assertEqual((self.profile.default_action, self.profile.default_errno), ("SCMP_ACT_ERRNO", 1))
        self.assertEqual(self.profile.arches[:3], ["SCMP_ARCH_X86_64", "SCMP_ARCH_X86", "SCMP_ARCH_X32"])
        self.assertEqual(len(self.profile.rules), 20)
        self.assertEqual(self.profile.rules[7].includes, {"caps": ["CAP_SYS_ADMIN"]})
        legacy = main.parse_profile({"defaultAction": "SCMP_ACT_ALLOW", "syscalls": [{"name": "ptrace", "action": "SCMP_ACT_ERRNO"}]})
        self.assertEqual(legacy.rules[0].names, ["ptrace"])
        with self.assertRaises(main.HardenError):
            main.parse_profile({"syscalls": []})

    def test_resolve_follows_rules_default_action_and_capability_gates(self):
        mkdir = main.resolve(self.profile, "mkdir", DOCKER_DEFAULT)
        self.assertEqual((mkdir.action, mkdir.rule, mkdir.conditional), ("SCMP_ACT_ALLOW", 0, False))
        mount = main.resolve(self.profile, "mount", DOCKER_DEFAULT)
        self.assertEqual((mount.action, mount.rule, mount.unlock), ("SCMP_ACT_ERRNO", None, "allowed with CAP_SYS_ADMIN"))
        admin = main.resolve(self.profile, "mount", DOCKER_DEFAULT | {"CAP_SYS_ADMIN"})
        self.assertEqual((admin.action, admin.rule), ("SCMP_ACT_ALLOW", 7))
        bpf = main.resolve(self.profile, "bpf", DOCKER_DEFAULT)
        self.assertEqual(bpf.unlock, "allowed with CAP_SYS_ADMIN or CAP_BPF")
        self.assertEqual(main.resolve(self.profile, "chroot", DOCKER_DEFAULT).action, "SCMP_ACT_ALLOW")
        self.assertEqual(main.resolve(self.profile, "chroot", set()).action, "SCMP_ACT_ERRNO")

    def test_argument_filters_are_conditional_until_a_capability_lifts_them(self):
        clone = main.resolve(self.profile, "clone", DOCKER_DEFAULT)
        self.assertTrue(clone.conditional)
        self.assertEqual(clone.rule, 6)
        self.assertIn("unless CAP_SYS_ADMIN, only when arg0 & 0x7e020000 == 0", clone.reason)
        lifted = main.resolve(self.profile, "clone", DOCKER_DEFAULT | {"CAP_SYS_ADMIN"})
        self.assertEqual((lifted.conditional, lifted.rule), (False, 7))
        personality = main.resolve(self.profile, "personality", DOCKER_DEFAULT)
        self.assertEqual((personality.conditional, personality.reason), (True, "rule 2, only when arg0 eq 0"))

    def test_min_kernel_and_architecture_conditions(self):
        self.assertEqual(main.resolve(self.profile, "ptrace", DOCKER_DEFAULT).reason, "rule 1, kernel >= 4.8")
        self.assertEqual(main.resolve(self.profile, "ptrace", DOCKER_DEFAULT, kernel="4.4.0").action, "SCMP_ACT_ERRNO")
        self.assertEqual(main.resolve(self.profile, "ptrace", DOCKER_DEFAULT, kernel="6.8.0-45-generic").action, "SCMP_ACT_ALLOW")
        self.assertEqual(main.resolve(self.profile, "arch_prctl", DOCKER_DEFAULT, arch="amd64").action, "SCMP_ACT_ALLOW")
        self.assertEqual(main.resolve(self.profile, "arch_prctl", DOCKER_DEFAULT, arch="arm64").action, "SCMP_ACT_ERRNO")
        self.assertEqual(main.resolve(self.profile, "clone", DOCKER_DEFAULT, arch="s390x").action, "SCMP_ACT_ERRNO")
        self.assertTrue(main.kernel_at_least(None, "4.8"))
        self.assertFalse(main.kernel_at_least("3.10", "4.8"))

    def test_classify_counts_and_explicit_denies(self):
        groups = main.classify(self.profile, DOCKER_DEFAULT)
        self.assertEqual(groups["conditional"], ["clone", "personality"])
        self.assertIn("mkdir", groups["allowed"])
        self.assertIn("mount", groups["default"])
        self.assertEqual(groups["denied"], [])
        tiny = main.load_profile(TINY_PROFILE)
        tiny_groups = main.classify(tiny, DOCKER_DEFAULT)
        self.assertEqual(tiny_groups["denied"], ["mkdir", "mkdirat", "rmdir", "unlink"])
        self.assertEqual(len(tiny_groups["allowed"]), 28)

    def test_parse_caps_accepts_aliases_and_any_spelling(self):
        self.assertEqual(len(DOCKER_DEFAULT), 14)
        self.assertIn("CAP_NET_BIND_SERVICE", DOCKER_DEFAULT)
        self.assertEqual(main.parse_caps("none"), set())
        self.assertEqual(main.parse_caps("sys_admin, CAP_BPF net_admin"), {"CAP_SYS_ADMIN", "CAP_BPF", "CAP_NET_ADMIN"})
        self.assertEqual(main.parse_caps("docker-default,SYS_ADMIN"), DOCKER_DEFAULT | {"CAP_SYS_ADMIN"})

    def test_diff_profiles_lists_what_only_one_side_allows(self):
        result = main.diff_profiles(self.profile, main.load_profile(TINY_PROFILE), DOCKER_DEFAULT)
        self.assertEqual(result["both"], 28)
        self.assertIn("mkdir", result["only_a"])
        self.assertIn("chroot", result["only_a"])
        self.assertEqual(result["only_b"], [])
        self.assertNotIn("mount", result["only_a"])


class TraceTests(unittest.TestCase):
    def test_syscalls_in_trace_handles_prefixes_timestamps_and_noise(self):
        counts = main.syscalls_in_trace(MESSY_TRACE)
        self.assertEqual(counts["execve"], 2)
        self.assertEqual(counts["clone"], 1)
        self.assertEqual(counts["mkdir"], 1)
        self.assertEqual(counts["wait4"], 1)
        self.assertEqual(counts["exit_group"], 2)
        self.assertNotIn("strace", counts)
        self.assertNotIn("syscall_0x1ff", counts)
        self.assertFalse(any(name.startswith(("+", "-", "<")) for name in counts))

    def test_generate_profile_adds_the_always_needed_set_and_denies_the_rest(self):
        with open(TRACE, encoding="utf-8") as handle:
            counts = main.syscalls_in_trace(handle.read())
        self.assertEqual(len(counts), 32)
        profile, added = main.generate_profile(sorted(counts))
        self.assertIn("wait4", added)
        self.assertNotIn("execve", added)
        self.assertEqual(profile["defaultAction"], "SCMP_ACT_ERRNO")
        self.assertNotIn("architectures", profile)
        names = profile["syscalls"][0]["names"]
        self.assertEqual(names, sorted(set(names)))
        parsed = main.parse_profile(profile, "generated")
        self.assertEqual(main.resolve(parsed, "mkdir", set()).action, "SCMP_ACT_ERRNO")
        self.assertEqual(main.resolve(parsed, "execve", set()).action, "SCMP_ACT_ALLOW")
        self.assertEqual(main.resolve(parsed, "getdents64", set()).action, "SCMP_ACT_ALLOW")

    def test_generate_cli_writes_the_profile_and_reports_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "profile.json")
            code, stdout, _ = run_main(["generate", TRACE, "--out", out, "--errno", "38"])
            self.assertEqual(code, 0)
            self.assertIn("32 from the trace + 32 always needed = 64 allowed", stdout)
            self.assertIn("ENOSYS", stdout)
            self.assertIn(f"seccomp={out}", stdout)
            self.assertEqual(load(out)["defaultErrnoRet"], 38)
            code, stdout, stderr = run_main(["generate", TRACE])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout)["defaultAction"], "SCMP_ACT_ERRNO")
            self.assertIn("distinct syscalls", stderr)

    def test_generate_cli_rejects_a_file_without_syscalls(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = os.path.join(tmp, "notes.txt")
            with open(empty, "w", encoding="utf-8") as handle:
                handle.write("nothing traced here\n")
            code, _, stderr = run_main(["generate", empty])
            self.assertEqual(code, 2)
            self.assertIn("no syscall lines found", stderr)
            self.assertEqual(run_main(["generate", os.path.join(tmp, "missing.log")])[0], 2)


class InspectTests(unittest.TestCase):
    def test_default_container_fails_the_usual_checks(self):
        findings = by_check(main.inspect_findings(load(INSPECT_DEFAULT)[0]))
        failing = sorted(name for name, f in findings.items() if f.status == "fail")
        self.assertEqual(failing, ["capabilities", "memory-limit", "no-new-privileges", "pids-limit", "ports", "read-only-rootfs", "sensitive-mounts", "user"])
        self.assertEqual(findings["sensitive-mounts"].detail, "mounts /var/run/docker.sock (rw)")
        self.assertEqual(findings["ports"].detail, "published on every interface: 0.0.0.0:8080->80/tcp")
        self.assertEqual(findings["seccomp"].detail, "runtime default profile")
        self.assertEqual(main.score(findings.values()), 40)
        self.assertEqual(main.hardened_flags(findings.values()), ["--user 1000", "--cap-drop ALL", "--security-opt no-new-privileges", "--read-only --tmpfs /tmp", "--pids-limit 100", "--memory 256m", "-p 127.0.0.1:8080:80"])

    def test_hardened_container_passes_everything(self):
        findings = main.inspect_findings(load(INSPECT_HARDENED)[0])
        self.assertEqual([f.status for f in findings], ["pass"] * 13)
        self.assertEqual(main.score(findings), 100)
        checks = by_check(findings)
        self.assertEqual(checks["user"].detail, "runs as 1000:1000")
        self.assertEqual(checks["memory-limit"].detail, "memory limit 256 MiB")
        self.assertEqual(checks["sensitive-mounts"].detail, "no sensitive host paths mounted")

    def test_privileged_disables_seccomp_capabilities_and_lsm(self):
        container = load(INSPECT_HARDENED)[0]
        container["HostConfig"]["Privileged"] = True
        checks = by_check(main.inspect_findings(container))
        self.assertEqual([checks[c].status for c in ("privileged", "capabilities", "seccomp", "lsm")], ["fail"] * 4)
        self.assertIn("all 41 capabilities", checks["privileged"].detail)
        container["HostConfig"]["Privileged"] = False
        container["HostConfig"]["CapAdd"] = ["SYS_ADMIN"]
        checks = by_check(main.inspect_findings(container))
        self.assertEqual(checks["capabilities"].detail, "adds root-equivalent capabilities: SYS_ADMIN")
        container["HostConfig"]["CapAdd"] = ["NET_BIND_SERVICE"]
        self.assertEqual(by_check(main.inspect_findings(container))["capabilities"].detail, "drops ALL, adds NET_BIND_SERVICE")

    def test_seccomp_unconfined_inline_profile_and_missing_apparmor(self):
        container = load(INSPECT_HARDENED)[0]
        container["HostConfig"]["SecurityOpt"] = ["no-new-privileges:true", "seccomp=unconfined"]
        checks = by_check(main.inspect_findings(container))
        self.assertEqual(checks["seccomp"].status, "fail")
        self.assertEqual(checks["no-new-privileges"].status, "pass")
        container["HostConfig"]["SecurityOpt"] = ["seccomp=" + json.dumps(load(TINY_PROFILE)), "apparmor=unconfined"]
        checks = by_check(main.inspect_findings(container))
        self.assertEqual(checks["seccomp"].detail, "custom profile: default SCMP_ACT_ERRNO, 32 syscalls in 2 rules")
        self.assertEqual(checks["lsm"].status, "fail")
        container["HostConfig"]["SecurityOpt"] = []
        container["AppArmorProfile"] = ""
        checks = by_check(main.inspect_findings(container))
        self.assertEqual(checks["lsm"].status, "skip")
        self.assertIsNone(main.score([main.Finding("x", "low", "skip", "", weight=5)]))

    def test_sensitive_mount_detection_ignores_volumes_and_ordinary_paths(self):
        for source in ("/var/run/docker.sock", "/", "/etc", "/etc/passwd", "/proc", "/sys/fs/cgroup", "/var/lib/kubelet/pods", "/home/alice/.ssh", "/run/containerd/containerd.sock"):
            self.assertTrue(main.is_sensitive(source), source)
        for source in ("/srv/data", "/opt/app/config", "/mnt/volumes/pg", "/var/lib/postgresql"):
            self.assertFalse(main.is_sensitive(source), source)
        container = load(INSPECT_DEFAULT)[0]
        container["Mounts"] = [{"Type": "volume", "Source": "/var/lib/docker/volumes/x/_data", "Destination": "/data", "RW": True}, {"Type": "bind", "Source": "/etc/localtime", "Destination": "/etc/localtime", "RW": False}]
        self.assertEqual(by_check(main.inspect_findings(container))["sensitive-mounts"].detail, "mounts /etc/localtime (ro)")
        container["HostConfig"]["PidMode"] = "host"
        container["HostConfig"]["NetworkMode"] = "host"
        self.assertEqual(by_check(main.inspect_findings(container))["host-namespaces"].detail, "shares host namespaces: PidMode=host, NetworkMode=host")

    def test_inspect_cli_reads_files_stdin_and_a_live_container_through_the_runner(self):
        calls = []

        def runner(argv):
            calls.append(list(argv))
            with open(INSPECT_HARDENED, encoding="utf-8") as handle:
                return 0, handle.read(), ""

        code, stdout, _ = run_main(["inspect", "web-hardened", "--json"], runner)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [["docker", "inspect", "web-hardened"]])
        report = json.loads(stdout)[0]
        self.assertEqual((report["name"], report["score"], report["flags"]), ("web-hardened", 100, []))
        code, stdout, _ = run_main(["inspect", INSPECT_DEFAULT])
        self.assertEqual(code, 0)
        self.assertIn("container web (nginx:1.27): score 40/100, 5 of 13 checks pass", stdout)
        self.assertIn("fix: --user 1000  (run as a fixed non-root uid", stdout)
        self.assertIn("hardened run: docker run --user 1000 --cap-drop ALL", stdout)
        with open(INSPECT_DEFAULT, encoding="utf-8") as handle:
            code, stdout, _ = run_main(["inspect", "-", "--json"], stdin=handle.read())
        self.assertEqual(json.loads(stdout)[0]["score"], 40)

    def test_inspect_errors_and_fail_under(self):
        def failing(argv):
            return 1, "", "Error: No such object: ghost"

        code, _, stderr = run_main(["inspect", "ghost"], failing)
        self.assertEqual(code, 2)
        self.assertIn("No such object: ghost", stderr)
        code, _, stderr = run_main(["inspect", POD_RESTRICTED])
        self.assertEqual(code, 2)
        self.assertIn("not docker inspect output", stderr)
        self.assertEqual(run_main(["inspect", INSPECT_DEFAULT, "--fail-under", "80"])[0], 1)
        self.assertEqual(run_main(["inspect", INSPECT_HARDENED, "--fail-under", "80"])[0], 0)


class KubernetesTests(unittest.TestCase):
    def test_violating_pod_is_rejected_with_admission_style_messages(self):
        kind, name, namespace, spec = main.pod_templates(load(POD_VIOLATING))[0]
        self.assertEqual((kind, name, namespace), ("Pod", "cc-smoke-violating", ""))
        findings = main.k8s_findings(spec)
        violations = {f.check: f.detail for f in main.rejected(findings)}
        self.assertEqual(sorted(violations), ["allowPrivilegeEscalation", "capabilities", "host-namespaces", "hostPath-volumes", "hostPort", "privileged", "runAsNonRoot", "seccompProfile"])
        self.assertEqual(violations["privileged"], 'container "debug" must not set securityContext.privileged=true')
        self.assertEqual(violations["host-namespaces"], "hostNetwork=true; hostPID=true")
        self.assertIn('volume "docker-sock" uses hostPath /var/run/docker.sock', violations["hostPath-volumes"])
        self.assertIn('pod or container "sidecar" must set securityContext.runAsNonRoot=true', violations["runAsNonRoot"])
        self.assertIn("REJECTED by pod-security.kubernetes.io/enforce=restricted (8 violations)", main.k8s_title(kind, name, namespace, findings))
        self.assertEqual(main.score(findings), 17)

    def test_restricted_pod_is_admitted(self):
        kind, name, namespace, spec = main.pod_templates(load(POD_RESTRICTED))[0]
        findings = main.k8s_findings(spec)
        self.assertEqual(main.rejected(findings), [])
        self.assertEqual([f.status for f in findings], ["pass"] * 15)
        self.assertEqual(main.k8s_title(kind, name, namespace, findings), "Pod cc-smoke-restricted: admitted under pod-security.kubernetes.io/enforce=restricted")

    def test_deployment_template_inherits_pod_context_and_containers_override_it(self):
        kind, name, namespace, spec = main.pod_templates(load(DEPLOYMENT))[0]
        self.assertEqual((kind, name, namespace), ("Deployment", "api", "payments"))
        checks = by_check(main.k8s_findings(spec))
        self.assertEqual(checks["runAsNonRoot"].detail, 'pod or container "log-shipper" must set securityContext.runAsNonRoot=true')
        self.assertEqual(checks["runAsUser"].detail, 'container "log-shipper" must not set runAsUser=0')
        self.assertIn("(is Unconfined)", checks["seccompProfile"].detail)
        self.assertNotIn('"api"', checks["seccompProfile"].detail)
        self.assertIn('container "log-shipper" must not include "SYS_PTRACE"', checks["capabilities"].detail)
        self.assertIn('initContainer "migrate" must set securityContext.capabilities.drop=["ALL"]', checks["capabilities"].detail)
        self.assertNotIn('"api" must set securityContext.capabilities', checks["capabilities"].detail)
        self.assertIn('initContainer "migrate" must set securityContext.allowPrivilegeEscalation=false', checks["allowPrivilegeEscalation"].detail)
        self.assertEqual(checks["hostPath-volumes"].detail, 'volume "varlog" uses hostPath /var/log')
        self.assertEqual(checks["memory-limit"].detail, 'container "log-shipper": set resources.limits.memory')

    def test_baseline_details_volume_types_hostport_procmount_and_sysctls(self):
        spec = {
            "securityContext": {"runAsNonRoot": True, "runAsUser": 0, "seccompProfile": {"type": "Localhost", "localhostProfile": "api.json"}, "sysctls": [{"name": "net.ipv4.ip_unprivileged_port_start", "value": "0"}, {"name": "kernel.msgmax", "value": "65536"}]},
            "containers": [{"name": "app", "ports": [{"containerPort": 80, "hostPort": 8080}], "securityContext": {"procMount": "Unmasked", "allowPrivilegeEscalation": False, "capabilities": {"drop": ["all"], "add": ["net_bind_service"]}}}],
            "volumes": [{"name": "data", "nfs": {"server": "fs", "path": "/x"}}, {"name": "cfg", "configMap": {"name": "cfg"}}],
        }
        checks = by_check(main.k8s_findings(spec))
        self.assertEqual(checks["volume-types"].detail, 'volume "data" uses restricted volume type "nfs"')
        self.assertEqual(checks["hostPort"].detail, 'container "app" uses hostPort 8080')
        self.assertEqual(checks["procMount"].detail, 'container "app" must not set securityContext.procMount=Unmasked')
        self.assertEqual(checks["sysctls"].detail, "forbidden sysctl kernel.msgmax")
        self.assertEqual(checks["runAsUser"].detail, "pod must not set runAsUser=0")
        self.assertEqual(checks["capabilities"].status, "pass")
        self.assertEqual(checks["seccompProfile"].status, "pass")

    def test_pod_templates_handles_lists_cronjobs_and_unknown_kinds(self):
        cronjob = {"kind": "CronJob", "metadata": {"name": "nightly"}, "spec": {"jobTemplate": {"spec": {"template": {"spec": {"containers": [{"name": "job"}]}}}}}}
        listed = {"kind": "List", "items": [load(POD_RESTRICTED), cronjob]}
        templates = main.pod_templates(listed)
        self.assertEqual([(k, n) for k, n, _, _ in templates], [("Pod", "cc-smoke-restricted"), ("CronJob", "nightly")])
        self.assertEqual(templates[1][3]["containers"][0]["name"], "job")
        with self.assertRaises(main.HardenError):
            main.pod_templates({"kind": "Service", "metadata": {"name": "svc"}})
        with self.assertRaises(main.HardenError):
            main.pod_templates("not an object")

    def test_fix_patch_lands_on_the_pod_spec_for_every_kind(self):
        spec = {"containers": [{"name": "a"}, {"name": "b"}], "initContainers": [{"name": "init"}]}
        pod = main.k8s_patch("Pod", spec)
        self.assertEqual(pod["spec"]["securityContext"]["seccompProfile"], {"type": "RuntimeDefault"})
        self.assertEqual([c["name"] for c in pod["spec"]["containers"]], ["a", "b"])
        self.assertEqual(pod["spec"]["containers"][0]["securityContext"]["capabilities"], {"drop": ["ALL"]})
        self.assertFalse(pod["spec"]["automountServiceAccountToken"])
        deploy = main.k8s_patch("Deployment", spec)
        self.assertIn("securityContext", deploy["spec"]["template"]["spec"])
        self.assertEqual(deploy["spec"]["template"]["spec"]["initContainers"][0]["name"], "init")
        cron = main.k8s_patch("CronJob", spec)
        self.assertIn("securityContext", cron["spec"]["jobTemplate"]["spec"]["template"]["spec"])

    def test_k8s_cli_text_json_fix_stdin_and_fail_under(self):
        code, stdout, _ = run_main(["k8s", POD_VIOLATING])
        self.assertEqual(code, 0)
        self.assertIn("Pod cc-smoke-violating: REJECTED", stdout)
        self.assertIn("FAIL baseline   privileged", stdout)
        code, stdout, _ = run_main(["k8s", DEPLOYMENT, "--json"])
        report = json.loads(stdout)[0]
        self.assertEqual((report["kind"], report["namespace"], report["rejected"], report["score"]), ("Deployment", "payments", True, 39))
        self.assertEqual(len(report["violations"]), 6)
        code, stdout, _ = run_main(["k8s", DEPLOYMENT, "--fix"])
        self.assertEqual(json.loads(stdout)["spec"]["template"]["spec"]["containers"][1]["name"], "log-shipper")
        with open(POD_RESTRICTED, encoding="utf-8") as handle:
            code, stdout, _ = run_main(["k8s", "-"], stdin=handle.read())
        self.assertIn("admitted under", stdout)
        self.assertEqual(run_main(["k8s", POD_VIOLATING, "--fail-under", "50"])[0], 1)
        self.assertEqual(run_main(["k8s", POD_RESTRICTED, "--fail-under", "100"])[0], 0)

    def test_yaml_input_gets_a_conversion_hint_and_no_command_prints_help(self):
        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = os.path.join(tmp, "pod.yaml")
            with open(yaml_path, "w", encoding="utf-8") as handle:
                handle.write("apiVersion: v1\nkind: Pod\n")
            code, _, stderr = run_main(["k8s", yaml_path])
            self.assertEqual(code, 2)
            self.assertIn("kubectl create --dry-run=client", stderr)
        code, _, stderr = run_main([])
        self.assertEqual(code, 2)
        self.assertIn("usage: hardencheck", stderr)


if __name__ == "__main__":
    unittest.main()
