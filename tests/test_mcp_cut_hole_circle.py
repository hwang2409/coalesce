import json
import math

import pytest
from test_mcp_vertical import call_server_tool, list_server_tools, run_server

from coalesce.service import DocumentService


def rectangle_sketch(
    *, request_id, revision, sketch_id, entity_id, width, height, plane="XY", offset=0, center=(0, 0)
):
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": request_id,
        "sketchId": sketch_id,
        "plane": {"kind": "origin", "name": plane, "offset": {"value": offset, "unit": "mm"}},
        "entities": [
            {
                "id": entity_id,
                "kind": "rectangle",
                "center": list(center),
                "width": {"value": width, "unit": "mm"},
                "height": {"value": height, "unit": "mm"},
            }
        ],
    }


def circle_sketch(*, request_id, revision, sketch_id, circles, offset=0):
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": request_id,
        "sketchId": sketch_id,
        "plane": {"kind": "origin", "name": "XY", "offset": {"value": offset, "unit": "mm"}},
        "entities": [
            {
                "id": entity_id,
                "kind": "circle",
                "center": list(center),
                "radius": {"value": radius, "unit": "mm"},
            }
            for entity_id, radius, center in circles
        ],
    }


def extrude(*, request_id, revision, feature_id, sketch_id, profile_ids, distance):
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": request_id,
        "featureId": feature_id,
        "kind": "extrude",
        "parameters": {
            "profile": {"sketchId": sketch_id, "profileIds": profile_ids},
            "distance": {"value": distance, "unit": "mm"},
            "operation": "new_body",
            "direction": "normal",
        },
    }


def cut(*, request_id, revision, feature_id, target, tool, depth=None, flip=False):
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": request_id,
        "featureId": feature_id,
        "kind": "cut",
        "parameters": {
            "target": {"featureId": target},
            "tool": {"sketchId": tool},
            "depth": (
                {"kind": "through_all"}
                if depth is None
                else {"kind": "distance", "distance": {"value": depth, "unit": "mm"}}
            ),
            "flip": flip,
        },
    }


def hole(*, request_id, revision, feature_id, target, position, diameter=3, depth=None):
    parameters = {
        "target": {"featureId": target},
        "diameter": {"value": diameter, "unit": "mm"},
        "position": list(position),
    }
    if depth is not None:
        parameters["depth"] = {"value": depth, "unit": "mm"}
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": request_id,
        "featureId": feature_id,
        "kind": "hole",
        "parameters": parameters,
    }


@pytest.mark.anyio
async def test_tools_advertise_cut_hole_circles_and_origin_planes(tmp_path):
    tools = {tool.name: tool for tool in await list_server_tools(tmp_path / "tools.coalesce.json")}

    feature_schema = tools["feature"].inputSchema
    assert feature_schema["properties"]["kind"]["enum"] == [
        "extrude",
        "cut",
        "hole",
        "fillet",
        "chamfer",
        "pcb_base",
        "standoffs",
        "pcb_lid",
        "port_cutout",
    ]
    assert set(feature_schema["properties"]["set"]["oneOf"][0]["properties"]) == {"distance"}
    assert set(feature_schema["properties"]["set"]["oneOf"][1]["properties"]) == {"target"}
    assert "extrude" in feature_schema["properties"]["set"]["description"]
    assert "cut" in feature_schema["properties"]["set"]["description"]
    assert "hole" in feature_schema["properties"]["set"]["description"]
    sketch = tools["sketch"].inputSchema
    assert sketch["properties"]["plane"]["properties"]["name"]["enum"] == ["XY", "XZ", "YZ"]
    assert {branch["properties"]["kind"]["const"] for branch in sketch["properties"]["entities"]["items"]["oneOf"]} == {"rectangle", "circle"}
    description = tools["feature"].description
    assert "global +Z" in description
    assert "flip=false" in description
    assert "negative sketch normal" in description
    assert "flip=true" in description
    assert "positive sketch normal" in description
    assert "through-all" in description
    assert "bidirectional" in description
    assert "XY +Z, XZ -Y, YZ +X" in description


