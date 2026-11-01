"""minipulumi and minixp: two front ends over one desired-state engine. A Python program that registers resources whose outputs are lazy, and a Crossplane-style composition renderer."""
from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import random
import re
import runpy
import sys
import types
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple, Union

STACK_TYPE = "pulumi:pulumi:Stack"
SECRET_SIG_KEY = "4dabf18193072939515e22adb298388d"
SECRET_SIG_VALUE = "1b47061264138c4ac30d75fd1eb44270"
UNKNOWN_STRING_SENTINEL = "04da6b54-80e4-46f7-96ec-b56ff0331ba9"
OUTPUT_STR_MESSAGE = (
    "Calling __str__ on an Output[T] is not supported.\n\n"
    "To get the value of an Output[T] as an Output[str] use o.apply(lambda v: f\"prefix{v}suffix\")."
)
SYMBOLS = {"create": "+", "update": "~", "replace": "+-", "delete": "-", "same": " "}
PAST = {"create": "created", "update": "updated", "replace": "replaced", "delete": "deleted", "same": "unchanged"}
FUTURE = {"create": "to create", "update": "to update", "replace": "to replace", "delete": "to delete"}

COMPOSITION_RESOURCE_NAME = "crossplane.io/composition-resource-name"
COMPOSITE_LABEL = "crossplane.io/composite"
PT_INPUT_API = "pt.fn.crossplane.io/v1beta1"
TO_COMPOSED_PATCHES = ("FromCompositeFieldPath", "CombineFromComposite")


class UsageError(Exception):
    pass


class EngineError(Exception):
    pass


class CompositionError(Exception):
    pass


class FieldNotFound(Exception):
    pass


class Unknown:
    def __repr__(self) -> str:
        return "[unknown]"


UNKNOWN = Unknown()
_UNSET = object()


