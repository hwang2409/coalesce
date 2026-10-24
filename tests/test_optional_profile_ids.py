import asyncio
import json

import pytest
from jsonschema import validate
from mcp import types
from test_constrained_sketch import graph_sketch, run_server
from test_mcp_cut_hole_circle import extrude
from test_revolve import PULLEY, polyline_sketch, revolve

from coalesce.server import create_server
from coalesce.service import DocumentService
from evals.golden.common import score_artifact
from evals.golden.constrained_common import extrude as constrained_extrude
from evals.golden.constrained_common import plate_model, sketch_create


def _without_profile_ids(request):
    payload = json.loads(json.dumps(request))
    del payload["parameters"]["profile"]["profileIds"]
    return payload


def test_omitted_profile_ids_passes_constrained_inner_loop_golden(tmp_path):
    document = tmp_path / "model.coalesce.json"
    (tmp_path / "steps").mkdir()
    request = _without_profile_ids(constrained_extrude()[1])

    results = asyncio.run(
        run_server(document, [sketch_create(plate_model(4)), ("feature", request)])
    )
    checks = score_artifact(
        tmp_path,
        "constrained-inner-loop",
        document,
        results[-1]["revision"],
    )

    assert results[-1]["ok"] is True
    assert all(check["status"] == "pass" for check in checks)


@pytest.mark.anyio
async def test_omitted_profile_ids_survive_restart_and_dimension_update(tmp_path):
    document = tmp_path / "identity.coalesce.json"
    request = _without_profile_ids(constrained_extrude()[1])
    first = await run_server(
        document,
        [sketch_create(plate_model(3)), ("feature", request)],
    )

    assert first[-1]["ok"] is True
    saved = json.loads(document.read_text())
    plate = next(node for node in saved["graph"]["nodes"] if node["id"] == "plate")
    assert plate["profile_ids"] == ["profile"]

    restarted = await run_server(
        document,
        [
            (
                "sketch",
                {
                    "action": "update",
                    "expectedRevision": 2,
                    "requestId": "resize-opening",
                    "sketchId": "profile",
                    "set": {"dimensionValues": {"opening-radius": 4}},
                },
            ),
            ("query", {"select": {"kind": "graph"}}),
            ("query", {"select": {"kind": "measurements"}}),
        ],
    )

    assert restarted[0]["ok"] is True
    assert graph_sketch(restarted[1])["constrained"]["dimensions"][0]["value"] == 4
    plate = next(node for node in restarted[1]["graph"]["nodes"] if node["id"] == "plate")
    assert plate["profile_ids"] == ["profile"]
    assert restarted[2]["parts"][0]["sourceBody"] == {
        "feature": "plate",
        "entity": "profile",
    }


@pytest.mark.parametrize(
    "received",
    [
        ["outer_boundary", "hole"],
        ["profile1"],
        ["profile1", "profile2"],
        ["outer"],
        ["line0", "line1", "line2", "line3", "hole"],
    ],
)
@pytest.mark.anyio
async def test_wrong_constrained_profile_ids_return_expected_ids(tmp_path, received):
    request = constrained_extrude()[1]
    request["parameters"]["profile"]["profileIds"] = received

    created, result = await run_server(
        tmp_path / "wrong-ids.coalesce.json",
        [sketch_create(plate_model(4)), ("feature", request)],
    )
    assert created["ok"] is True

    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert result["error"]["expectedProfileIds"] == ["profile"]
    assert result["error"]["unknownProfileIds"] == received
    assert result["error"]["message"].endswith(
        "omit profileIds to use all closed loops in graph order."
    )


def test_wrong_primitive_profile_ids_return_bounded_graph_order(tmp_path):
    service = DocumentService(tmp_path / "bounded-ids.coalesce.json", warmup=False)
    entities = [
        {
            "id": f"profile-{index:02}",
            "kind": "circle",
            "radius": {"value": 1, "unit": "mm"},
            "center": [index * 3, 0],
        }
        for index in range(25)
    ]
    assert service.sketch(
        {
            "action": "create",
            "expectedRevision": 0,
            "requestId": "many-profiles",
            "sketchId": "many",
            "plane": {"kind": "origin", "name": "XY"},
            "entities": entities,
        }
    )["ok"]
    request = extrude(
        request_id="wrong",
        revision=1,
        feature_id="wrong",
        sketch_id="many",
        profile_ids=["guess"],
        distance=2,
    )

    error = service.feature(request)["error"]

    assert error["expectedProfileIds"] == [entity["id"] for entity in entities[:20]]
    assert error["expectedProfileIdsTruncated"] is True
    assert error["unknownProfileIds"] == ["guess"]
    assert error["message"].endswith(
        "omit profileIds to use all closed loops in graph order."
    )


