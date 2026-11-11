---
name: log-oneliners
description: Answer questions from JSON Lines, CSV and space-separated logs on a box where only jq, awk, sed and coreutils are available, in constant memory, with numbers that are right and commands that can be rerun
version: 1.0.0
phase: scripting
lesson: scripting/stream-processing
tags: [jq, awk, sed, sort, xargs, logs, incident-response, json-lines, csv]
---

# Log one-liners that stay correct and stay small

Use this during an incident or an investigation when the question is "how many, which, since when" and the evidence is a log file or a directory of them. The rules: never load the whole file, never parse JSON with grep, check every count two ways, and write down the exact commands.

## 1. Know the input before you touch it

```bash
ls -lh /var/log/app/                                     # size decides the approach: anything over a few hundred MB, stream only
head -c 2000 access.jsonl; echo                          # format: JSON Lines, pretty JSON, CSV, space-separated?
head -n 3 access.jsonl | jq -c 'keys'                    # field names (keys sorts them; keys_unsorted keeps file order)
wc -l access.jsonl                                       # records, if one per line
jq -c 'select(type != "object")' access.jsonl | head     # stray non-object lines (stack traces, banners)
file access.csv; head -n 2 access.csv | cat -A | head    # separators, quoting, CRLF line endings (^M$)
```

If the file is gzip-compressed, stream it: `zcat access.jsonl.gz | jq ...`. Never `gunzip` a 20 GB file onto a nearly full disk during an incident.

## 2. Pick the tool by the question

| Question | Tool | Memory |
|---|---|---|
| Filter records, project fields | `jq -c 'select(...) \| {a, b}'`, `awk '$4 >= 500'` | one record |
| Count by one key | `jq -r .key \| sort \| uniq -c \| sort -rn`, or `awk '{ n[$k]++ } END { ... }'` | sort spills to disk; awk: one entry per distinct key |
| Sum, average, max per key | `awk -F, '{ s[$k] += $v; c[$k]++ } END { ... }'`, `jq -n 'reduce inputs as $r ({}; .[$r.k] += $r.v)'` | one entry per key |
| Percentiles | `jq -r .ms \| sort -n \| awk '{ a[NR] = $1 } END { print a[int(NR * 0.99)] }'` | awk holds the sorted column; fine for one numeric field |
| Group whole records, sort records | `jq -s 'group_by(.k)'` | the whole input: only on small files or after filtering |
| Join two files | `join` on sorted keys, or Python | stop using one-liners |
| Rewrite text in place | `sed -E 's/old/new/'` | one line; no fields, no arithmetic |

## 3. Tested one-liners

JSON Lines (`ts`, `method`, `path`, `status`, `ms`, `user`):

```bash
jq -c 'select(.status >= 500) | {ts, path, status}' access.jsonl
jq -r 'select(.status >= 500) | .path' access.jsonl | sort | uniq -c | sort -rn | head
jq -r '"\(.ts[0:16]) \(.status)"' access.jsonl | awk '$2 >= 500 { n[$1]++ } END { for (m in n) print m, n[m] }' | sort   # 5xx per minute
jq -r '[.ts, .method, .path, .status, .ms, (.user // "")] | @tsv' access.jsonl > access.tsv   # flatten once, then awk
jq -n 'reduce inputs as $r ({}; .[$r.path] += 1)' access.jsonl                                  # counts without -s
jq -c 'select(.user == null)' access.jsonl | wc -l                                              # null and missing both match
```

CSV with a header (`ts,method,path,status,bytes,ms,user`, no quoted commas):

```bash
awk -F, 'NR > 1 && $4 >= 500 { print $1, $3, $4 }' access.csv
awk -F, 'NR > 1 { s[$3] += $6; c[$3]++ } END { for (p in s) printf "%s %.1f %d\n", p, s[p] / c[p], c[p] }' access.csv | sort -k2,2nr
awk -F, 'NR > 1 && $6 + 0 > max { max = $6 + 0; at = $1 } END { print max, at }' access.csv
awk -F, -v OFS='\t' '{ $1 = $1; print }' access.csv > access.tsv
```

Combined log format with the response time appended (`ip - user [10/Nov/2026:13:55:36 +0000] "GET /path HTTP/1.1" status bytes referer agent ms`; the timestamp contains a space, so `$4` and `$5` are its two halves):

```bash
awk '$9 >= 500 { print $7 }' access.log | sort | uniq -c | sort -rn | head      # $9 status, $7 path
awk '{ ip[$1]++ } END { for (i in ip) print ip[i], i }' access.log | sort -rn | head
awk '{ t += $NF; if ($NF > 1000) slow++ } END { printf "avg %.1f ms, %d over 1s\n", t / NR, slow }' access.log
```

## 4. Make it parallel without corrupting output

```bash
split -l 1000000 access.jsonl part-                                          # or the rotated files you already have
ls part-* | xargs -P "$(nproc)" -I{} sh -c 'jq -r "select(.status >= 500) | .path" "$1" > "$1.out"' _ {}
cat part-*.out | sort | uniq -c | sort -rn | head
```

Each worker writes its own file. Workers sharing one stdout interleave their buffered writes in the middle of lines. With GNU parallel installed, `parallel -j "$(nproc)" --keep-order` does the same with ordered output.

## 5. Pitfalls that produce wrong numbers

| Symptom | Cause | Fix |
|---|---|---|
| Count too low on JSON | `grep '"status":5'` misses `"status": 5` and matches text inside other fields | parse with jq |
| Filter drops rows that should match | `$6 > "100"` is a string comparison | compare with a number: `$6 > 100`, or `$6 + 0` |
| Lines change separator after an edit | assigning a field rebuilds `$0` with `OFS` | set `OFS` in `BEGIN` or with `-v OFS=,` |
| Output order differs between machines | `for (k in a)` order is unspecified | pipe through `sort` |
| Wrong fields in CSV | quoted fields contain commas; `-F,` does not understand quotes | `gawk --csv`, or Python's `csv` module |
| Last field has a stray `\r` | CRLF line endings | `sub(/\r$/, "")` first, or `tr -d '\r'` |
| jq exits 0 after printing errors | jq's status reflects only the last input | check stderr: `2> err.txt; test ! -s err.txt` |
| Box starts swapping | `jq -s`, `sort` with a tiny `/tmp`, an awk array keyed by request ID | stream, `sort -T /bigdisk`, key by something with few values |
| Totals off by one | header line counted | `NR > 1`, or `tail -n +2` |

## 6. Check the number two ways

Every number that goes into an incident channel gets a second derivation with a different tool: a jq count and an awk count, or a streaming count and `wc -l` on a filtered file. If they disagree, the input has a surprise (missing fields, nulls, a second format) and finding it is part of the answer.

## 7. Report

State: the files and their sizes; the time window; for each question, the exact command, its output, and the cross-check command and output; anything excluded (unparseable lines, header rows, nulls) and how many; and which commands held more than one record in memory and why that was acceptable. Do not report a number you could only derive one way.
