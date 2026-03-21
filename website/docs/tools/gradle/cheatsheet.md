---
title: "Gradle Cheat Sheet"
sidebar_label: "Quick Reference"
description: "Gradle CLI commands, build.gradle patterns, plugins, and common tasks"
sidebar_position: 2
---

# Gradle Cheat Sheet

Quick reference for Gradle commands, build scripts, and common patterns.

## Installation

```bash
# Download and install Gradle
# https://gradle.org/install/

# macOS via Homebrew
brew install gradle

# Ubuntu/Debian via sdkman
sdk install gradle

# Verify installation
gradle --version
```

## Gradle Wrapper

```bash
# Create wrapper (generates gradlew script)
gradle wrapper

# Create with specific version
gradle wrapper --gradle-version 7.0

# Use wrapper (Unix/macOS)
./gradlew build

# Use wrapper (Windows)
gradlew.bat build

# Update wrapper
./gradlew wrapper --gradle-version 7.6
```

## Basic Commands

```bash
# Initialize new project
gradle init

# List all tasks
gradle tasks
gradle tasks --all

# Show task details
gradle tasks --group build
gradle help --task build

# Build project
gradle build

# Run without building
gradle run

# Clean build artifacts
gradle clean

# Force full rebuild
gradle clean build

# Check syntax without executing
gradle --dry-run build
```

## Task Execution

```bash
# Run single task
gradle test
gradle jar
gradle publish

# Run multiple tasks
gradle clean build test

# Run task with dependencies
gradle build  # Runs all dependencies

# Exclude tasks
gradle build -x test      # Skip tests
gradle build -x integrationTest

# Force task execution
gradle build --rerun-tasks

# Continue on failure
gradle build --continue

# Parallel execution
gradle build --parallel --parallel-threads 4
```

## Build Options

```bash
# Quiet mode (minimal output)
gradle build -q

# Verbose output
gradle build -v
gradle build --info
gradle build --debug

# Profile build
gradle build --profile

# Show task statistics
gradle build --scan

# Use specific gradle.properties
gradle build --gradle-user-home /custom/path
```

## Build Script - Basic Structure

```groovy
// build.gradle (Groovy DSL)

// 1. Plugins
plugins {
    id 'java'
    id 'application'
}

// 2. Project metadata
group = 'com.example'
version = '1.0.0'
description = 'My application'

// 3. Java configuration
java {
    sourceCompatibility = JavaVersion.VERSION_11
    targetCompatibility = JavaVersion.VERSION_11
}

// 4. Repositories
repositories {
    mavenCentral()
    google()
}

// 5. Dependencies
dependencies {
    implementation 'com.google.guava:guava:31.0-jre'
    testImplementation 'junit:junit:4.13.2'
}

// 6. Plugin configuration
application {
    mainClass = 'com.example.Main'
}

// 7. Custom tasks
task hello {
    doLast {
        println 'Hello!'
    }
}
```

## Kotlin DSL - Basic Structure

```kotlin
// build.gradle.kts (Kotlin DSL)

plugins {
    java
    application
}

group = "com.example"
version = "1.0.0"
description = "My application"

java {
    sourceCompatibility = JavaVersion.VERSION_11
    targetCompatibility = JavaVersion.VERSION_11
}

repositories {
    mavenCentral()
    google()
}

dependencies {
    implementation("com.google.guava:guava:31.0-jre")
    testImplementation("junit:junit:4.13.2")
}

application {
    mainClass.set("com.example.Main")
}

tasks.register("hello") {
    doLast {
        println("Hello!")
    }
}
```

## Plugins

### Common Plugins

```groovy
// Core plugins
plugins {
    id 'java'              // Java compilation and testing
    id 'java-library'      // Java library (exports API)
    id 'application'       // Executable JAR
    id 'maven-publish'     // Maven publishing
    id 'signing'           // Digital signing
}

// Framework plugins
plugins {
    id 'org.springframework.boot' version '2.6.0'
    id 'org.springframework.cloud' version '2021.0.0'
    id 'io.quarkus' version '2.5.0'
}

// Code quality plugins
plugins {
    id 'jacoco'           // Code coverage
    id 'checkstyle'       // Style checking
    id 'pmd'              // Problem detector
    id 'com.github.spotbugs' version '4.7.2'  // Bug detection
}
```

### Apply External Plugins

