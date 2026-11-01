import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

INI = """# web tier, declared ungrouped first with an inline var
web[01:02].example.com http_port=8080

[web]
web[01:02].example.com
canary.example.com http_port=8081 track=canary

[db]
db1.example.com ansible_host=10.0.0.5 ansible_port=2222

[prod:children]
web
db

[prod:vars]
ntp_server=ntp.prod.example.com
http_port=80
backup=True

[all:vars]
ntp_server=pool.ntp.org
"""

YAML_INVENTORY = """
all:
  vars:
    ntp_server: pool.ntp.org
  children:
    prod:
      vars:
        ntp_server: ntp.prod.example.com
        http_port: 80
        backup: true
      children:
        web:
          hosts:
            web[01:02].example.com:
              http_port: 8080
            canary.example.com:
              http_port: 8081
              track: canary
        db:
          hosts:
            db1.example.com:
              ansible_host: 10.0.0.5
              ansible_port: 2222
"""

PLAYBOOK = """
- name: configure the app
  hosts: app
  vars:
    app_root: /srv/app
    app_port: 8080
    features: [metrics, healthz]
  tasks:
    - name: app directory
      ansible.builtin.file:
        path: "{{ app_root }}"
        state: directory
        mode: "0755"
    - name: config
      ansible.builtin.template:
        src: app.conf.j2
        dest: "{{ app_root }}/app.conf"
      notify: restart app
    - name: env port
      ansible.builtin.lineinfile:
        path: "{{ app_root }}/app.env"
        regexp: "^PORT="
        line: "PORT={{ app_port }}"
        create: yes
      notify: restart app
    - name: seed once
      ansible.builtin.command: touch {{ app_root }}/seeded creates={{ app_root }}/seeded
    - name: only on debian
      ansible.builtin.copy:
        content: "debian\\n"
        dest: "{{ app_root }}/family"
      when: ansible_facts.os_family == "Debian"
    - name: only on alpine
      ansible.builtin.copy:
        content: "alpine\\n"
        dest: "{{ app_root }}/family"
      when: ansible_facts.os_family == "Alpine"
  handlers:
    - name: restart app
      ansible.builtin.service:
        name: app
        state: restarted
"""

TEMPLATE = """[server]
host = {{ ansible_facts.hostname }}
port = {{ app_port }}
{% for feature in features %}
feature.{{ feature }} = on
{% endfor %}
"""


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class Workspace(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="miniansible-")
        self.addCleanup(shutil.rmtree, self.dir)
        write(os.path.join(self.dir, "templates", "app.conf.j2"), TEMPLATE)
        self.conns = {}

    def connect(self, host, variables, default):
        if host not in self.conns:
            self.conns[host] = main.FakeConnection(hostname=host.split(".")[0])
        return self.conns[host]

    def run_playbook(self, text=PLAYBOOK, inventory="[app]\nweb1\nweb2\n", **options):
        plays, _ = main.parse_playbook(text)
        lines = []
        runner = main.PlaybookRunner(main.parse_ini_inventory(inventory), connect_fn=self.connect, emit=lines.append, warn=lines.append, playbook_dir=self.dir, **options)
        code = runner.run(plays)
        return code, "\n".join(lines), runner


class YamlTests(unittest.TestCase):
    def test_yaml_subset_reads_playbook_shapes_like_pyyaml(self):
        data = main.parse_yaml("""
- name: demo
  hosts: all
  become: yes
  vars:
    mode_quoted: "0644"
    mode_octal: 0644
    mode_decimal: 644
    ports: [80, 443]
    folded: >-
      one
      two
  tasks:
    - copy:
        content: |
          line one
            indented
          # not a comment inside a block
        dest: /tmp/x   # a comment
""")
        play = data[0]
        self.assertIs(play["become"], True)
        self.assertEqual(play["vars"]["mode_quoted"], "0644")
        self.assertEqual(play["vars"]["mode_octal"], 0o644)
        self.assertEqual(play["vars"]["mode_decimal"], 644)
        self.assertEqual(play["vars"]["ports"], [80, 443])
        self.assertEqual(play["vars"]["folded"], "one two")
        self.assertEqual(play["tasks"][0]["copy"]["content"], "line one\n  indented\n# not a comment inside a block\n")
        self.assertEqual(play["tasks"][0]["copy"]["dest"], "/tmp/x")

    def test_unquoted_jinja_value_is_an_error_with_the_line(self):
        with self.assertRaises(main.YamlError) as caught:
            main.parse_yaml("- hosts: all\n  vars:\n    x: {{ y }}\n")
        self.assertEqual(caught.exception.line, 3)
        self.assertIn("must be quoted", str(caught.exception))


