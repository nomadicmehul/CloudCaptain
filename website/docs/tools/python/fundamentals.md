---
title: "Python Fundamentals"
description: "Python basics for DevOps: data types, control flow, functions, file I/O, error handling, and OOP"
sidebar_label: "Fundamentals"
sidebar_position: 1
---

# Python Fundamentals

Master the core concepts of Python essential for DevOps automation and scripting.

## Data Types

### Numbers

Python supports two main numeric types:

**Integers** - whole numbers without decimals
```python
count = 42
port = 8080
```

**Floats** - numbers with decimals
```python
cpu_usage = 95.5
disk_percent = 78.25
```

Common operations:
```python
# Arithmetic operators
result = 10 + 5       # Addition
result = 10 - 5       # Subtraction
result = 10 * 5       # Multiplication
result = 10 / 5       # Division (returns float)
result = 10 // 3      # Floor division (returns 3)
result = 10 % 3       # Modulo/remainder (returns 1)
result = 2 ** 8       # Exponentiation (returns 256)
```

### Strings

Text data enclosed in quotes:
```python
message = "Hello, DevOps!"
filename = 'config.yaml'

# Multi-line strings
description = """
This is a multi-line
string for documentation
"""
```

**String formatting:**
```python
# f-strings (Python 3.6+) - recommended
name = "server"
ip = "192.168.1.10"
print(f"Connecting to {name} at {ip}")

# .format() method
print("Server: {}, Status: {}".format("prod-01", "healthy"))

# % formatting (legacy)
print("Error code: %d" % 500)
```

**String operations:**
```python
text = "DevOps Pipeline"
print(len(text))           # Length: 15
print(text.lower())        # devops pipeline
print(text.upper())        # DEVOPS PIPELINE
print(text.replace("Pipeline", "Workflow"))
print("DevOps" in text)    # True
```

### Boolean

True or False values:
```python
is_running = True
is_healthy = False
container_status = True
```

### Collections

**Lists** - ordered, mutable collections
```python
servers = ["web-01", "web-02", "db-01"]
ports = [8080, 8443, 5432]

# Access elements
print(servers[0])         # web-01
print(servers[-1])        # db-01 (last element)

# Slicing
print(servers[1:])        # ["web-02", "db-01"]
print(servers[:2])        # ["web-01", "web-02"]

# Modify lists
servers.append("cache-01")
servers.remove("db-01")
servers.pop(0)            # Remove and return first element
```

**Tuples** - ordered, immutable collections
```python
config = ("localhost", 3306, "mysql")
print(config[1])          # 3306

# Good for function return values
address, port, service = config
```

**Dictionaries** - key-value pairs
```python
server_info = {
    "hostname": "prod-web-01",
    "ip": "10.0.0.5",
    "port": 8080,
    "status": "running"
}

# Access values
print(server_info["hostname"])           # prod-web-01
print(server_info.get("port", 80))       # 8080 (default to 80 if missing)

# Modify dictionaries
server_info["status"] = "stopped"
server_info["uptime"] = 48  # Add new key

# Iterate
for key, value in server_info.items():
    print(f"{key}: {value}")
```

**Sets** - unordered, unique collections
```python
active_services = {"nginx", "postgresql", "redis"}
running = {"nginx", "redis", "docker"}

# Set operations
print(active_services & running)        # Intersection: {"nginx", "redis"}
print(active_services | running)        # Union
print(active_services - running)        # Difference
```

## Control Flow

### Conditionals

```python
status_code = 200

if status_code == 200:
    print("Success")
elif status_code == 404:
    print("Not found")
elif status_code == 500:
    print("Server error")
else:
    print("Other status")
```

**Comparison operators:**
```python
x == y      # Equal
x != y      # Not equal
x > y       # Greater than
x < y       # Less than
x >= y      # Greater than or equal
x <= y      # Less than or equal
```

**Logical operators:**
```python
if status == "running" and cpu < 80:
    print("Service healthy")

if env == "prod" or env == "staging":
    print("Production environment")

if not is_error:
    print("No errors detected")
```

### Loops

**For loops** - iterate over sequences
```python
servers = ["web-01", "web-02", "db-01"]
for server in servers:
    print(f"Checking {server}")

# With index
for index, server in enumerate(servers):
    print(f"{index}: {server}")

# Range
for i in range(5):      # 0 to 4
    print(i)

for i in range(2, 10, 2):  # 2, 4, 6, 8
    print(i)
```

**While loops** - repeat while condition is true
```python
retry_count = 0
max_retries = 3

while retry_count < max_retries:
    try:
        response = connect_to_server()
        break
    except ConnectionError:
        retry_count += 1
        print(f"Retry {retry_count} of {max_retries}")
```

**Loop control:**
```python
for service in services:
    if service == "temp":
        continue      # Skip this iteration

    if service == "broken":
        break         # Exit loop

    process(service)
```

## Functions

Reusable blocks of code:

```python
def greet(name):
    """Print a greeting message."""
    print(f"Hello, {name}!")

greet("DevOps")
```

**Parameters and return values:**
```python
def check_port(host, port, timeout=5):
    """Check if a port is open."""
    # Default parameter: timeout=5
    try:
        socket.create_connection((host, port), timeout)
        return True
    except socket.timeout:
        return False

# Call with positional arguments
result = check_port("localhost", 8080)

# Call with keyword arguments
result = check_port(host="localhost", port=8080, timeout=10)
```

**Multiple return values:**
```python
def parse_log_line(line):
    """Extract timestamp and message from log line."""
    timestamp, message = line.split(" - ")
    return timestamp, message

ts, msg = parse_log_line("2024-01-15 10:30:00 - Service started")
```

