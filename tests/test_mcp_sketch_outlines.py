import ast
import math
import re
from pathlib import Path

import pytest
from build123d import GeomType, import_step
from test_mcp_cut_hole_circle import extrude
from test_mcp_vertical import run_server

from coalesce.service import DocumentService

DOCS = Path(__file__).parents[1] / "docs"


def outline_sketch(kind, points, *, revision=0, request_id="outline", closed=True):
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": request_id,
        "sketchId": "outline_sketch",
        "plane": {"kind": "origin", "name": "XY"},
        "entities": [{"id": "outline", "kind": kind, "points": points, "closed": closed}],
    }


def update_outline(kind, points, revision, request_id):
    return {
        "action": "update",
        "expectedRevision": revision,
        "requestId": request_id,
        "sketchId": "outline_sketch",
        "set": {"entities": [{"id": "outline", "kind": kind, "points": points, "closed": True}]},
    }


def fillet_segment(segment="seg-p0-p1", revision=2):
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": "fillet-segment",
        "featureId": "rounded",
        "kind": "fillet",
        "parameters": {
            "target": {"featureId": "body"},
            "edges": [
                {
                    "feature": "body",
                    "role": f"between(side({segment}),cap_end)",
                    "cardinality": "one",
                }
            ],
            "radius": {"value": 0.5, "unit": "mm"},
        },
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "points", "expected_volume", "segments"),
    [
        (
            "polyline",
            [[0, 0], [4, 0], [0, 3]],
            30.0,
            ["seg-p0-p1", "seg-p1-p2", "seg-p2-p0"],
        ),
        (
            "spline",
            [
                [10 * math.cos(2 * math.pi * index / 32), 10 * math.sin(2 * math.pi * index / 32)]
                for index in range(32)
            ],
            500 * math.pi,
            ["spline"],
        ),
    ],
)
async def test_closed_outline_extrude_volume(kind, points, expected_volume, segments, tmp_path):
    results = await run_server(
        tmp_path / f"{kind}.coalesce.json",
        [
            ("sketch", outline_sketch(kind, points)),
            (
                "feature",
                extrude(
                    request_id="extrude",
                    revision=1,
                    feature_id="body",
                    sketch_id="outline_sketch",
                    profile_ids=["outline"],
                    distance=5,
                ),
            ),
            ("query", {"select": {"kind": "measurements"}}),
        ],
    )

    assert results[0]["entitySegments"] == {"outline": segments}
    assert results[1]["ok"] is True
    assert results[2]["volume"]["value"] == pytest.approx(expected_volume, rel=5e-4)


@pytest.mark.anyio
async def test_outline_id_defaults_to_kind_and_position(tmp_path):
    payload = outline_sketch("polyline", [[0, 0], [4, 0], [0, 3]])
    del payload["entities"][0]["id"]

    results = await run_server(tmp_path / "generated-id.coalesce.json", [("sketch", payload)])

    assert results[0]["ok"] is True
    assert results[0]["entitySegments"] == {
        "polyline-1": ["seg-p0-p1", "seg-p1-p2", "seg-p2-p0"]
    }


@pytest.mark.anyio
async def test_spline_step_round_trip_keeps_bspline_surface(tmp_path):
    document = tmp_path / "spline.coalesce.json"
    target = tmp_path / "spline.step"
    points = [
        [10 * math.cos(2 * math.pi * index / 12), 10 * math.sin(2 * math.pi * index / 12)]
        for index in range(12)
    ]
    results = await run_server(
        document,
        [
            ("sketch", outline_sketch("spline", points)),
            (
                "feature",
                extrude(
                    request_id="extrude",
                    revision=1,
                    feature_id="body",
                    sketch_id="outline_sketch",
                    profile_ids=["outline"],
                    distance=5,
                ),
            ),
            ("export", {"expectedRevision": 2, "path": str(target)}),
        ],
    )

    assert results[-1]["ok"] is True
    solid = import_step(target)
    assert solid.is_valid
    assert any(face.geom_type is GeomType.BSPLINE for face in solid.faces())


