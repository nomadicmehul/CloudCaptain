---
title: "DevOps Scripting with Python"
description: "Automation, system administration, cloud integration, log parsing, and monitoring scripts"
sidebar_label: "DevOps Scripting"
sidebar_position: 2
---

# DevOps Scripting with Python

Master Python for real-world DevOps automation, system administration, and cloud integration.

## Running External Commands

### subprocess Module

Execute system commands and capture output:

```python
import subprocess

# Run command and wait for completion
result = subprocess.run(["ls", "-la"], capture_output=True, text=True)
print(result.stdout)
print(result.returncode)  # 0 = success

# Run with shell=True (for shell features)
result = subprocess.run("echo $HOME", shell=True, text=True, capture_output=True)
print(result.stdout)

# Pass environment variables
env = os.environ.copy()
env["APP_ENV"] = "production"
result = subprocess.run(["app", "start"], env=env)

# Real-world: Check service status
def check_service_status(service_name):
    result = subprocess.run(
        ["systemctl", "is-active", service_name],
        capture_output=True,
        text=True
    )
    return result.returncode == 0
```

**Real-world example: Deploy script**
```python
def deploy_application(version, environment):
    """Deploy application to environment."""
    commands = [
        ["git", "pull", "origin", "main"],
        ["git", "checkout", f"v{version}"],
        ["pip", "install", "-r", "requirements.txt"],
        ["python", "manage.py", "migrate"],
        ["systemctl", "restart", "app-service"]
    ]

    for cmd in commands:
        result = subprocess.run(cmd)
        if result.returncode != 0:
            raise RuntimeError(f"Deployment failed at: {' '.join(cmd)}")

    print(f"Successfully deployed v{version} to {environment}")
```

## Operating System Operations

### os Module

**File and directory operations:**
```python
import os

# Current working directory
print(os.getcwd())
os.chdir("/opt/app")

# List files
files = os.listdir(".")
for f in files:
    print(f)

# Create directory
os.makedirs("/var/log/app", exist_ok=True)

# Check path exists
if os.path.exists("/etc/config.yaml"):
    print("Config found")

# Get file size
size = os.path.getsize("app.log")
print(f"Log size: {size} bytes")

# Environment variables
debug_mode = os.getenv("DEBUG", "false")
api_key = os.getenv("API_KEY")

# Set environment variable
os.environ["APP_DEBUG"] = "true"
```

**Real-world: Cleanup script**
```python
import os
import time

def cleanup_old_logs(log_dir, days=7):
    """Remove log files older than N days."""
    cutoff_time = time.time() - (days * 86400)

    for filename in os.listdir(log_dir):
        filepath = os.path.join(log_dir, filename)
        if os.path.isfile(filepath):
            if os.path.getmtime(filepath) < cutoff_time:
                os.remove(filepath)
                print(f"Deleted: {filepath}")

cleanup_old_logs("/var/log/app")
```

### shutil Module

**File operations:**
```python
import shutil

# Copy file
shutil.copy("source.txt", "destination.txt")
shutil.copy2("source.txt", "dest.txt")  # Preserves metadata

# Copy directory tree
shutil.copytree("source_dir", "dest_dir")

# Move/rename
shutil.move("old_name.txt", "new_name.txt")

# Remove directory tree
shutil.rmtree("temp_dir")

# Disk usage
total, used, free = shutil.disk_usage("/")
print(f"Free space: {free / (1024**3):.2f} GB")
```

**Real-world: Backup script**
```python
def backup_directory(source, backup_dir):
    """Create timestamped backup of directory."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{backup_dir}/backup_{timestamp}"

    try:
        shutil.copytree(source, backup_path)
        print(f"Backup created: {backup_path}")
        return backup_path
    except Exception as e:
        print(f"Backup failed: {e}")
        return None
```

## SSH and Remote Operations

### Paramiko Library

Execute commands on remote servers via SSH:

```python
import paramiko

def execute_remote_command(hostname, username, command, key_file=None):
    """Execute command on remote server."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        # Connect with key or password
        if key_file:
            client.connect(hostname, username=username, key_filename=key_file)
        else:
            client.connect(hostname, username=username, password="password")

        # Execute command
        stdin, stdout, stderr = client.exec_command(command)

        # Get output
        output = stdout.read().decode()
        error = stderr.read().decode()

        return output, error
    finally:
        client.close()

# Usage
output, error = execute_remote_command(
    "prod-01.example.com",
    "ubuntu",
    "df -h",
    key_file="/home/user/.ssh/id_rsa"
)
print(output)
```

