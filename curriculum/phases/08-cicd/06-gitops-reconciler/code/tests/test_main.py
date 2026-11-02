import contextlib
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

HAVE_GIT = shutil.which("git") is not None
RESOURCES = {
    "configmap": ("v1", "ConfigMap"), "service": ("v1", "Service"), "secret": ("v1", "Secret"),
    "deployment.apps": ("apps/v1", "Deployment"), "job.batch": ("batch/v1", "Job"),
}


def fixture_objects():
    return main.render(main.DirTree(main.FIXTURE_APP), "overlays/prod")


def by_name(objects):
    return {(o["kind"], o["metadata"]["name"]): o for o in objects}


def write_tree(root, files):
    for path, text in files.items():
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)
    return main.DirTree(root)


def deployment(name="web", image="nginx:1.29", replicas=2, namespace="prod", **extra):
    container = dict({"name": "web", "image": image, "ports": [{"containerPort": 8080}]}, **extra)
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": {"app": name}},
        "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}},
                 "template": {"metadata": {"labels": {"app": name}}, "spec": {"containers": [container]}}},
    }


def run_cli(argv, runner=main.run_command):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(argv, runner=runner, sleep=lambda _: None)
    return code, out.getvalue(), err.getvalue()


class KubectlOnFake:
    def __init__(self, cluster):
        self.cluster = cluster
        self.commands = []

    def ref(self, resource, name, args):
        api, kind = RESOURCES[resource]
        namespace = args[args.index("-n") + 1] if "-n" in args else ""
        return {"apiVersion": api, "kind": kind, "metadata": {"name": name, "namespace": namespace}}

    def __call__(self, argv, stdin):
        if argv[0] == "git":
            return main.run_command(argv, stdin)
        args = argv[1:]
        if args[0] == "--context":
            args = args[2:]
        self.commands.append(" ".join(args[:3]))
        verb = args[0]
        if verb == "get" and "-l" in args:
            items = self.cluster.list(args[args.index("-n") + 1], args[args.index("-l") + 1], args[1].split(","))
            return 0, json.dumps({"kind": "List", "items": items}), ""
        if verb == "get":
            self.cluster.tick()
            found = self.cluster.get(self.ref(args[1], args[2], args))
            return 0, json.dumps(found) if found else "", ""
        if verb == "apply":
            manager = next(a.split("=", 1)[1] for a in args if a.startswith("--field-manager="))
            return 0, json.dumps(self.cluster.apply(json.loads(stdin), manager, "--force-conflicts" in args)), ""
        if verb == "create":
            return 0, json.dumps(self.cluster.create(json.loads(stdin))), ""
        if verb == "delete":
            self.cluster.delete(self.ref(args[1], args[2], args))
            return 0, "", ""
        return 1, "", f"unexpected kubectl {verb}"


def git_app(test, mutate=None):
    root = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, root, True)
    origin, work = main.demo_repo(root)
    if mutate:
        mutate(work)
        main.commit_all(work, "fixture change")
    return root, origin, work


def reconciler_for(test, origin, root, prune=True, self_heal=False, cluster=None, path="overlays/prod"):
    cluster = cluster or main.FakeCluster()
    events = []
    app = main.App(name="shop", namespace="prod", repo=origin, path=path, prune=prune, self_heal=self_heal)
    source = main.Source(main.Git(origin, os.path.join(root, "cache-%d.git" % len(os.listdir(root)))), "main", path)
    reconciler = main.Reconciler(app, source, cluster, sleep=lambda _: cluster.tick(), log=events.append, health_timeout=12, poll_interval=1, now=lambda: "now")
    return reconciler, cluster, events


DEPLOY_REF = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "prod-web", "namespace": "prod"}}
LEGACY_REF = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "prod-legacy-flags", "namespace": "prod"}}


class YamlTests(unittest.TestCase):
    def test_scalars_follow_yaml_1_2_and_quoted_tags_stay_strings(self):
        doc = main.parse_yaml(
            "a: 1\nb: 3.10\nc: \"3.10\"\nd: yes\ne: true\nf: ~\ng: [x, 'y z', 2]\n"
            "h:\n  - name: web  # comment\n    image: 'pause:3.9'\n  - plain\ni: |\n  line one\n  line two\nj: {k: v}\n"
        )
        self.assertEqual(doc["a"], 1)
        self.assertEqual(doc["b"], 3.1)
        self.assertEqual(doc["c"], "3.10")
        self.assertEqual(doc["d"], "yes")
        self.assertIs(doc["e"], True)
        self.assertIsNone(doc["f"])
        self.assertEqual(doc["g"], ["x", "y z", 2])
        self.assertEqual(doc["h"], [{"name": "web", "image": "pause:3.9"}, "plain"])
        self.assertEqual(doc["i"], "line one\nline two\n")
        self.assertEqual(doc["j"], {"k": "v"})

    def test_documents_split_and_errors_name_the_line(self):
        self.assertEqual(main.parse_documents("---\na: 1\n---\n# only a comment\n---\nb: 2\n"), [{"a": 1}, {"b": 2}])
        for text, pattern in (("a:\n\tb: 1\n", "tabs"), ("a: &x 1\n", "anchors"), ("a: 1\na: 2\n", "line 2: duplicate key"), ("a: [1, 2\n", "flow collection"), ('a: "open\n', "unterminated")):
            with self.subTest(text=text):
                with self.assertRaisesRegex(main.YamlError, pattern):
                    main.parse_yaml(text)

    def test_emitted_yaml_round_trips_and_quotes_ambiguous_strings(self):
        objects = fixture_objects()
        self.assertEqual(main.parse_documents(main.to_yaml_documents(objects)), objects)
        text = main.to_yaml_documents([{"a": "-1", "b": "yes", "c": "3.10", "d": "true", "e": "", "f": "plain", "g": [], "h": {}}])
        for line in ('a: "-1"', 'b: "yes"', 'c: "3.10"', 'd: "true"', 'e: ""', "f: plain", "g: []", "h: {}"):
            self.assertIn(line, text)


