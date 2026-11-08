"""miniadmit: a validating and mutating admission webhook for Kubernetes. Policies are data, CEL expressions are evaluated the way ValidatingAdmissionPolicy evaluates them, and `test` runs manifests through the same code offline."""
from __future__ import annotations

import argparse
import base64
import copy
import ipaddress
import json
import math
import os
import re
import secrets
import ssl
import subprocess
import sys
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

REVIEW_VERSION = "admission.k8s.io/v1"
ACTIONS = ("Deny", "Warn", "Audit")
OPERATIONS = ("CREATE", "UPDATE", "DELETE", "CONNECT")
FAILURE_POLICIES = ("Fail", "Ignore")
POD_SPEC_PATHS = {
    "Pod": ("spec",),
    "Deployment": ("spec", "template", "spec"),
    "ReplicaSet": ("spec", "template", "spec"),
    "StatefulSet": ("spec", "template", "spec"),
    "DaemonSet": ("spec", "template", "spec"),
    "Job": ("spec", "template", "spec"),
    "CronJob": ("spec", "jobTemplate", "spec", "template", "spec"),
}
KIND_RESOURCES = {
    "Pod": ("", "v1", "pods"),
    "Deployment": ("apps", "v1", "deployments"),
    "ReplicaSet": ("apps", "v1", "replicasets"),
    "StatefulSet": ("apps", "v1", "statefulsets"),
    "DaemonSet": ("apps", "v1", "daemonsets"),
    "Job": ("batch", "v1", "jobs"),
    "CronJob": ("batch", "v1", "cronjobs"),
}
CONTAINER_LISTS = ("initContainers", "containers", "ephemeralContainers")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,61}[a-z0-9])?$")
VARIABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DNS_RE = re.compile(r"^(\*\.)?[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")
NAMESPACE_LABEL = "kubernetes.io/metadata.name"
DEFAULT_LABEL = "app.kubernetes.io/managed-by=miniadmit"
WEBHOOK_DOMAIN = "miniadmit.cloudcaptain.io"
MAX_BODY = 3 * 1024 * 1024
DECLARED = ("object", "oldObject", "request", "params", "namespaceObject", "variables")


class InputError(ValueError):
    pass


class CelSyntaxError(InputError):
    pass


class PatchError(InputError):
    pass


class CelError(Exception):
    pass


TOKEN = re.compile(
    r"(?P<space>\s+)"
    r"|(?P<double>\d+\.\d+(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+)"
    r"|(?P<int>0[xX][0-9a-fA-F]+|\d+)"
    r"|(?P<string>[rR]?(?:'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"))"
    r"|(?P<ident>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<op>&&|\|\||==|!=|<=|>=|[-+*/%!<>?:.,()\[\]{}])"
)
ESCAPES = {"\\": "\\", "'": "'", '"': '"', "`": "`", "?": "?", "n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}
HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}
KEYWORDS = {"true": True, "false": False, "null": None}
RESERVED = {"as", "break", "const", "continue", "else", "for", "function", "if", "import", "let", "loop", "package", "namespace", "return", "var", "void", "while"}
RELATIONS = ("==", "!=", "<", "<=", ">", ">=", "in")
MACROS = ("all", "exists", "exists_one", "map", "filter")
FUNCTIONS = ("size", "int", "double", "string", "bool", "matches")
METHODS = ("size", "startsWith", "endsWith", "contains", "matches", "lowerAscii", "upperAscii", "trim", "split", "join")
UNSUPPORTED_RE2 = re.compile(r"\(\?<?[=!]|\\[1-9]")


@dataclass
class Token:
    kind: str
    value: Any
    pos: int


def syntax_error(text: str, pos: int, message: str) -> CelSyntaxError:
    return CelSyntaxError(f"ERROR: <input>:1:{pos + 1}: {message} in: {text}")


def unescape(body: str, raw: bool, text: str, pos: int) -> str:
    if raw:
        return body
    out: List[str] = []
    i = 0
    while i < len(body):
        if body[i] != "\\":
            out.append(body[i])
            i += 1
            continue
        code = body[i + 1]
        if code in ESCAPES:
            out.append(ESCAPES[code])
            i += 2
        elif code in HEX_ESCAPES:
            digits = body[i + 2 : i + 2 + HEX_ESCAPES[code]]
            if len(digits) != HEX_ESCAPES[code] or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                raise syntax_error(text, pos, f"invalid \\{code} escape")
            out.append(chr(int(digits, 16)))
            i += 2 + HEX_ESCAPES[code]
        else:
            raise syntax_error(text, pos, f"invalid escape sequence \\{code} (write \\\\{code}, or use a raw string r'...')")
    return "".join(out)


def tokenize(text: str) -> List[Token]:
    tokens: List[Token] = []
    pos = 0
    while pos < len(text):
        found = TOKEN.match(text, pos)
        if not found:
            raise syntax_error(text, pos, f"unexpected character {text[pos]!r}")
        kind, raw = found.lastgroup, found.group()
        if kind == "double":
            tokens.append(Token("lit", float(raw), pos))
        elif kind == "int":
            tokens.append(Token("lit", int(raw, 16) if raw[:2].lower() == "0x" else int(raw), pos))
        elif kind == "string":
            is_raw = raw[0] in "rR"
            body = raw[2:-1] if is_raw else raw[1:-1]
            tokens.append(Token("lit", unescape(body, is_raw, text, pos), pos))
        elif kind == "ident":
            if raw in KEYWORDS:
                tokens.append(Token("lit", KEYWORDS[raw], pos))
            elif raw == "in":
                tokens.append(Token("op", "in", pos))
            else:
                tokens.append(Token("ident", raw, pos))
        elif kind == "op":
            tokens.append(Token("op", raw, pos))
        pos = found.end()
    tokens.append(Token("end", None, len(text)))
    return tokens


def describe(token: Token) -> str:
    if token.kind == "end":
        return "end of input"
    return repr(token.value)


