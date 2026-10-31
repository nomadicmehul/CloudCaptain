import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

TWO_SERVICES = """
# a database and a web front end
name: shop
services:
  db:
    image: postgres:17-alpine
    environment:
      POSTGRES_PASSWORD: secret   # trailing comment
      POSTGRES_DB: shop
    volumes:
      - dbdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 2s
      timeout: 3s
      retries: 5
      start_period: 1s
  web:
    image: shop/web:1.4
    command: python3 -m http.server 8000
    ports:
      - "8080:8000"
    environment:
      - DATABASE_URL=postgres://db/shop
      - DEBUG
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped
volumes:
  dbdata: {}
"""


class FakeDocker:
    def __init__(self, health=None):
        self.calls = []
        self.networks = set()
        self.volumes = set()
        self.containers = {}
        self.health = {name: list(sequence) for name, sequence in (health or {}).items()}
        self.next_id = 0

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        assert args[0] == "docker", args
        verb = args[1]
        if verb == "network":
            return self.network(args[2:])
        if verb == "volume":
            return self.volume(args[2:])
        if verb == "run":
            name = args[args.index("--name") + 1]
            self.next_id += 1
            self.containers[name] = {"image": args[-1], "status": "running", "service": args[args.index("com.docker.compose.service=") + 1] if "com.docker.compose.service=" in args else name}
            return main.CommandResult(0, f"{'a' * 60}{self.next_id:04d}\n")
        if verb == "inspect":
            return self.inspect(args[2:])
        if verb == "ps":
            lines = []
            for name, info in self.containers.items():
                label = [a for a in self.calls if a[1] == "run" and name in a]
                service = next(a[a.index("--label") + 3].split("=", 1)[1] for a in label) if label else "?"
                health = info.get("reported_health", "")
                status = "Up 3 seconds" + (f" ({health})" if health else "")
                lines.append(f"{name}\t{service}\t{info['image']}\t{info['status']}\t{status}")
            return main.CommandResult(0, "\n".join(lines) + ("\n" if lines else ""))
        if verb == "stop":
            return self.container_action(args[2], "exited")
        if verb == "rm":
            name = args[-1]
            if name not in self.containers:
                return main.CommandResult(1, "", f"Error response from daemon: No such container: {name}")
            del self.containers[name]
            return main.CommandResult(0, name + "\n")
        raise AssertionError(f"unexpected docker command: {args}")

    def container_action(self, name, status):
        if name not in self.containers:
            return main.CommandResult(1, "", f"Error response from daemon: No such container: {name}")
        self.containers[name]["status"] = status
        return main.CommandResult(0, name + "\n")

    def network(self, args):
        if args[0] == "inspect":
            return main.CommandResult(0 if args[1] in self.networks else 1, "", "" if args[1] in self.networks else f"Error: No such network: {args[1]}")
        if args[0] == "create":
            self.networks.add(args[-1])
            return main.CommandResult(0, "netid\n")
        if args[0] == "rm":
            if args[-1] not in self.networks:
                return main.CommandResult(1, "", f"Error response from daemon: network {args[-1]} not found: No such network")
            self.networks.discard(args[-1])
            return main.CommandResult(0, args[-1] + "\n")
        if args[0] == "connect":
            return main.CommandResult(0)
        raise AssertionError(args)

    def volume(self, args):
        if args[0] == "inspect":
            return main.CommandResult(0 if args[1] in self.volumes else 1, "", "" if args[1] in self.volumes else "Error: No such volume")
        if args[0] == "create":
            self.volumes.add(args[-1])
            return main.CommandResult(0, args[-1] + "\n")
        if args[0] == "rm":
            self.volumes.discard(args[-1])
            return main.CommandResult(0, args[-1] + "\n")
        raise AssertionError(args)

    def inspect(self, args):
        if args[:2] == ["--type", "container"]:
            name = args[2]
            return main.CommandResult(0 if name in self.containers else 1, "", "" if name in self.containers else f"Error: No such container: {name}")
        template, name = args[1], args[2]
        if name not in self.containers:
            return main.CommandResult(1, "", f"Error: No such object: {name}")
        if "Health" in template:
            sequence = self.health.get(name)
            if sequence is None:
                return main.CommandResult(0, "none\n")
            value = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            self.containers[name]["reported_health"] = value
            return main.CommandResult(0, value + "\n")
        if "ExitCode" in template:
            return main.CommandResult(0, f"{self.containers[name]['status']} {self.containers[name].get('exit_code', 0)}\n")
        return main.CommandResult(0, self.containers[name]["status"] + "\n")


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def project_from(text, name=None):
    return main.load_project(main.parse_yaml(text), name, "/srv/shop")


