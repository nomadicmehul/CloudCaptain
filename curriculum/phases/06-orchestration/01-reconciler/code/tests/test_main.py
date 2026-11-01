import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402


def widget(name, size=1, labels=None):
    return {"kind": "Widget", "metadata": {"name": name, "labels": labels or {}}, "spec": {"size": size}}


def pod(name, labels=None, image="x:1", owner=None):
    meta = {"name": name, "labels": labels or {"app": "rs"}}
    if owner:
        meta["ownerReferences"] = [owner]
    return {"kind": "Pod", "metadata": meta, "spec": {"containers": [{"name": "c", "image": image}]}}


def replica_set(name="rs", replicas=3, labels=None):
    labels = labels or {"app": "rs"}
    return {
        "apiVersion": "apps/v1",
        "kind": "ReplicaSet",
        "metadata": {"name": name, "namespace": "default", "labels": labels},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": labels},
            "template": {"metadata": {"labels": labels}, "spec": {"containers": [{"name": "c", "image": "x:1"}]}},
        },
    }


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


def ready_count(cluster, app):
    return len(cluster.kubectl.ready_pods(app))


class ApiServerTests(unittest.TestCase):
    def setUp(self):
        self.api = main.ApiServer()

    def test_create_assigns_identity_and_increasing_resource_versions(self):
        a = self.api.create(widget("a"))
        b = self.api.create(widget("b"))
        self.assertNotEqual(a["metadata"]["uid"], b["metadata"]["uid"])
        self.assertEqual(a["metadata"]["generation"], 1)
        self.assertLess(int(a["metadata"]["resourceVersion"]), int(b["metadata"]["resourceVersion"]))
        with self.assertRaises(main.AlreadyExists):
            self.api.create(widget("a"))

    def test_generate_name_is_deterministic_and_uses_the_safe_alphabet(self):
        first = self.api.create({"kind": "Widget", "metadata": {"generateName": "w-"}, "spec": {}})
        second = self.api.create({"kind": "Widget", "metadata": {"generateName": "w-"}, "spec": {}})
        suffix = first["metadata"]["name"][2:]
        self.assertEqual(len(suffix), 5)
        self.assertTrue(set(suffix) <= set(main.SAFE_ALPHABET))
        self.assertNotEqual(first["metadata"]["name"], second["metadata"]["name"])
        again = main.ApiServer().create({"kind": "Widget", "metadata": {"generateName": "w-"}, "spec": {}})
        self.assertEqual(again["metadata"]["name"], first["metadata"]["name"])

    def test_stale_resource_version_is_a_409_conflict(self):
        self.api.create(widget("a"))
        mine, theirs = self.api.get("Widget", "default", "a"), self.api.get("Widget", "default", "a")
        mine["spec"]["size"] = 2
        self.api.update(mine)
        theirs["spec"]["size"] = 3
        with self.assertRaises(main.Conflict) as caught:
            self.api.update(theirs)
        self.assertEqual(caught.exception.code, 409)
        self.assertIn("the object has been modified", caught.exception.message)
        self.assertEqual(self.api.get("Widget", "default", "a")["spec"]["size"], 2)

    def test_update_without_resource_version_overwrites_blindly(self):
        self.api.create(widget("a"))
        current = self.api.get("Widget", "default", "a")
        current["spec"]["size"] = 2
        self.api.update(current)
        blind = widget("a", size=9)
        blind["metadata"]["namespace"] = "default"
        self.api.update(blind)
        self.assertEqual(self.api.get("Widget", "default", "a")["spec"]["size"], 9)

    def test_generation_moves_only_with_spec_changes(self):
        self.api.create(widget("a"))
        obj = self.api.get("Widget", "default", "a")
        obj["metadata"]["labels"] = {"tier": "x"}
        obj = self.api.update(obj)
        self.assertEqual(obj["metadata"]["generation"], 1)
        obj["spec"]["size"] = 5
        self.assertEqual(self.api.update(obj)["metadata"]["generation"], 2)

    def test_identical_write_is_a_noop_with_no_event(self):
        self.api.create(widget("a"))
        revision = self.api.revision
        same = self.api.get("Widget", "default", "a")
        result = self.api.update(same)
        self.assertEqual(self.api.revision, revision)
        self.assertEqual(result["metadata"]["resourceVersion"], same["metadata"]["resourceVersion"])

    def test_main_update_ignores_status_and_status_update_ignores_spec(self):
        self.api.create(pod("p"))
        current = self.api.get("Pod", "default", "p")
        current["status"] = {"phase": "Running"}
        current["spec"]["containers"][0]["image"] = "x:2"
        after = self.api.update(current)
        self.assertEqual(after["status"], {})
        self.assertEqual(after["spec"]["containers"][0]["image"], "x:2")
        after["status"] = {"phase": "Running"}
        after["spec"]["containers"][0]["image"] = "x:3"
        after = self.api.update_status(after)
        self.assertEqual(after["status"], {"phase": "Running"})
        self.assertEqual(after["spec"]["containers"][0]["image"], "x:2")
        self.assertEqual(after["metadata"]["generation"], 2)

    def test_watch_replays_events_after_a_resource_version(self):
        self.api.create(widget("a"))
        _, revision = self.api.list("Widget")
        b = self.api.create(widget("b"))
        b["spec"]["size"] = 2
        self.api.update(b)
        self.api.delete("Widget", "default", "a")
        events, latest = self.api.events_since("Widget", int(revision))
        self.assertEqual([(e["type"], e["object"]["metadata"]["name"]) for e in events], [("ADDED", "b"), ("MODIFIED", "b"), ("DELETED", "a")])
        self.assertEqual(latest, self.api.revision)

    def test_watch_with_a_selector_turns_relabelling_into_added_and_deleted(self):
        self.api.create(widget("a", labels={"app": "web"}))
        revision = self.api.revision
        obj = self.api.get("Widget", "default", "a")
        obj["metadata"]["labels"] = {"app": "other"}
        self.api.update(obj)
        web, _ = self.api.events_since("Widget", revision, selector="app=web")
        other, _ = self.api.events_since("Widget", revision, selector="app=other")
        self.assertEqual([e["type"] for e in web], ["DELETED"])
        self.assertEqual([e["type"] for e in other], ["ADDED"])

    def test_compaction_makes_old_resource_versions_gone(self):
        self.api.create(widget("a"))
        old = self.api.revision
        self.api.create(widget("b"))
        self.api.compact()
        with self.assertRaises(main.Gone) as caught:
            self.api.events_since("Widget", old)
        self.assertEqual(caught.exception.code, 410)
        self.assertIn("too old resource version", caught.exception.message)
        events, _ = self.api.events_since("Widget", self.api.revision)
        self.assertEqual(events, [])

    def test_garbage_collection_cascades_and_waits_for_every_owner(self):
        owner_a = self.api.create(widget("a"))
        owner_b = self.api.create(widget("b"))
        child = widget("child")
        child["metadata"]["ownerReferences"] = [main.owner_reference(owner_a), main.owner_reference(owner_b)]
        child = self.api.create(child)
        grandchild = widget("grandchild")
        grandchild["metadata"]["ownerReferences"] = [main.owner_reference(child)]
        self.api.create(grandchild)
        self.api.delete("Widget", "default", "a")
        self.assertEqual(self.api.garbage_collect(), [])
        self.api.delete("Widget", "default", "b")
        removed = [name for _, _, name in self.api.garbage_collect()]
        self.assertEqual(removed, ["child", "grandchild"])
        self.assertEqual(self.api.list("Widget")[0], [])

    def test_orphan_delete_keeps_dependents_and_strips_the_reference(self):
        owner = self.api.create(widget("owner"))
        child = widget("child")
        child["metadata"]["ownerReferences"] = [main.owner_reference(owner)]
        self.api.create(child)
        self.api.delete("Widget", "default", "owner", orphan=True)
        self.assertEqual(self.api.garbage_collect(), [])
        self.assertEqual(self.api.get("Widget", "default", "child")["metadata"]["ownerReferences"], [])

    def test_selector_must_match_template_labels(self):
        broken = replica_set()
        broken["spec"]["template"]["metadata"]["labels"] = {"app": "different"}
        with self.assertRaises(main.Invalid) as caught:
            self.api.create(broken)
        self.assertEqual(caught.exception.code, 422)
        empty = replica_set()
        empty["spec"]["selector"] = {}
        with self.assertRaises(main.Invalid):
            self.api.create(empty)


