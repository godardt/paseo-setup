#!/usr/bin/env python3
"""Fixed-workspace, GET-only Plane MCP server, using only the Python stdlib."""

import argparse
import http.client
import json
import math
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from claude_codex import SetupError, say

SERVER_NAME = "claude-codex-plane-peppy-readonly"
BASE_URL = "https://api.plane.so/api/v1/workspaces/peppy/"
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
HTTP_TIMEOUT = 15
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_FRAME_BYTES = 64 * 1024
MAX_CREDENTIAL_BYTES = 64 * 1024
MAX_API_KEY_LENGTH = 4096
DEFAULT_PER_PAGE = 100
UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
UUID_SCHEMA = {"type": "string", "format": "uuid", "pattern": "^" + UUID_PATTERN + "$"}
PAGINATION_PROPERTIES = {
    "cursor": {"type": "string", "minLength": 1, "maxLength": 1024,
               "description": "Opaque cursor from the previous response; never a URL to follow."},
    "per_page": {"type": "integer", "minimum": 1, "maximum": 100, "default": DEFAULT_PER_PAGE},
}
TOOLS = [
    {"name": "list_projects", "description": "List projects in the peppy Plane workspace, one page at a time.",
     "inputSchema": {"type": "object", "properties": PAGINATION_PROPERTIES, "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "list_work_items", "description": "List a peppy project's work items, including pagination metadata.",
     "inputSchema": {"type": "object", "properties": {"project_id": UUID_SCHEMA, **PAGINATION_PROPERTIES},
                     "required": ["project_id"], "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "get_work_item", "description": "Read one work item in a peppy project.",
     "inputSchema": {"type": "object", "properties": {"project_id": UUID_SCHEMA, "work_item_id": UUID_SCHEMA},
                     "required": ["project_id", "work_item_id"], "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
]


def validate_api_key(value):
    """Normalize surrounding whitespace; never include a credential in errors."""
    if not isinstance(value, str):
        raise SetupError("Plane API key must be a nonempty string of visible ASCII characters.")
    value = value.strip()
    if not value or len(value) > MAX_API_KEY_LENGTH or any(not 33 <= ord(c) <= 126 for c in value):
        raise SetupError("Plane API key must contain 1–4096 visible ASCII characters without spaces.")
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite JSON number")


def _safe_json(value, api_key, depth=0):
    # Bound nesting independently of Python's JSON parser/recursion implementation.
    if depth > 64 or (isinstance(value, float) and not math.isfinite(value)):
        raise ValueError("JSON exceeds supported limits")
    if isinstance(value, str):
        return value.replace(api_key, "[REDACTED]") if api_key else value
    if isinstance(value, list):
        return [_safe_json(item, api_key, depth + 1) for item in value]
    if isinstance(value, dict):
        return {_safe_json(key, api_key, depth + 1): _safe_json(item, api_key, depth + 1)
                for key, item in value.items()}
    return value


def _parse_json(data, api_key=None):
    value = json.loads(data.decode("utf-8"), object_pairs_hook=_object, parse_constant=_invalid_constant)
    return _safe_json(value, api_key)


def load_credentials(path):
    """Read the dedicated credential file without leaking its path or contents."""
    try:
        path = Path(path)
        if not path.is_absolute():
            raise ValueError("Absolute path required")
        with path.open("rb") as handle:
            data = handle.read(MAX_CREDENTIAL_BYTES + 1)
        if len(data) > MAX_CREDENTIAL_BYTES:
            raise ValueError("Credential file too large")
        value = _parse_json(data)
        if not isinstance(value, dict) or set(value) != {"workspace", "api_key"} or value["workspace"] != "peppy":
            raise ValueError("Invalid credential object")
    except (OSError, ValueError, TypeError, RecursionError):
        # The shared read_json includes exception details, which may contain secrets.
        raise SetupError("Cannot load Plane credentials; expected an absolute JSON file for workspace peppy.") from None
    return validate_api_key(value["api_key"])


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ToolError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Returning None lets urllib raise HTTPError without forwarding the API key.
        return None


def _params(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise RpcError(-32602, "Invalid or unexpected parameters.")
    if "_meta" in value and not isinstance(value["_meta"], dict):
        raise RpcError(-32602, "Invalid request metadata.")


def _uuid(value):
    if not isinstance(value, str) or re.fullmatch(UUID_PATTERN, value) is None:
        raise RpcError(-32602, "Project and work item IDs must be canonical UUID strings.")
    return value.lower()


def _http_error(status):
    if 300 <= status < 400:
        return ToolError("Plane redirects are not allowed.")
    if status in (401, 403):
        return ToolError(f"Plane authentication or access was rejected (HTTP {status}).")
    if status == 429:
        return ToolError("Plane rate limit reached (HTTP 429); retry later.")
    return ToolError(f"Plane request failed (HTTP {status}).")


class PlaneServer:
    def __init__(self, api_key):
        self.api_key = validate_api_key(api_key)
        # Do not inherit proxy routing or redirect behavior from the environment.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.initialized = False
        self.ready = False

    def call_tool(self, name, arguments):
        # Validation here (not only in inputSchema) is the read-only boundary.
        if name == "list_projects":
            _params(arguments, {"cursor", "per_page"})
            route = "projects/"
        elif name == "list_work_items":
            _params(arguments, {"project_id", "cursor", "per_page"}, {"project_id"})
            route = f"projects/{_uuid(arguments['project_id'])}/work-items/"
        elif name == "get_work_item":
            _params(arguments, {"project_id", "work_item_id"}, {"project_id", "work_item_id"})
            route = f"projects/{_uuid(arguments['project_id'])}/work-items/{_uuid(arguments['work_item_id'])}/"
        else:
            raise RpcError(-32602, "Unknown tool.")
        if name != "get_work_item":
            per_page = arguments.get("per_page", DEFAULT_PER_PAGE)
            if type(per_page) is not int or not 1 <= per_page <= 100:
                raise RpcError(-32602, "per_page must be an integer between 1 and 100.")
            query = {"per_page": per_page}
            if "cursor" in arguments:
                cursor = arguments["cursor"]
                if (not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024
                        or any(not 33 <= ord(c) <= 126 for c in cursor)):
                    raise RpcError(-32602, "cursor must be a nonempty visible ASCII string of at most 1024 characters.")
                query["cursor"] = cursor
            route += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(BASE_URL + route, method="GET", headers={
            "X-API-Key": self.api_key, "Accept": "application/json", "User-Agent": SERVER_NAME + "/1.0",
        })
        try:
            with self.opener.open(request, timeout=HTTP_TIMEOUT) as response:
                if response.geturl() != request.full_url:
                    raise ToolError("Plane redirects are not allowed.")
                if not 200 <= response.status < 300:
                    raise _http_error(response.status)
                data = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise _http_error(status) from None
        except (TimeoutError, urllib.error.URLError) as exc:
            if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError):
                raise ToolError("Plane request timed out.") from None
            raise ToolError("Cannot connect to Plane.") from None
        except (OSError, http.client.HTTPException):
            raise ToolError("Plane request failed while receiving the response.") from None
        if len(data) > MAX_RESPONSE_BYTES:
            raise ToolError("Plane response exceeds the size limit.")
        try:
            # Redact even JSON-escaped credentials echoed in a successful response.
            result = _parse_json(data, self.api_key)
            if not isinstance(result, dict):
                raise ValueError("Expected an object")
        except (ValueError, RecursionError):
            raise ToolError("Plane returned an invalid JSON object.") from None
        # Keep the complete page and metadata. Never fetch next/previous URLs.
        return result

    def dispatch(self, method, params):
        if method == "ping":
            _params(params, {"_meta"})
            return {}
        if method == "initialize":
            _params(params, {"protocolVersion", "capabilities", "clientInfo", "_meta"},
                    {"protocolVersion", "capabilities", "clientInfo"})
            info = params["clientInfo"]
            if (not isinstance(params["protocolVersion"], str) or not params["protocolVersion"]
                    or not isinstance(params["capabilities"], dict) or not isinstance(info, dict)
                    or any(not isinstance(info.get(key), str) or not info[key] for key in ("name", "version"))):
                raise RpcError(-32602, "Invalid initialization parameters.")
            if self.initialized:
                raise RpcError(-32600, "Server is already initialized.")
            version = params["protocolVersion"]
            if version not in PROTOCOL_VERSIONS:
                version = PROTOCOL_VERSIONS[0]
            self.initialized = True
            return {"protocolVersion": version, "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": "1.0.0"}}
        if method not in ("tools/list", "tools/call"):
            raise RpcError(-32601, "Method not found.")
        if not self.ready:
            raise RpcError(-32002, "Server has not completed initialization.")
        if method == "tools/list":
            _params(params, {"_meta"})
            return {"tools": TOOLS}
        _params(params, {"name", "arguments", "_meta"}, {"name"})
        if not isinstance(params["name"], str):
            raise RpcError(-32602, "Tool name must be a string.")
        try:
            value = self.call_tool(params["name"], params.get("arguments", {}))
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(value, separators=(",", ":"), allow_nan=False)}],
                "isError": False}

    def handle(self, message):
        if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
                or not isinstance(message.get("method"), str) or not message["method"]
                or set(message) - {"jsonrpc", "method", "id", "params"}
                or ("id" in message and type(message["id"]) not in (str, int))):
            return _error(None, -32600, "Invalid request.")
        params = message.get("params", {})
        if "id" not in message:
            # Notifications never receive replies or run tools, even when malformed.
            if message["method"] == "notifications/initialized" and self.initialized:
                try:
                    _params(params, {"_meta"})
                    self.ready = True
                except RpcError:
                    pass
            return None
        try:
            if not isinstance(params, dict):
                raise RpcError(-32602, "Parameters must be an object.")
            result = self.dispatch(message["method"], params)
            return {"jsonrpc": "2.0", "id": message["id"], "result": result}
        except RpcError as exc:
            return _error(message["id"], exc.code, str(exc))
        except Exception:
            # Do not print tracebacks or remote response/credential details to either stream.
            return _error(message["id"], -32603, "Internal server error.")


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(server, incoming, outgoing):
    """Process bounded UTF-8 JSON lines; stdout is reserved for MCP messages."""
    while True:
        line = incoming.readline(MAX_FRAME_BYTES + 1)
        if not line:
            return 0
        oversized = len(line) > MAX_FRAME_BYTES
        if oversized:
            response = _error(None, -32700, "MCP input frame exceeds the size limit.")
        else:
            try:
                response = server.handle(_parse_json(line))
            except (ValueError, RecursionError):
                response = _error(None, -32700, "Parse error.")
        if response is not None:
            outgoing.write(json.dumps(response, separators=(",", ":"), allow_nan=False) + "\n")
            outgoing.flush()
        if oversized:
            # Do not drain arbitrarily long input or mistake its remainder for a new frame.
            return 1


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes unknown arguments; those could be accidental secrets.
        say("Invalid Plane MCP arguments; use --credentials with an absolute JSON file path.")
        raise SystemExit(2)


def main(argv=None):
    parser = _ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--credentials", required=True, help="Absolute path to the dedicated peppy credential JSON")
    args = parser.parse_args(argv)
    try:
        server = PlaneServer(load_credentials(args.credentials))
        return serve(server, sys.stdin.buffer, sys.stdout)
    except SetupError as exc:
        say(str(exc))
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
