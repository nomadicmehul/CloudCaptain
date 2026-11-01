"""minimod: load OpenTofu modules written in JSON syntax, evaluate variables, locals and outputs, expand count and for_each into instance addresses, and plan moved blocks against a state file."""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union

Key = Union[None, int, str]
ModulePath = Tuple[Tuple[str, Key], ...]

RESOURCE_META = {"count", "for_each", "depends_on", "lifecycle", "provider", "provisioner", "connection", "//"}
MODULE_META = {"source", "version", "count", "for_each", "depends_on", "providers", "//"}
VARIABLE_KEYS = {"type", "default", "description", "sensitive", "nullable", "validation", "//"}
OUTPUT_KEYS = {"value", "description", "sensitive", "depends_on", "//"}
REPLACE_ON_ANY_CHANGE = {"local_file", "local_sensitive_file"}
SYMBOLS = {"create": "+", "update": "~", "replace": "-/+", "delete": "-"}
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
NUMBER_RE = re.compile(r"-?\d+(\.\d+)?([eE][+-]?\d+)?")
TOKEN_RE = re.compile(
    r"(?P<space>\s+)"
    r"|(?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_-]*)"
    r"|(?P<op>==|!=|<=|>=|&&|\|\||[-+*/%<>!?:.,()\[\]{}=])"
)
TYPE_TOKEN_RE = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*|[(){}=,:])")
ADDRESS_TOKEN_RE = re.compile(r'(\.?)([A-Za-z_][A-Za-z0-9_-]*)|\[(\d+)\]|\[("(?:[^"\\]|\\.)*")\]')
TFVARS_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.+)$")
LEVELS = [("||",), ("&&",), ("==", "!="), ("<", ">", "<=", ">="), ("+", "-"), ("*", "/", "%")]


class MiniModError(Exception):
    def __init__(self, summary: str, detail: str = "") -> None:
        super().__init__(f"{summary}: {detail}" if detail else summary)
        self.summary = summary
        self.detail = detail

    def render(self) -> str:
        lines = [f"Error: {self.summary}"]
        if self.detail:
            lines.append("")
            lines.extend(f"  {line}".rstrip() for line in self.detail.splitlines())
        return "\n".join(lines)


class ConversionError(Exception):
    pass


class Unknown:
    def __repr__(self) -> str:
        return "(known after apply)"


UNKNOWN = Unknown()
MISSING = object()


def is_unknown(value: Any) -> bool:
    return isinstance(value, Unknown)


class ResourceObject:
    def __init__(self, address: str, attributes: Dict[str, Any], sensitive: Sequence[str]) -> None:
        self.address = address
        self.attributes = attributes
        self.sensitive = set(sensitive)


class ModuleObject:
    def __init__(self, instance: "ModuleInstance") -> None:
        self.instance = instance


def contains_unknown(value: Any) -> bool:
    if is_unknown(value):
        return True
    if isinstance(value, ResourceObject):
        return contains_unknown(value.attributes)
    if isinstance(value, dict):
        return any(contains_unknown(item) for item in value.values())
    if isinstance(value, (list, tuple, frozenset)):
        return any(contains_unknown(item) for item in value)
    return False


