##### What is DevOps ?

DevOps is the combination of cultural philosophies, practices, and tools that increases an organization’s ability to deliver applications and services at high velocity: evolving and improving products at a faster pace than organizations using traditional software development and infrastructure management processes. This speed enables organizations to better serve their customers and compete more effectively in the market.

DevOps process can be visualized as an infinite loop, comprising these steps: plan, code, build, test, release, deploy, operate, monitor.

##### DevOps and Cloud computing: What is the need?
Development and Operations are considered to be one single entity in the DevOps practice. This means that any form of Agile development, alongside Cloud Computing, will give it a straight-up advantage in scaling practices and creating strategies to bring about a change in business adaptability. If the cloud is considered to be a car, then DevOps would be its wheels

##### What is cloud computing?
Cloud computing refers to the on-demand delivery of IT resources and applications via the internet with pay as you go.
If we need we can get it right way. If we dont we can give it away.
Cloud computing is the practice of using a network of remote servers hosted on the Internet to store, manage and process data rather than a local server or a personal computer.
Features:
1. On demand provisioning
2. Scalability in minutes: We can scale out, or scale in

    ###### Cloud models:
        Public
        Private
        Hybrid
        Community


##### LAMP Architecture
LAMP stands for Linux, Apache, MySQL, and PHP. Together, they provide a proven set of software for delivering high-performance web applications.

##### Name some common top DevOps tools.
A. Some top DevOps tools are Chef, Puppet, and Ansible as Configuration Management and Deployment Tools, Git as a Version Control System Tool, Jenkins as a Continuous Integration Tool, Docker as an effective Containerization Tool, and Nagios as a Continuous Monitoring Tool.

##### What is the main goal of DevOps?
A. The main goal of DevOps is to optimize the flow of value from the idea to the end-user and make the delivery of value effective and efficient. DevOps also focuses on culture and cultural changes in the company in a big way.

##### What is the primary difference between agile and DevOps?

A. The essential difference between the two is that while agile is about increasing the efficiency of developers and development cycles, DevOps allows continuous integration and continuous delivery by utilizing the operations team.

##### What is Jenkins?
Jenkins is an open-source tool in Java for automation and Continuous Integration tasks. Jenkins allows users to create and test their projects continuously while integrating changes quickly. One of the biggest highlights of Jenkins is its large number of plugins. Plugins allow Jenkins to integrate with other software solutions and enhance its capabilities in multitudes.

Jenkins can integrate the entire development life-cycle process of an application. This means it can handle creation, testing, packaging, deployment, analysis, and other operations. 

##### What is Ansible?
A product of RedHat, Ansible is a management tool for service deployment.
 It is an open-source solution for software provisioning, application deployment, and configuration management; Ansible has become increasingly popular because it offers its users numerous facilities.
 You can automate multiple IT processes by using Ansible. Moreover, its design is for multi-tier deployment so it can handle your different systems together.

##### TOMCAT SERVER
Tomcat is used for web applications written in Java that don’t require full Java EE specifications but still need a reliable tool. We can develop web applications in Java EE technologies and use Tomcat as a web server to automate the web component execution process. The Tomcat server provides one set of Servlet and JSP containers.

##### What is a Web Server?
Whenever you open your browser, type a URL and then click enter. Basically, you are requesting the contents of that URL.
Web Servers are computers that deliver the requested web pages. Every web server has an IP address and domain name.

##### What is Nginx?
It is open-source software designed for maximum performance and stability.
Nginx is a dedicated web server that has solved efficiency issues and provided us with an optimum way to handle 1000s of requests concurrently.

###### Features of Nginx are as follows:

1. It provides HTTP server capabilities.
2. Designed to provide stability and maximum performance.
3. Functions as a proxy server for email (IMAP, POP3, and SMTP).
4. It uses an event-driven and non-threaded architecture to provide less CPU computation per request served.
5. It provides scalability.
6. Reduces wait time for the client.
7. Upgrades can be done while Nginx is hosting a website without any downtime.

##### What is CI/CD, and how does AWS play a role in it?

The combined processes of continuous integration and continuous delivery, also known as a continuous deployment (CD), are known as CI/CD in the field of software engineering. By mandating automation in creating, testing, and deploying applications, CI/CD fills the gaps between production and operational activities and teams. The progressive code changes made by programmers are compiled, linked, and packaged into software deliverables via CI/CD services.

##### How would you define Continuous Testing in DevOps?
A. Continuous testing is the execution of automated tests every time a piece of code is merged. This makes it possible for engineers to get immediate feedback on the most recent merging of code. Automation of tests is encouraged throughout the development cycle through principles of continuous testing.

##### Mention the various container resource monitoring tools.
The various container monitoring tools are as follows:

1.Grafana
2. Heapster
3. CAdvisor
4. InfluxDB
5. Prometheus

##### What is the use of SSH?
SSH stands for Secure Shell and is an administrative protocol that lets users have access and control the remote servers over the Internet to work using the command line.

SSH is a secured encrypted version of the previously known Telnet which was unencrypted and not secure. This ensured that the communication with the remote server occurs in an encrypted form.

SSH also has a mechanism for remote user authentication, input communication between the client and the host, and sending the output back to the client.

