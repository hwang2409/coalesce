import math
from dataclasses import replace

import pytest
from build123d import import_step
from test_mcp_cut_hole_circle import extrude, rectangle_sketch
from test_mcp_vertical import run_server

from coalesce import (
    Extrude,
    Fillet,
    Graph,
    GraphError,
    HoleSurfaceSource,
    Rectangle,
    Reference,
    Sketch,
    regenerate,
)
from coalesce.graph import Hole
from coalesce.service import _pattern_selection_summary


def circular_pattern(*, radius=12, count=6, prefix="bolt", start_angle=0):
    return {
        "kind": "circular",
        "center": [0, 0],
        "radius": {"value": radius, "unit": "mm"},
        "count": count,
        "startAngle": {"value": start_angle, "unit": "deg"},
        "namePrefix": prefix,
    }


def linear_pattern(*, pitch=8, count=4, prefix="row", direction=(3, 4)):
    return {
        "kind": "linear",
        "start": [-12, -8],
        "direction": list(direction),
        "pitch": {"value": pitch, "unit": "mm"},
        "count": count,
        "namePrefix": prefix,
    }


def pattern_hole(*, request_id, revision, feature_id, target, pattern, diameter=3):
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": request_id,
        "featureId": feature_id,
        "kind": "hole",
        "parameters": {
            "target": {"featureId": target},
            "diameter": {"value": diameter, "unit": "mm"},
            "pattern": pattern,
        },
    }


def pattern_update(*, request_id, revision, feature_id, kind, **changes):
    return {
        "action": "update",
        "expectedRevision": revision,
        "requestId": request_id,
        "featureId": feature_id,
        "kind": "hole",
        "set": {"pattern": {"kind": kind, **changes}},
    }


def member_fillet(*, revision, name):
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": "round-member",
        "featureId": "round",
        "kind": "fillet",
        "parameters": {
            "target": {"featureId": "holes"},
            "edges": [
                {
                    "feature": "holes",
                    "role": "between(hole_wall,cap_end)",
                    "cardinality": "one",
                    "source": {
                        "kind": "hole_surface",
                        "feature": "holes",
                        "name": name,
                        "surface": "hole_wall",
                    },
                }
            ],
            "radius": {"value": 0.25, "unit": "mm"},
        },
    }


def reference_query(name):
    return (
        "query",
        {
            "select": {
                "kind": "reference",
                "reference": {
                    "feature": "holes",
                    "role": "hole_wall",
                    "source": {
                        "kind": "hole_surface",
                        "feature": "holes",
                        "name": name,
                        "surface": "hole_wall",
                    },
                },
            }
        },
    )


def test_pattern_expansion_has_exact_deterministic_centers_and_names():
    from coalesce.graph import CircularHolePattern, LinearHolePattern
    from coalesce.hole import hole_positions

    circular = Hole(
        "holes",
        pattern=CircularHolePattern((2, -3), 12, 6, 30, "bolt"),
    )
    linear = Hole(
        "row",
        pattern=LinearHolePattern((-12, -8), (0.6, 0.8), 8, 4, "row"),
    )

    circular_positions = hole_positions(circular)
    assert [position.name for position in circular_positions] == [
        "bolt1",
        "bolt2",
        "bolt3",
        "bolt4",
        "bolt5",
        "bolt6",
    ]
    for index, position in enumerate(circular_positions):
        angle = math.radians(30 + index * 60)
        assert position.point == pytest.approx((2 + 12 * math.cos(angle), -3 + 12 * math.sin(angle)))
    assert [position.point for position in hole_positions(linear)] == pytest.approx(
        [(-12 + index * 4.8, -8 + index * 6.4) for index in range(4)]
    )


def patterned_graph(member: str) -> Graph:
    from coalesce.graph import CircularHolePattern

    graph = Graph()
    graph.add(Sketch("plate_sketch", entities=[Rectangle("plate", 50, 50)]))
    graph.add(Extrude("plate", sketch="plate_sketch", height=5))
    graph.add(
        Hole(
            "holes",
            target="plate",
            diameter=3,
            pattern=CircularHolePattern((0, 0), 12, 6, 0, "bolt"),
        )
    )
    graph.add(
        Fillet(
            "round",
            target="holes",
            radius=0.25,
            edges=[
                Reference(
                    "holes",
                    "between(hole_wall,cap_end)",
                    source=HoleSurfaceSource("holes", "hole_wall", member),
                )
            ],
        )
    )
    return graph


