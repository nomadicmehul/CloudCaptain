---
title: "kubectl Cheat Sheet"
sidebar_label: "Cheat Sheet"
description: "Complete kubectl command reference — 200+ commands for pods, deployments, services, debugging, and cluster management"
sidebar_position: 7
---

# kubectl Cheat Sheet

A comprehensive reference guide for kubectl commands, covering cluster management, resource operations, debugging, and advanced workflows. Perfect for daily Kubernetes work and interview preparation.

## Cluster Information

Get cluster details, version info, and manage kubeconfig contexts.

| Command | Description |
|---------|-------------|
| `kubectl cluster-info` | Display cluster master and services endpoint |
| `kubectl cluster-info dump` | Dump cluster state for debugging |
| `kubectl version` | Display kubectl and server API version |
| `kubectl version --short` | Short version output |
| `kubectl config view` | Display merged kubeconfig settings |
| `kubectl config view --raw` | Display raw kubeconfig (includes base64 secrets) |
| `kubectl config current-context` | Show current context |
| `kubectl config get-contexts` | List all contexts |
| `kubectl config use-context <context>` | Switch to specified context |
| `kubectl config set-context <context> --namespace=<ns>` | Set default namespace for context |
| `kubectl config set-cluster <cluster> --server=<URL>` | Set cluster server URL |
| `kubectl config delete-context <context>` | Delete context from kubeconfig |
| `kubectl config rename-context <old> <new>` | Rename context |
| `kubectl auth can-i create pods` | Check if current user can create pods |
| `kubectl auth can-i delete nodes --as=<user>` | Check permissions for specific user |

## Pods

Create, manage, and troubleshoot pods.

| Command | Description |
|---------|-------------|
| `kubectl get pods` | List all pods in default namespace |
| `kubectl get pods -n <namespace>` | List pods in specific namespace |
| `kubectl get pods --all-namespaces` | List pods across all namespaces |
| `kubectl get pods -o wide` | List pods with more details (node, IP) |
| `kubectl get pods --show-labels` | List pods with labels |
| `kubectl get pods --selector=<label>=<value>` | Filter pods by label |
| `kubectl get pods -w` | Watch pods in real-time |
| `kubectl describe pod <pod>` | Show detailed pod information |
| `kubectl describe pod <pod> -n <namespace>` | Describe pod in specific namespace |
| `kubectl create pod <name> --image=<image>` | Create pod from command line |
| `kubectl run <name> --image=<image>` | Run temporary pod (deprecated for create) |
| `kubectl run <name> --image=<image> --restart=Never` | Create single-run pod |
| `kubectl run <name> --image=<image> -i -t --rm -- /bin/sh` | Run interactive pod, delete on exit |
| `kubectl delete pod <pod>` | Delete pod immediately |
| `kubectl delete pod <pod> --grace-period=0 --force` | Force delete pod |
| `kubectl delete pods --all` | Delete all pods in namespace |
| `kubectl delete pod -l <label>=<value>` | Delete pods matching label |
| `kubectl exec -it <pod> -- /bin/bash` | Execute command in pod |
| `kubectl exec -it <pod> -c <container> -- /bin/sh` | Execute in specific container |
| `kubectl logs <pod>` | View pod logs |
| `kubectl logs <pod> -c <container>` | View logs from specific container |
| `kubectl logs <pod> --previous` | View logs from previous pod instance |
| `kubectl logs <pod> --tail=50` | View last 50 lines of logs |
| `kubectl logs <pod> -f` | Stream logs (follow) |
| `kubectl logs <pod> --timestamps=true` | Show timestamps in logs |
| `kubectl logs -l <label>=<value>` | View logs from pods matching label |
| `kubectl logs <pod> --all-containers=true` | View logs from all containers |
| `kubectl port-forward <pod> <local>:<remote>` | Forward local port to pod port |
| `kubectl port-forward svc/<service> <local>:<remote>` | Forward port to service |
| `kubectl cp <namespace>/<pod>:/path /local/path` | Copy file from pod to local |
| `kubectl cp /local/file <namespace>/<pod>:/path` | Copy file from local to pod |
| `kubectl cp <pod>:/path/file - \| tar xvf -` | Copy directory from pod |
| `kubectl top pod` | Show CPU and memory usage of pods |
| `kubectl top pod --containers` | Show per-container resource usage |
| `kubectl top pod <pod>` | Show usage for specific pod |
| `kubectl annotate pod <pod> <key>=<value>` | Add annotation to pod |
| `kubectl label pod <pod> <key>=<value>` | Add label to pod |
| `kubectl label pod <pod> <key>-` | Remove label from pod |
| `kubectl patch pod <pod> -p '{"spec":{"containers":[{"name":"<container>","image":"<image>"}]}}'` | Patch pod definition |