class RenderTests(unittest.TestCase):
    def test_overlay_renders_prefix_labels_image_and_patch_like_kustomize(self):
        objects = by_name(fixture_objects())
        self.assertEqual(sorted(objects), [("ConfigMap", "prod-legacy-flags"), ("ConfigMap", "prod-web-config"), ("Deployment", "prod-web"), ("Job", "prod-migrate"), ("Service", "prod-web")])
        web = objects[("Deployment", "prod-web")]
        container = web["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], "registry.k8s.io/pause:3.9")
        self.assertEqual(web["spec"]["replicas"], 2)
        self.assertEqual(container["env"], [{"name": "MODE", "value": "prod"}])
        self.assertEqual(container["envFrom"], [{"configMapRef": {"name": "prod-web-config"}}])
        self.assertEqual(web["metadata"]["labels"], {"app": "web", "team": "shop"})
        self.assertEqual(web["spec"]["selector"], {"matchLabels": {"app": "web"}})
        self.assertEqual(web["spec"]["template"]["metadata"]["labels"], {"app": "web"})
        self.assertEqual(objects[("Service", "prod-web")]["spec"]["selector"], {"app": "web"})
        job = objects[("Job", "prod-migrate")]
        self.assertEqual(main.hook_types(job), ["PreSync"])
        self.assertEqual(main.wave_of(objects[("ConfigMap", "prod-web-config")]), -1)

    def test_plain_directory_reads_yaml_json_and_lists_in_file_order(self):
        with tempfile.TemporaryDirectory() as root:
            tree = write_tree(root, {
                "app/b.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: b1\n---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: b2\n",
                "app/a.json": json.dumps({"apiVersion": "v1", "kind": "List", "items": [{"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "a"}}]}),
                "app/notes.txt": "not a manifest",
                "app/nested/c.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n",
            })
            self.assertEqual([o["metadata"]["name"] for o in main.render(tree, "app")], ["a", "b1", "b2"])
            write_tree(root, {"bad/x.yaml": "kind: ConfigMap\n"})
            with self.assertRaisesRegex(main.InputError, "not a Kubernetes object"):
                main.render(tree, "bad")

    def test_strategic_merge_merges_keyed_lists_replaces_plain_lists_and_deletes(self):
        base = deployment(args=["--a"], env=[{"name": "A", "value": "1"}, {"name": "B", "value": "2"}])
        patch = {"spec": {"replicas": None, "template": {"spec": {"containers": [
            {"name": "web", "args": ["--b"], "ports": [{"containerPort": 8080, "name": "http"}], "env": [{"name": "B", "$patch": "delete"}, {"name": "C", "value": "3"}]},
            {"name": "sidecar", "image": "envoy"},
        ]}}}}
        merged = main.strategic_merge(base, patch, ())
        self.assertNotIn("replicas", merged["spec"])
        web, sidecar = merged["spec"]["template"]["spec"]["containers"]
        self.assertEqual((web["image"], web["args"]), ("nginx:1.29", ["--b"]))
        self.assertEqual(web["ports"], [{"containerPort": 8080, "name": "http"}])
        self.assertEqual(web["env"], [{"name": "A", "value": "1"}, {"name": "C", "value": "3"}])
        self.assertEqual(sidecar, {"name": "sidecar", "image": "envoy"})
        service = {"spec": {"ports": [{"port": 80, "targetPort": 8080}, {"port": 443}]}}
        merged = main.strategic_merge(service, {"spec": {"ports": [{"port": 443, "targetPort": 8443}]}}, ())
        self.assertEqual(merged["spec"]["ports"], [{"port": 80, "targetPort": 8080}, {"port": 443, "targetPort": 8443}])
        self.assertEqual(base["spec"]["replicas"], 2)

    def test_name_prefix_rewrites_references_only_to_objects_in_the_kustomization(self):
        spec = deployment(envFrom=[{"configMapRef": {"name": "conf"}}, {"secretRef": {"name": "external"}}],
                          env=[{"name": "K", "valueFrom": {"secretKeyRef": {"name": "creds", "key": "k"}}}])
        spec["spec"]["template"]["spec"].update({"serviceAccountName": "runner", "volumes": [{"name": "v", "configMap": {"name": "conf"}}, {"name": "s", "secret": {"secretName": "creds"}}]})
        objects = [spec, {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "conf"}},
                   {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "creds"}}, {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "runner"}}]
        main.rename(objects, "dev-", "-v2")
        pod = objects[0]["spec"]["template"]["spec"]
        self.assertEqual([o["metadata"]["name"] for o in objects], ["dev-web-v2", "dev-conf-v2", "dev-creds-v2", "dev-runner-v2"])
        self.assertEqual(pod["containers"][0]["envFrom"], [{"configMapRef": {"name": "dev-conf-v2"}}, {"secretRef": {"name": "external"}}])
        self.assertEqual(pod["containers"][0]["env"][0]["valueFrom"]["secretKeyRef"]["name"], "dev-creds-v2")
        self.assertEqual((pod["volumes"][0]["configMap"]["name"], pod["volumes"][1]["secret"]["secretName"], pod["serviceAccountName"]), ("dev-conf-v2", "dev-creds-v2", "dev-runner-v2"))

    def test_labels_reach_selectors_only_when_asked_and_common_labels_always_do(self):
        def build(kustomization):
            with tempfile.TemporaryDirectory() as root:
                tree = write_tree(root, {"d.yaml": json.dumps(deployment()), "s.yaml": json.dumps({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "web"}, "spec": {"selector": {"app": "web"}}}),
                                         "kustomization.yaml": "resources:\n  - d.yaml\n  - s.yaml\n" + kustomization})
                return by_name(main.render(tree, "."))
        plain = build("labels:\n  - pairs:\n      env: dev\n")
        self.assertEqual(plain[("Deployment", "web")]["spec"]["selector"]["matchLabels"], {"app": "web"})
        templates = build("labels:\n  - pairs:\n      env: dev\n    includeTemplates: true\n")
        self.assertEqual(templates[("Deployment", "web")]["spec"]["template"]["metadata"]["labels"], {"app": "web", "env": "dev"})
        self.assertEqual(templates[("Deployment", "web")]["spec"]["selector"]["matchLabels"], {"app": "web"})
        for kustomization in ("labels:\n  - pairs:\n      env: dev\n    includeSelectors: true\n", "commonLabels:\n  env: dev\n"):
            built = build(kustomization)
            self.assertEqual(built[("Deployment", "web")]["spec"]["selector"]["matchLabels"], {"app": "web", "env": "dev"})
            self.assertEqual(built[("Service", "web")]["spec"]["selector"], {"app": "web", "env": "dev"})
        with self.assertRaisesRegex(main.InputError, "quote values"):
            build("labels:\n  - pairs:\n      canary: true\n")

    def test_images_by_name_tag_and_digest(self):
        self.assertEqual(main.split_image("localhost:5000/team/app:1.2@sha256:ab"), ("localhost:5000/team/app", "1.2", "sha256:ab"))
        self.assertEqual(main.split_image("localhost:5000/team/app"), ("localhost:5000/team/app", "", ""))
        cases = (({"name": "nginx", "newTag": "1.30"}, "nginx:1.30"), ({"name": "nginx", "newName": "mirror.example/nginx"}, "mirror.example/nginx:1.29"),
                 ({"name": "nginx", "digest": "sha256:0f"}, "nginx@sha256:0f"), ({"name": "other", "newTag": "9"}, "nginx:1.29"))
        for entry, want in cases:
            objects = [deployment()]
            main.set_image(objects, entry, "k.yaml")
            self.assertEqual(objects[0]["spec"]["template"]["spec"]["containers"][0]["image"], want)
        with self.assertRaisesRegex(main.InputError, "quote it"):
            main.set_image([deployment()], {"name": "nginx", "newTag": 3.1}, "k.yaml")

    def test_kustomization_errors_are_specific(self):
        cm = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\n"
        cases = {
            "configMapGenerator": ({"kustomization.yaml": "configMapGenerator: []\n"}, "configMapGenerator not supported"),
            "missing": ({"kustomization.yaml": "resources:\n  - nope.yaml\n"}, "resource 'nope.yaml' not found"),
            "remote": ({"kustomization.yaml": "resources:\n  - https://github.com/x/y//base\n"}, "remote resource"),
            "escape": ({"kustomization.yaml": "resources:\n  - ../../outside\n"}, "leaves the repository"),
            "duplicate": ({"kustomization.yaml": "resources:\n  - a.yaml\n  - b.yaml\n", "a.yaml": cm, "b.yaml": cm}, "already registered id: ConfigMap/a"),
            "target": ({"kustomization.yaml": "resources:\n  - a.yaml\npatches:\n  - patch: |\n      kind: ConfigMap\n      metadata:\n        name: zzz\n", "a.yaml": cm}, "patch target ConfigMap/zzz not found"),
            "json6902": ({"kustomization.yaml": "resources:\n  - a.yaml\npatches:\n  - path: p.yaml\n", "a.yaml": cm, "p.yaml": "- op: remove\n  path: /data\n"}, "JSON 6902"),
            "bare dir": ({"kustomization.yaml": "resources:\n  - sub\n", "sub/a.yaml": cm}, "without a kustomization.yaml"),
            "cycle": ({"kustomization.yaml": "resources:\n  - sub\n", "sub/kustomization.yaml": "resources:\n  - ..\n"}, "resources cycle"),
        }
        for name, (files, pattern) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as root:
                tree = write_tree(os.path.join(root, "repo"), files)
                with self.assertRaisesRegex(main.InputError, pattern):
                    main.render(tree, ".")


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class GitTests(unittest.TestCase):
    def test_source_resolves_the_branch_caches_by_revision_and_reads_old_commits(self):
        root, origin, work = git_app(self)
        source = main.Source(main.Git(origin, os.path.join(root, "cache.git")), "main", "overlays/prod")
        first = source.resolve()
        self.assertRegex(first, r"^[0-9a-f]{40}$")
        self.assertIn(("ConfigMap", "prod-legacy-flags"), by_name(source.manifests(first)))
        second = main.bump_image(work)
        self.assertEqual(source.resolve(), second)
        image = lambda rev: by_name(source.manifests(rev))[("Deployment", "prod-web")]["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual(image(second), "registry.k8s.io/pause:3.10")
        calls = len(source.git.calls)
        self.assertEqual(image(first), "registry.k8s.io/pause:3.9")
        self.assertEqual(image(second), "registry.k8s.io/pause:3.10")
        self.assertEqual(len(source.git.calls), calls)
        self.assertEqual(source.commit(second)["message"], "bump web to pause 3.10")

    def test_missing_branch_and_unreachable_repository_are_git_errors(self):
        root, origin, _ = git_app(self)
        with self.assertRaisesRegex(main.GitError, "branch 'release' not found"):
            main.Git(origin, os.path.join(root, "c.git")).resolve("release")
        with self.assertRaisesRegex(main.GitError, "cannot read"):
            main.Git(os.path.join(root, "nowhere.git"), os.path.join(root, "d.git")).resolve("main")

    def test_render_cli_from_git_equals_render_from_the_working_tree(self):
        root, origin, work = git_app(self)
        code, from_git, err = run_cli(["render", "--repo", origin, "--path", "overlays/prod", "--cache", os.path.join(root, "c.git"), "--json"])
        self.assertEqual(code, 0, err)
        code, from_dir, _ = run_cli(["render", "--dir", work, "--path", "overlays/prod", "--json"])
        self.assertEqual(json.loads(from_git), json.loads(from_dir))
        self.assertEqual(len(json.loads(from_git)["items"]), 5)


class DiffTests(unittest.TestCase):
    def test_defaulted_fields_status_and_server_metadata_are_not_drift(self):
        cluster = main.FakeCluster()
        for obj in fixture_objects():
            obj["metadata"]["namespace"] = "prod"
            cluster.apply(obj)
            cluster.tick()
            live = cluster.get(obj)
            self.assertIn("managedFields", live["metadata"])
            self.assertEqual(main.diff(obj, live), [], main.describe(obj))
        live = cluster.get(DEPLOY_REF)
        self.assertEqual((live["spec"]["strategy"]["type"], live["status"]["observedGeneration"]), ("RollingUpdate", 1))

    def test_changed_and_missing_fields_have_readable_paths(self):
        desired = deployment(resources={"requests": {"cpu": "0.5", "memory": "1Gi"}})
        live = copy.deepcopy(desired)
        container = live["spec"]["template"]["spec"]["containers"][0]
        container.update(image="nginx:1.28", resources={"requests": {"cpu": "500m", "memory": "1024Mi"}})
        live["spec"]["replicas"] = 5
        del live["metadata"]["labels"]
        found = {d.path: d for d in main.diff(desired, live)}
        self.assertEqual(sorted(found), ["metadata.labels.app", "spec.replicas", "spec.template.spec.containers[name=web].image"])
        self.assertEqual(found["spec.replicas"].text(), "spec.replicas: Git 2, live 5")
        self.assertEqual(found["metadata.labels.app"].text(), 'metadata.labels.app: Git "web", missing live')
        container["resources"]["requests"]["cpu"] = "250m"
        self.assertIn("spec.template.spec.containers[name=web].resources.requests.cpu", {d.path for d in main.diff(desired, live)})
        self.assertTrue(main.same_value(("f:resources", "f:limits"), "1", 1))

    def test_fields_removed_from_git_are_found_through_managed_fields_and_dropped_by_apply(self):
        cluster = main.FakeCluster()
        old = deployment(env=[{"name": "MODE", "value": "prod"}, {"name": "LEGACY", "value": "1"}])
        old["metadata"]["annotations"] = {"team/owner": "shop"}
        cluster.apply(old)
        new = deployment(env=[{"name": "MODE", "value": "prod"}])
        found = {d.path: d.change for d in main.diff(new, cluster.get(new))}
        self.assertEqual(found, {"metadata.annotations": "removed", "spec.template.spec.containers[name=web].env[name=LEGACY]": "removed"})
        cluster.update(new, lambda o: o["metadata"]["labels"].__setitem__("by-hand", "yes"))
        self.assertEqual(main.diff(new, cluster.get(new), "someone-else"), [])
        cluster.apply(new)
        live = cluster.get(new)
        self.assertNotIn("annotations", live["metadata"])
        self.assertEqual(live["metadata"]["labels"]["by-hand"], "yes")
        self.assertEqual(main.diff(new, live), [])

    def test_real_managed_fields_with_defaulted_keys_and_atomic_fields_are_not_removals(self):
        desired = deployment()
        live = copy.deepcopy(desired)
        live["spec"]["template"]["spec"]["containers"][0]["ports"][0]["protocol"] = "TCP"
        live["metadata"]["managedFields"] = [{"manager": "minigitops", "operation": "Apply", "apiVersion": "apps/v1", "fieldsType": "FieldsV1", "fieldsV1": {
            "f:metadata": {"f:labels": {"f:app": {}}},
            "f:spec": {"f:replicas": {}, "f:selector": {}, "f:template": {"f:metadata": {"f:labels": {"f:app": {}}}, "f:spec": {"f:containers": {
                'k:{"name":"web"}': {".": {}, "f:image": {}, "f:name": {}, "f:ports": {'k:{"containerPort":8080,"protocol":"TCP"}': {".": {}, "f:containerPort": {}, "f:protocol": {}}}}}}}},
        }}, {"manager": "kube-controller-manager", "operation": "Update", "subresource": "status", "fieldsType": "FieldsV1", "fieldsV1": {"f:status": {"f:replicas": {}}}}]
        self.assertEqual(main.diff(desired, live), [])
        paths = set(main.leaf_paths(desired))
        self.assertEqual(set(main.paths_of_fields(main.fields_v1(paths))), paths)
        self.assertIn(("f:spec", "f:template", "f:spec", "f:containers", 'k:{"name":"web"}', "f:image"), paths)


class HealthTests(unittest.TestCase):
    def test_deployment_health_reads_generation_counts_and_conditions(self):
        def status(spec=None, meta=None, **status):
            obj = {"kind": "Deployment", "metadata": dict({"name": "web", "generation": 2}, **(meta or {})), "spec": dict({"replicas": 3}, **(spec or {})), "status": status}
            return main.assess(obj)
        self.assertEqual(status(spec={"paused": True}), ("Suspended", "Deployment is paused"))
        self.assertEqual(status(observedGeneration=1)[0], "Progressing")
        self.assertIn("observed deployment generation less than desired", status(observedGeneration=1)[1])
        self.assertEqual(status(observedGeneration=2, updatedReplicas=1)[1], "Waiting for rollout to finish: 1 out of 3 new replicas have been updated...")
        self.assertIn("1 old replicas are pending termination", status(observedGeneration=2, updatedReplicas=3, replicas=4)[1])
        self.assertIn("2 of 3 updated replicas are available", status(observedGeneration=2, updatedReplicas=3, replicas=3, availableReplicas=2)[1])
        self.assertEqual(status(observedGeneration=2, updatedReplicas=1, conditions=[{"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}])[0], "Degraded")
        self.assertEqual(status(observedGeneration=2, updatedReplicas=3, replicas=3, availableReplicas=3), ("Healthy", ""))

    def test_jobs_services_pods_claims_and_the_app_rollup(self):
        job = lambda **st: main.assess({"kind": "Job", "metadata": {"name": "j"}, "spec": {}, "status": st})[0]
        self.assertEqual(job(conditions=[{"type": "Complete", "status": "True"}]), "Healthy")
        self.assertEqual(job(conditions=[{"type": "Failed", "status": "True", "message": "BackoffLimitExceeded"}]), "Degraded")
        self.assertEqual(job(), "Progressing")
        self.assertEqual(main.assess({"kind": "Service", "metadata": {"name": "s"}, "spec": {"type": "ClusterIP"}})[0], "Healthy")
        self.assertEqual(main.assess({"kind": "Service", "metadata": {"name": "s"}, "spec": {"type": "LoadBalancer"}, "status": {}})[0], "Progressing")
        self.assertEqual(main.assess({"kind": "Service", "metadata": {"name": "s"}, "spec": {"type": "LoadBalancer"}, "status": {"loadBalancer": {"ingress": [{"ip": "1.2.3.4"}]}}})[0], "Healthy")
        self.assertEqual(main.assess({"kind": "PersistentVolumeClaim", "metadata": {"name": "c"}, "status": {"phase": "Pending"}})[0], "Progressing")
        crash = {"kind": "Pod", "metadata": {"name": "p"}, "status": {"phase": "Running", "containerStatuses": [{"name": "web", "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}
        self.assertEqual(main.assess(crash), ("Degraded", "container web: CrashLoopBackOff"))
        self.assertEqual(main.assess(None), ("Missing", "resource is missing"))
        self.assertEqual(main.worst(["Healthy", "Missing", "Progressing", "Suspended"]), "Missing")
        self.assertEqual(main.worst(["Progressing", "Degraded"]), "Degraded")
        self.assertEqual(main.worst([]), "Healthy")


class FakeClusterTests(unittest.TestCase):
    def test_server_side_apply_conflicts_with_another_manager_unless_forced(self):
        cluster = main.FakeCluster()
        cluster.apply(deployment())
        cluster.scale(deployment(), 5)
        with self.assertRaises(main.Conflict) as caught:
            cluster.apply(deployment(), force=False)
        self.assertIn('conflict with "kubectl": .spec.replicas', str(caught.exception))
        live = cluster.apply(deployment(), force=True)
        self.assertEqual(live["spec"]["replicas"], 2)
        owners = {m["manager"]: main.paths_of_fields(m["fieldsV1"]) for m in live["metadata"]["managedFields"]}
        self.assertIn(("f:spec", "f:replicas"), owners["minigitops"])
        self.assertNotIn("kubectl", owners)
        self.assertEqual(live["metadata"]["generation"], 3)


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class ReconcilerTests(unittest.TestCase):
    def test_first_sync_runs_presync_hooks_then_waves_in_kind_order(self):
        root, origin, _ = git_app(self)
        reconciler, cluster, _ = reconciler_for(self, origin, root)
        status = reconciler.reconcile()
        steps = [(r["step"], r["kind"] + "/" + r["name"], r["action"]) for r in reconciler.operation["resources"]]
        self.assertEqual(steps, [
            ("PreSync", "Job/prod-migrate", "hook created"), ("PreSync", "Job/prod-migrate", "hook Healthy"),
            ("wave -1", "ConfigMap/prod-web-config", "created"), ("wave 0", "ConfigMap/prod-legacy-flags", "created"),
            ("wave 0", "Service/prod-web", "created"), ("wave 0", "Deployment/prod-web", "created"),
        ])
        self.assertEqual((status["sync"]["status"], status["health"]["status"], reconciler.operation["phase"]), ("Synced", "Healthy", "Succeeded"))
        self.assertEqual(cluster.get(DEPLOY_REF)["metadata"]["labels"][main.TRACKING_LABEL], "shop")
        self.assertEqual(status["hooks"], ["PreSync Job/prod-migrate"])

    def test_a_watcher_that_starts_on_a_synced_app_calls_the_next_commit_a_new_revision(self):
        root, origin, work = git_app(self)
        first, cluster, _ = reconciler_for(self, origin, root)
        first.reconcile()
        restarted, _, _ = reconciler_for(self, origin, root, cluster=cluster)
        restarted.reconcile()
        self.assertIsNone(restarted.operation)
        revision = main.bump_image(work)
        status = restarted.reconcile()
        self.assertEqual((status["revision"], status["sync"]["status"], restarted.operation["reason"]), (revision, "Synced", "new revision"))

    def test_a_new_commit_is_picked_up_and_only_what_changed_is_applied(self):
        root, origin, work = git_app(self)
        reconciler, cluster, events = reconciler_for(self, origin, root)
        reconciler.reconcile()
        self.assertIsNone(reconciler.decide(reconciler.compare(reconciler.source.resolve())))
        revision = main.bump_image(work)
        cluster.applies.clear()
        status = reconciler.reconcile()
        self.assertEqual((status["revision"], status["sync"]["status"], reconciler.operation["reason"]), (revision, "Synced", "new revision"))
        self.assertEqual(cluster.applies, ["Deployment/prod-web"])
        self.assertTrue(any("containers[name=web].image" in e and "pause:3.10" in e for e in events), events)
        self.assertEqual(cluster.get(DEPLOY_REF)["spec"]["template"]["spec"]["containers"][0]["image"], "registry.k8s.io/pause:3.10")

    def test_prune_needs_permission_and_honours_prune_false(self):
        root, origin, work = git_app(self)
        reconciler, cluster, _ = reconciler_for(self, origin, root, prune=False)
        reconciler.reconcile()
        main.drop_legacy_flags(work)
        status = reconciler.reconcile()
        self.assertEqual(status["sync"]["status"], "OutOfSync")
        self.assertIn("need pruning; automated prune is off", status["note"])
        self.assertEqual([r["name"] for r in status["resources"] if r["requiresPruning"]], ["prod-legacy-flags"])
        self.assertIsNotNone(cluster.get(LEGACY_REF))
        reconciler.app.prune = True
        self.assertEqual(reconciler.reconcile()["sync"]["status"], "Synced")
        self.assertIsNone(cluster.get(LEGACY_REF))

        def keep(work_dir):
            path = os.path.join(work_dir, "base", "legacy-flags.yaml")
            main.replace_in_file(path, "  name: legacy-flags\n", "  name: legacy-flags\n  annotations:\n    argocd.argoproj.io/sync-options: Prune=false\n")
        root, origin, work = git_app(self, keep)
        reconciler, cluster, _ = reconciler_for(self, origin, root, prune=True)
        reconciler.reconcile()
        main.drop_legacy_flags(work)
        status = reconciler.reconcile()
        self.assertIn(("prune", "skipped (Prune=false)"), [(r["step"], r["action"]) for r in reconciler.operation["resources"]])
        self.assertIsNotNone(cluster.get(LEGACY_REF))
        self.assertEqual(status["sync"]["status"], "OutOfSync")

    def test_drift_is_reported_without_self_heal_and_reverted_with_it(self):
        root, origin, _ = git_app(self)
        reconciler, cluster, events = reconciler_for(self, origin, root, self_heal=False)
        reconciler.reconcile()
        cluster.scale(DEPLOY_REF, 5)
        status = reconciler.reconcile()
        self.assertEqual((status["sync"]["status"], cluster.get(DEPLOY_REF)["spec"]["replicas"], len(reconciler.history)), ("OutOfSync", 5, 1))
        self.assertIn("self-heal is off", status["note"])
        drift = [d for r in status["resources"] for d in r["diff"]]
        self.assertEqual(drift, [{"path": "spec.replicas", "change": "changed", "desired": 2, "live": 5}])
        reconciler.app.self_heal = True
        status = reconciler.reconcile()
        self.assertEqual((status["sync"]["status"], cluster.get(DEPLOY_REF)["spec"]["replicas"]), ("Synced", 2))
        self.assertEqual([h["reason"] for h in reconciler.history], ["initial sync", "self-heal"])

    def test_a_failed_presync_hook_stops_the_sync_runs_syncfail_and_is_not_retried(self):
        def break_hook(work):
            main.replace_in_file(os.path.join(work, "base", "migrate-job.yaml"), "image: alpine:3.22", "image: alpine:broken")
            with open(os.path.join(work, "base", "notify.yaml"), "w") as handle:
                handle.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: notify\n  annotations:\n    argocd.argoproj.io/hook: SyncFail\n"
                             "spec:\n  template:\n    spec:\n      restartPolicy: Never\n      containers:\n        - name: n\n          image: alpine:3.22\n")
            main.replace_in_file(os.path.join(work, "base", "kustomization.yaml"), "  - legacy-flags.yaml\n", "  - legacy-flags.yaml\n  - notify.yaml\n")
        root, origin, _ = git_app(self, break_hook)
        reconciler, cluster, _ = reconciler_for(self, origin, root)
        status = reconciler.reconcile()
        self.assertEqual(reconciler.operation["phase"], "Failed")
        self.assertIn("PreSync hooks: Job/prod-migrate is Degraded", reconciler.operation["message"])
        self.assertIsNone(cluster.get(DEPLOY_REF))
        self.assertIn(("SyncFail", "Job/prod-notify", "hook created"), [(r["step"], r["kind"] + "/" + r["name"], r["action"]) for r in reconciler.operation["resources"]])
        self.assertEqual(status["sync"]["status"], "OutOfSync")
        reconciler.reconcile()
        self.assertEqual(len(reconciler.history), 1)

    def test_a_degraded_wave_stops_later_waves(self):
        def break_web(work):
            main.replace_in_file(os.path.join(work, "overlays", "prod", "kustomization.yaml"), 'newTag: "3.9"', 'newTag: "broken"')
            main.replace_in_file(os.path.join(work, "base", "legacy-flags.yaml"), "  name: legacy-flags\n", "  name: legacy-flags\n  annotations:\n    argocd.argoproj.io/sync-wave: \"1\"\n")
        root, origin, _ = git_app(self, break_web)
        reconciler, cluster, _ = reconciler_for(self, origin, root)
        status = reconciler.reconcile()
        self.assertEqual(reconciler.operation["phase"], "Failed")
        self.assertIn("wave 0: Deployment/prod-web is Degraded: Deployment 'prod-web' exceeded its progress deadline", reconciler.operation["message"])
        self.assertIsNone(cluster.get(LEGACY_REF))
        self.assertEqual(status["health"]["status"], "Degraded")

    def test_hook_delete_policies(self):
        root, origin, _ = git_app(self)
        reconciler, cluster, _ = reconciler_for(self, origin, root, self_heal=True)
        job_ref = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "prod-migrate", "namespace": "prod"}}
        reconciler.reconcile()
        first = cluster.get(job_ref)["metadata"]["uid"]
        cluster.scale(DEPLOY_REF, 3)
        reconciler.reconcile()
        self.assertNotEqual(cluster.get(job_ref)["metadata"]["uid"], first)

        def succeed_policy(work):
            main.replace_in_file(os.path.join(work, "base", "migrate-job.yaml"), "hook-delete-policy: BeforeHookCreation", "hook-delete-policy: HookSucceeded")
        root, origin, _ = git_app(self, succeed_policy)
        reconciler, cluster, _ = reconciler_for(self, origin, root)
        reconciler.reconcile()
        self.assertEqual(reconciler.operation["phase"], "Succeeded")
        self.assertIsNone(cluster.get(job_ref))


class KubectlTests(unittest.TestCase):
    def test_kubectl_arguments_for_get_list_apply_create_and_delete(self):
        calls = []

        def runner(argv, stdin):
            calls.append((argv, stdin))
            return 0, '{"kind": "List", "items": []}' if "-l" in argv else ("" if argv[3] == "get" else "{}"), ""
        kubectl = main.Kubectl("kind-cc-smoke", runner)
        obj = deployment(namespace="shop")
        self.assertIsNone(kubectl.get(obj))
        kubectl.list("shop", "app.kubernetes.io/instance=web", ["configmap", "deployment.apps"])
        kubectl.apply(obj)
        kubectl.delete(obj, wait=True)
        kubectl.delete({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "shop"}})
        argv = [c[0] for c in calls]
        self.assertEqual(argv[0], ["kubectl", "--context", "kind-cc-smoke", "get", "deployment.apps", "web", "-n", "shop", "-o", "json", "--show-managed-fields", "--ignore-not-found"])
        self.assertEqual(argv[1][3:], ["get", "configmap,deployment.apps", "-n", "shop", "-l", "app.kubernetes.io/instance=web", "-o", "json", "--show-managed-fields"])
        self.assertEqual(argv[2][3:], ["apply", "--server-side", "--field-manager=minigitops", "--force-conflicts", "-o", "json", "-f", "-"])
        self.assertEqual(json.loads(calls[2][1]), obj)
        self.assertEqual(argv[3][3:], ["delete", "deployment.apps", "web", "-n", "shop", "--ignore-not-found", "--cascade=background", "--wait=true", "--timeout=120s"])
        self.assertEqual(argv[4][3:6], ["delete", "namespace", "shop"])
        self.assertNotIn("-n", argv[4])

    def test_kubectl_errors_carry_stderr_and_apply_conflicts_are_typed(self):
        kubectl = main.Kubectl(None, lambda argv, stdin: (1, "", 'error: Apply failed with 1 conflict: conflict with "kubectl": .spec.replicas'))
        with self.assertRaisesRegex(main.Conflict, "conflict with \"kubectl\""):
            kubectl.apply(deployment(), force=False)
        kubectl = main.Kubectl(None, lambda argv, stdin: (1, "", 'Error from server (Forbidden): deployments.apps is forbidden'))
        with self.assertRaisesRegex(main.ClusterError, "Forbidden"):
            kubectl.get(deployment())


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class CliTests(unittest.TestCase):
    def test_render_dir_prints_yaml_and_bad_input_exits_2(self):
        code, out, _ = run_cli(["render", "--dir", main.FIXTURE_APP, "--path", "overlays/prod"])
        self.assertEqual(code, 0)
        self.assertIn("name: prod-web\n", out)
        self.assertEqual(out.count("---\n"), 4)
        self.assertEqual(run_cli(["render", "--dir", "/nonexistent-minigitops"])[0], 2)
        self.assertEqual(run_cli(["render", "--dir", main.FIXTURE_APP, "--path", "../.."])[0], 2)
        code, _, err = run_cli(["render", "--dir", main.FIXTURE_APP, "--path", "base/service.yaml"])
        self.assertEqual((code, err.strip()), (2, "minigitops: path base/service.yaml is a file; the application path is a directory"))
        self.assertEqual(run_cli(["frobnicate"])[0], 2)
        self.assertEqual(run_cli(["status", "--repo", "x"])[0], 2)

    def test_status_sync_and_watch_through_kubectl(self):
        root, origin, _ = git_app(self)
        cluster = main.FakeCluster()
        runner = KubectlOnFake(cluster)
        common = ["--repo", origin, "--path", "overlays/prod", "--app", "shop", "--namespace", "prod", "--cache", os.path.join(root, "cli.git"), "--context", "kind-cc-smoke"]
        code, out, _ = run_cli(["status"] + common + ["--json"], runner)
        status = json.loads(out)
        self.assertEqual((code, status["sync"]["status"], status["health"]["status"]), (1, "OutOfSync", "Missing"))
        code, out, err = run_cli(["sync"] + common + ["--prune", "--json"], runner)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["operation"]["phase"], "Succeeded")
        code, out, _ = run_cli(["status"] + common, runner)
        self.assertEqual(code, 0)
        self.assertIn("Sync:      Synced", out)
        cluster.scale(DEPLOY_REF, 4)
        code, out, _ = run_cli(["status"] + common, runner)
        self.assertEqual(code, 1)
        self.assertIn("spec.replicas: Git 2, live 4", out)
        status_file = os.path.join(root, "status.json")
        watch = ["watch"] + common + ["--iterations", "1", "--status-file", status_file]
        code, out, _ = run_cli(watch, runner)
        self.assertEqual(code, 0)
        self.assertIn("syncing (initial sync)", out)
        self.assertEqual(cluster.get(DEPLOY_REF)["spec"]["replicas"], 2)
        cluster.scale(DEPLOY_REF, 6)
        code, out, _ = run_cli(watch, runner)
        self.assertIn("self-heal is off", out)
        self.assertEqual(cluster.get(DEPLOY_REF)["spec"]["replicas"], 6)
        code, out, _ = run_cli(watch + ["--self-heal"], runner)
        self.assertIn("syncing (self-heal)", out)
        with open(status_file) as handle:
            saved = json.load(handle)
        self.assertEqual((saved["sync"]["status"], [h["reason"] for h in saved["history"]]), ("Synced", ["initial sync", "self-heal"]))
        self.assertEqual(cluster.get(DEPLOY_REF)["spec"]["replicas"], 2)
        self.assertTrue(any(c.startswith("apply --server-side --field-manager=minigitops") for c in runner.commands))

    def test_demo_walks_the_whole_loop(self):
        result = main.demo()
        steps = result["steps"]
        self.assertEqual([s["image"] for s in steps], ["registry.k8s.io/pause:3.9", "registry.k8s.io/pause:3.10", "registry.k8s.io/pause:3.10", "registry.k8s.io/pause:3.10"])
        self.assertEqual([s["legacy_flags"] for s in steps], [True, True, False, False])
        self.assertEqual([s["operation"] for s in steps], ["initial sync", "new revision", "new revision", "self-heal"])
        self.assertTrue(all(s["sync"] == "Synced" and s["health"] == "Healthy" and s["replicas"] == 2 for s in steps))
        self.assertIn(("kubectl", "Update", ["spec.replicas"]), result["managers_after_scale"])
        self.assertEqual(result["managers"], [("minigitops", "Apply", ["spec.replicas"])])


if __name__ == "__main__":
    unittest.main()
