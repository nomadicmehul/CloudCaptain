---
title: "Bash Interview Questions"
description: "35+ essential Bash and shell scripting interview questions with detailed answers"
sidebar_label: "Interview Questions"
sidebar_position: 4
---

# Bash Interview Questions

Comprehensive collection of 35+ frequently asked Bash scripting interview questions with detailed answers. Perfect for preparation and skill assessment.

## Fundamentals

### Q1: What is the difference between `sh` and `bash`?

**Answer:**

`sh` (Bourne Shell) is the POSIX standard shell with minimal features. `bash` (Bourne Again Shell) is an extended version with additional features.

Key differences:

| Feature | sh | bash |
|:--------|:---|:-----|
| Arrays | No | Yes |
| Associative arrays | No | Yes |
| Functions | Basic | Full support |
| Arithmetic | Limited | Full `$(( ))` |
| Job control | No | Yes |
| History | No | Yes |
| Aliases | No | Yes |

**Example:**

```bash
# This works in bash but not sh
declare -A person=(["name"]="Alice" ["age"]="30")
```

---

### Q2: What is the shebang and why is it important?

**Answer:**

The shebang (`#!`) is the first line of a script telling the system which interpreter to use:

```bash
#!/bin/bash              # Use bash
#!/bin/sh                # Use sh (POSIX)
#!/usr/bin/env python3   # Use python3
```

**Importance:**
- Specifies interpreter explicitly
- Makes script portable (uses `env` to find interpreter)
- Allows `./script.sh` execution without calling bash directly
- Enables proper script permissions handling

**Best practice:**

```bash
#!/usr/bin/env bash      # More portable than #!/bin/bash
```

---

### Q3: How do you declare and use variables in Bash?

**Answer:**

Bash variables are dynamically typed and don't require declaration:

```bash
# Simple assignment
name="Alice"
age=30
path="/home/user"

# Access with $ or ${}
echo $name               # Alice
echo ${name}             # Alice (explicit braces)

# Only ${} handles word boundaries correctly
echo ${name}_project     # Alice_project
echo $name_project       # Searches for $name_project (undefined)
```

**Variable scope:**

```bash
global_var="global"

function test_scope() {
  local local_var="local"  # Only visible in function
  global_var="modified"    # Modifies global
}

test_scope
echo $global_var          # modified
echo $local_var           # empty
```

---

### Q4: What are the different types of quotes and their differences?

**Answer:**

**Double quotes** (`"..."`) — Allows expansion:

```bash
greeting="Hello, $USER"
echo "$greeting"         # Expands $USER
result=$(date)
echo "$result"           # Expands command substitution
```

**Single quotes** (`'...'`) — No expansion:

```bash
path='$HOME/documents'
echo "$path"             # Outputs: $HOME/documents
command='echo "hello"'
echo "$command"          # Outputs: echo "hello"
```

**No quotes** — Subject to word splitting and globbing:

```bash
files=*.txt              # Expands *.txt to filenames
```

**Difference summary:**

| Type | Expansion | Word Splitting | Use Case |
|:-----|:----------|:---------------|:---------|
| `" "` | Yes | No | Variables, command substitution |
| `' '` | No | No | Literal strings, regex patterns |
| None | Yes | Yes | File globbing, word splitting |

---

## Control Structures

### Q5: Explain the difference between `[ ]` and `[[ ]]` conditional expressions.

**Answer:**

`[ ]` (test command) is POSIX-compliant but limited. `[[ ]]` (compound command) is bash-specific and more powerful.

**Key differences:**

```bash
# Word splitting with [ ]
var="hello world"
[ $var = "hello" ]       # Error: too many arguments
[ "$var" = "hello" ]     # OK (requires quotes)

# No word splitting with [[ ]]
[[ $var = hello* ]]      # OK (pattern matching)
[[ $var == "hello" ]]    # OK (without quotes)

# Regex matching (only in [[ ]])
[[ $var =~ ^h[a-z]+ ]]   # OK
[ $var =~ pattern ]      # Error

# Logical operators
[[ $x -gt 5 && $y -lt 10 ]]  # OK (bash syntax)
[ $x -gt 5 -a $y -lt 10 ]     # OK (POSIX syntax)
```

