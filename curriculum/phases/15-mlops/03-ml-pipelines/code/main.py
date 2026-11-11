from __future__ import annotations

import argparse
import concurrent.futures
import difflib
import hashlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

SDK_VERSION = "minipipe-1.0.0"
SCHEMA_VERSION = "2.1.0"
DEFAULT_IMAGE = "python:3.12-alpine"
STORE_MOUNT = "/minipipe/store"
SCHEMA = "0.0.1"
PARAMETER_TYPES = {bool: "BOOLEAN", int: "NUMBER_INTEGER", float: "NUMBER_DOUBLE", str: "STRING"}
MISSING = object()

BOOTSTRAP = r'''
import hashlib, inspect, json, os, sys, types


class Artifact:
    schema_title = "system.Artifact"

    def __init__(self, name="", uri="", metadata=None):
        self.name = name
        self.uri = uri
        self.metadata = dict(metadata or {})

    @property
    def path(self):
        return local_path(self.uri)


class Dataset(Artifact):
    schema_title = "system.Dataset"


class Model(Artifact):
    schema_title = "system.Model"


class Metrics(Artifact):
    schema_title = "system.Metrics"

    def log_metric(self, metric, value):
        self.metadata[metric] = value


class Direction:
    def __init__(self, direction):
        self.direction = direction

    def __getitem__(self, cls):
        return (self.direction, cls)


def local_path(uri):
    if uri.startswith("minipipe://"):
        return os.path.join(os.environ.get("MINIPIPE_STORE", "/minipipe/store"), uri[len("minipipe://"):])
    if uri.startswith("file://"):
        return uri[len("file://"):]
    return uri


def digest(path):
    h = hashlib.sha256()
    if os.path.isdir(path):
        for base, dirs, files in os.walk(path):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(base, name)
                h.update(os.path.relpath(full, path).replace(os.sep, "/").encode() + b"\0" + digest(full).encode() + b"\n")
        return "dir-" + h.hexdigest()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def coerce(value, annotation):
    if annotation is bool:
        return value if isinstance(value, bool) else str(value).lower() == "true"
    if annotation is int:
        return value if isinstance(value, int) and not isinstance(value, bool) else int(float(value))
    if annotation is float:
        return float(value)
    if annotation is str:
        return value if isinstance(value, str) else json.dumps(value)
    return value


def text(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def write(path, content):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def main(argv):
    source = argv[0]
    options = dict(zip(argv[1::2], argv[2::2]))
    executor_input = json.loads(options["--executor_input"])
    name = options["--function_to_execute"]
    kinds = dict(Artifact=Artifact, Dataset=Dataset, Model=Model, Metrics=Metrics, Input=Direction("input"), Output=Direction("output"))
    dsl = types.SimpleNamespace(component=lambda *a, **k: a[0] if a and callable(a[0]) else (lambda f: f), **kinds)
    namespace = dict(kinds, __name__="__minipipe_component__", dsl=dsl)
    exec(compile(source, "<component " + name + ">", "exec"), namespace)
    function = namespace[name]
    inputs = executor_input.get("inputs", {})
    outputs = executor_input.get("outputs", {})
    values = inputs.get("parameterValues", {})
    kwargs, produced = {}, {}
    for pname, parameter in inspect.signature(function).parameters.items():
        annotation = parameter.annotation
        if isinstance(annotation, tuple):
            direction, cls = annotation
            group = (inputs if direction == "input" else outputs).get("artifacts", {}).get(pname)
            if group is None:
                raise SystemExit(name + ": the executor input has no " + direction + " artifact " + pname)
            spec = group["artifacts"][0]
            artifact = cls(pname, spec["uri"], spec.get("metadata"))
            if direction == "output":
                os.makedirs(os.path.dirname(artifact.path), exist_ok=True)
                produced[pname] = artifact
            kwargs[pname] = artifact
        elif pname in values:
            kwargs[pname] = coerce(values[pname], annotation)
    result = function(**kwargs)
    report = {"parameterValues": {}, "artifacts": {}}
    flat = outputs.get("flatDir")
    for key, spec in outputs.get("parameters", {}).items():
        report["parameterValues"][key] = result
        if spec.get("outputFile"):
            write(spec["outputFile"], text(result))
        if flat:
            write(os.path.join(flat, key), text(result))
    for key, artifact in produced.items():
        if not os.path.exists(artifact.path):
            raise SystemExit(name + ": output artifact " + key + " was not written to " + artifact.path)
        sha = digest(artifact.path)
        report["artifacts"][key] = {"artifacts": [{"uri": artifact.uri, "metadata": dict(artifact.metadata, sha256=sha)}]}
        if flat:
            write(os.path.join(flat, key + "-uri"), artifact.uri)
            write(os.path.join(flat, key + "-sha256"), sha)
    if outputs.get("outputFile"):
        write(outputs["outputFile"], json.dumps(report, sort_keys=True))
    return 0


sys.exit(main(sys.argv[1:]))
'''


class PipelineError(Exception):
    pass


def digest(path: Path) -> str:
    h = hashlib.sha256()
    if path.is_dir():
        for base, dirs, files in os.walk(path):
            dirs.sort()
            for name in sorted(files):
                full = Path(base) / name
                h.update(full.relative_to(path).as_posix().encode() + b"\0" + digest(full).encode() + b"\n")
        return "dir-" + h.hexdigest()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def text_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def coerce(value: Any, ptype: str) -> Any:
    if ptype == "BOOLEAN":
        return value if isinstance(value, bool) else str(value).lower() == "true"
    if ptype == "NUMBER_INTEGER":
        if isinstance(value, bool):
            raise PipelineError(f"expected an integer, got {value!r}")
        number = float(value)
        if number != int(number):
            raise PipelineError(f"expected an integer, got {value!r}")
        return int(number)
    if ptype == "NUMBER_DOUBLE":
        return float(value)
    if ptype == "STRING":
        return value if isinstance(value, str) else json.dumps(value)
    return value


class Artifact:
    schema_title = "system.Artifact"

    def __init__(self, name: str = "", uri: str = "", metadata: Optional[Dict[str, Any]] = None):
        self.name, self.uri, self.metadata = name, uri, dict(metadata or {})


class Dataset(Artifact):
    schema_title = "system.Dataset"


class Model(Artifact):
    schema_title = "system.Model"


class Metrics(Artifact):
    schema_title = "system.Metrics"


@dataclass(frozen=True)
class ArtifactAnnotation:
    direction: str
    cls: type


class Direction:
    def __init__(self, direction: str):
        self.direction = direction

    def __getitem__(self, cls: type) -> ArtifactAnnotation:
        return ArtifactAnnotation(self.direction, cls)


Input = Direction("input")
Output = Direction("output")


@dataclass
class ParamSpec:
    name: str
    ptype: str
    default: Any = MISSING

    def definition(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"parameterType": self.ptype}
        if self.default is not MISSING:
            out.update(defaultValue=self.default, isOptional=True)
        return out


def parameter_type(annotation: Any, where: str) -> str:
    if annotation in PARAMETER_TYPES:
        return PARAMETER_TYPES[annotation]
    raise PipelineError(f"{where}: unsupported type {annotation!r}; use int, float, str, bool, Input[...] or Output[...]")


def artifact_definition(schema_title: str) -> Dict[str, Any]:
    return {"artifactType": {"schemaTitle": schema_title, "schemaVersion": SCHEMA}}


def resolved_annotation(func: Callable, annotation: Any) -> Any:
    if isinstance(annotation, str):
        return eval(annotation, func.__globals__)
    return annotation


def function_source(func: Callable) -> str:
    lines = textwrap.dedent(inspect.getsource(func)).splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("def "))
    return "\n".join(lines[start:]) + "\n"


