---
title: "Bash Scripting"
description: "Master shell scripting with comprehensive guides and best practices"
sidebar_position: 1
---

# Bash Scripting

Automate tasks, build tools, and master DevOps with Bash — the default shell on most Linux systems and cloud platforms. From fundamentals to production-grade scripts, these guides cover everything you need.

## Learning Path

Start with the fundamentals and progress through advanced topics:

1. **[Fundamentals](/docs/tools/bash/fundamentals)** — Variables, quoting, control flow, functions, arrays
2. **[Advanced](/docs/tools/bash/advanced)** — Process substitution, here documents, traps, debugging, sed/awk, cron
3. **[Cheatsheet](/docs/tools/bash/cheatsheet)** — 100+ commands and one-liners for quick reference
4. **[Interview Questions](/docs/tools/bash/interview-questions)** — 35+ questions with detailed answers

## Quick Start

### Your First Bash Script

```bash
#!/usr/bin/env bash
echo "Hello, World!"
```

Save as `hello.sh`, make executable, and run:

```bash
chmod +x hello.sh
./hello.sh
```

### Check Syntax

```bash
bash -n script.sh        # Check for errors without running
```

### Common Patterns

**Variables and quoting:**

```bash
name="Alice"
echo "Hello, $name"      # Variable expansion
echo 'Literal $name'     # No expansion
```

**Conditional logic:**

```bash
if [ $# -ne 1 ]; then
  echo "Usage: $0 <name>"
  exit 1
fi
```

**Looping:**

```bash
for file in *.txt; do
  echo "Processing: $file"
done
```

**Functions:**

```bash
greet() {
  echo "Hello, $1!"
}
greet "Alice"
```

## Why Learn Bash?

- **Ubiquitous**: Default shell on Linux servers and macOS
- **Essential for DevOps**: Docker, Kubernetes, CI/CD automation
- **System Administration**: Server management, monitoring, backups
- **Scripting**: Task automation, cron jobs, system tools
- **Interview Prep**: Common in technical assessments
- **Quick Wins**: Automate repetitive tasks immediately

## Key Topics

### Fundamentals
- Variables and data types
- Quoting and expansion
- Control structures (if/for/while/case)
- Functions and scope
- Arrays and associative arrays
- String manipulation
- Arithmetic operations
- Exit codes and error handling

### Advanced
- Process substitution (`<()`, `>()`)
- Here documents and here strings
- Signal handling with `trap`
- Debugging (`set -x`, `bash -n`)
- Parameter parsing with `getopts`
- Text processing with `sed` and `awk`
- Regular expressions
- Cron automation
- Parallel execution

### Real-World Use Cases
- System monitoring scripts
- Backup automation
- Log analysis
- Configuration management
- Deployment pipelines
- User provisioning
- Health checks

## Navigation

| Page | Focus |
|:-----|:------|
| [Fundamentals](/docs/tools/bash/fundamentals) | Core concepts, syntax, control flow |
| [Advanced](/docs/tools/bash/advanced) | Process substitution, traps, text processing, automation |
| [Cheatsheet](/docs/tools/bash/cheatsheet) | 100+ commands, snippets, one-liners |
| [Interview Questions](/docs/tools/bash/interview-questions) | 35+ Q&A with detailed explanations |

## Best Practices

### Error Handling

```bash
#!/usr/bin/env bash
set -euo pipefail      # Exit on error, undefined vars, pipe failures

# Use meaningful error messages
if [[ ! -f "$1" ]]; then
  echo "ERROR: File not found: $1" >&2
  exit 1
fi
```

### Safe Practices

```bash
# Always quote variables
echo "$var"            # Good

# Use [[ ]] over [ ]
[[ $var == pattern ]]  # Good
[ $var = pattern ]     # Old style

# Use local in functions
function my_func() {
  local var="local"    # Won't affect global scope
}
```

### Debugging

```bash
# Enable trace mode
set -x

# Or run with bash -x
bash -x script.sh

# Check syntax
bash -n script.sh
```

## External Resources

- [GNU Bash Manual](https://www.gnu.org/software/bash/manual/)
- [ShellCheck](https://www.shellcheck.net/) — Bash script linter
- [Bashfuscator](https://github.com/Bashfuscator/Bashfuscator) — Obfuscation research
- [POSIX Shell Standard](https://pubs.opengroup.org/onlinepubs/9699919799/)
- [Advanced Bash-Scripting Guide](https://www.tldp.org/LDP/abs/html/)

## Contributing

Have improvements, corrections, or additional examples? [Contribute to CloudCaptain](https://github.com/nomadicmehul/CloudCaptain) and help the community learn Bash!

## Next Steps

Ready to dive in? Start with [Fundamentals](/docs/tools/bash/fundamentals) and work through the exercises, or jump to the [Cheatsheet](/docs/tools/bash/cheatsheet) for quick reference when you need it.