@pytest.mark.anyio
async def test_feature_schema_uses_kind_to_validate_parameters(tmp_path):
    path = tmp_path / "schema.coalesce.json"
    base = cut(
        request_id="cut",
        revision=0,
        feature_id="cut",
        target="base",
        tool="tool",
        depth=2,
    )
    missing_distance = json.loads(json.dumps(base))
    del missing_distance["parameters"]["depth"]["distance"]
    missing_flip = json.loads(json.dumps(base))
    del missing_flip["parameters"]["flip"]
    invalid_depth = json.loads(json.dumps(base))
    invalid_depth["parameters"]["depth"] = {"kind": "blind"}
    mismatched = extrude(
        request_id="mismatch",
        revision=0,
        feature_id="mismatch",
        sketch_id="sketch",
        profile_ids=["profile"],
        distance=2,
    )
    mismatched["kind"] = "cut"

    results = await run_server(
        path,
        [
            ("feature", missing_distance),
            ("feature", missing_flip),
            ("feature", invalid_depth),
            ("feature", mismatched),
        ],
    )

    assert [result["error"]["message"] for result in results] == [
        "missing required field 'distance' (field: parameters.depth)",
        "missing required field 'flip' (field: parameters)",
        "kind must be one of 'through_all', 'distance' (field: parameters.depth.kind)",
        "missing required field 'target' (field: parameters)",
    ]
    assert all(result["revision"] == 0 for result in results)


@pytest.mark.anyio
async def test_quantity_error_includes_field_path_and_expected_shape(tmp_path):
    request = extrude(
        request_id="missing-value",
        revision=0,
        feature_id="plate",
        sketch_id="plate-sketch",
        profile_ids=["plate"],
        distance=2,
    )
    request["parameters"]["distance"] = {"unit": "mm"}

    result = await call_server_tool(
        DocumentService(tmp_path / "quantity-error.coalesce.json"),
        "feature",
        request,
    )

    assert result["error"]["message"] == (
        "missing required field 'value'; expected shape: "
        '{"value": 2, "unit": "mm"} (field: parameters.distance)'
    )


@pytest.mark.anyio
async def test_cut_update_rejects_extrude_distance_with_allowed_target_field(tmp_path):
    service = DocumentService(tmp_path / "cut-update-schema.coalesce.json")

    result = await call_server_tool(
        service,
        "feature",
        {
            "action": "update",
            "expectedRevision": 0,
            "requestId": "bad-cut-update",
            "featureId": "cut",
            "kind": "cut",
            "set": {"distance": {"value": 2, "unit": "mm"}},
        },
    )

    assert result["error"]["message"] == "unknown field 'distance' (field: set)"
    assert result["error"]["allowedFields"] == ["target"]