class Component:
    def __init__(self, func: Callable, base_image: str = DEFAULT_IMAGE):
        self.python_func = func
        self.base_image = base_image
        self.name = func.__name__.replace("_", "-")
        self.source = function_source(func)
        self.parameters: Dict[str, ParamSpec] = {}
        self.input_artifacts: Dict[str, str] = {}
        self.output_artifacts: Dict[str, str] = {}
        signature = inspect.signature(func)
        for pname, parameter in signature.parameters.items():
            annotation = resolved_annotation(func, parameter.annotation)
            if isinstance(annotation, ArtifactAnnotation):
                target = self.input_artifacts if annotation.direction == "input" else self.output_artifacts
                target[pname] = annotation.cls.schema_title
                continue
            default = MISSING if parameter.default is inspect.Parameter.empty else parameter.default
            self.parameters[pname] = ParamSpec(pname, parameter_type(annotation, f"{func.__name__}({pname})"), default)
        returned = resolved_annotation(func, signature.return_annotation)
        self.output_type = None if returned in (inspect.Signature.empty, None, type(None)) else parameter_type(returned, f"{func.__name__} return")

    def spec(self) -> Dict[str, Any]:
        inputs: Dict[str, Any] = {}
        if self.input_artifacts:
            inputs["artifacts"] = {n: artifact_definition(t) for n, t in self.input_artifacts.items()}
        if self.parameters:
            inputs["parameters"] = {n: p.definition() for n, p in self.parameters.items()}
        outputs: Dict[str, Any] = {}
        if self.output_artifacts:
            outputs["artifacts"] = {n: artifact_definition(t) for n, t in self.output_artifacts.items()}
        if self.output_type:
            outputs["parameters"] = {"Output": {"parameterType": self.output_type}}
        spec: Dict[str, Any] = {}
        if inputs:
            spec["inputDefinitions"] = inputs
        if outputs:
            spec["outputDefinitions"] = outputs
        return spec

    def container(self) -> Dict[str, Any]:
        return {
            "image": self.base_image,
            "command": ["python3", "-c", BOOTSTRAP, self.source],
            "args": ["--executor_input", "{{$}}", "--function_to_execute", self.python_func.__name__],
        }

    def __call__(self, **arguments: Any) -> "PipelineTask":
        return PipelineTask(self, arguments, active_builder(self.name))


def component(func: Optional[Callable] = None, *, base_image: str = DEFAULT_IMAGE) -> Any:
    if func is not None:
        return Component(func, base_image)
    return lambda f: Component(f, base_image)


class Channel:
    def __init__(self, kind: str, name: str, ptype: Optional[str] = None, task: Optional["PipelineTask"] = None, schema: Optional[str] = None):
        self.kind, self.name, self.ptype, self.task, self.schema = kind, name, ptype, task, schema

    @property
    def is_artifact(self) -> bool:
        return self.kind == "artifact"

    @property
    def channel_name(self) -> str:
        if self.task is not None:
            return f"pipelinechannel--{self.task.name}-{self.name}"
        return f"pipelinechannel--{self.name}"

    def __hash__(self) -> int:
        return id(self)

    def __bool__(self) -> bool:
        raise PipelineError(f"{self.channel_name} has no value while the pipeline is compiled; branch with dsl.If(...), not with Python's if")

    def _compare(self, op: str, other: Any) -> "Predicate":
        return Predicate(op, self, other)

    def __eq__(self, other: Any) -> "Predicate":
        return self._compare("==", other)

    def __ne__(self, other: Any) -> "Predicate":
        return self._compare("!=", other)

    def __lt__(self, other: Any) -> "Predicate":
        return self._compare("<", other)

    def __le__(self, other: Any) -> "Predicate":
        return self._compare("<=", other)

    def __gt__(self, other: Any) -> "Predicate":
        return self._compare(">", other)

    def __ge__(self, other: Any) -> "Predicate":
        return self._compare(">=", other)


@dataclass
class Predicate:
    op: str
    left: Any
    right: Any

    def __bool__(self) -> bool:
        raise PipelineError("a comparison of pipeline values is decided at run time; put it in dsl.If(...)")


class Group:
    def __init__(self, kind: str, name: str, parent: Optional["Group"], display: Optional[str] = None):
        self.kind, self.name, self.parent, self.display = kind, name, parent, display
        self.members: List[Any] = []
        self.predicate: Optional[Predicate] = None
        self.items: List[Any] = []
        self.item: Optional[Channel] = None
        self.parallelism = 0


class Builder:
    def __init__(self) -> None:
        self.tasks: List[PipelineTask] = []
        self.root = Group("root", "root", None)
        self.stack = [self.root]
        self.counter = 0
        self.seen: Dict[str, int] = {}

    def next_id(self) -> int:
        self.counter += 1
        return self.counter

    def task_name(self, base: str) -> str:
        self.seen[base] = self.seen.get(base, 0) + 1
        return base if self.seen[base] == 1 else f"{base}-{self.seen[base]}"

    def open_group(self, group: Group) -> None:
        if len(self.stack) > 1:
            raise PipelineError(f"{group.name}: minipipe supports one level of dsl.If / dsl.ParallelFor; move the inner block into its own pipeline")
        self.stack[-1].members.append(group)
        self.stack.append(group)


BUILDERS: List[Builder] = []


def active_builder(what: str) -> Builder:
    if not BUILDERS:
        raise PipelineError(f"{what} was called outside a pipeline; call components inside a @dsl.pipeline function")
    return BUILDERS[-1]


class PipelineTask:
    def __init__(self, comp: Component, arguments: Dict[str, Any], builder: Builder):
        self.component = comp
        self.name = builder.task_name(comp.name)
        self.group = builder.stack[-1]
        self.retry: Optional[Dict[str, Any]] = None
        self.cache = True
        self.after_tasks: List[PipelineTask] = []
        self.arguments: Dict[str, Any] = {}
        for pname, value in arguments.items():
            self.arguments[pname] = self.check_argument(pname, value)
        for pname, spec in comp.parameters.items():
            if pname not in self.arguments and spec.default is MISSING:
                raise PipelineError(f"{self.name}: missing argument {pname!r}")
        for pname in comp.input_artifacts:
            if pname not in self.arguments:
                raise PipelineError(f"{self.name}: missing input artifact {pname!r}")
        self.outputs: Dict[str, Channel] = {n: Channel("artifact", n, task=self, schema=t) for n, t in comp.output_artifacts.items()}
        if comp.output_type:
            self.outputs["Output"] = Channel("parameter", "Output", comp.output_type, task=self)
        builder.tasks.append(self)
        self.group.members.append(self)

    def check_argument(self, pname: str, value: Any) -> Any:
        comp = self.component
        if pname in comp.input_artifacts:
            if not isinstance(value, Channel) or not value.is_artifact:
                raise PipelineError(f"{self.name}: {pname} is an input artifact; pass another task's output artifact")
            expected = comp.input_artifacts[pname]
            if expected != Artifact.schema_title and value.schema != expected:
                raise PipelineError(f"{self.name}: {pname} expects {expected}, got {value.schema}")
            return value
        if pname not in comp.parameters:
            raise PipelineError(f"{self.name}: unknown argument {pname!r}")
        expected = comp.parameters[pname].ptype
        if isinstance(value, Channel):
            if value.is_artifact:
                raise PipelineError(f"{self.name}: {pname} is a parameter, got the artifact {value.channel_name}")
            if value.ptype != expected and not (expected == "NUMBER_DOUBLE" and value.ptype == "NUMBER_INTEGER"):
                raise PipelineError(f"{self.name}: {pname} expects {expected}, got {value.ptype} from {value.channel_name}")
            return value
        actual = PARAMETER_TYPES.get(type(value))
        if actual != expected and not (expected == "NUMBER_DOUBLE" and actual == "NUMBER_INTEGER"):
            raise PipelineError(f"{self.name}: {pname} expects {expected}, got {value!r}")
        return value

    @property
    def output(self) -> Channel:
        if len(self.outputs) != 1:
            raise PipelineError(f"{self.name} has {len(self.outputs)} outputs; use .outputs[NAME]")
        return next(iter(self.outputs.values()))

    def set_retry(self, num_retries: int, backoff_duration: Optional[str] = None, backoff_factor: Optional[float] = None, backoff_max_duration: Optional[str] = None) -> "PipelineTask":
        self.retry = {
            "backoffDuration": backoff_duration or "0s",
            "backoffFactor": float(backoff_factor if backoff_factor is not None else 2.0),
            "backoffMaxDuration": backoff_max_duration or "3600s",
            "maxRetryCount": num_retries,
        }
        return self

    def set_caching_options(self, enable_caching: bool) -> "PipelineTask":
        self.cache = enable_caching
        return self

    def after(self, *tasks: "PipelineTask") -> "PipelineTask":
        self.after_tasks.extend(tasks)
        return self


class If:
    def __init__(self, condition: Any, name: Optional[str] = None):
        if not isinstance(condition, Predicate):
            raise PipelineError("dsl.If needs a comparison with a pipeline value, such as evaluate.output < 0.5")
        self.condition, self.name = condition, name

    def __enter__(self) -> Group:
        builder = active_builder("dsl.If")
        group = Group("condition", f"condition-{builder.next_id()}", builder.stack[-1], self.name)
        group.predicate = self.condition
        builder.open_group(group)
        return group

    def __exit__(self, *exc: Any) -> bool:
        BUILDERS[-1].stack.pop()
        return False


Condition = If