class YamlParserTests(unittest.TestCase):
    def test_mappings_sequences_and_nesting(self):
        data = main.parse_yaml("a:\n  b: 1\n  c:\n    - x\n    - y\nd: [1, 2]\n")
        self.assertEqual(data, {"a": {"b": 1, "c": ["x", "y"]}, "d": [1, 2]})

    def test_scalars_quotes_comments_and_escapes(self):
        data = main.parse_yaml(
            'port: "8080:80"   # quoted keeps the colon\n'
            "flag: true\n"
            "nothing: ~\n"
            "version: '3.8'\n"
            "octal: 0755\n"
            "ratio: 1.5\n"
            "text: it''s plain # not a comment? yes a comment\n"
            'esc: "a\\tb\\"c"\n'
            "single: 'it''s'\n"
            "url: http://db:5432/x\n"
        )
        self.assertEqual(data["port"], "8080:80")
        self.assertIs(data["flag"], True)
        self.assertIsNone(data["nothing"])
        self.assertEqual(data["version"], "3.8")
        self.assertEqual(data["octal"], "0755")
        self.assertEqual(data["ratio"], 1.5)
        self.assertEqual(data["text"], "it''s plain")
        self.assertEqual(data["esc"], 'a\tb"c')
        self.assertEqual(data["single"], "it's")
        self.assertEqual(data["url"], "http://db:5432/x")

    def test_sequence_of_mappings_and_flow_collections(self):
        data = main.parse_yaml(
            "items:\n"
            "  - name: a\n"
            "    size: 1\n"
            "  - name: b\n"
            "  -\n"
            "    name: c\n"
            "  - [CMD, curl, -f, 'http://x']\n"
            "  - {k: v, n: 2}\n"
            "empty:\n"
            "next: 1\n"
        )
        self.assertEqual(data["items"][:3], [{"name": "a", "size": 1}, {"name": "b"}, {"name": "c"}])
        self.assertEqual(data["items"][3], ["CMD", "curl", "-f", "http://x"])
        self.assertEqual(data["items"][4], {"k": "v", "n": 2})
        self.assertIsNone(data["empty"])
        self.assertEqual(data["next"], 1)

    def test_list_at_same_indent_as_key(self):
        data = main.parse_yaml("ports:\n- 80:80\n- 443:443\nimage: x\n")
        self.assertEqual(data, {"ports": ["80:80", "443:443"], "image": "x"})

    def test_errors_carry_line_numbers(self):
        cases = {
            "a:\n  b: 1\n c: 2\n": (3, "bad indentation"),
            "a: &anchor x\n": (1, "anchors"),
            "a: |\n  text\n": (1, "block scalars"),
            "a: [1, 2\n": (1, "unterminated flow sequence"),
            'a: "open\n': (1, "unterminated double-quoted"),
            "a: 1\na: 2\n": (2, "duplicate key"),
            "\tx: 1\n": (1, "tabs"),
            "a:\n  - x\n  y: 1\n": (3, "expected a sequence item"),
        }
        for source, (line, fragment) in cases.items():
            with self.assertRaises(main.YamlError) as caught:
                main.parse_yaml(source)
            self.assertEqual(caught.exception.line, line, source)
            self.assertIn(fragment, str(caught.exception), source)
        self.assertEqual(main.parse_yaml("# only comments\n\n"), {})


