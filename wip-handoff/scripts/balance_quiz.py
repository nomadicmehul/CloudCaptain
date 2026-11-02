"""Move each quiz's correct answers to a balanced, lesson-specific set of positions (deterministic per lesson id).
usage: python3 balance_quiz.py <quiz.json>...
"""
import json, pathlib, random, sys
for f in sys.argv[1:]:
    p = pathlib.Path(f); raw = p.read_text(); q = json.loads(raw)
    qs = q["questions"]; rng = random.Random(q.get("lesson") or f)
    targets = [0, 1, 2, 3]; rng.shuffle(targets)
    while len(targets) < len(qs):
        targets.append(rng.choice([0, 1, 2, 3]))
    before = [x["answer"] for x in qs]
    for item, t in zip(qs, targets):
        n = len(item["choices"])
        t = t % n
        a = item["answer"]
        if a != t:
            item["choices"][a], item["choices"][t] = item["choices"][t], item["choices"][a]
            item["answer"] = t
    p.write_text(json.dumps(q, indent=2, ensure_ascii=False) + "\n")
    print(f"{f.split('/phases/')[-1]}: {before} -> {[x['answer'] for x in qs]}")