**When to use:**
- `[ ]` — For POSIX compatibility across shells
- `[[ ]]` — For bash-specific scripts (safer, more readable)

---

### Q6: What is parameter expansion and how is it used?

**Answer:**

Parameter expansion modifies variable values during expansion:

```bash
# Default values
echo ${name:-"default"}    # Use default if empty
echo ${name:="default"}    # Set AND use default if empty

# Remove patterns
filename="document.pdf"
echo ${filename%.pdf}      # Remove suffix: document
echo ${filename#*o}        # Remove prefix up to 'o': cument.pdf

# Substring
str="Hello, World!"
echo ${str:0:5}            # First 5 chars: Hello
echo ${str:7}              # From position 7: World!

# Case conversion (bash 4+)
echo ${str,,}              # Lowercase
echo ${str^^}              # UPPERCASE

# Replace
text="foo bar foo"
echo ${text/foo/FOO}       # First: FOO bar foo
echo ${text//foo/FOO}      # All: FOO bar FOO

# Length
echo ${#str}               # 13 (character count)

# Error on unset
echo ${required?Error message}  # Fails if required unset
```

---

### Q7: What's the difference between return and exit codes?

**Answer:**

`return` exits a function, `exit` exits the script. Both set exit code (0 = success, non-zero = failure).

```bash
check_positive() {
  if [ $1 -gt 0 ]; then
    return 0               # Success in function
  else
    return 1               # Failure in function
  fi
}

if check_positive 5; then
  echo "Positive"
fi

# Exit code stored in $?
check_positive 5
echo $?                    # 0

# Exit from script
if [ ! -f "$required_file" ]; then
  echo "Error: file not found"
  exit 1                   # Exit script with code 1
fi

# Exit code in main context
echo "Done"
exit 0                     # Explicit success
```

**Common exit codes:**

| Code | Meaning |
|:-----|:--------|
| 0 | Success |
| 1 | General error |
| 2 | Misuse of shell builtin |
| 126 | Command not executable |
| 127 | Command not found |
| 128+N | Signal N (e.g., 128+9=137 for SIGKILL) |
| 130 | Script terminated by Ctrl+C (SIGINT) |

---

## Advanced Concepts

### Q8: What is command substitution and how does it work?

**Answer:**

Command substitution captures command output into a variable using `$()` or backticks:

```bash
# Modern syntax (preferred)
current_user=$(whoami)
today=$(date +%Y-%m-%d)

# Backtick syntax (legacy, avoid)
user=`whoami`

# Nesting capability
files=$(ls -l $(find . -name "*.txt"))

# In compound commands
if [ $(whoami) = "root" ]; then
  echo "Running as root"
fi

# Loop over lines
for line in $(cat file.txt); do
  process "$line"
done

# Performance consideration: command substitution waits for completion
start=$(date +%s)
sleep 2
end=$(date +%s)
echo "Elapsed: $((end - start))"
```

**Note:** Use quotes to preserve whitespace:

```bash
output=$(ls -la)
echo "$output"             # Preserves formatting
echo $output               # Loses whitespace
```

---

### Q9: Explain process substitution and provide examples.

**Answer:**

Process substitution treats command output as a file using `<()` or `>()`:

```bash
# Compare sorted outputs
diff <(sort file1.txt) <(sort file2.txt)

# Paste output from two commands
paste <(seq 1 5) <(seq 10 14)

# Read multiple inputs into command
while IFS=: read user password uid rest; do
  echo "$user: $uid"
done < <(grep admin /etc/passwd)

# Output to multiple places
tee >(cat > file1.txt) >(cat > file2.txt) <<< "content"
```

**vs. Command substitution:**

```bash
# Command substitution (waits for completion)
result=$(long_running_command)

# Process substitution (runs in background)
while read line; do
  process "$line"
done < <(long_running_command)
```

---

### Q10: What is a here document and when would you use it?

**Answer:**

A here document (`<<`) provides multi-line input to a command:

```bash
# Prevent expansion with quoted delimiter
cat << 'EOF'
Variables like $PATH are not expanded
Backticks `date` are literal
EOF

# Allow expansion with unquoted delimiter
cat << EOF
User: $USER
Home: $HOME
Date: $(date)
EOF

# Common use case: send email
mail -s "Report" admin@example.com << 'EOF'
Daily backup completed successfully.
All systems operational.
EOF

# SQL queries
mysql -u user -p password database << 'SQL'
SELECT * FROM users WHERE id > 100;
UPDATE users SET status='active' WHERE id = 200;
SQL

# Configuration files
cat > /etc/config.conf << 'CONFIG'
setting1=value1
setting2=value2
CONFIG

# Redirect to variable
config=$(cat << 'CONF'
# Configuration
debug=true
loglevel=info
CONF
)
```

---

### Q11: Explain arrays in Bash and their uses.

**Answer:**

**Indexed arrays** (0-based):

```bash
# Declaration methods
colors=("red" "green" "blue")
colors[0]="red"
colors[1]="green"

# Access elements
echo ${colors[0]}          # red
echo ${colors[@]}          # All elements
echo ${colors[*]}          # All elements (as single word)
echo ${#colors[@]}         # Array length (3)

# Iteration
for color in "${colors[@]}"; do
  echo "Color: $color"
done

# Add element
colors[3]="yellow"
colors+=("purple")         # Append

# Delete element
unset colors[1]
```

**Associative arrays** (key-value, bash 4+):

```bash
declare -A person
person[name]="Alice"
person[age]="30"
person[city]="NYC"

# Access
echo ${person[name]}       # Alice

# Iterate keys
for key in "${!person[@]}"; do
  echo "$key: ${person[$key]}"
done

# Iterate values
for value in "${person[@]}"; do
  echo "Value: $value"
done

# Check key exists
if [[ -v person[name] ]]; then
  echo "Name is set"
fi

# Delete
unset person[city]
```

**Common operations:**

```bash
# Find element
arr=("a" "b" "c" "b")
search="b"
for i in "${!arr[@]}"; do
  if [ "${arr[i]}" = "$search" ]; then
    echo "Found at index $i"
  fi
done

# Get array length
length=${#arr[@]}

# Get element count
count=0
for _ in "${arr[@]}"; do
  ((count++))
done
```

---

### Q12: What is the purpose of `trap` and how do you use it?

**Answer:**

`trap` catches signals and system events for cleanup and error handling:

```bash
# Basic trap for cleanup
cleanup() {
  rm -f /tmp/tempfile_$$
  echo "Cleaned up"
}
trap cleanup EXIT

# Create temporary file
temp="/tmp/tempfile_$$"
touch "$temp"

# Trap multiple signals
shutdown() {
  echo "Received signal, shutting down gracefully"
  exit 0
}
trap shutdown INT TERM

# Error handling
error_handler() {
  echo "Error occurred on line $1"
  exit 1
}
trap 'error_handler $LINENO' ERR

# Ignore signal
trap '' INT                # Ignore Ctrl+C

# Reset trap
trap - EXIT                # Remove EXIT trap

# Debug trap
trap 'echo "Line $LINENO"' DEBUG
```

**Signal reference:**

| Signal | Number | Meaning |
|:-------|:-------|:--------|
| INT | 2 | Ctrl+C |
| TERM | 15 | Termination request |
| EXIT | 0 | Script exit |
| ERR | - | Command error |
| DEBUG | - | Before each command |
| KILL | 9 | Force kill (cannot trap) |

---

### Q13: Explain `getopts` and how to parse command-line arguments.

**Answer:**

`getopts` parses command-line options safely and portably:

```bash
#!/bin/bash

verbose=false
output_file=""
input_file=""

# Parse options
while getopts "vo:i:" opt; do
  case $opt in
    v)
      verbose=true
      ;;
    o)
      output_file="$OPTARG"
      ;;
    i)
      input_file="$OPTARG"
      ;;
    *)
      echo "Usage: $0 [-v] [-o FILE] [-i FILE] [args...]"
      exit 1
      ;;
  esac
done

# Remove parsed options from arguments
shift $((OPTIND - 1))

# Remaining arguments
remaining_args="$@"

# Validation
if [[ -z "$input_file" ]]; then
  echo "Error: -i is required"
  exit 1
fi

echo "Verbose: $verbose"
echo "Output: $output_file"
echo "Input: $input_file"
echo "Remaining: $remaining_args"
```

