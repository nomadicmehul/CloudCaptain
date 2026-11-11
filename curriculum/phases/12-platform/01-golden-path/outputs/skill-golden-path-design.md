---
name: golden-path-design
description: Design, test, release and roll out a golden-path service template (copier, cookiecutter with cruft, or a Backstage Software Template) so that it makes the boring decisions once, validates the few it delegates, and can update every service it generated with a three-way merge
version: 1.0.0
phase: platform
lesson: platform/golden-path
tags: [platform-engineering, golden-path, copier, cruft, cookiecutter, backstage, scaffolder, templates, three-way-merge]
---

# Golden-path template design and rollout

Use this when a platform team is creating a service template, adding a question or a file to one, releasing a new version, or wondering why teams are not using it. The output is a template that is cheaper than copying a repository, a test matrix that proves it renders valid services, and a release that reaches existing services as reviewable updates.

Do not make the template mandatory to fix low adoption; find out why the path costs more than the alternative. Do not push template changes straight to service default branches; send them as pull requests. Do not ship a template whose output you have not built, tested and dry-run.

The model: a template is questions plus a skeleton. Every question is a decision delegated to the service team; everything else is a decision already made. The generated repository records the template version and the answers, so an update can render the old version (the merge base), compare it with the service (ours) and the new version (theirs), and merge per file.

## 1. Inventory the decisions

List what every service in the target group has in common and what legitimately differs. Read five recent services and the last ten incidents or review comments about service setup.

| Item | Decision already made when | Question when |
|---|---|---|
| base image, user, filesystem, capabilities | security or operations has one answer | never; give an escape hatch instead (Step 7) |
| probes, resource requests, labels the catalog and dashboards rely on | the platform's tooling depends on them | only the numbers that differ by workload (`cpu_request`) |
| CI steps, registry, image naming | one pipeline serves every service | never |
| service name, owner, description | never | always; validate format and that the owner exists |
| exposure (internal, public), database, environments | never | when it changes which files exist |
| language or framework | never | only if you maintain one skeleton per choice; otherwise ship separate templates |

Keep the question count low: each boolean doubles the combinations you must test. Prefer a computed default (`hostname` from `service_name`) and a `when` condition over another unconditional question.

## 2. Write questions that fail at generation time

For copier:

```yaml
service_name:
  type: str
  help: DNS label used for the image, the Deployment and the catalog entity
  validator: "{% if not (service_name | regex_search('^[a-z][a-z0-9-]{1,38}[a-z0-9]$')) %}must be a DNS label of 3 to 40 characters{% endif %}"
hostname:
  type: str
  when: "{{ exposure == 'public' }}"
  default: "{{ service_name }}.example.com"
```

For Backstage, the same constraints go in JSON Schema (`pattern`, `enum`, `required`) and pickers (`OwnerPicker` with `catalogFilter: {kind: Group}`), which can check against the catalog what a regex cannot. For cookiecutter, validate in `hooks/pre_gen_project.py` and exit non-zero.

Checklist: every free-text answer has a validator; every answer that ends up in YAML is either constrained to safe characters or rendered with `tojson`; integers have ranges that encode a policy (replicas at least 2) with the reason in the message.

## 3. Write the skeleton so it renders valid files

| Trap | Symptom | Fix |
|---|---|---|
| block tags on their own lines | empty or indented lines inside YAML lists | `_envops: {trim_blocks: true, lstrip_blocks: true}` or `{%-`/`-%}` |
| inline `{% endif %}` at the end of a line with `trim_blocks` | two lines joined | use a conditional expression (`{{ 'x' if a else 'y' }}`) or put the tag on its own line |
| `${{ github.sha }}` in a workflow | rendered as Jinja, error or empty | wrap in `{% raw %}...{% endraw %}` (Backstage: list the file in `copyWithoutTemplating`) |
| free text in YAML (`description: Orders: carts`) | invalid YAML | `{{ description \| tojson }}` |
| a file that only some services need | an empty file, or a manual delete step | make the name conditional: `{% if exposure == 'public' %}httproute.yaml{% endif %}.jinja` |
| files that contain braces (tests, Helm charts) | rendered by mistake | do not give them the `.jinja` suffix |
| arbitrary post-generation commands | surprise side effects | keep `_tasks` to `git init` and the first commit, with `when: "{{ _copier_operation == 'copy' }}"` |

