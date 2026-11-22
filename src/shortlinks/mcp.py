"""A minimal MCP server over Streamable HTTP, stateless, JSON responses only.

Implements the subset of the Model Context Protocol an agent needs to call
tools: initialize, ping, tools/list and tools/call. Each POST carries one
JSON-RPC message and gets one JSON response; there is no session, no
server-to-client stream (GET answers 405, which the spec allows), and no
batching (removed from the protocol in 2025-06-18).
"""

from __future__ import annotations

import json
from typing import Any

from . import __version__
from .config import Client
from .service import Service, ServiceError

# 2025-03-26 is not offered: it requires accepting JSON-RPC batches.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18")

INSTRUCTIONS = (
    "Creates and looks up short links on this host. Use shortlink_create to get a "
    "short URL for a long one, for example a published report's stable link; "
    "it returns the existing short link when the target already has one."
)

_CODE = {"type": "string", "description": "Short code, case-insensitive, e.g. K7M2Q."}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "shortlink_create",
        "title": "Create a short link",
        "description": (
            "Create a short link to an https URL on an allowed host, or return the "
            "existing one for the same target. Optionally choose the code (letters, "
            "digits, inner hyphens). Returns the short URL."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "The long URL to redirect to."},
                "code": {**_CODE, "description": "Optional custom code."},
            },
            "required": ["target"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "shortlink_get",
        "title": "Look up a short link",
        "description": "Show a short link's target and its change history.",
        "inputSchema": {
            "type": "object",
            "properties": {"code": _CODE},
            "required": ["code"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "shortlink_list",
        "title": "List short links",
        "description": "List stored short links (pattern routes are not listed).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_deleted": {"type": "boolean", "description": "Also list deleted codes."}
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "shortlink_update",
        "title": "Change a short link's target",
        "description": (
            "Point an existing short link at a new target. Anything already printed "
            "or shared with this code follows the change."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"code": _CODE, "target": {"type": "string"}},
            "required": ["code", "target"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
    },
]

_TOOL_NAMES = {t["name"] for t in TOOLS}

# JSON-RPC error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class MCPHandler:
    def __init__(self, service: Service):
        self.service = service

    def handle(self, client: Client, body: bytes) -> tuple[int, dict | None]:
        """Handle one POSTed message. Returns (HTTP status, response).

        A request gets 200 and a JSON-RPC response. A notification or a
        client's response gets 202 and no body. A message the server cannot
        accept at all gets 400 with a JSON-RPC error, as Streamable HTTP asks.
        """
        try:
            msg = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            return 400, _error(None, PARSE_ERROR, "parse error")
        if isinstance(msg, list):
            return 400, _error(None, INVALID_REQUEST, "batching is not supported")
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return 400, _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        if "method" not in msg:
            if "result" in msg or "error" in msg:
                return 202, None  # a response; we never send requests, so ignore it
            return 400, _error(msg.get("id"), INVALID_REQUEST, "message has no method")
        if "id" not in msg:
            return 202, None  # notification, e.g. notifications/initialized
        req_id = msg["id"]
        method = msg["method"]
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            return 200, _error(req_id, INVALID_PARAMS, "params must be an object")

        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return 200, _result(req_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "shortlinks", "title": "Short links", "version": __version__},
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return 200, _result(req_id, {})
        if method == "tools/list":
            return 200, _result(req_id, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name not in _TOOL_NAMES:
                return 200, _error(req_id, INVALID_PARAMS, f"unknown tool: {name}")
            if not isinstance(args, dict):
                return 200, _error(req_id, INVALID_PARAMS, "arguments must be an object")
            return 200, _result(req_id, self._call(client, name, args))
        return 200, _error(req_id, METHOD_NOT_FOUND, f"method not found: {method}")

    def _call(self, client: Client, name: str, args: dict) -> dict:
        svc = self.service
        try:
            if name == "shortlink_create":
                link, created = svc.create(client, args.get("target"), args.get("code"))
                data = {**svc.link_json(link), "created": created}
            elif name == "shortlink_get":
                data = svc.link_json(svc.get(client, args.get("code")), history=True)
            elif name == "shortlink_list":
                include = args.get("include_deleted", False)
                if not isinstance(include, bool):
                    raise ServiceError(400, "include_deleted must be true or false")
                links = svc.list(client, include_deleted=include)
                data = {"links": [svc.link_json(link) for link in links]}
            else:  # shortlink_update
                data = svc.link_json(svc.update(client, args.get("code"), args.get("target")))
        except ServiceError as e:
            # Tool failures are results with isError, so the model sees them.
            return {"content": [{"type": "text", "text": e.message}], "isError": True}
        return {
            "content": [{"type": "text", "text": json.dumps(data, indent=2)}],
            "structuredContent": data,
            "isError": False,
        }


def _result(req_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
