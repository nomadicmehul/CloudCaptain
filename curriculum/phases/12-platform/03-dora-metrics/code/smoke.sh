#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
VENV="$WORK/venv"
SINCE=2026-09-07
UNTIL=2026-10-05
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

command -v git >/dev/null || fail "git is required"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1

exit_of() {
  local out="$1"
  shift
  local code=0
  "$@" > "$out" 2> "$out.err" || code=$?
  echo "$code"
}
check() {
  local what="$1"
  shift
  python3 - "$@" > "$WORK/check.txt" 2>&1 || fail "$what: $(cat "$WORK/check.txt")"
  sed 's/^/  /' "$WORK/check.txt"
}
report() {
  local dir="$1" out="$2"
  shift 2
  local code
  code="$(exit_of "$out" python3 code/main.py report --repo "$dir/repo" --deploys "$dir/deploys.jsonl" --incidents "$dir/incidents.jsonl" --since "$SINCE" --until "$UNTIL" --json "$@")"
  [[ "$code" == "0" ]] || fail "minidora report on $dir exit $code: $(cat "$out" "$out.err")"
}

git_oracle() {
  local dir="$1" out="$2"
  mkdir -p "$out"
  git -C "$dir/repo" log --all --format='%H,%at' > "$out/commits.body"
  { echo "sha,authored"; cat "$out/commits.body"; } > "$out/commits.csv"
  python3 - "$dir/deploys.jsonl" > "$out/deploys.body" <<'PY'
import json, sys
from datetime import datetime
events = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
events = [e for e in events if e.get("environment", "production") == "production" and e.get("status", "success") == "success"]
stamp = lambda e: int(datetime.fromisoformat(e["time"].replace("Z", "+00:00")).timestamp())
for event in sorted(events, key=lambda e: (stamp(e), e["id"])):
    print("%s,%s,%d" % (event["id"], event["sha"], stamp(event)))
PY
  { echo "deploy_id,sha,time_created"; cat "$out/deploys.body"; } > "$out/deploys.csv"
  echo "deploy_id,head,sha" > "$out/change_commits.csv"
  echo "deploy_id,rolled_back" > "$out/rollbacks.csv"
  local nots=() id sha t prev_id="" prev_sha="" head parent
  while IFS=, read -r id sha t; do
    git -C "$dir/repo" rev-list --first-parent "$sha" "${nots[@]}" > "$out/heads.txt"
    while read -r head; do
      parent="$(git -C "$dir/repo" rev-parse -q --verify "$head^1" || true)"
      git -C "$dir/repo" rev-list "$head" ${parent:+"^$parent"} "${nots[@]}" > "$out/members.txt"
      while read -r member; do echo "$id,$head,$member"; done < "$out/members.txt" >> "$out/change_commits.csv"
    done < "$out/heads.txt"
    if [[ -n "$prev_sha" && "$sha" != "$prev_sha" ]] && git -C "$dir/repo" merge-base --is-ancestor "$sha" "$prev_sha"; then
      echo "$id,$prev_id" >> "$out/rollbacks.csv"
    fi
    nots+=("^$sha")
    prev_id="$id"
    prev_sha="$sha"
  done < "$out/deploys.body"
}

