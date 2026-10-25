import copy
import json
import math
import os
import shutil
import stat
import threading
import time
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

import coalesce.service as service_module
from coalesce.document_store import Document, DocumentStore
from coalesce.graph import Extrude, Graph, Hole, Rectangle, Sketch
from coalesce.regen import regenerate as real_regenerate
from coalesce.schemas import ACTION_SCHEMAS, TOOL_SCHEMAS
from coalesce.server import create_server
from coalesce.service import (
    DocumentService,
    ExtrudeCreate,
    ExtrudeUpdate,
    FeatureDelete,
    SketchCreate,
    SketchDelete,
    SketchUpdate,
)

SKETCH = {
    "action": "create",
    "expectedRevision": 0,
    "requestId": "sketch-1",
    "sketchId": "plate_profile",
    "plane": {"kind": "origin", "name": "XY"},
    "entities": [
        {
            "id": "outline",
            "kind": "rectangle",
            "center": [0, 0],
            "width": {"value": 60, "unit": "mm"},
            "height": {"value": 40, "unit": "mm"},
            "rotation": {"value": 0, "unit": "deg"},
        }
    ],
}
FEATURE = {
    "action": "create",
    "expectedRevision": 1,
    "requestId": "feature-1",
    "featureId": "plate_extrude",
    "kind": "extrude",
    "parameters": {
        "profile": {"sketchId": "plate_profile", "profileIds": ["outline"]},
        "distance": {"value": 6, "unit": "mm"},
        "operation": "new_body",
        "direction": "normal",
    },
}


@pytest.mark.anyio
async def test_slow_tool_call_does_not_block_other_mcp_requests(tmp_path, monkeypatch):
    service = DocumentService(tmp_path / "concurrent.coalesce.json", warmup=False)
    started = threading.Event()

    def slow_query(_arguments):
        started.set()
        time.sleep(0.5)
        return {"ok": True}

    monkeypatch.setattr(service, "query", slow_query)
    server = create_server(service)
    call_handler = server.request_handlers[types.CallToolRequest]
    list_handler = server.request_handlers[types.ListToolsRequest]

    async def call_slow_tool():
        await call_handler(
            types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name="query", arguments={"select": {"kind": "revision"}}
                )
            )
        )

    slow_completed = anyio.Event()

    async def call_slow_tool_and_record_completion():
        await call_slow_tool()
        slow_completed.set()

    with anyio.fail_after(3):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(call_slow_tool_and_record_completion)
            assert await anyio.to_thread.run_sync(started.wait, 1.0)
            await list_handler(types.ListToolsRequest())
            assert not slow_completed.is_set()


async def call_server_tool(service, name, arguments):
    server = create_server(service)
    handler = server.request_handlers[types.CallToolRequest]
    result = await handler(
        types.CallToolRequest(
            params=types.CallToolRequestParams(name=name, arguments=arguments)
        )
    )
    return result.root.structuredContent


@pytest.mark.anyio
async def test_schema_error_uses_one_locked_snapshot_off_event_loop(tmp_path, monkeypatch):
    service = DocumentService(tmp_path / "atomic-error.coalesce.json", warmup=False)
    assert service.sketch(SKETCH)["revision"] == 1
    context_started = threading.Event()
    mutation_attempted = threading.Event()
    committed = threading.Event()
    context_threads = []
    with_context = service._with_error_context

    def overlap_mutation(payload, failure):
        context_threads.append(threading.get_ident())
        context_started.set()
        assert mutation_attempted.wait(1)
        if not service._lock._is_owned():
            assert committed.wait(3)
        return with_context(payload, failure)

    monkeypatch.setattr(service, "_with_error_context", overlap_mutation)

    def mutate():
        assert context_started.wait(1)
        mutation_attempted.set()
        assert service.feature(FEATURE)["revision"] == 2
        committed.set()

    mutation = threading.Thread(target=mutate)
    mutation.start()
    event_loop_thread = threading.get_ident()
    result = await call_server_tool(
        service,
        "feature",
        {
            "action": "update",
            "expectedRevision": 1,
            "requestId": "invalid-update",
            "featureId": "plate_extrude",
            "kind": "extrude",
            "set": {"distance": {"value": "bad", "unit": "mm"}},
        },
    )
    mutation.join(timeout=3)

    assert not mutation.is_alive()
    assert result["revision"] == 1
    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert "affectedParts" not in result["error"]
    assert context_threads and context_threads[0] != event_loop_thread


