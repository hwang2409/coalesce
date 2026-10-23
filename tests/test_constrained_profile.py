from __future__ import annotations

import copy
import json
import math
import os
import shutil
from pathlib import Path

import pytest
from build123d import GeomType, import_step
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from coalesce.service import DocumentService


def bracket_model(size: float = 20.0) -> dict:
    return {
        "kind": "constrained",
        "points": [
            {"id": "origin", "x": 0, "y": 0},
            {"id": "tangent", "x": 0, "y": size},
            {"id": "end", "x": -size, "y": 0},
            {"id": "center", "x": -size, "y": size, "construction": True},
        ],
        "lines": [
            {"id": "side", "start": "origin", "end": "tangent"},
            {"id": "base", "start": "origin", "end": "end"},
        ],
        "arcs": [
            {
                "id": "corner",
                "center": "center",
                "start": "tangent",
                "end": "end",
                "radius": size,
                "start_angle": 0,
                "end_angle": 3 * math.pi / 2,
            }
        ],
        "dimensions": [
            {"id": "side-length", "kind": "distance", "entities": ["origin", "tangent"], "value": size},
            {"id": "corner-radius", "kind": "radius", "entities": ["corner"], "value": size},
        ],
        "constraints": [
            {"id": "fix-origin", "kind": "fix", "entities": ["origin"]},
            {"id": "base-horizontal", "kind": "horizontal", "entities": ["base"]},
            {"id": "equal-sides", "kind": "equal", "entities": ["side", "base"]},
            {"id": "side-tangent", "kind": "tangent", "entities": ["side", "corner", "tangent"]},
        ],
    }


def sketch_request(model: dict, *, revision: int = 0, request_id: str = "sketch") -> dict:
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": request_id,
        "sketchId": "bracket_profile",
        "plane": {"kind": "origin", "name": "XY"},
        "constrained": model,
    }


def extrude_request(*, revision: int = 1, request_id: str = "extrude") -> dict:
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": request_id,
        "featureId": "bracket",
        "kind": "extrude",
        "parameters": {
            "profile": {"sketchId": "bracket_profile", "profileIds": ["bracket_profile"]},
            "distance": {"value": 5, "unit": "mm"},
            "operation": "new_body",
            "direction": "normal",
        },
    }


def fillet_request(revision: int = 2) -> dict:
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": "fillet",
        "featureId": "finished_bracket",
        "kind": "fillet",
        "parameters": {
            "target": {"featureId": "bracket"},
            "edges": [
                {
                    "feature": "bracket",
                    "role": "between(side(base),cap_end)",
                    "cardinality": "one",
                }
            ],
            "radius": {"value": 1, "unit": "mm"},
        },
    }


def update_request(model: dict, revision: int = 3) -> dict:
    return {
        "action": "update",
        "expectedRevision": revision,
        "requestId": "resize",
        "sketchId": "bracket_profile",
        "set": {"constrained": model},
    }


def result_json(result):
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


async def run_server(path: Path, calls, *, env=None):
    server = StdioServerParameters(
        command=shutil.which("coalesce") or "coalesce",
        args=["--document", str(path)],
        env={**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})},
    )
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        return [result_json(await session.call_tool(name, arguments)) for name, arguments in calls]


def fixed_line_model(points: list[tuple[float, float]], edges: list[tuple[int, int]]) -> dict:
    return {
        "kind": "constrained",
        "points": [
            {"id": f"p{index}", "x": x, "y": y} for index, (x, y) in enumerate(points)
        ],
        "lines": [
            {"id": f"edge{index}", "start": f"p{start}", "end": f"p{end}"}
            for index, (start, end) in enumerate(edges)
        ],
        "constraints": [
            {"id": f"fix{index}", "kind": "fix", "entities": [f"p{index}"]}
            for index in range(len(points))
        ],
    }


def square_model(size: float, *, origin: tuple[float, float] = (0, 0)) -> dict:
    x, y = origin
    return fixed_line_model(
        [(x, y), (x + size, y), (x + size, y + size), (x, y + size)],
        [(0, 1), (1, 2), (2, 3), (3, 0)],
    )


def circles_model(*, nested: bool) -> dict:
    second_x = 0 if nested else 8
    return {
        "kind": "constrained",
        "points": [
            {"id": "c1", "x": 0, "y": 0},
            {"id": "c2", "x": second_x, "y": 0},
        ],
        "circles": [
            {"id": "outer", "center": "c1", "radius": 5},
            {"id": "inner" if nested else "other", "center": "c2", "radius": 2},
        ],
        "dimensions": [
            {"id": "r1", "kind": "radius", "entities": ["outer"], "value": 5},
            {"id": "r2", "kind": "radius", "entities": ["inner" if nested else "other"], "value": 2},
        ],
        "constraints": [
            {"id": "fix-c1", "kind": "fix", "entities": ["c1"]},
            {"id": "fix-c2", "kind": "fix", "entities": ["c2"]},
        ],
    }