four_keys_sql() {
  local dir="$1" out="$2" incidents="$3"
  "$VENV/bin/python" - "$out" "$incidents" "$SINCE" "$UNTIL" > "$out/sql.json" 2> "$out/sql.err" <<'PY' || fail "duckdb on $dir: $(cat "$out/sql.err")"
import json, sys
from datetime import datetime, timezone
import duckdb
out, incidents_path, since_day, until_day = sys.argv[1:5]
epoch = lambda text: int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
since = epoch(since_day + "T00:00:00Z")
until = epoch(until_day + "T00:00:00Z")
window = 24 * 3600
con = duckdb.connect()
con.execute("SET TimeZone = 'UTC'")
con.execute(f"CREATE TABLE commits AS SELECT * FROM read_csv('{out}/commits.csv', header = true, columns = {{'sha': 'VARCHAR', 'authored': 'BIGINT'}})")
con.execute(f"CREATE TABLE deploys AS SELECT * FROM read_csv('{out}/deploys.csv', header = true, columns = {{'deploy_id': 'VARCHAR', 'sha': 'VARCHAR', 'time_created': 'BIGINT'}})")
con.execute(f"CREATE TABLE change_commits AS SELECT * FROM read_csv('{out}/change_commits.csv', header = true, columns = {{'deploy_id': 'VARCHAR', 'head': 'VARCHAR', 'sha': 'VARCHAR'}})")
con.execute(f"CREATE TABLE rollbacks AS SELECT * FROM read_csv('{out}/rollbacks.csv', header = true, columns = {{'deploy_id': 'VARCHAR', 'rolled_back': 'VARCHAR'}})")
con.execute("CREATE TABLE incidents (incident_id VARCHAR, started BIGINT, resolved BIGINT, root_cause VARCHAR, external BOOLEAN)")
for line in open(incidents_path):
    if line.strip():
        i = json.loads(line)
        con.execute("INSERT INTO incidents VALUES (?, ?, ?, ?, ?)", [i["id"], epoch(i["started"]), epoch(i["resolved"]) if i.get("resolved") else None, i.get("root_cause"), bool(i.get("external"))])
con.execute(f"CREATE VIEW period_deploys AS SELECT * FROM deploys WHERE time_created >= {since} AND time_created < {until}")
con.execute("""
    CREATE TABLE changes AS
    SELECT cc.deploy_id, cc.head, d.time_created - min(c.authored) AS lead_seconds
    FROM change_commits cc JOIN commits c USING (sha) JOIN period_deploys d USING (deploy_id)
    GROUP BY cc.deploy_id, cc.head, d.time_created""")
con.execute(f"""
    CREATE TABLE links AS
    SELECT i.*, CASE
        WHEN i.external THEN NULL
        WHEN i.root_cause IS NOT NULL THEN (SELECT any_value(cc.deploy_id) FROM change_commits cc WHERE cc.sha = i.root_cause)
        ELSE (SELECT arg_max(d.deploy_id, d.time_created) FROM deploys d
              WHERE d.time_created <= i.started AND i.started - d.time_created < {window})
      END AS deploy_id
    FROM incidents i""")
con.execute(f"""
    CREATE TABLE failures AS
    SELECT deploy_id, min(restored) AS restored FROM (
      SELECT r.rolled_back AS deploy_id, d.time_created AS restored
      FROM rollbacks r JOIN deploys d ON d.deploy_id = r.deploy_id JOIN deploys f ON f.deploy_id = r.rolled_back
      WHERE d.time_created - f.time_created < {window}
      UNION ALL
      SELECT deploy_id, CASE WHEN count(*) = count(resolved) THEN max(resolved) END
      FROM links WHERE deploy_id IS NOT NULL GROUP BY deploy_id
    ) GROUP BY deploy_id""")
one = lambda sql: con.execute(sql).fetchone()
deploys, per_day = one(f"SELECT count(*), count(*) / (({until} - {since}) / 86400.0) FROM period_deploys")
days_per_week = one(f"""
    WITH days AS (SELECT range AS day FROM range({since}, {until}, 86400)),
         deploy_days AS (SELECT DISTINCT time_created - time_created % 86400 AS day FROM period_deploys)
    SELECT quantile_cont(n::DOUBLE, 0.5) FROM (
      SELECT printf('%d-W%02d', isoyear(epoch_ms(days.day * 1000)), week(epoch_ms(days.day * 1000))) AS iso_week, count(deploy_days.day) AS n
      FROM days LEFT JOIN deploy_days USING (day) GROUP BY iso_week)""")[0]
weekly = dict(con.execute("""
    SELECT printf('%d-W%02d', isoyear(epoch_ms(time_created * 1000)), week(epoch_ms(time_created * 1000))) AS iso_week, count(*)
    FROM period_deploys GROUP BY iso_week""").fetchall())
lead = one("SELECT count(*), quantile_cont(lead_seconds::DOUBLE, 0.5), quantile_cont(lead_seconds::DOUBLE, 0.9) FROM changes")
per_commit = one("""
    SELECT count(*), quantile_cont((d.time_created - c.authored)::DOUBLE, 0.5)
    FROM change_commits cc JOIN commits c USING (sha) JOIN period_deploys d USING (deploy_id)
    WHERE d.time_created - c.authored > 0""")
cfr = one("""
    SELECT count(*), count(f.deploy_id), 100.0 * count(f.deploy_id) / count(*),
           count(f.restored), quantile_cont((f.restored - d.time_created)::DOUBLE, 0.5), quantile_cont((f.restored - d.time_created)::DOUBLE, 0.9)
    FROM period_deploys d LEFT JOIN failures f USING (deploy_id)""")
mttr = one(f"SELECT count(*), quantile_cont((resolved - started)::DOUBLE, 0.5) FROM incidents WHERE resolved IS NOT NULL AND started >= {since} AND started < {until}")
changes = con.execute("SELECT cc.deploy_id, cc.head, list(cc.sha ORDER BY cc.sha) FROM change_commits cc JOIN period_deploys USING (deploy_id) GROUP BY ALL").fetchall()
print(json.dumps({
    "duckdb": duckdb.__version__,
    "deploys": deploys, "per_day": per_day, "median_deploy_days_per_week": days_per_week, "weekly": weekly,
    "changes": lead[0], "lead_median": lead[1], "lead_p90": lead[2], "per_commit_median": per_commit[1],
    "failed": cfr[1], "cfr": cfr[2], "restored": cfr[3], "recovery_median": cfr[4], "recovery_p90": cfr[5],
    "incidents": mttr[0], "mttr_median": mttr[1],
    "partition": sorted([d, h, list(s)] for d, h, s in changes),
}))
PY
}

