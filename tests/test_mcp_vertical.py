import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from coalesce.service import DocumentService


SKETCH = {
    "action": "create",
    "expectedRevision": 0,
    "requestId": "sketch-1",
    "sketchId": "plate_profile",
    "partId": "plate",
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
    "partId": "plate",
    "kind": "extrude",
    "parameters": {
        "profile": {"sketchId": "plate_profile", "profileIds": ["outline"]},
        "distance": {"value": 6, "unit": "mm"},
        "operation": "new_body",
        "direction": "normal",
    },
}


def result_json(result):
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


async def run_server(path: Path, calls):
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "coalesce", "--document", str(path)],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
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
            "set": {"position": [5, 2, 0], "rotation": [0, 0, 0]},
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