class SelectorTests(unittest.TestCase):
    def test_parse_every_form(self):
        parsed = main.parse_selector("tier in (a, b),env!=prod,!canary,release,app==web,zone=eu")
        self.assertEqual(parsed, [
            main.Requirement("tier", "in", ("a", "b")),
            main.Requirement("env", "notin", ("prod",)),
            main.Requirement("canary", "!exists"),
            main.Requirement("release", "exists"),
            main.Requirement("app", "in", ("web",)),
            main.Requirement("zone", "in", ("eu",)),
        ])

    def test_negative_operators_match_objects_without_the_key(self):
        bare = {"app": "web"}
        self.assertTrue(main.selector_matches(main.parse_selector("env!=prod"), bare))
        self.assertTrue(main.selector_matches(main.parse_selector("env notin (prod,stage)"), bare))
        self.assertFalse(main.selector_matches(main.parse_selector("env in (prod)"), bare))
        self.assertFalse(main.selector_matches(main.parse_selector("env"), bare))
        self.assertTrue(main.selector_matches(main.parse_selector("!env"), bare))
        self.assertTrue(main.selector_matches([], bare))

    def test_requirements_are_anded(self):
        labels = {"app": "web", "env": "prod"}
        self.assertTrue(main.selector_matches(main.parse_selector("app=web,env=prod"), labels))
        self.assertFalse(main.selector_matches(main.parse_selector("app=web,env=dev"), labels))

    def test_selector_from_spec_reads_match_labels_and_expressions(self):
        spec = {"matchLabels": {"app": "web"}, "matchExpressions": [{"key": "tier", "operator": "In", "values": ["a", "b"]}, {"key": "canary", "operator": "DoesNotExist"}]}
        reqs = main.selector_from_spec(spec)
        self.assertTrue(main.selector_matches(reqs, {"app": "web", "tier": "b"}))
        self.assertFalse(main.selector_matches(reqs, {"app": "web", "tier": "b", "canary": "1"}))
        with self.assertRaises(main.Invalid):
            main.selector_from_spec({"matchExpressions": [{"key": "a", "operator": "Near"}]})

    def test_garbage_selectors_are_invalid(self):
        for text in ("app=web=x", "tier in a", "a b c"):
            with self.assertRaises(main.Invalid):
                main.parse_selector(text)