class ParallelFor:
    def __init__(self, items: List[Any], parallelism: int = 0, name: Optional[str] = None):
        if not isinstance(items, list) or not items:
            raise PipelineError("dsl.ParallelFor needs a non-empty list of constants")
        kinds = {type(item) for item in items}
        if len(kinds) != 1 or next(iter(kinds)) not in PARAMETER_TYPES:
            raise PipelineError(f"dsl.ParallelFor items must all be int, float, str or bool, got {sorted(k.__name__ for k in kinds)}")
        self.items, self.parallelism, self.name = items, parallelism, name

    def __enter__(self) -> Channel:
        builder = active_builder("dsl.ParallelFor")
        item_id = builder.next_id()
        group = Group("loop", f"for-loop-{builder.next_id()}", builder.stack[-1], self.name)
        group.items, group.parallelism = list(self.items), self.parallelism
        group.item = Channel("item", f"loop-item-param-{item_id}", PARAMETER_TYPES[type(self.items[0])])
        builder.open_group(group)
        return group.item

    def __exit__(self, *exc: Any) -> bool:
        BUILDERS[-1].stack.pop()
        return False


class Pipeline:
    def __init__(self, func: Callable, name: Optional[str] = None):
        self.func = func
        self.name = name or func.__name__.replace("_", "-")
        self.parameters: Dict[str, ParamSpec] = {}
        for pname, parameter in inspect.signature(func).parameters.items():
            default = MISSING if parameter.default is inspect.Parameter.empty else parameter.default
            ptype = parameter_type(resolved_annotation(func, parameter.annotation), f"pipeline {self.name}({pname})")
            self.parameters[pname] = ParamSpec(pname, ptype, default)


def pipeline(func: Optional[Callable] = None, *, name: Optional[str] = None) -> Any:
    if func is not None:
        return Pipeline(func, name)
    return lambda f: Pipeline(f, name)


dsl = types.SimpleNamespace(
    component=component, pipeline=pipeline, If=If, Condition=Condition, ParallelFor=ParallelFor,
    Input=Input, Output=Output, Artifact=Artifact, Dataset=Dataset, Model=Model, Metrics=Metrics,
)


def wire(channel: Channel, scope: Group, needs: Dict[str, Channel], deps: Set[str]) -> Dict[str, Any]:
    if channel.kind == "param":
        if scope.kind == "root":
            return {"componentInputParameter": channel.name}
        needs[channel.channel_name] = channel
        return {"componentInputParameter": channel.channel_name}
    if channel.kind == "item":
        if scope.item is not channel:
            raise PipelineError(f"the loop item {channel.name} is used outside its dsl.ParallelFor")
        return {"componentInputParameter": channel.channel_name}
    producer = channel.task
    assert producer is not None
    if producer.group is scope:
        deps.add(producer.name)
        if channel.is_artifact:
            return {"taskOutputArtifact": {"outputArtifactKey": channel.name, "producerTask": producer.name}}
        return {"taskOutputParameter": {"outputParameterKey": channel.name, "producerTask": producer.name}}
    if producer.group.kind == "root":
        needs[channel.channel_name] = channel
        return {("componentInputArtifact" if channel.is_artifact else "componentInputParameter"): channel.channel_name}
    raise PipelineError(f"{producer.name}.{channel.name} is produced inside {producer.group.name} and cannot be used outside it")


def task_spec(task: PipelineTask, scope: Group, needs: Dict[str, Channel], outer: Set[str]) -> Dict[str, Any]:
    deps: Set[str] = set()
    parameters: Dict[str, Any] = {}
    artifacts: Dict[str, Any] = {}
    for pname, value in task.arguments.items():
        if isinstance(value, Channel):
            (artifacts if value.is_artifact else parameters)[pname] = wire(value, scope, needs, deps)
        else:
            parameters[pname] = {"runtimeValue": {"constant": value}}
    for other in task.after_tasks:
        if other.group is scope:
            deps.add(other.name)
        elif other.group.kind == "root":
            outer.add(other.name)
        else:
            raise PipelineError(f"{task.name}.after({other.name}): {other.name} is inside {other.group.name}")
    spec: Dict[str, Any] = {
        "cachingOptions": {"enableCache": True} if task.cache else {},
        "componentRef": {"name": f"comp-{task.name}"},
        "taskInfo": {"name": task.name},
    }
    if deps:
        spec["dependentTasks"] = sorted(deps)
    inputs: Dict[str, Any] = {}
    if artifacts:
        inputs["artifacts"] = artifacts
    if parameters:
        inputs["parameters"] = parameters
    if inputs:
        spec["inputs"] = inputs
    if task.retry:
        spec["retryPolicy"] = dict(task.retry)
    return spec


def condition_operand(value: Any, group: Group, needs: Dict[str, Channel]) -> Tuple[str, str]:
    if isinstance(value, Channel):
        if value.is_artifact:
            raise PipelineError("dsl.If compares parameters, not artifacts")
        wire(value, group, needs, set())
        reference = f"inputs.parameter_values['{value.channel_name}']"
        return (f"int({reference})" if value.ptype == "NUMBER_INTEGER" else reference), str(value.ptype)
    if isinstance(value, bool):
        return ("true" if value else "false"), "BOOLEAN"
    if isinstance(value, str):
        return f"'{value}'", "STRING"
    if isinstance(value, (int, float)):
        return str(value), PARAMETER_TYPES[type(value)]
    raise PipelineError(f"dsl.If cannot compare with {value!r}")


def condition_text(predicate: Predicate, group: Group, needs: Dict[str, Channel]) -> str:
    left, left_type = condition_operand(predicate.left, group, needs)
    right, right_type = condition_operand(predicate.right, group, needs)
    if left_type != right_type:
        if {left_type, right_type} != {"NUMBER_INTEGER", "NUMBER_DOUBLE"}:
            raise PipelineError(f"dsl.If compares {left_type} with {right_type}; compare values of the same type")
        left = f"double({left})" if left_type == "NUMBER_INTEGER" else left
        right = f"double({right})" if right_type == "NUMBER_INTEGER" else right
    return f"{left} {predicate.op} {right}"


def compile_group(group: Group) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    needs: Dict[str, Channel] = {}
    outer: Set[str] = set()
    tasks = {member.name: task_spec(member, group, needs, outer) for member in group.members}
    condition = None
    if group.predicate is not None:
        condition = condition_text(group.predicate, group, needs)
    parameters: Dict[str, Any] = {}
    artifacts: Dict[str, Any] = {}
    for name, channel in needs.items():
        if channel.is_artifact:
            artifacts[name] = artifact_definition(channel.schema or Artifact.schema_title)
        else:
            parameters[name] = {"parameterType": channel.ptype}
    if group.item is not None:
        parameters[group.item.channel_name] = {"parameterType": group.item.ptype}
    definitions: Dict[str, Any] = {}
    if artifacts:
        definitions["artifacts"] = artifacts
    if parameters:
        definitions["parameters"] = parameters
    spec: Dict[str, Any] = {"dag": {"tasks": tasks}}
    if definitions:
        spec["inputDefinitions"] = definitions
    deps = set(outer)
    task_parameters: Dict[str, Any] = {}
    task_artifacts: Dict[str, Any] = {}
    for name, channel in needs.items():
        (task_artifacts if channel.is_artifact else task_parameters)[name] = wire(channel, group.parent or group, {}, deps)
    task: Dict[str, Any] = {"componentRef": {"name": f"comp-{group.name}"}, "taskInfo": {"name": group.display or group.name}}
    if deps:
        task["dependentTasks"] = sorted(deps)
    inputs: Dict[str, Any] = {}
    if task_artifacts:
        inputs["artifacts"] = task_artifacts
    if task_parameters:
        inputs["parameters"] = task_parameters
    if inputs:
        task["inputs"] = inputs
    if condition is not None:
        task["triggerPolicy"] = {"condition": condition}
    if group.item is not None:
        task["parameterIterator"] = {"itemInput": group.item.channel_name, "items": {"raw": json.dumps(group.items)}}
        if group.parallelism:
            task["iteratorPolicy"] = {"parallelismLimit": group.parallelism}
    return spec, task


def compile_pipeline(pipe: Pipeline) -> Dict[str, Any]:
    builder = Builder()
    BUILDERS.append(builder)
    try:
        pipe.func(**{n: Channel("param", n, p.ptype) for n, p in pipe.parameters.items()})
    finally:
        BUILDERS.pop()
    if not builder.tasks:
        raise PipelineError(f"pipeline {pipe.name} has no tasks")
    components: Dict[str, Any] = {}
    executors: Dict[str, Any] = {}
    for task in builder.tasks:
        components[f"comp-{task.name}"] = dict(task.component.spec(), executorLabel=f"exec-{task.name}")
        executors[f"exec-{task.name}"] = {"container": task.component.container()}
    tasks: Dict[str, Any] = {}
    for member in builder.root.members:
        if isinstance(member, PipelineTask):
            tasks[member.name] = task_spec(member, builder.root, {}, set())
        else:
            components[f"comp-{member.name}"], tasks[member.name] = compile_group(member)
    root: Dict[str, Any] = {"dag": {"tasks": tasks}}
    if pipe.parameters:
        root["inputDefinitions"] = {"parameters": {n: p.definition() for n, p in pipe.parameters.items()}}
    return {
        "components": dict(sorted(components.items())),
        "deploymentSpec": {"executors": dict(sorted(executors.items()))},
        "pipelineInfo": {"name": pipe.name},
        "root": root,
        "schemaVersion": SCHEMA_VERSION,
        "sdkVersion": SDK_VERSION,
    }


