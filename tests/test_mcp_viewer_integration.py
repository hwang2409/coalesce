import asyncio
import json
import math
import os
import re
import shutil
import sys
import threading
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
from test_pcb_enclosure import base_request, import_request, standoffs_request
from test_pcb_lid import lid_request
from test_port_cutout import explicit_envelope, port_request
from test_screw_closure import closure_request, normal_standoffs_request
from test_viewer_browser import playwright_module

import coalesce.service as service_module
from coalesce.document_store import Document, DocumentStore
from coalesce.graph import Cut, Extrude, Fillet, Graph, Hole, HolePosition, Plane, Rectangle, Sketch
from coalesce.references import Cardinality, Reference
from coalesce.service import DocumentService
from coalesce.viewer import ViewerSceneStore
from coalesce.viewer.scene import REFERENCE_COLOR

BROWSER_SMOKE = Path(__file__).with_name("mcp_viewer_smoke.mjs")
ENCLOSURE_SMOKE = Path(__file__).with_name("mcp_viewer_enclosure_smoke.mjs")
SCREW_SMOKE = Path(__file__).with_name("mcp_viewer_screw_smoke.mjs")
REPAIR_HISTORY_SMOKE = Path(__file__).with_name("viewer_repair_history.mjs")
LID_SMOKE = Path(__file__).with_name("mcp_viewer_lid_smoke.mjs")
REJECTED_SKETCH_SMOKE = Path(__file__).with_name("rejected_sketch_overlay.mjs")
REJECTED_SKETCH_RESTART = Path(__file__).with_name("rejected_sketch_restart.mjs")
KICAD_FIXTURE = Path(__file__).parent / "fixtures/kicad/features.kicad_pcb"
KICAD_MOVED_FIXTURE = Path(__file__).parent / "fixtures/kicad/features-moved.kicad_pcb"


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


