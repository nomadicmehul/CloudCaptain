from __future__ import annotations

import os
import shlex
from typing import Dict

from .runner import PaasError, Runner, must

DEPLOY_BRANCH = "main"


def post_receive(python: str, tinypaas: str, app: str, env: Dict[str, str], branch: str = DEPLOY_BRANCH) -> str:
    exports = "\n".join(f"export {key}={shlex.quote(value)}" for key, value in sorted(env.items()))
    return f"""#!/usr/bin/env bash
set -euo pipefail
{exports}
REPO="$(pwd)"
unset $(git rev-parse --local-env-vars)
while read -r OLD NEW REF; do
  if [[ "$REF" != "refs/heads/{branch}" ]]; then
    echo "tinypaas: $REF is not {branch}; nothing to deploy"
    continue
  fi
  if [[ "$NEW" =~ ^0+$ ]]; then
    echo "tinypaas: {branch} was deleted; nothing to deploy"
    continue
  fi
  WORK="$(mktemp -d)"
  git clone --quiet "$REPO" "$WORK/src"
  git -C "$WORK/src" checkout --quiet "$NEW"
  STATUS=0
  {shlex.quote(python)} {shlex.quote(tinypaas)} deploy "$WORK/src" --app {shlex.quote(app)} || STATUS=$?
  rm -rf "$WORK"
  if [[ "$STATUS" != "0" ]]; then
    echo "tinypaas: deploy of ${{NEW:0:7}} failed (exit $STATUS); the previous release keeps serving"
  fi
done
"""


def install(bare_path: str, script: str, run: Runner) -> str:
    bare_path = os.path.abspath(bare_path)
    if os.path.exists(bare_path) and os.listdir(bare_path):
        probe = run(["git", "--git-dir", bare_path, "rev-parse", "--is-bare-repository"], timeout=20)
        if not probe.ok or probe.out.strip() != "true":
            raise PaasError(f"{bare_path} exists and is not a bare git repository")
    else:
        must(run(["git", "init", "--quiet", "--bare", "--initial-branch", DEPLOY_BRANCH, bare_path], timeout=30), "git init --bare")
    hook = os.path.join(bare_path, "hooks", "post-receive")
    os.makedirs(os.path.dirname(hook), exist_ok=True)
    with open(hook, "w", encoding="utf-8") as f:
        f.write(script)
    os.chmod(hook, 0o755)
    return hook
