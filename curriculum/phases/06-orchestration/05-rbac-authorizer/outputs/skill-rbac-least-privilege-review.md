---
name: rbac-least-privilege-review
description: Review what a Kubernetes subject can do, or what an RBAC change will grant, against what it needs; find escalation-equivalent grants, write the smallest Role, and prove the before and after with a can-i matrix
version: 1.0.0
phase: orchestration
lesson: orchestration/rbac-authorizer
tags: [kubernetes, rbac, least-privilege, security, kubectl, serviceaccount, review]
---

# RBAC least-privilege review

Use this when a pull request adds or changes Roles, ClusterRoles or bindings, when a workload asks for "more permissions", when someone proposes `cluster-admin` to fix a 403, or during a periodic access review of service accounts.

## 1. Dump the policy and fix the identity

```bash
kubectl get roles,rolebindings,clusterroles,clusterrolebindings -A -o json > rbac.json
SUBJ=system:serviceaccount:<ns>:<name>                         # or a user name, plus --as-group for each group
kubectl auth whoami --as $SUBJ                                  # the user and groups the API server will evaluate
python3 canhaz.py whoami --as $SUBJ                             # the same, offline
```

Write the identity down as user plus groups. A service account is `system:serviceaccount:NS:NAME` in `system:serviceaccounts`, `system:serviceaccounts:NS` and `system:authenticated`; grants to any of those groups count. For a pull request, layer the proposed objects on top: `kubectl create -f change.yaml --dry-run=client -o json > proposed.json` and pass `-f rbac.json -f proposed.json`.

## 2. List what it can do, where

```bash
kubectl auth can-i --list -n <ns> --as $SUBJ                    # per namespace that matters
kubectl auth can-i --list -A --as $SUBJ                         # cluster scope: ClusterRoleBindings only
python3 canhaz.py can-i --list -n <ns> --as $SUBJ -f rbac.json  # same table, plus warnings for dangling bindings
python3 canhaz.py can-i VERB RESOURCE -n <ns> --as $SUBJ -f rbac.json   # which binding and rule grant one permission
```

Record every binding that applies (RoleBindings in each namespace, ClusterRoleBindings) and the role behind it. A RoleBinding to a ClusterRole is scoped to its namespace; a ClusterRoleBinding is everywhere.

## 3. Flag escalation-equivalent grants

```bash
for q in "impersonate users -A" "impersonate groups -A" "impersonate serviceaccounts -A" "bind clusterroles -A" "escalate clusterroles -A" \
         "bind roles -n <ns>" "escalate roles -n <ns>" "create pods -n <ns>" "create pods --subresource=exec -n <ns>" \
         "create serviceaccounts --subresource=token -n <ns>" "list secrets -A" "get secrets -n <ns>" "get nodes --subresource=proxy -A" \
         "create rolebindings -n <ns>" "create clusterrolebindings -A"; do
  printf '%-60s %s\n' "$q" "$(kubectl auth can-i $q --as $SUBJ 2>/dev/null | head -1)"
done
kubectl auth can-i '*' '*' -A --as $SUBJ                         # quoted, so the shell does not expand the stars
python3 canhaz.py who-can impersonate groups -A -f rbac.json     # the reverse view across the cluster
```

| Grant | Why it matters | Verdict |
|---|---|---|
| `*` verbs or `*` resources | covers every future resource and subresource too | reject unless the subject is a cluster operator |
| `impersonate` on users or groups | `--as-group system:masters` is a valid request | reject; impersonation belongs to break-glass identities |
| `bind` on a ClusterRole without `resourceNames` | can grant that role to anyone, itself included | narrow to the exact roles it must hand out |
| `escalate` on roles | can write any rule into a role it may update | reject for workloads |
| `create pods` or a controller (`deployments`, `jobs`) in a namespace | can run as any service account there and use its token | accept only with the namespace's accounts reviewed |
| `pods/exec`, `pods/attach`, `pods/portforward` | a shell or a tunnel inside every pod's network and files | accept for humans on call, not for automation |
| `get`/`list`/`watch` on `secrets`, especially cluster-wide | reads every credential, including long-lived token Secrets | narrow with `resourceNames` for `get`; never `list` cluster-wide |
| `create serviceaccounts/token` | mints tokens for other accounts | reject unless it is a token broker |
| `nodes/proxy` | the kubelet API: exec and logs without the API server's checks | reject outside monitoring agents, and then only `get` |
| a binding whose role does not exist | dead today, live the day someone creates that name | delete it |
| a ServiceAccount subject without `namespace` in a RoleBinding | means the binding's namespace, often not the one intended | add the namespace explicitly |

