import asyncio
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_mcp_vertical import FEATURE, SKETCH, call_server_tool, result_json
from test_viewer_browser import playwright_module

from coalesce.document_store import Document, DocumentStore
from coalesce.graph import Cut, Extrude, Graph, Hole, HolePosition, Plane, Rectangle, Sketch
from coalesce.service import DocumentService
from coalesce.viewer import ViewerSceneStore

BROWSER_SMOKE = Path(__file__).with_name("mcp_viewer_smoke.mjs")


def _viewer_access(url: str):
    parsed = urlsplit(url)
    token = parsed.fragment.removeprefix("token=")
    request = urllib.request.Request(
        f"{parsed.scheme}://{parsed.netloc}/api/session",
        data=json.dumps({"token": token}).encode(),
        headers={"Content-Type": "application/json", "Origin": f"{parsed.scheme}://{parsed.netloc}"},
    )
    response = urllib.request.urlopen(request, timeout=2)
    cookie = SimpleCookie(response.headers["Set-Cookie"])
    name, morsel = next(iter(cookie.items()))
    headers = {"Cookie": f"{name}={morsel.value}"}
    return parsed, headers


def _get_json(parsed, headers, path):
    request = urllib.request.Request(
        f"{parsed.scheme}://{parsed.netloc}{path}", headers=headers
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.load(response)


def _wait_for_scene(parsed, headers, revision):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            return _get_json(parsed, headers, f"/api/scene/{revision}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        time.sleep(0.02)
    raise AssertionError(f"viewer did not publish revision {revision}")


def _next_viewer_event(parsed, headers):
    request = urllib.request.Request(
        f"{parsed.scheme}://{parsed.netloc}/events", headers=headers
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        event = response.readline().decode().strip()
        data = response.readline().decode().strip()
    return event.removeprefix("event: "), json.loads(data.removeprefix("data: "))


@pytest.mark.anyio
async def test_persisted_hole_publishes_viewer_scene_after_restart(tmp_path):
    path = tmp_path / "hole.coalesce.json"
    graph = Graph()
    graph.add(Sketch("plate_sketch", entities=[Rectangle("plate", 60, 40)]))
    graph.add(Extrude("plate", sketch="plate_sketch", height=3))
    graph.add(Hole("hole", target="plate", diameter=3, position=(0, 0)))
    DocumentStore(path).save(Document(graph, revision=3))
    errlog = (tmp_path / "hole.stderr").open("w+")
    parameters = StdioServerParameters(
        command=shutil.which("coalesce") or "coalesce",
        args=["--document", str(path)],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )

    async with stdio_client(parameters, errlog=errlog) as (read, write), ClientSession(
        read, write
    ) as session:
        await session.initialize()
        measurements = result_json(
            await session.call_tool("query", {"select": {"kind": "measurements"}})
        )
        assert measurements["ok"]
        errlog.seek(0)
        viewer_url = next(
            line.removeprefix("coalesce viewer: ")
            for line in errlog.read().splitlines()
            if line.startswith("coalesce viewer: ")
        )
        parsed, headers = _viewer_access(viewer_url)
        scene = await asyncio.to_thread(_wait_for_scene, parsed, headers, 3)

    assert scene["revision"] == 3
    assert scene["parts"][0]["terminalFeatureId"] == "hole"


@pytest.mark.anyio
async def test_persisted_fork_is_diagnosable_and_repairable_over_real_stdio(tmp_path):
    path = tmp_path / "fork.coalesce.json"
    graph = Graph()
    graph.add(Sketch("plate_sketch", entities=[Rectangle("plate", 60, 40)]))
    graph.add(Extrude("plate", sketch="plate_sketch", height=3))
    graph.add(
        Sketch(
            "wall_sketch",
            plane=Plane("XY", 3),
            entities=[Rectangle("outer", 60, 40), Rectangle("inner", 54, 34)],
        )
    )
    graph.add(
        Extrude(
            "wall",
            sketch="wall_sketch",
            height=22,
            operation="join",
            target="plate",
        )
    )
    graph.add(
        Hole(
            "holes",
            target="wall",
            diameter=3,
            positions=[HolePosition("center", (0, 17))],
        )
    )
    graph.add(
        Sketch(
            "port_sketch",
            plane=Plane("XZ", -20),
            position=(0, 14, 0),
            entities=[Rectangle("port", 10, 8)],
        )
    )
    graph.add(Cut("portcut", target="wall", tool="port_sketch"))
    DocumentStore(path).save(Document(graph, revision=7))
    before = path.read_bytes()
    errlog = (tmp_path / "server.stderr").open("w+")
    parameters = StdioServerParameters(
        command=shutil.which("coalesce") or "coalesce",
        args=["--document", str(path)],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )

    async with stdio_client(parameters, errlog=errlog) as (read, write), ClientSession(
        read, write
    ) as session:
        await session.initialize()
        errlog.seek(0)
        viewer_url = next(
            line.removeprefix("coalesce viewer: ")
            for line in errlog.read().splitlines()
            if line.startswith("coalesce viewer: ")
        )
        parsed, headers = _viewer_access(viewer_url)

        measurements = result_json(
            await session.call_tool("query", {"select": {"kind": "measurements"}})
        )
        graph_error = {
            "code": "TARGET_ALREADY_CONSUMED",
            "message": "target wall is already consumed by holes",
            "retryable": False,
            "operation": "query",
            "feature": "portcut",
            "target": "wall",
            "consumer": "holes",
            "terminalFeatureId": "holes",
            "hint": "target holes to apply this after holes",
        }
        assert measurements["error"] == graph_error
        reference = result_json(
            await session.call_tool(
                "query",
                {
                    "select": {
                        "kind": "reference",
                        "reference": {"feature": "holes", "role": "cap_end"},
                    }
                },
            )
        )
        assert reference["error"] == graph_error
        checked = result_json(
            await session.call_tool("check", {"action": "evaluate"})
        )
        assert checked["error"] == {**graph_error, "operation": "check"}

        event, failure = await asyncio.to_thread(_next_viewer_event, parsed, headers)
        assert event == "failure"
        assert failure["error"]["code"] == "TARGET_ALREADY_CONSUMED"
        assert failure["error"]["hint"] == "target holes to apply this after holes"

        unrelated = result_json(
            await session.call_tool(
                "feature",
                {
                    "action": "update",
                    "expectedRevision": 7,
                    "requestId": "unrelated",
                    "featureId": "plate",
                    "kind": "extrude",
                    "set": {"distance": {"value": 4, "unit": "mm"}},
                },
            )
        )
        assert unrelated["error"]["code"] == "TARGET_ALREADY_CONSUMED"
        assert unrelated["revision"] == 7
        assert path.read_bytes() == before

        repaired = result_json(
            await session.call_tool(
                "feature",
                {
                    "action": "update",
                    "expectedRevision": 7,
                    "requestId": "repair-portcut",
                    "featureId": "portcut",
                    "kind": "cut",
                    "set": {"target": {"featureId": "holes"}},
                },
            )
        )
        assert repaired["revision"] == 8
        scene = await asyncio.to_thread(_wait_for_scene, parsed, headers, 8)
        assert len(scene["parts"]) == 1
        assert scene["parts"][0]["terminalFeatureId"] == "portcut"

    restart_log = (tmp_path / "restart.stderr").open("w+")
    async with stdio_client(parameters, errlog=restart_log) as (read, write), ClientSession(
        read, write
    ) as session:
        await session.initialize()
        measurements = result_json(
            await session.call_tool("query", {"select": {"kind": "measurements"}})
        )
        assert measurements["ok"]
        restart_log.seek(0)
        viewer_url = next(
            line.removeprefix("coalesce viewer: ")
            for line in restart_log.read().splitlines()
            if line.startswith("coalesce viewer: ")
        )
        parsed, headers = _viewer_access(viewer_url)
        restarted_scene = await asyncio.to_thread(_wait_for_scene, parsed, headers, 8)

    assert restarted_scene["revision"] == 8
    assert restarted_scene["parts"][0]["terminalFeatureId"] == "portcut"
    assert restarted_scene["parts"][0]["mesh"]["id"] == scene["parts"][0]["mesh"]["id"]


@pytest.mark.anyio
async def test_real_stdio_server_publishes_committed_plate_to_its_viewer(tmp_path):
    path = tmp_path / "plate.coalesce.json"
    errlog = (tmp_path / "server.stderr").open("w+")
    parameters = StdioServerParameters(
        command=shutil.which("coalesce") or "coalesce",
        args=["--document", str(path)],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )

    async with stdio_client(parameters, errlog=errlog) as (read, write), ClientSession(
        read, write
    ) as session:
        await session.initialize()
        errlog.seek(0)
        line = next(
            line for line in errlog.read().splitlines() if line.startswith("coalesce viewer: ")
        )
        assert re.fullmatch(r"coalesce viewer: http://127\.0\.0\.1:\d+/#token=[\w-]+", line)
        viewer_url = line.removeprefix("coalesce viewer: ")
        parsed, headers = _viewer_access(viewer_url)
        assert _get_json(parsed, headers, "/api/session") == {"authorized": True}

        assert result_json(await session.call_tool("sketch", SKETCH))["revision"] == 1
        assert result_json(await session.call_tool("feature", FEATURE))["revision"] == 2
        scene = _wait_for_scene(parsed, headers, 2)
        assert scene["revision"] == 2
        assert len(scene["parts"]) == 1
        assert scene["parts"][0]["size"] == pytest.approx([60, 40, 6])

        server = result_json(await session.call_tool("query", {"select": {"kind": "server"}}))
        assert server == {
            "ok": True,
            "revision": 2,
            "units": "mm",
            "viewerUrl": viewer_url,
        }

        updated = {
            "action": "update",
            "expectedRevision": 2,
            "requestId": "feature-2",
            "featureId": "plate_extrude",
            "kind": "extrude",
            "set": {"distance": {"value": 10, "unit": "mm"}},
        }
        assert result_json(await session.call_tool("feature", updated))["revision"] == 3
        assert _wait_for_scene(parsed, headers, 3)["parts"][0]["size"] == pytest.approx(
            [60, 40, 10]
        )


@pytest.mark.parametrize("with_feature", [False, True])
def test_existing_document_is_published_on_startup(tmp_path, with_feature):
    path = tmp_path / "plate.coalesce.json"
    writer = DocumentService(path)
    writer.sketch(SKETCH)
    if with_feature:
        writer.feature(FEATURE)
    expected_revision = 2 if with_feature else 1
    store = ViewerSceneStore()

    DocumentService(path, scene_store=store)
    deadline = time.monotonic() + 5
    while store.current_revision != expected_revision and time.monotonic() < deadline:
        time.sleep(0.02)

    scene = store.scene(expected_revision)
    if with_feature:
        assert scene["parts"][0]["size"] == pytest.approx([60, 40, 6])
    else:
        assert scene["parts"] == []


@pytest.fixture(scope="module")
def browser_module():
    found = playwright_module()
    if not found:
        pytest.skip("no Node with a launchable Playwright Chromium on this machine")
    return found


@pytest.mark.anyio
async def test_real_mcp_viewer_updates_in_browser_without_console_errors(
    tmp_path, browser_module
):
    path = tmp_path / "browser.coalesce.json"
    errlog = (tmp_path / "browser.stderr").open("w+")
    parameters = StdioServerParameters(
        command=shutil.which("coalesce") or "coalesce",
        args=["--document", str(path)],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    screenshots = Path(os.environ.get("INTEGRATION_SCREENSHOTS", tmp_path / "screenshots"))

    async with stdio_client(parameters, errlog=errlog) as (read, write), ClientSession(
        read, write
    ) as session:
        await session.initialize()
        errlog.seek(0)
        line = next(
            line for line in errlog.read().splitlines() if line.startswith("coalesce viewer: ")
        )
        viewer_url = line.removeprefix("coalesce viewer: ")
        await session.call_tool("sketch", SKETCH)
        await session.call_tool("feature", FEATURE)
        driver = await asyncio.create_subprocess_exec(
            "node",
            str(BROWSER_SMOKE),
            browser_module,
            viewer_url,
            str(screenshots),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            first = json.loads(await driver.stdout.readline())
            assert first == {
                "revision": "2",
                "parts": 1,
                "part": {"label": "plate_extrude", "title": "plate_extrude/outline"},
                "errors": [],
            }
            updated = {
                "action": "update",
                "expectedRevision": 2,
                "requestId": "feature-2",
                "featureId": "plate_extrude",
                "kind": "extrude",
                "set": {"distance": {"value": 10, "unit": "mm"}},
            }
            assert result_json(await session.call_tool("feature", updated))["revision"] == 3
            driver.stdin.write(b"next\n")
            await driver.stdin.drain()
            second = json.loads(await driver.stdout.readline())
            assert second == {
                "revision": "3",
                "parts": 1,
                "part": {"label": "plate_extrude", "title": "plate_extrude/outline"},
                "errors": [],
            }
            failed = {**updated, "expectedRevision": 3, "requestId": "bad-1", "colour": "red"}
            failure = result_json(await session.call_tool("feature", failed))
            assert failure["error"]["code"] == "INVALID_PARAMETER"
            driver.stdin.write(b"failure\n")
            await driver.stdin.drain()
            third = json.loads(await driver.stdout.readline())
            assert third == {
                "revision": "3",
                "parts": 1,
                "failure": "INVALID_PARAMETER: unknown field 'colour'",
                "errors": [],
            }
            assert await asyncio.wait_for(driver.wait(), timeout=15) == 0
        finally:
            if driver.returncode is None:
                driver.kill()
                await driver.wait()

    assert (screenshots / "revision-2.png").stat().st_size > 0
    assert (screenshots / "revision-3.png").stat().st_size > 0
    assert (screenshots / "failure.png").stat().st_size > 0


class FailingSlowStore:
    def publish(self, revision, parts):
        time.sleep(0.5)
        raise RuntimeError("viewer unavailable")

    def publish_failure(self, feature_ids, error):
        time.sleep(0.5)
        raise RuntimeError("viewer unavailable")


def test_slow_failing_viewer_store_does_not_delay_or_fail_mutation(tmp_path):
    service = DocumentService(tmp_path / "plate.coalesce.json", scene_store=FailingSlowStore())

    started = time.monotonic()
    result = service.sketch(SKETCH)

    assert result["ok"] is True
    assert time.monotonic() - started < 0.2


def test_failed_mutation_publishes_safe_failure_and_keeps_scene(tmp_path):
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / "plate.coalesce.json", scene_store=store)
    service.sketch(SKETCH)
    service.feature(FEATURE)
    deadline = time.monotonic() + 5
    while store.current_revision != 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    good = store.scene(2)

    result = service.feature(
        {
            "action": "update",
            "expectedRevision": 2,
            "requestId": "missing-1",
            "featureId": "missing_extrude",
            "kind": "extrude",
            "set": {"distance": {"value": 10, "unit": "mm"}},
        }
    )
    deadline = time.monotonic() + 5
    while store.snapshot()[2] is None and time.monotonic() < deadline:
        time.sleep(0.02)

    assert result["error"]["code"] == "UNKNOWN_ID"
    assert store.current_revision == 2
    assert store.scene(2) == good
    assert store.snapshot()[2] == {
        "revision": 2,
        "featureIds": ["missing_extrude"],
        "error": {"code": "UNKNOWN_ID", "message": "unknown extrude: missing_extrude"},
    }


async def _wait_for_failure(store, previous):
    deadline = time.monotonic() + 5
    while store.snapshot()[2] == previous and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return store.snapshot()[2]


async def _schema_failure(tmp_path, tool, arguments):
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / "plate.coalesce.json", scene_store=store)
    await call_server_tool(service, "sketch", SKETCH)
    await call_server_tool(service, "feature", FEATURE)
    deadline = time.monotonic() + 5
    while store.current_revision != 2 and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    good = store.scene(2)
    result = await call_server_tool(service, tool, arguments)
    failure = await _wait_for_failure(store, None)
    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert result["error"]["message"] == failure["error"]["message"]
    assert store.current_revision == 2
    assert store.scene(2) == good
    return failure


def _entity(**changes):
    return [{**SKETCH["entities"][0], **changes}]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"entities": "outline"}, "entities must be an array (field: entities)"),
        ({"entities": _entity(colour="red")}, "unknown field 'colour' (field: entities[0])"),
        (
            {"entities": [{k: v for k, v in _entity()[0].items() if k != "height"}]},
            "missing required field 'height' (field: entities[0])",
        ),
        (
            {"entities": _entity(rotation={"value": 1, "unit": "turn"})},
            "unit must be one of 'deg', 'rad' (field: entities[0].rotation.unit)",
        ),
        (
            {"plane": {"kind": "origin", "name": "ZX"}},
            "name must be one of 'XY', 'XZ', 'YZ' (field: plane.name)",
        ),
        ({"entities": []}, "invalid value (field: entities)"),
    ],
)
async def test_schema_invalid_sketch_shows_coalesce_wording_in_viewer(tmp_path, changes, message):
    arguments = {**SKETCH, "expectedRevision": 2, "requestId": "bad-sketch", **changes}
    failure = await _schema_failure(tmp_path, "sketch", arguments)
    assert failure == {
        "revision": 2,
        "featureIds": ["plate_profile"],
        "error": {"code": "INVALID_PARAMETER", "message": message},
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"colour": "red"}, "unknown field 'colour'"),
        ({"expectedRevision": -1}, "expectedRevision must be at least 0 (field: expectedRevision)"),
    ],
)
async def test_schema_invalid_feature_shows_coalesce_wording_in_viewer(tmp_path, changes, message):
    arguments = {**FEATURE, "expectedRevision": 2, "requestId": "bad-feature", **changes}
    failure = await _schema_failure(tmp_path, "feature", arguments)
    assert failure == {
        "revision": 2,
        "featureIds": ["plate_extrude"],
        "error": {"code": "INVALID_PARAMETER", "message": message},
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"filters": [{"kind": "Position", "direction": [0, 0, 1]}]},
            "missing required field 'value' (field: select.reference.filters[0])",
        ),
        (
            {"filters": [{"kind": "Normal", "direction": [0, 0, 1], "value": 6}]},
            "unknown field 'value' (field: select.reference.filters[0])",
        ),
        (
            {"filters": [{"kind": "Angle", "direction": [0, 0, 1]}]},
            (
                "kind must be one of 'Normal', 'MaxAlong', 'Position'"
                " (field: select.reference.filters[0].kind)"
            ),
        ),
        (
            {"source": {"kind": "sketch_segment", "sketch": "s", "entity": "e"}},
            "missing required field 'segment' (field: select.reference.source)",
        ),
        (
            {"source": {"kind": "edge"}},
            (
                "kind must be one of 'sketch_segment', 'hole_surface'"
                " (field: select.reference.source.kind)"
            ),
        ),
        (
            {"body": {"kind": "body_source", "feature": "plate_extrude"}},
            "missing required field 'entity' (field: select.reference.body)",
        ),
    ],
)
async def test_schema_union_errors_come_from_the_branch_the_kind_selects(
    tmp_path, changes, message
):
    service = DocumentService(tmp_path / "plate.coalesce.json")
    reference = {"feature": "plate_extrude", "role": "cap_end", **changes}
    result = await call_server_tool(
        service, "query", {"select": {"kind": "reference", "reference": reference}}
    )
    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert result["error"]["message"] == message