@pytest.mark.anyio
async def test_non_intersecting_cut_and_hole_fail_without_advancing_revision(tmp_path):
    path = tmp_path / "no-effect.coalesce.json"
    setup = await run_server(
        path,
        [
            (
                "sketch",
                rectangle_sketch(
                    request_id="base-sketch",
                    revision=0,
                    sketch_id="base_sketch",
                    entity_id="base_profile",
                    width=20,
                    height=20,
                ),
            ),
            (
                "feature",
                extrude(
                    request_id="base",
                    revision=1,
                    feature_id="base",
                    sketch_id="base_sketch",
                    profile_ids=["base_profile"],
                    distance=10,
                ),
            ),
            (
                "sketch",
                rectangle_sketch(
                    request_id="far-tool",
                    revision=2,
                    sketch_id="far_tool",
                    entity_id="far_profile",
                    width=2,
                    height=2,
                    center=(100, 100),
                ),
            ),
        ],
    )
    assert setup[-1]["revision"] == 3

    results = await run_server(
        path,
        [
            (
                "feature",
                cut(
                    request_id="no-cut",
                    revision=3,
                    feature_id="no_cut",
                    target="base",
                    tool="far_tool",
                ),
            ),
            (
                "feature",
                hole(
                    request_id="no-hole",
                    revision=3,
                    feature_id="no_hole",
                    target="base",
                    position=(100, 100),
                ),
            ),
            ("query", {"select": {"kind": "revision"}}),
        ],
    )

    assert [result["error"]["code"] for result in results[:2]] == [
        "CUT_NO_EFFECT",
        "HOLE_NO_EFFECT",
    ]
    for result, feature_id in zip(results[:2], ("no_cut", "no_hole"), strict=True):
        assert result["revision"] == 3
        assert result["error"]["featureId"] == feature_id
        assert result["error"]["target"] == "base"
    assert results[0]["error"]["hint"] == "check flip, plane, and offset"
    assert results[1]["error"]["hint"] == "check target, position, diameter, and depth"
    assert results[-1]["revision"] == 3


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["cut", "hole"])
async def test_small_subtractions_from_large_body_are_not_lost_to_scale(tmp_path, kind):
    path = tmp_path / f"small-{kind}.coalesce.json"
    calls = [
        (
            "sketch",
            rectangle_sketch(
                request_id="base-sketch",
                revision=0,
                sketch_id="base_sketch",
                entity_id="base_profile",
                width=1000,
                height=1000,
            ),
        ),
        (
            "feature",
            extrude(
                request_id="base",
                revision=1,
                feature_id="base",
                sketch_id="base_sketch",
                profile_ids=["base_profile"],
                distance=1000,
            ),
        ),
    ]
    if kind == "cut":
        calls.extend(
            [
                (
                    "sketch",
                    rectangle_sketch(
                        request_id="tool-sketch",
                        revision=2,
                        sketch_id="tool_sketch",
                        entity_id="tool_profile",
                        width=0.05,
                        height=0.05,
                        offset=1000,
                    ),
                ),
                (
                    "feature",
                    cut(
                        request_id="tiny-cut",
                        revision=3,
                        feature_id="tiny_cut",
                        target="base",
                        tool="tool_sketch",
                        depth=0.1,
                    ),
                ),
            ]
        )
        feature_result = 3
        expected_revision = 4
    else:
        calls.append(
            (
                "feature",
                hole(
                    request_id="tiny-hole",
                    revision=2,
                    feature_id="tiny_hole",
                    target="base",
                    position=(0, 0),
                    diameter=0.1,
                    depth=0.1,
                ),
            )
        )
        feature_result = 2
        expected_revision = 3

    results = await run_server(path, calls)

    assert results[feature_result]["revision"] == expected_revision
    assert "error" not in results[feature_result]


@pytest.mark.anyio
async def test_tangent_cut_returns_structured_no_effect_without_commit(tmp_path):
    path = tmp_path / "tangent.coalesce.json"
    results = await run_server(
        path,
        [
            (
                "sketch",
                rectangle_sketch(
                    request_id="base-sketch",
                    revision=0,
                    sketch_id="base_sketch",
                    entity_id="base_profile",
                    width=20,
                    height=20,
                ),
            ),
            (
                "feature",
                extrude(
                    request_id="base",
                    revision=1,
                    feature_id="base",
                    sketch_id="base_sketch",
                    profile_ids=["base_profile"],
                    distance=10,
                ),
            ),
            (
                "sketch",
                rectangle_sketch(
                    request_id="tool-sketch",
                    revision=2,
                    sketch_id="tool_sketch",
                    entity_id="tool_profile",
                    width=2,
                    height=2,
                    offset=0,
                ),
            ),
            (
                "feature",
                cut(
                    request_id="tangent-cut",
                    revision=3,
                    feature_id="tangent_cut",
                    target="base",
                    tool="tool_sketch",
                    depth=1,
                ),
            ),
            ("query", {"select": {"kind": "revision"}}),
        ],
    )

    error = results[3]["error"]
    assert error["code"] == "CUT_NO_EFFECT"
    assert error["message"] == "cut tangent_cut removes no material from base"
    assert error["featureId"] == "tangent_cut"
    assert error["target"] == "base"
    assert error["hint"] == "check flip, plane, and offset"
    assert results[4]["revision"] == 3


