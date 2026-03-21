---
title: Jenkins Cheat Sheet
sidebar_label: Cheat Sheet
sidebar_position: 2
---

# Jenkins Cheat Sheet

## Installation

### Docker

```bash
# Pull and run Jenkins
docker run -d -p 8080:8080 -p 50000:50000 \
  -v jenkins_home:/var/jenkins_home \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --name jenkins \
  jenkins/jenkins:latest

# Get initial admin password
docker logs jenkins | grep -A 5 "initialAdminPassword"
```

### Linux

```bash
# Ubuntu/Debian
wget -q -O - https://pkg.jenkins.io/debian-stable/jenkins.io.key | \
  sudo apt-key add -
echo "deb https://pkg.jenkins.io/debian-stable binary/" | \
  sudo tee /etc/apt/sources.list.d/jenkins.list
sudo apt-get update
sudo apt-get install jenkins

# Start service
sudo systemctl start jenkins
sudo systemctl enable jenkins
sudo systemctl status jenkins
```

## Jenkins CLI

### Basic Commands

```bash
# Get help
java -jar jenkins-cli.jar -s http://localhost:8080 help

# List all jobs
java -jar jenkins-cli.jar -s http://localhost:8080 list-jobs

# Build a job
java -jar jenkins-cli.jar -s http://localhost:8080 build jobname

# Build with parameters
java -jar jenkins-cli.jar -s http://localhost:8080 build jobname \
  -p PARAM1=value1 -p PARAM2=value2

# Get job info
java -jar jenkins-cli.jar -s http://localhost:8080 get-job jobname

# Delete job
java -jar jenkins-cli.jar -s http://localhost:8080 delete-job jobname

# Get console output
java -jar jenkins-cli.jar -s http://localhost:8080 console jobname buildnumber
```

### Authentication

```bash
# Using username and API token
java -jar jenkins-cli.jar -s http://localhost:8080 \
  -auth username:apitoken \
  list-jobs

# Using SSH key
java -jar jenkins-cli.jar -s http://localhost:8080 \
  -i ~/.ssh/id_rsa \
  list-jobs
```

## Jenkinsfile Syntax

### Declarative Pipeline Structure

```groovy
pipeline {
    agent any

    options {
        // Build options
        timeout(time: 1, unit: 'HOURS')
        timestamps()
        buildDiscarder(logRotator(numToKeepStr: '30'))
    }

    environment {
        // Global environment variables
        BUILD_ID = "${BUILD_NUMBER}"
        BUILD_USER = "${BUILD_USER}"
    }

    parameters {
        // Build parameters
        string(name: 'ENV', defaultValue: 'dev', description: 'Environment')
        choice(name: 'REGION', choices: ['us-east-1', 'us-west-2'], description: 'AWS Region')
        booleanParam(name: 'DEPLOY', defaultValue: false, description: 'Deploy?')
    }

    triggers {
        // Build triggers
        githubPush()
        pollSCM('*/5 * * * *')
        cron('@daily')
    }

    stages {
        stage('Stage Name') {
            agent {
                // Agent specification
                label 'linux'
            }

            when {
                // Conditional stage execution
                branch 'main'
            }

            environment {
                // Stage-specific environment
                APP_ENV = 'production'
            }

            steps {
                // Stage tasks
                sh 'echo "Running stage"'
            }
        }
    }

    post {
        // Post-build actions
        always {
            cleanWs()
        }

        success {
            echo 'Build successful'
        }

        failure {
            echo 'Build failed'
        }
    }
}
```

### All Directives

