"""bashlint: a tokenizer for the Bash that scripts actually use, and the checks that keep those scripts safe."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Sequence, Tuple


class LexError(Exception):
    def __init__(self, message: str, line: int):
        super().__init__(f"line {line}: {message}")
        self.line = line


@dataclass
class Part:
    kind: str
    text: str
    quoted: bool
    line: int
    col: int
    name: str = ""
    op: str = ""
    index: str = ""
    inner: List["Token"] = field(default_factory=list)


@dataclass
class Word:
    parts: List[Part]
    line: int
    col: int
    raw: str

    def literal(self) -> Optional[str]:
        if all(p.kind in ("lit", "sq", "esc") for p in self.parts):
            return "".join(p.text for p in self.parts)
        return None

    def bare(self) -> Optional[str]:
        if len(self.parts) == 1 and self.parts[0].kind == "lit" and not self.parts[0].quoted:
            return self.parts[0].text
        return None


@dataclass
class Token:
    kind: str
    text: str
    line: int
    col: int
    word: Optional[Word] = None
    heredoc: Optional[str] = None
    body: Optional[str] = None


@dataclass
class Redirect:
    op: str
    target: Optional[Word]
    body: Optional[str] = None


@dataclass
class Command:
    kind: str
    line: int
    col: int
    words: List[Word] = field(default_factory=list)
    assignments: List[Word] = field(default_factory=list)
    redirects: List[Redirect] = field(default_factory=list)
    followed_by: str = ""
    preceded_by: str = ""
    condition: bool = False
    loop_var: str = ""
    function: str = ""
    nested: bool = False

    def name(self) -> Optional[str]:
        return self.words[0].literal() if self.words else None


@dataclass
class Script:
    commands: List[Command]
    functions: Dict[str, int]
    shebang: str


OPERATORS = [";;&", "<<<", "<<-", "&>>", ";;", ";&", "&&", "||", "|&", "<<", ">>", "&>", "<&", ">&", "<>", ">|", "|", "&", ";", "<", ">", "(", ")"]
REDIRECTS = {"<<<", "<<-", "&>>", "<<", ">>", "&>", "<&", ">&", "<>", ">|", "<", ">"}
SEPARATORS = {";;&", ";;", ";&", "&&", "||", "|&", "|", "&", ";", "(", ")"}
COMMAND_START_WORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "time", "fi", "done"}
CONDITION_WORDS = {"if", "elif", "while", "until", "!"}
DECLARATIONS = {"local", "export", "declare", "readonly", "typeset"}
SPECIAL_SAFE = {"#", "?", "$", "!", "-", "RANDOM", "LINENO", "SECONDS", "PPID", "BASHPID", "EUID", "UID", "OPTIND"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?=")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PARAM_BODY = re.compile(r"^(#?)([A-Za-z_][A-Za-z0-9_]*|[0-9]+|[@*#?$!\-])(\[[^]]*\])?(.*)$", re.S)
WORD_END = set(" \t\n|&;<>()")


class Lexer:
    def __init__(self, text: str, line: int = 1, col: int = 1, stop_at_paren: bool = False):
        self.s = text
        self.i = 0
        self.line = line
        self.col = col
        self.stop_at_paren = stop_at_paren
        self.tokens: List[Token] = []
        self.pending: List[Token] = []
        self.dbracket = False
        self.depth = 0
        self.case_depth = 0
        self.stopped_at = -1

    def peek(self, offset: int = 0) -> str:
        j = self.i + offset
        return self.s[j] if j < len(self.s) else ""

    def advance(self, n: int = 1) -> str:
        chunk = self.s[self.i:self.i + n]
        for ch in chunk:
            if ch == "\n":
                self.line += 1
                self.col = 1
            else:
                self.col += 1
        self.i += n
        return chunk

    def command_position(self) -> bool:
        for tok in reversed(self.tokens):
            if tok.kind == "newline":
                return True
            if tok.kind == "op":
                return tok.text in SEPARATORS
            if tok.kind == "word":
                return tok.text in COMMAND_START_WORDS or tok.text == "for"
            return True
        return True

    def run(self) -> List[Token]:
        while self.i < len(self.s):
            c = self.peek()
            if c in " \t":
                self.advance()
            elif c == "\\" and self.peek(1) == "\n":
                self.advance(2)
            elif c == "\n":
                self.tokens.append(Token("newline", "\n", self.line, self.col))
                self.advance()
                self.read_heredocs()
            elif c == "#":
                while self.i < len(self.s) and self.peek() != "\n":
                    self.advance()
            elif self.stop_at_paren and c == ")" and self.depth == 0 and self.case_depth == 0 and not self.dbracket:
                self.stopped_at = self.i
                return self.tokens
            elif self.dbracket:
                self.lex_word()
            elif c == "(" and self.peek(1) == "(" and self.command_position():
                self.lex_arith_command()
            elif c in "<>" and self.peek(1) == "(":
                self.lex_word()
            elif re.match(r"[0-9]+[<>]", self.s[self.i:self.i + 12]) and self.at_word_boundary():
                self.lex_operator(fd=True)
            elif any(self.s.startswith(op, self.i) for op in OPERATORS):
                self.lex_operator()
            else:
                self.lex_word()
        if self.pending:
            raise LexError(f"here-document delimited by {self.pending[0].heredoc!r} is never closed", self.pending[0].line)
        if self.stop_at_paren:
            raise LexError("unterminated $(", self.line)
        return self.tokens

    def at_word_boundary(self) -> bool:
        return self.i == 0 or self.s[self.i - 1] in " \t\n;&|()"

    def lex_operator(self, fd: bool = False) -> None:
        line, col = self.line, self.col
        prefix = ""
        if fd:
            while self.peek().isdigit():
                prefix += self.advance()
        op = next(o for o in OPERATORS if self.s.startswith(o, self.i))
        self.advance(len(op))
        tok = Token("op", op, line, col)
        self.tokens.append(tok)
        if op == "(":
            self.depth += 1
        elif op == ")":
            self.depth -= 1
        if op in ("<<", "<<-"):
            while self.peek() in " \t":
                self.advance()
            delim = self.read_word()
            raw = delim.literal()
            tok.heredoc = raw if raw is not None else delim.raw
            tok.word = delim
            self.pending.append(tok)

    def read_heredocs(self) -> None:
        while self.pending:
            tok = self.pending.pop(0)
            strip = tok.text == "<<-"
            body: List[str] = []
            while True:
                if self.i >= len(self.s):
                    raise LexError(f"here-document delimited by {tok.heredoc!r} is never closed", tok.line)
                end = self.s.find("\n", self.i)
                end = len(self.s) if end == -1 else end
                text = self.s[self.i:end]
                self.advance(end - self.i + (1 if end < len(self.s) else 0))
                if (text.lstrip("\t") if strip else text) == tok.heredoc:
                    break
                body.append(text)
            tok.body = "\n".join(body)

    def lex_arith_command(self) -> None:
        line, col = self.line, self.col
        start = self.i
        self.advance(2)
        level = 2
        while self.i < len(self.s) and level > 0:
            ch = self.advance()
            if ch == "(":
                level += 1
            elif ch == ")":
                level -= 1
        if level:
            raise LexError("unterminated ((", line)
        self.tokens.append(Token("arith", self.s[start:self.i], line, col))

    def lex_word(self) -> None:
        word = self.read_word()
        self.tokens.append(Token("word", word.raw, word.line, word.col, word=word))
        bare = word.bare()
        if bare == "[[" and not self.dbracket:
            self.dbracket = True
        elif bare == "]]" and self.dbracket:
            self.dbracket = False
        elif bare == "case":
            self.case_depth += 1
        elif bare == "esac" and self.case_depth:
            self.case_depth -= 1

    def word_ends(self) -> bool:
        c = self.peek()
        if c == "":
            return True
        if self.dbracket:
            return c in " \t\n;"
        return c in WORD_END

    def read_word(self) -> Word:
        line, col, start = self.line, self.col, self.i
        parts: List[Part] = []
        if self.peek() in "<>" and self.peek(1) == "(":
            pline, pcol = self.line, self.col
            self.advance(2)
            inner, text = self.read_subshell(pline)
            parts.append(Part("procsub", text, False, pline, pcol, inner=inner))
        while True:
            c = self.peek()
            pline, pcol = self.line, self.col
            if c == "(" and self.at_array_assignment(parts):
                self.advance()
                level = 1
                body_start = self.i
                while self.i < len(self.s) and level:
                    ch = self.advance()
                    if ch == "(":
                        level += 1
                    elif ch == ")":
                        level -= 1
                parts.append(Part("array", self.s[body_start:self.i - 1], False, pline, pcol))
                continue
            if self.word_ends():
                break
            if c == "\\":
                self.advance()
                if self.peek() == "\n":
                    self.advance()
                    continue
                parts.append(Part("esc", self.advance(), True, pline, pcol))
            elif c == "'":
                self.advance()
                end = self.s.find("'", self.i)
                if end == -1:
                    raise LexError("unterminated single quote", pline)
                parts.append(Part("sq", self.advance(end - self.i), True, pline, pcol))
                self.advance()
            elif c == "$" and self.peek(1) == "'":
                self.advance(2)
                parts.append(Part("sq", self.read_ansi_c(pline), True, pline, pcol))
            elif c == "$" and self.peek(1) == '"':
                self.advance()
            elif c == '"':
                self.advance()
                parts.extend(self.read_dquote(pline))
            elif c == "$":
                parts.append(self.read_dollar(False))
            elif c == "`":
                parts.append(self.read_backtick(False))
            else:
                if parts and parts[-1].kind == "lit" and not parts[-1].quoted:
                    parts[-1].text += self.advance()
                else:
                    parts.append(Part("lit", self.advance(), False, pline, pcol))
        if self.i == start:
            raise LexError(f"unexpected character {self.peek()!r}", line)
        return Word(parts, line, col, self.s[start:self.i])

    @staticmethod
    def at_array_assignment(parts: List[Part]) -> bool:
        if not parts or parts[-1].kind != "lit" or parts[-1].quoted:
            return False
        text = "".join(p.text for p in parts)
        return text.endswith("=") and ASSIGNMENT.match(text) is not None

    def read_ansi_c(self, line: int) -> str:
        out = ""
        while True:
            c = self.peek()
            if c == "":
                raise LexError("unterminated $'", line)
            if c == "\\":
                out += self.advance(2)
            elif c == "'":
                self.advance()
                return out
            else:
                out += self.advance()

    def read_dquote(self, line: int) -> List[Part]:
        parts: List[Part] = []
        while True:
            c = self.peek()
            pline, pcol = self.line, self.col
            if c == "":
                raise LexError("unterminated double quote", line)
            if c == '"':
                self.advance()
                return parts
            if c == "\\" and self.peek(1) in '$`"\\\n':
                self.advance()
                ch = self.advance()
                if ch != "\n":
                    parts.append(Part("esc", ch, True, pline, pcol))
            elif c == "$" and (self.peek(1) in "({" or NAME.match(self.peek(1) or " ") or self.peek(1) in "@*#?$!-0123456789"):
                parts.append(self.read_dollar(True))
            elif c == "`":
                parts.append(self.read_backtick(True))
            else:
                if parts and parts[-1].kind == "lit":
                    parts[-1].text += self.advance()
                else:
                    parts.append(Part("lit", self.advance(), True, pline, pcol))

    def read_dollar(self, quoted: bool) -> Part:
        line, col = self.line, self.col
        if self.s.startswith("$((", self.i):
            self.advance(3)
            level = 2
            start = self.i
            while self.i < len(self.s) and level:
                ch = self.advance()
                if ch == "(":
                    level += 1
                elif ch == ")":
                    level -= 1
            if level:
                raise LexError("unterminated $((", line)
            return Part("arith", self.s[start:self.i - 2], quoted, line, col)
        if self.s.startswith("$(", self.i):
            self.advance(2)
            inner, text = self.read_subshell(line)
            return Part("cmdsub", text, quoted, line, col, inner=inner)
        if self.s.startswith("${", self.i):
            self.advance(2)
            start = self.i
            level = 1
            while self.i < len(self.s) and level:
                ch = self.advance()
                if ch == "{":
                    level += 1
                elif ch == "}":
                    level -= 1
                elif ch == "\\":
                    self.advance()
            if level:
                raise LexError("unterminated ${", line)
            body = self.s[start:self.i - 1]
            source = "${" + body + "}"
            m = PARAM_BODY.match(body)
            if not m:
                return Part("param", source, quoted, line, col, name=body, op="?bad")
            hash_, name, index, rest = m.groups()
            if hash_ and name == "#" and not rest and not index:
                return Part("param", source, quoted, line, col, name="#")
            if hash_:
                return Part("param", source, quoted, line, col, name=name, op="#len", index=(index or "")[1:-1])
            return Part("param", source, quoted, line, col, name=name, op=rest, index=(index or "")[1:-1])
        nxt = self.peek(1)
        m = NAME.match(self.s, self.i + 1)
        if m:
            self.advance(1 + len(m.group(0)))
            return Part("param", "$" + m.group(0), quoted, line, col, name=m.group(0))
        if nxt and (nxt.isdigit() or nxt in "@*#?$!-"):
            self.advance(2)
            return Part("param", "$" + nxt, quoted, line, col, name=nxt)
        self.advance()
        return Part("lit", "$", quoted, line, col)

    def read_subshell(self, line: int) -> Tuple[List[Token], str]:
        sub = Lexer(self.s[self.i:], self.line, self.col, stop_at_paren=True)
        try:
            tokens = sub.run()
        except LexError as exc:
            raise LexError(str(exc).split(": ", 1)[-1], line) from None
        text = self.s[self.i:self.i + sub.stopped_at]
        self.advance(sub.stopped_at + 1)
        return tokens, text

    def read_backtick(self, quoted: bool) -> Part:
        line, col = self.line, self.col
        self.advance()
        inner_line, inner_col = self.line, self.col
        text = ""
        while True:
            c = self.peek()
            if c == "":
                raise LexError("unterminated backtick", line)
            if c == "\\" and self.peek(1) in "`$\\":
                self.advance()
                text += self.advance()
            elif c == "`":
                self.advance()
                break
            else:
                text += self.advance()
        inner = Lexer(text, inner_line, inner_col).run()
        return Part("backtick", text, quoted, line, col, inner=inner)


def tokenize(text: str) -> List[Token]:
    return Lexer(text).run()


class Parser:
    def __init__(self) -> None:
        self.commands: List[Command] = []
        self.functions: Dict[str, int] = {}

    def parse(self, tokens: List[Token], nested: bool = False) -> None:
        cur: Optional[Command] = None
        preceded = ""
        condition = False
        case_states: List[str] = []
        braces = 0
        function_stack: List[Tuple[str, int]] = []
        i = 0

        def finish(followed: str) -> None:
            nonlocal cur
            if cur is not None and (cur.words or cur.assignments or cur.redirects):
                cur.followed_by = followed
                self.commands.append(cur)
            cur = None

        def current_function() -> str:
            return function_stack[-1][0] if function_stack else ""

        while i < len(tokens):
            tok = tokens[i]
            if case_states and case_states[-1] == "patterns":
                if tok.kind == "word" and tok.word and tok.word.bare() == "esac":
                    case_states.pop()
                elif tok.kind == "op" and tok.text == ")":
                    case_states[-1] = "commands"
                i += 1
                continue
            if tok.kind == "newline" or (tok.kind == "op" and tok.text in SEPARATORS):
                finish(tok.text)
                preceded = tok.text
                if tok.text in (";;", ";&", ";;&") and case_states:
                    case_states[-1] = "patterns"
                if tok.text != "|" and tok.text != "|&" and tok.text != "&&" and tok.text != "||":
                    condition = False
                i += 1
                continue
            if tok.kind == "arith":
                finish("")
                self.commands.append(Command("arith", tok.line, tok.col, preceded_by=preceded, function=current_function(), nested=nested))
                i += 1
                continue
            if tok.kind == "op" and tok.text in REDIRECTS:
                if cur is None:
                    cur = Command("simple", tok.line, tok.col, preceded_by=preceded, condition=condition, function=current_function(), nested=nested)
                if tok.text in ("<<", "<<-"):
                    cur.redirects.append(Redirect(tok.text, tok.word, tok.body))
                    i += 1
                    continue
                target = tokens[i + 1].word if i + 1 < len(tokens) and tokens[i + 1].kind == "word" else None
                if target is None:
                    raise LexError(f"redirection {tok.text} has no target", tok.line)
                cur.redirects.append(Redirect(tok.text, target))
                self.parse_parts(target, nested)
                i += 2
                continue
            word = tok.word
            assert word is not None
            self.parse_parts(word, nested)
            bare = word.bare()
            if cur is None or not (cur.words or cur.assignments):
                if case_states and case_states[-1] == "commands" and bare == "esac":
                    case_states.pop()
                    i += 1
                    continue
                if bare in COMMAND_START_WORDS:
                    if bare in CONDITION_WORDS:
                        condition = True
                    elif bare in ("then", "do", "else"):
                        condition = False
                    if bare == "{":
                        braces += 1
                    elif bare == "}":
                        braces -= 1
                        if function_stack and function_stack[-1][1] == braces:
                            function_stack.pop()
                    i += 1
                    continue
                if bare == "for":
                    i = self.parse_for(tokens, i, preceded, current_function(), nested)
                    continue
                if bare == "case":
                    subject = tokens[i + 1].word if i + 1 < len(tokens) else None
                    cmd = Command("case", tok.line, tok.col, words=[subject] if subject else [], preceded_by=preceded, function=current_function(), nested=nested)
                    self.commands.append(cmd)
                    case_states.append("patterns")
                    i += 3
                    continue
                if bare == "function" and i + 1 < len(tokens) and tokens[i + 1].word:
                    name = tokens[i + 1].text
                    self.functions[name] = tok.line
                    function_stack.append((name, braces))
                    i += 2
                    while i < len(tokens) and tokens[i].kind == "op" and tokens[i].text in "()":
                        i += 1
                    continue
                if bare and i + 2 < len(tokens) and tokens[i + 1].text == "(" and tokens[i + 2].text == ")" and NAME.fullmatch(bare.replace("-", "_").replace(".", "_").replace(":", "_")):
                    self.functions[bare] = tok.line
                    function_stack.append((bare, braces))
                    i += 3
                    continue
                if cur is None:
                    cur = Command("simple", tok.line, tok.col, preceded_by=preceded, condition=condition, function=current_function(), nested=nested)
                if not cur.words and ASSIGNMENT.match(word.raw):
                    cur.assignments.append(word)
                    i += 1
                    continue
                if bare == "[[":
                    cur.kind = "dbracket"
            cur.words.append(word)
            i += 1
        finish("")

    def parse_for(self, tokens: List[Token], i: int, preceded: str, function: str, nested: bool) -> int:
        start = tokens[i]
        if i + 1 < len(tokens) and tokens[i + 1].kind == "arith":
            self.commands.append(Command("arith", start.line, start.col, preceded_by=preceded, function=function, nested=nested))
            return i + 2
        var = tokens[i + 1].text if i + 1 < len(tokens) else ""
        cmd = Command("for", start.line, start.col, preceded_by=preceded, loop_var=var, function=function, nested=nested)
        i += 2
        while i < len(tokens) and tokens[i].kind == "newline":
            i += 1
        if i < len(tokens) and tokens[i].word and tokens[i].word.bare() == "in":
            i += 1
            while i < len(tokens) and tokens[i].kind == "word":
                self.parse_parts(tokens[i].word, nested)
                cmd.words.append(tokens[i].word)
                i += 1
        self.commands.append(cmd)
        return i

    def parse_parts(self, word: Optional[Word], nested: bool) -> None:
        if word is None:
            return
        for part in word.parts:
            if part.inner:
                Parser.parse(self, part.inner, nested=True)


def parse(text: str) -> Script:
    tokens = tokenize(text)
    parser = Parser()
    parser.parse(tokens)
    commands = sorted(parser.commands, key=lambda c: (c.line, c.col))
    first = text.split("\n", 1)[0]
    return Script(commands, parser.functions, first if first.startswith("#!") else "")


@dataclass(frozen=True)
class Check:
    id: str
    name: str
    shellcheck: str
    severity: str
    summary: str


CHECKS = [
    Check("BL001", "unquoted-expansion", "SC2086", "info", "unquoted $var is split on IFS and globbed; quote it"),
    Check("BL002", "for-over-ls", "SC2045", "error", "for f in $(ls) breaks on spaces and globs; iterate over a glob"),
    Check("BL003", "cd-without-exit", "SC2164", "warning", "cd can fail and the script carries on in the wrong directory; use cd ... || exit"),
    Check("BL004", "missing-strict-mode", "", "warning", "no set -euo pipefail: failed commands, unset variables and failed pipes go unnoticed"),
    Check("BL005", "unquoted-test-operand", "SC2086", "info", "an unquoted operand in [ ] disappears when empty and splits when it has spaces; quote it or use [[ ]]"),
    Check("BL006", "useless-cat", "SC2002", "style", "cat FILE | cmd: let cmd read the file, or use cmd < FILE"),
    Check("BL007", "read-without-r", "SC2162", "info", "read without -r treats backslashes as escapes; use read -r"),
    Check("BL008", "rm-empty-var-glob", "SC2115", "warning", "rm -r \"$var/\"* removes /* when var is empty; use \"${var:?}\""),
    Check("BL009", "backticks", "SC2006", "style", "legacy backticks nest badly and mangle backslashes; use $(...)"),
    Check("BL010", "unquoted-dollar-star", "SC2048", "warning", "$* joins and re-splits the arguments; use \"$@\""),
    Check("BL011", "unquoted-dollar-at", "SC2068", "error", "unquoted $@ re-splits each argument; use \"$@\""),
    Check("BL012", "unquoted-command-substitution", "SC2046", "warning", "unquoted $(...) is split on IFS and globbed; quote it"),
    Check("BL013", "tempfile-without-trap", "", "warning", "mktemp without a trap ... EXIT leaves temporary files behind on every failure"),
]
CHECKS_BY_ID = {c.id: c for c in CHECKS}
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2, "style": 3}


@dataclass
class Finding:
    file: str
    line: int
    col: int
    check: str
    message: str

    def as_dict(self) -> Dict[str, object]:
        c = CHECKS_BY_ID[self.check]
        return {"file": self.file, "line": self.line, "column": self.col, "id": c.id, "name": c.name, "shellcheck": c.shellcheck or None, "severity": c.severity, "message": self.message}


def literal_value(word: Word) -> Optional[str]:
    if any(p.kind not in ("lit", "sq", "esc") for p in word.parts):
        return None
    text = "".join(p.text for p in word.parts)
    return None if re.search(r"[\s*?\[]", text) else text


def assignment_value(word: Word) -> Tuple[str, Word]:
    raw_prefix = ""
    for idx, part in enumerate(word.parts):
        if part.kind == "lit" and not part.quoted and "=" in part.text:
            pos = part.text.index("=")
            raw_prefix += part.text[:pos]
            rest = part.text[pos + 1:]
            parts = ([Part("lit", rest, False, part.line, part.col + pos + 1)] if rest else []) + word.parts[idx + 1:]
            return raw_prefix.rstrip("+").split("[")[0], Word(parts, word.line, word.col, "")
        raw_prefix += part.text
    return raw_prefix, Word([], word.line, word.col, "")


def constant_variables(script: Script) -> set:
    values: Dict[str, List[Optional[str]]] = {}

    def record(name: str, value: Optional[str]) -> None:
        values.setdefault(name, []).append(value)

    for cmd in script.commands:
        for word in cmd.assignments:
            name, value = assignment_value(word)
            record(name, literal_value(value))
        if cmd.kind == "for" and cmd.loop_var:
            record(cmd.loop_var, None)
        name = cmd.name()
        if name in DECLARATIONS:
            for word in cmd.words[1:]:
                if ASSIGNMENT.match(word.raw):
                    var, value = assignment_value(word)
                    record(var, literal_value(value))
        if name in ("read", "mapfile", "readarray", "getopts", "printf"):
            for word in cmd.words[1:]:
                bare = word.bare()
                if bare and NAME.fullmatch(bare):
                    record(bare, None)
    return {name for name, seen in values.items() if None not in seen and any(seen)}


def strict_mode(script: Script) -> Dict[str, bool]:
    flags = {"errexit": False, "nounset": False, "pipefail": False}
    shebang_flags = script.shebang.split()[1:] if script.shebang else []
    if any(f.startswith("-") and "e" in f for f in shebang_flags):
        flags["errexit"] = True
    for cmd in script.commands:
        if cmd.name() != "set":
            continue
        words = [w.literal() or "" for w in cmd.words[1:]]
        j = 0
        while j < len(words):
            w = words[j]
            if w in ("-o", "+o") and j + 1 < len(words):
                opt = words[j + 1]
                if opt in flags:
                    flags[opt] = w == "-o"
                j += 2
                continue
            if re.fullmatch(r"-[A-Za-z]+", w):
                if "e" in w:
                    flags["errexit"] = True
                if "u" in w:
                    flags["nounset"] = True
                if w.endswith("o") and j + 1 < len(words) and words[j + 1] in flags:
                    flags[words[j + 1]] = True
                    j += 1
            j += 1
    return flags


def split_contexts(cmd: Command) -> Iterator[Tuple[Word, str]]:
    if cmd.kind == "for":
        for word in cmd.words:
            yield word, "forlist"
        return
    if cmd.kind != "simple":
        return
    name = cmd.name()
    if cmd.words:
        context = "test" if name in ("[", "test") else "arg"
        for word in cmd.words[1:]:
            if name in DECLARATIONS and ASSIGNMENT.match(word.raw):
                continue
            if context == "test" and word.bare() == "]":
                continue
            yield word, context
    for redirect in cmd.redirects:
        if redirect.op not in ("<<", "<<-") and redirect.target is not None:
            yield redirect.target, "redirect"


def first_command_name(tokens: List[Token]) -> Optional[str]:
    for tok in tokens:
        if tok.kind == "word" and tok.word is not None:
            bare = tok.word.bare()
            if bare in COMMAND_START_WORDS:
                continue
            if ASSIGNMENT.match(tok.text):
                continue
            return tok.word.literal()
        if tok.kind != "newline":
            return None
    return None


def every_word(cmd: Command) -> Iterator[Word]:
    yield from cmd.words
    yield from cmd.assignments
    for redirect in cmd.redirects:
        if redirect.target is not None and redirect.op not in ("<<", "<<-"):
            yield redirect.target


def lint_text(text: str, path: str = "-") -> List[Finding]:
    script = parse(text)
    constants = constant_variables(script)
    flags = strict_mode(script)
    findings: List[Finding] = []

    def add(line: int, col: int, check: str, message: str) -> None:
        findings.append(Finding(path, line, col, check, message))

    missing = [k for k, v in flags.items() if not v]
    if missing:
        add(1, 1, "BL004", f"strict mode incomplete: missing {', '.join(missing)} (use set -euo pipefail near the top)")

    for cmd in script.commands:
        for word in every_word(cmd):
            for part in word.parts:
                if part.kind == "backtick":
                    add(part.line, part.col, "BL009", f"use $({part.text.strip()}) instead of backticks")
        for word, context in split_contexts(cmd):
            for part in word.parts:
                if part.quoted:
                    continue
                if part.kind == "param":
                    star = part.name == "*" or part.index == "*"
                    at = part.name == "@" or part.index == "@"
                    if part.op == "#len" or part.op.startswith(("+", ":+")):
                        continue
                    if star:
                        add(part.line, part.col, "BL010", f"{part.text} joins the arguments and splits them again; use \"$@\"")
                    elif at:
                        add(part.line, part.col, "BL011", f"{part.text} re-splits each argument; quote it: \"{part.text}\"")
                    elif context == "forlist" or part.name in SPECIAL_SAFE or part.name in constants:
                        continue
                    elif context == "test":
                        add(part.line, part.col, "BL005", f"quote {part.text} inside [ ]: an empty value leaves the test with a missing operand")
                    else:
                        add(part.line, part.col, "BL001", f"quote {part.text}: unquoted, it is split on spaces and expanded as a glob")
                elif part.kind in ("cmdsub", "backtick"):
                    inner = first_command_name(part.inner)
                    if context == "forlist":
                        if inner == "ls":
                            add(part.line, part.col, "BL002", "iterating over ls output breaks on spaces, newlines and globs; use for f in ./*")
                    elif inner != "seq":
                        add(part.line, part.col, "BL012", "quote the command substitution, or read its lines with mapfile / while read -r")
        name = cmd.name()
        if cmd.kind != "simple" or not name:
            continue
        if name in ("cd", "pushd", "popd") and not flags["errexit"] and not cmd.condition and cmd.followed_by not in ("||", "&&"):
            escape = "return" if cmd.function else "exit"
            add(cmd.words[0].line, cmd.words[0].col, "BL003", f"use '{name} ... || {escape}': if {name} fails the script continues in the wrong directory")
        if name == "cat" and cmd.followed_by == "|" and cmd.preceded_by not in ("|", "|&") and not cmd.redirects:
            args = cmd.words[1:]
            if len(args) == 1 and not (args[0].literal() or "").startswith("-") and not has_glob(args[0]):
                add(args[0].line, args[0].col, "BL006", "useless cat: pass the file to the next command or redirect it with <")
        if name == "read":
            options = [w.literal() or "" for w in cmd.words[1:] if (w.literal() or "").startswith("-")]
            if not any("r" in o for o in options):
                add(cmd.words[0].line, cmd.words[0].col, "BL007", "read without -r turns backslashes into escapes; use read -r")
        if name == "rm":
            check_rm(cmd, add)
        if name == "mktemp" and not has_exit_trap(script):
            add(cmd.words[0].line, cmd.words[0].col, "BL013", "mktemp without trap '...' EXIT: the temporary file survives every early exit")
    whole_file, per_line = directives(text)
    unique: Dict[Tuple[int, int, str], Finding] = {}
    for f in findings:
        if f.check in whole_file or f.check in per_line.get(f.line, set()):
            continue
        unique.setdefault((f.line, f.col, f.check), f)
    return sorted(unique.values(), key=lambda f: (f.line, f.col, f.check))


def has_glob(word: Word) -> bool:
    return any(p.kind == "lit" and not p.quoted and re.search(r"[*?\[]", p.text) for p in word.parts)


DIRECTIVE = re.compile(r"^\s*#\s*(?:shellcheck|bashlint)\s+disable=([A-Za-z0-9,\s]+)")


def directives(text: str) -> Tuple[set, Dict[int, set]]:
    whole_file: set = set()
    per_line: Dict[int, set] = {}
    pending: set = set()
    seen_code = False
    for number, line in enumerate(text.split("\n"), start=1):
        stripped = line.strip()
        m = DIRECTIVE.match(line)
        if m:
            codes = {c.strip().upper() for c in m.group(1).split(",") if c.strip()}
            ids_ = {c.id for c in CHECKS if c.id in codes or (c.shellcheck and c.shellcheck in codes)}
            if seen_code:
                pending |= ids_
            else:
                whole_file |= ids_
            continue
        if not stripped or stripped.startswith("#"):
            continue
        seen_code = True
        if pending:
            per_line[number] = pending
            pending = set()
    return whole_file, per_line


def check_rm(cmd: Command, add) -> None:
    recursive = False
    options_done = False
    for word in cmd.words[1:]:
        lit = word.literal()
        if not options_done and lit is not None and lit.startswith("-"):
            if lit == "--":
                options_done = True
            elif lit == "--recursive" or (not lit.startswith("--") and re.search(r"[rR]", lit)):
                recursive = True
            continue
        if not recursive or not word.parts:
            continue
        head = word.parts[0]
        if head.kind != "param" or head.op.startswith(":?") or head.name in ("@", "*"):
            continue
        rest = word.parts[1:]
        if any(p.kind not in ("lit", "esc", "sq") for p in rest):
            continue
        tail = "".join(p.text for p in rest)
        if tail in ("/", "/*"):
            add(word.line, word.col, "BL008", f"if {head.name} is empty this is rm -r {tail}; write \"${{{head.name}:?}}{tail}\"")


def has_exit_trap(script: Script) -> bool:
    for cmd in script.commands:
        if cmd.name() == "trap":
            signals = [(w.literal() or "").upper() for w in cmd.words[2:]]
            if "EXIT" in signals or "0" in signals or "SIGEXIT" in signals:
                return True
    return False


def is_shell_script(path: str) -> bool:
    if path.endswith((".sh", ".bash")):
        return True
    try:
        with open(path, "rb") as fh:
            first = fh.readline(200)
    except OSError:
        return False
    return first.startswith(b"#!") and re.search(rb"\b(bash|sh)\b", first) is not None


def collect(paths: Sequence[str]) -> List[str]:
    out: List[str] = []
    for path in paths:
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                dirs.sort()
                for name in sorted(files):
                    full = os.path.join(root, name)
                    if is_shell_script(full):
                        out.append(full)
        else:
            out.append(path)
    return out


def render_text(f: Finding) -> str:
    c = CHECKS_BY_ID[f.check]
    code = f" [{c.shellcheck}]" if c.shellcheck else ""
    return f"{f.file}:{f.line}:{f.col}: {c.severity}: {c.id} {c.name}{code}: {f.message}"


def describe_word(word: Word) -> str:
    return " ".join(f"{p.kind}{'(q)' if p.quoted else ''}:{p.text!r}" for p in word.parts)


def explain(script: Script) -> List[Dict[str, object]]:
    rows = []
    for cmd in script.commands:
        rows.append({
            "line": cmd.line,
            "kind": cmd.kind,
            "nested": cmd.nested,
            "function": cmd.function or None,
            "assignments": [w.raw for w in cmd.assignments],
            "words": [w.raw for w in cmd.words],
            "parts": [describe_word(w) for w in cmd.words],
            "redirects": [{"op": r.op, "target": r.target.raw if r.target else None, "heredoc_lines": None if r.body is None else len(r.body.splitlines())} for r in cmd.redirects],
            "followed_by": cmd.followed_by.replace("\n", "newline"),
        })
    return rows


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    parser = argparse.ArgumentParser(prog="bashlint", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", parents=[common], help="lint files or directories")
    check.add_argument("paths", nargs="+")
    check.add_argument("--disable", default="", help="comma-separated check ids to skip, e.g. BL006,BL009")
    check.add_argument("--severity", choices=list(SEVERITY_ORDER), default="style", help="lowest severity to report")
    sub.add_parser("checks", parents=[common], help="list the checks and the ShellCheck codes they mirror")
    exp = sub.add_parser("explain", parents=[common], help="print the commands the tokenizer found")
    exp.add_argument("path")
    return parser


def read_source(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "checks":
        if args.json:
            print(json.dumps([c.__dict__ for c in CHECKS], indent=2))
        else:
            for c in CHECKS:
                print(f"{c.id}  {c.name:<30} {c.shellcheck or '-':<7} {c.severity:<8} {c.summary}")
        return 0
    if args.command == "explain":
        try:
            script = parse(read_source(args.path))
        except (OSError, LexError) as exc:
            print(f"bashlint: {args.path}: {exc}", file=sys.stderr)
            return 2
        rows = explain(script)
        if args.json:
            print(json.dumps({"functions": script.functions, "commands": rows}, indent=2))
        else:
            for row in rows:
                print(f"{row['line']:>4} {row['kind']:<9} {' '.join(row['words'])}  [{row['followed_by']}]")
            for name, line in sorted(script.functions.items()):
                print(f"function {name} at line {line}")
        return 0
    disabled = {d.strip().upper() for d in args.disable.split(",") if d.strip()}
    unknown = disabled - set(CHECKS_BY_ID)
    if unknown:
        print(f"bashlint: unknown check id(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    limit = SEVERITY_ORDER[args.severity]
    findings: List[Finding] = []
    errors = 0
    files = collect(args.paths)
    for path in files:
        try:
            found = lint_text(read_source(path), path)
        except (OSError, UnicodeDecodeError, LexError) as exc:
            print(f"bashlint: {path}: {exc}", file=sys.stderr)
            errors += 1
            continue
        findings.extend(f for f in found if f.check not in disabled and SEVERITY_ORDER[CHECKS_BY_ID[f.check].severity] <= limit)
    if args.json:
        print(json.dumps([f.as_dict() for f in findings], indent=2))
    else:
        for f in findings:
            print(render_text(f))
    print(f"bashlint: {len(findings)} finding(s) in {len(files)} file(s)", file=sys.stderr)
    if errors:
        return 2
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
