from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Tuple

NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
CPU_RE = re.compile(r"^([0-9]+m|[0-9]+(\.[0-9]+)?)$")
MEMORY_RE = re.compile(r"^[0-9]+(Ki|Mi|Gi)$")
FIELDS = ("name", "description", "web", "static", "port", "health", "env", "resources", "replicas")
RESOURCE_FIELDS = ("cpu", "memory")
MAX_NAME_LENGTH = 30
DEFAULT_RESOURCES = {"cpu": "100m", "memory": "128Mi"}
MANIFEST_FILE = "app.json"


def error(path: str, message: str) -> Dict[str, str]:
    return {"path": path, "message": message}


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def read_manifest(app_dir: str) -> Tuple[Any, List[Dict[str, str]]]:
    path = os.path.join(app_dir, MANIFEST_FILE)
    if not os.path.isfile(path):
        return None, [error("$", f"{MANIFEST_FILE} not found in {app_dir}")]
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), []
    except (ValueError, UnicodeDecodeError) as exc:
        return None, [error("$", f"{MANIFEST_FILE} is not valid JSON: {exc}")]


def check_name(data: Dict[str, Any]) -> List[Dict[str, str]]:
    if "name" not in data:
        return [error("$.name", "required: the app name, a DNS label such as my-app")]
    name = data["name"]
    if not isinstance(name, str) or not NAME_RE.match(name) or len(name) > MAX_NAME_LENGTH:
        return [error("$.name", f"must be a DNS label (lowercase letters, digits, '-') of at most {MAX_NAME_LENGTH} characters")]
    return []


def check_process(data: Dict[str, Any], app_dir: str) -> List[Dict[str, str]]:
    errors = []
    has_dockerfile = os.path.isfile(os.path.join(app_dir, "Dockerfile"))
    if "web" in data and (not isinstance(data["web"], str) or not data["web"].strip()):
        errors.append(error("$.web", "must be a non-empty command string"))
    if "static" in data:
        static = data["static"]
        if "web" in data:
            errors.append(error("$.static", "use either web or static, not both"))
        elif not isinstance(static, str) or os.path.isabs(static) or ".." in static.split("/"):
            errors.append(error("$.static", "must be a relative directory inside the app"))
        elif not os.path.isdir(os.path.join(app_dir, static)):
            errors.append(error("$.static", f"directory {static} does not exist"))
    if "web" not in data and "static" not in data and not has_dockerfile:
        errors.append(error("$.web", "needs a web command, a static directory or a Dockerfile"))
    return errors


def check_port_and_health(data: Dict[str, Any]) -> List[Dict[str, str]]:
    errors = []
    port = data.get("port")
    if not is_int(port) or not 1024 <= port <= 65535:
        errors.append(error("$.port", "required: an integer between 1024 and 65535"))
    health = data.get("health")
    if not isinstance(health, str) or not health.startswith("/"):
        errors.append(error("$.health", "required: an HTTP path starting with /"))
    return errors


def check_env(data: Dict[str, Any]) -> List[Dict[str, str]]:
    if "env" not in data:
        return []
    env = data["env"]
    if not isinstance(env, dict):
        return [error("$.env", "must be an object of NAME: value strings")]
    errors = []
    for key, value in env.items():
        if not ENV_KEY_RE.match(key):
            errors.append(error(f"$.env.{key}", "variable names are uppercase letters, digits and _"))
        elif not isinstance(value, str):
            errors.append(error(f"$.env.{key}", "values must be strings"))
    return errors


def check_resources(data: Dict[str, Any]) -> List[Dict[str, str]]:
    if "resources" not in data:
        return []
    resources = data["resources"]
    if not isinstance(resources, dict):
        return [error("$.resources", "must be an object with cpu and memory")]
    errors = [error(f"$.resources.{key}", "unknown resource (use cpu or memory)") for key in resources if key not in RESOURCE_FIELDS]
    cpu = resources.get("cpu")
    if "cpu" in resources and (not isinstance(cpu, str) or not CPU_RE.match(cpu)):
        errors.append(error("$.resources.cpu", "a CPU quantity such as 100m or 0.5"))
    memory = resources.get("memory")
    if "memory" in resources and (not isinstance(memory, str) or not MEMORY_RE.match(memory)):
        errors.append(error("$.resources.memory", "a memory quantity such as 64Mi or 1Gi"))
    return errors


def check_replicas(data: Dict[str, Any]) -> List[Dict[str, str]]:
    if "replicas" not in data:
        return []
    replicas = data["replicas"]
    if not is_int(replicas) or not 1 <= replicas <= 10:
        return [error("$.replicas", "an integer between 1 and 10")]
    return []


def validate(app_dir: str) -> Dict[str, Any]:
    data, errors = read_manifest(app_dir)
    if not errors and not isinstance(data, dict):
        errors = [error("$", f"{MANIFEST_FILE} must be a JSON object")]
    if not errors:
        errors.extend(error(f"$.{key}", "unknown field") for key in data if key not in FIELDS)
        if "description" in data and not isinstance(data["description"], str):
            errors.append(error("$.description", "must be a string"))
        for check in (check_name, check_port_and_health, check_env, check_resources, check_replicas):
            errors.extend(check(data))
        errors.extend(check_process(data, app_dir))
    return {
        "app_dir": os.path.abspath(app_dir),
        "valid": not errors,
        "errors": errors,
        "manifest": normalize(data, app_dir) if not errors else None,
    }


def normalize(data: Dict[str, Any], app_dir: str) -> Dict[str, Any]:
    resources = dict(DEFAULT_RESOURCES)
    resources.update(data.get("resources", {}))
    return {
        "name": data["name"],
        "port": data["port"],
        "health": data["health"],
        "env": dict(data.get("env", {})),
        "resources": resources,
        "replicas": data.get("replicas", 1),
        "web": data.get("web"),
        "static": data.get("static"),
        "dockerfile": os.path.isfile(os.path.join(app_dir, "Dockerfile")),
    }


def describe_errors(errors: List[Dict[str, str]]) -> str:
    return "\n".join(f"  {e['path']}: {e['message']}" for e in errors)

