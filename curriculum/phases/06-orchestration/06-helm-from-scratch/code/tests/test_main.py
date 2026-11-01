import base64
import gzip
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
WEBAPP = os.path.join(FIXTURES, "webapp")
OVERRIDE = os.path.join(FIXTURES, "override.yaml")


def ticking_clock(start=datetime(2026, 11, 1, 9, 0, 0, tzinfo=timezone.utc)):
    state = {"now": start}

    def clock():
        state["now"] = state["now"] + timedelta(seconds=1)
        return state["now"]

    return clock


def chart_from(templates, values=None, name="demo", version="0.1.0", app_version="1.0.0"):
    metadata = {"name": name, "version": version, "apiVersion": "v2", "appVersion": app_version}
    return main.Chart("/virtual/" + name, metadata, main.floatify(values or {}), dict(sorted(templates.items())))


def render_one(source, values=None, release="rel", helpers=""):
    templates = {"templates/x.yaml": source}
    if helpers:
        templates["templates/_helpers.tpl"] = helpers
    chart = chart_from(templates, values)
    engine = main.Engine()
    for path, text in chart.templates.items():
        engine.add(f"{chart.name}/{path}", text)
    top = main.Obj(
        "chartutil.Values",
        Values=main.coalesce(chart.values, {}),
        Release=main.Obj("common.ReleaseOptions", Name=release, Namespace="ns", Revision=1, IsInstall=True, IsUpgrade=False, Service="Helm"),
        Chart=main.chart_object(chart),
        Template=main.Obj("chartutil.Template", Name="demo/templates/x.yaml", BasePath="demo/templates"),
    )
    return engine.execute("demo/templates/x.yaml", top)


class YamlTests(unittest.TestCase):
    def test_scalars_follow_yaml_1_1_like_helm_including_the_norway_keys(self):
        doc = main.parse_yaml("a: yes\nb: NO\nc: 0755\nd: 0x1F\ne: ~\nf: 1e3\ng: .5\nh: '0755'\nn: x\ni: 1_000\nj: \"on\"\nk: 12:30\n")
        self.assertEqual(doc, {"a": True, "b": False, "c": 493, "d": 31, "e": None, "f": 1000.0, "g": 0.5, "h": "0755", "false": "x", "i": 1000, "j": "on", "k": "12:30"})

    def test_block_scalars_flow_collections_compact_and_nested_sequences(self):
        source = "script: |\n  echo one\n  # not a comment\n\n  echo two\nfolded: >-\n  a\n  b\nflow: {a: [1, \"x\"], b: {}}\nlist:\n- a: 1\n  b:\n  - 2\n- - x\n  - z\n- |-\n  text\nlast: done\n"
        self.assertEqual(main.parse_yaml(source), {
            "script": "echo one\n# not a comment\n\necho two\n",
            "folded": "a b",
            "flow": {"a": [1, "x"], "b": {}},
            "list": [{"a": 1, "b": [2]}, ["x", "z"], "text"],
            "last": "done",
        })

    def test_errors_name_the_line_and_duplicate_keys_keep_the_last_like_helm(self):
        with self.assertRaises(main.YamlError) as tabs:
            main.parse_yaml("a:\n\tb: 1\n")
        self.assertEqual(tabs.exception.line, 2)
        with self.assertRaises(main.YamlError) as indent:
            main.parse_yaml("a:\n  b: 1\n   c: 2\n")
        self.assertIn("line 3: bad indentation", str(indent.exception))
        self.assertEqual(main.parse_yaml("a: 1\nb: 2\na: 3\n"), {"b": 2, "a": 3})

    def test_split_manifests_drops_empty_and_comment_only_documents(self):
        text = "---\n# Source: a.yaml\nkind: A\n---\n# only a comment\n---   \n\nkind: B\n"
        self.assertEqual(len(main.split_manifests(text)), 3)
        self.assertEqual(main.parse_documents(text), [{"kind": "A"}, {"kind": "B"}])

    def test_to_yaml_quotes_and_indents_like_helm_and_round_trips(self):
        value = {"plain": "a:b", "yes": "yes", "num": "1.0", "colon": "a: b", "lead": " x", "dash": "-x", "empty": "", "multi": "a\nb\n", "list": [{"k": 1, "j": [1, 2]}, [], {}, ["x"]], "n": 3.0}
        self.assertEqual(main.to_yaml(value), "\n".join([
            "colon: 'a: b'", "dash: -x", 'empty: ""', "lead: ' x'", "list:", "- j:", "  - 1", "  - 2", "  k: 1", "- []", "- {}", "- - x",
            "multi: |", "  a", "  b", '"n": 3', 'num: "1.0"', "plain: a:b", '"yes": "yes"', "",
        ]))
        self.assertEqual(main.parse_yaml(main.to_yaml(value)), value)


