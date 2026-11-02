from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    print("TODO", argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
