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
from typing import Self

from coalesce.viewer.scene import ViewerSceneStore

LOOPBACK = "127.0.0.1"
KEEPALIVE_SECONDS = 15
_STATIC = files("coalesce.viewer") / "static"
_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", "": "text/plain"}
_SCENE = re.compile(r"/api/scene/(\d+)")
_MESH = re.compile(r"/api/mesh/([0-9a-f]{32})")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_PAGE_POLICY = (
    "default-src 'self'; img-src 'self' data:; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)


def _validate_token(token: str) -> None:
    if not _TOKEN.fullmatch(token):
        raise ValueError("COALESCE_VIEWER_TOKEN must be a 43-character URL-safe token")


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


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, viewer: ViewerServer, port: int) -> None:
        self.viewer = viewer
        super().__init__((LOOPBACK, port), _Handler)


class ViewerServer:
    """Serves one ``ViewerSceneStore`` on 127.0.0.1 and a free port until ``close``."""

    def __init__(
        self, store: ViewerSceneStore, port: int = 0, token: str | None = None
    ) -> None:
        self.store = store
        if token is not None:
            _validate_token(token)
        self._token = token or secrets.token_urlsafe(32)
        self._static = _static_files()
        self._closing = threading.Event()
        self._httpd = _HTTPServer(self, port)
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

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class _Handler(BaseHTTPRequestHandler):
    server: _HTTPServer
    protocol_version = "HTTP/1.1"

    @property
    def viewer(self) -> ViewerServer:
        return self.server.viewer

    def log_message(self, format, *args):
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

    def superseded(self):
        # a 200, not 404 or 204: catching up past dropped revisions is normal, and Chromium
        # logs every 204 to fetch() as an aborted request.
        self.json({"superseded": True})

    def trusted(self) -> bool:
        origin = self.headers.get("Origin")
        return self.headers.get("Host") == f"{LOOPBACK}:{self.viewer.port}" and (
            origin is None or origin == self.viewer.origin
        )

    def authorized(self) -> bool:
        prefix = f"{self.viewer.cookie_name}="
        for item in self.headers.get("Cookie", "").split(";"):
            item = item.strip()
            if item.startswith(prefix):
                return hmac.compare_digest(item[len(prefix):], self.viewer._token)
        return False

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if not self.trusted():
            return self.send(HTTPStatus.FORBIDDEN)
        if path in self.viewer._static:
            body, kind = self.viewer._static[path]
            headers = [("Cache-Control", "no-cache")]
            if path == "/":
                headers.append(("Content-Security-Policy", _PAGE_POLICY))
            return self.send(200, body, kind, headers)
        if not (path in ("/events", "/api/session") or _SCENE.fullmatch(path) or _MESH.fullmatch(path)):
            return self.send(HTTPStatus.NOT_FOUND)
        if path == "/api/session":
            # an expected state, not an error: the page asks whether its key still works.
            return self.json({"authorized": self.authorized()})
        if not self.authorized():
            return self.send(HTTPStatus.UNAUTHORIZED)
        if path == "/events":
            return self.events()
        if match := _SCENE.fullmatch(path):
            return self.scene(int(match[1]))
        mesh = self.viewer.store.mesh(_MESH.fullmatch(path)[1])
        if mesh is None:
            # ids are content hashes, so an unknown one is a mesh the store no longer keeps.
            return self.superseded()
        # content-addressed: unchanged parts are never downloaded twice.
        self.send(200, mesh, "application/octet-stream",
                  [("Cache-Control", "private, max-age=31536000, immutable")])

    def scene(self, revision: int):
        if scene := self.viewer.store.scene(revision):
            return self.json(scene)
        current = self.viewer.store.current_revision
        if current is not None and revision < current:
            return self.superseded()
        self.send(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if self.path != "/api/session":
            return self.send(HTTPStatus.NOT_FOUND)
        # a session needs our exact Origin, not just "no foreign Origin".
        if not self.trusted() or self.headers.get("Origin") != self.viewer.origin:
            return self.send(HTTPStatus.FORBIDDEN)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            token = json.loads(self.rfile.read(min(length, 4096)))["token"]
        except (ValueError, KeyError, TypeError):
            return self.send(HTTPStatus.BAD_REQUEST)
        if not isinstance(token, str):
            return self.send(HTTPStatus.BAD_REQUEST)
        if not hmac.compare_digest(token, self.viewer._token):
            return self.json({"authorized": False})
        cookie = f"{self.viewer.cookie_name}={self.viewer._token}; Path=/; HttpOnly; SameSite=Strict"
        self.send(200, b'{"authorized": true}', "application/json",
                  [("Cache-Control", "no-store"), ("Set-Cookie", cookie)])

    def events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.close_connection = True
        sent, version, idle = (None, None), None, 0.0
        try:
            while not self.viewer._closing.is_set():
                if version is not None and not self.viewer.store.wait_for_change(version, 1):
                    idle += 1
                    if idle >= KEEPALIVE_SECONDS:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        idle = 0
                    continue
                first_snapshot = version is None
                previous_version = version
                version, revision, failure = self.viewer.store.snapshot()
                visible_failure = _without_diagnostic_geometry(failure) if first_snapshot else failure
                previous_sent = sent
                out, sent = _pending_events(revision, visible_failure, sent)
                out = _refresh_event(
                    first_snapshot,
                    version,
                    previous_version,
                    revision,
                    failure,
                    previous_sent,
                    out,
                )
                if first_snapshot:
                    sent = (revision, failure)
                if out:
                    self.wfile.write(out)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def _without_diagnostic_geometry(failure: dict | None) -> dict | None:
    if failure is None:
        return None
    return {
        key: value
        for key, value in failure.items()
        if key not in {"diagnosticGeometry", "diagnosticGeometryOmitted"}
    }


def _refresh_event(first_snapshot, version, previous_version, revision, failure, sent, out):
    if (
        not first_snapshot
        and version != previous_version
        and revision == sent[0]
        and failure is sent[1]
        and not out
    ):
        return _event("revision", {"revision": revision, "refresh": True})
    return out


def _pending_events(revision, failure, sent):
    """Event bytes for what changed since ``sent`` = (revision, failure), and the new ``sent``.

    A new revision is announced once; a failure is announced when it is a new failure object.
    """
    sent_revision, sent_failure = sent
    out = b""
    if revision is not None and revision != sent_revision:
        out += _event("revision", {"revision": revision})
        sent_revision = revision
    if failure is not None and failure is not sent_failure:
        out += _event("failure", failure)
    return out, (sent_revision, failure)


def _event(name: str, data: dict) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