def _wait_for_scene(parsed, headers, revision, errlog):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            return _get_json(parsed, headers, f"/api/scene/{revision}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        time.sleep(0.02)
    try:
        viewer_state = _next_viewer_event(parsed, headers)
    except TimeoutError:
        viewer_state = "no revision or failure event"
    errlog.flush()
    errlog.seek(0)
    raise AssertionError(
        f"viewer did not publish revision {revision}; "
        f"viewer store state: {viewer_state!r}; server stderr:\n{errlog.read()}"
    )


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
        scene = await asyncio.to_thread(_wait_for_scene, parsed, headers, 3, errlog)

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
            "featureId": "portcut",
            "target": "wall",
            "consumer": "holes",
            "terminalFeatureId": "holes",
            "hint": "target holes to apply this after holes",
            "legalTargets": [{"featureId": "holes"}],
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
        scene = await asyncio.to_thread(_wait_for_scene, parsed, headers, 8, errlog)
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
        restarted_scene = await asyncio.to_thread(
            _wait_for_scene, parsed, headers, 8, restart_log
        )

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
        scene = _wait_for_scene(parsed, headers, 2, errlog)
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
        assert _wait_for_scene(parsed, headers, 3, errlog)["parts"][0]["size"] == pytest.approx(
            [60, 40, 10]
        )


@pytest.mark.anyio
async def test_real_stdio_kicad_import_publishes_pcb_reference_to_viewer(tmp_path):
    path = tmp_path / "board.coalesce.json"
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
        parsed, headers = _viewer_access(line.removeprefix("coalesce viewer: "))
        imported = result_json(
            await session.call_tool(
                "import",
                {
                    "action": "create",
                    "expectedRevision": 0,
                    "requestId": "import-board",
                    "assetId": "controller_board",
                    "format": "kicad_pcb",
                    "path": str(KICAD_FIXTURE.resolve()),
                },
            )
        )
        assert imported["ok"] is True
        [part] = _wait_for_scene(parsed, headers, 1, errlog)["parts"]
        assert part["role"] == "reference"
        assert part["label"] == "pcb reference"
        assert part["color"] == REFERENCE_COLOR
        assert part["visible"] is True


class RequestWinsKernelLoader:
    def __init__(self):
        self.startup_waiting = threading.Event()
        self.release_startup = threading.Event()

    def start(self):
        service_module._KERNEL_LOADER.start()

    def wait(self):
        service_module._KERNEL_LOADER.wait()
        if threading.current_thread().name == "coalesce-kernel":
            self.startup_waiting.set()
            self.release_startup.wait()


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


def test_query_that_wins_startup_race_still_publishes_persisted_scene(tmp_path):
    path = tmp_path / "plate.coalesce.json"
    graph = Graph()
    graph.add(Sketch("plate_sketch", entities=[Rectangle("plate", 60, 40)]))
    graph.add(Extrude("plate", sketch="plate_sketch", height=3))
    DocumentStore(path).save(Document(graph, revision=2))
    store = ViewerSceneStore()
    loader = RequestWinsKernelLoader()
    service = DocumentService(
        path, scene_store=store, warmup=False, kernel_loader=loader
    )

    try:
        service.start_kernel_warmup()
        assert loader.startup_waiting.wait(2)
        assert service.query({"select": {"kind": "measurements"}})["ok"]
        deadline = time.monotonic() + 5
        while store.current_revision != 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert store.scene(2) is not None
    finally:
        loader.release_startup.set()


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


@pytest.mark.anyio
async def test_fillet_repair_history_over_real_stdio_and_browser(tmp_path, browser_module):
    path = tmp_path / "repair.coalesce.json"
    errlog = (tmp_path / "repair.stderr").open("w+")
    shots = Path(
        os.environ.get(
            "REPAIR_HISTORY_SCREENSHOTS",
            tmp_path / "repair-history-screenshots",
        )
    )
    edges = [
        {
            "feature": "plate",
            "role": "between(side(bottom),cap_end)",
            "cardinality": "one",
        }
    ]
    calls = [
        (
            "sketch",
            {
                "action": "create",
                "expectedRevision": 0,
                "requestId": "plate-sketch",
                "sketchId": "plate_sketch",
                "plane": {"kind": "origin", "name": "XY", "offset": {"value": 0, "unit": "mm"}},
                "entities": [
                    {
                        "kind": "rectangle",
                        "id": "plate",
                        "width": {"value": 20, "unit": "mm"},
                        "height": {"value": 20, "unit": "mm"},
                    }
                ],
            },
        ),
        (
            "feature",
            {
                "action": "create",
                "expectedRevision": 1,
                "requestId": "plate-extrude",
                "featureId": "plate",
                "kind": "extrude",
                "parameters": {
                    "profile": {"sketchId": "plate_sketch", "profileIds": ["plate"]},
                    "distance": {"value": 10, "unit": "mm"},
                    "operation": "new_body",
                    "direction": "normal",
                },
            },
        ),
        (
            "feature",
            _edge_treatment("create", 2, "round-create", "round", "fillet", 4, edges, "plate"),
        ),
    ]
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
        for tool, arguments in calls:
            assert result_json(await session.call_tool(tool, arguments))["ok"] is True
        failed = result_json(
            await session.call_tool(
                "feature",
                _edge_treatment("update", 3, "too-large", "round", "fillet", 1000),
            )
        )
        assert failed["ok"] is False and failed["revision"] == 3

        invalid = _edge_treatment("update", 3, "schema-invalid", "round", "fillet", 1)
        invalid["set"]["radius"]["value"] = "not-a-number"
        assert result_json(await session.call_tool("feature", invalid))["error"]["code"] == "INVALID_PARAMETER"

        repaired = result_json(
            await session.call_tool(
                "feature",
                _edge_treatment("update", 3, "repaired", "round", "fillet", 1),
            )
        )
        assert repaired["revision"] == 4
        scene = await asyncio.to_thread(_wait_for_scene, parsed, headers, 4, errlog)
        assert scene["repairHistory"][0]["fields"]["radius"] == {
            "attempted": {"value": 1000, "unit": "mm"},
            "accepted": {"value": 1, "unit": "mm"},
        }
        browser = await asyncio.create_subprocess_exec(
            "node",
            str(REPAIR_HISTORY_SMOKE),
            browser_module,
            viewer_url,
            str(shots),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await browser.communicate()
        assert browser.returncode == 0, stderr.decode()
        shown = json.loads(stdout)
        assert shown["rows"] == 1
        assert "Session repair history" in shown["history"]
        assert (
            "Failed attempts are session-only and are not document revisions."
            in shown["history"]
        )
        assert "Failed 1000 mm → accepted 1 mm" in shown["history"]
        assert shown["errors"] == []

    restart_log = (tmp_path / "restart.stderr").open("w+")
    async with stdio_client(parameters, errlog=restart_log) as (read, write), ClientSession(
        read, write
    ) as session:
        await session.initialize()
        restart_log.seek(0)
        restarted_url = next(
            line.removeprefix("coalesce viewer: ")
            for line in restart_log.read().splitlines()
            if line.startswith("coalesce viewer: ")
        )
        restarted, restart_headers = _viewer_access(restarted_url)
        await session.call_tool("query", {"select": {"kind": "measurements"}})
        restarted_scene = await asyncio.to_thread(
            _wait_for_scene, restarted, restart_headers, 4, restart_log
        )
        assert restarted_scene["repairHistory"] == []

    assert (shots / "repair-history-1280x800.png").stat().st_size > 0
    assert (shots / "repair-history-390x844.png").stat().st_size > 0


def _edge_treatment(action, revision, request_id, feature_id, kind, size, edges=None, target=None):
    word = "radius" if kind == "fillet" else "distance"
    request = {"action": action, "expectedRevision": revision, "requestId": request_id,
               "featureId": feature_id, "kind": kind}
    if action == "create":
        request["parameters"] = {"target": {"featureId": target}, "edges": edges,
                                 word: {"value": size, "unit": "mm"}}
    else:
        request["set"] = {word: {"value": size, "unit": "mm"}}
    return request


def _enclosure_calls():
    """The ux review 6 flow: board, base, all mounting standoffs, J1 port, lip-less lid, fillet, chamfer."""
    outer = [{"feature": "base", "role": "outer_vertical", "cardinality": "exactly", "count": 4}]
    top = [{"feature": "lid", "role": "between(edge,top)", "cardinality": "exactly", "count": 4}]
    return [
        ("import", import_request(KICAD_FIXTURE)),
        ("feature", base_request()),
        ("feature", standoffs_request(selection={"mode": "all_mounting"})),
        ("feature", port_request(revision=3)),
        ("feature", lid_request(revision=4, include_lip=False)),
        ("feature", _edge_treatment("create", 5, "base-fillet", "base_fillet", "fillet", 1, outer, "base")),
        ("feature", _edge_treatment("create", 6, "lid-chamfer", "lid_chamfer", "chamfer", 0.5, top, "lid")),
    ]


def _stale_lid_calls():
    outer = [{"feature": "base", "role": "outer_vertical", "cardinality": "exactly", "count": 4}]
    return [
        ("import", import_request(KICAD_FIXTURE)),
        ("feature", base_request()),
        ("feature", _edge_treatment("create", 2, "round-base", "base_round", "fillet", 1, outer, "base")),
        ("feature", lid_request(revision=3, include_lip=False)),
        ("feature", port_request(
            revision=4,
            envelope=explicit_envelope(width=4, height=1, bottom=0),
        )),
    ]


@pytest.mark.anyio
async def test_stale_lid_outline_is_in_scene_and_clears_in_browser(tmp_path, browser_module):
    path = tmp_path / "stale-lid.coalesce.json"
    errlog = (tmp_path / "stale-lid.stderr").open("w+")
    parameters = StdioServerParameters(
        command=shutil.which("coalesce") or "coalesce",
        args=["--document", str(path)],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    screenshots = Path(
        os.environ.get(
            "LID_TRUTH_SCREENSHOTS",
            Path.home() / ".zeta/scratch/coalesce/ux-shots-lid-truth",
        )
    )

    async with stdio_client(parameters, errlog=errlog) as (read, write), ClientSession(
        read, write
    ) as session:
        await session.initialize()
        errlog.seek(0)
        url = next(
            line.removeprefix("coalesce viewer: ")
            for line in errlog.read().splitlines()
            if line.startswith("coalesce viewer: ")
        )
        for tool, arguments in _stale_lid_calls():
            assert result_json(await session.call_tool(tool, arguments))["ok"] is True
        parsed, headers = await asyncio.to_thread(_viewer_access, url)
        scene = await asyncio.to_thread(_wait_for_scene, parsed, headers, 5, errlog)
        outline = next(row for row in scene["fit"][0]["rows"] if row["id"].startswith("lid_outline:"))
        assert outline.pop("partId").startswith("lid/")
        assert outline == {
            "id": "lid_outline:lid",
            "label": "Lid outline",
            "status": "FAIL",
            "value": "the lid follows base_round, but port was added to the base after it",
            "reason": "update the lid's outlineSource to port",
            "evidence": {
                "source": "base_round",
                "legalSource": "port",
                "areaDifference": {"value": 0.0, "unit": "mm^2"},
                "bboxDifference": {"value": 0.0, "unit": "mm"},
                "matches": False,
                "reason": "outline source is stale: base_round != port",
            },
        }
        driver = await asyncio.create_subprocess_exec(
            "node", str(LID_SMOKE), browser_module, url, str(screenshots),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        )

        async def step(name):
            driver.stdin.write(f"{name}\n".encode())
            await driver.stdin.drain()
            return json.loads(await asyncio.wait_for(driver.stdout.readline(), timeout=60))

        try:
            stale = await step("stale")
            updated = result_json(await session.call_tool("feature", {
                "action": "update",
                "expectedRevision": 5,
                "requestId": "update-lid-outline",
                "featureId": "lid",
                "kind": "pcb_lid",
                "set": {"outlineSource": {"featureId": "port"}},
            }))
            assert updated["revision"] == 6
            cleared = await step("clear")
            driver.stdin.write_eof()
            assert await asyncio.wait_for(driver.wait(), timeout=15) == 0
        finally:
            if driver.returncode is None:
                driver.kill()
                await driver.wait()

    assert stale.pop("errors") == []
    assert cleared.pop("errors") == []
    for size in ("1280x800", "390x844"):
        assert stale[size] == {
            "status": "FAIL",
            "label": "Lid outline",
            "value": "the lid follows base_round, but port was added to the base after it",
            "reason": "update the lid's outlineSource to port",
            "focused": True,
            "readable": True,
        }
        assert cleared[size] == {"count": 0}


@pytest.mark.anyio
async def test_named_screw_closure_evidence_in_real_browser(tmp_path, browser_module):
    path = tmp_path / "screw-closure.coalesce.json"
    errlog = (tmp_path / "screw-closure.stderr").open("w+")
    parameters = StdioServerParameters(
        command=shutil.which("coalesce") or "coalesce",
        args=["--document", str(path)],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    screenshots = Path(
        os.environ.get(
            "UX_ROUND_9_SCREENSHOTS",
            Path.home() / ".zeta/scratch/coalesce/ux-shots-round-9-fixed",
        )
    )

    async with stdio_client(parameters, errlog=errlog) as (read, write), ClientSession(
        read, write
    ) as session:
        await session.initialize()
        errlog.seek(0)
        url = next(
            line.removeprefix("coalesce viewer: ")
            for line in errlog.read().splitlines()
            if line.startswith("coalesce viewer: ")
        )
        calls = [
            ("import", import_request(KICAD_FIXTURE)),
            ("feature", base_request(clearance={"value": 5, "unit": "mm"})),
            ("feature", normal_standoffs_request()),
            ("feature", lid_request(revision=3, include_lip=False)),
            ("feature", closure_request(revision=4)),
        ]
        for tool, arguments in calls:
            assert result_json(await session.call_tool(tool, arguments))["ok"] is True
        driver = await asyncio.create_subprocess_exec(
            "node", str(SCREW_SMOKE), browser_module, url, str(screenshots),
            stdout=asyncio.subprocess.PIPE,
        )
        output = [
            json.loads(line)
            for line in (await asyncio.wait_for(driver.stdout.read(), timeout=60)).decode().splitlines()
        ]
        assert await asyncio.wait_for(driver.wait(), timeout=15) == 0

    assert output[-1] == {"errors": []}
    expected = [
        "left: wall 1.2 mm, coaxial — pass",
        "right: wall 1.2 mm, coaxial — pass",
        "front: wall 1.2 mm, coaxial — pass",
        "rear: wall 1.2 mm, coaxial — pass",
    ]
    for result in output[:-1]:
        assert result["result"] == {
            "row": "PASS\nScrew closure\n4 points, 1.2 mm minimum wall, coaxial, no interference",
            "points": expected,
            "visible": True,
        }


@pytest.mark.anyio
async def test_live_enclosure_review_in_browser(tmp_path, browser_module):
    """Every review step is scrolled into view and activated in the browser before it is asserted."""
    path = tmp_path / "enclosure.coalesce.json"
    errlog = (tmp_path / "enclosure.stderr").open("w+")
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
        for tool, arguments in _enclosure_calls():
            assert result_json(await session.call_tool(tool, arguments))["ok"] is True, arguments
        driver = await asyncio.create_subprocess_exec(
            "node", str(ENCLOSURE_SMOKE), browser_module, line.removeprefix("coalesce viewer: "),
            str(screenshots), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        )

        async def step(name):
            driver.stdin.write(f"{name}\n".encode())
            await driver.stdin.drain()
            return json.loads(await asyncio.wait_for(driver.stdout.readline(), timeout=60))

        try:
            built = await step("built")
            rows = await step("rows")
            bad = _edge_treatment("update", 7, "bad-fillet", "base_fillet", "fillet", 1000)
            assert result_json(await session.call_tool("feature", bad))["error"]["code"] == "FILLET_FAILED"
            failed = await step("failure")
            fixed = _edge_treatment("update", 7, "repair-fillet", "base_fillet", "fillet", 1)
            assert result_json(await session.call_tool("feature", fixed))["revision"] == 8
            repaired = await step("repaired")
            refresh = {
                **import_request(KICAD_MOVED_FIXTURE, revision=8, request_id="refresh-delta", action="refresh"),
                "path": str(KICAD_MOVED_FIXTURE.resolve()),
            }
            assert result_json(await session.call_tool("import", refresh))["revision"] == 9
            refreshed = await step("refreshSummary")
            driver.stdin.write_eof()
            assert await asyncio.wait_for(driver.wait(), timeout=15) == 0
        finally:
            if driver.returncode is None:
                driver.kill()
                await driver.wait()
    for answer in (built, rows, failed, repaired, refreshed):
        assert answer.pop("errors") == []
    for size in ("1280x800", "390x844"):
        fit, evidence = built[size]["fit"], built[size]["evidence"]
        # a lip-less lid has no fit gap: neutral, last, and outside the counts
        assert fit["rows"][-1] == {"status": "N/A", "label": "Lid fit gap", "value": "the lid has no lip"}
        assert fit["visible"] is True
        assert fit["count"] == "1 fail, 1 unknown, 2 pass, 3 measured only"
        # readable lines with units; ids and the payload only behind the closed raw disclosure
        assert evidence["lines"] == [
            "Lid outline on lid: the lid follows port, but base_fillet was added to the base after it, fail",
            "Port J1 clears connector on base: J1: 5 × 3 mm on left wall, 0.5 mm clearance, unknown",
            "Wall thickness on base: 2 mm, measured only",
            "Floor thickness on base: 2 mm, measured only",
            "Board clearance on pcb reference: 1 mm min, all sides, measured only",
            "Standoffs on base: 3 of 3 mounting holes, pass",
            "Lid interference on lid: none, pass",
            "Lid fit gap on lid: the lid has no lip, n/a",
        ]
        assert (evidence["firstLineVisible"], evidence["rawOpen"], evidence["rawVisible"]) == (True, False, False)
        parts = rows[size]["click"]["parts"]
        assert [name.split("/")[0] for name in parts] == ["controller_board", "base", "lid"]
        for how, part, faded in (("click", "base", [True, False, True]), ("enter", "lid", [True, True, False])):
            shown = rows[size][how]
            assert shown["pressed"] == "true"
            assert shown["faded"] == faded
            assert shown["cameraUnchanged"] is True
            assert shown["canvasLabel"] == f"Showing {part}, other parts faded."
            assert shown["canvasLabelInView"] is True
            # Escape after Enter, or Tab on to "Show all parts" and Enter: all parts, focus back on the row
            assert shown["cleared"] == {"faded": [False, False, False], "labelHidden": True, "pressed": "false",
                                        "rowFocused": True, "announced": True}
        assert rows[size]["click"]["clearReachedByTab"] is True
        assert rows[size]["facts"] == {
            "text": "3: M2 mounting hole at (2, 3), M3 mounting hole at (18, 3), H1 at (10, 7) mm",
            "visible": True,
        }
        assert failed[size] == {"code": "FILLET_FAILED"}
        assert repaired[size] == {
            "change": "Edited fillet base_fillet, no value changed",
            "visible": True,
        }
        assert refreshed[size]["text"] == (
            "Refreshed board: J1 moved 2.0 mm, 3 mounting holes moved, "
            "standoffs and port cutout updated"
        )
        assert refreshed[size]["visible"] is True
        assert refreshed[size]["overflow"] == {"document": True, "change": True}


class FailingSlowStore:
    def publish(self, revision, parts, change=None, fit=None, accepted_edit=None):
        time.sleep(0.5)
        raise RuntimeError("viewer unavailable")

    def publish_failure(self, feature_ids, error, attempted_edit=None):
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
        "attemptedEdit": {
            "featureId": "plate_extrude",
            "kind": "extrude",
            "fields": {"distance": {"value": 6.0, "unit": "mm"}},
        },
    }


@pytest.mark.anyio
async def test_oversized_fillet_says_reduce_the_radius_in_mcp_and_viewer(tmp_path):
    """A fillet the kernel cannot build answers in coalesce terms through MCP and in the viewer."""
    graph = Graph()
    graph.add(Sketch("plate_sketch", entities=[Rectangle("plate", 20, 20)]))
    graph.add(Extrude("plate", sketch="plate_sketch", height=10))
    edge = Reference("plate", "between(side(bottom),cap_end)", Cardinality.ONE)
    graph.add(Fillet("round", target="plate", edges=[edge], radius=4))
    path = tmp_path / "fillet.coalesce.json"
    DocumentStore(path).save(Document(graph, revision=3, receipts=[]))
    store = ViewerSceneStore()
    service = DocumentService(path, scene_store=store)
    thin = {"action": "update", "expectedRevision": 3, "requestId": "thin", "featureId": "plate",
            "kind": "extrude", "set": {"distance": {"value": 2, "unit": "mm"}}}

    result = await call_server_tool(service, "feature", thin)
    failure = await _wait_for_failure(store, None)

    assert result["ok"] is False and result["revision"] == 3
    assert result["error"]["hint"] == "reduce the fillet radius and retry, or find the largest radius that works"
    assert result["error"]["featureId"] == "round" and result["error"]["radius"] == 4
    assert failure["error"]["hint"] == result["error"]["hint"]
    # visible guidance is in coalesce terms; the kernel's own words stay in the structured details
    for error in (result["error"], failure["error"]):
        visible = json.dumps({key: error[key] for key in ("code", "message", "hint")})
        assert "max_fillet" not in visible and "BRep" not in visible and "build123d" not in visible
        assert "ChFiDS_StartsolFailure" in error["kernelMessage"]
    assert failure["error"]["kernelMessage"] == result["error"]["kernelMessage"]


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


def _rejected_geometry_model(point_count: int | None = None) -> dict:
    if point_count is not None:
        return {
            "kind": "constrained",
            "points": [
                {"id": f"p{index:04d}", "x": index * 0.1, "y": -(index % 20)}
                for index in range(point_count)
            ],
            "lines": [],
            "arcs": [],
            "circles": [],
            "dimensions": [],
            "constraints": [
                {"id": "fix", "kind": "fix", "entities": ["p0000"]},
                {"id": "fix-again", "kind": "fix", "entities": ["p0000"]},
            ],
        }
    return {
        "kind": "constrained",
        "points": [
            {"id": "origin", "x": 0, "y": 0},
            {"id": "line-end", "x": 10, "y": 0},
            {"id": "arc-end", "x": 0, "y": 10},
            {"id": "circle-center", "x": 20, "y": 20},
        ],
        "lines": [{"id": "line", "start": "origin", "end": "line-end"}],
        "arcs": [{
            "id": "arc",
            "center": "origin",
            "start": "line-end",
            "end": "arc-end",
            "radius": 10,
            "start_angle": 0,
            "end_angle": math.pi / 2,
        }],
        "circles": [{"id": "circle", "center": "circle-center", "radius": 3}],
        "dimensions": [],
        "constraints": [
            {"id": "fix", "kind": "fix", "entities": ["origin"]},
            {"id": "fix-again", "kind": "fix", "entities": ["origin"]},
        ],
    }


def _rejected_sketch_request(model: dict, request_id: str) -> dict:
    return {
        "action": "create",
        "expectedRevision": 2,
        "requestId": request_id,
        "sketchId": "rejected",
        "plane": {"kind": "origin", "name": "XY"},
        "constrained": model,
    }


@pytest.mark.anyio
async def test_rejected_sketch_overlay_is_transient_in_real_stdio_browser(tmp_path, browser_module):
    path = tmp_path / "rejected-overlay.coalesce.json"
    exported = tmp_path / "accepted.step"
    errlog = (tmp_path / "rejected-overlay.stderr").open("w+")
    parameters = StdioServerParameters(
        command=shutil.which("coalesce") or "coalesce",
        args=["--document", str(path)],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    screenshots = Path(
        os.environ.get(
            "REJECTED_OVERLAY_SCREENSHOTS",
            Path.home() / ".zeta/scratch/coalesce/ux-shots-rejected-overlay",
        )
    )
    screenshots.mkdir(parents=True, exist_ok=True)

    async with stdio_client(parameters, errlog=errlog) as (read, write), ClientSession(
        read, write
    ) as session:
        await session.initialize()
        errlog.seek(0)
        url = next(
            line.removeprefix("coalesce viewer: ")
            for line in errlog.read().splitlines()
            if line.startswith("coalesce viewer: ")
        )
        base = {
            "action": "create",
            "expectedRevision": 0,
            "requestId": "base",
            "sketchId": "base",
            "plane": {"kind": "origin", "name": "XY"},
            "entities": [{
                "id": "outline",
                "kind": "rectangle",
                "width": {"value": 10, "unit": "mm"},
                "height": {"value": 8, "unit": "mm"},
            }],
        }
        extrude = {
            "action": "create",
            "expectedRevision": 1,
            "requestId": "body",
            "featureId": "body",
            "kind": "extrude",
            "parameters": {
                "profile": {"sketchId": "base", "profileIds": ["outline"]},
                "distance": {"value": 5, "unit": "mm"},
                "operation": "new_body",
                "direction": "normal",
            },
        }
        assert result_json(await session.call_tool("sketch", base))["revision"] == 1
        assert result_json(await session.call_tool("feature", extrude))["revision"] == 2
        driver = await asyncio.create_subprocess_exec(
            "node",
            str(REJECTED_SKETCH_SMOKE),
            browser_module,
            url,
            str(screenshots),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )

        async def answer():
            return json.loads(await asyncio.wait_for(driver.stdout.readline(), timeout=60))

        async def advance():
            driver.stdin.write(b"next\n")
            await driver.stdin.drain()
            return await answer()

        try:
            assert await answer() == {"ready": True}
            rejected = result_json(
                await session.call_tool(
                    "sketch", _rejected_sketch_request(_rejected_geometry_model(), "all-kinds")
                )
            )
            assert rejected["revision"] == 2
            assert set(rejected["error"]["diagnosticGeometry"]["entities"]) == {
                "origin", "line-end", "arc-end", "circle-center", "line", "arc", "circle"
            }
            shown = await advance()

            queried = result_json(await session.call_tool("query", {"select": {"kind": "graph"}}))
            checked = result_json(await session.call_tool("check", {"action": "evaluate", "revision": 2}))
            published = result_json(await session.call_tool("export", {
                "action": "publish", "expectedRevision": 2, "path": str(exported)
            }))
            assert queried["revision"] == checked["revision"] == published["revision"] == 2
            assert exported.is_file()

            large_result = result_json(
                await session.call_tool(
                    "sketch",
                    _rejected_sketch_request(_rejected_geometry_model(300), "bounded-large"),
                )
            )
            assert len(large_result["error"]["diagnosticGeometry"]["entities"]) == 300
            large = await advance()

            omitted_result = result_json(
                await session.call_tool(
                    "sketch",
                    _rejected_sketch_request(_rejected_geometry_model(2000), "over-cap"),
                )
            )
            assert omitted_result["error"]["diagnosticGeometryOmitted"]["limitBytes"] == 65536
            omitted = await advance()
            reloaded = await advance()

            repair = {**base, "expectedRevision": 2, "requestId": "repair", "sketchId": "rejected"}
            assert result_json(await session.call_tool("sketch", repair))["revision"] == 3
            repaired = await advance()
            driver.stdin.write_eof()
            assert await asyncio.wait_for(driver.wait(), timeout=15) == 0
        finally:
            if driver.returncode is None:
                driver.kill()
                await driver.wait()

    restart_log = (tmp_path / "rejected-overlay-restart.stderr").open("w+")
    async with stdio_client(parameters, errlog=restart_log) as (read, write), ClientSession(
        read, write
    ) as restarted_session:
        await restarted_session.initialize()
        restarted_graph = result_json(
            await restarted_session.call_tool("query", {"select": {"kind": "graph"}})
        )
        assert restarted_graph["revision"] == 3
        restart_log.seek(0)
        restart_url = next(
            line.removeprefix("coalesce viewer: ")
            for line in restart_log.read().splitlines()
            if line.startswith("coalesce viewer: ")
        )
        restart_driver = await asyncio.create_subprocess_exec(
            "node",
            str(REJECTED_SKETCH_RESTART),
            browser_module,
            restart_url,
            stdout=asyncio.subprocess.PIPE,
        )
        restarted = json.loads(
            (await asyncio.wait_for(restart_driver.stdout.read(), timeout=60)).decode()
        )
        assert await asyncio.wait_for(restart_driver.wait(), timeout=15) == 0

    assert restarted == {
        "labelVisible": False,
        "diagnostic": {"entityCount": 0, "kinds": []},
        "failureVisible": False,
        "errors": [],
    }
    for size in ("1280x800", "390x844"):
        first = shown["rejected"][size]
        assert first["revision"] == 2
        assert first["diagnostic"] == {
            "entityCount": 7,
            "kinds": ["arc", "circle", "line", "point", "point", "point", "point"],
        }
        assert first["label"] == "Rejected sketch — not accepted"
        assert "Rejected sketch — not accepted" in first["alert"]
        assert first["errors"] == []
        assert large["large"][size]["diagnostic"]["entityCount"] == 300
        assert large["large"][size]["errors"] == []
        assert omitted["omitted"][size]["revision"] == 2
        assert omitted["omitted"][size]["labelVisible"] is False
        assert "Preview too large" in omitted["omitted"][size]["failure"]
        assert omitted["omitted"][size]["errors"] == []
        assert reloaded["reloaded"][size] == {
            "labelVisible": False,
            "diagnostic": {"entityCount": 0, "kinds": []},
            "failureVisible": True,
            "errors": [],
        }
        assert repaired["repaired"][size]["labelVisible"] is False
        assert repaired["repaired"][size]["diagnostic"] == {"entityCount": 0, "kinds": []}
        assert repaired["repaired"][size]["keyboardFocus"] == "BUTTON"
        assert "Revision 3" in repaired["repaired"][size]["description"]
        assert repaired["repaired"][size]["errors"] == []