class Parser:
    def __init__(self, text: str, declared: Sequence[str]):
        self.text = text
        self.tokens = tokenize(text)
        self.index = 0
        self.scopes: List[set] = [set(declared)]

    def peek(self) -> Token:
        return self.tokens[self.index]

    def advance(self) -> Token:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def at(self, value: str) -> bool:
        token = self.peek()
        return token.kind == "op" and token.value == value

    def accept(self, value: str) -> bool:
        if self.at(value):
            self.advance()
            return True
        return False

    def expect(self, value: str) -> Token:
        if not self.at(value):
            raise syntax_error(self.text, self.peek().pos, f"expected {value!r}, found {describe(self.peek())}")
        return self.advance()

    def parse(self) -> Any:
        node = self.conditional()
        if self.peek().kind != "end":
            raise syntax_error(self.text, self.peek().pos, f"unexpected {describe(self.peek())}")
        return node

    def conditional(self) -> Any:
        node = self.logical_or()
        if self.accept("?"):
            then = self.logical_or()
            self.expect(":")
            return ("cond", node, then, self.conditional())
        return node

    def logical_or(self) -> Any:
        node = self.logical_and()
        while self.accept("||"):
            node = ("or", node, self.logical_and())
        return node

    def logical_and(self) -> Any:
        node = self.relation()
        while self.accept("&&"):
            node = ("and", node, self.relation())
        return node

    def relation(self) -> Any:
        node = self.addition()
        while self.peek().kind == "op" and self.peek().value in RELATIONS:
            op = self.advance().value
            node = ("binary", op, node, self.addition())
        return node

    def addition(self) -> Any:
        node = self.multiplication()
        while self.peek().kind == "op" and self.peek().value in ("+", "-"):
            op = self.advance().value
            node = ("binary", op, node, self.multiplication())
        return node

    def multiplication(self) -> Any:
        node = self.unary()
        while self.peek().kind == "op" and self.peek().value in ("*", "/", "%"):
            op = self.advance().value
            node = ("binary", op, node, self.unary())
        return node

    def unary(self) -> Any:
        if self.accept("!"):
            return ("not", self.unary())
        if self.accept("-"):
            return ("neg", self.unary())
        return self.member()

    def identifier(self) -> str:
        if self.peek().kind != "ident":
            raise syntax_error(self.text, self.peek().pos, f"expected an identifier, found {describe(self.peek())}")
        return self.advance().value

    def member(self) -> Any:
        node = self.primary()
        while True:
            if self.accept("."):
                pos = self.peek().pos
                name = self.identifier()
                if self.accept("("):
                    if name in MACROS:
                        node = self.macro(name, node)
                    elif name in METHODS:
                        node = ("method", name, node, self.arguments())
                    else:
                        raise syntax_error(self.text, pos, f"undeclared reference to '{name}'")
                else:
                    node = ("select", node, name)
            elif self.accept("["):
                key = self.conditional()
                self.expect("]")
                node = ("index", node, key)
            else:
                return node

    def arguments(self) -> List[Any]:
        args: List[Any] = []
        if self.accept(")"):
            return args
        while True:
            args.append(self.conditional())
            if self.accept(")"):
                return args
            self.expect(",")

    def macro(self, name: str, target: Any) -> Any:
        var = self.identifier()
        self.expect(",")
        self.scopes.append({var})
        body = self.conditional()
        self.scopes.pop()
        self.expect(")")
        return ("macro", name, target, var, body)

    def items(self, closer: str) -> List[Any]:
        out: List[Any] = []
        while not self.accept(closer):
            out.append(self.conditional())
            if not self.accept(","):
                self.expect(closer)
                break
        return out

    def entries(self) -> List[Tuple[Any, Any]]:
        out: List[Tuple[Any, Any]] = []
        while not self.accept("}"):
            key = self.conditional()
            self.expect(":")
            out.append((key, self.conditional()))
            if not self.accept(","):
                self.expect("}")
                break
        return out

    def primary(self) -> Any:
        token = self.advance()
        if token.kind == "lit":
            return ("lit", token.value)
        if token.kind == "ident":
            if token.value in RESERVED:
                raise syntax_error(self.text, token.pos, f"reserved identifier {token.value!r}")
            if self.accept("("):
                if token.value == "has":
                    arg = self.conditional()
                    self.expect(")")
                    if arg[0] != "select":
                        raise syntax_error(self.text, token.pos, "invalid argument to has() macro: it takes a field selection such as has(a.b)")
                    return ("has", arg[1], arg[2])
                if token.value not in FUNCTIONS:
                    raise syntax_error(self.text, token.pos, f"undeclared reference to '{token.value}'")
                return ("call", token.value, self.arguments())
            if not any(token.value in scope for scope in self.scopes):
                raise syntax_error(self.text, token.pos, f"undeclared reference to '{token.value}'")
            return ("ident", token.value)
        if token.kind == "op" and token.value == "(":
            node = self.conditional()
            self.expect(")")
            return node
        if token.kind == "op" and token.value == "[":
            return ("list", self.items("]"))
        if token.kind == "op" and token.value == "{":
            return ("map", self.entries())
        raise syntax_error(self.text, token.pos, f"unexpected {describe(token)}")


@dataclass
class Program:
    source: str
    ast: Any


def compile_cel(source: Any, declared: Sequence[str] = DECLARED) -> Program:
    if not isinstance(source, str) or not source.strip():
        raise CelSyntaxError("ERROR: <input>:1:1: an expression is required")
    return Program(source, Parser(source, declared).parse())


class Variables:
    def __init__(self, definitions: Sequence[Tuple[str, Program]], env: Dict[str, Any]):
        self.definitions = dict(definitions)
        self.env = env
        self.values: Dict[str, Any] = {}

    def has(self, name: str) -> bool:
        return name in self.definitions

    def get(self, name: str) -> Any:
        if name not in self.definitions:
            raise CelError(f"no such key: {name}")
        if name not in self.values:
            self.values[name] = evaluate(self.definitions[name], self.env)
        return self.values[name]


def type_name(value: Any) -> str:
    if value is None:
        return "null_type"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, (dict, Variables)):
        return "map"
    return type(value).__name__


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def no_overload(op: str, *values: Any) -> CelError:
    return CelError(f"no such overload: {op} applied to ({', '.join(type_name(v) for v in values)})")


def equal(a: Any, b: Any) -> bool:
    if is_number(a) and is_number(b):
        return a == b
    if type_name(a) != type_name(b):
        return False
    if isinstance(a, list):
        return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
    return a == b


def truncated_division(a: int, b: int) -> int:
    quotient = abs(a) // abs(b)
    return quotient if (a >= 0) == (b >= 0) else -quotient


def arithmetic(op: str, a: Any, b: Any) -> Any:
    if op == "+" and isinstance(a, str) and isinstance(b, str):
        return a + b
    if op == "+" and isinstance(a, list) and isinstance(b, list):
        return a + b
    if not (is_number(a) and is_number(b) and type(a) is type(b)):
        raise no_overload(f"_{op}_", a, b)
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if isinstance(a, float):
        if op == "%":
            raise no_overload("_%_", a, b)
        if b == 0:
            return math.nan if a == 0 or math.isnan(a) else math.copysign(math.inf, a) * math.copysign(1.0, b)
        return a / b
    if b == 0:
        raise CelError("division by zero" if op == "/" else "modulus by zero")
    if op == "/":
        return truncated_division(a, b)
    return a - b * truncated_division(a, b)


def compare(op: str, a: Any, b: Any) -> bool:
    comparable = (is_number(a) and is_number(b)) or (isinstance(a, str) and isinstance(b, str)) or (isinstance(a, bool) and isinstance(b, bool))
    if not comparable:
        raise no_overload(f"_{op}_", a, b)
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    return a >= b


def contains_element(item: Any, container: Any) -> bool:
    if isinstance(container, list):
        return any(equal(item, x) for x in container)
    if isinstance(container, dict) and isinstance(item, (str, int)):
        return item in container
    raise no_overload("@in", item, container)


def select(target: Any, name: str) -> Any:
    if isinstance(target, Variables):
        return target.get(name)
    if isinstance(target, dict):
        if name in target:
            return target[name]
        raise CelError(f"no such key: {name}")
    raise CelError(f"type '{type_name(target)}' does not support field selection")


def index(target: Any, key: Any) -> Any:
    if isinstance(target, list):
        if not isinstance(key, int) or isinstance(key, bool):
            raise no_overload("_[_]", target, key)
        if key < 0 or key >= len(target):
            raise CelError(f"index out of bounds: {key}")
        return target[key]
    if isinstance(target, dict):
        if isinstance(key, (str, int)) and key in target:
            return target[key]
        raise CelError(f"no such key: {key}")
    raise no_overload("_[_]", target, key)


def size_of(value: Any) -> int:
    if isinstance(value, (str, list, dict)):
        return len(value)
    raise no_overload("size", value)


def regex_search(text: Any, pattern: Any) -> bool:
    if not isinstance(text, str) or not isinstance(pattern, str):
        raise no_overload("matches", text, pattern)
    if UNSUPPORTED_RE2.search(pattern):
        raise CelError(f"error parsing regexp: invalid or unsupported Perl syntax in {pattern!r} (RE2 has no lookaround or backreferences)")
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise CelError(f"error parsing regexp: {exc}")
    return compiled.search(text) is not None


def ascii_case(text: str, upper: bool) -> str:
    low, high = ("a", "z") if upper else ("A", "Z")
    return "".join(chr(ord(c) + (-32 if upper else 32)) if low <= c <= high else c for c in text)


def call_function(name: str, args: List[Any]) -> Any:
    if name == "matches" and len(args) == 2:
        return regex_search(args[0], args[1])
    if len(args) != 1:
        raise no_overload(name, *args)
    value = args[0]
    if name == "size":
        return size_of(value)
    if name == "int":
        if is_number(value):
            if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                raise CelError("double out of range of int")
            return int(value)
        if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value):
            return int(value)
    if name == "double":
        if is_number(value):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                pass
    if name == "string":
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (str, int, float)):
            return str(value)
    if name == "bool":
        if isinstance(value, bool):
            return value
        if value in ("true", "false"):
            return value == "true"
    raise no_overload(name, value)


