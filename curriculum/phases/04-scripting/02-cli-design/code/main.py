"""ccops: an inventory CLI over a JSON file, built with cliforge. Data on stdout, diagnostics on stderr, documented exit codes, --json, --dry-run for every change, confirmations that refuse without a terminal, and config precedence it can explain."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cliforge
from cliforge import Arg, CliError, Command, Context, Flag, NotFound, Setting, UsageError

try:
    import fcntl
except ImportError:
    fcntl = None

VERSION = "1.0.0"
RELEASE_DATE = "2026-11-11"
INVENTORY_VERSION = 1
STATES = ("active", "maintenance", "retired")
STATE_STYLE = {"active": "green", "maintenance": "yellow", "retired": "dim"}
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
FIELDS = ("name", "env", "role", "ip", "state", "tags")
LOCK_POLL = 0.05


@dataclass(frozen=True)
class Host:
    name: str
    env: str
    role: str
    ip: str
    state: str = "active"
    tags: Tuple[str, ...] = ()

    def to_json(self) -> Dict[str, Any]:
        return {"name": self.name, "env": self.env, "role": self.role, "ip": self.ip, "state": self.state, "tags": list(self.tags)}


def host_problems(name: Any, env: Any, role: Any, ip: Any, state: Any, tags: Any) -> List[str]:
    problems = []
    if not isinstance(name, str) or not NAME_RE.match(name):
        problems.append(f"name {name!r} must be lowercase letters, digits and dashes (a DNS label)")
    for label, value in (("env", env), ("role", role)):
        if not isinstance(value, str) or not LABEL_RE.match(value):
            problems.append(f"{label} {value!r} must be a lowercase word such as prod or web")
    try:
        ipaddress.ip_address(ip if isinstance(ip, str) else "")
    except ValueError:
        problems.append(f"ip {ip!r} is not an IPv4 or IPv6 address")
    if state not in STATES:
        problems.append(f"state {state!r} must be one of {', '.join(STATES)}")
    if not isinstance(tags, (list, tuple)) or not all(isinstance(t, str) and LABEL_RE.match(t) for t in tags):
        problems.append(f"tags {tags!r} must be a list of lowercase words")
    return problems


def host_from_json(item: Any, where: str) -> Host:
    if not isinstance(item, dict):
        raise CliError(f"{where}: expected an object, got {type(item).__name__}")
    unknown = sorted(set(item) - set(FIELDS))
    if unknown:
        raise CliError(f"{where}: unknown field(s) {', '.join(unknown)}")
    values = {k: item.get(k) for k in FIELDS}
    values["state"] = item.get("state", "active")
    values["tags"] = item.get("tags", [])
    problems = host_problems(**values)
    if problems:
        raise CliError(f"{where}: " + "; ".join(problems))
    return Host(values["name"], values["env"], values["role"], values["ip"], values["state"], tuple(sorted(set(values["tags"]))))


def load_inventory(path: Path, create: bool = False) -> Dict[str, Host]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if create:
            return {}
        raise CliError(f"inventory {path} does not exist",
                       hint="create it with 'ccops hosts add', or point --inventory / CCOPS_INVENTORY at another file") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise CliError(f"cannot read inventory {path}: {exc}") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CliError(f"{path}:{exc.lineno}:{exc.colno}: not valid JSON: {exc.msg}") from None
    if not isinstance(data, dict) or not isinstance(data.get("hosts"), list):
        raise CliError(f"{path}: expected an object with a \"hosts\" list")
    if data.get("version", INVENTORY_VERSION) != INVENTORY_VERSION:
        raise CliError(f"{path}: inventory version {data.get('version')!r} is not supported (this ccops reads version {INVENTORY_VERSION})")
    hosts: Dict[str, Host] = {}
    for index, item in enumerate(data["hosts"]):
        host = host_from_json(item, f"{path}: hosts[{index}]")
        if host.name in hosts:
            raise CliError(f"{path}: hosts[{index}]: duplicate name {host.name!r}")
        hosts[host.name] = host
    return hosts


def render_inventory(hosts: Dict[str, Host]) -> str:
    return json.dumps({"version": INVENTORY_VERSION, "hosts": [hosts[n].to_json() for n in sorted(hosts)]}, indent=2) + "\n"


def save_inventory(path: Path, hosts: Dict[str, Host]) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(render_inventory(hosts))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        if temp.exists():
            temp.unlink()
        raise


@contextmanager
def locked(path: Path, timeout: float, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> Iterator[float]:
    lock_path = path.with_name(path.name + ".lock")
    started = clock()
    try:
        handle = open(lock_path, "a+")
    except OSError as exc:
        raise CliError(f"cannot create lock file {lock_path}: {exc.strerror}") from None
    try:
        while fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if clock() - started >= timeout:
                    raise CliError(f"{path} is locked by another writer; gave up after {timeout:g}s",
                                   hint=f"raise --lock-timeout, or find the holder with: lsof {lock_path}") from None
                sleep(LOCK_POLL)
        yield clock() - started
    finally:
        handle.close()


def inventory_path(ctx: Context) -> Path:
    return Path(ctx.value("inventory"))


def lock_for(ctx: Context):
    if ctx.dry_run:
        return nullcontext(0.0)
    return locked(inventory_path(ctx), ctx.value("lock_timeout"))


def report(ctx: Context, action: str, verb: str, names: Sequence[str], changed: bool, extra: Dict[str, Any]) -> None:
    if ctx.json:
        ctx.emit_json(dict({"action": action, "changed": changed, "dry_run": ctx.dry_run}, **extra))
        return
    if ctx.console.quiet:
        return
    for name in names:
        ctx.console.out(f"{verb} {name}")


def hosts_list(ctx: Context) -> int:
    hosts = load_inventory(inventory_path(ctx))
    wanted = {k: ctx.args.get(k) for k in ("env", "role", "state") if ctx.args.get(k)}
    selected = [hosts[n] for n in sorted(hosts) if all(getattr(hosts[n], k) == v for k, v in wanted.items())]
    if ctx.json:
        ctx.emit_json({"hosts": [h.to_json() for h in selected]})
        return cliforge.OK
    if not selected:
        ctx.console.note(f"no hosts match in {inventory_path(ctx)}")
        return cliforge.OK
    if ctx.console.quiet:
        for host in selected:
            ctx.console.out(host.name)
        return cliforge.OK
    rows = [("NAME", "ENV", "ROLE", "IP", "STATE", "TAGS")]
    rows += [(h.name, h.env, h.role, h.ip, h.state, ",".join(h.tags)) for h in selected]

    def paint(row: int, column: int, text: str) -> str:
        if row == 0:
            return ctx.console.paint(text, "bold")
        if column == 4:
            return ctx.console.paint(text, STATE_STYLE[text.strip()])
        return text

    for line in cliforge.table(rows, paint):
        ctx.console.out(line)
    return cliforge.OK


def find_host(ctx: Context, hosts: Dict[str, Host], name: str) -> Host:
    if name not in hosts:
        raise NotFound(f"host {name!r} not found in {inventory_path(ctx)}{cliforge.did_you_mean(name, hosts)}")
    return hosts[name]


def hosts_show(ctx: Context) -> int:
    host = find_host(ctx, load_inventory(inventory_path(ctx)), ctx.args["name"])
    if ctx.json:
        ctx.emit_json(host.to_json())
        return cliforge.OK
    for key, value in host.to_json().items():
        ctx.console.out(f"{key}: {','.join(value) if isinstance(value, list) else value}")
    return cliforge.OK


def host_from_args(args: Dict[str, Any]) -> Host:
    values = {"name": args["name"], "env": args.get("env"), "role": args.get("role"), "ip": args.get("ip"),
              "state": args.get("state", "active"), "tags": args.get("tag", [])}
    missing = [f"--{k}" for k in ("env", "role", "ip") if values[k] is None]
    if missing:
        raise UsageError(f"hosts add needs {', '.join(missing)}", hint="run 'ccops hosts add --help' for usage")
    problems = host_problems(**values)
    if problems:
        raise UsageError("; ".join(problems))
    return Host(values["name"], values["env"], values["role"], values["ip"], values["state"], tuple(sorted(set(values["tags"]))))


def differences(old: Host, new: Host) -> List[str]:
    return [f"{k} {getattr(old, k)!r} != {getattr(new, k)!r}" for k in FIELDS if getattr(old, k) != getattr(new, k)]


def hosts_add(ctx: Context) -> int:
    host = host_from_args(ctx.args)
    path = inventory_path(ctx)
    with lock_for(ctx) as waited:
        ctx.console.debug(f"lock acquired after {waited:.2f}s")
        hosts = load_inventory(path, create=True)
        existing = hosts.get(host.name)
        if existing == host:
            report(ctx, "add", "unchanged", [host.name], False, {"host": host.to_json()})
            return cliforge.OK
        if existing is not None:
            raise CliError(f"host {host.name!r} already exists with different values: {'; '.join(differences(existing, host))}",
                           hint="remove it first, or repeat the add with the same values")
        if not ctx.dry_run:
            hosts[host.name] = host
            save_inventory(path, hosts)
            ctx.console.debug(f"wrote {path} ({len(hosts)} hosts)")
    report(ctx, "add", "would add" if ctx.dry_run else "added", [host.name], True, {"host": host.to_json()})
    return cliforge.OK


def hosts_remove(ctx: Context) -> int:
    path = inventory_path(ctx)
    names = list(dict.fromkeys(ctx.args["names"]))
    hosts = load_inventory(path)
    present = [n for n in names if n in hosts]
    missing = [n for n in names if n not in hosts]
    if missing and not ctx.args.get("missing_ok"):
        raise NotFound(f"not in {path}: {', '.join(missing)}; nothing was removed", hint="--missing-ok treats absent hosts as already removed")
    if not present:
        report(ctx, "remove", "already absent", missing, False, {"removed": [], "missing": missing})
        return cliforge.OK
    if ctx.dry_run:
        report(ctx, "remove", "would remove", present, True, {"removed": present, "missing": missing})
        return cliforge.OK
    ctx.confirm(f"remove {len(present)} host(s) ({', '.join(present)}) from {path}")
    with lock_for(ctx) as waited:
        ctx.console.debug(f"lock acquired after {waited:.2f}s")
        hosts = load_inventory(path)
        removed = [n for n in present if n in hosts]
        for name in removed:
            del hosts[name]
        if removed:
            save_inventory(path, hosts)
    report(ctx, "remove", "removed", removed, bool(removed), {"removed": removed, "missing": missing + [n for n in present if n not in removed]})
    return cliforge.OK


def show_value(value: Any) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


def config_show(ctx: Context) -> int:
    resolved = list(ctx.settings.values())
    cfg = ctx.config_file
    if ctx.args.get("explain"):
        if ctx.json:
            ctx.emit_json({
                "config_file": {"path": str(cfg.path), "origin": cfg.origin, "exists": cfg.exists},
                "settings": [{"key": r.key, "value": r.value, "source": r.source, "origin": r.origin} for r in resolved],
                "color": {"enabled": ctx.console.color, "reason": ctx.color_reason},
            })
            return cliforge.OK
        rows = [("KEY", "VALUE", "SOURCE", "ORIGIN")] + [(r.key, show_value(r.value), r.source, r.origin) for r in resolved]
        for line in cliforge.table(rows, lambda row, column, text: ctx.console.paint(text, "bold") if row == 0 else text):
            ctx.console.out(line)
        ctx.console.out("")
        ctx.console.out(f"config file: {cfg.path} ({cfg.origin}, {'found' if cfg.exists else 'not found'})")
        ctx.console.out(f"colour on stdout: {'on' if ctx.console.color else 'off'} ({ctx.color_reason})")
        return cliforge.OK
    if ctx.json:
        ctx.emit_json({r.key: r.value for r in resolved})
        return cliforge.OK
    for r in resolved:
        ctx.console.out(f"{r.key} = {show_value(r.value)}")
    return cliforge.OK


def completion(ctx: Context) -> int:
    ctx.console.out(ctx.app.bash_completion())
    return cliforge.OK


def man(ctx: Context) -> int:
    ctx.console.out(ctx.app.man_page())
    return cliforge.OK


def build_app() -> cliforge.App:
    app = cliforge.App(
        "ccops", VERSION, "a small host inventory CLI built with cliforge",
        settings=[
            Setting("inventory", "inventory file", "inventory.json", metavar="PATH", kind="path", short="-i", complete="file"),
            Setting("lock_timeout", "seconds to wait for another writer's lock", "5", metavar="SECONDS", kind="seconds"),
        ],
        description="ccops keeps a list of hosts in a JSON file. Data goes to stdout, diagnostics to stderr, every change has --dry-run, and destructive changes ask on a terminal and refuse without one unless --yes is given.",
        release_date=RELEASE_DATE,
        files=[("inventory.json", "the default inventory; written atomically (temporary file, fsync, rename) under an flock on inventory.json.lock")],
    )
    filters = (
        Flag(("--env",), "only hosts in this environment", metavar="ENV"),
        Flag(("--role",), "only hosts with this role", metavar="ROLE"),
        Flag(("--state",), "only hosts in this state", metavar="STATE", choices=STATES),
    )
    host_arg = Arg("name", "NAME", "host name", complete=("hosts", "list", "--quiet"))
    app.add(Command(("hosts",), "list and change hosts"))
    app.add(Command(("hosts", "list"), "list hosts, optionally filtered", hosts_list, flags=filters,
                    examples=("ccops hosts list --env prod", "ccops hosts list --json | jq -r '.hosts[].ip'")))
    app.add(Command(("hosts", "show"), "show one host", hosts_show, args=(host_arg,), examples=("ccops hosts show web-1 --json",)))
    app.add(Command(("hosts", "add"), "add a host; repeating the same add changes nothing", hosts_add, changes=True, args=(Arg("name", "NAME", "host name"),),
                    flags=(Flag(("--env",), "environment, such as prod", metavar="ENV"),
                           Flag(("--role",), "role, such as web or db", metavar="ROLE"),
                           Flag(("--ip",), "IPv4 or IPv6 address", metavar="IP"),
                           Flag(("--state",), "lifecycle state (default active)", metavar="STATE", choices=STATES),
                           Flag(("--tag",), "a tag; repeat for several", metavar="TAG", repeat=True)),
                    examples=("ccops hosts add web-3 --env prod --role web --ip 10.0.1.13 --dry-run",)))
    app.add(Command(("hosts", "remove"), "remove hosts; asks first, refuses without a terminal unless --yes", hosts_remove,
                    changes=True, destructive=True,
                    args=(Arg("names", "NAME", "host names", many=True, complete=("hosts", "list", "--quiet")),),
                    flags=(Flag(("--missing-ok",), "exit 0 when a host is already absent"),),
                    examples=("ccops hosts remove web-3 --dry-run", "ccops hosts remove web-3 --yes")))
    app.add(Command(("config",), "print the effective settings; --explain shows where each came from", config_show,
                    flags=(Flag(("--explain",), "show the source of every value: flag, env, file or default"),),
                    examples=("ccops config --explain", "ccops config > ~/.config/ccops/config")))
    app.add(Command(("completion",), "print a Bash completion script", completion,
                    args=(Arg("shell", "SHELL", "the shell to complete for", choices=("bash",)),),
                    examples=("source <(ccops completion bash)",)))
    app.add(Command(("man",), "print the man page (roff)", man, examples=("ccops man > ccops.1 && man ./ccops.1",)))
    return app


def main(argv: Optional[Sequence[str]] = None, console: Optional[cliforge.Console] = None) -> int:
    return build_app().run(argv, console or cliforge.Console(prog="ccops"))


if __name__ == "__main__":
    raise SystemExit(main())
