##### what is docker?
Docker is an open platform for developing, shipping, and running applications. Docker enables you to separate your applications from your infrastructure so you can deliver software quickly. With Docker, you can manage your infrastructure in the same ways you manage your applications. By taking advantage of Docker’s methodologies for shipping, testing, and deploying code quickly, you can significantly reduce the delay between writing code and running it in production.

##### What is Docker Compose
Compose is a tool for defining and running multi-container Docker applications. With Compose, you use a YAML file to configure your application’s services. Then, with a single command, you create and start all the services from your configuration.

##### YAML File
YAML stands for YAML ain't markup language, which is a data serialization language that is often used for writing configuration files. YAML will store data in key value pairs, YAML file is a space indented.

##### Why use Docker?
Answer:
1. A user can quickly build, ship, and run its applications.
2. A single operating system kernel will run all containers.
3. Docker container is more light-weight than the virtual machines.
4. A user will deploy Docker containers anywhere, on any physical and virtual machines and even on the
cloud.

##### List the most used commands of Docker.
Answer:
 1. ps lists the running containers.
2. dockerd launches Docker Daemon.
3. build is used to build an image from a DockerFile.
4. create is used to create a new image form container’s changes.
5. pull is used to download a specific image or a repository.
6. run is used to run a container.
7. logs display the logs of a container.
8. rm removes one or more containers.
9.rmi removes one or more images.
10. stop is used to stop one or more container.
11. kill is used to kill all running containers.

##### Does the data get lost, if the Docker container exits?
Answer: No. any data which application will write to disk can get well preserved in its container until we will explicitly delete the container and the file system will persist even after the container halts.

##### What is Dockerfile and its use?
Answer: DockerFile is a text document which is used to assemble a Docker image. It is consist of a list of Docker commands and operating system commands for building an image. These commands will execute automatically in sequence in the Docker environment and create a new Docker image.

##### How Docker is advantageous over Hypervisors?
Answer:
Docker is advantageous in following way.
1. It is lightweight.
2. More efficient in terms of resources.
3. It uses very fewer resources and also the underlying host kernel rather than developing its hypervisor.

##### Is Docker a Microservice?
Answer: Docker will Benefits for Microservices. Docker, as a containerization tool, will be often compared to virtual machines. Virtual machines are introduced to optimize the use of computing resources. We will run several VMs on a single server and deploy each application instance on a separate virtual machine.

##### How much does Docker cost?
Answer: If we required to run Docker in production, however, the company will encourage users to sign up for a subscription package for an enterprise version of the platform. Docker will offer three enterprise editions of its software. Pricing which is start at $750 per node per year.

##### What is docker in AWS?
Answer: Docker is a software platform which will allow to build, test, and deploy applications quickly. Running Docker on AWS will provide developers and admins a highly reliable, low-cost way for building, ship, and run distributed applications at any scale.

##### Is Kubernetes free?
Answer: Pure open source Kubernetes is free and it can be downloaded from its repository on GitHub. Administrators should build and deploy the Kubernetes release to a local system or cluster or to a
system or cluster in a public cloud, such as AWS, Google Cloud Platform (GCP) or Microsoft Azure.

##### Explain how you can clone a Git repository via Jenkins?
Answer: To clone a Git repository via Jenkins, we have to enter the e-mail and user name for Jenkins system. To do that we have to switch into job directory and execute the “git config” command.

##### What is Docker Hub?
Answer: Docker hub is a cloud-based registry that will help us to link code repositories. It will allows us to build, test, store image in the Docker cloud. We can deploy the image to host with the help of the Docker hub.

##### What are the important features of Docker?
Answer: Following are the essential features of Docker:
1. Easy Modeling
2. version Control
3. Placement/Affinity
4. Application Agility
5. Developer Productivity
6. Operational Efficiencies

##### What are the main drawbacks of Docker?
Answer: Following are the disadvantages of Docker which we should keep in mind
1. It will not provide a storage option.
2. Offer a poor monitoring option.
3. No automatic rescheduling of inactive Nodes.
4. Complicated automatic horizontal scaling set up.

##### Tell us something about Docker Compose.
Answer: Docker Compose is a YAML file which will contain details about the service, network, and volumes to set up the Docker application. Therefore, we will use Docker compose for creating separate containers, host them and get them to communicate with other containers.

