---
title: "GitHub Actions Cheatsheet"
sidebar_label: "Cheatsheet"
description: "Quick reference for workflow YAML, common actions, expressions, and context variables"
sidebar_position: 2
---

# GitHub Actions Cheatsheet

## Basic Workflow Structure

```yaml
name: Workflow Name

on:
  push:
  pull_request:

jobs:
  job-name:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - run: echo "Hello World"
```

## Events (on:)

```yaml
on: push                             # Single event
on: [push, pull_request]             # Multiple events

on:
  push:
    branches: [main, develop]        # Specific branches
    paths: [src/**]                  # Only certain paths
  pull_request:
    branches: [main]

on:
  schedule:
    - cron: '0 9 * * 1'              # 9 AM Monday

on:
  workflow_dispatch:                 # Manual trigger
    inputs:
      environment:
        description: 'Environment'
        required: true
```

## Jobs

```yaml
jobs:
  job1:
    runs-on: ubuntu-latest
    if: github.event_name == 'push'  # Conditional
    steps:
      - run: echo "Job 1"

  job2:
    runs-on: ubuntu-latest
    needs: job1                       # Depends on job1
    steps:
      - run: echo "Job 2"
```

## Runners

```yaml
ubuntu:   runs-on: ubuntu-latest      # or ubuntu-20.04, ubuntu-22.04
windows:  runs-on: windows-latest     # or windows-2019, windows-2022
macos:    runs-on: macos-latest       # or macos-11, macos-12
```

## Steps

```yaml
steps:
  # Use action
  - uses: actions/checkout@v3
    with:
      ref: develop

  # Run command
  - run: npm install

  # Named step
  - name: Build
    run: npm run build

  # Multiple lines
  - run: |
      echo "Line 1"
      echo "Line 2"

  # Conditional
  - if: github.ref == 'refs/heads/main'
    run: npm run deploy

  # Always run
  - if: always()
    run: echo "Done"
```

## Setup Actions

```yaml
- uses: actions/setup-node@v3
  with:
    node-version: 16
    cache: npm

- uses: actions/setup-python@v4
  with:
    python-version: 3.9

- uses: actions/setup-go@v4
  with:
    go-version: 1.19

- uses: actions/setup-java@v3
  with:
    java-version: 11
```

## Cache

```yaml
- uses: actions/cache@v3
  with:
    path: ~/.npm
    key: ${{ runner.os }}-npm-${{ hashFiles('**/package-lock.json') }}
    restore-keys: |
      ${{ runner.os }}-npm-

# Auto cache with setup
- uses: actions/setup-node@v3
  with:
    cache: npm
```

## Artifacts

```yaml
# Upload
- uses: actions/upload-artifact@v3
  with:
    name: build-output
    path: dist/

# Download
- uses: actions/download-artifact@v3
  with:
    name: build-output
```

## Environment Variables

```yaml
env:
  global_var: value

jobs:
  job:
    env:
      job_var: value
    steps:
      - name: Step
        env:
          step_var: value
        run: echo $step_var

      # Use variables
      - run: echo ${{ env.job_var }}

      # Use secrets
      - run: echo ${{ secrets.API_KEY }}
```

## Expressions

```yaml
${{ expression }}

# Operators
${{ 1 + 2 }}                        # 3
${{ 'hello' }}                      # 'hello'
${{ true && false }}                # false
${{ github.actor }}                 # Username

# Functions
${{ contains(string, substring) }}  # true/false
${{ startsWith(string, prefix) }}   # true/false
${{ endsWith(string, suffix) }}     # true/false
${{ format('{0}-{1}', str1, str2) }} # Formatted string

# Conditions
if: github.event_name == 'push'
if: github.ref == 'refs/heads/main'
if: contains(github.actor, 'bot')
if: success()
if: failure()
if: always()
if: cancelled()
```

## Context Variables

```yaml
# GitHub context
${{ github.event_name }}             # push, pull_request
${{ github.event.action }}           # opened, synchronize
${{ github.repository }}             # owner/repo
${{ github.repository_owner }}       # owner
${{ github.ref }}                    # refs/heads/main
${{ github.ref_name }}               # main
${{ github.sha }}                    # commit hash
${{ github.actor }}                  # username
${{ github.token }}                  # GitHub token

# Runner context
${{ runner.os }}                     # Linux, Windows, macOS
${{ runner.arch }}                   # x64, ARM64
${{ runner.temp }}                   # Temp directory path
${{ runner.workspace }}              # Workspace path

# Job context
${{ job.status }}                    # success, failure
```