def load_pipeline(path: Path, name: Optional[str] = None) -> Pipeline:
    sys.modules["minipipe"] = sys.modules[__name__]
    spec = importlib.util.spec_from_file_location(f"pipeline_{sha256_text(str(path.resolve()))[:8]}", path)
    if spec is None or spec.loader is None:
        raise PipelineError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    found = {key: value for key, value in vars(module).items() if isinstance(value, Pipeline)}
    if name:
        if name not in found:
            raise PipelineError(f"{path} has no pipeline {name!r}; found {sorted(found)}")
        return found[name]
    if len(found) != 1:
        raise PipelineError(f"{path} defines {len(found)} pipelines {sorted(found)}; choose one with --pipeline")
    return next(iter(found.values()))


def load_ir(path: Path, name: Optional[str] = None) -> Dict[str, Any]:
    if path.suffix == ".py":
        return compile_pipeline(load_pipeline(path, name))
    try:
        ir = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PipelineError(f"cannot read {path} as IR JSON: {exc}")
    for key in ("components", "deploymentSpec", "root", "pipelineInfo"):
        if key not in ir:
            raise PipelineError(f"{path} is not a pipeline IR: no {key!r}")
    return ir


def leaf_component(ir: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any]:
    name = task["componentRef"]["name"]
    if name not in ir["components"]:
        raise PipelineError(f"component {name} is referenced but not defined")
    return ir["components"][name]


