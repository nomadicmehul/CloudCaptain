#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
LESSON="$PWD"
WORK="$(mktemp -d)"
MLFLOW_VERSION="3.17.2"
MLFLOW_IMAGE="ghcr.io/mlflow/mlflow:v$MLFLOW_VERSION@sha256:2cc40b3208991940d824d04f339833a0b69507569012aa5ec545690608602fc0"
MLFLOW_NAME="cc-experiment-tracking-mlflow"
MINI_PID=""
cleanup() {
  if [[ -n "$MINI_PID" ]]; then
    kill "$MINI_PID" 2>/dev/null || true
    wait "$MINI_PID" 2>/dev/null || true
  fi
  if command -v docker >/dev/null; then
    docker rm -f "$MLFLOW_NAME" >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

command -v docker >/dev/null || fail "needs docker to run the MLflow server image"
command -v git >/dev/null || fail "needs git"
command -v curl >/dev/null || fail "needs curl"
PY="$(command -v python3)" || fail "needs python3"
"$PY" -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version' || fail "code/requirements-mlflow.txt pins cp312 wheels; run with Python 3.12, found $("$PY" --version 2>&1)"
mt() { "$PY" "$LESSON/code/main.py" "$@"; }
wait_for() {
  local url="$1" want="$2" deadline=$((SECONDS + $3))
  while (( SECONDS < deadline )); do
    if curl -sf -o "$WORK/probe.out" "$url" 2>/dev/null && grep -q -- "$want" "$WORK/probe.out"; then
      return 0
    fi
    sleep 1
  done
  return 1
}
same_json() {
  "$PY" - "$@" <<'PY'
import json, sys
left, right, ignore = json.load(open(sys.argv[1])), json.load(open(sys.argv[2])), set(sys.argv[3:])
def strip(value):
    if isinstance(value, dict):
        return {k: strip(v) for k, v in value.items() if k not in ignore}
    if isinstance(value, list):
        return [strip(v) for v in value]
    return value
def diff(a, b, path=""):
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                yield f"{path}/{key}: only in {'minitrack' if key in a else 'mlflow'}"
            else:
                yield from diff(a[key], b[key], f"{path}/{key}")
    elif a != b:
        yield f"{path}: minitrack {json.dumps(a)[:300]} vs mlflow {json.dumps(b)[:300]}"
problems = list(diff(strip(left), strip(right)))
for line in problems[:25]:
    print("  " + line, file=sys.stderr)
sys.exit(1 if problems else 0)
PY
}

echo "▸ minitrack server on a free port (SQLite store, local artifact root)"
mt server --db "$WORK/minitrack.db" --artifacts "$WORK/minitrack-artifacts" --port 0 --port-file "$WORK/minitrack.port" > "$WORK/minitrack.log" 2>&1 &
MINI_PID=$!
deadline=$((SECONDS + 20))
until [[ -s "$WORK/minitrack.port" ]]; do
  (( SECONDS < deadline )) || fail "minitrack did not write its port: $(cat "$WORK/minitrack.log")"
  kill -0 "$MINI_PID" 2>/dev/null || fail "minitrack exited: $(cat "$WORK/minitrack.log")"
  sleep 0.2
done
MINI="http://127.0.0.1:$(cat "$WORK/minitrack.port")"
wait_for "$MINI/api/2.0/mlflow/experiments/get?experiment_id=0" '"Default"' 20 || fail "minitrack at $MINI does not answer like a tracking server: $(cat "$WORK/probe.out" 2>/dev/null) $(cat "$WORK/minitrack.log")"
curl -s -D "$WORK/mini.headers" -o /dev/null "$MINI/health"
grep -qi '^server: minitrack' "$WORK/mini.headers" || fail "the server at $MINI is not minitrack: $(cat "$WORK/mini.headers")"
echo "  $MINI"

echo "▸ MLflow $MLFLOW_VERSION server from the pinned image on a free port"
MLFLOW_PORT="$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
docker rm -f "$MLFLOW_NAME" >/dev/null 2>&1 || true
docker pull -q "$MLFLOW_IMAGE" > "$WORK/pull.txt" 2>&1 || fail "docker pull $MLFLOW_IMAGE: $(cat "$WORK/pull.txt")"
docker run -d --name "$MLFLOW_NAME" -p "127.0.0.1:$MLFLOW_PORT:5000" "$MLFLOW_IMAGE" \
  mlflow server --host 0.0.0.0 --port 5000 --workers 1 \
  --backend-store-uri sqlite:////tmp/mlflow.db --artifacts-destination /tmp/mlflow-artifacts > /dev/null
REAL="http://127.0.0.1:$MLFLOW_PORT"
wait_for "$REAL/version" "$MLFLOW_VERSION" 150 || fail "MLflow at $REAL did not report version $MLFLOW_VERSION: $(cat "$WORK/probe.out" 2>/dev/null) $(docker logs --tail 30 "$MLFLOW_NAME" 2>&1)"
MLFLOW_REPORTED="$(head -c 40 "$WORK/probe.out")"
wait_for "$REAL/api/2.0/mlflow/experiments/get?experiment_id=0" '"Default"' 30 || fail "MLflow REST API not ready: $(docker logs --tail 30 "$MLFLOW_NAME" 2>&1)"
echo "  $REAL reports version $MLFLOW_REPORTED"

echo "▸ the official mlflow client (mlflow-skinny $MLFLOW_VERSION), hash-pinned in a venv"
"$PY" -m venv "$WORK/venv"
VPY="$WORK/venv/bin/python"
"$WORK/venv/bin/pip" install --quiet --disable-pip-version-check --retries 3 --require-hashes --only-binary=:all: -r "$LESSON/code/requirements-mlflow.txt" > "$WORK/pip.txt" 2>&1 || fail "pip install mlflow-skinny: $(tail -20 "$WORK/pip.txt")"
[[ "$("$VPY" -c 'import mlflow; print(mlflow.__version__)')" == "$MLFLOW_VERSION" ]] || fail "mlflow client version: $("$VPY" -c 'import mlflow; print(mlflow.__version__)' 2>&1)"
export GIT_PYTHON_REFRESH=quiet MLFLOW_ENABLE_ASYNC_LOGGING=false

echo "▸ the same scripted session against both servers: experiments, runs, metrics, searches, artifacts, registry"
for target in minitrack mlflow; do
  url="$MINI"
  [[ "$target" == "mlflow" ]] && url="$REAL"
  "$VPY" "$LESSON/code/mlflow_session.py" "$url" "$WORK/session-$target.json" > "$WORK/session-$target.log" 2>&1 || fail "session against $target ($url): $(tail -30 "$WORK/session-$target.log")"
done
same_json "$WORK/session-minitrack.json" "$WORK/session-mlflow.json" || fail "the official client saw different results from minitrack and MLflow (differences above)"
"$PY" - "$WORK/session-mlflow.json" <<'PY' || fail "session contents: $(head -c 3000 "$WORK/session-mlflow.json")"
import json, sys
s = json.load(open(sys.argv[1]))
runs = s["runs"]
assert sorted(runs) == ["lr-0.01", "lr-0.03", "lr-0.1", "lr-0.3", "lr-2.5", "sweep"], sorted(runs)
assert runs["lr-2.5"]["status"] == "FAILED" and runs["lr-0.3"]["status"] == "FINISHED", runs["lr-2.5"]["status"]
assert runs["lr-0.3"]["tags"]["mlflow.parentRunId"] == "sweep", runs["lr-0.3"]["tags"]
assert len(s["metric_history"]["lr-0.3"]) == 30 and [step for step, _ in s["metric_history"]["lr-0.3"]] == list(range(30))
assert s["searches"]["metrics.rmse < 0.5 | ['metrics.rmse ASC']"] == ["lr-0.3", "lr-0.1"], s["searches"]
assert s["searches"][" | ['metrics.rmse DESC']"][-1] == "sweep", "a run without the metric must sort last"
assert s["pages_of_two"] == [["lr-0.01", "lr-0.03"], ["lr-0.1", "lr-0.3"], ["lr-2.5", "sweep"]], s["pages_of_two"]
assert set(s["errors"].values()) == {"INVALID_PARAMETER_VALUE", "RESOURCE_DOES_NOT_EXIST", "RESOURCE_ALREADY_EXISTS", "no error"}, s["errors"]
assert s["errors"]["log_param new value"] == "INVALID_PARAMETER_VALUE", s["errors"]
assert s["artifacts"]["model"] == [["model/model.json", False, s["artifacts"]["model"][0][2]]], s["artifacts"]
assert s["registry"]["champion"]["alias_points_to"]["version"] == "2" and s["registry"]["after rollback"]["alias_points_to"]["version"] == "1", s["registry"]
assert s["registry"]["champion"]["prediction"] != s["registry"]["after rollback"]["prediction"]
print(f"  identical: {len(runs)} runs, {sum(len(h) for h in s['metric_history'].values())} metric points, {len(s['searches'])} searches, {len(s['errors']) + len(s['registry']['errors'])} error codes, {len(s['artifacts'])} artifact checks, champion v2 -> rollback v1")
PY
if grep -q "no route" "$WORK/minitrack.log"; then
  fail "the official client called endpoints minitrack does not implement: $(grep 'no route' "$WORK/minitrack.log" | sort -u)"
fi

echo "▸ minitrack's own CLI (stdlib client) against both servers, with git and DVC-pointer lineage"
PROJECT="$WORK/project"
mkdir -p "$PROJECT"
export GIT_CONFIG_GLOBAL="$WORK/gitconfig" GIT_CONFIG_NOSYSTEM=1
git config --global user.name "Smoke Test"
git config --global user.email "smoke@example.invalid"
git config --global init.defaultBranch main
git config --global commit.gpgsign false
git -C "$PROJECT" init -q
mt make-data --rows 400 --seed 7 --out "$PROJECT/houses.csv" > /dev/null
mt make-data --rows 800 --seed 11 --out "$PROJECT/houses-v2.csv" > /dev/null
DATA_MD5="$(md5sum "$PROJECT/houses.csv" | cut -d' ' -f1)"
printf 'outs:\n- md5: %s\n  size: %s\n  hash: md5\n  path: houses.csv\n' "$DATA_MD5" "$(wc -c < "$PROJECT/houses.csv" | tr -d ' ')" > "$PROJECT/houses.csv.dvc"
printf '/houses.csv\n/houses-v2.csv\n' > "$PROJECT/.gitignore"
git -C "$PROJECT" add houses.csv.dvc .gitignore
git -C "$PROJECT" commit -q -m "data: houses v1 pointer"
COMMIT="$(git -C "$PROJECT" rev-parse HEAD)"
for target in minitrack mlflow; do
  url="$MINI"
  [[ "$target" == "mlflow" ]] && url="$REAL"
  (cd "$PROJECT" && mt train --tracking-uri "$url" --experiment cc-cli --data houses.csv --lr 0.01 --lr 0.1 --lr 0.3 --lr 2.5 --register cc-cli --alias champion --json > "$WORK/train-$target.json" 2> "$WORK/train-$target.err") || fail "minitrack train against $target: $(cat "$WORK/train-$target.err")"
  (cd "$PROJECT" && mt train --tracking-uri "$url" --experiment cc-cli --data houses-v2.csv --lr 0.3 --sweep-name v2 --json > "$WORK/train2-$target.json" 2> "$WORK/train2-$target.err") || fail "minitrack train v2 against $target: $(cat "$WORK/train2-$target.err")"
  mt compare --tracking-uri "$url" --experiment cc-cli --history --json > "$WORK/compare-$target.json" 2> "$WORK/compare-$target.err" || fail "minitrack compare against $target: $(cat "$WORK/compare-$target.err")"
  mt resolve models:/cc-cli@champion --tracking-uri "$url" --predict area_m2=95 rooms=3 age_years=20 --dest "$WORK/champion-$target" --json > "$WORK/resolve-$target.json" 2> "$WORK/resolve-$target.err" || fail "minitrack resolve against $target: $(cat "$WORK/resolve-$target.err")"
done
same_json "$WORK/compare-minitrack.json" "$WORK/compare-mlflow.json" || fail "minitrack compare differs between the servers"
same_json "$WORK/resolve-minitrack.json" "$WORK/resolve-mlflow.json" run_id source || fail "minitrack resolve differs between the servers"
cmp "$WORK/champion-minitrack/model/model.json" "$WORK/champion-mlflow/model/model.json" || fail "the champion model files differ"
"$PY" - "$WORK/compare-mlflow.json" "$WORK/resolve-mlflow.json" "$WORK/train-mlflow.json" "$COMMIT" "$DATA_MD5" <<'PY' || fail "minitrack CLI results: $(head -c 2500 "$WORK/compare-mlflow.json")"
import json, sys
compare, resolve, train = (json.load(open(p)) for p in sys.argv[1:4])
commit, md5 = sys.argv[4], sys.argv[5]
assert [r["status"] for r in train["runs"]] == ["FINISHED", "FINISHED", "FINISHED", "FAILED"], train["runs"]
rows = {r["run_name"]: r for r in compare["runs"]}
assert rows["lr-0.3"]["commit"] == commit and rows["lr-0.3"]["dirty"] == "false", rows["lr-0.3"]
assert rows["lr-0.3"]["data_md5"] == md5 and rows["v2-lr-0.3"]["data_md5"] != md5, rows
assert {(g["data_md5"] == md5, g["best"]) for g in compare["groups"]} == {(True, "lr-0.3"), (False, "v2-lr-0.3")}, compare["groups"]
assert any("different data versions" in w for w in compare["warnings"]), compare["warnings"]
assert len(compare["history"]["lr-0.1"]) == 30, compare["history"]
assert (resolve["version"], resolve["aliases"]) == ("1", ["champion"]) and 2.5 < resolve["prediction"] < 2.9, resolve
print(f"  same compare and resolve from both servers: best {train['best']['run_name']} rmse {train['best']['rmse']:.4f}, champion v{resolve['version']} predicts {resolve['prediction']:.4f}; commit {commit[:8]}, data md5 {md5[:8]} (dvc)")
PY
mt compare --tracking-uri "$REAL" --experiment cc-cli > "$WORK/compare.txt"
sed 's/^/  /' "$WORK/compare.txt"

echo "▸ data that no longer matches its DVC pointer is refused before anything is logged"
echo "40.0,1,1,1.1" >> "$PROJECT/houses.csv"
CODE=0
(cd "$PROJECT" && mt train --tracking-uri "$REAL" --experiment cc-cli --data houses.csv --lr 0.3 > /dev/null 2> "$WORK/stale.err") || CODE=$?
[[ "$CODE" == "1" ]] && grep -q "does not match its pointer" "$WORK/stale.err" || fail "stale data: exit $CODE, $(cat "$WORK/stale.err")"
echo "  exit 1: $(cut -c1-110 "$WORK/stale.err")"

echo "SMOKE OK: mlops/experiment-tracking"