class GoValueTests(unittest.TestCase):
    def test_numbers_print_like_go_fmt_and_values_files_hold_float64(self):
        self.assertEqual([main.go_float(x) for x in (1e6, 100000.0, 2.5, 1e-05, 0.0001, 1234567.0, -3.0)], ["1e+06", "100000", "2.5", "1e-05", "0.0001", "1.234567e+06", "-3"])
        self.assertEqual(main.go_print({"b": True, "a": [1, None]}), "map[a:[1 <nil>] b:true]")
        self.assertEqual(main.go_print(None), "")
        self.assertEqual(main.floatify({"n": 3, "flag": True, "l": [1]}), {"n": 3.0, "flag": True, "l": [1.0]})
        self.assertEqual(render_one("{{ .Values.big }} {{ .Values.small }}", {"big": 1000000, "small": 3}), "1e+06 3")

    def test_printf_print_and_quote_follow_go(self):
        self.assertEqual(main.go_sprintf("%d|%v|%s|%q|%5s|%%", 3.0, None, "x", "a", "b"), '%!d(float64=3)|<nil>|x|"a"|    b|%')
        self.assertEqual(main.go_sprintf("%s-%s", "a"), "a-%!s(MISSING)")
        self.assertEqual(main.go_sprint(1, 2, "a", "b", 3), "1 2ab3")
        self.assertEqual(render_one('{{ quote .Values.nope }}|{{ quote "a\\"b\\n" }}|{{ 3 | quote }}'), '|"a\\"b\\n"|"3"')


