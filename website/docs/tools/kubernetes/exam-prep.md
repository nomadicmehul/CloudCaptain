---
title: "Kubernetes Certification Prep"
sidebar_label: "Exam Prep (CKA/CKAD/CKS)"
description: "Comprehensive preparation guide for CKA, CKAD, and CKS Kubernetes certifications — exam domains, study tips, and practice exercises"
sidebar_position: 6
---

# Kubernetes Certification Preparation Guide

Master Kubernetes certifications with this comprehensive study guide covering **CKA**, **CKAD**, **CKS**, and **KCNA**. Includes exam breakdowns, practice questions, labs, and essential tips.

---

## Certification Overview

| Certification | Prerequisites | Duration | Passing Score | Cost | Difficulty | Focus Area |
|--------------|---------------|----------|---------------|------|-----------|-----------|
| **KCNA** | None | 90 min | 66% | $395 | Beginner | Kubernetes fundamentals |
| **CKA** | Docker/container basics | 120 min | 66% | $395 | Intermediate | Cluster administration |
| **CKAD** | Basic Kubernetes knowledge | 120 min | 66% | $395 | Intermediate | Application development |
| **CKS** | CKA or 1+ years K8s experience | 120 min | 66% | $395 | Advanced | Security & hardening |

:::note
All exams are hands-on, performance-based. You have access to Kubernetes documentation (kubernetes.io) but **not external sites**.
:::

---

## CKA: Certified Kubernetes Administrator

### Exam Domains & Weights

The CKA exam focuses on cluster administration and has five main domains:

| Domain | Weight | Key Skills |
|--------|--------|-----------|
| Cluster Architecture, Installation & Configuration | 25% | kubeadm, upgrades, HA, ETCD, certificates |
| Workloads & Scheduling | 15% | Deployments, StatefulSets, DaemonSets, scheduling, node affinity |
| Services & Networking | 20% | Service types, endpoints, DNS, network policies, ingress |
| Storage | 10% | PV, PVC, StorageClass, mount volumes, RBAC for storage |
| Troubleshooting | 30% | Debugging pods, nodes, services, logs, events, metrics |

### CKA: Detailed Topics & Key Concepts

#### Cluster Architecture (25%)

- **kubeadm**: Initialize clusters, join nodes, certificates
- **ETCD**: Backup/restore, snapshot management, key-value operations
- **API Server**: Authentication, authorization, RBAC, webhooks
- **Kubelet**: Node registration, runtime configuration, static pods
- **High Availability**: Multi-master setup, load balancing, quorum
- **Cluster Upgrades**: Rolling updates, component compatibility, drain nodes
- **Certificates**: Certificate management, kubeadm certificates, renewal

#### Workloads & Scheduling (15%)

- **Deployments**: Replicas, rolling updates, rollbacks, strategies
- **StatefulSets**: Ordered deployment, persistent identity, headless services
- **DaemonSets**: Node affinity, taints/tolerations, system pods
- **Pod Scheduling**: Node selectors, affinity rules, taints, resource requests/limits
- **Horizontal Pod Autoscaler (HPA)**: Metrics-based scaling, target utilization
- **Jobs & CronJobs**: Completions, parallelism, backoff policies

#### Services & Networking (20%)

- **Service Types**: ClusterIP, NodePort, LoadBalancer, ExternalName
- **Endpoints & DNS**: Service discovery, CoreDNS, headless services
- **Network Policies**: Traffic isolation, ingress/egress rules, label selectors
- **Ingress**: Ingress controllers, routing rules, TLS termination
- **CNI**: Container networking interface, cluster networking setup
- **Service Mesh (optional)**: Istio basics, traffic management

#### Storage (10%)

- **Persistent Volumes (PV)**: Static/dynamic provisioning, lifecycle, access modes
- **Persistent Volume Claims (PVC)**: Claims, binding, requests
- **StorageClass**: Provisioners, parameters, default storage class
- **Volume Mounts**: Directories, configMaps, secrets, emptyDir
- **StatefulSet Storage**: Persistent volume claim templates, ordered scaling

#### Troubleshooting (30%)

- **Pod Troubleshooting**: CrashLoopBackOff, ImagePullBackOff, OOMKilled
- **Node Issues**: NotReady, MemoryPressure, DiskPressure, unschedulable
- **Service Connectivity**: DNS resolution, port mappings, endpoint failures
- **Logging & Events**: kubectl logs, describe, events, node logs
- **Performance**: Metrics server, resource usage, bottlenecks
- **Cluster Health**: API availability, etcd status, component logs

### CKA: 10 Practice Questions with Solutions

**Question 1: Create a Deployment with 3 replicas**

Create a Deployment named `web-app` with 3 replicas running `nginx:1.14` and expose it via ClusterIP Service.

<details>
<summary>Solution</summary>

```bash
# Imperative approach (fastest for exam)
kubectl create deployment web-app --image=nginx:1.14 --replicas=3
kubectl expose deployment web-app --port=80 --target-port=80 --type=ClusterIP
```

Or declaratively (YAML):

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web-app
spec:
  replicas: 3
  selector:
    matchLabels:
      app: web-app
  template:
    metadata:
      labels:
        app: web-app
    spec:
      containers:
      - name: nginx
        image: nginx:1.14
        ports:
        - containerPort: 80
---
apiVersion: v1
kind: Service
metadata:
  name: web-app
spec:
  selector:
    app: web-app
  ports:
  - port: 80
    targetPort: 80
  type: ClusterIP
```

Verify:
```bash
kubectl get deployment web-app
kubectl get svc web-app
kubectl get pods -l app=web-app
```

</details>

---

**Question 2: Schedule a pod on a specific node**

Create a pod `nginx-on-node1` that runs only on the node labeled `disk=ssd`.

<details>
<summary>Solution</summary>

First, label the node:
```bash
kubectl label nodes node1 disk=ssd
```

Then create the pod with nodeSelector:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: nginx-on-node1
spec:
  nodeSelector:
    disk: ssd
  containers:
  - name: nginx
    image: nginx
```

Or using affinity:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: nginx-on-node1
spec:
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
        - matchExpressions:
          - key: disk
            operator: In
            values:
            - ssd
  containers:
  - name: nginx
    image: nginx
```

Verify:
```bash
kubectl get pod nginx-on-node1 -o wide  # Check which node it's on
```

</details>

---

**Question 3: Drain a node for maintenance**

Safely remove a node from the cluster for maintenance, ensuring pods are evicted gracefully.

<details>
<summary>Solution</summary>

```bash
# Drain the node (evicts pods, marks as unschedulable)
kubectl drain node1 --ignore-daemonsets --delete-emptydir-data

# After maintenance, uncordon the node
kubectl uncordon node1

# Verify status
kubectl get nodes
```

**Explanation**:
- `--ignore-daemonsets`: DaemonSet pods (like kube-proxy) won't be evicted
- `--delete-emptydir-data`: Allow deletion of pods with emptyDir volumes
- Use `--force` only as last resort (skips graceful termination)

</details>

---

**Question 4: Create a NetworkPolicy to isolate traffic**

Block all ingress traffic to pods in the `default` namespace except from pods with label `app=trusted`.

<details>
<summary>Solution</summary>

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: restrict-traffic
  namespace: default
spec:
  podSelector: {}  # Applies to all pods in namespace
  policyTypes:
  - Ingress
  ingress:
  - from:
    - podSelector:
        matchLabels:
          app: trusted
```