**Usage examples:**

```bash
./script.sh -v -o output.txt -i input.txt file1 file2
./script.sh -vi input.txt -o output.txt
./script.sh -i input.txt
```

**Benefits over manual parsing:**
- POSIX-compliant
- Handles option combinations (`-vi` equals `-v -i`)
- Tracks parsed options automatically
- Error handling built-in

---

## Functions and Scope

### Q14: How do variable scope and `local` variables work?

**Answer:**

By default, variables in Bash are global. Use `local` to limit scope to functions:

```bash
global_var="I'm global"

function test_scope() {
  local local_var="I'm local"
  global_var="Modified"        # Modifies global
  echo "Inside: $global_var, $local_var"
}

echo "Before: $global_var, $local_var"  # I'm global, (empty)
test_scope
echo "After: $global_var, $local_var"   # Modified, (empty)
```

**Practical example:**

```bash
# Without local (bugs possible)
counter=0
increment_bad() {
  counter=$((counter + 1))
  local temp=$((counter * 2))
  return $temp               # Can't return temp>255
}

# With proper scoping
counter=0
increment_good() {
  local local_counter=$((++counter))  # Doesn't affect global
  return $local_counter
}

# Better: return value via stdout
get_sum() {
  local a=$1 b=$2
  echo $((a + b))
}
result=$(get_sum 5 3)
echo "Sum: $result"
```

---

### Q15: What are the differences between functions and sourced scripts?

**Answer:**

**Functions** are defined in memory and share the current shell's variables:

```bash
my_function() {
  local var="local to function"
  global_var="I modify globals"
  echo "In function"
}

# Call function
my_function
echo $global_var             # "I modify globals"
```

**Sourced scripts** (`. file` or `source file`) execute in the current shell:

```bash
# In helper.sh
helper_var="set by sourced script"
helper_function() {
  echo "Helper function"
}

# In main script
source ./helper.sh           # Or . ./helper.sh
echo $helper_var             # "set by sourced script"
helper_function              # Works
```

**External scripts** (run as `./script.sh`) execute in a subshell:

```bash
# Subshell doesn't share variables
export_var="exported"
./script.sh $export_var      # Passes via argument, can't modify original
```

**Key differences:**

| Aspect | Function | Sourced | Subprocess |
|:-------|:---------|:--------|:-----------|
| Scope | Same shell | Same shell | New shell |
| Modify parent vars | Yes | Yes | No |
| Performance | Fast | Fast | Slower |
| Use case | Code reuse | Config files | External tools |

---

## Pattern Matching and Regex

### Q16: How do you use regular expressions in Bash?

**Answer:**

**Extended regex with `[[ ]]`:**

```bash
# Basic pattern matching
if [[ $string =~ ^hello ]]; then
  echo "Starts with hello"
fi

# Capture groups
if [[ "user@example.com" =~ ^([a-z]+)@([a-z.]+)$ ]]; then
  echo "User: ${BASH_REMATCH[1]}"
  echo "Domain: ${BASH_REMATCH[2]}"
fi

# Character classes
[[ $var =~ [0-9]+ ]]         # Contains digits
[[ $var =~ ^[a-zA-Z_] ]]     # Starts with letter/underscore
[[ $var =~ [[:space:]] ]]    # Contains whitespace
[[ $var =~ [[:digit:]] ]]    # Contains digit
```

**With grep:**

```bash
# Basic regex
grep "pattern" file.txt

# Extended regex (ERE)
grep -E "pattern1|pattern2" file.txt

# Perl regex (if available)
grep -P "(?=.*digit)(?=.*letter)" file.txt

# Invert match
grep -v "excluded" file.txt
```

**With sed and awk:**

```bash
# sed regex
sed 's/\([0-9]\+\)-\([0-9]\+\)/[\1,\2]/' file.txt

# awk regex
awk '/^2024/ {print}' log.txt
awk '{gsub(/foo/, "bar"); print}' file.txt
```

**Validation examples:**