def flatten(ir: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    def leaves(tasks: Dict[str, Any], prefix: str) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for name, task in tasks.items():
            comp = leaf_component(ir, task)
            out[name] = [f"{prefix}{name}"] if "dag" not in comp else [f"{prefix}{name}/{inner}" for inner in comp["dag"]["tasks"]]
        return out

    def describe(source: Dict[str, Any], scope: Dict[str, str], prefix: str) -> str:
        if "taskOutputArtifact" in source:
            ref = source["taskOutputArtifact"]
            return f"{prefix}{ref['producerTask']}.{ref['outputArtifactKey']}"
        if "taskOutputParameter" in source:
            ref = source["taskOutputParameter"]
            return f"{prefix}{ref['producerTask']}.{ref['outputParameterKey']}"
        if "runtimeValue" in source:
            return "constant " + json.dumps(source["runtimeValue"].get("constant"))
        key = source.get("componentInputParameter") or source.get("componentInputArtifact")
        return scope.get(key, f"pipeline.{key}")

    def walk(tasks: Dict[str, Any], prefix: str, scope: Dict[str, str], inherited: List[str], owner: Optional[Dict[str, Any]]) -> None:
        names = leaves(tasks, prefix)
        for name, task in tasks.items():
            comp = leaf_component(ir, task)
            deps = sorted(set(inherited) | {leaf for dep in task.get("dependentTasks", []) for leaf in names.get(dep, [])})
            inputs = {}
            for kind in ("parameters", "artifacts"):
                for key, source in task.get("inputs", {}).get(kind, {}).items():
                    inputs[key] = describe(source, scope, prefix)
            if "dag" in comp:
                inner_scope = dict(inputs)
                iterator = task.get("parameterIterator")
                if iterator:
                    inner_scope[iterator["itemInput"]] = "loop item of " + iterator["items"].get("raw", "?")
                walk(comp["dag"]["tasks"], f"{prefix}{name}/", inner_scope, deps, task)
                continue
            outputs = sorted(list(comp.get("outputDefinitions", {}).get("artifacts", {})) + list(comp.get("outputDefinitions", {}).get("parameters", {})))
            rows.append({
                "task": f"{prefix}{name}",
                "deps": deps,
                "inputs": dict(sorted(inputs.items())),
                "outputs": outputs,
                "condition": (owner or {}).get("triggerPolicy", {}).get("condition"),
                "loop": (owner or {}).get("parameterIterator", {}).get("items", {}).get("raw"),
                "retries": task.get("retryPolicy", {}).get("maxRetryCount", 0),
                "cache": bool(task.get("cachingOptions", {}).get("enableCache")),
            })

    walk(ir["root"]["dag"]["tasks"], "", {}, [], None)
    return sorted(rows, key=lambda row: row["task"])


COMPARISON = re.compile(r"^\s*(?P<left>.+?)\s*(?P<op>==|!=|<=|>=|<|>)\s*(?P<right>.+?)\s*$")
REFERENCE = re.compile(r"^inputs\.parameter_values\['(?P<name>[^']+)'\]$")
CAST = re.compile(r"^(?P<cast>int|double|float|string|bool)\((?P<inner>.*)\)$")


def condition_value(token: str, values: Dict[str, Any]) -> Any:
    cast = CAST.match(token)
    if cast:
        inner = condition_value(cast.group("inner"), values)
        kind = cast.group("cast")
        if kind == "int":
            return int(float(inner))
        if kind in ("double", "float"):
            return float(inner)
        if kind == "bool":
            return inner if isinstance(inner, bool) else str(inner).lower() == "true"
        return str(inner)
    match = REFERENCE.match(token)
    if match:
        name = match.group("name")
        if name not in values:
            raise PipelineError(f"condition refers to {name}, which is not an input of the group")
        return values[name]
    if len(token) >= 2 and token[0] == token[-1] == "'":
        return token[1:-1]
    if token in ("true", "false"):
        return token == "true"
    try:
        number = float(token)
    except ValueError:
        raise PipelineError(f"cannot read {token!r} in a condition")
    return int(number) if re.fullmatch(r"-?\d+", token) else number


def evaluate_condition(text: str, values: Dict[str, Any]) -> bool:
    def clause(part: str) -> bool:
        match = COMPARISON.match(part)
        if not match:
            raise PipelineError(f"unsupported condition {part!r}; minipipe evaluates comparisons joined by && and ||")
        left, right = condition_value(match.group("left"), values), condition_value(match.group("right"), values)
        op = match.group("op")
        try:
            return {"==": left == right, "!=": left != right}[op] if op in ("==", "!=") else {
                "<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[op]
        except TypeError:
            raise PipelineError(f"cannot compare {left!r} {op} {right!r}")
    return any(all(clause(c) for c in part.split("&&")) for part in text.split("||"))


def parse_duration(text: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)?", str(text).strip())
    if not match:
        raise PipelineError(f"cannot read duration {text!r}")
    return float(match.group(1)) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600, None: 1}[match.group(2)]


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        for sub in ("artifacts", "cache", "runs", "executions"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def local(self, uri: str) -> Path:
        if uri.startswith("minipipe://"):
            return self.root / uri[len("minipipe://"):]
        if uri.startswith("file://"):
            return Path(uri[len("file://"):])
        raise PipelineError(f"minipipe cannot read {uri}")

    def write_json(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)

    def lookup(self, key: str) -> Optional[Dict[str, Any]]:
        path = self.root / "cache" / f"{key}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def remember(self, key: str, entry: Dict[str, Any]) -> None:
        self.write_json(self.root / "cache" / f"{key}.json", entry)

    def verify(self, entry: Dict[str, Any]) -> Optional[str]:
        for name, artifact in entry.get("artifacts", {}).items():
            path = self.local(artifact["uri"])
            if not path.exists():
                return f"artifact {name} is gone from {path}"
            actual = digest(path)
            if actual != artifact["sha256"]:
                return f"artifact {name} changed on disk (sha256 {artifact['sha256'][:12]} became {actual[:12]})"
        return None

    def save_run(self, report: Dict[str, Any]) -> None:
        self.write_json(self.root / "runs" / f"{report['run_id']}.json", report)

    def runs(self) -> List[Dict[str, Any]]:
        out = [json.loads(p.read_text(encoding="utf-8")) for p in (self.root / "runs").glob("*.json")]
        return sorted(out, key=lambda r: (r["started"], r["run_id"]))


@dataclass
class TaskRun:
    task: str
    path: str
    status: str
    item: Any = None
    attempts: int = 0
    cache_key: str = ""
    reason: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    artifacts: Dict[str, Dict[str, str]] = field(default_factory=dict)
    ingredients: Dict[str, Any] = field(default_factory=dict)
    started: float = 0.0
    finished: float = 0.0
    log: str = ""


@dataclass
class NodeResult:
    status: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    artifacts: Dict[str, Dict[str, str]] = field(default_factory=dict)


class Tracker:
    def __init__(self, base_url: str, experiment: str, log: Callable[[str], None], timeout: float = 5.0):
        self.base = base_url.rstrip("/")
        self.experiment = experiment
        self.log = log
        self.timeout = timeout
        self.enabled = True
        self.parent: Optional[str] = None
        self.experiment_id: Optional[str] = None
        self.lock = threading.Lock()

    def call(self, method: str, endpoint: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base}/api/2.0/mlflow/{endpoint}"
        data = None
        if method == "GET" and payload:
            url += "?" + urllib.parse.urlencode(payload)
        elif payload is not None:
            data = json.dumps(payload).encode()
        request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = response.read()
        return json.loads(body) if body else {}

    def guarded(self, action: Callable[[], Any]) -> Any:
        if not self.enabled:
            return None
        try:
            return action()
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            self.enabled = False
            self.log(f"lineage: tracking server {self.base} is not usable ({exc}); continuing without lineage")
            return None

    def experiment_id_for(self) -> str:
        try:
            found = self.call("GET", "experiments/get-by-name", {"experiment_name": self.experiment})
            return found["experiment"]["experiment_id"]
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        return self.call("POST", "experiments/create", {"name": self.experiment})["experiment_id"]

    def tags(self, values: Dict[str, Any]) -> List[Dict[str, str]]:
        return [{"key": k, "value": str(v)} for k, v in sorted(values.items())]

    def start(self, run_id: str, pipeline_name: str, ir_sha: str, arguments: Dict[str, Any]) -> None:
        def action() -> None:
            self.experiment_id = self.experiment_id_for()
            created = self.call("POST", "runs/create", {
                "experiment_id": self.experiment_id, "run_name": f"{pipeline_name}-{run_id}", "start_time": int(time.time() * 1000),
                "tags": self.tags({"minipipe.pipeline": pipeline_name, "minipipe.run_id": run_id, "minipipe.ir_sha256": ir_sha})})
            self.parent = created["run"]["info"]["run_id"]
            self.call("POST", "runs/log-batch", {"run_id": self.parent, "params": self.tags(arguments)})
            self.log(f"lineage: MLflow run {self.parent} in experiment {self.experiment} at {self.base}")
        self.guarded(action)

    def record(self, run: TaskRun) -> None:
        if run.status not in ("executed", "cached") or self.parent is None:
            return

        def action() -> None:
            tags: Dict[str, Any] = {
                "mlflow.parentRunId": self.parent, "minipipe.task": run.task, "minipipe.path": run.path,
                "minipipe.status": run.status, "minipipe.cache_key": run.cache_key,
            }
            for name, sha in run.ingredients.get("artifacts", {}).items():
                tags[f"minipipe.input.{name}"] = f"sha256:{sha}"
            for name, artifact in run.artifacts.items():
                tags[f"minipipe.output.{name}"] = f"{artifact['uri']} sha256:{artifact['sha256']}"
            now = int(time.time() * 1000)
            created = self.call("POST", "runs/create", {
                "experiment_id": self.experiment_id, "run_name": run.path, "start_time": int(run.started * 1000), "tags": self.tags(tags)})
            child = created["run"]["info"]["run_id"]
            metrics = [{"key": k, "value": float(v), "timestamp": now, "step": 0}
                       for k, v in run.parameters.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
            self.call("POST", "runs/log-batch", {"run_id": child, "params": self.tags(run.ingredients.get("parameters", {})), "metrics": metrics})
            self.call("POST", "runs/update", {"run_id": child, "status": "FINISHED", "end_time": int(run.finished * 1000)})
        with self.lock:
            self.guarded(action)

    def finish(self, status: str) -> None:
        if self.parent is None:
            return
        self.guarded(lambda: self.call("POST", "runs/update", {"run_id": self.parent, "status": "FINISHED" if status == "succeeded" else "FAILED", "end_time": int(time.time() * 1000)}))


class LocalRunner:
    def __init__(self, ir: Dict[str, Any], store: Store, runner: str = "subprocess", use_cache: bool = True, parallelism: int = 4,
                 tracker: Optional[Tracker] = None, python: str = sys.executable, docker: str = "docker",
                 log: Callable[[str], None] = lambda line: print(line, file=sys.stderr), timeout: float = 600.0,
                 container_prefix: str = "minipipe-"):
        if runner not in ("subprocess", "docker"):
            raise PipelineError(f"unknown runner {runner!r}; use subprocess or docker")
        if runner == "docker" and shutil.which(docker) is None:
            raise PipelineError(f"--runner docker needs the {docker} CLI on PATH")
        self.ir, self.store, self.runner, self.use_cache = ir, store, runner, use_cache
        self.tracker, self.python, self.docker, self.log, self.timeout = tracker, python, docker, log, timeout
        self.container_prefix = container_prefix
        self.slots = threading.Semaphore(max(1, parallelism))
        self.lock = threading.Lock()
        self.results: List[TaskRun] = []
        self.run_id = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:6]

    def arguments(self, given: Dict[str, Any]) -> Dict[str, Any]:
        definitions = self.ir["root"].get("inputDefinitions", {}).get("parameters", {})
        unknown = sorted(set(given) - set(definitions))
        if unknown:
            raise PipelineError(f"unknown pipeline parameter(s) {unknown}; the pipeline takes {sorted(definitions)}")
        values: Dict[str, Any] = {}
        for name, definition in definitions.items():
            if name in given:
                values[name] = coerce(given[name], definition["parameterType"])
            elif "defaultValue" in definition:
                values[name] = coerce(definition["defaultValue"], definition["parameterType"])
            else:
                raise PipelineError(f"pipeline parameter {name} has no default; pass --param {name}=VALUE")
        return values

    def run(self, given: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        arguments = self.arguments(given or {})
        ir_sha = sha256_text(canonical(self.ir))
        started = time.time()
        if self.tracker:
            self.tracker.start(self.run_id, self.ir["pipelineInfo"]["name"], ir_sha, arguments)
        outcome = self.run_dag(self.ir["root"]["dag"]["tasks"], arguments, {}, "", None)
        failed = any(r.status in ("failed", "upstream-failed") for r in self.results)
        status = "failed" if failed or any(o.status == "failed" for o in outcome.values()) else "succeeded"
        report = {
            "run_id": self.run_id, "pipeline": self.ir["pipelineInfo"]["name"], "ir_sha256": ir_sha, "arguments": arguments,
            "runner": self.runner, "status": status, "started": started, "finished": time.time(),
            "tasks": [asdict(r) for r in sorted(self.results, key=lambda r: (r.started, r.path))],
        }
        self.store.save_run(report)
        if self.tracker:
            self.tracker.finish(status)
        return report

    def run_dag(self, tasks: Dict[str, Any], parameters: Dict[str, Any], artifacts: Dict[str, Dict[str, str]], prefix: str, item: Any) -> Dict[str, NodeResult]:
        done: Dict[str, NodeResult] = {}
        pending = dict(tasks)
        unknown = {d for t in tasks.values() for d in t.get("dependentTasks", []) if d not in tasks}
        if unknown:
            raise PipelineError(f"dependentTasks name tasks that do not exist in this DAG: {sorted(unknown)}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(tasks))) as pool:
            running: Dict[concurrent.futures.Future, str] = {}
            while pending or running:
                for name in sorted(pending):
                    deps = pending[name].get("dependentTasks", [])
                    if not all(d in done for d in deps):
                        continue
                    spec = pending.pop(name)
                    blocked = [d for d in deps if done[d].status in ("failed", "upstream-failed")]
                    if blocked:
                        done[name] = NodeResult("upstream-failed")
                        self.mark(spec, f"{prefix}{name}", name, "upstream-failed", f"upstream {', '.join(blocked)} failed", item)
                        continue
                    running[pool.submit(self.run_node, name, spec, parameters, artifacts, dict(done), prefix, item)] = name
                if not running:
                    if pending:
                        raise PipelineError(f"tasks {sorted(pending)} can never run: their dependencies form a cycle")
                    break
                finished, _ = concurrent.futures.wait(running, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in finished:
                    done[running.pop(future)] = future.result()
        return done

    def mark(self, spec: Dict[str, Any], path: str, name: str, status: str, reason: str, item: Any) -> None:
        comp = leaf_component(self.ir, spec)
        if "dag" in comp:
            for inner, inner_spec in comp["dag"]["tasks"].items():
                self.mark(inner_spec, f"{path}/{inner}", inner, status, reason, item)
            return
        now = time.time()
        with self.lock:
            self.results.append(TaskRun(task=name, path=path, status=status, item=item, reason=reason, started=now, finished=now))

    def resolve(self, spec: Dict[str, Any], parameters: Dict[str, Any], artifacts: Dict[str, Dict[str, str]], done: Dict[str, NodeResult]) -> Tuple[Dict[str, Any], Dict[str, Dict[str, str]]]:
        values: Dict[str, Any] = {}
        refs: Dict[str, Dict[str, str]] = {}
        for name, source in spec.get("inputs", {}).get("parameters", {}).items():
            if "parameterExpressionSelector" in source:
                raise PipelineError(f"{name}: parameterExpressionSelector is not supported by minipipe")
            if "runtimeValue" in source:
                values[name] = source["runtimeValue"]["constant"]
            elif "componentInputParameter" in source:
                values[name] = parameters[source["componentInputParameter"]]
            elif "taskOutputParameter" in source:
                ref = source["taskOutputParameter"]
                produced = done[ref["producerTask"]]
                if ref["outputParameterKey"] not in produced.parameters:
                    raise PipelineError(f"{name}: {ref['producerTask']} produced no parameter {ref['outputParameterKey']} ({produced.status})")
                values[name] = produced.parameters[ref["outputParameterKey"]]
            else:
                raise PipelineError(f"{name}: unsupported parameter source {sorted(source)}")
        for name, source in spec.get("inputs", {}).get("artifacts", {}).items():
            if "componentInputArtifact" in source:
                refs[name] = artifacts[source["componentInputArtifact"]]
            elif "taskOutputArtifact" in source:
                ref = source["taskOutputArtifact"]
                produced = done[ref["producerTask"]]
                if ref["outputArtifactKey"] not in produced.artifacts:
                    raise PipelineError(f"{name}: {ref['producerTask']} produced no artifact {ref['outputArtifactKey']} ({produced.status})")
                refs[name] = produced.artifacts[ref["outputArtifactKey"]]
            else:
                raise PipelineError(f"{name}: unsupported artifact source {sorted(source)}")
        return values, refs

    def run_node(self, name: str, spec: Dict[str, Any], parameters: Dict[str, Any], artifacts: Dict[str, Dict[str, str]], done: Dict[str, NodeResult], prefix: str, item: Any) -> NodeResult:
        values, refs = self.resolve(spec, parameters, artifacts, done)
        comp = leaf_component(self.ir, spec)
        path = f"{prefix}{name}"
        if "dag" not in comp:
            return self.run_leaf(name, path, spec, comp, values, refs, item)
        definitions = comp.get("inputDefinitions", {}).get("parameters", {})
        values = {k: coerce(v, definitions[k]["parameterType"]) if k in definitions else v for k, v in values.items()}
        condition = spec.get("triggerPolicy", {}).get("condition")
        if condition and not evaluate_condition(condition, values):
            self.log(f"[{path}] skipped: {condition} is false")
            self.mark(spec, path, name, "skipped", f"condition false: {condition}", item)
            return NodeResult("skipped")
        iterator = spec.get("parameterIterator")
        if not iterator:
            inner = self.run_dag(comp["dag"]["tasks"], values, refs, f"{path}/", item)
            return NodeResult("failed" if any(r.status in ("failed", "upstream-failed") for r in inner.values()) else "succeeded")
        if "raw" not in iterator.get("items", {}):
            raise PipelineError(f"{path}: minipipe loops over a constant list only")
        items = json.loads(iterator["items"]["raw"])
        limit = spec.get("iteratorPolicy", {}).get("parallelismLimit") or len(items)
        item_type = definitions.get(iterator["itemInput"], {}).get("parameterType", "STRING")

        def iteration(index: int, value: Any) -> Dict[str, NodeResult]:
            scope = dict(values, **{iterator["itemInput"]: coerce(value, item_type)})
            return self.run_dag(comp["dag"]["tasks"], scope, refs, f"{path}[{index}]/", value)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, limit)) as pool:
            outcomes = list(pool.map(lambda pair: iteration(*pair), enumerate(items)))
        failed = any(r.status in ("failed", "upstream-failed") for outcome in outcomes for r in outcome.values())
        return NodeResult("failed" if failed else "succeeded")

    def run_leaf(self, name: str, path: str, spec: Dict[str, Any], comp: Dict[str, Any], values: Dict[str, Any], refs: Dict[str, Dict[str, str]], item: Any) -> NodeResult:
        executor = self.ir["deploymentSpec"]["executors"][comp["executorLabel"]]["container"]
        definitions = comp.get("inputDefinitions", {}).get("parameters", {})
        params = {k: coerce(v, definitions[k]["parameterType"]) if k in definitions else v for k, v in values.items()}
        ingredients = {
            "component": {k: comp.get(k) for k in ("inputDefinitions", "outputDefinitions")},
            "container": executor,
            "parameters": params,
            "artifacts": {k: v["sha256"] for k, v in refs.items()},
        }
        key = sha256_text(canonical(ingredients))
        run = TaskRun(task=name, path=path, status="executed", item=item, cache_key=key, ingredients=ingredients, started=time.time())
        cache_enabled = bool(spec.get("cachingOptions", {}).get("enableCache"))
        if not self.use_cache:
            run.reason = "caching turned off for this run (--no-cache)"
        elif not cache_enabled:
            run.reason = "caching disabled for this task"
        else:
            entry = self.store.lookup(key)
            problem = "no cache entry for this key" if entry is None else self.store.verify(entry)
            if problem is None and entry is not None:
                run.status, run.reason = "cached", f"cache hit from run {entry['run_id']}"
                run.parameters, run.artifacts = entry["parameters"], entry["artifacts"]
                run.finished = time.time()
                return self.finish(run)
            run.reason = problem or ""
        retry = spec.get("retryPolicy", {})
        attempts = 1 + int(retry.get("maxRetryCount", 0))
        delay = parse_duration(retry.get("backoffDuration", "0s"))
        window = parse_duration(retry.get("backoffMaxDuration", "3600s"))
        factor = float(retry.get("backoffFactor", 2.0))
        workdir = self.store.root / "executions" / self.run_id / path.replace("/", "__")
        first = time.time()
        code = -1
        for attempt in range(1, attempts + 1):
            run.attempts = attempt
            shutil.rmtree(workdir, ignore_errors=True)
            workdir.mkdir(parents=True)
            executor_input = self.executor_input(comp, name, key, params, refs, workdir)
            for output in executor_input["outputs"].get("artifacts", {}).values():
                target = self.store.local(output["artifacts"][0]["uri"])
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
            label = re.sub(r"[^a-zA-Z0-9_.-]+", "-", f"{self.container_prefix}{self.run_id}-{path}-{attempt}")
            with self.slots:
                code, log = self.launch(executor, executor_input, label)
            run.log = log[-4000:]
            if code == 0:
                break
            self.log(f"[{path}] attempt {attempt}/{attempts} failed with exit code {code}")
            if attempt == attempts or time.time() - first + delay > window:
                break
            time.sleep(delay)
            delay *= factor
        if code != 0:
            run.status = "failed"
            run.reason = f"exit code {code} after {run.attempts} attempt(s)"
            run.finished = time.time()
            return self.finish(run)
        output = json.loads((workdir / "executor_output.json").read_text(encoding="utf-8"))
        run.parameters = output.get("parameterValues", {})
        for oname, group in output.get("artifacts", {}).items():
            artifact = group["artifacts"][0]
            actual = digest(self.store.local(artifact["uri"]))
            if actual != artifact["metadata"]["sha256"]:
                raise PipelineError(f"{path}: {oname} digest from the container {artifact['metadata']['sha256']} differs from the store {actual}")
            run.artifacts[oname] = {"uri": artifact["uri"], "sha256": actual}
        run.finished = time.time()
        self.store.remember(key, {"run_id": self.run_id, "task": path, "parameters": run.parameters, "artifacts": run.artifacts})
        return self.finish(run)

    def executor_input(self, comp: Dict[str, Any], name: str, key: str, params: Dict[str, Any], refs: Dict[str, Dict[str, str]], workdir: Path) -> Dict[str, Any]:
        outputs = comp.get("outputDefinitions", {})
        artifact_types = comp.get("inputDefinitions", {}).get("artifacts", {})
        return {
            "inputs": {
                "parameterValues": params,
                "artifacts": {n: {"artifacts": [{"name": n, "uri": r["uri"], "type": artifact_types.get(n, {}).get("artifactType", {}), "metadata": {"sha256": r["sha256"]}}]} for n, r in refs.items()},
            },
            "outputs": {
                "parameters": {n: {"outputFile": str(workdir / "parameters" / n)} for n in outputs.get("parameters", {})},
                "artifacts": {n: {"artifacts": [{"name": n, "uri": f"minipipe://artifacts/{name}/{key[:16]}/{n}", "type": d.get("artifactType", {})}]} for n, d in outputs.get("artifacts", {}).items()},
                "outputFile": str(workdir / "executor_output.json"),
            },
        }

    def launch(self, container: Dict[str, Any], executor_input: Dict[str, Any], label: str = "minipipe-step") -> Tuple[int, str]:
        payload = json.dumps(executor_input, sort_keys=True)
        args = [payload if a == "{{$}}" else a for a in container.get("args", [])]
        command = list(container.get("command", []))
        env = dict(os.environ, MINIPIPE_STORE=str(self.store.root))
        if self.runner == "subprocess":
            if command and command[0] in ("python3", "python"):
                command[0] = self.python
            argv = command + args
        else:
            user = [f"--user={os.getuid()}:{os.getgid()}"] if hasattr(os, "getuid") else []
            argv = [self.docker, "run", "--rm", f"--name={label}", "--network=none", *user, "-e", f"MINIPIPE_STORE={self.store.root}",
                    "-v", f"{self.store.root}:{self.store.root}", container["image"], *command, *args]
        try:
            done = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {self.timeout}s"
        except OSError as exc:
            return 127, str(exc)
        return done.returncode, done.stdout + done.stderr

    def finish(self, run: TaskRun) -> NodeResult:
        with self.lock:
            self.results.append(run)
        detail = run.reason if run.status != "executed" else f"{run.attempts} attempt(s)"
        self.log(f"[{run.path}] {run.status}: {detail}")
        if run.status == "failed":
            self.log(textwrap.indent(run.log.strip()[-2000:], "    "))
        if self.tracker:
            self.tracker.record(run)
        return NodeResult("failed" if run.status == "failed" else "succeeded", run.parameters, run.artifacts)


def argo_value(source: Dict[str, Any], in_root: bool) -> str:
    if "runtimeValue" in source:
        return text_value(source["runtimeValue"]["constant"])
    if "componentInputParameter" in source or "componentInputArtifact" in source:
        name = source.get("componentInputParameter") or source.get("componentInputArtifact")
        return f"{{{{workflow.parameters.{name}}}}}" if in_root else f"{{{{inputs.parameters.{name}}}}}"
    if "taskOutputParameter" in source:
        ref = source["taskOutputParameter"]
        return f"{{{{tasks.{ref['producerTask']}.outputs.parameters.{ref['outputParameterKey']}}}}}"
    if "taskOutputArtifact" in source:
        ref = source["taskOutputArtifact"]
        return f"{{{{tasks.{ref['producerTask']}.outputs.parameters.{ref['outputArtifactKey']}-uri}}}}"
    raise PipelineError(f"unsupported input source {sorted(source)}")


def argo_when(condition: str, sources: Dict[str, str], types_: Dict[str, str]) -> str:
    def replace(match: "re.Match[str]") -> str:
        name = match.group("name")
        if name not in sources:
            raise PipelineError(f"condition refers to {name}, which the group does not receive")
        return f"'{sources[name]}'" if types_.get(name) == "STRING" else sources[name]
    plain = re.sub(r"(?:int|double|float)\((-?[0-9.eE+-]+)\)", r"\1", condition)
    return re.sub(r"(?:(?:int|double|float)\()*inputs\.parameter_values\['(?P<name>[^']+)'\]\)*", replace, plain)


def to_argo(ir: Dict[str, Any], service_account: Optional[str] = None, storage: str = "64Mi", storage_class: Optional[str] = None) -> Dict[str, Any]:
    if not str(ir.get("sdkVersion", "")).startswith("minipipe"):
        raise PipelineError(f"the Argo emitter runs minipipe's own executor; {ir.get('sdkVersion')} IR needs the KFP backend")
    templates: List[Dict[str, Any]] = []
    out_dir = "/tmp/minipipe/out"

    def container_template(task_name: str, spec: Dict[str, Any], comp: Dict[str, Any]) -> str:
        executor = ir["deploymentSpec"]["executors"][comp["executorLabel"]]["container"]
        inputs = comp.get("inputDefinitions", {})
        outputs = comp.get("outputDefinitions", {})
        executor_input = {
            "inputs": {
                "parameterValues": {n: f"{{{{inputs.parameters.{n}}}}}" for n in spec.get("inputs", {}).get("parameters", {})},
                "artifacts": {n: {"artifacts": [{"name": n, "uri": f"{{{{inputs.parameters.{n}}}}}", "type": d.get("artifactType", {})}]}
                              for n, d in inputs.get("artifacts", {}).items()},
            },
            "outputs": {
                "parameters": {n: {"outputFile": f"{out_dir}/{n}"} for n in outputs.get("parameters", {})},
                "artifacts": {n: {"artifacts": [{"name": n, "uri": f"minipipe://artifacts/{{{{workflow.name}}}}/{{{{pod.name}}}}/{n}", "type": d.get("artifactType", {})}]}
                              for n, d in outputs.get("artifacts", {}).items()},
                "outputFile": "/tmp/minipipe/executor_output.json",
                "flatDir": out_dir,
            },
        }
        args = [json.dumps(executor_input, sort_keys=True) if a == "{{$}}" else a for a in executor.get("args", [])]
        output_parameters = [{"name": n, "valueFrom": {"path": f"{out_dir}/{n}"}} for n in outputs.get("parameters", {})]
        for n in outputs.get("artifacts", {}):
            output_parameters += [{"name": f"{n}-uri", "valueFrom": {"path": f"{out_dir}/{n}-uri"}}, {"name": f"{n}-sha256", "valueFrom": {"path": f"{out_dir}/{n}-sha256"}}]
        retry = spec.get("retryPolicy")
        template_name = comp["executorLabel"] + (f"-retry-{retry['maxRetryCount']}" if retry else "")
        if any(t["name"] == template_name for t in templates):
            return template_name
        template: Dict[str, Any] = {
            "name": template_name,
            "inputs": {"parameters": [{"name": n} for n in list(spec.get("inputs", {}).get("parameters", {})) + list(spec.get("inputs", {}).get("artifacts", {}))]},
            "outputs": {"parameters": output_parameters},
            "container": {
                "image": executor["image"], "command": executor["command"], "args": args,
                "env": [{"name": "MINIPIPE_STORE", "value": STORE_MOUNT}],
                "volumeMounts": [{"name": "minipipe-store", "mountPath": STORE_MOUNT}],
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}},
            },
        }
        if retry:
            factor = retry.get("backoffFactor", 2.0)
            template["retryStrategy"] = {
                "limit": retry["maxRetryCount"], "retryPolicy": "OnFailure",
                "backoff": {"duration": retry.get("backoffDuration", "0s"), "factor": int(factor) if float(factor).is_integer() else str(factor), "maxDuration": retry.get("backoffMaxDuration", "3600s")},
            }
        if not template["inputs"]["parameters"]:
            del template["inputs"]
        templates.append(template)
        return template_name

    def dag_template(template_name: str, tasks: Dict[str, Any], in_root: bool, input_names: List[str], parallelism: int = 0) -> None:
        dag_tasks: List[Dict[str, Any]] = []
        for name, spec in tasks.items():
            comp = leaf_component(ir, spec)
            arguments = [{"name": n, "value": argo_value(s, in_root)} for kind in ("parameters", "artifacts") for n, s in spec.get("inputs", {}).get(kind, {}).items()]
            entry: Dict[str, Any] = {"name": name}
            if spec.get("dependentTasks"):
                entry["dependencies"] = sorted(spec["dependentTasks"])
            if "dag" not in comp:
                entry["template"] = container_template(name, spec, comp)
            else:
                inner_inputs = list(comp.get("inputDefinitions", {}).get("parameters", {})) + list(comp.get("inputDefinitions", {}).get("artifacts", {}))
                body = spec["componentRef"]["name"].replace("comp-", "", 1)
                dag_template(body, comp["dag"]["tasks"], False, inner_inputs)
                entry["template"] = body
                condition = spec.get("triggerPolicy", {}).get("condition")
                if condition:
                    sources = {a["name"]: a["value"] for a in arguments}
                    types_ = {n: d["parameterType"] for n, d in comp.get("inputDefinitions", {}).get("parameters", {}).items()}
                    entry["when"] = argo_when(condition, sources, types_)
                iterator = spec.get("parameterIterator")
                if iterator:
                    if "raw" not in iterator.get("items", {}):
                        raise PipelineError(f"{name}: the Argo emitter loops over a constant list only")
                    items = json.loads(iterator["items"]["raw"])
                    limit = spec.get("iteratorPolicy", {}).get("parallelismLimit", 0)
                    looped = {"name": name, "template": body, "withItems": items,
                              "arguments": {"parameters": [{"name": a["name"], "value": f"{{{{inputs.parameters.{a['name']}}}}}"} for a in arguments] + [{"name": iterator["itemInput"], "value": "{{item}}"}]}}
                    if limit:
                        fanout = f"{body}-fanout"
                        templates.append({"name": fanout, "parallelism": limit, "inputs": {"parameters": [{"name": a["name"]} for a in arguments]}, "dag": {"tasks": [looped]}})
                        if not arguments:
                            del templates[-1]["inputs"]
                        entry["template"] = fanout
                    else:
                        entry["withItems"] = items
                        arguments = arguments + [{"name": iterator["itemInput"], "value": "{{item}}"}]
            if arguments:
                entry["arguments"] = {"parameters": arguments}
            dag_tasks.append(entry)
        template: Dict[str, Any] = {"name": template_name, "dag": {"tasks": dag_tasks}}
        if input_names:
            template["inputs"] = {"parameters": [{"name": n} for n in input_names]}
        if parallelism:
            template["parallelism"] = parallelism
        templates.insert(0, template) if in_root else templates.append(template)

    dag_template("root", ir["root"]["dag"]["tasks"], True, [])
    claim: Dict[str, Any] = {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": storage}}}
    if storage_class:
        claim["storageClassName"] = storage_class
    name = ir["pipelineInfo"]["name"]
    spec: Dict[str, Any] = {
        "entrypoint": "root",
        "arguments": {"parameters": [dict({"name": n}, **({"value": text_value(d["defaultValue"]) if d["parameterType"] != "NUMBER_INTEGER" else str(int(d["defaultValue"]))} if "defaultValue" in d else {}))
                                     for n, d in ir["root"].get("inputDefinitions", {}).get("parameters", {}).items()]},
        "volumeClaimTemplates": [{"metadata": {"name": "minipipe-store"}, "spec": claim}],
        "templates": templates,
    }
    if service_account:
        spec["serviceAccountName"] = service_account
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Workflow",
        "metadata": {"generateName": f"{name}-", "labels": {"minipipe/pipeline": name},
                     "annotations": {"minipipe/ir-sha256": sha256_text(canonical(ir))}},
        "spec": spec,
    }


def diff_values(old: Any, new: Any, path: str = "") -> List[str]:
    if isinstance(old, dict) and isinstance(new, dict):
        lines: List[str] = []
        for key in sorted(set(old) | set(new)):
            child = f"{path}.{key}" if path else str(key)
            if key not in old:
                lines.append(f"{child}: added {short(new[key])}")
            elif key not in new:
                lines.append(f"{child}: removed (was {short(old[key])})")
            else:
                lines += diff_values(old[key], new[key], child)
        return lines
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        return [line for i, (a, b) in enumerate(zip(old, new)) for line in diff_values(a, b, f"{path}[{i}]")]
    if old == new:
        return []
    if isinstance(old, str) and isinstance(new, str) and ("\n" in old or "\n" in new):
        changed = [l for l in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0) if l[:1] in "+-" and not l.startswith(("+++", "---"))]
        return [f"{path}: text changed ({len(changed)} line(s)): " + " | ".join(c.strip() for c in changed[:4])]
    return [f"{path}: {short(old)} -> {short(new)}"]


