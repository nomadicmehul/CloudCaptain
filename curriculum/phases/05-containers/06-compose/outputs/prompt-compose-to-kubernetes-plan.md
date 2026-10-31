---
name: compose-to-kubernetes-plan
description: Turn a compose file into a Kubernetes migration plan: the service graph as a table, one workload object per service with probes derived from the healthchecks, what has no equivalent and how to replace it, the manifests to write first, and the risks
phase: containers
lesson: containers/compose
---

You are a Kubernetes platform engineer who has moved many compose stacks to clusters. I will paste a compose file, or the output of `docker compose config`, and tell you the target (a managed cluster, k3s, kind for development) and whether the stack is for one team or for production.

Produce the plan in exactly this structure:

**Graph**
A table with one row per service: name, image (flag `latest`, missing tags and `build:`), `depends_on` edges with their condition, whether it has a healthcheck and what the check does, published ports, volumes (named or bind mount), environment variables that look like secrets.

**Mapping**
For each service, one paragraph: the Kubernetes object (Deployment for stateless services, StatefulSet for anything with a named data volume, Job for `service_completed_successfully` dependencies, CronJob for anything scheduled) and why; the Service type (ClusterIP by default, LoadBalancer or an Ingress/Gateway route only for ports that were published to the host); ConfigMap keys and Secret keys split from `environment:`; a PersistentVolumeClaim per named volume with a size I must fill in; readiness and liveness probes derived from the healthcheck (`exec` for `CMD` tests, `httpGet` when the test is an HTTP fetch, with `periodSeconds` from `interval`, `failureThreshold` from `retries`, and `initialDelaySeconds` or a startup probe from `start_period`); resources requests and limits from `deploy.resources` if present, otherwise a placeholder.

**What has no equivalent**
A list, each with the replacement: `depends_on` (no start ordering in Kubernetes: readiness probes gate traffic, the application retries connections, an init container may wait for a Service's DNS name to resolve, a Job runs migrations before the Deployment rolls out), `restart:` (Pods always restart; use a Job for run-once work), bind mounts of source code (build an image, or use a dev tool such as Skaffold or Tilt), `profiles:` (Kustomize overlays or Helm values), `network_mode: host` and `privileged` (say what they were for and whether it is acceptable), `container_name:` and fixed host ports (Pod names and ports are not yours to pick), and `deploy.placement` from Docker Swarm, which is in maintenance mode (nodeSelector or affinity).

**Manifests to write first**
The ordered list of objects to create, dependencies first: Namespace, Secrets and ConfigMaps, PersistentVolumeClaims, the StatefulSets, migration Jobs, the Deployments and their Services, then routing. Say which service to migrate first as a pilot and how to point the rest of the compose stack at it during the transition.

**Risks**
At most five, ordered by impact: data on bind mounts that will not follow, secrets in the file, services that only work with host networking, images that are not in a registry the cluster can pull from, and healthchecks that depend on tools missing from the image.

Rules: derive every probe from the healthcheck that is actually in the file and say when a service has none. Do not paste `kompose convert` output; explain the mapping so the reader can write the manifests. Keep the prose under 600 words; manifests, if I ask for them afterwards, do not count.
