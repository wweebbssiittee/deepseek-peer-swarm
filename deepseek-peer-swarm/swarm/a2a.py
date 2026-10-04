"""A2A 1.0 JSON-RPC binding for the collective and its equal peers.

Specification: https://a2a-protocol.org/latest/specification/
This binding supports text messages and task polling. Authentication belongs to
the application's middleware; in-process callers are already trusted peers.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime
from typing import Any, Protocol
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

PROTOCOL_VERSION = "1.0"
MAX_BODY_BYTES = 256_000
MAX_TEXT_LENGTH = 64_000
MAX_PARTS = 64
DEFAULT_AGENT_IDS = tuple(f"peer-{number:02d}" for number in range(1, 11))
TASK_STATES = frozenset(
    "TASK_STATE_" + state
    for state in (
        "UNSPECIFIED", "SUBMITTED", "WORKING", "COMPLETED", "FAILED",
        "CANCELED", "INPUT_REQUIRED", "REJECTED", "AUTH_REQUIRED",
    )
)
WAIT_STATES = {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}
ERROR_REASONS = {
    -32001: "TASK_NOT_FOUND", -32002: "TASK_NOT_CANCELABLE",
    -32003: "PUSH_NOTIFICATION_NOT_SUPPORTED", -32004: "UNSUPPORTED_OPERATION",
    -32005: "CONTENT_TYPE_NOT_SUPPORTED", -32006: "INVALID_AGENT_RESPONSE",
    -32007: "EXTENDED_AGENT_CARD_NOT_CONFIGURED", -32008: "EXTENSION_SUPPORT_REQUIRED",
    -32009: "VERSION_NOT_SUPPORTED",
}
logger = logging.getLogger(__name__)


class A2ABackend(Protocol):
    public_url: str

    async def a2a_send(self, agent_id: str | None, params: dict) -> dict: ...
    async def a2a_get(self, params: dict) -> dict: ...
    async def a2a_cancel(self, params: dict) -> dict: ...
    async def a2a_list(self, params: dict) -> dict: ...


class A2AError(ValueError):
    """An intentional protocol error. Messages must never contain secrets."""

    def __init__(self, code: int, message: str, field: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


def _invalid(field: str, message: str) -> None:
    raise A2AError(-32602, message, field)


def _object(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        _invalid(field, f"{field} must be an object")
    return value


def _string(value: Any, field: str, limit: int = 512, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        _invalid(field, f"{field} must be a {'possibly empty ' if empty else 'nonempty '}string of at most {limit} characters")
    return value


def _integer(obj: dict, key: str, minimum: int = 0, maximum: int = 2_147_483_647) -> None:
    if key in obj and (type(obj[key]) is not int or not minimum <= obj[key] <= maximum):
        _invalid(key, f"{key} must be an integer from {minimum} to {maximum}")


def _strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_PARTS:
        _invalid(field, f"{field} must be an array of at most {MAX_PARTS} strings")
    for item in value:
        _string(item, field, limit=2048)
    return value


def _common(params: dict) -> None:
    if "metadata" in params:
        _object(params["metadata"], "metadata")
    if "tenant" in params:
        _string(params["tenant"], "tenant", empty=True)
        if params["tenant"]:
            raise A2AError(-32004, "Tenant routing is not supported; use a peer endpoint")


def parse_text(params: dict) -> str:
    """Validate a 1.0 SendMessage request and join its text parts.

    The limit applies to the full message, not each part independently. File and
    data parts are explicitly rejected instead of silently dropping user input.
    """
    params = _object(params, "params")
    _common(params)
    message = _object(params.get("message"), "message")
    _string(message.get("messageId"), "message.messageId")
    if message.get("role") != "ROLE_USER":
        _invalid("message.role", "Incoming messages must use ROLE_USER")
    for key in ("contextId", "taskId"):
        if key in message:
            _string(message[key], f"message.{key}")
    if "metadata" in message:
        _object(message["metadata"], "message.metadata")
    for key in ("extensions", "referenceTaskIds"):
        if key in message:
            _strings(message[key], f"message.{key}")
    parts = message.get("parts")
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS:
        _invalid("message.parts", f"message.parts must contain 1 to {MAX_PARTS} parts")
    texts = []
    for part in parts:
        part = _object(part, "message.parts[]")
        content_keys = {"text", "raw", "url", "data"}.intersection(part)
        if len(content_keys) != 1:
            _invalid("message.parts", "Each part must contain exactly one of text, raw, url, data")
        if "text" not in content_keys:
            raise A2AError(-32005, "Only text/plain message parts are supported")
        if "mediaType" in part and part["mediaType"] != "text/plain":
            raise A2AError(-32005, "Only text/plain message parts are supported")
        if "metadata" in part:
            _object(part["metadata"], "message.parts.metadata")
        if "filename" in part:
            _string(part["filename"], "message.parts.filename")
        texts.append(_string(part["text"], "message.parts.text", MAX_TEXT_LENGTH, empty=True))
    text = "\n".join(texts)
    if not text.strip() or len(text) > MAX_TEXT_LENGTH:
        _invalid("message.parts", f"Combined text must contain 1 to {MAX_TEXT_LENGTH} characters")
    if "configuration" in params:
        config = _object(params["configuration"], "configuration")
        _integer(config, "historyLength")
        if "returnImmediately" in config and type(config["returnImmediately"]) is not bool:
            _invalid("configuration.returnImmediately", "returnImmediately must be a boolean")
        if "taskPushNotificationConfig" in config:
            raise A2AError(-32003, "Push notifications are not supported; poll GetTask")
        if "acceptedOutputModes" in config:
            modes = _strings(config["acceptedOutputModes"], "configuration.acceptedOutputModes")
            if modes and not {"text/plain", "text/*", "*/*"}.intersection(modes):
                raise A2AError(-32005, "This agent produces text/plain output")
    return text


def peer_message(sender: str, recipient: str, run_id: str, text: str) -> dict:
    """Create a complete 1.0 SendMessage envelope for a peer's durable inbox.

    ROLE_USER describes the sending side of this particular protocol exchange;
    it does not turn the peer into a human or elevate its authority.
    """
    _string(sender, "sender")
    _string(recipient, "recipient")
    _string(run_id, "run_id")
    envelope = {
        "jsonrpc": "2.0", "id": str(uuid4()), "method": "SendMessage",
        "params": {
            "message": {
                "messageId": str(uuid4()), "role": "ROLE_USER",
                "contextId": run_id, "taskId": run_id,
                "parts": [{"text": text}],
                "metadata": {"sender": sender, "recipient": recipient},
            },
            "configuration": {"returnImmediately": True},
        },
    }
    parse_text(envelope["params"])
    return envelope


def _error(request_id: Any, exc: A2AError) -> dict:
    error: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.code in ERROR_REASONS:
        error["data"] = [{
            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
            "reason": ERROR_REASONS[exc.code], "domain": "a2a-protocol.org",
        }]
    elif exc.field:
        error["data"] = [{
            "@type": "type.googleapis.com/google.rpc.BadRequest",
            "fieldViolations": [{"field": exc.field, "description": exc.message}],
        }]
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _task(task: Any) -> dict:
    if (not isinstance(task, dict) or not isinstance(task.get("id"), str)
            or not task["id"] or not isinstance(task.get("status"), dict)
            or not isinstance(task["status"].get("state"), str)
            or task["status"].get("state") not in TASK_STATES):
        raise A2AError(-32006, "Backend returned an invalid task")
    return task


def _history(task: dict, length: int | None) -> dict:
    task = dict(_task(task))
    if length == 0:
        task.pop("history", None)
    elif length is not None and "history" in task:
        if not isinstance(task["history"], list):
            raise A2AError(-32006, "Backend returned invalid task history")
        task["history"] = task["history"][-length:]
    return task


def _agent_ids(backend: A2ABackend) -> tuple[str, ...]:
    return tuple(getattr(backend, "agent_ids", DEFAULT_AGENT_IDS))


async def dispatch_a2a(
    backend: A2ABackend, envelope: Any, agent_id: str | None = None,
    version: str = PROTOCOL_VERSION,
) -> dict | None:
    """Shared transport-independent dispatcher; None means a notification.

    The backend stores/executes requests and enforces task lifecycle semantics.
    KeyError maps to TaskNotFound; intentional failures use A2AError. Unexpected
    exceptions never disclose backend error text, paths, or provider secrets.
    """
    request_id = None
    notification = False
    try:
        if not isinstance(envelope, dict):
            raise A2AError(-32600, "Request payload must be a JSON-RPC object")
        candidate_id = envelope.get("id")
        if candidate_id is None or type(candidate_id) in (str, int):
            request_id = candidate_id
        else:
            raise A2AError(-32600, "JSON-RPC id must be a string, integer, or null")
        if envelope.get("jsonrpc") != "2.0" or not isinstance(envelope.get("method"), str):
            raise A2AError(-32600, "Expected jsonrpc 2.0 and a string method")
        notification = "id" not in envelope
        if version != PROTOCOL_VERSION:
            raise A2AError(-32009, "Use A2A-Version: 1.0; legacy 0.3 is not supported")
        if agent_id is not None and agent_id not in _agent_ids(backend):
            _invalid("agent_id", "Unknown peer")
        method = envelope["method"]
        params = _object(envelope.get("params", {}), "params")
        _common(params)
        if method == "SendMessage":
            parse_text(params)
            result = await backend.a2a_send(agent_id, params)
            if not isinstance(result, dict) or len({"task", "message"}.intersection(result)) != 1:
                raise A2AError(-32006, "Backend must return exactly one task or message")
            if "task" in result:
                task = _task(result["task"])
                config = params.get("configuration", {})
                # The 1.0 specification defaults to blocking; peer envelopes
                # explicitly opt into nonblocking delivery.
                while not config.get("returnImmediately", False) and task["status"]["state"] in WAIT_STATES:
                    await asyncio.sleep(0.2)
                    task = _task(await backend.a2a_get({"id": task["id"]}))
                result = {"task": _history(task, config.get("historyLength"))}
            elif (not isinstance(result["message"], dict)
                  or result["message"].get("role") != "ROLE_AGENT"
                  or not result["message"].get("messageId")
                  or not result["message"].get("contextId")
                  or not result["message"].get("parts")):
                raise A2AError(-32006, "Backend returned an invalid message")
        elif method in {"GetTask", "CancelTask"}:
            _string(params.get("id"), "id")
            _integer(params, "historyLength")
            if method == "GetTask":
                result = _history(await backend.a2a_get(params), params.get("historyLength"))
            else:
                result = _task(await backend.a2a_cancel(params))
        elif method == "ListTasks":
            _integer(params, "pageSize", 1, 100)
            _integer(params, "historyLength")
            for key in ("contextId", "pageToken"):
                if key in params:
                    _string(params[key], key, limit=4096, empty=(key == "pageToken"))
            if "status" in params and (not isinstance(params["status"], str) or params["status"] not in TASK_STATES):
                _invalid("status", "status must be an A2A TASK_STATE enum")
            if "includeArtifacts" in params and type(params["includeArtifacts"]) is not bool:
                _invalid("includeArtifacts", "includeArtifacts must be a boolean")
            if "statusTimestampAfter" in params:
                stamp = _string(params["statusTimestampAfter"], "statusTimestampAfter")
                try:
                    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        raise ValueError()
                except ValueError:
                    _invalid("statusTimestampAfter", "statusTimestampAfter must be an ISO 8601 timestamp with a timezone")
            result = await backend.a2a_list(params)
            if (not isinstance(result, dict) or not isinstance(result.get("tasks"), list)
                    or not isinstance(result.get("nextPageToken"), str)
                    or type(result.get("pageSize")) is not int or type(result.get("totalSize")) is not int):
                raise A2AError(-32006, "Backend returned an invalid task list")
            result = dict(result)
            result["tasks"] = [_history(task, params.get("historyLength")) for task in result["tasks"]]
            if not params.get("includeArtifacts", False):
                for task in result["tasks"]:
                    task.pop("artifacts", None)
        elif method in {"SendStreamingMessage", "SubscribeToTask", "GetExtendedAgentCard"}:
            raise A2AError(-32004, "This optional operation is not supported; use SendMessage and GetTask")
        elif method in {
            "CreateTaskPushNotificationConfig", "GetTaskPushNotificationConfig",
            "ListTaskPushNotificationConfigs", "DeleteTaskPushNotificationConfig",
        }:
            raise A2AError(-32003, "Push notifications are not supported; poll GetTask")
        else:
            raise A2AError(-32601, "Method not found")
        return None if notification else {"jsonrpc": "2.0", "id": request_id, "result": result}
    except A2AError as exc:
        return None if notification else _error(request_id, exc)
    except KeyError:
        return None if notification else _error(request_id, A2AError(-32001, "Task not found"))
    except Exception as exc:
        logger.error("A2A backend failure (%s)", type(exc).__name__)
        return None if notification else _error(request_id, A2AError(-32603, "Internal error"))


def agent_card(backend: A2ABackend, agent_id: str | None = None) -> dict:
    base = backend.public_url.rstrip("/")
    path = "/a2a" if agent_id is None else f"/a2a/agents/{agent_id}"
    name = "DeepSeek Peer Swarm" if agent_id is None else f"DeepSeek Swarm {agent_id}"
    return {
        "name": name,
        "description": (
            "Ten equal peers collaborate on durable tasks without a leader. "
            "Text messages, task polling, listing and cancellation are supported. "
            "Internet, file changes and command execution follow the owner's run permissions."
        ),
        "supportedInterfaces": [{"url": base + path, "protocolBinding": "JSONRPC", "protocolVersion": PROTOCOL_VERSION}],
        "version": "0.1.0",
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
        "securitySchemes": {
            "swarmToken": {"httpAuthSecurityScheme": {"scheme": "Bearer", "description": "Local harness token, not a DeepSeek API key"}},
            "swarmHeader": {"apiKeySecurityScheme": {"location": "header", "name": "X-Swarm-Token"}},
        },
        "securityRequirements": [{"schemes": {"swarmToken": {"list": []}}}, {"schemes": {"swarmHeader": {"list": []}}}],
        "skills": [{
            "id": "peer-collaboration", "name": "Peer collaboration",
            "description": "Research, implement, run experiments and review evidence within owner-granted access.",
            "tags": ["swarm", "research", "coding", "experiments"],
        }],
    }


def install_a2a(app: FastAPI, backend: A2ABackend) -> None:
    """Install public cards and authenticated-by-host JSON-RPC endpoints."""

    def card_response(request: Request, agent_id: str | None = None) -> Response:
        if agent_id is not None and agent_id not in _agent_ids(backend):
            raise HTTPException(status_code=404, detail="Unknown peer")
        card = agent_card(backend, agent_id)
        etag = '"' + hashlib.sha256(json.dumps(card, sort_keys=True).encode()).hexdigest() + '"'
        headers = {"Cache-Control": "public, max-age=60", "ETag": etag}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return JSONResponse(card, headers=headers)

    @app.get("/.well-known/agent-card.json")
    async def collective_card(request: Request) -> Response:
        return card_response(request)

    @app.get("/a2a/agents/{agent_id}/.well-known/agent-card.json")
    async def individual_card(agent_id: str, request: Request) -> Response:
        return card_response(request, agent_id)

    async def receive(request: Request, agent_id: str | None = None) -> Response:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                return JSONResponse(_error(None, A2AError(-32600, "Request body exceeds 256000 bytes")), status_code=413)
        try:
            envelope = json.loads(body, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, UnicodeError, RecursionError):
            return JSONResponse(_error(None, A2AError(-32700, "Invalid JSON payload")))
        result = await dispatch_a2a(backend, envelope, agent_id, request.headers.get("a2a-version", ""))
        headers = {"A2A-Version": PROTOCOL_VERSION}
        return Response(status_code=204, headers=headers) if result is None else JSONResponse(result, headers=headers)

    @app.post("/a2a")
    async def collective_rpc(request: Request) -> Response:
        return await receive(request)

    @app.post("/a2a/agents/{agent_id}")
    async def individual_rpc(agent_id: str, request: Request) -> Response:
        return await receive(request, agent_id)