| Directive | Purpose | Example |
|-----------|---------|---------|
| `agent` | Where to run | `agent any`, `agent { label 'linux' }` |
| `environment` | Environment variables | `JAVA_HOME='/usr/lib/jvm/java'` |
| `options` | Build options | `timeout(time: 1, unit: 'HOURS')` |
| `parameters` | Build parameters | `string(name: 'ENV', defaultValue: 'dev')` |
| `triggers` | Build triggers | `githubPush()`, `pollSCM('* * * * *')` |
| `stages` | Build stages | Contains multiple stage blocks |
| `stage` | Single stage | Contains steps and post |
| `steps` | Stage actions | Shell commands, scripts |
| `post` | Post-build actions | `always`, `success`, `failure` |
| `when` | Conditional execution | `branch 'main'`, `expression { ... }` |

## Common Pipeline Snippets

### Checkout from Git

```groovy
// Basic checkout
checkout scm

// Checkout specific branch
checkout([
    $class: 'GitSCM',
    branches: [[name: 'main']],
    userRemoteConfigs: [[url: 'https://github.com/org/repo.git']]
])

// Checkout with credentials
checkout([
    $class: 'GitSCM',
    branches: [[name: 'main']],
    userRemoteConfigs: [[
        url: 'git@github.com:org/repo.git',
        credentialsId: 'github-ssh'
    ]]
])
```

### Build with Maven

```groovy
// Standard Maven build
sh 'mvn clean package'

// With custom settings
sh 'mvn -s settings.xml clean package'

// Skip tests
sh 'mvn clean package -DskipTests'

// Specific goals
sh 'mvn clean compile test package'
```

### Build with Gradle

```groovy
// Standard Gradle build
sh './gradlew clean build'

// With custom properties
sh './gradlew clean build -Penv=production'

// Skip tests
sh './gradlew build -x test'
```

### Build with Node.js

```groovy
// Install and build
sh 'npm install'
sh 'npm run build'
sh 'npm test'

// Using specific Node version
withEnv(['NODE_PATH=/opt/nodejs/bin']) {
    sh 'npm install && npm build'
}
```

### Docker Build and Push

```groovy
// Build Docker image
sh 'docker build -t myapp:${BUILD_NUMBER} .'

// Push to registry
sh '''
    docker login -u $DOCKER_USER -p $DOCKER_PASS myregistry.azurecr.io
    docker tag myapp:${BUILD_NUMBER} myregistry.azurecr.io/myapp:${BUILD_NUMBER}
    docker push myregistry.azurecr.io/myapp:${BUILD_NUMBER}
'''

// Using credentials
withCredentials([
    usernamePassword(credentialsId: 'docker-hub', usernameVariable: 'USER', passwordVariable: 'PASS')
]) {
    sh '''
        docker login -u $USER -p $PASS
        docker build -t myapp:${BUILD_NUMBER} .
        docker push myapp:${BUILD_NUMBER}
    '''
}
```

### Deploy to Kubernetes

```groovy
// Deploy using kubectl
sh '''
    kubectl apply -f deployment.yaml
    kubectl set image deployment/myapp \
        myapp=myregistry.azurecr.io/myapp:${BUILD_NUMBER}
'''

// Deploy using Helm
sh '''
    helm upgrade --install myapp ./chart \
        --set image.tag=${BUILD_NUMBER}
'''
```

### Parallel Execution

```groovy
parallel(
    'Unit Tests': {
        sh 'npm test'
    },
    'Integration Tests': {
        sh 'npm run test:integration'
    },
    'Lint': {
        sh 'npm run lint'
    }
)
```

### Error Handling

```groovy
try {
    sh 'failing-command'
} catch (Exception e) {
    echo "Command failed: ${e.message}"
    currentBuild.result = 'FAILURE'
}

// Or using script step
script {
    def result = sh(script: 'some-command', returnStatus: true)
    if (result != 0) {
        error('Command failed')
    }
}
```

### Artifact Archiving

```groovy
// Archive build artifacts
archiveArtifacts artifacts: 'build/libs/*.jar', allowEmptyArchive: true

// Archive multiple patterns
archiveArtifacts artifacts: '''
    build/libs/**/*.jar,
    build/reports/**/*,
    dist/**/*
''', allowEmptyArchive: true

// Publish to repository
sh '''
    curl -v -u user:pass \
        -F file=@build/app.jar \
        https://repository.example.com/artifacts/
'''
```