## Deployments

Create and manage application deployments with rolling updates.

| Command | Description |
|---------|-------------|
| `kubectl create deployment <name> --image=<image>` | Create deployment |
| `kubectl create deployment <name> --image=<image> --replicas=3` | Create deployment with replicas |
| `kubectl get deployments` | List all deployments |
| `kubectl get deployment <name>` | Get specific deployment |
| `kubectl get deployment -o wide` | List deployments with more details |
| `kubectl describe deployment <name>` | Show deployment details |
| `kubectl edit deployment <name>` | Edit deployment in editor |
| `kubectl set image deployment/<name> <container>=<image>` | Update container image |
| `kubectl set image deployment/<name> <c1>=<img1>,<c2>=<img2>` | Update multiple container images |
| `kubectl scale deployment <name> --replicas=5` | Scale deployment to 5 replicas |
| `kubectl scale deployment -l app=<app> --replicas=3` | Scale deployments by label |
| `kubectl autoscale deployment <name> --min=2 --max=10` | Create horizontal pod autoscaler |
| `kubectl rollout status deployment/<name>` | Check rollout status |
| `kubectl rollout history deployment/<name>` | View rollout history |
| `kubectl rollout history deployment/<name> --revision=2` | View specific revision details |
| `kubectl rollout undo deployment/<name>` | Rollback to previous revision |
| `kubectl rollout undo deployment/<name> --to-revision=2` | Rollback to specific revision |
| `kubectl rollout pause deployment/<name>` | Pause deployment rollout |
| `kubectl rollout resume deployment/<name>` | Resume deployment rollout |
| `kubectl delete deployment <name>` | Delete deployment |
| `kubectl delete deployment --all` | Delete all deployments in namespace |

## Services

Expose applications and manage network access.

| Command | Description |
|---------|-------------|
| `kubectl get services` | List all services |
| `kubectl get svc` | Short form: list services |
| `kubectl get svc -o wide` | List services with endpoints |
| `kubectl get svc <service>` | Get specific service details |
| `kubectl describe svc <service>` | Show service details |
| `kubectl create service clusterip <name> --tcp=8080:8080` | Create ClusterIP service |
| `kubectl create service nodeport <name> --tcp=8080:8080` | Create NodePort service |
| `kubectl create service loadbalancer <name> --tcp=8080:8080` | Create LoadBalancer service |
| `kubectl expose deployment <name> --port=8080 --type=LoadBalancer` | Expose deployment as service |
| `kubectl expose pod <pod> --port=8080 --target-port=8080` | Expose pod as service |
| `kubectl edit svc <service>` | Edit service definition |
| `kubectl patch svc <service> -p '{"spec":{"type":"NodePort"}}'` | Patch service |
| `kubectl delete svc <service>` | Delete service |
| `kubectl get endpoints <service>` | View service endpoints |
| `kubectl get svc -o jsonpath='{.items[*].status.loadBalancer.ingress[*].ip}'` | Get LoadBalancer external IPs |

## StatefulSets

Manage stateful applications.

| Command | Description |
|---------|-------------|
| `kubectl create statefulset <name> --image=<image>` | Create StatefulSet |
| `kubectl get statefulsets` | List all StatefulSets |
| `kubectl get sts` | Short form: list StatefulSets |
| `kubectl describe sts <name>` | Show StatefulSet details |
| `kubectl scale sts <name> --replicas=3` | Scale StatefulSet |
| `kubectl rollout history sts/<name>` | View StatefulSet rollout history |
| `kubectl rollout undo sts/<name>` | Rollback StatefulSet |
| `kubectl delete sts <name>` | Delete StatefulSet |
| `kubectl delete sts <name> --cascade=orphan` | Delete StatefulSet, keep pods |

## Namespaces

Organize and isolate resources.