class ExpressionTests(unittest.TestCase):
    def ctx(self, **variables):
        return main.Context(variables)

    def value(self, text, **variables):
        return main.template_value(text, self.ctx(**variables))

    def test_single_expressions_keep_native_types_and_filters_work(self):
        self.assertEqual(self.value("{{ port }}", port=8080), 8080)
        self.assertEqual(self.value("port={{ port }}", port=8080), "port=8080")
        self.assertEqual(self.value("{{ missing | default('x') | upper }}"), "X")
        self.assertEqual(self.value("{{ items | join(',') }}", items=["a", "b"]), "a,b")
        self.assertEqual(self.value("{{ items | length }}", items=[1, 2, 3]), 3)
        self.assertEqual(self.value("{{ facts.os['family'] }}", facts={"os": {"family": "Debian"}}), "Debian")
        self.assertEqual(self.value("{{ 'b' in items and not ('z' in items) }}", items=["a", "b"]), True)
        self.assertEqual(self.value("{{ x is defined }}/{{ y is not defined }}", x=1), "True/True")
        self.assertEqual(self.value("{{ 'on' if enabled else 'off' }}", enabled=False), "off")

    def test_undefined_variables_and_missing_keys_fail_loudly(self):
        with self.assertRaises(main.TemplateError) as caught:
            self.value("{{ nope }}")
        self.assertIn("'nope' is undefined", str(caught.exception))
        with self.assertRaises(main.TemplateError) as caught:
            self.value("{{ d.missing }}", d={})
        self.assertIn("has no attribute 'missing'", str(caught.exception))

    def test_conditionals_must_be_boolean(self):
        ctx = self.ctx(name="web", count=3)
        self.assertTrue(main.evaluate_condition("name == 'web' and count >= 3", ctx))
        self.assertFalse(main.evaluate_condition("name != 'web' or count < 3", ctx))
        with self.assertRaises(main.TemplateError) as caught:
            main.evaluate_condition("name", ctx)
        self.assertIn("Conditionals must have a boolean result", str(caught.exception))

    def test_variables_are_templated_lazily_and_loops_are_detected(self):
        ctx = self.ctx(root="/srv", app_root="{{ root }}/app", conf="{{ app_root }}/app.conf", a="{{ b }}", b="{{ a }}")
        self.assertEqual(main.template_value("{{ conf }}", ctx), "/srv/app/app.conf")
        with self.assertRaises(main.TemplateError) as caught:
            main.template_value("{{ a }}", ctx)
        self.assertIn("recursive loop", str(caught.exception))


class TemplateTests(unittest.TestCase):
    def render(self, source, **variables):
        return main.render_template(source, main.Context(variables))

    def test_if_for_and_trim_blocks_match_ansible_defaults(self):
        source = "{% if debug %}\ndebug = true\n{% elif verbose %}\nverbose = true\n{% else %}\nquiet = true\n{% endif %}\n{% for f in features %}\n{{ loop.index }}:{{ f }}\n{% endfor %}\n"
        self.assertEqual(self.render(source, debug=False, verbose=True, features=["a", "b"]), "verbose = true\n1:a\n2:b\n")
        self.assertEqual(self.render("{% for k, v in d.items() %}{{ k }}={{ v }};{% endfor %}", d={"x": 1, "y": 2}), "x=1;y=2;")
        self.assertEqual(self.render("a  {%- if true -%}  b  {%- endif -%}  c"), "abc")
        self.assertEqual(self.render("{# note #}\nvalue={{ v }}\n\n", v=1), "value=1\n\n")
        self.assertEqual(self.render("{% set n = 2 %}{{ n + 1 }}"), "3")

    def test_template_errors_name_the_line(self):
        with self.assertRaises(main.TemplateError) as caught:
            self.render("ok\n{% while x %}\n")
        self.assertIn("unknown tag 'while'", str(caught.exception))
        with self.assertRaises(main.TemplateError) as caught:
            self.render("a\nb={{ missing }}\n")
        self.assertIn("line 2", str(caught.exception))


