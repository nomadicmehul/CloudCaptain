#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import functools
import getpass
import hashlib
import http.server
import json
import math
import os
import random
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

API_PREFIXES = ("/api/2.0/mlflow/", "/ajax-api/2.0/mlflow/")
ARTIFACTS_API = "/api/2.0/mlflow-artifacts/artifacts"
SERVER_INFO_API = "/api/3.0/mlflow/server-info"
ARTIFACT_SCHEME = "mlflow-artifacts:"
FEATURES = ["area_m2", "rooms", "age_years"]
TARGET = "price_100k"
TAG_RUN_NAME = "mlflow.runName"
TAG_PARENT = "mlflow.parentRunId"
TAG_USER = "mlflow.user"
TAG_SOURCE_NAME = "mlflow.source.name"
TAG_SOURCE_TYPE = "mlflow.source.type"
TAG_GIT_COMMIT = "mlflow.source.git.commit"
TAG_GIT_DIRTY = "git.dirty"
TAG_DATA_MD5 = "data.md5"
TAG_DATA_PATH = "data.path"
TAG_DATA_SOURCE = "data.version_source"
TERMINAL_STATUSES = ("FINISHED", "FAILED", "KILLED")
RUN_STATUSES = ("RUNNING", "SCHEDULED") + TERMINAL_STATUSES
MAX_RESULTS_LIMIT = 50000
NAME_WORDS = ("amber", "brisk", "calm", "deft", "eager", "fond", "glad", "keen", "lucid", "merry", "noble", "quiet")
NAME_NOUNS = ("otter", "heron", "lynx", "finch", "moth", "trout", "wren", "yak", "bison", "crane", "gecko", "koala")
KEY_PATTERN = re.compile(r"^[\w./\- :]+$")
STATUS_CODES = {
    "RESOURCE_DOES_NOT_EXIST": 404,
    "ENDPOINT_NOT_FOUND": 404,
    "RESOURCE_ALREADY_EXISTS": 400,
    "INVALID_PARAMETER_VALUE": 400,
    "INVALID_STATE": 400,
    "INTERNAL_ERROR": 500,
}


class MlflowError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def status(self) -> int:
        return STATUS_CODES.get(self.code, 400)

    def to_json(self) -> Dict[str, str]:
        return {"error_code": self.code, "message": self.message}


def invalid(message: str) -> MlflowError:
    return MlflowError("INVALID_PARAMETER_VALUE", message)


def not_found(message: str) -> MlflowError:
    return MlflowError("RESOURCE_DOES_NOT_EXIST", message)


def now_ms() -> int:
    return int(time.time() * 1000)


def kv_list(mapping: Dict[str, str]) -> List[Dict[str, str]]:
    return [{"key": key, "value": value} for key, value in mapping.items()]


def kv_dict(items: Optional[Iterable[Dict[str, Any]]]) -> Dict[str, str]:
    return {str(item["key"]): str(item.get("value", "")) for item in items or []}


def compact(entity: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in entity.items() if value not in (None, [], {})}


def validate_key(kind: str, key: str) -> None:
    if not key or not KEY_PATTERN.fullmatch(key) or ".." in key.split("/") or key.startswith("/"):
        raise invalid(f"Invalid {kind} name: {key!r}. Names may only contain alphanumerics, underscores (_), dashes (-), periods (.), spaces ( ), colons (:) and slashes (/).")


@dataclass
class Clause:
    kind: str
    key: str
    op: str
    value: Any


IDENTIFIER = re.compile(r'(?:(?P<prefix>[A-Za-z_]+)\.)?(?:`(?P<backtick>[^`]+)`|"(?P<quoted>[^"]+)"|(?P<bare>[\w.\-:/]+))')
COMPARATOR = re.compile(r"\s*(<=|>=|!=|=|<|>|(?i:ILIKE|LIKE)\b)")
VALUE = re.compile(r"""\s*(?:'(?P<single>[^']*)'|"(?P<double>[^"]*)"|(?P<number>-?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)(?![\w.]))""")
AND = re.compile(r"\s+(?i:AND)\s+")
ORDER = re.compile(r"^\s*(?P<ident>.+?)(?:\s+(?P<direction>(?i:ASC|DESC)))?\s*$")
PREFIXES = {
    "metric": "metric", "metrics": "metric",
    "param": "param", "params": "param", "parameter": "param", "parameters": "param",
    "tag": "tag", "tags": "tag",
    "attribute": "attribute", "attributes": "attribute", "attr": "attribute", "run": "attribute",
}
NUMERIC_OPS = ("=", "!=", "<", "<=", ">", ">=")
STRING_OPS = ("=", "!=", "LIKE", "ILIKE")
RUN_STRING_ATTRIBUTES = ("run_id", "run_name", "status", "user_id", "artifact_uri", "lifecycle_stage")
RUN_NUMERIC_ATTRIBUTES = ("start_time", "end_time")


def parse_identifier(text: str, pos: int) -> Tuple[str, str, int]:
    match = IDENTIFIER.match(text, pos)
    if not match:
        raise invalid(f"Invalid clause(s) in filter string: expected an identifier at {text[pos:pos + 20]!r}")
    prefix = match.group("prefix")
    key = match.group("backtick") or match.group("quoted") or match.group("bare")
    if prefix is None:
        return "attribute", key, match.end()
    kind = PREFIXES.get(prefix.lower())
    if kind is None:
        raise invalid(f"Invalid entity type '{prefix}'. Valid values are ['metric', 'parameter', 'tag', 'attribute']")
    return kind, key, match.end()


def parse_filter(text: Optional[str], numeric_attributes: Sequence[str] = RUN_NUMERIC_ATTRIBUTES, string_attributes: Sequence[str] = RUN_STRING_ATTRIBUTES) -> List[Clause]:
    if text is None or not text.strip():
        return []
    clauses: List[Clause] = []
    pos = len(text) - len(text.lstrip())
    while True:
        kind, key, pos = parse_identifier(text, pos)
        op_match = COMPARATOR.match(text, pos)
        if not op_match:
            raise invalid(f"Invalid clause(s) in filter string: no comparator after {key!r}")
        op = op_match.group(1).upper()
        value_match = VALUE.match(text, op_match.end())
        if not value_match:
            raise invalid(f"Invalid clause(s) in filter string: no value after '{key} {op}'")
        clauses.append(typed_clause(kind, key, op, value_match, numeric_attributes, string_attributes))
        pos = value_match.end()
        if text[pos:].strip() == "":
            return clauses
        and_match = AND.match(text, pos)
        if not and_match:
            raise invalid(f"Invalid clause(s) in filter string: {text[pos:].strip().split()[0]!r}; only AND joins clauses")
        pos = and_match.end()


def typed_clause(kind: str, key: str, op: str, value_match: "re.Match[str]", numeric_attributes: Sequence[str], string_attributes: Sequence[str]) -> Clause:
    number = value_match.group("number")
    string = value_match.group("single") if value_match.group("single") is not None else value_match.group("double")
    numeric = kind == "metric" or (kind == "attribute" and key in numeric_attributes)
    if kind == "attribute" and key not in numeric_attributes and key not in string_attributes:
        raise invalid(f"Invalid attribute key '{key}' specified. Valid keys are {sorted(list(numeric_attributes) + list(string_attributes))}")
    if numeric:
        if op not in NUMERIC_OPS:
            raise invalid(f"Invalid comparator '{op}' not one of {list(NUMERIC_OPS)}")
        if number is None:
            raise invalid(f"Expected numeric value type for {kind}. Found {string!r}")
        return Clause(kind, key, op, float(number))
    if op not in STRING_OPS:
        raise invalid(f"Invalid comparator '{op}' not one of {list(STRING_OPS)}")
    if string is None:
        raise invalid(f"Expected a quoted string value for {kind} (e.g. 'my-value'). Got value {number}")
    return Clause(kind, key, op, string)


def like_to_regex(pattern: str, ignore_case: bool) -> "re.Pattern[str]":
    body = "".join(".*" if char == "%" else "." if char == "_" else re.escape(char) for char in pattern)
    return re.compile(body, re.IGNORECASE | re.DOTALL if ignore_case else re.DOTALL)


def compare(actual: Any, op: str, expected: Any) -> bool:
    if actual is None:
        return False
    if op in ("LIKE", "ILIKE"):
        return like_to_regex(expected, op == "ILIKE").fullmatch(str(actual)) is not None
    if op == "=":
        return actual == expected
    if op == "!=":
        return actual != expected
    if op == "<":
        return actual < expected
    if op == "<=":
        return actual <= expected
    if op == ">":
        return actual > expected
    return actual >= expected


@dataclass
class OrderKey:
    kind: str
    key: str
    ascending: bool


def parse_order_by(items: Optional[Sequence[str]], allowed_attributes: Sequence[str]) -> List[OrderKey]:
    keys: List[OrderKey] = []
    seen = set()
    for item in items or []:
        match = ORDER.match(item)
        if not match:
            raise invalid(f"Invalid order_by clause '{item}'")
        kind, key, end = parse_identifier(match.group("ident"), 0)
        if match.group("ident")[end:].strip():
            raise invalid(f"Invalid order_by clause '{item}'")
        if kind == "attribute" and key not in allowed_attributes:
            raise invalid(f"Invalid order_by attribute '{key}'. Valid keys are {sorted(allowed_attributes)}")
        if (kind, key) in seen:
            raise invalid(f"`order_by` contains duplicate fields: {list(items or [])}")
        seen.add((kind, key))
        keys.append(OrderKey(kind, key, (match.group("direction") or "ASC").upper() == "ASC"))
    return keys


