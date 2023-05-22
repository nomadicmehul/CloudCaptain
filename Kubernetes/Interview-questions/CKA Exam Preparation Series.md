## CKA Exam Preparation Series (Step by Step)

*In this quick post, we will go through 15 Important terms short explanation  with their real-world example to understand better in less time.* 

1. Container:
Containers are a lightweight and portable way to package and run software applications. They provide a consistent and reproducible environment for running applications across different computing environments. In production, containers are often used to isolate applications and their dependencies, improve resource utilization, and streamline software delivery processes.

Real-world example: A web application can be containerized and run in a containerized environment such as Kubernetes, which allows easy scaling and management of the application.

2. Container Orchestration:
Container orchestration refers to the automated management of containerized applications, including deployment, scaling, and monitoring. It helps to abstract the underlying infrastructure and provides a unified API for managing applications across different environments.

Real-world example: Kubernetes is an example of container orchestration platform that automates the deployment, scaling, and management of containerized applications.

3. Kubernetes (K8s):
Kubernetes is an open-source container orchestration platform that provides a unified API for managing containerized applications. It automates the deployment, scaling, and management of containerized applications, making it easier to manage applications in production.

Real-world example: Kubernetes is used by many companies, such as Airbnb, Spotify, and Lyft, to manage their containerized applications in production.

4. Core Concepts of K8s:
The core concepts of Kubernetes include Pods, ReplicaSets, Deployments, Services, ConfigMaps, Secrets, and Persistent Volumes. These concepts are used to define and manage the lifecycle of containerized applications in Kubernetes.

Real-world example: Pods are used to run one or more containers together, while ReplicaSets and Deployments are used to manage the scaling and rollout of these Pods.

5. K8s Architecture:
The Kubernetes architecture consists of a control plane and worker nodes. 
The control plane includes the Kube API server, ETCD, Kube Controller Manager, and Kube Scheduler, while the worker nodes include Kubelet and Kube Proxy. 
The control plane is responsible for managing the state of the cluster, while the worker nodes are responsible for running containers.

Real-world example: In a Kubernetes cluster, the control plane can be run on a set of dedicated servers or in the cloud, while the worker nodes can be run on a set of physical or virtual machines.

6. Kube API Server:
The Kube API server provides a unified API for managing Kubernetes objects, such as Pods, ReplicaSets, and Deployments. It also acts as the front-end for the Kubernetes control plane, handling authentication, authorization, and admission control.

Real-world example: The Kube API server is used by Kubernetes administrators to manage the state of the cluster and the applications running on it.

7. ETCD:
ETCD is a distributed key-value store that is used by Kubernetes to store the state of the cluster. It provides a consistent and reliable way to store and retrieve data, making it easy to manage the state of the cluster.

Real-world example: ETCD is used by Kubernetes to store information about the Pods, Services, ConfigMaps, Secrets, and other objects that are used to manage containerized applications.

8. Kube Controller Manager:
The Kube Controller Manager is responsible for running controllers that monitor the state of the cluster and make changes as necessary. It includes controllers for ReplicaSets, Deployments, Services, and other Kubernetes objects.

Real-world example: The Kube Controller Manager is used by Kubernetes to ensure that the state of the cluster is consistent and that containerized applications are running as expected.

9. Kube Scheduler:
The Kube Scheduler is responsible for scheduling Pods on worker nodes in the cluster. It takes into account factors such as resource requirements and node availability when making scheduling decisions.

Real-world example: The Kube Scheduler is used by Kubernetes to ensure that Pods are scheduled on worker nodes in an efficient and reliable manner, based on the available resources and constraints.

10. Kubelet:
The Kubelet is an agent that runs on each worker node in the Kubernetes cluster. It is responsible for managing the lifecycle of Pods on the node, including pulling container images and starting and stopping containers.

Real-world example: The Kubelet is used by Kubernetes to manage the containers running on each worker node in the cluster, ensuring that they are running as expected and that their resource usage is optimized.

11. Kube Proxy:
The Kube Proxy is responsible for managing network traffic between Pods in the Kubernetes cluster. It runs on each worker node and is responsible for routing traffic to the appropriate Pod.

Real-world example: The Kube Proxy is used by Kubernetes to ensure that network traffic between Pods is managed efficiently and securely, providing a stable and reliable network environment for containerized applications.

12. Pod:
A Pod is the smallest deployable unit in Kubernetes. It represents a single instance of a running process in a container. Pods are used to run one or more containers together, and can be scaled up or down as needed.

Real-world example: In Kubernetes, a web application can be run in a Pod, with multiple instances of the Pod running to ensure high availability and scalability.

13. Yaml:
YAML is a human-readable data serialization language used to define the configuration of Kubernetes objects. It is used to define and manage the state of Kubernetes objects such as Pods, ReplicaSets, and Deployments.

Real-world example: Kubernetes administrators use YAML to define and manage the configuration of the various objects used to manage containerized applications running on the Kubernetes cluster.

14. Replica Set:
A ReplicaSet is a Kubernetes object that manages the scaling and replication of Pods. It ensures that the specified number of replicas of a Pod is running at any given time.

Real-world example: A ReplicaSet can be used to ensure that multiple instances of a web application are running in the Kubernetes cluster, with automatic scaling based on demand.

15. Deployments:
A Deployment is a Kubernetes object that manages the rolling out and rolling back of changes to the state of the cluster. It provides a declarative way to manage updates to containerized applications running in the Kubernetes cluster.

Real-world example: A Deployment can be used to manage the rollout of a new version of a web application, with automated rollbacks in case of failures.