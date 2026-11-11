---
name: catalog-hygiene
description: Review a Backstage-style service catalog for entities that fail validation, unregistered descriptors, broken and dropped references, orphans, components without a resolvable owner or on-call, scorecard levels and Kubernetes workloads nobody owns, and end with a report each team can act on
version: 1.0.0
phase: platform
lesson: platform/service-catalog
tags: [backstage, service-catalog, ownership, on-call, scorecards, platform-engineering, kubernetes]
---

# Catalog hygiene review

Use this monthly, after a reorganisation or team rename, before an on-call rotation changes, or when an incident showed the catalog was wrong about who owns something. Work from the descriptors in the repositories and from the running catalog, never from a spreadsheet. Fix descriptors in pull requests to the repositories that own them; never edit the catalog database.

Inputs: a checkout (or `gh repo clone` loop) of the organisation's repositories, the root Location file, the scorecard definition, `minicatalog.py` (or Backstage's `@backstage/catalog-model` checker from the lesson), and `kubectl` access to the clusters.

## 1. Inventory: is every descriptor reachable?

```bash
find repos -name 'catalog-info.yaml' -o -name 'catalog-info.yml' | sort > on-disk.txt
python3 minicatalog.py validate repos/catalog/all.yaml --json > validate.json; echo "exit $?"
python3 -c "import json; d=json.load(open('validate.json')); print('\n'.join(d['files']))" | sort > reachable.txt
comm -23 <(sed 's|^repos/||' on-disk.txt) reachable.txt     # descriptors no Location reaches
```

| What you see | Meaning | Next |
|---|---|---|
| a descriptor on disk but not reachable | the service is invisible in the portal | add its URL to a Location's `targets` or enable discovery for that path |
| `load_errors` with `location target does not exist` | a Location points at a moved or deleted file | fix the target or set `presence: optional` if it is genuinely optional |
| `YAML error on line N` | the file cannot be parsed, so none of its entities load | fix the YAML; check tabs, duplicate keys and unquoted `:` in values |

## 2. Validate with Backstage's own rules

```bash
python3 minicatalog.py validate repos/catalog/all.yaml
node backstage-check.js "$(jq -r .base validate.json)" $(jq -r '.files[]' validate.json) | jq -r '.documents[] | select(.accepted | not) | "\(.source)#\(.doc) \(.field): \(.error)"'
```

| Field in the finding | Usual cause | Fix |
|---|---|---|
| `metadata.name` | spaces, capitals with spaces, a number, or more than 63 characters | lowercase-with-dashes; keep the human name in `metadata.title` |
| `metadata.annotations.<key>` | unquoted number or boolean, or a key with spaces | quote the value; use `example.com/key` form |
| `metadata.tags.N` | uppercase or spaces in a tag | lowercase, dashes |
| `kind` | `component` instead of `Component`, or a kind the catalog does not know | fix the case; a custom kind needs a processor first |
| `spec.<field>` required | missing `owner`, `lifecycle`, `type`, API `definition`, Group `children`, User `memberOf` | add it; `children: []` is valid |
| `spec.dependsOn` reference | entry without a kind (`payments-db`) | `resource:payments-db` or `component:ledger` |
| an unknown root field | `owner:` at the top level instead of under `spec` | move it under `spec` or into an annotation |

A rejected entity is not in the catalog at all. Treat every rejection as a missing service.

## 3. References that point nowhere

```bash
python3 minicatalog.py lint repos/catalog/all.yaml --previous last-month-export.json --json > lint.json; echo "exit $?"
jq -r '.broken_references[] | "\(.ref) \(.field) -> \(.target)"' lint.json
jq -r '.dropped_relations[] | "\(.ref) \(.field): \(.message)"' lint.json
jq -r '.orphans[] | "\(.status) \(.ref): \(.message)"' lint.json
python3 minicatalog.py export repos/catalog/all.yaml -o this-month-export.json
```

| Finding | Meaning | Fix |
|---|---|---|
| broken `spec.owner` in a non-default namespace | the owner inherited the entity's namespace | `owner: group:default/<team>` |
| broken `spec.owner` after a reorganisation | the group was renamed or merged | update every descriptor that names the old group; search with `grep -rn 'owner: old-team'` |
| dropped `spec.owner` | the owner is a System, Component or other kind that cannot own | name the Group that is paged for it |
| broken `consumesApis` or `dependsOn` | the target was renamed, deleted or never registered | fix the reference or register the target; a dependency you cannot name is a risk to record |
| orphan | the descriptor dropped the entity or lost its Location | restore it, or unregister the entity on purpose |
| stale | the source could not be read this run | fix the location error from step 1 before deciding anything |
| conflict | two descriptors define the same reference | keep the one in the repository that owns the code |

## 4. Owners and on-call

```bash
python3 minicatalog.py gaps repos/catalog/all.yaml
python3 minicatalog.py owner <component> repos/catalog/all.yaml
```

Every production Component needs an owner that resolves to a Group and an on-call target on the component or its group (`pagerduty.com/service-id`, `pagerduty.com/integration-key`, `opsgenie.com/team` or `opsgenie.com/component-selector`). Send each team its own list from `gaps`; a component claimed by a group that does not exist goes to that group's parent or to the engineering manager who renamed it.

## 5. Scorecard

```bash
python3 minicatalog.py scorecard repos/catalog/all.yaml --rules scorecard.json
python3 minicatalog.py scorecard repos/catalog/all.yaml --rules scorecard.json --min-level silver; echo "exit $?"
```

| Situation | Verdict |
|---|---|
| a high score but a low level | a lower-level rule fails; fix that rule first, the score is misleading |
| many services fail the same rule | the golden-path template or the platform is missing something; fix it there once |
| template version rule fails | run the template update (cruft or copier style) instead of editing by hand |
| a rule nobody can pass | the rule is wrong or the tooling is missing; change the scorecard in a reviewed pull request |

## 6. Running workloads without an owner

```bash
kubectl get deployments,statefulsets,cronjobs -A -o json > workloads.json
python3 minicatalog.py k8s-owners repos/catalog/all.yaml --objects workloads.json
kubectl get deployments -A -l '!backstage.io/kubernetes-id'
```

| Status | Next |
|---|---|
| `unlabelled` | find the owner from the namespace, image or Git history; add the `backstage.io/kubernetes-id` label in its manifests |
| `no-component` | the label names a component that does not exist: register it or correct the label |
| `no-owner` | the component exists but its owner does not resolve: step 3 |
| not running | the annotation names a workload that is gone; remove the annotation or the component |

## 7. Report

State: the number of descriptors on disk, reachable and registered; rejected entities with file, field and fix; broken references, dropped relations, orphans and conflicts with the pull request that fixes each; per team, the components without a resolvable owner or on-call; the scorecard level counts per team compared with last month; unlabelled and ownerless workloads per cluster and namespace; and the date of the export used as the baseline next time. Never close the review with a production component that has no owner that resolves.
