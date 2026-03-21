---
title: "CI/CD Cheatsheet"
description: "Quick reference for CI/CD pipeline syntax patterns"
sidebar_label: "Cheatsheet"
sidebar_position: 3
---

# CI/CD Cheatsheet

Quick reference guide for pipeline syntax across popular CI/CD platforms.

## GitHub Actions

YAML workflow files in `.github/workflows/`

### Basic Workflow Structure
```yaml
name: CI Pipeline
on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Setup Node
        uses: actions/setup-node@v3
        with:
          node-version: '18'
      - name: Install dependencies
        run: npm install
      - name: Run tests
        run: npm test
      - name: Build
        run: npm run build
```

### Matrix Strategy (Run across multiple versions)
```yaml
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        node-version: [16, 18, 20]
        os: [ubuntu-latest, windows-latest]
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-node@v3
        with:
          node-version: ${{ matrix.node-version }}
      - run: npm test
```

### Conditional Steps
```yaml
steps:
  - name: Deploy to production
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    run: npm run deploy
```

### Docker in GitHub Actions
```yaml
steps:
  - uses: actions/checkout@v3
  - name: Build Docker image
    run: docker build -t myapp:${{ github.sha }} .
  - name: Push to registry
    run: |
      echo ${{ secrets.DOCKER_PASSWORD }} | docker login -u ${{ secrets.DOCKER_USER }} --password-stdin
      docker push myapp:${{ github.sha }}
```

### Artifacts & Caching
```yaml
steps:
  - uses: actions/cache@v3
    with:
      path: ~/.npm
      key: ${{ runner.os }}-node-${{ hashFiles('**/package-lock.json') }}
  - name: Upload test results
    uses: actions/upload-artifact@v3
    with:
      name: test-results
      path: ./coverage/
```

## Jenkins

Declarative Pipeline syntax (Jenkinsfile)

### Basic Pipeline Structure
```groovy
pipeline {
    agent any

    stages {
        stage('Checkout') {
            steps {
                git 'https://github.com/user/repo.git'
            }
        }
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
            steps {
                sh 'npm run deploy'
            }
        }
    }
}
```

### Parallel Stages
```groovy
stage('Test') {
    parallel {
        stage('Unit Tests') {
            steps {
                sh 'npm run test:unit'
            }
        }
        stage('Integration Tests') {
            steps {
                sh 'npm run test:integration'
            }
        }
        stage('E2E Tests') {
            steps {
                sh 'npm run test:e2e'
            }
        }
    }
}
```

### Docker Agent
```groovy
pipeline {
    agent {
        docker {
            image 'node:18'
            args '-v /var/run/docker.sock:/var/run/docker.sock'
        }
    }
    stages {
        stage('Build') {
            steps {
                sh 'npm install && npm build'
            }
        }
    }
}
```

### Post Actions
```groovy
pipeline {
    agent any
    stages {
        stage('Test') {
            steps {
                sh 'npm test'
            }
        }
    }
    post {
        always {
            junit 'test-results.xml'
        }
        success {
            echo 'Build succeeded!'
        }
        failure {
            echo 'Build failed!'
        }
        cleanup {
            deleteDir()
        }
    }
}
```

### Credentials & Environment Variables
```groovy
pipeline {
    agent any
    environment {
        APP_ENV = 'production'
        DOCKER_REGISTRY = 'registry.example.com'
    }
    stages {
        stage('Deploy') {
            steps {
                withCredentials([usernamePassword(
                    credentialsId: 'docker-credentials',
                    usernameVariable: 'DOCKER_USER',
                    passwordVariable: 'DOCKER_PASS'
                )]) {
                    sh '''
                        echo $DOCKER_PASS | docker login -u $DOCKER_USER --password-stdin $DOCKER_REGISTRY
                    '''
                }
            }
        }
    }
}
```

## GitLab CI

YAML file: `.gitlab-ci.yml`

### Basic Pipeline
```yaml
stages:
  - build
  - test
  - deploy

build:
  stage: build
  script:
    - npm install
    - npm run build
  artifacts:
    paths:
      - dist/

test:
  stage: test
  script:
    - npm test
    - npm run lint
  coverage: '/Coverage: \d+\.\d+%/'

deploy:
  stage: deploy
  script:
    - npm run deploy
  only:
    - main
```

### Matrix Builds
```yaml
test:
  stage: test
  script:
    - npm test
  parallel:
    matrix:
      - NODE_VERSION: [16, 18, 20]
        OS: [ubuntu, debian]
```

### Docker Image
```yaml
stages:
  - build
  - push

build_image:
  stage: build
  image: docker:latest
  services:
    - docker:dind
  script:
    - docker build -t myapp:$CI_COMMIT_SHA .
    - docker tag myapp:$CI_COMMIT_SHA myapp:latest

push_image:
  stage: push
  image: docker:latest
  services:
    - docker:dind
  before_script:
    - echo $CI_REGISTRY_PASSWORD | docker login -u $CI_REGISTRY_USER $CI_REGISTRY
  script:
    - docker push myapp:$CI_COMMIT_SHA
  only:
    - main
```