class InventoryTests(unittest.TestCase):
    def test_ini_groups_children_ranges_and_vars_precedence(self):
        inventory = main.parse_ini_inventory(INI)
        self.assertEqual(list(inventory.hosts), ["web01.example.com", "web02.example.com", "canary.example.com", "db1.example.com"])
        self.assertEqual(inventory.groups["ungrouped"].hosts, [])
        self.assertEqual(inventory.groups["all"].children, ["ungrouped", "prod"])
        self.assertEqual(inventory.groups["prod"].children, ["web", "db"])
        self.assertEqual(inventory.depths()["web"], 2)
        self.assertEqual(inventory.host_vars("web01.example.com"), {"ntp_server": "ntp.prod.example.com", "http_port": 8080, "backup": True})
        self.assertEqual(inventory.host_vars("db1.example.com"), {"ntp_server": "ntp.prod.example.com", "http_port": 80, "backup": True, "ansible_host": "10.0.0.5", "ansible_port": 2222})
        self.assertEqual(inventory.host_vars("canary.example.com")["track"], "canary")

    def test_yaml_inventory_gives_the_same_hosts_and_vars(self):
        ini, yml = main.parse_ini_inventory(INI), main.parse_yaml_inventory(YAML_INVENTORY)
        self.assertEqual(sorted(ini.hosts), sorted(yml.hosts))
        for host in ini.hosts:
            self.assertEqual(ini.host_vars(host), yml.host_vars(host), host)
        self.assertEqual(ini.to_list()["_meta"], yml.to_list()["_meta"])

    def test_graph_list_and_patterns(self):
        inventory = main.parse_ini_inventory(INI)
        self.assertEqual(inventory.graph().splitlines()[:5], ["@all:", "  |--@ungrouped:", "  |--@prod:", "  |  |--@web:", "  |  |  |--web01.example.com"])
        listed = inventory.to_list()
        self.assertEqual(listed["all"]["children"], ["ungrouped", "prod"])
        self.assertNotIn("ungrouped", listed)
        self.assertEqual(listed["db"]["hosts"], ["db1.example.com"])
        self.assertEqual(inventory.match("web:&prod:!canary.example.com"), ["web01.example.com", "web02.example.com"])
        self.assertEqual(inventory.match("all:!web"), ["db1.example.com"])
        self.assertEqual(inventory.match("web0*"), ["web01.example.com", "web02.example.com"])

    def test_group_priority_and_sibling_order(self):
        text = "[a]\nh1\n[b]\nh1\n[a:vars]\ncolor=red\n[b:vars]\ncolor=blue\n"
        self.assertEqual(main.parse_ini_inventory(text).host_vars("h1")["color"], "blue")
        prioritised = text + "[a:vars]\nansible_group_priority=10\n"
        self.assertEqual(main.parse_ini_inventory(prioritised).host_vars("h1")["color"], "red")

    def test_inventory_errors_are_specific(self):
        for text, fragment in (
            ("[web:children]\nmissing\n", "includes undefined group 'missing'"),
            ("[ghost:vars]\nx=1\n", "not valid for undefined group 'ghost'"),
            ("[web:hostz]\nh\n", "unknown type"),
            ("[web]\nh1 port\n", "expected key=value"),
        ):
            with self.assertRaises(main.InventoryError) as caught:
                main.parse_ini_inventory(text)
            self.assertIn(fragment, str(caught.exception))


