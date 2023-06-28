<h1 align="center">Docker Basic Commands</h1> 
## Build Images

- Create image using current directory’s Dockerfile <br>
    ``` docker build -t image-name . ```

- List images<br>
   ``` docker images ```

<br>

## Running Container

- Create new container from specified image<br>
   ``` docker run image-name ```

- Assign a name to the container<br>
   ``` docker run --name nick-name image-name ```

- Run image with an entry point | override existing entrypoint<br>
   ```docker run image-name cmd```<br>
   ```docker run image-name --entrypoint cmd```

- Run image in interactive mode<br>
   ``` docker run -it image-name ```

- Run image in detached mode<br>
   ``` docker run -d image-name ```

- Run image mapping container's port to the host<br>
   ``` docker run -p host-port:container-port image-name ```

<br>

## Managing Containers

- List running containers<br>
   ``` docker ps ```

- List all containers<br>
   ```docker ps -a```

- Stop one or more running containers<br>
   ```docker stop containerId```

- Start one or more stopped containers<br>
   ```docker start containerId```

- Fetch the logs of a container<br>
   ```docker logs containerId```

- Fetch and follow log output of a container<br>
   ```docker logs -f containerId```

- Run a command in a running container in interactive mode<br>
   ```docker exec -it containerId cmd```

- Copy files/folders from container to local filesystem<br>
  ```  docker cp containerId:/workdir/file.ext .```

- Copy files/folders from local filesystem to container<br>
  ```  docker cp  file.ext containerId:/workdir/```

- Remove container<br>
   ```docker rm containerId```

- Remove running container<br>
   ```docker rm -f containerId```

- Remove all running and stopped containers<br>
   ```docker rm -f $(docker ps -a -q)```

<br>


## Persistant data using Volumes

- Creates a new volume that containers store data<br>
   ```docker volume create volume-name```

- Display detailed information on one or more volumes<br>
   ```docker volume inspect volume-name```

- List volumes<br>
   ```docker volume ls```

- Create a volume and then configure the container to use it<br>
  ``` docker run -v volume-name:/dir/dir container-name```

- Create mapping between dir in host and container<br>
  ``` docker run -v $(pwd):/workdir container-name```




<br>

## Managing Images

#### Tagging Images

- Create tag to the image while building<br>
   ``` docker build -t image-name:tag . ```

- Create tag to the image after building<br>
   ``` docker image tag src-image:latest dst-image:tag ```

#### Saving & Loading Images

- Save one or more images to a tar archive<br>
   ``` docker image save -o image-name.tar image-name:tag ```

- Load an image from a tar archive<br>
   ``` docker image load -i image-name.tar ```

#### Remove Images

- Remove one or more images<br>
   ``` docker image rm image-name ```<br>
   ``` docker rmi image-name ```

- Remove all images<br>
   ``` docker system prune -a ```

- Remove all stopped containers<br>
   ``` docker container prune ```

- Remove all dangling images<br>
   ``` docker image prune ```

- Remove all unused containers, networks, dangling and unreferenced images<br>
   ``` docker system prune ```

  ******************************************************

### Docker Swarm 
- Initialize

``` docker swarm init ```

- Join Docker Cluster

```` docker swarm join --token SWMTKN-1-3pu6hszjas19xyp7ghgosyx9k8atbfcr8p2is99znpy26u2lkl-1awxwuwd3z9j1z3puu7rcgdbx <manager/worker>:2377 ```

- List Docker Nodes in Swarm Cluster

``` docker node ls ```

- List all running applications on this Docker host

``` docker stack ls ```

- Run the specified Compose file

``` docker stack deploy -c <composefile> <STACK_NAME> ```

- List the services associated with an app

``` docker stack services <appname> ```

- List the running containers associated with an app

``` docker stack ps <appname> ```

- Tear down an application

``` docker stack rm <STACK_NAME>alias dstr='docker stack rm' ```

- Docker Swarm Service list

``` docker service ls ```
``` alias dsls='docker service ls' ```

- List the tasks of one or more services

``` docker service ps <service_name>  ```
``` alias dsp='docker service ps' ```

- Docker Swarm Service logs

``` alias dsl='docker service logs' ```

- Remove specific docker swarm service

``` alias dsr='docker service rm' ```

- Remove unused Containers, Images, Network, etc.

``` alias sprune='docker system prune' ```

- Remove unused Volumes

``` alias vprune='docker volume prune' ```

- Create Secret

``` docker secret create <SECRET_NAME> <SECRET_PATH> ```

- Create Config

``` docker config create <CONFIG_NAME> <CONFIG_FILE_PATH> ```