@pytest.mark.anyio
async def test_documented_line_arc_bracket_extrudes_exactly_through_real_stdio(tmp_path):
    docs = Path("docs/sketch.md").read_text()
    example = json.loads(docs.split("<!-- bracket-stdio -->\n```json\n", 1)[1].split("\n```", 1)[0])
    path = tmp_path / "documented-bracket.coalesce.json"

    results = await run_server(path, [(call["tool"], call["arguments"]) for call in example])

    assert all(result["ok"] is True for result in results)
    assert results[0]["solve"]["status"] == "fully_constrained"
    expected_volume = (300 * math.pi + 400) * 5
    assert results[2]["volume"]["value"] == pytest.approx(expected_volume, rel=1e-6)
    service = DocumentService(path, warmup=False)
    service.query({"select": {"kind": "measurements"}})
    assert any(face.geom_type is GeomType.CYLINDER for face in service._regen.shapes["bracket"].faces())


@pytest.mark.anyio
async def test_dimension_update_regenerates_and_keeps_profile_face_reference(tmp_path):
    path = tmp_path / "updated-bracket.coalesce.json"
    results = await run_server(
        path,
        [
            ("sketch", sketch_request(bracket_model())),
            ("feature", extrude_request()),
            ("feature", fillet_request()),
            ("sketch", update_request(bracket_model(25))),
            ("query", {"select": {"kind": "measurements"}}),
        ],
    )

    assert results[2]["ok"] is True
    assert results[3]["ok"] is True and results[3]["revision"] == 4
    assert results[4]["parts"][0]["terminalFeatureId"] == "finished_bracket"
    service = DocumentService(path, warmup=False)
    service.query({"select": {"kind": "measurements"}})
    expected_volume = (3 * math.pi * 25**2 / 4 + 25**2) * 5
    assert service._regen.shapes["bracket"].volume == pytest.approx(expected_volume, rel=1e-6)
    corner = next(
        item.entity
        for item in service._regen.provenance["bracket"]
        if item.key.role == "side(corner)"
    )
    assert corner.geom_type is GeomType.CYLINDER
    assert corner.radius == pytest.approx(25)


@pytest.mark.anyio
async def test_reordered_entities_keep_reference_but_split_segment_is_rejected_atomically(tmp_path):
    path = tmp_path / "topology-reference.coalesce.json"
    reordered = bracket_model()
    reordered["lines"] = list(reversed(reordered["lines"]))
    split = bracket_model()
    split["points"].append({"id": "base-mid", "x": -10, "y": 0})
    split["lines"] = [
        {"id": "side", "start": "origin", "end": "tangent"},
        {"id": "base", "start": "origin", "end": "base-mid"},
        {"id": "base-tail", "start": "base-mid", "end": "end"},
    ]
    split["constraints"] = [
        constraint
        for constraint in split["constraints"]
        if constraint["id"] not in {"equal-sides", "base-horizontal"}
    ]
    split["constraints"].append(
        {"id": "fix-base-mid", "kind": "fix", "entities": ["base-mid"]}
    )
    results = await run_server(
        path,
        [
            ("sketch", sketch_request(bracket_model())),
            ("feature", extrude_request()),
            ("feature", fillet_request()),
            ("sketch", update_request(reordered)),
        ],
    )

    assert results[3]["ok"] is True
    assert results[3]["selectionChanges"] == []
    before_split = path.read_bytes()
    (rejected,) = await run_server(
        path,
        [("sketch", update_request(split, revision=4) | {"requestId": "split"})],
    )

    assert rejected["ok"] is False
    assert rejected["revision"] == 4
    assert rejected["error"]["code"] == "REFERENCE_FAILED"
    assert rejected["error"]["resolution"]["status"] == "EMPTY"
    assert path.read_bytes() == before_split
    service = DocumentService(path, warmup=False)
    measured = service.query({"select": {"kind": "measurements"}})
    assert measured["parts"][0]["terminalFeatureId"] == "finished_bracket"


