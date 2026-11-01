#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
ANSIBLE_CORE_VERSION="2.21.5"
ANSIBLE_CORE_WHEEL_SHA256="08886f9f3e23d129aa98a01271c4a47833516e1cb7c737a246788cb474f41222"
COMMUNITY_DOCKER_VERSION="5.4.0"
ANSIBLE_LINT_VERSION="26.10.1"
MINI="$PWD/code/main.py"
export CODE_DIR="$PWD/code"
WORK="$(mktemp -d)"
BOX="cc-ansible-config-box"
cleanup() {
  if command -v docker >/dev/null 2>&1; then docker rm -f "$BOX" >/dev/null 2>&1 || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

mkdir -p "$WORK/pb/templates" "$WORK/mini" "$WORK/real" "$WORK/remote-tmp"
cat > "$WORK/pb/site.yml" <<'EOF'
- name: configure the demo app
  hosts: app
  gather_facts: true
  vars:
    app_root: "{{ root }}/srv/app"
    app_port: 8080
    features:
      - metrics
      - healthz
  tasks:
    - name: app directory
      ansible.builtin.file:
        path: "{{ app_root }}"
        state: directory
        mode: "0755"
    - name: config file from a template
      ansible.builtin.template:
        src: app.conf.j2
        dest: "{{ app_root }}/app.conf"
        mode: "0644"
      notify: reload app
    - name: banner
      ansible.builtin.copy:
        content: |
          managed by configuration management
          do not edit by hand
        dest: "{{ app_root }}/motd"
        mode: "0644"
    - name: port in the env file
      ansible.builtin.lineinfile:
        path: "{{ app_root }}/app.env"
        regexp: "^PORT="
        line: "PORT={{ app_port }}"
        create: true
        mode: "0644"
      notify: reload app
    - name: seed data once
      ansible.builtin.command:
        cmd: "touch {{ app_root }}/seeded"
        creates: "{{ app_root }}/seeded"
    - name: debian family marker
      ansible.builtin.copy:
        content: "debian family\n"
        dest: "{{ app_root }}/family"
        mode: "0644"
      when: ansible_facts.os_family == "Debian"
  handlers:
    - name: reload app
      ansible.builtin.shell:
        cmd: "echo reloaded >> {{ app_root }}/reloads"
      changed_when: true
EOF
cat > "$WORK/pb/templates/app.conf.j2" <<'EOF'
[server]
host = {{ ansible_facts.hostname }}
port = {{ app_port }}
{% if app_debug | default(false) %}
debug = true
{% else %}
debug = false
{% endif %}
{% for feature in features %}
feature.{{ feature }} = on
{% endfor %}
EOF
printf '[app]\nlocalhost ansible_connection=local\n' > "$WORK/pb/hosts.ini"
cat > "$WORK/inv.ini" <<'EOF'
web[01:02].example.com http_port=8080

[web]
web[01:02].example.com
canary.example.com http_port=8081 track=canary

[db]
db1.example.com ansible_host=10.0.0.5 ansible_port=2222

[prod:children]
web
db

[prod:vars]
ntp_server=ntp.prod.example.com
http_port=80
backup=True

[all:vars]
ntp_server=pool.ntp.org
EOF
cat > "$WORK/inv.yml" <<'EOF'
all:
  vars:
    ntp_server: pool.ntp.org
  children:
    prod:
      vars:
        ntp_server: ntp.prod.example.com
        http_port: 80
        backup: true
      children:
        web:
          hosts:
            web[01:02].example.com:
              http_port: 8080
            canary.example.com:
              http_port: 8081
              track: canary
        db:
          hosts:
            db1.example.com:
              ansible_host: 10.0.0.5
              ansible_port: 2222
EOF

cat > "$WORK/check.py" <<'EOF'
import hashlib
import json
import os
import re
import stat
import sys

sys.path.insert(0, os.environ["CODE_DIR"])
import main


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def recap(path, host):
    stats = main.parse_recap(read(path))
    if host not in stats:
        raise SystemExit(f"no PLAY RECAP line for {host} in {path}:\n{read(path)[-1500:]}")
    return stats[host]


def tree(root):
    entries = {}
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(base, name)
            mode = os.lstat(path).st_mode
            if stat.S_ISDIR(mode):
                entries[os.path.relpath(path, root)] = ["dir", oct(stat.S_IMODE(mode)), ""]
            else:
                with open(path, "rb") as handle:
                    entries[os.path.relpath(path, root)] = ["file", oct(stat.S_IMODE(mode)), hashlib.sha1(handle.read()).hexdigest()]
    return entries


def graph_edges(text):
    edges, stack = set(), []
    for line in text.splitlines():
        if not line.strip() or line.startswith("["):
            continue
        depth = line.count("  |")
        name = line.split("--", 1)[1] if "--" in line else line
        stack = stack[:depth]
        node = ("group", name[1:].rstrip(":")) if name.startswith("@") else ("host", name)
        if stack:
            edges.add((stack[-1], node[0], node[1]))
        if node[0] == "group":
            stack.append(node[1])
    return edges


def memberships(listing):
    pairs = set()
    for group, body in listing.items():
        if group != "_meta":
            pairs |= {(group, "host", h) for h in body.get("hosts", [])}
            pairs |= {(group, "child", c) for c in body.get("children", [])}
    return pairs


def adhoc_facts(path):
    text = read(path)
    match = re.search(r"^(\S+) \| (SUCCESS|CHANGED) => ", text, re.M)
    if not match:
        raise SystemExit(f"no ad hoc result in {path}:\n{text[-1500:]}")
    data, _ = json.JSONDecoder().raw_decode(text[match.end():])
    return data["ansible_facts"]


command, args = sys.argv[1], sys.argv[2:]
if command == "recap":
    stats = recap(args[0], args[1])
    for expectation in args[2:]:
        key, value = expectation.split("=")
        if stats[key] != int(value):
            raise SystemExit(f"{args[0]}: expected {key}={value}, got {stats}")
    print("  recap", json.dumps(stats))
elif command == "same-recap":
    left, right = recap(args[0], args[2]), recap(args[1], args[2])
    if left != right:
        raise SystemExit(f"recaps differ\n  {args[0]}: {left}\n  {args[1]}: {right}")
    print("  same recap in both:", json.dumps(left))
elif command == "same-tree":
    left, right = tree(args[0]), tree(args[1])
    if left != right:
        keys = sorted(set(left) | set(right))
        raise SystemExit("trees differ:\n" + "\n".join(f"  {k}: {left.get(k)} vs {right.get(k)}" for k in keys if left.get(k) != right.get(k)))
    print(f"  same {len(left)} paths, kinds, modes and contents")
elif command == "same-inventory":
    real, mini = json.loads(read(args[0])), json.loads(read(args[1]))
    if real["_meta"]["hostvars"] != mini["_meta"]["hostvars"]:
        raise SystemExit(f"hostvars differ\n  ansible-inventory: {json.dumps(real['_meta']['hostvars'], sort_keys=True)}\n  miniansible:       {json.dumps(mini['_meta']['hostvars'], sort_keys=True)}")
    if memberships(real) != memberships(mini):
        raise SystemExit(f"groups differ: only ansible {sorted(memberships(real) - memberships(mini))}, only mini {sorted(memberships(mini) - memberships(real))}")
    print(f"  same {len(real['_meta']['hostvars'])} hosts' merged vars and {len(memberships(real))} group memberships")
elif command == "same-graph":
    left, right = graph_edges(read(args[0])), graph_edges(read(args[1]))
    if left != right:
        raise SystemExit(f"graphs differ: only ansible {sorted(left - right)}, only mini {sorted(right - left)}")
    print(f"  same graph: {len(left)} edges")
elif command == "same-facts":
    real, mini = adhoc_facts(args[0]), adhoc_facts(args[1])
    wanted = {key: real.get(key) for key in mini}
    if wanted != mini:
        raise SystemExit(f"facts differ\n  ansible:     {wanted}\n  miniansible: {mini}")
    print("  same facts:", json.dumps(mini, sort_keys=True))
else:
    raise SystemExit(f"unknown check {command}")
EOF
check() { python3 "$WORK/check.py" "$@"; }

echo "▸ miniansible: inventory graph, list and the source of every variable"
python3 "$MINI" inventory -i "$WORK/inv.ini" --graph > "$WORK/mini-graph.txt" || fail "inventory --graph"
head -4 "$WORK/mini-graph.txt"
python3 "$MINI" inventory -i "$WORK/inv.ini" --host db1.example.com --explain > "$WORK/explain.json" || fail "inventory --explain"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["ntp_server"]["source"]=="group_vars (prod)" and d["ntp_server"]["overrides"]==["group_vars (all)"], d; print("  ntp_server from", d["ntp_server"]["source"], "overriding", d["ntp_server"]["overrides"])' "$WORK/explain.json" || fail "explain: $(cat "$WORK/explain.json")"

echo "▸ miniansible -c local: first run changes, second run reports changed=0"
python3 "$MINI" playbook -i "$WORK/pb/hosts.ini" "$WORK/pb/site.yml" -e "root=$WORK/mini" > "$WORK/mini-run1.txt" 2>&1 || fail "miniansible run 1: $(tail -30 "$WORK/mini-run1.txt")"
FAMILY="$(python3 "$MINI" adhoc localhost -i "$WORK/pb/hosts.ini" -m setup -a filter=ansible_os_family --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["plays"][0]["tasks"][0]["hosts"]["localhost"]["ansible_facts"]["ansible_os_family"])')"
echo "  os_family here: $FAMILY"
check recap "$WORK/mini-run1.txt" localhost failed=0 || fail "miniansible run 1 recap"
grep -q "RUNNING HANDLER \[reload app\]" "$WORK/mini-run1.txt" || fail "the handler should run once after the first run: $(cat "$WORK/mini-run1.txt")"
[[ "$(cat "$WORK/mini/srv/app/reloads")" == "reloaded" ]] || fail "two notifications should run the handler once, got: $(cat "$WORK/mini/srv/app/reloads")"
grep -qx "port = 8080" "$WORK/mini/srv/app/app.conf" || fail "template output: $(cat "$WORK/mini/srv/app/app.conf")"
python3 "$MINI" playbook -i "$WORK/pb/hosts.ini" "$WORK/pb/site.yml" -e "root=$WORK/mini" --json > "$WORK/mini-run2.json" || fail "miniansible run 2"
python3 -c 'import json,sys; s=json.load(open(sys.argv[1]))["stats"]["localhost"]; assert s["changed"]==0 and s["failed"]==0, s; print("  second run:", s)' "$WORK/mini-run2.json" || fail "second run should change nothing: $(cat "$WORK/mini-run2.json")"
python3 "$MINI" playbook -i "$WORK/pb/hosts.ini" "$WORK/pb/site.yml" -e "root=$WORK/mini" > "$WORK/mini-run2.txt" 2>&1 || fail "miniansible run 2 text"

echo "▸ install ansible-core $ANSIBLE_CORE_VERSION into a venv (wheel sha256 checked)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' || fail "ansible-core $ANSIBLE_CORE_VERSION needs Python 3.12 or newer on the controller; this is $(python3 --version)"
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" download -q --disable-pip-version-check --no-deps --only-binary=:all: "ansible-core==$ANSIBLE_CORE_VERSION" -d "$WORK/dl" || fail "pip download ansible-core"
echo "$ANSIBLE_CORE_WHEEL_SHA256  $WORK/dl/ansible_core-$ANSIBLE_CORE_VERSION-py3-none-any.whl" | sha256sum -c - >/dev/null || fail "ansible-core wheel checksum mismatch"
"$WORK/venv/bin/pip" install -q --disable-pip-version-check "$WORK/dl/ansible_core-$ANSIBLE_CORE_VERSION-py3-none-any.whl" || fail "pip install ansible-core"
export LC_ALL=C.UTF-8 LANG=C.UTF-8
export ANSIBLE_HOME="$WORK/ansible-home" ANSIBLE_LOCAL_TEMP="$WORK/ansible-local-tmp" ANSIBLE_NOCOWS=1 ANSIBLE_NOCOLOR=1 ANSIBLE_FORCE_COLOR=0 ANSIBLE_RETRY_FILES_ENABLED=0 ANSIBLE_DEPRECATION_WARNINGS=0
PYI="ansible_python_interpreter=$WORK/venv/bin/python3"
"$WORK/venv/bin/ansible" --version > "$WORK/ansible-version.txt"
head -1 "$WORK/ansible-version.txt"
grep -q "core $ANSIBLE_CORE_VERSION" "$WORK/ansible-version.txt" || fail "unexpected ansible version: $(head -1 "$WORK/ansible-version.txt")"
ap() { ANSIBLE_REMOTE_TMP="$WORK/remote-tmp" "$WORK/venv/bin/ansible-playbook" "$@"; }

echo "▸ ansible-playbook -c local: same playbook, same files, same recap, then changed=0"
ap -i "$WORK/pb/hosts.ini" "$WORK/pb/site.yml" -e "root=$WORK/real" -e "$PYI" > "$WORK/real-run1.txt" 2> "$WORK/real-run1.err" || fail "ansible-playbook run 1: $(tail -30 "$WORK/real-run1.txt") $(tail -10 "$WORK/real-run1.err")"
check same-recap "$WORK/real-run1.txt" "$WORK/mini-run1.txt" localhost || fail "run 1 recaps"
check same-tree "$WORK/real" "$WORK/mini" || fail "run 1 trees"
ap -i "$WORK/pb/hosts.ini" "$WORK/pb/site.yml" -e "root=$WORK/real" -e "$PYI" > "$WORK/real-run2.txt" 2> "$WORK/real-run2.err" || fail "ansible-playbook run 2: $(tail -30 "$WORK/real-run2.txt")"
check recap "$WORK/real-run2.txt" localhost changed=0 failed=0 || fail "ansible run 2 should report changed=0"
check same-recap "$WORK/real-run2.txt" "$WORK/mini-run2.txt" localhost || fail "run 2 recaps"

echo "▸ --check --diff on drifted targets: both report the same changes and apply none"
for root in "$WORK/real" "$WORK/mini"; do
  sed -i 's/^PORT=.*/PORT=9999/' "$root/srv/app/app.env"
  rm "$root/srv/app/motd"
done
ap -i "$WORK/pb/hosts.ini" "$WORK/pb/site.yml" -e "root=$WORK/real" -e "$PYI" --check --diff > "$WORK/real-check.txt" 2> "$WORK/real-check.err" || fail "ansible-playbook --check: $(tail -30 "$WORK/real-check.txt")"
python3 "$MINI" playbook -i "$WORK/pb/hosts.ini" "$WORK/pb/site.yml" -e "root=$WORK/mini" --check --diff > "$WORK/mini-check.txt" 2>&1 || fail "miniansible --check: $(tail -30 "$WORK/mini-check.txt")"
check recap "$WORK/mini-check.txt" localhost changed=2 || fail "check mode should report the two drifted files"
check same-recap "$WORK/real-check.txt" "$WORK/mini-check.txt" localhost || fail "check-mode recaps"
for out in "$WORK/real-check.txt" "$WORK/mini-check.txt"; do
  for token in "-PORT=9999" "+PORT=8080" "+managed by configuration management"; do
    grep -qF -- "$token" "$out" || fail "$out: --diff should show $token: $(cat "$out")"
  done
done
for root in "$WORK/real" "$WORK/mini"; do
  grep -qx "PORT=9999" "$root/srv/app/app.env" || fail "--check must not edit $root/srv/app/app.env"
  [[ ! -e "$root/srv/app/motd" ]] || fail "--check must not create $root/srv/app/motd"
done
echo "  both: 2 changes reported, app.env still PORT=9999, motd still absent"
ap -i "$WORK/pb/hosts.ini" "$WORK/pb/site.yml" -e "root=$WORK/real" -e "$PYI" > "$WORK/real-run3.txt" 2>&1 || fail "ansible-playbook repair run"
python3 "$MINI" playbook -i "$WORK/pb/hosts.ini" "$WORK/pb/site.yml" -e "root=$WORK/mini" > "$WORK/mini-run3.txt" 2>&1 || fail "miniansible repair run"
check same-recap "$WORK/real-run3.txt" "$WORK/mini-run3.txt" localhost || fail "repair recaps"
check same-tree "$WORK/real" "$WORK/mini" || fail "trees after repair"

echo "▸ ansible-inventory and miniansible inventory agree on groups and merged vars (INI and YAML)"
for inv in "$WORK/inv.ini" "$WORK/inv.yml"; do
  "$WORK/venv/bin/ansible-inventory" -i "$inv" --list > "$WORK/real-list.json" 2> "$WORK/real-list.err" || fail "ansible-inventory --list $inv: $(cat "$WORK/real-list.err")"
  python3 "$MINI" inventory -i "$inv" --list > "$WORK/mini-list.json" || fail "miniansible inventory --list $inv"
  check same-inventory "$WORK/real-list.json" "$WORK/mini-list.json" || fail "inventory $inv"
  "$WORK/venv/bin/ansible-inventory" -i "$inv" --graph > "$WORK/real-graph.txt" 2>/dev/null || fail "ansible-inventory --graph $inv"
  python3 "$MINI" inventory -i "$inv" --graph > "$WORK/mini-graph.txt" || fail "miniansible inventory --graph $inv"
  check same-graph "$WORK/real-graph.txt" "$WORK/mini-graph.txt" || fail "graph $inv: $(cat "$WORK/real-graph.txt")"
done

echo "▸ ad hoc: ansible -m setup and ansible -i localhost, -c local -m ping"
"$WORK/venv/bin/ansible" app -i "$WORK/pb/hosts.ini" -e "$PYI" -m ansible.builtin.setup -a 'filter=ansible_hostname,ansible_os_family,ansible_distribution' > "$WORK/real-facts.txt" 2>/dev/null || fail "ansible -m setup: $(cat "$WORK/real-facts.txt")"
python3 "$MINI" adhoc app -i "$WORK/pb/hosts.ini" -m setup -a 'filter=ansible_hostname,ansible_os_family,ansible_distribution' > "$WORK/mini-facts.txt" || fail "miniansible adhoc -m setup"
check same-facts "$WORK/real-facts.txt" "$WORK/mini-facts.txt" || fail "facts"
ANSIBLE_REMOTE_TMP="$WORK/remote-tmp" "$WORK/venv/bin/ansible" all -i localhost, -c local -e "$PYI" -m ansible.builtin.ping > "$WORK/real-ping.txt" 2>/dev/null || fail "ansible ping: $(cat "$WORK/real-ping.txt")"
python3 "$MINI" adhoc all -i localhost, -c local -m ping > "$WORK/mini-ping.txt" || fail "miniansible ping"
for out in "$WORK/real-ping.txt" "$WORK/mini-ping.txt"; do
  grep -q '^localhost | SUCCESS' "$out" && grep -q '"ping": "pong"' "$out" || fail "$out should be a SUCCESS with pong: $(cat "$out")"
done
echo "  both: localhost | SUCCESS, pong"

echo "▸ ansible-lint $ANSIBLE_LINT_VERSION: the idempotence rules pass on site.yml and fire on a script-style playbook"
if "$WORK/venv/bin/pip" install -q --disable-pip-version-check "ansible-lint==$ANSIBLE_LINT_VERSION" "ansible-core==$ANSIBLE_CORE_VERSION" > "$WORK/lint-install.txt" 2>&1; then
  mkdir -p "$WORK/lint/playbooks"
  cp -R "$WORK/pb/." "$WORK/lint/playbooks/"
  cat > "$WORK/lint/playbooks/bad.yml" <<'EOF'
- name: Provision like a shell script
  hosts: all
  tasks:
    - name: Append the port
      ansible.builtin.shell: echo PORT=8080 >> /tmp/app.env
    - name: App directory with a decimal mode
      ansible.builtin.file:
        path: /tmp/app
        state: directory
        mode: 644
EOF
  (cd "$WORK/lint" && "$WORK/venv/bin/ansible-lint" --offline --nocolor playbooks/bad.yml > "$WORK/lint-bad.txt" 2>&1) && fail "ansible-lint should reject bad.yml: $(cat "$WORK/lint-bad.txt")"
  for rule in no-changed-when risky-octal; do
    grep -q "$rule" "$WORK/lint-bad.txt" || fail "ansible-lint should report $rule on bad.yml: $(tail -30 "$WORK/lint-bad.txt")"
  done
  (cd "$WORK/lint" && "$WORK/venv/bin/ansible-lint" --offline --nocolor playbooks/site.yml > "$WORK/lint-site.txt" 2>&1) || true
  for rule in no-changed-when risky-octal risky-file-permissions; do
    if grep -qw "$rule" "$WORK/lint-site.txt"; then fail "site.yml should pass $rule: $(tail -30 "$WORK/lint-site.txt")"; fi
  done
  echo "  bad.yml: no-changed-when and risky-octal reported; site.yml: none of no-changed-when, risky-octal, risky-file-permissions"
else
  echo "  (ansible-lint $ANSIBLE_LINT_VERSION could not be installed; lint step skipped: $(tail -1 "$WORK/lint-install.txt"))"
fi

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "▸ miniansible over docker exec: a throwaway python:3.12-alpine container"
  docker rm -f "$BOX" >/dev/null 2>&1 || true
  docker run -d --name "$BOX" --hostname cc-box python:3.12-alpine sleep 600 >/dev/null || fail "docker run $BOX"
  printf '[app]\n%s ansible_connection=community.docker.docker ansible_python_interpreter=/usr/local/bin/python3\n' "$BOX" > "$WORK/box.ini"
  python3 "$MINI" playbook -i "$WORK/box.ini" "$WORK/pb/site.yml" -e root=/srv/mini > "$WORK/box-mini1.txt" 2>&1 || fail "miniansible docker run 1: $(tail -30 "$WORK/box-mini1.txt")"
  check recap "$WORK/box-mini1.txt" "$BOX" failed=0 skipped=1 || fail "docker run 1: the Debian-only task should be skipped on Alpine"
  docker exec "$BOX" cat /srv/mini/srv/app/app.conf > "$WORK/box-app.conf"
  grep -qx "host = cc-box" "$WORK/box-app.conf" || fail "facts inside the container: $(cat "$WORK/box-app.conf")"
  python3 "$MINI" playbook -i "$WORK/box.ini" "$WORK/pb/site.yml" -e root=/srv/mini > "$WORK/box-mini2.txt" 2>&1 || fail "miniansible docker run 2"
  check recap "$WORK/box-mini2.txt" "$BOX" changed=0 failed=0 || fail "docker run 2 should change nothing"
  if timeout 180 "$WORK/venv/bin/ansible-galaxy" collection install "community.docker:==$COMMUNITY_DOCKER_VERSION" -p "$WORK/collections" > "$WORK/galaxy.txt" 2>&1; then
    echo "▸ ansible-playbook over community.docker.docker against the same container"
    export ANSIBLE_COLLECTIONS_PATH="$WORK/collections"
    "$WORK/venv/bin/ansible-playbook" -i "$WORK/box.ini" "$WORK/pb/site.yml" -e root=/srv/real > "$WORK/box-real1.txt" 2>&1 || fail "ansible docker run 1: $(tail -30 "$WORK/box-real1.txt")"
    check same-recap "$WORK/box-real1.txt" "$WORK/box-mini1.txt" "$BOX" || fail "docker run 1 recaps"
    "$WORK/venv/bin/ansible-playbook" -i "$WORK/box.ini" "$WORK/pb/site.yml" -e root=/srv/real > "$WORK/box-real2.txt" 2>&1 || fail "ansible docker run 2"
    check same-recap "$WORK/box-real2.txt" "$WORK/box-mini2.txt" "$BOX" || fail "docker run 2 recaps"
    LIST='cd "$1" && find . | sort | while read -r p; do if [ -f "$p" ]; then printf "%s %s %s\n" "$p" "$(stat -c %a "$p")" "$(sha1sum "$p" | cut -d " " -f 1)"; else printf "%s %s dir\n" "$p" "$(stat -c %a "$p")"; fi; done'
    docker exec "$BOX" sh -c "$LIST" sh /srv/mini > "$WORK/box-mini-tree.txt"
    docker exec "$BOX" sh -c "$LIST" sh /srv/real > "$WORK/box-real-tree.txt"
    cmp -s "$WORK/box-mini-tree.txt" "$WORK/box-real-tree.txt" || fail "container trees differ: $(diff "$WORK/box-real-tree.txt" "$WORK/box-mini-tree.txt" || true)"
    echo "  same $(wc -l < "$WORK/box-mini-tree.txt") paths, modes and checksums inside the container"
  else
    echo "  (community.docker $COMMUNITY_DOCKER_VERSION could not be installed from Galaxy; ansible docker comparison skipped: $(tail -1 "$WORK/galaxy.txt"))"
  fi
else
  echo "  (docker not available; container steps skipped)"
fi

echo "SMOKE OK: iac/ansible-config"