class HashTests(unittest.TestCase):
    TEMPLATE = {"metadata": {"labels": {"app": "w"}}, "spec": {"containers": [{"name": "c", "image": "a:1"}]}}

    def test_fnv1a_known_vectors(self):
        self.assertEqual(main.fnv1a_32(b""), 2166136261)
        self.assertEqual(main.fnv1a_32(b"a"), 0xE40C292C)
        self.assertEqual(main.fnv1a_32(b"foobar"), 0xBF9CF968)

    def test_template_hash_changes_with_the_template_but_not_the_hash_label(self):
        base = main.pod_template_hash(self.TEMPLATE)
        self.assertEqual(base, main.pod_template_hash(copy.deepcopy(self.TEMPLATE)))
        newer = copy.deepcopy(self.TEMPLATE)
        newer["spec"]["containers"][0]["image"] = "a:2"
        self.assertNotEqual(base, main.pod_template_hash(newer))
        labelled = copy.deepcopy(self.TEMPLATE)
        labelled["metadata"]["labels"][main.HASH_LABEL] = base
        self.assertEqual(base, main.pod_template_hash(labelled))
        self.assertTrue(set(base) <= set("456789bcdf") and len(base) <= 10)


class WorkQueueTests(unittest.TestCase):
    def test_a_key_added_many_times_is_handed_out_once(self):
        queue = main.WorkQueue()
        for _ in range(3):
            queue.add("default/a", 0)
        queue.add("default/b", 0)
        self.assertEqual(len(queue), 2)
        self.assertEqual(queue.get(0), "default/a")
        self.assertEqual(queue.get(0), "default/b")
        self.assertIsNone(queue.get(0))

    def test_backoff_doubles_caps_and_forget_resets(self):
        queue = main.WorkQueue(base_delay=1, max_delay=8)
        self.assertEqual([queue.add_rate_limited("k", 0) for _ in range(6)], [1, 2, 4, 8, 8, 8])
        self.assertEqual(queue.requeues("k"), 6)
        queue.forget("k")
        self.assertEqual(queue.add_rate_limited("k", 0), 1)

    def test_a_key_in_flight_is_not_handed_out_again(self):
        queue = main.WorkQueue()
        queue.add("a", 0)
        self.assertEqual(queue.get(0), "a")
        queue.add("a", 0)
        self.assertIsNone(queue.get(0))
        queue.done("a")
        self.assertEqual(queue.get(0), "a")

    def test_fresh_news_beats_a_pending_backoff(self):
        queue = main.WorkQueue()
        queue.add_after("k", 10, 0)
        self.assertIsNone(queue.get(5))
        queue.add("k", 5)
        self.assertEqual(queue.get(5), "k")

    def test_earliest_due_key_goes_first(self):
        queue = main.WorkQueue()
        queue.add_after("late", 3, 0)
        queue.add("early", 1)
        self.assertEqual(queue.get(5), "early")
        self.assertEqual(queue.get(5), "late")