def test_update_kind_error_uses_one_locked_snapshot(tmp_path, monkeypatch):
    service = DocumentService(tmp_path / "atomic-kind-error.coalesce.json", warmup=False)
    assert service.sketch(SKETCH)["revision"] == 1
    assert service.feature(FEATURE)["revision"] == 2
    resolved = threading.Event()
    mutation_attempted = threading.Event()
    committed = threading.Event()
    resolve_update_kind = service._resolve_update_kind

    def wait_for_mutation():
        resolved.set()
        assert mutation_attempted.wait(1)
        if not service._lock._is_owned():
            assert committed.wait(3)

    def pause_after_resolution(payload):
        try:
            result = resolve_update_kind(payload)
        except Exception:
            wait_for_mutation()
            raise
        wait_for_mutation()
        return result

    monkeypatch.setattr(service, "_resolve_update_kind", pause_after_resolution)

    def mutate():
        assert resolved.wait(1)
        mutation_attempted.set()
        assert service.feature(
            {
                "action": "delete",
                "expectedRevision": 2,
                "requestId": "delete-feature",
                "featureId": "plate_extrude",
            }
        )["revision"] == 3
        committed.set()

    mutation = threading.Thread(target=mutate)
    mutation.start()
    result = service.feature(
        {
            "action": "update",
            "expectedRevision": 2,
            "requestId": "wrong-kind",
            "featureId": "plate_extrude",
            "kind": "hole",
            "set": {"diameter": {"value": 2, "unit": "mm"}},
        }
    )
    mutation.join(timeout=3)

    assert not mutation.is_alive()
    assert result["revision"] == 2
    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert result["error"]["affectedParts"] == ["plate_extrude/outline"]


def result_json(result):
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