def _stdio(path, *args, env=None):
    return StdioServerParameters(
        command=shutil.which("coalesce") or "coalesce",
        args=["--document", str(path), *args],
        env={**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})},
    )


@pytest.mark.anyio
async def test_no_viewer_flag_starts_mcp_without_a_viewer(tmp_path):
    errlog = (tmp_path / "server.stderr").open("w+")
    async with stdio_client(
        _stdio(tmp_path / "plate.coalesce.json", "--no-viewer"), errlog=errlog
    ) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        assert result_json(await session.call_tool("sketch", SKETCH))["revision"] == 1
        server = result_json(await session.call_tool("query", {"select": {"kind": "server"}}))
    errlog.seek(0)
    assert "coalesce viewer:" not in errlog.read()
    assert server == {"ok": True, "revision": 1, "units": "mm", "viewerUrl": None}


@pytest.mark.anyio
async def test_open_flag_opens_the_printed_viewer_link(tmp_path):
    opened = tmp_path / "opened.txt"
    recorder = tmp_path / "record_url.py"
    recorder.write_text(f"import sys\nopen({str(opened)!r}, 'w').write(sys.argv[1])\n")
    errlog = (tmp_path / "server.stderr").open("w+")
    browser = {"BROWSER": f"{sys.executable} {recorder} %s"}
    async with stdio_client(
        _stdio(tmp_path / "plate.coalesce.json", "--open", env=browser), errlog=errlog
    ) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        deadline = time.monotonic() + 5
        while not opened.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
    errlog.seek(0)
    line = next(line for line in errlog.read().splitlines() if line.startswith("coalesce viewer: "))
    assert opened.read_text() == line.removeprefix("coalesce viewer: ")


