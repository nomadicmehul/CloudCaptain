---
name: shell-script-to-playbook
description: Turn a provisioning shell script into an idempotent Ansible inventory and playbook, line by line, with a guard or changed_when for everything that has no module and restarts moved into handlers
phase: iac
lesson: iac/ansible-config
---

You are converting a provisioning shell script into Ansible. I will paste the script, and optionally the hosts it runs on and the distribution. The goal is a playbook whose second run reports `changed=0` and whose `--check --diff` output is honest. Produce exactly these sections and nothing else.

**What the script does**
Three to six bullets describing the end state the script is trying to reach (packages, files, users, services, one-off data), not the commands it runs.

**Line-by-line mapping**
A table with the columns: script line(s) | intent | module (fully qualified, for example `ansible.builtin.lineinfile`) | how the module checks before it changes. Every non-comment line of the script appears in the table. Lines that only exist because the script was not idempotent (`grep -q ... ||`, `if [ ! -f ... ]`, `|| true`) map to "absorbed by the module above" and name which one.

**Inventory**
An INI inventory with the groups the script implies and the variables it hard-codes (ports, paths, versions, user names) moved into `[group:vars]`. Hosts and secrets are placeholders.

**Playbook**
One YAML document. Rules:
- Use a module whenever one exists: `copy`/`template` for files written with `cat <<EOF` or `echo >`, `lineinfile`/`blockinfile` for `echo >>` and `sed -i`, `file` for `mkdir -p`/`chmod`/`chown`/`ln -s`, `package` or `apt`/`dnf` with pinned versions for installs, `user`/`group`, `get_url` with `checksum:` for downloads, `unarchive` with `creates:`, `service`/`systemd_service` with `state: started` and `enabled: true`.
- Every `command` or `shell` that remains has `creates:`, `removes:` or a `changed_when:` that names the evidence of change, and read-only commands have `changed_when: false`.
- Every restart or reload becomes a handler notified only by the tasks that change what the service reads; `systemctl daemon-reload` becomes `daemon_reload: true` on the handler.
- Modes are quoted strings (`"0644"`), secrets use `no_log: true` and a vault variable, and no package uses `state: latest`.
- Facts replace `uname`/`lsb_release` checks (`when: ansible_facts.os_family == "Debian"`).

**Leftovers**
Anything that cannot be made idempotent (a database migration, a one-time registration call): the task as written, the guard you chose, and the risk if the guard is wrong.

**How to prove it**
The exact commands to run the playbook twice against a throwaway container of the target distribution and the expected `PLAY RECAP` for each run, then a drift test (change one managed file, run `--check --diff`, expect exactly that change).

Rules: never invent paths, versions or users the script does not contain; mark guesses with `# assumption:`. Keep task names short and imperative. If the script does something dangerous (`rm -rf` with a variable, `curl | sh`, disabling a firewall), say so in Leftovers and do not translate it silently.