@pytest.mark.anyio
async def test_primitive_multi_profile_omission_resolves_graph_order(tmp_path):
    sketch = {
        "action": "create",
        "expectedRevision": 0,
        "requestId": "profiles",
        "sketchId": "profiles",
        "plane": {"kind": "origin", "name": "XY"},
        "entities": [
            {
                "id": "left",
                "kind": "rectangle",
                "width": {"value": 4, "unit": "mm"},
                "height": {"value": 4, "unit": "mm"},
                "center": [-5, 0],
            },
            {
                "id": "right",
                "kind": "circle",
                "radius": {"value": 2, "unit": "mm"},
                "center": [5, 0],
            },
        ],
    }
    request = extrude(
        request_id="both",
        revision=1,
        feature_id="both",
        sketch_id="profiles",
        profile_ids=["left", "right"],
        distance=2,
    )
    del request["parameters"]["profile"]["profileIds"]

    results = await run_server(
        tmp_path / "primitive.coalesce.json",
        [("sketch", sketch), ("feature", request), ("query", {"select": {"kind": "graph"}})],
    )

    assert results[1]["ok"] is True
    feature = next(node for node in results[2]["graph"]["nodes"] if node["id"] == "both")
    assert feature["profile_ids"] == ["left", "right"]


@pytest.mark.anyio
async def test_revolve_omission_is_allowed_for_one_profile_and_schema_is_true(tmp_path):
    request = _without_profile_ids(revolve(feature_id="pulley"))
    results = await run_server(
        tmp_path / "revolve.coalesce.json",
        [("sketch", polyline_sketch(PULLEY)), ("feature", request)],
    )

    assert results[-1]["ok"] is True
    server = create_server(DocumentService(tmp_path / "schema.coalesce.json", warmup=False))
    listed = await server.request_handlers[types.ListToolsRequest](types.ListToolsRequest())
    feature_schema = next(tool for tool in listed.root.tools if tool.name == "feature").inputSchema
    validate(request, feature_schema)
    profile_text = json.dumps(feature_schema["properties"]["parameters"]).lower()
    assert "omit" in profile_text and "one outer loop" in profile_text


def _outline(entity_id=None, x=0):
    entity = {
        "kind": "polyline",
        "points": [[x, 0], [x + 2, 0], [x + 1, 2]],
        "closed": True,
    }
    if entity_id is not None:
        entity["id"] = entity_id
    return entity


def _primitive_sketch(entities, *, action="create", revision=0, request_id="sketch"):
    payload = {
        "action": action,
        "expectedRevision": revision,
        "requestId": request_id,
        "sketchId": "profiles",
    }
    if action == "create":
        payload.update(
            {
                "plane": {"kind": "origin", "name": "XY"},
                "entities": entities,
            }
        )
    else:
        payload["set"] = {"entities": entities}
    return payload


@pytest.mark.parametrize(
    "changed",
    [
        [_outline(x=10), _outline(x=20)],
        [_outline(x=10), _outline(x=0)],
    ],
    ids=["remove-and-append", "reorder"],
)
def test_generated_profile_ids_go_stale_when_entities_change(tmp_path, changed):
    document = tmp_path / "stale-profile-ids.coalesce.json"
    service = DocumentService(document)
    original = [_outline(x=0), _outline(x=10)]
    assert service.sketch(_primitive_sketch(original))["ok"]
    assert service.feature(
        extrude(
            request_id="extrude",
            revision=1,
            feature_id="body",
            sketch_id="profiles",
            profile_ids=["polyline-1", "polyline-2"],
            distance=2,
        )
    )["ok"]
    before = document.read_bytes()
    service = DocumentService(document)

    result = service.sketch(
        _primitive_sketch(changed, action="update", revision=2, request_id="edit")
    )

    assert result["ok"] is False
    assert result["revision"] == 2
    assert result["error"]["code"] == "REFERENCE_FAILED"
    assert result["error"]["featureId"] == "body"
    assert result["error"]["sketch"] == "profiles"
    assert result["error"]["staleProfileIds"] == ["polyline-1", "polyline-2"]
    assert "give the outlines explicit ids" in result["error"]["message"]
    assert document.read_bytes() == before