| Command | Description |
|---------|-------------|
| `kubectl get namespaces` | List all namespaces |
| `kubectl get ns` | Short form: list namespaces |
| `kubectl create namespace <name>` | Create namespace |
| `kubectl config set-context --current --namespace=<namespace>` | Set default namespace |
| `kubectl get pods --namespace=<namespace>` | Get resources in specific namespace |
| `kubectl get pods -n <namespace>` | Short form: get pods in namespace |
| `kubectl describe namespace <name>` | Show namespace details |
| `kubectl delete namespace <name>` | Delete namespace (deletes all resources in it) |
| `kubectl label namespace <name> <key>=<value>` | Label namespace |
| `kubectl edit ns <name>` | Edit namespace |

## ConfigMaps & Secrets

Manage application configuration and sensitive data.

### ConfigMaps

| Command | Description |
|---------|-------------|
| `kubectl create configmap <name> --from-literal=<key>=<value>` | Create ConfigMap from literal |
| `kubectl create configmap <name> --from-literal=<k1>=<v1> --from-literal=<k2>=<v2>` | Create ConfigMap with multiple literals |
| `kubectl create configmap <name> --from-file=<path>` | Create ConfigMap from file |
| `kubectl create configmap <name> --from-file=<key>=<file>` | Create ConfigMap from file with custom key |
| `kubectl create configmap <name> --from-env-file=<file>` | Create ConfigMap from env file |
| `kubectl get configmaps` | List all ConfigMaps |
| `kubectl get configmap <name>` | Get specific ConfigMap |
| `kubectl describe configmap <name>` | Show ConfigMap details |
| `kubectl get configmap <name> -o yaml` | View ConfigMap in YAML |
| `kubectl edit configmap <name>` | Edit ConfigMap |
| `kubectl delete configmap <name>` | Delete ConfigMap |

### Secrets

| Command | Description |
|---------|-------------|
| `kubectl create secret generic <name> --from-literal=<key>=<value>` | Create generic secret |
| `kubectl create secret generic <name> --from-file=<path>` | Create secret from file |
| `kubectl create secret docker-registry <name> --docker-server=<server> --docker-username=<user> --docker-password=<pass> --docker-email=<email>` | Create image pull secret |
| `kubectl create secret tls <name> --cert=<cert> --key=<key>` | Create TLS secret |
| `kubectl get secrets` | List all secrets |
| `kubectl get secret <name>` | Get specific secret |
| `kubectl describe secret <name>` | Show secret metadata (not values) |
| `kubectl get secret <name> -o yaml` | View secret in YAML (values are base64) |
| `kubectl get secret <name> -o jsonpath='{.data.<key>}' \| base64 --decode` | Decode secret value |
| `kubectl edit secret <name>` | Edit secret |
| `kubectl delete secret <name>` | Delete secret |

## Nodes

Manage cluster nodes.

| Command | Description |
|---------|-------------|
| `kubectl get nodes` | List all nodes |
| `kubectl get nodes -o wide` | List nodes with more details (IP, OS) |
| `kubectl describe node <node>` | Show node details |
| `kubectl top nodes` | Show node CPU and memory usage |
| `kubectl label node <node> <key>=<value>` | Add label to node |
| `kubectl label node <node> <key>-` | Remove label from node |
| `kubectl taint node <node> <key>=<value>:<effect>` | Add taint to node |
| `kubectl taint node <node> <key>:<effect>-` | Remove taint from node |
| `kubectl cordon <node>` | Mark node as unschedulable |
| `kubectl uncordon <node>` | Mark node as schedulable |
| `kubectl drain <node>` | Drain node (evict pods gracefully) |
| `kubectl drain <node> --ignore-daemonsets` | Drain node, ignoring DaemonSet pods |
| `kubectl drain <node> --delete-emptydir-data` | Drain node, delete pods with empty dir |
| `kubectl drain <node> --force` | Force drain node |
| `kubectl get node <node> -o yaml` | View node definition in YAML |
| `kubectl patch node <node> -p '{"spec":{"unschedulable":true}}'` | Patch node to make unschedulable |

## Persistent Volumes & Claims

Manage storage resources.

| Command | Description |
|---------|-------------|
| `kubectl get persistentvolumes` | List all persistent volumes |
| `kubectl get pv` | Short form: list PVs |
| `kubectl get persistentvolumeclaims` | List all PVCs |
| `kubectl get pvc` | Short form: list PVCs |
| `kubectl get pvc -n <namespace>` | List PVCs in specific namespace |
| `kubectl describe pv <name>` | Show PV details |
| `kubectl describe pvc <name>` | Show PVC details |
| `kubectl delete pv <name>` | Delete PV |
| `kubectl delete pvc <name>` | Delete PVC |
| `kubectl edit pv <name>` | Edit PV definition |
| `kubectl edit pvc <name>` | Edit PVC definition |
| `kubectl get pv -o wide` | List PVs with storage class and capacity |
| `kubectl get pvc -o wide` | List PVCs with status and capacity |

