---
title: "Advanced Bash Scripting"
description: "Process substitution, here documents, traps, debugging, sed, awk, and automation"
sidebar_label: "Advanced"
sidebar_position: 2
---

# Advanced Bash Scripting

Master advanced techniques for production-grade shell scripts: process substitution, here documents, signal handling, debugging, text processing, and automation.

## Here Documents

A here document (`<<`) passes multi-line input to a command:

```bash
cat << 'EOF'
This is a multi-line
document that preserves
all formatting and newlines.
EOF
```

**Quoted delimiter** (`'EOF'`) prevents expansion:

```bash
cat << 'EOF'
Variables like $PATH are not expanded
Backticks `date` are literal
EOF
```

**Unquoted delimiter** allows expansion:

```bash
cat << EOF
User: $USER
Home: $HOME
Date: $(date)
EOF
```

### Here Strings

A here string (`<<<`) passes a single string to a command:

```bash
grep "pattern" <<< "Here is the input string"
bc <<< "5 + 3"                    # Outputs: 8
wc -w <<< "Count these words"     # Outputs: 3
```

### Redirect to Variable

```bash
output=$(cat << 'EOF'
Line 1
Line 2
Line 3
EOF
)
echo "$output"
```

## Process Substitution

Process substitution (`<()` and `>()`) treats command output as files:

**Input substitution** — Pass command output as file argument:

```bash
diff <(sort file1.txt) <(sort file2.txt)
```

Equivalent to:

```bash
diff <(cat file1.txt | sort) <(cat file2.txt | sort)
```

**Output substitution** — Redirect to command input:

```bash
tee >(cat > file1.txt) >(cat > file2.txt) <<< "content"
```

### Practical Examples

**Compare two commands:**

```bash
diff <(ps aux) <(ps aux)
```

**Merge sorted streams:**

```bash
comm <(grep admin /etc/passwd | sort) \
     <(grep admin /etc/group | sort)
```

**Parallel processing:**

```bash
paste <(seq 1 5) <(seq 6 10)  # Creates two-column output
```

## Signal Handling with trap

The `trap` command catches signals and system events:

```bash
trap 'echo "Caught interrupt"; exit' INT
```

**Clean up on exit:**

```bash
#!/bin/bash

cleanup() {
  echo "Cleaning up..."
  rm -f /tmp/temp_$$
  exit 0
}

trap cleanup EXIT

# Create temp file
temp_file="/tmp/temp_$$"
touch "$temp_file"
echo "Working with $temp_file"
```

**Handle multiple signals:**

```bash
error_handler() {
  echo "Error on line $1"
  exit 1
}

trap 'error_handler $LINENO' ERR
```

**Common signals:**

| Signal | Meaning | Use |
|:-------|:--------|:----|
| `INT` | Ctrl+C | Clean shutdown |
| `TERM` | Termination | Graceful exit |
| `EXIT` | Script exit | Final cleanup |
| `ERR` | Command error | Error handling |
| `DEBUG` | Each command | Tracing |

## Debugging

### Debug Mode

Enable debugging to trace execution:

```bash
bash -x script.sh              # Run with trace
```

**Inside script:**

```bash
set -x                         # Start debugging
commands...
set +x                         # Stop debugging
```

**Output shows command before execution:**

```
+ echo 'Processing file'
Processing file
+ ls -l
total 8
...
```

### PS4 Customization

Customize debug prompt:

```bash
PS4='+ [${BASH_SOURCE}:${LINENO}] '
set -x
```

Output shows filename and line number.

### Verbose Mode

```bash
set -v                         # Print commands before execution
set +v                         # Turn off
```

### Check Syntax Without Running

```bash
bash -n script.sh              # Check syntax
```

## Parameter Parsing with getopts

Parse command-line options safely:

```bash
#!/bin/bash

verbose=false
output_file=""

while getopts "vo:" opt; do
  case $opt in
    v)
      verbose=true
      ;;
    o)
      output_file="$OPTARG"
      ;;
    *)
      echo "Usage: $0 [-v] [-o FILE]"
      exit 1
      ;;
  esac
done

shift $((OPTIND-1))            # Remove processed options
remaining_args="$@"

echo "Verbose: $verbose"
echo "Output: $output_file"
echo "Arguments: $remaining_args"
```

**Usage:**

```bash
./script.sh -v -o output.txt file1 file2
```

## Text Processing: sed and awk

### sed (Stream Editor)

**Print lines:**

```bash
sed -n '5p' file.txt           # Print line 5
sed -n '1,5p' file.txt         # Print lines 1-5
sed -n '/pattern/p' file.txt   # Print matching lines
```

**Delete lines:**

