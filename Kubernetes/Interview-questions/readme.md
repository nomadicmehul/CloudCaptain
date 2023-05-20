##### What is Kubernetes?
Kubernetes is an open-source container orchestration system for deploying, scaling, and managing automated applications. It offers an excellent community and works with all cloud providers. Hence, it is a multi-container management solution.

##### Name some of the main Kubernetes features.
Some of the main Kubernetes features that candidates may name include:
1. Simultaneous, multiple cluster management
2. Container management
3. Self-monitoring features for nodes and containers
4. Resource scaling options – both vertically and horizontally

##### What is Kubernetes?
Kubernetes is an open-source container orchestration system for deploying, scaling, and managing automated applications. It offers an excellent community and works with all cloud providers. Hence, it is a multi-container management solution.

##### What is a container?
Containers are a technology for collecting the compiled code for an application when it is required at run-time. Each container allows you to run repeatable, standard dependencies and the same behavior whenever the container runs. It divides the application from the underlying host infrastructure to make the deployment much easier in cloud or OS platforms.

##### What are the nodes that run inside Kubernetes?
A node is a worker machine or VM depending on the cluster. Each node contains services to run the pods and the pods are managed by the master components.

##### What are the services that a node gives and its responsibilities?
The services that include in a node are as follows:
1. Container run-time
2. Kubelet
3. Kube-proxy
The Container run-time is responsible to start and manage the containers.
The kubelet is responsible for running the state of each node and receives commands from the master to work on it and it is also responsible for the metric collection of pods.
The Kube-proxy is a component that manages the subnets and makes services available for all other components.

##### What is a master node in Kubernetes?
A master node is a node that controls and manages the set of worker nodes and resembles a cluster in Kubernetes.
Q6. What are the main components of the master node?
The main components of the master node that help to manage worker nodes are as follows:

###### Kube-server: It acts as a front end of the cluster and communicates with the cluster through the API server.
Kube controller: It implements governance across the cluster and runs the set of controllers for the running cluster.
Kube scheduler: It schedules the activities of the nodes and holds the node resource to determine the proper action for triggering events.

##### What is a pod and what does it do?
A pod is a group of containers that are deployed together on the same host. It is the basic execution unit of the Kubernetes application that can create or deploy the Kubernetes unit of object models.

Kubernetes pods can be used in two ways. they are as follows:

Pods that can run in a single container
Pods that can run with multiple containers when it is required to work together

##### What are the different types of multiple-container pods?
There are three different types of multi-container pods. They are as follows:

Sidecar: The Sidecar pattern is a single node pattern made of two containers of the application. It contains the core logic of the application and it sends the logic files to the bucket.
Adapter: It is used to standardize and normalize the output application or monitor data for aggregation. It performs restructuring, and reformatting and can write the correct formatted output for the application.
Ambassador: It is a proxy pattern that allows connecting other containers with a port on the localhost.

##### What is the Namespace? How many namespaces are there in Kubernetes?
A namespace is used to work with multiple teams or projects spread across. It is used to divide the cluster resources for multiple users.
Q. What is kubectl?
Kubectl is the command-line tool used to control the Kubernetes clusters. It provides the CLI to run the command against clusters to create and manage the Kubernetes components.

##### What are the different types of services in Kubernetes?
The different types of services that support Kubernetes are as follows:

1. Cluster IP: It exposes the services on cluster internal IP and makes the services reachable within the cluster only.
2. Node port: It exposes the services on each node’s IP at the static port.
3. Load balancer: It provides services externally using a cloud provider’s load balancer. It creates the service to route the external load balancer automatically.
4. External name: It navigates the service to the contents of the external field by returning the CNAME record by its value.

##### What is the role of clusters in Kubernetes?
Kubernetes allows the required state management by cluster services of a specified configuration. These cluster services run the configurations in the infrastructure. The following are steps that are involved in this process as follows:

1. The deployment file contains all the configuration that is fed into the cluster
2. These deployments are fed into the API server
3. The cluster services will schedule the pods in the environment
4. It also ensures the right number of pods were running
Q. What are the advantages of Kubernetes?
The advantages of Kubernetes are as follows:

1. Kubernetes is open-source and free
2. It is highly scalable and runs in any operating system
3. It provides more concepts and is more powerful than Docker swarm
4. It provides a scheduler, auto-scaling, rolling upgrades, and health checks
5. It has a flat network space and customized functionalities
6. It is easy to make effective CI/CD pipelines
7. It can improve productivity

##### What are the disadvantages of Kubernetes?
The disadvantages of Kubernetes are as follows:

1. The installation process and configuration is highly difficult
2. It is not easy to manage the services
3. It takes a lot of time to run and compile
4. It is more expensive than the other alternatives
5. It can be overkill for simple application
Playing with Ansible... on Docker | mklein.io
https://mklein.io/2018/02/23/ansible-docker/


##### What are the nodes that run inside Kubernetes?
A node is a worker machine or VM depending on the cluster. Each node contains services to run the pods and the pods are managed by the master components.

##### What are the services that a node gives and its responsibilities?
The services that include in a node are as follows:

* Container run-time
* Kubelet
* Kube-proxy

