---
title: "Python Interview Questions"
description: "35+ Python interview questions focused on DevOps context with detailed answers"
sidebar_label: "Interview Questions"
sidebar_position: 4
---

# Python Interview Questions for DevOps

Prepare for DevOps engineer interviews with these 35+ Python questions and answers.

## Fundamentals

### 1. What are the differences between lists, tuples, and dictionaries?

**Answer:**
- **Lists**: Ordered, mutable (changeable), allow duplicates. Use `[1, 2, 3]`
- **Tuples**: Ordered, immutable (unchangeable), allow duplicates. Use `(1, 2, 3)`. Better performance, used as dict keys
- **Dictionaries**: Unordered, mutable, key-value pairs. Use `{"key": "value"}`

```python
# Lists - mutable
servers = ["web-01", "web-02"]
servers[0] = "web-03"  # OK

# Tuples - immutable
config = ("localhost", 3306)
config[0] = "remote"  # TypeError

# Dictionaries
server = {"name": "web-01", "port": 8080}
server["status"] = "running"  # OK
```

### 2. What is the difference between `==` and `is`?

**Answer:**
- `==` checks **value equality** (two objects have same content)
- `is` checks **identity** (two variables reference same object)

```python
a = [1, 2, 3]
b = [1, 2, 3]
c = a

a == b  # True (same values)
a is b  # False (different objects)
a is c  # True (same object reference)

# This is why comparing with None uses 'is'
if x is None:
    pass
```

### 3. Explain list comprehensions and their advantages

**Answer:**
List comprehensions provide a concise, readable way to create lists:

```python
# Traditional
squares = []
for x in range(5):
    squares.append(x**2)

# List comprehension
squares = [x**2 for x in range(5)]

# With conditions
even_squares = [x**2 for x in range(10) if x % 2 == 0]

# Nested comprehensions
matrix = [[i*j for j in range(3)] for i in range(3)]
```

**Advantages**: More readable, faster execution, less code, Pythonic.

### 4. What is a lambda function and when would you use it?

**Answer:**
Lambda functions are anonymous, single-expression functions:

```python
# Basic lambda
square = lambda x: x**2
print(square(5))  # 25

# Common use: sorting
servers = [{"name": "web-01", "cpu": 45}, {"name": "db-01", "cpu": 78}]
sorted_by_cpu = sorted(servers, key=lambda x: x["cpu"])

# With map and filter
numbers = [1, 2, 3, 4, 5]
doubled = list(map(lambda x: x*2, numbers))  # [2, 4, 6, 8, 10]
evens = list(filter(lambda x: x % 2 == 0, numbers))  # [2, 4]
```

**Use case**: Short operations in `map()`, `filter()`, `sorted()`. Avoid for complex logic.

### 5. What is unpacking in Python?

**Answer:**
Unpacking assigns values from a sequence to multiple variables:

```python
# Basic unpacking
a, b, c = [1, 2, 3]
a, b = (10, 20)

# Tuple unpacking in function return
def get_server_info():
    return ("prod-01", "10.0.0.5", 22)

hostname, ip, port = get_server_info()

# Extended unpacking
a, *rest, z = [1, 2, 3, 4, 5]
# a=1, rest=[2,3,4], z=5

# Unpacking in loops
servers = [("web-01", "10.0.0.1"), ("web-02", "10.0.0.2")]
for name, ip in servers:
    print(f"{name}: {ip}")
```

## String and Data Manipulation

### 6. How do you work with strings in Python? Explain f-strings vs format()

**Answer:**
Modern approach is f-strings (Python 3.6+):

```python
name = "DevOps"
version = 2.5

# f-strings (best)
print(f"Hello {name} v{version}")
print(f"Calculation: {2 + 2}")
print(f"Uppercase: {name.upper()}")

# .format() (legacy)
print("Hello {} v{}".format(name, version))
print("Hello {0} v{1}".format(name, version))

# % formatting (outdated)
print("Hello %s v%.1f" % (name, version))

# Real-world DevOps example
deployment = {
    "app": "api-service",
    "version": "3.2.1",
    "replicas": 5
}
message = f"Deploying {deployment['app']} v{deployment['version']} with {deployment['replicas']} replicas"
```

### 7. How would you parse JSON and YAML in Python?

