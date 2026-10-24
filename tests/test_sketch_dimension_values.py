import json
import math

import pytest
from jsonschema import ValidationError, validate
from mcp import types
from test_constrained_sketch import graph_sketch, run_server
from test_mcp_vertical import SKETCH, call_server_tool

from coalesce.constrained_sketch import ConstrainedSketch
from coalesce.graph import Sketch
from coalesce.server import create_server
from coalesce.service import (
    DocumentService,
    RequestError,
    SketchChanges,
    _apply_sketch_update,
    _parse_sketch_update,
    _updated_dimensions,
)
from coalesce.sketch_solver import (
    Circle,
    Dimension,
    DimensionKind,
    OPERAND_SIGNATURES,
    Point,
    SolvedCircle,
    SolveResult,
    SolveStatus,
)
from evals.golden.common import score_artifact
from evals.golden.constrained_common import extrude, plate_model, sketch_create
from evals.run import build_before_state


def fillet():
    return {
        "action": "create",
        "expectedRevision": 2,
        "requestId": "fillet-rim",
        "featureId": "finished_bracket",
        "kind": "fillet",
        "parameters": {
            "target": {"featureId": "plate"},
            "edges": [
                {
                    "feature": "plate",
                    "role": "between(side(outer-edge0),cap_end)",
                    "cardinality": "one",
                }
            ],
            "radius": {"value": 0.25, "unit": "mm"},
        },
    }


def dimension_update(revision=3, values=None, request_id="resize-opening"):
    return {
        "action": "update",
        "expectedRevision": revision,
        "requestId": request_id,
        "sketchId": "profile",
        "set": {"dimensionValues": values or {"opening-radius": 4}},
    }


@pytest.mark.anyio
async def test_dimension_update_over_real_stdio_preserves_graph_identity_and_exact_geometry(tmp_path):
    path = tmp_path / "dimension-update.coalesce.json"
    created = await run_server(
        path,
        [
            sketch_create(plate_model(3)),
            extrude(),
            ("feature", fillet()),
            ("query", {"select": {"kind": "graph"}}),
            ("query", {"select": {"kind": "measurements"}}),
            ("sketch", dimension_update()),
            ("query", {"select": {"kind": "graph"}}),
            ("query", {"select": {"kind": "measurements"}}),
        ],
    )

    assert created[5]["ok"] is True and created[5]["revision"] == 4
    before_sketch = graph_sketch(created[3])
    after_sketch = graph_sketch(created[6])
    assert [item["id"] for item in after_sketch["constrained"]["dimensions"]] == [
        item["id"] for item in before_sketch["constrained"]["dimensions"]
    ]
    assert [item["id"] for item in after_sketch["constrained"]["constraints"]] == [
        item["id"] for item in before_sketch["constrained"]["constraints"]
    ]
    for entities in ("points", "lines", "arcs", "circles"):
        assert [item["id"] for item in after_sketch["constrained"][entities]] == [
            item["id"] for item in before_sketch["constrained"][entities]
        ]
    assert after_sketch["constrained"]["dimensions"][0]["value"] == 4
    assert after_sketch["constrained"]["solved"]["entities"]["opening"]["radius"] == pytest.approx(4)
    assert [node["id"] for node in created[6]["graph"]["nodes"]] == [
        "profile",
        "plate",
        "finished_bracket",
    ]
    assert created[7]["parts"][0]["terminalFeatureId"] == "finished_bracket"
    assert created[7]["volume"]["value"] < created[4]["volume"]["value"]


@pytest.mark.anyio
async def test_dimension_update_survives_restart_with_identity(tmp_path):
    path = tmp_path / "restart.coalesce.json"
    await run_server(
        path,
        [sketch_create(plate_model(3)), extrude(), ("feature", fillet()), ("sketch", dimension_update())],
    )

    restarted = await run_server(
        path,
        [
            ("query", {"select": {"kind": "graph"}}),
            ("query", {"select": {"kind": "measurements"}}),
        ],
    )

    assert [node["id"] for node in restarted[0]["graph"]["nodes"]] == [
        "profile",
        "plate",
        "finished_bracket",
    ]
    assert graph_sketch(restarted[0])["constrained"]["dimensions"][0]["value"] == 4
    assert restarted[1]["parts"][0]["terminalFeatureId"] == "finished_bracket"


