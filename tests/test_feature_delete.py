import asyncio
import json
import time

import pytest
from build123d import import_step
from test_mcp_cut_hole_circle import hole
from test_mcp_vertical import FEATURE, SKETCH, call_server_tool, run_server
from test_pcb_enclosure import FIXTURES, base_request, import_request, standoffs_request
from test_port_cutout import port_request

from coalesce.service import DocumentService
from coalesce.viewer import ViewerSceneStore


def delete_feature(feature_id, revision, request_id, *, cascade=False):
    return {
        "action": "delete",
        "expectedRevision": revision,
        "requestId": request_id,
        "featureId": feature_id,
        "cascade": cascade,
    }


def delete_sketch(sketch_id, revision, request_id, *, cascade=False):
    return {
        "action": "delete",
        "expectedRevision": revision,
        "requestId": request_id,
        "sketchId": sketch_id,
        "cascade": cascade,
    }


def second_base_request():
    request = base_request(revision=3, request_id="base-2")
    request["featureId"] = "base2"
    return request


def second_standoffs_request():
    request = standoffs_request(revision=4, request_id="standoffs-2")
    request["featureId"] = "mounts2"
    request["parameters"]["target"] = {"featureId": "base2"}
    return request


@pytest.mark.anyio
async def test_harness_duplicate_base_cleanup_updates_query_viewer_and_step(tmp_path):
    document = tmp_path / "duplicate.coalesce.json"
    target = tmp_path / "clean.step"
    store = ViewerSceneStore()
    service = DocumentService(document, scene_store=store)
    calls = [
        ("import", import_request(FIXTURES / "features.kicad_pcb")),
        ("feature", base_request()),
        ("feature", standoffs_request()),
        ("feature", second_base_request()),
        ("feature", second_standoffs_request()),
        ("feature", delete_feature("base", 5, "delete-first-base", cascade=True)),
        ("query", {"select": {"kind": "graph"}}),
        ("query", {"select": {"kind": "measurements"}}),
        ("export", {"expectedRevision": 6, "path": str(target)}),
    ]
    results = [await call_server_tool(service, tool, payload) for tool, payload in calls]

    deleted, graph, measured, exported = results[-4:]
    assert deleted["deleted"] == [
        {"id": "base", "kind": "pcb_base"},
        {"id": "mounts", "kind": "standoffs"},
    ]
    assert deleted["changedFeatureIds"] == ["base", "mounts"]
    assert {node["id"] for node in graph["graph"]["nodes"]} == {
        "controller_board",
        "base2",
        "mounts2",
    }
    assert [part["terminalFeatureId"] for part in measured["parts"]] == ["mounts2"]
    assert exported["validation"]["reimport"]["partCount"] == 1
    assert len(import_step(str(target)).solids()) == 1
    deadline = time.monotonic() + 10
    while store.current_revision != 6 and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    scene = store.scene(6)
    assert [
        part["terminalFeatureId"] for part in scene["parts"] if part["role"] == "manufacturing"
    ] == ["mounts2"]
    assert "deleted base" in scene["change"]["summary"]


@pytest.mark.anyio
async def test_delete_without_cascade_lists_all_transitive_dependents_and_commits_nothing(tmp_path):
    path = tmp_path / "blocked.coalesce.json"
    service = DocumentService(path)
    for tool, payload in [
        ("sketch", SKETCH),
        ("feature", FEATURE),
        (
            "feature",
            hole(
                request_id="hole",
                revision=2,
                feature_id="hole",
                target="plate_extrude",
                diameter=2,
                position=[0, 0],
            ),
        ),
    ]:
        assert (await call_server_tool(service, tool, payload))["ok"]
    before = path.read_bytes()

    blocked = await call_server_tool(
        service, "sketch", delete_sketch("plate_profile", 3, "blocked-delete")
    )

    assert blocked["error"]["code"] == "HAS_DEPENDENTS"
    assert blocked["error"]["dependents"] == [
        {"id": "plate_extrude", "kind": "extrude"},
        {"id": "hole", "kind": "hole"},
    ]
    assert blocked["error"]["hint"] == "delete the listed dependents first or pass cascade: true"
    assert service.revision == 3
    assert path.read_bytes() == before