**Answer:**
```python
import json
import yaml

# JSON parsing
json_str = '{"host": "localhost", "port": 3306}'
data = json.loads(json_str)
print(data["host"])  # localhost

# JSON from file
with open("config.json") as f:
    config = json.load(f)

# Write JSON
with open("output.json", "w") as f:
    json.dump(config, f, indent=2)

# YAML parsing
yaml_str = """
servers:
  - name: web-01
    ip: 10.0.0.1
    port: 8080
"""
data = yaml.safe_load(yaml_str)
for server in data["servers"]:
    print(f"{server['name']}: {server['ip']}")

# YAML from file
with open("inventory.yaml") as f:
    inventory = yaml.safe_load(f)
```

### 8. What are common string methods you use for DevOps?

**Answer:**
```python
# Parsing and validation
hostname = "prod-web-01.example.com"
hostname.startswith("prod")     # True
hostname.endswith(".com")       # True
hostname.split(".")             # ["prod-web-01", "example", "com"]
".".join(["192", "168", "1", "1"])  # "192.168.1.1"

# Case handling
"ERROR: Connection failed".lower()
"warning: disk full".upper()

# Stripping whitespace
"  message  ".strip()           # "message"

# Replacement
"old-server".replace("old", "new")  # "new-server"

# Finding substrings
index = "app.log.2024".find(".log")  # 3
if "error" in log_line:
    handle_error()

# Checking content
"12345".isdigit()
"abc.txt".endswith((".log", ".txt"))
```

## File and System Operations

### 9. How do you read and write files safely in Python?

**Answer:**
Use context managers with `with` statement:

```python
# Reading
with open("config.yaml", "r") as f:
    content = f.read()
# File automatically closed

# Writing (overwrites)
with open("output.txt", "w") as f:
    f.write("content\n")
    f.writelines(["line1\n", "line2\n"])

# Appending
with open("log.txt", "a") as f:
    f.write("new log entry\n")

# Line by line reading (memory efficient)
with open("large_file.log") as f:
    for line in f:
        process(line.strip())

# Real-world: Config backup
import shutil
with open("config.yaml") as source:
    with open("config.yaml.bak", "w") as backup:
        backup.write(source.read())

# Or simpler:
shutil.copy("config.yaml", "config.yaml.bak")
```

### 10. How would you execute shell commands from Python?

**Answer:**
Use `subprocess` module:

```python
import subprocess

# Basic execution
result = subprocess.run(["ls", "-la"], capture_output=True, text=True)
print(result.stdout)
print(result.returncode)  # 0 = success

# Check for errors
if result.returncode != 0:
    print(f"Error: {result.stderr}")

# Using shell features (less safe)
result = subprocess.run("echo $HOME", shell=True, text=True, capture_output=True)

# With timeout
try:
    result = subprocess.run(
        ["sleep", "10"],
        timeout=5,
        capture_output=True
    )
except subprocess.TimeoutExpired:
    print("Command timed out")

# Real-world: Check service status
def is_service_running(service):
    result = subprocess.run(
        ["systemctl", "is-active", service],
        capture_output=True
    )
    return result.returncode == 0
```

### 11. What is the difference between relative and absolute imports?

**Answer:**
```python
# Absolute import (preferred)
from mypackage.mymodule import MyClass
import utils.validators

# Relative import (within package)
from . import config          # Current package
from ..utils import helpers   # Parent package

# Real-world project structure:
# myproject/
#   ├── main.py
#   ├── config.py
#   └── utils/
#       ├── __init__.py
#       └── validators.py

# In utils/validators.py
from .. import config  # Access parent package

# In main.py
from utils.validators import validate_ip  # Absolute import
```

## Functions and Control Flow

### 12. What are `*args` and `**kwargs`?

**Answer:**
Allow functions to accept variable arguments:

```python
# *args - variable positional arguments (tuple)
def process_servers(*servers):
    for server in servers:
        print(f"Processing {server}")

process_servers("web-01", "web-02", "db-01")

# **kwargs - variable keyword arguments (dict)
def create_config(**options):
    for key, value in options.items():
        print(f"Setting {key} = {value}")

create_config(host="localhost", port=8080, debug=True)

# Combined
def deploy(app_name, version, *environments, **options):
    print(f"Deploying {app_name} v{version}")
    print(f"Environments: {environments}")
    print(f"Options: {options}")

deploy("api", "1.0", "dev", "staging", "prod", replicas=3, timeout=60)
```

