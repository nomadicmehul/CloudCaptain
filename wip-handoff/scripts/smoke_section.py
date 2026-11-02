import sys, re
log, lesson = sys.argv[1], sys.argv[2]
lines = [l.rstrip("\n")[29:] if re.match(r"^\d{4}-\d\d-\d\dT", l) else l.rstrip("\n") for l in open(log, encoding="utf-8", errors="replace")]
out, on = [], False
for l in lines:
    if "[smoke] $ bash code/smoke.sh" in l:
        on = (" " + lesson + " ") in (l + " ")
    if on:
        out.append(l)
tail = int(sys.argv[3]) if len(sys.argv) > 3 else 40
print("\n".join(out[-tail:]))
