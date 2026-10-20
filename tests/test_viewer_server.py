import http.client
import inspect
import json
import time

import pytest
from build123d import Align, Box

from coalesce.viewer import ScenePart, ViewerSceneStore, ViewerServer
from coalesce.viewer.server import _pending_events


def plate(height: float = 6) -> Box:
    return Box(60, 40, height, align=(Align.CENTER, Align.CENTER, Align.MIN))


@pytest.fixture
def served():
    store = ViewerSceneStore()
    with ViewerServer(store) as server:
        yield store, server


def token_of(server: ViewerServer) -> str:
    return server.url.split("#token=", 1)[1]


def call(server, method, path, *, headers=None, body=None, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    headers = {"Host": f"127.0.0.1:{server.port}", **(headers or {})}
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    return response.status, dict(response.getheaders()), response.read()


def login(server) -> str:
    status, headers, _ = call(
        server,
        "POST",
        "/api/session",
        headers={"Origin": server.origin, "Content-Type": "application/json"},
        body=json.dumps({"token": token_of(server)}),
    )
    assert status == 200
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    return cookie.split(";", 1)[0]


class EventStream:
    def __init__(self, server, cookie):
        self.conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        self.conn.request(
            "GET", "/events", headers={"Host": f"127.0.0.1:{server.port}", "Cookie": cookie}
        )
        self.sock = self.conn.sock  # the response takes the socket over for streaming
        self.response = self.conn.getresponse()
        assert self.response.status == 200
        assert self.response.getheader("Content-Type") == "text/event-stream"

    def next(self, timeout: float = 10):
        self.sock.settimeout(timeout)
        name, data = None, None
        while True:
            line = self.response.fp.readline().decode().rstrip("\n")
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:])
            elif line == "" and name:
                return name, data

    def close(self):
        self.response.close()
        self.sock.close()


def test_late_subscriber_gets_full_current_scene(served):
    store, server = served
    store.publish(1, [ScenePart("plate", plate())])
    store.publish(2, [ScenePart("plate", plate(10)), ScenePart("lid", plate(2), color="#a9a08e")])
    cookie = login(server)

    stream = EventStream(server, cookie)
    assert stream.next() == ("revision", {"revision": 2})
    stream.close()

    status, headers, body = call(server, "GET", "/api/scene/2", cookie=cookie)
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    scene = json.loads(body)
    assert [p["name"] for p in scene["parts"]] == ["plate", "lid"]
    assert scene["bbox"]["max"][2] == pytest.approx(10)
    for part in scene["parts"]:
        status, _, mesh = call(server, "GET", part["mesh"]["url"], cookie=cookie)
        assert status == 200
        assert mesh == store.mesh(part["mesh"]["id"])
    assert call(server, "GET", "/api/scene/1", cookie=cookie)[0] == 200
    assert call(server, "GET", "/api/scene/3", cookie=cookie)[0] == 404


def test_dropped_revision_and_mesh_answer_superseded_not_404(served):
    store, server = served
    store.publish(1, [ScenePart("plate", plate(6))])
    first_mesh = store.scene(1)["parts"][0]["mesh"]["url"]
    store.publish(2, [ScenePart("plate", plate(8))])
    store.publish(3, [ScenePart("plate", plate(10))])  # the store keeps 2 and 3 only
    cookie = login(server)

    for path in ["/api/scene/1", first_mesh]:
        status, _, body = call(server, "GET", path, cookie=cookie)
        assert (status, json.loads(body)) == (200, {"superseded": True})
    assert call(server, "GET", "/api/scene/2", cookie=cookie)[0] == 200
    assert call(server, "GET", "/api/scene/4", cookie=cookie)[0] == 404  # not published yet


def test_pending_events_announce_each_revision_and_failure_once():
    failure = {"revision": 1, "featureIds": ["f"], "error": "e"}
    out, sent = _pending_events(1, None, (None, None))
    assert out == b'event: revision\ndata: {"revision": 1}\n\n'
    assert _pending_events(1, None, sent) == (b"", sent)

    out, sent = _pending_events(1, failure, sent)
    assert out.startswith(b"event: failure\n") and out.count(b"event:") == 1
    assert _pending_events(1, failure, sent)[0] == b""
    # an equal but new failure object is a new failed attempt
    assert _pending_events(1, dict(failure), sent)[0].startswith(b"event: failure\n")
    out, sent = _pending_events(2, None, sent)
    assert out == b'event: revision\ndata: {"revision": 2}\n\n' and sent == (2, None)