class ModuleTests(unittest.TestCase):
    def setUp(self):
        self.conn = main.FakeConnection()

    def run_module(self, name, args, check=False, **variables):
        ctx = main.ModuleContext(self.conn, check=check, diff=True, variables=main.Context(variables))
        return main.MODULES[name].run(ctx, main.normalise_args(name, args))

    def test_file_creates_parents_with_mode_then_reports_ok(self):
        first = self.run_module("file", {"path": "/srv/app", "state": "directory", "mode": "0750"})
        self.assertTrue(first.changed)
        self.assertEqual(first.data["created"], ["/srv", "/srv/app"])
        self.assertEqual(self.conn.stat("/srv/app").mode, 0o750)
        self.assertFalse(self.run_module("file", {"path": "/srv/app", "state": "directory", "mode": "0750"}).changed)
        self.assertTrue(self.run_module("file", {"path": "/srv/app", "state": "directory", "mode": "0755"}).changed)
        with self.assertRaises(main.ModuleFailure) as caught:
            self.run_module("file", {"path": "/srv/missing", "state": "file"})
        self.assertIn("is absent, cannot continue", str(caught.exception))
        self.assertTrue(self.run_module("file", {"path": "/srv", "state": "absent"}).changed)
        self.assertIsNone(self.conn.stat("/srv/app"))

    def test_unquoted_decimal_mode_warns(self):
        result = self.run_module("file", {"path": "/tmp", "state": "directory", "mode": 644})
        self.assertIn("octal 1204", result.warnings[0])
        self.assertEqual(self.conn.stat("/tmp").mode, 644)

    def test_copy_compares_checksums_and_check_mode_never_writes(self):
        planned = self.run_module("copy", {"content": "hello\n", "dest": "/tmp/motd"}, check=True)
        self.assertTrue(planned.changed)
        self.assertIsNone(self.conn.stat("/tmp/motd"))
        self.assertIn("+hello", main.render_diff(planned.diffs[0]))
        self.assertTrue(self.run_module("copy", {"content": "hello\n", "dest": "/tmp/motd"}).changed)
        again = self.run_module("copy", {"content": "hello\n", "dest": "/tmp/motd"})
        self.assertFalse(again.changed)
        self.assertEqual(again.data["checksum"], "f572d396fae9206628714fb2ce00f72e94f2258f")
        self.assertEqual(self.conn.writes, ["/tmp/motd"])
        with self.assertRaises(main.ModuleFailure) as caught:
            self.run_module("copy", {"content": "x", "dest": "/nowhere/file"})
        self.assertIn("Destination directory /nowhere does not exist", str(caught.exception))

    def test_lineinfile_replaces_inserts_and_removes(self):
        present = main.lineinfile_present
        lines = ["# config\n", "PORT=1\n", "HOST=a\n"]
        self.assertEqual(present(lines, "PORT=8080", "^PORT="), (["# config\n", "PORT=8080\n", "HOST=a\n"], "line replaced"))
        self.assertEqual(present(lines, "PORT=1", "^NOPE="), (lines, ""))
        self.assertEqual(present(lines, "DEBUG=0", None, "^PORT=")[0], ["# config\n", "PORT=1\n", "DEBUG=0\n", "HOST=a\n"])
        self.assertEqual(present(["no newline"], "x")[0], ["no newline\n", "x\n"])
        self.assertEqual(main.lineinfile_absent(lines, regexp="^(PORT|HOST)=")[0], ["# config\n"])
        with self.assertRaises(main.ModuleFailure) as caught:
            self.run_module("lineinfile", {"path": "/tmp/app.env", "line": "PORT=1"})
        self.assertIn("does not exist !", str(caught.exception))
        self.assertTrue(self.run_module("lineinfile", {"path": "/tmp/app.env", "line": "PORT=1", "create": True}).changed)
        self.assertTrue(self.run_module("lineinfile", {"path": "/opt/new/app.env", "line": "PORT=1", "create": True}).changed)
        self.assertEqual(self.conn.files["/opt/new/app.env"], "PORT=1\n")
        self.assertFalse(self.run_module("lineinfile", {"path": "/tmp/app.env", "line": "PORT=1", "regexp": "^PORT="}).changed)
        self.assertTrue(self.run_module("lineinfile", {"path": "/tmp/app.env", "line": "PORT=1", "mode": "0600"}).changed)
        self.assertEqual(self.conn.stat("/tmp/app.env").mode, 0o600)

    def test_command_guards_and_check_mode(self):
        self.conn.files["/tmp/done"] = ""
        guarded = self.run_module("command", "touch /tmp/done creates=/tmp/done")
        self.assertFalse(guarded.changed)
        self.assertIn("not run command since '/tmp/done' exists", guarded.msg)
        self.assertTrue(self.run_module("command", "touch /tmp/other creates=/tmp/other", check=True).changed)
        self.assertTrue(self.run_module("command", "touch /tmp/other", check=True).skipped)
        self.assertEqual(self.conn.ran, [])
        ran = self.run_module("command", {"cmd": "echo hi"})
        self.assertTrue(ran.changed)
        self.assertEqual(self.conn.ran, [["echo", "hi"]])
        self.conn.commands["false*"] = main.CommandResult(1, "", "boom")
        self.assertTrue(self.run_module("command", "false").failed)
        self.assertEqual(self.run_module("shell", "echo a | wc -l").data["cmd"], ["/bin/sh", "-c", "echo a | wc -l"])

    def test_fake_service_is_idempotent_except_restart(self):
        self.assertTrue(self.run_module("service", {"name": "app", "state": "started", "enabled": True}).changed)
        self.assertFalse(self.run_module("service", {"name": "app", "state": "started", "enabled": True}).changed)
        restarted = self.run_module("service", {"name": "app", "state": "restarted"})
        self.assertTrue(restarted.changed)
        self.assertEqual(restarted.data["restarts"], 1)

    def test_facts_from_uname_and_os_release(self):
        facts = main.gather_facts(main.FakeConnection(hostname="web1.example.com"))
        self.assertEqual((facts["hostname"], facts["system"], facts["distribution"], facts["distribution_major_version"], facts["os_family"]), ("web1", "Linux", "Ubuntu", "24", "Debian"))
        alpine = main.FakeConnection(files={"/etc/os-release": 'NAME="Alpine Linux"\nID=alpine\nVERSION_ID=3.22.2\n'})
        self.assertEqual(main.gather_facts(alpine)["os_family"], "Alpine")
        ctx = main.ModuleContext(alpine)
        self.assertEqual(sorted(main.module_setup(ctx, {"filter": "ansible_os_family,ansible_hostname"}).data["ansible_facts"]), ["ansible_hostname", "ansible_os_family"])