##### What is Docker Swarm?
Answer: Docker Swarm is native clustering for Docker. It will turn a pool of Docker hosts into a single, virtual Docker host. Docker Swarm will serve the standard Docker API, any tool which already virtual Docker host. communicates with a Docker daemon will be use Swarm to transparently scale to multiple hosts.

##### What is Docker Engine?
Answer: Docker daemon or Docker engine will represent the server. The docker daemon and the clients will run on the same or remote host, which will communicate through command-line client binary and
full RESTful API.

##### What command should you run to see all running container in Docker?
Answer: $docker ps

##### Write the command to stop the Docker Container.
Answer: $ sudo docker stop container name

##### What is the command to run the image as a container?
Answer: $ sudo docker run -i -t alpine /bin/bash

##### Explain Docker object labels?
Answer: Docker object labels is a method to apply metadata to docker objects which will include images,
containers, volumes, network, swarm nodes, and services.

##### Write a Docker file to create and copy a directory and built it using python modules?
Answer: FROM pyhton:2.7-slim
WORKDIR /app
COPY . /app
docker build –tag

##### Where the docker volumes are stored?
Answer: We required to navigate
/var/lib/docker/volumes

##### How do you run multiple copies of Compose file on the same host?
Answer: Compose will use the project name which will allow us for creating unique identifiers for all of a
project’s containers and other resources. To run multiple copies of a project, set a custom project name
using the -a command-line option or using COMPOSE_PROJECT_NAME environment variable.

##### Did Docker come up with the ‘container’ technology?
Answer: No, Docker did not come up with the container technology. Multiple other development tools offer containers similar to Docker.

##### How is Docker better than other tools that use containers, then?
Answer: Docker will utilise the cloud to run its container-related operations – which is not used by many other development tools. Docker will become much more flexible and adaptable to different scenarios using the cloud, which might come up during the development or shipment processes. This is the main reason to use the Docker when compared to other container-based developer tools.

##### What is a Dockerfile?
Answer: A Dockerfile is a set of instructions. Developers provided Docker with such instructions therefore the program could do the job correctly, with those specific parameters in mind.

##### What are the three main types of Docker components?
Answer: The Client, the Host, and the Registry.
The client is the component which will issue “run” and “build” commands to the host. The host is where all of the containers and images will be created. They will be then sent to the registry, for execution.

##### Will you lose all of your work if you accidentally exit a container?
Answer: No, We won’t lose any information, data and other parameters if we accidentally exit the Docker container. The only way to lose progress would be to issue a specific command to delete the container – exiting it won’t do the files within any harm.

##### Can you use any other files for composing instead of the default YAML?
Answer: Yes, The more popular version to use than YAML is the good-old JSON.

##### What is ‘NameSpaces’ used for?
Answer: NameSpaces will isolate the Docker containers from other activities or tampering with them.
Q. What is the single most important requirement for building a Docker container?
Answer: The most important requirement for building a container with Docker is the default image. This default image will vary depending on the code that we are using. To find out and access the default image, we should go to the Docker Hub and search for the specific domain that we required. After we find the image, all that’s left to do is deal with the documentation and that’s it – we can create a Dockercontainer.

##### How does Docker manage ‘Dockerized nodes’?
Answer: A Dockerized node can be any machine which has Docker installed and running. Docker will manage both in-house and cloud-based nodes. Therefore, whether the node will exist in the area of the main computer running Docker or it is present on the cloud – it will not matter. Docker can manage it without a problem.

##### What are the main factors that dictate the number of containers you can run?
Answer: There is no defined limit of containers that we can run with Docker. The limitations may start
due to hardware.

##### How is Docker different from Hypervisor?
Answer: Hypervisor will require to have extensive hardware to function properly, while Docker will runon the actual operating system. This will allow Docker to be exceptionally fast and perform tasks in a
fluid manner – something which Hypervisor tends to lack.

##### Can I use JSON instead of YAML for my compose file in Docker?
Answer: YES, We can very comfortably use JSON instead of the default YAML for Docker compose file. In
order to use JSON file with compose, we required to specify the filename to use as the following:docker-compose -f docker-compose.json up

##### Tell us how you have used Docker in your past position?
Answer: We could also explain the ease that this technology has brought in the automation of the development to production lifecycle management. We can also discuss about any other integrations that we might have worked along with Docker like Puppet, Chef or even the most popular of all technologies – Jenkins.

##### How to create Docker container?
Answer: We will create a Docker container out of any specific Docker image of choice and the same can
be achieved using the command given below:
docker run -t -i command name