agree() {
  local what="$1" minidora="$2" sql="$3"
  check "$what: minidora and the Four Keys style SQL disagree" "$minidora" "$sql" <<'PY'
import json, sys
mine, sql = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
m = mine["metrics"]
pairs = {
    "deploys": (m["deployment_frequency"]["deploys"], sql["deploys"]),
    "per_day": (m["deployment_frequency"]["per_day"], sql["per_day"]),
    "median_deploy_days_per_week": (m["deployment_frequency"]["median_deploy_days_per_week"], sql["median_deploy_days_per_week"]),
    "changes": (m["lead_time"]["changes"], sql["changes"]),
    "lead_median": (m["lead_time"]["median_seconds"], sql["lead_median"]),
    "lead_p90": (m["lead_time"]["p90_seconds"], sql["lead_p90"]),
    "per_commit_median": (m["lead_time"]["per_commit_median_seconds"], sql["per_commit_median"]),
    "failed": (m["change_failure_rate"]["failed"], sql["failed"]),
    "cfr": (m["change_failure_rate"]["percent"], sql["cfr"]),
    "restored": (m["failed_deployment_recovery_time"]["restored"], sql["restored"]),
    "recovery_median": (m["failed_deployment_recovery_time"]["median_seconds"], sql["recovery_median"]),
    "recovery_p90": (m["failed_deployment_recovery_time"]["p90_seconds"], sql["recovery_p90"]),
    "incidents": (m["incident_time_to_restore"]["incidents"], sql["incidents"]),
    "mttr_median": (m["incident_time_to_restore"]["median_seconds"], sql["mttr_median"]),
}
wrong = {k: v for k, v in pairs.items() if (v[0] is None) != (v[1] is None) or (v[0] is not None and abs(v[0] - v[1]) > 1e-6)}
assert not wrong, "minidora vs SQL: %s" % wrong
weekly = {r["bucket"]: r["deploys"] for r in mine["view"]["rows"] if r["deploys"]}
assert weekly == sql["weekly"], (weekly, sql["weekly"])
partition = sorted([c["deploy"], c["head"], sorted(c["commits"])] for c in mine["changes"])
assert partition == sql["partition"], "change partition differs from git rev-list"
h = lambda s: "%.2f h" % (s / 3600)
print("duckdb %s agrees: %d deploys (%.2f/day), lead p50 %s p90 %s over %d changes, CFR %d/%d = %.2f%%, recovery p50 %s, MTTR p50 %s"
      % (sql["duckdb"], sql["deploys"], sql["per_day"], h(sql["lead_median"]), h(sql["lead_p90"]), sql["changes"],
         sql["failed"], sql["deploys"], sql["cfr"], h(sql["recovery_median"]), h(sql["mttr_median"])))
PY
}

