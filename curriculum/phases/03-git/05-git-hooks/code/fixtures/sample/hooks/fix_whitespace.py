import sys

for name in sys.argv[1:]:
    with open(name, "rb") as handle:
        original = handle.read()
    fixed = b"\n".join(line.rstrip(b" \t") for line in original.split(b"\n"))
    if fixed and not fixed.endswith(b"\n"):
        fixed += b"\n"
    if fixed != original:
        with open(name, "wb") as handle:
            handle.write(fixed)
        print(f"Fixing {name}")
sys.exit(0)