class ModelTests(unittest.TestCase):
    def test_load_project_normalises_both_environment_forms_and_healthcheck(self):
        project = project_from(TWO_SERVICES)
        self.assertEqual(project.name, "shop")
        self.assertEqual(sorted(project.services), ["db", "web"])
        web, db = project.services["web"], project.services["db"]
        self.assertEqual(web.environment, {"DATABASE_URL": "postgres://db/shop", "DEBUG": None})
        self.assertEqual(db.environment, {"POSTGRES_PASSWORD": "secret", "POSTGRES_DB": "shop"})
        self.assertEqual(web.command, ["python3", "-m", "http.server", "8000"])
        self.assertEqual(web.ports, ["8080:8000"])
        self.assertEqual(web.depends_on, {"db": "service_healthy"})
        self.assertEqual(web.restart, "unless-stopped")
        self.assertEqual((db.healthcheck.interval, db.healthcheck.timeout, db.healthcheck.retries, db.healthcheck.start_period), (2.0, 3.0, 5, 1.0))
        self.assertEqual(db.healthcheck.command(), "pg_isready -U postgres")
        self.assertEqual(project.volumes, ["dbdata"])
        self.assertEqual(project.networks, ["default"])

    def test_durations_and_health_command_forms(self):
        self.assertEqual(main.parse_duration("1m30s", "x"), 90.0)
        self.assertEqual(main.parse_duration("500ms", "x"), 0.5)
        self.assertEqual(main.parse_duration("2h", "x"), 7200.0)
        self.assertEqual(main.format_duration(0.5), "500ms")
        self.assertEqual(main.format_duration(90), "90s")
        with self.assertRaises(main.ComposeError):
            main.parse_duration("soon", "services.db.healthcheck.interval")
        self.assertEqual(main.HealthCheck(["CMD", "wget", "-qO-", "http://localhost:8000/"]).command(), "wget -qO- http://localhost:8000/")
        self.assertEqual(main.HealthCheck(["CMD-SHELL", "curl -f localhost || exit 1"]).command(), "curl -f localhost || exit 1")
        self.assertIsNone(main.HealthCheck(["NONE"]).command())

    def test_validation_errors_name_the_path_and_the_fix(self):
        cases = [
            ("services: {}\n", "services: is required"),
            ("services:\n  web:\n    build: .\n", "build: is not supported"),
            ("services:\n  web:\n    ports: 80:80\n", "image: is required"),
            ("services:\n  web:\n    image: x\n    depends_on: [db]\n", "'db' is not a service"),
            ("services:\n  web:\n    image: x\n    depends_on:\n      web:\n        condition: service_started\n", "cannot depend on itself"),
            ("services:\n  db:\n    image: x\n  web:\n    image: y\n    depends_on:\n      db:\n        condition: service_ready\n", "condition must be one of"),
            ("services:\n  db:\n    image: x\n  web:\n    image: y\n    depends_on:\n      db:\n        condition: service_healthy\n", "needs a healthcheck: on db"),
            ("services:\n  db:\n    image: x\n    healthcheck:\n      test: [pg_isready]\n", "must start with CMD or CMD-SHELL"),
            ("services:\n  web:\n    image: x\n    restart: sometimes\n", "restart: must be one of"),
            ("services:\n  web:\n    image: x\n    networks: [backend]\n", "'backend' is not declared"),
            ("services:\n  web:\n    image: x\n    volumes:\n      - data:/data\n", "neither a path nor a top-level volume"),
            ("services:\n  web:\n    image: x\n    environment: DEBUG=1\n", "environment must be a mapping"),
        ]
        for source, fragment in cases:
            with self.assertRaises(main.ComposeError) as caught:
                project_from(source)
            self.assertIn(fragment, str(caught.exception), source)

    def test_unknown_keys_are_collected_not_rejected(self):
        project = project_from("services:\n  web:\n    image: x\n    deploy:\n      replicas: 3\n    container_name: custom\n")
        self.assertEqual(project.services["web"].ignored_keys, ["container_name", "deploy"])


