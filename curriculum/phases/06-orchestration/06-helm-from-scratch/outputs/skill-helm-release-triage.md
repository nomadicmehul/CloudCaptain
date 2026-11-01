---
name: helm-release-triage
description: Diagnose a Helm release that is stuck, failed, lost its values on upgrade, or does not match the cluster, by reading the release ledger (history, release Secrets, stored manifest and values) before changing anything
version: 1.0.0
phase: orchestration
lesson: orchestration/helm-from-scratch
tags: [helm, kubernetes, releases, rollback, upgrade, values, debugging, incident]
---

# Helm release triage

Use this when `helm upgrade` or `helm rollback` fails, when a release shows `pending-*` or `failed`, when an upgrade "lost" configuration, when objects disappeared after an upgrade, or when what runs in the cluster does not match what Helm says it deployed. Read first, change last: every step up to 5 is read-only.

## 1. What Helm thinks happened

```bash
REL=<release>; NS=<namespace>
helm list -n $NS --all                           # --all includes pending, failed and uninstalled releases
helm status $REL -n $NS                          # status and description of the newest record
helm history $REL -n $NS --max 20                # every revision: status, chart, app version, description
helm history $REL -n $NS -o json > history.json  # keep a copy before you change anything
```

Write down: the newest revision and its status, the newest `deployed` revision, and any `failed` or `pending-*` rows with their descriptions. The description of a failed revision holds the API server's error (`Upgrade "x" failed: ...`).

## 2. Read the ledger directly

```bash
kubectl get secrets -n $NS -l owner=helm,name=$REL --show-labels     # one Secret per revision: sh.helm.release.v1.$REL.vN
kubectl get secret sh.helm.release.v1.$REL.v<N> -n $NS -o jsonpath='{.data.release}' \
  | base64 -d | base64 -d | gunzip > rev<N>.json                    # Helm's base64 of gzipped JSON, inside the Secret API's base64
python3 -c 'import json; r=json.load(open("rev<N>.json")); print(r["version"], r["info"]["status"], r["info"].get("description")); print(sorted(r.get("config") or {}))'
```

If `HELM_DRIVER` is set to `configmap` or `sql`, the records are ConfigMaps or table rows instead; the record format is the same. A Secret whose labels say one status and whose decoded record says another means someone edited labels by hand.

## 3. Values: what was passed, what was computed

```bash
helm get values $REL -n $NS --revision <N>       # user-supplied values (the record's config) for that revision
helm get values $REL -n $NS --revision <N> --all # computed: chart defaults under the user values
diff <(helm get values $REL -n $NS --revision <GOOD> -o json) <(helm get values $REL -n $NS --revision <BAD> -o json)
```

An upgrade that passed any `-f` or `--set` starts from the chart defaults: values from earlier `-f` files are gone unless the same files were passed again or `--reuse-values` / `--reset-then-reuse-values` was used. A `--set list[1].x=y` replaces the whole default list. A value that changed type (`"on"` became `true`, `0755` became `493`, a large number became `1e+06`) is a quoting problem in a values file.

## 4. Stored state against live state

```bash
helm get manifest $REL -n $NS > stored.yaml              # what the newest record says was applied
kubectl diff -n $NS -f stored.yaml                       # drift: what is different in the cluster now (exit 1 means differences)
helm template $REL <chart> -n $NS -f <values files> > next.yaml
diff stored.yaml next.yaml                               # what the next upgrade would change
```

Objects in `stored.yaml` that are missing from `next.yaml` will be deleted by the next upgrade. Fields changed by hand in the cluster show up in `kubectl diff`; with server-side apply (Helm 4's default) a hand edit by another field manager can also cause a conflict on the next upgrade.

## 5. Decide

| Symptom | Cause | Fix |
|---|---|---|
| `another operation (install/upgrade/rollback) is in progress`; newest record `pending-upgrade` or `pending-install` | the client died between writing the pending record and finishing; the record is a lock | `helm rollback $REL <last deployed revision> -n $NS` (rollback does not check the lock). For a first install that never finished: `helm uninstall $REL -n $NS` and install again |
| newest record `failed`, older one `deployed` | the API server rejected the new manifest; the cluster still runs the deployed revision (except objects created before the failure) | read the failed description, fix the chart or values, upgrade again; add `--rollback-on-failure` in CI |
| `field is immutable` on upgrade | the chart changed an immutable field: a Deployment's `spec.selector`, a Service's `clusterIP`, a Job's template | revert the field, or delete and recreate that object deliberately in a maintenance window; never change selector labels in a chart |
| configuration "lost" after an upgrade | the upgrade passed some values, so earlier `-f` values were dropped | upgrade again with every `-f` file, or `--reset-then-reuse-values`; make the pipeline pass the full set every time |
| rollback "did not fix it" | rollback replays the old revision's stored manifest and values; changes outside the chart (a ConfigMap edited by hand, a Secret rotated, a CRD) are not part of it | compare `helm get manifest --revision <target>` with the live objects; fix the outside change |
| objects disappeared after an upgrade | they were in the old manifest and not in the new one (a renamed resource, a disabled `if`) | restore the template or rename back; for data-bearing objects add `helm.sh/resource-policy: keep` |
| `invalid ownership metadata` / `exists and cannot be imported into the current release` | an object with the same name exists without Helm's `meta.helm.sh/release-name` annotations | adopt it with `--take-ownership` (or add the label and annotations), or rename it in the chart |
| `cannot reuse a name that is still in use` | `helm install` found records for that name; any record blocks it | `helm upgrade --install`; `helm install --replace` only when the newest record is `uninstalled` or `failed` |
| `has no deployed releases` | no records for that name in this namespace (check `-n`), or the newest record is `uninstalled` | `helm upgrade --install`; an all-`failed` history still upgrades, from the newest failed record |
| history shows only the last 10 revisions | `--history-max` (default 10) pruned the rest | the pruned revisions cannot be rolled back to; keep values files in Git |

## 6. Act, then verify

```bash
helm rollback $REL <N> -n $NS --wait --timeout 5m        # or the upgrade from the table
helm history $REL -n $NS --max 5                         # a new deployed revision, the previous one superseded
kubectl rollout status deployment -n $NS -l app.kubernetes.io/instance=$REL --timeout=300s
helm get manifest $REL -n $NS | kubectl diff -n $NS -f - # empty diff: the cluster matches the record
```

Only if a rollback is impossible (the target revision was pruned, or the record is undecodable) delete the single pending Secret `sh.helm.release.v1.$REL.v<pending>` after saving it with `kubectl get secret ... -o yaml > saved.yaml`. Never delete all release Secrets: Helm then forgets the release while its objects keep running, and the next install fails on ownership.

## 7. Report

State: the release, namespace and chart version; the revision table before and after (number, status, description); the root cause in one sentence from the decision table; the values difference if values were involved; the exact command you ran; the drift check result after the fix; and the change that prevents a repeat (pass all values files, quote typed values, `--rollback-on-failure`, keep selectors stable).