@pytest.mark.parametrize(
    ("kind", "points", "closed", "code", "reason"),
    [
        ("polyline", [[0, 0], [2, 0], [2, 2]], False, "INVALID_PARAMETER", None),
        (
            "polyline",
            [[0, 0], [2, 0], [2, 0], [0, 2]],
            True,
            "SKETCH_PROFILE_UNSUPPORTED",
            "degenerate",
        ),
        (
            "polyline",
            [[0, 0], [2, 2], [0, 2], [2, 0]],
            True,
            "SKETCH_PROFILE_UNSUPPORTED",
            "self_intersecting",
        ),
        ("spline", [[0, 0], [2, 0], [4, 0]], True, "SKETCH_PROFILE_UNSUPPORTED", "degenerate"),
    ],
)
def test_outline_validation_is_atomic(kind, points, closed, code, reason, tmp_path, capsys):
    path = tmp_path / "atomic.coalesce.json"
    service = DocumentService(path)
    before = service.query({"select": {"kind": "revision"}})
    before_bytes = path.read_bytes() if path.exists() else None

    result = service.sketch(outline_sketch(kind, points, closed=closed))

    assert result["ok"] is False
    assert result["revision"] == before["revision"]
    assert result["error"]["code"] == code
    if reason is not None:
        assert result["error"]["reason"] == reason
    assert (path.read_bytes() if path.exists() else None) == before_bytes
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "points",
    [
        [
            [2.5678578322, 7.5556633803],
            [-4.4744705047, 6.7027488092],
            [-3.6635116046, 11.3781793690],
            [-11.4178075313, 4.4769722369],
        ],
        [
            [8.6150701539, 2.5463658245],
            [0.7447011837, 0.6392991655],
            [0.7391157214, 0.6376842937],
            [-2.8943135861, -2.8359960397],
            [-1.1485377415, -1.7054714911],
        ],
    ],
)
def test_exact_spline_validation_is_atomic(points, tmp_path, capsys):
    path = tmp_path / "invalid-spline.coalesce.json"
    service = DocumentService(path)
    before = service.query({"select": {"kind": "revision"}})
    before_bytes = path.read_bytes() if path.exists() else None

    result = service.sketch(outline_sketch("spline", points))

    assert result["ok"] is False
    assert result["revision"] == before["revision"]
    assert result["error"]["code"] == "SKETCH_PROFILE_UNSUPPORTED"
    assert result["error"]["reason"] == "self_intersecting"
    assert (path.read_bytes() if path.exists() else None) == before_bytes
    assert capsys.readouterr().err == ""


def test_outline_point_bounds_and_finite_coordinates_are_rejected(tmp_path):
    service = DocumentService(tmp_path / "bounds.coalesce.json", warmup=False)
    invalid_points = (
        [[0, 0], [1, 0]],
        [[0, 0], [1, 0], [math.inf, 1]],
        [[index, index % 2] for index in range(2001)],
    )
    for index, points in enumerate(invalid_points):
        result = service.sketch(outline_sketch("polyline", points, request_id=f"invalid-{index}"))
        assert result["error"]["code"] == "INVALID_PARAMETER"


