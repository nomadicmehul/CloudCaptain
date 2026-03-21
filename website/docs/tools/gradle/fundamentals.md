---
title: "Gradle Fundamentals"
sidebar_label: "Fundamentals"
description: "Gradle build automation, DSL, tasks, plugins, dependencies, multi-project builds"
sidebar_position: 1
---

# Gradle Fundamentals

Gradle is a modern, flexible build automation tool that uses a Groovy or Kotlin-based Domain Specific Language (DSL). It improves upon Maven and Ant by combining their best features with powerful scripting capabilities.

## What is Gradle?

Gradle is an open-source build automation tool that automates the compilation, testing, packaging, and deployment of software. It evolved from Ant and Maven, addressing their limitations while providing superior performance and flexibility.

**Key characteristics:**
- **Convention over Configuration** — Sensible defaults reduce boilerplate
- **Incremental Builds** — Only rebuilds what changed
- **Powerful DSL** — Groovy or Kotlin-based, not XML
- **Plugin Ecosystem** — Extensible with custom plugins
- **Dependency Management** — Advanced resolution and caching
- **Multi-Project Builds** — Efficient builds for complex projects

## History and Evolution

### Ant → Maven → Gradle

**Ant (2000)**
- Procedural XML syntax
- Full control but verbose
- Limited dependency management

**Maven (2004)**
- Declarative XML
- Convention-based structure
- Built-in dependency management
- Struggled with complex customizations

**Gradle (2012)**
- Groovy/Kotlin DSL
- Combines best of both worlds
- Incremental compilation
- Superior performance

## Core Concepts

### Build Script Structure

```groovy
// build.gradle

// 1. Apply plugins
plugins {
    id 'java'
    id 'application'
}

// 2. Configure source and target versions
java {
    sourceCompatibility = JavaVersion.VERSION_11
    targetCompatibility = JavaVersion.VERSION_11
}

// 3. Define repositories
repositories {
    mavenCentral()
    google()
}

// 4. Declare dependencies
dependencies {
    implementation 'com.google.guava:guava:31.0-jre'
    testImplementation 'junit:junit:4.13.2'
}

// 5. Configure application
application {
    mainClass = 'com.example.App'
}

// 6. Define custom tasks
tasks.register('hello') {
    doLast {
        println 'Hello, World!'
    }
}
```

### Project Structure

```
my-project/
├── gradle/                    # Gradle wrapper files
│   └── wrapper/
│       ├── gradle-wrapper.jar
│       └── gradle-wrapper.properties
├── src/
│   ├── main/
│   │   ├── java/
│   │   └── resources/
│   └── test/
│       ├── java/
│       └── resources/
├── build.gradle               # Build configuration
├── gradle.properties          # Gradle properties
├── settings.gradle            # Multi-project configuration
├── gradlew                    # Gradle wrapper script
├── gradlew.bat               # Gradle wrapper (Windows)
└── pom.xml                   # Optional: Legacy Maven config
```

## Groovy vs Kotlin DSL

### Groovy DSL (Traditional)

```groovy
// build.gradle
plugins {
    id 'java'
}

repositories {
    mavenCentral()
}

dependencies {
    implementation 'junit:junit:4.13.2'
}

tasks {
    build {
        doLast {
            println "Build complete!"
        }
    }
}
```

### Kotlin DSL (Modern)

```kotlin
// build.gradle.kts
plugins {
    id("java")
}

repositories {
    mavenCentral()
}

dependencies {
    implementation("junit:junit:4.13.2")
}

tasks {
    build {
        doLast {
            println("Build complete!")
        }
    }
}
```

**Advantages of Kotlin DSL:**
- Type-safe
- IDE autocomplete support
- Compile-time error checking
- Better for large builds

## Tasks

### Defining Tasks