### 13. What is the difference between a generator and a list?

**Answer:**
Generators are memory-efficient, lazy-evaluated sequences:

```python
# List - loads all in memory
def get_numbers_list():
    numbers = []
    for i in range(1000000):
        numbers.append(i)
    return numbers

# Generator - yields one at a time
def get_numbers_generator():
    for i in range(1000000):
        yield i

# Generator expression
numbers = (i for i in range(1000000))

# Practical use
for num in get_numbers_generator():
    process(num)  # Memory-efficient

# Reading large files line-by-line
with open("huge_log.txt") as f:
    for line in f:  # Uses generator-like iteration
        process(line)

# Can only iterate once
gen = get_numbers_generator()
list(gen)  # Gets all values
list(gen)  # Empty! Generator exhausted
```

### 14. How do you handle exceptions properly?

**Answer:**
```python
# Basic try-except
try:
    result = int("invalid")
except ValueError:
    print("Invalid number")

# Multiple exceptions
try:
    file = open("config.yaml")
    data = yaml.safe_load(file)
except FileNotFoundError:
    print("Config file not found")
except yaml.YAMLError as e:
    print(f"Invalid YAML: {e}")

# Catch all (rarely use)
try:
    operation()
except Exception as e:
    print(f"Unexpected error: {e}")
    raise  # Re-raise if you can't handle

# Else and finally
try:
    result = connect_to_server()
except ConnectionError:
    retry()
else:
    process(result)  # Runs if no exception
finally:
    cleanup()  # Always runs

# Context managers handle exceptions automatically
with open("file.txt") as f:
    # File is properly closed even if error occurs
    content = f.read()
```

### 15. What is a decorator and how would you use one?

**Answer:**
Decorators wrap functions to modify behavior:

```python
# Simple decorator
def log_execution(func):
    def wrapper(*args, **kwargs):
        print(f"Executing {func.__name__}")
        result = func(*args, **kwargs)
        print(f"Completed {func.__name__}")
        return result
    return wrapper

@log_execution
def deploy_app(version):
    print(f"Deploying v{version}")

deploy_app("2.0")

# Decorator with parameters
def retry(max_attempts=3):
    def decorator(func):
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_attempts - 1:
                        raise
                    print(f"Attempt {attempt + 1} failed, retrying...")
            return wrapper
    return decorator

@retry(max_attempts=3)
def flaky_api_call():
    # Implementation
    pass

# Real-world: timing decorator
import time

def timing(func):
    def wrapper(*args, **kwargs):
        start = time.time()
        result = func(*args, **kwargs)
        elapsed = time.time() - start
        print(f"{func.__name__} took {elapsed:.2f}s")
        return result
    return wrapper

@timing
def slow_operation():
    time.sleep(2)
```

## DevOps Specific

### 16. How would you connect to an AWS S3 bucket and upload a file?

**Answer:**
```python
import boto3

def upload_to_s3(local_file, bucket, s3_key):
    s3 = boto3.client("s3")
    try:
        s3.upload_file(local_file, bucket, s3_key)
        print(f"Uploaded {local_file} to s3://{bucket}/{s3_key}")
    except Exception as e:
        print(f"Upload failed: {e}")
        raise

# Usage
upload_to_s3("backup.tar.gz", "my-backup-bucket", "daily/backup-2024-01-15.tar.gz")

# List files in S3
def list_s3_files(bucket, prefix=""):
    s3 = boto3.client("s3")
    response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    for obj in response.get("Contents", []):
        print(obj["Key"])
```

### 17. How would you SSH into a remote server and execute commands?

**Answer:**
```python
import paramiko

def execute_on_server(hostname, username, command, key_file=None):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        if key_file:
            client.connect(hostname, username=username, key_filename=key_file)
        else:
            client.connect(hostname, username=username, password="password")

        stdin, stdout, stderr = client.exec_command(command)
        return stdout.read().decode().strip()
    finally:
        client.close()

# Usage
output = execute_on_server(
    "prod-01.example.com",
    "ubuntu",
    "ps aux | wc -l",
    key_file="/home/user/.ssh/id_rsa"
)
print(f"Process count: {output}")
```