### Test Result Publishing

```groovy
// Publish JUnit test results
junit 'target/surefire-reports/**/*.xml'

// Publish code coverage
publishHTML([
    allowMissing: false,
    alwaysLinkToLastBuild: false,
    keepAll: false,
    reportDir: 'coverage',
    reportFiles: 'index.html',
    reportName: 'Code Coverage'
])
```

### Notifications

```groovy
// Email notification
emailext(
    subject: 'Build ${BUILD_NUMBER} ${BUILD_STATUS}',
    body: '''
        Build: ${BUILD_NUMBER}
        Status: ${BUILD_STATUS}
        Log: ${BUILD_LOG}
    ''',
    to: '${DEFAULT_RECIPIENTS}',
    attachmentsPattern: 'build/reports/*'
)

// Slack notification
slackSend(
    color: currentBuild.result == 'SUCCESS' ? 'good' : 'danger',
    message: "Build ${BUILD_NUMBER}: ${currentBuild.result}\n${BUILD_URL}"
)
```

## Groovy Snippets

### Variables and Strings

```groovy
// Define variables
def name = "Jenkins"
def version = 2.300

// String interpolation
echo "Running ${name} version ${version}"

// Multi-line strings
def script = '''
    #!/bin/bash
    echo "Hello World"
    exit 0
'''
```

### Conditionals

```groovy
// If statement
if (env.BRANCH_NAME == 'main') {
    echo "Running on main branch"
} else {
    echo "Running on feature branch"
}

// Switch statement
switch(env.BUILD_NUMBER) {
    case '1':
        echo "First build"
        break
    default:
        echo "Regular build"
}

// Ternary operator
def environment = env.BRANCH_NAME == 'main' ? 'prod' : 'dev'
```

### Loops

```groovy
// For loop
for (int i = 0; i < 3; i++) {
    echo "Iteration ${i}"
}

// For-each loop
def items = ['a', 'b', 'c']
for (item in items) {
    echo "Item: ${item}"
}

// While loop
def count = 0
while (count < 3) {
    echo "Count: ${count}"
    count++
}
```

### Functions

```groovy
// Define function
def greet(name) {
    return "Hello, ${name}!"
}

// Call function
echo greet("Jenkins")

// Function with default parameters
def deploy(env = 'dev') {
    echo "Deploying to ${env}"
}
```

### Exception Handling

```groovy
try {
    sh 'failing-command'
} catch (Exception e) {
    echo "Error: ${e.message}"
    currentBuild.result = 'FAILURE'
    error('Build failed')
} finally {
    echo "Cleanup"
}
```

## Environment Variables

### Predefined Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `BUILD_NUMBER` | Build number | 123 |
| `BUILD_ID` | Build ID | 2021-03-15_10-30-45 |
| `BUILD_URL` | Build URL | http://jenkins/job/myjob/123/ |
| `BUILD_LOG` | Build log | (see below) |
| `WORKSPACE` | Job workspace | /var/jenkins_home/jobs/myjob/workspace |
| `JOB_NAME` | Job name | myjob |
| `GIT_BRANCH` | Git branch | main |
| `GIT_COMMIT` | Git commit SHA | abc123def456 |
| `BUILD_TIMESTAMP` | Build timestamp | 2021-03-15T10:30:45Z |
| `BRANCH_NAME` | Pipeline branch | main |

### Setting Variables

```groovy
// In environment block
environment {
    BUILD_ENV = 'production'
    APP_VERSION = "${BUILD_NUMBER}.0"
}

// In script
script {
    env.CUSTOM_VAR = "value"
}

// From command output
script {
    env.GIT_COMMIT = sh(script: 'git rev-parse HEAD', returnStdout: true).trim()
}
```

## Common Plugins Reference

### Source Control