def ordering(keys: List[OrderKey], value_of: Callable[[Any, OrderKey], Any]) -> Callable[[Any, Any], int]:
    def compare_rows(left: Any, right: Any) -> int:
        for key in keys:
            a, b = value_of(left, key), value_of(right, key)
            if a is None or b is None:
                if a is None and b is None:
                    continue
                return 1 if a is None else -1
            if a == b:
                continue
            result = -1 if a < b else 1
            return result if key.ascending else -result
        return 0
    return compare_rows


def page_token(offset: int) -> str:
    return base64.b64encode(json.dumps({"offset": offset}).encode()).decode()


def page_offset(token: Optional[str]) -> int:
    if not token:
        return 0
    try:
        offset = json.loads(base64.b64decode(token))["offset"]
    except (ValueError, KeyError, TypeError) as exc:
        raise invalid(f"Invalid page token {token!r}: {exc}") from None
    if not isinstance(offset, int) or offset < 0:
        raise invalid(f"Invalid page token {token!r}")
    return offset


def paginate(items: List[Any], max_results: Any, token: Optional[str], default: int = 1000) -> Tuple[List[Any], Optional[str]]:
    limit = int(max_results) if max_results not in (None, "") else default
    if limit < 1 or limit > MAX_RESULTS_LIMIT:
        raise invalid(f"Invalid value {limit} for parameter 'max_results' supplied. It must be between 1 and {MAX_RESULTS_LIMIT}.")
    start = page_offset(token)
    end = start + limit
    return items[start:end], page_token(end) if end < len(items) else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (experiment_id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, artifact_location TEXT NOT NULL, lifecycle_stage TEXT NOT NULL, creation_time INTEGER, last_update_time INTEGER);