def normalize_number(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def format_number(value: Union[int, float]) -> str:
    value = normalize_number(value)
    return str(value) if isinstance(value, int) else repr(value)


def type_label(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "tuple"
    if isinstance(value, frozenset):
        return "set"
    if isinstance(value, ModuleObject):
        return "module object"
    if isinstance(value, ResourceObject):
        return "resource object"
    if isinstance(value, dict):
        return "object"
    if is_unknown(value):
        return "unknown"
    return type(value).__name__


def sort_key(value: Any) -> Tuple[int, Any]:
    if isinstance(value, bool):
        return (2, str(value))
    if isinstance(value, (int, float)):
        return (0, value)
    return (1, str(value))


def ordered(value: Any) -> List[Any]:
    return sorted(value, key=sort_key) if isinstance(value, frozenset) else list(value)


def to_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return format_number(value)
    raise MiniModError("Invalid value", f"a string is required, got {type_label(value)}")


def to_number(value: Any) -> Union[int, float]:
    if isinstance(value, bool):
        raise MiniModError("Invalid value", "a number is required, got bool")
    if isinstance(value, (int, float)):
        return normalize_number(value)
    if isinstance(value, str) and NUMBER_RE.fullmatch(value.strip()):
        return normalize_number(float(value))
    raise MiniModError("Invalid value", f"a number is required, got {type_label(value)}")


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value in ("true", "false"):
        return value == "true"
    raise MiniModError("Invalid value", f"a bool is required, got {type_label(value)}")


def values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(values_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(values_equal(left[key], right[key]) for key in left)
    return type(left) is type(right) and left == right


def to_json(value: Any) -> Any:
    if is_unknown(value):
        return None
    if isinstance(value, ResourceObject):
        return to_json(value.attributes)
    if isinstance(value, dict):
        return {key: to_json(item) for key, item in value.items()}
    if isinstance(value, frozenset):
        return [to_json(item) for item in ordered(value)]
    if isinstance(value, (list, tuple)):
        return [to_json(item) for item in value]
    return normalize_number(value)


def display(value: Any) -> str:
    if is_unknown(value):
        return "(known after apply)"
    if isinstance(value, ResourceObject):
        return display(value.attributes)
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(key, ensure_ascii=False)}: {display(item)}" for key, item in value.items()) + "}"
    if isinstance(value, frozenset):
        return "toset([" + ", ".join(display(item) for item in ordered(value)) + "])"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(display(item) for item in value) + "]"
    return json.dumps(to_json(value), ensure_ascii=False)


def skip_string(text: str, index: int) -> int:
    while index < len(text):
        if text[index] == "\\":
            index += 2
        elif text[index] == '"':
            return index + 1
        elif text.startswith("$${", index):
            index += 3
        elif text.startswith("${", index):
            index = find_close(text, index + 2) + 1
        else:
            index += 1
    raise MiniModError("Unterminated string", f"missing closing quote in {text!r}")


def find_close(text: str, index: int) -> int:
    depth = 0
    while index < len(text):
        char = text[index]
        if char == '"':
            index = skip_string(text, index + 1)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            if depth == 0:
                return index
            depth -= 1
        index += 1
    raise MiniModError("Unclosed template interpolation", f"missing }} in {text!r}")


def split_template(text: str, unescape: bool = False) -> List[Any]:
    parts: List[Any] = []
    literal: List[str] = []
    index = 0
    while index < len(text):
        if unescape and text[index] == "\\" and index + 1 < len(text):
            literal.append(ESCAPES.get(text[index + 1], text[index + 1]))
            index += 2
        elif text.startswith("$${", index) or text.startswith("%%{", index):
            literal.append(text[index + 1 : index + 3])
            index += 3
        elif text.startswith("%{", index):
            raise MiniModError("Unsupported template directive", f"minimod does not implement %{{ if }} or %{{ for }} directives (in {text!r}); use a conditional or a function")
        elif text.startswith("${", index):
            end = find_close(text, index + 2)
            if literal:
                parts.append("".join(literal))
                literal = []
            parts.append(parse_expression(text[index + 2 : end]))
            index = end + 1
        else:
            literal.append(text[index])
            index += 1
    if literal or not parts:
        parts.append("".join(literal))
    return parts


def compile_template(text: str, unescape: bool = False) -> Any:
    parts = split_template(text, unescape)
    if len(parts) == 1:
        return ("lit", parts[0]) if isinstance(parts[0], str) else parts[0]
    return ("template", parts)


def compile_json(value: Any) -> Any:
    if isinstance(value, str):
        return compile_template(value)
    if isinstance(value, list):
        return ("list", [compile_json(item) for item in value])
    if isinstance(value, dict):
        return ("object", [(compile_template(key), compile_json(item)) for key, item in value.items() if key != "//"])
    return ("lit", value)


def tokenize(text: str) -> List[Tuple[str, str]]:
    tokens: List[Tuple[str, str]] = []
    index = 0
    while index < len(text):
        if text[index] == '"':
            end = skip_string(text, index + 1)
            tokens.append(("string", text[index + 1 : end - 1]))
            index = end
            continue
        match = TOKEN_RE.match(text, index)
        if not match:
            raise MiniModError("Invalid expression", f"unexpected character {text[index]!r} in {text.strip()!r}")
        if match.lastgroup != "space":
            tokens.append((match.lastgroup or "", match.group()))
        index = match.end()
    tokens.append(("end", ""))
    return tokens


class Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = tokenize(text)
        self.position = 0

    def peek(self, offset: int = 0) -> Tuple[str, str]:
        return self.tokens[min(self.position + offset, len(self.tokens) - 1)]

    def next(self) -> Tuple[str, str]:
        token = self.peek()
        self.position += 1
        return token

    def accept(self, op: str) -> bool:
        if self.peek() == ("op", op):
            self.position += 1
            return True
        return False

    def expect(self, op: str) -> None:
        if not self.accept(op):
            raise self.error(f"expected {op!r} but found {self.peek()[1] or 'the end'!r}")

    def error(self, message: str) -> MiniModError:
        return MiniModError("Invalid expression", f"{message} in {self.text.strip()!r}")

    def parse(self) -> Any:
        node = self.conditional()
        if self.peek()[0] != "end":
            raise self.error(f"unexpected {self.peek()[1]!r}")
        return node

    def conditional(self) -> Any:
        condition = self.binary(0)
        if self.accept("?"):
            when_true = self.conditional()
            self.expect(":")
            return ("cond", condition, when_true, self.conditional())
        return condition

    def binary(self, level: int) -> Any:
        if level == len(LEVELS):
            return self.unary()
        left = self.binary(level + 1)
        while self.peek()[0] == "op" and self.peek()[1] in LEVELS[level]:
            op = self.next()[1]
            left = ("binop", op, left, self.binary(level + 1))
        return left

    def unary(self) -> Any:
        if self.accept("!"):
            return ("unop", "!", self.unary())
        if self.accept("-"):
            return ("unop", "-", self.unary())
        return self.postfix(self.primary())

    def postfix(self, node: Any) -> Any:
        while True:
            if self.accept("."):
                kind, value = self.next()
                if kind != "name":
                    raise self.error("expected an attribute name after '.'")
                node = ("attr", node, value)
            elif self.peek() == ("op", "[") and self.peek(1) == ("op", "*") and self.peek(2) == ("op", "]"):
                self.position += 3
                node = ("splat", node, self.splat_steps())
            elif self.accept("["):
                key = self.conditional()
                self.expect("]")
                node = ("index", node, key)
            else:
                return node

    def splat_steps(self) -> List[Tuple[str, Any]]:
        steps: List[Tuple[str, Any]] = []
        while True:
            if self.peek() == ("op", ".") and self.peek(1)[0] == "name":
                self.position += 1
                steps.append(("attr", self.next()[1]))
            elif self.peek() == ("op", "[") and self.peek(1) != ("op", "*"):
                self.position += 1
                key = self.conditional()
                self.expect("]")
                steps.append(("index", key))
            else:
                return steps

    def primary(self) -> Any:
        kind, value = self.next()
        if kind == "number":
            return ("lit", normalize_number(float(value)) if not value.isdigit() else int(value))
        if kind == "string":
            return compile_template(value, unescape=True)
        if kind == "name":
            if value in ("true", "false"):
                return ("lit", value == "true")
            if value == "null":
                return ("lit", None)
            if self.accept("("):
                args: List[Any] = []
                while not self.accept(")"):
                    args.append(self.conditional())
                    if not self.accept(","):
                        self.expect(")")
                        break
                return ("call", value, args)
            return ("ref", value)
        if (kind, value) == ("op", "("):
            node = self.conditional()
            self.expect(")")
            return node
        if (kind, value) == ("op", "["):
            if self.peek() == ("name", "for"):
                raise self.error("minimod does not implement for expressions")
            items: List[Any] = []
            while not self.accept("]"):
                items.append(self.conditional())
                if not self.accept(","):
                    self.expect("]")
                    break
            return ("list", items)
        if (kind, value) == ("op", "{"):
            if self.peek() == ("name", "for"):
                raise self.error("minimod does not implement for expressions")
            pairs: List[Tuple[Any, Any]] = []
            while not self.accept("}"):
                if self.peek()[0] == "name" and self.peek(1) in (("op", "="), ("op", ":")):
                    key: Any = ("lit", self.next()[1])
                else:
                    key = self.conditional()
                if not (self.accept("=") or self.accept(":")):
                    raise self.error("expected = or : in an object constructor")
                pairs.append((key, self.conditional()))
                self.accept(",")
            return ("object", pairs)
        raise self.error(f"unexpected {value or 'end of expression'!r}")


def parse_expression(text: str) -> Any:
    return Parser(text).parse()


def hcl_format(pattern: Any, *args: Any) -> str:
    pattern = to_string(pattern)
    pieces: List[str] = []
    position = 0
    used = 0
    while position < len(pattern):
        if pattern[position] != "%":
            pieces.append(pattern[position])
            position += 1
            continue
        verb = pattern[position + 1 : position + 2]
        position += 2
        if verb == "%":
            pieces.append("%")
            continue
        if used >= len(args):
            raise MiniModError("Error in function call", "format: not enough arguments for the format string")
        value = args[used]
        used += 1
        if verb in ("s", "v"):
            pieces.append(json.dumps(to_json(value)) if isinstance(value, (list, dict, frozenset)) else to_string(value))
        elif verb == "d":
            pieces.append(str(int(to_number(value))))
        else:
            raise MiniModError("Error in function call", f"format: minimod implements %s, %d, %v and %%, not %{verb}")
    return "".join(pieces)


def regex_match(pattern: Any, text: Any) -> Any:
    match = re.search(to_string(pattern), to_string(text))
    if match is None:
        raise MiniModError("Error in function call", "regex: the pattern did not match the given string")
    if match.groupdict():
        return dict(match.groupdict())
    if match.groups():
        return list(match.groups())
    return match.group(0)


def lookup(mapping: Dict[str, Any], key: Any, *default: Any) -> Any:
    if key in mapping:
        return mapping[key]
    if default:
        return default[0]
    raise MiniModError("Error in function call", f'lookup: the key "{key}" does not exist and no default was given')


FUNCTIONS: Dict[str, Callable[..., Any]] = {
    "length": lambda value: len(value if not isinstance(value, ResourceObject) else value.attributes),
    "upper": lambda text: to_string(text).upper(),
    "lower": lambda text: to_string(text).lower(),
    "join": lambda separator, items: to_string(separator).join(to_string(item) for item in ordered(items)),
    "contains": lambda items, value: any(values_equal(item, value) for item in items),
    "keys": lambda mapping: sorted(mapping),
    "values": lambda mapping: [mapping[key] for key in sorted(mapping)],
    "merge": lambda *maps: {key: value for mapping in maps for key, value in mapping.items()},
    "concat": lambda *lists: [item for items in lists for item in items],
    "lookup": lookup,
    "toset": lambda items: frozenset(items),
    "tostring": to_string,
    "tonumber": to_number,
    "format": hcl_format,
    "regex": regex_match,
    "startswith": lambda text, prefix: to_string(text).startswith(to_string(prefix)),
    "endswith": lambda text, suffix: to_string(text).endswith(to_string(suffix)),
}


@dataclass
class Scope:
    module: "ModuleInstance"
    each: Optional[Tuple[str, Any]] = None
    count: Optional[int] = None
    validating: Optional[Tuple[str, Any]] = None


def evaluate(node: Any, scope: Scope) -> Any:
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "template":
        return render_template(node[1], scope)
    if kind == "list":
        return [evaluate(item, scope) for item in node[1]]
    if kind == "object":
        result: Dict[str, Any] = {}
        unknown_key = False
        for key_node, value_node in node[1]:
            key = evaluate(key_node, scope)
            value = evaluate(value_node, scope)
            if is_unknown(key):
                unknown_key = True
                continue
            result[to_string(key)] = value
        return UNKNOWN if unknown_key else result
    if kind == "ref":
        raise MiniModError("Invalid reference", f'A reference to "{node[1]}" must be followed by an attribute, as in {node[1]}.NAME.')
    if kind == "attr":
        if node[1][0] == "ref":
            return scope.module.reference(scope, node[1][1], node[2])
        return get_attribute(evaluate(node[1], scope), node[2], scope.module.run)
    if kind == "index":
        return get_index(evaluate(node[1], scope), evaluate(node[2], scope), scope.module.run)
    if kind == "splat":
        source = evaluate(node[1], scope)
        if is_unknown(source):
            return UNKNOWN
        items = [] if source is None else source if isinstance(source, list) else [source]
        return [walk_steps(item, node[2], scope) for item in items]
    if kind == "call":
        return call_function(node[1], node[2], scope)
    if kind == "unop":
        value = evaluate(node[2], scope)
        if is_unknown(value):
            return UNKNOWN
        return (not to_bool(value)) if node[1] == "!" else normalize_number(-to_number(value))
    if kind == "binop":
        return binary(node[1], evaluate(node[2], scope), evaluate(node[3], scope))
    if kind == "cond":
        condition = evaluate(node[1], scope)
        if is_unknown(condition):
            return UNKNOWN
        return evaluate(node[2] if to_bool(condition) else node[3], scope)
    raise MiniModError("Invalid expression", f"unknown node type {kind!r}")


def render_template(parts: Sequence[Any], scope: Scope) -> Any:
    pieces: List[str] = []
    unknown = False
    for part in parts:
        if isinstance(part, str):
            pieces.append(part)
            continue
        value = evaluate(part, scope)
        if is_unknown(value):
            unknown = True
        elif value is None:
            raise MiniModError("Invalid template interpolation value", "The expression result is null. Cannot include a null value in a string template.")
        elif isinstance(value, (list, dict, frozenset, ModuleObject, ResourceObject)):
            raise MiniModError("Invalid template interpolation value", f"Cannot include the given value in a string template: string required, got {type_label(value)}.")
        else:
            pieces.append(to_string(value))
    return UNKNOWN if unknown else "".join(pieces)


def walk_steps(value: Any, steps: Sequence[Tuple[str, Any]], scope: Scope) -> Any:
    for step, argument in steps:
        if step == "attr":
            value = get_attribute(value, argument, scope.module.run)
        else:
            value = get_index(value, evaluate(argument, scope), scope.module.run)
    return value


def get_attribute(value: Any, name: str, run: "Run") -> Any:
    if is_unknown(value):
        return UNKNOWN
    if isinstance(value, ModuleObject):
        return value.instance.output(name)
    if isinstance(value, ResourceObject):
        run.mark(name in value.sensitive)
        return value.attributes.get(name, UNKNOWN)
    if isinstance(value, dict):
        if name not in value:
            raise MiniModError("Unsupported attribute", f'This object does not have an attribute named "{name}".')
        return value[name]
    raise MiniModError("Unsupported attribute", f'Can\'t access attribute "{name}" on a value of type {type_label(value)}.')


def get_index(value: Any, key: Any, run: "Run") -> Any:
    if is_unknown(value) or is_unknown(key):
        return UNKNOWN
    if isinstance(value, (ModuleObject, ResourceObject)):
        return get_attribute(value, to_string(key), run)
    if isinstance(value, list):
        number = to_number(key)
        if not isinstance(number, int) or not 0 <= number < len(value):
            raise MiniModError("Invalid index", f"The given key does not identify an element in this collection value: index {display(key)} of a list with {len(value)} element(s).")
        return value[number]
    if isinstance(value, dict):
        text = to_string(key)
        if text not in value:
            raise MiniModError("Invalid index", f'The given key does not identify an element in this collection value: no key "{text}" (keys: {", ".join(sorted(value)) or "none"}).')
        return value[text]
    if isinstance(value, frozenset):
        raise MiniModError("Invalid index", "Elements of a set are identified only by their value and don't have any separate index or key to select with, so it's only possible to perform operations across all elements of the set.")
    raise MiniModError("Invalid index", f"This value does not have any indices ({type_label(value)}).")


def call_function(name: str, arg_nodes: Sequence[Any], scope: Scope) -> Any:
    if name == "can":
        if len(arg_nodes) != 1:
            raise MiniModError("Invalid function call", "can() takes exactly one argument")
        try:
            value = evaluate(arg_nodes[0], scope)
        except MiniModError:
            return False
        return UNKNOWN if is_unknown(value) else True
    function = FUNCTIONS.get(name)
    if function is None:
        raise MiniModError("Call to unknown function", f'There is no function named "{name}" in minimod (it implements can, {", ".join(sorted(FUNCTIONS))}).')
    args = [evaluate(node, scope) for node in arg_nodes]
    if any(is_unknown(arg) for arg in args):
        return UNKNOWN
    try:
        return function(*args)
    except MiniModError:
        raise
    except (TypeError, ValueError, KeyError, IndexError, AttributeError, re.error) as error:
        raise MiniModError("Error in function call", f'Call to function "{name}" failed: {error}.') from None


def binary(op: str, left: Any, right: Any) -> Any:
    if is_unknown(left) or is_unknown(right):
        return UNKNOWN
    if op == "&&":
        return to_bool(left) and to_bool(right)
    if op == "||":
        return to_bool(left) or to_bool(right)
    if op == "==":
        return values_equal(left, right)
    if op == "!=":
        return not values_equal(left, right)
    a, b = to_number(left), to_number(right)
    if op == "<":
        return a < b
    if op == ">":
        return a > b
    if op == "<=":
        return a <= b
    if op == ">=":
        return a >= b
    if op in ("/", "%") and b == 0:
        raise MiniModError("Operation failed", "Can't divide by zero.")
    results = {"+": lambda: a + b, "-": lambda: a - b, "*": lambda: a * b, "/": lambda: a / b, "%": lambda: a % b}
    return normalize_number(results[op]())


def parse_type(text: str) -> Tuple:
    tokens: List[str] = []
    position = 0
    while text[position:].strip():
        match = TYPE_TOKEN_RE.match(text, position)
        if not match:
            raise MiniModError("Invalid type specification", f"cannot read {text!r}")
        tokens.append(match.group(1))
        position = match.end()
    result, used = parse_type_tokens(tokens, 0, text)
    if used != len(tokens):
        raise MiniModError("Invalid type specification", f"unexpected {tokens[used]!r} in {text!r}")
    return result


def parse_type_tokens(tokens: List[str], index: int, text: str) -> Tuple[Tuple, int]:
    def expect(position: int, token: str) -> int:
        if position >= len(tokens) or tokens[position] != token:
            raise MiniModError("Invalid type specification", f"expected {token!r} in {text!r}")
        return position + 1

    if index >= len(tokens):
        raise MiniModError("Invalid type specification", f"incomplete type {text!r}")
    name = tokens[index]
    if name in ("string", "number", "bool", "any"):
        return (name,), index + 1
    if name in ("list", "set", "map"):
        inner, index = parse_type_tokens(tokens, expect(index + 1, "("), text)
        return (name, inner), expect(index, ")")
    if name == "object":
        index = expect(expect(index + 1, "("), "{")
        attributes: Dict[str, Tuple] = {}
        while index < len(tokens) and tokens[index] != "}":
            attribute = tokens[index]
            if index + 1 >= len(tokens) or tokens[index + 1] not in ("=", ":"):
                raise MiniModError("Invalid type specification", f"expected = after {attribute!r} in {text!r}")
            attributes[attribute], index = parse_type_tokens(tokens, index + 2, text)
            if index < len(tokens) and tokens[index] == ",":
                index += 1
        return ("object", attributes), expect(expect(index, "}"), ")")
    raise MiniModError("Invalid type specification", f"minimod understands string, number, bool, any, list(T), set(T), map(T) and object({{...}}); got {text!r}")


def type_name(typ: Tuple) -> str:
    if typ[0] in ("list", "set", "map"):
        return f"{typ[0]} of {type_name(typ[1])}"
    return typ[0]


def convert(value: Any, typ: Tuple) -> Any:
    kind = typ[0]
    if kind == "any" or value is None or is_unknown(value):
        return value
    if isinstance(value, ResourceObject):
        value = value.attributes
    if kind == "string":
        if isinstance(value, (str, bool, int, float)):
            return to_string(value)
        raise ConversionError("string required")
    if kind == "number":
        try:
            return to_number(value)
        except MiniModError:
            raise ConversionError("a number is required") from None
    if kind == "bool":
        try:
            return to_bool(value)
        except MiniModError:
            raise ConversionError("a bool is required") from None
    if kind in ("list", "set"):
        if not isinstance(value, (list, frozenset)):
            raise ConversionError(f"{type_name(typ)} required")
        items = [convert_element(item, typ[1], f"element {index}") for index, item in enumerate(ordered(value))]
        if kind == "list":
            return items
        try:
            return frozenset(items)
        except TypeError:
            raise ConversionError("minimod sets can hold only strings, numbers and bools") from None
    if kind == "map":
        if not isinstance(value, dict):
            raise ConversionError(f"{type_name(typ)} required")
        return {key: convert_element(item, typ[1], f'element "{key}"') for key, item in value.items()}
    if not isinstance(value, dict):
        raise ConversionError("object required")
    for attribute in typ[1]:
        if attribute not in value:
            raise ConversionError(f'attribute "{attribute}" is required')
    return {attribute: convert_element(value[attribute], attribute_type, f'attribute "{attribute}"') for attribute, attribute_type in typ[1].items()}


def convert_element(value: Any, typ: Tuple, label: str) -> Any:
    try:
        return convert(value, typ)
    except ConversionError as error:
        raise ConversionError(f"{label}: {error}") from None


def render_key(key: Key) -> str:
    if key is None:
        return ""
    if isinstance(key, int) and not isinstance(key, bool):
        return f"[{key}]"
    return "[" + json.dumps(key, ensure_ascii=False) + "]"


def render_module_path(path: ModulePath) -> str:
    return ".".join(f"module.{name}{render_key(key)}" for name, key in path)


@dataclass(frozen=True)
class Address:
    modules: ModulePath = ()
    rtype: Optional[str] = None
    name: Optional[str] = None
    key: Key = None

    def __str__(self) -> str:
        prefix = render_module_path(self.modules)
        if self.rtype is None:
            return prefix
        resource = f"{self.rtype}.{self.name}{render_key(self.key)}"
        return f"{prefix}.{resource}" if prefix else resource

    def within(self, path: ModulePath) -> "Address":
        return Address(path + self.modules, self.rtype, self.name, self.key)


def parse_address(text: str) -> Address:
    text = text.strip()
    invalid = MiniModError("Invalid address", f"{text!r} is not a resource or module address such as module.web[\"blog\"].local_file.page[0]")
    tokens: List[Tuple[str, Any]] = []
    position = 0
    while position < len(text):
        match = ADDRESS_TOKEN_RE.match(text, position)
        if not match or (match.group(2) and bool(match.group(1)) != (position > 0)):
            raise invalid
        if match.group(2):
            tokens.append(("name", match.group(2)))
        elif match.group(3):
            tokens.append(("key", int(match.group(3))))
        else:
            tokens.append(("key", json.loads(match.group(4))))
        position = match.end()
    modules: List[Tuple[str, Key]] = []
    index = 0
    while index < len(tokens) and tokens[index] == ("name", "module"):
        if index + 1 >= len(tokens) or tokens[index + 1][0] != "name":
            raise invalid
        name = tokens[index + 1][1]
        index += 2
        key: Key = None
        if index < len(tokens) and tokens[index][0] == "key":
            key = tokens[index][1]
            index += 1
        modules.append((name, key))
    if index == len(tokens):
        if not modules:
            raise invalid
        return Address(tuple(modules))
    if tokens[index] == ("name", "data"):
        raise MiniModError("Invalid address", f"{text!r} is a data source; only managed resources and modules have state to move")
    if index + 1 >= len(tokens) or tokens[index][0] != "name" or tokens[index + 1][0] != "name":
        raise invalid
    rtype, name = tokens[index][1], tokens[index + 1][1]
    index += 2
    key = None
    if index < len(tokens) and tokens[index][0] == "key":
        key = tokens[index][1]
        index += 1
    if index != len(tokens):
        raise invalid
    return Address(tuple(modules), rtype, name, key)


@dataclass
class Validation:
    condition: Any
    error_message: Any


@dataclass
class VariableSpec:
    name: str
    type: Tuple
    file: str
    has_default: bool = False
    default: Any = None
    sensitive: bool = False
    validations: List[Validation] = field(default_factory=list)


@dataclass
class OutputSpec:
    name: str
    value: Any
    file: str
    sensitive: bool = False


@dataclass
class ResourceSpec:
    rtype: str
    name: str
    file: str
    attributes: Dict[str, Any]
    count: Any = None
    for_each: Any = None


@dataclass
class ModuleCall:
    name: str
    source: str
    file: str
    inputs: Dict[str, Any]
    count: Any = None
    for_each: Any = None
    version: Optional[str] = None
    definition: Optional["ModuleDef"] = None


@dataclass
class MovedSpec:
    source: Address
    target: Address
    file: str


@dataclass
class ModuleDef:
    directory: str
    rel_dir: str
    variables: Dict[str, VariableSpec] = field(default_factory=dict)
    locals: Dict[str, Any] = field(default_factory=dict)
    outputs: Dict[str, OutputSpec] = field(default_factory=dict)
    resources: Dict[Tuple[str, str], ResourceSpec] = field(default_factory=dict)
    modules: Dict[str, ModuleCall] = field(default_factory=dict)
    moved: List[MovedSpec] = field(default_factory=list)
    origins: Dict[str, str] = field(default_factory=dict)


def as_list(body: Any) -> List[Any]:
    return body if isinstance(body, list) else [body]


def labeled(body: Any) -> Iterator[Tuple[str, Any]]:
    for item in as_list(body):
        for label, content in item.items():
            if label != "//":
                yield label, content


def body_of(content: Any, what: str, file: str) -> Dict[str, Any]:
    items = as_list(content)
    if len(items) != 1 or not isinstance(items[0], dict):
        raise MiniModError(f"Invalid {what} block", f"{file}: expected one JSON object for {what}")
    return items[0]


def claim(definition: ModuleDef, label: str, file: str, summary: str) -> None:
    if label in definition.origins:
        raise MiniModError(summary, f"{label} is declared in {definition.origins[label]} and again in {file}.")
    definition.origins[label] = file


def check_keys(spec: Dict[str, Any], allowed: set, what: str, file: str) -> None:
    for key in spec:
        if key not in allowed:
            raise MiniModError("Unsupported argument", f'{file}: an argument named "{key}" is not expected in {what}.')


def optional_expression(spec: Dict[str, Any], key: str) -> Any:
    return compile_json(spec[key]) if key in spec else None


def parse_variable(name: str, spec: Dict[str, Any], file: str) -> VariableSpec:
    check_keys(spec, VARIABLE_KEYS, f'variable "{name}"', file)
    typ = parse_type(spec["type"]) if "type" in spec else ("any",)
    variable = VariableSpec(name, typ, file, sensitive=bool(spec.get("sensitive", False)))
    for rule in as_list(spec.get("validation", [])):
        if "condition" not in rule or "error_message" not in rule:
            raise MiniModError("Invalid validation block", f'{file}: every validation on variable "{name}" needs "condition" and "error_message".')
        variable.validations.append(Validation(compile_json(rule["condition"]), compile_json(rule["error_message"])))
    if "default" in spec:
        try:
            variable.default = convert(spec["default"], typ)
        except ConversionError as error:
            raise MiniModError("Invalid default value for variable", f'{file}: the default for variable "{name}" is not a valid {type_name(typ)}: {error}.') from None
        variable.has_default = True
    return variable


def parse_file(definition: ModuleDef, data: Any, file: str) -> None:
    if not isinstance(data, dict):
        raise MiniModError("Invalid configuration", f"{file}: the top level must be a JSON object")
    for block_type, body in data.items():
        if block_type in ("//", "terraform", "provider"):
            continue
        if block_type == "variable":
            for name, content in labeled(body):
                claim(definition, f'variable "{name}"', file, "Duplicate variable declaration")
                definition.variables[name] = parse_variable(name, body_of(content, "variable", file), file)
        elif block_type == "locals":
            for item in as_list(body):
                for name, value in item.items():
                    if name != "//":
                        claim(definition, f"local.{name}", file, "Duplicate local value definition")
                        definition.locals[name] = compile_json(value)
        elif block_type == "output":
            for name, content in labeled(body):
                claim(definition, f'output "{name}"', file, "Duplicate output definition")
                spec = body_of(content, "output", file)
                check_keys(spec, OUTPUT_KEYS, f'output "{name}"', file)
                if "value" not in spec:
                    raise MiniModError("Missing required argument", f'{file}: output "{name}" needs a "value".')
                definition.outputs[name] = OutputSpec(name, compile_json(spec["value"]), file, bool(spec.get("sensitive", False)))
        elif block_type == "resource":
            for rtype, by_name in labeled(body):
                for name, content in labeled(by_name):
                    claim(definition, f"{rtype}.{name}", file, f'Duplicate resource "{rtype}" configuration')
                    spec = body_of(content, "resource", file)
                    attributes = {key: compile_json(value) for key, value in spec.items() if key not in RESOURCE_META}
                    definition.resources[(rtype, name)] = ResourceSpec(rtype, name, file, attributes, optional_expression(spec, "count"), optional_expression(spec, "for_each"))
        elif block_type == "module":
            for name, content in labeled(body):
                claim(definition, f"module.{name}", file, "Duplicate module call")
                spec = body_of(content, "module", file)
                if not isinstance(spec.get("source"), str):
                    raise MiniModError("Missing required argument", f'{file}: module "{name}" needs a "source" string.')
                inputs = {key: compile_json(value) for key, value in spec.items() if key not in MODULE_META}
                definition.modules[name] = ModuleCall(name, spec["source"], file, inputs, optional_expression(spec, "count"), optional_expression(spec, "for_each"), spec.get("version"))
        elif block_type == "moved":
            for item in as_list(body):
                if not isinstance(item.get("from"), str) or not isinstance(item.get("to"), str):
                    raise MiniModError("Invalid moved block", f'{file}: a moved block needs "from" and "to" addresses as strings.')
                definition.moved.append(MovedSpec(parse_address(item["from"]), parse_address(item["to"]), file))
        elif block_type == "data":
            raise MiniModError("Unsupported block type", f"{file}: minimod does not implement data sources; tofu reads them during plan.")
        else:
            raise MiniModError("Unsupported block type", f'{file}: blocks of type "{block_type}" are not expected here.')


def check_call(call: ModuleCall) -> None:
    child = call.definition
    assert child is not None
    for name in call.inputs:
        if name not in child.variables:
            raise MiniModError("Unsupported argument", f'{call.file}: module "{call.name}" passes "{name}", but {child.rel_dir} declares no variable "{name}".')
    for name, variable in child.variables.items():
        if not variable.has_default and name not in call.inputs:
            raise MiniModError("Missing required argument", f'{call.file}: module "{call.name}" must set "{name}"; the variable has no default in {variable.file}.')


def check_moves(definition: ModuleDef) -> None:
    for spec in definition.moved:
        source, target = spec.source, spec.target
        if (source.rtype is None) != (target.rtype is None):
            raise MiniModError('Invalid "moved" addresses', f"{spec.file}: cannot move {source} to {target}; both must be resources or both must be modules.")
        if source.rtype is not None:
            if source.rtype != target.rtype:
                raise MiniModError("Resource type mismatch", f"{spec.file}: {source} and {target} are different resource types; moved only renames, it cannot change the type.")
            renamed = (source.modules, source.name) != (target.modules, target.name)
            if renamed and not source.modules and (source.rtype, source.name) in definition.resources:
                raise MiniModError("Moved object still exists", f"{spec.file}: this statement moves {source}, but {source.rtype}.{source.name} is still declared. Rename the resource block to {target.rtype}.{target.name}.")
        elif len(source.modules) == 1 and source.modules[0][0] in definition.modules and source.modules[0][0] != target.modules[-1][0]:
            raise MiniModError("Moved object still exists", f'{spec.file}: this statement moves {source}, but module "{source.modules[0][0]}" is still declared. Rename the module block to "{target.modules[-1][0]}".')


def load_module(directory: str, root: str, chain: Tuple[str, ...] = ()) -> ModuleDef:
    directory = os.path.normpath(os.path.abspath(directory))
    root = os.path.normpath(os.path.abspath(root))
    if directory in chain:
        raise MiniModError("Module cycle", " -> ".join(os.path.relpath(item, root) for item in chain + (directory,)))
    if not os.path.isdir(directory):
        raise MiniModError("Module not found", f"{directory} is not a directory")
    native = sorted(glob.glob(os.path.join(directory, "*.tf")))
    if native:
        raise MiniModError("Unsupported file", f"{os.path.relpath(native[0], root)} is HCL native syntax; minimod reads only *.tf.json files (tofu reads both)")
    files = sorted(glob.glob(os.path.join(directory, "*.tf.json")))
    if not files:
        raise MiniModError("No configuration files", f"{directory} contains no *.tf.json files")
    rel_dir = os.path.relpath(directory, root).replace(os.sep, "/")
    definition = ModuleDef(directory, rel_dir)
    for path in files:
        relative = os.path.relpath(path, root).replace(os.sep, "/")
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except ValueError as error:
            raise MiniModError("Invalid JSON", f"{relative}: {error}") from None
        parse_file(definition, data, relative)
    for call in definition.modules.values():
        if not call.source.startswith(("./", "../")):
            raise MiniModError("Module not installed", f'module "{call.name}" ({call.file}) has source "{call.source}". minimod loads only local paths that start with ./ or ../; tofu init downloads registry and git sources into .terraform/modules.')
        call.definition = load_module(os.path.join(directory, call.source), root, chain + (directory,))
        check_call(call)
    check_moves(definition)
    return definition


def resolve_modules(value: Any) -> Any:
    if isinstance(value, ModuleObject):
        return {name: resolve_modules(value.instance.output(name)) for name in sorted(value.instance.definition.outputs)}
    if isinstance(value, dict):
        return {key: resolve_modules(item) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_modules(item) for item in value]
    if isinstance(value, tuple):
        return tuple(resolve_modules(item) for item in value)
    return value


def shape(count: Any, for_each: Any, objects: List[Tuple[Key, Any]]) -> Any:
    if count is not None:
        return [item for _, item in objects]
    if for_each is not None:
        return {key: item for key, item in objects}
    return objects[0][1]


@dataclass
class Instance:
    address: Address
    attributes: Dict[str, Any]


class Run:
    def __init__(self, definition: ModuleDef, inputs: Dict[str, Tuple[Any, bool]]) -> None:
        self.stack: List[str] = []
        self.taint: List[bool] = []
        self.root_inputs = inputs
        self.root = ModuleInstance(self, definition, ())
        self.instances: List[ModuleInstance] = [self.root]

    def tracked(self, label: str, compute: Callable[[], Any]) -> Tuple[Any, bool]:
        if label in self.stack:
            cycle = self.stack[self.stack.index(label):] + [label]
            raise MiniModError("Cycle", " -> ".join(cycle))
        self.stack.append(label)
        self.taint.append(False)
        try:
            value = resolve_modules(compute())
            return value, self.taint[-1]
        finally:
            self.stack.pop()
            self.taint.pop()

    def mark(self, sensitive: bool) -> None:
        if sensitive and self.taint:
            self.taint[-1] = True

    def evaluate_all(self) -> Dict[str, Tuple[Any, bool]]:
        index = 0
        while index < len(self.instances):
            self.instances[index].evaluate_everything()
            index += 1
        return self.root_outputs()

    def root_outputs(self) -> Dict[str, Tuple[Any, bool]]:
        result = {}
        for name, spec in sorted(self.root.definition.outputs.items()):
            value = self.root.output(name)
            sensitive = self.root.output_is_sensitive(name)
            if sensitive and not spec.sensitive:
                raise MiniModError("Output refers to sensitive values", f'output "{name}" ({spec.file}) is derived from a sensitive value. Add "sensitive": true to the output to confirm you mean to expose it.')
            result[name] = (value, sensitive)
        return result

    def resources(self) -> List[Instance]:
        found = []
        for instance in self.instances:
            for spec in instance.definition.resources.values():
                for key, attributes, _ in instance.resource_instances(spec):
                    found.append(Instance(Address(instance.path, spec.rtype, spec.name, key), attributes))
        return sorted(found, key=lambda item: str(item.address))

    def moves(self) -> List[Tuple[Address, Address]]:
        return [(spec.source.within(instance.path), spec.target.within(instance.path)) for instance in self.instances for spec in instance.definition.moved]


class ModuleInstance:
    def __init__(self, run: Run, definition: ModuleDef, path: ModulePath, call: Optional[ModuleCall] = None, caller: Optional[Scope] = None) -> None:
        self.run = run
        self.definition = definition
        self.path = path
        self.call = call
        self.caller = caller
        self.cache: Dict[Tuple, Tuple[Any, bool]] = {}

    @property
    def prefix(self) -> str:
        return render_module_path(self.path)

    def label(self, text: str) -> str:
        return f"{self.prefix}.{text}" if self.path else text

    def where(self) -> str:
        return f"{self.prefix} ({self.definition.rel_dir})" if self.path else "the root module"

    def memo(self, key: Tuple, label: str, compute: Callable[[], Any]) -> Any:
        if key not in self.cache:
            self.cache[key] = self.run.tracked(self.label(label), compute)
        value, sensitive = self.cache[key]
        self.run.mark(sensitive)
        return value

    def reference(self, scope: Scope, root: str, name: str) -> Any:
        if scope.validating is not None and root != "var":
            raise MiniModError("Invalid reference in variable validation", f"minimod evaluates validation conditions with only input variables in scope; {root}.{name} is not one.")
        if root == "var":
            if scope.validating is not None and scope.validating[0] == name:
                return scope.validating[1]
            return self.variable(name)
        if root == "local":
            return self.local(name)
        if root == "module":
            return self.module_value(name)
        if root == "each":
            if scope.each is None:
                raise MiniModError('Reference to "each" in context without for_each', f"each.{name} is only available in a resource or module block that sets for_each.")
            if name not in ("key", "value"):
                raise MiniModError("Invalid each attribute", f'"each" has only key and value, not {name}.')
            return scope.each[0] if name == "key" else scope.each[1]
        if root == "count":
            if scope.count is None:
                raise MiniModError('Reference to "count" in non-counted context', "count.index is only available in a resource or module block that sets count.")
            if name != "index":
                raise MiniModError("Invalid count attribute", f'"count" has only index, not {name}.')
            return scope.count
        if root == "path":
            if name not in ("module", "root"):
                raise MiniModError("Invalid path attribute", f"minimod knows path.module and path.root, not path.{name}.")
            return self.definition.rel_dir if name == "module" else "."
        if root == "terraform" and name == "workspace":
            return "default"
        if root in ("data", "self", "terraform"):
            raise MiniModError("Unsupported reference", f"minimod does not implement {root}.{name}.")
        return self.resource_value(root, name)

    def variable(self, name: str) -> Any:
        spec = self.definition.variables.get(name)
        if spec is None:
            raise MiniModError("Reference to undeclared input variable", f'An input variable with the name "{name}" has not been declared in {self.where()}.')
        return self.memo(("var", name), f"var.{name}", lambda: self.compute_variable(spec))

    def raw_input(self, spec: VariableSpec) -> Any:
        if self.call is None:
            raw, from_text = self.run.root_inputs.get(spec.name, (MISSING, False))
            if raw is not MISSING and from_text and spec.type[0] not in ("string", "number", "bool", "any"):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raise MiniModError("Invalid value for input variable", f"minimod reads -var and TF_VAR_ values for {type_name(spec.type)} variables as JSON; could not parse {raw!r} for var.{spec.name}.") from None
        elif spec.name in self.call.inputs:
            assert self.caller is not None
            raw = evaluate(self.call.inputs[spec.name], self.caller)
        else:
            raw = MISSING
        if raw is not MISSING:
            return raw
        if spec.has_default:
            return spec.default
        raise MiniModError("No value for required variable", f'The root module input variable "{spec.name}" is not set, and has no default value. Use -var or -var-file to set it.')

    def compute_variable(self, spec: VariableSpec) -> Any:
        raw = self.raw_input(spec)
        try:
            value = convert(raw, spec.type)
        except ConversionError as error:
            raise MiniModError("Invalid value for input variable", f"The given value is not suitable for {self.label('var.' + spec.name)} declared at {spec.file}: {error}.") from None
        self.run.mark(spec.sensitive)
        for index, rule in enumerate(spec.validations):
            self.check_rule(spec, value, index, rule)
        return value

    def check_rule(self, spec: VariableSpec, value: Any, index: int, rule: Validation) -> None:
        scope = Scope(self, validating=(spec.name, value))
        result = evaluate(rule.condition, scope)
        if is_unknown(result):
            return
        if not isinstance(result, bool):
            raise MiniModError("Invalid validation result", f"The condition for {self.label('var.' + spec.name)} returned {type_label(result)}, not bool.")
        if result:
            return
        message = evaluate(rule.error_message, scope)
        shown = "(sensitive value)" if spec.sensitive else display(value)
        raise MiniModError("Invalid value for variable", f"{to_string(message) if isinstance(message, str) else display(message)}\n\n{self.label('var.' + spec.name)} is {shown}\nThis was checked by the validation rule at {spec.file} (variable \"{spec.name}\", validation {index}).")

    def local(self, name: str) -> Any:
        node = self.definition.locals.get(name)
        if node is None:
            raise MiniModError("Reference to undeclared local value", f'A local value with the name "{name}" has not been declared in {self.where()}.')
        return self.memo(("local", name), f"local.{name}", lambda: evaluate(node, Scope(self)))

    def expansion(self, count_node: Any, for_each_node: Any, what: str) -> List[Tuple[Key, Scope]]:
        if count_node is not None and for_each_node is not None:
            raise MiniModError('Invalid combination of "count" and "for_each"', f'{self.label(what)} sets both "count" and "for_each"; use one.')
        if count_node is not None:
            value, _ = self.run.tracked(self.label(f"{what}.count"), lambda: evaluate(count_node, Scope(self)))
            if is_unknown(value):
                raise MiniModError("Invalid count argument", f'The "count" value for {self.label(what)} depends on values that cannot be determined until apply, so the number of instances is not known during plan.')
            number = to_number(value)
            if not isinstance(number, int) or number < 0:
                raise MiniModError("Invalid count argument", f'The given "count" argument value for {self.label(what)} is unsuitable: it must be a whole number greater than or equal to zero, got {display(value)}.')
            return [(index, Scope(self, count=index)) for index in range(number)]
        if for_each_node is not None:
            value, sensitive = self.run.tracked(self.label(f"{what}.for_each"), lambda: evaluate(for_each_node, Scope(self)))
            if sensitive:
                raise MiniModError("Invalid for_each argument", f"Sensitive values, or values derived from sensitive values, cannot be used as for_each arguments ({self.label(what)}): the keys would appear in every instance address.")
            if is_unknown(value) or (isinstance(value, frozenset) and contains_unknown(value)):
                raise MiniModError("Invalid for_each argument", f'The "for_each" value for {self.label(what)} includes keys that cannot be determined until apply, so the instance addresses are not known during plan.')
            if isinstance(value, dict):
                return [(key, Scope(self, each=(key, item))) for key, item in sorted(value.items())]
            if isinstance(value, frozenset) and all(isinstance(item, str) for item in value):
                return [(item, Scope(self, each=(item, item))) for item in sorted(value)]
            raise MiniModError("Invalid for_each argument", f'The given "for_each" argument value for {self.label(what)} is unsuitable: the "for_each" argument must be a map, or set of strings, and you have provided a value of type {type_label(value)}.')
        return [(None, Scope(self))]

    def resource_instances(self, spec: ResourceSpec) -> List[Tuple[Key, Dict[str, Any], List[str]]]:
        return self.memo(("resource", spec.rtype, spec.name), f"{spec.rtype}.{spec.name}", lambda: self.expand_resource(spec))

    def expand_resource(self, spec: ResourceSpec) -> List[Tuple[Key, Dict[str, Any], List[str]]]:
        instances = []
        for key, scope in self.expansion(spec.count, spec.for_each, f"{spec.rtype}.{spec.name}"):
            address = Address(self.path, spec.rtype, spec.name, key)
            attributes: Dict[str, Any] = {}
            sensitive: List[str] = []
            for attribute, node in spec.attributes.items():
                value, tainted = self.run.tracked(f"{address}.{attribute}", lambda node=node, scope=scope: evaluate(node, scope))
                attributes[attribute] = value
                if tainted:
                    sensitive.append(attribute)
            instances.append((key, attributes, sensitive))
        return instances

    def resource_value(self, rtype: str, name: str) -> Any:
        spec = self.definition.resources.get((rtype, name))
        if spec is None:
            raise MiniModError("Reference to undeclared resource", f'A managed resource "{rtype}" "{name}" has not been declared in {self.where()}.')
        objects = [(key, ResourceObject(str(Address(self.path, rtype, name, key)), attributes, sensitive)) for key, attributes, sensitive in self.resource_instances(spec)]
        return shape(spec.count, spec.for_each, objects)

    def module_instances(self, call: ModuleCall) -> List[Tuple[Key, "ModuleInstance"]]:
        return self.memo(("module", call.name), f"module.{call.name}", lambda: self.expand_module(call))

    def expand_module(self, call: ModuleCall) -> List[Tuple[Key, "ModuleInstance"]]:
        assert call.definition is not None
        children = []
        for key, scope in self.expansion(call.count, call.for_each, f"module.{call.name}"):
            child = ModuleInstance(self.run, call.definition, self.path + ((call.name, key),), call, scope)
            self.run.instances.append(child)
            children.append((key, child))
        return children

    def module_value(self, name: str) -> Any:
        call = self.definition.modules.get(name)
        if call is None:
            raise MiniModError("Reference to undeclared module", f'No module call named "{name}" is declared in {self.where()}.')
        return shape(call.count, call.for_each, [(key, ModuleObject(child)) for key, child in self.module_instances(call)])

    def output(self, name: str) -> Any:
        spec = self.definition.outputs.get(name)
        if spec is None:
            raise MiniModError("Unsupported attribute", f'{self.prefix or "The root module"} has no output named "{name}".')
        value = self.memo(("output", name), f"output.{name}", lambda: evaluate(spec.value, Scope(self)))
        self.run.mark(spec.sensitive)
        return value

    def output_is_sensitive(self, name: str) -> bool:
        return self.definition.outputs[name].sensitive or self.cache[("output", name)][1]

    def evaluate_everything(self) -> None:
        for name in self.definition.variables:
            self.variable(name)
        for name in self.definition.locals:
            self.local(name)
        for spec in self.definition.resources.values():
            self.resource_instances(spec)
        for call in self.definition.modules.values():
            self.module_instances(call)
        for name in self.definition.outputs:
            self.output(name)


def read_tfvars(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        raise MiniModError("Failed to read variables file", f"{path} does not exist")
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    if path.endswith(".json"):
        try:
            data = json.loads(text)
        except ValueError as error:
            raise MiniModError("Invalid variables file", f"{path}: {error}") from None
        if not isinstance(data, dict):
            raise MiniModError("Invalid variables file", f"{path}: the top level must be a JSON object")
        return data
    values: Dict[str, Any] = {}
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//")):
            continue
        match = TFVARS_LINE_RE.match(stripped)
        try:
            if not match:
                raise ValueError(stripped)
            values[match.group(1)] = json.loads(match.group(2))
        except ValueError:
            raise MiniModError("Unsupported tfvars syntax", f"{path}:{number}: minimod reads one NAME = VALUE per line, where VALUE is a JSON literal; got {stripped!r}") from None
    return values


def collect_root_inputs(definition: ModuleDef, directory: str, inputs: Sequence[Tuple[str, str]], environ: Dict[str, str]) -> Tuple[Dict[str, Tuple[Any, bool]], List[str]]:
    values: Dict[str, Tuple[Any, bool]] = {}
    warnings: List[str] = []
    for name in definition.variables:
        if f"TF_VAR_{name}" in environ:
            values[name] = (environ[f"TF_VAR_{name}"], True)

    def load_file(path: str) -> None:
        for name, value in read_tfvars(path).items():
            if name not in definition.variables:
                warnings.append(f'Value for undeclared variable: {os.path.basename(path)} sets "{name}", but the root module declares no variable of that name.')
                continue
            values[name] = (value, False)

    automatic = [name for name in ("terraform.tfvars", "terraform.tfvars.json") if os.path.isfile(os.path.join(directory, name))]
    automatic += sorted(os.path.basename(path) for path in glob.glob(os.path.join(directory, "*.auto.tfvars")) + glob.glob(os.path.join(directory, "*.auto.tfvars.json")))
    for name in automatic:
        load_file(os.path.join(directory, name))
    for kind, text in inputs:
        if kind == "file":
            load_file(text if os.path.isabs(text) else os.path.join(directory, text))
            continue
        name, separator, value = text.partition("=")
        if not separator or not name:
            raise MiniModError("Invalid -var option", f'The given -var option "{text}" is not correctly specified. It must be a variable name and value separated by an equals sign, like -var="key=value".')
        if name not in definition.variables:
            raise MiniModError("Value for undeclared variable", f'A variable named "{name}" was assigned on the command line, but the root module does not declare a variable of that name.')
        values[name] = (value, True)
    return values, warnings


def load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"serial": 0, "lineage": None, "written_by_tofu": False, "instances": {}}
    with open(path, encoding="utf-8") as handle:
        try:
            data = json.load(handle)
        except ValueError as error:
            raise MiniModError("Invalid state", f"{path}: {error}") from None
    if data.get("version") != 4:
        raise MiniModError("Unsupported state version", f"{path} has version {data.get('version')}; minimod reads version 4, the format current tofu and terraform write.")
    instances: Dict[str, Instance] = {}
    for resource in data.get("resources", []):
        if resource.get("mode", "managed") != "managed":
            continue
        modules = parse_address(resource["module"]).modules if resource.get("module") else ()
        for item in resource.get("instances", []):
            address = Address(modules, resource["type"], resource["name"], item.get("index_key"))
            instances[str(address)] = Instance(address, item.get("attributes") or {})
    return {"serial": data.get("serial", 0), "lineage": data.get("lineage"), "written_by_tofu": "terraform_version" in data, "instances": instances}


def move_address(address: Address, source: Address, target: Address) -> Optional[Address]:
    if source.rtype is not None:
        if address.modules != source.modules or (address.rtype, address.name) != (source.rtype, source.name):
            return None
        if source.key is None and target.key is None:
            return Address(target.modules, target.rtype, target.name, address.key)
        if address.key != source.key:
            return None
        return Address(target.modules, target.rtype, target.name, target.key)
    depth = len(source.modules)
    if len(address.modules) < depth or address.modules[: depth - 1] != source.modules[:-1]:
        return None
    name, key = address.modules[depth - 1]
    source_name, source_key = source.modules[-1]
    target_name, target_key = target.modules[-1]
    whole = source_key is None and target_key is None
    if name != source_name or (not whole and key != source_key):
        return None
    step = (target_name, key if whole else target_key)
    return Address(target.modules[:-1] + (step,) + address.modules[depth:], address.rtype, address.name, address.key)


def move_feeds(earlier: Tuple[Address, Address], later: Tuple[Address, Address]) -> bool:
    source, target = str(later[0]), str(earlier[1])
    return source == target or source.startswith(target + ".") or source.startswith(target + "[")


def order_moves(statements: Sequence[Tuple[Address, Address]]) -> List[Tuple[Address, Address]]:
    pending = list(statements)
    ordered_statements = []
    while pending:
        ready = [statement for statement in pending if not any(other is not statement and move_feeds(other, statement) for other in pending)]
        if not ready:
            raise MiniModError("Cyclic dependency in move statements", ", ".join(f"{source} -> {target}" for source, target in pending))
        ordered_statements.append(ready[0])
        pending.remove(ready[0])
    return ordered_statements


def apply_moves(prior: Dict[str, Instance], statements: Sequence[Tuple[Address, Address]]) -> Tuple[Dict[str, Instance], Dict[str, str], List[str]]:
    current = dict(prior)
    previous: Dict[str, str] = {}
    warnings: List[str] = []
    for source, target in order_moves(statements):
        renames = {}
        for text, instance in current.items():
            new_address = move_address(instance.address, source, target)
            if new_address is not None and str(new_address) != text:
                renames[text] = new_address
        moving = {}
        for text, new_address in renames.items():
            if str(new_address) in current and str(new_address) not in renames:
                warnings.append(f"Skipped moving {text} to {new_address}: an object already exists at the new address.")
                continue
            moving[text] = new_address
        lifted = {text: current.pop(text) for text in moving}
        for text, new_address in moving.items():
            current[str(new_address)] = Instance(new_address, lifted[text].attributes)
            previous[str(new_address)] = previous.pop(text, text)
    return current, previous, warnings


@dataclass
class Change:
    address: str
    action: str
    rtype: str
    previous_address: Optional[str] = None
    changed: List[str] = field(default_factory=list)


def plan_changes(desired: Sequence[Instance], prior: Dict[str, Instance], previous: Dict[str, str]) -> List[Change]:
    changes: List[Change] = []
    wanted = set()
    for instance in desired:
        text = str(instance.address)
        rtype = instance.address.rtype or ""
        wanted.add(text)
        before = prior.get(text)
        if before is None:
            changes.append(Change(text, "create", rtype))
            continue
        changed = sorted(key for key, value in instance.attributes.items() if contains_unknown(value) or not values_equal(to_json(value), before.attributes.get(key)))
        action = "noop" if not changed else "replace" if rtype in REPLACE_ON_ANY_CHANGE else "update"
        changes.append(Change(text, action, rtype, previous.get(text), changed))
    for text, instance in sorted(prior.items()):
        if text not in wanted:
            changes.append(Change(text, "delete", instance.address.rtype or "", previous.get(text)))
    return changes


def summary(changes: Sequence[Change]) -> Dict[str, int]:
    counts = {"add": 0, "change": 0, "destroy": 0, "move": 0}
    for change in changes:
        if change.action in ("create", "replace"):
            counts["add"] += 1
        if change.action in ("delete", "replace"):
            counts["destroy"] += 1
        if change.action == "update":
            counts["change"] += 1
        if change.previous_address:
            counts["move"] += 1
    return counts


def render_plan(changes: Sequence[Change], warnings: Sequence[str]) -> str:
    lines: List[str] = []
    for change in changes:
        if change.previous_address:
            lines.append(f"  # {change.previous_address} has moved to {change.address}")
        if change.action != "noop":
            reason = ""
            if change.changed:
                reason = f"  ({', '.join(change.changed)} changed{'; forces replacement' if change.action == 'replace' else ''})"
            lines.append(f"  {SYMBOLS[change.action]} {change.address}{reason}")
    if lines:
        counts = summary(changes)
        lines += ["", f"Plan: {counts['add']} to add, {counts['change']} to change, {counts['destroy']} to destroy."]
    else:
        lines.append("No changes. Your infrastructure matches the configuration.")
    lines += [f"\nWarning: {warning}" for warning in warnings]
    return "\n".join(lines)


def state_document(desired: Sequence[Instance], outputs: Dict[str, Tuple[Any, bool]], serial: int, lineage: str) -> Dict[str, Any]:
    groups: Dict[Tuple[ModulePath, Optional[str], Optional[str]], List[Instance]] = {}
    for instance in desired:
        address = instance.address
        groups.setdefault((address.modules, address.rtype, address.name), []).append(instance)
    resources = []
    for (modules, rtype, name), items in groups.items():
        entry: Dict[str, Any] = {"module": render_module_path(modules)} if modules else {}
        entry.update({"mode": "managed", "type": rtype, "name": name, "instances": []})
        for item in items:
            record: Dict[str, Any] = {} if item.address.key is None else {"index_key": item.address.key}
            record["attributes"] = to_json(item.attributes)
            entry["instances"].append(record)
        resources.append(entry)
    state_outputs = {name: {"value": to_json(value), **({"sensitive": True} if sensitive else {})} for name, (value, sensitive) in outputs.items()}
    return {"version": 4, "serial": serial, "lineage": lineage, "outputs": state_outputs, "resources": resources}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minimod", description=__doc__, allow_abbrev=False)
    parser.add_argument("command", choices=["addresses", "plan", "apply", "outputs", "eval"])
    parser.add_argument("expression", nargs="?", help="with eval: an expression such as 'module.web[\"blog\"].files'")
    parser.add_argument("--chdir", default=".", help="root module directory (like tofu -chdir)")
    parser.add_argument("-var", dest="inputs", action="append", type=lambda text: ("var", text), metavar="NAME=VALUE", help="set a root module variable (repeatable)")
    parser.add_argument("-var-file", dest="inputs", action="append", type=lambda text: ("file", text), metavar="FILE", help="load a .tfvars or .tfvars.json file (repeatable)")
    parser.add_argument("--state", default="minimod.tfstate", help="state file relative to --chdir; tofu's terraform.tfstate can be read too")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def execute(args: argparse.Namespace) -> int:
    directory = args.chdir
    definition = load_module(directory, directory)
    inputs, warnings = collect_root_inputs(definition, directory, args.inputs or [], dict(os.environ))
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    run = Run(definition, inputs)
    if args.command == "eval":
        node = parse_expression(args.expression)
        value, sensitive = run.tracked("console", lambda: evaluate(node, Scope(run.root)))
        if args.json:
            print(json.dumps({"value": None if sensitive else to_json(value), "sensitive": sensitive, "known": not contains_unknown(value)}, indent=2, ensure_ascii=False))
        else:
            print("(sensitive value)" if sensitive else display(value))
        return 0
    outputs = run.evaluate_all()
    desired = run.resources()
    if args.command == "addresses":
        if args.json:
            print(json.dumps([{"address": str(item.address), "module": render_module_path(item.address.modules), "type": item.address.rtype, "name": item.address.name, "index_key": item.address.key, "attributes": to_json(item.attributes)} for item in desired], indent=2, ensure_ascii=False))
        else:
            for item in desired:
                print(item.address)
        return 0
    if args.command == "outputs":
        if args.json:
            print(json.dumps({name: {"sensitive": sensitive, "value": to_json(value)} for name, (value, sensitive) in outputs.items()}, indent=2, ensure_ascii=False))
        else:
            for name, (value, sensitive) in outputs.items():
                print(f"{name} = {'<sensitive>' if sensitive else display(value)}")
        return 0
    state_path = os.path.join(directory, args.state)
    state = load_state(state_path)
    prior, previous, move_warnings = apply_moves(state["instances"], run.moves())
    changes = plan_changes(desired, prior, previous)
    if args.json:
        moves = [{"from": change.previous_address, "to": change.address} for change in changes if change.previous_address]
        print(json.dumps({"changes": [asdict(change) for change in changes], "moves": moves, "summary": summary(changes), "warnings": move_warnings}, indent=2, ensure_ascii=False))
    else:
        print(render_plan(changes, move_warnings))
    if args.command == "apply":
        if state["written_by_tofu"]:
            raise MiniModError("Refusing to overwrite state", f"{args.state} was written by tofu or terraform; minimod reads it but writes only its own state. Pass --state minimod.tfstate.")
        dirty = any(change.action != "noop" or change.previous_address for change in changes) or not os.path.exists(state_path)
        serial = state["serial"] + (1 if dirty else 0)
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump(state_document(desired, outputs, serial, state["lineage"] or str(uuid.uuid4())), handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        counts = summary(changes)
        print(f"\nApply complete! Resources: {counts['add']} added, {counts['change']} changed, {counts['destroy']} destroyed, {counts['move']} moved. State serial {serial} in {args.state} (minimod has no provider: only the state file changed).")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_intermixed_args(list(sys.argv[1:] if argv is None else argv))
    if args.command == "eval" and not args.expression:
        print("minimod: eval needs an expression, for example: minimod eval 'local.prefix'", file=sys.stderr)
        return 2
    try:
        return execute(args)
    except MiniModError as error:
        print(error.render(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