```bash
# Email validation (simplified)
is_email() {
  [[ $1 =~ ^[a-zA-Z0-9]+@[a-zA-Z0-9]+\.[a-z]{2,}$ ]]
}

# IP address validation
is_ip() {
  [[ $1 =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]
}

# URL validation
is_url() {
  [[ $1 =~ ^https?:// ]]
}
```

---

### Q17: How do you use `sed` for text transformation?

**Answer:**

`sed` is a stream editor for filtering and transforming text:

```bash
# Substitution (s command)
sed 's/old/new/' file.txt         # Replace first occurrence per line
sed 's/old/new/g' file.txt        # Replace all occurrences
sed 's/old/new/2' file.txt        # Replace second occurrence
sed 's/old/new/i' file.txt        # Case-insensitive

# Delete (d command)
sed '5d' file.txt                 # Delete line 5
sed '1,5d' file.txt               # Delete lines 1-5
sed '/pattern/d' file.txt         # Delete matching lines
sed '$d' file.txt                 # Delete last line

# Print (p command) - use with -n
sed -n '10p' file.txt             # Print line 10
sed -n '/pattern/p' file.txt      # Print matching lines
sed -n '1,5p' file.txt            # Print lines 1-5

# In-place editing
sed -i 's/old/new/g' file.txt     # Modify file directly
sed -i.bak 's/old/new/g' file.txt # Backup original

# Address ranges
sed '/start/,/end/d' file.txt     # Delete from start to end
sed '/start/,/end/s/old/new/' file.txt  # Replace in range

# Multiple commands
sed -e 's/foo/bar/' -e 's/hello/hi/' file.txt
```

**Advanced examples:**

```bash
# Add prefix to each line
sed 's/^/PREFIX: /' file.txt

# Add suffix to each line
sed 's/$/ :SUFFIX/' file.txt

# Swap two fields (CSV)
sed 's/\([^,]*\),\([^,]*\)/\2,\1/' file.csv

# Remove leading whitespace
sed 's/^[ \t]*//' file.txt

# Number lines
sed = file.txt | sed 'N;s/\n/: /'
```

---

### Q18: Explain `awk` and common use cases.

**Answer:**

`awk` is a powerful text processing language for structured data:

```bash
# Print columns
awk '{print $1, $3}' file.txt      # Columns 1 and 3
awk -F: '{print $1}' /etc/passwd   # With separator

# Filtering
awk '$2 > 100' file.txt            # Rows where column 2 > 100
awk '/pattern/ {print}' file.txt   # Print matching lines
awk 'NR > 5 && NR < 10' file.txt   # Lines 6-9

# Calculations
awk '{sum += $1} END {print sum}' file.txt      # Sum column
awk '{print $1 * 2}' file.txt                   # Double column
awk '{sum += $1; count++} END {print sum/count}' file.txt  # Average

# String functions
awk '{print toupper($1)}' file.txt              # Uppercase
awk '{print length($0)}' file.txt               # Line length
awk '{gsub(/old/, "new"); print}' file.txt     # Replace all

# Built-in variables
awk '{print NR, NF, $0}' file.txt   # Line #, field count, line
awk 'BEGIN {print FILENAME} {print}' file.txt  # Filename
```

**Practical examples:**

```bash
# Sum column 3, grouped by column 1
awk '{sum[$1] += $3} END {for (key in sum) print key, sum[key]}' file.txt

# Count occurrences
awk '{count[$1]++} END {for (word in count) print word, count[word]}' file.txt

# Extract and format
awk -F: '{printf "%s is user %d\n", $1, $3}' /etc/passwd

# Conditional processing
awk '{
  if ($1 == "ERROR") {
    print "Found error on line", NR
  }
}' log.txt

# Multiple conditions
awk '$2 > 50 && $3 < 100 {print $0}' file.txt
```

---

## I/O and Redirection

### Q19: Explain input/output redirection in Bash.

**Answer:**

Bash provides flexible redirection of standard streams:

```bash
# Standard output (1) redirection
command > file.txt           # Overwrite file
command >> file.txt          # Append to file
command 1> file.txt          # Explicit stdout (1)

# Standard error (2) redirection
command 2> errors.txt        # Redirect errors
command 2>> errors.txt       # Append errors

# Redirect both
command > out.txt 2> err.txt # Separate files
command > all.txt 2>&1       # Combine to one file
command &> all.txt           # Shorthand for 2>&1

# Input redirection
command < input.txt          # Read from file
command 0< input.txt         # Explicit stdin (0)

# Here strings (<<<)
grep "pattern" <<< "text"    # Pass string as input

# Here documents (<<)
cat << 'EOF'
Multi-line
input
EOF

# Null redirection
command >/dev/null 2>&1      # Suppress all output

# Duplicate file descriptors
command 2>&1 | tee output.txt    # Copy to file and stdout
command > >(tee output.txt)      # Process substitution
```

**File descriptor reference:**

| FD | Name | Default |
|:---|:-----|:--------|
| 0 | stdin | keyboard |
| 1 | stdout | terminal |
| 2 | stderr | terminal |

---

### Q20: How do you read input in Bash scripts?

**Answer:**

**Using `read` builtin:**

```bash
# Simple read
read name
echo "Hello, $name"

# With prompt
read -p "Enter name: " name

# Multiple variables
read first last email
echo "$first $last <$email>"

# Default value
read -p "Name [default]: " name
name="${name:-default}"

# Limit input length
read -n 5 -p "5 chars: " input

# Timeout (10 seconds)
read -t 10 -p "Answer (10s): " answer

# Array input
read -a array
echo ${array[@]}

# Suppress echo (password)
read -sp "Password: " pass
```

**Reading files line-by-line:**

```bash
# Standard approach
while IFS= read -r line; do
  echo "Line: $line"
done < file.txt

# With field separator
while IFS=: read -r user password uid gid rest; do
  echo "User: $user, UID: $uid"
done < /etc/passwd

# Process substitution (for subshells)
while read line; do
  process "$line"
done < <(some_command)

# Command output
some_command | while read line; do
  process "$line"
done
```

**Edge cases:**

```bash
# Preserve whitespace
IFS= read -r line        # Trim internal spaces: IFS=""

# Read binary data safely
read -r -d '' -n $size data < file.bin

# Check read success
if read -t 5 var; then
  echo "Got input: $var"
else
  echo "Timeout or error"
fi
```

---

## Error Handling and Debugging

### Q21: How do you implement error handling in Bash?

**Answer:**

**Set error flags:**

```bash
#!/bin/bash
set -e                    # Exit on error
set -u                    # Error on undefined variables
set -o pipefail           # Pipe errors propagate
set -E                    # Trap inheritance in functions
```

**Trap errors:**

```bash
error_handler() {
  echo "Error on line $1"
  exit 1
}
trap 'error_handler $LINENO' ERR

# Function that fails
might_fail() {
  return 1
}

might_fail    # Triggers error handler
```

**Check exit codes:**

```bash
if ! command; then
  echo "Command failed with code $?"
  exit 1
fi

# Or use ||
command || {
  echo "Error: command failed"
  exit 1
}

# Use && for success
command && echo "Success" || echo "Failed"
```

**Validate inputs:**

```bash
check_args() {
  if [ $# -ne 2 ]; then
    echo "Usage: $0 <arg1> <arg2>"
    exit 1
  fi

  if [ -z "$1" ]; then
    echo "Error: arg1 cannot be empty"
    exit 1
  fi

  if [ ! -f "$2" ]; then
    echo "Error: file '$2' not found"
    exit 1
  fi
}
check_args "$@"
```

**Optional strict mode:**

```bash
#!/bin/bash
set -euo pipefail
IFS=$'\n\t'              # Safer IFS
```

---

### Q22: How do you debug Bash scripts?

**Answer:**

**Debug modes:**

```bash
# Run with -x (trace mode)
bash -x script.sh

# Enable inside script
set -x
commands...
set +x

# Verbose mode (-v shows before execution)
bash -v script.sh
set -v

# Check syntax without running
bash -n script.sh
```

**Customize PS4 for better tracing:**

```bash
PS4='+ [${BASH_SOURCE}:${LINENO}] ${FUNCNAME[0]}(): '
set -x
```