**Real-world: Multi-server health check**
```python
def health_check_servers(servers):
    """Check health across multiple servers."""
    results = {}

    for server in servers:
        hostname, username = server["host"], server["user"]
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            client.connect(hostname, username=username)
            stdin, stdout, stderr = client.exec_command("ps aux | wc -l")
            proc_count = int(stdout.read().decode().strip())

            results[hostname] = {
                "status": "healthy" if proc_count > 5 else "warning",
                "processes": proc_count
            }
        except Exception as e:
            results[hostname] = {"status": "down", "error": str(e)}
        finally:
            client.close()

    return results
```

## AWS Integration with Boto3

**Interact with AWS services:**

```python
import boto3

# EC2 operations
ec2 = boto3.client("ec2", region_name="us-east-1")

# List all instances
response = ec2.describe_instances()
for reservation in response["Reservations"]:
    for instance in reservation["Instances"]:
        print(f"Instance: {instance['InstanceId']} - {instance['State']['Name']}")

# Start instance
ec2.start_instances(InstanceIds=["i-1234567890abcdef0"])

# Create security group rule
ec2.authorize_security_group_ingress(
    GroupId="sg-12345678",
    IpProtocol="tcp",
    FromPort=443,
    ToPort=443,
    CidrIp="0.0.0.0/0"
)
```

**S3 operations:**
```python
s3 = boto3.client("s3")

# List buckets
response = s3.list_buckets()
for bucket in response["Buckets"]:
    print(bucket["Name"])

# Upload file
s3.upload_file("local_file.txt", "my-bucket", "s3_key.txt")

# Download file
s3.download_file("my-bucket", "s3_key.txt", "local_file.txt")

# List objects in bucket
response = s3.list_objects_v2(Bucket="my-bucket", Prefix="logs/")
for obj in response.get("Contents", []):
    print(obj["Key"])
```

**Real-world: Automated backups to S3**
```python
def backup_to_s3(local_dir, bucket_name, s3_prefix):
    """Backup local directory to S3."""
    s3 = boto3.client("s3")

    for filename in os.listdir(local_dir):
        filepath = os.path.join(local_dir, filename)
        if os.path.isfile(filepath):
            s3_key = f"{s3_prefix}/{datetime.now().strftime('%Y%m%d')}/{filename}"

            try:
                s3.upload_file(filepath, bucket_name, s3_key)
                print(f"Uploaded: {filename}")
            except Exception as e:
                print(f"Failed to upload {filename}: {e}")
```

## HTTP Requests

### requests Library

**Make HTTP requests:**

```python
import requests

# GET request
response = requests.get("https://api.example.com/servers")
if response.status_code == 200:
    data = response.json()
else:
    print(f"Error: {response.status_code}")

# POST request
payload = {
    "name": "new-server",
    "region": "us-east-1",
    "type": "t3.medium"
}
response = requests.post(
    "https://api.example.com/servers",
    json=payload,
    headers={"Authorization": "Bearer TOKEN"}
)

# Query parameters
response = requests.get(
    "https://api.example.com/logs",
    params={"service": "api", "level": "error", "limit": 100}
)

# Handle timeouts
try:
    response = requests.get(url, timeout=5)
except requests.Timeout:
    print("Request timed out")
```

**Real-world: Health check with retries**
```python
def health_check_api(url, max_retries=3, timeout=5):
    """Check API health with exponential backoff."""
    for attempt in range(max_retries):
        try:
            response = requests.get(
                f"{url}/health",
                timeout=timeout
            )
            if response.status_code == 200:
                return True
        except requests.RequestException as e:
            print(f"Attempt {attempt + 1} failed: {e}")
            time.sleep(2 ** attempt)  # Exponential backoff

    return False
```

## Data Parsing

### JSON Operations

```python
import json

# Parse JSON string
json_str = '{"host": "prod-01", "status": "running"}'
data = json.loads(json_str)
print(data["host"])

# Convert to JSON string
config = {"app": "myapp", "port": 8080, "debug": False}
json_output = json.dumps(config, indent=2)
print(json_output)

# Read JSON file
with open("config.json", "r") as f:
    config = json.load(f)

# Write JSON file
with open("output.json", "w") as f:
    json.dump(config, f, indent=2)
```

### YAML Operations

```python
import yaml

# Parse YAML string
yaml_str = """
servers:
  - name: web-01
    ip: 10.0.0.1
    port: 8080
  - name: db-01
    ip: 10.0.0.2
    port: 5432
"""
data = yaml.safe_load(yaml_str)
for server in data["servers"]:
    print(f"{server['name']}: {server['ip']}")

# Read YAML file
with open("inventory.yaml", "r") as f:
    inventory = yaml.safe_load(f)

# Write YAML file
config = {"environment": "prod", "replicas": 3}
with open("deploy.yaml", "w") as f:
    yaml.dump(config, f, default_flow_style=False)
```