def call_method(name: str, target: Any, args: List[Any]) -> Any:
    if name == "size" and not args:
        return size_of(target)
    if isinstance(target, str):
        if name in ("startsWith", "endsWith", "contains") and len(args) == 1 and isinstance(args[0], str):
            if name == "startsWith":
                return target.startswith(args[0])
            if name == "endsWith":
                return target.endswith(args[0])
            return args[0] in target
        if name == "matches" and len(args) == 1:
            return regex_search(target, args[0])
        if name in ("lowerAscii", "upperAscii") and not args:
            return ascii_case(target, name == "upperAscii")
        if name == "trim" and not args:
            return target.strip()
        if name == "split" and len(args) == 1 and isinstance(args[0], str):
            return list(target) if args[0] == "" else target.split(args[0])
    if isinstance(target, list) and name == "join" and len(args) <= 1 and all(isinstance(x, str) for x in target):
        separator = args[0] if args else ""
        if isinstance(separator, str):
            return separator.join(target)
    raise no_overload(name, target, *args)


def bind(env: Dict[str, Any], name: str, value: Any) -> Dict[str, Any]:
    child = dict(env)
    child[name] = value
    return child


def logical(node: Any, env: Dict[str, Any], stop: bool) -> bool:
    error: Optional[CelError] = None
    for side in (node[1], node[2]):
        try:
            value = run(side, env)
        except CelError as exc:
            error = error or exc
            continue
        if not isinstance(value, bool):
            error = error or no_overload("_||_" if stop else "_&&_", value)
            continue
        if value is stop:
            return stop
    if error is not None:
        raise error
    return not stop


def run_macro(node: Any, env: Dict[str, Any]) -> Any:
    _, name, target_node, var, body = node
    target = run(target_node, env)
    if isinstance(target, dict):
        elements = list(target.keys())
    elif isinstance(target, list):
        elements = target
    else:
        raise no_overload(name, target)
    if name in ("all", "exists"):
        decisive = name == "exists"
        error: Optional[CelError] = None
        for element in elements:
            try:
                value = run(body, bind(env, var, element))
            except CelError as exc:
                error = error or exc
                continue
            if not isinstance(value, bool):
                error = error or no_overload(name, value)
                continue
            if value is decisive:
                return decisive
        if error is not None:
            raise error
        return not decisive
    if name == "map":
        return [run(body, bind(env, var, element)) for element in elements]
    kept: List[Any] = []
    for element in elements:
        value = run(body, bind(env, var, element))
        if not isinstance(value, bool):
            raise no_overload(name, value)
        if value:
            kept.append(element)
    if name == "exists_one":
        return len(kept) == 1
    return kept


def run(node: Any, env: Dict[str, Any]) -> Any:
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "ident":
        if node[1] not in env:
            raise CelError(f"undeclared reference to '{node[1]}'")
        return env[node[1]]
    if kind == "select":
        return select(run(node[1], env), node[2])
    if kind == "has":
        target = run(node[1], env)
        if isinstance(target, Variables):
            return target.has(node[2])
        if isinstance(target, dict):
            return node[2] in target
        raise CelError(f"invalid type for field selection: {type_name(target)}")
    if kind == "index":
        return index(run(node[1], env), run(node[2], env))
    if kind == "list":
        return [run(item, env) for item in node[1]]
    if kind == "map":
        out: Dict[Any, Any] = {}
        for key_node, value_node in node[1]:
            key = run(key_node, env)
            if not isinstance(key, (str, int)):
                raise CelError(f"unsupported map key type: {type_name(key)}")
            out[key] = run(value_node, env)
        return out
    if kind == "not":
        value = run(node[1], env)
        if not isinstance(value, bool):
            raise no_overload("!_", value)
        return not value
    if kind == "neg":
        value = run(node[1], env)
        if not is_number(value):
            raise no_overload("-_", value)
        return -value
    if kind == "and":
        return logical(node, env, False)
    if kind == "or":
        return logical(node, env, True)
    if kind == "cond":
        condition = run(node[1], env)
        if not isinstance(condition, bool):
            raise no_overload("_?_:_", condition)
        return run(node[2] if condition else node[3], env)
    if kind == "binary":
        op, left, right = node[1], run(node[2], env), run(node[3], env)
        if op == "==":
            return equal(left, right)
        if op == "!=":
            return not equal(left, right)
        if op == "in":
            return contains_element(left, right)
        if op in ("<", "<=", ">", ">="):
            return compare(op, left, right)
        return arithmetic(op, left, right)
    if kind == "call":
        return call_function(node[1], [run(arg, env) for arg in node[2]])
    if kind == "method":
        return call_method(node[1], run(node[2], env), [run(arg, env) for arg in node[3]])
    if kind == "macro":
        return run_macro(node, env)
    raise CelError(f"unknown node {kind}")


def evaluate(program: Program, env: Dict[str, Any]) -> Any:
    return run(program.ast, env)


@dataclass
class AdmissionRequest:
    operation: str
    object: Optional[Dict[str, Any]]
    old_object: Optional[Dict[str, Any]] = None
    namespace: str = ""
    name: str = ""
    kind: str = ""
    group: str = ""
    version: str = "v1"
    username: str = ""
    groups: List[str] = field(default_factory=list)
    dry_run: bool = False
    uid: str = ""


def request_view(request: AdmissionRequest) -> Dict[str, Any]:
    resource = KIND_RESOURCES.get(request.kind, (request.group, request.version, request.kind.lower() + "s"))
    return {
        "uid": request.uid,
        "kind": {"group": request.group, "version": request.version, "kind": request.kind},
        "resource": {"group": resource[0], "version": resource[1], "resource": resource[2]},
        "name": request.name,
        "namespace": request.namespace,
        "operation": request.operation,
        "userInfo": {"username": request.username, "groups": list(request.groups)},
        "dryRun": request.dry_run,
    }


def namespace_view(namespace: str) -> Optional[Dict[str, Any]]:
    if not namespace:
        return None
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace, "labels": {NAMESPACE_LABEL: namespace}}}


def cel_environment(request: AdmissionRequest, variables: Sequence[Tuple[str, Program]] = ()) -> Dict[str, Any]:
    env: Dict[str, Any] = {
        "object": request.object,
        "oldObject": request.old_object,
        "request": request_view(request),
        "params": None,
        "namespaceObject": namespace_view(request.namespace),
    }
    env["variables"] = Variables(variables, env)
    return env


@dataclass
class Image:
    registry: str
    repository: str
    tag: Optional[str]
    digest: Optional[str]

    @property
    def name(self) -> str:
        return f"{self.registry}/{self.repository}"


def parse_image(reference: str) -> Image:
    if not isinstance(reference, str) or not reference or any(c.isspace() for c in reference):
        raise InputError(f"not an image reference: {reference!r}")
    ref, digest = reference, None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    first, slash, rest = ref.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        registry, path = first, rest
    else:
        registry, path = "docker.io", ref
    if registry == "index.docker.io":
        registry = "docker.io"
    tag = None
    head, colon, last = path.rpartition(":")
    if colon and "/" not in last:
        path, tag = head, last
    if registry == "docker.io" and "/" not in path:
        path = "library/" + path
    if not path or path != path.lower() or not all(path.split("/")):
        raise InputError(f"not an image reference: {reference!r} (repository names are lowercase path components)")
    return Image(registry, path, tag, digest)


def registry_allowed(image: Image, allowed: Sequence[str]) -> bool:
    for prefix in allowed:
        base = prefix.rstrip("/")
        if image.name == base or image.name.startswith(base + "/"):
            return True
    return False


def get_path(obj: Any, path: Sequence[str]) -> Any:
    node = obj
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def pod_spec_path(obj: Dict[str, Any]) -> Optional[Tuple[str, ...]]:
    return POD_SPEC_PATHS.get(obj.get("kind", ""))


def pod_metadata_path(obj: Dict[str, Any]) -> Optional[Tuple[str, ...]]:
    path = pod_spec_path(obj)
    return path[:-1] + ("metadata",) if path else None


@dataclass
class ContainerRef:
    group: str
    position: int
    container: Dict[str, Any]
    where: str

    @property
    def name(self) -> str:
        return str(self.container.get("name") or f"#{self.position}")