class InformerTests(unittest.TestCase):
    def test_list_then_watch_then_relist_after_gone(self):
        api = main.ApiServer()
        seen = []
        informer = main.Informer(api, "Widget", lambda kind, obj: seen.append((kind, obj["metadata"]["name"])))
        api.create(widget("a"))
        informer.poll(0)
        api.create(widget("b"))
        informer.poll(1)
        self.assertEqual(seen, [("ADDED", "a"), ("ADDED", "b")])
        api.create(widget("c"))
        api.delete("Widget", "default", "a")
        api.compact()
        informer.poll(2)
        self.assertEqual(informer.lists, 2)
        self.assertIn("too old resource version", informer.last_gone)
        self.assertIn(("DELETED", "a"), seen)
        self.assertIn(("ADDED", "c"), seen)

    def test_resync_replays_the_cache_as_sync_events(self):
        api = main.ApiServer()
        seen = []
        informer = main.Informer(api, "Widget", lambda kind, obj: seen.append(kind), resync_period=5)
        api.create(widget("a"))
        informer.poll(1)
        informer.poll(5)
        self.assertEqual(seen, ["ADDED", "SYNC"])


class ReplicaSetTests(unittest.TestCase):
    def setUp(self):
        self.cluster = main.Cluster()

    def converge(self, name="rs", replicas=3):
        self.cluster.api.create(replica_set(name, replicas))
        self.cluster.run_until(lambda: ready_count(self.cluster, "rs") == replicas, what="ready pods")

    def test_pods_are_created_with_a_controller_owner_reference_and_template_labels(self):
        self.converge()
        rs = self.cluster.api.get("ReplicaSet", "default", "rs")
        pods = self.cluster.kubectl.pods("rs")
        self.assertEqual(len(pods), 3)
        for item in pods:
            ref = main.controller_of(item)
            self.assertEqual((ref["kind"], ref["name"], ref["uid"], ref["blockOwnerDeletion"]), ("ReplicaSet", "rs", rs["metadata"]["uid"], True))
            self.assertTrue(item["metadata"]["name"].startswith("rs-"))
        self.cluster.run(2)
        self.assertEqual(self.cluster.api.get("ReplicaSet", "default", "rs")["status"]["readyReplicas"], 3)

    def test_a_deleted_pod_is_replaced_without_being_told_why(self):
        self.converge()
        victim = main.names(self.cluster.kubectl.pods("rs"))[0]
        self.cluster.kubectl.delete_pod(victim)
        self.cluster.run_until(lambda: ready_count(self.cluster, "rs") == 3, what="replacement")
        self.assertNotIn(victim, main.names(self.cluster.kubectl.pods("rs")))

    def test_failed_pods_are_replaced_and_left_for_inspection(self):
        self.converge()
        sick = self.cluster.api.get("Pod", "default", main.names(self.cluster.kubectl.pods("rs"))[0])
        sick["status"] = main.pod_status("Failed", False)
        self.cluster.api.update_status(sick)
        self.cluster.run_until(lambda: ready_count(self.cluster, "rs") == 3, what="replacement for the failed pod")
        self.assertEqual(len(self.cluster.kubectl.pods("rs")), 4)

    def test_scale_down_deletes_unready_pods_before_ready_ones_and_newest_first(self):
        pending = {"metadata": {"name": "p", "creationTimestamp": 9}, "status": {}}
        unready = {"metadata": {"name": "u", "creationTimestamp": 1}, "status": main.pod_status("Running", False)}
        new_ready = {"metadata": {"name": "n", "creationTimestamp": 8}, "status": main.pod_status("Running", True)}
        old_ready = {"metadata": {"name": "o", "creationTimestamp": 2}, "status": main.pod_status("Running", True)}
        order = [p["metadata"]["name"] for p in sorted([old_ready, new_ready, unready, pending], key=main.deletion_rank)]
        self.assertEqual(order, ["p", "u", "n", "o"])

    def test_scaling_down_a_replica_set_keeps_the_oldest_pods(self):
        self.converge(replicas=3)
        oldest = main.names(self.cluster.kubectl.pods("rs"))
        rs = self.cluster.api.get("ReplicaSet", "default", "rs")
        rs["spec"]["replicas"] = 1
        self.cluster.api.update(rs)
        self.cluster.run_until(lambda: len(self.cluster.kubectl.pods("rs")) == 1, what="one pod")
        self.assertIn(main.names(self.cluster.kubectl.pods("rs"))[0], oldest)

    def test_orphans_with_matching_labels_are_adopted_not_duplicated(self):
        self.cluster.api.create(pod("stray", {"app": "rs"}))
        self.cluster.api.create(replica_set("rs", 1))
        self.cluster.run(3)
        pods = self.cluster.kubectl.pods("rs")
        self.assertEqual(len(pods), 1)
        self.assertEqual(main.controller_of(pods[0])["name"], "rs")

    def test_a_pod_relabelled_out_of_the_selector_is_released_and_replaced(self):
        self.converge()
        sick = self.cluster.api.get("Pod", "default", main.names(self.cluster.kubectl.pods("rs"))[0])
        sick["metadata"]["labels"] = {"app": "quarantine"}
        self.cluster.api.update(sick)
        self.cluster.run_until(lambda: ready_count(self.cluster, "rs") == 3, what="replacement")
        kept = self.cluster.api.get("Pod", "default", sick["metadata"]["name"])
        self.assertIsNone(main.controller_of(kept))
        self.assertEqual(len(self.cluster.kubectl.pods("rs")), 3)

    def test_pods_owned_by_another_controller_are_ignored(self):
        other = self.cluster.api.create(replica_set("other", 1, {"app": "other"}))
        foreign = pod("foreign", {"app": "rs"}, owner=main.owner_reference(other))
        self.cluster.api.create(foreign)
        self.cluster.api.create(replica_set("rs", 2))
        self.cluster.run(4)
        mine = [p for p in self.cluster.kubectl.pods("rs") if main.controller_of(p)["name"] == "rs"]
        self.assertEqual(len(mine), 2)

    def test_steady_state_writes_nothing_even_across_a_resync(self):
        self.converge()
        self.cluster.run(3)
        revision = self.cluster.api.revision
        self.cluster.run(25)
        self.assertEqual(self.cluster.api.revision, revision)
        self.assertGreater(self.cluster.replicasets.stats["reconciles"], 3)

    def test_a_controller_that_lost_every_event_heals_at_the_next_resync(self):
        summary = main.scenario_resync(main.Cluster())
        self.assertEqual(summary["pods_while_blind"], 2)
        self.assertEqual(summary["healed_at_tick"] - 2, summary["resync_period"])
        self.assertGreater(summary["healed_at_tick"], summary["deleted_at_tick"] + 3)

    def test_a_controller_that_was_down_past_compaction_relists_and_converges(self):
        summary = main.scenario_relist(main.Cluster())
        self.assertEqual(summary["relists"], 2)
        self.assertIn("too old resource version", summary["gone"])
        self.assertEqual(summary["ready_pods"], 3)