@pytest.mark.anyio
async def test_deleting_mid_chain_requires_successor_removal_and_restores_prior_volume(tmp_path):
    service = DocumentService(tmp_path / "lineage.coalesce.json")
    for tool, payload in [
        ("import", import_request(FIXTURES / "features.kicad_pcb")),
        ("feature", base_request()),
        ("feature", standoffs_request()),
    ]:
        assert (await call_server_tool(service, tool, payload))["ok"]
    earlier_volume = service.query({"select": {"kind": "measurements"}})["volume"]
    drilled = hole(
        request_id="generic-hole",
        revision=3,
        feature_id="generic-hole",
        target="mounts",
        diameter=1,
        position=[10, 5],
    )
    assert (await call_server_tool(service, "feature", drilled))["ok"]
    assert (await call_server_tool(service, "feature", port_request(revision=4)))["ok"]

    blocked = await call_server_tool(
        service, "feature", delete_feature("generic-hole", 5, "blocked-hole")
    )
    assert blocked["error"]["code"] == "HAS_DEPENDENTS"
    assert blocked["error"]["dependents"] == [{"id": "port", "kind": "port_cutout"}]

    assert (await call_server_tool(service, "feature", delete_feature("port", 5, "delete-port")))[
        "ok"
    ]
    removed_hole = await call_server_tool(
        service, "feature", delete_feature("generic-hole", 6, "delete-hole")
    )
    assert removed_hole["ok"]
    assert service.query({"select": {"kind": "measurements"}})["volume"] == earlier_volume
    assert service._regen.predecessors == {"mounts": "base"}


@pytest.mark.anyio
async def test_sketch_delete_cascade_and_reference_queries_have_no_deleted_provenance(tmp_path):
    service = DocumentService(tmp_path / "sketch.coalesce.json")
    assert (await call_server_tool(service, "sketch", SKETCH))["ok"]
    assert (await call_server_tool(service, "feature", FEATURE))["ok"]

    deleted = await call_server_tool(
        service, "sketch", delete_sketch("plate_profile", 2, "delete-sketch", cascade=True)
    )

    assert deleted["deleted"] == [
        {"id": "plate_profile", "kind": "sketch"},
        {"id": "plate_extrude", "kind": "extrude"},
    ]
    reference = await call_server_tool(
        service,
        "query",
        {
            "select": {
                "kind": "reference",
                "reference": {"feature": "plate_extrude", "role": "cap_end"},
            }
        },
    )
    assert reference["status"] == "EMPTY"
    assert reference["error"]["status"] == "EMPTY"
    assert service.query({"select": {"kind": "measurements"}})["parts"] == []


@pytest.mark.anyio
async def test_import_delete_uses_the_same_dependency_and_cascade_rules(tmp_path):
    service = DocumentService(tmp_path / "import-delete.coalesce.json")
    assert (
        await call_server_tool(
            service, "import", import_request(FIXTURES / "features.kicad_pcb")
        )
    )["ok"]
    assert (await call_server_tool(service, "feature", base_request()))["ok"]

    blocked = await call_server_tool(
        service,
        "import",
        {
            "action": "delete",
            "expectedRevision": 2,
            "requestId": "blocked-board-delete",
            "assetId": "controller_board",
        },
    )
    assert blocked["error"]["code"] == "HAS_DEPENDENTS"
    assert blocked["error"]["dependents"] == [{"id": "base", "kind": "pcb_base"}]

    deleted = await call_server_tool(
        service,
        "import",
        {
            "action": "delete",
            "expectedRevision": 2,
            "requestId": "cascade-board-delete",
            "assetId": "controller_board",
            "cascade": True,
        },
    )
    assert deleted["deleted"] == [
        {"id": "controller_board", "kind": "board_import"},
        {"id": "base", "kind": "pcb_base"},
    ]


@pytest.mark.anyio
async def test_delete_stale_revision_replay_restart_and_request_id_conflict(tmp_path):
    path = tmp_path / "receipts.coalesce.json"
    created = await run_server(path, [("sketch", SKETCH)])
    assert created[0]["revision"] == 1
    request = delete_sketch("plate_profile", 1, "delete-once")

    stale = (await run_server(path, [("sketch", {**request, "expectedRevision": 0})]))[0]
    first, replay = await run_server(path, [("sketch", request), ("sketch", request)])
    restarted_replay = (await run_server(path, [("sketch", request)]))[0]
    conflict = (
        await run_server(
            path,
            [("sketch", {**request, "sketchId": "other"})],
        )
    )[0]

    assert stale["error"]["code"] == "REVISION_CONFLICT"
    assert first == replay == restarted_replay
    assert first["revision"] == 2
    assert conflict["error"]["code"] == "REQUEST_ID_REUSED"
    persisted = json.loads(path.read_text())
    assert persisted["revision"] == 2
    assert persisted["graph"]["nodes"] == []


def test_published_roots_are_plain_objects_and_delete_schemas_are_action_specific():
    from jsonschema import validate

    from coalesce.schemas import ACTION_SCHEMAS, FEATURE_ACTION_SCHEMAS, TOOL_SCHEMAS

    for name in ("feature", "sketch"):
        root = TOOL_SCHEMAS[name]
        assert root["type"] == "object"
        assert not ({"oneOf", "anyOf", "allOf"} & root.keys())
    validate(
        delete_feature("base", 1, "schema-delete", cascade=True),
        FEATURE_ACTION_SCHEMAS[("delete", None)],
    )
    validate(delete_sketch("profile", 1, "schema-sketch"), ACTION_SCHEMAS["sketch"]["delete"])