class GraphTests(unittest.TestCase):
    def test_start_order_is_topological_with_alphabetical_ties(self):
        project = project_from(
            "services:\n"
            "  web:\n    image: w\n    depends_on: [api, cache]\n"
            "  api:\n    image: a\n    depends_on: [db]\n"
            "  cache:\n    image: c\n"
            "  db:\n    image: d\n"
            "  worker:\n    image: k\n    depends_on: [api]\n"
        )
        self.assertEqual(main.start_order(project), ["cache", "db", "api", "web", "worker"])
        self.assertEqual(main.stop_order(project), ["worker", "web", "api", "db", "cache"])

    def test_cycles_are_reported_as_a_path(self):
        project = project_from("services:\n  a:\n    image: x\n    depends_on: [b]\n  b:\n    image: x\n    depends_on: [c]\n  c:\n    image: x\n    depends_on: [a]\n  d:\n    image: x\n")
        with self.assertRaises(main.ComposeError) as caught:
            main.start_order(project)
        self.assertEqual(str(caught.exception), "dependency cycle: a -> b -> c -> a")


class RenderTests(unittest.TestCase):
    def test_run_args_are_exact(self):
        project = project_from(TWO_SERVICES)
        self.assertEqual(main.network_create_args(project, "default"), ["docker", "network", "create", "--label", "com.docker.compose.project=shop", "--label", "com.docker.compose.network=default", "shop_default"])
        self.assertEqual(
            main.run_args(project, project.services["web"]),
            [
                "docker", "run", "-d", "--name", "shop-web-1",
                "--label", "com.docker.compose.project=shop", "--label", "com.docker.compose.service=web",
                "--network", "shop_default", "--network-alias", "web",
                "--restart", "unless-stopped",
                "-p", "8080:8000",
                "-e", "DATABASE_URL=postgres://db/shop", "-e", "DEBUG",
                "shop/web:1.4", "python3", "-m", "http.server", "8000",
            ],
        )
        db = main.run_args(project, project.services["db"])
        self.assertEqual(db[db.index("-v") + 1], "shop_dbdata:/var/lib/postgresql/data")
        self.assertEqual(db[db.index("--health-cmd") :], ["--health-cmd", "pg_isready -U postgres", "--health-interval", "2s", "--health-timeout", "3s", "--health-retries", "5", "--health-start-period", "1s", "postgres:17-alpine"])
        self.assertNotIn("--restart", db)

    def test_volumes_networks_and_plan_order(self):
        project = project_from(
            "services:\n"
            "  web:\n    image: w\n    networks: [front, back]\n    volumes:\n      - ./site:/usr/share/nginx/html:ro\n      - /var/log:/logs\n    depends_on: [db]\n"
            "  db:\n    image: d\n    networks: [back]\n"
            "networks:\n  front: {}\n  back: {}\n",
            name="demo",
        )
        plan = main.render_plan(project)
        self.assertEqual(project.networks, ["back", "front"], "default is only created when a service uses it")
        self.assertEqual(plan[0][-1], "demo_back")
        self.assertEqual(plan[1][-1], "demo_front")
        self.assertEqual(plan[2][plan[2].index("--name") + 1], "demo-db-1")
        web = plan[3]
        self.assertEqual(web[web.index("--network") + 1], "demo_front")
        self.assertEqual(web[web.index("-v") + 1], "/srv/shop/site:/usr/share/nginx/html:ro")
        self.assertIn("/var/log:/logs", web)
        self.assertEqual(plan[4], ["docker", "network", "connect", "--alias", "web", "demo_back", "demo-web-1"])
        self.assertEqual(len(plan), 5)

    def test_normalised_config_is_stable_json(self):
        config = main.normalised(project_from(TWO_SERVICES))
        self.assertEqual(list(config["services"]), ["db", "web"])
        self.assertEqual(config["services"]["web"]["depends_on"], {"db": {"condition": "service_healthy"}})
        self.assertEqual(config["services"]["db"]["healthcheck"]["interval"], "2s")
        self.assertEqual(config["services"]["db"]["volumes"], ["shop_dbdata:/var/lib/postgresql/data"])
        self.assertEqual(config["networks"], {"default": {"name": "shop_default"}})
        json.dumps(config)


