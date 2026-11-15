#!/usr/bin/env bash
# Grade a capstone project: bash grader/grade.sh <project-dir> [--milestone ID] [--json]
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/grade.py" "$@"