| Plugin | Purpose |
|--------|---------|
| Git | Git repository integration |
| GitHub | GitHub-specific features |
| GitLab | GitLab integration |
| Subversion | SVN support |

### Build Tools

| Plugin | Purpose |
|--------|---------|
| Maven | Maven build support |
| Gradle | Gradle build support |
| Node.js | Node.js plugin |
| Docker | Docker build/push |
| Kubernetes | Kubernetes integration |

### Reporting

| Plugin | Purpose |
|--------|---------|
| JUnit | Test result publishing |
| Cobertura | Code coverage reports |
| SonarQube | Code quality analysis |
| HTMLPublisher | HTML report publishing |

### Notifications

| Plugin | Purpose |
|--------|---------|
| Email Extension | Advanced email notifications |
| Slack | Slack integration |
| GitHub | GitHub status updates |
| Teams | Microsoft Teams notifications |

### Pipeline

| Plugin | Purpose |
|--------|---------|
| Pipeline | Pipeline support |
| Blue Ocean | Modern pipeline UI |
| Shared Library | Reusable pipeline code |

## Global Configuration

### Jenkins Configuration File

Located at `/var/jenkins_home/config.xml` (Docker) or `/var/lib/jenkins/config.xml` (Linux)

### Backup and Restore

```bash
# Backup Jenkins home
tar -czf jenkins-backup.tar.gz /var/jenkins_home/

# Restore
tar -xzf jenkins-backup.tar.gz -C /

# Or Docker
docker exec jenkins tar -czf /var/jenkins_home/backup.tar.gz /var/jenkins_home
docker cp jenkins:/var/jenkins_home/backup.tar.gz .
```

### Jenkins File Locations

| Item | Location |
|------|----------|
| Home | `/var/jenkins_home` or `/var/lib/jenkins` |
| Config | `$JENKINS_HOME/config.xml` |
| Jobs | `$JENKINS_HOME/jobs/` |
| Workspace | `$JENKINS_HOME/workspace/` |
| Plugins | `$JENKINS_HOME/plugins/` |
| Logs | `$JENKINS_HOME/logs/` |

## Pipeline Best Practices

```groovy
// Use descriptive stage names
stage('Build and Package Application') {
    steps {
        sh './build.sh'
    }
}

// Fail fast on errors
steps {
    sh 'set -e'  // Exit on first error
    sh './test.sh'
}

// Use timeouts
options {
    timeout(time: 1, unit: 'HOURS')
}

// Clean workspace
post {
    always {
        cleanWs()
    }
}

// Use credentials safely
withCredentials([string(credentialsId: 'secret-id', variable: 'SECRET')]) {
    sh 'echo $SECRET > /tmp/secret'  // Masked in logs
}

// Parallel for performance
parallel(
    'Test': { sh 'npm test' },
    'Build': { sh 'npm run build' }
)
```

## Troubleshooting

```bash
# View Jenkins logs
tail -f /var/log/jenkins/jenkins.log

# Check Jenkins status
systemctl status jenkins

# Restart Jenkins
systemctl restart jenkins

# Access Jenkins Script Console (for debugging)
# http://localhost:8080/script

# Monitor running builds
curl http://localhost:8080/api/json

# Get job configuration
curl http://localhost:8080/job/jobname/config.xml
```

---

## Quick Reference

**Pipeline Syntax**: `pipeline { agent any; stages { stage('Name') { steps { sh 'command' } } } }`

**Common Commands**:
- Build: `sh 'command'`
- Docker: `docker build -t app:latest .`
- Maven: `mvn clean package`
- Test: `junit 'test-results/**/*.xml'`
- Deploy: `kubectl apply -f deployment.yaml`
- Notify: `slackSend(message: 'Build complete')`

**Common Patterns**:
- Checkout: `checkout scm`
- Parallel: `parallel(...)`
- Post: `post { always { ... } success { ... } failure { ... } }`
- Credentials: `withCredentials([...])`
- Artifacts: `archiveArtifacts artifacts: '...'`
