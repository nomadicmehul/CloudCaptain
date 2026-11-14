"""A careful HCL structure scanner: blocks, labels and attribute names, no values.

It skips comments, quoted strings with ${...} templates, heredocs and nested
expressions, which is enough to answer "does every variable block have a type,
a description and a validation block" without a full HCL parser.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
HEREDOC = re.compile(r"<<-?([A-Za-z_][A-Za-z0-9_]*)[ \t]*\n")
OPENERS = {"{": "}", "[": "]", "(": ")"}


class HclError(Exception):
    pass


@dataclass
class Block:
    type: str
    labels: List[str]
    attributes: List[str] = field(default_factory=list)
    blocks: List["Block"] = field(default_factory=list)
    line: int = 0

    def children(self, kind: str) -> List["Block"]:
        return [b for b in self.blocks if b.type == kind]


@dataclass
class Token:
    kind: str
    text: str
    line: int


def _skip_string(text: str, start: int) -> int:
    index = start + 1
    depth = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if depth == 0 and char == '"':
            return index + 1
        if text.startswith("${", index) or text.startswith("%{", index):
            depth += 1
            index += 2
            continue
        if depth and char == "{":
            depth += 1
        elif depth and char == "}":
            depth -= 1
        elif depth and char == '"':
            index = _skip_string(text, index)
            continue
        elif char == "\n" and depth == 0:
            raise HclError(f"unterminated string on line {text.count(chr(10), 0, start) + 1}")
        index += 1
    raise HclError(f"unterminated string on line {text.count(chr(10), 0, start) + 1}")


def tokenize(text: str) -> List[Token]:
    tokens: List[Token] = []
    index = 0
    line = 1
    while index < len(text):
        char = text[index]
        if char == "\n":
            tokens.append(Token("newline", "\n", line))
            line += 1
            index += 1
        elif char in " \t\r":
            index += 1
        elif char == "#" or text.startswith("//", index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise HclError(f"unterminated comment on line {line}")
            line += text.count("\n", index, end)
            index = end + 2
        elif char == '"':
            end = _skip_string(text, index)
            tokens.append(Token("string", text[index + 1:end - 1], line))
            index = end
        elif text.startswith("<<", index) and HEREDOC.match(text, index):
            match = HEREDOC.match(text, index)
            assert match is not None
            marker = match.group(1)
            closing = re.compile(r"^[ \t]*" + re.escape(marker) + r"[ \t]*$", re.MULTILINE)
            found = closing.search(text, match.end())
            if not found:
                raise HclError(f"heredoc {marker} on line {line} is never closed")
            tokens.append(Token("string", text[match.end():found.start()], line))
            line += text.count("\n", index, found.end())
            index = found.end()
        elif IDENT.match(text, index):
            match = IDENT.match(text, index)
            assert match is not None
            tokens.append(Token("ident", match.group(0), line))
            index = match.end()
        elif char in "{}[]()=":
            if char == "=" and text.startswith("==", index):
                tokens.append(Token("other", "==", line))
                index += 2
            else:
                tokens.append(Token(char, char, line))
                index += 1
        else:
            tokens.append(Token("other", char, line))
            index += 1
    return tokens


class _Parser:
    def __init__(self, tokens: List[Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self, offset: int = 0) -> Optional[Token]:
        index = self.pos + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def skip_newlines(self) -> None:
        while self.peek() is not None and self.peek().kind == "newline":  # type: ignore[union-attr]
            self.pos += 1

    def skip_expression(self) -> None:
        stack: List[str] = []
        while self.peek() is not None:
            token = self.peek()
            assert token is not None
            if not stack and token.kind in ("newline", "}"):
                return
            if token.kind in OPENERS:
                stack.append(OPENERS[token.kind])
            elif token.kind in ("}", "]", ")"):
                if not stack or stack[-1] != token.kind:
                    raise HclError(f"unbalanced {token.kind!r} on line {token.line}")
                stack.pop()
            self.pos += 1
        if stack:
            raise HclError("expression never closed at end of file")

    def body(self, closing: bool) -> Tuple[List[str], List[Block]]:
        attributes: List[str] = []
        blocks: List[Block] = []
        while True:
            self.skip_newlines()
            token = self.peek()
            if token is None:
                if closing:
                    raise HclError("block never closed at end of file")
                return attributes, blocks
            if token.kind == "}":
                if not closing:
                    raise HclError(f"unexpected '}}' on line {token.line}")
                self.pos += 1
                return attributes, blocks
            if token.kind != "ident":
                raise HclError(f"expected an attribute or a block on line {token.line}, found {token.text!r}")
            name = token.text
            self.pos += 1
            following = self.peek()
            if following is not None and following.kind == "=":
                self.pos += 1
                self.skip_expression()
                attributes.append(name)
                continue
            labels: List[str] = []
            while self.peek() is not None and self.peek().kind in ("string", "ident"):  # type: ignore[union-attr]
                labels.append(self.peek().text)  # type: ignore[union-attr]
                self.pos += 1
            opener = self.peek()
            if opener is None or opener.kind != "{":
                raise HclError(f"expected '{{' after {name} {' '.join(labels)} on line {token.line}")
            self.pos += 1
            inner_attributes, inner_blocks = self.body(True)
            blocks.append(Block(name, labels, inner_attributes, inner_blocks, token.line))


def parse(text: str) -> Block:
    parser = _Parser(tokenize(text))
    attributes, blocks = parser.body(False)
    return Block("file", [], attributes, blocks, 1)


def parse_dir(path: str, suffixes: Tuple[str, ...] = (".tf",)) -> Dict[str, Block]:
    files: Dict[str, Block] = {}
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if os.path.isfile(full) and name.endswith(suffixes):
            with open(full, encoding="utf-8") as handle:
                try:
                    files[name] = parse(handle.read())
                except HclError as error:
                    raise HclError(f"{full}: {error}") from None
    return files


def top_blocks(files: Dict[str, Block], kind: str) -> List[Block]:
    return [block for root in files.values() for block in root.blocks if block.type == kind]