async def run_server(path: Path, calls):
    server = StdioServerParameters(
        command=shutil.which("coalesce") or "coalesce",
        args=["--document", str(path)],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        return [result_json(await session.call_tool(name, arguments)) for name, arguments in calls]


async def list_server_tools(path: Path):
    server = StdioServerParameters(
        command=shutil.which("coalesce") or "coalesce",
        args=["--document", str(path)],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        return (await session.list_tools()).tools


@pytest.mark.anyio
async def test_real_stdio_path_builds_plate_and_survives_restart(tmp_path):
    path = tmp_path / "plate.coalesce.json"
    first = await run_server(
        path,
        [
            ("sketch", SKETCH),
            ("feature", FEATURE),
            ("query", {"select": {"kind": "graph"}}),
            ("query", {"select": {"kind": "measurements"}}),
            ("check", {"action": "evaluate", "requirements": [{"id": "valid", "kind": "valid_brep"}]}),
        ],
    )

    assert [first[0]["revision"], first[1]["revision"]] == [1, 2]
    assert "solver" not in first[0]
    assert first[2]["revision"] == 2
    assert {node["id"] for node in first[2]["graph"]["nodes"]} == {
        "plate_profile",
        "plate_extrude",
    }
    assert first[3]["bbox"] == {
        "x": {"value": 60, "unit": "mm"},
        "y": {"value": 40, "unit": "mm"},
        "z": {"value": 6, "unit": "mm"},
        "min": {"x": -30, "y": -20, "z": 0, "unit": "mm"},
        "max": {"x": 30, "y": 20, "z": 6, "unit": "mm"},
        "center": {"x": 0, "y": 0, "z": 3, "unit": "mm"},
    }
    assert first[3]["volume"] == {"value": 14400, "unit": "mm^3"}
    assert first[4]["results"][0]["status"] == "PASS"

    restarted = await run_server(
        path,
        [
            ("query", {"select": {"kind": "graph"}}),
            ("query", {"select": {"kind": "measurements"}}),
        ],
    )
    assert restarted[0] == first[2]
    assert restarted[1] == first[3]


def test_startup_regeneration_cannot_overwrite_a_concurrent_commit(tmp_path, monkeypatch):
    graph = Graph()
    graph.add(Sketch("plate_profile", entities=[Rectangle("outline", 60, 40)]))
    graph.add(Extrude("plate_extrude", sketch="plate_profile", height=6))
    path = tmp_path / "plate.coalesce.json"
    DocumentStore(path).save(Document(graph, revision=2))

    startup_entered = threading.Event()
    release_startup = threading.Event()
    service_module._KERNEL_LOADER.wait()

    def blocked_regenerate(graph):
        startup_entered.set()
        release_startup.wait()
        return real_regenerate(graph)

    monkeypatch.setattr(service_module, "regenerate", blocked_regenerate, raising=False)
    service = DocumentService(path, warmup=False)
    service.start_kernel_warmup()
    assert startup_entered.wait(5)

    committed = threading.Event()

    def update_feature() -> None:
        result = service.feature(
            {
                "action": "update",
                "expectedRevision": 2,
                "requestId": "feature-update",
                "featureId": "plate_extrude",
                "kind": "extrude",
                "set": {"distance": {"value": 8, "unit": "mm"}},
            }
        )
        assert result["revision"] == 3
        committed.set()

    mutation = threading.Thread(target=update_feature)
    mutation.start()
    try:
        committed_during_startup = committed.wait(0.5)
    finally:
        release_startup.set()
        mutation.join(timeout=5)

    assert not committed_during_startup
    assert not mutation.is_alive()
    assert service.query({"select": {"kind": "measurements"}})["bbox"]["z"]["value"] == 8


def test_measurements_include_only_terminal_bodies(tmp_path):
    graph = Graph()
    graph.add(Sketch("plate_profile", entities=[Rectangle("outline", 60, 40)]))
    graph.add(Extrude("plate", sketch="plate_profile", height=6))
    graph.add(Hole("mounting_hole", target="plate", diameter=10, depth=None))
    path = tmp_path / "plate.coalesce.json"
    DocumentStore(path).save(Document(graph, revision=2, receipts=[]))

    measurements = DocumentService(path).query({"select": {"kind": "measurements"}})

    assert measurements["solidCount"] == 1
    assert measurements["volume"]["value"] == pytest.approx(14400 - 150 * math.pi)
    assert measurements["bbox"] == {
        "x": {"value": 60, "unit": "mm"},
        "y": {"value": 40, "unit": "mm"},
        "z": {"value": 6, "unit": "mm"},
        "min": {"x": -30, "y": -20, "z": 0, "unit": "mm"},
        "max": {"x": 30, "y": 20, "z": 6, "unit": "mm"},
        "center": {"x": 0, "y": 0, "z": 3, "unit": "mm"},
    }


def test_replay_conflict_and_stale_revision_are_atomic(tmp_path):
    path = tmp_path / "plate.coalesce.json"
    service = DocumentService(path)

    created = service.sketch(SKETCH)
    before_replay = path.read_bytes()
    assert service.sketch(SKETCH) == created
    assert path.read_bytes() == before_replay

    reused = service.sketch({**SKETCH, "sketchId": "different"})
    assert reused["error"]["code"] == "REQUEST_ID_REUSED"
    assert path.read_bytes() == before_replay

    stale = service.feature({**FEATURE, "expectedRevision": 0})
    assert stale["error"]["code"] == "REVISION_CONFLICT"
    assert path.read_bytes() == before_replay


def test_update_sketch_and_extrude(tmp_path):
    service = DocumentService(tmp_path / "plate.coalesce.json")
    service.sketch(SKETCH)
    service.feature(FEATURE)

    sketch_result = service.sketch(
        {
            "action": "update",
            "expectedRevision": 2,
            "requestId": "sketch-2",
            "sketchId": "plate_profile",
            "set": {"position": [5, 2, 0]},
        }
    )
    feature_result = service.feature(
        {
            "action": "update",
            "expectedRevision": 3,
            "requestId": "feature-2",
            "featureId": "plate_extrude",
            "kind": "extrude",
            "set": {"distance": {"value": 8, "unit": "mm"}},
        }
    )

    assert sketch_result["revision"] == 3
    assert feature_result["revision"] == 4
    assert service.query({"select": {"kind": "measurements"}})["bbox"]["z"]["value"] == 8


def test_replay_hash_applies_operation_defaults_and_rejects_different_intent(tmp_path):
    service = DocumentService(tmp_path / "plate.coalesce.json")
    omitted_defaults = {
        **SKETCH,
        "entities": [
            {
                key: value
                for key, value in SKETCH["entities"][0].items()
                if key not in {"center", "rotation"}
            }
        ],
    }
    created = service.sketch(omitted_defaults)

    assert service.sketch(SKETCH) == created

    different = service.sketch(
        {
            **SKETCH,
            "entities": [
                {
                    **SKETCH["entities"][0],
                    "center": [1, 0],
                }
            ],
        }
    )
    assert different["error"]["code"] == "REQUEST_ID_REUSED"


@pytest.mark.parametrize(
    ("field", "negative", "positive"),
    [
        ("position", [-0.0, 0.0], [0.0, 0.0]),
        ("quantity", {"value": -0.0, "unit": "mm"}, {"value": 0.0, "unit": "mm"}),
        ("angle", {"value": -0.0, "unit": "deg"}, {"value": 0.0, "unit": "deg"}),
    ],
)
def test_replay_hash_normalizes_signed_zero(tmp_path, field, negative, positive):
    service = DocumentService(tmp_path / f"{field}.coalesce.json")
    negative_entity = {**SKETCH["entities"][0]}
    positive_entity = {**SKETCH["entities"][0]}
    if field == "position":
        negative_entity["center"] = negative
        positive_entity["center"] = positive
    elif field == "quantity":
        negative_entity["width"] = negative
        positive_entity["width"] = positive
    else:
        negative_entity["rotation"] = negative
        positive_entity["rotation"] = positive

    created = service.sketch({**SKETCH, "entities": [negative_entity]})

    assert service.sketch({**SKETCH, "entities": [positive_entity]}) == created


def test_replay_hash_normalizes_numeric_forms_and_angles(tmp_path):
    service = DocumentService(tmp_path / "plate.coalesce.json")
    request = {
        **SKETCH,
        "entities": [{**SKETCH["entities"][0], "rotation": {"value": 180, "unit": "deg"}}],
    }
    created = service.sketch(request)

    replay = service.sketch(
        {
            **request,
            "expectedRevision": 0.0,
            "entities": [
                {
                    **request["entities"][0],
                    "center": [0.0, 0.0],
                    "width": {"value": 60.0, "unit": "mm"},
                    "rotation": {"value": math.pi, "unit": "rad"},
                }
            ],
        }
    )

    assert replay == created


def test_request_id_tombstone_survives_full_receipt_eviction(tmp_path):
    service = DocumentService(tmp_path / "plate.coalesce.json")
    service.sketch(SKETCH)
    for revision in range(1, 101):
        result = service.sketch(
            {
                "action": "update",
                "expectedRevision": revision,
                "requestId": f"update-{revision}",
                "sketchId": "plate_profile",
                "set": {"position": [revision, 0, 0]},
            }
        )
        assert result["ok"] is True

    service = DocumentService(tmp_path / "plate.coalesce.json")
    reused = service.sketch(
        {
            **SKETCH,
            "expectedRevision": 101,
            "sketchId": "different",
        }
    )
    assert reused["error"]["code"] == "REQUEST_ID_REUSED"


@pytest.mark.anyio
async def test_real_stdio_publishes_root_object_schemas(tmp_path):
    tools = await list_server_tools(tmp_path / "schemas.coalesce.json")

    assert tools
    for tool in tools:
        assert tool.inputSchema["type"] == "object"
        assert not ({"anyOf", "allOf", "oneOf"} & tool.inputSchema.keys())


def test_mutation_schema_fields_match_typed_operations():
    operation_types = {
        "sketch": {
            "create": SketchCreate,
            "update": SketchUpdate,
            "delete": SketchDelete,
        },
        "feature": {
            "create": ExtrudeCreate,
            "update": ExtrudeUpdate,
            "delete": FeatureDelete,
        },
    }

    for tool, actions in operation_types.items():
        published = TOOL_SCHEMAS[tool]
        assert set(published["properties"]["action"]["enum"]) == set(actions)
        expected_fields = set().union(*(operation.wire_fields() for operation in actions.values()))
        if tool == "feature":
            expected_fields.add("occurrenceId")
        assert set(published["properties"]) == expected_fields
        for action, operation_type in actions.items():
            private = ACTION_SCHEMAS[tool][action]
            assert set(private["properties"]) == operation_type.wire_fields()


@pytest.mark.anyio
@pytest.mark.parametrize("value", [pytest.param(math.nan, id="NaN"), pytest.param(math.inf, id="Infinity"), pytest.param(1e400, id="1e400")])
@pytest.mark.parametrize("field", ["width", "center", "rotation"])
async def test_call_tool_rejects_non_finite_numbers_without_mutation(tmp_path, field, value):
    path = tmp_path / f"{field}.coalesce.json"
    service = DocumentService(path)
    assert (await call_server_tool(service, "sketch", SKETCH))["ok"] is True
    before = path.read_bytes()
    entity = copy.deepcopy(SKETCH["entities"][0])
    if field == "width":
        entity["width"]["value"] = value
    elif field == "center":
        entity["center"][0] = value
    else:
        entity["rotation"]["value"] = value

    result = await call_server_tool(
        service,
        "sketch",
        {
            "action": "update",
            "expectedRevision": 1,
            "requestId": f"bad-{field}",
            "sketchId": "plate_profile",
            "set": {"entities": [entity]},
        },
    )

    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert result["error"]["field"] == field
    assert service.revision == 1
    assert path.read_bytes() == before


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "payload", "field"),
    [
        ("sketch", {**SKETCH, "set": {"position": [1, 2, 3]}}, "set"),
        (
            "sketch",
            {
                "action": "update",
                "expectedRevision": 0,
                "requestId": "sketch-update-plane",
                "sketchId": "plate_profile",
                "set": {"position": [1, 2, 3]},
                "plane": {"kind": "origin", "name": "XY"},
            },
            "plane",
        ),
        (
            "sketch",
            {
                "action": "update",
                "expectedRevision": 0,
                "requestId": "sketch-update-entities",
                "sketchId": "plate_profile",
                "set": {"position": [1, 2, 3]},
                "entities": SKETCH["entities"],
            },
            "entities",
        ),
        ("feature", {**FEATURE, "set": {"distance": {"value": 9, "unit": "mm"}}}, "set"),
        (
            "feature",
            {
                "action": "update",
                "expectedRevision": 0,
                "requestId": "feature-update-parameters",
                "featureId": "plate_extrude",
                "kind": "extrude",
                "set": {"distance": {"value": 9, "unit": "mm"}},
                "parameters": FEATURE["parameters"],
            },
            "parameters",
        ),
    ],
)
async def test_action_schemas_reject_inapplicable_fields(tmp_path, tool, payload, field):
    result = await call_server_tool(
        DocumentService(tmp_path / f"{tool}-{field}.coalesce.json"), tool, payload
    )

    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert field not in result["error"]["allowedFields"]


