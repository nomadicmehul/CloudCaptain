#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
LESSON="$PWD"
F="$LESSON/code/fixtures"
M="$F/manifests"
WORK="$(mktemp -d)"
CTX="${CC_SMOKE_CONTEXT:-kind-cc-smoke}"
NS="cc-admission-policy"
HOOK_NS="cc-admission-policy-hook"
LABEL="cloudcaptain.io/lesson=security-admission-policy"
KYVERNO_VERSION="1.19.2"
VAP_SET=(good privileged no-team untrusted-registry lookalike-registry root tag-only no-limit digest-exempt)
PIDS=()
CLUSTER=""
mkdir -p "$WORK/bin" "$WORK/dl"

mini() { python3 "$LESSON/code/main.py" "$@"; }
k() { kubectl --context "$CTX" "$@"; }
sha256_of() { if command -v sha256sum >/dev/null; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi; }
wait_for() {
  local deadline=$((SECONDS + $1))
  shift
  until "$@"; do
    (( SECONDS < deadline )) || return 1
    sleep 1
  done
}
delete_labelled() {
  for kind in validatingwebhookconfigurations mutatingwebhookconfigurations validatingadmissionpolicybindings validatingadmissionpolicies mutatingadmissionpolicybindings mutatingadmissionpolicies; do
    k delete "$kind" -l "$LABEL" --ignore-not-found >/dev/null 2>&1 || true
  done
}
cleanup() {
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do
    kill "$pid" >/dev/null 2>&1 || true
    wait "$pid" 2>/dev/null || true
  done
  if [[ -n "$CLUSTER" ]]; then
    delete_labelled
    k delete namespace "$NS" "$HOOK_NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

cat > "$WORK/check.py" <<'PY'
import json, re, subprocess, sys, time

UNITS = {"": 1, "m": 0.001, "k": 1e3, "M": 1e6, "G": 1e9, "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3}


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def quantity(text):
    found = re.fullmatch(r"([0-9.]+)(m|k|M|G|Ki|Mi|Gi)?", str(text))
    if not found:
        raise SystemExit("unparsed quantity %r" % (text,))
    return float(found.group(1)) * UNITS[found.group(2) or ""]


def same_requests(a, b):
    return set(a) == set(b) and all(abs(quantity(a[key]) - quantity(b[key])) < 1e-9 for key in a)


def pod_spec(obj):
    return obj["spec"] if obj.get("kind") == "Pod" else obj["spec"]["template"]["spec"]


def requests_of(obj):
    return pod_spec(obj)["containers"][0].get("resources", {}).get("requests", {})


def mini_results(path):
    return {r["name"]: r for r in load(path)["results"]}


def expect(mini_path, expected_path, table):
    results, want = mini_results(mini_path), load(expected_path)[table]
    problems = []
    for name, row in want.items():
        got = results.get(name)
        if got is None:
            problems.append("%s: not in miniadmit's output" % name)
            continue
        if (got["allowed"], got["deny"], got["warn"]) != (row["allowed"], row["deny"], row["warn"]):
            problems.append("%s: miniadmit says allowed=%s deny=%s warn=%s, expected.json says %s" % (name, got["allowed"], got["deny"], got["warn"], row))
        if "requests" in row and not same_requests(requests_of(got["patched"]), row["requests"]):
            problems.append("%s: patched requests %s, expected %s" % (name, requests_of(got["patched"]), row["requests"]))
    if problems:
        sys.exit("\n".join(problems))
    print("  miniadmit agrees with expected.json on %d objects (%s)" % (len(want), table))


def dry_run(kubectl, namespace, path):
    proc = subprocess.run(kubectl + ["create", "--dry-run=server", "-o", "json", "-n", namespace, "-f", path], capture_output=True, text=True, timeout=90)
    lines = proc.stderr.splitlines()
    warnings = [line[len("Warning: "):] for line in lines if line.startswith("Warning: ")]
    error = " ".join(line for line in lines if line.strip() and not line.startswith("Warning: "))
    obj = json.loads(proc.stdout) if proc.returncode == 0 and proc.stdout.strip() else None
    return proc.returncode == 0, obj, warnings, error


def observe(mode, kubectl, namespace, path):
    allowed, obj, warnings, error = dry_run(kubectl, namespace, path)
    if mode == "webhook":
        deny = set(re.findall(r"(?:denied the request: |; )\[([a-z0-9][-a-z0-9.]*)\]", error)) if not allowed else set()
        warn = set(m.group(1) for m in (re.match(r"\[([a-z0-9][-a-z0-9.]*)\]", w) for w in warnings) if m)
        marker = "denied the request"
    else:
        deny = set(re.findall(r"ValidatingAdmissionPolicy '([^']+)'", error)) if not allowed else set()
        warn = set(re.findall(r"Validation failed for ValidatingAdmissionPolicy '([^']+)'", " ".join(warnings)))
        marker = "denied request"
    return {"allowed": allowed, "obj": obj, "deny": deny, "warn": warn, "warnings": warnings, "error": error, "marker": marker in error}


def compare(mode, seen, mine):
    problems = []
    if seen["allowed"] != mine["allowed"]:
        problems.append("allowed: API server %s, miniadmit %s (%s)" % (seen["allowed"], mine["allowed"], seen["error"] or seen["warnings"]))
        return problems
    if not seen["allowed"]:
        if not seen["marker"]:
            problems.append("the denial does not come from admission: %s" % seen["error"])
        elif mode == "webhook" and seen["deny"] != set(mine["deny"]):
            problems.append("denied by %s, miniadmit %s" % (sorted(seen["deny"]), mine["deny"]))
        elif mode == "vap" and not (seen["deny"] and seen["deny"] <= set(mine["deny"])):
            problems.append("first denial %s is not among miniadmit's %s" % (sorted(seen["deny"]), mine["deny"]))
        return problems
    if seen["warn"] != set(mine["warn"]):
        problems.append("warnings from %s, miniadmit %s (%s)" % (sorted(seen["warn"]), mine["warn"], seen["warnings"]))
    if mode == "webhook" and mine["mutated_by"] and not same_requests(requests_of(seen["obj"]), requests_of(mine["patched"])):
        problems.append("requests %s, miniadmit %s" % (requests_of(seen["obj"]), requests_of(mine["patched"])))
    return problems


def cluster(mode, kubectl_json, namespace, mini_path, deadline, *paths):
    kubectl, results = json.loads(kubectl_json), mini_results(mini_path)
    names = [p.rsplit("/", 1)[-1][: -len(".json")] for p in paths]
    stop = time.time() + float(deadline)
    while True:
        rows, mismatches = [], []
        for name, path in zip(names, paths):
            seen, mine = observe(mode, kubectl, namespace, path), results[name]
            problems = compare(mode, seen, mine)
            verdict = "allowed" if seen["allowed"] else "denied by " + ",".join(sorted(seen["deny"]))
            if seen["warn"]:
                verdict += ", warned by " + ",".join(sorted(seen["warn"]))
            if seen["allowed"] and mode == "webhook" and mine["mutated_by"]:
                verdict += ", requests " + json.dumps(requests_of(seen["obj"]), sort_keys=True)
            rows.append("  %-8s %-22s %s" % ("MISMATCH" if problems else "ok", name, verdict))
            mismatches += ["%s: %s" % (name, p) for p in problems]
        if not mismatches or time.time() > stop:
            break
        time.sleep(3)
    print("\n".join(rows))
    if mismatches:
        sys.exit("%d difference(s) between the API server and miniadmit:\n%s" % (len(mismatches), "\n".join(mismatches)))
    print("  all %d outcomes match miniadmit test" % len(paths))


def kyverno(report_path, mini_path, expected_path):
    text = open(report_path, encoding="utf-8").read()
    decoder, docs, position = json.JSONDecoder(), [], 0
    while True:
        position = text.find("{", position)
        if position < 0:
            break
        try:
            doc, end = decoder.raw_decode(text, position)
        except ValueError:
            position += 1
            continue
        docs.append(doc)
        position = end
    results = [r for d in docs if isinstance(d, dict) for r in d.get("results") or []]
    if not results:
        sys.exit("no policy report results in the kyverno output:\n" + text[:3000])
    observed = {}
    for result in results:
        for resource in result.get("resources") or result.get("subjects") or []:
            observed.setdefault(resource.get("name"), {}).setdefault(result.get("result"), set()).add(result.get("policy"))
    want, mine = load(expected_path)["kyverno"], mini_results(mini_path)
    policies = set(want["policies"])
    problems = []
    for name in want["manifests"]:
        got = observed.get(name, {})
        fails, warns = got.get("fail", set()), got.get("warn", set())
        want_fail, want_warn = set(mine[name]["deny"]) & policies, set(mine[name]["warn"]) & policies
        evaluated = set().union(*got.values()) if got else set()
        ok = fails == want_fail and warns == want_warn and not got.get("error") and evaluated == policies
        print("  %-8s %-22s kyverno fail=%s warn=%s | miniadmit deny=%s warn=%s" % ("ok" if ok else "MISMATCH", name, sorted(fails), sorted(warns), sorted(want_fail), sorted(want_warn)))
        if not ok:
            problems.append("%s: kyverno %s, miniadmit deny=%s warn=%s" % (name, {k: sorted(v) for k, v in got.items()}, sorted(want_fail), sorted(want_warn)))
    if problems:
        sys.exit("\n".join(problems))
    print("  kyverno and miniadmit agree on %d manifests x %d policies" % (len(want["manifests"]), len(policies)))


if __name__ == "__main__":
    command, args = sys.argv[1], sys.argv[2:]
    {"expect": expect, "cluster": cluster, "kyverno": kyverno}[command](*args)
PY

command -v openssl >/dev/null || fail "openssl is required"
command -v curl >/dev/null || fail "curl is required"

echo "▸ miniadmit test: the webhook policy set over 11 manifests reproduces expected.json (allow, deny, warn, default requests)"
CODE=0
mini test --policies "$F/policies.json" --namespace "$NS" --json "$M"/*.json > "$WORK/mini-webhook.json" || CODE=$?
[[ "$CODE" == "1" ]] || fail "miniadmit test should exit 1 when an object is denied, got $CODE: $(head -c 2000 "$WORK/mini-webhook.json")"
python3 "$WORK/check.py" expect "$WORK/mini-webhook.json" "$F/expected.json" webhook || fail "miniadmit test differs from expected.json"
mini test --policies "$F/policies.json" "$M/tag-only.json" > "$WORK/tag-only.txt" || fail "tag-only should be admitted: $(cat "$WORK/tag-only.txt")"
grep -q "warn+audit \[require-digest\]" "$WORK/tag-only.txt" || fail "tag-only should carry the digest warning: $(cat "$WORK/tag-only.txt")"

echo "▸ miniadmit cel: a missing field is an error, has() guards it, a bad escape is a compile error"
CODE=0
mini cel "object.spec.containers[0].securityContext.privileged" --object "$M/good.json" > "$WORK/cel-error.txt" || CODE=$?
[[ "$CODE" == "1" ]] && grep -q "no such key: securityContext" "$WORK/cel-error.txt" || fail "expected exit 1 and no such key: code $CODE, $(cat "$WORK/cel-error.txt")"
[[ "$(mini cel "object.spec.containers.all(c, !has(c.securityContext) || !c.securityContext.privileged)" --object "$M/good.json")" == "true" ]] || fail "the guarded expression should be true for good.json"
CODE=0
mini cel "'\\d'.size()" > /dev/null 2> "$WORK/cel-syntax.txt" || CODE=$?
[[ "$CODE" == "2" ]] && grep -q "invalid escape sequence" "$WORK/cel-syntax.txt" || fail "expected exit 2 for a bad escape: code $CODE, $(cat "$WORK/cel-syntax.txt")"

echo "▸ miniadmit certs and serve: openssl verifies the chain, curl gets AdmissionReview answers over HTTPS"
mini certs --out "$WORK/local-tls" --dns localhost --ip 127.0.0.1 > "$WORK/certs.json" || fail "certs: $(cat "$WORK/certs.json")"
openssl verify -CAfile "$WORK/local-tls/ca.crt" "$WORK/local-tls/tls.crt" > "$WORK/verify.txt" 2>&1 || fail "openssl verify: $(cat "$WORK/verify.txt")"
openssl x509 -in "$WORK/local-tls/tls.crt" -noout -text > "$WORK/x509.txt" 2>&1 || fail "openssl x509: $(cat "$WORK/x509.txt")"
for want in "DNS:localhost" "IP Address:127.0.0.1" "TLS Web Server Authentication" "CA:FALSE"; do
  grep -q "$want" "$WORK/x509.txt" || fail "the serving certificate lacks $want: $(cat "$WORK/x509.txt")"
done
echo "  $(cat "$WORK/verify.txt")"
PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
mini serve --policies "$F/policies.json" --cert "$WORK/local-tls/tls.crt" --key "$WORK/local-tls/tls.key" --host 127.0.0.1 --port "$PORT" > "$WORK/serve.log" 2>&1 &
PIDS+=("$!")
CURL=(curl -fsS --max-time 10 --cacert "$WORK/local-tls/ca.crt")
healthy() { "${CURL[@]}" "https://127.0.0.1:$PORT/healthz" > "$WORK/healthz.json" 2>/dev/null; }
wait_for 20 healthy || fail "miniadmit serve did not answer /healthz: $(cat "$WORK/serve.log")"
"${CURL[@]}" -H 'Content-Type: application/json' --data-binary @"$F/review-pod.json" "https://127.0.0.1:$PORT/validate?timeout=5s" > "$WORK/validate.json" || fail "POST /validate: $(cat "$WORK/serve.log")"
"${CURL[@]}" -H 'Content-Type: application/json' --data-binary @"$F/review-pod.json" "https://127.0.0.1:$PORT/mutate?timeout=5s" > "$WORK/mutate.json" || fail "POST /mutate: $(cat "$WORK/serve.log")"
python3 - "$WORK/validate.json" "$WORK/mutate.json" <<'PY' || fail "AdmissionReview answers: $(cat "$WORK/validate.json" "$WORK/mutate.json")"
import base64, json, sys
validate = json.load(open(sys.argv[1]))
mutate = json.load(open(sys.argv[2]))
v, m = validate["response"], mutate["response"]
assert validate["apiVersion"] == "admission.k8s.io/v1" and validate["kind"] == "AdmissionReview", validate
assert v["uid"] == "705ab4f5-6393-11e8-b7cc-42010a800002" and v["allowed"] is False and v["status"]["code"] == 403, v
assert "[disallow-privileged]" in v["status"]["message"] and "[require-team-label]" in v["status"]["message"], v
assert any(w.startswith("[require-digest]") for w in v.get("warnings", [])), v
ops = json.loads(base64.b64decode(m["patch"]))
assert m["patchType"] == "JSONPatch" and ops == [{"op": "add", "path": "/spec/containers/0/resources/requests/cpu", "value": "100m"}], m
print("  /validate: allowed=false, %s" % v["status"]["message"][:110])
print("  /mutate:   %s" % json.dumps(ops))
PY
if curl -fsS --max-time 10 "https://127.0.0.1:$PORT/healthz" > /dev/null 2> "$WORK/untrusted.txt"; then
  fail "curl without the CA should refuse miniadmit's certificate"
fi
echo "  without --cacert, curl refuses the certificate: $(head -c 120 "$WORK/untrusted.txt")"

echo "▸ Kyverno CLI v$KYVERNO_VERSION: download, verify against the release's checksums.txt"
case "$(uname -s)/$(uname -m)" in
  Linux/x86_64|Linux/amd64) KY_ASSET="kyverno-cli_v${KYVERNO_VERSION}_linux_x86_64.tar.gz" ;;
  Linux/aarch64|Linux/arm64) KY_ASSET="kyverno-cli_v${KYVERNO_VERSION}_linux_arm64.tar.gz" ;;
  Darwin/arm64) KY_ASSET="kyverno-cli_v${KYVERNO_VERSION}_darwin_arm64.tar.gz" ;;
  Darwin/x86_64) KY_ASSET="kyverno-cli_v${KYVERNO_VERSION}_darwin_x86_64.tar.gz" ;;
  *) fail "no Kyverno CLI build for $(uname -s)/$(uname -m)" ;;
esac
BASE="https://github.com/kyverno/kyverno/releases/download/v$KYVERNO_VERSION"
curl -fsSL --retry 3 -o "$WORK/dl/$KY_ASSET" "$BASE/$KY_ASSET" || fail "download $BASE/$KY_ASSET"
curl -fsSL --retry 3 -o "$WORK/dl/checksums.txt" "$BASE/checksums.txt" || fail "download $BASE/checksums.txt"
WANT="$(awk -v f="$KY_ASSET" '$2 == f {print $1}' "$WORK/dl/checksums.txt")"
GOT="$(sha256_of "$WORK/dl/$KY_ASSET")"
[[ -n "$WANT" && "$WANT" == "$GOT" ]] || fail "kyverno checksum mismatch for $KY_ASSET: checksums.txt says '$WANT', the download is '$GOT'"
tar -xzf "$WORK/dl/$KY_ASSET" -C "$WORK/bin" kyverno || fail "unpack $KY_ASSET"
KYVERNO="$WORK/bin/kyverno"
"$KYVERNO" version > "$WORK/kyverno-version.txt" 2>&1 || fail "kyverno version: $(cat "$WORK/kyverno-version.txt")"
grep -q "$KYVERNO_VERSION" "$WORK/kyverno-version.txt" || fail "unexpected kyverno: $(cat "$WORK/kyverno-version.txt")"
echo "  sha256 $GOT matches checksums.txt; $(grep -m1 -i version "$WORK/kyverno-version.txt")"

echo "▸ kyverno apply: equivalent ClusterPolicies on the same 9 pods give the same fail and warn sets as miniadmit"
KY_ARGS=()
for name in "${VAP_SET[@]}"; do KY_ARGS+=(--resource "$M/$name.json"); done
CODE=0
"$KYVERNO" apply "$F/kyverno-policies.yaml" "${KY_ARGS[@]}" --policy-report --output-format json --audit-warn --remove-color > "$WORK/kyverno.out" 2> "$WORK/kyverno.err" || CODE=$?
[[ "$CODE" == "0" || "$CODE" == "1" ]] || fail "kyverno apply exited $CODE: $(cat "$WORK/kyverno.err") $(head -c 2000 "$WORK/kyverno.out")"
if grep -q "Invalid Policies\|Policies Skipped" "$WORK/kyverno.out"; then fail "kyverno rejected or skipped a policy: $(head -c 3000 "$WORK/kyverno.out")"; fi
python3 "$WORK/check.py" kyverno "$WORK/kyverno.out" "$WORK/mini-webhook.json" "$F/expected.json" || fail "kyverno and miniadmit disagree (stderr: $(head -c 1000 "$WORK/kyverno.err"))"
CODE=0
"$KYVERNO" apply "$F/kyverno-policies.yaml" --resource "$M/untrusted-registry.json" --audit-warn --remove-color > "$WORK/kyverno-plain.out" 2>&1 || CODE=$?
python3 - "$WORK/kyverno-plain.out" "$CODE" <<'PY' || fail "kyverno's summary line for untrusted-registry: $(cat "$WORK/kyverno-plain.out")"
import re, sys
text, code = open(sys.argv[1]).read(), int(sys.argv[2])
counts = re.search(r"pass: (\d+), fail: (\d+), warn: (\d+), error: (\d+), skip: (\d+)", text)
assert counts and counts.groups()[:4] == ("3", "1", "1", "0") and code == 1, (counts and counts.groups(), code)
assert "allowed-registries" in text, text
print("  untrusted-registry: pass 3, fail 1 (allowed-registries), warn 1 (require-digest, Audit), exit 1")
PY

if ! command -v kubectl >/dev/null; then
  echo "  (kubectl not installed; cluster checks skipped)"
  echo "SMOKE OK: security/admission-policy (offline only)"
  exit 0
fi
kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if ! grep -qx "$CTX" "$WORK/contexts.txt"; then
  echo "  (context $CTX not found; this smoke registers admission webhooks, so it only runs against the throwaway kind cluster)"
  echo "SMOKE OK: security/admission-policy (offline only)"
  exit 0
fi
k cluster-info > "$WORK/cluster-info.txt" 2>&1 || fail "context $CTX is not reachable: $(cat "$WORK/cluster-info.txt")"
CLUSTER=1
KJSON="$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' kubectl --context "$CTX")"
k version -o json > "$WORK/version.json" 2>/dev/null || true
python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print("  API server %s" % d.get("serverVersion", {}).get("gitVersion", "?"))' "$WORK/version.json" 2>/dev/null || true
k api-resources --api-group=admissionregistration.k8s.io -o name > "$WORK/api-resources.txt" 2>&1 || fail "api-resources: $(cat "$WORK/api-resources.txt")"

echo "▸ namespaces $NS (guarded) and $HOOK_NS (runs the webhook, never intercepted)"
delete_labelled
k delete namespace "$NS" "$HOOK_NS" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1 || true
k create namespace "$NS" >/dev/null || fail "create namespace $NS"
k create namespace "$HOOK_NS" >/dev/null || fail "create namespace $HOOK_NS"
service_accounts() { k get serviceaccount default -n "$NS" >/dev/null 2>&1 && k get serviceaccount default -n "$HOOK_NS" >/dev/null 2>&1; }
wait_for 60 service_accounts || fail "the default ServiceAccounts never appeared"

echo "▸ deploy miniadmit: an openssl CA and a serving certificate for miniadmit.$HOOK_NS.svc, the code and policies in a ConfigMap"
mini certs --out "$WORK/tls" --dns "miniadmit.$HOOK_NS.svc" --dns "miniadmit.$HOOK_NS.svc.cluster.local" > /dev/null || fail "certs for the in-cluster webhook"
k -n "$HOOK_NS" create secret tls miniadmit-tls --cert "$WORK/tls/tls.crt" --key "$WORK/tls/tls.key" > "$WORK/secret.txt" 2>&1 || fail "secret: $(cat "$WORK/secret.txt")"
k -n "$HOOK_NS" create configmap miniadmit --from-file=main.py=code/main.py --from-file=policies.json="$F/policies.json" > "$WORK/cm.txt" 2>&1 || fail "configmap: $(cat "$WORK/cm.txt")"
k -n "$HOOK_NS" apply -f "$F/webhook-deploy.json" > "$WORK/deploy.txt" 2>&1 || fail "apply webhook-deploy.json: $(cat "$WORK/deploy.txt")"
k -n "$HOOK_NS" rollout status deployment/miniadmit --timeout=180s > /dev/null 2>&1 || fail "miniadmit not ready: $(k -n "$HOOK_NS" get pods -o wide 2>&1) $(k -n "$HOOK_NS" logs deployment/miniadmit --tail=40 2>&1)"

echo "▸ webhook configurations from miniadmit webhook-config: failurePolicy Ignore, namespaceSelector $NS, timeout 5 s, sideEffects None"
mini webhook-config --name cc-admission-policy --service miniadmit --service-namespace "$HOOK_NS" --namespace "$NS" --ca-bundle "$WORK/tls/ca.crt" --label "$LABEL" > "$WORK/webhooks.json" || fail "webhook-config"
python3 - "$WORK/webhooks.json" "$NS" <<'PY' || fail "generated webhook configurations: $(cat "$WORK/webhooks.json")"
import json, sys
doc, ns = json.load(open(sys.argv[1])), sys.argv[2]
for item in doc["items"]:
    hook = item["webhooks"][0]
    assert hook["failurePolicy"] == "Ignore" and hook["sideEffects"] == "None" and hook["timeoutSeconds"] == 5, hook
    assert hook["namespaceSelector"]["matchExpressions"][0]["values"] == [ns], hook["namespaceSelector"]
    print("  %s %s -> %s%s" % (item["kind"], hook["name"], hook["clientConfig"]["service"]["name"], hook["clientConfig"]["service"]["path"]))
PY
k apply -f "$WORK/webhooks.json" > "$WORK/webhooks.txt" 2>&1 || fail "apply webhook configurations: $(cat "$WORK/webhooks.txt")"
webhook_denies() { ! k -n "$NS" create --dry-run=server -f "$M/privileged.json" > /dev/null 2> "$WORK/probe.err" && grep -q "denied the request" "$WORK/probe.err"; }
wait_for 90 webhook_denies || fail "the API server never sent the privileged pod to miniadmit: $(cat "$WORK/probe.err") $(k -n "$HOOK_NS" logs deployment/miniadmit --tail=20 2>&1)"
echo "  $(head -c 220 "$WORK/probe.err")"

echo "▸ API server plus miniadmit versus miniadmit test: 11 manifests, the same allow, deny, warnings and mutated requests"
python3 "$WORK/check.py" cluster webhook "$KJSON" "$NS" "$WORK/mini-webhook.json" 60 "$M"/*.json || fail "webhook outcomes differ from miniadmit test"

echo "▸ a real create: the stored pod carries the requests the mutating webhook added"
python3 - "$M/good.json" "$WORK/good-stored.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
doc["metadata"]["name"] = "good-stored"
json.dump(doc, open(sys.argv[2], "w"))
PY
k -n "$NS" create -f "$WORK/good-stored.json" > "$WORK/create.txt" 2>&1 || fail "create good-stored: $(cat "$WORK/create.txt")"
k -n "$NS" get pod good-stored -o json > "$WORK/stored.json" || fail "get good-stored"
python3 - "$WORK/stored.json" <<'PY' || fail "stored requests: $(head -c 1500 "$WORK/stored.json")"
import json, sys
requests = json.load(open(sys.argv[1]))["spec"]["containers"][0]["resources"]["requests"]
assert requests.get("cpu") == "100m" and requests.get("memory") == "64Mi", requests
print("  stored requests %s (cpu from the webhook, memory defaulted from the limit)" % json.dumps(requests, sort_keys=True))
PY
k -n "$NS" delete pod good-stored --wait=false > /dev/null 2>&1 || true

echo "▸ miniadmit's own log: one JSON line per review, both phases, dryRun true for the dry runs"
k -n "$HOOK_NS" logs deployment/miniadmit --tail=1000 > "$WORK/hook.log" 2>&1 || fail "webhook logs: $(cat "$WORK/hook.log")"
python3 - "$WORK/hook.log" <<'PY' || fail "webhook log: $(tail -20 "$WORK/hook.log")"
import json, sys
lines = [json.loads(line) for line in open(sys.argv[1]) if line.startswith("{")]
ok = [l for l in lines if l.get("status") == 200]
phases = {l["phase"] for l in ok}
dry = sum(1 for l in ok if l.get("dryRun") is True)
stored = [l for l in ok if l.get("name") == "good-stored" and l.get("dryRun") is False]
assert {"mutate", "validate"} <= phases and dry >= 22 and len(stored) >= 2, (phases, dry, stored, lines[-3:])
print("  %d reviews: %d dry runs, %d for the stored pod, phases %s" % (len(ok), dry, len(stored), sorted(phases)))
PY

echo "▸ failurePolicy: webhook scaled to 0; Ignore admits a privileged pod, Fail rejects even a compliant one, and only in $NS"
k -n "$HOOK_NS" scale deployment/miniadmit --replicas=0 > /dev/null || fail "scale miniadmit to 0"
no_webhook_pods() { [[ -z "$(k -n "$HOOK_NS" get pods -l app=miniadmit -o name 2>/dev/null)" ]]; }
wait_for 90 no_webhook_pods || fail "miniadmit pods did not go away: $(k -n "$HOOK_NS" get pods 2>&1)"
fail_open() { k -n "$NS" create --dry-run=server -f "$M/privileged.json" > /dev/null 2> "$WORK/open.err"; }
wait_for 60 fail_open || fail "with the webhook down and failurePolicy Ignore the privileged pod should be admitted: $(cat "$WORK/open.err")"
echo "  Ignore: the privileged pod is admitted while the webhook is down"
k patch validatingwebhookconfiguration cc-admission-policy --type=json -p '[{"op": "replace", "path": "/webhooks/0/failurePolicy", "value": "Fail"}]' > /dev/null || fail "patch failurePolicy"
fail_closed() { ! k -n "$NS" create --dry-run=server -f "$M/good.json" > /dev/null 2> "$WORK/closed.err" && grep -q "failed calling webhook" "$WORK/closed.err"; }
wait_for 60 fail_closed || fail "with failurePolicy Fail a compliant pod should be rejected while the webhook is down: $(cat "$WORK/closed.err")"
echo "  Fail: $(head -c 200 "$WORK/closed.err")"
k -n "$HOOK_NS" create --dry-run=server -f "$M/privileged.json" > /dev/null 2> "$WORK/scoped.err" || fail "$HOOK_NS is outside the namespaceSelector and should be unaffected: $(cat "$WORK/scoped.err")"
echo "  $HOOK_NS is outside the namespaceSelector: unaffected"
k delete validatingwebhookconfiguration,mutatingwebhookconfiguration -l "$LABEL" > "$WORK/delete-hooks.txt" 2>&1 || fail "delete webhook configurations: $(cat "$WORK/delete-hooks.txt")"
webhooks_gone() { k -n "$NS" create --dry-run=server -f "$M/privileged.json" > /dev/null 2>&1; }
wait_for 60 webhooks_gone || fail "requests in $NS still fail after the webhook configurations were deleted"
echo "  deleting the configurations restores writes, even with the webhook down"

if grep -qx "validatingadmissionpolicies.admissionregistration.k8s.io" "$WORK/api-resources.txt"; then
  echo "▸ ValidatingAdmissionPolicy: miniadmit vap translates cel-policies.json; the API server's decisions match miniadmit's CEL subset"
  VAP_FILES=()
  for name in "${VAP_SET[@]}"; do VAP_FILES+=("$M/$name.json"); done
  CODE=0
  mini test --policies "$F/cel-policies.json" --namespace "$NS" --json "${VAP_FILES[@]}" > "$WORK/mini-vap.json" || CODE=$?
  [[ "$CODE" == "1" ]] || fail "miniadmit test on cel-policies.json should exit 1, got $CODE"
  python3 "$WORK/check.py" expect "$WORK/mini-vap.json" "$F/expected.json" vap || fail "miniadmit's CEL results differ from expected.json"
  mini vap --policies "$F/cel-policies.json" --namespace "$NS" --label "$LABEL" > "$WORK/vap.json" || fail "miniadmit vap"
  k apply -f "$WORK/vap.json" > "$WORK/vap.txt" 2>&1 || fail "apply ValidatingAdmissionPolicies: $(cat "$WORK/vap.txt")"
  python3 "$WORK/check.py" cluster vap "$KJSON" "$NS" "$WORK/mini-vap.json" 90 "${VAP_FILES[@]}" || fail "ValidatingAdmissionPolicy outcomes differ from miniadmit"
  k get validatingadmissionpolicies -l "$LABEL" -o json > "$WORK/vap-status.json" 2>/dev/null || true
  python3 - "$WORK/vap-status.json" <<'PY' || true
import json, sys
for item in json.load(open(sys.argv[1])).get("items", []):
    warnings = (item.get("status", {}).get("typeChecking") or {}).get("expressionWarnings") or []
    print("  %s: %s" % (item["metadata"]["name"], "type checking clean" if not warnings else "type warnings %s" % warnings))
PY

  echo "▸ an expression that errors: under Fail it denies with 'resulted in error', under Ignore it admits; miniadmit says the same"
  CODE=0
  mini test --policies "$F/cel-naive.json" --namespace "$NS" --json "$M/good.json" > "$WORK/mini-naive.json" || CODE=$?
  [[ "$CODE" == "1" ]] || fail "miniadmit should deny good.json under the naive policy, got exit $CODE"
  mini vap --policies "$F/cel-naive.json" --namespace "$NS" --label "$LABEL" > "$WORK/naive.json" || fail "miniadmit vap naive"
  k apply -f "$WORK/naive.json" > "$WORK/naive-apply.txt" 2>&1 || fail "apply the naive policy: $(cat "$WORK/naive-apply.txt")"
  naive_denies() { ! k -n "$NS" create --dry-run=server -f "$M/good.json" > /dev/null 2> "$WORK/naive.err" && grep -q "cc-admission-naive-privileged" "$WORK/naive.err"; }
  wait_for 60 naive_denies || fail "the naive policy should deny good.json: $(cat "$WORK/naive.err")"
  grep -q "resulted in error" "$WORK/naive.err" || fail "the API server should report an evaluation error: $(cat "$WORK/naive.err")"
  python3 - "$WORK/mini-naive.json" "$WORK/naive.err" <<'PY' || fail "miniadmit's naive verdict: $(cat "$WORK/mini-naive.json")"
import json, sys
mine = json.load(open(sys.argv[1]))["results"][0]
real = open(sys.argv[2]).read()
message = mine["violations"][0]["message"]
assert not mine["allowed"] and mine["violations"][0]["error"] and "resulted in error: no such key: securityContext" in message, mine
print("  API server: %s" % real.strip()[real.find("ValidatingAdmissionPolicy"):][:200])
print("  miniadmit:  %s" % message[:200])
if "no such key" not in real:
    print("  (note: this API server words the CEL error differently)")
PY
  k patch validatingadmissionpolicy cc-admission-naive-privileged --type=merge -p '{"spec": {"failurePolicy": "Ignore"}}' > /dev/null || fail "patch the naive policy to Ignore"
  naive_admits() { k -n "$NS" create --dry-run=server -f "$M/good.json" > /dev/null 2> "$WORK/naive-ignore.err"; }
  wait_for 60 naive_admits || fail "under Ignore the naive policy's error should admit good.json: $(cat "$WORK/naive-ignore.err")"
  sed 's/"failurePolicy": "Fail"/"failurePolicy": "Ignore"/' "$F/cel-naive.json" > "$WORK/cel-naive-ignore.json"
  mini test --policies "$WORK/cel-naive-ignore.json" --namespace "$NS" "$M/good.json" > "$WORK/mini-naive-ignore.txt" || fail "miniadmit should admit good.json under Ignore: $(cat "$WORK/mini-naive-ignore.txt")"
  echo "  Ignore: admitted by the API server and by miniadmit"
  for kind in validatingadmissionpolicybindings validatingadmissionpolicies; do k delete "$kind" -l "$LABEL" --ignore-not-found > /dev/null || fail "delete $kind"; done
else
  echo "  (ValidatingAdmissionPolicy is not served by this API server; the VAP comparison is skipped)"
fi

k get --raw /apis/admissionregistration.k8s.io/v1 > "$WORK/admreg-v1.json" 2>/dev/null || echo '{}' > "$WORK/admreg-v1.json"
if python3 -c 'import json, sys; sys.exit(0 if any(r.get("name") == "mutatingadmissionpolicies" for r in json.load(open(sys.argv[1])).get("resources", [])) else 1)' "$WORK/admreg-v1.json"; then
  echo "▸ MutatingAdmissionPolicy (v1): a CEL JSONPatch adds the same escaped-pointer label miniadmit's default-labels adds"
  python3 - "$NS" "$LABEL" > "$WORK/map.json" <<'PY'
import json, sys
ns, label = sys.argv[1], sys.argv[2]
key, value = label.split("=", 1)
meta = {"name": "cc-admission-mutated-by", "labels": {key: value}}
policy = {
    "apiVersion": "admissionregistration.k8s.io/v1",
    "kind": "MutatingAdmissionPolicy",
    "metadata": meta,
    "spec": {
        "failurePolicy": "Fail",
        "reinvocationPolicy": "Never",
        "matchConstraints": {"resourceRules": [{"apiGroups": [""], "apiVersions": ["v1"], "operations": ["CREATE"], "resources": ["pods"]}]},
        "matchConditions": [{"name": "has-labels-without-marker", "expression": "has(object.metadata.labels) && !('cloudcaptain.io/mutated-by' in object.metadata.labels)"}],
        "mutations": [{"patchType": "JSONPatch", "jsonPatch": {"expression": '[JSONPatch{op: "add", path: "/metadata/labels/cloudcaptain.io~1mutated-by", value: "mutatingadmissionpolicy"}]'}}],
    },
}
binding = {
    "apiVersion": "admissionregistration.k8s.io/v1",
    "kind": "MutatingAdmissionPolicyBinding",
    "metadata": meta,
    "spec": {"policyName": "cc-admission-mutated-by", "matchResources": {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": ns}}}},
}
print(json.dumps({"apiVersion": "v1", "kind": "List", "items": [policy, binding]}))
PY
  k apply -f "$WORK/map.json" > "$WORK/map-apply.txt" 2>&1 || fail "apply MutatingAdmissionPolicy: $(cat "$WORK/map-apply.txt")"
  map_labels() {
    k -n "$NS" create --dry-run=server -o json -f "$M/good.json" > "$WORK/map-pod.json" 2> "$WORK/map.err" &&
      python3 -c 'import json, sys; sys.exit(0 if json.load(open(sys.argv[1]))["metadata"]["labels"].get("cloudcaptain.io/mutated-by") == "mutatingadmissionpolicy" else 1)' "$WORK/map-pod.json"
  }
  wait_for 60 map_labels || fail "MutatingAdmissionPolicy never added the label: $(cat "$WORK/map.err") $(head -c 800 "$WORK/map-pod.json")"
  mini test --policies "$F/labels-policy.json" --namespace "$NS" --json "$M/good.json" > "$WORK/mini-labels.json" || fail "miniadmit default-labels"
  python3 - "$WORK/mini-labels.json" "$WORK/map-pod.json" <<'PY' || fail "MutatingAdmissionPolicy and miniadmit disagree: $(cat "$WORK/mini-labels.json")"
import json, sys
mine = json.load(open(sys.argv[1]))["results"][0]
real = json.load(open(sys.argv[2]))["metadata"]["labels"]
assert mine["patch"] == [{"op": "add", "path": "/metadata/labels/cloudcaptain.io~1mutated-by", "value": "mutatingadmissionpolicy"}], mine["patch"]
assert mine["patched"]["metadata"]["labels"] == real, (mine["patched"]["metadata"]["labels"], real)
print("  both: %s, patch path %s" % (json.dumps(real, sort_keys=True), mine["patch"][0]["path"]))
PY
  for kind in mutatingadmissionpolicybindings mutatingadmissionpolicies; do k delete "$kind" -l "$LABEL" --ignore-not-found > /dev/null || fail "delete $kind"; done
else
  echo "  (MutatingAdmissionPolicy is not served as admissionregistration.k8s.io/v1 here, GA from Kubernetes 1.36; skipped)"
fi

echo "SMOKE OK: security/admission-policy"
