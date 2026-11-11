"""cliforge: declare commands, flags and settings once, and get argparse parsing, flag > env > file > default precedence with an explanation, TTY-aware output, confirmations, help, Bash completion and a man page from the same model."""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, TextIO, Tuple

OK = 0
FAILURE = 1
USAGE = 2
NOT_FOUND = 3
REFUSED = 4
INTERRUPTED = 130
BROKEN_PIPE = 141

EXIT_CODES: Tuple[Tuple[int, str, str], ...] = (
    (OK, "ok", "the command did what was asked, or there was nothing to do"),
    (FAILURE, "failure", "the command ran and failed: unreadable data, a conflict, a lock timeout"),
    (USAGE, "usage", "the invocation was wrong: unknown command or flag, bad value, bad config file"),
    (NOT_FOUND, "not found", "a named object does not exist"),
    (REFUSED, "refused", "a destructive change was not confirmed"),
    (INTERRUPTED, "interrupted", "Ctrl-C (128 + SIGINT)"),
    (BROKEN_PIPE, "broken pipe", "the reader of stdout went away (128 + SIGPIPE)"),
)

SOURCES = ("flag", "env", "file", "default")
STYLES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33"}
HELP_COLUMN_MIN = 24
HELP_WIDTH = 80


class CliError(Exception):
    code = FAILURE

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


class UsageError(CliError):
    code = USAGE


class NotFound(CliError):
    code = NOT_FOUND


class Refused(CliError):
    code = REFUSED


def did_you_mean(word: str, candidates: Iterable[str]) -> str:
    close = difflib.get_close_matches(word, sorted(candidates), n=1, cutoff=0.6)
    return f"; did you mean {close[0]!r}?" if close else ""


def is_tty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def color_decision(mode: str, stream: Any, environ: Mapping[str, str], name: str = "stdout") -> Tuple[bool, str]:
    if mode == "always":
        return True, "color is always"
    if mode == "never":
        return False, "color is never"
    if environ.get("NO_COLOR", ""):
        return False, "NO_COLOR is set"
    if environ.get("TERM") == "dumb":
        return False, "TERM is dumb"
    if not is_tty(stream):
        return False, f"{name} is not a terminal"
    return True, f"{name} is a terminal"


@dataclass(frozen=True)
class Setting:
    key: str
    help: str
    default: str
    metavar: str = "VALUE"
    choices: Tuple[str, ...] = ()
    kind: str = "str"
    short: str = ""
    complete: str = ""

    @property
    def flag(self) -> str:
        return "--" + self.key.replace("_", "-")

    def env(self, prefix: str) -> str:
        return f"{prefix}_{self.key.upper()}"

    def convert(self, raw: str, origin: str, base: Optional[Path] = None) -> Any:
        text = raw.strip()
        if self.choices and text not in self.choices:
            raise UsageError(f"invalid {self.key} {raw!r} from {origin} (choose from {', '.join(self.choices)})")
        if self.kind == "seconds":
            try:
                number = float(text)
            except ValueError:
                raise UsageError(f"invalid {self.key} {raw!r} from {origin}: not a number of seconds") from None
            if not 0 <= number <= 3600:
                raise UsageError(f"invalid {self.key} {raw!r} from {origin}: must be between 0 and 3600 seconds")
            return int(number) if number.is_integer() else number
        if self.kind == "path":
            if not text:
                raise UsageError(f"invalid {self.key} from {origin}: empty path")
            path = Path(text).expanduser()
            if base is not None and not path.is_absolute():
                path = base / path
            return str(path)
        return text


@dataclass(frozen=True)
class Resolved:
    key: str
    value: Any
    source: str
    origin: str


def resolve(settings: Sequence[Setting], flags: Mapping[str, Tuple[str, str]], environ: Mapping[str, str], prefix: str,
            file_values: Mapping[str, Tuple[str, str]], file_dir: Optional[Path] = None) -> Dict[str, Resolved]:
    resolved: Dict[str, Resolved] = {}
    for setting in settings:
        env_name = setting.env(prefix)
        base = None
        if setting.key in flags:
            raw, origin = flags[setting.key]
            source = "flag"
        elif environ.get(env_name, ""):
            raw, origin, source = environ[env_name], env_name, "env"
        elif setting.key in file_values:
            raw, origin = file_values[setting.key]
            source, base = "file", file_dir
        else:
            raw, origin, source = setting.default, "built-in default", "default"
        resolved[setting.key] = Resolved(setting.key, setting.convert(raw, origin, base), source, origin)
    return resolved


def unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def parse_config(text: str, source: str, known: Iterable[str]) -> Dict[str, Tuple[str, str]]:
    keys = set(known)
    values: Dict[str, Tuple[str, str]] = {}
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition("=")
        key = key.strip()
        if not sep or not key:
            raise UsageError(f"{source}:{number}: expected 'key = value', got {stripped!r}")
        if key not in keys:
            raise UsageError(f"{source}:{number}: unknown key {key!r}{did_you_mean(key, keys)}", hint=f"known keys: {', '.join(sorted(keys))}")
        values[key] = (unquote(value.strip()), f"{source}:{number}")
    return values


@dataclass(frozen=True)
class ConfigFile:
    path: Path
    origin: str
    exists: bool


def locate_config(flag_value: Optional[str], environ: Mapping[str, str], prog: str, prefix: str) -> Tuple[Path, str, bool]:
    if flag_value:
        return Path(flag_value).expanduser(), "--config", True
    env_name = f"{prefix}_CONFIG"
    if environ.get(env_name, ""):
        return Path(environ[env_name]).expanduser(), env_name, True
    base = environ.get("XDG_CONFIG_HOME", "")
    if not base or not os.path.isabs(base):
        base = os.path.join(environ.get("HOME") or os.path.expanduser("~"), ".config")
    return Path(base) / prog / "config", "default", False


def load_config(path: Path, origin: str, explicit: bool, known: Iterable[str]) -> Tuple[ConfigFile, Dict[str, Tuple[str, str]]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if explicit:
            raise UsageError(f"config file {path} (from {origin}) does not exist") from None
        return ConfigFile(path, origin, False), {}
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"cannot read config file {path} (from {origin}): {exc}") from None
    return ConfigFile(path, origin, True), parse_config(text, str(path), known)


class Console:
    def __init__(self, stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None, stderr: Optional[TextIO] = None,
                 environ: Optional[Mapping[str, str]] = None, prog: str = "cli"):
        self.stdin = sys.stdin if stdin is None else stdin
        self.stdout = sys.stdout if stdout is None else stdout
        self.stderr = sys.stderr if stderr is None else stderr
        self.environ: Mapping[str, str] = dict(os.environ) if environ is None else environ
        self.prog = prog
        self.color = False
        self.color_err = False
        self.quiet = False
        self.verbose = False

    def out(self, text: str = "") -> None:
        self.stdout.write(text + "\n")

    def paint(self, text: str, style: str, stream: str = "out") -> str:
        enabled = self.color if stream == "out" else self.color_err
        return f"\033[{STYLES[style]}m{text}\033[0m" if enabled and style in STYLES else text

    def note(self, text: str) -> None:
        if not self.quiet:
            self.stderr.write(f"{self.prog}: {text}\n")

    def debug(self, text: str) -> None:
        if self.verbose:
            self.stderr.write(f"{self.prog}: debug: {text}\n")

    def error(self, text: str, hint: str = "") -> None:
        self.stderr.write(f"{self.prog}: {self.paint('error', 'red', 'err')}: {text}\n")
        if hint:
            self.stderr.write(f"{self.prog}: hint: {hint}\n")

    def confirm(self, question: str) -> None:
        if not is_tty(self.stdin):
            raise Refused(f"refusing to {question} without confirmation: stdin is not a terminal",
                          hint="pass --yes to confirm, or --dry-run to preview")
        self.stderr.write(f"{self.prog}: {question}? [y/N] ")
        self.stderr.flush()
        answer = self.stdin.readline()
        if answer.strip().lower() not in ("y", "yes"):
            raise Refused("not confirmed; nothing was changed")

    def silence_stdout(self) -> None:
        try:
            fd = self.stdout.fileno()
        except (AttributeError, OSError, ValueError):
            return
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, fd)
        os.close(devnull)


