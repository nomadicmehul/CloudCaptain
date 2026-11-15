from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from .settings import PaasError, Settings

DEFAULT_TIMEOUT = 180


class NotBuiltYet(Exception):
    pass


def todo(milestone: str, command: str) -> int:
    raise NotBuiltYet(f"not implemented: milestone {milestone} ({command})")


def validate(settings: Settings, args: argparse.Namespace) -> int:
    return todo("app-manifest", "tinypaas validate <app-dir> --json")


def build(settings: Settings, args: argparse.Namespace) -> int:
    return todo("build-image", "tinypaas build <app-dir> --json")


def deploy(settings: Settings, args: argparse.Namespace) -> int:
    return todo("deploy", "tinypaas deploy <app-dir> --timeout SECONDS")


def url(settings: Settings, args: argparse.Namespace) -> int:
    return todo("deploy", "tinypaas url <app> --json")


def remote_add(settings: Settings, args: argparse.Namespace) -> int:
    return todo("git-push", "tinypaas remote add <app> <bare-repo-path>")


def config_set(settings: Settings, args: argparse.Namespace) -> int:
    return todo("config-scale", "tinypaas config set <app> KEY=VALUE")


def config_get(settings: Settings, args: argparse.Namespace) -> int:
    return todo("config-scale", "tinypaas config get <app> --json")


def scale(settings: Settings, args: argparse.Namespace) -> int:
    return todo("config-scale", "tinypaas scale <app> web=N")


def releases(settings: Settings, args: argparse.Namespace) -> int:
    return todo("releases-rollback", "tinypaas releases <app> --json")


def rollback(settings: Settings, args: argparse.Namespace) -> int:
    return todo("releases-rollback", "tinypaas rollback <app> [--to N]")


def logs(settings: Settings, args: argparse.Namespace) -> int:
    return todo("logs", "tinypaas logs <app> --since 5m --json")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tinypaas", description="A tiny Heroku-style PaaS on a kind cluster.")
    p.add_argument("--data-dir", help="where .tinypaas/ state lives (default: $TINYPAAS_HOME or your home directory)")
    sub = p.add_subparsers(dest="command", required=True)

    def command(name: str, handler, *positionals: str, timeout: bool = False) -> argparse.ArgumentParser:
        cmd = sub.add_parser(name)
        for positional in positionals:
            cmd.add_argument(positional)
        cmd.add_argument("--json", action="store_true", help="print JSON on stdout")
        if timeout:
            cmd.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds to wait for the rollout")
        cmd.set_defaults(handler=handler)
        return cmd

    command("validate", validate, "app_dir")
    command("build", build, "app_dir")
    command("deploy", deploy, "app_dir", timeout=True).add_argument("--app", help="refuse unless app.json names this app")
    command("url", url, "app")
    remote = sub.add_parser("remote").add_subparsers(dest="remote_command", required=True)
    add = remote.add_parser("add")
    add.add_argument("app")
    add.add_argument("path")
    add.add_argument("--json", action="store_true")
    add.set_defaults(handler=remote_add)
    config = sub.add_parser("config").add_subparsers(dest="config_command", required=True)
    setter = config.add_parser("set")
    setter.add_argument("app")
    setter.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    setter.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    setter.add_argument("--json", action="store_true")
    setter.set_defaults(handler=config_set)
    getter = config.add_parser("get")
    getter.add_argument("app")
    getter.add_argument("--json", action="store_true")
    getter.set_defaults(handler=config_get)
    command("scale", scale, "app", "formation", timeout=True)
    command("releases", releases, "app")
    command("rollback", rollback, "app", timeout=True).add_argument("--to", type=int)
    command("logs", logs, "app").add_argument("--since", default="5m")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    settings = Settings.load(args.data_dir, os.environ)
    try:
        return args.handler(settings, args)
    except (NotBuiltYet, PaasError) as exc:
        print(f"tinypaas: {exc}", file=sys.stderr)
        return 1