@pytest.mark.anyio
async def test_dimension_update_rejects_unknown_reference_and_non_positive_values_atomically(tmp_path):
    path = tmp_path / "invalid-values.coalesce.json"
    model = plate_model(3)
    model["dimensions"].append(
        {
            "id": "opening-diameter-reference",
            "kind": "diameter",
            "entities": ["opening"],
            "value": 6,
            "driving": False,
        }
    )
    created = (await run_server(path, [sketch_create(model)]))[0]
    saved = path.read_bytes()
    service = DocumentService(path, warmup=False)

    unknown = await call_server_tool(
        service, "sketch", dimension_update(1, {"missing": 4}, "unknown-dimension")
    )
    reference = await call_server_tool(
        service,
        "sketch",
        dimension_update(1, {"opening-diameter-reference": 8}, "reference-dimension"),
    )
    non_positive = await call_server_tool(
        service, "sketch", dimension_update(1, {"opening-radius": 0}, "zero-radius")
    )
    non_finite = await call_server_tool(
        service,
        "sketch",
        dimension_update(1, {"opening-radius": float("nan")}, "non-finite-radius"),
    )

    assert created["revision"] == 1
    assert unknown["error"]["code"] == "UNKNOWN_ID"
    assert unknown["error"]["validDrivingDimensionIds"] == ["opening-radius"]
    assert reference["error"]["code"] == "INVALID_PARAMETER"
    assert non_positive["error"]["code"] == "INVALID_PARAMETER"
    assert non_finite["error"]["code"] == "INVALID_PARAMETER"
    for rejected in (unknown, reference, non_positive, non_finite):
        assert rejected["revision"] == 1
    assert path.read_bytes() == saved


@pytest.mark.parametrize(
    "dimension_values",
    [
        {},
        {"opening-radius": True},
        {"opening-radius": False},
    ],
)
def test_dimension_value_parser_rejects_empty_objects_and_booleans(dimension_values):
    payload = dimension_update()
    payload["set"]["dimensionValues"] = dimension_values

    with pytest.raises(RequestError, match="dimensionValues"):
        _parse_sketch_update(payload)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "extra_keys",
    [
        ("position",),
        ("entities",),
        ("constrained",),
        ("position", "entities"),
        ("position", "constrained"),
        ("entities", "constrained"),
        ("position", "entities", "constrained"),
    ],
)
async def test_dimension_values_is_exclusive_with_every_other_update_shape(tmp_path, extra_keys):
    server = create_server(DocumentService(tmp_path / "schema.coalesce.json", warmup=False))
    listed = await server.request_handlers[types.ListToolsRequest](types.ListToolsRequest())
    schema = next(tool for tool in listed.root.tools if tool.name == "sketch").inputSchema
    values = {
        "position": [0, 0, 0],
        "entities": SKETCH["entities"],
        "constrained": plate_model(3),
    }
    payload = dimension_update()
    payload["set"].update({key: values[key] for key in extra_keys})

    with pytest.raises(ValidationError):
        validate(payload, schema)


def test_multi_dimension_update_replaces_all_values_before_one_solve():
    model = ConstrainedSketch(
        points=(Point("center", 0, 0),),
        circles=(Circle("opening", "center", 3),),
        dimensions=(
            Dimension("radius", DimensionKind.RADIUS, ("opening",), 3),
            Dimension("diameter", DimensionKind.DIAMETER, ("opening",), 6),
        ),
    )
    sketch = Sketch("profile", constrained=model)
    solved_values = []

    def solve_once(request):
        solved_values.append({dimension.id: dimension.value for dimension in request.dimensions})
        assert request.circles[0].radius == 4
        return SolveResult(SolveStatus.UNDER_CONSTRAINED)

    _apply_sketch_update(
        sketch,
        SketchChanges(None, None, dimension_values={"radius": 4, "diameter": 8}),
        solve_once,
    )

    assert solved_values == [{"radius": 4, "diameter": 8}]