## Storage Classes

Manage storage provisioning.

| Command | Description |
|---------|-------------|
| `kubectl get storageclasses` | List all storage classes |
| `kubectl get sc` | Short form: list storage classes |
| `kubectl describe sc <name>` | Show storage class details |
| `kubectl create -f storageclass.yaml` | Create storage class from file |
| `kubectl delete sc <name>` | Delete storage class |
| `kubectl patch sc <name> -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'` | Set as default storage class |

## Network Policies

Control network traffic between pods.

| Command | Description |
|---------|-------------|
| `kubectl get networkpolicies` | List all network policies |
| `kubectl get networkpolicy` | List network policies (singular form) |
| `kubectl get netpol` | Short form: list network policies |
| `kubectl describe netpol <name>` | Show network policy details |
| `kubectl create -f networkpolicy.yaml` | Create network policy from file |
| `kubectl delete netpol <name>` | Delete network policy |
| `kubectl edit netpol <name>` | Edit network policy |

## Ingress

Manage external access to services.

| Command | Description |
|---------|-------------|
| `kubectl get ingress` | List all ingress resources |
| `kubectl get ing` | Short form: list ingress |
| `kubectl describe ingress <name>` | Show ingress details |
| `kubectl create -f ingress.yaml` | Create ingress from file |
| `kubectl edit ingress <name>` | Edit ingress definition |
| `kubectl delete ingress <name>` | Delete ingress |
| `kubectl get ingress -o wide` | List ingress with hosts and addresses |

## Jobs & CronJobs

Manage batch and scheduled workloads.

### Jobs

| Command | Description |
|---------|-------------|
| `kubectl create job <name> --image=<image>` | Create job |
| `kubectl create job <name> --image=<image> -- <command>` | Create job with command |
| `kubectl get jobs` | List all jobs |
| `kubectl get job <name>` | Get specific job |
| `kubectl describe job <name>` | Show job details |
| `kubectl logs job/<name>` | View job logs |
| `kubectl logs job/<name> -p` | View previous job instance logs |
| `kubectl delete job <name>` | Delete job |
| `kubectl delete job <name> --cascade=orphan` | Delete job, keep pods |

### CronJobs

| Command | Description |
|---------|-------------|
| `kubectl create cronjob <name> --image=<image> --schedule="0 0 * * *"` | Create CronJob |
| `kubectl create cronjob <name> --image=<image> --schedule="0 0 * * *" -- <command>` | Create CronJob with command |
| `kubectl get cronjobs` | List all CronJobs |
| `kubectl get cronjob <name>` | Get specific CronJob |
| `kubectl describe cronjob <name>` | Show CronJob details |
| `kubectl suspend cronjob <name>` | Suspend CronJob (stop schedule) |
| `kubectl resume cronjob <name>` | Resume CronJob |
| `kubectl delete cronjob <name>` | Delete CronJob |

## DaemonSets

Run pods on every node.

| Command | Description |
|---------|-------------|
| `kubectl get daemonsets` | List all DaemonSets |
| `kubectl get ds` | Short form: list DaemonSets |
| `kubectl describe ds <name>` | Show DaemonSet details |
| `kubectl delete ds <name>` | Delete DaemonSet |
| `kubectl rollout status ds/<name>` | Check DaemonSet rollout status |
| `kubectl rollout history ds/<name>` | View DaemonSet rollout history |

## RBAC (Role-Based Access Control)

Manage access control and permissions.

