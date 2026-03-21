---
title: "Gradle"
sidebar_label: "Gradle Overview"
description: "Gradle build automation, DSL, tasks, plugins, and multi-project builds"
sidebar_position: 0
---

# Gradle

Gradle is a modern, flexible build automation tool that uses a Groovy or Kotlin-based Domain Specific Language (DSL). It combines the best of Ant and Maven while providing superior performance, incremental builds, and powerful customization.

## What is Gradle?

Gradle is an open-source build automation tool designed for multi-language software development. It evolved from Ant (procedural) and Maven (declarative) by providing a balance between flexibility and convention.

**Key advantages:**
- **Groovy/Kotlin DSL** — Expressive, readable build scripts (not XML)
- **Incremental Builds** — Only rebuild what changed
- **Plugin Ecosystem** — Extensible with custom and community plugins
- **Multi-Project Support** — Efficient builds for complex projects
- **Dependency Management** — Advanced resolution and caching
- **CI/CD Ready** — Built for automation and scalability
- **Performance** — Fast, parallel builds with caching

## Quick Start

### Install Gradle

```bash
# macOS
brew install gradle

# Ubuntu/Debian
sudo apt-get install gradle

# Verify
gradle --version
```

### Create Project

```bash
gradle init
```

### Build Project

```bash
./gradlew build
```

## Documentation

| Guide | Description |
|:------|:------------|
| [Gradle Fundamentals](/docs/tools/gradle/fundamentals) | DSL, tasks, plugins, dependencies, multi-project builds |
| [Gradle Cheat Sheet](/docs/tools/gradle/cheatsheet) | Commands, build.gradle patterns, common configurations |

## Core Concepts

### Build Script Structure

```groovy
// build.gradle
plugins {
    id 'java'
    id 'application'
}

repositories {
    mavenCentral()
}

dependencies {
    implementation 'com.google.guava:guava:31.0-jre'
    testImplementation 'junit:junit:4.13.2'
}

application {
    mainClass = 'com.example.Main'
}
```

### Common Commands

```bash
gradle build              # Full build
gradle test              # Run tests
gradle clean             # Clean artifacts
gradle run               # Run application
gradle tasks             # List available tasks
gradle --help task_name  # Help for specific task
```

## Gradle vs Maven vs Ant

| Feature | Gradle | Maven | Ant |
|---------|--------|-------|-----|
| **Syntax** | Groovy/Kotlin DSL | XML | XML |
| **Configuration** | Convention + flexibility | Convention heavy | Procedural |
| **Incremental** | Yes (fast) | No (rebuilds) | No (rebuilds) |
| **Learning curve** | Moderate | Gentle | Steep |
| **Extension** | Plugins + DSL | Plugins | Manual |
| **Multi-project** | Excellent | Good | Manual |

## Why Choose Gradle?

1. **Performance** — Incremental builds and caching
2. **Flexibility** — Powerful DSL for complex requirements
3. **Modern tooling** — Built for CI/CD and automation
4. **Rich ecosystem** — Spring Boot, Kotlin, Android official build tool
5. **Developer experience** — IDE support, debugging, profiling

## Features

- **Convention over Configuration** — Sensible defaults reduce boilerplate
- **Tasks** — Define custom build steps
- **Plugins** — Extend functionality (Java, Spring Boot, Android, etc.)
- **Dependencies** — Sophisticated dependency resolution
- **Build Cache** — Reuse build outputs across machines
- **Parallel Builds** — Use multi-core systems efficiently
- **Gradle Wrapper** — Build without Gradle installation

## Common Use Cases

### Java Application

```groovy
plugins {
    id 'java'
    id 'application'
}

repositories {
    mavenCentral()
}

dependencies {
    implementation 'com.google.guava:guava:31.0-jre'
    testImplementation 'junit:junit:4.13.2'
}

application {
    mainClass = 'com.example.Main'
}
```

### Spring Boot Application

```groovy
plugins {
    id 'java'
    id 'org.springframework.boot' version '2.6.0'
}

group = 'com.example'
version = '1.0.0'

dependencies {
    implementation 'org.springframework.boot:spring-boot-starter-web'
    testImplementation 'org.springframework.boot:spring-boot-starter-test'
}
```

### Multi-Project Build

```groovy
// settings.gradle
include 'api', 'web', 'cli'

// Root build.gradle
subprojects {
    repositories { mavenCentral() }
    dependencies {
        testImplementation 'junit:junit:4.13.2'
    }
}

project(':web') {
    dependencies {
        implementation project(':api')
    }
}
```

## Gradle Wrapper

Gradle Wrapper ensures consistent builds across environments:

```bash
# Create wrapper
gradle wrapper

# Use wrapper (simpler, no Gradle installation needed)
./gradlew build

# Wrapper includes gradlew, gradlew.bat, gradle/wrapper/
```

## Integration with IDEs

- **IntelliJ IDEA** — Native support
- **Eclipse** — Buildship plugin
- **VS Code** — Gradle extension
- **NetBeans** — Gradle support plugin

## Next Steps

- **[Gradle Fundamentals](/docs/tools/gradle/fundamentals)** — Deep dive into DSL, tasks, plugins
- **[Gradle Cheat Sheet](/docs/tools/gradle/cheatsheet)** — Quick command and configuration reference
- **[Official Documentation](https://docs.gradle.org/)** — Complete reference
- **[Spring Boot Gradle Plugin](https://docs.spring.io/spring-boot/docs/current/gradle-plugin/reference/)** — Spring Boot integration

## Common Tasks

| Task | Command | Purpose |
|------|---------|---------|
| Build | `gradle build` | Compile, test, package |
| Test | `gradle test` | Run unit tests |
| Run | `gradle run` | Execute application |
| Clean | `gradle clean` | Remove build artifacts |
| Publish | `gradle publish` | Publish to repository |
| Dependencies | `gradle dependencies` | Show dependency tree |

## Tips

- Use `./gradlew` (wrapper) for consistency
- Enable daemon for faster builds: `org.gradle.daemon=true`
- Use build cache: `org.gradle.build.cache=true`
- Parallel builds: `org.gradle.parallel=true`
- Always version your dependencies for reproducibility

## Contributing

Have Gradle tips, patterns, or best practices to share? [Contribute](https://github.com/nomadicmehul/CloudCaptain) to CloudCaptain!