def containers(obj: Dict[str, Any]) -> List[ContainerRef]:
    path = pod_spec_path(obj)
    spec = get_path(obj, path) if path else None
    if not isinstance(spec, dict):
        return []
    found: List[ContainerRef] = []
    for group in CONTAINER_LISTS:
        items = spec.get(group)
        for position, container in enumerate(items if isinstance(items, list) else []):
            if isinstance(container, dict):
                found.append(ContainerRef(group, position, container, ".".join(path) + f".{group}[{position}]"))
    return found


def security_context(container: Dict[str, Any]) -> Dict[str, Any]:
    context = container.get("securityContext")
    return context if isinstance(context, dict) else {}


def as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def check_required_labels(policy: "Policy", obj: Dict[str, Any]) -> List[str]:
    path = pod_metadata_path(obj) or ("metadata",)
    labels = as_dict(get_path(obj, path + ("labels",)))
    where = ".".join(path) + ".labels"
    return [f'missing required label "{key}" ({where})' for key in policy.params["labels"] if not str(labels.get(key) or "")]


def check_privileged(policy: "Policy", obj: Dict[str, Any]) -> List[str]:
    return [f'container "{c.name}" is privileged ({c.where}.securityContext.privileged)' for c in containers(obj) if security_context(c.container).get("privileged") is True]


def image_of(ref: ContainerRef) -> Tuple[str, Optional[Image]]:
    image = ref.container.get("image") or ""
    try:
        return image, parse_image(image)
    except InputError:
        return image, None


def check_registries(policy: "Policy", obj: Dict[str, Any]) -> List[str]:
    allowed = policy.params["registries"]
    out: List[str] = []
    for ref in containers(obj):
        image, parsed = image_of(ref)
        if parsed is None:
            out.append(f'image "{image}" in container "{ref.name}" is not a valid image reference')
        elif not registry_allowed(parsed, allowed):
            out.append(f'image "{image}" in container "{ref.name}" resolves to {parsed.name}, which is not under an allowed registry ({", ".join(allowed)})')
    return out