@pytest.mark.parametrize(
    "changed",
    [
        [_outline("right", 10), _outline("replacement", 20)],
        [_outline("right", 10), _outline("left", 0)],
    ],
    ids=["remove-and-append", "reorder"],
)
def test_explicit_profile_id_follows_entity_edits(tmp_path, changed):
    document = tmp_path / "explicit-profile-ids.coalesce.json"
    service = DocumentService(document)
    original = [_outline("left", 0), _outline("right", 10)]
    assert service.sketch(_primitive_sketch(original))["ok"]
    assert service.feature(
        extrude(
            request_id="extrude",
            revision=1,
            feature_id="body",
            sketch_id="profiles",
            profile_ids=["right"],
            distance=2,
        )
    )["ok"]
    service = DocumentService(document)

    result = service.sketch(
        _primitive_sketch(changed, action="update", revision=2, request_id="edit")
    )

    assert result["ok"] is True
    graph = service.query({"select": {"kind": "graph"}})["graph"]["nodes"]
    body = next(node for node in graph if node["id"] == "body")
    assert body["profile_ids"] == ["right"]


def test_revolve_generated_profile_id_goes_stale_when_entities_change(tmp_path):
    document = tmp_path / "stale-revolve-profile-id.coalesce.json"
    service = DocumentService(document)
    assert service.sketch(_primitive_sketch([_outline(x=0)]))["ok"]
    request = revolve(feature_id="turned", sketch_id="profiles")
    request["parameters"]["profile"]["profileIds"] = ["polyline-1"]
    assert service.feature(request)["ok"]

    result = service.sketch(
        _primitive_sketch([_outline(x=1)], action="update", revision=2, request_id="edit")
    )

    assert result["error"]["code"] == "REFERENCE_FAILED"
    assert result["error"]["featureId"] == "turned"
    assert result["error"]["staleProfileIds"] == ["polyline-1"]


def test_explicit_profile_subset_is_stored_in_graph_order(tmp_path):
    document = tmp_path / "profile-subset.coalesce.json"
    service = DocumentService(document)
    entities = [_outline("left", 0), _outline("middle", 10), _outline("right", 20)]
    assert service.sketch(_primitive_sketch(entities))["ok"]

    result = service.feature(
        extrude(
            request_id="subset",
            revision=1,
            feature_id="body",
            sketch_id="profiles",
            profile_ids=["right", "left"],
            distance=2,
        )
    )

    assert result["ok"] is True
    graph = service.query({"select": {"kind": "graph"}})["graph"]["nodes"]
    body = next(node for node in graph if node["id"] == "body")
    assert body["profile_ids"] == ["left", "right"]


def test_duplicate_profile_ids_are_named(tmp_path):
    service = DocumentService(tmp_path / "duplicate-profile-id.coalesce.json")
    assert service.sketch(_primitive_sketch([_outline("left", 0)]))["ok"]

    result = service.feature(
        extrude(
            request_id="duplicate",
            revision=1,
            feature_id="body",
            sketch_id="profiles",
            profile_ids=["left", "left"],
            distance=2,
        )
    )

    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert result["error"]["duplicateProfileIds"] == ["left"]
    assert result["error"]["unknownProfileIds"] == []


def test_unknown_profile_ids_are_named(tmp_path):
    service = DocumentService(tmp_path / "unknown-profile-id.coalesce.json")
    assert service.sketch(
        _primitive_sketch([_outline("left", 0), _outline("right", 10)])
    )["ok"]

    result = service.feature(
        extrude(
            request_id="unknown",
            revision=1,
            feature_id="body",
            sketch_id="profiles",
            profile_ids=["missing", "also_missing"],
            distance=2,
        )
    )

    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert result["error"]["expectedProfileIds"] == ["left", "right"]
    assert result["error"]["unknownProfileIds"] == ["missing", "also_missing"]
    assert "omit profileIds to use all closed loops in graph order." in result["error"]["message"]


def test_unknown_and_duplicate_profile_ids_are_bounded(tmp_path):
    service = DocumentService(tmp_path / "bounded-profile-ids.coalesce.json")
    assert service.sketch(
        _primitive_sketch([_outline("left", 0), _outline("right", 10)])
    )["ok"]
    unknown = [f"missing-{index}" for index in range(100)]
    duplicates = [f"dup-{index}" for index in range(100)]

    result = service.feature(
        extrude(
            request_id="bounded",
            revision=1,
            feature_id="body",
            sketch_id="profiles",
            profile_ids=unknown + duplicates + duplicates,
            distance=2,
        )
    )

    error = result["error"]
    assert error["code"] == "INVALID_PARAMETER"
    assert len(error["unknownProfileIds"]) == 20
    assert error["unknownProfileIdsTruncated"] is True
    assert len(error["duplicateProfileIds"]) == 20
    assert error["duplicateProfileIdsTruncated"] is True
    assert "missing-20" not in error["message"]
    assert len(error["message"]) < 1500
