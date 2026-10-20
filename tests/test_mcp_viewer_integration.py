import asyncio
import json
import os
import shutil
import time
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_mcp_vertical import FEATURE, SKETCH, result_json
from test_viewer_browser import playwright_module

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
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            return _get_json(parsed, headers, f"/api/scene/{revision}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        time.sleep(0.02)
    raise AssertionError(f"viewer did not publish revision {revision}")


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
            assert first == {"revision": "2", "parts": 1, "errors": []}
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
            assert second == {"revision": "3", "parts": 1, "errors": []}
            failed = {
                "action": "update",
                "expectedRevision": 3,
                "requestId": "missing-1",
                "featureId": "missing_extrude",
                "kind": "extrude",
                "set": {"distance": {"value": 12, "unit": "mm"}},
            }
            failure = result_json(await session.call_tool("feature", failed))
            assert failure["error"]["code"] == "UNKNOWN_ID"
            driver.stdin.write(b"failure\n")
            await driver.stdin.drain()
            third = json.loads(await driver.stdout.readline())
            assert third == {
                "revision": "3",
                "parts": 1,
                "failure": "UNKNOWN_ID: unknown extrude: missing_extrude",
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