```groovy
plugins {
    id 'com.example.plugin' version '1.0'
}

// Or use legacy syntax
apply plugin: 'com.example.plugin'
apply plugin: 'java'
```

## Dependencies

### Repository Configuration

```groovy
repositories {
    mavenCentral()
    google()
    jcenter()

    maven {
        url = uri('https://repo.example.com/release')
        credentials {
            username = 'user'
            password = 'pass'
        }
    }

    ivy {
        url = uri('https://ivy.example.com')
    }

    // Private repository with authentication
    maven {
        url = uri('https://nexus.example.com/content/repositories/releases')
        authentication {
            basic(BasicAuthentication)
        }
    }
}
```

### Dependency Declarations

```groovy
dependencies {
    // Implementation (compile + runtime)
    implementation 'com.google.guava:guava:31.0-jre'
    implementation group: 'commons-io', name: 'commons-io', version: '2.11.0'

    // API (exposed to consumers)
    api 'org.apache.commons:commons-lang3:3.12.0'

    // Runtime only
    runtimeOnly 'mysql:mysql-connector-java:8.0.26'

    // Compile only
    compileOnly 'org.projectlombok:lombok:1.18.22'

    // Annotation processor
    annotationProcessor 'org.projectlombok:lombok:1.18.22'

    // Test implementation
    testImplementation 'junit:junit:4.13.2'
    testRuntimeOnly 'org.junit.vintage:junit-vintage-engine'

    // Test fixtures
    testFixturesImplementation 'org.mockito:mockito-core:4.2.0'

    // External file
    implementation files('libs/custom.jar')

    // Project dependency
    implementation project(':shared')
}
```

### Dependency Constraints

```groovy
dependencies {
    implementation 'org.apache.commons:commons-lang3'

    constraints {
        implementation 'org.apache.commons:commons-lang3:3.12.0'
    }
}

// Version strategies
dependencies {
    implementation 'org.slf4j:slf4j-api:1.7.+'    // 1.7.x
    implementation 'org.slf4j:slf4j-api:latest.release'
}

// Exclude transitive dependency
dependencies {
    implementation('org.springframework.boot:spring-boot-starter') {
        exclude module: 'spring-boot-starter-logging'
    }
}
```

## Build Configuration

### Java Configuration

```groovy
java {
    sourceCompatibility = JavaVersion.VERSION_11
    targetCompatibility = JavaVersion.VERSION_11

    // Module info
    modularity.inferModulePath = true
}

// Or set directly on tasks
tasks.compileJava {
    sourceCompatibility = '11'
    targetCompatibility = '11'
}

// Compiler options
compileJava {
    options.encoding = 'UTF-8'
    options.compilerArgs = ['-Xlint:deprecation']
    options.release = 11
}
```

### Testing Configuration

```groovy
test {
    useJUnit()                    // Use JUnit 4
    // OR
    useJUnitPlatform()           // Use JUnit 5

    // Logging
    testLogging {
        events "passed", "skipped", "failed"
        showStandardStreams = true
        exceptionFormat = 'full'
    }

    // System properties
    systemProperty 'java.io.tmpdir', System.getProperty('java.io.tmpdir')

    // Environment variables
    environment 'ENV_VAR', 'value'
}

// Run specific test
gradle test --tests *ServiceTest
gradle test --tests *ServiceTest.testMethod

// Integration tests
task integrationTest(type: Test) {
    useJUnitPlatform {
        includeTags 'integration'
    }
}
```

### JAR Configuration

```groovy
jar {
    archiveBaseName = 'my-app'
    archiveVersion = '1.0'

    manifest {
        attributes 'Main-Class': 'com.example.Main',
                    'Implementation-Title': project.name,
                    'Implementation-Version': version
    }
}

// Fat JAR
task fatJar(type: Jar) {
    archiveBaseName = "${project.name}-all"

    from sourceSets.main.output

    dependsOn configurations.runtimeClasspath
    from {
        configurations.runtimeClasspath.findAll { it.canBeConvertedToPath() }.collect { zipTree(it) }
    }
}
```

## Multi-Project Builds

### settings.gradle

```groovy
rootProject.name = 'my-app'

include 'api'
include 'web'
include 'cli'

// Nested projects
include 'services:auth'
include 'services:payment'

// Project name mapping
project(':api').projectDir = file('modules/api')
```

### Root build.gradle