def test_member_reference_survives_radius_change_and_removed_member_fails_closed():
    surviving = patterned_graph("bolt2")
    regenerate(surviving)
    surviving.nodes["holes"].pattern = replace(
        surviving.nodes["holes"].pattern,
        radius=10,
    )
    assert len(regenerate(surviving).shapes["round"].solids()) == 1

    stale = patterned_graph("bolt6")
    regenerate(stale)
    stale.nodes["holes"].pattern = replace(stale.nodes["holes"].pattern, count=4)
    with pytest.raises(GraphError) as caught:
        regenerate(stale)
    assert caught.value.error["code"] == "REFERENCE_FAILED"
    assert caught.value.error["resolution"]["status"] == "EMPTY"
    assert caught.value.error["resolution"]["reference"]["source"]["name"] == "bolt6"


def plate_setup():
    return [
        (
            "sketch",
            rectangle_sketch(
                request_id="plate-sketch",
                revision=0,
                sketch_id="plate_sketch",
                entity_id="plate",
                width=50,
                height=50,
            ),
        ),
        (
            "feature",
            extrude(
                request_id="plate",
                revision=1,
                feature_id="plate",
                sketch_id="plate_sketch",
                profile_ids=["plate"],
                distance=5,
            ),
        ),
    ]


@pytest.mark.anyio
async def test_circular_pattern_updates_exact_intent_and_survives_restart_and_step(tmp_path):
    document = tmp_path / "circular.coalesce.json"
    step = tmp_path / "circular.step"
    results = await run_server(
        document,
        [
            *plate_setup(),
            (
                "feature",
                pattern_hole(
                    request_id="holes",
                    revision=2,
                    feature_id="holes",
                    target="plate",
                    pattern=circular_pattern(),
                ),
            ),
            *[reference_query(f"bolt{index}") for index in range(1, 7)],
            (
                "feature",
                pattern_update(
                    request_id="resize",
                    revision=3,
                    feature_id="holes",
                    kind="circular",
                    radius={"value": 10, "unit": "mm"},
                    count=4,
                ),
            ),
            ("export", {"expectedRevision": 4, "partIds": ["holes"], "path": str(step)}),
        ],
    )

    assert results[2]["positionNames"] == [f"bolt{index}" for index in range(1, 7)]
    expected = [
        (12 * math.cos(index * math.pi / 3), 12 * math.sin(index * math.pi / 3))
        for index in range(6)
    ]
    for result, _center in zip(results[3:9], expected, strict=True):
        assert result["status"] == "OK"
        assert result["candidates"][0]["type"] == "CYLINDER"
    assert results[9]["featureId"] == "holes"
    assert results[9]["positionNames"] == ["bolt1", "bolt2", "bolt3", "bolt4"]
    assert len(import_step(step).solids()) == 1

    restarted = await run_server(
        document,
        [
            ("query", {"select": {"kind": "feature", "id": "holes"}}),
            *[reference_query(f"bolt{index}") for index in range(1, 7)],
        ],
    )
    assert restarted[0]["feature"]["pattern"] == {
        "kind": "circular",
        "center": [0, 0],
        "radius": 10,
        "count": 4,
        "startAngle": 0,
        "namePrefix": "bolt",
    }
    assert [result["status"] for result in restarted[1:]] == ["OK"] * 4 + ["EMPTY"] * 2


@pytest.mark.anyio
async def test_count_update_reports_moved_and_removed_pattern_members(tmp_path):
    results = await run_server(
        tmp_path / "selection-changes.coalesce.json",
        [
            *plate_setup(),
            (
                "feature",
                pattern_hole(
                    request_id="holes",
                    revision=2,
                    feature_id="holes",
                    target="plate",
                    pattern=circular_pattern(),
                ),
            ),
            ("feature", member_fillet(revision=3, name="bolt2")),
            (
                "feature",
                pattern_update(
                    request_id="resize",
                    revision=4,
                    feature_id="holes",
                    kind="circular",
                    count=4,
                ),
            ),
        ],
    )

    update = results[4]
    assert update["selectionChanges"] == [
        {
            "featureId": "holes",
            "selector": "pattern",
            "holesMoved": [
                {
                    "name": "bolt2",
                    "oldPosition": pytest.approx([6.0, 10.392304845]),
                    "newPosition": pytest.approx([0.0, 12.0]),
                },
            ],
            "holesAdded": [],
            "holesRemoved": ["bolt5", "bolt6"],
        }
    ]