class EngineTests(unittest.TestCase):
    def make(self, text=TWO_SERVICES, health=None, name=None):
        project = project_from(text, name)
        docker = FakeDocker(health)
        clock = FakeClock()
        logs = []
        engine = main.Engine(project, docker, now=clock, sleep=clock.sleep, log=logs.append, poll_interval=0.5)
        return engine, docker, clock, logs

    def test_up_waits_for_health_before_starting_dependents(self):
        engine, docker, clock, logs = self.make(health={"shop-db-1": ["starting", "starting", "healthy"]})
        started = engine.up(timeout=30)
        self.assertEqual(started, ["db", "web"])
        verbs = [(call[1], call[2] if len(call) > 2 else "") for call in docker.calls]
        self.assertEqual(verbs[0], ("network", "inspect"))
        self.assertEqual(verbs[1], ("network", "create"))
        self.assertEqual(verbs[2], ("volume", "inspect"))
        self.assertEqual(verbs[3], ("volume", "create"))
        run_calls = [call for call in docker.calls if call[1] == "run"]
        self.assertEqual([call[call.index("--name") + 1] for call in run_calls], ["shop-db-1", "shop-web-1"])
        health_polls = [call for call in docker.calls if call[1] == "inspect" and "Health" in call[3]]
        self.assertEqual(len(health_polls), 3)
        self.assertEqual(clock.t, 1.0)
        self.assertIn("web: db is healthy after 1.0s", logs)
        self.assertEqual(docker.networks, {"shop_default"})
        self.assertEqual(docker.volumes, {"shop_dbdata"})

    def test_up_fails_fast_on_unhealthy_and_on_timeout(self):
        engine, docker, _, _ = self.make(health={"shop-db-1": ["starting", "unhealthy"]})
        with self.assertRaises(main.ComposeError) as caught:
            engine.up(timeout=30)
        self.assertIn("db is unhealthy", str(caught.exception))
        self.assertIn("docker inspect -f '{{json .State.Health.Log}}' shop-db-1", str(caught.exception))
        self.assertNotIn("shop-web-1", docker.containers)
        engine, docker, clock, _ = self.make(health={"shop-db-1": ["starting"]})
        with self.assertRaises(main.ComposeError) as caught:
            engine.up(timeout=5)
        self.assertEqual(str(caught.exception), "timed out after 5s waiting for db to be healthy")
        self.assertGreaterEqual(clock.t, 5.0)

    def test_service_started_condition_and_recreate(self):
        text = "services:\n  db:\n    image: d\n  web:\n    image: w\n    depends_on: [db]\n"
        engine, docker, _, logs = self.make(text, name="demo")
        docker.networks.add("demo_default")
        docker.containers["demo-web-1"] = {"image": "old", "status": "exited"}
        engine.up()
        self.assertNotIn(["docker", "network", "create", "--label", "com.docker.compose.project=demo", "--label", "com.docker.compose.network=default", "demo_default"], docker.calls)
        self.assertIn(["docker", "rm", "-f", "demo-web-1"], docker.calls)
        self.assertIn("demo-web-1 recreated", logs)
        self.assertEqual(docker.containers["demo-web-1"]["image"], "w")
        started_polls = [call for call in docker.calls if call[1] == "inspect" and call[3] == "{{.State.Status}}"]
        self.assertEqual(len(started_polls), 1)

    def test_ps_parses_health_from_status(self):
        engine, docker, _, _ = self.make(health={"shop-db-1": ["healthy"]})
        engine.up()
        rows = engine.ps()
        self.assertEqual([(row.name, row.service, row.state, row.health) for row in rows], [("shop-db-1", "db", "running", "healthy"), ("shop-web-1", "web", "running", "-")])
        text = main.render_ps(rows)
        self.assertIn("NAME        SERVICE    STATE     HEALTH    STATUS", text)
        self.assertIn("shop-db-1   db         running   healthy   Up 3 seconds (healthy)", text)
        self.assertIn("shop-web-1  web        running   -         Up 3 seconds", text)
        self.assertEqual(main.render_ps([]), "no containers")

    def test_down_is_reverse_order_and_idempotent(self):
        engine, docker, _, _ = self.make(health={"shop-db-1": ["healthy"]})
        engine.up()
        removed = engine.down(volumes=True)
        self.assertEqual(removed, ["shop-web-1", "shop-db-1"])
        stops = [call[2] for call in docker.calls if call[1] == "stop"]
        self.assertEqual(stops, ["shop-web-1", "shop-db-1"])
        self.assertIn(["docker", "network", "rm", "shop_default"], docker.calls)
        self.assertIn(["docker", "volume", "rm", "shop_dbdata"], docker.calls)
        self.assertEqual((docker.containers, docker.networks, docker.volumes), ({}, set(), set()))
        self.assertEqual(engine.down(), [])

    def test_docker_failures_become_compose_errors_with_the_command(self):
        def broken(args):
            return main.CommandResult(125, "", "docker: Error response from daemon: pull access denied for shop/web")

        engine = main.Engine(project_from(TWO_SERVICES), broken)
        with self.assertRaises(main.ComposeError) as caught:
            engine.up()
        message = str(caught.exception)
        self.assertIn("command failed (125): docker network create", message)
        self.assertIn("pull access denied", message)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="minicompose-")
        self.path = os.path.join(self.dir, "compose.yaml")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(TWO_SERVICES)

    def run_cli(self, *argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_config_order_and_render_without_docker(self):
        code, out, _ = self.run_cli("-f", self.path, "config")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["name"], "shop")
        code, out, _ = self.run_cli("-f", self.path, "order")
        self.assertEqual((code, out), (0, "1. db\n2. web\n"))
        code, out, _ = self.run_cli("-f", self.path, "--json", "render")
        plan = json.loads(out)
        self.assertEqual([step[:3] for step in plan][:3], [["docker", "network", "create"], ["docker", "volume", "create"], ["docker", "run", "-d"]])
        code, out, _ = self.run_cli("-f", self.path, "up", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("docker run -d --name shop-db-1", out)
        self.assertIn("--health-cmd 'pg_isready -U postgres'", out)

    def test_project_name_override_and_default_from_directory(self):
        code, out, _ = self.run_cli("-f", self.path, "-p", "Staging Shop", "config")
        self.assertEqual((code, json.loads(out)["name"]), (0, "stagingshop"))
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("services:\n  web:\n    image: x\n")
        code, out, _ = self.run_cli("-f", self.path, "config")
        self.assertEqual(json.loads(out)["name"], main.re.sub(r"[^a-z0-9_-]", "", os.path.basename(self.dir).lower()))

    def test_errors_exit_2_with_file_and_line(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("services:\n  web:\n    image: x\n   ports: [1]\n")
        code, _, err = self.run_cli("-f", self.path, "config")
        self.assertEqual(code, 2)
        self.assertIn(f"minicompose: {self.path}:4: bad indentation", err)
        code, _, err = self.run_cli("-f", os.path.join(self.dir, "missing.yaml"), "order")
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("services:\n  web:\n    image: x\n    deploy:\n      replicas: 2\n")
        code, _, err = self.run_cli("-f", self.path, "order")
        self.assertEqual(code, 0)
        self.assertIn("services.web: ignoring keys deploy", err)


if __name__ == "__main__":
    unittest.main()