### Rules & Conditions
```yaml
deploy:
  stage: deploy
  script:
    - echo "Deploying..."
  rules:
    - if: '$CI_COMMIT_BRANCH == "main"'
      when: always
    - if: '$CI_COMMIT_BRANCH == "develop"'
      when: manual
    - when: never
```

### Artifacts & Caching
```yaml
test:
  stage: test
  cache:
    paths:
      - node_modules/
    key: ${CI_COMMIT_REF_SLUG}
  script:
    - npm install
    - npm test
  artifacts:
    reports:
      junit: test-results.xml
    paths:
      - coverage/
    expire_in: 1 week
```

## CircleCI

YAML file: `.circleci/config.yml`

### Basic Configuration
```yaml
version: 2.1

jobs:
  build:
    docker:
      - image: cimg/node:18.0
    steps:
      - checkout
      - run: npm install
      - run: npm test
      - run: npm run build

workflows:
  build_and_test:
    jobs:
      - build
```

### Workflow with Multiple Jobs
```yaml
version: 2.1

jobs:
  checkout_code:
    docker:
      - image: cimg/base:2023.01
    steps:
      - checkout
      - save_cache:
          key: source-{{ .Environment.CIRCLE_SHA1 }}
          paths:
            - .

  build:
    docker:
      - image: cimg/node:18.0
    steps:
      - restore_cache:
          key: source-{{ .Environment.CIRCLE_SHA1 }}
      - run: npm install
      - run: npm run build

  test:
    docker:
      - image: cimg/node:18.0
    steps:
      - restore_cache:
          key: source-{{ .Environment.CIRCLE_SHA1 }}
      - run: npm test

  deploy:
    docker:
      - image: cimg/base:2023.01
    steps:
      - run: echo "Deploying to production"

workflows:
  build_test_deploy:
    jobs:
      - checkout_code
      - build:
          requires:
            - checkout_code
      - test:
          requires:
            - checkout_code
      - deploy:
          requires:
            - build
            - test
          filters:
            branches:
              only: main
```

### Docker Image Building
```yaml
jobs:
  build_and_push:
    docker:
      - image: cimg/base:2023.01
    steps:
      - setup_remote_docker
      - checkout
      - run: |
          docker build -t myapp:$CIRCLE_SHA1 .
          echo $DOCKER_PASS | docker login -u $DOCKER_USER --password-stdin
          docker push myapp:$CIRCLE_SHA1
```

## Common Patterns

### Build & Push Docker Image
```yaml
# GitHub Actions
- name: Build and push Docker image
  uses: docker/build-push-action@v4
  with:
    context: .
    push: true
    tags: |
      registry.example.com/myapp:${{ github.sha }}
      registry.example.com/myapp:latest
```

### Run Tests with Coverage
```yaml
# GitLab CI
test:
  script:
    - npm install
    - npm run test -- --coverage
    - npm run test:e2e
  coverage: '/Lines\s*:\s*(\d+\.\d+)%/'
  artifacts:
    reports:
      coverage_report:
        coverage_format: cobertura
        path: coverage/cobertura-coverage.xml
```

### Deploy to Kubernetes
```yaml
# GitHub Actions
- name: Deploy to Kubernetes
  run: |
    kubectl set image deployment/myapp \
      myapp=myregistry/myapp:${{ github.sha }} \
      -n production
    kubectl rollout status deployment/myapp -n production
```

### Slack Notifications
```yaml
# GitHub Actions
- name: Notify Slack on failure
  if: failure()
  uses: slackapi/slack-github-action@v1
  with:
    webhook-url: ${{ secrets.SLACK_WEBHOOK }}
    payload: |
      {
        "text": "Build failed for ${{ github.repository }}"
      }
```

### Security Scanning
```yaml
# GitHub Actions
- name: Run Trivy vulnerability scanner
  uses: aquasecurity/trivy-action@master
  with:
    image-ref: myapp:${{ github.sha }}
    format: 'sarif'
    output: 'trivy-results.sarif'

- name: Upload Trivy results
  uses: github/codeql-action/upload-sarif@v2
  with:
    sarif_file: 'trivy-results.sarif'
```

## Environment Variables & Secrets

| Platform | Syntax | Example |
|----------|--------|---------|
| GitHub Actions | `${{ secrets.MY_SECRET }}` | `${{ secrets.DOCKER_TOKEN }}` |
| Jenkins | `${VARIABLE_NAME}` | `${DOCKER_CREDENTIALS}` |
| GitLab CI | `$VARIABLE_NAME` | `$DOCKER_TOKEN` |
| CircleCI | `$VARIABLE_NAME` | `$DOCKER_TOKEN` |

## Quick Tips

- **Parallelize jobs** — Run independent tasks simultaneously
- **Cache dependencies** — Cache node_modules, Maven cache, etc.
- **Use base images** — Start with relevant Docker images to avoid setup
- **Fail fast** — Run quick tests first, expensive tests later
- **Clear logs** — Remove sensitive data before storing logs
- **Artifact retention** — Set expiration dates to manage storage
- **Conditional execution** — Only deploy on specific branches
- **Matrix testing** — Test across multiple versions/platforms