| Command | Description |
|---------|-------------|
| `kubectl get roles` | List all roles in namespace |
| `kubectl get roles -A` | List all roles in all namespaces |
| `kubectl describe role <name>` | Show role details |
| `kubectl create role <name> --verb=<verb> --resource=<resource>` | Create role |
| `kubectl get rolebindings` | List all role bindings |
| `kubectl get rolebindings -A` | List all role bindings across namespaces |
| `kubectl describe rolebinding <name>` | Show role binding details |
| `kubectl create rolebinding <name> --clusterrole=<role> --serviceaccount=<sa>` | Create role binding |
| `kubectl get clusterroles` | List all cluster roles |
| `kubectl describe clusterrole <name>` | Show cluster role details |
| `kubectl create clusterrole <name> --verb=<verb> --resource=<resource>` | Create cluster role |
| `kubectl get clusterrolebindings` | List all cluster role bindings |
| `kubectl describe clusterrolebinding <name>` | Show cluster role binding details |
| `kubectl create clusterrolebinding <name> --clusterrole=<role> --serviceaccount=<sa>` | Create cluster role binding |
| `kubectl auth can-i <verb> <resource>` | Check if current user can perform action |
| `kubectl auth can-i create pods --as=<user>` | Check permissions for specific user |
| `kubectl auth can-i create pods --as-group=<group>` | Check permissions for group |

## Service Accounts

Manage Kubernetes service accounts.

| Command | Description |
|---------|-------------|
| `kubectl get serviceaccounts` | List all service accounts |
| `kubectl get sa` | Short form: list service accounts |
| `kubectl describe sa <name>` | Show service account details |
| `kubectl create serviceaccount <name>` | Create service account |
| `kubectl delete sa <name>` | Delete service account |
| `kubectl get sa <name> -o yaml` | View service account in YAML |

## Debugging & Troubleshooting

Debug and troubleshoot applications and cluster issues.

| Command | Description |
|---------|-------------|
| `kubectl describe pod <pod>` | Show pod details (check events section) |
| `kubectl get events` | List cluster events |
| `kubectl get events -n <namespace>` | List events in specific namespace |
| `kubectl get events --sort-by='.lastTimestamp'` | Sort events by timestamp |
| `kubectl logs <pod>` | View pod logs |
| `kubectl logs <pod> --previous` | View logs from crashed container |
| `kubectl logs <pod> -c <container>` | View logs from specific container |
| `kubectl logs <pod> --all-containers=true` | View logs from all containers |
| `kubectl logs <pod> -f` | Stream/follow pod logs |
| `kubectl logs <pod> --tail=100` | View last 100 lines of logs |
| `kubectl logs <pod> --timestamps=true` | Show timestamps in logs |
| `kubectl logs -l <label>=<value>` | View logs from pods matching label |
| `kubectl exec -it <pod> -- /bin/bash` | Open shell in running pod |
| `kubectl exec -it <pod> -c <container> -- /bin/sh` | Open shell in specific container |
| `kubectl exec <pod> -- <command>` | Execute command in pod (non-interactive) |
| `kubectl debug node/<node>` | Start debug pod on node |
| `kubectl debug <pod>` | Debug pod by adding container |
| `kubectl debug <pod> -it --image=<image>` | Debug pod with custom debug image |
| `kubectl port-forward <pod> <local>:<remote>` | Forward local port to pod port |
| `kubectl port-forward svc/<service> <local>:<remote>` | Forward port to service |
| `kubectl describe node <node>` | Show node conditions and events |
| `kubectl top nodes` | Show node resource usage |
| `kubectl top pod` | Show pod resource usage |
| `kubectl top pod --containers` | Show per-container resource usage |
| `kubectl api-resources` | List all API resources |
| `kubectl explain <resource>` | Explain resource structure |
| `kubectl explain pod.spec` | Explain resource field |
| `kubectl get <resource> -o yaml` | Export resource as YAML |
| `kubectl diff -f <file>` | Show what would change with apply |
| `kubectl apply --dry-run=client -f <file>` | Dry-run apply (show changes) |
| `kubectl apply --dry-run=server -f <file>` | Server-side dry-run apply |

## Output Formatting

Format command output for different use cases.

| Command | Description |
|---------|-------------|
| `kubectl get pods -o wide` | Show detailed columns (node, IP) |
| `kubectl get pods -o yaml` | Output as YAML |
| `kubectl get pods -o json` | Output as JSON |
| `kubectl get pods -o jsonpath='{.items[0].metadata.name}'` | Custom JSON path extraction |
| `kubectl get pods -o custom-columns=NAME:.metadata.name,STATUS:.status.phase` | Custom columns output |
| `kubectl get pods --sort-by=.metadata.name` | Sort by field |
| `kubectl get pods --sort-by=.status.containerStatuses[0].lastState.terminated.exitCode` | Sort by nested field |
| `kubectl get pods -l app=web` | Filter by label |
| `kubectl get pods --field-selector=status.phase=Running` | Filter by field |
| `kubectl get pods --selector="app in (web,api)"` | Filter by label selector |
| `kubectl get pods -A` | Show all namespaces |
| `kubectl get pods --all-namespaces` | Long form: show all namespaces |
| `kubectl get pods --show-labels` | Display object labels |
| `kubectl get pods -w` | Watch resource changes in real-time |