class DockerConnectionTests(unittest.TestCase):
    def test_every_operation_is_a_docker_exec(self):
        calls = []

        def runner(argv, stdin=None):
            calls.append((list(argv), stdin))
            script = argv[argv.index("-c") + 1] if "-c" in argv else ""
            if script.startswith("if [ -d"):
                return main.CommandResult(0, "file 644\n") if argv[-1] == "/etc/motd" else main.CommandResult(3)
            return main.CommandResult(0, "contents")

        conn = main.DockerConnection("box", runner)
        self.assertEqual(conn.stat("/etc/motd"), main.FileStat("file", 0o644))
        self.assertIsNone(conn.stat("/missing"))
        conn.write("/etc/motd", "hi\n")
        self.assertEqual(calls[-1], (["docker", "exec", "-i", "box", "sh", "-c", 'cat > "$1"', "sh", "/etc/motd"], "hi\n"))
        conn.run(["uname", "-n"], chdir="/tmp")
        self.assertEqual(calls[-1][0], ["docker", "exec", "-w", "/tmp", "box", "uname", "-n"])

    def test_missing_container_is_unreachable(self):
        conn = main.DockerConnection("ghost", lambda argv, stdin=None: main.CommandResult(1, "", "Error response from daemon: No such container: ghost"))
        with self.assertRaises(main.Unreachable):
            conn.stat("/etc")