def test_second_publish_emits_exactly_one_revision_event(served):
    store, server = served
    store.publish(1, [ScenePart("plate", plate())])
    stream = EventStream(server, login(server))
    assert stream.next() == ("revision", {"revision": 1})

    store.publish(2, [ScenePart("plate", plate(10))])

    assert stream.next() == ("revision", {"revision": 2})
    with pytest.raises(TimeoutError):
        stream.next(timeout=1.5)
    stream.close()


def test_failure_event_keeps_last_good_scene(served):
    store, server = served
    store.publish(1, [ScenePart("plate", plate())])
    cookie = login(server)
    stream = EventStream(server, cookie)
    assert stream.next() == ("revision", {"revision": 1})

    store.publish_failure(
        ["edge_fillet"],
        {"code": "INVALID_PARAMETER", "message": "fillet radius too large"},
    )

    assert stream.next() == (
        "failure",
        {
            "revision": 1,
            "featureIds": ["edge_fillet"],
            "error": {"code": "INVALID_PARAMETER", "message": "fillet radius too large"},
        },
    )
    stream.close()
    status, _, body = call(server, "GET", "/api/scene/1", cookie=cookie)
    assert status == 200 and json.loads(body)["revision"] == 1
    # a late subscriber sees the current scene and the outstanding failure.
    late = EventStream(server, cookie)
    assert late.next() == ("revision", {"revision": 1})
    assert late.next()[0] == "failure"
    late.close()


def test_rejects_missing_token_bad_origin_and_bad_host(served):
    store, server = served
    store.publish(1, [ScenePart("plate", plate())])
    mesh_url = store.scene(1)["parts"][0]["mesh"]["url"]
    token = token_of(server)

    for cookie in [None, f"coalesce_{server.port}=wrong"]:
        status, _, body = call(server, "GET", "/api/session", cookie=cookie)
        assert (status, json.loads(body)) == (200, {"authorized": False})
    for path in ["/events", "/api/scene/1", mesh_url]:
        assert call(server, "GET", path)[0] == 401
        assert call(server, "GET", path, cookie=f"coalesce_{server.port}=wrong")[0] == 401

    def session(origin, value):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        status, _, body = call(server, "POST", "/api/session", headers=headers,
                    body=json.dumps({"token": value}))
        return status if status != 200 else (status, json.loads(body))

    # a wrong key is an expected answer, not a browser error
    assert session(server.origin, "wrong") == (200, {"authorized": False})
    assert "Set-Cookie" not in call(server, "POST", "/api/session", body=json.dumps({"token": "wrong"}),
                                    headers={"Origin": server.origin})[1]
    assert session("http://evil.example", token) == 403
    assert session(None, token) == 403

    cookie = login(server)
    status, _, body = call(server, "GET", "/api/session", cookie=cookie)
    assert (status, json.loads(body)) == (200, {"authorized": True})
    assert call(server, "GET", "/api/scene/1", cookie=cookie)[0] == 200
    evil_origin = {"Origin": "http://evil.example"}
    assert call(server, "GET", "/api/scene/1", cookie=cookie, headers=evil_origin)[0] == 403
    # dns rebinding: the right cookie with a foreign Host header is refused.
    for host in ["evil.example", f"localhost:{server.port}", "127.0.0.1"]:
        assert call(server, "GET", "/api/scene/1", cookie=cookie, headers={"Host": host})[0] == 403

    status, headers, page = call(server, "GET", "/")
    assert status == 200 and b"<canvas" in page and token.encode() not in page
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert call(server, "GET", "/static/../scene.py")[0] == 404
    assert call(server, "GET", "/static/viewer.js")[0] == 200


def test_server_binds_loopback_only_and_keeps_token_out_of_query(served):
    _, server = served
    assert "host" not in inspect.signature(ViewerServer).parameters
    assert server.address == ("127.0.0.1", server.port)
    assert server.origin == f"http://127.0.0.1:{server.port}"
    assert server.url.startswith(server.origin + "/#token=")
    assert "?" not in server.url
    assert len(token_of(server)) >= 32
    other = ViewerServer(ViewerSceneStore())
    try:
        assert token_of(other) != token_of(server)
    finally:
        other.close()


def test_publish_does_not_wait_for_a_stalled_browser(served):
    store, server = served
    store.publish(1, [ScenePart("plate", plate())])
    stalled = EventStream(server, login(server))  # connected, never read

    start = time.monotonic()
    for revision in range(2, 12):
        store.publish(revision, [ScenePart("plate", plate(revision))])
    assert time.monotonic() - start < 5
    stalled.close()
