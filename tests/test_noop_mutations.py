import json

import pytest
from mcp import types
from test_mcp_assembly import FEATURE, SKETCH, ground_create, occurrence_create, transform
from test_pcb_enclosure import FIXTURES, base_request, import_request, quantity

from coalesce.document_store import MAX_REQUEST_HASHES
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


def test_assembly_occurrence_and_mate_same_values_are_noops(tmp_path):
    service = DocumentService(tmp_path / "assembly.json")
    assert service.sketch(SKETCH)["ok"]
    assert service.feature(FEATURE)["ok"]
    assert service.feature(occurrence_create(2, "place", "plate"))["ok"]
    update_occurrence = {
        "action": "update",
        "kind": "assembly_occurrence",
        "expectedRevision": 3,
        "requestId": "same-occurrence",
        "occurrenceId": "plate",
        "set": {"transform": transform()},
    }
    occurrence = service.feature(update_occurrence)
    assert occurrence["changed"] is False
    assert occurrence["revision"] == 3
    assert service.feature(ground_create(3, "ground", "fixture", "plate"))["changed"] is True
    update_mate = {
        "action": "update",
        "kind": "assembly_mate",
        "expectedRevision": 4,
        "requestId": "same-mate",
        "mateId": "fixture",
        "set": {"occurrenceId": "plate"},
    }
    mate = service.feature(update_mate)
    assert mate["changed"] is False
    assert mate["revision"] == 4


def test_noop_request_id_can_be_reused_after_restart_but_not_in_process(tmp_path):
    path = tmp_path / "restart.json"
    service = DocumentService(path)
    assert service.import_board(import_request(FIXTURES / "features.kicad_pcb"))["ok"]
    assert service.feature(base_request())["ok"]
    request = {
        "action": "update",
        "expectedRevision": 2,
        "requestId": "restart-noop",
        "featureId": "base",
        "kind": "pcb_base",
        "set": {"clearance": quantity(1)},
    }
    first = service.feature(request)
    assert first["changed"] is False
    different = {**request, "set": {"clearance": quantity(2)}}
    assert service.feature(different)["error"]["code"] == "REQUEST_ID_REUSED"
    service.close()
    restarted = DocumentService(path)
    assert restarted.feature(request)["changed"] is False
    restarted.close()


def test_noop_replays_use_the_request_history_bound(tmp_path):
    service = DocumentService(tmp_path / "bound.json")
    assert service.import_board(import_request(FIXTURES / "features.kicad_pcb"))["ok"]
    assert service.feature(base_request())["ok"]
    service._noop_replays = {
        f"old-{index}": ("hash", {}) for index in range(MAX_REQUEST_HASHES)
    }
    request = {
        "action": "update",
        "expectedRevision": 2,
        "requestId": "bounded-noop-new",
        "featureId": "base",
        "kind": "pcb_base",
        "set": {"clearance": quantity(1)},
    }
    assert service.feature(request)["changed"] is False
    assert len(service._noop_replays) == MAX_REQUEST_HASHES
    assert "old-0" not in service._noop_replays


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
