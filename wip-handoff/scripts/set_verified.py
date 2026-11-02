import json, pathlib, sys
date = sys.argv[1]
for f in sys.argv[2:]:
    p = pathlib.Path(f); raw = p.read_text(); d = json.loads(raw)
    assert "last_verified" not in d, f
    out = {}
    for k, v in d.items():
        out[k] = v
        if k == "smoke":
            out["last_verified"] = date
    assert "last_verified" in out, f
    new = json.dumps(out, indent=2, ensure_ascii=False) + "\n"
    old_again = json.dumps(d, indent=2, ensure_ascii=False) + "\n"
    assert old_again == raw, "formatting differs in " + f
    p.write_text(new); print("set", f)
