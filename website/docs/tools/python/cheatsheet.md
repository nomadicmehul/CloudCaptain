---
title: "Python Cheatsheet"
description: "Quick reference for Python syntax, data structures, and DevOps utilities"
sidebar_label: "Cheatsheet"
sidebar_position: 3
---

# Python Cheatsheet

Quick reference guide for Python developers and DevOps engineers.

## Data Types

```python
# Numbers
x = 42              # int
x = 3.14            # float
x = 2 + 3j          # complex

# Strings
s = "hello"
s = 'world'
s = """multi
line"""

# Collections
lst = [1, 2, 3]
tpl = (1, 2, 3)
dct = {"key": "value"}
st = {1, 2, 3}

# Boolean
flag = True
flag = False

# None
empty = None
```

## String Operations

```python
s = "DevOps"

# Length
len(s)

# Indexing
s[0]            # 'D'
s[-1]           # 's'

# Slicing
s[0:3]          # 'Dev'
s[2:]           # 'Ops'
s[:-1]          # 'DevOp'
s[::2]          # 'DvO'

# Methods
s.lower()       # "devops"
s.upper()       # "DEVOPS"
s.replace("Op", "Stuff")
s.split(",")    # Split by delimiter
",".join(["a", "b"])
s.strip()       # Remove whitespace
s.startswith("Dev")
s.endswith("Ops")
s.find("Op")    # Index of substring

# Formatting
f"Hello {name}"              # f-string
"Hello {}".format(name)      # .format()
"Hello %s" % name            # % formatting

# String checks
"hello".isdigit()
"12345".isalpha()
"Hello World".isupper()
```

## List Operations

```python
lst = [1, 2, 3, 4, 5]

# Indexing
lst[0]          # 1
lst[-1]         # 5

# Slicing
lst[1:3]        # [2, 3]
lst[:2]         # [1, 2]
lst[2:]         # [3, 4, 5]

# Modification
lst.append(6)
lst.extend([7, 8])
lst.insert(0, 0)
lst.remove(2)
lst.pop()       # Remove last
lst.pop(0)      # Remove at index
lst.clear()
lst.sort()
lst.reverse()

# Information
len(lst)
lst.count(2)
lst.index(3)
2 in lst

# Comprehension
[x*2 for x in lst]
[x for x in lst if x > 2]
{x: x**2 for x in lst}
```

## Dictionary Operations

```python
dct = {"host": "localhost", "port": 8080}

# Access
dct["host"]
dct.get("port", 3000)
dct.get("missing", "default")

# Modification
dct["new_key"] = "value"
dct.update({"host": "0.0.0.0"})
dct.pop("port")
dct.clear()

# Iteration
for key in dct:
    print(key)

for key, value in dct.items():
    print(f"{key}: {value}")

for value in dct.values():
    print(value)

for key in dct.keys():
    print(key)

# Information
len(dct)
"host" in dct
dct.keys()
dct.values()
```

## Control Flow

```python
# If-elif-else
if condition:
    pass
elif other_condition:
    pass
else:
    pass

# Ternary
result = "yes" if condition else "no"

# For loop
for i in range(10):
    print(i)

for item in list_var:
    print(item)

for index, item in enumerate(list_var):
    print(index, item)

for key, value in dict_var.items():
    print(key, value)

# While loop
while condition:
    # code
    pass

# Loop control
continue  # Skip iteration
break     # Exit loop

# Try-except
try:
    code()
except ValueError:
    handle_error()
except (TypeError, KeyError):
    handle_multiple()
except Exception as e:
    handle_any(e)
else:
    run_if_no_error()
finally:
    cleanup()
```

## Function Definitions

```python
# Basic function
def func(x, y):
    return x + y

# Default parameters
def func(x, y=10):
    return x + y

# Variable arguments
def func(*args):
    for arg in args:
        print(arg)

# Keyword arguments
def func(**kwargs):
    for key, value in kwargs.items():
        print(key, value)

# Mixed
def func(a, b=2, *args, **kwargs):
    pass

# Lambda (anonymous function)
square = lambda x: x**2
square(5)

# Map, filter
list(map(lambda x: x*2, [1, 2, 3]))
list(filter(lambda x: x > 2, [1, 2, 3, 4]))
```

## File Operations

```python
# Read
with open("file.txt", "r") as f:
    content = f.read()          # Entire file
    lines = f.readlines()       # List of lines
    for line in f:              # Line by line
        print(line)

# Write
with open("file.txt", "w") as f:
    f.write("content")
    f.writelines(["line1\n", "line2\n"])

# Append
with open("file.txt", "a") as f:
    f.write("appended")

# Check existence
import os
os.path.exists("file.txt")
os.path.isfile("file.txt")
os.path.isdir("directory")
os.path.getsize("file.txt")

# Paths
from pathlib import Path
p = Path("config.yaml")
p.exists()
p.read_text()
p.write_text("content")
p.parent
p.name
```

## List/Dict/Set Comprehensions