def check_digest(policy: "Policy", obj: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for ref in containers(obj):
        image, parsed = image_of(ref)
        if parsed is None or not parsed.digest or not DIGEST.match(parsed.digest):
            out.append(f'image "{image}" in container "{ref.name}" is not pinned by digest (@sha256:...)')
    return out


def check_non_root(policy: "Policy", obj: Dict[str, Any]) -> List[str]:
    path = pod_spec_path(obj) or ("spec",)
    pod_level = as_dict(as_dict(get_path(obj, path)).get("securityContext")).get("runAsNonRoot")
    out: List[str] = []
    for ref in containers(obj):
        own = security_context(ref.container).get("runAsNonRoot")
        effective = own if own is not None else pod_level
        if effective is not True:
            out.append(f'container "{ref.name}" must run with runAsNonRoot=true (set it in {".".join(path)}.securityContext or {ref.where}.securityContext)')
    return out


def mutate_requests(policy: "Policy", obj: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(obj)
    for ref in containers(result):
        if ref.group == "ephemeralContainers":
            continue
        resources = as_dict(ref.container.get("resources"))
        requests = as_dict(resources.get("requests"))
        limits = as_dict(resources.get("limits"))
        missing = {name: str(limits.get(name, value)) for name, value in policy.params["requests"].items() if name not in requests}
        if missing:
            merged = dict(requests)
            merged.update(missing)
            ref.container["resources"] = dict(resources)
            ref.container["resources"]["requests"] = merged
    return result


def mutate_labels(policy: "Policy", obj: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(obj)
    meta = get_path(result, pod_metadata_path(result) or ("metadata",))
    if not isinstance(meta, dict):
        return result
    labels = as_dict(meta.get("labels"))
    missing = {key: value for key, value in policy.params["labels"].items() if key not in labels}
    if missing:
        meta["labels"] = dict(labels)
        meta["labels"].update(missing)
    return result


VALIDATORS: Dict[str, Callable[["Policy", Dict[str, Any]], List[str]]] = {
    "required-labels": check_required_labels,
    "no-privileged": check_privileged,
    "allowed-registries": check_registries,
    "image-digest": check_digest,
    "run-as-non-root": check_non_root,
}
MUTATORS: Dict[str, Callable[["Policy", Dict[str, Any]], Dict[str, Any]]] = {
    "default-requests": mutate_requests,
    "default-labels": mutate_labels,
}
PARAMS = {
    "required-labels": ("labels", list),
    "allowed-registries": ("registries", list),
    "default-requests": ("requests", dict),
    "default-labels": ("labels", dict),
}


@dataclass
class Validation:
    program: Program
    message: str = ""
    message_program: Optional[Program] = None


@dataclass
class Policy:
    name: str
    type: str
    actions: List[str]
    kinds: List[str]
    operations: List[str]
    namespaces: List[str] = field(default_factory=list)
    exclude_namespaces: List[str] = field(default_factory=list)
    params: Dict[str, Any] = field(default_factory=dict)
    failure_policy: str = "Fail"
    validations: List[Validation] = field(default_factory=list)
    variables: List[Tuple[str, Program]] = field(default_factory=list)
    match_conditions: List[Tuple[str, Program]] = field(default_factory=list)

    @property
    def mutating(self) -> bool:
        return self.type in MUTATORS


def check_actions(actions: Any, where: str, mutating: bool) -> List[str]:
    if mutating:
        if actions:
            raise InputError(f"{where}: a mutating policy has no actions; it changes the object instead of judging it")
        return []
    if not isinstance(actions, list) or not actions:
        raise InputError(f"{where}: actions must be a non-empty list of {', '.join(ACTIONS)}")
    for action in actions:
        if action not in ACTIONS:
            raise InputError(f"{where}: unknown action {action!r} (use Deny, Warn or Audit)")
    if len(set(actions)) != len(actions):
        raise InputError(f"{where}: actions must not repeat")
    if "Deny" in actions and "Warn" in actions:
        raise InputError(f"{where}: Deny and Warn together report the same failure twice; ValidatingAdmissionPolicyBinding rejects that combination")
    return list(actions)


def check_params(kind: str, raw: Dict[str, Any], where: str) -> Dict[str, Any]:
    if kind not in PARAMS:
        return {}
    key, expected = PARAMS[kind]
    value = raw.get(key)
    if expected is list:
        if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
            raise InputError(f"{where}: type {kind} needs {key}: a non-empty list of strings")
    elif not isinstance(value, dict) or not value or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise InputError(f"{where}: type {kind} needs {key}: a non-empty object of string values")
    return {key: value}


def compile_at(source: Any, where: str) -> Program:
    try:
        return compile_cel(source)
    except CelSyntaxError as exc:
        raise InputError(f"{where}: {exc}")


def named_programs(raw: Any, where: str, pattern: "re.Pattern[str]") -> List[Tuple[str, Program]]:
    out: List[Tuple[str, Program]] = []
    for position, item in enumerate(raw or []):
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or not pattern.match(name):
            raise InputError(f"{where}[{position}]: needs a name and an expression")
        out.append((name, compile_at(item.get("expression"), f"{where}[{position}] ({name})")))
    return out


def load_cel(policy: Policy, raw: Dict[str, Any], where: str) -> None:
    policy.variables = named_programs(raw.get("variables"), f"{where}.variables", VARIABLE_RE)
    policy.match_conditions = named_programs(raw.get("matchConditions"), f"{where}.matchConditions", NAME_RE)
    validations = raw.get("validations")
    if not isinstance(validations, list) or not validations:
        raise InputError(f"{where}: type cel needs validations: a non-empty list of {{expression, message}}")
    for position, item in enumerate(validations):
        if not isinstance(item, dict):
            raise InputError(f"{where}.validations[{position}]: expected an object")
        program = compile_at(item.get("expression"), f"{where}.validations[{position}]")
        message_source = item.get("messageExpression")
        message_program = compile_at(message_source, f"{where}.validations[{position}].messageExpression") if message_source else None
        policy.validations.append(Validation(program, str(item.get("message") or ""), message_program))


def load_policies(doc: Any, source: str = "policies") -> List[Policy]:
    items = doc.get("policies") if isinstance(doc, dict) else doc
    if not isinstance(items, list) or not items:
        raise InputError(f'{source}: expected {{"policies": [...]}} with at least one policy')
    seen = set()
    out: List[Policy] = []
    for position, raw in enumerate(items):
        where = f"{source}: policies[{position}]"
        if not isinstance(raw, dict):
            raise InputError(f"{where}: expected an object")
        name = raw.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise InputError(f"{where}: name must be a lowercase DNS label of at most 63 characters, got {name!r}")
        if name in seen:
            raise InputError(f"{where}: duplicate policy name {name}")
        seen.add(name)
        where = f"{where} ({name})"
        kind = raw.get("type")
        if kind not in VALIDATORS and kind not in MUTATORS and kind != "cel":
            raise InputError(f"{where}: unknown type {kind!r}; known types: {', '.join(sorted(list(VALIDATORS) + list(MUTATORS) + ['cel']))}")
        mutating = kind in MUTATORS
        actions = check_actions(raw.get("actions", [] if mutating else ["Deny"]), where, mutating)
        match = as_dict(raw.get("match"))
        kinds = list(match.get("kinds") or (["Pod"] if kind == "cel" else list(POD_SPEC_PATHS)))
        for item in kinds:
            if item not in POD_SPEC_PATHS:
                raise InputError(f"{where}: match.kinds: {item} carries no pod template (known: {', '.join(POD_SPEC_PATHS)})")
        operations = [str(op).upper() for op in (match.get("operations") or ["CREATE", "UPDATE"])]
        for op in operations:
            if op not in OPERATIONS:
                raise InputError(f"{where}: match.operations: unknown operation {op}")
        failure_policy = raw.get("failurePolicy", "Fail")
        if failure_policy not in FAILURE_POLICIES:
            raise InputError(f"{where}: failurePolicy must be Fail or Ignore")
        policy = Policy(name, kind, actions, kinds, operations, list(match.get("namespaces") or []), list(match.get("excludeNamespaces") or []), check_params(kind, raw, where), failure_policy)
        if kind == "cel":
            load_cel(policy, raw, where)
        elif any(key in raw for key in ("validations", "variables", "matchConditions")):
            raise InputError(f"{where}: validations, variables and matchConditions belong to policies of type cel")
        out.append(policy)
    return out


@dataclass
class Violation:
    policy: str
    actions: List[str]
    message: str
    error: bool = False


@dataclass
class Decision:
    request: AdmissionRequest
    violations: List[Violation]
    patch: List[Dict[str, Any]]
    mutated_by: List[str]
    patched: Optional[Dict[str, Any]]

    @property
    def allowed(self) -> bool:
        return not any("Deny" in v.actions for v in self.violations)

    def policies(self, action: str) -> List[str]:
        return list(dict.fromkeys(v.policy for v in self.violations if action in v.actions))


def applies(policy: Policy, request: AdmissionRequest) -> bool:
    if request.kind not in policy.kinds or request.operation not in policy.operations:
        return False
    if policy.namespaces and request.namespace not in policy.namespaces:
        return False
    if request.namespace in policy.exclude_namespaces:
        return False
    return request.object is not None or policy.type == "cel"


def failure_message(validation: Validation, env: Dict[str, Any]) -> str:
    if validation.message_program is not None:
        try:
            text = evaluate(validation.message_program, env)
        except CelError:
            text = None
        if isinstance(text, str) and text.strip() and "\n" not in text:
            return text.strip()
    if validation.message.strip():
        return validation.message.strip()
    return f"failed expression: {validation.program.source.strip()}"


def cel_violations(policy: Policy, request: AdmissionRequest) -> List[Violation]:
    env = cel_environment(request, policy.variables)

    def failed(prefix: str, program: Program, exc: CelError) -> List[Violation]:
        if policy.failure_policy == "Ignore":
            return []
        return [Violation(policy.name, policy.actions, f"{prefix}expression '{program.source}' resulted in error: {exc}", True)]

    for name, program in policy.match_conditions:
        try:
            value = evaluate(program, env)
        except CelError as exc:
            return failed(f"matchCondition {name}: ", program, exc)
        if value is not True:
            return []
    out: List[Violation] = []
    for validation in policy.validations:
        try:
            value = evaluate(validation.program, env)
        except CelError as exc:
            out.extend(failed("", validation.program, exc))
            continue
        if value is not True:
            out.append(Violation(policy.name, policy.actions, failure_message(validation, env)))
    return out


def validate(policies: Sequence[Policy], request: AdmissionRequest) -> List[Violation]:
    found: List[Violation] = []
    for policy in policies:
        if policy.mutating or not applies(policy, request):
            continue
        if policy.type == "cel":
            found.extend(cel_violations(policy, request))
        else:
            found.extend(Violation(policy.name, policy.actions, message) for message in VALIDATORS[policy.type](policy, request.object or {}))
    return found


def same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def escape_token(key: Any) -> str:
    return str(key).replace("~", "~0").replace("/", "~1")


def unescape_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def json_patch(old: Any, new: Any, path: str = "") -> List[Dict[str, Any]]:
    if isinstance(old, dict) and isinstance(new, dict):
        ops: List[Dict[str, Any]] = [{"op": "remove", "path": f"{path}/{escape_token(key)}"} for key in old if key not in new]
        for key, value in new.items():
            where = f"{path}/{escape_token(key)}"
            if key not in old:
                ops.append({"op": "add", "path": where, "value": copy.deepcopy(value)})
            else:
                ops.extend(json_patch(old[key], value, where))
        return ops
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        ops = []
        for position, (a, b) in enumerate(zip(old, new)):
            ops.extend(json_patch(a, b, f"{path}/{position}"))
        return ops
    if same(old, new):
        return []
    return [{"op": "replace", "path": path, "value": copy.deepcopy(new)}]


def pointer(path: Any) -> List[str]:
    if path == "":
        return []
    if not isinstance(path, str) or not path.startswith("/"):
        raise PatchError(f"a JSON pointer starts with '/': {path!r}")
    return [unescape_token(token) for token in path[1:].split("/")]


def resolve(doc: Any, tokens: Sequence[str]) -> Any:
    node = doc
    for depth, token in enumerate(tokens):
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            raise PatchError(f"path /{'/'.join(escape_token(t) for t in tokens[: depth + 1])} does not exist")
    return node


def list_index(token: str, size: int) -> int:
    if not token.isdigit() or (len(token) > 1 and token[0] == "0"):
        raise PatchError(f"invalid array index {token!r}")
    position = int(token)
    if position >= size:
        raise PatchError(f"array index {position} is out of range")
    return position


def apply_patch(doc: Any, ops: Sequence[Dict[str, Any]]) -> Any:
    result = copy.deepcopy(doc)
    for op in ops:
        kind, path = op.get("op"), op.get("path")
        tokens = pointer(path)
        if kind == "test":
            if not same(resolve(result, tokens), op.get("value")):
                raise PatchError(f"test failed at {path}")
            continue
        if kind not in ("add", "replace", "remove"):
            raise PatchError(f"unsupported JSON Patch op {kind!r}")
        if not tokens:
            if kind == "remove":
                raise PatchError("cannot remove the whole document")
            result = copy.deepcopy(op.get("value"))
            continue
        parent, last = resolve(result, tokens[:-1]), tokens[-1]
        value = copy.deepcopy(op.get("value"))
        if isinstance(parent, dict):
            if kind != "add" and last not in parent:
                raise PatchError(f"cannot {kind} {path}: no such member")
            if kind == "remove":
                del parent[last]
            else:
                parent[last] = value
        elif isinstance(parent, list):
            if kind == "add":
                if last == "-":
                    parent.append(value)
                else:
                    parent.insert(list_index(last, len(parent) + 1), value)
            elif kind == "replace":
                parent[list_index(last, len(parent))] = value
            else:
                del parent[list_index(last, len(parent))]
        else:
            raise PatchError(f"cannot {kind} {path}: the parent is not an object or an array")
    return result


def mutate(policies: Sequence[Policy], request: AdmissionRequest) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    original = request.object
    current = original
    applied: List[str] = []
    for policy in policies:
        if not policy.mutating or current is None or not applies(policy, request):
            continue
        changed = MUTATORS[policy.type](policy, current)
        if not same(changed, current):
            applied.append(policy.name)
            current = changed
    return current, (json_patch(original, current) if original is not None else []), applied


def admit(policies: Sequence[Policy], request: AdmissionRequest) -> Decision:
    patched, ops, applied = mutate(policies, request)
    return Decision(request, validate(policies, replace(request, object=patched)), ops, applied, patched)


def labelled(violation: Violation) -> str:
    return f"[{violation.policy}] {violation.message}"


def validation_response(uid: str, violations: Sequence[Violation]) -> Dict[str, Any]:
    response: Dict[str, Any] = {"uid": uid, "allowed": True}
    denials = [v for v in violations if "Deny" in v.actions]
    warnings = [labelled(v) for v in violations if "Warn" in v.actions]
    audit: Dict[str, List[str]] = {}
    for violation in violations:
        if "Audit" in violation.actions:
            audit.setdefault(violation.policy, []).append(violation.message)
    if denials:
        response["allowed"] = False
        response["status"] = {"code": 403, "reason": "Forbidden", "message": "; ".join(labelled(v) for v in denials)}
    if warnings:
        response["warnings"] = warnings
    if audit:
        response["auditAnnotations"] = {name: json.dumps(messages) for name, messages in audit.items()}
    return response


def mutation_response(uid: str, ops: Sequence[Dict[str, Any]], applied: Sequence[str]) -> Dict[str, Any]:
    response: Dict[str, Any] = {"uid": uid, "allowed": True}
    if ops:
        response["patchType"] = "JSONPatch"
        response["patch"] = base64.b64encode(json.dumps(list(ops)).encode("utf-8")).decode("ascii")
        response["auditAnnotations"] = {"mutated-by": ",".join(applied)}
    return response


def request_from_review(req: Dict[str, Any]) -> AdmissionRequest:
    kind = as_dict(req.get("kind"))
    user = as_dict(req.get("userInfo"))
    obj, old = req.get("object"), req.get("oldObject")
    return AdmissionRequest(
        operation=str(req.get("operation") or ""),
        object=obj if isinstance(obj, dict) else None,
        old_object=old if isinstance(old, dict) else None,
        namespace=str(req.get("namespace") or ""),
        name=str(req.get("name") or ""),
        kind=str(kind.get("kind") or ""),
        group=str(kind.get("group") or ""),
        version=str(kind.get("version") or ""),
        username=str(user.get("username") or ""),
        groups=[str(g) for g in user.get("groups") or []],
        dry_run=bool(req.get("dryRun")),
        uid=str(req.get("uid") or ""),
    )


def request_from_object(obj: Dict[str, Any], namespace: str = "default", operation: str = "CREATE", username: str = "", groups: Sequence[str] = ()) -> AdmissionRequest:
    copied = copy.deepcopy(obj)
    meta = copied.setdefault("metadata", {})
    ns = str(meta.get("namespace") or namespace)
    meta["namespace"] = ns
    group, _, version = str(copied.get("apiVersion") or "v1").rpartition("/")
    return AdmissionRequest(
        operation=operation,
        object=copied,
        old_object=copy.deepcopy(copied) if operation == "UPDATE" else None,
        namespace=ns,
        name=str(meta.get("name") or ""),
        kind=str(copied.get("kind") or ""),
        group=group,
        version=version,
        username=username,
        groups=list(groups),
    )


def handle_review(policies: Sequence[Policy], body: bytes, phase: str) -> Tuple[int, Dict[str, Any], Dict[str, Any]]:
    try:
        review = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 400, {"error": "the request body is not JSON"}, {"phase": phase, "status": 400}
    if not isinstance(review, dict) or review.get("kind") != "AdmissionReview":
        return 400, {"error": "expected an AdmissionReview"}, {"phase": phase, "status": 400}
    if review.get("apiVersion") != REVIEW_VERSION:
        return 400, {"error": f"unsupported apiVersion {review.get('apiVersion')!r}; register the webhook with admissionReviewVersions: [v1]"}, {"phase": phase, "status": 400}
    req = review.get("request")
    if not isinstance(req, dict) or not req.get("uid"):
        return 400, {"error": "AdmissionReview.request.uid is missing"}, {"phase": phase, "status": 400}
    request = request_from_review(req)
    if phase == "mutate":
        _, ops, applied = mutate(policies, request)
        response = mutation_response(request.uid, ops, applied)
    else:
        response = validation_response(request.uid, validate(policies, request))
    summary = {
        "phase": phase,
        "status": 200,
        "uid": request.uid,
        "operation": request.operation,
        "kind": request.kind,
        "namespace": request.namespace,
        "name": request.name or str(as_dict(as_dict(request.object).get("metadata")).get("generateName") or ""),
        "dryRun": request.dry_run,
        "allowed": response["allowed"],
        "patched": "patch" in response,
        "warnings": len(response.get("warnings") or []),
    }
    return 200, {"apiVersion": REVIEW_VERSION, "kind": "AdmissionReview", "response": response}, summary


def make_handler(policies: Sequence[Policy], log: Callable[[str], None]) -> type:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "miniadmit"

        def reply(self, status: int, doc: Dict[str, Any]) -> None:
            body = json.dumps(doc).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if urlsplit(self.path).path in ("/healthz", "/readyz"):
                self.reply(200, {"status": "ok", "policies": len(policies)})
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self) -> None:
            phase = {"/validate": "validate", "/mutate": "mutate"}.get(urlsplit(self.path).path)
            if phase is None:
                self.reply(404, {"error": "POST AdmissionReviews to /validate or /mutate"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY:
                self.reply(400, {"error": "a Content-Length between 1 byte and 3 MiB is required"})
                return
            try:
                status, doc, summary = handle_review(policies, self.rfile.read(length), phase)
            except Exception as exc:
                status, doc, summary = 500, {"error": f"{type(exc).__name__}: {exc}"}, {"phase": phase, "status": 500, "error": str(exc)}
            self.reply(status, doc)
            log(json.dumps(summary, sort_keys=True))

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def build_server(policies: Sequence[Policy], host: str, port: int, cert: str, key: str, log: Optional[Callable[[str], None]] = None) -> ThreadingHTTPServer:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(cert, key)
    except (OSError, ssl.SSLError) as exc:
        raise InputError(f"cannot load the serving certificate {cert} and key {key}: {exc}")
    writer = log or (lambda line: print(line, file=sys.stderr, flush=True))
    server = ThreadingHTTPServer((host, port), make_handler(policies, writer))
    server.daemon_threads = True
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def run_command(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True)


def openssl_config(dns: Sequence[str], ips: Sequence[str]) -> str:
    alt = [f"DNS.{i} = {name}" for i, name in enumerate(dns, 1)] + [f"IP.{i} = {ip}" for i, ip in enumerate(ips, 1)]
    return "\n".join(
        [
            "[req]",
            "distinguished_name = dn",
            "prompt = no",
            "[dn]",
            "CN = miniadmit",
            "[ca_ext]",
            "basicConstraints = critical, CA:TRUE",
            "keyUsage = critical, keyCertSign, cRLSign",
            "subjectKeyIdentifier = hash",
            "[leaf_ext]",
            "basicConstraints = critical, CA:FALSE",
            "keyUsage = critical, digitalSignature",
            "extendedKeyUsage = serverAuth",
            "subjectAltName = @alt_names",
            "[alt_names]",
            *alt,
        ]
    ) + "\n"


def make_certs(out_dir: str, dns: Sequence[str], ips: Sequence[str] = (), days: int = 2, runner: Optional[Callable[[Sequence[str]], Any]] = None, openssl: str = "openssl") -> Dict[str, str]:
    if not dns and not ips:
        raise InputError("certs: give at least one --dns name or --ip address; clients check the subjectAltName, not the CN")
    for name in dns:
        if not DNS_RE.match(name):
            raise InputError(f"certs: {name!r} is not a DNS name")
    for ip in ips:
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            raise InputError(f"certs: {ip!r} is not an IP address")
    if days < 1:
        raise InputError("certs: --days must be at least 1")
    os.makedirs(out_dir, exist_ok=True)
    paths = {key: os.path.join(out_dir, name) for key, name in (("config", "openssl.cnf"), ("ca_key", "ca.key"), ("ca_cert", "ca.crt"), ("key", "tls.key"), ("csr", "tls.csr"), ("cert", "tls.crt"))}
    with open(paths["config"], "w", encoding="utf-8") as handle:
        handle.write(openssl_config(dns, ips))
    keygen = ["genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out"]
    steps = [
        [openssl, *keygen, paths["ca_key"]],
        [openssl, "req", "-new", "-x509", "-config", paths["config"], "-extensions", "ca_ext", "-key", paths["ca_key"], "-subj", "/CN=miniadmit-ca", "-days", str(days), "-out", paths["ca_cert"]],
        [openssl, *keygen, paths["key"]],
        [openssl, "req", "-new", "-config", paths["config"], "-key", paths["key"], "-subj", "/CN=miniadmit-webhook", "-out", paths["csr"]],
        [openssl, "x509", "-req", "-in", paths["csr"], "-CA", paths["ca_cert"], "-CAkey", paths["ca_key"], "-set_serial", "0x" + secrets.token_hex(8), "-days", str(days), "-extfile", paths["config"], "-extensions", "leaf_ext", "-out", paths["cert"]],
    ]
    run = runner or run_command
    for cmd in steps:
        try:
            done = run(cmd)
        except FileNotFoundError:
            raise InputError(f"certs: {openssl} not found on PATH")
        if done.returncode != 0:
            raise InputError(f"certs: {' '.join(cmd[:2])} failed: {(done.stderr or '').strip()}")
    for secret in (paths["ca_key"], paths["key"]):
        if os.path.exists(secret):
            os.chmod(secret, 0o600)
    return paths


def parse_labels(pairs: Sequence[str]) -> Dict[str, str]:
    labels: Dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise InputError(f"--label wants KEY=VALUE, got {pair!r}")
        labels[key] = value
    return labels


def rules_for(kinds: Sequence[str], operations: Sequence[str] = ("CREATE", "UPDATE")) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, str], List[str]] = {}
    for kind in kinds:
        if kind not in KIND_RESOURCES:
            raise InputError(f"unknown kind {kind} (known: {', '.join(KIND_RESOURCES)})")
        group, version, resource = KIND_RESOURCES[kind]
        grouped.setdefault((group, version), []).append(resource)
    return [
        {"apiGroups": [group], "apiVersions": [version], "operations": list(operations), "resources": sorted(set(resources)), "scope": "Namespaced"}
        for (group, version), resources in sorted(grouped.items())
    ]


def namespace_selector(namespaces: Sequence[str]) -> Dict[str, Any]:
    if not namespaces:
        raise InputError("name at least one --namespace: a policy without a namespace scope intercepts the whole cluster, kube-system included")
    if "kube-system" in namespaces:
        raise InputError("refusing to intercept kube-system: a failing webhook there can stop the control plane from healing itself")
    return {"matchExpressions": [{"key": NAMESPACE_LABEL, "operator": "In", "values": sorted(set(namespaces))}]}


def webhook_configs(name: str, ca_pem: str, namespaces: Sequence[str], service: Optional[str] = None, service_namespace: Optional[str] = None, url: Optional[str] = None, port: int = 443, failure_policy: str = "Ignore", timeout: int = 5, kinds: Sequence[str] = tuple(KIND_RESOURCES), labels: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    if not NAME_RE.match(name):
        raise InputError(f"--name {name!r} is not a DNS label")
    if bool(url) == bool(service):
        raise InputError("give either --service (with --service-namespace) or --url")
    if service and not service_namespace:
        raise InputError("--service needs --service-namespace")
    if url and not url.startswith("https://"):
        raise InputError("the API server only calls webhooks over https://")
    if failure_policy not in FAILURE_POLICIES:
        raise InputError("--failure-policy must be Fail or Ignore")
    if not 1 <= timeout <= 30:
        raise InputError("--timeout must be between 1 and 30 seconds (the API server's limit)")
    if "BEGIN CERTIFICATE" not in ca_pem:
        raise InputError("--ca-bundle must be a PEM certificate (the CA that signed the serving certificate)")
    selector = namespace_selector(namespaces)
    if service_namespace and service_namespace in namespaces:
        raise InputError(f"the webhook's own namespace {service_namespace} must not be intercepted: after a restart nothing could admit the webhook's pod")
    ca_bundle = base64.b64encode(ca_pem.encode("utf-8")).decode("ascii")
    rules = rules_for(kinds)

    def hook(prefix: str, path: str) -> Dict[str, Any]:
        client: Dict[str, Any] = {"caBundle": ca_bundle}
        if url:
            client["url"] = url.rstrip("/") + path
        else:
            client["service"] = {"name": service, "namespace": service_namespace, "path": path, "port": port}
        return {
            "name": f"{prefix}.{WEBHOOK_DOMAIN}",
            "admissionReviewVersions": ["v1"],
            "sideEffects": "None",
            "failurePolicy": failure_policy,
            "timeoutSeconds": timeout,
            "matchPolicy": "Equivalent",
            "namespaceSelector": copy.deepcopy(selector),
            "rules": copy.deepcopy(rules),
            "clientConfig": client,
        }

    mutating = hook("mutate", "/mutate")
    mutating["reinvocationPolicy"] = "Never"
    meta = {"name": name, "labels": dict(labels or parse_labels([DEFAULT_LABEL]))}
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "MutatingWebhookConfiguration", "metadata": copy.deepcopy(meta), "webhooks": [mutating]},
            {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingWebhookConfiguration", "metadata": copy.deepcopy(meta), "webhooks": [hook("validate", "/validate")]},
        ],
    }


def vap_objects(policies: Sequence[Policy], namespaces: Sequence[str], labels: Optional[Dict[str, str]] = None) -> Tuple[Dict[str, Any], List[str]]:
    selector = namespace_selector(namespaces)
    meta_labels = dict(labels or parse_labels([DEFAULT_LABEL]))
    items: List[Dict[str, Any]] = []
    skipped: List[str] = []
    for policy in policies:
        if policy.type != "cel":
            skipped.append(policy.name)
            continue
        validations = []
        for validation in policy.validations:
            entry: Dict[str, Any] = {"expression": validation.program.source}
            if validation.message:
                entry["message"] = validation.message
            if validation.message_program is not None:
                entry["messageExpression"] = validation.message_program.source
            validations.append(entry)
        spec: Dict[str, Any] = {"failurePolicy": policy.failure_policy, "matchConstraints": {"resourceRules": rules_for(policy.kinds, policy.operations)}, "validations": validations}
        if policy.match_conditions:
            spec["matchConditions"] = [{"name": name, "expression": program.source} for name, program in policy.match_conditions]
        if policy.variables:
            spec["variables"] = [{"name": name, "expression": program.source} for name, program in policy.variables]
        meta = {"name": policy.name, "labels": dict(meta_labels)}
        items.append({"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicy", "metadata": meta, "spec": spec})
        items.append(
            {
                "apiVersion": "admissionregistration.k8s.io/v1",
                "kind": "ValidatingAdmissionPolicyBinding",
                "metadata": copy.deepcopy(meta),
                "spec": {"policyName": policy.name, "validationActions": list(policy.actions), "matchResources": {"namespaceSelector": copy.deepcopy(selector)}},
            }
        )
    if not items:
        raise InputError("no policy of type cel to translate into a ValidatingAdmissionPolicy")
    return {"apiVersion": "v1", "kind": "List", "items": items}, skipped


def read_text(path: str) -> str:
    try:
        if path == "-":
            return sys.stdin.read()
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}")