```groovy
// Simple task
task hello {
    doLast {
        println 'Hello, World!'
    }
}

// Task with inputs and outputs
task processData {
    inputs.file 'data/input.txt'
    outputs.file 'data/output.txt'
    doLast {
        // Process file
    }
}

// Task with description
task deploy {
    description = 'Deploy application'
    group = 'deployment'
    doLast {
        println 'Deploying...'
    }
}

// Task with dependencies
task compile
task test {
    dependsOn compile
}

// Task ordering
task buildApp {
    mustRunAfter test
}
```

### Running Tasks

```bash
# List available tasks
gradle tasks

# Run specific task
gradle hello
gradle build

# Run multiple tasks
gradle clean build test

# Run with options
gradle build --debug
gradle test -x integrationTest  # Exclude integration tests

# Parallel builds
gradle build -x test --parallel

# Show detailed output
gradle build --info
```

## Plugins

### Using Plugins

```groovy
plugins {
    id 'java'                    // Core Java plugin
    id 'org.springframework.boot' version '2.6.0'  // Third-party plugin
    id 'com.example.custom'      // Custom plugin
}
```

### Common Plugins

```groovy
plugins {
    id 'java'               // Java compilation, testing
    id 'application'        // Create executable JARs
    id 'maven-publish'      // Publish to Maven repos
    id 'java-library'       // Java library (exports API)
}
```

### Built-in Java Plugin Tasks

The `java` plugin automatically provides:

| Task | Purpose |
|------|---------|
| `compileJava` | Compile source code |
| `processResources` | Process resource files |
| `classes` | Compile and process resources |
| `jar` | Create JAR archive |
| `test` | Run unit tests |
| `build` | Full build (depends on all above) |
| `clean` | Remove build artifacts |

## Dependency Management

### Repository Configuration

```groovy
repositories {
    // Central Maven repository
    mavenCentral()

    // Google's Maven repository
    google()

    // JCenter (deprecated, but some projects still use)
    jcenter()

    // Custom repository
    maven {
        url = uri('https://repo.company.com/releases')
        credentials {
            username = 'user'
            password = 'pass'
        }
    }

    // Ivy repository
    ivy {
        url = uri('https://ivy.company.com')
    }
}
```

### Declaring Dependencies

```groovy
dependencies {
    // Implementation dependency (compiled and on runtime classpath)
    implementation 'com.google.guava:guava:31.0-jre'

    // API dependency (exposed to consumers)
    api 'org.apache.commons:commons-lang3:3.12.0'

    // Test implementation
    testImplementation 'junit:junit:4.13.2'

    // Annotation processor
    annotationProcessor 'org.projectlombok:lombok:1.18.22'

    // Runtime only
    runtimeOnly 'mysql:mysql-connector-java:8.0.26'

    // Multiple dependencies
    implementation(
        'com.google.guava:guava:31.0-jre',
        'org.apache.commons:commons-lang3:3.12.0'
    )

    // With version constraint
    implementation 'org.slf4j:slf4j-api:1.7.+'
}
```

### Dependency Resolution

```groovy
// Exclude transitive dependency
dependencies {
    implementation('org.springframework.boot:spring-boot-starter') {
        exclude module: 'spring-boot-starter-logging'
    }
}

// Force version
configurations.all {
    resolutionStrategy {
        force 'org.slf4j:slf4j-api:1.7.36'
    }
}
```

## Multi-Project Builds

### Settings File

```groovy
// settings.gradle
rootProject.name = 'my-app'

include(
    'shared',
    'api',
    'web',
    'cli'
)

// Nested modules
include(':services:auth')
include(':services:payment')
```

### Project Structure

```
my-app/
├── settings.gradle
├── build.gradle            # Root build configuration
├── shared/
│   └── build.gradle
├── api/
│   └── build.gradle
├── web/
│   └── build.gradle
└── cli/
    └── build.gradle
```

### Root Build Configuration

```groovy
// build.gradle - Apply to all projects
allprojects {
    group = 'com.example'
    version = '1.0.0'
}

// Apply only to subprojects
subprojects {
    repositories {
        mavenCentral()
    }

    dependencies {
        testImplementation 'junit:junit:4.13.2'
    }
}

// Project-specific configuration
project(':api') {
    dependencies {
        implementation project(':shared')
    }
}
```