@pytest.mark.anyio
async def test_reload_and_step_round_trip_keep_constrained_arc_cylindrical(tmp_path):
    path = tmp_path / "round-trip.coalesce.json"
    target = tmp_path / "bracket.step"
    created = await run_server(
        path,
        [
            ("sketch", sketch_request(bracket_model())),
            ("feature", extrude_request()),
            ("query", {"select": {"kind": "measurements"}}),
        ],
    )
    reloaded = await run_server(
        path,
        [
            ("query", {"select": {"kind": "measurements"}}),
            ("export", {"expectedRevision": 2, "path": str(target)}),
        ],
    )

    assert reloaded[0] == created[2]
    assert reloaded[1]["ok"] is True
    assert reloaded[1]["validation"]["reimport"]["parts"][0]["cylinders"][0]["radius"] == pytest.approx(20)
    imported = import_step(target)
    assert any(face.geom_type is GeomType.CYLINDER for face in imported.faces())


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("reason", "model"),
    [
        ("open", fixed_line_model([(0, 0), (4, 0), (4, 3)], [(0, 1), (1, 2)])),
        (
            "self_intersecting",
            fixed_line_model(
                [(0, 0), (4, 4), (0, 4), (4, 0)],
                [(0, 1), (1, 2), (2, 3), (3, 0)],
            ),
        ),
        ("nested", circles_model(nested=True)),
        ("multiple", circles_model(nested=False)),
        (
            "degenerate",
            {
                "kind": "constrained",
                "points": [{"id": "p0", "x": 0, "y": 0}, {"id": "p1", "x": 1, "y": 0}],
                "lines": [{"id": "edge0", "start": "p0", "end": "p1"}],
                "constraints": [
                    {"id": "fix", "kind": "fix", "entities": ["p0"]},
                    {"id": "same", "kind": "coincident", "entities": ["p0", "p1"]},
                ],
            },
        ),
    ],
)
async def test_unsupported_profiles_fail_extrude_atomically(tmp_path, reason, model):
    path = tmp_path / f"{reason}.coalesce.json"
    (created,) = await run_server(path, [("sketch", sketch_request(model))])
    saved = path.read_bytes()

    (rejected,) = await run_server(path, [("feature", extrude_request())])

    assert created["ok"] is True
    assert rejected["ok"] is False
    assert rejected["revision"] == 1
    assert rejected["error"]["code"] == "SKETCH_PROFILE_UNSUPPORTED"
    assert rejected["error"]["reason"] == reason
    assert path.read_bytes() == saved


@pytest.mark.anyio
async def test_tiny_and_large_coordinate_squares_match_primitive_volume(tmp_path):
    for name, model, expected_volume in (
        ("tiny", square_model(0.00001), 0.00001**2 * 5),
        ("large-coordinate", square_model(10, origin=(1_000_000_000, -1_000_000_000)), 10**2 * 5),
    ):
        path = tmp_path / f"{name}.coalesce.json"
        results = await run_server(
            path,
            [
                ("sketch", sketch_request(model)),
                ("feature", extrude_request()),
                ("query", {"select": {"kind": "measurements"}}),
            ],
        )

        assert all(result["ok"] is True for result in results)
        assert results[2]["volume"]["value"] == pytest.approx(expected_volume, rel=1e-6)


@pytest.mark.anyio
async def test_minor_arc_profile_has_exact_volume(tmp_path):
    radius = 6
    angle = math.pi / 3
    model = {
        "kind": "constrained",
        "points": [
            {"id": "center", "x": 0, "y": 0, "construction": True},
            {"id": "start", "x": radius, "y": 0},
            {"id": "end", "x": radius * math.cos(angle), "y": radius * math.sin(angle)},
        ],
        "lines": [{"id": "chord", "start": "end", "end": "start"}],
        "arcs": [
            {
                "id": "minor",
                "center": "center",
                "start": "start",
                "end": "end",
                "radius": radius,
                "start_angle": 0,
                "end_angle": angle,
            }
        ],
        "constraints": [],
    }
    path = tmp_path / "minor-arc.coalesce.json"

    results = await run_server(
        path,
        [
            ("sketch", sketch_request(model)),
            ("feature", extrude_request()),
            ("query", {"select": {"kind": "measurements"}}),
        ],
    )

    expected_area = radius**2 * (angle - math.sin(angle)) / 2
    assert all(result["ok"] is True for result in results)
    assert results[2]["volume"]["value"] == pytest.approx(expected_area * 5, rel=1e-6)


@pytest.mark.anyio
async def test_tangent_arc_loops_are_rejected_through_real_stdio(tmp_path):
    model = {
        "kind": "constrained",
        "points": [
            {"id": "touch", "x": 0, "y": 0},
            {"id": "left", "x": -2, "y": 0},
            {"id": "left-center", "x": -1, "y": 0, "construction": True},
            {"id": "right", "x": 2, "y": 0},
            {"id": "right-center", "x": 1, "y": 0, "construction": True},
        ],
        "arcs": [
            {"id": "left-top", "center": "left-center", "start": "touch", "end": "left", "radius": 1, "start_angle": 0, "end_angle": math.pi},
            {"id": "left-bottom", "center": "left-center", "start": "left", "end": "touch", "radius": 1, "start_angle": math.pi, "end_angle": 2 * math.pi},
            {"id": "right-top", "center": "right-center", "start": "touch", "end": "right", "radius": 1, "start_angle": math.pi, "end_angle": 2 * math.pi},
            {"id": "right-bottom", "center": "right-center", "start": "right", "end": "touch", "radius": 1, "start_angle": 0, "end_angle": math.pi},
        ],
        "constraints": [],
    }
    path = tmp_path / "tangent-arcs.coalesce.json"
    (created,) = await run_server(path, [("sketch", sketch_request(model))])
    saved = path.read_bytes()

    (rejected,) = await run_server(path, [("feature", extrude_request())])

    assert created["ok"] is True
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "SKETCH_PROFILE_UNSUPPORTED"
    assert path.read_bytes() == saved