## Matrix

```yaml
strategy:
  matrix:
    node-version: [14, 16, 18]
    os: [ubuntu-latest, windows-latest]

steps:
  - uses: actions/setup-node@v3
    with:
      node-version: ${{ matrix.node-version }}

# Exclude combination
strategy:
  matrix:
    os: [ubuntu-latest, windows-latest]
    node: [14, 16, 18]
  exclude:
    - os: windows-latest
      node: 14
```

## Secrets

```yaml
# Store in Settings > Secrets

steps:
  - env:
      API_KEY: ${{ secrets.API_KEY }}
    run: curl -H "Authorization: Bearer $API_KEY" ...

  # With action
  - uses: docker/login-action@v2
    with:
      username: ${{ secrets.DOCKER_USERNAME }}
      password: ${{ secrets.DOCKER_PASSWORD }}
```

## Common Actions

```yaml
# Checkout
- uses: actions/checkout@v3

# Setup Node
- uses: actions/setup-node@v3
  with:
    node-version: 16

# Upload artifact
- uses: actions/upload-artifact@v3
  with:
    name: artifacts
    path: dist/

# Download artifact
- uses: actions/download-artifact@v3
  with:
    name: artifacts

# Create release
- uses: actions/create-release@v1
  with:
    tag_name: v1.0.0
    release_name: Release 1.0.0

# Deploy to GitHub Pages
- uses: actions/deploy-pages@v2
```

## Community Actions

```yaml
# Docker build and push
- uses: docker/build-push-action@v4
  with:
    context: .
    push: true
    tags: registry/image:latest

# Create PR
- uses: peter-evans/create-pull-request@v4
  with:
    title: 'Automated PR'

# AWS
- uses: aws-actions/configure-aws-credentials@v2
  with:
    role-to-assume: arn:aws:iam::...
    aws-region: us-east-1

# Slack notification
- uses: 8398a7/action-slack@v3
  with:
    status: ${{ job.status }}
    webhook_url: ${{ secrets.SLACK_WEBHOOK }}
```

## Useful Patterns

### Conditional Deployment

```yaml
- name: Deploy
  if: github.ref == 'refs/heads/main' && github.event_name == 'push'
  run: npm run deploy
```

### Matrix Build

```yaml
strategy:
  matrix:
    python-version: [3.8, 3.9, '3.10']
steps:
  - uses: actions/setup-python@v4
    with:
      python-version: ${{ matrix.python-version }}
```

### Upload Multiple Artifacts

```yaml
- name: Upload artifacts
  uses: actions/upload-artifact@v3
  with:
    name: all-artifacts
    path: |
      build/
      dist/
      coverage/
```

### Dynamic Job Names

```yaml
jobs:
  test:
    runs-on: ${{ matrix.os }}
    name: Test on ${{ matrix.os }} with Node ${{ matrix.node }}
    strategy:
      matrix:
        os: [ubuntu-latest, windows-latest]
        node: [14, 16]
```

## Debugging

```yaml
# Print debug info
- run: |
    echo "Event: ${{ github.event_name }}"
    echo "Ref: ${{ github.ref }}"
    echo "Actor: ${{ github.actor }}"

# Enable debug logging
env:
  ACTIONS_STEP_DEBUG: true
```

## Fail Fast

```yaml
strategy:
  fail-fast: false  # Don't cancel other jobs if one fails
  matrix:
    os: [ubuntu-latest, windows-latest]
```

## Timeout

```yaml
jobs:
  job:
    timeout-minutes: 30  # Job timeout
    runs-on: ubuntu-latest
    steps:
      - run: npm test
        timeout-minutes: 10  # Step timeout
```

## Quick Commands

```bash
# Check workflow syntax
# Use GitHub's built-in validation or:
# Push to repo and check Actions tab

# View workflow runs
# Settings > Actions > All workflows

# Re-run failed jobs
# Actions > Workflow > Re-run failed jobs
```