@dataclass(frozen=True)
class Flag:
    names: Tuple[str, ...]
    help: str
    metavar: str = ""
    choices: Tuple[str, ...] = ()
    repeat: bool = False
    complete: str = ""
    setting: str = ""

    @property
    def dest(self) -> str:
        return max(self.names, key=len).lstrip("-").replace("-", "_")

    @property
    def takes_value(self) -> bool:
        return bool(self.metavar)

    def display(self) -> str:
        shorts = [n for n in self.names if not n.startswith("--")]
        longs = [n for n in self.names if n.startswith("--")]
        text = (shorts[0] + ", " if shorts else "    ") + ", ".join(longs)
        return f"{text} {self.metavar}" if self.metavar else text


@dataclass(frozen=True)
class Arg:
    dest: str
    metavar: str
    help: str
    many: bool = False
    choices: Tuple[str, ...] = ()
    complete: Tuple[str, ...] = ()

    def display(self) -> str:
        return self.metavar + ("..." if self.many else "")


@dataclass
class Command:
    path: Tuple[str, ...]
    summary: str
    handler: Optional[Callable[["Context"], Optional[int]]] = None
    flags: Tuple[Flag, ...] = ()
    args: Tuple[Arg, ...] = ()
    examples: Tuple[str, ...] = ()
    changes: bool = False
    destructive: bool = False


HELP = Flag(("-h", "--help"), "show this help and exit 0")
VERSION = Flag(("--version",), "print the version and exit 0")
CONFIG = Flag(("--config",), "config file (env {prefix}_CONFIG, default ~/.config/{prog}/config)", metavar="PATH", complete="file")
JSON = Flag(("--json",), "same as --output json: one JSON document on stdout")
QUIET = Flag(("-q", "--quiet"), "print only what a pipe needs: no notes, names only in lists")
VERBOSE = Flag(("-v", "--verbose"), "print debug diagnostics on stderr")
DRY_RUN = Flag(("-n", "--dry-run"), "print what would change and change nothing")
YES = Flag(("-y", "--yes"), "confirm without asking (required when stdin is not a terminal)")


def standard_settings() -> List[Setting]:
    return [
        Setting("output", "output format", "text", metavar="FORMAT", choices=("text", "json"), short="-o"),
        Setting("color", "colour: auto, always or never; auto is off when NO_COLOR is set or stdout is not a terminal", "auto", metavar="WHEN", choices=("auto", "always", "never")),
    ]


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UsageError(message, hint=f"run '{self.prog} --help' for usage")


@dataclass
class Context:
    app: "App"
    command: Command
    args: Dict[str, Any]
    settings: Dict[str, Resolved]
    config_file: ConfigFile
    console: Console
    dry_run: bool = False
    yes: bool = False
    color_reason: str = ""

    def value(self, key: str) -> Any:
        return self.settings[key].value

    @property
    def json(self) -> bool:
        return self.value("output") == "json"

    def emit_json(self, data: Any) -> None:
        self.console.out(json.dumps(data, indent=2, sort_keys=True))

    def confirm(self, question: str) -> None:
        if self.yes:
            self.console.debug(f"--yes given: {question}")
            return
        self.console.confirm(question)