The Container run-time is responsible to start and manage the containers. The kubelet is responsible for running the state of each node and receives commands from the master to work on it and it is also responsible for the metric collection of pods. The Kube-proxy is a component that manages the subnets and makes services available for all other components.

##### What is a master node in Kubernetes?
A master node is a node that controls and manages the set of worker nodes and resembles a cluster in Kubernetes.
What are the main components of the master node?
The main components of the master node that help to manage worker nodes are as follows:

* Kube-server: It acts as a front end of the cluster and communicates with the cluster through the API server.
* Kube controller: It implements governance across the cluster and runs the set of controllers for the running cluster.
* Kube scheduler: It schedules the activities of the nodes and holds the node resource to determine the proper action for triggering events.

##### What is a pod and what does it do?
A pod is a group of containers that are deployed together on the same host. It is the basic execution unit of the Kubernetes application that can create or deploy the Kubernetes unit of object models.

Kubernetes pods can be used in two ways. they are as follows:

1. Pods that can run in a single container
2. Pods that can run with multiple containers when it is required to work together

##### What are the different types of multiple-container pods?
There are three different types of multi-container pods. They are as follows:

* Sidecar: The Sidecar pattern is a single node pattern made of two containers of the application. It contains the core logic of the application and it sends the logic files to the bucket.
* Adapter: It is used to standardize and normalize the output application or monitor data for aggregation. It performs restructuring, and reformatting and can write the correct formatted output for the application.
* Ambassador: It is a proxy pattern that allows connecting other containers with a port on the localhost.

##### What is the Namespace? How many namespaces are there in Kubernetes?
A namespace is used to work with multiple teams or projects spread across. It is used to divide the cluster resources for multiple users.
Mention different kinds of Namespaces in Kubernetes.
The namespaces are of three kinds. They are:

Default: The default namespace that when the cluster comes out of the box with no other namespaces
* Kube-system: The namespace for objects created by Kubernetes.
* Kune-public: The namespace that can create automatically and is visible and readable publicly throughout the whole cluster. The public aspect of this namespace is only convenient and reserved for cluster usage.

##### Explain the Replica set?
A Replica set is used to maintain a stable set of replica pods. It is used to specify the available number of identical pods. It was also considered as a replacement for the replication controller sometimes.

##### Explain the Ingress controller?
An ingress controller is a pod that acts as an inbound traffic handler. It is responsible for reading the ingress resource information and processing the data accordingly.

##### Explain the Load balancer in Kubernetes?
The load balancer is a way of distributing the loads, which is easy to implement at the dispatch level. Each load balancer sits between the client devices and the backend servers. It receives and distributes the incoming requests to all available servers.
Explain the two different types of load balancers.
The two different load balancers are one is an internal load balancer that balances the load and allocates the pods automatically with the required configuration. And the other is the External load balancer that directs the traffic from external loads to the backend pods.

##### What is Minikube?
Minikube is a type of tool that helps to run Kubernetes locally. It runs on a single-node Kubernetes cluster inside a Virtual machine (VM).
Explain Prometheus in Kubernetes.
Prometheus is an open-source toolkit that is used for metric-based monitoring and alerting the application. It provides a data model and a query language and can provide details and actions of metrics. It supports the instrumental application of language for many languages. The Prometheus operator provides easy monitoring for deployments and k8s services, besides Alertmanager and Grafana.

##### What is the role of clusters in Kubernetes?
Kubernetes allows the required state management by cluster services of a specified configuration. These cluster services run the configurations in the infrastructure. The following are steps that are involved in this process as follows:

* The deployment file contains all the configuration that is fed into the cluster
* These deployments are fed into the API server
* The cluster services will schedule the pods in the environment
* It also ensures the right number of pods were running

##### What is the Cluster IP?
The cluster Ip is a default Kubernetes service that provides a link between the pods or map container port and the host ports. It provides the services within the cluster and gives access to other apps which are inside the same cluster.

##### What is Kubernetes architecture?
The Kubernetes architecture provides a flexible, coupled mechanism for the service. It consists of one master node and multiple containers. The master node is responsible for managing the clusters, API, and scheduling the pods. Each node runs on the container runtime such as Docker, rkt along with the node that communicates with the master.

##### What are the main components of Kubernetes architecture?
The two main components of the Kubernetes architecture are as follows:

Master node
Worker node
Each node contains the individual components in it

##### Define Kube-api server?
The Kube-API is the frontend of the master node that exposes all the components in the API server. It provides communication between the Kubernetes nodes and the master components.

##### What are the advantages of Kubernetes?
The advantages of Kubernetes are as follows:
Kubernetes is open-source and free
* It is highly scalable and runs in any operating system
* It provides more concepts and is more powerful than Docker swarm
* It provides a scheduler, auto-scaling, rolling upgrades, and health checks
* It has a flat network space and customized functionalities
* It is easy to make effective CI/CD pipelines
* It can improve productivity

##### What are the disadvantages of Kubernetes?
* The disadvantages of Kubernetes are as follows:
* The installation process and configuration is highly difficult
* It is not easy to manage the services
* It takes a lot of time to run and compile
* It is more expensive than the other alternatives
* It can be overkill for simple application