@pytest.mark.anyio
async def test_real_stdio_xz_port_cut_has_exact_terminal_volume_and_restart_reference(tmp_path):
    path = tmp_path / "port.coalesce.json"
    calls = [
        ("sketch", rectangle_sketch(request_id="base-sketch", revision=0, sketch_id="base_sketch", entity_id="plate", width=60, height=40)),
        ("feature", extrude(request_id="base-extrude", revision=1, feature_id="base", sketch_id="base_sketch", profile_ids=["plate"], distance=10)),
        ("sketch", rectangle_sketch(request_id="port-sketch", revision=2, sketch_id="port_sketch", entity_id="port", width=10, height=4, plane="XZ", offset=-20, center=(0, 5))),
        ("feature", cut(request_id="port-cut", revision=3, feature_id="port_cut", target="base", tool="port_sketch")),
        ("query", {"select": {"kind": "measurements"}}),
    ]

    results = await run_server(path, calls)
    metrics = results[-1]
    assert metrics["volume"]["value"] == pytest.approx(24000 - 10 * 4 * 40)
    assert metrics["solidCount"] == 1
    assert [body["featureId"] for body in metrics["bodies"]] == ["port_cut"]
    assert results[-2]["revision"] == 4

    reference = {
        "select": {
            "kind": "reference",
            "reference": {
                "feature": "port_cut",
                "role": "side(left)",
                "cardinality": "one_or_more",
                "source": {"kind": "sketch_segment", "sketch": "port_sketch", "entity": "port", "segment": "left"},
            },
        }
    }
    restarted = await run_server(path, [("query", reference), ("query", {"select": {"kind": "measurements"}})])
    assert restarted[0]["status"] == "OK"
    assert restarted[0]["keys"]
    assert restarted[1] == metrics


@pytest.mark.anyio
async def test_real_stdio_four_m3_holes_have_exact_blind_and_through_geometry(tmp_path):
    path = tmp_path / "holes.coalesce.json"
    calls = [
        ("sketch", rectangle_sketch(request_id="plate-sketch", revision=0, sketch_id="plate_sketch", entity_id="plate", width=60, height=40)),
        ("feature", extrude(request_id="plate-extrude", revision=1, feature_id="plate", sketch_id="plate_sketch", profile_ids=["plate"], distance=10)),
    ]
    target = "plate"
    positions = [(-25, -15), (25, -15), (-25, 15), (25, 15)]
    for index, position in enumerate(positions):
        feature = f"m3_{index + 1}"
        calls.append(
            (
                "feature",
                hole(
                    request_id=feature,
                    revision=index + 2,
                    feature_id=feature,
                    target=target,
                    position=position,
                    depth=None if index < 2 else 4,
                ),
            )
        )
        target = feature
    calls.append(("query", {"select": {"kind": "measurements"}}))

    results = await run_server(path, calls)
    metrics = results[-1]
    removed = math.pi * 1.5**2 * (10 + 10 + 4 + 4)
    assert metrics["volume"]["value"] == pytest.approx(24000 - removed)
    assert metrics["solidCount"] == 1
    assert metrics["bodies"][0]["featureId"] == "m3_4"

    restarted = await run_server(
        path,
        [
            ("query", {"select": {"kind": "reference", "reference": {"feature": "m3_1", "role": "hole_wall"}}}),
            ("query", {"select": {"kind": "reference", "reference": {"feature": "m3_4", "role": "hole_floor"}}}),
        ],
    )
    assert [result["status"] for result in restarted] == ["OK", "OK"]


