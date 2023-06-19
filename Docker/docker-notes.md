## Docker Notes:  ![Docker](https://img.shields.io/badge/Docker-Notes-blue?style=for-the-badge)

***********************

* [Dockerfile Format](#docker-fileformats)

* [Docker Commands](#docker-build--run--)

* [Docker Compose](#docker-compose--)

* [Docker Swarm](#docker-swarm--)



**********************

- Why DOCKER?

    - An application is working fine on developer console but not in testing or in production.

    - In Dev there can be a software which is updated but in testing and production an older version is being used.

    - Docker is a computer program/tool that makes it easier to deploy and run applications using a concept known as “containerization”.

    - Imagine as a developer your application is packaged up with all of the parts it needs, such as libraries and other dependencies, and it is shipped out as one package

    - Docker in other words is a light weight virtualization tool and containerizing platform where you can run and deploy applications and its dependencies which can be run in a Linux environment.

    - Docker is an open-source project based on Linux containers. It uses Linux Kernel features like namespaces and control groups to create containers on top of an operating system.

- Docker Engine

    - Docker engine is the layer on which Docker runs. It’s a lightweight runtime and tooling that manages containers, images, builds, and more. It runs natively on Linux systems and is made up of:
      - 1. A Docker Daemon that runs in the host computer.
      - 2. A Docker Client that then communicates with the Docker Daemon to execute commands.
      - 3. A REST API for interacting with the Docker Daemon remotely.

- Docker Client

    - The Docker Client is what you, as the end-user of Docker, communicate with. Think of it as the UI for Docker. 

- Docker Daemon

    - The Docker daemon is what actually executes commands sent to the Docker Client — like building, running, and distributing your containers. The Docker Daemon runs on the host machine, but as a user, you never communicate directly with the Daemon. The Docker Client can run on the host machine as well, but it’s not required to. It can run on a different machine and communicate with the Docker Daemon that’s running on the host machine.

- Volumes

    - Volumes are the “data” part of a container, initialized when a container is created. Volumes allow you to persist and share a container’s data. Data volumes are separate from the default Union File System and exist as normal directories and files on the host filesystem. So, even if you destroy, update, or rebuild your container, the data volumes will remain untouched. When you want to update a volume, you make changes to it directly. (As an added bonus, data volumes can be shared and reused among multiple containers, which is pretty neat.)



- Microservice Architecture : 

    - The idea behind microservice is some application is easier to build and maintain where broken down to smaller parts.
    - Each component is developed separately and done....

    - Example : Online Shopping Service : 
		- Account Service
		- Product Catalog
		- Cart Server
		- Order Server

- Advantages of microservice architecture : 

	- Building and maintenance is easy as broken down to smaller parts.
	- If we need some new features or update in a module,
		 it is easier because dependencies will be less compared to the application as a whole.
	- If any component go down, application will be largely unaffected.

- What is the problem to adopting microservices :

    - Before DOCKER : For microservice architecture we have a host machine and there are several virtual machines 
				each virtual machine is for a microservice. So problem is that lots of resource waste.
				As we use more and more VMs for bigger application lots of disc space, RAM are unused.

- How Docker solve this problem :

	- We can run several microservices in one virtual machine by running various docker
	- containers for each microservice.
	- Docker do not need any RAM,DISK requirements initially.

- How Docker solves the problem "not having consistent computing environment throughout the process of delivery (development, testing, production)" :

	- Docker containers are developed by the developers.
	- Docker provides a consistent computing environment throughout the whole SDLC (Software Development Life Cycle).

- What is an Image?
  - Docker image is the basis of containers. It’s a collection of layers stacked on top of each other. Each Docker image references a list of read-only layers that represent filesystem differences. Think of it like the jar file for java applications, you create one jar file but you can deploy it anywhere a java run time is enabled.
  - A docker image is an archive containing all the files that go in a container.
  - You can create many docker containers from the same docker image.
  - The image can then be deployed to any Docker environment and executable as a container.
  - A Docker image is containing everything needed to run an application as a container. This includes:

    - code
    - runtime
    - libraries
    - environment variables
    - configuration files
    
    ![docker image](https://github.com/Tikam02/DevOps-Guide/blob/master/img/container-layers.jpg)

		
- What is a container?

	- A container is a standard unit of software that packages up code and all its dependencies so the application runs quickly and reliably from one computing environment to another.

	- A docker container image is s lightweight, standalone, executable package of software that includes everything needed to run an application: code, libraries, settings etc.

	- Container images become containers at runtime and in the case of docker containers images become containers when they run on docker engine.

	- Containers share the machine's OS system kernel and therefore do not require an OS per application.

	- Applications are safer in containers and Docker provides the strongest default isolation capabilities in the industry.

    - Docker container is the actual running piece created from a docker image. The only difference between a docker image and a docker container is a top writable layer. When you create a new container, you add a new, thin, writable layer on top of the underlying stack. This layer is often called the “container layer”. All changes made to the running container — such as writing new files, modifying existing files, and deleting files — are written to this thin writable container layer. But once you delete the container, this top layer will be deleted as well. So it’s not persistent. The best thing with docker is that you can create a docker image using the current docker container with a commit. Hence, enabling us to capture system information and make it immutable so its reproducible anywhere. This solves many of the server related problems we encounter these days.
    
    ![Docker container](https://github.com/Tikam02/DevOps-Guide/blob/master/img/docker-image.png)  
    
  

- Containers Vs VM

    - When talking about containerization it is very often compared to virtual machines. Let’s take a look at the following image to see the main difference :
  
    - The Docker container platform is always running on top of the host operating system. Containers are containing the binaries, libraries, and the application itself. Containers do not contain a guest operating system which ensures that containers are lightweight.
  
    -In contrast virtual machines are running on a hypervisor (responsible for running virtual machines) and include it’s own guest operating system. This increased the size of the virtual machines significantly, makes setting up virtual machines more complex and requires more resources to run each virtual machine.

    ![container vs VM](https://github.com/Tikam02/DevOps-Guide/blob/master/img/dockervsvm.png)

- Dockerfile

    - Blueprint of a docker image (a text document) is known as Dockerfile. This file contains all the commands you would run in order to build the docker image you want. Docker can build images reading this file, which is one of the key advantages of docker.

    ```bash
    # Super simple example of a Dockerfile
    FROM ubuntu:latest
    MAINTAINER Tikam Alma 

    RUN apt-get update
    RUN apt-get install -y python python-pip wget
    RUN pip install Flask

    ADD hello.py /home/hello.py

    WORKDIR /home
    ```

    - We first write a Dockerfile which is like the definition of the image. Using the Dockerfile we create a docker image. We then push this image to Docker Hub and provide a unique tag that can be used to identify our image. Using this tag and image name, we can pull the docker image and deploy on another computer as a docker container.


*******************************

## Working of Containers Deep-Dive

* The term “container” is really just an abstract concept to describe how a few different features work together to visualize a “container”. Let’s run through them real quick:
- 1) Namespaces

    - Namespaces provide containers with their own view of the underlying Linux system, limiting what the container can see and access. When you run a container, Docker creates namespaces that the specific container will use.

    - There are several different types of namespaces in a kernel that Docker makes use of, for example:

        - a. NET: Provides a container with its own view of the network stack of the system (e.g. its own network devices, IP addresses, IP routing tables, /proc/net directory, port numbers, etc.).
  
        - b. PID: PID stands for Process ID. If you’ve ever ran ps aux in the command line to check what processes are running on your system, you’ll have seen a column named “PID”. The PID namespace gives containers their own scoped view of processes they can view and interact with, including an independent init (PID 1), which is the “ancestor of all processes”.
  
        - c. MNT: Gives a container its own view of the “mounts” on the system. So, processes in different mount namespaces have different views of the filesystem hierarchy.

        - d. UTS: UTS stands for UNIX Timesharing System. It allows a process to identify system identifiers (i.e. hostname, domainname, etc.). UTS allows containers to have their own hostname and NIS domain name that is independent of other containers and the host system.

        - e. IPC: IPC stands for InterProcess Communication. IPC namespace is responsible for isolating IPC resources between processes running inside each container.

        - f. USER: This namespace is used to isolate users within each container. It functions by allowing containers to have a different view of the uid (user ID) and gid (group ID) ranges, as compared with the host system. As a result, a process’s uid and gid can be different inside and outside a user namespace, which also allows a process to have an unprivileged user outside a container without sacrificing root privilege inside a container.
  
- 2) Control groups
       -  Control groups (also called cgroups) is a Linux kernel feature that isolates, prioritizes, and accounts for the resource usage (CPU, memory, disk I/O, network, etc.) of a set of processes. In this sense, a cgroup ensures that Docker containers only use the resources they need — and, if needed, set up limits to what resources a container *can* use. Cgroups also ensure that a single container doesn’t exhaust one of those resources and bring the entire system down.

- 3) Isolated Union file system:
      - Docker uses Union File Systems to build up an image. You can think of a Union File System as a stackable file system, meaning files and directories of separate file systems (known as branches) can be transparently overlaid to form a single file system.

      - The contents of directories which have the same path within the overlaid branches are seen as a single merged directory, which avoids the need to create separate copies of each layer. Instead, they can all be given pointers to the same resource; when certain layers need to be modified, it’ll create a copy and modify a local copy, leaving the original unchanged. That’s how file systems can *appear* writable without actually allowing writes. (In other words, a “copy-on-write” system.)

      - Layered systems offer two main benefits:
          - 1. Duplication-free: layers help avoid duplicating a complete set of files every time you use an image to create and run a new container, making instantiation of docker containers very fast and cheap.
          - 2. Layer segregation: Making a change is much faster — when you change an image, Docker only propagates the updates to the layer that was changed.
    
    

 **********************   


### Docker fileformats 

* FROM

Usage:

    FROM <image>
    FROM <image>:<tag>
    FROM <image>@<digest>

Information:

   -  FROM must be the first non-comment instruction in the Dockerfile.
   - FROM can appear multiple times within a single Dockerfile in order to create multiple images. Simply make a note of the last image ID output by the commit before each new FROM command.
   - The tag or digest values are optional. If you omit either of them, the builder assumes a latest by default. The builder returns an error if it cannot match the tag value.

Reference - Best Practices

* MAINTAINER

Usage:

    MAINTAINER <name>

The MAINTAINER instruction allows you to set the Author field of the generated images.

Reference

* RUN

Usage:

    RUN <command> (shell form, the command is run in a shell, which by default is /bin/sh -c on Linux or cmd /S /C on Windows)
    RUN ["<executable>", "<param1>", "<param2>"] (exec form)

Information:

    The exec form makes it possible to avoid shell string munging, and to RUN commands using a base image that does not contain the specified shell executable.
    The default shell for the shell form can be changed using the SHELL command.
    Normal shell processing does not occur when using the exec form. For example, RUN ["echo", "$HOME"] will not do variable substitution on $HOME.

Reference - Best Practices

* CMD

Usage:

    CMD ["<executable>","<param1>","<param2>"] (exec form, this is the preferred form)
    CMD ["<param1>","<param2>"] (as default parameters to ENTRYPOINT)
    CMD <command> <param1> <param2> (shell form)

Information:

    The main purpose of a CMD is to provide defaults for an executing container. These defaults can include an executable, or they can omit the executable, in which case you must specify an ENTRYPOINT instruction as well.
    There can only be one CMD instruction in a Dockerfile. If you list more than one CMD then only the last CMD will take effect.
    If CMD is used to provide default arguments for the ENTRYPOINT instruction, both the CMD and ENTRYPOINT instructions should be specified with the JSON array format.
    If the user specifies arguments to docker run then they will override the default specified in CMD.
    Normal shell processing does not occur when using the exec form. For example, CMD ["echo", "$HOME"] will not do variable substitution on $HOME.

Reference - Best Practices

* LABEL

Usage:

    LABEL <key>=<value> [<key>=<value> ...]

Information:

    The LABEL instruction adds metadata to an image.
    To include spaces within a LABEL value, use quotes and backslashes as you would in command-line parsing.
    Labels are additive including LABELs in FROM images.
    If Docker encounters a label/key that already exists, the new value overrides any previous labels with identical keys.
    To view an image’s labels, use the docker inspect command. They will be under the "Labels" JSON attribute.

Reference - Best Practices

EXPOSE

Usage:

    EXPOSE <port> [<port> ...]

Information:

    Informs Docker that the container listens on the specified network port(s) at runtime.
    EXPOSE does not make the ports of the container accessible to the host.

Reference - Best Practices

ENV

Usage:

    ENV <key> <value>
    ENV <key>=<value> [<key>=<value> ...]

Information:

    The ENV instruction sets the environment variable <key> to the value <value>.
    The value will be in the environment of all “descendant” Dockerfile commands and can be replaced inline as well.
    The environment variables set using ENV will persist when a container is run from the resulting image.
    The first form will set a single variable to a value with the entire string after the first space being treated as the <value> - including characters such as spaces and quotes.

Reference - Best Practices

ADD

Usage:

    ADD <src> [<src> ...] <dest>
    ADD ["<src>", ... "<dest>"] (this form is required for paths containing whitespace)

Information:

    Copies new files, directories, or remote file URLs from <src> and adds them to the filesystem of the image at the path <dest>.
    <src> may contain wildcards and matching will be done using Go’s filepath.Match rules.
    If <src> is a file or directory, then they must be relative to the source directory that is being built (the context of the build).
    <dest> is an absolute path, or a path relative to WORKDIR.
    If <dest> doesn’t exist, it is created along with all missing directories in its path.

Reference - Best Practices

COPY

Usage:

    COPY <src> [<src> ...] <dest>
    COPY ["<src>", ... "<dest>"] (this form is required for paths containing whitespace)

Information:

    Copies new files or directories from <src> and adds them to the filesystem of the image at the path <dest>.
    <src> may contain wildcards and matching will be done using Go’s filepath.Match rules.
    <src> must be relative to the source directory that is being built (the context of the build).
    <dest> is an absolute path, or a path relative to WORKDIR.
    If <dest> doesn’t exist, it is created along with all missing directories in its path.

Reference - Best Practices

ENTRYPOINT

Usage:

    ENTRYPOINT ["<executable>", "<param1>", "<param2>"] (exec form, preferred)
    ENTRYPOINT <command> <param1> <param2> (shell form)

Information:

    Allows you to configure a container that will run as an executable.
    Command line arguments to docker run <image> will be appended after all elements in an exec form ENTRYPOINT and will override all elements specified using CMD.
    The shell form prevents any CMD or run command line arguments from being used, but the ENTRYPOINT will start via the shell. This means the executable will not be PID 1 nor will it receive UNIX signals. Prepend exec to get around this drawback.
    Only the last ENTRYPOINT instruction in the Dockerfile will have an effect.

Reference - Best Practices

VOLUME

Usage:

    VOLUME ["<path>", ...]
    VOLUME <path> [<path> ...]

Creates a mount point with the specified name and marks it as holding externally mounted volumes from native host or other containers.

Reference - Best Practices

USER

Usage:

    USER <username | UID>

The USER instruction sets the user name or UID to use when running the image and for any RUN, CMD and ENTRYPOINT instructions that follow it in the Dockerfile.

Reference - Best Practices

WORKDIR

Usage:

    WORKDIR </path/to/workdir>

Information:

    Sets the working directory for any RUN, CMD, ENTRYPOINT, COPY, and ADD instructions that follow it.
    It can be used multiple times in the one Dockerfile. If a relative path is provided, it will be relative to the path of the previous WORKDIR instruction.

Reference - Best Practices

ARG

Usage:

    ARG <name>[=<default value>]

Information:

    Defines a variable that users can pass at build-time to the builder with the docker build command using the --build-arg <varname>=<value> flag.
    Multiple variables may be defined by specifying ARG multiple times.
    It is not recommended to use build-time variables for passing secrets like github keys, user credentials, etc. Build-time variable values are visible to any user of the image with the docker history command.
    Environment variables defined using the ENV instruction always override an ARG instruction of the same name.
    Docker has a set of predefined ARG variables that you can use without a corresponding ARG instruction in the Dockerfile.
        HTTP_PROXY and http_proxy
        HTTPS_PROXY and https_proxy
        FTP_PROXY and ftp_proxy
        NO_PROXY and no_proxy

Reference

ONBUILD

Usage:

    ONBUILD <Dockerfile INSTRUCTION>

Information:

    Adds to the image a trigger instruction to be executed at a later time, when the image is used as the base for another build. The trigger will be executed in the context of the downstream build, as if it had been inserted immediately after the FROM instruction in the downstream Dockerfile.
    Any build instruction can be registered as a trigger.
    Triggers are inherited by the "child" build only. In other words, they are not inherited by "grand-children" builds.
    The ONBUILD instruction may not trigger FROM, MAINTAINER, or ONBUILD instructions.

Reference - Best Practices

STOPSIGNAL

Usage:

    STOPSIGNAL <signal>

The STOPSIGNAL instruction sets the system call signal that will be sent to the container to exit. This signal can be a valid unsigned number that matches a position in the kernel’s syscall table, for instance 9, or a signal name in the format SIGNAME, for instance SIGKILL.

Reference

HEALTHCHECK

Usage:

    HEALTHCHECK [<options>] CMD <command> (check container health by running a command inside the container)
    HEALTHCHECK NONE (disable any healthcheck inherited from the base image)

Information:

    Tells Docker how to test a container to check that it is still working
    Whenever a health check passes, it becomes healthy. After a certain number of consecutive failures, it becomes unhealthy.
    The <options> that can appear are...
        --interval=<duration> (default: 30s)
        --timeout=<duration> (default: 30s)
        --retries=<number> (default: 3)
    The health check will first run interval seconds after the container is started, and then again interval seconds after each previous check completes. If a single run of the check takes longer than timeout seconds then the check is considered to have failed. It takes retries consecutive failures of the health check for the container to be considered unhealthy.
    There can only be one HEALTHCHECK instruction in a Dockerfile. If you list more than one then only the last HEALTHCHECK will take effect.
    <command> can be either a shell command or an exec JSON array.
    The command's exit status indicates the health status of the container.
        0: success - the container is healthy and ready for use
        1: unhealthy - the container is not working correctly
        2: reserved - do not use this exit code
    The first 4096 bytes of stdout and stderr from the <command> are stored and can be queried with docker inspect.
    When the health status of a container changes, a health_status event is generated with the new status.

Reference

SHELL

Usage:

    SHELL ["<executable>", "<param1>", "<param2>"]

Information:

    Allows the default shell used for the shell form of commands to be overridden.
    Each SHELL instruction overrides all previous SHELL instructions, and affects all subsequent instructions.
    Allows an alternate shell be used such as zsh, csh, tcsh, powershell, and others.




Basic Docker Commands

1) Docker run - Start a Container
2) Docker ps - List all the running Container
3) Docker ps -a - List all the running as well as stoped docker container
4) Docker stop <container-id> or <container-name> - to stop container
5) Docker rm <container-id> or <container-name> - to remove the containner
6) Docker images - show all the images find locally or downloaded
7) Docker rmi <image-name> - remove the image that is specified
8) Docker pull <image-name> - to pull image but not run
9) Docker exec - to see what inside running container
10) Docker run -d <image-name> - run image in the background mode
