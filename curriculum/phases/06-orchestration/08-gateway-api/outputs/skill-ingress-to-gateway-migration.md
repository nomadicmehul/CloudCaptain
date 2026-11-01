---
name: ingress-to-gateway-migration
description: Move Ingress objects to the Gateway API with a maintained implementation (ingress-nginx is retired): inventory hosts, paths and annotations, translate with ingress2gateway, check attachment, status and precedence, run old and new side by side, compare responses, shift traffic and decommission
version: 1.0.0
phase: orchestration
lesson: orchestration/gateway-api
tags: [kubernetes, gateway-api, ingress, ingress-nginx, envoy-gateway, httproute, migration]
---

# Ingress to Gateway API migration

Use this when a cluster still serves traffic through Ingress objects, and in particular through the ingress-nginx controller, which was retired in March 2026 and no longer gets releases or security fixes. The Ingress API itself is still served but frozen: new routing features go into the Gateway API only. The goal is the same responses from a Gateway and HTTPRoutes, proven request by request, before DNS moves.

## 1. Inventory what the old controller actually does

```bash
kubectl get ingressclass
kubectl get ingress -A -o json > ingress.json
kubectl get ingress -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\t"}{.spec.ingressClassName}{"\t"}{range .spec.rules[*]}{.host}{" "}{end}{"\n"}{end}'
jq -r '.items[] | .metadata.annotations // {} | keys[]' ingress.json | sort | uniq -c | sort -rn
jq -r '.items[] | .metadata as $m | .spec.rules[]? | .host as $h | .http.paths[] | "\($h)\t\(.pathType)\t\(.path)\t\($m.namespace)/\(.backend.service.name):\(.backend.service.port.number // .backend.service.port.name)"' ingress.json > paths.tsv
```

`paths.tsv` (host, pathType, path, backend) is the test plan for step 6. Add the paths you know clients call that are not literally in the file: trailing slashes, `/apiv2`-style neighbours of a prefix, mixed case, query strings.

## 2. Classify every annotation

Each annotation is either a standard Gateway API field, an implementation policy (a CRD of the controller you pick, so lock-in you should list), or something to delete.

| ingress-nginx annotation | Gateway API equivalent | Portable |
|---|---|---|
| `canary`, `canary-weight` | two `backendRefs` with `weight` in one rule | yes |
| `canary-by-header`, `canary-by-header-value` | a rule whose match has `headers` | yes |
| `ssl-redirect`, `force-ssl-redirect` | a rule on the HTTP listener with `RequestRedirect` `scheme: https`, `statusCode: 301` | yes |
| `rewrite-target` without capture groups | `URLRewrite` with `ReplacePrefixMatch` or `ReplaceFullPath` | yes |
| `use-regex: "true"`, `rewrite-target: /$2` | `RegularExpression` paths are implementation-specific and capture groups have no standard rewrite; prefer splitting into `Exact` and `PathPrefix` rules | no: retest every path |
| `backend-protocol: GRPC` | a `GRPCRoute` | yes |
| `backend-protocol: HTTPS` | a `BackendTLSPolicy` for the Service | yes |
| `proxy-read-timeout`, `proxy-send-timeout` | rule `timeouts.request` and `timeouts.backendRequest` | partly |
| `proxy-body-size`, `limit-rps`, `auth-url`, `whitelist-source-range`, `affinity: cookie`, `enable-cors` | the implementation's policy CRDs (check its docs; some have a standard or experimental field in recent releases) | no |
| `configuration-snippet`, `server-snippet` | none; find out what the snippet does and express it as one of the rows above, or delete it | no |

A migration that keeps more than a handful of "no" rows is a redesign, not a translation; say so in the report.

## 3. Install the new path next to the old one

```bash
kubectl apply --server-side -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.7.0/standard-install.yaml
helm pull oci://docker.io/envoyproxy/gateway-helm --version v1.9.3 --untar
kubectl apply --server-side -f gateway-helm/crds/generated/
helm install eg ./gateway-helm -n envoy-gateway-system --create-namespace --skip-crds
kubectl wait --timeout=5m -n envoy-gateway-system deployment/envoy-gateway --for=condition=Available
```

Envoy Gateway is one maintained implementation; Cilium, Istio, kgateway and Traefik also implement the Gateway API. Whatever you choose, create one GatewayClass, then a Gateway per traffic entry point owned by the platform team: one listener per port and hostname (wildcards like `*.shop.example.com` do not cover the apex `shop.example.com`), `certificateRefs` for TLS, and `allowedRoutes.namespaces.from: Selector` with a namespace label such as `gateway-access: shop` so only intended teams attach. The new Gateway gets its own address; the old controller keeps serving.

