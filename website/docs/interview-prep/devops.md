---
title: "DevOps Interview Prep"
description: "Common DevOps interview questions and preparation guide"
---

# DevOps Interview Preparation

## Core Concepts

### What is DevOps?
DevOps is a set of practices that combines software development (Dev) and IT operations (Ops) to shorten the development lifecycle while delivering features, fixes, and updates frequently in close alignment with business objectives.

### Key Topics to Master

| Area | Topics |
|:-----|:-------|
| **CI/CD** | Pipeline design, blue-green deployments, canary releases, rollbacks |
| **IaC** | Terraform, CloudFormation, immutable infrastructure |
| **Containers** | Docker internals, orchestration, microservices |
| **Cloud** | AWS/Azure/GCP core services, architecture patterns |
| **Monitoring** | Observability, metrics, logs, traces, alerting strategies |
| **Security** | Shift-left, secrets management, supply chain security |
| **Culture** | Blameless postmortems, collaboration, continuous improvement |

### Common Questions

1. **Explain the difference between CI and CD**
2. **How would you design a deployment pipeline for a microservices application?**
3. **What is Infrastructure as Code and why is it important?**
4. **Describe blue-green vs canary deployment strategies**
5. **How do you handle secrets in a CI/CD pipeline?**
6. **What is the difference between monitoring and observability?**
7. **Explain the concept of "shifting left" in DevOps**
8. **How would you implement disaster recovery for a cloud application?**
9. **What are SLOs, SLIs, and SLAs?**
10. **Describe your experience with container orchestration**

### Tips for Success

- Focus on **real-world experience** and problem-solving
- Be prepared to **whiteboard architectures**
- Know your tools deeply — don't just name-drop
- Understand **trade-offs** in every decision
- Practice explaining complex topics simply

## Additional Concepts

### What is DevOps? (Extended)

DevOps is the combination of cultural philosophies, practices, and tools that increases an organization's ability to deliver applications and services at high velocity. It enables organizations to evolve and improve products faster than traditional software development and infrastructure management.

**DevOps Process** visualized as an infinite loop:
Plan → Code → Build → Test → Release → Deploy → Operate → Monitor

### DevOps and Cloud Computing

Development and Operations are considered a single entity in DevOps practice. Agile development alongside Cloud Computing provides straight-up advantage in scaling practices and business adaptability. Cloud is the car; DevOps is its wheels.

### Cloud Computing

On-demand delivery of IT resources and applications via the internet with pay-as-you-go model.

**Features:**
1. On-demand provisioning
2. Scalability in minutes (scale out or scale in)

**Cloud Models:**
- Public
- Private
- Hybrid
- Community

### LAMP Architecture

LAMP stands for Linux, Apache, MySQL, and PHP. Together, they provide proven software for delivering high-performance web applications.

### Top DevOps Tools

- **Configuration Management & Deployment:** Chef, Puppet, Ansible
- **Version Control:** Git
- **Continuous Integration:** Jenkins
- **Containerization:** Docker
- **Continuous Monitoring:** Nagios

### Main Goal of DevOps

Optimize the flow of value from idea to end-user. Make delivery effective and efficient. Focus on culture and cultural changes in the company.

### Agile vs DevOps

- **Agile** — Increases efficiency of developers and development cycles
- **DevOps** — Enables continuous integration and continuous delivery through operations team collaboration

### Jenkins

An open-source Java tool for automation and continuous integration. Features:
- Create and test projects continuously
- Integrate changes quickly
- Large number of plugins for integration with other software
- Handle entire development lifecycle: creation, testing, packaging, deployment, analysis

### Ansible

A RedHat product for service deployment. An open-source solution for:
- Software provisioning
- Application deployment
- Configuration management

Offers numerous facilities and is designed for multi-tier deployment to handle different systems together.

### Tomcat Server

Used for web applications written in Java that don't require full Java EE specifications. Provides a reliable tool for developing Java EE applications. Includes Servlet and JSP containers.

### Web Server

Computers that deliver requested web pages. Every web server has an IP address and domain name.

### Nginx

Open-source software designed for maximum performance and stability. A dedicated web server solving efficiency issues and handling thousands of requests concurrently.

**Features:**
1. Provides HTTP server capabilities
2. Designed for stability and maximum performance
3. Functions as proxy server for email (IMAP, POP3, SMTP)
4. Uses event-driven, non-threaded architecture
5. Provides scalability
6. Reduces client wait time
7. Upgrades possible without downtime

### CI/CD and AWS

CI/CD (Continuous Integration/Continuous Delivery or Continuous Deployment) combines processes that fill gaps between production and operational activities. AWS services automate creating, testing, and deploying applications. Progressive code changes are compiled, linked, and packaged into software deliverables.

### Continuous Testing in DevOps

Execution of automated tests every time code is merged. Enables engineers to get immediate feedback on recent code merges. Automation encouraged throughout development cycle.

### Container Resource Monitoring Tools

1. Grafana
2. Heapster
