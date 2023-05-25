##### what is docker?
Docker is an open platform for developing, shipping, and running applications. Docker enables you to separate your applications from your infrastructure so you can deliver software quickly. With Docker, you can manage your infrastructure in the same ways you manage your applications. By taking advantage of Docker’s methodologies for shipping, testing, and deploying code quickly, you can significantly reduce the delay between writing code and running it in production.

##### What is Docker Compose
Compose is a tool for defining and running multi-container Docker applications. With Compose, you use a YAML file to configure your application’s services. Then, with a single command, you create and start all the services from your configuration.

##### YAML File
YAML stands for YAML ain't markup language, which is a data serialization language that is often used for writing configuration files. YAML will store data in key value pairs, YAML file is a space indented.


##### What is the purpose of the expose and publish commands in Docker?
Expose
• Expose is an instruction used in Dockerfile.
• It is used to expose ports within a Docker network.
• It is a documenting instruction used at the time of building an image and running a container.
• Expose is the command used in Docker.
• Example: Expose 8080
Publish
• Publish is used in a Docker run command.
• It can be used outside a Docker environment.
• It is used to map a host port to a running container port.
• --publish or –p is the command used in Docker.
• Example: docker run –d –p 0.0.0.80:80