**Debug specific sections:**

```bash
debug() {
  [[ "$DEBUG" == "true" ]] && echo "DEBUG: $@" >&2
}

DEBUG=true ./script.sh      # Enable debugging
debug "This is a debug message"
```

**Breakpoints and stepping:**

```bash
# Pause and allow interaction (not true breakpoints)
set -i                      # Interactive
PS1='\u@\h:\w\$ '

# Or use bash debugger (bashdb)
bashdb script.sh
```

**Print variable state:**

```bash
print_vars() {
  echo "=== Variables ==="
  declare -p
}

print_functions() {
  echo "=== Functions ==="
  declare -F
}
```

---

## Performance and Best Practices

### Q23: How do you optimize Bash script performance?

**Answer:**

**Avoid unnecessary subshells:**

```bash
# Bad: spawns subshell
result=$(cat file.txt)

# Better: read into variable
IFS= read -r result < file.txt

# Bad: subshell for each iteration
for file in *; do
  echo "$(cat "$file")"
done

# Better: use pipe (single process)
while IFS= read -r file; do
  process "$file"
done < <(find . -type f)
```

**Use builtins over external commands:**

```bash
# Bad: calls external wc command
count=$(wc -l < file.txt)

# Better: use parameter expansion
lines="${line%%$'\n'*}"       # Gets first line

# Bad: multiple external commands
user=$(echo "$line" | awk -F: '{print $1}')

# Better: bash parameter expansion
IFS=: read user _ < <(echo "$line")
```

**Avoid pipes when possible:**

```bash
# Bad: multiple processes
cat file.txt | grep pattern | awk '{print $1}'

# Better: single awk process
awk '/pattern/ {print $1}' file.txt
```

**Cache results:**

```bash
# Bad: recalculates each iteration
for i in {1..1000}; do
  echo "$(($(date +%s) + i))"
done

# Better: calculate once
timestamp=$(date +%s)
for i in {1..1000}; do
  echo "$((timestamp + i))"
done
```

**Use appropriate data structures:**

```bash
# Bad: grep in loop
for item in "${items[@]}"; do
  if grep -q "$item" whitelist.txt; then
    process "$item"
  fi
done

# Better: load into array first
declare -A whitelist
while read item; do
  whitelist["$item"]=1
done < whitelist.txt

for item in "${items[@]}"; do
  if [[ -v whitelist["$item"] ]]; then
    process "$item"
  fi
done
```

---

### Q24: What are Bash best practices?

**Answer:**

**Code style:**

```bash
#!/usr/bin/env bash        # Portable shebang
set -euo pipefail          # Error handling
IFS=$'\n\t'                # Safer IFS

# Meaningful variable names
declare username           # Or local in functions
declare -i count           # Type hints

# Consistent formatting
if [[ condition ]]; then
  statement
fi
```

**Quoting:**

```bash
# Always quote variables
echo "$var"                # Good
echo $var                  # Bad: word splitting

# Use [[ ]] for tests
[[ $var == pattern ]]      # Good
[ $var = pattern ]         # Old style

# Array iteration
for item in "${array[@]}"; do  # Good
  process "$item"
done
```

**Functions:**

```bash
# Use function keyword (optional but explicit)
function check_file() {
  local file="$1"
  [[ -f "$file" ]] || return 1
}

# Validate arguments
main() {
  if [ $# -ne 1 ]; then
    echo "Usage: $0 <arg>" >&2
    return 1
  fi
  process "$1"
}
```

**Comments:**

```bash
#!/bin/bash
# Script purpose: brief description
# Usage: script.sh [-v] <file>

# Check if file exists and is readable
if [[ -r "$file" ]]; then
  process_file "$file"
fi
```

**Error handling:**

```bash
# Use trap for cleanup
cleanup() {
  [[ -n "${temp_dir:-}" ]] && rm -rf "$temp_dir"
}
trap cleanup EXIT

# Informative error messages
if [[ ! -f "$file" ]]; then
  echo "ERROR: File not found: $file" >&2
  return 1
fi
```

---

## System Administration

### Q25: How do you write a system monitoring script?

**Answer:**