```bash
sed '5d' file.txt              # Delete line 5
sed '/pattern/d' file.txt      # Delete matching lines
sed '1,5d' file.txt            # Delete lines 1-5
```

**Substitute (replace):**

```bash
sed 's/old/new/' file.txt      # Replace first on each line
sed 's/old/new/g' file.txt     # Replace all on each line
sed 's/old/new/2' file.txt     # Replace second on each line
sed -i 's/old/new/g' file.txt  # In-place edit
```

**Case-insensitive:**

```bash
sed 's/Pattern/replaced/I' file.txt
```

**Multi-line sed:**

```bash
sed -e 's/pattern1/replace1/' \
    -e 's/pattern2/replace2/' \
    -e '/unwanted/d' file.txt
```

### awk (Pattern-Action Language)

**Print columns:**

```bash
awk '{print $1, $3}' file.txt  # Print columns 1 and 3
awk '{print NF}' file.txt      # Print number of fields
```

**Filter by pattern:**

```bash
awk '/pattern/ {print}' file.txt           # Print matching lines
awk '$2 > 100 {print $1, $2}' file.txt     # Print where col 2 > 100
```

**Arithmetic:**

```bash
awk '{sum += $1} END {print sum}' file.txt  # Sum column 1
awk '{print $1 * 2}' file.txt               # Double column 1
```

**Field separator:**

```bash
awk -F: '{print $1, $3}' /etc/passwd   # Use ':' as separator
awk -F'[,:]' '{print $1}' file.txt     # Multiple separators
```

**Built-in variables:**

| Variable | Meaning |
|:---------|:--------|
| `NR` | Current row number |
| `NF` | Number of fields |
| `FS` | Field separator (default: space) |
| `RS` | Record separator (default: newline) |
| `OFS` | Output field separator |
| `ORS` | Output record separator |

**Complex example:**

```bash
awk -F: '
  NR > 1 {                    # Skip header
    sum += $3
    count++
  }
  END {
    if (count > 0)
      print "Average:", sum/count
  }
' data.txt
```

## Text Processing Pipelines

**Extract and transform:**

```bash
cat data.txt | \
  grep "status=active" | \
  awk -F',' '{print $2, $4}' | \
  sort | \
  uniq -c | \
  sort -rn
```

**Count occurrences:**

```bash
cat file.txt | \
  tr ' ' '\n' | \
  sort | \
  uniq -c | \
  sort -rn | \
  head -10
```

**Extract IP addresses:**

```bash
grep -oE '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}' file.txt | \
  sort | \
  uniq -c | \
  sort -rn
```

## Regular Expressions

### Basics

```bash
^text       # Start of line
text$       # End of line
.           # Any character
*           # Zero or more
+           # One or more (extended regex)
?           # Zero or one (extended regex)
[abc]       # Character class
[^abc]      # Negated character class
\d          # Digit (in some contexts)
\w          # Word character (in some contexts)
```

### Examples

```bash
# Match email-like pattern
grep -E '[a-zA-Z0-9]+@[a-zA-Z0-9]+\.[a-z]{2,}' file.txt

# Extract version numbers
grep -oE '[0-9]+\.[0-9]+\.[0-9]+' file.txt

# Find log entries for specific date
grep '^2024-03-' log.txt

# Match lines with exactly 3 digits
grep -E '^[0-9]{3}$' file.txt
```

## Cron Automation

### Crontab Syntax

```
┌───────────── minute (0-59)
│ ┌───────────── hour (0-23)
│ │ ┌───────────── day of month (1-31)
│ │ │ ┌───────────── month (1-12)
│ │ │ │ ┌───────────── day of week (0-7, 0/7 = Sunday)
│ │ │ │ │
│ │ │ │ │
* * * * * command
```

### Common Schedules

```bash
0 9 * * *       # 9:00 AM daily
0 */4 * * *     # Every 4 hours
30 2 * * 0      # 2:30 AM on Sunday
0 0 1 * *       # Midnight on first of month
0 12 * * 1-5    # Noon, Monday-Friday
*/15 * * * *    # Every 15 minutes
```

### Create Cron Job

```bash
crontab -e                     # Edit current user's crontab
crontab -l                     # List current crontab
crontab -r                     # Remove crontab
crontab -u user -e             # Edit another user's (as root)
```

**Example crontab entry:**

```bash
# Backup database daily at 2 AM
0 2 * * * /home/user/scripts/backup.sh >> /var/log/backup.log 2>&1

# Clean logs weekly
0 3 * * 0 find /var/log -name "*.log" -mtime +30 -delete
```

### Best Practices

- Use full paths in cron scripts
- Redirect output to log files
- Use `>/dev/null 2>&1` to suppress output if not needed
- Set email recipient for errors: `MAILTO=admin@example.com`
- Use UTC for consistency across systems