def test_pattern_change_summary_rounds_axis_aligned_circular_positions():
    positions = [
        [10 * math.cos(math.radians(angle)), 10 * math.sin(math.radians(angle))]
        for angle in (90, 180, 270)
    ]
    changes = [
        {
            "selector": "pattern",
            "holesAdded": [],
            "holesRemoved": [],
            "holesMoved": [
                {"name": "bolt2", "oldPosition": positions[0], "newPosition": positions[1]},
                {"name": "bolt3", "oldPosition": positions[1], "newPosition": positions[2]},
            ],
        }
    ]

    summary = _pattern_selection_summary(changes, "edited hole holes")

    assert summary == (
        "edited hole holes, bolt2 moved (0, 10) → (-10, 0), "
        "bolt3 moved (-10, 0) → (0, -10)"
    )
    assert len(summary) <= 160


def test_pattern_change_summary_is_bounded_with_long_member_names():
    name_prefix = "p" * 140
    moved = [
        {
            "name": f"{name_prefix}{index}",
            "oldPosition": [float(index), 0.0],
            "newPosition": [float(index), 1.0],
        }
        for index in range(1, 65)
    ]

    summary = _pattern_selection_summary(
        [
            {
                "selector": "pattern",
                "holesAdded": [],
                "holesRemoved": [],
                "holesMoved": moved,
            }
        ],
        "edited hole holes",
    )

    assert summary is not None
    assert len(summary) <= 160
    assert "+N more" not in summary
    assert "+64 more" in summary


def test_pattern_change_summary_with_overlong_feature_id_keeps_plain_summary():
    changes = [
        {
            "selector": "pattern",
            "holesAdded": [],
            "holesRemoved": [],
            "holesMoved": [
                {"name": "bolt2", "oldPosition": [6.0, 10.4], "newPosition": [0.0, 12.0]}
            ],
        }
    ]

    assert _pattern_selection_summary(changes, "edited hole " + "h" * 150) is None


@pytest.mark.anyio
async def test_linear_pattern_normalizes_direction_and_updates_pitch_and_count(tmp_path):
    document = tmp_path / "linear.coalesce.json"
    results = await run_server(
        document,
        [
            *plate_setup(),
            (
                "feature",
                pattern_hole(
                    request_id="holes",
                    revision=2,
                    feature_id="holes",
                    target="plate",
                    pattern=linear_pattern(),
                ),
            ),
            *[reference_query(f"row{index}") for index in range(1, 5)],
            (
                "feature",
                pattern_update(
                    request_id="resize",
                    revision=3,
                    feature_id="holes",
                    kind="linear",
                    pitch={"value": 5, "unit": "mm"},
                    count=3,
                ),
            ),
            ("query", {"select": {"kind": "feature", "id": "holes"}}),
        ],
    )

    expected = [(-12 + index * 8 * 0.6, -8 + index * 8 * 0.8) for index in range(4)]
    for result, _center in zip(results[3:7], expected, strict=True):
        assert result["status"] == "OK"
        assert result["candidates"][0]["type"] == "CYLINDER"
    assert results[7]["positionNames"] == ["row1", "row2", "row3"]
    assert results[8]["feature"]["pattern"] == {
        "kind": "linear",
        "start": [-12, -8],
        "direction": [0.6, 0.8],
        "pitch": 5,
        "count": 3,
        "namePrefix": "row",
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("pattern", "code", "detail"),
    [
        (circular_pattern(radius=0), "INVALID_PARAMETER", "pattern.radius"),
        (circular_pattern(radius=1, count=2), "HOLES_INTERSECT", ["bolt1", "bolt2"]),
        (circular_pattern(count=65), "INVALID_PARAMETER", "pattern.count"),
        (circular_pattern(radius=40, count=2), "HOLE_NO_EFFECT", ["bolt1", "bolt2"]),
        (linear_pattern(direction=(0, 0)), "INVALID_PARAMETER", "pattern.direction"),
        (linear_pattern(pitch=50, count=2), "HOLE_NO_EFFECT", ["row2"]),
        (
            {
                **linear_pattern(pitch=30, count=2),
                "start": [0, 0],
            },
            "INVALID_HOLE",
            ["row2"],
        ),
    ],
)
async def test_pattern_errors_are_atomic_and_name_members(tmp_path, pattern, code, detail):
    document = tmp_path / f"invalid-{code}.coalesce.json"
    setup = await run_server(document, plate_setup())
    assert setup[-1]["revision"] == 2
    before = document.read_bytes()

    result = (
        await run_server(
            document,
            [
                (
                    "feature",
                    pattern_hole(
                        request_id="holes",
                        revision=2,
                        feature_id="holes",
                        target="plate",
                        pattern=pattern,
                    ),
                )
            ],
        )
    )[0]

    assert result["error"]["code"] == code
    if isinstance(detail, list):
        assert detail == result["error"].get("names", result["error"].get("pairs", [[]])[0])
    else:
        assert result["error"]["field"] == detail
    assert result["revision"] == 2
    assert document.read_bytes() == before