class PlaybookTests(Workspace):
    def test_first_run_changes_second_run_is_idempotent_handler_runs_once(self):
        code, output, runner = self.run_playbook()
        self.assertEqual(code, 0)
        self.assertEqual(main.parse_recap(output)["web1"], {"ok": 7, "changed": 6, "unreachable": 0, "failed": 0, "skipped": 1, "rescued": 0, "ignored": 0})
        conn = self.conns["web1"]
        self.assertIn("/srv/app/seeded", conn.files)
        self.assertEqual(conn.files["/srv/app/app.conf"], "[server]\nhost = web1\nport = 8080\nfeature.metrics = on\nfeature.healthz = on\n")
        self.assertEqual(conn.files["/srv/app/family"], "debian\n")
        self.assertEqual(json.loads(conn.files["/tmp/miniansible-services.json"])["app"]["restarts"], 1)
        self.assertEqual(output.count("RUNNING HANDLER [restart app]"), 1)
        code, output, _ = self.run_playbook()
        self.assertEqual(main.parse_recap(output)["web2"]["changed"], 0)
        self.assertNotIn("RUNNING HANDLER", output)
        self.assertEqual(json.loads(conn.files["/tmp/miniansible-services.json"])["app"]["restarts"], 1)

    def test_check_mode_reports_changes_and_writes_nothing(self):
        self.run_playbook()
        conn = self.conns["web1"]
        conn.files["/srv/app/app.env"] = "PORT=9999\n"
        before = len(conn.writes)
        code, output, _ = self.run_playbook(check=True, diff=True)
        self.assertEqual(code, 0)
        self.assertEqual(main.parse_recap(output)["web1"]["changed"], 2)
        self.assertIn("-PORT=9999", output)
        self.assertIn("+PORT=8080", output)
        self.assertEqual(conn.files["/srv/app/app.env"], "PORT=9999\n")
        self.assertEqual(len(conn.writes), before)

    def test_when_loop_register_and_changed_when(self):
        text = """
- hosts: app
  gather_facts: false
  vars:
    users: [ann, bob]
  tasks:
    - name: home dirs
      file:
        path: "/tmp/{{ item }}"
        state: directory
      loop: "{{ users }}"
      register: homes
    - name: read only command
      command: uname -n
      register: who
      changed_when: false
    - name: report
      debug:
        msg: "{{ homes.results | length }} dirs, host {{ who.stdout }}"
      when: homes.changed and who.rc == 0
"""
        code, output, runner = self.run_playbook(text, inventory="[app]\nweb1\n")
        self.assertEqual(code, 0)
        self.assertIn("changed: [web1] => (item=ann)", output)
        self.assertIn('"msg": "2 dirs, host web1"', output)
        self.assertEqual(main.parse_recap(output)["web1"], {"ok": 3, "changed": 1, "unreachable": 0, "failed": 0, "skipped": 0, "rescued": 0, "ignored": 0})

    def test_changed_when_is_not_applied_to_skipped_tasks(self):
        text = """
- hosts: app
  gather_facts: false
  tasks:
    - name: append
      shell:
        cmd: echo x >> /tmp/log
      changed_when: true
    - name: tolerated failure
      command: "false"
      failed_when: false
"""
        self.conns["web1"] = main.FakeConnection(commands={"false": main.CommandResult(1, "", "")})
        _, output, _ = self.run_playbook(text, inventory="[app]\nweb1\n", check=True)
        self.assertIn("skipping: [web1]", output)
        self.assertEqual(main.parse_recap(output)["web1"]["skipped"], 2)
        _, output, _ = self.run_playbook(text, inventory="[app]\nweb1\n")
        self.assertEqual(main.parse_recap(output)["web1"], {"ok": 2, "changed": 2, "unreachable": 0, "failed": 0, "skipped": 0, "rescued": 0, "ignored": 0})

    def test_a_failure_stops_the_host_and_its_handlers(self):
        text = """
- hosts: app
  gather_facts: false
  tasks:
    - copy:
        content: "x"
        dest: /tmp/x
      notify: h
    - command: "false"
    - copy:
        content: "never"
        dest: /tmp/never
  handlers:
    - name: h
      debug:
        msg: handler ran
"""
        self.conns["web1"] = main.FakeConnection(commands={"false": main.CommandResult(1, "", "")})
        code, output, _ = self.run_playbook(text, inventory="[app]\nweb1\n")
        self.assertEqual(code, 2)
        self.assertIn("fatal: [web1]: FAILED!", output)
        self.assertNotIn("handler ran", output)
        self.assertNotIn("/tmp/never", self.conns["web1"].files)
        self.assertEqual(main.parse_recap(output)["web1"]["failed"], 1)

    def test_variable_precedence_across_layers(self):
        text = """
- hosts: app
  gather_facts: false
  vars:
    level: play
    play_only: play
  tasks:
    - name: show
      debug:
        msg: "{{ level }} {{ group_level }} {{ host_level }} {{ play_only }}"
      vars:
        level: task
"""
        inventory = "[app]\nweb1 host_level=host level=host\n[app:vars]\ngroup_level=group\nlevel=group\nhost_level=group\n"
        _, output, _ = self.run_playbook(text, inventory=inventory)
        self.assertIn('"msg": "task group host play"', output)
        _, output, _ = self.run_playbook(text, inventory=inventory, extra_vars={"level": "extra"})
        self.assertIn('"msg": "extra group host play"', output)

    def test_unreachable_docker_host(self):
        def connect(host, variables, default):
            return main.DockerConnection(host, lambda argv, stdin=None: main.CommandResult(1, "", "No such container: " + host))

        plays, _ = main.parse_playbook("- hosts: all\n  tasks:\n    - ping:\n")
        lines = []
        runner = main.PlaybookRunner(main.parse_ini_inventory("ghost ansible_connection=docker\n"), connect_fn=connect, emit=lines.append)
        self.assertEqual(runner.run(plays), 4)
        self.assertEqual(main.parse_recap("\n".join(lines))["ghost"]["unreachable"], 1)

    def test_loader_rejects_mistakes_with_ansible_style_messages(self):
        for text, fragment in (
            ("- hosts: all\n  tasks:\n    - copyy:\n        dest: /x\n", "couldn't resolve module/action 'copyy'"),
            ("- hosts: all\n  tasks:\n    - copy: {dest: /x, content: a}\n      file: {path: /x}\n", "conflicting action statements: copy, file"),
            ("- hosts: all\n  tasks:\n    - copy: {dest: /x, contnet: a}\n", "Unsupported parameters for (copy) module: contnet"),
            ("- hosts: all\n  tasks:\n    - ping:\n      notify: nobody\n", "notifies 'nobody'"),
            ("- hosts: all\n  taks: []\n", "'taks' is not a valid attribute for a Play"),
        ):
            with self.assertRaises(main.PlaybookError) as caught:
                main.parse_playbook(text)
            self.assertIn(fragment, str(caught.exception))


