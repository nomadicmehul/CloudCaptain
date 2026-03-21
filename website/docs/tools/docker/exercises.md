---
title: "Docker Exercises"
description: "Hands-on Docker practice exercises"
---

# Docker Exercises

## Exercise 01: Docker Installation on Ubuntu

### Step 1: Prepare System

Open a terminal and update the apt package index:

```bash
sudo apt-get update
```

Install packages to allow apt to use a repository over HTTPS:

```bash
sudo apt-get install \
  apt-transport-https \
  ca-certificates \
  curl \
  software-properties-common
```

### Step 2: Add Docker Repository

Add Docker's official GPG key:

```bash
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo apt-key add -
```

Verify that you have the key with fingerprint `9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88`:

```bash
sudo apt-key fingerprint 0EBFCD88
```

Set up the stable repository:

```bash
sudo add-apt-repository \
  "deb [arch=amd64] https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) \
  stable"
```

### Step 3: Install Docker

Update the apt package index:

```bash
sudo apt-get update
```

Install the latest version of Docker CE:

```bash
sudo apt-get install docker-ce
sudo docker --version
```

---

## Exercise 02: Automate Building Images

### Objective

Build a custom Docker image with Java and JTrac application.

### Step 1: Firewall Configuration

Create a firewall rule to allow HTTP traffic on port 8080:
- **Name**: `default-allow-http-8080`
- **Source IP**: `0.0.0.0/0`
- **Protocols/Ports**: `tcp:8080`

### Step 2: Create Dockerfile

Create a new directory and Dockerfile:

```bash
mkdir jtrac
cd jtrac
```

Create a Dockerfile with the following content:

```dockerfile
FROM centos:latest
RUN yum install -y wget
RUN yum install -y zip
RUN cd /tmp && wget -q --no-check-certificate --no-cookies \
  --header "Cookie: oraclelicense=accept-securebackup-cookie" \
  http://download.oracle.com/otn-pub/java/jdk/8u181-b13/96a7b8442fe848ef90c96a2fad6ed6d1/jdk-8u181-linux-x64.rpm
RUN rpm -i /tmp/jdk*linux-x64.rpm
ENV JAVA_HOME=/usr/java/latest
RUN cd /tmp && wget https://sourceforge.net/projects/j-trac/files/jtrac/2.1.0/jtrac-2.1.0.zip
RUN yum install -y unzip
RUN cd /opt && unzip /tmp/jtrac*.zip
RUN cd /opt/jtrac && mv start.bat start.sh && chmod +x start.sh
RUN rm /tmp/jdk*linux-x64.rpm /tmp/jtrac*.zip
WORKDIR /opt/jtrac
CMD ./start.sh
```

### Step 3: Build and Run

Build the Docker image:

```bash
docker build -t jtrac .
docker images
```

Run the container:

```bash
docker run -d --name jtrac -p 8080:80 jtrac
docker ps
```

### Step 4: Access Application

1. Find the external IP address of your VM
2. Open a browser and navigate to `http://YOUR_IP:8080/jtrac`
3. Log in with default credentials:
   - **Username**: `admin`
   - **Password**: `admin`