## 4. Test the template like code

In the template's own CI, for a matrix of answer sets that covers every question value at least once (defaults; public with a database and three environments; a description with `:`, `&` and quotes):

```bash
copier copy --trust --defaults --vcs-ref HEAD -d service_name=orders -d owner=shop -d exposure=public -d database=true . "$OUT"
python -c 'import sys, yaml; [list(yaml.safe_load_all(open(p))) for p in sys.argv[1:]]' $(find "$OUT" -name '*.yaml')
(cd "$OUT" && python -m unittest discover -s tests -t .)
docker build -t template-check "$OUT"
kubectl apply --dry-run=server -f "$OUT/k8s/"
```

Add an update test: generate from the previous tag, apply a typical team edit, commit, `copier update --defaults --vcs-ref HEAD`, and assert the edit survives, the change lands and `git status` shows no `UU`. A release that conflicts on a line every team edits needs a migration or a different change.

## 5. Release

1. Tag every release `vX.Y.Z`; copier and `scaffolder` pick the highest tag, cruft records the commit.
2. Write a changelog entry per release: what changed in generated services, which files, whether a migration runs, whether it is likely to conflict.
3. Keep releases small: one concern per release merges cleanly; a release that reformats every file conflicts with every team.
4. Renames and moves need `_migrations` (copier) so the update does not see a deleted file and a new one.
5. Files a service owns after the first render go in `_skip_if_exists`.

## 6. Roll an update out to the fleet

```bash
for repo in $(cat services.txt); do
  git -C "$repo" switch -c template/v1.1.0
  (cd "$repo" && copier update --trust --defaults --vcs-ref v1.1.0)
  git -C "$repo" status --porcelain | grep -q '^UU' && echo "$repo conflicts" || echo "$repo clean"
done
grep -h '^_commit:' */.copier-answers.yml | sort | uniq -c
```

| Result | Action |
|---|---|
| clean update | commit, open a pull request labelled `template-update`, let the service's CI decide |
| conflict on a line the team changed on purpose | open the pull request with the markers and the changelog link; the owner resolves |
| conflict on many lines or many files | the release is too large or needs a migration; fix the template, not 50 services |
| `orphaned` or `skipped` files | the template dropped or the team deleted a file; ask the owner, do not recreate silently |
| service far behind (several majors) | update one version at a time so each migration runs |
| no answers file | the service was copied, not generated; offer `copier copy` into a branch and a manual merge, or leave it |

## 7. Keep it optional and cheaper

- Measure time from "I need a service" to first production deploy on the path and off it; the path must win by a wide margin.
- Count services per template and per version (`_commit` in answers files, `backstage.io/source-template` and `cloudcaptain.io/template-version` in the catalog, the annotation a scorecard can check); a rising share of copied repositories means the path costs too much.
- Document the escape hatch: how to leave the path for one file (`_skip_if_exists`) or entirely (delete the answers file), and what support the team gives up.
- Review every question once a quarter: remove questions everyone answers the same way; turn repeated support requests into questions or decisions.

## 8. Report

State: the template and version; the decisions it makes and the questions it asks, with validators; the answer matrix tested and what each case checked (YAML parses, tests pass, image builds, server dry run); for a release, the changelog, the files it touches and the update test result; for a rollout, services clean, conflicting (with files), orphaned or skipped, and those without an answers file; adoption numbers (services per template version, share of new services on the path); and the next change, with the reason it makes the path cheaper.