Apply and test:
```bash
kubectl apply -f network-policy.yaml

# Test from a trusted pod
kubectl run test --image=busybox --labels=app=trusted -- sleep 1000
kubectl run untrusted --image=busybox -- sleep 1000

# From test pod, connectivity should work
# From untrusted pod, connectivity should fail
```

</details>

---

**Question 5: Back up and restore etcd**

Create a snapshot of etcd and restore it.

<details>
<summary>Solution</summary>

First, find etcd pod (usually in kube-system namespace):

```bash
kubectl -n kube-system get pods | grep etcd
```

Snapshot etcd (from control plane node):

```bash
ETCDCTL_API=3 etcdctl --endpoints=127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  snapshot save /tmp/etcd-backup.db
```

Verify snapshot:
```bash
ETCDCTL_API=3 etcdctl snapshot status /tmp/etcd-backup.db
```

Restore (stops API server during restore):

```bash
ETCDCTL_API=3 etcdctl snapshot restore /tmp/etcd-backup.db \
  --data-dir=/var/lib/etcd-restored
```

</details>

---

**Question 6: Troubleshoot a pod that won't start**

Debug a pod stuck in `CrashLoopBackOff` state.

<details>
<summary>Solution</summary>

```bash
# Check pod status
kubectl describe pod problematic-pod

# View logs (if container started)
kubectl logs problematic-pod
kubectl logs problematic-pod --previous  # Previous container logs

# Check events
kubectl get events --sort-by='.lastTimestamp'

# Common causes and fixes:
# 1. Image doesn't exist: kubectl set image deployment/name container=image:tag
# 2. Missing env vars: kubectl set env deployment/name KEY=VALUE
# 3. Port conflict: kubectl set env deployment/name PORT=8081
# 4. Health check failure: kubectl describe pod to check probes
```

Example fix for bad image:

```bash
kubectl set image deployment/my-app \
  my-app=nginx:1.14 --record
```

</details>

---

**Question 7: Upgrade Kubernetes cluster**

Upgrade a kubeadm cluster from 1.24 to 1.25.

<details>
<summary>Solution</summary>

On control plane node:

```bash
# Check current version
kubectl version

# Upgrade kubeadm
apt update && apt-get install -y kubeadm=1.25.x-00

# Plan the upgrade
kubeadm upgrade plan

# Apply the upgrade
sudo kubeadm upgrade apply v1.25.x

# Upgrade kubelet and kubectl
apt-get install -y kubelet=1.25.x-00 kubectl=1.25.x-00
sudo systemctl daemon-reload
sudo systemctl restart kubelet
```

On worker nodes (repeat for each):

```bash
# Drain the node
kubectl drain <node-name> --ignore-daemonsets

# SSH to worker node and upgrade
apt-get install -y kubeadm=1.25.x-00
sudo kubeadm upgrade node

apt-get install -y kubelet=1.25.x-00 kubectl=1.25.x-00
sudo systemctl daemon-reload
sudo systemctl restart kubelet

# Back to control plane
kubectl uncordon <node-name>
```

Verify upgrade:
```bash
kubectl version
kubectl get nodes
```

</details>

---

**Question 8: Configure RBAC for service account**

Create a service account and RBAC role allowing it to manage Deployments in `apps-namespace`.

<details>
<summary>Solution</summary>

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: deployment-manager
  namespace: apps-namespace
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: deployment-manager-role
  namespace: apps-namespace
