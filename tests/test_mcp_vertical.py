import json
import math
import os
import shutil
import stat
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from coalesce.schemas import TOOL_SCHEMAS
from coalesce.server import create_server
from coalesce.service import DocumentService

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


async def call_server_tool(service, name, arguments):
    server = create_server(service)
    handler = server.request_handlers[types.CallToolRequest]
    result = await handler(
        types.CallToolRequest(
            params=types.CallToolRequestParams(name=name, arguments=arguments)
        )
    )
    return result.root.structuredContent


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


def _schema_fields(schema, prefix=""):
    fields = set()
    for name, subschema in schema.get("properties", {}).items():
        path = f"{prefix}.{name}" if prefix else name
        fields.add(path)
        fields.update(_schema_fields(subschema, path))
    if "items" in schema:
        fields.update(_schema_fields(schema["items"], f"{prefix}[]"))
    for subschema in schema.get("oneOf", []):
        fields.update(_schema_fields(subschema, prefix))
    return fields


def test_public_schemas_only_accept_consumed_fields():
    consumed = {
        "sketch": {
            "action", "expectedRevision", "requestId", "sketchId", "plane", "plane.kind",
            "plane.name", "entities", "entities[].id", "entities[].kind", "entities[].center",
            "entities[].width", "entities[].width.value", "entities[].width.unit",
            "entities[].height", "entities[].height.value", "entities[].height.unit",
            "entities[].rotation", "entities[].rotation.value", "entities[].rotation.unit", "set",
            "set.position", "set.entities", "set.entities[].id", "set.entities[].kind",
            "set.entities[].center", "set.entities[].width", "set.entities[].width.value",
            "set.entities[].width.unit", "set.entities[].height", "set.entities[].height.value",
            "set.entities[].height.unit", "set.entities[].rotation",
            "set.entities[].rotation.value", "set.entities[].rotation.unit",
        },
        "feature": {
            "action", "expectedRevision", "requestId", "featureId", "kind", "parameters",
            "parameters.profile", "parameters.profile.sketchId", "parameters.profile.profileIds",
            "parameters.distance", "parameters.distance.value", "parameters.distance.unit",
            "parameters.operation", "parameters.direction", "set", "set.distance",
            "set.distance.value", "set.distance.unit",
        },
        "query": {
            "revision", "select", "select.kind", "select.reference", "select.reference.feature",
            "select.reference.role", "select.reference.cardinality", "select.reference.count",
            "select.reference.source", "select.reference.source.kind",
            "select.reference.source.sketch", "select.reference.source.entity",
            "select.reference.source.segment", "select.reference.body", "select.reference.body.kind",
            "select.reference.body.feature", "select.reference.body.entity",
            "select.reference.filters", "select.reference.filters[].kind",
            "select.reference.filters[].direction", "select.reference.filters[].tolerance",
            "select.reference.filters[].value",
        },
        "check": {
            "action", "revision", "requirements", "requirements[].id", "requirements[].kind",
        },
    }

    assert {name: _schema_fields(schema) for name, schema in TOOL_SCHEMAS.items()} == consumed


@pytest.mark.anyio
async def test_schemas_reject_deferred_fields_with_allowed_list(tmp_path):
    service = DocumentService(tmp_path / "plate.coalesce.json")
    allowed = set(SKETCH) - {"entities", "plane"}
    allowed.update({"entities", "plane", "set"})
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


def test_unknown_feature_kind_and_kernel_failure_are_safe(tmp_path):
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
    assert unknown["error"]["allowedKinds"] == ["extrude", "boolean", "hole", "fillet"]

    service.sketch(
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
    failed = service.feature(FEATURE)
    encoded = json.dumps(failed)
    assert failed["error"]["code"] == "KERNEL_OPERATION_FAILED"
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
