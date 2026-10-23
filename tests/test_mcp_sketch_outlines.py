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


def fillet_segment(segment="seg-0", revision=2):
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
        ("polyline", [[0, 0], [4, 0], [0, 3]], 30.0, ["seg-0", "seg-1", "seg-2"]),
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
    assert results[0]["entitySegments"] == {"polyline-1": ["seg-0", "seg-1", "seg-2"]}


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
def test_outline_validation_is_atomic(kind, points, closed, code, reason, tmp_path):
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
@pytest.mark.parametrize(
    ("kind", "points", "changed", "inserted", "segment"),
    [
        (
            "polyline",
            [[-5, -5], [5, -5], [5, 5], [-5, 5]],
            [[-6, -5], [6, -5], [5, 5], [-5, 5]],
            [[-6, -5], [0, -6], [6, -5], [5, 5], [-5, 5]],
            "seg-0",
        ),
        (
            "spline",
            [
                [
                    10 * math.cos(2 * math.pi * index / 12),
                    10 * math.sin(2 * math.pi * index / 12),
                ]
                for index in range(12)
            ],
            [
                [
                    11 * math.cos(2 * math.pi * index / 12),
                    10 * math.sin(2 * math.pi * index / 12),
                ]
                for index in range(12)
            ],
            [
                [
                    10 * math.cos(2 * math.pi * index / 13),
                    10 * math.sin(2 * math.pi * index / 13),
                ]
                for index in range(13)
            ],
            "spline",
        ),
    ],
)
async def test_outline_reference_survives_same_count_and_is_stale_on_count_change(
    kind, points, changed, inserted, segment, tmp_path
):
    path = tmp_path / "references.coalesce.json"
    results = await run_server(
        path,
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
            ("feature", fillet_segment(segment)),
            ("sketch", update_outline(kind, changed, 3, "same-count")),
            ("sketch", update_outline(kind, inserted, 4, "new-count")),
        ],
    )

    assert results[2]["ok"] is True
    assert results[3]["ok"] is True and results[3]["revision"] == 4
    assert results[4]["ok"] is False and results[4]["revision"] == 4
    assert results[4]["error"]["code"] == "REFERENCE_FAILED"
    assert results[4]["error"]["resolution"]["status"] == "EMPTY"


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
    points = namespace["points"]
    radii = [math.hypot(x, y) for x, y in points]
    maxima = sum(
        radii[index] > radii[index - 1] and radii[index] > radii[(index + 1) % len(radii)]
        for index in range(len(radii))
    )
    calls = _calls_block(markdown, namespace)
    target = tmp_path / "cycloidal-disc.step"
    calls[-1][1]["path"] = str(target)

    results = await run_server(tmp_path / "cycloidal-disc.coalesce.json", calls)

    assert maxima == 7
    assert all(result["ok"] is True for result in results)
    solid = import_step(target)
    assert solid.is_valid
    assert len(solid.solids()) == 1
    assert any(face.geom_type is GeomType.BSPLINE for face in solid.faces())
