---
name: platform-api-review
description: Review a platform abstraction (Score with score-k8s, a KubeVela definition, a Crossplane XRD and Composition, or a home-grown CRD and translator) before teams depend on it, with render, validation, versioning, escape-hatch and change drills on kind and a fixed report
version: 1.0.0
phase: platform
lesson: platform/platform-api
tags: [platform-engineering, score, score-k8s, kubevela, crossplane, crd, cel, conversion, hpa, networkpolicy, review]
---

# Platform API review

Use this before a platform abstraction becomes the default way to ship services, when its schema gets a new version, or when teams start asking for "just one more field". You need the abstraction's sources (schema, translator or definitions, provisioners), two or three real service specs written in it, and a scratch cluster: `kind create cluster --name cc-review`. Never run the drills against production.

## 1. Render the samples and count what they hide

```bash
score-k8s init --no-sample && score-k8s generate *.score.yaml --namespace review     # Score
vela dry-run -f app.yaml > rendered.yaml                                            # KubeVela
crossplane render xr.yaml composition.yaml functions.yaml > rendered.yaml           # Crossplane (crossplane beta render on 1.x CLIs)
python3 main.py render a.json b.json -o json --namespace review > rendered.json      # a home-grown translator
grep -c '^kind:' rendered.yaml || python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["items"]))' rendered.json
```

Write down, per sample: spec lines written by the team, objects rendered, and the kinds. A good abstraction turns about twenty lines into the full set (Deployment, Service, ServiceAccount, HPA or fixed replicas, PDB when it makes sense, NetworkPolicy, route, dependency objects). Fewer objects than that means teams will add the rest by hand next to the abstraction.

## 2. Check the defaults it decides

Read one rendered Deployment and its neighbours against this table.

| Check | Pass looks like | Fail looks like |
|---|---|---|
| Pod security | `runAsNonRoot: true`, numeric `runAsUser`, `seccompProfile: RuntimeDefault`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `readOnlyRootFilesystem: true` with an `emptyDir` for `/tmp` | no `securityContext`, or one a spec can override |
| Service account | one per workload, `automountServiceAccountToken: false` unless the spec asks for API access | `default` service account with a mounted token |
| Probes | readiness and liveness on a named port, paths from the spec | no probes, or liveness identical to a slow readiness check |
| Resources | requests from a size table, a memory limit, no CPU limit unless the platform has decided otherwise | nothing set, or CPU limits that throttle |
| Replicas and HPA | with autoscaling, no `spec.replicas` in the Deployment | `replicas` and an HPA together |
| PodDisruptionBudget | only with 2 or more replicas, `maxUnavailable: 1` | `minAvailable: 1` on one replica (blocks every drain) |
| NetworkPolicy | ingress only from callers named in dependencies and from the gateway's namespace | none, or `podSelector: {}` allow-all |
| Route | attaches to the platform Gateway with an explicit namespace and port | a Gateway name that does not exist (for example `default`) |
| Dependencies | env vars pointing at Services that exist in the output, secrets by `secretKeyRef` | literal passwords in env, URLs with the target port instead of the Service port |

```bash
jq '.items[] | select(.kind=="Deployment") | .spec.template.spec | {securityContext, sa: .serviceAccountName, token: .automountServiceAccountToken, c: .containers[0].securityContext}' rendered.json
jq -r '.items[] | select(.kind=="Deployment") | "\(.metadata.name) replicas=\(.spec.replicas // "unset")"' rendered.json
```

## 3. Validation parity

Write a table of invalid specs (one mistake each: both replicas and min/max, min above max, `:latest`, an unknown field, an enum typo, a duplicate dependency, a too-long name). Run each through the local validator and through the API server:

```bash
kubectl apply --dry-run=server --validate=strict -n review -f case.json
```

Pass: both reject every case, with messages that name the field and say what to do. Record every case only one side rejects. A rule only the translator enforces is fine if CI runs the translator on every change; a rule only the API server enforces will surprise developers at deploy time. Also apply the CRD itself on kind: a CRD whose CEL rules are too expensive is refused at that point, which is cheaper to learn here than in a release.

## 4. Versions and conversion

```bash
kubectl get crd NAME -o jsonpath='{range .spec.versions[*]}{.name} served={.served} storage={.storage}{"\n"}{end}{.spec.conversion.strategy}{"\n"}'
```

For every pair of served versions: convert a sample down and back up and compare the result with the original by parsing both, not by diffing text. Pass: equal, including fields the older version cannot express (preserved in an annotation). Fail: a field lost, or several served versions with different schemas and `strategy: None`, which only rewrites `apiVersion`.

## 5. The escape hatch

Try three overrides: one legitimate (a `priorityClassName`, a pod annotation), one that touches the security context, one that targets a kind the workload does not render. Pass: the first applies, the second is refused with a pointer to the platform team, the third names the kinds that exist. Fail: no override path at all (teams will fork), or one that can turn off `runAsNonRoot`.

## 6. Change drills

1. **Image change.** Diff old and new renders (`miniplatform diff`, `kubectl diff -f`). Pass: only the pod template changes and the tool says pods restart.
2. **Fixed to autoscaled.** Pass: the tool warns that removing `spec.replicas` resets the Deployment to 1 unless ownership is handed over, and documents the handover (`kubectl apply edit-last-applied` for client-side apply, the server-side apply transfer otherwise). Verify on kind with `kubectl get deployment -w` that the count never drops.
3. **Route path change.** Pass: only the HTTPRoute changes; no pod restart.
4. **Immutable field.** Change something that ends up in a selector or a StatefulSet's `serviceName`. Pass: the tool says the object must be recreated. Fail: the apply fails in the pipeline with `field is immutable`.

## 7. Decide

| Finding | Decision |
|---|---|
| defaults table has a Fail in pod security or service account | not the default path until fixed |
| replicas and an HPA rendered together | not for autoscaled services until fixed |
| a served version loses fields on round trip | do not serve that version; fix conversion first |
| security context can be overridden | not for shared clusters |
| validation parity gaps without CI running the local validator | acceptable for a pilot only |
| no override path | expect forks; add one before rollout |
| everything passes | approve, with the schema's field owners and the version policy written down |

## Report

```markdown
## Platform API review: <name> (<tool and version>)
**Decision:** approve | approve for pilot | not yet
**Samples:** <service>: <spec lines> lines -> <n> objects (<kinds>)
**Defaults:** <table rows with Fail, or "all pass">
**Validation parity:** <n> cases, <n> both reject, <list of one-sided cases and where the rule lives>
**Versions:** <served versions, storage, strategy>; round trip: <pass or the lost fields>
**Escape hatch:** <mechanism>; guarded: <paths>; gaps: <...>
**Change drills:** image <pass/fail>, autoscaling <pass/fail>, route <pass/fail>, immutable <pass/fail>
**Required before rollout:** <numbered list>
```