@pytest.mark.parametrize("value", [-math.pi / 2, 0, 2 * math.pi, 100])
def test_angle_dimension_updates_use_unbounded_signed_radians(value):
    assert OPERAND_SIGNATURES[DimensionKind.ANGLE][0].value_unit == "radians"
    model = ConstrainedSketch(
        dimensions=(Dimension("angle", DimensionKind.ANGLE, ("first", "second"), 1),)
    )

    updated = _updated_dimensions(model, {"angle": value})

    assert updated.dimensions[0].value == value


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (SolveStatus.CONFLICTING, "SKETCH_CONFLICTING"),
        (SolveStatus.DID_NOT_CONVERGE, "SKETCH_DID_NOT_CONVERGE"),
    ],
)
async def test_dimension_update_solve_failure_is_atomic_with_diagnostics(
    tmp_path, monkeypatch, status, code
):
    path = tmp_path / f"{status.value}.coalesce.json"
    await run_server(path, [sketch_create(plate_model(3))])
    saved = path.read_bytes()
    service = DocumentService(path, warmup=False)

    def reject(model):
        result = SolveResult(
            status,
            conflicting_constraints=("opening-radius",) if status is SolveStatus.CONFLICTING else (),
            diagnostic_entities={"opening": SolvedCircle((20, 15), 4)},
        )
        model.solved = result
        return result

    monkeypatch.setattr(ConstrainedSketch, "solve", reject)
    rejected = await call_server_tool(service, "sketch", dimension_update(1))

    assert rejected["error"]["code"] == code
    assert rejected["error"]["diagnosticGeometry"]["accepted"] is False
    assert rejected["error"]["diagnostics"]["status"] == status.value
    assert rejected["revision"] == 1
    assert path.read_bytes() == saved


@pytest.mark.anyio
async def test_string_set_has_exact_actionable_error(tmp_path):
    service = DocumentService(tmp_path / "string-set.coalesce.json", warmup=False)
    result = await call_server_tool(
        service,
        "sketch",
        {
            "action": "update",
            "expectedRevision": 0,
            "requestId": "string-set",
            "sketchId": "profile",
            "set": '{"dimensionValues":{"opening-radius":4}}',
        },
    )

    assert result["error"]["message"] == (
        'set must be a JSON object, not a JSON-encoded string; send "set": '
        '{"constrained": {...}} or "set": {"dimensionValues": {...}} without surrounding '
        'quotes (field: set)'
    )


@pytest.mark.anyio
async def test_dimension_update_is_published_in_schema_with_staged_example(tmp_path):
    server = create_server(DocumentService(tmp_path / "schema.coalesce.json", warmup=False))
    listed = await server.request_handlers[types.ListToolsRequest](types.ListToolsRequest())
    snapshot = json.loads(listed.model_dump_json(by_alias=True, exclude_none=True))
    schema = next(tool for tool in snapshot["tools"] if tool["name"] == "sketch")["inputSchema"]
    payload = dimension_update(2)

    validate(payload, schema)
    serialized = json.dumps(schema)
    assert '"dimensionValues"' in serialized
    assert '"opening-radius": 4' in serialized
    sketch_tool = next(tool for tool in snapshot["tools"] if tool["name"] == "sketch")
    assert sketch_tool["description"] == (
        "Create/update/delete profiles. set.entities or set.constrained replaces the full "
        "definition; set.dimensionValues changes only the named driving dimensions. "
        "See coalesce://docs/sketch."
    )


def test_dimension_update_passes_staged_eval_checks(tmp_path):
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "steps").mkdir()
    document = tmp_path / "model.coalesce.json"
    facts = tmp_path / "before-state.json"
    build_before_state(
        "constrained-sketch-update-after-extrusion", document, tmp_path / "fixtures", facts
    )

    result = DocumentService(document, warmup=False).sketch(dimension_update(2))
    checks = score_artifact(
        tmp_path,
        "constrained-sketch-update-after-extrusion",
        document,
        result["revision"],
        facts,
    )

    assert result["ok"] is True
    assert {check["kind"] for check in checks if check["status"] == "pass"} >= {
        "runner_feature_edit",
        "document_state",
        "hole_positions",
    }