## Log Parsing and Analysis

**Parse and analyze logs:**

```python
import re
from collections import defaultdict

def parse_nginx_log(log_file):
    """Parse nginx access log."""
    # Typical nginx log format
    pattern = r'(\S+) - (\S+) \[(.*?)\] "(\S+) (\S+) (\S+)" (\d+) (\d+)'

    stats = defaultdict(int)

    with open(log_file, "r") as f:
        for line in f:
            match = re.match(pattern, line)
            if match:
                ip, user, timestamp, method, path, protocol, status, size = match.groups()
                stats[f"status_{status}"] += 1
                stats[f"method_{method}"] += 1

    return stats

# Real-world: Find and report errors
def analyze_errors(log_file, pattern=r"ERROR"):
    """Find error lines in log file."""
    errors = []

    with open(log_file, "r") as f:
        for line_num, line in enumerate(f, 1):
            if re.search(pattern, line):
                errors.append({
                    "line": line_num,
                    "message": line.strip()
                })

    return errors

errors = analyze_errors("/var/log/app/error.log")
print(f"Found {len(errors)} errors")
```

## System Monitoring

**Monitor system resources:**

```python
import psutil

# CPU usage
cpu_percent = psutil.cpu_percent(interval=1)
print(f"CPU: {cpu_percent}%")

# Memory usage
memory = psutil.virtual_memory()
print(f"Memory: {memory.percent}% used")
print(f"Available: {memory.available / (1024**3):.2f} GB")

# Disk usage
disk = psutil.disk_usage("/")
print(f"Disk: {disk.percent}% used")

# Process info
for proc in psutil.process_iter(["pid", "name", "memory_percent"]):
    try:
        if proc.info["memory_percent"] > 10:
            print(f"{proc.info['name']}: {proc.info['memory_percent']}%")
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
```

**Real-world: Monitoring script**
```python
def monitor_system(alert_thresholds):
    """Check system metrics against thresholds."""
    alerts = {}

    if psutil.cpu_percent(interval=1) > alert_thresholds.get("cpu", 80):
        alerts["cpu"] = "High CPU usage"

    memory = psutil.virtual_memory()
    if memory.percent > alert_thresholds.get("memory", 85):
        alerts["memory"] = f"Memory: {memory.percent}%"

    disk = psutil.disk_usage("/")
    if disk.percent > alert_thresholds.get("disk", 90):
        alerts["disk"] = f"Disk: {disk.percent}%"

    return alerts

alerts = monitor_system({"cpu": 75, "memory": 80, "disk": 85})
for key, message in alerts.items():
    print(f"ALERT: {message}")
```

## Logging Best Practices

**Configure logging for production scripts:**

```python
import logging
import logging.handlers

# Create logger
logger = logging.getLogger("deployment")

# File handler with rotation
handler = logging.handlers.RotatingFileHandler(
    "/var/log/app/deploy.log",
    maxBytes=10485760,  # 10MB
    backupCount=5
)

# Format logs
formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
handler.setFormatter(formatter)

# Add handler
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# Use logging
logger.info("Deployment started")
logger.warning("No backup found")
logger.error("Deployment failed")
```

## Exercises

1. **System Health Dashboard**: Create a script that displays CPU, memory, and disk usage with alerts if thresholds are exceeded.

2. **Log Analyzer**: Parse application logs, count errors by type, and generate a report.

3. **Multi-Server Deployment**: Write a script using Paramiko to deploy a new application version across 10 servers with rollback on failure.

4. **AWS Snapshot Manager**: Create an automated backup script that creates daily snapshots of EC2 volumes and cleans up snapshots older than 30 days.

5. **Configuration Validator**: Write a script that validates JSON/YAML configuration files against a schema.

6. **API Monitoring**: Create a monitoring script that checks multiple API endpoints and logs response times, retrying with exponential backoff on failures.

7. **Log Aggregator**: Build a script that collects logs from multiple remote servers via SSH and stores them in a central location with parsing and analysis.

## Summary

- **subprocess**: Execute system commands
- **os/shutil**: File and directory operations
- **paramiko**: SSH connections and remote command execution
- **boto3**: AWS service integration
- **requests**: HTTP API interactions
- **json/yaml**: Data parsing
- **logging**: Structured logging for production code
- **psutil**: System monitoring and metrics
- **regex**: Log parsing and pattern matching