class CliTests(Workspace):
    def cli(self, *argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_inventory_commands(self):
        path = os.path.join(self.dir, "hosts.ini")
        write(path, INI)
        code, out, _ = self.cli("inventory", "-i", path, "--list")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["_meta"]["hostvars"]["web02.example.com"]["http_port"], 8080)
        code, out, _ = self.cli("inventory", "-i", path, "--host", "db1.example.com", "--explain")
        explained = json.loads(out)
        self.assertEqual(explained["ntp_server"]["source"], "group_vars (prod)")
        self.assertEqual(explained["ntp_server"]["overrides"], ["group_vars (all)"])
        self.assertEqual(self.cli("inventory", "-i", path, "--graph", "db")[1].strip(), "@db:\n  |--db1.example.com")

    def test_playbook_on_the_local_connection_twice(self):
        target = os.path.join(self.dir, "target")
        os.mkdir(target)
        playbook = os.path.join(self.dir, "site.yml")
        write(playbook, PLAYBOOK.replace("app_root: /srv/app", "app_root: \"{{ root }}/app\"").replace("ansible.builtin.service:\n        name: app\n        state: restarted", "ansible.builtin.shell: echo restarted >> {{ app_root }}/restarts"))
        inventory = os.path.join(self.dir, "hosts.ini")
        write(inventory, "[app]\nlocalhost ansible_connection=local\n")
        code, out, err = self.cli("playbook", "-i", inventory, playbook, "-e", f"root={target}", "--json")
        self.assertEqual(code, 0, err)
        summary = json.loads(out)
        self.assertGreaterEqual(summary["stats"]["localhost"]["changed"], 4)
        with open(os.path.join(target, "app", "restarts"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "restarted\n")
        code, out, _ = self.cli("playbook", "-i", inventory, playbook, "-e", f"root={target}")
        self.assertEqual(main.parse_recap(out)["localhost"]["changed"], 0)

    def test_adhoc_ping_and_errors(self):
        code, out, _ = self.cli("adhoc", "localhost", "-m", "ping")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("localhost | SUCCESS => {"))
        code, _, err = self.cli("playbook", os.path.join(self.dir, "missing.yml"))
        self.assertEqual(code, 1)
        self.assertIn("cannot read playbook", err)
        self.assertEqual(main.parse_extra_vars(["a=1 b='two words'", '{"c": 3}']), {"a": "1", "b": "two words", "c": 3})

    def test_parse_recap_reads_the_real_format(self):
        text = "PLAY RECAP ****\nlocalhost                  : ok=8    changed=7    unreachable=0    failed=0    skipped=0    rescued=0    ignored=0   \n"
        self.assertEqual(main.parse_recap(text)["localhost"]["changed"], 7)


if __name__ == "__main__":
    unittest.main()