def read_json(path: str) -> Any:
    text = read_text(path)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise InputError(f"{path} is not JSON ({exc}); convert YAML with: kubectl create --dry-run=client -o json -f FILE")


def load_objects(doc: Any, source: str) -> List[Dict[str, Any]]:
    if isinstance(doc, dict) and isinstance(doc.get("items"), list):
        items = doc["items"]
    elif isinstance(doc, list):
        items = doc
    else:
        items = [doc]
    for position, item in enumerate(items):
        if not isinstance(item, dict) or not item.get("kind") or not isinstance(item.get("metadata"), dict):
            raise InputError(f"{source}: item {position} is not a Kubernetes object with kind and metadata")
    return items


def decision_json(source: str, decision: Decision) -> Dict[str, Any]:
    request = decision.request
    return {
        "file": source,
        "kind": request.kind,
        "namespace": request.namespace,
        "name": request.name,
        "allowed": decision.allowed,
        "deny": decision.policies("Deny"),
        "warn": decision.policies("Warn"),
        "audit": decision.policies("Audit"),
        "violations": [{"policy": v.policy, "actions": v.actions, "message": v.message, "error": v.error} for v in decision.violations],
        "mutated_by": decision.mutated_by,
        "patch": decision.patch,
        "patched": decision.patched,
    }


