---
title: "Bash Fundamentals"
description: "Shell basics: variables, quoting, control flow, functions, arrays, and more"
sidebar_label: "Fundamentals"
sidebar_position: 1
---

# Bash Fundamentals

Master the core concepts of Bash scripting including variables, control structures, functions, and data types.

## Getting Started

### Hello World

```bash
#!/bin/bash
echo "Hello, World!"
```

Save as `hello.sh`, then:

```bash
chmod +x hello.sh
./hello.sh
```

### Variables

Bash uses dynamic typing. Assign values without declaring types:

```bash
name="Alice"
age=30
path="/home/user"
```

Retrieve variables with `$`:

```bash
echo $name
echo ${name}      # Explicit braces (safer)
echo "${name}"    # With quotes (prevents word splitting)
```

**Variable Naming Rules:**
- Start with letter or underscore
- Contain letters, numbers, underscores
- Case-sensitive (`Name` ≠ `name`)

### Quoting

Quoting prevents word splitting, globbing, and variable expansion in different ways:

**Double Quotes** — Allow variable and command expansion:

```bash
greeting="Hello, $name"
echo "$greeting"  # Expands $name
```

**Single Quotes** — Literal strings (no expansion):

```bash
path='$HOME/docs'
echo "$path"      # Outputs: $HOME/docs
```

**Escape Character** — Backslash escapes special characters:

```bash
echo "He said \"Hello\""   # Outputs: He said "Hello"
echo "Price: \$100"        # Outputs: Price: $100
```

**No Quotes** — Subject to word splitting and globbing:

```bash
files=*.txt       # Expands to all .txt files
```

### Command Substitution

Capture command output into variables:

```bash
current_user=$(whoami)
host=$(hostname)
current_date=`date +%Y-%m-%d`   # Backticks (older syntax)
```

## Data Types & Operations

### Arrays

**Indexed Arrays:**

```bash
colors=("red" "green" "blue")
colors[3]="yellow"
echo ${colors[0]}          # red
echo ${colors[@]}          # All elements
echo ${#colors[@]}         # Array length
```

**Looping Through Arrays:**

```bash
for color in "${colors[@]}"; do
  echo "Color: $color"
done
```

**Unset Array Elements:**

```bash
unset colors[1]           # Remove green
unset colors              # Remove entire array
```

### Associative Arrays

Key-value pairs (Bash 4+):

```bash
declare -A person
person[name]="Alice"
person[age]="30"
person[city]="New York"

echo ${person[name]}       # Alice
echo ${!person[@]}         # Keys: name age city
echo ${person[@]}          # Values: Alice 30 New York
```

### String Manipulation

**Length:**

```bash
str="hello"
echo ${#str}               # 5
```

**Substring:**

```bash
str="Hello, World!"
echo ${str:0:5}            # Hello (offset 0, length 5)
echo ${str:7}              # World! (from position 7)
```

**Parameter Expansion (Remove/Replace):**

```bash
filename="document.txt"
echo ${filename%.txt}      # document (remove suffix)
echo ${filename#*o}        # document.txt (remove prefix up to 'o')

text="foo bar foo"
echo ${text/foo/FOO}       # FOO bar foo (replace first)
echo ${text//foo/FOO}      # FOO bar FOO (replace all)
```

### Arithmetic

**Using `$(( ))`:**

```bash
result=$((5 + 3))
echo $result               # 8

x=10
y=3
echo $((x * y))            # 30
echo $((x / y))            # 3
echo $((x % y))            # 1
echo $((x ** 2))           # 100 (exponentiation)
```

**Increment/Decrement:**

```bash
count=0
((count++))
((count+=5))
echo $count                # 6
```

**Using `let`:**

```bash
let result=5+3
let x+=10
```

## Control Structures

### If/Else

```bash
if [ "$age" -gt 18 ]; then
  echo "Adult"
elif [ "$age" -gt 12 ]; then
  echo "Teenager"
else
  echo "Child"
fi
```

**Test Operators:**

| Operator | Meaning |
|:---------|:--------|
| `-eq` | Equal |
| `-ne` | Not equal |
| `-lt` | Less than |
| `-le` | Less than or equal |
| `-gt` | Greater than |
| `-ge` | Greater than or equal |
| `-z` | String is empty |
| `-n` | String is not empty |
| `=` | Strings equal |
| `!=` | Strings not equal |
| `-f` | File exists and is regular file |
| `-d` | Directory exists |
| `-e` | File/dir exists |
| `-r` | File is readable |
| `-w` | File is writable |
| `-x` | File is executable |

**Logical Operators:**

```bash
if [ "$age" -gt 18 ] && [ "$age" -lt 65 ]; then
  echo "Working age"
fi

if [ "$role" = "admin" ] || [ "$role" = "root" ]; then
  echo "Has privileges"
fi

if [ ! -f "$file" ]; then
  echo "File does not exist"
fi
```

### For Loop

**Iterate over list:**