@pytest.mark.anyio
async def test_single_solved_circle_extrudes_exactly_with_stable_segment_source(tmp_path):
    model = {
        "kind": "constrained",
        "points": [{"id": "center", "x": 2, "y": 3}],
        "circles": [{"id": "rim", "center": "center", "radius": 3}],
        "dimensions": [
            {"id": "radius", "kind": "radius", "entities": ["rim"], "value": 3}
        ],
        "constraints": [{"id": "fix-center", "kind": "fix", "entities": ["center"]}],
    }
    path = tmp_path / "circle.coalesce.json"

    results = await run_server(
        path,
        [
            ("sketch", sketch_request(model)),
            ("feature", extrude_request()),
            ("query", {"select": {"kind": "measurements"}}),
        ],
    )

    assert all(result["ok"] is True for result in results)
    assert results[2]["volume"]["value"] == pytest.approx(math.pi * 3**2 * 5)
    service = DocumentService(path, warmup=False)
    service.query({"select": {"kind": "measurements"}})
    side = next(
        item for item in service._regen.provenance["bracket"] if item.key.role == "side(rim)"
    )
    assert side.key.source.segment == "rim"
    assert side.entity.geom_type is GeomType.CYLINDER


@pytest.mark.anyio
async def test_cut_consumes_one_exact_constrained_circle(tmp_path):
    from test_mcp_cut_hole_circle import cut, extrude, rectangle_sketch

    circle = {
        "kind": "constrained",
        "points": [{"id": "center", "x": 0, "y": 0}],
        "circles": [{"id": "rim", "center": "center", "radius": 3}],
        "dimensions": [
            {"id": "radius", "kind": "radius", "entities": ["rim"], "value": 3}
        ],
        "constraints": [{"id": "fix-center", "kind": "fix", "entities": ["center"]}],
    }
    path = tmp_path / "constrained-cut.coalesce.json"
    tool = sketch_request(circle, revision=2, request_id="tool")

    results = await run_server(
        path,
        [
            (
                "sketch",
                rectangle_sketch(
                    request_id="base-profile",
                    revision=0,
                    sketch_id="base-profile",
                    entity_id="outline",
                    width=10,
                    height=8,
                ),
            ),
            (
                "feature",
                extrude(
                    request_id="base",
                    revision=1,
                    feature_id="base",
                    sketch_id="base-profile",
                    profile_ids=["outline"],
                    distance=5,
                ),
            ),
            ("sketch", tool),
            (
                "feature",
                cut(
                    request_id="cut",
                    revision=3,
                    feature_id="cut",
                    target="base",
                    tool="bracket_profile",
                ),
            ),
            ("query", {"select": {"kind": "measurements"}}),
        ],
    )

    assert all(result["ok"] is True for result in results)
    assert results[4]["volume"]["value"] == pytest.approx((10 * 8 - math.pi * 3**2) * 5)


@pytest.mark.anyio
async def test_base_install_rejects_constrained_workflow_but_primitive_extrude_works(tmp_path):
    blocker = tmp_path / "without-planegcs"
    blocker.mkdir()
    (blocker / "planegcs.py").write_text("raise ImportError('blocked optional dependency')\n")
    path = tmp_path / "base-install.coalesce.json"
    primitive = {
        "action": "create",
        "expectedRevision": 0,
        "requestId": "primitive",
        "sketchId": "primitive",
        "plane": {"kind": "origin", "name": "XY"},
        "entities": [
            {
                "id": "outline",
                "kind": "rectangle",
                "width": {"value": 10, "unit": "mm"},
                "height": {"value": 8, "unit": "mm"},
            }
        ],
    }
    primitive_extrude = copy.deepcopy(extrude_request(revision=1))
    primitive_extrude["parameters"]["profile"] = {
        "sketchId": "primitive",
        "profileIds": ["outline"],
    }

    constrained, created, extruded = await run_server(
        path,
        [
            ("sketch", sketch_request(bracket_model())),
            ("sketch", primitive),
            ("feature", primitive_extrude),
        ],
        env={"PYTHONPATH": str(blocker)},
    )

    assert constrained["error"]["code"] == "SKETCH_SOLVER_UNAVAILABLE"
    assert created["ok"] is True
    assert extruded["ok"] is True
