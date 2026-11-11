import json
import sys

failed = 0
for name in sys.argv[1:]:
    try:
        with open(name, encoding="utf-8") as handle:
            json.load(handle)
    except ValueError as error:
        print(f"{name}: {error}")
        failed = 1
sys.exit(failed)
