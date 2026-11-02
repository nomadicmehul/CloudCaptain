"""Update README.md and i18n/README.*.md for a lesson batch.
usage: python3 readme_numbers.py <repo> <spec.json>
spec: {"header": {"lessons":[old,new], "phases":[old,new], "hours":[old,new], "tests":[old,new], "artifacts":[old,new]},
       "devops": [old,new], "why": [old,new],
       "rows": {"after": "| **07 ·", "en": "...", "de": "...", ...},
       "more": {"en": [old,new], ...}}
"""
import json, pathlib, re, sys
repo, spec = pathlib.Path(sys.argv[1]), json.loads(pathlib.Path(sys.argv[2]).read_text())
files = {"en": repo / "README.md"}
for p in sorted((repo / "i18n").glob("README.*.md")):
    files[p.name.split(".")[1]] = p
h = spec["header"]
for lang, p in files.items():
    lines = p.read_text().split("\n")
    i = next(n for n, l in enumerate(lines) if "<strong>" in l and "MCP" in l)
    l = lines[i]
    l, n1 = re.subn(rf"<strong>{h['lessons'][0]} ", f"<strong>{h['lessons'][1]} ", l, count=1)
    l, n2 = re.subn(rf"<strong>{h['phases'][0]} ", f"<strong>{h['phases'][1]} ", l, count=1)
    l, n3 = re.subn(rf"<strong>{h['hours'][0]} ", f"<strong>{h['hours'][1]} ", l, count=1)
    old_t, new_t = h["tests"]
    l, n4 = re.subn(rf"{old_t[:-4]}[,.]{old_t[-3:]}", lambda m: new_t[:-4] + m.group(0)[len(old_t[:-4])] + new_t[-3:], l, count=1)
    l, n5 = re.subn(rf"\b{h['artifacts'][0]}\b", str(h["artifacts"][1]), l, count=1)
    assert (n1, n2, n3, n4, n5) == (1, 1, 1, 1, 1), (lang, n1, n2, n3, n4, n5, l)
    lines[i] = l
    text = "\n".join(lines)
    text, n = re.subn(rf"(\(https://cloudcaptain\.io/learn/paths/devops\) \|[^\n]*\| ){spec['devops'][0]} \|", rf"\g<1>{spec['devops'][1]} |", text)
    assert n == 1, (lang, "devops")
    text, n = re.subn(rf"✅ {spec['why'][0]} ", f"✅ {spec['why'][1]} ", text, count=1)
    assert n == 1, (lang, "why")
    rows = spec["rows"]
    tl = text.split("\n")
    k = next(n for n, l in enumerate(tl) if l.startswith(rows["after"]))
    tl.insert(k + 1, rows[lang])
    text = "\n".join(tl)
    old, new = spec["more"][lang]
    assert text.count(old) == 1, (lang, "more", old)
    text = text.replace(old, new)
    p.write_text(text)
    print(lang, "ok")
