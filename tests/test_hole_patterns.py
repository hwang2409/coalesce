import math

import pytest
from build123d import import_step
from test_mcp_cut_hole_circle import extrude, rectangle_sketch
from test_mcp_vertical import run_server

from coalesce.graph import CircularHolePattern, Hole, LinearHolePattern
from coalesce.hole import hole_positions


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
