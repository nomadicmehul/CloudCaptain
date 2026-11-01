#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux (/proc/PID/status)"
WORK="$(mktemp -d)"
POD_CREATED=0
cleanup() {
  rm -rf "$WORK"
  docker rm -f cc-smoke-caps >/dev/null 2>&1 || true
  if [[ "$POD_CREATED" == "1" ]]; then kubectl delete pod cc-smoke-caps --ignore-not-found --wait=false >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT
names_of() { python3 -c 'import json, sys; print(",".join(json.load(sys.stdin)["names"]))'; }

echo "▸ decode this shell's CapEff and compare with capsh"
EFF=$(awk '$1 == "CapEff:" {print $2}' /proc/self/status)
python3 code/main.py "$EFF" | tee "$WORK/eff.txt"
OURS=$(python3 code/main.py "$EFF" --json | names_of | tr ',' '\n' | tr 'A-Z' 'a-z' | sort)
if command -v capsh >/dev/null; then
  THEIRS=$(capsh --decode="$EFF" | cut -d= -f2 | tr ',' '\n' | sed '/^$/d' | sort)
  [[ "$OURS" == "$THEIRS" ]] || fail "capsh and capcheck disagree on $EFF: [$THEIRS] vs [$OURS]"
  echo "  capsh --decode agrees"
else
  echo "  (capsh not installed; comparison skipped)"
fi

echo "▸ --explain self: identity, the five sets, NoNewPrivs, Seccomp"
python3 code/main.py --explain self | tee "$WORK/self.txt"
grep -q "^NoNewPrivs" "$WORK/self.txt" || fail "--explain should print NoNewPrivs"
grep -qE "^pid [0-9]+ \(python[0-9.]*\): uid $(id -u)" "$WORK/self.txt" || fail "--explain self should name uid $(id -u)"

echo "▸ main.sh: the same by hand with grep, bash arithmetic, capsh, getpcaps and stat"
bash code/main.sh self /usr/bin/passwd /tmp | tee "$WORK/sh.txt"
grep -q "CapEff" "$WORK/sh.txt" || fail "main.sh should print CapEff"
grep -qE "^  CapEff +[0-9]+ of 41" "$WORK/sh.txt" || fail "main.sh should decode CapEff by hand"

echo "▸ --perms: a setuid binary, the sticky /tmp, and what CAP_DAC_OVERRIDE changes"
MODE=$(stat -c '%A' /usr/bin/passwd)
python3 code/main.py --perms="$MODE" --owner root --group root --user alice --groups alice | tee "$WORK/passwd.txt"
if [[ "$MODE" == *s* ]]; then grep -q "runs with effective uid root" "$WORK/passwd.txt" || fail "setuid note missing for $MODE"; fi
python3 code/main.py --perms="$(stat -c '%A' /tmp)" --owner root --group root --user alice --groups alice --json \
  | python3 -c 'import json, sys; d = json.load(sys.stdin); assert d["write"] and d["traverse"] and any("sticky" in n for n in d["notes"]), d' || fail "sticky /tmp"
python3 code/main.py --perms 0600 --owner root --group root --user app --groups app --json \
  | python3 -c 'import json, sys; d = json.load(sys.stdin); assert not d["read"] and not d["write"], d' || fail "0600 as other must be unreadable"
python3 code/main.py --perms 0600 --owner root --group root --user app --groups app --caps docker-default --json \
  | python3 -c 'import json, sys; d = json.load(sys.stdin); assert d["read"] and d["write"] and not d["execute"], d' || fail "CAP_DAC_OVERRIDE must bypass read and write"
chmod 0640 "$WORK/eff.txt"
python3 code/main.py --path "$WORK/eff.txt" --json \
  | python3 -c 'import json, os, pwd, sys; d = json.load(sys.stdin); assert d["owner"] == pwd.getpwuid(os.getuid()).pw_name and d["read"] and d["write"] and not d["execute"], d' || fail "--path on a real file"
echo "  passwd $MODE, /tmp $(stat -c '%A' /tmp), 0600 with and without CAP_DAC_OVERRIDE, a real 0640 file: as expected"

if command -v getpcaps >/dev/null; then
  echo "▸ getpcaps on this shell"
  getpcaps $$ || true
else
  echo "  (getpcaps not installed; skipped)"
fi
if command -v getcap >/dev/null; then
  echo "▸ file capabilities installed on this host (getcap -r /usr, first five)"
  getcap -r /usr 2>/dev/null | head -5 || true
fi

if command -v docker >/dev/null; then
  cat > "$WORK/bind80.py" <<'PY'
import os
import socket

s = socket.socket()
s.bind(("0.0.0.0", 80))
eff = [line.split()[1] for line in open("/proc/self/status") if line.startswith("CapEff:")][0]
print(f"bound :80 as uid {os.getuid()} with CapEff {eff}")
PY
  cat > "$WORK/filecap.sh" <<'FC'
set -e
apk add -q libcap
PY="$(readlink -f "$(command -v python3)")"
setcap cap_net_bind_service=+ep "$PY"
getcap "$PY"
exec python3 -c "import os; os.setgroups([]); os.setgid(1000); os.setuid(1000); os.execv('$PY', ['python3', '/bind80.py'])"
FC
  chmod 0644 "$WORK/bind80.py" "$WORK/filecap.sh"

  echo "▸ Use It: the Docker default set, decoded"
  DEF=$(docker run --rm alpine:3.22 grep CapEff /proc/self/status | awk '{print $2}')
  python3 code/main.py "$DEF" | tee "$WORK/def.txt"
  DEFNAMES=$(python3 code/main.py "$DEF" --json | names_of)
  [[ "$DEFNAMES" == *CAP_NET_BIND_SERVICE* && "$DEFNAMES" == *CAP_CHOWN* && "$DEFNAMES" != *CAP_SYS_ADMIN* ]] || fail "unexpected default set: $DEFNAMES"

  echo "▸ --cap-drop ALL leaves zero bits; --cap-add NET_BIND_SERVICE adds exactly one"
  DROP=$(docker run --rm --cap-drop ALL alpine:3.22 grep CapEff /proc/self/status | awk '{print $2}')
  [[ $((16#$DROP)) -eq 0 ]] || fail "--cap-drop ALL should give CapEff 0, got $DROP"
  ONE=$(docker run --rm --cap-drop ALL --cap-add NET_BIND_SERVICE alpine:3.22 grep CapEff /proc/self/status | awk '{print $2}')
  [[ "$(python3 code/main.py "$ONE" --json | names_of)" == "CAP_NET_BIND_SERVICE" ]] || fail "expected exactly CAP_NET_BIND_SERVICE, got $ONE"
  python3 code/main.py --diff "$DEF" "$ONE" | tee "$WORK/diff.txt"
  grep -q "kept (1): CAP_NET_BIND_SERVICE" "$WORK/diff.txt" || fail "--diff should keep only CAP_NET_BIND_SERVICE"
  echo "  default $DEF  drop ALL $DROP  drop ALL + NET_BIND_SERVICE $ONE"

  echo "▸ --explain a status captured inside a hardened container (--user 1000, drop ALL, add NET_BIND_SERVICE, no-new-privileges)"
  docker run --rm --user 1000 --cap-drop ALL --cap-add NET_BIND_SERVICE --security-opt no-new-privileges alpine:3.22 cat /proc/self/status > "$WORK/container.status"
  python3 code/main.py --explain "$WORK/container.status" | tee "$WORK/container.txt"
  grep -q "unprivileged uid 1000" "$WORK/container.txt" || fail "expected an unprivileged uid 1000 verdict"
  grep -q "NoNewPrivs   1" "$WORK/container.txt" || fail "expected NoNewPrivs 1"
  grep -q "effective    0x0000000000000000" "$WORK/container.txt" || fail "a non-root user gets no effective capabilities from --cap-add"
  grep -q "bounding     0x0000000000000400" "$WORK/container.txt" || fail "the bit should be in the bounding set only"

  echo "▸ binding :80: uid 0 needs the bit, uid 1000 needs it as a file capability"
  docker run --rm --cap-drop ALL --cap-add NET_BIND_SERVICE -v "$WORK/bind80.py:/bind80.py:ro" python:3.12-alpine python3 /bind80.py | tee "$WORK/bind-root.txt"
  grep -q "bound :80 as uid 0 with CapEff 0000000000000400" "$WORK/bind-root.txt" || fail "uid 0 with CAP_NET_BIND_SERVICE should bind :80"
  set +e
  OUT=$(docker run --rm --cap-drop ALL -v "$WORK/bind80.py:/bind80.py:ro" python:3.12-alpine python3 /bind80.py 2>&1); CODE=$?
  set -e
  { [[ "$CODE" != "0" ]] && grep -q "Permission denied" <<<"$OUT"; } || fail "uid 0 without CAP_NET_BIND_SERVICE should get EACCES, got ($CODE): $OUT"
  echo "  uid 0 with --cap-drop ALL: $(grep -o 'Errno 13.*' <<<"$OUT")"
  set +e
  OUT=$(docker run --rm --user 1000 --cap-add NET_BIND_SERVICE -v "$WORK/bind80.py:/bind80.py:ro" python:3.12-alpine python3 /bind80.py 2>&1); CODE=$?
  set -e
  { [[ "$CODE" != "0" ]] && grep -q "Permission denied" <<<"$OUT"; } || fail "--user 1000 with --cap-add should still fail (non-root gets only the bounding set), got ($CODE): $OUT"
  echo "  uid 1000 with --cap-add NET_BIND_SERVICE: still $(grep -o 'Errno 13.*' <<<"$OUT")"
  docker run --rm -v "$WORK/bind80.py:/bind80.py:ro" -v "$WORK/filecap.sh:/filecap.sh:ro" python:3.12-alpine sh /filecap.sh | tee "$WORK/filecap.txt"
  grep -q "bound :80 as uid 1000 with CapEff 0000000000000400" "$WORK/filecap.txt" || fail "a file capability should give uid 1000 exactly CAP_NET_BIND_SERVICE"
  set +e
  OUT=$(docker run --rm --security-opt no-new-privileges -v "$WORK/bind80.py:/bind80.py:ro" -v "$WORK/filecap.sh:/filecap.sh:ro" python:3.12-alpine sh /filecap.sh 2>&1); CODE=$?
  set -e
  { [[ "$CODE" != "0" ]] && grep -q "Permission denied" <<<"$OUT"; } || fail "no-new-privileges should make execve ignore the file capability, got ($CODE): $OUT"
  echo "  with --security-opt no-new-privileges the file capability is ignored: $(grep -o 'Errno 13.*' <<<"$OUT")"

  echo "▸ docker inspect shows what was asked for; /proc/1/status shows what was granted"
  docker run -d --name cc-smoke-caps --cap-drop ALL --cap-add NET_BIND_SERVICE --security-opt no-new-privileges alpine:3.22 sleep 60 >/dev/null
  docker inspect -f '  CapAdd={{.HostConfig.CapAdd}} CapDrop={{.HostConfig.CapDrop}} SecurityOpt={{.HostConfig.SecurityOpt}} Privileged={{.HostConfig.Privileged}}' cc-smoke-caps
  docker exec cc-smoke-caps grep -E 'CapEff|NoNewPrivs' /proc/1/status | sed 's/^/  /'
  [[ "$(docker exec cc-smoke-caps awk '$1 == "CapEff:" {print $2}' /proc/1/status)" == "$ONE" ]] || fail "PID 1 in the container should hold $ONE"
  docker rm -f cc-smoke-caps >/dev/null
else
  echo "  (docker not available; Use It skipped)"
fi

if command -v kubectl >/dev/null && [[ "$(kubectl config current-context 2>/dev/null || true)" == "kind-cc-smoke" ]]; then
  echo "▸ Kubernetes: drop ALL, allowPrivilegeEscalation false, runAsNonRoot, RuntimeDefault, read back from /proc/1/status"
  kubectl apply -f - <<'YAML' >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: cc-smoke-caps
spec:
  restartPolicy: Never
  containers:
    - name: app
      image: alpine:3.22
      command: ["sleep", "300"]
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
        seccompProfile:
          type: RuntimeDefault
YAML
  POD_CREATED=1
  kubectl wait --for=condition=Ready pod/cc-smoke-caps --timeout=120s >/dev/null
  kubectl exec cc-smoke-caps -- cat /proc/1/status > "$WORK/pod.status"
  python3 code/main.py --explain "$WORK/pod.status" | tee "$WORK/pod.txt"
  grep -q "effective    0x0000000000000000" "$WORK/pod.txt" || fail "drop ALL should leave no effective capabilities in the pod"
  grep -q "NoNewPrivs   1" "$WORK/pod.txt" || fail "allowPrivilegeEscalation: false should set NoNewPrivs"
  grep -q "Seccomp      2" "$WORK/pod.txt" || fail "RuntimeDefault should install a seccomp filter"
  kubectl delete pod cc-smoke-caps --wait=false >/dev/null
  POD_CREATED=0
else
  echo "  (no kind-cc-smoke context; Kubernetes check skipped)"
fi

echo "SMOKE OK: linux/permissions-capabilities"