echo "▸ minidora demo: a Git repository with a scripted history (merges, a hotfix, a rollback), a deploy log and an incident log"
CODE="$(exit_of "$WORK/demo.txt" python3 code/main.py demo "$WORK/base")"
[[ "$CODE" == "0" ]] || fail "demo exit $CODE: $(cat "$WORK/demo.txt" "$WORK/demo.txt.err")"
git -C "$WORK/base/repo" fsck --strict > "$WORK/fsck.txt" 2>&1 || fail "git fsck on the demo repository: $(cat "$WORK/fsck.txt")"
git -C "$WORK/base/repo" log --oneline --graph --first-parent -5 > "$WORK/graph.txt"
sed 's/^/  /' "$WORK/graph.txt"
git -C "$WORK/base/repo" rev-list --count --all > "$WORK/count.txt"
[[ "$(cat "$WORK/count.txt")" == "56" ]] || fail "expected 56 commits in the demo repository, got $(cat "$WORK/count.txt")"

echo "▸ minidora report: the four keys for 2026-09-07 to 2026-10-05 match the hand calculation"
report "$WORK/base" "$WORK/base.json"
check "hand-checked numbers" "$WORK/base.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
m = doc["metrics"]
assert m["deployment_frequency"]["deploys"] == 21 and m["deployment_frequency"]["level"] == "high", m["deployment_frequency"]
assert (m["lead_time"]["changes"], m["lead_time"]["median_seconds"]) == (22, 8 * 3600), m["lead_time"]
assert m["change_failure_rate"]["failed"] == 4 and abs(m["change_failure_rate"]["percent"] - 400 / 21) < 1e-9, m["change_failure_rate"]
assert m["failed_deployment_recovery_time"]["median_seconds"] == 9.5 * 3600, m["failed_deployment_recovery_time"]
causes = {d["id"]: d["causes"] for d in doc["deploys"] if d["failed"]}
assert causes == {"d-03": ["incident inc-1"], "d-06": ["rolled back by d-07"], "d-13": ["incident inc-2"], "d-19": ["incident inc-4"]}, causes
how = {i["id"]: (i["deploy"], i["how"]) for i in doc["incidents"]}
assert how["inc-2"] == ("d-13", "root cause") and how["inc-3"] == ("", "external") and how["inc-5"] == ("", "unlinked"), how
assert [w["id"] for w in doc["warnings"]] == ["empty-deploys", "unlinked-incidents"], doc["warnings"]
assert [a["id"] for a in doc["attempts"]] == ["d-09"], doc["attempts"]
print("21 deploys, lead p50 8.0 h over 22 changes, CFR 4/21 = 19.05%, recovery p50 9.5 h; inc-2 linked by root cause, not by the window")
PY

echo "▸ GitHub deployments API shape (deployments with their statuses) gives the same metrics as the deploy log"
CODE="$(exit_of "$WORK/github.json" python3 code/main.py report --repo "$WORK/base/repo" --deploys "$WORK/base/github-deployments.json" --incidents "$WORK/base/incidents.jsonl" --since "$SINCE" --until "$UNTIL" --json)"
[[ "$CODE" == "0" ]] || fail "report from github-deployments.json exit $CODE: $(cat "$WORK/github.json.err")"
check "GitHub deployments" "$WORK/base.json" "$WORK/github.json" <<'PY'
import json, sys
log, github = (json.load(open(p)) for p in sys.argv[1:3])
assert log["metrics"] == github["metrics"], (log["metrics"], github["metrics"])
print("same metrics from %d GitHub deployments (staging and the failed attempt filtered out)" % len(github["deploys"]))
PY