**Variable-length arguments:**
```python
def print_servers(*servers):
    """Print all provided servers."""
    for server in servers:
        print(server)

print_servers("web-01", "web-02", "db-01")

def create_config(**kwargs):
    """Create config from keyword arguments."""
    config = {}
    for key, value in kwargs.items():
        config[key] = value
    return config

cfg = create_config(host="localhost", port=3306, user="admin")
```

## File I/O

**Reading files:**
```python
# Read entire file
with open("config.yaml", "r") as f:
    content = f.read()

# Read line by line
with open("access.log", "r") as f:
    for line in f:
        print(line.strip())

# Read all lines as list
with open("hosts.txt", "r") as f:
    lines = f.readlines()
```

**Writing files:**
```python
# Write new file (overwrites if exists)
with open("output.txt", "w") as f:
    f.write("Line 1\n")
    f.write("Line 2\n")

# Append to file
with open("output.txt", "a") as f:
    f.write("Line 3\n")

# Write multiple lines
with open("servers.txt", "w") as f:
    f.writelines(["server1\n", "server2\n", "server3\n"])
```

**Best practice - use context managers:**
```python
# Context manager (with statement) automatically closes file
with open("data.txt", "r") as f:
    data = f.read()
# File is automatically closed here
```

## Error Handling

**Try-except blocks:**
```python
try:
    file = open("missing.txt", "r")
    content = file.read()
except FileNotFoundError:
    print("File not found")
except IOError as e:
    print(f"Error reading file: {e}")
finally:
    # Always runs
    if file:
        file.close()
```

**Common exceptions:**
```python
# Value error
int("not a number")  # ValueError

# Index error
list_var = [1, 2, 3]
print(list_var[10])  # IndexError

# Key error
dict_var = {"a": 1}
print(dict_var["z"])  # KeyError

# Type error
result = "string" + 5  # TypeError

# Attribute error
class Config:
    pass
cfg = Config()
print(cfg.missing_attr)  # AttributeError
```

**Raising exceptions:**
```python
def validate_port(port):
    if not isinstance(port, int) or port < 0 or port > 65535:
        raise ValueError(f"Invalid port: {port}")
    return port

try:
    validate_port(99999)
except ValueError as e:
    print(f"Validation failed: {e}")
```

## Modules and Imports

**Standard library modules:**
```python
import os                          # Operating system
import sys                         # System functions
import subprocess                  # Run external commands
import json                        # JSON parsing
import yaml                        # YAML parsing
import logging                     # Logging
import shutil                      # File operations

# Use imported module
print(os.getcwd())
result = subprocess.run(["ls", "-la"], capture_output=True)
```

**Importing specific items:**
```python
from datetime import datetime, timedelta
from pathlib import Path

now = datetime.now()
tomorrow = now + timedelta(days=1)
config_path = Path("/etc/app/config.yaml")
```

**Creating your own modules:**
```python
# utils.py
def validate_hostname(hostname):
    return len(hostname) > 0 and "." in hostname

def parse_config(filepath):
    with open(filepath, "r") as f:
        return f.read()

# main.py
import utils
if utils.validate_hostname("prod.example.com"):
    print("Valid")
```

## Object-Oriented Programming Basics

**Classes:**
```python
class Server:
    """Represents a server."""

    def __init__(self, hostname, ip, port=22):
        """Initialize server."""
        self.hostname = hostname
        self.ip = ip
        self.port = port
        self.status = "unknown"

    def connect(self):
        """Connect to server."""
        print(f"Connecting to {self.hostname} ({self.ip}:{self.port})")
        self.status = "connected"

    def disconnect(self):
        """Disconnect from server."""
        self.status = "disconnected"

    def info(self):
        """Return server info."""
        return f"{self.hostname} at {self.ip}:{self.port} ({self.status})"

# Create and use objects
server = Server("web-01", "10.0.0.5", 22)
server.connect()
print(server.info())
```

**Inheritance:**
```python
class LinuxServer(Server):
    """Linux-specific server."""

    def __init__(self, hostname, ip, port=22, distro="Ubuntu"):
        super().__init__(hostname, ip, port)
        self.distro = distro

    def update_packages(self):
        print(f"Updating packages on {self.hostname}")

class WindowsServer(Server):
    """Windows-specific server."""

    def __init__(self, hostname, ip, port=3389):
        super().__init__(hostname, ip, port)

    def update_windows(self):
        print(f"Running Windows Update on {self.hostname}")
```

## Exercises

1. **Temperature Converter**: Write a function that converts Celsius to Fahrenheit. Test it with multiple values.

2. **Log Parser**: Read a log file, count errors by type, and return a dictionary with counts.

3. **Configuration Validator**: Create a class that validates server configurations (hostname, IP, port).

4. **File Backup**: Write a script that backs up all `.conf` files from `/etc/` to a backup directory, preserving directory structure.

5. **List Processing**: Given a list of numbers, write a function that returns those divisible by 3, using list comprehension.

6. **Exception Handling**: Improve the temperature converter to handle invalid input gracefully.

7. **Module Creation**: Create a `network_utils.py` module with functions for validating IP addresses and checking host connectivity.

## Summary

- **Data types**: int, float, string, list, tuple, dict, set, bool
- **Control flow**: if/elif/else, for, while loops
- **Functions**: reusable code with parameters and return values
- **File I/O**: reading and writing with context managers
- **Error handling**: try-except-finally for robust code
- **Modules**: reuse code across files
- **OOP**: classes for organized, reusable code structures