## 4. Find out what it actually needs

```bash
# with audit logging enabled: every request this identity made, with the decision
jq -c 'select(.user.username=="'"$SUBJ"'") | [.verb, .objectRef.apiGroup, .objectRef.resource, .objectRef.subresource, .objectRef.namespace, .annotations["authorization.k8s.io/decision"]]' audit.log | sort | uniq -c | sort -rn
kubectl logs deploy/<app> -n <ns> | grep -i forbidden              # 403s the application already hit
```

No audit log: read the client code or the operator's documented RBAC, then run the workload in a scratch namespace with the candidate Role and collect the 403s.

## 5. Write the smallest Role

One rule per API group and resource set; explicit verbs (never `*`); subresources named (`pods/log`); `resourceNames` for `get`, `update`, `patch`, `delete` on known objects; a Role and RoleBinding per namespace instead of a ClusterRoleBinding; reuse `view` through a RoleBinding when read-only access to most objects is genuinely needed. If the same rule must reach every reader, use an aggregated ClusterRole with `aggregate-to-view` and say in the review that it also reaches `edit` and `admin`.

```bash
kubectl create role <app>-reader -n <ns> --verb=get,list,watch --resource=configmaps,pods --dry-run=client -o yaml
kubectl create role <app>-secret -n <ns> --verb=get --resource=secrets --resource-name=<one-secret> --dry-run=client -o yaml
```

## 6. Prove it: a before and after matrix

```bash
kubectl create -f proposed-rbac.yaml --dry-run=client -o json > proposed.json
for q in "get configmaps -n <ns>" "list pods -n <ns>" "get secrets/<one-secret> -n <ns>" "list secrets -n <ns>" "create pods -n <ns>" "list pods -A"; do
  before=$(python3 canhaz.py can-i $q --as $SUBJ -f rbac.json | head -1)
  after=$(python3 canhaz.py can-i $q --as $SUBJ -f rbac.json -f proposed.json | head -1)
  printf '%-45s before=%-3s after=%s\n' "$q" "$before" "$after"
done
```

Every permission from step 4 must be `yes` after; every grant flagged in step 3 must be `no` after, unless it is justified in writing. Removing a permission means removing or changing the binding that grants it; adding a Role never takes anything away.

## 7. Check who will apply it

```bash
python3 canhaz.py can-bind --role <app>-reader -n <ns> --as <the-deployer> -f rbac.json -f proposed.json
python3 canhaz.py can-bind --clusterrole view -n <ns> --as <the-deployer> -f rbac.json
```

If the pipeline that applies RBAC is refused by the escalation check, give it `bind` on exactly the roles it hands out (`--verb=bind --resource=clusterroles.rbac.authorization.k8s.io --resource-name=view`) rather than `cluster-admin`.

## 8. Report

State: the identity (user and groups); every binding that applies, with scope; the escalation-equivalent grants found, with the binding and rule for each and the verdict from the table; what the workload needs, with the evidence (audit query or 403 list); the proposed Role and RoleBinding; the before and after matrix; the escalation check for whoever applies it; and anything left over-privileged with an owner and a date. Do not approve a change that grants `*`, `impersonate`, unrestricted `bind` or `escalate`, or cluster-wide Secret reads to a workload.
