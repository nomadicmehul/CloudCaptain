---
title: "Aws Interview Prep"
description: "Common Aws interview questions"
---

# Aws Interview Preparation

Comprehensive interview preparation guide for Aws. Check the main [Aws section](/docs/cloud/aws//) for detailed learning resources.

## Key Areas to Study

Review the [Aws documentation](/docs/cloud/aws//) and practice hands-on exercises.

## Practice Resources

- Review cheatsheets in the Aws section
- Complete hands-on exercises
- Study real-world architecture patterns
- Practice explaining concepts to others

## Core Concepts

### Load Balancer

A load balancer serves as the single point of contact for clients and distributes incoming application traffic across multiple targets, such as EC2 instances in multiple availability zones. This increases the availability of your application.

### Auto Scaling

AWS Auto Scaling monitors your applications and automatically adjusts capacity to maintain steady, predictable performance at the lowest possible cost. Using AWS Auto Scaling, you can setup application scaling for multiple resources across multiple services in minutes.

**Pay only for what you need** — AWS Auto Scaling helps you optimize utilization and cost efficiencies. When demand drops, it automatically removes excess resource capacity so you avoid overspending. AWS Auto Scaling is free to use.

### VPCs and Subnets

- A **subnet** defines a range of IP addresses in your VPC
- You can launch AWS resources into a subnet you select
- A **private subnet** should be used for resources that won't be accessible over the internet
- A **public subnet** should be used for resources that will be accessible over the internet
- Each subnet must reside entirely within one availability zone and cannot span zones

### Security Groups

1. Virtual firewalls that control inbound and outbound traffic for one or more instances
2. Deny all incoming traffic by default and use allow rules that can filter based on TCP, UDP, and ICMP protocols
3. Stateful — if your inbound request is allowed, the outbound response doesn't need inspection
4. Can define a source/target as either a CIDR block or another security group

### Internet Gateway

1. Directs traffic to your VPC
2. Allows communication between instances in your VPC and the internet
3. Horizontally scaled, redundant and highly available by default
4. Provides a target in your VPC route tables for internet-routable traffic

### DevOps Components

1. Continuous integration
2. Continuous testing
3. Continuous development
4. Continuous feedback
5. Continuous monitoring
6. Continuous deployment

### AWS CodeBuild

- Compiles source code, runs tests, and produces software packages ready for deployment
- You can provision, manage, and scale your own build servers

### AWS CodeDeploy

- Automated service that deploys code to any instances (local servers or Amazon EC2 instances)
- Handles all complexity involved in updating applications for release
- Helps users rapidly release new builds and features, avoiding downtime

### AWS CodePipeline

- Provides continuous integration and continuous delivery
- After every build, operations like building, testing, and deployment become easy
- Ensures delivering reliability for new software updates and features

### AWS CodeStar

- Enables you to quickly produce, build, and deploy applications on AWS
- Provides a unified interface to easily govern your software
- Set up entire continuous delivery toolchain in minutes

### DevOps vs Agile

- **Agile** — Revolves around seamless development or production of software
- **DevOps** — Deals with the deployment of software; focuses on faster turnaround time, high reliability, and minimum errors

### AWS Lambda (DevOps)

Lambda is a compute service where users can run their code without provisioning or managing servers explicitly. Users can run any code for their applications by uploading it; Lambda handles everything required to run and scale it.

### AWS CodeCommit

A source control service that hosts Git repositories safely in a highly scalable manner. Removes the need to setup and maintain a source control system.

### Infrastructure as Code (IaC)

A DevOps feature where code and software development techniques help manage overall infrastructure from continuous integration to version control systems. The API model in the cloud helps developers work on infrastructure programmatically.

### VPC in AWS DevOps

VPC stands for Virtual Private Cloud — a cloud network mapped to an AWS account. Helps users create regions, subnets, routing tables, and internet gateways. Provides ability to use EC2 or RDS per requirements.

### AWS CloudFormation

Provides simpler and easier access for businesses and developers. Builds a collection of related AWS resources and enables provisioning them in a predictable and orderly manner.

### Hybrid Cloud in AWS DevOps

A computation setting using a combination of private and public clouds. Created using a VPN tunnel between cloud VPN and on-premises network. AWS Direct Connect can bypass the internet and connect securely between VPN and a data center.

### EBS (Elastic Block Storage)

- Virtual storage area
- Block-level volumes of storage used in EC2 machines
- Highly compatible with other instances
- Stores data reliably

### Build Project

Defines how CodeBuild will run a build. Includes:
1. Where to get the source code?
2. Which build environment to use?
3. What build commands to run?
4. Where to store the build output?

### AWS IoT

A cloud management platform that provisions connected devices for interacting with cloud applications smoothly and securely.