def test_kernel_failure_publishes_coalesce_wording_only(tmp_path, monkeypatch):
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / "plate.coalesce.json", scene_store=store)
    service.sketch(SKETCH)

    def kernel_failure(graph):
        raise RuntimeError("BRep_API: command not done; use max_fillet()")

    monkeypatch.setattr("coalesce.service.regenerate", kernel_failure)
    result = service.feature(FEATURE)
    deadline = time.monotonic() + 5
    while store.snapshot()[2] is None and time.monotonic() < deadline:
        time.sleep(0.02)

    assert result["error"]["code"] == "KERNEL_OPERATION_FAILED"
    assert store.current_revision == 1
    assert store.snapshot()[2] == {
        "revision": 1,
        "featureIds": ["plate_extrude"],
        "error": {
            "code": "KERNEL_OPERATION_FAILED",
            "message": "the modeling operation could not produce valid geometry",
        },
    }


def test_scene_parts_are_named_by_the_body_that_owns_each_solid(tmp_path):
    graph = Graph()
    graph.add(
        Sketch(
            "bars",
            entities=[
                Rectangle("left", 10, 10, position=(-20, 0)),
                Rectangle("right", 30, 10, position=(20, 0)),
            ],
        )
    )
    graph.add(Extrude("base", sketch="bars", height=5))
    graph.add(Sketch("slot_tool", entities=[Rectangle("slot", 2, 20, position=(20, 0))]))
    graph.add(Cut("split", target="base", tool="slot_tool"))
    path = tmp_path / "bars.coalesce.json"
    DocumentStore(path).save(Document(graph, revision=4, receipts=[]))
    store = ViewerSceneStore()

    DocumentService(path, scene_store=store)
    deadline = time.monotonic() + 5
    while store.current_revision != 4 and time.monotonic() < deadline:
        time.sleep(0.02)

    parts = store.scene(4)["parts"]
    assert len(parts) == 3
    assert {part["label"] for part in parts} == {"base"}
    assert all(part["partId"].startswith("base/") for part in parts)
    assert all(part["terminalFeatureId"] == "split" for part in parts)
    assert sorted(part["size"] for part in parts) == [
        pytest.approx([10, 10, 5]),
        pytest.approx([14, 10, 5]),
        pytest.approx([14, 10, 5]),
    ]