def table(rows: Sequence[Sequence[str]], paint: Optional[Callable[[int, int, str], str]] = None) -> List[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = []
    for r, row in enumerate(rows):
        cells = []
        for c, cell in enumerate(row):
            padded = cell if c == len(row) - 1 else cell.ljust(widths[c])
            cells.append(paint(r, c, padded) if paint else padded)
        lines.append("  ".join(cells).rstrip())
    return lines


def roff(text: str) -> str:
    escaped = text.replace("\\", "\\e").replace("-", "\\-")
    return "\\&" + escaped if escaped[:1] in (".", "'") else escaped


class App:
    def __init__(self, prog: str, version: str, summary: str, settings: Sequence[Setting], description: str = "",
                 release_date: str = "", files: Sequence[Tuple[str, str]] = ()):
        self.prog = prog
        self.version = version
        self.summary = summary
        self.description = description
        self.release_date = release_date
        self.prefix = prog.upper().replace("-", "_")
        self.settings = list(standard_settings()) + list(settings)
        self.files = list(files)
        self.commands: Dict[Tuple[str, ...], Command] = {(): Command((), summary)}

    def add(self, command: Command) -> None:
        for depth in range(1, len(command.path)):
            parent = command.path[:depth]
            if parent not in self.commands:
                raise ValueError(f"declare group {' '.join(parent)!r} before {' '.join(command.path)!r}")
            if self.commands[parent].handler is not None:
                raise ValueError(f"{' '.join(parent)!r} has a handler and cannot also be a group")
        self.commands[command.path] = command

    def display(self, path: Sequence[str]) -> str:
        return " ".join((self.prog,) + tuple(path))

    def children(self, path: Tuple[str, ...]) -> List[Command]:
        return [c for p, c in self.commands.items() if len(p) == len(path) + 1 and p[:len(path)] == path]

    def leaves(self, path: Tuple[str, ...] = ()) -> List[Command]:
        return [c for p, c in self.commands.items() if c.handler is not None and p[:len(path)] == path and p != path]

    def setting_flags(self) -> List[Flag]:
        flags = []
        for s in self.settings:
            names = (s.short, s.flag) if s.short else (s.flag,)
            text = f"{s.help} (env {s.env(self.prefix)}, config {s.key}, default {s.default})"
            flags.append(Flag(names, text, metavar=s.metavar, choices=s.choices, complete=s.complete, setting=s.key))
        return flags

    def global_flags(self, command: Command) -> List[Flag]:
        config = Flag(CONFIG.names, CONFIG.help.format(prefix=self.prefix, prog=self.prog), metavar=CONFIG.metavar, complete="file")
        flags = self.setting_flags()
        flags[1:1] = [JSON]
        flags += [config, QUIET, VERBOSE, HELP]
        if command.path == ():
            flags.append(VERSION)
        return flags

    def command_flags(self, command: Command) -> List[Flag]:
        flags = list(command.flags)
        if command.changes:
            flags.append(DRY_RUN)
        if command.destructive:
            flags.append(YES)
        return flags

    def all_flags(self, command: Command) -> List[Flag]:
        return self.command_flags(command) + self.global_flags(command)

    def value_flag_names(self) -> List[str]:
        names = set()
        for command in self.commands.values():
            for flag in self.all_flags(command):
                if flag.takes_value:
                    names.update(flag.names)
        return sorted(names)

    def locate(self, argv: Sequence[str]) -> Tuple[Command, List[str]]:
        value_flags = set(self.value_flag_names())
        path: Tuple[str, ...] = ()
        rest: List[str] = []
        index = 0
        while index < len(argv):
            token = argv[index]
            if token == "--":
                rest.extend(argv[index:])
                break
            if token.startswith("-") and token != "-":
                rest.append(token)
                if token in value_flags and index + 1 < len(argv):
                    rest.append(argv[index + 1])
                    index += 1
                index += 1
                continue
            if self.commands[path].handler is not None:
                rest.extend(argv[index:])
                break
            child = path + (token,)
            if child not in self.commands:
                names = [c.path[-1] for c in self.children(path)]
                raise UsageError(f"unknown command {token!r} for {self.display(path)!r}{did_you_mean(token, names)}",
                                 hint=f"run '{self.display(path)} --help' to list commands")
            path = child
            index += 1
        return self.commands[path], rest

    def build_parser(self, command: Command) -> Parser:
        parser = Parser(prog=self.display(command.path), add_help=False, allow_abbrev=False)
        for flag in self.all_flags(command):
            kwargs: Dict[str, Any] = {"dest": flag.dest, "default": argparse.SUPPRESS}
            if flag.repeat:
                kwargs.update(action="append", metavar=flag.metavar)
            elif flag.takes_value:
                kwargs.update(metavar=flag.metavar)
                if flag.choices and not flag.setting:
                    kwargs["choices"] = flag.choices
            else:
                kwargs["action"] = "store_true"
            parser.add_argument(*flag.names, **kwargs)
        for arg in command.args:
            kwargs = {"metavar": arg.metavar, "nargs": "+" if arg.many else None}
            if arg.choices:
                kwargs["choices"] = arg.choices
            parser.add_argument(arg.dest, **kwargs)
        return parser

    def context(self, command: Command, values: Dict[str, Any], console: Console) -> Context:
        flags: Dict[str, Tuple[str, str]] = {}
        for setting in self.settings:
            if setting.key in values:
                flags[setting.key] = (values.pop(setting.key), setting.flag)
        if values.pop("json", False):
            if flags.get("output", ("json", ""))[0] != "json":
                raise UsageError("--json conflicts with --output " + flags["output"][0])
            flags["output"] = ("json", "--json")
        quiet, verbose = values.pop("quiet", False), values.pop("verbose", False)
        if quiet and verbose:
            raise UsageError("--quiet and --verbose cannot be used together")
        path, origin, explicit = locate_config(values.pop("config", None), console.environ, self.prog, self.prefix)
        config_file, file_values = load_config(path, origin, explicit, [s.key for s in self.settings])
        settings = resolve(self.settings, flags, console.environ, self.prefix, file_values, path.parent)
        console.quiet, console.verbose = quiet, verbose
        console.color, reason = color_decision(settings["color"].value, console.stdout, console.environ)
        console.color_err = color_decision(settings["color"].value, console.stderr, console.environ, "stderr")[0]
        console.debug(f"config file {path} ({origin}, {'found' if config_file.exists else 'not found'})")
        for item in settings.values():
            console.debug(f"{item.key} = {item.value} from {item.source} ({item.origin})")
        dry_run, yes = values.pop("dry_run", False), values.pop("yes", False)
        return Context(self, command, values, settings, config_file, console, dry_run, yes, reason)

    def run(self, argv: Optional[Sequence[str]] = None, console: Optional[Console] = None) -> int:
        console = console or Console(prog=self.prog)
        argv = list(sys.argv[1:] if argv is None else argv)
        try:
            code = self.dispatch(argv, console)
            console.stdout.flush()
            return code
        except CliError as exc:
            console.error(exc.message, exc.hint)
            return exc.code
        except KeyboardInterrupt:
            console.stderr.write("\n")
            console.error("interrupted; changes are only written after confirmation, so nothing was half-written")
            return INTERRUPTED
        except BrokenPipeError:
            console.silence_stdout()
            return BROKEN_PIPE

    def dispatch(self, argv: List[str], console: Console) -> int:
        command, rest = self.locate(argv)
        before_separator = rest[:rest.index("--")] if "--" in rest else rest
        if any(token in HELP.names for token in before_separator):
            console.out(self.render_help(command))
            return OK
        values = vars(self.build_parser(command).parse_args(rest))
        if values.pop("version", False):
            console.out(f"{self.prog} {self.version}")
            return OK
        if command.handler is None:
            names = ", ".join(c.path[-1] for c in self.children(command.path))
            raise UsageError(f"missing command for {self.display(command.path)!r} (one of: {names})",
                             hint=f"run '{self.display(command.path)} --help' for usage")
        ctx = self.context(command, values, console)
        result = command.handler(ctx)
        return OK if result is None else result

    def usage(self, command: Command) -> str:
        if command.handler is None:
            return f"{self.display(command.path)} [flags] COMMAND [ARGS]"
        positional = " ".join(a.display() for a in command.args)
        return f"{self.display(command.path)} [flags]" + (f" {positional}" if positional else "")

    def render_help(self, command: Command) -> str:
        lines = [f"{self.display(command.path)} - {command.summary}", "", "Usage:", "  " + self.usage(command)]
        if command.path == () and self.description:
            lines += [""] + textwrap.wrap(self.description, HELP_WIDTH)
        sections: List[Tuple[str, List[Tuple[str, str]]]] = []
        if command.handler is None:
            sections.append(("Commands", [(" ".join(c.path[len(command.path):]), c.summary) for c in self.leaves(command.path)]))
        if command.args:
            sections.append(("Arguments", [(a.display(), a.help) for a in command.args]))
        if self.command_flags(command):
            sections.append(("Flags", [(f.display(), f.help) for f in self.command_flags(command)]))
        sections.append(("Global flags", [(f.display(), f.help) for f in self.global_flags(command)]))
        width = max(HELP_COLUMN_MIN, max(len(left) for _, rows in sections for left, _ in rows) + 2)
        wrap_at = max(30, HELP_WIDTH - width - 2)
        for title, rows in sections:
            lines += ["", f"{title}:"]
            for left, right in rows:
                chunks = textwrap.wrap(right, wrap_at) or [""]
                lines.append(f"  {left.ljust(width)}{chunks[0]}".rstrip())
                lines += [" " * (width + 2) + chunk for chunk in chunks[1:]]
        if command.examples:
            lines += ["", "Examples:"] + [f"  {example}" for example in command.examples]
        if command.path == ():
            lines += ["", "Exit codes:"] + [f"  {code:<5}{name.ljust(13)}{meaning}" for code, name, meaning in EXIT_CODES]
            lines += ["", f"Run '{self.prog} COMMAND --help' for the flags of one command."]
        else:
            lines += ["", "Exit codes: " + ", ".join(f"{code} {name}" for code, name, _ in EXIT_CODES)]
        return "\n".join(lines)

    def bash_completion(self) -> str:
        fn = f"_{self.prog.replace('-', '_')}_complete"
        value_flags = self.value_flag_names()
        passthrough = [n for n in CONFIG.names] + [n for s in self.settings if s.kind == "path" for n in ((s.short, s.flag) if s.short else (s.flag,))]
        skip = [n for n in value_flags if n not in passthrough]
        groups = sorted(" ".join((self.prog,) + p) for p in self.commands if p)
        out = [
            f"{fn}() {{",
            "    local cur prev word candidate path i skip commands flags dynamic line",
            "    local -a extra",
            "    COMPREPLY=()",
            '    cur="${COMP_WORDS[COMP_CWORD]}"',
            '    prev=""',
            "    if (( COMP_CWORD > 0 )); then",
            '        prev="${COMP_WORDS[COMP_CWORD-1]}"',
            "    fi",
            f'    path="{self.prog}"',
            "    skip=0",
            "    extra=()",
            "    for (( i = 1; i < COMP_CWORD; i++ )); do",
            '        word="${COMP_WORDS[i]}"',
            "        if (( skip )); then",
            "            skip=0",
            "            continue",
            "        fi",
            '        case "$word" in',
            f"            {'|'.join(passthrough)})",
            '                extra+=("$word" "${COMP_WORDS[i+1]}")',
            "                skip=1",
            "                ;;",
            f"            {'|'.join(n + '=*' for n in passthrough if n.startswith('--'))})",
            '                extra+=("$word")',
            "                ;;",
            f"            {'|'.join(skip)})",
            "                skip=1",
            "                ;;",
            "            -*)",
            "                ;;",
            "            *)",
            '                candidate="$path $word"',
            '                case "$candidate" in',
            f"                    {'|'.join(repr_bash(g) for g in groups)})",
            '                        path="$candidate"',
            "                        ;;",
            "                esac",
            "                ;;",
            "        esac",
            "    done",
            '    case "$prev" in',
        ]
        by_completion: Dict[str, List[str]] = {}
        for name in value_flags:
            flag = self.find_flag(name)
            key = "file" if flag.complete == "file" else ("words:" + " ".join(flag.choices) if flag.choices else "none")
            by_completion.setdefault(key, []).append(name)
        for key in sorted(by_completion):
            out.append(f"        {'|'.join(sorted(by_completion[key]))})")
            if key == "file":
                out += ["            if type compopt >/dev/null 2>&1; then", "                compopt -o filenames", "            fi",
                        '            while IFS= read -r line; do COMPREPLY+=("$line"); done < <(compgen -f -- "$cur")']
            elif key.startswith("words:"):
                out.append(f'            while IFS= read -r line; do COMPREPLY+=("$line"); done < <(compgen -W "{key[6:]}" -- "$cur")')
            out += ["            return 0", "            ;;"]
        out += ["    esac", '    commands=""', '    dynamic=""', '    case "$path" in']
        for path, command in sorted(self.commands.items()):
            words = " ".join(c.path[-1] for c in self.children(path))
            for arg in command.args:
                if arg.choices:
                    words = (words + " " + " ".join(arg.choices)).strip()
            flags = " ".join(n for f in self.all_flags(command) for n in f.names)
            dyn = next((" ".join(a.complete) for a in command.args if a.complete), "")
            out.append(f"        {repr_bash(self.display(path))})")
            out.append(f'            commands="{words}"')
            out.append(f'            flags="{flags}"')
            if dyn:
                out.append(f'            dynamic="{dyn}"')
            out.append("            ;;")
        out += [
            "    esac",
            '    if [[ "$cur" == -* ]]; then',
            '        while IFS= read -r line; do COMPREPLY+=("$line"); done < <(compgen -W "$flags" -- "$cur")',
            '    elif [[ -n "$commands" ]]; then',
            '        while IFS= read -r line; do COMPREPLY+=("$line"); done < <(compgen -W "$commands" -- "$cur")',
            '    elif [[ -n "$dynamic" ]]; then',
            '        while IFS= read -r line; do',
            '            [[ -n "$line" ]] && COMPREPLY+=("$line")',
            '        done < <(compgen -W "$("${COMP_WORDS[0]}" ${extra[@]+"${extra[@]}"} $dynamic 2>/dev/null)" -- "$cur")',
            "    fi",
            "    return 0",
            "}",
            f"complete -F {fn} {self.prog}",
        ]
        return "\n".join(out)

    def find_flag(self, name: str) -> Flag:
        for command in self.commands.values():
            for flag in self.all_flags(command):
                if name in flag.names:
                    return flag
        raise KeyError(name)

    def man_page(self) -> str:
        title = self.prog.upper()
        lines = [
            f'.TH {title} 1 {self.release_date} "{self.prog} {self.version}" "User Commands"',
            ".SH NAME",
            f"{self.prog} \\- {roff(self.summary)}",
            ".SH SYNOPSIS",
            f".B {self.prog}",
            "[\\fIflags\\fR] \\fICOMMAND\\fR [\\fIARGS\\fR]",
            ".SH DESCRIPTION",
            roff(self.description or self.summary),
            ".SH COMMANDS",
        ]
        for command in self.leaves():
            lines += [".TP", f".B {self.display(command.path)}" + "".join(f" \\fI{roff(a.display())}\\fR" for a in command.args), roff(command.summary)]
            flags = self.command_flags(command)
            if flags:
                lines.append(".RS")
                for flag in flags:
                    lines += [".TP", man_flag(flag), roff(flag.help)]
                lines.append(".RE")
        lines.append(".SH GLOBAL OPTIONS")
        for flag in self.global_flags(self.commands[()]):
            lines += [".TP", man_flag(flag), roff(flag.help)]
        lines.append(".SH ENVIRONMENT")
        for setting in self.settings:
            lines += [".TP", f".B {setting.env(self.prefix)}", roff(f"{setting.help}; overrides the config file, overridden by {setting.flag}. An empty value counts as unset.")]
        lines += [".TP", f".B {self.prefix}_CONFIG", roff("path of the config file; overridden by --config."),
                  ".TP", ".B NO_COLOR", roff("when set to a non-empty value, color auto turns colour off; color always still turns it on (https://no-color.org)."),
                  ".SH FILES", ".TP", f".I ~/.config/{self.prog}/config",
                  roff(f"key = value lines, # comments; keys: {', '.join(s.key for s in self.settings)}. $XDG_CONFIG_HOME replaces ~/.config. Relative paths in it are relative to the file.")]
        for name, text in self.files:
            lines += [".TP", f".I {roff(name)}", roff(text)]
        lines.append(".SH EXIT STATUS")
        for code, name, meaning in EXIT_CODES:
            lines += [".TP", f".B {code}", roff(f"{name}: {meaning}")]
        examples = [e for c in self.leaves() for e in c.examples]
        if examples:
            lines += [".SH EXAMPLES", ".nf"] + [roff(e) for e in examples] + [".fi"]
        lines += [".SH SEE ALSO", f".BR {self.prog} (1) \\-\\-help, \\fB{self.prog} completion bash\\fR"]
        return "\n".join(lines)


def man_flag(flag: Flag) -> str:
    names = ", ".join(f"\\fB{roff(n)}\\fR" for n in flag.names)
    return names + (f" \\fI{flag.metavar}\\fR" if flag.metavar else "")


def repr_bash(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'
