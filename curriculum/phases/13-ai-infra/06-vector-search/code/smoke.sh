#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
PG="cc-smoke-pgvector-$$"
IMAGE="${PGVECTOR_IMAGE:-pgvector/pgvector:0.8.7-pg18}"
cleanup() {
  if command -v docker >/dev/null; then docker rm -f "$PG" >/dev/null 2>&1 || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

vec() { python3 code/main.py "$@"; }
lines() { echo $(( $(wc -l < "$1") )); }

echo "▸ generate 4000 seeded vectors (32 dims, 50 overlapping clusters, tag = id % 10) and 50 queries"
vec generate --n 4000 --dim 32 --clusters 50 --spread 0.8 --queries 50 --seed 7 --out "$WORK/data.jsonl" --queries-out "$WORK/queries.jsonl" --pgcopy "$WORK/items.tsv"
[[ "$(lines "$WORK/data.jsonl")" == "4000" && "$(lines "$WORK/queries.jsonl")" == "50" && "$(lines "$WORK/items.tsv")" == "4000" ]] || fail "unexpected line counts: $(wc -l "$WORK"/*)"

echo "▸ vecindex HNSW (M=16, efConstruction=64): recall@10 against brute force rises with ef_search"
vec build --data "$WORK/data.jsonl" --index hnsw --m 16 --ef-construction 64 --seed 1 -o "$WORK/hnsw.json" --json > "$WORK/hnsw-build.json"
vec sweep --snapshot "$WORK/hnsw.json" --queries "$WORK/queries.jsonl" --k 10 --values 10,20,40,80,200 > "$WORK/v-hnsw.txt"
cat "$WORK/v-hnsw.txt"
vec sweep --snapshot "$WORK/hnsw.json" --queries "$WORK/queries.jsonl" --k 10 --values 10,20,40,80,200 --json > "$WORK/v-hnsw.json"

echo "▸ vecindex IVF (40 lists, k-means): recall@10 rises with probes"
vec build --data "$WORK/data.jsonl" --index ivf --nlist 40 --seed 1 -o "$WORK/ivf.json" > /dev/null
vec sweep --snapshot "$WORK/ivf.json" --queries "$WORK/queries.jsonl" --k 10 --values 1,2,4,8,20 --json > "$WORK/v-ivf.json"

echo "▸ vecindex filters on tag=3 (10% of rows): post-filtering comes up short, iterative and pre return k"
vec sweep --snapshot "$WORK/hnsw.json" --queries "$WORK/queries.jsonl" --k 10 --values 40 --filter tag=3 --filter-modes post,iterative,pre --json > "$WORK/v-hnsw-filter.json"
vec sweep --snapshot "$WORK/ivf.json" --queries "$WORK/queries.jsonl" --k 10 --values 1 --filter tag=3 --filter-modes post,iterative --json > "$WORK/v-ivf-filter.json"
python3 - "$WORK/v-hnsw.json" "$WORK/v-ivf.json" "$WORK/v-hnsw-filter.json" "$WORK/v-ivf-filter.json" > "$WORK/v-check.txt" 2>&1 <<'PY' || fail "vecindex curves: $(cat "$WORK/v-check.txt")"
import json, sys
hnsw, ivf, hnsw_f, ivf_f = ({r["label"]: r for r in json.load(open(p))["rows"]} for p in sys.argv[1:5])
def rising(rows, labels, top, name):
    values = [rows[l]["recall"] for l in labels]
    print(f"  {name}: " + "  ".join(f"{l.split('=')[1]}:{v:.3f}" for l, v in zip(labels, values)))
    assert all(b >= a - 0.03 for a, b in zip(values, values[1:])), f"{name} recall should not fall as the knob grows: {values}"
    assert values[-1] >= values[0] + 0.03, f"{name} recall should rise: {values}"
    assert values[-1] >= top, f"{name} recall at the top setting should be at least {top}: {values}"
rising(hnsw, [f"hnsw:ef_search={v}" for v in (10, 20, 40, 80, 200)], 0.95, "HNSW ef_search")
rising(ivf, [f"ivf:probes={v}" for v in (1, 2, 4, 8, 20)], 0.9, "IVF probes")
post, iterative, pre = (hnsw_f[f"hnsw:ef_search=40:{m}"] for m in ("post", "iterative", "pre"))
print(f"  HNSW tag=3 ef_search=40: post returns {post['returned']} rows (recall {post['recall']}), iterative {iterative['returned']} ({iterative['recall']}, scanned {iterative['scanned']:.0%}), pre {pre['returned']} ({pre['recall']}, scanned {pre['scanned']:.0%})")
assert post["returned"] <= 6, post
assert iterative["returned"] == 10 and iterative["recall"] >= 0.95, iterative
assert pre["returned"] == 10 and pre["recall"] == 1.0, pre
ivf_post, ivf_iter = ivf_f["ivf:probes=1:post"], ivf_f["ivf:probes=1:iterative"]
print(f"  IVF tag=3 probes=1: post returns {ivf_post['returned']} rows, iterative {ivf_iter['returned']}")
assert ivf_post["returned"] < 10 and ivf_iter["returned"] == 10, (ivf_post, ivf_iter)
PY
cat "$WORK/v-check.txt"

echo "▸ vecindex snapshot: delete leaves a tombstone, upsert makes the query vector its own nearest neighbour"
cp "$WORK/hnsw.json" "$WORK/mut.json"
Q="$(python3 -c 'import json,sys; print(json.dumps(json.loads(open(sys.argv[1]).readline())["vector"]).replace(" ", ""))' "$WORK/queries.jsonl")"
vec search --index "$WORK/mut.json" --vector "$Q" --k 10 --json > "$WORK/s1.json"
TOP="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["hits"][0]["id"])' "$WORK/s1.json")"
vec delete --index "$WORK/mut.json" --id "$TOP" > "$WORK/del.txt"
grep -q "1 tombstones" "$WORK/del.txt" || fail "delete should report one tombstone: $(cat "$WORK/del.txt")"
vec search --index "$WORK/mut.json" --vector "$Q" --k 10 --json > "$WORK/s2.json"
python3 - "$WORK/s2.json" "$TOP" <<'PY' || fail "deleted id $TOP still returned: $(cat "$WORK/s2.json")"
import json, sys
assert sys.argv[2] not in [h["id"] for h in json.load(open(sys.argv[1]))["hits"]]
PY
vec upsert --index "$WORK/mut.json" --id probe --vector "$Q" --meta '{"tag": 99}' > /dev/null
vec search --index "$WORK/mut.json" --vector "$Q" --k 1 --json > "$WORK/s3.json"
python3 - "$WORK/s3.json" <<'PY' || fail "upserted vector should be the nearest neighbour at distance 0: $(cat "$WORK/s3.json")"
import json, sys
hit = json.load(open(sys.argv[1]))["hits"][0]
assert hit["id"] == "probe" and hit["distance"] == 0.0 and hit["meta"] == {"tag": 99}, hit
PY
echo "  deleted $TOP, upserted probe; the snapshot survives both edits"

echo "▸ estimate: one million 768-dimension float32 vectors"
vec estimate --n 1000000 --dim 768 --nlist 1000 --nprobe 10 --hnsw-evals 2000 --json > "$WORK/estimate.json"
vec estimate --n 1000000 --dim 768 --nlist 1000 --nprobe 10 --hnsw-evals 2000
python3 - "$WORK/estimate.json" <<'PY' || fail "estimate: $(cat "$WORK/estimate.json")"
import json, sys
rows = {r["index"]: r for r in json.load(open(sys.argv[1]))["rows"]}
assert rows["flat"]["memory"]["vectors"] == 3_072_000_000, rows["flat"]
assert rows["ivf"]["evals"] == 11000, rows["ivf"]
assert rows["flat"]["latency_ms"] > rows["ivf"]["latency_ms"] > rows["hnsw"]["latency_ms"], rows
PY

if ! command -v docker >/dev/null; then
  echo "  (docker not installed; pgvector comparison skipped)"
  echo "SMOKE OK: ai-infra/vector-search (vecindex only)"
  exit 0
fi

echo "▸ Use It: pgvector in Docker ($IMAGE)"
docker run -d --name "$PG" --shm-size=256m -e POSTGRES_PASSWORD=smoke -e POSTGRES_DB=vec "$IMAGE" > /dev/null || fail "docker run $IMAGE"
READY=0
for _ in $(seq 1 90); do
  if docker exec "$PG" pg_isready -h 127.0.0.1 -U postgres -d vec > /dev/null 2>&1; then READY=1; break; fi
  sleep 1
done
[[ "$READY" == "1" ]] || { docker logs "$PG" 2>&1 | tail -30; fail "postgres in $PG did not accept TCP connections within 90s"; }
pg() { docker exec -e PGPASSWORD=smoke "$PG" psql -h 127.0.0.1 -U postgres -d vec -v ON_ERROR_STOP=1 -qAt "$@"; }
pg_in() { docker exec -i -e PGPASSWORD=smoke "$PG" psql -h 127.0.0.1 -U postgres -d vec -v ON_ERROR_STOP=1 -qAt "$@"; }

pg -c "CREATE EXTENSION IF NOT EXISTS vector" || fail "CREATE EXTENSION vector"
EXT="$(pg -c "SELECT extversion FROM pg_extension WHERE extname = 'vector'")" || fail "cannot query pg_extension"
[[ -n "$EXT" ]] || fail "the vector extension is not installed"
echo "  pgvector $EXT on PostgreSQL $(pg -c 'SHOW server_version')"
pg -c "CREATE TABLE items (id text NOT NULL, meta jsonb NOT NULL, embedding vector(32) NOT NULL)" || fail "CREATE TABLE"
pg_in -c "COPY items (id, meta, embedding) FROM STDIN" < "$WORK/items.tsv" || fail "COPY items from $WORK/items.tsv"
COUNT="$(pg -c "SELECT count(*) FROM items")" || fail "cannot count items"
[[ "$COUNT" == "4000" ]] || fail "expected 4000 rows, got $COUNT"
pg -c "ANALYZE items" || fail "ANALYZE items"

echo "▸ EXPLAIN: with an HNSW index and enable_seqscan off, ORDER BY embedding <-> q LIMIT 10 is an index scan, and a WHERE is a filter after it"
pg -c "CREATE INDEX items_embedding_ann ON items USING hnsw (embedding vector_l2_ops) WITH (m = 16, ef_construction = 64)" || fail "CREATE INDEX hnsw"
pg -c "SET enable_seqscan = off" -c "EXPLAIN (COSTS OFF) SELECT id FROM items WHERE meta @> '{\"tag\": 3}'::jsonb ORDER BY embedding <-> '$Q' LIMIT 10" > "$WORK/explain.txt" || fail "EXPLAIN"
sed 's/^/  /' "$WORK/explain.txt" | cut -c1-120
grep -q "Index Scan using items_embedding_ann" "$WORK/explain.txt" || fail "plan should use the HNSW index: $(cat "$WORK/explain.txt")"
grep -q "Filter" "$WORK/explain.txt" || fail "plan should apply the tag filter after the index scan: $(cat "$WORK/explain.txt")"
EF5="$(pg -c "SET enable_seqscan = off" -c "SET hnsw.ef_search = 5" -c "SELECT count(*) FROM (SELECT id FROM items ORDER BY embedding <-> '$Q' LIMIT 10) AS r" | tail -n 1)" || fail "the hnsw.ef_search = 5 query failed"
[[ "$EF5" =~ ^[0-9]+$ && "$EF5" -le 5 ]] || fail "with hnsw.ef_search = 5 an HNSW scan returns at most 5 rows, got: $EF5"
VEF5="$(vec search --index "$WORK/hnsw.json" --vector "$Q" --k 10 --ef 5 --json | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["hits"]))')"
echo "  hnsw.ef_search = 5 with LIMIT 10: pgvector returned $EF5 rows, vecindex $VEF5"
[[ "$VEF5" == "5" ]] || fail "vecindex should also cap results at ef_search: $VEF5"
SIZE="$(pg -c "SELECT pg_relation_size('items_embedding_ann')")" || fail "cannot read the index size"
EST="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["memory_estimate"]["total"])' "$WORK/hnsw-build.json")"
echo "  HNSW index on disk: $SIZE bytes; vecindex estimate for vectors + links: $EST bytes (pages, tuple headers and 6-byte heap pointers make up the difference)"
pg -c "DROP INDEX items_embedding_ann" || fail "DROP INDEX"

echo "▸ the same 50 queries through pgvector: exact, HNSW per hnsw.ef_search, IVFFlat per ivfflat.probes, filtered with iterative scans off and on"
vec sql --queries "$WORK/queries.jsonl" --k 10 --metric l2 --hnsw-ef 10,20,40,80,200 --ivf-probes 1,2,4,8,20 --m 16 --ef-construction 64 --lists 40 --filter tag=3 --filter-ef 40 --filter-probes 1 > "$WORK/bench.sql"
pg_in -F $'\t' < "$WORK/bench.sql" > "$WORK/pg.tsv" 2> "$WORK/pg.err" || fail "bench.sql failed: $(tail -5 "$WORK/pg.err")"
[[ "$(lines "$WORK/pg.tsv")" == "750" ]] || fail "expected 750 result rows (15 runs x 50 queries), got $(lines "$WORK/pg.tsv"): $(head -3 "$WORK/pg.tsv") $(tail -5 "$WORK/pg.err")"
vec recall --data "$WORK/data.jsonl" --queries "$WORK/queries.jsonl" --results "$WORK/pg.tsv" --k 10 > "$WORK/pg-recall.txt"
cat "$WORK/pg-recall.txt"
vec recall --data "$WORK/data.jsonl" --queries "$WORK/queries.jsonl" --results "$WORK/pg.tsv" --k 10 --json > "$WORK/pg-recall.json"

echo "▸ compare: pgvector's curves move the way vecindex's do"
python3 - "$WORK/pg-recall.json" "$WORK/v-hnsw.json" "$WORK/v-ivf.json" "$WORK/v-hnsw-filter.json" "$WORK/v-ivf-filter.json" > "$WORK/compare.txt" 2>&1 <<'PY' || fail "pgvector versus vecindex: $(cat "$WORK/compare.txt")"
import json, sys
pg = {r["label"]: r for r in json.load(open(sys.argv[1]))["rows"]}
mine = {}
for path in sys.argv[2:]:
    mine.update({r["label"]: r for r in json.load(open(path))["rows"]})
print(f"  {'label':<30} {'pgvector':>9} {'vecindex':>9} {'pg rows':>8} {'vec rows':>9}")
for label, row in pg.items():
    other = mine.get(label)
    print(f"  {label:<30} {row['recall']:>9.3f} {other['recall'] if other else float('nan'):>9.3f} {row['returned']:>8.1f} {other['returned'] if other else float('nan'):>9.1f}")
assert pg["exact"]["recall"] >= 0.99, ("pgvector's sequential scan and vecindex's brute force should agree", pg["exact"])
def rising(labels, top, name):
    values = [pg[l]["recall"] for l in labels]
    assert all(b >= a - 0.03 for a, b in zip(values, values[1:])), f"pgvector {name}: recall should not fall as the knob grows: {values}"
    assert values[-1] >= values[0] + 0.03, f"pgvector {name}: recall should rise: {values}"
    assert values[-1] >= top, f"pgvector {name}: recall at the top setting should be at least {top}: {values}"
    gaps = [abs(pg[l]["recall"] - mine[l]["recall"]) for l in labels]
    print(f"  {name}: both rise; largest gap between pgvector and vecindex {max(gaps):.3f}" + ("  (note: large gap, different graphs or centroids)" if max(gaps) > 0.15 else ""))
rising([f"hnsw:ef_search={v}" for v in (10, 20, 40, 80, 200)], 0.95, "hnsw.ef_search")
rising([f"ivf:probes={v}" for v in (1, 2, 4, 8, 20)], 0.85, "ivfflat.probes")
post, iterative = pg["hnsw:ef_search=40:post"], pg["hnsw:ef_search=40:iterative"]
assert post["returned"] <= 6 and mine["hnsw:ef_search=40:post"]["returned"] <= 6, ("post-filtering after an ef_search=40 scan keeps about 4 of 40 candidates", post)
assert iterative["returned"] == 10 and iterative["recall"] >= 0.9, ("hnsw.iterative_scan = relaxed_order should fill LIMIT 10", iterative)
ivf_post, ivf_iter = pg["ivf:probes=1:post"], pg["ivf:probes=1:iterative"]
assert ivf_post["returned"] < 10 and ivf_iter["returned"] == 10, ("ivfflat.iterative_scan should keep probing until LIMIT 10 is met", ivf_post, ivf_iter)
print(f"  filtered: hnsw post {post['returned']} rows vs iterative {iterative['returned']}; ivfflat post {ivf_post['returned']} vs iterative {ivf_iter['returned']}")
PY
cat "$WORK/compare.txt"

echo "SMOKE OK: ai-infra/vector-search"