class ValuesTests(unittest.TestCase):
    def test_set_coerces_types_builds_lists_and_honours_escapes(self):
        values = main.parse_set("a=true,b=0,c=007,d=1.5,e=null,f={x,2},g=a\\,b,h.i[2]=z,j=", {})
        self.assertEqual(values, {"a": True, "b": 0, "c": "007", "d": "1.5", "e": None, "f": ["x", 2], "g": "a,b", "h": {"i": [None, None, "z"]}, "j": ""})
        self.assertEqual(main.parse_set("port=8080", {}, as_string=True), {"port": "8080"})

    def test_set_rejects_paths_it_cannot_build(self):
        for bad in ("a", "a[-1]=x", "a[70000]=x", "[0]=x"):
            with self.subTest(bad=bad), self.assertRaises(main.ValuesError):
                main.parse_set(bad, {})

    def test_precedence_defaults_then_files_then_set_then_set_string(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        first, second = os.path.join(tmp, "a.yaml"), os.path.join(tmp, "b.yaml")
        with open(first, "w") as handle:
            handle.write("labels:\n  team: a\nkeep: null\nports: []\nreplicas: 2\n")
        with open(second, "w") as handle:
            handle.write("labels:\n  tier: api\n")
        config = main.user_values([first, second], ["labels.extra=1,replicas=3"], ["replicas=4"])
        defaults = {"labels": {"team": "x", "tier": "web", "owner": "o"}, "keep": 1, "ports": [{"name": "http"}], "absent": None}
        self.assertEqual(main.coalesce(defaults, config), {"labels": {"team": "a", "tier": "api", "owner": "o", "extra": 1}, "ports": [], "replicas": "4"})

    def test_a_list_index_replaces_the_whole_default_list(self):
        config = main.parse_set("ports[1].containerPort=9100", {})
        computed = main.coalesce({"ports": [{"name": "http", "containerPort": 8080}]}, config)
        self.assertEqual(computed["ports"], [None, {"containerPort": 9100}])


class TemplateTests(unittest.TestCase):
    def test_fields_missing_keys_and_nil_pointers(self):
        self.assertEqual(render_one("[{{ .Values.a.b }}][{{ .Values.missing }}][{{ .Release.Name }}/{{ .Chart.Name }}]", {"a": {"b": "x"}}), "[x][][rel/demo]")
        with self.assertRaises(main.TemplateError) as nil:
            render_one("{{ .Values.missing.deeper }}")
        self.assertIn("nil pointer evaluating interface {}.deeper", str(nil.exception))
        self.assertIn('executing "demo/templates/x.yaml" at <.Values.missing.deeper>', str(nil.exception))
        with self.assertRaises(main.TemplateError) as field:
            render_one("{{ .Release.Nope }}")
        self.assertIn("can't evaluate field Nope", str(field.exception))

    def test_trim_markers_and_comments(self):
        self.assertEqual(render_one("a:\n  {{- /* gone */ -}}\n  b\n{{ \"c\" -}}  \n\n d"), "a:b\ncd")

    def test_if_else_if_else_with_and_range_over_lists_and_maps(self):
        source = (
            "{{ if eq .Values.tier \"a\" }}A{{ else if eq .Values.tier \"b\" }}B{{ else }}C{{ end }}|"
            "{{ with .Values.obj }}{{ .k }}{{ else }}none{{ end }}|{{ with .Values.empty }}x{{ else }}empty{{ end }}|"
            "{{ range .Values.list }}<{{ . }}>{{ end }}|{{ range $k, $v := .Values.obj }}{{ $k }}={{ $v }};{{ end }}|"
            "{{ range $i, $v := .Values.list }}{{ $i }}{{ end }}|{{ range .Values.none }}x{{ else }}nothing{{ end }}"
        )
        values = {"tier": "b", "obj": {"k": "v", "a": 1}, "empty": {}, "list": ["x", "y"]}
        self.assertEqual(render_one(source, values), "B|v|empty|<x><y>|a=1;k=v;|01|nothing")

    def test_variables_root_scope_and_assignment(self):
        source = "{{ $x := 1 }}{{ if true }}{{ $x = 2 }}{{ $inner := 9 }}{{ end }}{{ $x }}|{{ range .Values.list }}{{ $.Release.Name }}-{{ . }} {{ end }}"
        self.assertEqual(render_one(source, {"list": ["a", "b"]}), "2|rel-a rel-b ")
        with self.assertRaises(main.TemplateError):
            render_one("{{ if true }}{{ $inner := 9 }}{{ end }}{{ $inner }}")

    def test_define_include_nindent_and_to_yaml(self):
        helpers = '{{- define "demo.labels" -}}\napp: {{ .Chart.Name }}\nrelease: {{ .Release.Name }}\n{{- end -}}'
        source = "metadata:\n  labels:\n    {{- include \"demo.labels\" . | nindent 4 }}\nspec:\n  {{- toYaml .Values.spec | nindent 2 }}\n{{ indent 2 \"a\\nb\" }}"
        self.assertEqual(render_one(source, {"spec": {"replicas": 2, "ports": [80]}}, helpers=helpers), "metadata:\n  labels:\n    app: demo\n    release: rel\nspec:\n  ports:\n  - 80\n  replicas: 2\n  a\n  b")

    def test_string_functions_default_and_logic(self):
        source = (
            '{{ default "d" .Values.none }}|{{ default "d" 0 }}|{{ .Values.s | default "d" }}|{{ "abcdef" | trunc 3 }}|{{ trunc -2 "abcdef" }}|'
            '{{ "web-" | trimSuffix "-" }}|{{ replace "+" "_" "1.0+b" }}|{{ contains "ab" "xaby" }}|{{ .Values.s | upper }}|{{ "ABC" | lower }}|'
            "{{ not .Values.none }}|{{ and 1 0 }}|{{ or 0 \"\" \"z\" }}|{{ len .Values.s }}|{{ sha256sum \"x\" | trunc 8 }}|{{ b64enc \"hi\" }}"
        )
        self.assertEqual(render_one(source, {"s": "val"}), "d|d|val|abc|ef|web|1.0_b|true|VAL|abc|true|0|z|3|2d711642|aGk=")

    def test_eq_between_a_values_number_and_an_int_literal_fails_like_go(self):
        with self.assertRaises(main.TemplateError) as error:
            render_one("{{ eq .Values.n 3 }}", {"n": 3})
        self.assertIn("incompatible types for comparison: float64 and int", str(error.exception))
        self.assertEqual(render_one("{{ eq .Values.n 3.0 }} {{ eq .Values.none \"x\" }}", {"n": 3}), "true false")

    def test_and_short_circuits_so_guards_do_not_hit_nil_pointers(self):
        self.assertEqual(render_one("{{ if and .Values.a .Values.a.b }}yes{{ else }}no{{ end }}"), "no")

    def test_required_fails_with_the_outer_call_site(self):
        helpers = '{{- define "demo.team" -}}{{ required "team is required" .Values.team }}{{- end -}}'
        with self.assertRaises(main.TemplateError) as error:
            render_one('a: 1\nteam: {{ include "demo.team" . }}', helpers=helpers)
        self.assertEqual(str(error.exception), "execution error at (demo/templates/x.yaml:2): team is required")

    def test_parse_time_errors_for_unknown_functions_and_missing_end(self):
        with self.assertRaises(main.TemplateError) as unknown:
            render_one("{{ lookup \"v1\" \"Secret\" \"ns\" \"x\" }}")
        self.assertIn('function "lookup" not defined', str(unknown.exception))
        with self.assertRaises(main.TemplateError) as missing:
            render_one("{{ if true }}open")
        self.assertIn("unexpected EOF", str(missing.exception))
        with self.assertRaises(main.TemplateError) as stray:
            render_one("text {{ end }}")
        self.assertIn("unexpected {{end}}", str(stray.exception))


class ChartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def write_chart(self, chart_yaml, templates=None, values=""):
        root = os.path.join(self.tmp, "c")
        os.makedirs(os.path.join(root, "templates"), exist_ok=True)
        with open(os.path.join(root, "Chart.yaml"), "w") as handle:
            handle.write(chart_yaml)
        with open(os.path.join(root, "values.yaml"), "w") as handle:
            handle.write(values)
        for name, text in (templates or {}).items():
            path = os.path.join(root, "templates", name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                handle.write(text)
        return root

    def test_load_chart_validates_metadata_and_stringifies_versions(self):
        with self.assertRaises(main.ChartError) as missing:
            main.load_chart(self.tmp)
        self.assertIn("Chart.yaml file is missing", str(missing.exception))
        with self.assertRaises(main.ChartError) as no_version:
            main.load_chart(self.write_chart("apiVersion: v2\nname: c\n"))
        self.assertIn("chart.metadata.version is required", str(no_version.exception))
        with self.assertRaises(main.ChartError) as bad_version:
            main.load_chart(self.write_chart("apiVersion: v2\nname: c\nversion: latest\n"))
        self.assertIn('version "latest" is invalid', str(bad_version.exception).replace("'", '"'))
        chart = main.load_chart(self.write_chart("apiVersion: v2\nname: c\nversion: 1.2.3\nappVersion: 1.16\n", {"a.yaml": "x: 1\n", "sub/b.yaml": "y: 2\n"}))
        self.assertEqual(chart.metadata["appVersion"], "1.16")
        self.assertEqual(list(chart.templates), ["templates/a.yaml", "templates/sub/b.yaml"])

    def test_render_fixture_orders_by_kind_and_writes_source_headers(self):
        chart = main.load_chart(WEBAPP)
        rendered = main.render_chart(chart, main.user_values([OVERRIDE]), "cc-web", "cc-ns")
        headers = [line for line in rendered.manifest.splitlines() if line.startswith("# Source:")]
        self.assertEqual(headers, ["# Source: webapp/templates/configmap.yaml", "# Source: webapp/templates/service.yaml", "# Source: webapp/templates/deployment.yaml"])
        docs = main.parse_documents(rendered.manifest)
        self.assertEqual([doc["kind"] for doc in docs], ["ConfigMap", "Service", "Deployment"])
        self.assertEqual(docs[0]["data"]["MESSAGE"], "installed with -f")
        self.assertEqual(docs[2]["spec"]["template"]["spec"]["containers"][0]["image"], "registry.k8s.io/pause:3.10")
        self.assertEqual(docs[2]["metadata"]["annotations"]["webapp/rendered-at-revision"], "1")
        self.assertEqual(len(docs[2]["spec"]["template"]["spec"]["containers"][0]["ports"]), 2)

    def test_config_checksum_changes_with_config_not_with_replicas(self):
        chart = main.load_chart(WEBAPP)

        def checksum(config):
            docs = main.parse_documents(main.render_chart(chart, config, "w").manifest)
            return docs[2]["spec"]["template"]["metadata"]["annotations"]["checksum/config"]

        base = checksum({})
        self.assertEqual(checksum({"replicaCount": 5}), base)
        self.assertNotEqual(checksum({"message": "changed"}), base)

    def test_hooks_partials_notes_and_yaml_errors(self):
        chart = chart_from({
            "templates/_helpers.tpl": "not: [rendered",
            "templates/NOTES.txt": "{{ .Values.nope.deeper }}",
            "templates/cm.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cm\n",
            "templates/test.yaml": "apiVersion: v1\nkind: Pod\nmetadata:\n  name: t\n  annotations:\n    \"helm.sh/hook\": test\n",
        })
        rendered = main.render_chart(chart, {}, "r")
        self.assertEqual([e["name"] for e in rendered.entries], ["cm"])
        self.assertEqual([(h["name"], h["events"]) for h in rendered.hooks], [("t", ["test"])])
        self.assertTrue(main.template_output(rendered).rstrip().endswith('"helm.sh/hook": test'))
        broken = chart_from({"templates/bad.yaml": "a: 1\n  b: 2\n"})
        with self.assertRaises(main.ChartError) as error:
            main.render_chart(broken, {}, "r")
        self.assertIn("YAML parse error on demo/templates/bad.yaml", str(error.exception))
        with self.assertRaises(main.InputError):
            main.render_chart(broken, {}, "Bad_Name")


class RecordTests(unittest.TestCase):
    def test_release_payload_is_base64_of_gzipped_json(self):
        record = {"name": "web", "info": {"status": "deployed"}, "version": 1, "namespace": "ns", "manifest": "---\n"}
        payload = main.encode_release(record)
        self.assertEqual(base64.b64decode(payload)[:3], b"\x1f\x8b\x08")
        self.assertEqual(json.loads(gzip.decompress(base64.b64decode(payload))), record)
        self.assertEqual(main.decode_release(payload), record)

    def test_secret_object_has_helms_name_labels_type_and_double_base64(self):
        moment = datetime(2026, 11, 1, tzinfo=timezone.utc)
        record = {"name": "web", "info": {"status": "superseded"}, "version": 4, "namespace": "ns"}
        secret = main.secret_object(record, moment, created=True)
        self.assertEqual(secret["metadata"]["name"], "sh.helm.release.v1.web.v4")
        self.assertEqual(secret["type"], "helm.sh/release.v1")
        self.assertEqual(secret["metadata"]["labels"], {"name": "web", "owner": "helm", "status": "superseded", "version": "4", "createdAt": str(int(moment.timestamp()))})
        inner = base64.b64decode(secret["data"]["release"]).decode()
        self.assertEqual(main.decode_release(inner), record)
        self.assertEqual(main.release_from_secret(secret), record)
        self.assertEqual(main.decode_any(json.dumps(secret)), record)
        self.assertEqual(main.decode_any(secret["data"]["release"]), record)


class StatusLog:
    def __init__(self):
        self.backend = main.MemorySecrets()
        self.events = []

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def create(self, secret):
        self.events.append(("create", secret["metadata"]["name"], secret["metadata"]["labels"]["status"]))
        self.backend.create(secret)

    def update(self, secret):
        self.events.append(("update", secret["metadata"]["name"], secret["metadata"]["labels"]["status"]))
        self.backend.update(secret)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.chart = main.load_chart(WEBAPP)
        self.backend = StatusLog()
        self.applier = main.RecordingApplier()
        self.manager = main.Manager(main.ReleaseStorage(self.backend, "ns", ticking_clock()), self.applier, ticking_clock())

    def statuses(self, name="web"):
        return [(r["version"], r["info"]["status"], r["info"]["description"]) for r in self.manager.history(name)]

    def test_install_writes_pending_then_deployed_and_refuses_a_second_install(self):
        record = self.manager.install("web", self.chart, {})
        self.assertEqual(record["info"]["status"], "deployed")
        self.assertEqual(self.backend.events, [("create", "sh.helm.release.v1.web.v1", "pending-install"), ("update", "sh.helm.release.v1.web.v1", "deployed")])
        self.assertEqual(self.applier.calls[0][0], "apply")
        self.assertEqual({key[1] for key in self.applier.calls[0][2]}, {"ConfigMap", "Service", "Deployment"})
        self.assertNotIn("config", record)
        with self.assertRaises(main.ReleaseError) as again:
            self.manager.install("web", self.chart, {})
        self.assertIn("cannot reuse a name that is still in use", str(again.exception))

    def test_upgrade_reuses_old_values_only_when_none_are_given(self):
        first = main.user_values([OVERRIDE])
        self.manager.install("web", self.chart, first)
        reused = self.manager.upgrade("web", self.chart, {})
        self.assertEqual(reused["config"]["message"], "installed with -f")
        replaced = self.manager.upgrade("web", self.chart, {"replicaCount": 2})
        self.assertEqual(replaced["config"], {"replicaCount": 2})
        self.assertIn("MESSAGE: \"hello from minihelm\"", replaced["manifest"])
        merged = self.manager.upgrade("web", self.chart, {"logLevel": "warn"}, reuse_values=True)
        self.assertEqual(merged["config"], {"replicaCount": 2, "logLevel": "warn"})
        self.assertEqual([s[1] for s in self.statuses()], ["superseded", "superseded", "superseded", "deployed"])
        self.assertIn('webapp/rendered-at-revision: "4"', merged["manifest"])

    def test_rollback_writes_a_new_revision_that_replays_the_old_manifest(self):
        rev1 = self.manager.install("web", self.chart, main.user_values([OVERRIDE]))
        self.manager.upgrade("web", self.chart, {"replicaCount": 2})
        rev3 = self.manager.rollback("web", 1)
        self.assertEqual(rev3["version"], 3)
        self.assertEqual(rev3["manifest"], rev1["manifest"])
        self.assertEqual(rev3["config"], rev1["config"])
        self.assertIn('webapp/rendered-at-revision: "1"', rev3["manifest"])
        self.assertEqual(rev3["info"]["rollback_revision"], 1)
        self.assertEqual(self.statuses(), [(1, "superseded", "Install complete"), (2, "superseded", "Upgrade complete"), (3, "deployed", "Rollback to 1")])
        self.assertEqual(self.manager.rollback("web")["info"]["description"], "Rollback to 2")
        with self.assertRaises(main.ReleaseError) as missing:
            self.manager.rollback("web", 9)
        self.assertIn("release has no 9 version", str(missing.exception))

    def test_failed_apply_marks_failed_pending_blocks_upgrades_and_rollback_rescues(self):
        self.manager.install("web", self.chart, {})
        self.manager.applier = main.RecordingApplier(fail_with='Deployment.apps "web-webapp" is invalid')
        with self.assertRaises(main.ReleaseError) as failed:
            self.manager.upgrade("web", self.chart, {"replicaCount": 2})
        self.assertIn("UPGRADE FAILED", str(failed.exception))
        self.assertEqual(self.statuses()[-1][:2], (2, "failed"))
        self.assertEqual(self.statuses()[0][1], "deployed")
        stuck = self.manager.storage.history("web")[-1]
        main.set_status(stuck, "pending-upgrade")
        self.manager.storage.update(stuck)
        with self.assertRaises(main.ReleaseError) as pending:
            self.manager.upgrade("web", self.chart, {})
        self.assertIn("another operation (install/upgrade/rollback) is in progress", str(pending.exception))
        self.manager.applier = self.applier
        rescued = self.manager.rollback("web", 1)
        self.assertEqual((rescued["version"], rescued["info"]["status"]), (3, "deployed"))
        self.assertEqual([s[1] for s in self.statuses()], ["superseded", "pending-upgrade", "deployed"])
        with self.assertRaises(main.ReleaseError) as absent:
            self.manager.upgrade("ghost", self.chart, {})
        self.assertIn('"ghost" has no deployed releases', str(absent.exception))

    def test_upgrade_deletes_objects_the_new_manifest_no_longer_contains(self):
        self.manager.install("web", self.chart, {})
        self.manager.upgrade("web", self.chart, {"service": {"enabled": False}})
        self.assertEqual(self.applier.calls[-1], ("delete", "ns", [("v1", "Service", "web-webapp")]))
        self.assertNotIn(("ns", "v1", "Service", "web-webapp"), self.applier.live)

    def test_history_max_prunes_the_oldest_revisions_but_never_the_deployed_one(self):
        self.manager.max_history = 3
        self.manager.install("web", self.chart, {})
        for replicas in (2, 3, 4, 5):
            self.manager.upgrade("web", self.chart, {"replicaCount": replicas})
        self.assertEqual([s[0] for s in self.statuses()], [3, 4, 5])

    def test_uninstall_purges_records_or_keeps_them_marked_uninstalled(self):
        self.manager.install("web", self.chart, {})
        self.manager.install("api", self.chart, {})
        self.manager.uninstall("web")
        self.assertEqual(self.manager.storage.history("web"), [])
        self.assertEqual(self.applier.calls[-1][0], "delete")
        self.manager.uninstall("api", keep_history=True)
        kept = self.manager.storage.history("api")[-1]
        self.assertEqual((kept["info"]["status"], kept["info"]["description"]), ("uninstalled", "Uninstallation complete"))
        self.assertIn("deleted", kept["info"])


class FakeKubectl:
    def __init__(self):
        self.calls = []
        self.secrets = {}
        self.applied = []

    def __call__(self, args, stdin=None):
        args = list(args)
        self.calls.append((args, stdin))
        if args[1] == "--context":
            args = [args[0]] + args[3:]
        verb = args[1]
        if verb == "create":
            secret = json.loads(stdin)
            if secret["metadata"]["name"] in self.secrets:
                return 1, "", 'Error from server (AlreadyExists): secrets "x" already exists'
            self.secrets[secret["metadata"]["name"]] = secret
            return 0, "secret/created\n", ""
        if verb == "replace":
            secret = json.loads(stdin)
            self.secrets[secret["metadata"]["name"]] = secret
            return 0, "", ""
        if verb == "get" and args[2] == "secrets":
            wanted = dict(pair.split("=") for pair in args[args.index("-l") + 1].split(","))
            items = [s for s in self.secrets.values() if all(s["metadata"]["labels"].get(k) == v for k, v in wanted.items())]
            return 0, json.dumps({"apiVersion": "v1", "kind": "List", "items": items}), ""
        if verb == "get" and args[2] == "secret":
            if args[3] not in self.secrets:
                return 1, "", f'Error from server (NotFound): secrets "{args[3]}" not found'
            return 0, json.dumps(self.secrets[args[3]]), ""
        if verb in ("apply", "delete"):
            self.applied.append((verb, stdin))
            return 0, "", ""
        return 1, "", "unexpected " + " ".join(args)


class KubectlTests(unittest.TestCase):
    def test_secret_store_and_applier_drive_kubectl_like_helm_would(self):
        fake = FakeKubectl()
        storage = main.ReleaseStorage(main.KubectlSecrets(fake, "cc-ns", "kind-cc-smoke"), "cc-ns", ticking_clock())
        manager = main.Manager(storage, main.KubectlApplier(fake, "kind-cc-smoke"), ticking_clock())
        chart = main.load_chart(WEBAPP)
        manager.install("cc-mini", chart, {})
        manager.upgrade("cc-mini", chart, {"replicaCount": 2})
        create_args, create_stdin = next(call for call in fake.calls if "create" in call[0])
        self.assertEqual(create_args, ["kubectl", "--context", "kind-cc-smoke", "create", "-n", "cc-ns", "-f", "-"])
        self.assertEqual(json.loads(create_stdin)["metadata"]["labels"]["status"], "pending-install")
        list_args = next(call[0] for call in fake.calls if call[0][3:5] == ["get", "secrets"])
        self.assertEqual(list_args[list_args.index("-l") + 1], "name=cc-mini,owner=helm")
        self.assertEqual([verb for verb, _ in fake.applied], ["apply", "apply"])
        self.assertEqual(main.parse_documents(fake.applied[1][1])[2]["spec"]["replicas"], 2)
        self.assertEqual(sorted(fake.secrets), ["sh.helm.release.v1.cc-mini.v1", "sh.helm.release.v1.cc-mini.v2"])
        self.assertEqual(fake.secrets["sh.helm.release.v1.cc-mini.v1"]["metadata"]["labels"]["status"], "superseded")
        self.assertIn("createdAt", fake.secrets["sh.helm.release.v1.cc-mini.v1"]["metadata"]["labels"])
        with self.assertRaises(main.ReleaseError) as exists:
            storage.create(manager.history("cc-mini")[0])
        self.assertEqual(str(exists.exception), "release: already exists")


class CliTests(unittest.TestCase):
    def run_cli(self, *argv, clock=None):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(list(argv), clock=clock or ticking_clock())
        return code, out.getvalue(), err.getvalue()

    def test_template_prints_the_manifest_and_json_and_exits_2_on_template_errors(self):
        code, out, _ = self.run_cli("template", "cc-web", WEBAPP, "-f", OVERRIDE, "-n", "cc-ns")
        self.assertEqual(code, 0)
        self.assertEqual(out, main.template_output(main.render_chart(main.load_chart(WEBAPP), main.user_values([OVERRIDE]), "cc-web", "cc-ns")))
        code, out, _ = self.run_cli("template", "cc-web", WEBAPP, "--json", "--set", "service.enabled=false")
        self.assertEqual([doc["kind"] for doc in json.loads(out)], ["ConfigMap", "Deployment"])
        code, _, err = self.run_cli("template", "cc-web", WEBAPP, "--set", "team=null")
        self.assertEqual(code, 2)
        self.assertIn("team is required", err)

    def test_lifecycle_with_the_dir_store_history_get_and_decode(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        store = ["--store", "dir:" + tmp, "-n", "demo"]
        clock = ticking_clock()
        self.assertEqual(self.run_cli("install", "web", WEBAPP, "-f", OVERRIDE, *store, clock=clock)[0], 0)
        self.assertEqual(self.run_cli("upgrade", "web", WEBAPP, "--set", "replicaCount=2", *store, clock=clock)[0], 0)
        self.assertEqual(self.run_cli("rollback", "web", "1", *store, clock=clock)[0], 0)
        code, out, _ = self.run_cli("history", "web", "--json", *store, clock=clock)
        rows = json.loads(out)
        self.assertEqual([(r["revision"], r["status"], r["description"]) for r in rows], [(1, "superseded", "Install complete"), (2, "superseded", "Upgrade complete"), (3, "deployed", "Rollback to 1")])
        self.assertEqual(rows[2]["chart"], "webapp-0.1.0")
        code, out, _ = self.run_cli("get", "values", "web", "--json", *store, clock=clock)
        self.assertEqual(json.loads(out)["message"], "installed with -f")
        code, out, _ = self.run_cli("decode", os.path.join(tmp, "demo", "sh.helm.release.v1.web.v3.json"))
        record = json.loads(out)
        self.assertEqual((record["name"], record["version"], record["info"]["status"]), ("web", 3, "deployed"))
        code, _, err = self.run_cli("upgrade", "ghost", WEBAPP, *store, clock=clock)
        self.assertEqual(code, 1)
        self.assertIn("has no deployed releases", err)
        code, out, _ = self.run_cli("uninstall", "web", *store, clock=clock)
        self.assertEqual((code, out.strip()), (0, 'release "web" uninstalled'))
        self.assertEqual(os.listdir(os.path.join(tmp, "demo")), [])


if __name__ == "__main__":
    unittest.main()
