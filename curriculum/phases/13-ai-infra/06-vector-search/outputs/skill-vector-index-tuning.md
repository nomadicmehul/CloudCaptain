---
name: vector-index-tuning
description: Measure and tune a vector index (pgvector HNSW or IVFFlat, Qdrant, Milvus) against exact search, so recall@k, filtered results, latency and memory are numbers you chose rather than defaults you inherited
version: 1.0.0
phase: ai-infra
lesson: ai-infra/vector-search
tags: [vector-search, pgvector, hnsw, ivfflat, recall, filtering, rag, retrieval, qdrant, milvus]
---

# Vector index tuning

Use this when retrieval answers got worse after an index was added, filtered queries return fewer rows than their `LIMIT`, retrieval latency misses its budget, before a RAG index goes live, and after a backfill, a re-embedding or a change of embedding model. Do not change index parameters before step 2 has produced a baseline: without ground truth every change is a guess.

## 0. Inputs

Collect before starting, and write them at the top of the report:

- `k` (the `LIMIT` the application uses) and the recall target, as a mean and as a per-query minimum (for example mean 0.95, min 0.8).
- The latency budget for retrieval (p95) and the query rate.
- At least 200 real queries from logs, with the filters they used. Synthetic queries give synthetic recall.
- The table or collection, the distance the embedding model was trained for (cosine, inner product or L2), and the index definition.

```bash
psql -c "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'items'"
psql -c "SHOW hnsw.ef_search" -c "SHOW ivfflat.probes" -c "SHOW hnsw.iterative_scan"
psql -c "SELECT count(*), pg_size_pretty(pg_total_relation_size('items')) FROM items"
```

Check that the operator matches the operator class (`<->` with `vector_l2_ops`, `<=>` with `vector_cosine_ops`, `<#>` with `vector_ip_ops`) and that the query orders by the distance ascending with a `LIMIT`. `ORDER BY 1 - (embedding <=> q) DESC` never uses the index.

## 1. Confirm the index is used

```sql
EXPLAIN (ANALYZE, BUFFERS) SELECT id FROM items WHERE <filters> ORDER BY embedding <=> '<query>' LIMIT 10;
```

| Plan shows | Meaning | Next |
|---|---|---|
| `Index Scan using <hnsw or ivfflat index>`, `Order By: (embedding <=> ...)` | approximate search | step 2 |
| `Seq Scan` + `Sort` | exact search; either no usable index or the planner thinks a scan is cheaper | small tables: fine, recall is 1.0. Large tables: fix the operator, operator class or ORDER BY form |
| `Index Scan` on a B-tree plus `Sort` | exact search over the rows matching the filter | this is pre-filtering; recall is 1.0, check latency only |
| `Filter:` under the vector index scan | post-filtering | step 4 |

## 2. Build the ground truth

Run every sample query exactly and keep the ids. In PostgreSQL disable index scans for the transaction:

```sql
BEGIN;
SET LOCAL enable_indexscan = off;
SELECT id FROM items WHERE <filters> ORDER BY embedding <=> '<query>' LIMIT 10;
COMMIT;
```

With the lesson's tool, `vecindex sql` writes the whole benchmark (exact first, then each setting) and `vecindex recall` grades the rows psql prints against brute force over an export of the table:

```bash
python3 vecindex.py sql --queries queries.jsonl --metric cosine --hnsw-ef 20,40,80,160,320 --ivf-probes "" --filter team=payments > bench.sql
psql -qAt -F $'\t' -v ON_ERROR_STOP=1 < bench.sql > results.tsv
python3 vecindex.py recall --data export.jsonl --queries queries.jsonl --results results.tsv --metric cosine --k 10
```

Run the benchmark against a copy or a replica: the script creates and drops an index named `items_embedding_ann`. Qdrant: the same query with `"params": {"exact": true}`. Milvus: a `FLAT` index on a copy of the collection.

## 3. Sweep the knob, pick the smallest value that meets the target

Sweep `hnsw.ef_search` (20, 40, 80, 160, 320) or `ivfflat.probes` (1, 2, 4, 8, 16, 32, up to half of `lists`). For each value record mean recall@k, minimum recall@k, mean rows returned and p95 latency from `EXPLAIN ANALYZE` or the application.

