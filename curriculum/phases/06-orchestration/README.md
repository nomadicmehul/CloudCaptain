# Orchestration

*A scheduler and a reconciler from scratch, then Kubernetes, Helm, operators and the Gateway API.*

Kubernetes is easy to use and hard to debug for the same reason: almost nothing in it happens when you run a command. `kubectl apply` writes a record; a dozen independent loops notice the record and move the world towards it. A Pending pod, a rollout that never finishes, a Service that answers from the wrong pod or a 403 from the API server is one of those loops telling you exactly what it compared and why it stopped. This phase builds each loop as a small tool first (a reconciler, a scheduler, a node agent, a service proxy, an authorizer, a release manager, an operator and a router) and then runs the same job on a real cluster, so every event and status field reads as something you have written yourself.

## How this phase is built

| Lesson | Type | What you build | What you then use |
|---|---|---|---|
| Reconcilers | Build | `minictl`: an API store with resourceVersion, watch and 409 conflicts, owner references and garbage collection, a work queue with backoff, ReplicaSet and Deployment controllers with rolling updates and rollback | `kubectl rollout`, `get -w`, `ownerReferences`, `pod-template-hash`, raw watches, client-go informers |
| Scheduler | Build | `minisched`: kube-scheduler's filters (cordon, taints, affinity, resources, topology spread) and scores, normalised and weighted, with the FailedScheduling message and a preemption pass | `kubectl describe` events, `taint`, `cordon`, allocatable, priority and preemption, Karpenter |
| Kubelet | Build | `minikubelet`: static pods, a CRI-shaped runtime (fake, processes, docker with a pause sandbox), init containers, restart back-off, startup, liveness and readiness probes, graceful stop | static and mirror pods on kind, `crictl`, CrashLoopBackOff, `terminationGracePeriodSeconds` |
| Services & DNS | Build | `miniproxy`: an EndpointSlice controller, kube-proxy's iptables and nftables rules with the probability cascade, a trace through a real ruleset, cluster DNS records and the `ndots:5` cost | `kubectl get endpointslices`, `iptables-save` on the node, `nslookup`, CoreDNS, headless Services |
| RBAC | Build | `canhaz`: the API server's RBAC evaluation with aggregation, its "allowed by" reasons and 403 text, `--list`, who-can and the escalation check | `kubectl auth can-i --as`, `--list`, `auth whoami`, SubjectAccessReview |
| Helm | Build | `minihelm`: values merging, a Go-template subset whose output matches `helm template`, and release records stored as Helm's own Secrets with install, upgrade, rollback and history | `helm template`, `install`, `upgrade`, `rollback`, `history`, decoding release Secrets, OCI charts |
| Operators | Build | `minioperator`: a CRD with a CEL rule and a controller over `kubectl proxy` with watches, owned objects, status conditions, observedGeneration and a finalizer | `kubectl explain`, `--subresource=status`, stuck finalizers, kubebuilder, kopf |
| Gateway API | Build | `minigateway`: listener attachment, HTTPRoute status conditions, the spec's match precedence, filters and weighted backends in a working proxy | Gateway API CRDs, Envoy Gateway, HTTPRoute status, `ingress2gateway` (ingress-nginx is retired) |

Further lessons for this phase are proposed in [ROADMAP.md](https://github.com/nomadicmehul/CloudCaptain/blob/main/ROADMAP.md).

## Reference layer

The long-form guides, cheat sheet, CKA/CKAD/CKS exam prep and interview questions for this phase live under [Kubernetes](https://cloudcaptain.io/docs/tools/kubernetes/) and [Helm](https://cloudcaptain.io/docs/tools/helm/). Keep the cheat sheet open while you work; the exam prep page maps these lessons onto the CKA domains.