@pytest.mark.anyio
async def test_schemas_reject_deferred_fields_with_allowed_list(tmp_path):
    service = DocumentService(tmp_path / "plate.coalesce.json")
    allowed = set(SKETCH)
    for field, value in (
        ("partId", "plate"),
        ("constraints", [{"id": "c1", "kind": "coincident", "entities": ["a", "b"]}]),
    ):
        result = await call_server_tool(service, "sketch", {**SKETCH, field: value})
        assert result["error"]["code"] == "INVALID_PARAMETER"
        assert set(result["error"]["allowedFields"]) == allowed

    feature = await call_server_tool(service, "feature", {**FEATURE, "partId": "plate"})
    assert feature["error"]["code"] == "INVALID_PARAMETER"
    assert "partId" not in feature["error"]["allowedFields"]

    query = await call_server_tool(
        service,
        "query",
        {"select": {"kind": "graph", "ids": ["plate_profile"]}},
    )
    assert query["error"]["code"] == "INVALID_PARAMETER"
    assert "ids" not in query["error"]["allowedFields"]


@pytest.mark.anyio
async def test_angle_schema_accepts_radians_and_rejects_untyped_update_rotation(tmp_path):
    service = DocumentService(tmp_path / "plate.coalesce.json")
    radians = {
        **SKETCH,
        "entities": [
            {
                **SKETCH["entities"][0],
                "rotation": {"value": math.pi / 2, "unit": "rad"},
            }
        ],
    }
    created = await call_server_tool(service, "sketch", radians)
    assert created["ok"] is True
    graph = service.query({"select": {"kind": "graph"}})["graph"]
    assert graph["nodes"][0]["entities"][0]["rotation"] == pytest.approx(90)

    rejected = await call_server_tool(
        service,
        "sketch",
        {
            "action": "update",
            "expectedRevision": 1,
            "requestId": "rotate",
            "sketchId": "plate_profile",
            "set": {"rotation": [0, 0, 90]},
        },
    )
    assert rejected["error"]["code"] == "INVALID_PARAMETER"
    assert "rotation" not in rejected["error"]["allowedFields"]