rules:
- apiGroups: ["apps"]
  resources: ["deployments", "statefulsets"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: deployment-manager-binding
  namespace: apps-namespace
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: deployment-manager-role
subjects:
- kind: ServiceAccount
  name: deployment-manager
  namespace: apps-namespace
```

Test:
```bash
kubectl auth can-i get deployments \
  --as=system:serviceaccount:apps-namespace:deployment-manager \
  -n apps-namespace
```

</details>

---

**Question 9: Create and mount a ConfigMap**

Create a ConfigMap with application configuration and mount it in a Deployment.

<details>
<summary>Solution</summary>

```bash
# Create ConfigMap from literal values
kubectl create configmap app-config \
  --from-literal=database_host=postgres.default.svc \
  --from-literal=log_level=debug
```

Or from file:

```bash
kubectl create configmap app-config --from-file=config.ini
```

Mount in Deployment:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: app
spec:
  replicas: 2
  selector:
    matchLabels:
      app: app
  template:
    metadata:
      labels:
        app: app
    spec:
      containers:
      - name: app
        image: myapp:1.0
        volumeMounts:
        - name: config
          mountPath: /etc/config
      volumes:
      - name: config
        configMap:
          name: app-config
```

View ConfigMap:
```bash
kubectl get configmap app-config -o yaml
kubectl describe configmap app-config
```

</details>

---

**Question 10: Set resource requests and limits**

Configure CPU and memory requests/limits for a Deployment to ensure proper scheduling and QoS.

<details>
<summary>Solution</summary>

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: resource-aware-app
spec:
  replicas: 3
  selector:
    matchLabels:
      app: app
  template:
    metadata:
      labels:
        app: app
    spec:
      containers:
      - name: app
        image: nginx
        resources:
          requests:
            memory: "128Mi"
            cpu: "100m"
          limits:
            memory: "256Mi"
            cpu: "500m"
```

**Explanation**:
- `requests`: Guaranteed minimum resources for scheduling
- `limits`: Maximum resources the container can use
- QoS Classes: Guaranteed (requests equals limits), Burstable (requests less than limits), BestEffort (no requests/limits set)

Check resource usage:
```bash
kubectl top pods
kubectl top nodes
```

</details>

### CKA Exam Tips

1. **Time Management**: Allocate ~45 min per question on average. Use imperative commands first, YAML only when necessary.
2. **kubectl Cheat Sheet**: Bookmark `kubernetes.io/docs/reference/kubectl/cheatsheet/` before exam.
3. **Aliases & Autocomplete**: Set `alias k=kubectl` and enable tab completion:
   ```bash
   source <(kubectl completion bash)
   complete -o default -F __start_kubectl k
   ```
4. **Imperative Commands**: Master these:
   ```bash
   kubectl create deployment name --image=image
   kubectl expose deployment/pod --port=8080 --target-port=8080
   kubectl set image deployment/name container=image:tag
   kubectl scale deployment name --replicas=5
   ```
5. **Vim Fundamentals**: Know how to insert, edit, delete, save, quit. Practice daily.
6. **Use --help & --dry-run**:
   ```bash
   kubectl create deployment --help
   kubectl run nginx --image=nginx --dry-run=client -o yaml
   ```
7. **Node Troubleshooting**: Always check `kubectl describe node` first to see conditions.
8. **Practice Speed**: Time yourself solving problems. Target 15-30 min per question.
9. **Cluster Setup**: Practice with kubeadm on VMs (DigitalOcean, AWS, or local VMs).
10. **Context Switching**: Practice switching between clusters and namespaces quickly.
11. **ETCD Snapshots**: Memorize the etcdctl command syntax and certificate paths.
12. **Diagram the Cluster**: Before exam, draw connections between components mentally.

---

## CKAD: Certified Kubernetes Application Developer

### Exam Domains & Weights

The CKAD exam focuses on application deployment and development:

| Domain | Weight | Key Skills |
|--------|--------|-----------|
| Application Design and Build | 20% | Dockerfile, container images, deployments, jobs |
| Application Deployment | 20% | Rolling updates, canary, blue-green, health checks |
| Application Observability and Maintenance | 15% | Logging, monitoring, debugging, troubleshooting |
| Application Environment, Configuration and Security | 25% | Secrets, ConfigMaps, RBAC, security policies, PodSecurityPolicy |
| Services and Networking | 20% | Services, Ingress, network policies, DNS |

### CKAD: Detailed Topics & Key Concepts

#### Application Design and Build (20%)

- **Container Images**: Dockerfile best practices, multi-stage builds, image optimization
- **Image Registries**: Docker Hub, private registries, image pull secrets
- **Jobs & CronJobs**: Completions, parallelism, backoff, cleanup
- **Init Containers**: Sequential initialization, shared volumes
- **Sidecar Pattern**: Logging sidecars, monitoring sidecars

#### Application Deployment (20%)

- **Deployments**: Replicas, strategies (RollingUpdate, Recreate), revisions
- **Rolling Updates**: maxSurge, maxUnavailable, readiness probes
- **Rollbacks**: kubectl rollout history, rollout undo, revision management
- **StatefulSets**: Ordered deployment, persistent identity, scale management
- **DaemonSets**: System components, node-based scheduling
- **Canary & Blue-Green**: Traffic splitting, gradual rollout strategies

#### Application Observability & Maintenance (15%)

- **Logging**: Container logs, multi-container logs, log streams
- **Monitoring**: Metrics server, custom metrics, Prometheus basics
- **Health Checks**: Liveness, readiness, startup probes (exec, HTTP, TCP)
- **Debugging**: kubectl debug, ephemeral containers, exec into pods
- **Events**: Understanding event messages, troubleshooting pods

#### Application Environment, Configuration & Security (25%)

- **Secrets**: Generic secrets, Docker registry secrets, TLS secrets
- **ConfigMaps**: Key-value configuration, file-based config, env vars
- **Security Context**: RunAsUser, fsGroup, capabilities, SELinux
- **RBAC**: Roles, RoleBindings, service accounts
- **PodSecurityPolicy**: Pod security standards, policy enforcement
- **Network Policies**: Ingress/egress rules, default deny policies

#### Services and Networking (20%)

- **Service Types**: ClusterIP, NodePort, LoadBalancer, ExternalName
- **Service Discovery**: DNS, environment variables, headless services
- **Ingress**: Ingress rules, TLS, path-based routing, host-based routing
- **Network Policies**: Pod-to-pod communication, namespace isolation
- **CoreDNS**: Service resolution, custom DNS entries

### CKAD: 10 Practice Questions with Solutions

**Question 1: Create and mount a Secret**

Create a Secret for database credentials and use it in a Deployment as environment variables.

<details>
<summary>Solution</summary>

Create Secret:
```bash
kubectl create secret generic db-creds \
  --from-literal=username=dbuser \
  --from-literal=password=secretpass123
```

Or as YAML:
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: db-creds
type: Opaque
data:
  username: ZGJ1c2Vy  # base64 encoded: dbuser
  password: c2VjcmV0cGFzczEyMw==  # base64 encoded: secretpass123
```

Mount in Deployment:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: app
spec:
  replicas: 2
  selector:
    matchLabels:
      app: app
  template:
    metadata:
      labels:
        app: app
    spec:
      containers:
      - name: app
        image: myapp:1.0
        env:
        - name: DB_USER
          valueFrom:
            secretKeyRef:
              name: db-creds
              key: username
        - name: DB_PASSWORD
          valueFrom:
            secretKeyRef:
              name: db-creds
              key: password
```

Verify:
```bash
kubectl get secret db-creds
kubectl describe secret db-creds
```

</details>

---

**Question 2: Configure health checks (probes)**

Create a Deployment with liveness, readiness, and startup probes.

<details>
<summary>Solution</summary>

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: health-check-app
spec:
  replicas: 2
  selector:
    matchLabels:
      app: app
  template:
    metadata:
      labels:
        app: app
    spec:
      containers:
      - name: app
        image: myapp:1.0
        ports:
        - containerPort: 8080

        # Readiness probe: Is the app ready to serve traffic?
        readinessProbe:
          httpGet:
            path: /ready
            port: 8080
          initialDelaySeconds: 5
          periodSeconds: 10
          timeoutSeconds: 2
          successThreshold: 1
          failureThreshold: 3

        # Liveness probe: Is the app still alive?
        livenessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 15
          periodSeconds: 20
          timeoutSeconds: 2
          failureThreshold: 3

        # Startup probe: Has the app finished starting?
        startupProbe:
          httpGet:
            path: /startup
            port: 8080
          failureThreshold: 30
          periodSeconds: 10
```

**Probe Types**:
- `httpGet`: HTTP request to endpoint
- `exec`: Execute command in container
- `tcpSocket`: TCP connection check

</details>

---

**Question 3: Implement rolling update with custom strategy**

Configure a Deployment with controlled rolling update (maxSurge=1, maxUnavailable=0).

<details>
<summary>Solution</summary>

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: rolling-update-app
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1          # Max 1 extra pod during update
      maxUnavailable: 0    # No pods unavailable (zero-downtime)
  selector:
    matchLabels:
      app: app
  template:
    metadata:
      labels:
        app: app
    spec:
      containers:
      - name: app
        image: myapp:1.0
```

Trigger update and monitor:
```bash
# Update image
kubectl set image deployment/rolling-update-app app=myapp:2.0 --record

# Watch rollout progress
kubectl rollout status deployment/rolling-update-app

# View rollout history
kubectl rollout history deployment/rolling-update-app

# Rollback if needed
kubectl rollout undo deployment/rolling-update-app
```

</details>

---

**Question 4: Create a Job with specific completion count**

Create a Job that completes after 5 successful runs.

<details>
<summary>Solution</summary>

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: batch-job
spec:
  completions: 5        # Job completes after 5 successful pods
  parallelism: 2        # Run 2 pods in parallel
  backoffLimit: 3       # Retry up to 3 times on failure
  activeDeadlineSeconds: 600
  template:
    spec:
      containers:
      - name: job-task
        image: busybox
        command: ["sh", "-c"]
        args: ["echo Processing batch task && exit 0"]
      restartPolicy: Never
```

Monitor Job:
```bash
kubectl get job batch-job
kubectl describe job batch-job
kubectl logs job/batch-job
```

Create CronJob:
```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: daily-backup
spec:
  schedule: "0 2 * * *"  # 2 AM every day
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: backup
            image: backup-tool:1.0
            command: ["/bin/backup.sh"]
          restartPolicy: OnFailure
```

</details>

---

**Question 5: Configure Pod security context**

Create a Pod with security constraints (non-root user, read-only filesystem).

<details>
<summary>Solution</summary>

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: secure-pod
spec:
  securityContext:
    runAsUser: 1000       # Run as user 1000 (non-root)
    runAsGroup: 3000      # Run as group 3000
    fsGroup: 2000         # Shared filesystem group
    fsGroupChangePolicy: OnRootMismatch
    seLinuxOptions:
      level: "s0:c123,c456"

  containers:
  - name: app
    image: myapp:1.0
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities:
        drop:
        - ALL
        add:
        - NET_BIND_SERVICE
    volumeMounts:
    - name: writable
      mountPath: /tmp

  volumes:
  - name: writable
    emptyDir: {}
```

Verify:
```bash
kubectl exec -it secure-pod -- id
```

</details>

---

**Question 6: Create a multi-container Pod with init container**

Design a Pod with init container for setup and two app containers sharing data.

<details>
<summary>Solution</summary>

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: multi-container-app
spec:
  initContainers:
  - name: init-setup
    image: busybox
    command: ['sh', '-c']
    args:
    - |
      mkdir -p /shared-data
      echo "App initialized" > /shared-data/init.txt
      wget -O /shared-data/config.json https://example.com/config.json
    volumeMounts:
    - name: shared
      mountPath: /shared-data

  containers:
  - name: web-server
    image: nginx
    ports:
    - containerPort: 80
    volumeMounts:
    - name: shared
      mountPath: /app-data

  - name: sidecar-logger
    image: busybox
    command: ['sh', '-c']
    args:
    - |
      while true; do
        tail -f /app-data/init.txt
        sleep 5
      done
    volumeMounts:
    - name: shared
      mountPath: /app-data

  volumes:
  - name: shared
    emptyDir: {}
```

Test:
```bash
kubectl apply -f multi-container-pod.yaml
kubectl exec -it multi-container-app -c web-server -- cat /app-data/init.txt
```

</details>

---

**Question 7: Expose Deployment via LoadBalancer service**

Create a Deployment and expose it externally with a LoadBalancer service.

<details>
<summary>Solution</summary>

```bash
# Imperative
kubectl create deployment my-app --image=myapp:1.0 --replicas=3
kubectl expose deployment my-app --type=LoadBalancer --port=80 --target-port=8080
```

Or declaratively:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
spec:
  replicas: 3
  selector:
    matchLabels:
      app: my-app
  template:
    metadata:
      labels:
        app: my-app
    spec:
      containers:
      - name: app
        image: myapp:1.0
        ports:
        - containerPort: 8080
---
apiVersion: v1
kind: Service
metadata:
  name: my-app
spec:
  type: LoadBalancer
  selector:
    app: my-app
  ports:
  - protocol: TCP
    port: 80
    targetPort: 8080
```

Verify:
```bash
kubectl get svc my-app  # View external IP
curl <external-ip>
```

</details>

---

**Question 8: Configure Ingress with path-based routing**

Create an Ingress resource that routes `/api` to one service and `/web` to another.

<details>
<summary>Solution</summary>

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api-service
spec:
  replicas: 2
  selector:
    matchLabels:
      app: api
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
      - name: api
        image: api:1.0
        ports:
        - containerPort: 3000
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web-service
spec:
  replicas: 2
  selector:
    matchLabels:
      app: web
  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
      - name: web
        image: web:1.0
        ports:
        - containerPort: 80
---
apiVersion: v1
kind: Service
metadata:
  name: api-service
spec:
  selector:
    app: api
  ports:
  - port: 3000
    targetPort: 3000
---
apiVersion: v1
kind: Service
metadata:
  name: web-service
spec:
  selector:
    app: web
  ports:
  - port: 80
    targetPort: 80
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: app-ingress
spec:
  ingressClassName: nginx
  rules:
  - host: example.com
    http:
      paths:
      - path: /api
        pathType: Prefix
        backend:
          service:
            name: api-service
            port:
              number: 3000
      - path: /web
        pathType: Prefix
        backend:
          service:
            name: web-service
            port:
              number: 80
```

Test:
```bash
kubectl get ingress app-ingress
curl http://example.com/api
curl http://example.com/web
```

</details>

---

**Question 9: Create a NetworkPolicy for namespace isolation**

Deny all traffic between namespaces except for specific pods.

<details>
<summary>Solution</summary>

Create namespaces:
```bash
kubectl create namespace app1
kubectl create namespace app2
```

Default deny all ingress:
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
  namespace: app1
spec:
  podSelector: {}
  policyTypes:
  - Ingress
  - Egress
```

Allow specific ingress:
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-from-app2
  namespace: app1
spec:
  podSelector:
    matchLabels:
      app: frontend
  policyTypes:
  - Ingress
  ingress:
  - from:
    - namespaceSelector:
        matchLabels:
          name: app2
      podSelector:
        matchLabels:
          app: backend
    ports:
    - protocol: TCP
      port: 8080
```

Label namespaces:
```bash
kubectl label namespace app2 name=app2
```

Test:
```bash
kubectl run -n app1 test-pod --image=busybox --labels=app=frontend
kubectl run -n app2 request-pod --image=busybox
```

</details>

---

**Question 10: Use ResourceQuota to limit namespace usage**

Set resource limits for a namespace to prevent resource exhaustion.

<details>
<summary>Solution</summary>

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: limited-namespace
---
apiVersion: v1
kind: ResourceQuota
metadata:
  name: namespace-quota
  namespace: limited-namespace
spec:
  hard:
    requests.cpu: "10"           # Max 10 CPUs requested
    requests.memory: "20Gi"       # Max 20GB memory requested
    limits.cpu: "20"              # Max 20 CPUs as limits
    limits.memory: "40Gi"         # Max 40GB as limits
    pods: "50"                    # Max 50 pods
    services.loadbalancers: "2"   # Max 2 LoadBalancer services
    services.nodeports: "2"       # Max 2 NodePort services
---
apiVersion: v1
kind: LimitRange
metadata:
  name: namespace-limits
  namespace: limited-namespace
spec:
  limits:
  - max:
      cpu: "2"
      memory: "2Gi"
    min:
      cpu: "100m"
      memory: "128Mi"
    default:
      cpu: "500m"
      memory: "512Mi"
    defaultRequest:
      cpu: "250m"
      memory: "256Mi"
    type: Container
```

Check quota usage:
```bash
kubectl describe quota -n limited-namespace
kubectl describe limitrange -n limited-namespace
```

</details>

### CKAD Exam Tips

1. **Deploy First**: Use `kubectl create` imperative commands for speed.
2. **Know ConfigMap & Secret**: Master mounting as volumes and environment variables.
3. **Probe Syntax**: Memorize livenessProbe, readinessProbe, startupProbe structure.
4. **Rolling Updates**: Understand maxSurge/maxUnavailable and test locally.
5. **Container Logging**: Practice `kubectl logs`, `-c` flag for multi-container, `--previous`.
6. **Security Context**: Know runAsUser, fsGroup, allowPrivilegeEscalation settings.
7. **YAML Generation**: Use `kubectl run --dry-run=client -o yaml` to generate quickly.
8. **Service Networking**: Practice creating Services and accessing via DNS.
9. **Health Checks**: Implement all three probe types (startup, readiness, liveness).
10. **Debug Pods**: Master `kubectl describe`, `kubectl exec`, `kubectl logs`.
11. **Copy & Paste**: Use `kubectl get pod -o yaml` as template base, edit quickly.
12. **Watch Deployments**: Monitor rollouts with `kubectl rollout status` and `watch`.

---

## CKS: Certified Kubernetes Security Specialist

### Exam Domains & Weights

The CKS exam is the most advanced, focusing entirely on security:

| Domain | Weight | Key Skills |
|--------|--------|-----------|
| Cluster Setup | 10% | Secure API access, kubelet config, network policies |
| Cluster Hardening | 15% | RBAC, admission controllers, Pod Security Policies |
| System Hardening | 15% | OS hardening, kernel parameters, AppArmor, SELinux |
| Minimize Microservice Vulnerabilities | 20% | Security contexts, network policies, image scanning |
| Supply Chain Security | 20% | Image signing, artifact verification, registry security |
| Monitoring, Logging & Runtime Security | 20% | Audit logging, runtime monitoring, intrusion detection |

### CKS: Detailed Topics & Key Concepts

#### Cluster Setup (10%)

- **API Server Security**: Authentication methods, authorization modes, certificate validation
- **Kubelet Configuration**: Webhook authorization, read-only port, event rate limiting
- **Network Policies**: Deny-all default, ingress/egress rules, advanced selectors
- **Secret Encryption**: etcd encryption, envelope encryption, key rotation

#### Cluster Hardening (15%)

- **RBAC**: Principle of least privilege, role segregation, service account binding
- **Admission Controllers**: PodSecurityPolicy, NetworkPolicy validation, image validation
- **API Server Flags**: `--authorization-mode`, `--admission-control`, `--enable-audit-log`
- **Certificate Management**: Certificate rotation, certificate pinning, CA security

#### System Hardening (15%)

- **OS Hardening**: Kernel parameters, SELinux/AppArmor enforcement, container runtime security
- **Node Security**: Kubelet anonymous auth disabled, API server audit logging
- **Minimize Attack Surface**: Disable unused services, remove unnecessary binaries

#### Minimize Microservice Vulnerabilities (20%)

- **Security Context**: Privileged/unprivileged containers, read-only filesystems, capability dropping
- **Network Policies**: Microsegmentation, zero-trust networking, egress filtering
- **RBAC for Workloads**: Service accounts, RBAC rules limiting container permissions
- **Image Security**: Scanning, signing, source verification, private registries

#### Supply Chain Security (20%)

- **Image Signing & Verification**: Notary, Cosign, binary authorization
- **Container Registry Security**: Private registries, authentication, access control
- **Build Pipeline Security**: Source code scanning, dependency management, artifact signing
- **Runtime Security**: Falco, runtime policies, behavior monitoring

#### Monitoring, Logging & Runtime Security (20%)

- **Audit Logging**: Audit policy, event logging, log analysis
- **Runtime Monitoring**: Falco rules, behavioral baselines, anomaly detection
- **Intrusion Detection**: Network traffic analysis, suspicious activity detection
- **Log Aggregation**: ELK stack, Datadog, Prometheus alerts

### CKS: 5 Practice Questions with Solutions

**Question 1: Enable API server audit logging**

Configure the API server to log all requests to create/delete resources.

<details>
<summary>Solution</summary>

Create audit policy file at `/etc/kubernetes/audit-policy.yaml`:

```yaml
apiVersion: audit.k8s.io/v1
kind: Policy
rules:
# Log pod exec at RequestResponse level
- level: RequestResponse
  verbs: ["create", "update", "patch", "delete"]
  resources: ["pods", "pods/exec", "pods/attach"]
  omitStages:
  - RequestReceived

# Log all API calls at Metadata level
- level: Metadata
  omitStages:
  - RequestReceived

# Log system requests at basic level
- level: Basic
  userGroups: ["system:authenticated"]
  omitStages:
  - RequestReceived
```

Update `/etc/kubernetes/manifests/kube-apiserver.yaml`:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: kube-apiserver
  namespace: kube-system
spec:
  containers:
  - name: kube-apiserver
    image: k8s.gcr.io/kube-apiserver:v1.24.0
    command:
    - kube-apiserver
    - --audit-policy-file=/etc/kubernetes/audit-policy.yaml
    - --audit-log-path=/var/log/kubernetes/audit.log
    - --audit-log-maxage=30
    - --audit-log-maxbackup=10
    - --audit-log-maxsize=100
    volumeMounts:
    - name: audit
      mountPath: /etc/kubernetes
    - name: audit-log
      mountPath: /var/log/kubernetes
  volumes:
  - name: audit
    hostPath:
      path: /etc/kubernetes
  - name: audit-log
    hostPath:
      path: /var/log/kubernetes
```

Verify:
```bash
kubectl logs -n kube-system kube-apiserver
tail -f /var/log/kubernetes/audit.log
```

</details>

---

**Question 2: Implement Pod Security Policy**

Create a Pod Security Policy that prevents privileged containers and enforces read-only filesystems.

<details>
<summary>Solution</summary>

```yaml
apiVersion: policy/v1beta1
kind: PodSecurityPolicy
metadata:
  name: restricted-psp
spec:
  privileged: false
  allowPrivilegeEscalation: false
  requiredDropCapabilities:
  - ALL
  volumes:
  - 'configMap'
  - 'emptyDir'
  - 'projected'
  - 'secret'
  - 'downwardAPI'
  - 'persistentVolumeClaim'
  hostNetwork: false
  hostIPC: false
  hostPID: false
  runAsUser:
    rule: 'MustRunAsNonRoot'
  seLinux:
    rule: 'MustRunAs'
    seLinuxOptions:
      level: "s0:c123,c456"
  fsGroup:
    rule: 'MustRunAs'
    ranges:
    - min: 1000
      max: 65535
  readOnlyRootFilesystem: true
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: restricted-psp-user
rules:
- apiGroups: ['policy']
  resources: ['podsecuritypolicies']
  verbs: ['use']
  resourceNames:
  - restricted-psp
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: restricted-psp-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: restricted-psp-user
subjects:
- kind: Group
  name: system:authenticated
  apiGroup: rbac.authorization.k8s.io
```

Enable PSP admission controller in API server flags:
```bash
--enable-admission-plugins=PodSecurityPolicy,RBAC
```

</details>

---

**Question 3: Create a Network Policy for zero-trust networking**

Design a network policy that requires explicit allow rules for all traffic.

<details>
<summary>Solution</summary>

```yaml
# Step 1: Default deny all traffic
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
  namespace: production
spec:
  podSelector: {}
  policyTypes:
  - Ingress
  - Egress
---
# Step 2: Allow frontend pods to receive traffic on port 8080
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-frontend-ingress
  namespace: production
spec:
  podSelector:
    matchLabels:
      tier: frontend
  policyTypes:
  - Ingress
  ingress:
  - from:
    - podSelector:
        matchLabels:
          tier: client
    ports:
    - protocol: TCP
      port: 8080
---
# Step 3: Allow frontend to communicate with backend
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-frontend-to-backend
  namespace: production
spec:
  podSelector:
    matchLabels:
      tier: frontend
  policyTypes:
  - Egress
  egress:
  - to:
    - podSelector:
        matchLabels:
          tier: backend
    ports:
    - protocol: TCP
      port: 3000
---
# Step 4: Allow backend to communicate with database
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-backend-to-db
  namespace: production
spec:
  podSelector:
    matchLabels:
      tier: backend
  policyTypes:
  - Egress
  egress:
  - to:
    - namespaceSelector:
        matchLabels:
          name: database
    ports:
    - protocol: TCP
      port: 5432
---
# Step 5: Allow DNS egress for all pods
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-dns-egress
  namespace: production
spec:
  podSelector: {}
  policyTypes:
  - Egress
  egress:
  - to:
    - namespaceSelector: {}
      podSelector:
        matchLabels:
          k8s-app: kube-dns
    ports:
    - protocol: UDP
      port: 53
```

Test connectivity:
```bash
# DNS should work
kubectl exec -it frontend-pod -- nslookup backend-service

# Test service communication
kubectl exec -it frontend-pod -- curl backend-service:3000
```

</details>

---

**Question 4: Scan container images and prevent vulnerable image deployment**

Implement image scanning and admission control to block images with critical vulnerabilities.

<details>
<summary>Solution</summary>

Use Trivy to scan images:

```bash
# Install Trivy
curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh -s -- -b /usr/local/bin

# Scan image
trivy image nginx:1.14

# Generate SBOM
trivy image --format json nginx:1.14 > sbom.json
```

Create ValidatingWebhookConfiguration to block images with HIGH/CRITICAL vulnerabilities:

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingWebhookConfiguration
metadata:
  name: image-security-webhook
webhooks:
- name: image-security.example.com
  clientConfig:
    service:
      name: image-security-service
      namespace: security
      path: "/validate"
    caBundle: LS0tLS1CRUdJTi... # base64 encoded CA cert
  rules:
  - operations: ["CREATE", "UPDATE"]
    apiGroups: [""]
    apiVersions: ["v1"]
    resources: ["pods"]
    scope: "Namespaced"
  admissionReviewVersions: ["v1"]
  sideEffects: None
  timeoutSeconds: 5
  failurePolicy: Fail
```

Build custom webhook that:
1. Extracts image name from pod spec
2. Runs Trivy scan
3. Rejects if HIGH or CRITICAL vulnerabilities found
4. Allows if MEDIUM or LOW only

</details>

---

**Question 5: Configure kubelet security hardening**

Secure the kubelet with minimal attack surface.

<details>
<summary>Solution</summary>

Edit `/etc/kubernetes/kubelet.conf` or kubelet flags:

```yaml
# Disable anonymous auth
--anonymous-auth=false

# Enable webhook authorization
--authorization-mode=Webhook

# Enable webhook authn
--authentication-token-webhook=true

# Disable read-only port
--read-only-port=0

# Enable audit logging
--audit-log-maxage=30
--audit-log-maxbackup=10
--audit-log-maxsize=100

# Restrict pod access
--protect-kernel-defaults=true
--make-iptables-util-chains=true

# Kubelet certificate rotation
--rotate-certificates=true
--rotate-server-certificates=true

# Event rate limiting
--event-qps=5
```

Systemd service file at `/etc/systemd/system/kubelet.service.d/10-kubeadm.conf`:

```ini
[Service]
Environment="KUBELET_EXTRA_ARGS=--anonymous-auth=false --authorization-mode=Webhook"
```

Apply changes:
```bash
sudo systemctl daemon-reload
sudo systemctl restart kubelet
```

Verify:
```bash
systemctl status kubelet
sudo journalctl -u kubelet -f
```

</details>

### CKS Exam Tips

1. **Understand Each Security Domain**: Security is broad; know implications of each setting.
2. **RBAC Mastery**: Test RBAC rules with `kubectl auth can-i` frequently.
3. **Network Policies First**: Always implement deny-all first, then allow explicit rules.
4. **PSP vs PodSecurityPolicy**: Know both old PSP and new Pod Security Standards (PSS).
5. **Audit Logging**: Memorize audit policy syntax and API server flags.
6. **Image Security**: Practice Trivy, image signing, private registry setup.
7. **Falco Rules**: Understand runtime monitoring and Falco rule syntax basics.
8. **SELinux/AppArmor**: Know how to check, enable, and troubleshoot enforcement modes.
9. **kubelet Hardening**: Know critical flags by memory (anonymous-auth, authorization-mode).
10. **Encryption**: Understand etcd encryption, secret encryption, certificate validation.

---

## General Kubernetes Exam Tips

### 1. Time Management

- **Allocate time wisely**: 120 minutes ÷ 15-20 questions = 6-8 minutes per question average
- **Triage questions**: Read all questions first; do quick ones first, hard ones later
- **Use imperative commands**: `kubectl create` is faster than writing YAML from scratch
- **Dry-run & generate**: Use `--dry-run=client -o yaml` to build YAML templates quickly

### 2. kubectl Mastery

Essential commands to master:

```bash
# Imperative Pod/Deployment creation
kubectl run nginx --image=nginx
kubectl create deployment app --image=app:1.0 --replicas=3
kubectl expose deployment app --port=8080 --target-port=8080 --type=ClusterIP

# Get/Describe
kubectl get pods -o wide
kubectl describe pod <pod-name>
kubectl describe node <node-name>

# Logs & Exec
kubectl logs <pod-name>
kubectl logs <pod-name> -c <container-name>
kubectl exec -it <pod-name> -- /bin/bash

# Update/Delete
kubectl set image deployment/app app=app:2.0 --record
kubectl set resources deployment/app --limits=cpu=500m,memory=256Mi
kubectl delete deployment app

# Port forwarding & proxying
kubectl port-forward pod/nginx 8080:80
kubectl proxy --port=8001

# Help & Explanations
kubectl explain deployment.spec
kubectl api-resources
```

### 3. Aliases & Autocomplete

Set up quickly in exam:

```bash
alias k=kubectl
alias kg='k get'
alias kd='k describe'
alias kl='k logs'
alias ke='k exec -it'
alias ka='k apply -f'
alias kdel='k delete'

# Tab completion
source <(kubectl completion bash)
complete -o default -F __start_kubectl k
```

### 4. Bookmarks for Kubernetes Documentation

Allowed during exam (kubernetes.io only):

- `kubernetes.io/docs/reference/kubectl/` — kubectl reference
- `kubernetes.io/docs/concepts/` — architectural concepts
- `kubernetes.io/docs/tasks/` — how-to guides
- `kubernetes.io/docs/reference/api-docs/` — API documentation
- `kubernetes.io/docs/reference/kubernetes-api/` — API resources

### 5. Vim Essentials

Practice these Vim commands daily:

```vim
" Insert mode
i       Insert before cursor
a       Insert after cursor
o       New line below
O       New line above

" Normal mode
dd      Delete line
yy      Copy line
p       Paste after
P       Paste before
:w      Save
:q      Quit
:wq     Save and quit
:q!     Quit without saving

" Find & Replace
:/text  Find
:s/old/new  Replace in line
:%s/old/new/g  Replace all

" Navigation
G       Go to end
gg      Go to start
:set number  Show line numbers
```

### 6. Practice with Real Clusters

Set up local test environments:

**Option 1: Minikube** (single-node, good for learning)
```bash
minikube start --vm-driver=kvm2
minikube dashboard
```

**Option 2: Kind** (Kubernetes in Docker, fast)
```bash
kind create cluster
kind create cluster --config=multi-node.yaml
```

**Option 3: Kubeadm on VMs** (closest to real exam)
```bash
# On control plane
kubeadm init --pod-network-cidr=10.244.0.0/16

# On worker nodes
kubeadm join 10.0.0.1:6443 --token <token> --discovery-token-ca-cert-hash <hash>
```

### 7. Speed Techniques

Speed up your workflow:

```bash
# Generate YAML quickly
kubectl run nginx --image=nginx --dry-run=client -o yaml > pod.yaml
kubectl create deployment app --image=app --replicas=3 --dry-run=client -o yaml

# Copy existing resources as templates
kubectl get pod existing-pod -o yaml > new-pod.yaml
# Edit and apply

# Use kubectl explain for structure
kubectl explain deployment.spec.template.spec
kubectl explain pv.spec

# Diff before applying
kubectl diff -f file.yaml

# Watch changes in real-time
watch -n 1 'kubectl get pods -o wide'
kubectl rollout status deployment/app
```

### 8. Debugging Workflow

Systematic approach to troubleshooting:

```bash
# 1. Check pod status
kubectl get pods -o wide
kubectl describe pod <pod-name>

# 2. Check events
kubectl get events --sort-by='.lastTimestamp'

# 3. View logs
kubectl logs <pod-name>
kubectl logs <pod-name> --previous
kubectl logs <pod-name> -f  # Follow logs

# 4. Execute commands in pod
kubectl exec -it <pod-name> -- bash
kubectl exec <pod-name> -- cat /var/log/app.log

# 5. Check node status
kubectl describe node <node-name>
kubectl get nodes -o wide

# 6. Check resources
kubectl top pods
kubectl top nodes

# 7. Check cluster health
kubectl get componentstatuses
kubectl cluster-info
```

### 9. Common Pitfalls to Avoid

- **Don't forget labels**: Always add labels to resources for easier filtering
- **Namespace matters**: Remember `-n namespace` flag; use contexts to switch
- **YAML indentation**: 2 spaces, not tabs; verify with `kubectl apply --dry-run=client`
- **Resource names**: Must be lowercase alphanumeric + hyphens
- **Image pull errors**: Ensure images exist, registry credentials are correct
- **Port mismatches**: containerPort ≠ service port; verify with `kubectl describe svc`
- **Probes timing**: initialDelaySeconds should allow time for app startup
- **Node affinity**: Test with `kubectl get nodes --show-labels`
- **Context switching**: Verify current context with `kubectl config current-context`

### 10. Practice Exams & Resources

Practice regularly before attempting the real exam:

- **Killer.sh** (official CKA/CKAD/CKS labs): Most realistic practice
- **Linux Academy/A Cloud Guru**: Structured courses with labs
- **Kubernetes Official Docs**: Read all example YAML files
- **GitHub Repositories**: Search for "CKA exam repo" or "CKAD practice"

---

## Study Resources

| Resource | Type | Cost | Best For |
|----------|------|------|----------|
| **Kubernetes Official Docs** | Free | Free | Reference material, hands-on tasks |
| **Killer.sh** | Practice Exams | $30-50 | Most realistic CKA/CKAD/CKS simulation |
| **Linux Academy / A Cloud Guru** | Video Courses + Labs | $49-99/month | Structured learning with instructor guidance |
| **Kodekloud** | Interactive Labs | $10-20/month | Hands-on lab environment, quick drills |
| **CKA Study Guide (Nigel Poulton)** | Book | $35-40 | Deep conceptual understanding |
| **Kubernetes in Action** | Book | $40-50 | Comprehensive reference for all topics |
| **Mumshad's CKA Course** | Video Course | $10-15 | Excellent CKA/CKAD curriculum |
| **YouTube: Kubesimplify** | Free Videos | Free | Practical walkthroughs and tips |
| **Practice Clusters (minikube/kind)** | Free | Free | Local testing environment |

---

## Practice Labs

### Lab 1: Build a Cluster from Scratch with kubeadm

**Objective**: Initialize a Kubernetes cluster with kubeadm, add a worker node, and verify health.

**Environment**: 2 VMs (Ubuntu 20.04+, 2GB+ RAM each)

**Steps**:

```bash
# On both VMs: Disable swap and install dependencies
sudo swapoff -a
sudo apt-get update
sudo apt-get install -y docker.io kubeadm kubelet kubectl
sudo systemctl start docker kubelet

# On control plane VM:
sudo kubeadm init --pod-network-cidr=10.244.0.0/16

# Setup kubeconfig
mkdir -p $HOME/.kube
sudo cp /etc/kubernetes/admin.conf $HOME/.kube/config
sudo chown $USER:$USER $HOME/.kube/config

# Install CNI plugin (Flannel)
kubectl apply -f https://raw.githubusercontent.com/coreos/flannel/master/Documentation/kube-flannel.yml

# On worker node: Join cluster
kubeadm join <control-plane-ip>:6443 --token <token> --discovery-token-ca-cert-hash <hash>

# Back on control plane: Verify nodes
kubectl get nodes
kubectl get pods -A
```

**Verification**:
- All nodes should be `Ready`
- kube-system pods should be `Running`
- `kubectl cluster-info` should show API server, controller-manager, scheduler

### Lab 2: Troubleshoot a Broken Deployment

**Scenario**: A deployment is failing. Diagnose and fix three issues: bad image, wrong port, insufficient resources.

**Setup**:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: broken-app
spec:
  replicas: 2
  selector:
    matchLabels:
      app: broken-app
  template:
    metadata:
      labels:
        app: broken-app
    spec:
      containers:
      - name: app
        image: myapp:invalid-tag  # Issue 1: Wrong image
        ports:
        - containerPort: 3000
        resources:
          requests:
            cpu: "10"  # Issue 2: Unreasonable resource request
            memory: "50Gi"
---
apiVersion: v1
kind: Service
metadata:
  name: broken-app
spec:
  selector:
    app: broken-app
  ports:
  - port: 8080
    targetPort: 9000  # Issue 3: Wrong target port
  type: ClusterIP
```

**Troubleshooting Steps**:

```bash
# 1. Check pod status
kubectl describe deployment broken-app
kubectl describe pod <pod-name>

# 2. Identify issues
# - ImagePullBackOff: Image doesn't exist
# - Insufficient resources: Node can't allocate requested CPU/memory
# - Port mismatch: Service forwarding to wrong port

# 3. Fix issues
kubectl set image deployment/broken-app app=nginx:1.14
kubectl set resources deployment/broken-app --requests=cpu=100m,memory=128Mi
kubectl patch service broken-app -p '{"spec":{"ports":[{"port":8080,"targetPort":80}]}}'

# 4. Verify fixes
kubectl rollout status deployment/broken-app
kubectl get pods -o wide
```

### Lab 3: Implement RBAC for Multi-Tenant Cluster

**Objective**: Create two namespaces, service accounts, and RBAC roles to isolate team permissions.

**Setup**:

```bash
# Create namespaces
kubectl create namespace team-a
kubectl create namespace team-b

# Create service accounts
kubectl create serviceaccount team-a-user -n team-a
kubectl create serviceaccount team-b-user -n team-b
```

**RBAC Configuration**:

```yaml
# Role for team-a: can manage pods and deployments only
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: team-a-role
  namespace: team-a
rules:
- apiGroups: [""]
  resources: ["pods", "pods/logs"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["apps"]
  resources: ["deployments", "deployments/scale"]
  verbs: ["get", "list", "create", "update", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: team-a-binding
  namespace: team-a
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: team-a-role
subjects:
- kind: ServiceAccount
  name: team-a-user
  namespace: team-a
---
# Same for team-b with different permissions
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: team-b-role
  namespace: team-b
rules:
- apiGroups: [""]
  resources: ["configmaps", "secrets"]
  verbs: ["get", "list", "create", "update"]
- apiGroups: ["apps"]
  resources: ["statefulsets"]
  verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: team-b-binding
  namespace: team-b
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: team-b-role
subjects:
- kind: ServiceAccount
  name: team-b-user
  namespace: team-b
```

**Test RBAC**:

```bash
# Verify permissions
kubectl auth can-i get pods --as=system:serviceaccount:team-a:team-a-user -n team-a
kubectl auth can-i create services --as=system:serviceaccount:team-a:team-a-user -n team-a

# team-b should NOT access team-a resources
kubectl auth can-i get pods --as=system:serviceaccount:team-b:team-b-user -n team-a
```

### Lab 4: Implement NetworkPolicy for Namespace Isolation

**Objective**: Create network policies to isolate `prod` and `dev` namespaces while allowing managed cross-namespace communication.

**Setup**:

```bash
# Create namespaces with labels
kubectl create namespace prod
kubectl label namespace prod tier=production
kubectl create namespace dev
kubectl label namespace dev tier=development
```

**Network Policies**:

```yaml
# Deny all ingress by default in prod
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
  namespace: prod
spec:
  podSelector: {}
  policyTypes:
  - Ingress
---
# Allow ingress only from ingress-nginx controller
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-ingress-controller
  namespace: prod
spec:
  podSelector:
    matchLabels:
      app: web
  policyTypes:
  - Ingress
  ingress:
  - from:
    - namespaceSelector:
        matchLabels:
          name: ingress-nginx
    ports:
    - protocol: TCP
      port: 8080
---
# Allow frontend in prod to talk to backend in prod
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-intra-communication
  namespace: prod
spec:
  podSelector:
    matchLabels:
      app: frontend
  policyTypes:
  - Egress
  egress:
  - to:
    - podSelector:
        matchLabels:
          app: backend
    ports:
    - protocol: TCP
      port: 3000
---
# Allow pods to reach CoreDNS for DNS resolution
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-dns
  namespace: prod
spec:
  podSelector: {}
  policyTypes:
  - Egress
  egress:
  - to:
    - namespaceSelector:
        matchLabels:
          name: kube-system
      podSelector:
        matchLabels:
          k8s-app: kube-dns
    ports:
    - protocol: UDP
      port: 53
```

**Test Connectivity**:

```bash
# Deploy test pods
kubectl run frontend --image=busybox --labels=app=frontend -n prod -- sleep 1000
kubectl run backend --image=busybox --labels=app=backend -n prod -- sleep 1000

# Test: Frontend should reach backend, dev should not reach prod
kubectl exec -it frontend -n prod -- wget -O- http://backend:3000
kubectl exec -it testpod -n dev -- wget -O- http://backend.prod.svc:3000
# ^ Should timeout
```

### Lab 5: Configure Persistent Storage for StatefulSet

**Objective**: Deploy a StatefulSet with persistent volumes for a database.

**Setup**:

```yaml
# StorageClass for dynamic provisioning
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: fast-storage
provisioner: kubernetes.io/no-provisioner
volumeBindingMode: WaitForFirstConsumer
---
# PersistentVolumes (static provisioning)
apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-1
spec:
  capacity:
    storage: 10Gi
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: fast-storage
  hostPath:
    path: /mnt/data/pv-1
---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-2
spec:
  capacity:
    storage: 10Gi
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: fast-storage
  hostPath:
    path: /mnt/data/pv-2
---
# StatefulSet with persistent storage
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: mysql-db
spec:
  serviceName: mysql-headless  # Required for StatefulSet
  replicas: 2
  selector:
    matchLabels:
      app: mysql
  template:
    metadata:
      labels:
        app: mysql
    spec:
      containers:
      - name: mysql
        image: mysql:5.7
        env:
        - name: MYSQL_ROOT_PASSWORD
          valueFrom:
            secretKeyRef:
              name: mysql-secret
              key: root-password
        ports:
        - containerPort: 3306
          name: mysql
        volumeMounts:
        - name: mysql-data
          mountPath: /var/lib/mysql
  # Persistent volume claim template
  volumeClaimTemplates:
  - metadata:
      name: mysql-data
    spec:
      accessModes: ["ReadWriteOnce"]
      storageClassName: fast-storage
      resources:
        requests:
          storage: 10Gi
---
# Headless Service for StatefulSet
apiVersion: v1
kind: Service
metadata:
  name: mysql-headless
spec:
  clusterIP: None  # Headless service
  selector:
    app: mysql
  ports:
  - port: 3306
    targetPort: 3306
---
# Regular Service for client access
apiVersion: v1
kind: Service
metadata:
  name: mysql
spec:
  selector:
    app: mysql
  ports:
  - port: 3306
    targetPort: 3306
  type: ClusterIP
---
# Secret for MySQL root password
apiVersion: v1
kind: Secret
metadata:
  name: mysql-secret
type: Opaque
stringData:
  root-password: "rootpassword123"
```

**Deployment & Verification**:

```bash
# Create directories on node for hostPath volumes
mkdir -p /mnt/data/pv-{1,2}
chmod 777 /mnt/data/pv-{1,2}

# Deploy StatefulSet
kubectl apply -f statefulset.yaml

# Wait for pods
kubectl get statefulset mysql-db
kubectl get pods -o wide

# Verify persistent volumes
kubectl get pv
kubectl get pvc

# Test database persistence
kubectl exec -it mysql-db-0 -- mysql -u root -prootpassword123 -e "CREATE TABLE test (id INT); INSERT INTO test VALUES (1);"

# Scale up StatefulSet
kubectl scale statefulset mysql-db --replicas=3

# Verify new pod gets new PVC
kubectl get pvc

# Delete a pod; StatefulSet creates new one with same name and PVC
kubectl delete pod mysql-db-0

# Data persists
kubectl exec -it mysql-db-0 -- mysql -u root -prootpassword123 -e "SELECT * FROM test;"
```

---

## Quick Reference: Command Cheatsheet

```bash
# Context & Cluster
kubectl config current-context
kubectl config use-context cluster-name
kubectl cluster-info
kubectl get nodes -o wide

# Pods
kubectl run <name> --image=image
kubectl get pods -n namespace
kubectl describe pod <name>
kubectl logs <name>
kubectl exec -it <name> -- bash
kubectl delete pod <name>

# Deployments
kubectl create deployment <name> --image=image --replicas=3
kubectl get deployment
kubectl set image deployment/<name> app=image:v2
kubectl rollout status deployment/<name>
kubectl rollout history deployment/<name>
kubectl rollout undo deployment/<name>

# Services & Networking
kubectl expose pod/deployment <name> --port=8080 --target-port=80
kubectl get svc
kubectl port-forward svc/<name> 8080:80
kubectl get ingress

# Config & Storage
kubectl create configmap <name> --from-file=config.yaml
kubectl create secret generic <name> --from-literal=key=value
kubectl get pv, pvc

# RBAC
kubectl create serviceaccount <name>
kubectl create role <name> --verb=create,get --resource=pods
kubectl create rolebinding <name> --role=role --serviceaccount=ns:sa
kubectl auth can-i <action> --as=sa:default

# Troubleshooting
kubectl describe <resource> <name>
kubectl get events
kubectl top pods
kubectl top nodes
```

---

**Master these resources and labs, and you'll be well-prepared for Kubernetes certification success!**