def render(decisions: Sequence[Tuple[str, Decision]]) -> str:
    lines: List[str] = []
    for _, decision in decisions:
        request = decision.request
        lines.append(f"{'ALLOWED' if decision.allowed else 'DENIED':<8} {request.kind} {request.namespace}/{request.name}")
        for op in decision.patch:
            value = f" {json.dumps(op['value'], sort_keys=True)}" if "value" in op else ""
            lines.append(f"         patch {op['op']} {op['path']}{value}  ({', '.join(decision.mutated_by)})")
        for violation in decision.violations:
            actions = "+".join(a.lower() for a in violation.actions)
            lines.append(f"         {actions:<10} {labelled(violation)}")
    allowed = sum(1 for _, d in decisions if d.allowed)
    warned = sum(1 for _, d in decisions if d.policies("Warn"))
    lines.append(f"{len(decisions)} object(s): {allowed} allowed, {len(decisions) - allowed} denied, {warned} with warnings")
    return "\n".join(lines)


def command_test(args: argparse.Namespace) -> int:
    policies = load_policies(read_json(args.policies), args.policies)
    decisions: List[Tuple[str, Decision]] = []
    for path in args.manifests:
        for obj in load_objects(read_json(path), path):
            decisions.append((path, admit(policies, request_from_object(obj, args.namespace, args.operation, args.user, args.group))))
    if args.json:
        allowed = sum(1 for _, d in decisions if d.allowed)
        print(json.dumps({"results": [decision_json(p, d) for p, d in decisions], "summary": {"objects": len(decisions), "allowed": allowed, "denied": len(decisions) - allowed}}, indent=2))
    else:
        print(render(decisions))
    return 0 if all(d.allowed for _, d in decisions) else 1