def short(value: Any) -> str:
    text = json.dumps(value, sort_keys=True)
    return text if len(text) <= 60 else text[:57] + "..."


def explain(store: Store, task: str) -> List[str]:
    history = [(run, t) for run in store.runs() for t in run["tasks"] if t["path"] == task or t["task"] == task]
    if not history:
        raise PipelineError(f"no run in {store.root} has a task {task!r}")
    run, latest = history[-1]
    lines = [f"{latest['path']} in run {run['run_id']}: {latest['status']} ({latest['reason'] or 'no reason recorded'})"]
    if latest["status"] in ("skipped", "upstream-failed"):
        return lines
    lines.append(f"cache key {latest['cache_key'][:16]}")
    earlier = [(r, t) for r, t in history[:-1] if t["path"] == latest["path"] and t["cache_key"]]
    if not earlier:
        lines.append("no earlier execution of this task to compare with")
        return lines
    previous_run, previous = earlier[-1]
    if previous["cache_key"] == latest["cache_key"]:
        lines.append(f"same key as in run {previous_run['run_id']}: the ingredients did not change")
        return lines
    lines.append(f"key differs from run {previous_run['run_id']} ({previous['cache_key'][:16]}) because:")
    lines += ["  " + line for line in diff_values(previous["ingredients"], latest["ingredients"])]
    return lines