```python
# List comprehension
[x*2 for x in range(5)]
[x for x in range(10) if x % 2 == 0]
[x*y for x in range(3) for y in range(3)]

# Dict comprehension
{x: x**2 for x in range(5)}
{k: v for k, v in dict_var.items() if v > 0}

# Set comprehension
{x % 3 for x in range(10)}

# Generator expression (memory efficient)
(x*2 for x in range(1000000))
sum(x for x in range(100))
```

## Common Modules

```python
# String
import string
string.ascii_lowercase
string.digits

# Math
import math
math.sqrt(16)
math.ceil(4.3)
math.floor(4.9)

# Random
import random
random.choice([1, 2, 3])
random.randint(1, 10)
random.shuffle(lst)

# DateTime
from datetime import datetime, timedelta
now = datetime.now()
tomorrow = now + timedelta(days=1)
now.strftime("%Y-%m-%d")
datetime.strptime("2024-01-15", "%Y-%m-%d")

# JSON
import json
data = json.loads(json_str)
json.dumps(data, indent=2)

# Regular Expressions
import re
re.search(r"pattern", "text")
re.findall(r"\d+", "abc123def456")
re.sub(r"\s+", " ", text)

# OS/System
import os
os.getcwd()
os.listdir(".")
os.makedirs("path/to/dir")
os.getenv("HOME")

# Subprocess
import subprocess
result = subprocess.run(["ls", "-la"], capture_output=True, text=True)
result.stdout
result.returncode

# Logging
import logging
logging.basicConfig(level=logging.INFO)
logging.info("Message")
logging.error("Error")
```

## Classes and Objects

```python
# Class definition
class MyClass:
    class_var = "shared"

    def __init__(self, name):
        self.name = name

    def method(self):
        return f"Hello {self.name}"

    @property
    def prop(self):
        return self.name.upper()

    @staticmethod
    def static_method():
        return "static"

    @classmethod
    def class_method(cls):
        return cls.class_var

# Create instance
obj = MyClass("Alice")
obj.method()
obj.prop
MyClass.static_method()

# Inheritance
class Parent:
    def method(self):
        return "parent"

class Child(Parent):
    def method(self):
        return super().method() + " child"

# Dunder methods
class Example:
    def __str__(self):
        return "string representation"

    def __repr__(self):
        return "Example()"

    def __len__(self):
        return 42

    def __getitem__(self, index):
        return index * 2

    def __eq__(self, other):
        return self.value == other.value
```

## Useful One-Liners

```python
# Find Python version
import sys; print(sys.version)

# Start HTTP server
python -m http.server 8000

# Pretty print JSON
python -m json.tool file.json

# Check module location
python -c "import requests; print(requests.__file__)"

# Install package from requirements.txt
pip install -r requirements.txt

# List all imports in file
python -c "import ast; print([n.names[0].name for n in ast.parse(open('file.py').read()).body if isinstance(n, ast.Import)])"

# Generate random string
python -c "import secrets; print(secrets.token_hex(16))"

# One-liner web server for debugging
python -m http.server 8000 --directory /path/to/serve

# Check if port is in use
python -c "import socket; s = socket.socket(); s.bind(('', 8080))"

# Base64 encode/decode
python -c "import base64; print(base64.b64encode(b'text').decode())"

# Pretty print dict
python -c "import pprint; pprint.pprint(my_dict)"

# Sum values in JSON
python -c "import json; d = json.load(open('file.json')); print(sum(d.values()))"
```

## DevOps Common Patterns

### Execute command with error handling
```python
import subprocess

try:
    result = subprocess.run(
        ["command"],
        capture_output=True,
        text=True,
        timeout=30
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    output = result.stdout
except subprocess.TimeoutExpired:
    print("Command timed out")
```

### Retry with exponential backoff
```python
import time

def retry(func, max_attempts=3):
    for attempt in range(max_attempts):
        try:
            return func()
        except Exception as e:
            if attempt == max_attempts - 1:
                raise
            wait = 2 ** attempt
            time.sleep(wait)
```

### Parse environment variables
```python
import os

config = {
    "debug": os.getenv("DEBUG", "false").lower() == "true",
    "port": int(os.getenv("PORT", 8080)),
    "host": os.getenv("HOST", "0.0.0.0"),
    "required": os.getenv("REQUIRED_VAR")
}

if not config["required"]:
    raise ValueError("REQUIRED_VAR not set")
```

### Read configuration files
```python
import json
import yaml

# JSON config
with open("config.json") as f:
    config = json.load(f)

# YAML config
with open("config.yaml") as f:
    config = yaml.safe_load(f)

# Environment-based config
import os
env = os.getenv("APP_ENV", "dev")
config_file = f"config.{env}.yaml"
```

### Structured logging
```python
import logging

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)
logger.info("Application started")
logger.error("An error occurred", exc_info=True)
```

## Tips and Tricks

- Use `with` for file operations (auto-closes files)
- Use f-strings for string formatting (fast, readable)
- Use `pathlib.Path` instead of `os.path` (modern, object-oriented)
- Use `type hints` for better code documentation
- Use `docstrings` to document functions and classes
- Use `requirements.txt` to manage dependencies
- Use `virtual environments` to isolate projects
- Use `logging` instead of `print()` for production
- Use `exceptions` for error handling, not return codes
- Use comprehensions for concise list/dict creation