@pytest.mark.anyio
async def test_polyline_reference_follows_explicit_point_ids(tmp_path):
    points = [
        {"id": "a", "x": -5, "y": -5},
        {"id": "b", "x": 5, "y": -5},
        {"id": "c", "x": 5, "y": 5},
        {"id": "d", "x": -5, "y": 5},
    ]
    rotated = [points[1], points[2], points[3], points[0]]
    changed_endpoint = [*rotated[:3], {"id": "replacement", "x": -5, "y": -5}]
    path = tmp_path / "references.coalesce.json"

    results = await run_server(
        path,
        [
            ("sketch", outline_sketch("polyline", points)),
            (
                "feature",
                extrude(
                    request_id="extrude",
                    revision=1,
                    feature_id="body",
                    sketch_id="outline_sketch",
                    profile_ids=["outline"],
                    distance=5,
                ),
            ),
            ("feature", fillet_segment("seg-a-b")),
            ("sketch", update_outline("polyline", rotated, 3, "rotate")),
            ("sketch", update_outline("polyline", changed_endpoint, 4, "replace-id")),
        ],
    )

    assert results[0]["entitySegments"] == {
        "outline": ["seg-a-b", "seg-b-c", "seg-c-d", "seg-d-a"]
    }
    assert results[2]["ok"] is True
    assert results[3]["ok"] is True and results[3]["revision"] == 4
    assert results[4]["ok"] is False and results[4]["revision"] == 4
    assert results[4]["error"]["code"] == "REFERENCE_FAILED"
    assert results[4]["error"]["resolution"]["status"] == "EMPTY"


@pytest.mark.anyio
async def test_spline_reference_stays_one_role_when_point_count_changes(tmp_path):
    points = [
        [10 * math.cos(2 * math.pi * index / 12), 10 * math.sin(2 * math.pi * index / 12)]
        for index in range(12)
    ]
    changed = [
        [11 * math.cos(2 * math.pi * index / 13), 10 * math.sin(2 * math.pi * index / 13)]
        for index in range(13)
    ]
    results = await run_server(
        tmp_path / "spline-reference.coalesce.json",
        [
            ("sketch", outline_sketch("spline", points)),
            (
                "feature",
                extrude(
                    request_id="extrude",
                    revision=1,
                    feature_id="body",
                    sketch_id="outline_sketch",
                    profile_ids=["outline"],
                    distance=5,
                ),
            ),
            ("feature", fillet_segment("spline")),
            ("sketch", update_outline("spline", changed, 3, "new-count")),
        ],
    )

    assert results[0]["entitySegments"] == {"outline": ["spline"]}
    assert results[2]["ok"] is True
    assert results[3]["ok"] is True


def _python_block(markdown):
    match = re.search(r"<!-- cycloid-python -->\s*```python\n(.*?)```", markdown, re.DOTALL)
    assert match
    return match.group(1)


def _calls_block(markdown, namespace):
    match = re.search(r"<!-- cycloid-stdio -->\s*```python\n(.*?)```", markdown, re.DOTALL)
    assert match
    exec(  # noqa: S102 - execute the documented example verbatim
        compile(ast.parse(match.group(1)), "cycloidal-disc.md", "exec"), namespace
    )
    return namespace["calls"]


@pytest.mark.anyio
async def test_cycloidal_disc_documented_example_end_to_end(tmp_path):
    markdown = (DOCS / "examples/cycloidal-disc.md").read_text()
    namespace = {}
    exec(  # noqa: S102 - execute the documented example verbatim
        compile(ast.parse(_python_block(markdown)), "cycloidal-disc.md", "exec"), namespace
    )
    calls = _calls_block(markdown, namespace)
    target = tmp_path / "cycloidal-disc.step"
    calls[-1][1]["path"] = str(target)

    results = await run_server(tmp_path / "cycloidal-disc.coalesce.json", calls)

    assert all(result["ok"] is True for result in results)
    solid = import_step(target)
    assert solid.is_valid
    assert len(solid.solids()) == 1
    assert any(face.geom_type is GeomType.BSPLINE for face in solid.faces())
    spline = max(
        (edge for edge in solid.edges() if edge.geom_type is GeomType.BSPLINE),
        key=lambda edge: edge.length,
    )
    samples = [spline.position_at(index / 2000) for index in range(2000)]
    radii = [math.hypot(point.X, point.Y) for point in samples]
    maxima = sum(
        radii[index] > radii[index - 1] and radii[index] > radii[(index + 1) % len(radii)]
        for index in range(len(radii))
    )
    assert maxima == 7