### 18. How would you parse an application log file for errors?

**Answer:**
```python
import re
from collections import defaultdict

def parse_logs(log_file):
    error_pattern = r"\[(\d+)\]\s+ERROR:\s+(.*)"
    errors = defaultdict(int)

    with open(log_file) as f:
        for line in f:
            match = re.search(error_pattern, line)
            if match:
                error_code, message = match.groups()
                errors[error_code] += 1

    return dict(errors)

# Find and log all errors
def find_errors(log_file, since_time=None):
    with open(log_file) as f:
        for line in f:
            if "ERROR" in line or "FATAL" in line:
                print(line.strip())

# Parse specific pattern
def get_response_times(log_file):
    pattern = r"response_time=(\d+\.?\d*)ms"
    times = []

    with open(log_file) as f:
        for line in f:
            matches = re.findall(pattern, line)
            times.extend([float(m) for m in matches])

    return times

# Usage
errors = parse_logs("/var/log/app/error.log")
print(f"Error distribution: {errors}")
```

### 19. How would you monitor system resources (CPU, memory, disk)?

**Answer:**
```python
import psutil

def check_system_health():
    # CPU
    cpu_percent = psutil.cpu_percent(interval=1)
    cpu_count = psutil.cpu_count()

    # Memory
    memory = psutil.virtual_memory()
    memory_percent = memory.percent
    available_gb = memory.available / (1024**3)

    # Disk
    disk = psutil.disk_usage("/")
    disk_percent = disk.percent

    return {
        "cpu": cpu_percent,
        "memory": memory_percent,
        "disk": disk_percent,
        "available_memory_gb": available_gb
    }

def alert_if_thresholds_exceeded(thresholds):
    health = check_system_health()
    alerts = []

    if health["cpu"] > thresholds.get("cpu", 80):
        alerts.append(f"HIGH CPU: {health['cpu']}%")

    if health["memory"] > thresholds.get("memory", 85):
        alerts.append(f"HIGH MEMORY: {health['memory']}%")

    if health["disk"] > thresholds.get("disk", 90):
        alerts.append(f"HIGH DISK: {health['disk']}%")

    return alerts

# Usage
alerts = alert_if_thresholds_exceeded({"cpu": 75, "memory": 80})
for alert in alerts:
    print(f"ALERT: {alert}")
```

### 20. How would you implement retry logic with exponential backoff?

**Answer:**
```python
import time
import random

def retry_with_backoff(func, max_attempts=3, initial_delay=1):
    """Retry function with exponential backoff."""
    for attempt in range(max_attempts):
        try:
            return func()
        except Exception as e:
            if attempt == max_attempts - 1:
                raise

            # Exponential backoff with jitter
            delay = initial_delay * (2 ** attempt)
            jitter = random.uniform(0, delay * 0.1)
            wait_time = delay + jitter

            print(f"Attempt {attempt + 1} failed: {e}")
            print(f"Retrying in {wait_time:.2f}s...")
            time.sleep(wait_time)

# Usage
def unreliable_api_call():
    import requests
    return requests.get("https://api.example.com/data", timeout=5)

data = retry_with_backoff(unreliable_api_call, max_attempts=5)
```

### 21. How would you write a monitoring script that checks multiple services?

**Answer:**
```python
import subprocess
import time
from datetime import datetime

def check_services(services, check_interval=60):
    """Monitor multiple services and report status."""
    while True:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n--- Status Check at {timestamp} ---")

        for service in services:
            result = subprocess.run(
                ["systemctl", "is-active", service],
                capture_output=True
            )
            status = "✓ UP" if result.returncode == 0 else "✗ DOWN"
            print(f"{service}: {status}")

        time.sleep(check_interval)

# Usage
services = ["nginx", "postgresql", "redis", "app-service"]
check_services(services, check_interval=30)
```

## Advanced Topics

### 22. What is a context manager and how do you create one?

**Answer:**
Context managers manage resource setup and cleanup:

```python
# Built-in context managers
with open("file.txt") as f:  # File automatically closed
    content = f.read()

with sqlite3.connect("db.sql") as conn:  # Commit or rollback
    conn.execute("INSERT ...")

# Create custom context manager
class SSHConnection:
    def __init__(self, host, user):
        self.host = host
        self.user = user
        self.client = None

    def __enter__(self):
        self.client = paramiko.SSHClient()
        self.client.connect(self.host, username=self.user)
        return self.client

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.client:
            self.client.close()
        return False

# Usage
with SSHConnection("prod-01", "ubuntu") as client:
    stdin, stdout, stderr = client.exec_command("df -h")
    print(stdout.read().decode())
# Connection automatically closed

# Using contextlib decorator
from contextlib import contextmanager

@contextmanager
def database_transaction():
    conn = sqlite3.connect("app.db")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# Usage
with database_transaction() as conn:
    conn.execute("INSERT ...")
```

### 23. What is the difference between shallow and deep copy?

**Answer:**
```python
import copy

# Shallow copy - copies only top level
original = {"servers": ["web-01", "web-02"], "port": 8080}
shallow = original.copy()

# Modifying nested objects affects both
shallow["servers"].append("web-03")
print(original)  # Also has "web-03"!

# Deep copy - copies everything recursively
deep = copy.deepcopy(original)
deep["servers"].append("web-04")
print(original)  # Unchanged

# Real DevOps example
def configure_server(base_config):
    config = copy.deepcopy(base_config)  # Don't modify original
    config["environment"] = "prod"
    return config
```

### 24. How would you handle configuration files (JSON/YAML)?

**Answer:**
```python
import json
import yaml
import os
from pathlib import Path

class ConfigManager:
    def __init__(self, config_path=None):
        self.config_path = config_path or os.getenv("CONFIG_PATH", "config.yaml")
        self.config = self.load_config()

    def load_config(self):
        if self.config_path.endswith(".json"):
            with open(self.config_path) as f:
                return json.load(f)
        elif self.config_path.endswith((".yaml", ".yml")):
            with open(self.config_path) as f:
                return yaml.safe_load(f)

    def get(self, key, default=None):
        return self.config.get(key, default)

    def set(self, key, value):
        self.config[key] = value

    def save(self):
        with open(self.config_path, "w") as f:
            if self.config_path.endswith(".json"):
                json.dump(self.config, f, indent=2)
            else:
                yaml.dump(self.config, f, default_flow_style=False)

# Usage
config = ConfigManager()
debug = config.get("debug", False)
config.set("last_updated", datetime.now().isoformat())
config.save()
```

## More Questions

### 25. What is Python's GIL (Global Interpreter Lock)?

The GIL prevents true parallelism in multi-threaded Python code. Only one thread executes Python bytecode at a time. Use `multiprocessing` for CPU-bound tasks, `threading` for I/O-bound tasks.

### 26. How do you debug Python code?

Use `pdb` (Python debugger):
```python
import pdb
pdb.set_trace()  # or breakpoint() in Python 3.7+

# At breakpoint:
# n - next line
# s - step into function
# c - continue
# p variable - print variable
```

### 27. What are decorators commonly used in web frameworks like Flask?

```python
from flask import Flask

app = Flask(__name__)

@app.route("/health")  # Decorator
def health_check():
    return {"status": "ok"}

@app.before_request  # Decorator for middleware
def log_request():
    print(f"Request to {request.path}")
```

### 28. How do you handle different Python versions in code?

```python
import sys

if sys.version_info >= (3, 9):
    # Use match/case (Python 3.10+)
    pass
else:
    # Use if/elif

# Check specific version
if sys.version_info.major < 3 or sys.version_info.minor < 8:
    raise RuntimeError("Requires Python 3.8+")
```

### 29. What is virtual environment and why use it?

Virtual environments isolate project dependencies:

```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate  # Windows
pip install -r requirements.txt
```

### 30. How would you structure a large DevOps project?

```
devops_project/
├── requirements.txt         # Dependencies
├── README.md
├── config/
│   ├── dev.yaml
│   ├── prod.yaml
│   └── __init__.py
├── scripts/
│   ├── deploy.py
│   ├── backup.py
│   └── monitor.py
├── utils/
│   ├── __init__.py
│   ├── aws.py              # AWS utilities
│   ├── ssh.py              # SSH utilities
│   └── logging.py
├── tests/
│   ├── test_deploy.py
│   └── test_utils.py
└── main.py                 # Entry point
```

## Summary

These questions cover fundamental Python concepts essential for DevOps roles. Practice implementing the real-world examples to solidify your understanding.