def parse_params(pairs: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise PipelineError(f"--param needs NAME=VALUE, got {pair!r}")
        key, value = pair.split("=", 1)
        out[key] = value
    return out


def print_report(report: Dict[str, Any]) -> None:
    print(f"run {report['run_id']}  pipeline {report['pipeline']}  runner {report['runner']}  status {report['status']}")
    print(f"{'task':<34} {'status':<15} {'tries':>5}  {'key':<12}  outputs")
    for t in report["tasks"]:
        outputs = [f"{k}={text_value(v)}" for k, v in sorted(t["parameters"].items())]
        outputs += [f"{k}=sha256:{v['sha256'][-12:]}" for k, v in sorted(t["artifacts"].items())]
        print(f"{t['path']:<34} {t['status']:<15} {t['attempts']:>5}  {t['cache_key'][:12]:<12}  {' '.join(outputs) or t['reason']}")
    counts: Dict[str, int] = {}
    for t in report["tasks"]:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    print("summary: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="minipipe", description="Compile Python pipelines to a KFP-shaped IR, run them with a step cache, and emit Argo Workflows.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_compile = sub.add_parser("compile", help="compile a pipeline file to IR JSON")
    p_compile.add_argument("file", type=Path)
    p_compile.add_argument("--pipeline")
    p_compile.add_argument("-o", "--output", type=Path)
    p_dag = sub.add_parser("dag", help="list the leaf tasks of an IR (minipipe or kfp) with dependencies and wiring")
    p_dag.add_argument("file", type=Path)
    p_dag.add_argument("--pipeline")
    p_dag.add_argument("--json", action="store_true")
    p_run = sub.add_parser("run", help="run a pipeline locally with a step cache")
    p_run.add_argument("file", type=Path)
    p_run.add_argument("--pipeline")
    p_run.add_argument("--store", type=Path, required=True)
    p_run.add_argument("--param", action="append", default=[])
    p_run.add_argument("--runner", choices=["subprocess", "docker"], default="subprocess")
    p_run.add_argument("--no-cache", action="store_true")
    p_run.add_argument("--parallelism", type=int, default=4)
    p_run.add_argument("--tracking-uri", default=os.environ.get("MLFLOW_TRACKING_URI"))
    p_run.add_argument("--experiment", default=None)
    p_run.add_argument("--container-prefix", default="minipipe-")
    p_run.add_argument("--json", action="store_true")
    p_argo = sub.add_parser("argo", help="emit an Argo Workflows Workflow for an IR")
    p_argo.add_argument("file", type=Path)
    p_argo.add_argument("--pipeline")
    p_argo.add_argument("--service-account")
    p_argo.add_argument("--storage", default="64Mi")
    p_argo.add_argument("--storage-class")
    p_argo.add_argument("-o", "--output", type=Path)
    p_explain = sub.add_parser("explain", help="say why a task was or was not a cache hit")
    p_explain.add_argument("task")
    p_explain.add_argument("--store", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "explain":
            print("\n".join(explain(Store(args.store), args.task)))
            return 0
        ir = load_ir(args.file, args.pipeline)
        if args.command == "compile":
            text = json.dumps(ir, indent=2, sort_keys=True) + "\n"
            if args.output:
                args.output.write_text(text, encoding="utf-8")
                print(f"compiled {ir['pipelineInfo']['name']}: {len(flatten(ir))} tasks -> {args.output}")
            else:
                sys.stdout.write(text)
            return 0
        if args.command == "dag":
            rows = flatten(ir)
            if args.json:
                print(json.dumps(rows, indent=2, sort_keys=True))
                return 0
            for row in rows:
                extra = [f"if {row['condition']}"] if row["condition"] else []
                extra += [f"for each of {row['loop']}"] if row["loop"] else []
                extra += [f"retries {row['retries']}"] if row["retries"] else []
                extra += [] if row["cache"] else ["no cache"]
                print(f"{row['task']}  after [{', '.join(row['deps'])}]  {'; '.join(extra)}")
                for key, source in row["inputs"].items():
                    print(f"    {key} <- {source}")
                print(f"    -> {', '.join(row['outputs']) or '(nothing)'}")
            return 0
        if args.command == "argo":
            workflow = to_argo(ir, args.service_account, args.storage, args.storage_class)
            text = json.dumps(workflow, indent=2) + "\n"
            if args.output:
                args.output.write_text(text, encoding="utf-8")
                print(f"wrote Workflow {workflow['metadata']['generateName']}... with {len(workflow['spec']['templates'])} templates -> {args.output}")
            else:
                sys.stdout.write(text)
            return 0
        store = Store(args.store)
        log = (lambda line: print(line, file=sys.stderr))
        tracker = Tracker(args.tracking_uri, args.experiment or ir["pipelineInfo"]["name"], log) if args.tracking_uri else None
        runner = LocalRunner(ir, store, args.runner, not args.no_cache, args.parallelism, tracker, container_prefix=args.container_prefix)
        report = runner.run(parse_params(args.param))
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print_report(report)
        return 0 if report["status"] == "succeeded" else 1
    except PipelineError as exc:
        print(f"minipipe: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
