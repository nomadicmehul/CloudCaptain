---
name: idempotent-playbook-review
description: Review an Ansible playbook or role for idempotence before it merges - lint it, read every task for its check, run it twice against a throwaway container and require changed=0, prove --check --diff reports exactly the drift, and report in a fixed shape
version: 1.0.0
phase: iac
lesson: iac/ansible-config
tags: [ansible, idempotence, check-mode, handlers, ansible-lint, molecule, review]
---

# Idempotent playbook review

Use this when a pull request adds or changes a playbook, a role or an inventory, when a scheduled run keeps reporting `changed` on hosts nobody touched, or before a playbook is allowed to run unattended. Work from real runs, not from reading alone.

## 1. Static checks

```bash
ansible-playbook -i inventory.ini site.yml --syntax-check
ansible-lint site.yml                                  # fqcn, no-changed-when, risky-octal, risky-file-permissions, package-latest
ansible-inventory -i inventory.ini --graph             # the hosts and groups the play will really target
ansible-inventory -i inventory.ini --host HOST         # merged inventory vars for one host; check the values the PR depends on
ansible-playbook -i inventory.ini site.yml --list-tasks --list-hosts
```

Stop on syntax errors. Lint findings are not all blockers; the ones in the table below are.

## 2. Read every task for its check

For each task, name the comparison the module makes before it writes. If you cannot name one, the task is not idempotent.

| Task shape | Verdict | Fix to ask for |
|---|---|---|
| `shell`/`command` that edits files (`echo ... >>`, `sed -i`, `cat <<EOF >`) | Never idempotent; duplicates or rewrites on every run | `lineinfile`, `blockinfile`, `copy` or `template` |
| `command`/`shell` that creates something, with no `creates:`/`removes:` | Reports changed on every run; skipped in check mode | `creates:` on the artifact it makes, or a module (`unarchive`, `get_url` with `checksum:`) |
| read-only `command` (`uname`, `kubectl get`, `curl -f`) without `changed_when: false` | False changes, triggers handlers | `changed_when: false`, and `check_mode: false` if later tasks need its output |
| `service: state=restarted` in `tasks:` | Restarts on every run | A handler notified by the tasks that change its config; `state: started` in tasks |
| `mode: 644` or any unquoted mode without a leading zero | Wrong permissions (decimal 644 is octal 1204) | `mode: "0644"` |
| `copy`/`template` of secrets with no `mode:` or no `no_log: true` | Secret world-readable or printed in logs and `--diff` | `mode: "0600"`, `owner:`, `no_log: true`, Ansible Vault for the value |
| `state: latest` on packages | Changes whenever the repository moves; not reproducible | Pin `name: pkg=1.2.3` or `state: present` plus a separate upgrade play |
| `when:` containing `{{ }}`, or a condition on a string variable | Fragile; current ansible-core rejects non-boolean conditionals | Bare expression, compare explicitly (`x \| bool`, `x == "yes"`) |
| `ignore_errors: true` | Hides real failures and keeps going | `failed_when:` with the specific condition that is acceptable |
| Template that renders a timestamp, random value or unordered dict | Content differs every run | Remove the volatile value or sort it |

## 3. Run it twice on a throwaway target

Use a container whose distribution matches production. Alpine and the official `python` images already have Python; Debian and Ubuntu base images need `apt-get install -y python3` first.

```bash
docker run -d --name review-box --hostname review-box python:3.12-alpine sleep 3600
printf '[target]\nreview-box ansible_connection=community.docker.docker ansible_python_interpreter=/usr/local/bin/python3\n' > review.ini
ansible-galaxy collection install community.docker
ansible-playbook -i review.ini site.yml -l review-box -e @review-vars.yml 2>&1 | tee run1.txt
ansible-playbook -i review.ini site.yml -l review-box -e @review-vars.yml 2>&1 | tee run2.txt
grep -A3 'PLAY RECAP' run1.txt run2.txt
```

Required: `failed=0` and `unreachable=0` on both runs, `changed=0` on the second. For every task that reports `changed` on the second run, write down the task name and the reason from step 2. A role with Molecule gets the same check from `molecule test`, whose `idempotence` step fails on any change in the second converge.

## 4. Prove that check mode tells the truth

```bash
docker exec review-box sh -c 'echo drift >> /etc/app/app.env'        # or delete one managed file
ansible-playbook -i review.ini site.yml -l review-box --check --diff 2>&1 | tee check.txt
docker exec review-box cat /etc/app/app.env                           # the drift must still be there
ansible-playbook -i review.ini site.yml -l review-box 2>&1 | tee repair.txt
docker rm -f review-box
```

Required: `check.txt` reports exactly the tasks that manage the drifted files as `changed`, shows the drift in its diff, and writes nothing; `repair.txt` fixes it. Tasks that report `changed` in check mode without drift are the same bugs as step 3. Tasks shown as `skipping` in check mode are commands without guards; list them, because a dry run cannot vouch for them.

## 5. Handlers, ordering and blast radius

- Every restart or reload is a handler, notified only by tasks that change what the service reads. Handler names are unique, or they use `listen:` topics.
- A play that must restart before later tasks continue uses `meta: flush_handlers` at that point, not a restart task.
- Rolling changes across many hosts set `serial:` and `max_fail_percentage:`, so one bad host stops the rollout instead of the fleet.
- `become: true` is set on the tasks that need it, not the whole play, unless every task does.
- Variables the PR introduces are defined at one precedence level; check with `ansible-inventory --host` that no group or host var silently overrides them.

## Report

Post one comment in this shape:

```
Verdict: IDEMPOTENT | FIX BEFORE MERGE | DO NOT RUN
Second run: changed=N (tasks: ...)
Check mode: honest | reports X without drift | skips N commands (list)
Blocking findings:
  - <task name> (<file>:<line>): <shape from the table> -> <fix>
Non-blocking:
  - ...
Commands run: syntax-check, ansible-lint, two runs on <image>, drift test
```

`IDEMPOTENT` only when the second run was `changed=0` and the drift test passed. `DO NOT RUN` when a task can destroy data (an unguarded `rm`, a `shell` that re-runs a migration, `state: absent` on a broad path) or when a secret leaks in `--diff` output.
