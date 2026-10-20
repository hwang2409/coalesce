"""Loopback-only HTTP viewer: static page, SSE revision events, scene JSON, mesh assets.

Security model: the page shell is public (it holds no geometry). Geometry needs a
per-process token. The token travels in the URL fragment, which browsers never send
or log; the page posts it once to ``/api/session`` and gets an HttpOnly, SameSite=Strict
cookie. Every request must carry the exact loopback ``Host`` (DNS rebinding) and, when
present, our own ``Origin`` (cross-site requests).
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files

from coalesce.viewer.scene import ViewerSceneStore

LOOPBACK = "127.0.0.1"
KEEPALIVE_SECONDS = 15
_STATIC = files("coalesce.viewer") / "static"
_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", "": "text/plain"}
_SCENE = re.compile(r"/api/scene/(\d+)")
_MESH = re.compile(r"/api/mesh/([0-9a-f]{32})")
_PAGE_POLICY = (
    "default-src 'self'; img-src 'self' data:; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)


def _static_files() -> dict[str, tuple[bytes, str]]:
    """Serve an allowlist built at startup, so request paths never reach the filesystem."""
    found = {}

    def walk(node, prefix):
        for child in node.iterdir():
            if child.is_dir():
                walk(child, f"{prefix}{child.name}/")
            else:
                suffix = "." + child.name.rsplit(".", 1)[1] if "." in child.name else ""
                kind = _TYPES.get(suffix, "application/octet-stream")
                found[f"/static/{prefix}{child.name}"] = (child.read_bytes(), kind)

    walk(_STATIC, "")
    found["/"] = found.pop("/static/index.html")
    return found


class ViewerServer:
    """Serves one ``ViewerSceneStore`` on 127.0.0.1 and a free port until ``close``."""

    def __init__(self, store: ViewerSceneStore, port: int = 0) -> None:
        self.store = store
        self._token = secrets.token_urlsafe(32)
        self._static = _static_files()
        self._closing = threading.Event()
        self._httpd = ThreadingHTTPServer((LOOPBACK, port), _handler(self))
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self.origin = f"http://{LOOPBACK}:{self.port}"
        self.cookie_name = f"coalesce_{self.port}"  # cookies ignore ports; keep servers apart
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def address(self) -> tuple[str, int]:
        return self._httpd.server_address[:2]

    @property
    def url(self) -> str:
        return f"{self.origin}/#token={self._token}"

    def close(self) -> None:
        self._closing.set()
        self._httpd.shutdown()
        self._httpd.server_close()

    def __enter__(self) -> ViewerServer:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _handler(server: ViewerServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            pass  # stderr belongs to the MCP host; never log request lines.

        def send(self, status, body=b"", kind="text/plain", headers=()):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def json(self, value):
            self.send(200, json.dumps(value).encode(), "application/json",
                      [("Cache-Control", "no-store")])

        def trusted(self) -> bool:
            origin = self.headers.get("Origin")
            return self.headers.get("Host") == f"{LOOPBACK}:{server.port}" and (
                origin is None or origin == server.origin
            )

        def authorized(self) -> bool:
            prefix = f"{server.cookie_name}="
            for item in self.headers.get("Cookie", "").split(";"):
                item = item.strip()
                if item.startswith(prefix):
                    return hmac.compare_digest(item[len(prefix):], server._token)
            return False

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if not self.trusted():
                return self.send(HTTPStatus.FORBIDDEN)
            if path in server._static:
                body, kind = server._static[path]
                headers = [("Cache-Control", "no-cache")]
                if path == "/":
                    headers.append(("Content-Security-Policy", _PAGE_POLICY))
                return self.send(200, body, kind, headers)
            if not (path == "/events" or _SCENE.fullmatch(path) or _MESH.fullmatch(path)):
                return self.send(HTTPStatus.NOT_FOUND)
            if not self.authorized():
                return self.send(HTTPStatus.UNAUTHORIZED)
            if path == "/events":
                return self.events()
            if match := _SCENE.fullmatch(path):
                scene = server.store.scene(int(match[1]))
                return self.json(scene) if scene else self.send(HTTPStatus.NOT_FOUND)
            mesh = server.store.mesh(_MESH.fullmatch(path)[1])
            if mesh is None:
                return self.send(HTTPStatus.NOT_FOUND)
            # content-addressed: unchanged parts are never downloaded twice.
            self.send(200, mesh, "application/octet-stream",
                      [("Cache-Control", "private, max-age=31536000, immutable")])

        def do_POST(self):
            if self.path != "/api/session":
                return self.send(HTTPStatus.NOT_FOUND)
            # a session needs our exact Origin, not just "no foreign Origin".
            if not self.trusted() or self.headers.get("Origin") != server.origin:
                return self.send(HTTPStatus.FORBIDDEN)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                token = json.loads(self.rfile.read(min(length, 4096)))["token"]
            except (ValueError, KeyError, TypeError):
                return self.send(HTTPStatus.BAD_REQUEST)
            if not isinstance(token, str) or not hmac.compare_digest(token, server._token):
                return self.send(HTTPStatus.FORBIDDEN)
            cookie = f"{server.cookie_name}={server._token}; Path=/; HttpOnly; SameSite=Strict"
            self.send(HTTPStatus.NO_CONTENT, headers=[("Set-Cookie", cookie)])

        def events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.close_connection = True
            sent_revision, sent_failure, version, idle = None, None, None, 0.0
            try:
                while not server._closing.is_set():
                    if version is not None and not server.store.wait_for_change(version, 1):
                        idle += 1
                        if idle >= KEEPALIVE_SECONDS:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            idle = 0
                        continue
                    version, revision, failure = server.store.snapshot()
                    out = b""
                    if revision is not None and revision != sent_revision:
                        out += _event("revision", {"revision": revision})
                        sent_revision = revision
                    if failure is not None and failure is not sent_failure:
                        out += _event("failure", failure)
                    sent_failure = failure
                    if out:
                        self.wfile.write(out)
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def _event(name: str, data: dict) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
