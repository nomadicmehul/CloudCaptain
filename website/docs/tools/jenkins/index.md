---
title: "Jenkins"
sidebar_label: "Jenkins Overview"
description: "Comprehensive Jenkins learning resources — CI/CD pipelines, Jenkinsfile, plugins, and interview prep"
sidebar_position: 0
---

# Jenkins

The most widely adopted open-source automation server for building CI/CD pipelines.

## Documentation

| Guide | Description |
|:------|:------------|
| [Jenkins Fundamentals](/docs/tools/jenkins/fundamentals) | Architecture, pipelines, Jenkinsfile, agents, plugins, shared libraries, exercises |
| [Command Cheat Sheet](/docs/tools/jenkins/cheatsheet) | Pipeline syntax, CLI commands, Groovy snippets, plugin reference |
| [Interview Questions](/docs/tools/jenkins/interview-questions) | 38 questions from beginner to advanced with detailed answers |

## Key Features

- **Automation** — Automate testing, building, and deployment
- **Plugins** — Integrate with hundreds of tools and services
- **Pipeline as Code** — Define builds as code (Jenkinsfile)
- **Distributed Builds** — Scale with agent nodes
- **Community** — Active community and extensive documentation

## Learning Path

1. [Start with fundamentals](/docs/tools/jenkins/fundamentals) — architecture, pipelines, Jenkinsfile, plugins
2. [Keep the cheat sheet handy](/docs/tools/jenkins/cheatsheet) — pipeline syntax and CLI commands
3. [Practice interview questions](/docs/tools/jenkins/interview-questions) — 38 Q&A with detailed explanations

## Quick Start

```groovy
// Jenkinsfile — Declarative Pipeline
pipeline {
    agent any
    stages {
        stage('Build') {
            steps {
                sh 'npm install'
                sh 'npm run build'
            }
        }
        stage('Test') {
            steps {
                sh 'npm test'
            }
        }
        stage('Deploy') {
            when { branch 'main' }
            steps {
                sh './deploy.sh'
            }
        }
    }
    post {
        failure {
            mail to: 'team@example.com',
                 subject: "Build Failed: ${currentBuild.fullDisplayName}",
                 body: "Check: ${env.BUILD_URL}"
        }
    }
}
```

## External Resources

| Resource | Description |
|:---------|:------------|
| [Jenkins Documentation](https://www.jenkins.io/doc/) | Official Jenkins docs |
| [Jenkins Pipeline Tutorial](https://www.lambdatest.com/blog/jenkins-pipeline-tutorial/) | Pipeline tutorial for beginners |
| [Scheduling Jenkins Jobs](https://www.codeproject.com/Articles/1242604/Scheduling-the-Jenkins-Job-with-CRON) | CRON scheduling guide |
| [Ansible + Docker CI/CD](https://gcore.com/blog/integrating-ansible-and-docker-in-ci-cd-process-using-jenkins-job/) | Integration with Ansible and Docker |
| [SonarQube Integration](https://www.tatvasoft.com/blog/integrate-sonarqube-with-jenkins/amp/) | Code quality with SonarQube |