class DeploymentTests(unittest.TestCase):
    def test_replica_set_is_named_by_the_template_hash_and_stamped_with_a_revision(self):
        cluster = main.Cluster()
        main.serve(cluster, "web", "web:1.0", 3)
        deployment = cluster.api.get("Deployment", "default", "web")
        digest = main.pod_template_hash(deployment["spec"]["template"])
        rs = cluster.api.list("ReplicaSet")[0][0]
        self.assertEqual(rs["metadata"]["name"], f"web-{digest}")
        self.assertEqual(main.labels_of(rs)[main.HASH_LABEL], digest)
        self.assertEqual(rs["metadata"]["annotations"][main.REVISION_ANNOTATION], "1")
        self.assertEqual(deployment["metadata"]["annotations"][main.REVISION_ANNOTATION], "1")
        self.assertTrue(all(main.labels_of(p)[main.HASH_LABEL] == digest for p in cluster.kubectl.pods("web")))
        self.assertEqual(main.controller_of(rs)["uid"], deployment["metadata"]["uid"])

    def test_rolling_limits_round_surge_up_and_unavailable_down(self):
        cases = [(10, "25%", "25%", (3, 2)), (3, "25%", "25%", (1, 0)), (4, "25%", "25%", (1, 1)), (5, 0, 0, (0, 1)), (3, 1, 0, (1, 0)), (6, "50%", "0%", (3, 0))]
        for replicas, surge, unavailable, expected in cases:
            manifest = main.deployment_manifest("web", "i", replicas, max_surge=surge, max_unavailable=unavailable)
            self.assertEqual(main.rolling_limits(manifest), expected, (replicas, surge, unavailable))

    def test_rolling_update_never_exceeds_surge_or_dips_below_availability(self):
        summary = main.scenario_rolling(main.Cluster())
        self.assertEqual(summary["max_pods"], 4)
        self.assertEqual(summary["min_ready"], 3)
        self.assertEqual(summary["revisions"], [1, 2])
        self.assertEqual(summary["images"], ["web:2.0"])
        self.assertIn("successfully rolled out", summary["status"])

    def test_zero_surge_rolls_by_taking_one_pod_down_first(self):
        cluster = main.Cluster()
        main.serve(cluster, "web", "web:1.0", 3, max_surge=0, max_unavailable=1)
        samples = []
        cluster.observers.append(lambda: samples.append((len(cluster.kubectl.pods("web")), len(cluster.kubectl.ready_pods("web")))))
        cluster.kubectl.set_image("web", "web:2.0")
        cluster.run_until(lambda: cluster.kubectl.rollout_status("web")[0] and len(cluster.kubectl.pods("web")) == 3, what="rollout")
        self.assertEqual(max(total for total, _ in samples), 3)
        self.assertEqual(min(ready for _, ready in samples), 2)

    def test_broken_image_stalls_without_an_outage_and_undo_reuses_the_old_replica_set(self):
        summary = main.scenario_rollback(main.Cluster())
        self.assertIn("exceeded its progress deadline", summary["stuck_message"])
        self.assertEqual(summary["pods_while_stuck"], 4)
        self.assertEqual(summary["min_ready"], 3)
        self.assertEqual(summary["history_before_undo"], [1, 2])
        self.assertEqual(summary["history_after_undo"], [2, 3])
        self.assertTrue(summary["reused_first_replicaset"])
        self.assertEqual(summary["image_after"], "web:1.0")

    def test_revision_history_limit_prunes_old_replica_sets_and_undo_then_fails(self):
        cluster = main.Cluster()
        main.serve(cluster, "web", "web:1.0", 2, history=0)
        cluster.kubectl.set_image("web", "web:2.0")
        cluster.run_until(lambda: cluster.kubectl.rollout_status("web")[0] and len(cluster.kubectl.pods("web")) == 2, what="rollout")
        cluster.run(2)
        self.assertEqual(len(cluster.api.list("ReplicaSet")[0]), 1)
        with self.assertRaises(main.Invalid) as caught:
            cluster.kubectl.undo("web")
        self.assertIn("no rollout history found", caught.exception.message)

    def test_scaling_a_deployment_scales_the_same_replica_set(self):
        cluster = main.Cluster()
        main.serve(cluster, "web", "web:1.0", 3)
        before = [rs["metadata"]["name"] for rs in cluster.api.list("ReplicaSet")[0]]
        cluster.kubectl.scale("web", 5)
        cluster.run_until(lambda: len(cluster.kubectl.ready_pods("web")) == 5, what="5 pods")
        self.assertEqual([rs["metadata"]["name"] for rs in cluster.api.list("ReplicaSet")[0]], before)
        self.assertEqual(len(cluster.kubectl.history("web")), 1)

    def test_deleting_a_deployment_cascades_and_orphaning_leaves_the_dependents(self):
        summary = main.scenario_gc(main.Cluster())
        self.assertEqual(summary["web_objects_left"], 0)
        self.assertEqual(len(summary["db_objects_left"]), 2)
        owners = summary["db_owners"]
        self.assertIsNone(owners[0])
        self.assertEqual(owners[1]["kind"], "ReplicaSet")

    def test_a_write_that_loses_a_race_is_requeued_with_backoff_and_succeeds(self):
        cluster = main.Cluster()
        summary = main.scenario_conflict(cluster)
        self.assertIn("the object has been modified", summary["stale_update_error"])
        self.assertEqual(summary["retry_replicas"], 4)
        self.assertEqual(summary["controller_conflicts"], 1)
        self.assertEqual(summary["ready_pods"], 4)
        self.assertTrue(any(e["reason"] == "Conflict" and "requeue in 1 tick(s)" in e["message"] for e in cluster.events))

    def test_a_relabelled_pod_is_quarantined_and_keeps_running(self):
        summary = main.scenario_quarantine(main.Cluster())
        self.assertIsNone(summary["quarantined_owner"])
        self.assertTrue(summary["quarantined_still_running"])
        self.assertEqual(len(summary["serving_pods"]), 3)
        self.assertNotIn(summary["quarantined"], summary["serving_pods"])

    def test_rollout_status_follows_kubectl_wording(self):
        cluster = main.Cluster()
        main.serve(cluster, "web", "web:1.0", 3)
        cluster.run(3)
        self.assertEqual(cluster.kubectl.rollout_status("web"), (True, 'deployment "web" successfully rolled out'))
        cluster.kubectl.set_image("web", "web:2.0")
        self.assertEqual(cluster.kubectl.rollout_status("web"), (False, "Waiting for deployment spec update to be observed..."))
        seen = set()
        while True:
            done, message = cluster.kubectl.rollout_status("web")
            seen.add(message.split(":")[-1].strip() if not done else message)
            if done:
                break
            cluster.tick()
        self.assertTrue(any("new replicas have been updated" in m for m in seen))
        self.assertTrue(any("old replicas are pending termination" in m for m in seen))

    def test_many_events_collapse_into_few_reconciles(self):
        cluster = main.Cluster()
        main.scenario_self_heal(cluster)
        stats = cluster.stats()["replicaset-controller"]
        self.assertGreater(stats["events"], 2 * stats["reconciles"])