| What you see | Meaning | Action |
|---|---|---|
| mean and min meet the target at the default | the default is fine | keep it; record the margin |
| mean meets the target, min does not | a few queries sit in sparse or border regions | raise the knob until min meets the target, or accept the min explicitly in the report |
| recall flat below target as the knob grows | the structure is the limit, not the search | HNSW: rebuild with a larger `m` or `ef_construction`. IVF: rebuild with lists chosen for the current size; check step 5 |
| recall meets the target but latency does not | too much of the index is read, or it is not in memory | lower the knob to the minimum that meets the target; check index size against memory (step 6); consider `halfvec` or binary quantisation with re-ranking |
| HNSW returns fewer rows than `LIMIT` without any filter | `ef_search` is below `LIMIT`, or dead tuples | set `ef_search` to at least `LIMIT`; step 5 |

Set the chosen value where the query runs: `SET LOCAL hnsw.ef_search = N` inside the transaction, or `ALTER DATABASE ... SET hnsw.ef_search = N`, or `hnsw_ef` in the Qdrant search request.

## 4. Filtered queries

For each filter in the sample, compute its selectivity (matching rows over total) and the mean rows returned.

| Situation | Choice |
|---|---|
| selectivity × `ef_search` ≥ `LIMIT` with room to spare | post-filtering is fine; keep the HNSW index |
| rows returned below `LIMIT`, selectivity between about 1% and 20% | `SET hnsw.iterative_scan = relaxed_order` (or `strict_order`), check that `hnsw.max_scan_tuples` (default 20000) is not the limit; IVFFlat: `ivfflat.iterative_scan = relaxed_order` and `ivfflat.max_probes` |
| few matching rows (an exact scan of them fits the latency budget) | pre-filter: a B-tree on the filter column so the planner searches matching rows exactly, or a partial HNSW index per hot value |
| one value per tenant, many tenants | list partitioning by tenant, which also stops one tenant's data from changing another's recall |
| Qdrant | create a payload index on the field before loading data, so filterable HNSW adds its extra edges; the planner switches to a full scan below `full_scan_threshold` |

Re-run step 3 for the filtered queries after the change. Iterative scans can return rows slightly out of order with `relaxed_order`; re-sort in a materialised CTE if the order matters.

## 5. Churn and drift

```sql
SELECT n_live_tup, n_dead_tup, last_autovacuum, last_autoanalyze FROM pg_stat_user_tables WHERE relname = 'items';
```

- Dead tuples above about 10% of live tuples on a table with an HNSW index: index scans return dead rows that the heap then discards, so results come up short. Vacuum; for a large HNSW index `REINDEX INDEX CONCURRENTLY` first, then `VACUUM`.
- IVFFlat built when the table was much smaller (more than about twice smaller), or before a change of embedding model: the centroids describe old data. Rebuild with `lists` near `rows / 1000` up to a million rows and `sqrt(rows)` above, and start `probes` near `sqrt(lists)`.
- A new embedding model: a new column and a new index, never mixed vectors in one index. Ground truth (step 2) must be rebuilt too.

## 6. Memory

```sql
SELECT pg_size_pretty(pg_relation_size('<index>')), current_setting('shared_buffers');
```

```bash
python3 vecindex.py estimate --n <rows> --dim <dims> --m 16 --nlist <lists> --nprobe <probes> --hnsw-evals <from step 3>
```

Vectors take `4 × dims + 8` bytes each in pgvector (`halfvec` halves that); HNSW adds links per vector on top. If the index does not fit in memory, latency is set by disk reads, not by `ef_search`. Options: `halfvec` columns or a half-precision expression index, binary quantisation (`binary_quantize(embedding)::bit(N)` with `bit_hamming_ops`) with re-ranking on the full vectors, or more memory. Build large HNSW indexes with `maintenance_work_mem` large enough to hold the graph and, in containers, `--shm-size` at least as large.

## 7. Gate it

Keep the sample queries and their exact ids in the repository and run the grading in CI or on a schedule against a replica:

```bash
python3 vecindex.py recall --data export.jsonl --queries queries.jsonl --results results.tsv --k 10 --min-recall 0.95   # exit 1 below target
```

## 8. Report

State: the inputs from step 0; the plan type from step 1; a table of knob value against mean recall, min recall, rows returned and p95 latency, unfiltered and per filter; the chosen settings and where they are set; dead-tuple ratio and IVF growth since build; index size against memory; any rebuild done or recommended with its expected duration; and the date and query sample used, so the next review can compare. Never report recall without saying how the ground truth was computed.