## Resource Management

Declare and manage resources using files.

| Command | Description |
|---------|-------------|
| `kubectl apply -f <file>` | Apply resource from file |
| `kubectl apply -f <directory>` | Apply all files in directory |
| `kubectl apply -f <url>` | Apply resource from URL |
| `kubectl apply -f - < <file>` | Apply from stdin |
| `kubectl create -f <file>` | Create resource from file (fails if exists) |
| `kubectl create -f <directory>` | Create all resources in directory |
| `kubectl delete -f <file>` | Delete resource from file |
| `kubectl delete -f <directory>` | Delete resources from directory |
| `kubectl delete <resource> <name>` | Delete specific resource |
| `kubectl delete <resource> --all` | Delete all resources of type |
| `kubectl delete <resource> -l <label>=<value>` | Delete resources by label |
| `kubectl replace -f <file>` | Replace resource (differs from apply) |
| `kubectl replace --force -f <file>` | Force replace (delete and recreate) |
| `kubectl diff -f <file>` | Show differences before applying |
| `kubectl apply --dry-run=client -f <file>` | Preview changes without applying |
| `kubectl set image deployment/<name> <c>=<image>` | Update container image |
| `kubectl set env deployment/<name> KEY=value` | Set environment variables |
| `kubectl set resources deployment/<name> --limits=cpu=100m,memory=128Mi` | Set resource limits |
| `kubectl rollout status deployment/<name>` | Check deployment rollout status |
| `kubectl rollout history deployment/<name>` | View rollout history |
| `kubectl rollout undo deployment/<name>` | Rollback to previous revision |

## Kustomize

Use kustomize for template-free customization.

| Command | Description |
|---------|-------------|
| `kubectl kustomize <directory>` | Display kustomized resources |
| `kubectl apply -k <directory>` | Apply kustomized resources |
| `kubectl diff -k <directory>` | Show kustomized resource diff |
| `kubectl kustomize <directory> -o yaml` | Output kustomized resources as YAML |

## Patch & Edit Operations

Modify existing resources.

| Command | Description |
|---------|-------------|
| `kubectl edit <resource> <name>` | Edit resource in default editor |
| `kubectl edit pod <pod>` | Edit pod configuration |
| `kubectl patch <resource> <name> -p '<patch>'` | Apply strategic merge patch |
| `kubectl patch <resource> <name> --type='json' -p '<patch>'` | Apply JSON patch |
| `kubectl annotate <resource> <name> <key>=<value>` | Add annotation |
| `kubectl annotate <resource> <name> <key>=<value> --overwrite` | Update annotation |
| `kubectl label <resource> <name> <key>=<value>` | Add label |
| `kubectl label <resource> <name> <key>=<value> --overwrite` | Update label |
| `kubectl label <resource> <name> <key>-` | Remove label |

## Advanced Commands

Powerful commands for advanced operations and inspection.

| Command | Description |
|---------|-------------|
| `kubectl api-resources` | List all API resource types |
| `kubectl api-versions` | List all API versions |
| `kubectl explain <resource>` | Explain resource structure |
| `kubectl explain pod.spec.containers` | Explain nested field structure |
| `kubectl get <resource> -o json \| jq '.items[] \| select(.metadata.labels.app=="web")'` | Complex filtering with jq |
| `kubectl cluster-info dump --output-directory=./cluster-dump` | Dump entire cluster state |
| `kubectl get all` | Get all resource types in namespace |
| `kubectl get all -A` | Get all resources across all namespaces |
| `kubectl get all --selector=app=myapp` | Get all resources matching label |
| `kubectl proxy` | Start proxy to Kubernetes API |
| `kubectl proxy --port=8080` | Start proxy on specific port |
| `kubectl certificate approve <csr>` | Approve certificate signing request |
| `kubectl certificate deny <csr>` | Deny certificate signing request |
| `kubectl get csr` | List certificate signing requests |
| `kubectl describe csr <name>` | Show CSR details |

## Important File Paths & Configurations

Quick reference for important Kubernetes paths and configuration files.

