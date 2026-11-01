"""statesrv: a remote state server for the OpenTofu/Terraform HTTP backend, with locking, serial and lineage checks, and version history."""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import socket
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LOCK_ACQUIRE = {"LOCK", "POST", "PUT"}
LOCK_RELEASE = {"UNLOCK", "DELETE"}
STATE_METHODS = {"GET", "POST", "PUT", "DELETE", "LOCK", "UNLOCK"}
Credentials = Optional[Tuple[str, str]]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def md5_b64(body: bytes) -> str:
    return base64.b64encode(hashlib.md5(body).digest()).decode("ascii")


class StoreError(Exception):
    def __init__(self, status: int, message: str, lock: Optional[Dict[str, Any]] = None, lock_body: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.lock = lock
        self.lock_body = lock_body

    def payload(self) -> Dict[str, Any]:
        if self.lock_body and self.lock is not None:
            return self.lock
        return {"error": self.message, "lock": self.lock}


def describe(lock: Optional[Dict[str, Any]]) -> str:
    if not lock:
        return "nobody"
    who = lock.get("Who") or "unknown"
    operation = lock.get("Operation") or "unknown operation"
    return f"{lock.get('ID')} held by {who} for {operation} since {lock.get('Created') or 'unknown time'}"


def ordered(state: Optional[Dict[str, Any]]) -> bool:
    return bool(state) and type(state.get("serial")) is int and isinstance(state.get("lineage"), str) and bool(state.get("lineage"))


def parse_state(body: bytes) -> Dict[str, Any]:
    try:
        state = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StoreError(400, f"state is not JSON: {exc}")
    if not isinstance(state, dict):
        raise StoreError(400, "state must be a JSON object")
    if not ordered(state) and "encrypted_data" not in state:
        raise StoreError(400, "not a state file: it needs an integer serial and a lineage")
    return state


def check_lock(holder: Optional[Dict[str, Any]], lock_id: Optional[str], require_lock: bool) -> None:
    if holder is not None:
        if not lock_id:
            raise StoreError(423, f"state is locked ({describe(holder)}); a write must carry ?ID=<lock id>", holder)
        if lock_id != holder.get("ID"):
            raise StoreError(409, f"lock ID {lock_id} does not match the held lock ({describe(holder)})", holder)
        return
    if lock_id:
        raise StoreError(409, f"lock ID {lock_id} is not held: the lock was released or force-unlocked while you were working")
    if require_lock:
        raise StoreError(428, "this server only accepts writes made under a lock: configure lock_address and unlock_address")


def check_order(current: Optional[Dict[str, Any]], current_md5: Optional[str], incoming: Dict[str, Any], incoming_md5: str) -> str:
    if not ordered(current) or not ordered(incoming):
        return "new"
    assert current is not None
    if incoming["lineage"] != current["lineage"]:
        raise StoreError(409, f"lineage {incoming['lineage']} does not match the stored lineage {current['lineage']}: this is a different state, not a newer version of it")
    if incoming["serial"] < current["serial"]:
        raise StoreError(409, f"serial {incoming['serial']} is older than the stored serial {current['serial']}: someone wrote after you read")
    if incoming["serial"] == current["serial"]:
        if incoming_md5 == current_md5:
            return "same"
        raise StoreError(409, f"serial {incoming['serial']} is already stored with different content")
    return "new"


def write_temp(directory: str, data: bytes) -> str:
    fd, staged = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return staged


def atomic_write(path: str, data: bytes) -> None:
    os.replace(write_temp(os.path.dirname(path), data), path)


class StateStore:
    def __init__(self, root: str, clock: Callable[[], str] = utc_now) -> None:
        self.root = root
        self.clock = clock
        self.mutex = threading.Lock()
        os.makedirs(root, exist_ok=True)

    def _dir(self, name: str) -> str:
        if not NAME_RE.match(name):
            raise StoreError(400, f"invalid state name {name!r}: use letters, digits, '.', '_' and '-'")
        return os.path.join(self.root, name)

    def _file(self, name: str, *parts: str) -> str:
        return os.path.join(self._dir(name), *parts)

    def names(self) -> List[str]:
        return sorted(entry for entry in os.listdir(self.root) if NAME_RE.match(entry) and os.path.isdir(os.path.join(self.root, entry)))

    def read(self, name: str) -> Optional[bytes]:
        try:
            with open(self._file(name, "state.tfstate"), "rb") as handle:
                return handle.read()
        except FileNotFoundError:
            return None

    def lock_info(self, name: str) -> Optional[Dict[str, Any]]:
        try:
            with open(self._file(name, "lock.json"), encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return None

    def versions(self, name: str) -> List[Dict[str, Any]]:
        try:
            with open(self._file(name, "versions.json"), encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return []

    def version_body(self, name: str, number: int) -> Optional[bytes]:
        try:
            with open(self._file(name, "versions", f"{number:06d}.tfstate"), "rb") as handle:
                return handle.read()
        except FileNotFoundError:
            return None

    def events(self, name: str) -> List[Dict[str, Any]]:
        try:
            with open(self._file(name, "events.jsonl"), encoding="utf-8") as handle:
                return [json.loads(line) for line in handle if line.strip()]
        except FileNotFoundError:
            return []

    def record(self, name: str, event: str, **fields: Any) -> None:
        os.makedirs(self._dir(name), exist_ok=True)
        entry = {"at": self.clock(), "event": event, **{key: value for key, value in fields.items() if value not in (None, "")}}
        with open(self._file(name, "events.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")

    def lock(self, name: str, info: Dict[str, Any], by: str = "") -> Dict[str, Any]:
        lock_id = info.get("ID")
        if not isinstance(lock_id, str) or not lock_id:
            raise StoreError(400, "lock info needs a non-empty ID")
        record = dict(info)
        if not record.get("Created"):
            record["Created"] = self.clock()
        directory = self._dir(name)
        with self.mutex:
            os.makedirs(directory, exist_ok=True)
            staged = write_temp(directory, json.dumps(record, indent=2).encode("utf-8"))
            try:
                os.link(staged, os.path.join(directory, "lock.json"))
            except FileExistsError:
                holder = self.lock_info(name) or {}
                if holder.get("ID") == lock_id:
                    return holder
                self.record(name, "lock-refused", lock_id=lock_id, who=record.get("Who"), holder=holder.get("ID"), by=by)
                raise StoreError(423, f"state {name} is already locked ({describe(holder)})", holder, lock_body=True)
            finally:
                os.unlink(staged)
            self.record(name, "lock", lock_id=lock_id, who=record.get("Who"), operation=record.get("Operation"), by=by)
        return record

    def unlock(self, name: str, info: Dict[str, Any], by: str = "") -> Optional[Dict[str, Any]]:
        lock_id = info.get("ID")
        id_only = not info.get("Who") and not info.get("Operation")
        with self.mutex:
            holder = self.lock_info(name)
            if holder is None:
                self.record(name, "unlock-noop", lock_id=lock_id, by=by)
                return None
            if not lock_id or lock_id != holder.get("ID"):
                self.record(name, "unlock-refused", lock_id=lock_id, holder=holder.get("ID"), by=by)
                raise StoreError(409, f"lock ID {lock_id or '(none)'} does not match the held lock ({describe(holder)})", holder, lock_body=True)
            os.unlink(self._file(name, "lock.json"))
            released_by = "ID only, as tofu force-unlock sends it" if id_only else info.get("Who")
            self.record(name, "unlock", lock_id=lock_id, who=holder.get("Who"), released_by=released_by, by=by)
        return holder

    def force_unlock(self, name: str, by: str = "admin") -> Optional[Dict[str, Any]]:
        with self.mutex:
            holder = self.lock_info(name)
            if holder is None:
                return None
            os.unlink(self._file(name, "lock.json"))
            self.record(name, "force-unlock", lock_id=holder.get("ID"), who=holder.get("Who"), by=by)
        return holder

    def write(self, name: str, body: bytes, lock_id: Optional[str] = None, require_lock: bool = False, content_md5: Optional[str] = None, by: str = "") -> Dict[str, Any]:
        digest = md5_b64(body)
        if content_md5 and content_md5 != digest:
            raise StoreError(400, f"Content-MD5 {content_md5} does not match the body ({digest}): the upload was corrupted")
        state = parse_state(body)
        directory = self._dir(name)
        with self.mutex:
            current_body = self.read(name)
            current = json.loads(current_body.decode("utf-8")) if current_body else None
            try:
                check_lock(self.lock_info(name), lock_id, require_lock)
                verdict = check_order(current, md5_b64(current_body) if current_body else None, state, digest)
            except StoreError as exc:
                self.record(name, "write-refused", lock_id=lock_id, serial=state.get("serial"), status=exc.status, reason=exc.message, by=by)
                raise
            versions = self.versions(name)
            if verdict == "same":
                return {**(versions[-1] if versions else {}), "unchanged": True}
            holder = self.lock_info(name) or {}
            entry = {
                "version": versions[-1]["version"] + 1 if versions else 1,
                "serial": state.get("serial"),
                "lineage": state.get("lineage"),
                "md5": digest,
                "bytes": len(body),
                "written_at": self.clock(),
                "lock_id": lock_id or "",
                "who": holder.get("Who", ""),
                "operation": holder.get("Operation", ""),
                "opaque": not ordered(state),
            }
            os.makedirs(os.path.join(directory, "versions"), exist_ok=True)
            atomic_write(os.path.join(directory, "versions", f"{entry['version']:06d}.tfstate"), body)
            atomic_write(os.path.join(directory, "versions.json"), json.dumps(versions + [entry], indent=2).encode("utf-8"))
            atomic_write(os.path.join(directory, "state.tfstate"), body)
            self.record(name, "write", lock_id=lock_id, serial=entry["serial"], version=entry["version"], by=by)
        return entry

    def delete(self, name: str, lock_id: Optional[str] = None, by: str = "") -> None:
        with self.mutex:
            check_lock(self.lock_info(name), lock_id, False)
            path = self._file(name, "state.tfstate")
            if not os.path.exists(path):
                raise StoreError(404, f"no state named {name}")
            os.unlink(path)
            self.record(name, "delete", lock_id=lock_id, by=by)

    def summary(self, name: str) -> Dict[str, Any]:
        body = self.read(name)
        state = json.loads(body.decode("utf-8")) if body else {}
        return {"name": name, "serial": state.get("serial"), "lineage": state.get("lineage"), "lock": self.lock_info(name), "versions": len(self.versions(name))}


class StateServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], store: StateStore, credentials: Credentials = None, require_lock: bool = False, log: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(address, StateHandler)
        self.store = store
        self.credentials = credentials
        self.require_lock = require_lock
        self.log = log


def make_server(store: StateStore, host: str = "127.0.0.1", port: int = 0, credentials: Credentials = None, require_lock: bool = False, log: Optional[Callable[[str], None]] = None) -> StateServer:
    return StateServer((host, port), store, credentials, require_lock, log)


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode("utf-8")


def header_safe(text: str) -> str:
    return " ".join(text.split()).encode("ascii", "replace").decode("ascii")


def parse_object(body: bytes, what: str) -> Dict[str, Any]:
    if not body.strip():
        return {}
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StoreError(400, f"{what} is not JSON: {exc}")
    if not isinstance(value, dict):
        raise StoreError(400, f"{what} must be a JSON object")
    return value


class StateHandler(BaseHTTPRequestHandler):
    server: StateServer
    server_version = "statesrv/1.0"
    user = ""

    def do_GET(self) -> None:
        self.route()

    do_POST = do_PUT = do_DELETE = do_LOCK = do_UNLOCK = do_GET

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.server.log:
            self.server.log(f"{self.address_string()} {fmt % args}")

    def caller(self) -> str:
        return self.user or self.client_address[0]

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def authorize(self, path: str) -> None:
        credentials = self.server.credentials
        if credentials is None or path == "/healthz":
            return
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            raise StoreError(401, "authentication required: set username and password (TF_HTTP_USERNAME, TF_HTTP_PASSWORD)")
        try:
            user, _, password = base64.b64decode(header[6:], validate=True).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            raise StoreError(401, "malformed Authorization header")
        if not (hmac.compare_digest(user.encode(), credentials[0].encode()) and hmac.compare_digest(password.encode(), credentials[1].encode())):
            raise StoreError(403, "invalid credentials")
        self.user = user

    def send(self, status: int, body: bytes, content_type: str = "application/json", headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def route(self) -> None:
        body = self.read_body()
        parsed = urllib.parse.urlsplit(self.path)
        try:
            self.authorize(parsed.path)
            lock_id = (urllib.parse.parse_qs(parsed.query).get("ID") or [None])[0]
            parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
            status, payload, content_type, headers = self.dispatch(self.command, parts, body, lock_id)
        except StoreError as exc:
            headers = {"X-Statesrv-Error": header_safe(exc.message)}
            if exc.status == 401:
                headers["WWW-Authenticate"] = 'Basic realm="statesrv"'
            if exc.status == 405:
                headers["Allow"] = ", ".join(sorted(STATE_METHODS))
            self.send(exc.status, json_bytes(exc.payload()), headers=headers)
            return
        self.send(status, payload, content_type, headers)

    def dispatch(self, method: str, parts: List[str], body: bytes, lock_id: Optional[str]) -> Tuple[int, bytes, str, Dict[str, str]]:
        store = self.server.store
        if parts == ["healthz"]:
            return 200, b"ok\n", "text/plain", {}
        if parts == ["states"] and method == "GET":
            return 200, json_bytes([store.summary(name) for name in store.names()]), "application/json", {}
        if len(parts) < 2 or parts[0] != "states":
            raise StoreError(404, "unknown path: a state lives at /states/<name>")
        name, rest = parts[1], parts[2:]
        if not rest:
            if method == "GET":
                state = store.read(name)
                if state is None:
                    raise StoreError(404, f"no state named {name}")
                return 200, state, "application/json", {"Content-MD5": md5_b64(state)}
            if method in ("POST", "PUT"):
                entry = store.write(name, body, lock_id, self.server.require_lock, self.headers.get("Content-MD5"), self.caller())
                return 200, json_bytes(entry), "application/json", {}
            if method == "DELETE":
                store.delete(name, lock_id, self.caller())
                return 200, json_bytes({"deleted": name}), "application/json", {}
            if method == "LOCK":
                return self.acquire(name, body)
            if method == "UNLOCK":
                return self.release(name, body)
        elif rest == ["lock"]:
            if method == "GET":
                holder = store.lock_info(name)
                if holder is None:
                    raise StoreError(404, f"state {name} is not locked")
                return 200, json_bytes(holder), "application/json", {}
            if method in LOCK_ACQUIRE:
                return self.acquire(name, body)
            if method in LOCK_RELEASE:
                return self.release(name, body)
        elif rest == ["versions"] and method == "GET":
            return 200, json_bytes(store.versions(name)), "application/json", {}
        elif len(rest) == 2 and rest[0] == "versions" and rest[1].isdigit() and method == "GET":
            version = store.version_body(name, int(rest[1]))
            if version is None:
                raise StoreError(404, f"state {name} has no version {rest[1]}")
            return 200, version, "application/json", {"Content-MD5": md5_b64(version)}
        elif rest == ["events"] and method == "GET":
            return 200, json_bytes(store.events(name)), "application/json", {}
        else:
            raise StoreError(404, f"unknown path under /states/{name}")
        raise StoreError(405, f"{method} is not supported on {self.path}")

    def acquire(self, name: str, body: bytes) -> Tuple[int, bytes, str, Dict[str, str]]:
        record = self.server.store.lock(name, parse_object(body, "lock info"), self.caller())
        return 200, json_bytes(record), "application/json", {}

    def release(self, name: str, body: bytes) -> Tuple[int, bytes, str, Dict[str, str]]:
        holder = self.server.store.unlock(name, parse_object(body, "lock info"), self.caller())
        return 200, json_bytes({"unlocked": holder}), "application/json", {}


class ClientError(Exception):
    pass


class HTTPStatusError(ClientError):
    def __init__(self, status: int, message: str, body: Any = None) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.body = body


class LockedError(HTTPStatusError):
    @property
    def holder(self) -> Dict[str, Any]:
        return self.body if isinstance(self.body, dict) else {}


def default_who() -> str:
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = "unknown"
    return f"{user}@{socket.gethostname()}"


def new_lock_info(operation: str = "OperationTypeApply", who: Optional[str] = None, info: str = "", path: str = "") -> Dict[str, Any]:
    return {"ID": str(uuid.uuid4()), "Operation": operation, "Info": info, "Who": who or default_who(), "Version": "statesrv/1.0", "Created": utc_now(), "Path": path}


def empty_state(lineage: Optional[str] = None) -> Dict[str, Any]:
    return {"version": 4, "terraform_version": "0.0.0", "serial": 0, "lineage": lineage or str(uuid.uuid4()), "outputs": {}, "resources": [], "check_results": None}


def decode(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, ValueError):
        return body.decode("utf-8", "replace")


class StateClient:
    def __init__(self, address: str, lock_address: Optional[str] = None, unlock_address: Optional[str] = None, lock_method: str = "LOCK", unlock_method: str = "UNLOCK", update_method: str = "POST", username: Optional[str] = None, password: Optional[str] = None, timeout: float = 10.0) -> None:
        self.address = address.rstrip("/")
        self.lock_address = lock_address or self.address
        self.unlock_address = unlock_address or self.lock_address
        self.lock_method = lock_method
        self.unlock_method = unlock_method
        self.update_method = update_method
        self.username = username
        self.password = password
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, url: str, body: Optional[bytes] = None) -> Tuple[int, Dict[str, str], bytes]:
        headers: Dict[str, str] = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-MD5"] = md5_b64(body)
        if self.username:
            token = base64.b64encode(f"{self.username}:{self.password or ''}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read()
        except OSError as exc:
            raise ClientError(f"cannot reach {url}: {getattr(exc, 'reason', exc)}")

    def failure(self, status: int, headers: Dict[str, str], body: bytes) -> HTTPStatusError:
        decoded = decode(body)
        message = headers.get("X-Statesrv-Error") or (decoded.get("error") if isinstance(decoded, dict) else None) or str(decoded or "")
        return HTTPStatusError(status, message, decoded)

    def get(self) -> Optional[Dict[str, Any]]:
        status, headers, body = self.request("GET", self.address)
        if status in (204, 404):
            return None
        if status != 200:
            raise self.failure(status, headers, body)
        return json.loads(body.decode("utf-8")) if body else None

    def lock(self, operation: str = "OperationTypeApply", who: Optional[str] = None, info: str = "") -> Dict[str, Any]:
        record = new_lock_info(operation, who, info, self.address)
        status, headers, body = self.request(self.lock_method, self.lock_address, json.dumps(record).encode("utf-8"))
        if status == 200:
            return record
        if status in (409, 423):
            holder = decode(body)
            raise LockedError(status, f"state already locked: ID={holder.get('ID') if isinstance(holder, dict) else '?'}", holder)
        raise self.failure(status, headers, body)

    def unlock(self, lock_id: str, info: Optional[Dict[str, Any]] = None) -> None:
        payload = dict(info or {})
        payload["ID"] = lock_id
        status, headers, body = self.request(self.unlock_method, self.unlock_address, json.dumps(payload).encode("utf-8"))
        if status != 200:
            raise self.failure(status, headers, body)

    def put_bytes(self, body: bytes, lock_id: Optional[str] = None) -> Dict[str, Any]:
        url = self.address + ("?" + urllib.parse.urlencode({"ID": lock_id}) if lock_id else "")
        status, headers, response = self.request(self.update_method, url, body)
        if status not in (200, 201, 204):
            raise self.failure(status, headers, response)
        return decode(response) or {}

    def put(self, state: Dict[str, Any], lock_id: Optional[str] = None) -> Dict[str, Any]:
        return self.put_bytes(json.dumps(state, indent=2).encode("utf-8"), lock_id)

    def lock_status(self) -> Optional[Dict[str, Any]]:
        status, headers, body = self.request("GET", self.address + "/lock")
        if status == 404:
            return None
        if status != 200:
            raise self.failure(status, headers, body)
        return decode(body)

    def versions(self) -> List[Dict[str, Any]]:
        status, headers, body = self.request("GET", self.address + "/versions")
        if status != 200:
            raise self.failure(status, headers, body)
        return decode(body) or []

    def version(self, number: int) -> bytes:
        status, headers, body = self.request("GET", f"{self.address}/versions/{number}")
        if status != 200:
            raise self.failure(status, headers, body)
        return body

    def events(self) -> List[Dict[str, Any]]:
        status, headers, body = self.request("GET", self.address + "/events")
        if status != 200:
            raise self.failure(status, headers, body)
        return decode(body) or []


def locked_apply(client: StateClient, change: Callable[[Dict[str, Any]], None], who: str, operation: str = "OperationTypeApply") -> Dict[str, Any]:
    lock = client.lock(operation=operation, who=who)
    try:
        state = client.get() or empty_state()
        change(state)
        state["serial"] += 1
        client.put(state, lock_id=lock["ID"])
        return state
    finally:
        client.unlock(lock["ID"], lock)


def render_status(address: str, versions: List[Dict[str, Any]], lock: Optional[Dict[str, Any]]) -> str:
    latest = versions[-1] if versions else {}
    lines = [
        f"state     {address}",
        f"serial    {latest.get('serial', '-')}",
        f"lineage   {latest.get('lineage', '-')}",
        f"versions  {len(versions)}",
        f"lock      {describe(lock) if lock else 'free'}",
    ]
    return "\n".join(lines)


def render_history(versions: List[Dict[str, Any]]) -> str:
    lines = [f"{'VERSION':<8} {'SERIAL':<7} {'WRITTEN_AT':<28} {'WHO':<24} LOCK_ID"]
    for entry in versions:
        lines.append(f"{entry['version']:<8} {str(entry.get('serial')):<7} {entry.get('written_at', ''):<28} {(entry.get('who') or '-'):<24} {entry.get('lock_id') or '-'}")
    return "\n".join(lines)


def client_from(args: argparse.Namespace) -> StateClient:
    return StateClient(args.address, args.lock_address, args.unlock_address, args.lock_method, args.unlock_method, args.update_method, args.user, args.password)


def cmd_lock(client: StateClient, args: argparse.Namespace) -> int:
    try:
        record = client.lock(operation=args.operation, who=args.who, info=args.info)
    except LockedError as exc:
        print(f"statesrv: locked: {describe(exc.holder)}", file=sys.stderr)
        if args.json:
            print(json.dumps({"locked": False, "holder": exc.holder}, indent=2))
        return 1
    print(json.dumps(record, indent=2) if args.json else record["ID"])
    return 0


def cmd_unlock(client: StateClient, args: argparse.Namespace) -> int:
    client.unlock(args.id)
    print(f"unlocked {args.id}")
    return 0


def cmd_status(client: StateClient, args: argparse.Namespace) -> int:
    versions = client.versions()
    lock = client.lock_status()
    latest = versions[-1] if versions else {}
    if args.json:
        print(json.dumps({"address": client.address, "serial": latest.get("serial"), "lineage": latest.get("lineage"), "versions": len(versions), "lock": lock}, indent=2))
    else:
        print(render_status(client.address, versions, lock))
    return 0


def cmd_history(client: StateClient, args: argparse.Namespace) -> int:
    versions = client.versions()
    print(json.dumps(versions, indent=2) if args.json else render_history(versions))
    return 0


def cmd_events(client: StateClient, args: argparse.Namespace) -> int:
    events = client.events()
    if args.json:
        print(json.dumps(events, indent=2))
    else:
        for event in events:
            details = " ".join(f"{key}={value}" for key, value in sorted(event.items()) if key not in ("at", "event"))
            print(f"{event['at']}  {event['event']:<15} {details}")
    return 0


def cmd_pull(client: StateClient, args: argparse.Namespace) -> int:
    if args.version is not None:
        sys.stdout.write(client.version(args.version).decode("utf-8"))
        return 0
    state = client.get()
    if state is None:
        print(f"statesrv: no state at {client.address}", file=sys.stderr)
        return 1
    print(json.dumps(state, indent=2))
    return 0


def cmd_push(client: StateClient, args: argparse.Namespace) -> int:
    with open(args.file, "rb") as handle:
        body = handle.read()
    json.loads(body.decode("utf-8"))
    entry = client.put_bytes(body, args.id)
    if args.json:
        print(json.dumps(entry, indent=2))
    elif entry.get("unchanged"):
        print(f"unchanged: version {entry.get('version')} already holds these bytes (serial {entry.get('serial')})")
    else:
        print(f"stored version {entry.get('version')} (serial {entry.get('serial')})")
    return 0


def cmd_force_unlock(args: argparse.Namespace) -> int:
    holder = StateStore(args.data).force_unlock(args.name, by=f"admin:{default_who()}")
    if holder is None:
        print(f"state {args.name} was not locked")
        return 0
    print(f"removed lock {describe(holder)}")
    return 0


def serve(args: argparse.Namespace) -> int:
    password = args.password or os.environ.get("STATESRV_PASSWORD")
    credentials: Credentials = None
    if args.user:
        if not password:
            print("statesrv: --user needs --password or STATESRV_PASSWORD", file=sys.stderr)
            return 2
        credentials = (args.user, password)
    log = None if args.quiet else (lambda line: print(line, file=sys.stderr, flush=True))
    server = make_server(StateStore(args.data), args.host, args.port, credentials, args.require_lock, log)
    host, port = server.server_address[:2]
    if args.port_file:
        atomic_write(os.path.abspath(args.port_file), f"{port}\n".encode("ascii"))
    extras = (", basic auth" if credentials else "") + (", writes require a lock" if args.require_lock else "")
    print(f"statesrv listening on http://{host}:{port} (data {args.data}{extras})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


COMMANDS: Dict[str, Callable[[StateClient, argparse.Namespace], int]] = {
    "lock": cmd_lock,
    "unlock": cmd_unlock,
    "status": cmd_status,
    "history": cmd_history,
    "events": cmd_events,
    "pull": cmd_pull,
    "push": cmd_push,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="statesrv", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    srv = sub.add_parser("serve", help="run the state server")
    srv.add_argument("--data", required=True, help="directory holding every state, its lock and its history")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8080, help="0 picks a free port")
    srv.add_argument("--port-file", help="write the bound port here once the server listens")
    srv.add_argument("--user", help="require HTTP basic auth with this user")
    srv.add_argument("--password", help="basic auth password (or STATESRV_PASSWORD)")
    srv.add_argument("--require-lock", action="store_true", help="refuse writes not made under a lock")
    srv.add_argument("--quiet", action="store_true", help="do not log requests")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("address", help="state address, e.g. http://127.0.0.1:8080/states/demo")
    common.add_argument("--lock-address", help="defaults to the address (tofu's lock_address)")
    common.add_argument("--unlock-address", help="defaults to the lock address")
    common.add_argument("--lock-method", default="LOCK")
    common.add_argument("--unlock-method", default="UNLOCK")
    common.add_argument("--update-method", default="POST")
    common.add_argument("--user", default=os.environ.get("TF_HTTP_USERNAME"), help="basic auth user (or TF_HTTP_USERNAME)")
    common.add_argument("--password", default=os.environ.get("TF_HTTP_PASSWORD"), help="basic auth password (or TF_HTTP_PASSWORD)")
    common.add_argument("--json", action="store_true", help="machine-readable output")

    lock = sub.add_parser("lock", parents=[common], help="acquire the lock and print its ID")
    lock.add_argument("--who", help="defaults to user@host")
    lock.add_argument("--operation", default="OperationTypeInvalid", help="what the holder is doing")
    lock.add_argument("--info", default="", help="free text stored with the lock")
    unlock = sub.add_parser("unlock", parents=[common], help="release the lock with its ID")
    unlock.add_argument("--id", required=True)
    sub.add_parser("status", parents=[common], help="serial, lineage, versions and lock holder")
    sub.add_parser("history", parents=[common], help="every stored version")
    sub.add_parser("events", parents=[common], help="lock, unlock and write audit log")
    pull = sub.add_parser("pull", parents=[common], help="print the current state or one version")
    pull.add_argument("--version", type=int)
    push = sub.add_parser("push", parents=[common], help="write a state file (the server checks lock, lineage and serial)")
    push.add_argument("file")
    push.add_argument("--id", help="lock ID to present")

    force = sub.add_parser("force-unlock", help="remove a lock directly in the data directory (admin)")
    force.add_argument("--data", required=True)
    force.add_argument("name")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        if args.command == "serve":
            return serve(args)
        if args.command == "force-unlock":
            return cmd_force_unlock(args)
        return COMMANDS[args.command](client_from(args), args)
    except (ClientError, StoreError) as exc:
        print(f"statesrv: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"statesrv: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