@pytest.mark.anyio
async def test_real_stdio_circle_boss_and_hollow_standoff(tmp_path):
    path = tmp_path / "circles.coalesce.json"
    results = await run_server(
        path,
        [
            ("sketch", circle_sketch(request_id="boss-sketch", revision=0, sketch_id="boss_sketch", circles=[("boss_profile", 5, (0, 0))])),
            ("feature", extrude(request_id="boss-extrude", revision=1, feature_id="boss", sketch_id="boss_sketch", profile_ids=["boss_profile"], distance=2)),
            ("sketch", circle_sketch(request_id="standoff-sketch", revision=2, sketch_id="standoff_sketch", circles=[("standoff", 4, (15, 0)), ("clearance", 1.5, (15, 0))])),
            ("feature", extrude(request_id="standoff-extrude", revision=3, feature_id="standoff", sketch_id="standoff_sketch", profile_ids=["standoff", "clearance"], distance=6)),
            ("query", {"select": {"kind": "measurements"}}),
        ],
    )

    assert results[0]["entitySegments"] == {"boss_profile": ["circle"]}
    assert results[2]["entitySegments"] == {
        "standoff": ["circle"],
        "clearance": ["circle"],
    }
    metrics = results[-1]
    expected = math.pi * 5**2 * 2 + math.pi * (4**2 - 1.5**2) * 6
    assert metrics["volume"]["value"] == pytest.approx(expected)
    assert metrics["solidCount"] == 2
    assert {body["featureId"] for body in metrics["bodies"]} == {"boss", "standoff"}


@pytest.mark.anyio
async def test_cut_and_hole_validation_replay_and_stale_calls_are_atomic(tmp_path):
    path = tmp_path / "atomic.coalesce.json"
    setup = await run_server(
        path,
        [
            ("sketch", rectangle_sketch(request_id="base-sketch", revision=0, sketch_id="base_sketch", entity_id="base_profile", width=20, height=20)),
            ("feature", extrude(request_id="base", revision=1, feature_id="base", sketch_id="base_sketch", profile_ids=["base_profile"], distance=10)),
            ("sketch", rectangle_sketch(request_id="tool", revision=2, sketch_id="tool", entity_id="tool_profile", width=2, height=2, offset=10)),
        ],
    )
    assert setup[-1]["revision"] == 3

    invalid_cut = cut(request_id="bad-cut", revision=3, feature_id="bad_cut", target="base", tool="tool", depth=0)
    invalid_hole = hole(request_id="bad-hole", revision=3, feature_id="bad_hole", target="base", position=(0, 0), diameter=-3)
    results = await run_server(
        path,
        [
            ("feature", invalid_cut),
            ("feature", invalid_hole),
            ("feature", cut(request_id="cut", revision=3, feature_id="cut", target="base", tool="tool", depth=2)),
            ("feature", cut(request_id="cut", revision=3, feature_id="cut", target="base", tool="tool", depth=2)),
            ("feature", hole(request_id="stale", revision=3, feature_id="stale", target="cut", position=(0, 0))),
            ("query", {"select": {"kind": "revision"}}),
        ],
    )
    assert [results[0]["error"]["code"], results[1]["error"]["code"]] == ["INVALID_PARAMETER", "INVALID_PARAMETER"]
    assert results[2] == results[3]
    assert results[4]["error"]["code"] == "REVISION_CONFLICT"
    assert results[5]["revision"] == 4

    service = DocumentService(path)
    before = path.read_bytes()
    non_finite = hole(request_id="nan-hole", revision=4, feature_id="nan", target="cut", position=(0, 0))
    non_finite["parameters"]["diameter"]["value"] = math.nan
    result = await call_server_tool(service, "feature", non_finite)
    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert service.revision == 4
    assert path.read_bytes() == before
    assert "jsonschema" not in json.dumps(result).lower()