CREATE TABLE IF NOT EXISTS experiment_tags (experiment_id INTEGER, key TEXT, value TEXT, PRIMARY KEY (experiment_id, key));
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, experiment_id INTEGER NOT NULL, run_name TEXT, user_id TEXT, status TEXT, start_time INTEGER, end_time INTEGER, artifact_uri TEXT, lifecycle_stage TEXT);
CREATE TABLE IF NOT EXISTS params (run_id TEXT, key TEXT, value TEXT, PRIMARY KEY (run_id, key));
CREATE TABLE IF NOT EXISTS tags (run_id TEXT, key TEXT, value TEXT, PRIMARY KEY (run_id, key));
CREATE TABLE IF NOT EXISTS metrics (run_id TEXT, key TEXT, value REAL, timestamp INTEGER, step INTEGER, is_nan INTEGER, PRIMARY KEY (run_id, key, timestamp, step, value, is_nan));
CREATE TABLE IF NOT EXISTS latest_metrics (run_id TEXT, key TEXT, value REAL, timestamp INTEGER, step INTEGER, is_nan INTEGER, PRIMARY KEY (run_id, key));
CREATE TABLE IF NOT EXISTS registered_models (name TEXT PRIMARY KEY, description TEXT, creation_timestamp INTEGER, last_updated_timestamp INTEGER);
CREATE TABLE IF NOT EXISTS registered_model_tags (name TEXT, key TEXT, value TEXT, PRIMARY KEY (name, key));
CREATE TABLE IF NOT EXISTS model_versions (name TEXT, version INTEGER, source TEXT, run_id TEXT, status TEXT, description TEXT, run_link TEXT, creation_timestamp INTEGER, last_updated_timestamp INTEGER, PRIMARY KEY (name, version));
CREATE TABLE IF NOT EXISTS model_version_tags (name TEXT, version INTEGER, key TEXT, value TEXT, PRIMARY KEY (name, version, key));
CREATE TABLE IF NOT EXISTS registered_model_aliases (name TEXT, alias TEXT, version INTEGER, PRIMARY KEY (name, alias));
"""


def metric_json(row: sqlite3.Row) -> Dict[str, Any]:
    value = float("nan") if row["is_nan"] else row["value"]
    return {"key": row["key"], "value": value, "timestamp": row["timestamp"], "step": row["step"]}


class Store:
    def __init__(self, db_path: str, artifact_root: str, clock: Callable[[], int] = now_ms):
        self.artifact_root = Path(artifact_root)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.lock = threading.RLock()
        self.db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if self.db.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 0:
            stamp = self.clock()
            self.db.execute("INSERT INTO experiments VALUES (0, 'Default', ?, 'active', ?, ?)", (f"{ARTIFACT_SCHEME}/0", stamp, stamp))

    def close(self) -> None:
        self.db.close()

    def transaction(self) -> "Transaction":
        return Transaction(self)

    def experiment_row(self, experiment_id: Any) -> sqlite3.Row:
        try:
            number = int(experiment_id)
        except (TypeError, ValueError):
            raise invalid(f"Invalid experiment ID {experiment_id!r}") from None
        row = self.db.execute("SELECT * FROM experiments WHERE experiment_id = ?", (number,)).fetchone()
        if row is None:
            raise not_found(f"No Experiment with id={experiment_id} exists")
        return row

    def experiment_json(self, row: sqlite3.Row) -> Dict[str, Any]:
        tags = {r["key"]: r["value"] for r in self.db.execute("SELECT key, value FROM experiment_tags WHERE experiment_id = ? ORDER BY rowid", (row["experiment_id"],))}
        return compact({
            "experiment_id": str(row["experiment_id"]),
            "name": row["name"],
            "artifact_location": row["artifact_location"],
            "lifecycle_stage": row["lifecycle_stage"],
            "last_update_time": row["last_update_time"],
            "creation_time": row["creation_time"],
            "tags": kv_list(tags),
        })

    def create_experiment(self, name: str, tags: Dict[str, str], artifact_location: Optional[str] = None) -> str:
        if not name:
            raise invalid("Invalid experiment name: ''")
        for key in tags:
            validate_key("tag", key)
        with self.transaction():
            if self.db.execute("SELECT 1 FROM experiments WHERE name = ?", (name,)).fetchone():
                raise MlflowError("RESOURCE_ALREADY_EXISTS", f"Experiment(name={name}) already exists.")
            stamp = self.clock()
            cursor = self.db.execute("INSERT INTO experiments (name, artifact_location, lifecycle_stage, creation_time, last_update_time) VALUES (?, '', 'active', ?, ?)", (name, stamp, stamp))
            experiment_id = cursor.lastrowid
            location = artifact_location or f"{ARTIFACT_SCHEME}/{experiment_id}"
            self.db.execute("UPDATE experiments SET artifact_location = ? WHERE experiment_id = ?", (location, experiment_id))
            self.db.executemany("INSERT INTO experiment_tags VALUES (?, ?, ?)", [(experiment_id, k, v) for k, v in tags.items()])
        return str(experiment_id)

    def get_experiment(self, experiment_id: Any) -> Dict[str, Any]:
        with self.lock:
            return self.experiment_json(self.experiment_row(experiment_id))

    def get_experiment_by_name(self, name: str) -> Dict[str, Any]:
        with self.lock:
            row = self.db.execute("SELECT * FROM experiments WHERE name = ?", (name,)).fetchone()
            if row is None:
                raise not_found(f"Could not find experiment with name '{name}'")
            return self.experiment_json(row)

    def set_experiment_tag(self, experiment_id: Any, key: str, value: str) -> None:
        validate_key("tag", key)
        with self.transaction():
            self.experiment_row(experiment_id)
            self.db.execute("INSERT OR REPLACE INTO experiment_tags VALUES (?, ?, ?)", (int(experiment_id), key, value))

    def search_experiments(self, filter_string: Optional[str], order_by: Optional[Sequence[str]], max_results: Any, token: Optional[str], view_type: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        clauses = parse_filter(filter_string, numeric_attributes=("creation_time", "last_update_time"), string_attributes=("name",))
        for clause in clauses:
            if clause.kind not in ("attribute", "tag"):
                raise invalid(f"Invalid entity type '{clause.kind}' for experiments. Valid values are ['attribute', 'tag']")
        keys = parse_order_by(order_by or ["creation_time DESC", "experiment_id ASC"], ("name", "experiment_id", "creation_time", "last_update_time"))
        if not any(key.key == "experiment_id" for key in keys):
            keys.append(OrderKey("attribute", "experiment_id", True))
        with self.lock:
            experiments = [self.experiment_json(row) for row in self.db.execute("SELECT * FROM experiments")]
        stages = {"ACTIVE_ONLY": ("active",), "DELETED_ONLY": ("deleted",), "ALL": ("active", "deleted")}.get(view_type or "ACTIVE_ONLY", ("active",))

        def field_value(experiment: Dict[str, Any], kind: str, key: str) -> Any:
            if kind == "tag":
                return kv_dict(experiment.get("tags")).get(key)
            if key == "experiment_id":
                return int(experiment["experiment_id"])
            return experiment.get(key)

        selected = [e for e in experiments if e["lifecycle_stage"] in stages and all(compare(field_value(e, c.kind, c.key), c.op, c.value) for c in clauses)]
        selected.sort(key=functools.cmp_to_key(ordering(keys, lambda e, k: field_value(e, k.kind, k.key))))
        return paginate(selected, max_results, token)

    def run_row(self, run_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise not_found(f"Run with id={run_id} not found")
        return row

    def run_json(self, row: sqlite3.Row) -> Dict[str, Any]:
        run_id = row["run_id"]
        info = compact({
            "run_uuid": run_id,
            "experiment_id": str(row["experiment_id"]),
            "run_name": row["run_name"],
            "user_id": row["user_id"],
            "status": row["status"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "artifact_uri": row["artifact_uri"],
            "lifecycle_stage": row["lifecycle_stage"],
            "run_id": run_id,
        })
        metrics = [metric_json(r) for r in self.db.execute("SELECT * FROM latest_metrics WHERE run_id = ? ORDER BY rowid", (run_id,))]
        params = [{"key": r["key"], "value": r["value"]} for r in self.db.execute("SELECT key, value FROM params WHERE run_id = ? ORDER BY rowid", (run_id,))]
        tags = [{"key": r["key"], "value": r["value"]} for r in self.db.execute("SELECT key, value FROM tags WHERE run_id = ? ORDER BY rowid", (run_id,))]
        return {"info": info, "data": compact({"metrics": metrics, "params": params, "tags": tags})}

    def create_run(self, experiment_id: Any, run_name: Optional[str], user_id: str, start_time: Optional[int], tags: Dict[str, str]) -> Dict[str, Any]:
        for key in tags:
            validate_key("tag", key)
        with self.transaction():
            experiment = self.experiment_row(experiment_id)
            if experiment["lifecycle_stage"] != "active":
                raise MlflowError("INVALID_STATE", f"The experiment {experiment_id} must be in the 'active' state.")
            name = run_name or tags.get(TAG_RUN_NAME) or random_run_name()
            if tags.get(TAG_RUN_NAME, name) != name:
                raise invalid(f"Both 'run_name' argument and 'mlflow.runName' tag are specified, but with different values (run_name='{name}', mlflow.runName='{tags[TAG_RUN_NAME]}').")
            run_id = uuid.uuid4().hex
            artifact_uri = f"{experiment['artifact_location'].rstrip('/')}/{run_id}/artifacts"
            self.db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, 'RUNNING', ?, NULL, ?, 'active')",
                (run_id, experiment["experiment_id"], name, user_id, start_time or self.clock(), artifact_uri),
            )
            merged = dict(tags)
            merged.setdefault(TAG_RUN_NAME, name)
            self.db.executemany("INSERT INTO tags VALUES (?, ?, ?)", [(run_id, k, v) for k, v in merged.items()])
            return self.run_json(self.run_row(run_id))

    def get_run(self, run_id: str) -> Dict[str, Any]:
        with self.lock:
            return self.run_json(self.run_row(run_id))

    def update_run(self, run_id: str, status: Optional[str], end_time: Optional[int], run_name: Optional[str]) -> Dict[str, Any]:
        if status and status not in RUN_STATUSES:
            raise invalid(f"Invalid run status {status!r}; expected one of {list(RUN_STATUSES)}")
        with self.transaction():
            row = self.active_run(run_id)
            self.db.execute(
                "UPDATE runs SET status = ?, end_time = ?, run_name = ? WHERE run_id = ?",
                (status or row["status"], end_time if end_time is not None else row["end_time"], run_name or row["run_name"], run_id),
            )
            if run_name:
                self.db.execute("INSERT OR REPLACE INTO tags VALUES (?, ?, ?)", (run_id, TAG_RUN_NAME, run_name))
            return self.run_json(self.run_row(run_id))["info"]

    def active_run(self, run_id: str) -> sqlite3.Row:
        row = self.run_row(run_id)
        if row["lifecycle_stage"] != "active":
            raise MlflowError("INVALID_STATE", f"The run {run_id} must be in the 'active' state. Current state is {row['lifecycle_stage']}.")
        return row

    def log_batch(self, run_id: str, metrics: Sequence[Dict[str, Any]] = (), params: Sequence[Dict[str, Any]] = (), tags: Sequence[Dict[str, Any]] = ()) -> None:
        if len(metrics) > 1000 or len(params) > 100 or len(tags) > 100:
            raise invalid("A batch may hold at most 1000 metrics, 100 params and 100 tags")
        for kind, items in (("metric", metrics), ("param", params), ("tag", tags)):
            for item in items:
                validate_key(kind, str(item.get("key", "")))
        with self.transaction():
            self.active_run(run_id)
            for param in params:
                key, value = str(param["key"]), str(param.get("value", ""))
                existing = self.db.execute("SELECT value FROM params WHERE run_id = ? AND key = ?", (run_id, key)).fetchone()
                if existing is not None and existing["value"] != value:
                    raise invalid(f"Changing param values is not allowed. Param with key='{key}' was already logged with value='{existing['value']}' for run ID='{run_id}'. Attempted logging new value '{value}'.")
                if existing is None:
                    self.db.execute("INSERT INTO params VALUES (?, ?, ?)", (run_id, key, value))
            for metric in metrics:
                self.insert_metric(run_id, metric)
            for tag in tags:
                self.db.execute("INSERT OR REPLACE INTO tags VALUES (?, ?, ?)", (run_id, str(tag["key"]), str(tag.get("value", ""))))

    def insert_metric(self, run_id: str, metric: Dict[str, Any]) -> None:
        try:
            value = float(metric["value"])
        except (KeyError, TypeError, ValueError):
            raise invalid(f"Got invalid value {metric.get('value')!r} for metric {metric.get('key')!r}") from None
        if "timestamp" not in metric:
            raise invalid(f"Missing value for required parameter 'timestamp' in metric {metric.get('key')!r}")
        is_nan = math.isnan(value)
        stored = 0.0 if is_nan else value
        key, timestamp, step = str(metric["key"]), int(metric["timestamp"]), int(metric.get("step") or 0)
        self.db.execute("INSERT OR IGNORE INTO metrics VALUES (?, ?, ?, ?, ?, ?)", (run_id, key, stored, timestamp, step, int(is_nan)))
        latest = self.db.execute("SELECT step, timestamp, value FROM latest_metrics WHERE run_id = ? AND key = ?", (run_id, key)).fetchone()
        if latest is None:
            self.db.execute("INSERT INTO latest_metrics VALUES (?, ?, ?, ?, ?, ?)", (run_id, key, stored, timestamp, step, int(is_nan)))
        elif (step, timestamp, stored) > (latest["step"], latest["timestamp"], latest["value"]):
            self.db.execute("UPDATE latest_metrics SET value = ?, timestamp = ?, step = ?, is_nan = ? WHERE run_id = ? AND key = ?", (stored, timestamp, step, int(is_nan), run_id, key))

    def get_metric_history(self, run_id: str, key: str) -> List[Dict[str, Any]]:
        with self.lock:
            self.run_row(run_id)
            rows = self.db.execute("SELECT * FROM metrics WHERE run_id = ? AND key = ? ORDER BY timestamp, step, value", (run_id, key))
            return [metric_json(row) for row in rows]

    def search_runs(self, experiment_ids: Sequence[Any], filter_string: Optional[str], order_by: Optional[Sequence[str]], max_results: Any, token: Optional[str], view_type: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        clauses = parse_filter(filter_string)
        keys = parse_order_by(order_by, RUN_STRING_ATTRIBUTES + RUN_NUMERIC_ATTRIBUTES)
        if not any(k.kind == "attribute" and k.key == "start_time" for k in keys):
            keys.append(OrderKey("attribute", "start_time", False))
        keys.append(OrderKey("attribute", "run_id", True))
        stages = {"ACTIVE_ONLY": ("active",), "DELETED_ONLY": ("deleted",), "ALL": ("active", "deleted")}.get(view_type or "ACTIVE_ONLY", ("active",))
        with self.lock:
            ids = [int(self.experiment_row(e)["experiment_id"]) for e in experiment_ids]
            placeholders = ",".join("?" for _ in ids) or "NULL"
            rows = self.db.execute(f"SELECT * FROM runs WHERE experiment_id IN ({placeholders})", ids).fetchall()
            runs = [self.run_json(row) for row in rows if row["lifecycle_stage"] in stages]
        selected = [run for run in runs if all(compare(run_value(run, c.kind, c.key), c.op, c.value) for c in clauses)]
        selected.sort(key=functools.cmp_to_key(ordering(keys, lambda run, k: run_value(run, k.kind, k.key))))
        return paginate(selected, max_results, token)

    def registered_model_row(self, name: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM registered_models WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise not_found(f"Registered Model with name={name} not found")
        return row

    def aliases(self, name: str, version: Optional[int] = None) -> List[sqlite3.Row]:
        if version is None:
            return self.db.execute("SELECT alias, version FROM registered_model_aliases WHERE name = ? ORDER BY alias", (name,)).fetchall()
        return self.db.execute("SELECT alias, version FROM registered_model_aliases WHERE name = ? AND version = ? ORDER BY alias", (name, version)).fetchall()

    def registered_model_json(self, row: sqlite3.Row) -> Dict[str, Any]:
        name = row["name"]
        latest = self.db.execute("SELECT * FROM model_versions WHERE name = ? ORDER BY version DESC LIMIT 1", (name,)).fetchone()
        tags = {r["key"]: r["value"] for r in self.db.execute("SELECT key, value FROM registered_model_tags WHERE name = ? ORDER BY rowid", (name,))}
        return compact({
            "name": name,
            "creation_timestamp": row["creation_timestamp"],
            "last_updated_timestamp": row["last_updated_timestamp"],
            "description": row["description"],
            "latest_versions": [self.model_version_json(latest, with_aliases=False)] if latest else [],
            "tags": kv_list(tags),
            "aliases": [{"alias": a["alias"], "version": str(a["version"])} for a in self.aliases(name)],
        })

    def model_version_json(self, row: sqlite3.Row, with_aliases: bool = True) -> Dict[str, Any]:
        tags = {r["key"]: r["value"] for r in self.db.execute("SELECT key, value FROM model_version_tags WHERE name = ? AND version = ? ORDER BY rowid", (row["name"], row["version"]))}
        entity = {
            "name": row["name"],
            "version": str(row["version"]),
            "creation_timestamp": row["creation_timestamp"],
            "last_updated_timestamp": row["last_updated_timestamp"],
            "current_stage": "None",
            "description": row["description"] or "",
            "source": row["source"],
            "run_id": row["run_id"],
            "status": row["status"],
            "run_link": row["run_link"] or "",
            "tags": kv_list(tags),
        }
        if with_aliases:
            entity["aliases"] = [a["alias"] for a in self.aliases(row["name"], row["version"])]
        return {key: value for key, value in entity.items() if value not in (None, [])}

    def create_registered_model(self, name: str, tags: Dict[str, str], description: Optional[str]) -> Dict[str, Any]:
        if not name:
            raise invalid("Registered model name cannot be empty.")
        for key in tags:
            validate_key("tag", key)
        with self.transaction():
            if self.db.execute("SELECT 1 FROM registered_models WHERE name = ?", (name,)).fetchone():
                raise MlflowError("RESOURCE_ALREADY_EXISTS", f"Registered Model (name={name}) already exists.")
            stamp = self.clock()
            self.db.execute("INSERT INTO registered_models VALUES (?, ?, ?, ?)", (name, description or "", stamp, stamp))
            self.db.executemany("INSERT INTO registered_model_tags VALUES (?, ?, ?)", [(name, k, v) for k, v in tags.items()])
            return self.registered_model_json(self.registered_model_row(name))

    def get_registered_model(self, name: str) -> Dict[str, Any]:
        with self.lock:
            return self.registered_model_json(self.registered_model_row(name))

    def search_registered_models(self, filter_string: Optional[str], max_results: Any, token: Optional[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        clauses = parse_filter(filter_string, numeric_attributes=(), string_attributes=("name",))
        with self.lock:
            models = [self.registered_model_json(row) for row in self.db.execute("SELECT * FROM registered_models ORDER BY name")]
        selected = [m for m in models if all(compare(kv_dict(m.get("tags")).get(c.key) if c.kind == "tag" else m.get(c.key), c.op, c.value) for c in clauses)]
        return paginate(selected, max_results, token, default=100)

    def set_registered_model_tag(self, name: str, key: str, value: str) -> None:
        validate_key("tag", key)
        with self.transaction():
            self.registered_model_row(name)
            self.db.execute("INSERT OR REPLACE INTO registered_model_tags VALUES (?, ?, ?)", (name, key, value))

    def validate_source(self, source: str, run_id: Optional[str]) -> None:
        parsed = urllib.parse.urlparse(source)
        if ".." in parsed.path.split("/"):
            raise invalid(f"Invalid model version source: '{source}'. Relative path segments are not allowed.")
        if parsed.scheme == "runs":
            source_run = parsed.path.lstrip("/").split("/", 1)[0]
            if parsed.netloc or not source_run or (run_id and source_run != run_id):
                raise invalid(f"Invalid model version source: '{source}'. The run ID in the source must match the run_id request parameter.")
            self.run_row(source_run)
            return
        if parsed.scheme in ("", "file"):
            raise invalid(f"Invalid model version source: '{source}'. A local path is only accepted inside the artifact directory of the run given by run_id.")
        if run_id:
            artifact_uri = self.run_row(run_id)["artifact_uri"]
            if parsed.scheme == "mlflow-artifacts" and not source.startswith(artifact_uri.rstrip("/") + "/"):
                raise invalid(f"Invalid model version source: '{source}' is not inside the artifact location of run {run_id} ({artifact_uri}).")

    def create_model_version(self, name: str, source: str, run_id: Optional[str], tags: Dict[str, str], run_link: Optional[str], description: Optional[str]) -> Dict[str, Any]:
        if not source:
            raise invalid("Missing value for required parameter 'source'.")
        for key in tags:
            validate_key("tag", key)
        with self.transaction():
            self.registered_model_row(name)
            self.validate_source(source, run_id)
            version = self.db.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM model_versions WHERE name = ?", (name,)).fetchone()[0]
            stamp = self.clock()
            self.db.execute("INSERT INTO model_versions VALUES (?, ?, ?, ?, 'READY', ?, ?, ?, ?)", (name, version, source, run_id or "", description or "", run_link or "", stamp, stamp))
            self.db.executemany("INSERT INTO model_version_tags VALUES (?, ?, ?, ?)", [(name, version, k, v) for k, v in tags.items()])
            self.db.execute("UPDATE registered_models SET last_updated_timestamp = ? WHERE name = ?", (stamp, name))
            return self.model_version_json(self.model_version_row(name, version))

    def model_version_row(self, name: str, version: Any) -> sqlite3.Row:
        try:
            number = int(version)
        except (TypeError, ValueError):
            raise invalid(f"Parameter 'version' must be an integer, got {version!r}.") from None
        row = self.db.execute("SELECT * FROM model_versions WHERE name = ? AND version = ?", (name, number)).fetchone()
        if row is None:
            raise not_found(f"Model Version (name={name}, version={version}) not found")
        return row

    def get_model_version(self, name: str, version: Any) -> Dict[str, Any]:
        with self.lock:
            return self.model_version_json(self.model_version_row(name, version))

    def set_model_version_tag(self, name: str, version: Any, key: str, value: str) -> None:
        validate_key("tag", key)
        with self.transaction():
            row = self.model_version_row(name, version)
            self.db.execute("INSERT OR REPLACE INTO model_version_tags VALUES (?, ?, ?, ?)", (name, row["version"], key, value))

    def search_model_versions(self, filter_string: Optional[str], order_by: Optional[Sequence[str]], max_results: Any, token: Optional[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        clauses = parse_filter(filter_string, numeric_attributes=("version_number",), string_attributes=("name", "run_id", "source_path"))
        keys = parse_order_by(order_by or ["last_updated_timestamp DESC", "name ASC", "version_number DESC"], ("name", "version_number", "creation_timestamp", "last_updated_timestamp"))
        with self.lock:
            versions = [self.model_version_json(row, with_aliases=False) for row in self.db.execute("SELECT * FROM model_versions")]

        def field_value(entity: Dict[str, Any], kind: str, key: str) -> Any:
            if kind == "tag":
                return kv_dict(entity.get("tags")).get(key)
            if key == "version_number":
                return int(entity["version"])
            if key == "source_path":
                return entity.get("source")
            return entity.get(key)

        selected = [v for v in versions if all(compare(field_value(v, c.kind, c.key), c.op, c.value) for c in clauses)]
        selected.sort(key=functools.cmp_to_key(ordering(keys, lambda v, k: field_value(v, k.kind, k.key))))
        return paginate(selected, max_results, token, default=10000)

    def set_alias(self, name: str, alias: str, version: Any) -> None:
        if not alias or alias.lower() == "latest" or re.fullmatch(r"[vV]\d+", alias):
            raise invalid(f"Invalid alias name: '{alias}'. Aliases cannot be 'latest' or look like a version ('v1'), because models:/{name}/latest and models:/{name}/1 already mean something.")
        validate_key("alias", alias)
        with self.transaction():
            self.registered_model_row(name)
            row = self.model_version_row(name, version)
            self.db.execute("INSERT OR REPLACE INTO registered_model_aliases VALUES (?, ?, ?)", (name, alias, row["version"]))
            self.db.execute("UPDATE registered_models SET last_updated_timestamp = ? WHERE name = ?", (self.clock(), name))

    def delete_alias(self, name: str, alias: str) -> None:
        with self.transaction():
            self.registered_model_row(name)
            self.db.execute("DELETE FROM registered_model_aliases WHERE name = ? AND alias = ?", (name, alias))

    def get_model_version_by_alias(self, name: str, alias: str) -> Dict[str, Any]:
        with self.lock:
            self.registered_model_row(name)
            row = self.db.execute("SELECT version FROM registered_model_aliases WHERE name = ? AND alias = ?", (name, alias)).fetchone()
            if row is None:
                raise invalid(f"Registered model alias {alias} not found.")
            return self.model_version_json(self.model_version_row(name, row["version"]))

    def artifact_path(self, relative: str) -> Path:
        clean = urllib.parse.unquote(relative).strip("/")
        parts = [p for p in clean.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts) or "\\" in clean or "\0" in clean:
            raise invalid(f"Invalid artifact path: '{relative}'")
        target = self.artifact_root.joinpath(*parts) if parts else self.artifact_root
        root = self.artifact_root.resolve()
        resolved = target.resolve()
        if resolved != root and root not in resolved.parents:
            raise invalid(f"Invalid artifact path: '{relative}'")
        return target

    def put_artifact(self, relative: str, data: bytes) -> None:
        target = self.artifact_path(relative)
        if target == self.artifact_root or target.is_dir():
            raise invalid(f"Artifact path '{relative}' names a directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        partial.write_bytes(data)
        os.replace(partial, target)

    def get_artifact(self, relative: str) -> bytes:
        target = self.artifact_path(relative)
        if not target.is_file():
            raise not_found(f"Artifact '{relative}' not found")
        return target.read_bytes()

    def list_artifacts(self, relative: str) -> List[Dict[str, Any]]:
        target = self.artifact_path(relative)
        if not target.is_dir():
            return []
        files = []
        for child in sorted(target.iterdir(), key=lambda p: p.name):
            if child.name.endswith(".part"):
                continue
            entry: Dict[str, Any] = {"path": child.name, "is_dir": child.is_dir()}
            if child.is_file():
                entry["file_size"] = child.stat().st_size
            files.append(entry)
        return files


class Transaction:
    def __init__(self, store: Store):
        self.store = store

    def __enter__(self) -> "Transaction":
        self.store.lock.acquire()
        self.store.db.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            self.store.db.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.store.lock.release()


def random_run_name() -> str:
    rng = random.SystemRandom()
    return f"{rng.choice(NAME_WORDS)}-{rng.choice(NAME_NOUNS)}-{rng.randint(100, 999)}"


def run_value(run: Dict[str, Any], kind: str, key: str) -> Any:
    data = run["data"]
    if kind == "metric":
        for metric in data.get("metrics", []):
            if metric["key"] == key:
                return metric["value"]
        return None
    if kind in ("param", "tag"):
        for item in data.get("params" if kind == "param" else "tags", []):
            if item["key"] == key:
                return item["value"]
        return None
    return run["info"].get(key)


def need(request: Dict[str, Any], *names: str) -> List[Any]:
    values = []
    for name in names:
        value = request.get(name)
        if value in (None, ""):
            raise invalid(f"Missing value for required parameter '{name}'. See the API docs for more information about request parameters.")
        values.append(value)
    return values


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def with_token(key: str, page: Tuple[List[Dict[str, Any]], Optional[str]]) -> Dict[str, Any]:
    items, token = page
    response: Dict[str, Any] = {key: items} if items else {}
    if token:
        response["next_page_token"] = token
    return response


def create_run_endpoint(store: Store, r: Dict[str, Any]) -> Dict[str, Any]:
    start = r.get("start_time")
    return {"run": store.create_run(need(r, "experiment_id")[0], r.get("run_name"), r.get("user_id") or "", int(start) if start else None, kv_dict(r.get("tags")))}


def update_run_endpoint(store: Store, r: Dict[str, Any]) -> Dict[str, Any]:
    end = r.get("end_time")
    return {"run_info": store.update_run(run_id_of(r), r.get("status"), int(end) if end else None, r.get("run_name"))}


def run_id_of(request: Dict[str, Any]) -> str:
    value = request.get("run_id") or request.get("run_uuid")
    if not value:
        raise invalid("Missing value for required parameter 'run_id'. See the API docs for more information about request parameters.")
    return str(value)


def single(store: Store, run_id: str, kind: str, item: Dict[str, Any]) -> Dict[str, Any]:
    store.log_batch(run_id, **{kind: [item]})
    return {}


def search_runs_endpoint(store: Store, r: Dict[str, Any]) -> Dict[str, Any]:
    page = store.search_runs(as_list(r.get("experiment_ids")), r.get("filter"), as_list(r.get("order_by")), r.get("max_results"), r.get("page_token"), r.get("run_view_type"))
    return with_token("runs", page)


def metric_history_endpoint(store: Store, r: Dict[str, Any]) -> Dict[str, Any]:
    metrics = store.get_metric_history(run_id_of(r), need(r, "metric_key")[0])
    return {"metrics": metrics} if metrics else {}


ROUTES: Dict[Tuple[str, str], Callable[[Store, Dict[str, Any]], Dict[str, Any]]] = {
    ("POST", "experiments/create"): lambda s, r: {"experiment_id": s.create_experiment(need(r, "name")[0], kv_dict(r.get("tags")), r.get("artifact_location"))},
    ("GET", "experiments/get"): lambda s, r: {"experiment": s.get_experiment(need(r, "experiment_id")[0])},
    ("GET", "experiments/get-by-name"): lambda s, r: {"experiment": s.get_experiment_by_name(need(r, "experiment_name")[0])},
    ("POST", "experiments/search"): lambda s, r: with_token("experiments", s.search_experiments(r.get("filter"), as_list(r.get("order_by")), r.get("max_results"), r.get("page_token"), r.get("view_type"))),
    ("GET", "experiments/search"): lambda s, r: with_token("experiments", s.search_experiments(r.get("filter"), as_list(r.get("order_by")), r.get("max_results"), r.get("page_token"), r.get("view_type"))),
    ("POST", "experiments/set-experiment-tag"): lambda s, r: s.set_experiment_tag(*need(r, "experiment_id", "key"), str(r.get("value", ""))) or {},
    ("POST", "runs/create"): create_run_endpoint,
    ("POST", "runs/update"): update_run_endpoint,
    ("GET", "runs/get"): lambda s, r: {"run": s.get_run(run_id_of(r))},
    ("POST", "runs/log-parameter"): lambda s, r: single(s, run_id_of(r), "params", {"key": need(r, "key")[0], "value": r.get("value", "")}),
    ("POST", "runs/log-metric"): lambda s, r: single(s, run_id_of(r), "metrics", {"key": need(r, "key")[0], "value": r.get("value"), "timestamp": need(r, "timestamp")[0], "step": r.get("step", 0)}),
    ("POST", "runs/set-tag"): lambda s, r: single(s, run_id_of(r), "tags", {"key": need(r, "key")[0], "value": r.get("value", "")}),
    ("POST", "runs/log-batch"): lambda s, r: s.log_batch(run_id_of(r), r.get("metrics") or [], r.get("params") or [], r.get("tags") or []) or {},
    ("POST", "runs/search"): search_runs_endpoint,
    ("GET", "metrics/get-history"): metric_history_endpoint,
    ("POST", "logged-models/search"): lambda s, r: {},
    ("POST", "registered-models/create"): lambda s, r: {"registered_model": s.create_registered_model(need(r, "name")[0], kv_dict(r.get("tags")), r.get("description"))},
    ("GET", "registered-models/get"): lambda s, r: {"registered_model": s.get_registered_model(need(r, "name")[0])},
    ("GET", "registered-models/search"): lambda s, r: with_token("registered_models", s.search_registered_models(r.get("filter"), r.get("max_results"), r.get("page_token"))),
    ("POST", "registered-models/set-tag"): lambda s, r: s.set_registered_model_tag(*need(r, "name", "key"), str(r.get("value", ""))) or {},
    ("POST", "registered-models/alias"): lambda s, r: s.set_alias(*need(r, "name", "alias", "version")) or {},
    ("DELETE", "registered-models/alias"): lambda s, r: s.delete_alias(*need(r, "name", "alias")) or {},
    ("GET", "registered-models/alias"): lambda s, r: {"model_version": s.get_model_version_by_alias(*need(r, "name", "alias"))},
    ("POST", "model-versions/create"): lambda s, r: {"model_version": s.create_model_version(need(r, "name")[0], need(r, "source")[0], r.get("run_id"), kv_dict(r.get("tags")), r.get("run_link"), r.get("description"))},
    ("GET", "model-versions/get"): lambda s, r: {"model_version": s.get_model_version(*need(r, "name", "version"))},
    ("GET", "model-versions/search"): lambda s, r: with_token("model_versions", s.search_model_versions(r.get("filter"), as_list(r.get("order_by")), r.get("max_results"), r.get("page_token"))),
    ("GET", "model-versions/get-download-uri"): lambda s, r: {"artifact_uri": s.get_model_version(*need(r, "name", "version"))["source"]},
    ("POST", "model-versions/set-tag"): lambda s, r: s.set_model_version_tag(*need(r, "name", "version", "key"), str(r.get("value", ""))) or {},
}
LIST_ARGS = ("experiment_ids", "order_by")


def request_args(query: str, body: bytes) -> Dict[str, Any]:
    args: Dict[str, Any] = {}
    for key, values in urllib.parse.parse_qs(query, keep_blank_values=True).items():
        args[key] = values if key in LIST_ARGS or len(values) > 1 else values[0]
    if body.strip():
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise invalid(f"Malformed JSON request body: {exc}") from None
        if not isinstance(payload, dict):
            raise invalid("The request body must be a JSON object")
        args.update(payload)
    return args


class TrackingHandler(http.server.BaseHTTPRequestHandler):
    server_version = "minitrack/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.dispatch("GET")

    def do_POST(self) -> None:
        self.dispatch("POST")

    def do_PUT(self) -> None:
        self.dispatch("PUT")

    def do_DELETE(self) -> None:
        self.dispatch("DELETE")

    def do_PATCH(self) -> None:
        self.dispatch("PATCH")

    def log_message(self, format: str, *args: Any) -> None:
        if getattr(self.server, "verbose", False):
            sys.stderr.write("minitrack: " + format % args + "\n")

    def read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return b"".join(chunks)
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, payload: Dict[str, Any]) -> None:
        self.send(status, json.dumps(payload).encode(), "application/json")

    def dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        store: Store = self.server.store
        try:
            body = self.read_body()
            if parsed.path == "/health":
                return self.send(200, b"OK", "text/plain")
            if parsed.path == SERVER_INFO_API:
                return self.send_json(200, {"store_type": "SqlStore", "multipart_uploads_enabled": False, "multipart_downloads_enabled": False})
            if parsed.path == ARTIFACTS_API or parsed.path.startswith(ARTIFACTS_API + "/"):
                return self.artifacts(method, parsed, body, store)
            for prefix in API_PREFIXES:
                if parsed.path.startswith(prefix):
                    route = ROUTES.get((method, parsed.path[len(prefix):]))
                    if route is None:
                        break
                    return self.send_json(200, route(store, request_args(parsed.query, body)))
            self.server.unknown.append(f"{method} {parsed.path}")
            sys.stderr.write(f"minitrack: no route for {method} {parsed.path}\n")
            raise MlflowError("ENDPOINT_NOT_FOUND", f"minitrack does not implement {method} {parsed.path}")
        except MlflowError as exc:
            self.send_json(exc.status, exc.to_json())
        except (KeyError, TypeError, ValueError) as exc:
            self.send_json(400, invalid(f"Bad request: {exc!r}").to_json())
        except Exception as exc:
            self.send_json(500, MlflowError("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}").to_json())

    def artifacts(self, method: str, parsed: urllib.parse.SplitResult, body: bytes, store: Store) -> None:
        relative = parsed.path[len(ARTIFACTS_API):].lstrip("/")
        if method == "PUT" and relative:
            store.put_artifact(relative, body)
            return self.send(200, b"", "application/json")
        if method == "GET" and relative:
            return self.send(200, store.get_artifact(relative), "application/octet-stream")
        if method == "GET":
            listing = store.list_artifacts(request_args(parsed.query, b"").get("path", ""))
            return self.send_json(200, {"files": listing} if listing else {})
        raise MlflowError("ENDPOINT_NOT_FOUND", f"minitrack does not implement {method} on artifacts")


class TrackingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], store: Store, verbose: bool = False):
        super().__init__(address, TrackingHandler)
        self.store = store
        self.verbose = verbose
        self.unknown: List[str] = []

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"


def start_server(store: Store, host: str = "127.0.0.1", port: int = 0) -> Tuple[TrackingServer, threading.Thread]:
    server = TrackingServer((host, port), store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class TrackingClient:
    def __init__(self, tracking_uri: str, timeout: float = 30.0):
        self.base = tracking_uri.rstrip("/")
        if not self.base.startswith(("http://", "https://")):
            raise ValueError(f"tracking URI must be http(s)://host:port, got {tracking_uri!r}")
        self.timeout = timeout

    def request(self, method: str, url: str, data: Optional[bytes] = None, content_type: str = "application/json") -> bytes:
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                error = json.loads(raw)
                raise MlflowError(error.get("error_code", "INTERNAL_ERROR"), error.get("message", raw.decode(errors="replace"))) from None
            except ValueError:
                raise MlflowError("ENDPOINT_NOT_FOUND" if exc.code == 404 else "INTERNAL_ERROR", f"{method} {url} answered {exc.code}: {raw[:200]!r}") from None

    def call(self, method: str, endpoint: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base}/api/2.0/mlflow/{endpoint}"
        if method == "GET":
            query = urllib.parse.urlencode({k: v for k, v in (payload or {}).items() if v is not None}, doseq=True)
            raw = self.request("GET", url + ("?" + query if query else ""))
        else:
            raw = self.request(method, url, json.dumps(payload or {}).encode())
        return json.loads(raw) if raw.strip() else {}

    def experiment_id(self, name: str, create: bool = True, tags: Optional[Dict[str, str]] = None) -> str:
        try:
            return self.call("GET", "experiments/get-by-name", {"experiment_name": name})["experiment"]["experiment_id"]
        except MlflowError as exc:
            if exc.code != "RESOURCE_DOES_NOT_EXIST" or not create:
                raise
        return self.call("POST", "experiments/create", {"name": name, "tags": kv_list(tags or {})})["experiment_id"]

    def create_run(self, experiment_id: str, name: str, tags: Dict[str, str]) -> Dict[str, Any]:
        all_tags = dict(tags)
        all_tags[TAG_RUN_NAME] = name
        payload = {"experiment_id": experiment_id, "run_name": name, "user_id": all_tags.get(TAG_USER, ""), "start_time": now_ms(), "tags": kv_list(all_tags)}
        return self.call("POST", "runs/create", payload)["run"]

    def log_batch(self, run_id: str, metrics: Sequence[Dict[str, Any]] = (), params: Optional[Dict[str, str]] = None, tags: Optional[Dict[str, str]] = None) -> None:
        metric_list = list(metrics)
        for start in range(0, max(len(metric_list), 1), 1000):
            first = start == 0
            self.call("POST", "runs/log-batch", {
                "run_id": run_id,
                "metrics": metric_list[start:start + 1000],
                "params": kv_list(params or {}) if first else [],
                "tags": kv_list(tags or {}) if first else [],
            })

    def finish(self, run_id: str, status: str = "FINISHED") -> Dict[str, Any]:
        return self.call("POST", "runs/update", {"run_id": run_id, "status": status, "end_time": now_ms()})["run_info"]

    def artifact_url(self, artifact_uri: str, path: str = "") -> str:
        if not artifact_uri.startswith(ARTIFACT_SCHEME):
            raise ValueError(f"only proxied {ARTIFACT_SCHEME} URIs are supported, got {artifact_uri!r}; start the server with artifact proxying")
        location = artifact_uri[len(ARTIFACT_SCHEME):].lstrip("/")
        full = "/".join(p for p in (location, path.strip("/")) if p)
        return f"{self.base}{ARTIFACTS_API}/{urllib.parse.quote(full)}"

    def upload(self, artifact_uri: str, path: str, data: bytes) -> None:
        self.request("PUT", self.artifact_url(artifact_uri, path), data, "application/octet-stream")

    def list_artifacts(self, artifact_uri: str, path: str = "") -> List[Dict[str, Any]]:
        location = artifact_uri[len(ARTIFACT_SCHEME):].lstrip("/")
        full = "/".join(p for p in (location, path.strip("/")) if p)
        raw = self.request("GET", f"{self.base}{ARTIFACTS_API}?{urllib.parse.urlencode({'path': full})}")
        return json.loads(raw).get("files", []) if raw.strip() else []

    def download_tree(self, artifact_uri: str, path: str = "") -> Dict[str, bytes]:
        files: Dict[str, bytes] = {}
        for entry in self.list_artifacts(artifact_uri, path):
            child = "/".join(p for p in (path.strip("/"), entry["path"]) if p)
            if entry.get("is_dir"):
                files.update(self.download_tree(artifact_uri, child))
            else:
                files[child] = self.request("GET", self.artifact_url(artifact_uri, child))
        return files

    def search_runs(self, experiment_ids: Sequence[str], filter_string: str = "", order_by: Sequence[str] = (), page_size: int = 1000) -> List[Dict[str, Any]]:
        runs: List[Dict[str, Any]] = []
        token: Optional[str] = None
        while True:
            payload: Dict[str, Any] = {"experiment_ids": list(experiment_ids), "filter": filter_string, "order_by": list(order_by), "max_results": page_size, "run_view_type": "ACTIVE_ONLY"}
            if token:
                payload["page_token"] = token
            page = self.call("POST", "runs/search", payload)
            runs.extend(page.get("runs", []))
            token = page.get("next_page_token")
            if not token:
                return runs

    def metric_history(self, run_id: str, key: str) -> List[Dict[str, Any]]:
        return self.call("GET", "metrics/get-history", {"run_id": run_id, "metric_key": key}).get("metrics", [])


def make_dataset(rows: int, seed: int) -> List[Dict[str, float]]:
    rng = random.Random(seed)
    data = []
    for _ in range(rows):
        area = round(rng.uniform(35.0, 180.0), 1)
        rooms = rng.randint(1, 6)
        age = rng.randint(0, 80)
        price = 0.5 + 0.02 * area + 0.15 * rooms - 0.008 * age + rng.gauss(0.0, 0.12)
        data.append({"area_m2": area, "rooms": float(rooms), "age_years": float(age), TARGET: round(price, 3)})
    return data


def write_csv(rows: List[Dict[str, float]], path: Path) -> None:
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FEATURES + [TARGET], lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: format_number(v) for k, v in row.items()})


def format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(value)


def read_csv(path: Path) -> List[Dict[str, float]]:
    with open(path, newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in FEATURES + [TARGET] if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path} lacks column(s) {', '.join(missing)}; expected {','.join(FEATURES + [TARGET])}")
        return [{k: float(v) for k, v in row.items() if k in FEATURES + [TARGET]} for row in reader]


@dataclass
class TrainResult:
    lr: float
    epochs: int
    weights: List[float]
    bias: float
    means: List[float]
    stds: List[float]
    history: List[float] = field(default_factory=list)
    metrics: Dict[str, float] = field(default_factory=dict)
    diverged: bool = False

    def model(self) -> Dict[str, Any]:
        return {"kind": "linear-regression", "features": FEATURES, "target": TARGET, "mean": self.means, "std": self.stds, "weights": self.weights, "bias": self.bias}


def predict(model: Dict[str, Any], row: Dict[str, float]) -> float:
    total = model["bias"]
    for name, mean, std, weight in zip(model["features"], model["mean"], model["std"], model["weights"]):
        total += weight * (row[name] - mean) / std
    return total


def regression_metrics(model: Dict[str, Any], rows: List[Dict[str, float]]) -> Dict[str, float]:
    errors = [predict(model, row) - row[TARGET] for row in rows]
    mean_y = sum(row[TARGET] for row in rows) / len(rows)
    total = sum((row[TARGET] - mean_y) ** 2 for row in rows)
    sse = sum(e * e for e in errors)
    return {"rmse": math.sqrt(sse / len(rows)), "mae": sum(abs(e) for e in errors) / len(rows), "r2": 1.0 - sse / total if total else 0.0}


def train_linear(rows: List[Dict[str, float]], lr: float, epochs: int, holdout: float = 0.2) -> TrainResult:
    if len(rows) < 10:
        raise ValueError(f"need at least 10 rows to train and validate, got {len(rows)}")
    split = int(len(rows) * (1 - holdout))
    train, valid = rows[:split], rows[split:]
    means = [sum(r[f] for r in train) / len(train) for f in FEATURES]
    stds = [math.sqrt(sum((r[f] - m) ** 2 for r in train) / len(train)) or 1.0 for f, m in zip(FEATURES, means)]
    xs = [[(r[f] - m) / s for f, m, s in zip(FEATURES, means, stds)] for r in train]
    ys = [r[TARGET] for r in train]
    result = TrainResult(lr, epochs, [0.0] * len(FEATURES), 0.0, means, stds)
    for _ in range(epochs):
        errors = [result.bias + sum(w * x for w, x in zip(result.weights, row)) - y for row, y in zip(xs, ys)]
        gradients = [sum(e * row[j] for e, row in zip(errors, xs)) / len(xs) for j in range(len(FEATURES))]
        result.weights = [w - lr * g for w, g in zip(result.weights, gradients)]
        result.bias -= lr * sum(errors) / len(errors)
        rmse = regression_metrics(result.model(), valid)["rmse"]
        if not math.isfinite(rmse) or rmse > 1e6:
            result.diverged = True
            return result
        result.history.append(rmse)
    result.metrics = regression_metrics(result.model(), valid)
    result.metrics["train_rmse"] = regression_metrics(result.model(), train)["rmse"]
    return result


Runner = Callable[[List[str]], Tuple[int, str]]


def run_command(argv: List[str]) -> Tuple[int, str]:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return completed.returncode, completed.stdout.strip()


def git_lineage(directory: Path, runner: Runner = run_command) -> Dict[str, str]:
    code, commit = runner(["git", "-C", str(directory), "rev-parse", "HEAD"])
    if code != 0 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        return {}
    code, status = runner(["git", "-C", str(directory), "status", "--porcelain", "--untracked-files=no"])
    return {TAG_GIT_COMMIT: commit, TAG_GIT_DIRTY: "true" if code != 0 or status else "false"}


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_dvc_pointer(pointer: Path) -> Dict[str, str]:
    text = pointer.read_text()
    md5 = re.search(r"^\s*-?\s*md5:\s*['\"]?([0-9a-f]{32}(?:\.dir)?)['\"]?\s*$", text, re.M)
    path = re.search(r"^\s*-?\s*path:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", text, re.M)
    if not md5:
        raise ValueError(f"{pointer} has no outs md5; is it a DVC pointer file?")
    return {"md5": md5.group(1), "path": path.group(1) if path else ""}


def data_lineage(data: Path) -> Dict[str, str]:
    actual = file_md5(data)
    pointer = data.with_name(data.name + ".dvc")
    if pointer.exists():
        recorded = read_dvc_pointer(pointer)["md5"]
        if recorded != actual:
            raise ValueError(f"{data} (md5 {actual}) does not match its pointer {pointer.name} (md5 {recorded}); run `dvc add {data.name}` and commit the pointer before training")
        return {TAG_DATA_MD5: actual, TAG_DATA_PATH: data.name, TAG_DATA_SOURCE: "dvc"}
    return {TAG_DATA_MD5: actual, TAG_DATA_PATH: data.name, TAG_DATA_SOURCE: "file"}


def source_tags(script: str) -> Dict[str, str]:
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = "unknown"
    return {TAG_USER: user, TAG_SOURCE_NAME: script, TAG_SOURCE_TYPE: "LOCAL"}


def run_name_for(lr: float) -> str:
    return f"lr-{format_number(lr)}"


def train_sweep(client: TrackingClient, experiment: str, data: Path, lrs: Sequence[float], epochs: int, sweep_name: str, lineage: Dict[str, str], register: Optional[str] = None, alias: Optional[str] = None) -> Dict[str, Any]:
    rows = read_csv(data)
    experiment_id = client.experiment_id(experiment)
    base_tags = dict(lineage)
    parent = client.create_run(experiment_id, sweep_name, base_tags)
    parent_id = parent["info"]["run_id"]
    children = []
    for lr in lrs:
        result = train_linear(rows, lr, epochs)
        tags = dict(base_tags)
        tags[TAG_PARENT] = parent_id
        child = client.create_run(experiment_id, run_name_for(lr) if sweep_name == "sweep" else f"{sweep_name}-{run_name_for(lr)}", tags)
        run_id = child["info"]["run_id"]
        stamp = now_ms()
        history = [{"key": "rmse", "value": v, "timestamp": stamp, "step": step} for step, v in enumerate(result.history)]
        finals = [{"key": k, "value": v, "timestamp": stamp, "step": epochs} for k, v in sorted(result.metrics.items()) if k != "rmse"]
        params = {"lr": format_number(lr), "epochs": str(epochs), "features": ",".join(FEATURES), "rows": str(len(rows))}
        client.log_batch(run_id, history + finals, params)
        if result.diverged:
            client.log_batch(run_id, tags={"failure": f"diverged after {len(result.history)} epochs"})
            client.finish(run_id, "FAILED")
        else:
            client.upload(child["info"]["artifact_uri"], "model/model.json", (json.dumps(result.model(), indent=2, sort_keys=True) + "\n").encode())
            client.finish(run_id)
        children.append({"run_id": run_id, "run_name": child["info"]["run_name"], "lr": lr, "status": "FAILED" if result.diverged else "FINISHED", "rmse": result.history[-1] if result.history and not result.diverged else None})
    finished = [c for c in children if c["status"] == "FINISHED"]
    best = min(finished, key=lambda c: (c["rmse"], c["run_name"])) if finished else None
    if best:
        client.log_batch(parent_id, [{"key": "best_rmse", "value": best["rmse"], "timestamp": now_ms(), "step": 0}], tags={"best_run": best["run_name"]})
    client.finish(parent_id, "FINISHED" if best else "FAILED")
    summary: Dict[str, Any] = {"experiment_id": experiment_id, "parent_run_id": parent_id, "runs": children, "best": best}
    if register and best:
        summary["registered"] = register_run(client, register, best["run_id"], alias)
    return summary


def register_run(client: TrackingClient, name: str, run_id: str, alias: Optional[str]) -> Dict[str, Any]:
    try:
        client.call("POST", "registered-models/create", {"name": name})
    except MlflowError as exc:
        if exc.code != "RESOURCE_ALREADY_EXISTS":
            raise
    version = client.call("POST", "model-versions/create", {"name": name, "source": f"runs:/{run_id}/model", "run_id": run_id})["model_version"]
    if alias:
        client.call("POST", "registered-models/alias", {"name": name, "alias": alias, "version": version["version"]})
    return {"name": name, "version": version["version"], "alias": alias, "source": version["source"]}


MODEL_URI = re.compile(r"^models:/(?P<name>[^@/]+)(?:@(?P<alias>[\w\-]+)|/(?P<version>\d+))$")


def resolve_model(client: TrackingClient, uri: str) -> Dict[str, Any]:
    match = MODEL_URI.match(uri)
    if not match:
        raise ValueError(f"expected models:/<name>@<alias> or models:/<name>/<version>, got {uri!r}")
    name = match.group("name")
    if match.group("alias"):
        version = client.call("GET", "registered-models/alias", {"name": name, "alias": match.group("alias")})["model_version"]
    else:
        version = client.call("GET", "model-versions/get", {"name": name, "version": match.group("version")})["model_version"]
    source = client.call("GET", "model-versions/get-download-uri", {"name": name, "version": version["version"]})["artifact_uri"]
    if source.startswith("runs:/"):
        run_id, _, path = source[len("runs:/"):].partition("/")
        artifact_uri = client.call("GET", "runs/get", {"run_id": run_id})["run"]["info"]["artifact_uri"]
    else:
        artifact_uri, path = source, ""
    files = client.download_tree(artifact_uri, path)
    return {"name": name, "version": version["version"], "aliases": version.get("aliases", []), "run_id": version.get("run_id"), "source": source, "files": files, "path": path}


def summarize_runs(runs: List[Dict[str, Any]], metric: str) -> Dict[str, Any]:
    names = {r["info"]["run_id"]: r["info"].get("run_name", r["info"]["run_id"]) for r in runs}
    rows = []
    for run in runs:
        data = run["data"]
        tags = kv_dict(data.get("tags"))
        metrics = {m["key"]: m["value"] for m in data.get("metrics", [])}
        if metric not in metrics:
            continue
        rows.append({
            "run_name": run["info"].get("run_name"),
            "status": run["info"]["status"],
            "params": kv_dict(data.get("params")),
            "metrics": metrics,
            "commit": tags.get(TAG_GIT_COMMIT, ""),
            "dirty": tags.get(TAG_GIT_DIRTY, ""),
            "data_md5": tags.get(TAG_DATA_MD5, ""),
            "parent": names.get(tags.get(TAG_PARENT, ""), tags.get(TAG_PARENT, "")),
        })
    all_params = sorted({k for r in rows for k in r["params"]})
    varying = [k for k in all_params if len({r["params"].get(k) for r in rows}) > 1]
    groups: Dict[Tuple[str, str], List[str]] = {}
    for row in rows:
        groups.setdefault((row["data_md5"], row["commit"]), []).append(row["run_name"])
    warnings = []
    if len({r["data_md5"] for r in rows}) > 1:
        warnings.append("runs were trained on different data versions (data.md5); compare metrics only inside one group")
    if len({r["commit"] for r in rows}) > 1:
        warnings.append("runs come from different git commits; a metric change may be a code change")
    if any(r["dirty"] == "true" for r in rows):
        warnings.append("some runs were trained from a dirty working tree: their commit does not identify their code")
    if any(not r["data_md5"] for r in rows):
        warnings.append("some runs have no data.md5 tag: their data version is unknown")
    best = []
    for (md5, commit), members in sorted(groups.items()):
        candidates = [r for r in rows if r["run_name"] in members and r["status"] == "FINISHED"]
        winner = min(candidates, key=lambda r: (r["metrics"][metric], r["run_name"])) if candidates else None
        best.append({"data_md5": md5, "commit": commit, "runs": sorted(members), "best": winner["run_name"] if winner else None, metric: winner["metrics"][metric] if winner else None})
    return {"metric": metric, "runs": rows, "varying_params": varying, "groups": best, "warnings": warnings}


def render_compare(summary: Dict[str, Any]) -> str:
    metric = summary["metric"]
    metric_keys = sorted({k for r in summary["runs"] for k in r["metrics"]}, key=lambda k: (k != metric, k))
    header = ["run"] + summary["varying_params"] + metric_keys + ["data", "commit", "parent"]
    table = [header]
    for row in summary["runs"]:
        table.append(
            [row["run_name"] or "?"]
            + [row["params"].get(k, "-") for k in summary["varying_params"]]
            + [f"{row['metrics'][k]:.4f}" if k in row["metrics"] else "-" for k in metric_keys]
            + [row["data_md5"][:8] or "-", (row["commit"][:8] + ("*" if row["dirty"] == "true" else "")) or "-", row["parent"] or "-"]
        )
    widths = [max(len(str(r[i])) for r in table) for i in range(len(header))]
    lines = ["  ".join(str(cell).ljust(width) for cell, width in zip(r, widths)).rstrip() for r in table]
    for group in summary["groups"]:
        lines.append(f"best by {metric} for data {group['data_md5'][:8] or '?'} @ {group['commit'][:8] or '?'}: {group['best']} ({group[metric]:.4f})" if group["best"] else f"no finished run for data {group['data_md5'][:8]}")
    lines.extend(f"warning: {w}" for w in summary["warnings"])
    return "\n".join(lines)


def parse_row(items: Sequence[str]) -> Dict[str, float]:
    row = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or key not in FEATURES:
            raise ValueError(f"--predict expects {' '.join(f + '=NUMBER' for f in FEATURES)}, got {item!r}")
        row[key] = float(value)
    missing = [f for f in FEATURES if f not in row]
    if missing:
        raise ValueError(f"--predict is missing {', '.join(missing)}")
    return row


def cmd_server(args: argparse.Namespace) -> int:
    store = Store(args.db, args.artifacts)
    server = TrackingServer((args.host, args.port), store, verbose=args.verbose)
    if args.port_file:
        Path(args.port_file).write_text(f"{server.server_address[1]}\n")
    print(f"minitrack serving the MLflow REST API on {server.url} (db {args.db}, artifacts {args.artifacts})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server.unknown:
            print(f"endpoints the client asked for that minitrack does not implement: {sorted(set(server.unknown))}", file=sys.stderr)
        server.server_close()
        store.close()
    return 0


def cmd_make_data(args: argparse.Namespace) -> int:
    rows = make_dataset(args.rows, args.seed)
    write_csv(rows, Path(args.out))
    print(f"{args.out}: {len(rows)} rows, seed {args.seed}, md5 {file_md5(Path(args.out))}")
    return 0


def cmd_train(args: argparse.Namespace) -> int:
    data = Path(args.data)
    if not data.is_file():
        print(f"error: {data} does not exist; create it with `make-data`", file=sys.stderr)
        return 2
    try:
        lineage = data_lineage(data)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    lineage.update(git_lineage(Path(args.git_dir)))
    lineage.update(source_tags(args.source_name))
    client = TrackingClient(args.tracking_uri)
    summary = train_sweep(client, args.experiment, data, args.lr or [0.01, 0.1, 0.3], args.epochs, args.sweep_name, lineage, args.register, args.alias)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    for run in summary["runs"]:
        rmse = f"{run['rmse']:.4f}" if run["rmse"] is not None else "-"
        print(f"{run['run_name']:<16} {run['status']:<9} rmse {rmse}  run {run['run_id']}")
    if summary["best"]:
        print(f"best: {summary['best']['run_name']} (rmse {summary['best']['rmse']:.4f})")
    if "registered" in summary:
        reg = summary["registered"]
        print(f"registered {reg['name']} version {reg['version']}" + (f" as @{reg['alias']}" if reg["alias"] else ""))
    missing = [k for k in (TAG_GIT_COMMIT, TAG_DATA_MD5) if k not in lineage]
    if missing:
        print(f"warning: no {', '.join(missing)} recorded; this run cannot be reproduced from its tags", file=sys.stderr)
    return 0 if summary["best"] else 1


def cmd_compare(args: argparse.Namespace) -> int:
    client = TrackingClient(args.tracking_uri)
    experiment_id = client.experiment_id(args.experiment, create=False)
    runs = client.search_runs([experiment_id], args.filter or "", args.order_by or [f"metrics.{args.metric} ASC"])
    summary = summarize_runs(runs, args.metric)
    if args.history:
        names = {r["info"]["run_id"]: r["info"].get("run_name") for r in runs}
        summary["history"] = {names[r["info"]["run_id"]]: [m["value"] for m in client.metric_history(r["info"]["run_id"], args.metric)] for r in runs if names[r["info"]["run_id"]] in {row["run_name"] for row in summary["runs"]}}
    print(json.dumps(summary, indent=2, sort_keys=True) if args.json else render_compare(summary))
    return 0 if summary["runs"] else 1


def cmd_resolve(args: argparse.Namespace) -> int:
    client = TrackingClient(args.tracking_uri)
    resolved = resolve_model(client, args.model_uri)
    files = resolved.pop("files")
    if args.dest:
        for relative, data in files.items():
            target = Path(args.dest) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    resolved["files"] = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}
    model_file = next((data for name, data in files.items() if name.endswith("model.json")), None)
    if args.predict:
        if model_file is None:
            print(f"error: {args.model_uri} has no model.json to predict with", file=sys.stderr)
            return 1
        resolved["prediction"] = predict(json.loads(model_file), parse_row(args.predict))
    if args.json:
        print(json.dumps(resolved, indent=2, sort_keys=True))
        return 0
    print(f"{args.model_uri} -> {resolved['name']} version {resolved['version']} (aliases {', '.join(resolved['aliases']) or '-'})")
    print(f"source {resolved['source']} from run {resolved['run_id']}")
    for name, digest in resolved["files"].items():
        print(f"  {name}  sha256:{digest[:16]}...")
    if "prediction" in resolved:
        print(f"prediction {resolved['prediction']:.4f} ({TARGET})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minitrack", description="A small experiment tracker that speaks MLflow's REST API.")
    sub = parser.add_subparsers(dest="command", required=True)
    server = sub.add_parser("server", help="serve the tracking API, artifacts and the model registry")
    server.add_argument("--db", default="minitrack.db")
    server.add_argument("--artifacts", default="minitrack-artifacts")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=5005)
    server.add_argument("--port-file", help="write the bound port here (useful with --port 0)")
    server.add_argument("--verbose", action="store_true")
    server.set_defaults(func=cmd_server)
    data = sub.add_parser("make-data", help="write a seeded house-price CSV")
    data.add_argument("--rows", type=int, default=400)
    data.add_argument("--seed", type=int, default=7)
    data.add_argument("--out", default="houses.csv")
    data.set_defaults(func=cmd_make_data)
    train = sub.add_parser("train", help="run a learning-rate sweep and log it with lineage")
    train.add_argument("--tracking-uri", default=os.environ.get("MLFLOW_TRACKING_URI", "http://127.0.0.1:5005"))
    train.add_argument("--experiment", default="house-prices")
    train.add_argument("--data", default="houses.csv")
    train.add_argument("--lr", type=float, action="append")
    train.add_argument("--epochs", type=int, default=30)
    train.add_argument("--sweep-name", default="sweep")
    train.add_argument("--git-dir", default=".")
    train.add_argument("--source-name", default="minitrack train")
    train.add_argument("--register")
    train.add_argument("--alias")
    train.add_argument("--json", action="store_true")
    train.set_defaults(func=cmd_train)
    comp = sub.add_parser("compare", help="compare runs: varying params, metrics, lineage groups")
    comp.add_argument("--tracking-uri", default=os.environ.get("MLFLOW_TRACKING_URI", "http://127.0.0.1:5005"))
    comp.add_argument("--experiment", default="house-prices")
    comp.add_argument("--filter")
    comp.add_argument("--order-by", action="append")
    comp.add_argument("--metric", default="rmse")
    comp.add_argument("--history", action="store_true")
    comp.add_argument("--json", action="store_true")
    comp.set_defaults(func=cmd_compare)
    resolve = sub.add_parser("resolve", help="resolve models:/name@alias to a version, a run and its files")
    resolve.add_argument("model_uri")
    resolve.add_argument("--tracking-uri", default=os.environ.get("MLFLOW_TRACKING_URI", "http://127.0.0.1:5005"))
    resolve.add_argument("--dest")
    resolve.add_argument("--predict", nargs="+", metavar="FEATURE=VALUE")
    resolve.add_argument("--json", action="store_true")
    resolve.set_defaults(func=cmd_resolve)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return args.func(args)
    except MlflowError as exc:
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, ConnectionError) as exc:
        print(f"error: cannot reach the tracking server: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