def test_post_rename_fsync_failure_commits_and_replays_identically(
    tmp_path, monkeypatch, caplog
):
    path = tmp_path / "plate.coalesce.json"
    service = DocumentService(path)
    real_fsync = os.fsync

    def fail_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory fsync failed at /secret/kernel/path")
        return real_fsync(fd)

    monkeypatch.setattr("coalesce.document_store.os.fsync", fail_directory_fsync)
    created = service.sketch(SKETCH)

    assert created["ok"] is True
    assert "durability" not in created
    assert service.sketch(SKETCH) == created
    assert "durability is unconfirmed" in caplog.text
    assert service.revision == 1
    assert json.loads(path.read_text())["revision"] == 1
    assert DocumentService(path).revision == 1

    monkeypatch.setattr("coalesce.document_store.os.fsync", real_fsync)
    extruded = service.feature(FEATURE)
    assert extruded["ok"] is True
    assert extruded["revision"] == 2
    assert DocumentService(path).revision == 2


@pytest.mark.anyio
async def test_common_tool_seam_redacts_unexpected_metric_failure(tmp_path, monkeypatch):
    service = DocumentService(tmp_path / "plate.coalesce.json")

    def fail_metrics():
        raise RuntimeError("kernel exploded at /secret/kernel/path")

    monkeypatch.setattr(service, "_metrics", fail_metrics)
    result = await call_server_tool(service, "query", {"select": {"kind": "measurements"}})
    encoded = json.dumps(result)
    assert result["error"]["code"] == "KERNEL_OPERATION_FAILED"
    assert "kernel exploded" not in encoded
    assert "/secret/kernel/path" not in encoded