```bash
for item in apple banana cherry; do
  echo "Fruit: $item"
done
```

**Iterate over range:**

```bash
for i in {1..5}; do
  echo "Number: $i"
done
```

**C-style for loop:**

```bash
for ((i=0; i<5; i++)); do
  echo "Count: $i"
done
```

**Iterate over array:**

```bash
files=("file1.txt" "file2.txt" "file3.txt")
for file in "${files[@]}"; do
  echo "Processing: $file"
done
```

### While Loop

```bash
counter=0
while [ $counter -lt 5 ]; do
  echo "Counter: $counter"
  ((counter++))
done
```

**Read input line-by-line:**

```bash
while IFS= read -r line; do
  echo "Line: $line"
done < file.txt
```

### Case Statement

```bash
case "$1" in
  start)
    echo "Starting service..."
    ;;
  stop)
    echo "Stopping service..."
    ;;
  restart)
    echo "Restarting service..."
    ;;
  *)
    echo "Usage: $0 {start|stop|restart}"
    exit 1
    ;;
esac
```

## Functions

### Basic Function

```bash
greet() {
  echo "Hello, $1!"
}

greet "Alice"              # Hello, Alice!
```

### Function with Local Variables

```bash
calculate() {
  local x=$1
  local y=$2
  local result=$((x + y))
  echo $result
}

sum=$(calculate 10 20)
echo "Sum: $sum"           # Sum: 30
```

### Function Return Values

```bash
is_positive() {
  if [ $1 -gt 0 ]; then
    return 0              # Success
  else
    return 1              # Failure
  fi
}

if is_positive 5; then
  echo "5 is positive"
fi
```

**Capture output:**

```bash
get_user_info() {
  echo "Alice:30:NYC"
}

info=$(get_user_info)
echo "$info"              # Alice:30:NYC
```

## Exit Codes & Error Handling

### Exit Codes

Every command returns an exit code (0 = success, non-zero = failure):

```bash
ls /nonexistent 2>/dev/null
echo $?                   # Non-zero (command failed)
```

**Check exit code immediately:**

```bash
if command; then
  echo "Success"
else
  echo "Failed"
fi
```

### Error Prevention

**Set error flags:**

```bash
#!/bin/bash
set -e    # Exit on any error
set -u    # Exit if undefined variable used
set -o pipefail  # Return error if pipe command fails
```

**Handle errors:**

```bash
command || {
  echo "Error occurred"
  exit 1
}
```

## Built-in Commands

| Command | Purpose |
|:--------|:--------|
| `echo` | Print text |
| `printf` | Formatted output |
| `read` | Read user input |
| `test` / `[` | Evaluate condition |
| `pwd` | Print working directory |
| `cd` | Change directory |
| `export` | Set environment variable |
| `source` / `.` | Execute commands from file |
| `type` | Show command type |
| `command` | Execute command |

## Exercises

### Exercise 1: Temperature Converter

Create a script that converts Celsius to Fahrenheit:

```bash
#!/bin/bash

if [ $# -ne 1 ]; then
  echo "Usage: $0 <celsius>"
  exit 1
fi

celsius=$1
fahrenheit=$(echo "scale=2; $celsius * 9/5 + 32" | bc)
echo "$celsius°C = $fahrenheit°F"
```

### Exercise 2: File Info Script

Create a script that displays file information:

```bash
#!/bin/bash

if [ ! -f "$1" ]; then
  echo "File not found: $1"
  exit 1
fi

echo "File: $1"
echo "Size: $(wc -c < "$1") bytes"
echo "Lines: $(wc -l < "$1")"
echo "Modified: $(stat -c %y "$1")"
```

### Exercise 3: Array Processing

Create a script that processes an array:

```bash
#!/bin/bash

numbers=(5 12 3 8 15 2)
sum=0
count=0

for num in "${numbers[@]}"; do
  sum=$((sum + num))
  ((count++))
done

average=$((sum / count))
echo "Sum: $sum"
echo "Count: $count"
echo "Average: $average"
```

### Exercise 4: Simple Menu

Create an interactive menu system:

```bash
#!/bin/bash

while true; do
  echo "=== Menu ==="
  echo "1) List files"
  echo "2) Show current directory"
  echo "3) Exit"
  read -p "Choose option: " choice

  case $choice in
    1) ls -l ;;
    2) pwd ;;
    3) exit 0 ;;
    *) echo "Invalid choice" ;;
  esac
done
```

## Key Takeaways

- Variables are dynamically typed; use `$` or `${}` to expand
- Quotes control expansion: `""` (expand) vs `''` (literal)
- Arrays are 0-indexed; associative arrays use keys
- Control structures (if/for/while/case) power script logic
- Functions promote reusability and code organization
- Exit codes (0 = success) drive error handling
- Use `set -e` and `set -u` to catch common errors early

## Next Steps

Explore [Advanced Bash](/docs/tools/bash/advanced) topics like process substitution, here documents, signal handling, and text processing with sed/awk.