echo "▸ git rev-list and merge-base as the oracle for which commits each deploy shipped"
git_oracle "$WORK/base" "$WORK/oracle-base"
wc -l < "$WORK/oracle-base/change_commits.csv" | sed 's/^/  change_commits rows incl. header: /'
cat "$WORK/oracle-base/rollbacks.csv" | sed 's/^/  /'
grep -q '^d-07,d-06$' "$WORK/oracle-base/rollbacks.csv" || fail "git merge-base should mark d-07 as a rollback of d-06: $(cat "$WORK/oracle-base/rollbacks.csv")"

echo "▸ installing DuckDB 1.4.5 into a venv for the Four Keys style SQL (sha256 of the PyPI manylinux wheels, CPython 3.12 to 3.14, x86_64 and aarch64)"
python3 -m venv "$VENV" || fail "python3 -m venv failed"
cat > "$WORK/requirements.txt" <<'REQ'
duckdb==1.4.5 \
    --hash=sha256:aa294d028c149ca21110e366eaffcb4fc9ab11d7d203d50f7bc49a07ab34b960 \
    --hash=sha256:0c72b1dcf27a71ef5f3dc14b92b9ed9274c5584bb0e88590b78907cbb8e254f3 \
    --hash=sha256:a3569583e12d61f9b8446ca8a0e4ee25c2fe9b04c2b010c2e3bad26fc3d65882 \
    --hash=sha256:414d50b59864582cf00e503c316d7ca5a8577ee628c62fc203993eba2ad51a69 \
    --hash=sha256:64fe5e7ec74696788ce1e4157d1b70e45806756234c22c1a59bfcd28de1cae7b \
    --hash=sha256:52f429653701676df74ccfbfb05baf9ee8cf46d830353574872d053142d6b018
REQ
"$VENV/bin/pip" install --quiet --disable-pip-version-check --only-binary=:all: --require-hashes -r "$WORK/requirements.txt" > "$WORK/pip.log" 2>&1 || fail "pip install duckdb: $(tail -20 "$WORK/pip.log")"

echo "▸ DuckDB: deployments, changes, incidents and failures as tables; the SQL answers equal minidora's"
four_keys_sql "$WORK/base" "$WORK/oracle-base" "$WORK/base/incidents.jsonl"
agree "base" "$WORK/base.json" "$WORK/oracle-base/sql.json"

echo "▸ planted slow review: a pull request opened on day 1 and merged on day 21 moves lead time and nothing else"
python3 - code/fixtures/scenario.json "$WORK/slow-scenario.json" <<'PY'
import json, sys
scenario = json.load(open(sys.argv[1]))
scenario["changes"].append({"id": "pr-slow", "title": "Rework the tax rules", "author": "eve", "commits": [20, 21], "merged": 500})
json.dump(scenario, open(sys.argv[2], "w"))
PY
CODE="$(exit_of "$WORK/demo-slow.txt" python3 code/main.py demo "$WORK/slow" --scenario "$WORK/slow-scenario.json")"
[[ "$CODE" == "0" ]] || fail "demo with the slow review exit $CODE: $(cat "$WORK/demo-slow.txt.err")"
report "$WORK/slow" "$WORK/slow.json"
git_oracle "$WORK/slow" "$WORK/oracle-slow"
four_keys_sql "$WORK/slow" "$WORK/oracle-slow" "$WORK/slow/incidents.jsonl"
agree "slow review" "$WORK/slow.json" "$WORK/oracle-slow/sql.json"
check "slow review moves lead time" "$WORK/base.json" "$WORK/slow.json" <<'PY'
import json, sys
base, slow = (json.load(open(p))["metrics"] for p in sys.argv[1:3])
assert slow["lead_time"]["changes"] == base["lead_time"]["changes"] + 1, slow["lead_time"]
assert slow["lead_time"]["p90_seconds"] > base["lead_time"]["p90_seconds"], (base["lead_time"], slow["lead_time"])
for key in ("deployment_frequency", "change_failure_rate", "failed_deployment_recovery_time"):
    assert slow[key] == base[key], (key, base[key], slow[key])