@pytest.mark.anyio
async def test_feature_update_uses_existing_kind_when_omitted(tmp_path):
    service = DocumentService(tmp_path / "update-without-kind.coalesce.json")
    service.sketch(SKETCH)
    service.feature(FEATURE)

    result = await call_server_tool(
        service,
        "feature",
        {
            "action": "update",
            "expectedRevision": 2,
            "requestId": "thicker",
            "featureId": "plate_extrude",
            "set": {"distance": {"value": 8, "unit": "mm"}},
        },
    )

    assert result["ok"] is True
    assert result["revision"] == 3
    assert service._document.graph.nodes["plate_extrude"].height == 8


@pytest.mark.anyio
async def test_feature_update_rejects_kind_that_differs_from_existing_feature(tmp_path):
    service = DocumentService(tmp_path / "update-wrong-kind.coalesce.json")
    service.sketch(SKETCH)
    service.feature(FEATURE)

    result = await call_server_tool(
        service,
        "feature",
        {
            "action": "update",
            "expectedRevision": 2,
            "requestId": "wrong-kind",
            "featureId": "plate_extrude",
            "kind": "pcb_base",
            "set": {"wallThickness": {"value": 2, "unit": "mm"}},
        },
    )

    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert result["error"]["expectedKind"] == "extrude"
    assert result["error"]["actualKind"] == "pcb_base"
    assert service.revision == 2


def test_unknown_feature_kind_and_invalid_geometry_are_safe(tmp_path):
    service = DocumentService(tmp_path / "plate.coalesce.json")
    unknown = service.feature(
        {
            "action": "create",
            "expectedRevision": 0,
            "requestId": "future-1",
            "featureId": "future",
            "kind": "warp_drive",
            "parameters": {},
        }
    )
    assert unknown["error"]["code"] == "INVALID_PARAMETER"
    assert unknown["error"]["allowedKinds"] == [
        "extrude",
        "revolve",
        "cut",
        "hole",
        "fillet",
        "chamfer",
        "pcb_base",
        "standoffs",
        "pcb_lid",
        "port_cutout",
        "screw_closure",
    ]

    failed = service.sketch(
        {
            **SKETCH,
            "entities": [
                {
                    **SKETCH["entities"][0],
                    "width": {"value": 0, "unit": "mm"},
                }
            ],
        }
    )
    encoded = json.dumps(failed)
    assert failed["error"]["code"] == "INVALID_PARAMETER"
    assert "build123d" not in encoded.lower()
    assert "occt" not in encoded.lower()


def test_reference_query_returns_status_keys_and_fingerprints(tmp_path):
    service = DocumentService(tmp_path / "plate.coalesce.json")
    service.sketch(SKETCH)
    service.feature(FEATURE)

    result = service.query(
        {
            "select": {
                "kind": "reference",
                "reference": {
                    "feature": "plate_extrude",
                    "role": "cap_end",
                    "cardinality": "one",
                },
            }
        }
    )

    assert result["status"] == "OK"
    assert result["keys"][0]["feature"] == "plate_extrude"
    assert len(result["fingerprints"][0]) == 64