def test_single_body_part_is_labelled_by_its_feature(tmp_path):
    graph = Graph()
    graph.add(Sketch("plate_profile", entities=[Rectangle("outline", 60, 40)]))
    graph.add(Extrude("plate", sketch="plate_profile", height=6))
    graph.add(Sketch("slot_tool", entities=[Rectangle("slot", 2, 50)]))
    graph.add(Cut("split", target="plate", tool="slot_tool"))
    graph.add(Sketch("boss_profile", entities=[Rectangle("boss", 5, 5, position=(100, 0))]))
    graph.add(Extrude("boss_extrude", sketch="boss_profile", height=3))
    path = tmp_path / "plate.coalesce.json"
    DocumentStore(path).save(Document(graph, revision=6, receipts=[]))
    store = ViewerSceneStore()

    DocumentService(path, scene_store=store)
    deadline = time.monotonic() + 5
    while store.current_revision != 6 and time.monotonic() < deadline:
        time.sleep(0.02)

    parts = store.scene(6)["parts"]
    assert len(parts) == 3
    assert {part["label"] for part in parts} == {"plate", "boss_extrude"}
    assert all(part["partId"].startswith("plate/") for part in parts if part["label"] == "plate")
    assert all(part["terminalFeatureId"] == "split" for part in parts if part["label"] == "plate")
    boss = next(part for part in parts if part["label"] == "boss_extrude")
    assert boss["partId"] == "boss_extrude/boss"
    assert boss["terminalFeatureId"] == "boss_extrude"
