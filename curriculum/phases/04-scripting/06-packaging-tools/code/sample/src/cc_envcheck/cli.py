import argparse
import json
import os
import sys

from cc_colors import paint

from cc_envcheck import __version__


def installed_version():
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:
        return __version__
    try:
        return version("cc-envcheck")
    except PackageNotFoundError:
        return __version__


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cc-envcheck", description="Check that environment variables are set.")
    parser.add_argument("names", nargs="*")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        print(f"cc-envcheck {installed_version()}")
        return 0
    if not args.names:
        parser.print_usage(sys.stderr)
        return 2
    missing = [name for name in args.names if not os.environ.get(name)]
    if args.json:
        print(json.dumps({"checked": args.names, "missing": missing}))
    else:
        for name in args.names:
            print(paint(f"missing {name}", "red") if name in missing else paint(f"ok {name}", "green"))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