class CliTests(unittest.TestCase):
    def test_every_scenario_is_deterministic(self):
        for name, (function, _) in main.SCENARIOS.items():
            first, second = main.Cluster(), main.Cluster()
            self.assertEqual(function(first), function(second), name)
            self.assertEqual(first.events, second.events, name)

    def test_every_scenario_runs_through_the_cli_with_json(self):
        for name in main.SCENARIOS:
            code, out, _ = run_cli(["run", name, "--json"])
            self.assertEqual(code, 0, name)
            data = json.loads(out)
            self.assertEqual(data["scenario"], name)
            self.assertTrue(data["events"] and data["objects"] and data["summary"])

    def test_text_output_has_event_log_table_and_summary(self):
        code, out, _ = run_cli(["run", "self-heal"])
        self.assertEqual(code, 0)
        self.assertIn("SuccessfulCreate", out)
        self.assertIn("replicaset/web-", out)
        self.assertIn("same_replicaset: true", out)

    def test_list_and_usage_errors(self):
        code, out, _ = run_cli(["list", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual({row["name"] for row in json.loads(out)}, set(main.SCENARIOS))
        self.assertEqual(run_cli(["run", "nope"])[0], 2)
        self.assertEqual(run_cli([])[0], 2)
        self.assertEqual(run_cli(["bogus"])[0], 2)

    def test_select_matches_kubectl_json_lists(self):
        items = [
            {"metadata": {"name": "a", "labels": {"app": "web", "env": "prod"}}},
            {"metadata": {"name": "b", "labels": {"app": "web"}}},
            {"metadata": {"name": "c", "labels": {"app": "db", "env": "prod"}}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "list.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"kind": "List", "items": items}, handle)
            code, out, _ = run_cli(["select", "app=web,env!=dev", "--file", path, "--json"])
            self.assertEqual((code, json.loads(out)["matched"]), (0, ["a", "b"]))
            self.assertEqual(run_cli(["select", "env in (prod)", "--file", path])[1].split(), ["a", "c"])
            self.assertEqual(run_cli(["select", "not a selector", "--file", path])[0], 2)
            self.assertEqual(run_cli(["select", "app", "--file", os.path.join(tmp, "missing.json")])[0], 2)

    def test_run_until_raises_stalled_when_the_condition_never_holds(self):
        cluster = main.Cluster()
        with self.assertRaises(main.Stalled):
            cluster.run_until(lambda: False, limit=3, what="never")


if __name__ == "__main__":
    unittest.main()