def command_cel(args: argparse.Namespace) -> int:
    program = compile_cel(args.expression)
    obj = read_json(args.object) if args.object else None
    if isinstance(obj, dict):
        request = request_from_object(obj, args.namespace, args.operation)
    else:
        request = AdmissionRequest(operation=args.operation, object=obj, namespace=args.namespace)
    if args.old_object:
        request.old_object = read_json(args.old_object)
    try:
        value = evaluate(program, cel_environment(request))
    except CelError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"error: {exc}")
        return 1
    print(json.dumps({"value": value}) if args.json else json.dumps(value))
    return 0


def command_serve(args: argparse.Namespace) -> int:
    policies = load_policies(read_json(args.policies), args.policies)
    server = build_server(policies, args.host, args.port, args.cert, args.key)
    print(f"miniadmit: {len(policies)} policies, listening on https://{args.host}:{server.server_address[1]} (/validate, /mutate, /healthz)", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def command_certs(args: argparse.Namespace) -> int:
    paths = make_certs(args.out, args.dns, args.ip, args.days)
    print(json.dumps({key: paths[key] for key in ("ca_cert", "cert", "key")}, indent=2))
    return 0


def command_webhook_config(args: argparse.Namespace) -> int:
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    doc = webhook_configs(args.name, read_text(args.ca_bundle), args.namespace, args.service, args.service_namespace, args.url, args.port, args.failure_policy, args.timeout, kinds, parse_labels(args.label or [DEFAULT_LABEL]))
    print(json.dumps(doc, indent=2))
    return 0


def command_vap(args: argparse.Namespace) -> int:
    policies = load_policies(read_json(args.policies), args.policies)
    doc, skipped = vap_objects(policies, args.namespace, parse_labels(args.label or [DEFAULT_LABEL]))
    if skipped:
        print(f"miniadmit: skipped {', '.join(skipped)}: only type cel has a ValidatingAdmissionPolicy form", file=sys.stderr)
    print(json.dumps(doc, indent=2))
    return 0


COMMANDS = {
    "test": command_test,
    "cel": command_cel,
    "serve": command_serve,
    "certs": command_certs,
    "webhook-config": command_webhook_config,
    "vap": command_vap,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="miniadmit", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    test = sub.add_parser("test", help="run manifests through the mutating and then the validating policies, offline")
    test.add_argument("manifests", nargs="+", help="JSON manifests (an object, a List or an array); - reads stdin")
    test.add_argument("--policies", required=True, help="policies JSON file")
    test.add_argument("--namespace", default="default", help="namespace for objects that do not name one (default: default)")
    test.add_argument("--operation", default="CREATE", choices=["CREATE", "UPDATE"])
    test.add_argument("--user", default="", help="request.userInfo.username seen by CEL")
    test.add_argument("--group", action="append", default=[], help="request.userInfo.groups entry (repeatable)")
    test.add_argument("--json", action="store_true", help="machine-readable output")
    cel = sub.add_parser("cel", help="evaluate one CEL expression against an object, as ValidatingAdmissionPolicy would")
    cel.add_argument("expression")
    cel.add_argument("--object", help="the object as JSON; - reads stdin")
    cel.add_argument("--old-object", help="oldObject as JSON")
    cel.add_argument("--namespace", default="default")
    cel.add_argument("--operation", default="CREATE", choices=list(OPERATIONS))
    cel.add_argument("--json", action="store_true")
    serve = sub.add_parser("serve", help="serve /validate and /mutate over HTTPS")
    serve.add_argument("--policies", required=True)
    serve.add_argument("--cert", required=True, help="serving certificate (PEM)")
    serve.add_argument("--key", required=True, help="serving key (PEM)")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8443)
    certs = sub.add_parser("certs", help="create a CA and a serving certificate with openssl")
    certs.add_argument("--out", required=True, help="directory for ca.crt, tls.crt and tls.key")
    certs.add_argument("--dns", action="append", default=[], help="subjectAltName DNS entry, e.g. SERVICE.NAMESPACE.svc (repeatable)")
    certs.add_argument("--ip", action="append", default=[], help="subjectAltName IP entry (repeatable)")
    certs.add_argument("--days", type=int, default=2)
    hook = sub.add_parser("webhook-config", help="print Mutating and ValidatingWebhookConfiguration objects with safe defaults")
    hook.add_argument("--name", required=True)
    hook.add_argument("--ca-bundle", required=True, help="PEM file of the CA that signed the serving certificate")
    hook.add_argument("--namespace", action="append", required=True, help="namespace the webhooks intercept (repeatable)")
    hook.add_argument("--service", help="Service in front of miniadmit serve")
    hook.add_argument("--service-namespace")
    hook.add_argument("--url", help="https URL instead of a Service, for a webhook outside the cluster")
    hook.add_argument("--port", type=int, default=443)
    hook.add_argument("--failure-policy", default="Ignore", choices=list(FAILURE_POLICIES))
    hook.add_argument("--timeout", type=int, default=5)
    hook.add_argument("--kinds", default=",".join(KIND_RESOURCES))
    hook.add_argument("--label", action="append", help=f"KEY=VALUE label on the objects (default {DEFAULT_LABEL})")
    vap = sub.add_parser("vap", help="translate cel policies into ValidatingAdmissionPolicy and Binding objects")
    vap.add_argument("--policies", required=True)
    vap.add_argument("--namespace", action="append", required=True, help="namespace the bindings select (repeatable)")
    vap.add_argument("--label", action="append", help=f"KEY=VALUE label on the objects (default {DEFAULT_LABEL})")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return COMMANDS[args.command](args)
    except InputError as exc:
        print(f"miniadmit: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