print("lead p90 %.1f h -> %.1f h; deploys, failures and recovery unchanged" % (base["lead_time"]["p90_seconds"] / 3600, slow["lead_time"]["p90_seconds"] / 3600))
PY

echo "▸ planted failed deploy: an incident an hour after d-20 moves the change failure rate and recovery time, not lead time"
cp -R "$WORK/base" "$WORK/failed"
python3 - "$WORK/failed/incidents.jsonl" <<'PY'
import json, sys
with open(sys.argv[1], "a") as handle:
    handle.write(json.dumps({"id": "inc-6", "title": "Search returns stale results", "started": "2026-09-30T09:00:00Z", "resolved": "2026-09-30T15:00:00Z"}) + "\n")
PY
report "$WORK/failed" "$WORK/failed.json"
git_oracle "$WORK/failed" "$WORK/oracle-failed"
four_keys_sql "$WORK/failed" "$WORK/oracle-failed" "$WORK/failed/incidents.jsonl"
agree "failed deploy" "$WORK/failed.json" "$WORK/oracle-failed/sql.json"
check "failed deploy moves CFR and recovery" "$WORK/base.json" "$WORK/failed.json" <<'PY'
import json, sys
base, failed = (json.load(open(p)) for p in sys.argv[1:3])
b, f = base["metrics"], failed["metrics"]
assert f["change_failure_rate"]["failed"] == b["change_failure_rate"]["failed"] + 1 == 5, f["change_failure_rate"]
assert f["change_failure_rate"]["percent"] > b["change_failure_rate"]["percent"]
assert f["failed_deployment_recovery_time"]["restored"] == 5, f["failed_deployment_recovery_time"]
d20 = next(d for d in failed["deploys"] if d["id"] == "d-20")
assert d20["causes"] == ["incident inc-6"] and d20["recovery_seconds"] == 7 * 3600, d20
assert f["lead_time"] == b["lead_time"] and f["deployment_frequency"] == b["deployment_frequency"]
print("CFR %.2f%% -> %.2f%%, d-20 recovered after 7.0 h; lead time and deployment frequency unchanged" % (b["change_failure_rate"]["percent"], f["change_failure_rate"]["percent"]))
PY

echo "▸ gaming check: five redeploys of the same commit raise deployment frequency, ship nothing, and are flagged"
cp -R "$WORK/base" "$WORK/padded"
python3 - "$WORK/padded/deploys.jsonl" <<'PY'
import json, sys
events = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
last = [e for e in events if e["id"] == "d-21"][0]
with open(sys.argv[1], "a") as handle:
    for n in range(5):
        handle.write(json.dumps(dict(last, id="pad-%d" % n, time="2026-10-02T1%d:00:00Z" % n)) + "\n")
PY
report "$WORK/padded" "$WORK/padded.json"
check "padded deploys" "$WORK/base.json" "$WORK/padded.json" <<'PY'
import json, sys
base, padded = (json.load(open(p)) for p in sys.argv[1:3])
assert padded["metrics"]["deployment_frequency"]["deploys"] == 26, padded["metrics"]["deployment_frequency"]
assert padded["metrics"]["lead_time"] == base["metrics"]["lead_time"]
empty = [w for w in padded["warnings"] if w["id"] == "empty-deploys"]
assert empty and "pad-4" in empty[0]["message"], padded["warnings"]
print("deploys 21 -> 26 with no new change; warning: %s" % empty[0]["message"][:60])
PY

echo "SMOKE OK: platform/dora-metrics"