##### Explain the term "Infrastructure as Code" (IaC) as it relates to configuration management.
• Writing code to manage configuration, deployment, and automatic provisioning.
• Managing data centers with machine-readable definition files, rather than physical hardware configuration.
• Ensuring all your servers and other infrastructure components are provisioned consistently and effortlessly.
• Administering cloud computing environments, also known as infrastructure as a service (IaaS).

##### How is IaC implemented using AWS?
Start by talking about the age-old mechanisms of writing commands onto script files and testing them in a separate environment before deployment and how this approach is being replaced by IaC. Similar to the codes written for other services, with the help of AWS, IaC allows developers to write, test, and maintain infrastructure entities in a descriptive manner, using formats such as JSON or YAML. This enables easier development and faster deployment of infrastructure changes.

##### Explain the difference between a centralized and distributed version control system (VCS).
Centralized Version Control System
• All file versions are stored on a central server
• No developer has a copy of all files on a local system
• If the central server crashes, all data from the project will be lost
 
##### Distributed Control System
• Every developer has a copy of all versions of the code on their systems
• Enables team members to work offline and does not rely on a single location for backups
• There is no threat, even if the server crashes

##### What is Jenkinsfile?
Jenkinsfile contains the definition of a Jenkins pipeline and is checked into the source control repository. It is a text file.
• It allows code review and iteration on the pipeline.
• It permits an audit trail for the pipeline.
• There is a single source of truth for the pipeline, which can be viewed and edited.

##### What concepts are key aspects of the Jenkins pipeline?
• Pipeline: User-defined model of a CD pipeline. The pipeline's code defines the entire build process, which includes building, testing and delivering an application
• Node: A machine that is part of the Jenkins environment and capable of executing a pipeline
• Step: A single task that tells Jenkins what to do at a particular point in time
• Stage: Defines a conceptually distinct subset of tasks performed through the entire pipeline (build, test, deploy stages)

##### How can you copy Jenkins from one server to another?
 
• Move the job from one Jenkins installation to another by copying the corresponding job directory.
• Create a copy of an existing job by making a clone of a job directory with a different name.
• Rename an existing job by renaming a directory.

##### Explain the architecture of Docker.
• Docker uses a client-server architecture.
• Docker Client is a service that runs a command. The command is translated using the REST API and is sent to the Docker Daemon (server).
• Docker Daemon accepts the request and interacts with the operating system to build Docker images and run Docker containers.
• A Docker image is a template of instructions, which is used to create containers.
• Docker container is an executable package of an application and its dependencies together.
• Docker registry is a service to host and distribute Docker images among users.

##### Explain the two types of pipeline in Jenkins, along with their syntax.
Jenkins provides two ways of developing a pipeline code: Scripted and Declarative.
A. Scripted Pipeline: It is based on Groovy script as their Domain Specific Language. One or more node blocks do the core work throughout the entire pipeline.
Syntax:
1. Executes the pipeline or any of its stages on any available agent
2. Defines the build stage
3. Performs steps related to building stage
4. Defines the test stage
5. Performs steps related to the test stage
6. Defines the deploy stage
7. Performs steps related to the deploy stage
 
B. Declarative Pipeline: It provides a simple and friendly syntax to define a pipeline. Here, the pipeline block defines the work done throughout the pipeline.
Syntax:
1. Executes the pipeline or any of its stages on any available agent
2. Defines the build stage
3. Performs steps related to building stage
4. Defines the test stage
5. Performs steps related to the test stage
6. Defines the deploy stage
7. Performs steps related to the deploy stage

##### What are the ways in which a build can be scheduled/run in Jenkins?
• By source code management commits.
• After completion of other builds.
• Scheduled to run at a specified time.
• Manual build requests.

##### What is an Ansible role?
An Ansible role is an independent block of tasks, variables, files, and templates embedded inside a playbook.
How do you run multiple containers using a single service?
• It is possible to run multiple containers as a single service with Docker Compose.
• Here, each container runs in isolation but can interact with each other.
• All Docker Compose files are YAML files.

##### What is a Dockerfile used for?
• A Dockerfile is used for creating Docker images using the build command.
• With a Docker image, any user can run the code to create Docker containers.
• Once a Docker image is built, it's uploaded in a Docker registry.
• From the Docker registry, users can get the Docker image and build new containers whenever they want.
Instead of YAML, what can you use as an alternate file for building Docker compose?
To build a Docker compose, a user can use a JSON file instead of YAML. In case a user wants to use a JSON file, he/she should specify the filename as given:
Docker-compose -f Docker-compose.json up

##### How do you create a Docker container?
Task: Create a MySQL Docker container
A user can either build a Docker image or pull an existing Docker image (like MySQL) from Docker Hub.
Now, Docker creates a new container MySQL from the existing Docker image. Simultaneously, the container layer of the read-write filesystem is also created on top of the image layer.
• Command to create a Docker container: Docker run -t –i MySQL
• Command to list down the running containers: Docker ps

##### What are the cloud platforms that support Docker?
The following are the cloud platforms that Docker runs on:
• Amazon Web Services
• Microsoft Azure
• Google Cloud Platform
• Rackspace

