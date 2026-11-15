"""Milestone checks for capstones/aiops-copilot.

One function per milestone in ../lesson.json: milestone `some-id` is checked by
`check_some_id(project)`, which gets the project folder and returns
(passed, detail). The grader drives the learner's `bin/copilot` only through
its documented interface (subcommands with --json, environment variables) and
never imports learner code.

Cluster checks run only against a `kind-*` context (or exactly
$CC_GRADER_CONTEXT), each in its own `cc-grade-copilot-*` namespace that it
creates and deletes. The model is always the grader's scripted fake on
127.0.0.1; no check reaches a real API. Python 3.9+, standard library only.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cluster  # noqa: E402
import verdicts  # noqa: E402
from fake_model import FAKE_API_KEY, FakeModel, text_block, thinking_block, tool_use, turn  # noqa: E402

Result = Tuple[bool, str]

ENTRY = os.path.join("bin", "copilot")
SUITE = os.path.join(HERE, "fixtures", "eval-suite.json")
COMMAND_TIMEOUT = 150.0
SCRUBBED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "COPILOT_MODEL_URL", "COPILOT_MODEL", "COPILOT_ALLOWED_NAMESPACES", "COPILOT_CONTEXT", "COPILOT_HOME", "COPILOT_EFFORT")
EVAL_EXPECTED = {
    "oom-basic": True,
    "image-pull-backoff": True,
    "crashloop-bad-config": True,
    "oom-behind-crashloop": True,
    "service-without-endpoints": True,
    "pod-not-ready": True,
    "healthy-workload": True,
    "mislabelled-case": False,
    "stale-citation": False,
}
FINAL_ANSWER = "cc-api crashes at start because LOG_LEVEL=verbose is not a valid value."


def entry_problem(project: str) -> str:
    path = os.path.join(project, ENTRY)
    if not os.path.isfile(path):
        return f"{ENTRY} not found in {os.path.abspath(project)}; copy starter/ into your repository and keep the entry point at {ENTRY}"
    if not os.access(path, os.X_OK):
        return f"{ENTRY} is not executable; run chmod +x {ENTRY} (and commit the mode with git update-index --chmod=+x {ENTRY})"
    return ""


def base_env(allowed: str = "", context: str = "", home: str = "", model_url: str = "") -> Dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in SCRUBBED_ENV}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if allowed:
        env["COPILOT_ALLOWED_NAMESPACES"] = allowed
    if context:
        env["COPILOT_CONTEXT"] = context
    if home:
        env["COPILOT_HOME"] = home
    if model_url:
        env["COPILOT_MODEL_URL"] = model_url
        env["ANTHROPIC_API_KEY"] = FAKE_API_KEY
    return env


def run_copilot(project: str, args: Sequence[str], env: Dict[str, str], cwd: str, timeout: float = COMMAND_TIMEOUT) -> Tuple[int, str, str]:
    argv = [os.path.join(os.path.abspath(project), ENTRY), *args]
    try:
        done = subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", f"`copilot {' '.join(args[:3])}` did not finish within {timeout:.0f}s"
    except OSError as exc:
        return 126, "", f"could not run {ENTRY}: {exc} (does it start with a #! line?)"
    return done.returncode, done.stdout, done.stderr


def stub_problem(code: int, err: str) -> str:
    if code != 0 and "not implemented" in err.lower():
        line = next((line for line in err.splitlines() if "not implemented" in line.lower()), "not implemented")
        return f"still a stub: {line.strip()[:160]}"
    return ""


def describe_failure(args: Sequence[str], code: int, out: str, err: str) -> str:
    tail = (err.strip() or out.strip()).splitlines()[-3:]
    return f"`copilot {' '.join(args)}` exited {code}: {' | '.join(tail)[:300] or '(no output)'}"


def parse_json(text: str) -> Tuple[Any, str]:
    try:
        return json.loads(text), ""
    except json.JSONDecodeError as exc:
        return None, f"stdout is not one JSON document ({exc.msg} at line {exc.lineno}); print logs to stderr, JSON to stdout"


class Workspace:
    def __init__(self, project: str, tag: str, apps: Sequence[str]) -> None:
        self.project = os.path.abspath(project)
        self.tag = tag
        self.apps = list(apps)
        self.home = tempfile.mkdtemp(prefix="cc-grade-copilot-")
        self.cwd = os.path.join(self.home, "cwd")
        os.makedirs(self.cwd)
        self.context = ""
        self.kube: Optional[cluster.Kube] = None
        self.incidents: Optional[cluster.IncidentEnv] = None

    @property
    def namespace(self) -> str:
        return self.incidents.namespace if self.incidents else ""

    @property
    def pods(self) -> Dict[str, str]:
        return self.incidents.pods if self.incidents else {}

    def env(self, model_url: str = "") -> Dict[str, str]:
        return base_env(self.namespace, self.context, os.path.join(self.home, "state"), model_url)

    def open(self, probe: Sequence[str]) -> str:
        problem = entry_problem(self.project)
        if problem:
            return problem
        context, problem = cluster.guard()
        if problem or context is None:
            return problem
        self.context = context
        probe_ns = f"{cluster.PREFIX}probe-{os.getpid()}"
        with FakeModel([turn([text_block("probe")])]) as model:
            args = [part.replace("{ns}", probe_ns) for part in probe]
            env = base_env(probe_ns, context, os.path.join(self.home, "probe"), model.url)
            code, _, err = run_copilot(self.project, args, env, self.cwd, timeout=60)
        problem = stub_problem(code, err)
        if problem:
            return problem
        self.kube = cluster.Kube(context)
        self.incidents = cluster.IncidentEnv(self.kube, self.tag, self.apps)
        return self.incidents.create()

    def run(self, *args: str, model_url: str = "", timeout: float = COMMAND_TIMEOUT) -> Tuple[int, str, str]:
        return run_copilot(self.project, args, self.env(model_url), self.cwd, timeout)

    def run_json(self, *args: str, model_url: str = "", ok_codes: Sequence[int] = (0,)) -> Tuple[Any, str]:
        code, out, err = self.run(*args, model_url=model_url)
        if code not in ok_codes:
            return None, stub_problem(code, err) or describe_failure(args, code, out, err)
        document, problem = parse_json(out)
        if problem:
            return None, f"`copilot {' '.join(args[:2])}`: {problem}"
        return document, ""

    def close(self) -> None:
        if self.incidents is not None:
            self.incidents.delete()
        shutil.rmtree(self.home, ignore_errors=True)


def verdict(problems: List[str], success: str) -> Result:
    if problems:
        extra = f" (+{len(problems) - 3} more)" if len(problems) > 3 else ""
        return False, "; ".join(problems[:3]) + extra
    return True, success


def collect_ids(ws: Workspace) -> Tuple[set, str]:
    document, problem = ws.run_json("collect", "--namespace", ws.namespace, "--json")
    if problem:
        return set(), problem
    items, _ = verdicts.evidence_items(document)
    return {item.get("id") for item in items}, ""


def check_evidence(project: str) -> Result:
    ws = Workspace(project, "evidence", [*verdicts.INCIDENT_APPS, *verdicts.DECOY_APPS])
    try:
        problem = ws.open(["collect", "--namespace", "{ns}", "--json"])
        if problem:
            return False, problem
        first, problem = ws.run_json("collect", "--namespace", ws.namespace, "--json")
        if problem:
            return False, problem
        problems = verdicts.evidence_problems(first, ws.pods)
        second, problem = ws.run_json("collect", "--namespace", ws.namespace, "--json")
        problems += [problem] if problem else verdicts.stability_problems(first, second, ws.pods)
        return verdict(problems, f"{len(verdicts.INCIDENT_APPS)} incidents named with the right pod and signal, the decoy is quiet, ids are stable across runs")
    finally:
        ws.close()


def check_triage(project: str) -> Result:
    ws = Workspace(project, "triage", [*verdicts.INCIDENT_APPS, *verdicts.DECOY_APPS])
    try:
        problem = ws.open(["triage", "--namespace", "{ns}", "--json"])
        if problem:
            return False, problem
        before, problem = collect_ids(ws)
        if problem:
            return False, f"triage cites evidence ids, so `copilot collect` must work first: {problem}"
        document, problem = ws.run_json("triage", "--namespace", ws.namespace, "--json")
        if problem:
            return False, problem
        after, _ = collect_ids(ws)
        problems = verdicts.triage_problems(document, before | after)
        return verdict(problems, "every workload got the right category and every citation is a real evidence id")
    finally:
        ws.close()


def agent_script(namespace: str, api_pod: str) -> List[Dict[str, Any]]:
    return [
        turn([thinking_block("sig-ccgrade-1"), text_block("Starting wide."), tool_use("toolu_cc_01", "list_pods", {"namespace": namespace}), tool_use("toolu_cc_02", "list_events", {"namespace": namespace})], input_tokens=120, output_tokens=40),
        turn([thinking_block("sig-ccgrade-2"), tool_use("toolu_cc_03", "pod_logs", {"namespace": namespace, "name": api_pod})], input_tokens=160, output_tokens=30),
        turn([tool_use("toolu_cc_04", "describe", {"namespace": namespace, "kind": "pod", "name": api_pod})], input_tokens=200, output_tokens=30),
        turn([thinking_block("sig-ccgrade-4"), text_block(FINAL_ANSWER)], input_tokens=240, output_tokens=60),
    ]


def rejected(model: FakeModel) -> List[str]:
    return [f"the fake model rejected request {index} with HTTP {record.get('status')}: {record.get('error', '')[:200]}" for index, record in enumerate(model.requests, start=1) if record.get("status") != 200]


def called(document: Any) -> List[Tuple[Any, Any, Any]]:
    calls = document.get("tool_calls") if isinstance(document, dict) else None
    if not isinstance(calls, list):
        return []
    return [(call.get("id"), call.get("name"), call.get("input")) for call in calls if isinstance(call, dict)]


def scripted_calls(script: Sequence[Dict[str, Any]]) -> List[Tuple[Any, Any, Any]]:
    return [(block["id"], block["name"], block["input"]) for item in script for block in item["content"] if block.get("type") == "tool_use"]


def require_text(results: Dict[str, Dict[str, Any]], block_id: str, needles: Sequence[str], what: str) -> List[str]:
    block = results.get(block_id)
    if block is None:
        return [f"no tool_result for {block_id} in the next request ({what}); answer every tool_use id in one user message"]
    text = verdicts.result_text(block)
    if block.get("is_error"):
        return [f"{what} came back as an error: {text[:160]}"]
    missing = [needle for needle in needles if needle not in text]
    return [f"{what}: the real kubectl output did not reach the model (missing {', '.join(missing)})"] if missing else []


def agent_limit_run(ws: Workspace, script: List[Dict[str, Any]], flags: Sequence[str], stop: str) -> List[str]:
    with FakeModel(script, repeat_last=True) as model:
        document, problem = ws.run_json("ask", "--namespace", ws.namespace, *flags, "keep looking", "--json", model_url=model.url)
        sent = len(model.accepted())
        refused = rejected(model)
    if problem:
        return [problem]
    problems = refused[:1]
    if sent != 3:
        problems.append(f"with {' '.join(flags)} against a model that never stops, expected exactly 3 model calls, saw {sent}")
    if not isinstance(document, dict) or document.get("stop_reason") != stop:
        problems.append(f"with {' '.join(flags)}, expected stop_reason {stop!r}, got {document.get('stop_reason') if isinstance(document, dict) else None!r}")
    return problems


def check_agent_loop(project: str) -> Result:
    ws = Workspace(project, "agent", ["cc-api", "cc-ok"])
    try:
        problem = ws.open(["ask", "--namespace", "{ns}", "probe", "--json"])
        if problem:
            return False, problem
        api_pod, ok_pod = ws.pods["cc-api"], ws.pods["cc-ok"]
        script = agent_script(ws.namespace, api_pod)
        with FakeModel(script) as model:
            document, problem = ws.run_json("ask", "--namespace", ws.namespace, "why is cc-api down?", "--json", model_url=model.url)
            records, sent, refused = model.accepted(), model.sent, rejected(model)
        if refused:
            return False, refused[0]
        if problem:
            return False, problem
        problems: List[str] = []
        if len(records) != 4:
            problems.append(f"expected 4 model calls for the 4 scripted turns, saw {len(records)}")
        if records:
            problems += verdicts.request_shape_problems(records[0], FAKE_API_KEY)
        problems += verdicts.echo_problems(records, sent)
        if len(records) >= 2:
            results = verdicts.tool_results(records[1]["body"])
            problems += require_text(results, "toolu_cc_01", [api_pod, ok_pod], "list_pods")
            problems += require_text(results, "toolu_cc_02", [api_pod], "list_events")
        if len(records) >= 3:
            problems += require_text(verdicts.tool_results(records[2]["body"]), "toolu_cc_03", [verdicts.CRASH_MARKER], "pod_logs")
        if len(records) >= 4:
            problems += require_text(verdicts.tool_results(records[3]["body"]), "toolu_cc_04", [api_pod], "describe pod")
        if called(document) != scripted_calls(script):
            problems.append(f"`tool_calls` in the --json output should list the 4 executed calls with the model's ids and inputs, got {[c[0] for c in called(document)]}")
        if not isinstance(document, dict) or document.get("stop_reason") != "end_turn" or FINAL_ANSWER not in str(document.get("answer")):
            problems.append("on end_turn the --json output needs stop_reason \"end_turn\" and the model's final text in `answer`")
        if not problems:
            loop = [turn([tool_use("toolu_loop", "list_pods", {"namespace": ws.namespace})])]
            problems += agent_limit_run(ws, loop, ["--max-turns", "3"], "max_turns")
            hungry = [turn([tool_use("toolu_hungry", "list_pods", {"namespace": ws.namespace})], input_tokens=600, output_tokens=100)]
            problems += agent_limit_run(ws, hungry, ["--max-turns", "10", "--token-budget", "1500"], "token_budget")
        return verdict(problems, "scripted tool calls ran against the cluster, real output reached the model with matching ids, max-turns and token budget stop a runaway loop")
    finally:
        ws.close()


def guardrail_script(namespace: str, api_pod: str) -> List[Dict[str, Any]]:
    return [
        turn([thinking_block("sig-ccgrade-g1"), tool_use("toolu_g_01", "pod_logs", {"namespace": namespace, "name": api_pod}), tool_use("toolu_g_02", "describe", {"namespace": namespace, "kind": "pod", "name": api_pod})]),
        turn([tool_use("toolu_g_03", "describe", {"namespace": namespace, "kind": "secret", "name": "cc-db-credentials"}), tool_use("toolu_g_04", "list_pods", {"namespace": "kube-system"})]),
        turn([
            tool_use("toolu_g_05", "propose_action", {"namespace": namespace, "action": "restart", "target": "cc-api", "reason": "the pod keeps crashing"}),
            tool_use("toolu_g_06", "propose_action", {"namespace": namespace, "action": "scale", "target": "cc-scratch", "replicas": 2, "reason": "more capacity"}),
        ]),
        turn([tool_use("toolu_g_07", "list_events", {"namespace": namespace})]),
        turn([text_block("Proposed a restart of cc-api and a scale-up of cc-scratch; nothing was changed.")]),
    ]


def unchanged(before: Dict[str, Dict[str, Any]], after: Dict[str, Dict[str, Any]], names: Sequence[str]) -> List[str]:
    return [f"deployment/{name} changed in the cluster ({before.get(name)} -> {after.get(name)}); proposals must not touch it" for name in names if before.get(name) != after.get(name)]


def proposal_problems(document: Any) -> Tuple[List[str], Optional[str]]:
    proposals = document.get("proposals") if isinstance(document, dict) else None
    if not isinstance(proposals, list) or len(proposals) != 2:
        return [f"`proposals` in the ask --json output should hold the 2 proposals the model asked for, got {proposals if not isinstance(proposals, list) else len(proposals)}"], None
    problems = []
    scale_id = None
    for proposal in proposals:
        if not isinstance(proposal, dict) or not isinstance(proposal.get("id"), str):
            problems.append("each proposal needs a string `id`")
            continue
        if proposal.get("status") != "proposed":
            problems.append(f"proposal {proposal['id']}: status {proposal.get('status')!r}, expected \"proposed\"")
        diff = proposal.get("diff")
        if not isinstance(diff, list) or not diff:
            problems.append(f"proposal {proposal['id']}: `diff` must list the fields a server-side dry run changes")
            continue
        if proposal.get("action") == "scale":
            scale_id = proposal["id"]
            if not any(isinstance(d, dict) and str(d.get("path", "")).endswith("replicas") and d.get("before") == 1 and d.get("after") == 2 for d in diff):
                problems.append(f"the scale proposal's diff should show replicas 1 -> 2, got {json.dumps(diff)[:160]}")
    if scale_id is None:
        problems.append("no proposal with action \"scale\" for cc-scratch")
    return problems, scale_id


def wait_for_replicas(ws: Workspace, name: str, replicas: int, deadline: float = 30.0) -> bool:
    until = time.monotonic() + deadline
    while time.monotonic() < until:
        if (ws.incidents.snapshot().get(name) or {}).get("replicas") == replicas:
            return True
        time.sleep(1)
    return False


def check_guardrails(project: str) -> Result:
    ws = Workspace(project, "guard", ["cc-api", "cc-scratch", "cc-db-credentials"])
    try:
        problem = ws.open(["ask", "--namespace", "{ns}", "probe", "--json"])
        if problem:
            return False, problem
        assert ws.incidents is not None
        api_pod = ws.pods["cc-api"]
        before = ws.incidents.snapshot()
        script = guardrail_script(ws.namespace, api_pod)
        with FakeModel(script) as model:
            document, problem = ws.run_json("ask", "--namespace", ws.namespace, "cc-api is down, fix it", "--json", model_url=model.url)
            all_records, records, refused = list(model.requests), model.accepted(), rejected(model)
        problems: List[str] = verdicts.leaked_secrets(all_records)
        if refused:
            return False, "; ".join(problems[:1] + refused[:1])
        if problem:
            return False, problem
        if len(records) != 5:
            problems.append(f"expected 5 model calls for the 5 scripted turns, saw {len(records)}")
        if len(records) >= 2:
            results = verdicts.tool_results(records[1]["body"])
            logs = results.get("toolu_g_01")
            if logs is None or logs.get("is_error"):
                problems.append("pod_logs must succeed so the grader can see how log text is wrapped")
            else:
                text = verdicts.result_text(logs)
                problems += [f"pod_logs result: {p}" for p in verdicts.wrapper_problems(text)]
                if verdicts.INJECTION_MARKER not in text.lower():
                    problems.append("keep log lines as data inside the wrapper; the planted line was dropped")
        if len(records) >= 3:
            results = verdicts.tool_results(records[2]["body"])
            if not (results.get("toolu_g_04") or {}).get("is_error"):
                problems.append("list_pods on kube-system must come back with is_error: true (namespace outside COPILOT_ALLOWED_NAMESPACES)")
        if called(document) != scripted_calls(script):
            problems.append("the executed tool calls must be exactly the model's calls; a log line must not add, drop or change one")
        found, scale_id = proposal_problems(document)
        problems += found
        problems += unchanged(before, ws.incidents.snapshot(), ["cc-api", "cc-scratch"])
        code, _, _ = ws.run("apply", "--approve", "p-not-a-real-proposal", "--json")
        if code == 0:
            problems.append("`copilot apply --approve` with an unknown id must exit non-zero")
        code, out, err = ws.run("collect", "--namespace", "kube-system", "--json")
        if code == 0:
            problems.append("`copilot collect --namespace kube-system` must be refused (exit non-zero): it is not in COPILOT_ALLOWED_NAMESPACES")
        if not problems and scale_id:
            code, out, err = ws.run("apply", "--approve", scale_id, "--json")
            if code != 0:
                problems.append(describe_failure(["apply", "--approve", scale_id], code, out, err))
            elif not wait_for_replicas(ws, "cc-scratch", 2):
                problems.append("after `copilot apply --approve <scale id>` deployment/cc-scratch should have spec.replicas 2")
            problems += unchanged(before, ws.incidents.snapshot(), ["cc-api"])
        return verdict(problems, "writes stayed proposals until approved, kube-system was refused, no planted secret reached the model, the injected log line stayed data")
    finally:
        ws.close()


def check_report(project: str) -> Result:
    ws = Workspace(project, "report", [*verdicts.INCIDENT_APPS, *verdicts.DECOY_APPS])
    try:
        problem = ws.open(["report", "--namespace", "{ns}", "--format", "json"])
        if problem:
            return False, problem
        assert ws.incidents is not None
        before = ws.incidents.snapshot()
        report, problem = ws.run_json("report", "--namespace", ws.namespace, "--format", "json")
        if problem:
            return False, problem
        problems = verdicts.report_problems(report)
        code, out, err = ws.run("report", "--namespace", ws.namespace, "--format", "md")
        if code != 0:
            problems.append(describe_failure(["report", "--format", "md"], code, out, err))
        else:
            cited = [c for h in (report.get("hypotheses") or []) if isinstance(h, dict) for c in (h.get("citations") or [])]
            problems += verdicts.markdown_problems(out, [str(c) for c in cited])
        problems += unchanged(before, ws.incidents.snapshot(), list(before))
        return verdict(problems, "summary, impact, ordered timeline, hypotheses with confidence and resolvable citations, actions left as proposals")
    finally:
        ws.close()


def check_evals(project: str) -> Result:
    problem = entry_problem(project)
    if problem:
        return False, problem
    with open(SUITE, encoding="utf-8") as handle:
        trials = int(json.load(handle).get("trials", 1))
    work = tempfile.mkdtemp(prefix="cc-grade-copilot-eval-")
    try:
        env = base_env(home=os.path.join(work, "state"))
        args = ["eval", SUITE, "--json"]
        code, out, err = run_copilot(project, args, env, work, timeout=90)
        if code not in (0, 1):
            return False, describe_failure(args, code, out, err)
        problem = stub_problem(code, err)
        if problem:
            return False, problem
        document, problem = parse_json(out)
        if problem:
            return False, f"`copilot eval`: {problem}"
        problems = verdicts.eval_problems(document, EVAL_EXPECTED, trials)
        return verdict(problems, f"{len(EVAL_EXPECTED)} recorded cases gave the expected verdicts and the pass rate and Wilson interval are right")
    finally:
        shutil.rmtree(work, ignore_errors=True)