def contains_unknown(value: Any) -> bool:
    if value is UNKNOWN:
        return True
    if isinstance(value, dict):
        return any(contains_unknown(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_unknown(item) for item in value)
    return False


class Output:
    def __init__(self, thunk: Callable[[], Any], resources: Iterable[str] = (), secret: bool = False) -> None:
        self._thunk = thunk
        self._resources = frozenset(resources)
        self._secret = secret
        self._value: Any = _UNSET

    def resources(self) -> FrozenSet[str]:
        return self._resources

    def is_secret(self) -> bool:
        return self._secret

    def resolve(self) -> Any:
        if self._value is not _UNSET:
            return self._value
        value = self._thunk()
        if not contains_unknown(value):
            self._value = value
        return value

    def apply(self, fn: Callable[[Any], Any]) -> "Output":
        def thunk() -> Any:
            value = self.resolve()
            if contains_unknown(value):
                return UNKNOWN
            result = fn(value)
            if isinstance(result, Output):
                raise TypeError("apply() callback returned an Output; combine Outputs with Output.all() before apply()")
            return result

        return Output(thunk, self._resources, self._secret)

    @staticmethod
    def from_input(value: Any) -> "Output":
        if isinstance(value, Output):
            return value
        resources, secret = collect(value)
        return Output(lambda: resolve_value(value), resources, secret)

    @staticmethod
    def secret(value: Any) -> "Output":
        inner = Output.from_input(value)
        return Output(inner.resolve, inner.resources(), True)

    @staticmethod
    def all(*args: Any, **kwargs: Any) -> "Output":
        if args and kwargs:
            raise TypeError("Output.all() takes positional or keyword arguments, not both")
        if kwargs:
            named = {key: Output.from_input(value) for key, value in kwargs.items()}
            outputs = list(named.values())

            def thunk() -> Any:
                values = {key: output.resolve() for key, output in named.items()}
                return UNKNOWN if contains_unknown(values) else values
        else:
            outputs = [Output.from_input(value) for value in args]

            def thunk() -> Any:
                values = [output.resolve() for output in outputs]
                return UNKNOWN if contains_unknown(values) else values

        resources = frozenset().union(*(output.resources() for output in outputs))
        return Output(thunk, resources, any(output.is_secret() for output in outputs))

    @staticmethod
    def concat(*parts: Any) -> "Output":
        return Output.all(*parts).apply(lambda values: "".join(str(value) for value in values))

    def __str__(self) -> str:
        return OUTPUT_STR_MESSAGE

    def __repr__(self) -> str:
        return f"Output(resources={sorted(self._resources)}, secret={self._secret})"


def collect(value: Any) -> Tuple[FrozenSet[str], bool]:
    if isinstance(value, Output):
        return value.resources(), value.is_secret()
    resources: FrozenSet[str] = frozenset()
    secret = False
    children: Iterable[Any] = ()
    if isinstance(value, dict):
        children = value.values()
    elif isinstance(value, (list, tuple)):
        children = value
    for child in children:
        child_resources, child_secret = collect(child)
        resources = resources | child_resources
        secret = secret or child_secret
    return resources, secret


def resolve_value(value: Any) -> Any:
    if isinstance(value, Output):
        return value.resolve()
    if isinstance(value, dict):
        return {key: resolve_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [resolve_value(item) for item in value]
    return value


def to_jsonable(value: Any) -> Any:
    if value is UNKNOWN:
        return UNKNOWN_STRING_SENTINEL
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value


def seal(values: Dict[str, Any], secret_keys: Iterable[str]) -> Dict[str, Any]:
    sealed = to_jsonable(values)
    for key in secret_keys:
        if key in sealed:
            sealed[key] = {SECRET_SIG_KEY: SECRET_SIG_VALUE, "plaintext": json.dumps(sealed[key])}
    return sealed


def is_sealed(value: Any) -> bool:
    return isinstance(value, dict) and value.get(SECRET_SIG_KEY) == SECRET_SIG_VALUE


def reveal(values: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {key: json.loads(value["plaintext"]) if is_sealed(value) else value for key, value in (values or {}).items()}


@dataclass
class ResourceOptions:
    depends_on: Optional[Sequence["Resource"]] = None
    protect: bool = False
    delete_before_replace: bool = False


@dataclass
class Step:
    op: str
    urn: str
    type: str
    name: str
    diffs: List[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "op": self.op,
            "urn": self.urn,
            "type": self.type,
            "name": self.name,
            "diffReasons": self.diffs,
            "replaceReasons": self.diffs if self.op == "replace" else [],
        }


_ACTIVE: List["Deployment"] = []


def active_deployment() -> "Deployment":
    if not _ACTIVE:
        raise EngineError("no minipulumi program is running; resources exist only inside a program run by preview or up")
    return _ACTIVE[-1]


def export(name: str, value: Any) -> None:
    active_deployment().exports[name] = value


def get_stack() -> str:
    return active_deployment().stack


def get_project() -> str:
    return active_deployment().project


class Config:
    def __init__(self, name: Optional[str] = None) -> None:
        deployment = active_deployment()
        self.name = name or deployment.project
        self._values = deployment.config

    def full_key(self, key: str) -> str:
        return key if ":" in key else f"{self.name}:{key}"

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self._values.get(self.full_key(key), default)

    def get_int(self, key: str, default: Optional[int] = None) -> Optional[int]:
        raw = self.get(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            raise EngineError(f"configuration '{self.full_key(key)}' is not an integer: {raw!r}") from None

    def get_bool(self, key: str, default: Optional[bool] = None) -> Optional[bool]:
        raw = self.get(key)
        if raw is None:
            return default
        if raw.lower() not in ("true", "false"):
            raise EngineError(f"configuration '{self.full_key(key)}' is not a boolean: {raw!r}")
        return raw.lower() == "true"

    def require(self, key: str) -> str:
        value = self.get(key)
        if value is None:
            raise EngineError(f"Missing required configuration variable '{self.full_key(key)}'; set it with: pulumi config set {key} <value>")
        return value

    def get_secret(self, key: str) -> Optional[Output]:
        value = self.get(key)
        return None if value is None else Output.secret(value)


ADVERBS = ["briefly", "gently", "quickly", "really", "solely", "truly", "vastly", "wholly"]
ADJECTIVES = ["able", "brave", "calm", "eager", "fond", "keen", "lucky", "noble", "proud", "quiet", "steady", "witty"]
NAMES = ["badger", "crane", "falcon", "gecko", "heron", "koala", "lynx", "marmot", "otter", "panda", "quail", "walrus"]
SPECIAL_CHARACTERS = "!@#$%&*()-_=+[]{}<>:?"


class RandomProvider:
    PET = "random:index/randomPet:RandomPet"
    STRING = "random:index/randomString:RandomString"
    PASSWORD = "random:index/randomPassword:RandomPassword"
    INTEGER = "random:index/randomInteger:RandomInteger"
    CHARSET_DEFAULTS = {"lower": True, "upper": True, "numeric": True, "special": True}
    DEFAULTS: Dict[str, Dict[str, Any]] = {PET: {"length": 2, "separator": "-"}, STRING: CHARSET_DEFAULTS, PASSWORD: CHARSET_DEFAULTS, INTEGER: {}}
    REQUIRED: Dict[str, Tuple[str, ...]] = {PET: (), STRING: ("length",), PASSWORD: ("length",), INTEGER: ("min", "max")}
    SECRET_OUTPUTS: Dict[str, Tuple[str, ...]] = {PASSWORD: ("result",)}

    def __init__(self, rng: Optional[random.Random] = None) -> None:
        self.rng = rng or random.SystemRandom()

    def check(self, rtype: str, raw: Dict[str, Any]) -> Dict[str, Any]:
        if rtype not in self.DEFAULTS:
            raise EngineError(f"the random provider has no resource type {rtype}")
        inputs = {**self.DEFAULTS[rtype], **{key: value for key, value in raw.items() if value is not None}}
        missing = [key for key in self.REQUIRED[rtype] if key not in inputs]
        if missing:
            raise EngineError(f"{rtype}: missing required input {', '.join(missing)}")
        if rtype == self.INTEGER and not contains_unknown([inputs["min"], inputs["max"]]) and inputs["min"] > inputs["max"]:
            raise EngineError(f"{rtype}: min ({inputs['min']}) must not be greater than max ({inputs['max']})")
        return inputs

    def forces_replacement(self, rtype: str, key: str) -> bool:
        return True

    def secret_outputs(self, rtype: str) -> Tuple[str, ...]:
        return self.SECRET_OUTPUTS.get(rtype, ())

    def preview_outputs(self, rtype: str, inputs: Dict[str, Any]) -> Dict[str, Any]:
        return dict(inputs)

    def create(self, rtype: str, inputs: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        if rtype == self.PET:
            return self.pet(inputs), dict(inputs)
        if rtype in (self.STRING, self.PASSWORD):
            value = self.string(inputs)
            return (value if rtype == self.STRING else "none"), {**inputs, "result": value}
        rng = random.Random(inputs["seed"]) if "seed" in inputs else self.rng
        value = rng.randint(int(inputs["min"]), int(inputs["max"]))
        return str(value), {**inputs, "result": value}

    def update(self, rtype: str, rid: str, inputs: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        raise EngineError(f"{rtype} cannot be updated in place; every input forces replacement")

    def delete(self, rtype: str, rid: str, outputs: Dict[str, Any]) -> None:
        return None

    def pet(self, inputs: Dict[str, Any]) -> str:
        length = int(inputs["length"])
        words = [self.rng.choice(NAMES)]
        if length >= 2:
            words.insert(0, self.rng.choice(ADJECTIVES))
        for _ in range(length - 2):
            words.insert(0, self.rng.choice(ADVERBS))
        if inputs.get("prefix"):
            words.insert(0, str(inputs["prefix"]))
        return str(inputs["separator"]).join(words)

    def string(self, inputs: Dict[str, Any]) -> str:
        pool = ""
        if inputs["lower"]:
            pool += "abcdefghijklmnopqrstuvwxyz"
        if inputs["upper"]:
            pool += "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        if inputs["numeric"]:
            pool += "0123456789"
        if inputs["special"]:
            pool += inputs.get("overrideSpecial") or SPECIAL_CHARACTERS
        if not pool:
            raise EngineError("random string: lower, upper, numeric and special are all false; nothing to draw from")
        return "".join(self.rng.choice(pool) for _ in range(int(inputs["length"])))


class Deployment:
    def __init__(self, project: str, stack: str, config: Dict[str, str], state: dict, providers: Dict[str, Any], mode: str) -> None:
        self.project = project
        self.stack = stack
        self.config = config
        self.providers = providers
        self.mode = mode
        self.preview = mode == "preview"
        prior = state_resources(state)
        self.prior: Dict[str, dict] = {entry["urn"]: entry for entry in prior}
        self.prior_order = [entry["urn"] for entry in prior]
        self.entries: Dict[str, dict] = {}
        self.values: Dict[str, Dict[str, Any]] = {}
        self.exports: Dict[str, Any] = {}
        self.export_values: Dict[str, Any] = {}
        self.secret_exports: List[str] = []
        self.steps: List[Step] = []
        self.events: List[str] = []
        self.pending_deletes: List[dict] = []
        self.deleted: List[str] = []
        self.stack_urn = self.urn_for(STACK_TYPE, f"{project}-{stack}")
        if mode != "destroy":
            self.register_stack()

    def urn_for(self, rtype: str, name: str) -> str:
        return f"urn:pulumi:{self.stack}::{self.project}::{rtype}::{name}"

    def register_stack(self) -> None:
        op = "same" if self.stack_urn in self.prior else "create"
        self.steps.append(Step(op, self.stack_urn, STACK_TYPE, f"{self.project}-{self.stack}"))
        self.entries[self.stack_urn] = {"urn": self.stack_urn, "custom": False, "type": STACK_TYPE, "outputs": {}}
        if op == "create" and not self.preview:
            self.events.append(f"created {self.project}-{self.stack}")

    def provider_for(self, rtype: str) -> Any:
        provider = self.providers.get(rtype.split(":")[0])
        if provider is None:
            raise EngineError(f"no provider plugin for {rtype}")
        return provider

    def in_registration_order(self, urns: Iterable[str]) -> List[str]:
        wanted = set(urns)
        return [urn for urn in self.entries if urn in wanted]

    def register(self, rtype: str, name: str, props: Dict[str, Any], opts: ResourceOptions) -> Tuple[str, FrozenSet[str]]:
        urn = self.urn_for(rtype, name)
        if urn in self.entries:
            raise EngineError(f"Duplicate resource URN '{urn}'; give each resource a unique name")
        provider = self.provider_for(rtype)
        dependencies: set = set()
        property_dependencies: Dict[str, List[str]] = {}
        secret_inputs = set()
        for key, value in props.items():
            resources, secret = collect(value)
            if resources:
                property_dependencies[key] = self.in_registration_order(resources)
            dependencies |= resources
            if secret:
                secret_inputs.add(key)
        dependencies |= {dependency._urn for dependency in opts.depends_on or []}
        inputs = provider.check(rtype, {key: resolve_value(value) for key, value in props.items()})
        secret_outputs = frozenset(provider.secret_outputs(rtype)) | frozenset(secret_inputs)

        prior = self.prior.get(urn)
        op, diffs = "create", []
        if prior is not None:
            old = reveal(prior.get("inputs"))
            diffs = sorted(key for key in set(old) | set(inputs) if contains_unknown(inputs.get(key)) or old.get(key) != inputs.get(key))
            if not diffs:
                op = "same"
            elif any(provider.forces_replacement(rtype, key) for key in diffs):
                op = "replace"
            else:
                op = "update"
            if op == "replace" and prior.get("protect"):
                raise EngineError(f"unable to replace '{urn}': it is protected; set protect=False first (pulumi: pulumi state unprotect '{urn}')")
        self.steps.append(Step(op, urn, rtype, name, diffs))

        if op == "same":
            rid, outputs = prior["id"], reveal(prior.get("outputs"))
        elif self.preview:
            rid, outputs = UNKNOWN, provider.preview_outputs(rtype, inputs)
        elif op == "update":
            rid, outputs = provider.update(rtype, prior["id"], inputs)
            self.events.append(f"updated {name}")
        else:
            if op == "replace" and opts.delete_before_replace:
                self.delete_entry(prior, "deleted original")
            rid, outputs = provider.create(rtype, inputs)
            self.events.append(f"created {name}" if op == "create" else f"created replacement {name}")
            if op == "replace" and not opts.delete_before_replace:
                self.pending_deletes.append(prior)

        self.values[urn] = {**outputs, "id": rid}
        self.entries[urn] = {
            "urn": urn,
            "custom": True,
            "id": to_jsonable(rid),
            "type": rtype,
            "inputs": seal(inputs, secret_inputs),
            "outputs": seal(outputs, secret_outputs),
            "parent": self.stack_urn,
            "dependencies": self.in_registration_order(dependencies),
            "propertyDependencies": property_dependencies,
            "protect": opts.protect,
        }
        return urn, secret_outputs

    def output_of(self, urn: str, key: str, secret: bool) -> Output:
        return Output(lambda: self.values.get(urn, {}).get(key, UNKNOWN), {urn}, secret)

    def delete_entry(self, entry: dict, verb: str) -> None:
        if entry.get("custom"):
            self.provider_for(entry["type"]).delete(entry["type"], entry.get("id"), reveal(entry.get("outputs")))
        self.deleted.append(entry["urn"])
        self.events.append(f"{verb} {name_of(entry['urn'])}")

    def finish(self) -> None:
        if self.mode != "destroy":
            for key, value in self.exports.items():
                self.export_values[key] = resolve_value(value)
                if collect(value)[1]:
                    self.secret_exports.append(key)
            self.entries[self.stack_urn]["outputs"] = seal(self.export_values, self.secret_exports)
        removed = [self.prior[urn] for urn in reversed(self.prior_order) if urn not in self.entries]
        protected = [entry["urn"] for entry in removed if entry.get("protect")]
        if protected:
            raise EngineError(f"cannot delete protected resource(s) {', '.join(protected)}; set protect=False and run up first")
        if not self.preview:
            for entry in reversed(self.pending_deletes):
                self.delete_entry(entry, "deleted original")
        for entry in removed:
            self.steps.append(Step("delete", entry["urn"], entry["type"], name_of(entry["urn"])))
            if not self.preview:
                self.delete_entry(entry, "deleted")

    def counts(self) -> Dict[str, int]:
        return dict(Counter(step.op for step in self.steps))

    def checkpoint(self) -> dict:
        remaining = [self.prior[urn] for urn in self.prior_order if urn not in self.entries and urn not in self.deleted]
        return {"version": 3, "deployment": {"manifest": {"magic": "minipulumi", "version": "1"}, "resources": remaining + list(self.entries.values())}}

    def masked_outputs(self, show_secrets: bool = False) -> Dict[str, Any]:
        return {key: (value if show_secrets or key not in self.secret_exports else "[secret]") for key, value in self.export_values.items()}


def name_of(urn: str) -> str:
    return urn.rsplit("::", 1)[-1]


def empty_state() -> dict:
    return {"version": 3, "deployment": {"resources": []}}


def state_resources(state: dict) -> List[dict]:
    return list((state.get("deployment") or {}).get("resources") or [])


class Resource:
    def __init__(self, type_token: str, name: str, props: Dict[str, Any], opts: Optional[ResourceOptions] = None, output_names: Sequence[str] = ()) -> None:
        deployment = active_deployment()
        self._urn, secret_outputs = deployment.register(type_token, name, props, opts or ResourceOptions())
        self.urn = Output(lambda: self._urn, {self._urn})
        self.id = deployment.output_of(self._urn, "id", False)
        for key in output_names:
            setattr(self, key, deployment.output_of(self._urn, key, key in secret_outputs))


class RandomPet(Resource):
    def __init__(self, resource_name: str, opts: Optional[ResourceOptions] = None, keepers: Any = None, length: Any = None, prefix: Any = None, separator: Any = None) -> None:
        super().__init__(RandomProvider.PET, resource_name, {"keepers": keepers, "length": length, "prefix": prefix, "separator": separator}, opts)


class RandomString(Resource):
    TYPE = RandomProvider.STRING

    def __init__(self, resource_name: str, opts: Optional[ResourceOptions] = None, length: Any = None, keepers: Any = None, lower: Any = None, numeric: Any = None, override_special: Any = None, special: Any = None, upper: Any = None) -> None:
        props = {"length": length, "keepers": keepers, "lower": lower, "numeric": numeric, "overrideSpecial": override_special, "special": special, "upper": upper}
        super().__init__(self.TYPE, resource_name, props, opts, ("result",))


class RandomPassword(RandomString):
    TYPE = RandomProvider.PASSWORD


class RandomInteger(Resource):
    def __init__(self, resource_name: str, opts: Optional[ResourceOptions] = None, max: Any = None, min: Any = None, keepers: Any = None, seed: Any = None) -> None:
        super().__init__(RandomProvider.INTEGER, resource_name, {"keepers": keepers, "max": max, "min": min, "seed": seed}, opts, ("result",))


def build_shims() -> Dict[str, types.ModuleType]:
    sdk = types.ModuleType("pulumi")
    for name, value in {"Output": Output, "ResourceOptions": ResourceOptions, "Config": Config, "export": export, "get_stack": get_stack, "get_project": get_project, "Input": Any}.items():
        setattr(sdk, name, value)
    provider = types.ModuleType("pulumi_random")
    for cls in (RandomPet, RandomString, RandomPassword, RandomInteger):
        setattr(provider, cls.__name__, cls)
    return {"pulumi": sdk, "pulumi_random": provider}


def run_program(program: Union[str, Callable[[], None]], deployment: Deployment) -> Deployment:
    shims = build_shims()
    saved = {name: sys.modules.get(name) for name in shims}
    sys.modules.update(shims)
    _ACTIVE.append(deployment)
    try:
        if callable(program):
            program()
        else:
            runpy.run_path(program, run_name="__main__")
    except (EngineError, UsageError):
        raise
    except Exception as exc:
        raise EngineError(f"the program raised {type(exc).__name__}: {exc}") from exc
    finally:
        _ACTIVE.pop()
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    deployment.finish()
    return deployment


CONFIG_LINE = re.compile(r"^(\s+)(\S+):(?:\s+(.*))?$")


def unquote(text: str) -> str:
    text = text.strip()
    if text.startswith('"'):
        return json.loads(text)
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("''", "'")
    return text


def parse_stack_config(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    in_config = False
    indent: Optional[int] = None
    open_key: Optional[str] = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw[0].isspace():
            in_config = raw.rstrip() == "config:"
            continue
        if not in_config:
            continue
        match = CONFIG_LINE.match(raw)
        if not match:
            raise UsageError(f"cannot read config line: {raw.strip()!r}")
        width = len(match.group(1))
        if indent is None:
            indent = width
        if width > indent:
            raise UsageError(f"config '{open_key}' is a secure or structured value; minipulumi reads plain scalars only")
        key, value = match.group(2), match.group(3)
        open_key = key
        if value is not None:
            values[key] = unquote(value)
    return values


@dataclass
class Project:
    root: str
    name: str
    stack: str
    config: Dict[str, str]
    program: str
    state_path: str


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise UsageError(f"cannot read {path}: {exc.strerror}") from None


def load_project(cwd: str, stack: str, state_path: Optional[str] = None) -> Project:
    project_file = os.path.join(cwd, "Pulumi.yaml")
    if not os.path.isfile(project_file):
        raise UsageError(f"no Pulumi.yaml in {cwd}; run inside a project directory or pass --cwd")
    text = read_text(project_file)
    name = re.search(r"^name:\s*[\"']?([A-Za-z0-9_.-]+)", text, re.M)
    if not name:
        raise UsageError(f"{project_file} has no top-level name:")
    program = os.path.join(cwd, "__main__.py")
    main_entry = re.search(r"^main:\s*(\S+)", text, re.M)
    if main_entry:
        program = os.path.join(cwd, main_entry.group(1))
        if os.path.isdir(program):
            program = os.path.join(program, "__main__.py")
    if not os.path.isfile(program):
        raise UsageError(f"no program at {program}")
    config_file = os.path.join(cwd, f"Pulumi.{stack}.yaml")
    config = parse_stack_config(read_text(config_file)) if os.path.isfile(config_file) else {}
    return Project(cwd, name.group(1), stack, config, program, state_path or os.path.join(cwd, ".minipulumi", f"{stack}.json"))


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return empty_state()
    try:
        return json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise UsageError(f"{path} is not valid JSON: {exc}") from None


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")


def show_value(value: Any) -> str:
    if contains_unknown(value):
        return "[unknown]"
    return json.dumps(value)


def summary_lines(counts: Dict[str, int], applied: bool) -> List[str]:
    lines = []
    for op in ("create", "update", "replace", "delete"):
        if counts.get(op):
            lines.append(f"    {SYMBOLS[op]} {counts[op]} {PAST[op] if applied else FUTURE[op]}")
    if counts.get("same"):
        lines.append(f"    {counts['same']} unchanged")
    return lines or ["    no changes"]


def render_deployment(deployment: Deployment) -> str:
    applied = not deployment.preview
    title = {"preview": "Previewing update", "up": "Updating", "destroy": "Destroying"}[deployment.mode]
    lines = [f"{title} ({deployment.stack}):"]
    changed = [step for step in deployment.steps if step.op != "same"]
    type_width = max([len(step.type) for step in changed] or [0])
    name_width = max([len(step.name) for step in changed] or [0])
    for step in changed:
        diff = f"  [diff: {', '.join('~' + key for key in step.diffs)}]" if step.diffs else ""
        lines.append(f"  {SYMBOLS[step.op]:<2} {step.type:<{type_width}}  {step.name:<{name_width}}  {PAST[step.op] if applied else step.op}{diff}")
    if deployment.export_values:
        lines.append("Outputs:")
        for key, value in deployment.masked_outputs().items():
            lines.append(f"    {key}: {value if value == '[secret]' else show_value(value)}")
    lines.append("Resources:")
    lines.extend(summary_lines(deployment.counts(), applied))
    return "\n".join(lines)


def deployment_json(deployment: Deployment) -> dict:
    steps = [step.to_json() for step in deployment.steps if step.op != "same" or step.type == STACK_TYPE]
    return {"steps": steps, "changeSummary": deployment.counts(), "outputs": to_jsonable(deployment.masked_outputs()), "events": deployment.events}


def stack_outputs(state: dict, show_secrets: bool) -> Dict[str, Any]:
    root = next((entry for entry in state_resources(state) if entry["type"] == STACK_TYPE), None)
    if root is None:
        return {}
    return {key: (json.loads(value["plaintext"]) if show_secrets else "[secret]") if is_sealed(value) else value for key, value in (root.get("outputs") or {}).items()}


def graph_of(state: dict) -> Dict[str, Any]:
    resources = [entry for entry in state_resources(state) if entry.get("custom")]
    nodes = [{"name": name_of(entry["urn"]), "type": entry["type"]} for entry in resources]
    edges = [[name_of(entry["urn"]), name_of(dependency)] for entry in resources for dependency in entry.get("dependencies") or []]
    return {"nodes": nodes, "edges": edges}


def render_dot(graph: Dict[str, Any]) -> str:
    lines = ["strict digraph {"]
    for node in graph["nodes"]:
        lines.append(f'  "{node["name"]}" [label="{node["type"]}::{node["name"]}"];')
    for source, target in graph["edges"]:
        lines.append(f'  "{source}" -> "{target}";')
    lines.append("}")
    return "\n".join(lines)


def pulumi_command(args: argparse.Namespace) -> int:
    project = load_project(args.cwd, args.stack, args.state)
    state = load_state(project.state_path)
    if args.command == "output":
        outputs = stack_outputs(state, args.show_secrets)
        if args.name:
            if args.name not in outputs:
                raise UsageError(f"current stack does not have output property '{args.name}'")
            value = outputs[args.name]
            print(json.dumps(value) if args.json or not isinstance(value, str) else value)
        else:
            print(json.dumps(outputs, indent=2) if args.json else "\n".join(f"{key}  {value}" for key, value in outputs.items()))
        return 0
    if args.command == "export":
        print(json.dumps(state, indent=2))
        return 0
    if args.command == "graph":
        graph = graph_of(state)
        print(json.dumps(graph, indent=2) if args.json else render_dot(graph))
        return 0
    rng = random.Random(args.seed) if args.seed is not None else None
    deployment = Deployment(project.name, project.stack, project.config, state, {"random": RandomProvider(rng)}, args.command)
    try:
        if args.command == "destroy":
            deployment.finish()
        else:
            run_program(project.program, deployment)
    finally:
        if args.command != "preview":
            save_state(project.state_path, deployment.checkpoint())
    print(json.dumps(deployment_json(deployment), indent=2) if args.json else render_deployment(deployment))
    return 0


def parse_field_path(path: str) -> List[Union[str, int]]:
    segments: List[Union[str, int]] = []
    buffer = ""
    index = 0
    previous = ""
    while index < len(path):
        char = path[index]
        if char == ".":
            if buffer:
                segments.append(buffer)
                buffer = ""
            elif previous != "]":
                raise CompositionError(f"invalid field path {path!r}: empty segment")
            index += 1
        elif char == "[":
            if buffer:
                segments.append(buffer)
                buffer = ""
            close = path.find("]", index)
            if close == -1:
                raise CompositionError(f"invalid field path {path!r}: unclosed [")
            inner = path[index + 1:close].strip("'\"")
            segments.append(int(inner) if inner.isdigit() else inner)
            index = close + 1
            char = "]"
        else:
            buffer += char
            index += 1
        previous = char
    if buffer:
        segments.append(buffer)
    if not segments:
        raise CompositionError("empty field path")
    return segments


def get_value(obj: Any, path: str) -> Any:
    current = obj
    for segment in parse_field_path(path):
        if isinstance(segment, int):
            if not isinstance(current, list) or segment >= len(current):
                raise FieldNotFound(path)
        elif not isinstance(current, dict) or segment not in current:
            raise FieldNotFound(path)
        current = current[segment]
    return current


def set_value(obj: dict, path: str, value: Any) -> None:
    segments = parse_field_path(path)
    current: Any = obj
    for position, segment in enumerate(segments):
        last = position == len(segments) - 1
        empty: Any = None if last else ([] if isinstance(segments[position + 1], int) else {})
        if isinstance(segment, int):
            if not isinstance(current, list):
                raise CompositionError(f"cannot set {path}: [{segment}] indexes something that is not a list")
            while len(current) <= segment:
                current.append(None)
        elif not isinstance(current, dict):
            raise CompositionError(f"cannot set {path}: {segment!r} is inside something that is not an object")
        if last:
            current[segment] = copy.deepcopy(value)
        else:
            if (current[segment] if isinstance(segment, int) else current.get(segment)) is None:
                current[segment] = empty
            current = current[segment]


GO_VERB = re.compile(r"%(%|[-+ 0]*\d*(?:\.\d+)?[svdfq])")


def go_str(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "<nil>"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def go_format(fmt: str, args: Sequence[Any]) -> str:
    values = list(args)
    used = [0]

    def substitute(match: "re.Match[str]") -> str:
        spec = match.group(1)
        if spec == "%":
            return "%"
        verb, flags = spec[-1], spec[:-1]
        if used[0] >= len(values):
            return f"%!{verb}(MISSING)"
        value = values[used[0]]
        used[0] += 1
        if verb == "d":
            return ("%" + flags + "d") % int(value)
        if verb == "f":
            return ("%" + flags + "f") % float(value)
        if verb == "q":
            return json.dumps(go_str(value))
        return ("%" + flags + "s") % go_str(value)

    return GO_VERB.sub(substitute, fmt)


def type_name(value: Any) -> str:
    return {bool: "bool", int: "int64", float: "float64", str: "string", list: "array", dict: "object"}.get(type(value), type(value).__name__)


def string_transform(config: dict, value: Any) -> Any:
    kind = config.get("type", "Format")
    text = value if isinstance(value, str) else go_str(value)
    if kind == "Format":
        if config.get("fmt") is None:
            raise CompositionError("string transform of type Format fmt is not set")
        return go_format(config["fmt"], [value])
    if kind == "Convert":
        conversions = {
            "ToUpper": str.upper,
            "ToLower": str.lower,
            "ToBase64": lambda s: base64.b64encode(s.encode()).decode(),
            "FromBase64": lambda s: base64.b64decode(s.encode()).decode(),
            "ToJson": lambda s: json.dumps(value, separators=(",", ":")),
        }
        if config.get("convert") not in conversions:
            raise CompositionError(f"type {config.get('convert')} is not supported for string convert")
        return conversions[config["convert"]](text)
    if kind in ("TrimPrefix", "TrimSuffix"):
        if config.get("trim") is None:
            raise CompositionError(f"string transform of type {kind} trim is not set")
        return text.removeprefix(config["trim"]) if kind == "TrimPrefix" else text.removesuffix(config["trim"])
    if kind == "Regexp":
        rule = config.get("regexp") or {}
        group = int(rule.get("group", 0))
        found = re.search(rule.get("match", ""), text)
        if not found or group > (found.re.groups or 0):
            raise CompositionError(f"regexp {rule.get('match')!r} had no matches for group {group}")
        return found.group(group)
    if kind == "Replace":
        rule = config.get("replace") or {}
        return text.replace(rule.get("search", ""), rule.get("replace", ""))
    if kind == "Join":
        if not isinstance(value, list):
            raise CompositionError("could not parse input array")
        return str((config.get("join") or {}).get("separator", "")).join(go_str(item) for item in value)
    raise CompositionError(f"type {kind} is not supported for string transform type")


def number(value: Any) -> Union[int, float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CompositionError(f"input is required to be a number for math transformer, got {type_name(value)}")
    return value


def math_transform(config: dict, value: Any) -> Any:
    kind = config.get("type", "Multiply")
    value = number(value)
    if kind == "Multiply":
        return value * int(config.get("multiply", 1))
    if kind == "ClampMin":
        return max(value, int(config["clampMin"]))
    if kind == "ClampMax":
        return min(value, int(config["clampMax"]))
    raise CompositionError(f"type {kind} is not supported for math transform type")


def convert_transform(config: dict, value: Any) -> Any:
    target = config.get("toType")
    try:
        if target == "string":
            return go_str(value)
        if target in ("int", "int64"):
            return int(value)
        if target == "float64":
            return float(value)
        if target == "bool":
            if isinstance(value, bool):
                return value
            if str(value).lower() in ("true", "false"):
                return str(value).lower() == "true"
            raise ValueError(value)
    except (TypeError, ValueError):
        raise CompositionError(f"cannot convert {value!r} to {target}") from None
    raise CompositionError(f"conversion to {target} is not supported")


def run_transform(transform: dict, value: Any) -> Any:
    kind = transform.get("type")
    if kind == "map":
        table = transform.get("map")
        if not isinstance(table, dict):
            raise CompositionError("given transform type map requires configuration")
        if not isinstance(value, str):
            raise CompositionError(f"type {type_name(value)} is not supported for map transform")
        if value not in table:
            raise CompositionError(f"key {value} is not found in map")
        return copy.deepcopy(table[value])
    if kind == "string":
        return string_transform(transform.get("string") or {}, value)
    if kind == "math":
        return math_transform(transform.get("math") or {}, value)
    if kind == "convert":
        return convert_transform(transform.get("convert") or {}, value)
    raise CompositionError(f"transform type {kind} is not supported by minixp")


def run_transforms(transforms: Optional[List[dict]], value: Any) -> Any:
    for index, transform in enumerate(transforms or []):
        try:
            value = run_transform(transform, value)
        except CompositionError as exc:
            raise CompositionError(f"transform at index {index} returned error: {exc}") from None
    return value


def apply_patch(patch: dict, xr: dict, desired_xr: dict, composed: dict, observed: Optional[dict]) -> None:
    kind = patch.get("type", "FromCompositeFieldPath")
    source = patch.get("fromFieldPath")
    if kind in ("FromCompositeFieldPath", "ToCompositeFieldPath") and not source:
        raise CompositionError(f"fromFieldPath is required by type {kind}")
    if kind == "FromCompositeFieldPath":
        value = run_transforms(patch.get("transforms"), get_value(xr, source))
        set_value(composed, patch.get("toFieldPath") or source, value)
    elif kind == "ToCompositeFieldPath":
        if observed is None:
            raise FieldNotFound(source)
        value = run_transforms(patch.get("transforms"), get_value(observed, source))
        set_value(desired_xr, patch.get("toFieldPath") or source, value)
    elif kind == "CombineFromComposite":
        combine = patch.get("combine") or {}
        if combine.get("strategy") != "string":
            raise CompositionError(f"combine strategy {combine.get('strategy')} is not supported")
        if not patch.get("toFieldPath"):
            raise CompositionError("toFieldPath is required by type CombineFromComposite")
        values = [get_value(xr, variable["fromFieldPath"]) for variable in combine.get("variables") or []]
        value = run_transforms(patch.get("transforms"), go_format((combine.get("string") or {}).get("fmt", ""), values))
        set_value(composed, patch["toFieldPath"], value)
    else:
        raise CompositionError(f"patch type {kind} is not supported by minixp")


def expand_patch_sets(patch_sets: Optional[List[dict]], templates: List[dict]) -> List[dict]:
    by_name: Dict[str, List[dict]] = {}
    for patch_set in patch_sets or []:
        if any(patch.get("type") == "PatchSet" for patch in patch_set.get("patches") or []):
            raise CompositionError("a patch in a PatchSet cannot be of type PatchSet")
        by_name[patch_set["name"]] = patch_set.get("patches") or []
    expanded = []
    for template in templates:
        patches: List[dict] = []
        for patch in template.get("patches") or []:
            if patch.get("type") != "PatchSet":
                patches.append(patch)
                continue
            name = patch.get("patchSetName")
            if name not in by_name:
                raise CompositionError(f"cannot find PatchSet by name {name}")
            patches.extend(copy.deepcopy(by_name[name]))
        expanded.append({**template, "patches": patches})
    return expanded


def get_condition(obj: dict, ctype: str) -> dict:
    for condition in (obj.get("status") or {}).get("conditions") or []:
        if condition.get("type") == ctype:
            return condition
    return {"type": ctype, "status": "Unknown"}


def run_readiness_check(check: dict, obj: dict) -> bool:
    kind = check.get("type")
    if kind == "None":
        return True
    if kind == "MatchCondition":
        wanted = check.get("matchCondition") or {}
        return get_condition(obj, wanted.get("type", "Ready")).get("status") == wanted.get("status", "True")
    if kind not in ("NonEmpty", "MatchString", "MatchInteger", "MatchTrue", "MatchFalse"):
        raise CompositionError(f"readiness check type {kind} is not supported")
    if not check.get("fieldPath"):
        raise CompositionError(f"readiness check {kind} needs fieldPath")
    try:
        value = get_value(obj, check["fieldPath"])
    except FieldNotFound:
        return False
    if kind == "NonEmpty":
        return True
    if kind == "MatchString":
        return isinstance(value, str) and value == check.get("matchString")
    if kind == "MatchInteger":
        return isinstance(value, int) and not isinstance(value, bool) and value == check.get("matchInteger")
    if kind == "MatchTrue":
        return value is True
    return value is False


def is_ready(observed: dict, checks: List[dict]) -> bool:
    if not checks:
        return get_condition(observed, "Ready").get("status") == "True"
    return all(run_readiness_check(check, observed) for check in checks)


@dataclass
class Rendered:
    composite: dict
    composed: List[dict]
    ready: Dict[str, bool]
    warnings: List[str]
    skipped: List[str]


def validate_composition(xr: dict, composition: dict) -> dict:
    spec = composition.get("spec") or {}
    ref = spec.get("compositeTypeRef") or {}
    if ref.get("kind") != xr.get("kind"):
        raise CompositionError(f"composition's compositeTypeRef.kind ({ref.get('kind')}) does not match XR's kind ({xr.get('kind')})")
    if ref.get("apiVersion") != xr.get("apiVersion"):
        raise CompositionError(f"composition's compositeTypeRef.apiVersion ({ref.get('apiVersion')}) does not match XR's apiVersion ({xr.get('apiVersion')})")
    mode = spec.get("mode", "Pipeline")
    if mode == "Resources":
        raise CompositionError("spec.mode: Resources (native patch and transform) was removed in Crossplane v2; move the resources into a function-patch-and-transform pipeline step")
    if mode != "Pipeline":
        raise CompositionError(f"unknown composition mode {mode!r}; Crossplane v2 renders only mode: Pipeline")
    return spec


def composite_uid(xr: dict) -> str:
    meta = xr.get("metadata") or {}
    return meta.get("uid") or str(uuid.uuid5(uuid.NAMESPACE_URL, f"{xr.get('apiVersion')}/{xr.get('kind')}/{meta.get('namespace', '')}/{meta.get('name', '')}"))


def stamp_metadata(composed: dict, xr: dict, name: str, observed: Optional[dict]) -> None:
    xr_meta = xr.get("metadata") or {}
    meta = composed.setdefault("metadata", {})
    meta.setdefault("annotations", {})[COMPOSITION_RESOURCE_NAME] = name
    meta.setdefault("labels", {})[COMPOSITE_LABEL] = xr_meta.get("name", "")
    if observed is not None and (observed.get("metadata") or {}).get("name"):
        meta["name"] = observed["metadata"]["name"]
        meta.pop("generateName", None)
    else:
        meta["generateName"] = f"{xr_meta.get('name', '')}-"
    if xr_meta.get("namespace"):
        meta["namespace"] = xr_meta["namespace"]
    meta["ownerReferences"] = [{"apiVersion": xr.get("apiVersion"), "kind": xr.get("kind"), "name": xr_meta.get("name"), "uid": composite_uid(xr), "controller": True, "blockOwnerDeletion": True}]


def composite_output(xr: dict, desired_xr: dict, ready: Dict[str, bool], skipped: List[str]) -> dict:
    meta = xr.get("metadata") or {}
    status = copy.deepcopy(desired_xr.get("status") or {})
    unready = sorted([name for name, ok in ready.items() if not ok] + skipped)
    if unready:
        condition = {"type": "Ready", "status": "False", "reason": "Creating", "message": f"Unready resources: {', '.join(unready)}"}
    else:
        condition = {"type": "Ready", "status": "True", "reason": "Available"}
    status["conditions"] = [condition]
    return {"apiVersion": xr.get("apiVersion"), "kind": xr.get("kind"), "metadata": {key: meta[key] for key in ("name", "namespace") if key in meta}, "status": status}


def render(xr: dict, composition: dict, observed: Sequence[dict] = ()) -> Rendered:
    spec = validate_composition(xr, composition)
    observed_by_name: Dict[str, dict] = {}
    for obj in observed:
        name = ((obj.get("metadata") or {}).get("annotations") or {}).get(COMPOSITION_RESOURCE_NAME)
        if not name:
            raise CompositionError(f"observed {obj.get('kind')} {(obj.get('metadata') or {}).get('name')} has no {COMPOSITION_RESOURCE_NAME} annotation; that annotation is how observed resources are matched to templates")
        observed_by_name[name] = obj
    desired_xr: dict = {}
    desired: Dict[str, dict] = {}
    ready: Dict[str, bool] = {}
    warnings: List[str] = []
    skipped: List[str] = []
    for step in spec.get("pipeline") or []:
        fn_input = step.get("input") or {}
        if fn_input.get("apiVersion") != PT_INPUT_API or fn_input.get("kind") != "Resources":
            warnings.append(f"step {step.get('step')!r} runs {(step.get('functionRef') or {}).get('name')}; minixp only runs patch-and-transform input and skipped it")
            continue
        for template in expand_patch_sets(fn_input.get("patchSets"), fn_input.get("resources") or []):
            name = template.get("name")
            if not name:
                raise CompositionError("every resource template needs a name")
            if template.get("base") is None:
                if name not in desired:
                    raise CompositionError(f"composed resource {name!r} has no base template, and was not produced by a previous step")
                composed = copy.deepcopy(desired[name])
            else:
                composed = copy.deepcopy(template["base"])
            seen = observed_by_name.get(name)
            ready[name] = seen is not None and is_ready(seen, template.get("readinessChecks") or [])
            skip = False
            for index, patch in enumerate(template.get("patches") or []):
                kind = patch.get("type", "FromCompositeFieldPath")
                try:
                    apply_patch(patch, xr, desired_xr, composed, seen)
                except FieldNotFound as missing:
                    if (patch.get("policy") or {}).get("fromFieldPath") != "Required":
                        continue
                    if kind in TO_COMPOSED_PATCHES and seen is None:
                        warnings.append(f"not adding new composed resource {name!r}: {kind} patch at index {index} has policy.fromFieldPath: Required and {missing} is not set")
                        skip = True
                        break
                    warnings.append(f"{name!r} {kind} patch at index {index}: {missing} is not set; ignoring Required because the resource already exists")
                except CompositionError as exc:
                    raise CompositionError(f"cannot render composed resource {name!r} {kind} patch at index {index}: {exc}") from None
            if skip:
                skipped.append(name)
                ready.pop(name, None)
                continue
            stamp_metadata(composed, xr, name, seen)
            desired[name] = composed
    extra = sorted(set(desired_xr) - {"status"})
    if extra:
        warnings.append(f"ToCompositeFieldPath patches wrote {', '.join(extra)} on the XR; only status is returned")
    return Rendered(composite_output(xr, desired_xr, ready, skipped), [desired[name] for name in sorted(desired)], ready, warnings, skipped)


def load_documents(path: str) -> List[Any]:
    text = read_text(path)
    documents: List[Any] = []
    for chunk in re.split(r"^---[ \t]*$", text, flags=re.M):
        if not chunk.strip():
            continue
        try:
            value = json.loads(chunk)
        except json.JSONDecodeError as exc:
            raise UsageError(f"{path}: minixp reads JSON documents, separated by --- lines when there are several (line {exc.lineno} of a document: {exc.msg})") from None
        if isinstance(value, list):
            documents.extend(value)
        elif isinstance(value, dict) and str(value.get("kind", "")).endswith("List") and isinstance(value.get("items"), list):
            documents.extend(value["items"])
        else:
            documents.append(value)
    return documents


def load_one(path: str, what: str) -> dict:
    documents = load_documents(path)
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise UsageError(f"{path}: expected exactly one {what} object, found {len(documents)}")
    return documents[0]


def render_table(result: Rendered) -> str:
    xr = result.composite
    condition = xr["status"]["conditions"][0]
    where = f" (namespace {xr['metadata']['namespace']})" if xr["metadata"].get("namespace") else ""
    lines = [f"{xr['kind']}/{xr['metadata'].get('name')}{where}: Ready={condition['status']} {condition['reason']}" + (f": {condition['message']}" if condition.get("message") else "")]
    rows = [(obj["metadata"]["annotations"][COMPOSITION_RESOURCE_NAME], f"{obj.get('apiVersion')}/{obj.get('kind')}", str(result.ready.get(obj["metadata"]["annotations"][COMPOSITION_RESOURCE_NAME], False))) for obj in result.composed]
    rows += [(name, "(skipped)", "False") for name in result.skipped]
    widths = [max(len(row[column]) for row in rows + [("RESOURCE", "KIND", "READY")]) for column in range(3)]
    for row in [("RESOURCE", "KIND", "READY")] + rows:
        lines.append("  " + "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
    status = {key: value for key, value in xr["status"].items() if key != "conditions"}
    if status:
        lines.append(f"status: {json.dumps(status, sort_keys=True)}")
    return "\n".join(lines)


def xp_command(args: argparse.Namespace) -> int:
    xr = load_one(args.composite, "composite resource")
    composition = load_one(args.composition, "Composition")
    observed = load_documents(args.observed) if args.observed else []
    result = render(xr, composition, observed)
    for warning in result.warnings:
        print(f"minixp: warning: {warning}", file=sys.stderr)
    objects = [result.composite] + result.composed
    if args.json:
        print(json.dumps(objects, indent=2))
    elif args.stream:
        print("\n".join("---\n" + json.dumps(obj, indent=2) for obj in objects))
    else:
        print(render_table(result))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="main.py", description=__doc__)
    tools = parser.add_subparsers(dest="tool", required=True)
    pulumi = tools.add_parser("pulumi", help="minipulumi: run a Pulumi-style Python program against a stack state file")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cwd", default=".", help="project directory with Pulumi.yaml and __main__.py")
    common.add_argument("--stack", default="dev", help="stack name; config comes from Pulumi.<stack>.yaml")
    common.add_argument("--state", help="state file (default: <cwd>/.minipulumi/<stack>.json)")
    common.add_argument("--seed", type=int, help="seed the random provider for repeatable runs")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    commands = pulumi.add_subparsers(dest="command", required=True)
    commands.add_parser("preview", parents=[common], help="run the program, diff against state, change nothing")
    commands.add_parser("up", parents=[common], help="run the program and apply the diff")
    commands.add_parser("destroy", parents=[common], help="delete every resource in the stack, dependents first")
    commands.add_parser("export", parents=[common], help="print the state file")
    commands.add_parser("graph", parents=[common], help="dependency graph from state (DOT, or --json)")
    output = commands.add_parser("output", parents=[common], help="print stack outputs")
    output.add_argument("name", nargs="?")
    output.add_argument("--show-secrets", action="store_true")
    xp = tools.add_parser("xp", help="minixp: render a composite resource through a patch-and-transform Composition")
    xp_commands = xp.add_subparsers(dest="command", required=True)
    render_parser = xp_commands.add_parser("render", help="render composed resources and the XR status as a pure function")
    render_parser.add_argument("composite", help="the XR (JSON)")
    render_parser.add_argument("composition", help="the Composition (JSON)")
    render_parser.add_argument("functions", nargs="?", help="accepted so the arguments match crossplane composition render; not read")
    render_parser.add_argument("--observed", "-o", help="observed composed resources (JSON documents, --- separated)")
    formats = render_parser.add_mutually_exclusive_group()
    formats.add_argument("--json", action="store_true", help="a JSON array: the XR, then composed resources by name")
    formats.add_argument("--stream", action="store_true", help="--- separated JSON documents for kubectl create -f -")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    prog = "minipulumi" if args.tool == "pulumi" else "minixp"
    try:
        return pulumi_command(args) if args.tool == "pulumi" else xp_command(args)
    except UsageError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 2
    except (EngineError, CompositionError) as exc:
        print(f"{prog}: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