## 4. Translate, then review the draft

```bash
ingress2gateway print --providers=ingress-nginx -A > gateway-draft.yaml
ingress2gateway print --providers=ingress-nginx --input-file ingress.yaml >> gateway-draft.yaml
```

| Check in the draft | Accept | Change |
|---|---|---|
| Gateways | point every HTTPRoute at the shared Gateway from step 3 | drafts that create one Gateway per Ingress class or per namespace |
| `parentRefs` | `name`, `namespace` of the shared Gateway, `sectionName` of the listener when there are several | missing namespace when the Gateway lives elsewhere |
| `pathType: Prefix` | `PathPrefix` (both compare whole segments) | nothing |
| `pathType: ImplementationSpecific` or regex paths | an explicit `Exact` or `PathPrefix` you have tested | a `RegularExpression` copied blindly |
| backend in another namespace | a ReferenceGrant in the backend's namespace | ExternalName Services that existed only to work around Ingress |
| two routes with identical matches for one host | one owner per path | relying on creationTimestamp to pick the winner |

## 5. Apply and read status before any traffic

```bash
kubectl apply -f gateway-draft.yaml
kubectl get httproute -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\t"}{range .status.parents[*].conditions[*]}{.type}={.status}/{.reason} {end}{"\n"}{end}'
kubectl get gateway -A -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .status.listeners[*]}{.name}={.attachedRoutes} {end}{"\n"}{end}'
kubectl get gateways.gateway.networking.k8s.io,httproutes,referencegrants,services -A -o json > live.json
python3 minigateway.py status live.json && python3 minigateway.py table live.json --gateway infra/web
```

| Status | Meaning | Fix |
|---|---|---|
| no entry in `status.parents` | no controller owns the parent the route names | fix `parentRefs` name, namespace or group; check the GatewayClass `controllerName` |
| `Accepted=False NotAllowedByListeners` | the listener's `allowedRoutes` excludes the route's namespace or kind | label the namespace, or change `from` |
| `Accepted=False NoMatchingListenerHostname` | route and listener hostnames do not intersect | add the hostname to a listener or fix the route |
| `Accepted=False NoMatchingParent` | `sectionName` or `port` names no listener | use the listener's name |
| `ResolvedRefs=False RefNotPermitted` | cross-namespace backend without consent | ReferenceGrant in the backend's namespace |
| `ResolvedRefs=False BackendNotFound` | the Service does not exist | fix the name or namespace; that share of traffic gets 500s |
| `observedGeneration` below `metadata.generation` | the controller has not processed the latest spec | wait, then check the controller's logs |

Do not continue while any route intended for production is not `Accepted=True` and `ResolvedRefs=True`.

## 6. Compare old and new response by response

```bash
OLD=$(kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
NEW=$(kubectl get gateway -n infra web -o jsonpath='{.status.addresses[0].value}')
while IFS=$'\t' read -r host type path backend; do
  for p in "$path" "${path%/}/" "${path}x"; do
    a=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' -H "Host: $host" "http://$OLD$p")
    b=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' -H "Host: $host" "http://$NEW$p")
    [[ "$a" == "$b" ]] || echo "DIFF $host$p ($backend) old=[$a] new=[$b]"
  done
done < paths.tsv
```

Every `DIFF` line is either a bug in the draft or a behaviour you decide to change on purpose (a regex that matched too much is the usual one); write each decision down. For canaries, send 200 requests and compare the split, not single responses. Use `--resolve host:443:$NEW` for HTTPS listeners.

## 7. Shift traffic and keep a way back

Lower the DNS TTL a day ahead. Move one hostname at a time, low-traffic ones first, by pointing its record at the Gateway address. Watch 4xx and 5xx rates and latency on both paths for at least a full traffic cycle. Rolling back is pointing the record back; keep the old controller and its Ingress objects untouched until every hostname has run on the Gateway for the agreed period.

## 8. Decommission

```bash
kubectl delete ingress -A -l migrated-to=gateway-api            # or by name from the inventory
helm uninstall ingress-nginx -n ingress-nginx
kubectl get validatingwebhookconfiguration | grep -i ingress   # a leftover admission webhook blocks Ingress writes cluster-wide
kubectl delete ingressclass nginx
```

## 9. Report

State: the number of Ingress objects, hosts and paths inventoried; the annotation table with each row's decision and every implementation-specific policy you now depend on; the Gateway and listeners created and which namespaces may attach; route status (all `Accepted=True` and `ResolvedRefs=True`, or which are not and why); the `DIFF` lines from step 6 and the decision on each; the cutover order and dates per hostname; and the rollback step. Do not mark the migration done while ingress-nginx still serves any production hostname.
