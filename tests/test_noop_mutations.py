import json

import pytest
from mcp import types
from test_pcb_enclosure import FIXTURES, base_request, import_request, quantity

from coalesce.server import create_server
from coalesce.service import DocumentService
from coalesce.viewer import ViewerSceneStore


def test_unchanged_board_refresh_is_noop(tmp_path):
    scene = ViewerSceneStore()
    path = tmp_path / "board.json"
    service = DocumentService(path, scene_store=scene)
    source = FIXTURES / "features.kicad_pcb"
    assert service.import_board(import_request(source))["ok"]
    before_bytes = path.read_bytes()
    before = service.store.load()
    before_scene = scene.snapshot()
    scene.wait_for_change(before_scene[0], 5)
    before_scene = scene.snapshot()
    result = service.import_board(import_request(source, revision=1, request_id="refresh", action="refresh"))
    after = service.store.load()
    assert result["changed"] is False
    assert result["revision"] == 1
    assert path.read_bytes() == before_bytes
    assert after.receipts == before.receipts
    assert scene.snapshot() == before_scene


def test_changed_board_refresh_commits_and_reports_changed(tmp_path):
    service = DocumentService(tmp_path / "board.json")
    source = FIXTURES / "features.kicad_pcb"
    assert service.import_board(import_request(source))["ok"]
    changed = FIXTURES / "features-moved.kicad_pcb"
    result = service.import_board({**import_request(changed, revision=1, request_id="refresh", action="refresh"), "path": str(changed.resolve())})
    assert result["changed"] is True
    assert result["revision"] == 2
    assert result["boardChanges"]


@pytest.mark.anyio
async def test_mcp_server_unchanged_refresh_is_noop(tmp_path):
    service = DocumentService(tmp_path / "board.json")
    server = create_server(service)
    handler = server.request_handlers[types.CallToolRequest]
    source = FIXTURES / "features.kicad_pcb"

    async def call(arguments):
        result = await handler(types.CallToolRequest(params=types.CallToolRequestParams(name="import", arguments=arguments)))
        return json.loads(result.root.content[0].text)

    await call(import_request(source))
    response = await call(import_request(source, revision=1, request_id="mcp-refresh", action="refresh"))
    assert response["ok"] is True
    assert response["changed"] is False
    assert response["revision"] == 1


def test_setting_current_parameter_is_noop_and_replays(tmp_path):
    service = DocumentService(tmp_path / "board.json")
    source = FIXTURES / "features.kicad_pcb"
    assert service.import_board(import_request(source))["ok"]
    assert service.feature(base_request())["ok"]
    request = {
        "action": "update",
        "expectedRevision": 2,
        "requestId": "same-value",
        "featureId": "base",
        "kind": "pcb_base",
        "set": {"clearance": quantity(1)},
    }
    first = service.feature(request)
    second = service.feature(request)
    assert first["changed"] is False
    assert second == first
    assert service.store.load().revision == 2
    stale = {**request, "requestId": "stale", "expectedRevision": 1}
    assert service.feature(stale)["error"]["code"] == "REVISION_CONFLICT"
