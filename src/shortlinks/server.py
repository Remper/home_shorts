"""HTTP front end.

Routes:
  GET|HEAD /healthz                 200 "ok"
  GET|HEAD /<code>                  302 to the target, or 404
  GET|POST /api/v1/links            list / create         (bearer token)
  GET|PATCH|DELETE /api/v1/links/<code>                   (bearer token)
  POST     /mcp                     MCP, Streamable HTTP  (bearer token)

Plain HTTP on loopback by default: TLS belongs to the reverse proxy in front.
Redirects are not logged (no per-scan analytics); writes are, by the service.
"""

from __future__ import annotations

import http.server
import json
import logging
import socket
import socketserver
import urllib.parse

from . import __version__
from .config import Client, Config
from .mcp import INVALID_REQUEST, PROTOCOL_VERSIONS, MCPHandler
from .mcp import _error as mcp_error
from .service import Service, ServiceError
from .store import Store

log = logging.getLogger("shortlinks")

MAX_BODY = 64 * 1024
NOT_FOUND_BODY = b"not found\n"
API_PREFIX = "/api/v1/links"

# Sent with every response. no-store matters most on redirects: a phone must
# never replay an old target after a link has been changed.
COMMON_HEADERS = (
    ("Cache-Control", "no-store"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Content-Type-Options", "nosniff"),
)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = f"shortlinks/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    # Per-read socket timeout. Short, because each connection holds a
    # thread: a client that dribbles its request cannot pin many of them.
    timeout = 10

    # Set on the subclass made by make_server().
    service: Service
    mcp: MCPHandler

    # --- dispatch ----------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def _dispatch(self, method: str) -> None:
        self._body_read = False
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/mcp":
                self._mcp(method)
            elif path == API_PREFIX or path.startswith(API_PREFIX + "/"):
                self._api(method, path)
            elif path == "/healthz":
                if method not in ("GET", "HEAD"):
                    return self._status(405, allow="GET, HEAD")
                self._send(200, b"ok\n", "text/plain; charset=utf-8")
            else:
                self._redirect(method, path)
        except Exception:
            # The path is not logged: it may be a short code, and redirects
            # leave no trace here.
            log.exception("error handling %s request", method)
            self._json(500, {"error": "internal error"})

    # --- redirects ---------------------------------------------------------

    def _redirect(self, method: str, path: str) -> None:
        if method not in ("GET", "HEAD"):
            return self._status(405, allow="GET, HEAD")
        target = self.service.resolve(path[1:]) if path.startswith("/") else None
        if target is None:
            return self._send(404, NOT_FOUND_BODY, "text/plain; charset=utf-8")
        self._send(302, b"", "text/plain; charset=utf-8", location=target)

    # --- REST API ----------------------------------------------------------

    def _api(self, method: str, path: str) -> None:
        client = self._client()
        if client is None:
            return
        svc = self.service
        rest = path[len(API_PREFIX):]
        try:
            if rest in ("", "/"):
                if method == "GET":
                    q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    include = q.get("include_deleted", ["0"])[0] in ("1", "true", "yes")
                    links = svc.list(client, include_deleted=include)
                    return self._json(200, {"links": [svc.link_json(x) for x in links]})
                if method == "POST":
                    body = self._body_json()
                    link, created = svc.create(client, body.get("target"), body.get("code"))
                    return self._json(201 if created else 200, {**svc.link_json(link), "created": created})
                return self._status(405, allow="GET, POST")

            code = urllib.parse.unquote(rest[1:])
            if method == "GET":
                return self._json(200, svc.link_json(svc.get(client, code), history=True))
            if method == "PATCH":
                body = self._body_json()
                link = svc.update(client, code, body.get("target"))
                return self._json(200, svc.link_json(link))
            if method == "DELETE":
                link = svc.delete(client, code)
                return self._json(200, svc.link_json(link))
            return self._status(405, allow="GET, PATCH, DELETE")
        except ServiceError as e:
            self._json(e.status, {"error": e.message})

    def _body_json(self) -> dict:
        body = self._body()
        try:
            data = json.loads(body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            raise ServiceError(400, "body must be JSON") from None
        if not isinstance(data, dict):
            raise ServiceError(400, "body must be a JSON object")
        return data

    # --- MCP ---------------------------------------------------------------

    def _mcp(self, method: str) -> None:
        if method != "POST":
            # No server-initiated stream and no sessions to end.
            return self._status(405, allow="POST")
        client = self._client()
        if client is None:
            return
        try:
            body = self._body()
        except ServiceError as e:
            return self._json(e.status, {"error": e.message})
        version = self.headers.get("MCP-Protocol-Version")
        if version is not None and version not in PROTOCOL_VERSIONS:
            return self._json(400, mcp_error(None, INVALID_REQUEST, f"unsupported MCP-Protocol-Version {version}"))
        status, response = self.mcp.handle(client, body)
        if response is None:
            return self._send(status, b"", "text/plain; charset=utf-8")
        self._json(status, response)

    # --- plumbing ----------------------------------------------------------

    def _client(self) -> Client | None:
        """Authenticate the request, or answer it and return None."""
        origin = self.headers.get("Origin")
        if origin is not None and origin.rstrip("/").lower() != self.service.cfg.public_base_url:
            # Browsers send Origin on cross-site requests; refusing foreign
            # origins stops a web page from driving the API through a
            # victim's browser (DNS rebinding included). The MCP spec asks
            # servers to check it.
            self._json(403, {"error": "origin not allowed"})
            return None
        client = self.service.cfg.authenticate(self.headers.get("Authorization"))
        if client is None:
            self._json(401, {"error": "missing or invalid bearer token"},
                       extra=(("WWW-Authenticate", 'Bearer realm="shortlinks"'),))
        return client

    def _body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding"):
            raise ServiceError(411, "chunked bodies are not supported; send Content-Length")
        try:
            n = int(length or "0")
        except ValueError:
            raise ServiceError(400, "bad Content-Length") from None
        if n < 0 or n > MAX_BODY:
            self.close_connection = True
            raise ServiceError(413, f"body larger than {MAX_BODY} bytes")
        self._body_read = True
        return self.rfile.read(n)

    def _status(self, status: int, allow: str | None = None) -> None:
        extra = (("Allow", allow),) if allow else ()
        self._json(status, {"error": http.HTTPStatus(status).phrase.lower()}, extra=extra)

    def _json(self, status: int, data: dict, extra: tuple = ()) -> None:
        body = json.dumps(data, indent=2).encode() + b"\n"
        self._send(status, body, "application/json", extra=extra)

    def _send(self, status: int, body: bytes, ctype: str, location: str | None = None,
              extra: tuple = ()) -> None:
        if not self._body_read and (self.headers.get("Content-Length", "0") != "0"
                                    or self.headers.get("Transfer-Encoding")):
            # A request body we did not read would be parsed as the next
            # request on this connection; close it instead.
            self.close_connection = True
        self.send_response(status)
        for k, v in COMMON_HEADERS:
            self.send_header(k, v)
        for k, v in extra:
            self.send_header(k, v)
        if location is not None:
            # Targets are validated on the way in; this guards the header
            # itself against a row that bypassed validation.
            if any(c in location for c in "\r\n\0"):
                log.error("refusing a stored target with control characters")
                return self._send(500, b"internal error\n", "text/plain; charset=utf-8")
            self.send_header("Location", location)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        # The stdlib's own errors (unknown method, malformed request line, a
        # too-long URL) get the same headers as every other response, and a
        # JSON body instead of an HTML page.
        self.close_connection = True
        if not hasattr(self, "_body_read"):
            self._body_read = True  # never read a body for these
        try:
            phrase = http.HTTPStatus(code).phrase.lower()
        except ValueError:
            phrase = "error"
        if self.command is None or not hasattr(self, "headers"):
            # Too broken to have parsed headers; answer minimally.
            self.send_response(code)
            for k, v in COMMON_HEADERS:
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        self._json(code, {"error": phrase})

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        # No access log: a scan should leave no trace beyond the proxy's own.
        pass

    def log_error(self, format: str, *args) -> None:  # noqa: A002
        # The stdlib passes the request line here, which carries the code.
        log.warning("rejected a malformed request")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Server6(Server):
    address_family = socket.AF_INET6


def make_server(cfg: Config, store: Store, host: str | None = None, port: int | None = None) -> Server:
    service = Service(cfg, store)
    handler = type("BoundHandler", (Handler,), {"service": service, "mcp": MCPHandler(service)})
    host = host or cfg.listen
    cls = Server6 if ":" in host else Server
    return cls((host, cfg.port if port is None else port), handler)
