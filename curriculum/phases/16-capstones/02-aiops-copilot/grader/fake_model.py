"""A scripted fake of the Claude Messages API for the grader.

It listens on 127.0.0.1, answers POST /v1/messages with the next scripted
response, validates the parts of the request the real API would reject
(missing key or version, sampling parameters on 2026-10-21, a `thinking`
setting Claude Opus 5.5 refuses, forced tool_choice, tool_use ids without
matching tool_result blocks) and records every request so checks can assert
on exactly what the copilot sent. Standard library only.
"""
from __future__ import annotations

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

FAKE_API_KEY = "cc-grader-fake-key"
RECORDED_HEADERS = ("x-api-key", "anthropic-version", "anthropic-beta", "content-type", "authorization")


def text_block(text: str) -> Dict[str, Any]:
    return {"type": "text", "text": text}


def thinking_block(signature: str) -> Dict[str, Any]:
    return {"type": "thinking", "thinking": "", "signature": signature}


def tool_use(block_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "tool_use", "id": block_id, "name": name, "input": arguments}


def turn(content: List[Dict[str, Any]], stop_reason: Optional[str] = None, input_tokens: int = 50, output_tokens: int = 20) -> Dict[str, Any]:
    if stop_reason is None:
        stop_reason = "tool_use" if any(block.get("type") == "tool_use" for block in content) else "end_turn"
    return {"content": content, "stop_reason": stop_reason, "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}


def api_error(kind: str, message: str) -> Dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": message}}


def blocks_of(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    content = message.get("content")
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def validate(headers: Dict[str, str], body: Any) -> Optional[Tuple[int, Dict[str, Any]]]:
    if not headers.get("x-api-key") and not headers.get("authorization"):
        return 401, api_error("authentication_error", "x-api-key header is required")
    if not headers.get("anthropic-version"):
        return 400, api_error("invalid_request_error", "anthropic-version header is required")
    if not isinstance(body, dict):
        return 400, api_error("invalid_request_error", "request body must be a JSON object")
    for key in ("model", "max_tokens", "messages"):
        if key not in body:
            return 400, api_error("invalid_request_error", f"{key}: Field required")
    if headers.get("anthropic-version", "") >= "2026-10-21":
        for key in ("temperature", "top_p", "top_k"):
            if key in body:
                return 400, api_error("invalid_request_error", f"{key}: not supported on API version {headers['anthropic-version']}")
    thinking = body.get("thinking")
    if thinking is not None and not (isinstance(thinking, dict) and thinking.get("type") == "adaptive"):
        return 400, api_error("invalid_request_error", "thinking: only adaptive thinking is supported on this model; omit the parameter and set output_config.effort")
    choice = body.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") in ("any", "tool"):
        return 400, api_error("invalid_request_error", 'tool_choice: type "tool" and "any" are not supported for this model.')
    if "fallbacks" in body and "server-side-fallback-2026-07-01" not in headers.get("anthropic-beta", ""):
        return 400, api_error("invalid_request_error", "fallbacks: requires the anthropic-beta header server-side-fallback-2026-07-01")
    messages = body["messages"]
    if not isinstance(messages, list) or not messages:
        return 400, api_error("invalid_request_error", "messages: at least one message is required")
    if not isinstance(messages[0], dict) or messages[0].get("role") != "user":
        return 400, api_error("invalid_request_error", "messages.0.role: the first message must use the user role")
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        ids = [block.get("id") for block in blocks_of(message) if block.get("type") == "tool_use"]
        if not ids:
            continue
        following = messages[index + 1] if index + 1 < len(messages) else None
        answered = [block.get("tool_use_id") for block in blocks_of(following)] if isinstance(following, dict) and following.get("role") == "user" else []
        missing = [block_id for block_id in ids if block_id not in answered]
        if missing:
            return 400, api_error("invalid_request_error", f"messages.{index + 1}: `tool_use` ids were found without `tool_result` blocks immediately after: {', '.join(str(m) for m in missing)}. Each `tool_use` block must have a corresponding `tool_result` block in the next message.")
    return None


class FakeModel:
    def __init__(self, script: List[Dict[str, Any]], repeat_last: bool = False) -> None:
        self.script = [copy.deepcopy(item) for item in script]
        self.repeat_last = repeat_last
        self.requests: List[Dict[str, Any]] = []
        self.sent: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def next_response(self, body: Dict[str, Any]) -> Dict[str, Any]:
        position = len(self.sent)
        if position < len(self.script):
            item = copy.deepcopy(self.script[position])
        elif self.repeat_last and self.script:
            item = copy.deepcopy(self.script[-1])
            for block in item["content"]:
                if block.get("type") == "tool_use":
                    block["id"] = f"{block['id']}_{position + 1}"
        else:
            item = turn([text_block("(the grader's script is exhausted)")], "end_turn")
        response = {
            "id": f"msg_ccgrade_{position + 1:03d}",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "claude-opus-5-5"),
            "content": item["content"],
            "stop_reason": item["stop_reason"],
            "stop_sequence": None,
            "stop_details": item.get("stop_details"),
            "usage": item.get("usage") or {"input_tokens": 0, "output_tokens": 0},
        }
        self.sent.append(response)
        return response

    def handle(self, path: str, headers: Dict[str, str], raw: bytes) -> Tuple[int, Dict[str, Any]]:
        try:
            body: Any = json.loads(raw.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = None
        with self.lock:
            record: Dict[str, Any] = {"path": path, "headers": {k: v for k, v in headers.items() if k in RECORDED_HEADERS}, "body": body, "raw": raw.decode("utf-8", "replace")}
            self.requests.append(record)
            if path.rstrip("/") != "/v1/messages":
                record["status"] = 404
                return 404, api_error("not_found_error", f"no route {path}; the copilot must POST to $COPILOT_MODEL_URL/v1/messages")
            problem = validate(headers, body)
            if problem:
                record["status"] = problem[0]
                record["error"] = problem[1]["error"]["message"]
                return problem
            record["status"] = 200
            return 200, self.next_response(body)

    def start(self) -> str:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                return None

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                headers = {key.lower(): value for key, value in self.headers.items()}
                status, payload = owner.handle(self.path, headers, raw)
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                data = json.dumps(api_error("not_found_error", "use POST /v1/messages")).encode("utf-8")
                self.send_response(404)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.url

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None

    def accepted(self) -> List[Dict[str, Any]]:
        return [record for record in self.requests if record.get("status") == 200]

    def __enter__(self) -> "FakeModel":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()
