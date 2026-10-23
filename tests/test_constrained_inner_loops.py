import math

import pytest
from build123d import import_step
from test_constrained_profile import extrude_request, run_server, sketch_request

from coalesce.references import SketchSegmentSource
from coalesce.service import DocumentService


def _fixed_square(prefix: str, size: float, origin: tuple[float, float]) -> tuple[list[dict], list[dict], list[dict]]:
    x, y = origin
    points = [
        {"id": f"{prefix}-p{index}", "x": px, "y": py}
        for index, (px, py) in enumerate(
            ((x, y), (x + size, y), (x + size, y + size), (x, y + size))
        )
    ]
    lines = [
        {
            "id": f"{prefix}-edge{index}",
            "start": f"{prefix}-p{index}",
            "end": f"{prefix}-p{(index + 1) % 4}",
        }
        for index in range(4)
    ]
    constraints = [
        {"id": f"fix-{prefix}-p{index}", "kind": "fix", "entities": [f"{prefix}-p{index}"]}
        for index in range(4)
    ]
    return points, lines, constraints


def _line_loops_model() -> dict:
    outer = _fixed_square("outer", 10, (-5, -5))
    inner = _fixed_square("inner", 4, (-2, -2))
    return {
        "kind": "constrained",
        "points": inner[0] + outer[0],
        "lines": inner[1] + outer[1],
        "constraints": inner[2] + outer[2],
    }


def _rectangle_circle_model() -> dict:
    outer = _fixed_square("outer", 10, (-5, -5))
    return {
        "kind": "constrained",
        "points": outer[0] + [{"id": "hole-center", "x": 0, "y": 0}],
        "lines": outer[1],
        "circles": [{"id": "hole", "center": "hole-center", "radius": 2}],
        "constraints": outer[2]
        + [{"id": "fix-hole-center", "kind": "fix", "entities": ["hole-center"]}],
    }


def _circles_model(circles: list[tuple[str, float, float]]) -> dict:
    return {
        "kind": "constrained",
        "points": [
            {"id": f"{name}-center", "x": x, "y": 0}
            for name, x, _radius in circles
        ],
        "circles": [
            {"id": name, "center": f"{name}-center", "radius": radius}
            for name, _x, radius in circles
        ],
        "constraints": [
            {"id": f"fix-{name}", "kind": "fix", "entities": [f"{name}-center"]}
            for name, _x, _radius in circles
        ],
    }


def _fillet_both_loops() -> dict:
    return {
        "action": "create",
        "expectedRevision": 2,
        "requestId": "fillet-both-loops",
        "featureId": "finished_bracket",
        "kind": "fillet",
        "parameters": {
            "target": {"featureId": "bracket"},
            "edges": [
                {
                    "feature": "bracket",
                    "role": "between(side(outer-edge0),cap_end)",
                    "cardinality": "one",
                },
                {
                    "feature": "bracket",
                    "role": "between(side(hole),cap_end)",
                    "cardinality": "one",
                },
            ],
            "radius": {"value": 0.25, "unit": "mm"},
        },
    }


@pytest.mark.anyio
async def test_rectangle_and_circle_hole_extrude_with_typed_segment_sources_after_fillet(tmp_path):
    path = tmp_path / "rectangle-circle.coalesce.json"

    results = await run_server(
        path,
        [
            ("sketch", sketch_request(_rectangle_circle_model())),
            ("feature", extrude_request()),
            ("query", {"select": {"kind": "measurements"}}),
            ("feature", _fillet_both_loops()),
            ("query", {"select": {"kind": "measurements"}}),
        ],
    )

    assert all(result["ok"] is True for result in results)
    assert results[2]["volume"]["value"] == pytest.approx((100 - 4 * math.pi) * 5)
    assert results[4]["parts"][0]["terminalFeatureId"] == "finished_bracket"
    service = DocumentService(path, warmup=False)
    service.query({"select": {"kind": "measurements"}})
    sources = {
        item.key.role: item.key.source
        for item in service._regen.provenance["bracket"]
        if item.key.role in {"side(outer-edge0)", "side(hole)"}
    }
    assert set(sources) == {"side(outer-edge0)", "side(hole)"}
    assert all(isinstance(source, SketchSegmentSource) for source in sources.values())
    assert sources["side(outer-edge0)"].topology == ("outer-p0", "outer-p1")
    assert sources["side(hole)"].topology == ("hole-center",)


@pytest.mark.anyio
async def test_two_closed_line_loops_keep_hole_through_step_round_trip(tmp_path):
    path = tmp_path / "line-loops.coalesce.json"
    target = tmp_path / "line-loops.step"

    results = await run_server(
        path,
        [
            ("sketch", sketch_request(_line_loops_model())),
            ("feature", extrude_request()),
            ("query", {"select": {"kind": "measurements"}}),
            ("export", {"expectedRevision": 2, "path": str(target)}),
        ],
    )

    assert all(result["ok"] is True for result in results)
    assert results[2]["volume"]["value"] == pytest.approx((100 - 16) * 5)
    imported = import_step(target)
    assert imported.volume == pytest.approx((100 - 16) * 5)
    assert len(imported.faces()) == 10


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("reason", "circles", "named_loops"),
    [
        ("touching", [("outer", 0, 5), ("hole", 3, 2)], ["hole", "outer"]),
        ("crossing", [("outer", 0, 5), ("crossing", 4, 2)], ["crossing", "outer"]),
        ("multiple_outer", [("left", -6, 2), ("right", 6, 2)], ["left", "right"]),
        (
            "island",
            [("outer", 0, 6), ("hole", 0, 4), ("island", 0, 2)],
            ["hole", "island", "outer"],
        ),
    ],
)
async def test_invalid_loop_relationships_fail_atomically_with_named_loops(
    tmp_path, reason, circles, named_loops
):
    path = tmp_path / f"{reason}.coalesce.json"
    (created,) = await run_server(path, [("sketch", sketch_request(_circles_model(circles)))])
    saved = path.read_bytes()

    (rejected,) = await run_server(path, [("feature", extrude_request())])

    assert created["ok"] is True
    assert rejected["ok"] is False
    assert rejected["revision"] == 1
    assert rejected["error"]["code"] == "SKETCH_PROFILE_UNSUPPORTED"
    assert rejected["error"]["reason"] == reason
    assert rejected["error"]["loops"] == named_loops
    assert path.read_bytes() == saved