| Path | Description |
|------|-------------|
| `~/.kube/config` | kubeconfig file (cluster credentials and contexts) |
| `/etc/kubernetes/` | Kubernetes configuration directory (on control plane) |
| `/etc/kubernetes/manifests/` | Static pod manifests |
| `/etc/kubernetes/pki/` | PKI certificates and keys |
| `/var/lib/kubelet/` | Kubelet data directory |
| `/var/lib/kubelet/kubeconfig.conf` | Kubelet kubeconfig |
| `/var/lib/kubelet/config.yaml` | Kubelet configuration |
| `/var/log/pods/` | Pod logs directory |
| `/var/log/containers/` | Container logs directory |
| `/var/lib/etcd/` | etcd data directory |
| `/etc/cni/net.d/` | CNI plugin configuration |
| `/opt/cni/bin/` | CNI plugin binaries |
| `$KUBECONFIG` | Environment variable for kubeconfig location |

## Helm Quick Reference

Package manager for Kubernetes.

| Command | Description |
|---------|-------------|
| `helm version` | Show Helm version |
| `helm repo add <name> <url>` | Add Helm repository |
| `helm repo list` | List Helm repositories |
| `helm repo update` | Update Helm repositories |
| `helm repo remove <name>` | Remove Helm repository |
| `helm search repo <chart>` | Search chart in repositories |
| `helm search hub <chart>` | Search chart in Artifact Hub |
| `helm install <name> <chart>` | Install chart |
| `helm install <name> <chart> --values values.yaml` | Install with custom values |
| `helm install <name> <chart> --set key=value` | Install with set parameters |
| `helm install <name> <chart> -n <namespace>` | Install in specific namespace |
| `helm install <name> <chart> --create-namespace` | Create namespace if not exists |
| `helm install <name> <chart> --dry-run --debug` | Dry-run install |
| `helm upgrade <name> <chart>` | Upgrade release |
| `helm upgrade <name> <chart> --values values.yaml` | Upgrade with custom values |
| `helm upgrade <name> <chart> --install` | Install or upgrade |
| `helm upgrade <name> <chart> --reuse-values` | Keep previous custom values |
| `helm list` | List releases in current namespace |
| `helm list -A` | List releases across all namespaces |
| `helm history <name>` | Show release history |
| `helm rollback <name>` | Rollback to previous release |
| `helm rollback <name> <revision>` | Rollback to specific revision |
| `helm uninstall <name>` | Uninstall release |
| `helm status <name>` | Show release status |
| `helm get values <name>` | Get release values |
| `helm get manifest <name>` | Get release manifest |
| `helm template <name> <chart>` | Render chart templates locally |
| `helm lint <chart>` | Lint chart for errors |
| `helm create <name>` | Create new Helm chart |
| `helm package <chart>` | Package chart for distribution |

## Useful Aliases & Functions

Boost productivity with common kubectl aliases and shell functions.

### Bash Aliases

Add these to your `~/.bashrc`:

```bash
# Basic shortcuts
alias k=kubectl
alias kd='kubectl describe'
alias ke='kubectl edit'
alias kg='kubectl get'
alias kl='kubectl logs'
alias kex='kubectl exec -it'
alias kdel='kubectl delete'
alias ka='kubectl apply -f'
alias kaf='kubectl apply -f'
alias kc='kubectl config'

# Pods
alias kgp='kubectl get pods'
alias kgpa='kubectl get pods --all-namespaces'
alias kgpo='kubectl get pods -o wide'
alias kdp='kubectl describe pod'
alias klp='kubectl logs pod'

# Deployments
alias kgd='kubectl get deployments'
alias kdd='kubectl describe deployment'
alias kled='kubectl logs deployment'

# Services
alias kgs='kubectl get services'
alias kds='kubectl describe service'

# Namespaces
alias kgn='kubectl get namespaces'
alias kdn='kubectl describe namespace'

# Nodes
alias kgno='kubectl get nodes'
alias kdno='kubectl describe node'
alias ktn='kubectl top node'

# All resources
alias kga='kubectl get all'
alias kgaa='kubectl get all --all-namespaces'

# Debugging
alias kex='kubectl exec -it'
alias kpf='kubectl port-forward'
```

### Zsh Aliases

Add these to your `~/.zshrc`:

```zsh
alias -g k=kubectl
alias -g kd=kubectl describe
alias -g ke=kubectl edit
alias -g kg=kubectl get
alias -g kl=kubectl logs
alias -g kex=kubectl exec -it
alias -g kdel=kubectl delete
alias -g ka=kubectl apply -f
alias -g kc=kubectl config

# Pods
alias -g kgp='kubectl get pods'
alias -g kgpa='kubectl get pods --all-namespaces'
alias -g kgpo='kubectl get pods -o wide'
alias -g kdp='kubectl describe pod'
alias -g klp='kubectl logs pod'

# Deployments
alias -g kgd='kubectl get deployments'
alias -g kdd='kubectl describe deployment'

# Nodes
alias -g kgno='kubectl get nodes'
alias -g kdno='kubectl describe node'
```

### Useful Functions

```bash
# View pod logs with color filtering for errors
klerr() {
    kubectl logs $1 | grep -i error
}

# Get pod by partial name
kgp-grep() {
    kubectl get pods | grep $1
}

# Describe pod by partial name
kdp-grep() {
    kubectl describe pod $(kubectl get pods | grep $1 | awk '{print $1}')
}

# Port-forward to service
kpf-svc() {
    kubectl port-forward svc/$1 $2:$3 --namespace=$4
}

# Get all events sorted by time
kevents() {
    kubectl get events --sort-by='.lastTimestamp'
}

# Show resource definitions as YAML
kshow() {
    kubectl get $1 $2 -o yaml
}

# Quick debug pod
kdebug() {
    kubectl debug $1 -it --image=alpine:latest
}
```

## Shell Autocompletion

Enable autocompletion for kubectl in bash and zsh.

### Bash Autocompletion

Install bash-completion:

```bash
# macOS with Homebrew
brew install bash-completion

# Ubuntu/Debian
sudo apt-get install bash-completion
```

Enable kubectl autocompletion:

```bash
# Add to ~/.bashrc
source <(kubectl completion bash)

# Or with alias
echo "complete -o default -F __start_kubectl k" >> ~/.bashrc

# Reload
source ~/.bashrc
```

### Zsh Autocompletion

Enable kubectl autocompletion:

```bash
# Add to ~/.zshrc
source <(kubectl completion zsh)

# Or add this for alias support
compdef __start_kubectl k

# Reload
source ~/.zshrc
```

### Fish Shell Autocompletion

```bash
# Generate and add completion
mkdir -p ~/.config/fish/completions
kubectl completion fish | sudo tee /usr/share/fish/vendor_completions.d/kubectl.fish
```

## Quick Reference Tips

**Pro Tips for Daily Use:**

- Use `-o wide` to see more columns (node, IP addresses, restarts)
- Use `-w` flag to watch resources in real-time
- Use `--dry-run=client` before applying changes to preview
- Use `--all-namespaces` or `-A` to see all namespaces at once
- Use `--show-labels` to see pod/node labels
- Chain `|` with `grep` to filter output
- Use `jq` with JSON output for complex filtering
- Set namespace default with `kubectl config set-context --current --namespace=<ns>`
- Use `-l` flag to filter by labels
- Keep multiple kubeconfig files and switch with `use-context`
- Use `-c` for multi-container pod operations
- Use `--previous` to debug crashed containers
- Use `kubectl explain` to understand resource fields
- Use `--sort-by` to order output
- Use `--field-selector` for advanced filtering

## Common Workflows

**Deploy an application:**

```bash
kubectl create deployment myapp --image=myapp:1.0
kubectl scale deployment myapp --replicas=3
kubectl expose deployment myapp --type=LoadBalancer --port=8080
```

**Debug a failing pod:**

```bash
kubectl describe pod <pod-name>
kubectl logs <pod-name> --previous
kubectl exec -it <pod-name> -- /bin/bash
```

**Update image in deployment:**

```bash
kubectl set image deployment/myapp app=myapp:2.0
kubectl rollout status deployment/myapp
```

**Rollback deployment:**

```bash
kubectl rollout history deployment/myapp
kubectl rollout undo deployment/myapp --to-revision=1
```

**Create and manage secrets:**

```bash
kubectl create secret docker-registry regcred --docker-server=docker.io --docker-username=user --docker-password=pass
kubectl create secret generic db-secret --from-literal=username=admin --from-literal=password=secret
```

**Scale and autoscale:**

```bash
kubectl scale deployment myapp --replicas=5
kubectl autoscale deployment myapp --min=2 --max=10 --cpu-percent=80
```

---

**Last Updated:** March 21, 2026 | **kubectl Version:** 1.28+ | Maintained for CloudCaptain Learning Platform