```groovy
allprojects {
    group = 'com.example'
    version = '1.0.0'

    repositories {
        mavenCentral()
    }
}

subprojects {
    apply plugin: 'java'

    dependencies {
        testImplementation 'junit:junit:4.13.2'
    }
}

project(':api') {
    dependencies {
        implementation project(':shared')
    }
}

// Build dependencies
project(':web').dependsOn(':api')
```

## Custom Tasks

### Task Definition

```groovy
task hello {
    doLast {
        println 'Hello, World!'
    }
}

task goodbye {
    doFirst {
        println 'Goodbye'
    }
    doLast {
        println 'Farewell'
    }
}

// Task with inputs and outputs
task processData {
    inputs.file 'data/input.txt'
    outputs.file 'build/data/output.txt'

    doLast {
        // Process file
    }
}

// Task with description
task deploy {
    group = 'deployment'
    description = 'Deploy application to production'

    doLast {
        println 'Deploying...'
    }
}
```

### Task Dependencies

```groovy
task compile
task test {
    dependsOn compile          // Must run after compile
}
task build {
    dependsOn [compile, test]  // Multiple dependencies
}

// Ordering without hard dependency
task after {
    mustRunAfter before
}

// Finalization
task cleanup {
    finalizedBy 'test'         // test finalizes cleanup
}
```

### Register Task (Recommended)

```groovy
tasks.register('hello') {
    doLast {
        println 'Hello'
    }
}

tasks.register('greet', GreetTask) {
    greeting = 'Hi'
}
```

## Properties and Settings

### gradle.properties

```properties
# JVM settings
org.gradle.jvmargs=-Xmx2g

# Build settings
org.gradle.parallel=true
org.gradle.daemon=true
org.gradle.build.cache=true

# Logging
org.gradle.logging.level=lifecycle

# Custom properties
app.version=1.0.0
java.version=11
```

### system.properties (Per-project)

```properties
# Project-specific properties
systemProp.http.proxyHost=proxy.example.com
systemProp.http.proxyPort=8080
```

## Gradle Daemon

```bash
# Use daemon (faster builds)
gradle build           # Uses daemon by default

# Disable daemon
gradle build --no-daemon

# Stop daemon
gradle --stop

# Status
gradle --status
```

## Build Cache

```bash
# Enable build cache
gradle build --build-cache

# Or in gradle.properties
org.gradle.build.cache=true

# Cleanup cache
gradle cleanBuildCache
```

## Common Patterns

### Spring Boot Application

```groovy
plugins {
    id 'java'
    id 'org.springframework.boot' version '2.6.0'
}

bootRun {
    args = ['--server.port=8080']
}

springBoot {
    mainClass = 'com.example.Application'
}
```

### Publishing to Maven Central

```groovy
plugins {
    id 'maven-publish'
    id 'signing'
}

publishing {
    publications {
        mavenJava(MavenPublication) {
            from components.java

            pom {
                name = 'My Project'
                description = 'Description'
                url = 'https://github.com/example/project'
            }
        }
    }
}
```

### Code Coverage (JaCoCo)

```groovy
plugins {
    id 'jacoco'
}

jacoco {
    toolVersion = "0.8.7"
}

jacocoTestReport {
    reports {
        xml.enabled = true
        html.enabled = true
    }
}
```

## Troubleshooting

```bash
# Verbose output
gradle build --info
gradle build --debug

# Show project structure
gradle projects

# Dependency tree
gradle dependencies
gradle dependencies --configuration implementation

# Task graph
gradle build --dry-run

# Show which tasks run
gradle build -i
```

## Tips and Best Practices

1. **Use wrapper** — Ensures consistent Gradle version
2. **Enable daemon** — Faster builds by reusing JVM
3. **Use incremental builds** — Only rebuild changed code
4. **Separate concerns** — Split large build scripts
5. **Document tasks** — Add descriptions and groups
6. **Version dependencies** — Use BOMs for consistency
7. **Cache aggressively** — Enable build cache
8. **Parallel builds** — Use multi-core systems efficiently

## Quick Reference

| Task | Command |
|------|---------|
| Build | `gradle build` |
| Test | `gradle test` |
| Run | `gradle run` |
| Clean | `gradle clean` |
| Publish | `gradle publish` |
| Check dependencies | `gradle dependencies` |
| List tasks | `gradle tasks` |
| Help | `gradle help --task build` |

## Summary

Gradle is powerful and flexible. This cheat sheet covers the most common tasks. For more details, refer to [Gradle Documentation](https://docs.gradle.org/).
