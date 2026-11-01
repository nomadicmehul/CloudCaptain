#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux (seccomp, Docker)"
WORK="$(mktemp -d)"
NS_CREATED=0
cleanup() {
  rm -rf "$WORK"
  docker rm -f cc-smoke-default cc-smoke-hardened >/dev/null 2>&1 || true
  if [[ "$NS_CREATED" == "1" ]]; then kubectl delete namespace cc-smoke-psa --ignore-not-found --timeout=120s >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT
EXCERPT=code/fixtures/seccomp-docker-default-excerpt.json
jsonq() { python3 -c "import json, sys; d = json.load(sys.stdin); print($1)"; }

echo "▸ seccomp: resolve syscalls under docker-default, then with CAP_SYS_ADMIN added"
python3 code/main.py seccomp "$EXCERPT" --check mkdir,mount,ptrace,clone,bpf | tee "$WORK/seccomp.txt" | tail -8
grep -qE '^  mkdir +ALLOW ' "$WORK/seccomp.txt" || fail "mkdir should be allowed by the first rule"
grep -qE '^  mount +ERRNO .*allowed with CAP_SYS_ADMIN' "$WORK/seccomp.txt" || fail "mount should be denied and name CAP_SYS_ADMIN"
grep -qE '^  clone +ALLOW\* ' "$WORK/seccomp.txt" || fail "clone should be conditional on its flags"
python3 code/main.py seccomp "$EXCERPT" --caps docker-default,SYS_ADMIN --check mount --json | jsonq 'd["checks"][0]["action"]' | grep -qx SCMP_ACT_ALLOW || fail "CAP_SYS_ADMIN should unlock mount"

echo "▸ seccomp-diff, and generate from the fixture trace"
python3 code/main.py seccomp-diff "$EXCERPT" code/fixtures/seccomp-tiny.json --json | jsonq '"mkdir" in d["only_a"] and not d["only_b"]' | grep -qx True || fail "diff should list mkdir only in the excerpt"
python3 code/main.py generate code/fixtures/strace-python-print.log --out "$WORK/fixture-profile.json" | tee "$WORK/gen.txt"
grep -q "32 from the trace + 32 always needed = 64 allowed" "$WORK/gen.txt" || fail "generate summary"
python3 code/main.py seccomp "$WORK/fixture-profile.json" --caps none --check mkdir,execve --json | jsonq '[c["action"] for c in d["checks"]]' | grep -qx "\['SCMP_ACT_ERRNO', 'SCMP_ACT_ALLOW'\]" || fail "generated profile should deny mkdir and allow execve"

echo "▸ inspect and k8s on the fixtures"
python3 code/main.py inspect code/fixtures/inspect-default.json --json | jsonq 'd[0]["score"]' | grep -qx 40 || fail "default fixture should score 40"
python3 code/main.py inspect code/fixtures/inspect-hardened.json --fail-under 100 >/dev/null || fail "hardened fixture should score 100"
python3 code/main.py k8s code/fixtures/pod-violating.json > "$WORK/k8s-violating.txt"
head -1 "$WORK/k8s-violating.txt"
grep -q "REJECTED by pod-security.kubernetes.io/enforce=restricted (8 violations)" "$WORK/k8s-violating.txt" || fail "violating pod verdict"
python3 code/main.py k8s code/fixtures/pod-restricted.json --fail-under 100 > "$WORK/k8s-restricted.txt" || fail "restricted pod should pass --fail-under 100"
head -1 "$WORK/k8s-restricted.txt" | grep -q "admitted under" || fail "restricted pod verdict"

if command -v docker >/dev/null; then
  echo "▸ Use It: trace python3 -c print inside the image it will run in, generate a profile, run under it"
  docker run --rm --cap-add SYS_PTRACE python:3.12-alpine sh -c 'apk add -q --no-cache strace >/dev/null 2>&1 && strace -f -qq -o /tmp/t.log python3 -c print >/dev/null && cat /tmp/t.log' > "$WORK/trace.log"
  [[ -s "$WORK/trace.log" ]] || fail "empty trace from the container"
  python3 code/main.py generate "$WORK/trace.log" --out "$WORK/profile.json" | tee "$WORK/gen-live.txt"
  OUT=$(docker run --rm --security-opt seccomp="$WORK/profile.json" python:3.12-alpine python3 -c 'print("runs under the generated profile")')
  [[ "$OUT" == "runs under the generated profile" ]] || fail "python should run under its own profile, got: $OUT"
  echo "  $OUT"
  MODE=$(docker run --rm --security-opt seccomp="$WORK/profile.json" python:3.12-alpine python3 -c 'import re; print(re.search(r"Seccomp:\s*(\d)", open("/proc/self/status").read()).group(1))')
  [[ "$MODE" == "2" ]] || fail "Seccomp mode should be 2 (filter) under the profile, got $MODE"
  set +e
  OUT=$(docker run --rm --security-opt seccomp="$WORK/profile.json" python:3.12-alpine python3 -c 'import os; os.mkdir("/tmp/x")' 2>&1); CODE=$?
  set -e
  { [[ "$CODE" != "0" ]] && grep -q "Operation not permitted" <<<"$OUT"; } || fail "mkdir should be denied with EPERM under the generated profile, got ($CODE): $OUT"
  echo "  mkdir under the profile: $(grep -o 'Errno 1.*' <<<"$OUT" | head -1)"
  docker run --rm python:3.12-alpine python3 -c 'import os; os.mkdir("/tmp/x"); print("  mkdir under the default profile: ok")' || fail "mkdir should work under the default profile"

  echo "▸ inspect: a default container against a hardened one, live through docker inspect"
  docker run -d --name cc-smoke-default alpine:3.22 sleep 300 >/dev/null
  docker run -d --name cc-smoke-hardened --user 1000 --cap-drop ALL --security-opt no-new-privileges --read-only --tmpfs /tmp --pids-limit 100 --memory 128m alpine:3.22 sleep 300 >/dev/null
  python3 code/main.py inspect cc-smoke-default > "$WORK/inspect-default.txt"
  head -1 "$WORK/inspect-default.txt"
  python3 code/main.py inspect cc-smoke-hardened > "$WORK/inspect-hardened.txt"
  head -1 "$WORK/inspect-hardened.txt"
  DEF=$(python3 code/main.py inspect cc-smoke-default --json | jsonq 'd[0]["score"]')
  HARD=$(python3 code/main.py inspect cc-smoke-hardened --json | jsonq 'd[0]["score"]')
  [[ "$HARD" -gt "$DEF" ]] || fail "hardened container should score higher ($HARD vs $DEF)"
  for check in user capabilities no-new-privileges read-only-rootfs pids-limit memory-limit; do
    grep -qE "^  FAIL +[a-z]+ +$check " "$WORK/inspect-default.txt" || fail "default container should fail $check"
    grep -qE "^  PASS +[a-z]+ +$check " "$WORK/inspect-hardened.txt" || fail "hardened container should pass $check"
  done
  docker exec cc-smoke-hardened grep -E 'CapEff|NoNewPrivs|Seccomp' /proc/1/status | sed 's/^/  /'
  docker exec cc-smoke-hardened grep -q "NoNewPrivs:.*1" /proc/1/status || fail "NoNewPrivs should be 1 in the hardened container"
  docker exec cc-smoke-hardened grep -q "CapEff:.*0000000000000000" /proc/1/status || fail "CapEff should be 0 after --cap-drop ALL"
  docker rm -f cc-smoke-default cc-smoke-hardened >/dev/null
else
  echo "  (docker not available; Use It skipped)"
fi

if command -v kubectl >/dev/null && [[ "$(kubectl config current-context 2>/dev/null || true)" == "kind-cc-smoke" ]]; then
  echo "▸ Kubernetes: Pod Security Admission in a namespace labelled enforce=restricted"
  kubectl create namespace cc-smoke-psa >/dev/null
  NS_CREATED=1
  kubectl label namespace cc-smoke-psa pod-security.kubernetes.io/enforce=restricted pod-security.kubernetes.io/warn=restricted >/dev/null
  kubectl apply -n cc-smoke-psa -f code/fixtures/pod-restricted.json >/dev/null || fail "the restricted pod should be admitted"
  set +e
  OUT=$(kubectl apply -n cc-smoke-psa -f code/fixtures/pod-violating.json 2>&1); CODE=$?
  set -e
  { [[ "$CODE" != "0" ]] && grep -q "violates PodSecurity" <<<"$OUT"; } || fail "the violating pod should be rejected at admission, got ($CODE): $OUT"
  echo "  rejected: $(grep -o 'violates PodSecurity.*' <<<"$OUT" | cut -c1-200)"
  for word in privileged "host namespaces" hostPath allowPrivilegeEscalation runAsNonRoot seccompProfile; do
    grep -q "$word" <<<"$OUT" || echo "  (admission message did not mention $word)"
  done
  kubectl wait --for=condition=Ready pod/cc-smoke-restricted -n cc-smoke-psa --timeout=120s >/dev/null
  kubectl get pod cc-smoke-restricted -n cc-smoke-psa -o json | python3 code/main.py k8s - > "$WORK/k8s-live.txt"
  head -1 "$WORK/k8s-live.txt"
  grep -q "admitted under pod-security.kubernetes.io/enforce=restricted" "$WORK/k8s-live.txt" || fail "hardencheck should admit the live restricted pod"
  kubectl exec -n cc-smoke-psa cc-smoke-restricted -- grep -E 'CapEff|NoNewPrivs|Seccomp' /proc/1/status | sed 's/^/  /'
  kubectl exec -n cc-smoke-psa cc-smoke-restricted -- grep -q "Seccomp:.*2" /proc/1/status || fail "RuntimeDefault should install a seccomp filter"
  kubectl delete namespace cc-smoke-psa --timeout=120s >/dev/null
  NS_CREATED=0
else
  echo "  (no kind-cc-smoke context; Kubernetes check skipped)"
fi

echo "SMOKE OK: containers/runtime-security"