```bash
#!/usr/bin/env bash

# Monitor CPU, memory, and disk
check_system() {
  local threshold_cpu=80
  local threshold_mem=85
  local threshold_disk=90

  # CPU usage (varies by system)
  cpu_usage=$(top -bn1 | grep "Cpu(s)" | sed 's/.*, \([0-9.]*\)%* id.*/\1/' | awk '{print 100 - $1}')
  if (( $(echo "$cpu_usage > $threshold_cpu" | bc -l) )); then
    echo "WARNING: CPU at ${cpu_usage}%"
  fi

  # Memory usage
  mem_usage=$(free | awk 'NR==2{printf("%.0f", $3/$2 * 100)}')
  if [ "$mem_usage" -gt "$threshold_mem" ]; then
    echo "WARNING: Memory at ${mem_usage}%"
  fi

  # Disk usage
  disk_usage=$(df / | awk 'NR==2{print $5}' | sed 's/%//')
  if [ "$disk_usage" -gt "$threshold_disk" ]; then
    echo "WARNING: Disk at ${disk_usage}%"
  fi

  # Load average
  load=$(uptime | awk -F'load average:' '{print $2}' | cut -d, -f1 | xargs)
  cpu_count=$(nproc)
  if (( $(echo "$load > $cpu_count" | bc -l) )); then
    echo "WARNING: Load average $load exceeds CPU count"
  fi
}

# Log output
main() {
  local logfile="/var/log/system_monitor.log"
  {
    echo "[$(date)] System Check"
    check_system
  } | tee -a "$logfile"
}

main "$@"
```

---

## Additional Questions

### Q26: What's the difference between `source` and `exec`?

`source` executes in current shell (same PID), `exec` replaces current shell (same PID, different process).

### Q27: How do you handle spaces in filenames?

Quote variables: `"$file"`, use arrays: `for f in "${array[@]}"`, use `find -print0` with `read -d ''`

### Q28: What is IFS and why is it important?

IFS (Internal Field Separator) controls how Bash splits words. Default is space/tab/newline. Critical for reading fields correctly.

### Q29: How do you create multiline strings?

Use `$'...'` syntax, heredocs, or concatenation: `var=$'line1\nline2'`

### Q30: How do you implement a simple retry logic?

```bash
retry() {
  local n=1 max=5 delay=1
  while true; do
    "$@" && break || {
      if [ $n -lt $max ]; then
        echo "Attempt $n failed, retrying..."
        sleep $((delay))
        n=$((n + 1))
      else
        return 1
      fi
    }
  done
}
```

### Q31: How do you validate input types?

```bash
is_integer() { [[ $1 =~ ^-?[0-9]+$ ]]; }
is_float() { [[ $1 =~ ^-?[0-9]+\.[0-9]+$ ]]; }
is_email() { [[ $1 =~ ^[a-zA-Z0-9]+@[a-zA-Z0-9]+\.[a-z]+$ ]]; }
```

### Q32: How do you organize large Bash projects?

Use modular structure with sourced files, separate concerns (functions, config, main), document with comments, use consistent naming.

### Q33: What are the pitfalls to avoid in Bash?

Don't rely on word splitting, quote variables, use `[[ ]]` over `[ ]`, avoid `eval`, check exit codes, use meaningful names, avoid global variables.

### Q34: How do you test Bash scripts?

Use `bash -n` for syntax, `shellcheck` for linting, write test functions, mock external commands, test edge cases.

### Q35: How do you ensure script portability?

Use POSIX features, check `sh` compatibility, avoid bash-isms, test on multiple shells/systems, document requirements.

---

## Key Takeaways

- **Fundamentals**: Understand variables, quoting, and control structures
- **Advanced**: Master process substitution, traps, and text processing
- **Error handling**: Always validate inputs and check exit codes
- **Performance**: Use builtins, avoid unnecessary subshells and pipes
- **Best practices**: Quote variables, use `[[ ]]`, add comments, handle errors gracefully

## See Also

- [Fundamentals](/docs/tools/bash/fundamentals) for detailed core concepts
- [Advanced](/docs/tools/bash/advanced) for signal handling and automation
- [Cheatsheet](/docs/tools/bash/cheatsheet) for quick reference