## Build Properties

### gradle.properties

```properties
# JVM arguments
org.gradle.jvmargs=-Xmx2g

# Parallel builds
org.gradle.parallel=true

# Gradle daemon
org.gradle.daemon=true

# Build cache
org.gradle.build.cache=true

# Logging level
org.gradle.logging.level=lifecycle

# Custom properties
app.version=1.0.0
java.version=11
```

### System Properties

```bash
# Via command line
gradle build -Dorg.gradle.jvmargs=-Xmx4g

# Via environment variable
export GRADLE_OPTS=-Xmx4g
gradle build

# Via properties file
echo "org.gradle.jvmargs=-Xmx4g" >> gradle.properties
```

## Java Configuration

```groovy
java {
    sourceCompatibility = JavaVersion.VERSION_11
    targetCompatibility = JavaVersion.VERSION_11
}

// Or using compiler options
tasks.compileJava {
    options.release = 11
}
```

## Testing

```groovy
// Configure test task
test {
    useJUnit()  // Use JUnit 4

    testLogging {
        events "passed", "skipped", "failed"
        showStandardStreams = true
    }
}

// Or JUnit 5
test {
    useJUnitPlatform()
}

// Skip tests during build
gradle build -x test

// Run specific test
gradle test --tests *TestClass
```

## Gradle Wrapper

The Gradle Wrapper allows projects to be built without Gradle installation:

```bash
# Create wrapper
gradle wrapper

# Or specify version
gradle wrapper --gradle-version 7.0

# Run with wrapper (Unix)
./gradlew build

# Run with wrapper (Windows)
gradlew.bat build
```

## Performance Optimization

```groovy
// Enable build cache
org.gradle.build.cache=true

// Parallel builds
org.gradle.parallel=true

// Daemon (background process)
org.gradle.daemon=true

// Incremental compilation
compileJava {
    options.incremental = true
}
```

## Custom Plugins

```groovy
// build.gradle - Custom task
class GreetingTask extends DefaultTask {
    @Input
    String greeting = 'Hello'

    @TaskAction
    void greet() {
        println "$greeting from $name"
    }
}

tasks.register('myGreeting', GreetingTask) {
    greeting = 'Hi'
}
```

## Exercises

### Exercise 1: Basic Java Project
1. Create a new Gradle Java project
2. Define a simple main class
3. Configure dependencies (JUnit for testing)
4. Build and run the JAR

### Exercise 2: Multi-Project Build
1. Create a multi-project structure
2. Configure dependency between projects
3. Build entire project structure
4. Verify builds complete successfully

### Exercise 3: Custom Tasks
1. Define a custom Gradle task
2. Configure task dependencies
3. Add task descriptions and groups
4. Execute and verify output

### Exercise 4: Dependency Management
1. Add multiple dependencies
2. Exclude transitive dependencies
3. Force specific versions
4. Build and verify resolution

### Exercise 5: Plugin Development
1. Create a simple Gradle plugin
2. Apply plugin to build
3. Configure plugin settings
4. Verify plugin functionality

## Common Build Patterns

### Simple Application

```groovy
plugins {
    id 'java'
    id 'application'
}

java {
    sourceCompatibility = JavaVersion.VERSION_11
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

### Library Project

```groovy
plugins {
    id 'java-library'
    id 'maven-publish'
}

java {
    sourceCompatibility = JavaVersion.VERSION_11
}

repositories {
    mavenCentral()
}

dependencies {
    api 'com.google.guava:guava:31.0-jre'
}

publishing {
    publications {
        maven(MavenPublication) {
            from components.java
        }
    }
}
```

## Summary

Gradle is a powerful, modern build tool that:
- Provides flexible Groovy/Kotlin DSL
- Handles complex multi-project builds efficiently
- Manages dependencies with precision
- Integrates deeply with the Java ecosystem
- Offers excellent performance and caching

Master Gradle to build scalable, maintainable Java projects.