## Regex Matching in Bash

**Conditional regex match:**

```bash
if [[ $email =~ ^[a-zA-Z0-9]+@[a-zA-Z0-9]+\.[a-z]{2,}$ ]]; then
  echo "Valid email"
fi
```

**Extract matched groups:**

```bash
if [[ $string =~ ^([a-z]+)-([0-9]+)$ ]]; then
  name="${BASH_REMATCH[1]}"
  number="${BASH_REMATCH[2]}"
fi
```

## Advanced I/O

### Read with IFS

Split input by field separator:

```bash
IFS=: read -r user password uid gid rest <<< "root:x:0:0:..."
echo "User: $user, UID: $uid"
```

### Parallel Processing

**Using xargs:**

```bash
find . -name "*.txt" | xargs -I {} wc -l {}
cat file_list.txt | xargs -P 4 -I {} process_file {}
```

**Using GNU Parallel:**

```bash
parallel "process {} > {.}_output.txt" ::: file1.txt file2.txt file3.txt
```

## Exercises

### Exercise 1: Log Analysis Script

Create a script analyzing access logs:

```bash
#!/bin/bash

logfile="${1:-/var/log/access.log}"

if [ ! -f "$logfile" ]; then
  echo "Error: $logfile not found"
  exit 1
fi

echo "=== Log Analysis ==="
echo "Total requests: $(wc -l < "$logfile")"
echo "Unique IPs:"
awk '{print $1}' "$logfile" | sort | uniq -c | sort -rn | head -5
echo "Status codes:"
awk '{print $9}' "$logfile" | sort | uniq -c | sort -rn
```

### Exercise 2: Backup with Cleanup

Create an automated backup script:

```bash
#!/bin/bash

set -euo pipefail

backup_dir="/backups"
source_dir="/home/data"
retention_days=7

cleanup() {
  echo "Cleaning up..."
  rm -f "$backup_file.tmp"
}

trap cleanup EXIT

backup_file="$backup_dir/backup_$(date +%Y%m%d_%H%M%S).tar.gz"

echo "Starting backup of $source_dir..."
tar -czf "$backup_file.tmp" "$source_dir"
mv "$backup_file.tmp" "$backup_file"
echo "Backup complete: $backup_file"

# Delete old backups
find "$backup_dir" -name "backup_*.tar.gz" -mtime "+$retention_days" -delete
```

### Exercise 3: Config Parser

Parse key=value configuration:

```bash
#!/bin/bash

config_file="${1:-config.txt}"

while IFS='=' read -r key value; do
  # Skip comments and empty lines
  [[ "$key" =~ ^#.*$ ]] && continue
  [ -z "$key" ] && continue

  # Trim whitespace
  key="${key// /}"
  value="${value// /}"

  export "$key=$value"
done < "$config_file"

echo "Loaded config:"
declare -p | grep "^\|declare"
```

### Exercise 4: Monitoring Script with Cron

Create a system monitoring script:

```bash
#!/bin/bash

check_disk() {
  usage=$(df / | awk 'NR==2 {print $5}' | sed 's/%//')
  if [ "$usage" -gt 80 ]; then
    echo "WARNING: Disk usage at $usage%"
  fi
}

check_memory() {
  usage=$(free | awk 'NR==2 {printf "%.0f", ($3/$2)*100}')
  if [ "$usage" -gt 80 ]; then
    echo "WARNING: Memory usage at $usage%"
  fi
}

check_load() {
  load=$(uptime | awk -F'load average:' '{print $2}' | cut -d, -f1)
  threshold=$(nproc)
  if (( $(echo "$load > $threshold" | bc -l) )); then
    echo "WARNING: Load average $load exceeds CPU count"
  fi
}

{
  check_disk
  check_memory
  check_load
} 2>&1 | while IFS= read -r line; do
  [ -n "$line" ] && echo "[$(date)] $line"
done >> /var/log/system_checks.log
```

Add to crontab:

```bash
*/5 * * * * /usr/local/bin/monitor.sh
```

## Key Takeaways

- Here documents enable multi-line input; here strings pass single strings
- Process substitution treats command output as file arguments
- `trap` handles signals for clean shutdown and resource cleanup
- Debugging with `set -x` and `bash -n` catches errors early
- `getopts` provides robust command-line parsing
- `sed` and `awk` are powerful for text transformation
- Regular expressions enable pattern matching and extraction
- Cron automates periodic tasks with specific scheduling
- Combining commands in pipelines creates powerful data workflows

## Next Steps

Explore the [Cheatsheet](/docs/tools/bash/cheatsheet) for quick reference of 100+ commands and snippets, or review [Interview Questions](/docs/tools/bash/interview-questions) to prepare for technical assessments.
