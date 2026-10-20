import math

import pytest
from test_mcp_cut_hole_circle import cut, extrude, hole, rectangle_sketch
from test_mcp_vertical import run_server

from coalesce.document_store import Document, DocumentStore
from coalesce.graph import Extrude, Fillet, Graph, Rectangle, Sketch
from coalesce.references import Reference


def join_extrude(
    *, request_id, revision, feature_id, sketch_id, profile_ids, distance, target
):
    payload = extrude(
        request_id=request_id,
        revision=revision,
        feature_id=feature_id,
        sketch_id=sketch_id,
        profile_ids=profile_ids,
        distance=distance,
    )
    payload["parameters"]["operation"] = "join"
    payload["parameters"]["target"] = {"featureId": target}
    return payload


def patterned_hole(*, request_id, revision, feature_id, target, positions):
    payload = hole(
        request_id=request_id,
        revision=revision,
        feature_id=feature_id,
        target=target,
        position=(0, 0),
    )
    del payload["parameters"]["position"]
    payload["parameters"]["positions"] = positions
    return payload


@pytest.mark.anyio
async def test_real_stdio_join_wall_ring_is_one_exact_solid(tmp_path):
    path = tmp_path / "joined-wall.coalesce.json"
    results = await run_server(
        path,
        [
            (
                "sketch",
                rectangle_sketch(
                    request_id="plate-sketch",
                    revision=0,
                    sketch_id="plate_sketch",
                    entity_id="plate",
                    width=60,
                    height=40,
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
                    distance=3,
                ),
            ),
            (
                "sketch",
                {
                    "action": "create",
                    "expectedRevision": 2,
                    "requestId": "wall-sketch",
                    "sketchId": "wall_sketch",
                    "plane": {
                        "kind": "origin",
                        "name": "XY",
                        "offset": {"value": 3, "unit": "mm"},
                    },
                    "entities": [
                        {
                            "id": "outer",
                            "kind": "rectangle",
                            "width": {"value": 60, "unit": "mm"},
                            "height": {"value": 40, "unit": "mm"},
                        },
                        {
                            "id": "inner",
                            "kind": "rectangle",
                            "width": {"value": 54, "unit": "mm"},
                            "height": {"value": 34, "unit": "mm"},
                        },
                    ],
                },
            ),
            (
                "feature",
                join_extrude(
                    request_id="wall",
                    revision=3,
                    feature_id="wall",
                    sketch_id="wall_sketch",
                    profile_ids=["outer", "inner"],
                    distance=4,
                    target="plate",
                ),
            ),
            ("query", {"select": {"kind": "measurements"}}),
            ("query", {"select": {"kind": "feature", "id": "wall"}}),
        ],
    )

    assert results[3]["revision"] == 4
    assert results[3]["direction"] == [0, 0, 1]
    assert results[3]["toolBBox"]["min"] == {"x": -30, "y": -20, "z": 3, "unit": "mm"}
    assert results[3]["toolBBox"]["max"] == {"x": 30, "y": 20, "z": 7, "unit": "mm"}
    metrics = results[4]
    assert metrics["solidCount"] == 1
    assert metrics["volume"]["value"] == pytest.approx(7200 + (60 * 40 - 54 * 34) * 4)
    assert metrics["bbox"]["min"] == {"x": -30, "y": -20, "z": 0, "unit": "mm"}
    assert metrics["bbox"]["max"] == {"x": 30, "y": 20, "z": 7, "unit": "mm"}
    assert results[5]["feature"]["operation"] == "join"
    assert results[5]["feature"]["target"] == "plate"
    assert results[5]["direction"] == results[3]["direction"]
    assert results[5]["toolBBox"] == results[3]["toolBBox"]


@pytest.mark.anyio
async def test_real_stdio_detached_join_is_atomic_and_names_entity(tmp_path):
    path = tmp_path / "detached.coalesce.json"
    setup = await run_server(
        path,
        [
            (
                "sketch",
                rectangle_sketch(
                    request_id="plate-sketch",
                    revision=0,
                    sketch_id="plate_sketch",
                    entity_id="plate",
                    width=60,
                    height=40,
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
                    distance=3,
                ),
            ),
            (
                "sketch",
                rectangle_sketch(
                    request_id="boss-sketch",
                    revision=2,
                    sketch_id="boss_sketch",
                    entity_id="detached_boss",
                    width=5,
                    height=5,
                    offset=10,
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
                join_extrude(
                    request_id="detached",
                    revision=3,
                    feature_id="detached",
                    sketch_id="boss_sketch",
                    profile_ids=["detached_boss"],
                    distance=2,
                    target="plate",
                ),
            ),
            ("query", {"select": {"kind": "revision"}}),
        ],
    )

    assert results[0]["error"]["code"] == "JOIN_NO_CONTACT"
    assert results[0]["error"]["entities"] == ["detached_boss"]
    assert results[0]["error"]["hint"] == "move or extend the listed profiles to contact the target"
    assert results[0]["revision"] == results[1]["revision"] == 3


@pytest.mark.anyio
async def test_real_stdio_four_named_holes_are_one_revision_and_survive_restart(tmp_path):
    path = tmp_path / "pattern.coalesce.json"
    positions = [
        {"name": "nw", "point": [-25, 15]},
        {"name": "ne", "point": [25, 15]},
        {"name": "sw", "point": [-25, -15]},
        {"name": "se", "point": [25, -15]},
    ]
    results = await run_server(
        path,
        [
            (
                "sketch",
                rectangle_sketch(
                    request_id="plate-sketch",
                    revision=0,
                    sketch_id="plate_sketch",
                    entity_id="plate",
                    width=60,
                    height=40,
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
                    distance=3,
                ),
            ),
            (
                "feature",
                patterned_hole(
                    request_id="mounts",
                    revision=2,
                    feature_id="mounts",
                    target="plate",
                    positions=positions,
                ),
            ),
            ("query", {"select": {"kind": "measurements"}}),
        ],
    )

    assert results[2]["revision"] == 3
    assert results[2]["positionNames"] == ["nw", "ne", "sw", "se"]
    assert results[3]["solidCount"] == 1
    assert results[3]["volume"]["value"] == pytest.approx(60 * 40 * 3 - 4 * math.pi * 1.5**2 * 3)

    references = [
        (
            "query",
            {
                "select": {
                    "kind": "reference",
                    "reference": {
                        "feature": "mounts",
                        "role": "hole_wall",
                        "source": {
                            "kind": "hole_surface",
                            "feature": "mounts",
                            "name": name,
                            "surface": "hole_wall",
                        },
                    },
                }
            },
        )
        for name in ("nw", "ne", "sw", "se")
    ]
    restarted = await run_server(
        path,
        [*references, ("query", {"select": {"kind": "feature", "id": "mounts"}})],
    )
    assert [result["status"] for result in restarted[:-1]] == ["OK"] * 4
    assert restarted[-1]["feature"]["positions"] == positions
    assert restarted[-1]["positionNames"] == ["nw", "ne", "sw", "se"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("flip", "offset", "direction", "minimum_y", "maximum_y"),
    [
        (False, 20, [0, 1, 0], -20, -17),
        (True, -20, [0, -1, 0], 17, 20),
    ],
)
async def test_xz_cut_reports_global_frame_direction_and_cutter_bbox(
    tmp_path, flip, offset, direction, minimum_y, maximum_y
):
    path = tmp_path / f"xz-{flip}.coalesce.json"
    results = await run_server(
        path,
        [
            (
                "sketch",
                rectangle_sketch(
                    request_id="base-sketch",
                    revision=0,
                    sketch_id="base_sketch",
                    entity_id="plate",
                    width=60,
                    height=40,
                ),
            ),
            (
                "feature",
                extrude(
                    request_id="base",
                    revision=1,
                    feature_id="base",
                    sketch_id="base_sketch",
                    profile_ids=["plate"],
                    distance=10,
                ),
            ),
            (
                "sketch",
                rectangle_sketch(
                    request_id="port-sketch",
                    revision=2,
                    sketch_id="port_sketch",
                    entity_id="port",
                    width=10,
                    height=4,
                    plane="XZ",
                    offset=offset,
                    center=(0, 5),
                ),
            ),
            (
                "feature",
                cut(
                    request_id="port",
                    revision=3,
                    feature_id="port",
                    target="base",
                    tool="port_sketch",
                    depth=3,
                    flip=flip,
                ),
            ),
            ("query", {"select": {"kind": "feature", "id": "port"}}),
        ],
    )

    frame = {
        "origin": [0, -offset, 0],
        "normal": [0, -1, 0],
        "xAxis": [1, 0, 0],
        "yAxis": [0, 0, 1],
    }
    assert results[2]["frame"] == frame
    assert results[3]["frame"] == frame
    assert results[3]["direction"] == direction
    assert results[3]["toolBBox"]["min"]["y"] == pytest.approx(minimum_y)
    assert results[3]["toolBBox"]["max"]["y"] == pytest.approx(maximum_y)
    assert results[4]["frame"] == frame
    assert results[4]["direction"] == direction
    assert results[4]["toolBBox"] == results[3]["toolBBox"]


@pytest.mark.anyio
async def test_hole_positions_validation_is_atomic(tmp_path):
    path = tmp_path / "invalid-pattern.coalesce.json"
    too_many = patterned_hole(
        request_id="many",
        revision=0,
        feature_id="many",
        target="plate",
        positions=[{"point": [index, 0]} for index in range(65)],
    )
    duplicate = patterned_hole(
        request_id="duplicate",
        revision=0,
        feature_id="duplicate",
        target="plate",
        positions=[
            {"name": "mount", "point": [0, 0]},
            {"name": "mount", "point": [1, 0]},
        ],
    )
    non_finite = patterned_hole(
        request_id="nan",
        revision=0,
        feature_id="nan",
        target="plate",
        positions=[{"name": "mount", "point": [math.nan, 0]}],
    )

    results = await run_server(
        path,
        [
            ("feature", too_many),
            ("feature", duplicate),
            ("feature", non_finite),
            ("query", {"select": {"kind": "revision"}}),
        ],
    )

    assert [result["error"]["code"] for result in results[:3]] == [
        "INVALID_PARAMETER",
        "INVALID_PARAMETER",
        "INVALID_PARAMETER",
    ]
    assert results[0]["error"]["operation"] == "feature"
    assert "unique" in results[1]["error"]["message"]
    assert "positions[0].point[0]" in results[2]["error"]["message"]
    assert results[-1]["revision"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "positions",
    [
        [
            {"name": "left", "point": [0, 0]},
            {"name": "right", "point": [2, 0]},
        ],
        [
            {"name": "first", "point": [0, 0]},
            {"name": "second", "point": [0, 0]},
        ],
    ],
)
async def test_intersecting_named_holes_are_atomic(tmp_path, positions):
    path = tmp_path / "intersecting-holes.coalesce.json"
    setup = await run_server(
        path,
        [
            (
                "sketch",
                rectangle_sketch(
                    request_id="plate-sketch",
                    revision=0,
                    sketch_id="plate_sketch",
                    entity_id="plate",
                    width=20,
                    height=20,
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
                    distance=3,
                ),
            ),
        ],
    )
    assert setup[-1]["revision"] == 2

    results = await run_server(
        path,
        [
            (
                "feature",
                patterned_hole(
                    request_id="holes",
                    revision=2,
                    feature_id="holes",
                    target="plate",
                    positions=positions,
                ),
            ),
            ("query", {"select": {"kind": "revision"}}),
        ],
    )

    assert results[0]["error"]["code"] == "HOLES_INTERSECT"
    assert results[0]["error"]["pairs"] == [
        [positions[0]["name"], positions[1]["name"]]
    ]
    assert results[0]["revision"] == results[1]["revision"] == 2


@pytest.mark.anyio
async def test_mixed_effect_named_holes_are_atomic_and_name_misses(tmp_path):
    path = tmp_path / "mixed-effect-holes.coalesce.json"
    setup = await run_server(
        path,
        [
            (
                "sketch",
                rectangle_sketch(
                    request_id="plate-sketch",
                    revision=0,
                    sketch_id="plate_sketch",
                    entity_id="plate",
                    width=20,
                    height=20,
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
                    distance=3,
                ),
            ),
        ],
    )
    assert setup[-1]["revision"] == 2

    positions = [
        {"name": "hit", "point": [0, 0]},
        {"name": "miss", "point": [20, 0]},
    ]
    results = await run_server(
        path,
        [
            (
                "feature",
                patterned_hole(
                    request_id="holes",
                    revision=2,
                    feature_id="holes",
                    target="plate",
                    positions=positions,
                ),
            ),
            ("query", {"select": {"kind": "revision"}}),
        ],
    )

    assert results[0]["error"]["code"] == "HOLE_NO_EFFECT"
    assert results[0]["error"]["names"] == ["miss"]
    assert results[0]["revision"] == results[1]["revision"] == 2


@pytest.mark.anyio
async def test_all_named_holes_miss_with_whole_feature_error(tmp_path):
    path = tmp_path / "all-miss-holes.coalesce.json"
    setup = await run_server(
        path,
        [
            (
                "sketch",
                rectangle_sketch(
                    request_id="plate-sketch",
                    revision=0,
                    sketch_id="plate_sketch",
                    entity_id="plate",
                    width=20,
                    height=20,
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
                    distance=3,
                ),
            ),
        ],
    )
    assert setup[-1]["revision"] == 2

    result = (
        await run_server(
            path,
            [
                (
                    "feature",
                    patterned_hole(
                        request_id="holes",
                        revision=2,
                        feature_id="holes",
                        target="plate",
                        positions=[
                            {"name": "far-left", "point": [20, 0]},
                            {"name": "far-right", "point": [30, 0]},
                        ],
                    ),
                )
            ],
        )
    )[0]

    assert result["error"]["code"] == "HOLE_NO_EFFECT"
    assert result["error"]["featureId"] == "holes"
    assert "names" not in result["error"]
    assert result["revision"] == 2


@pytest.mark.anyio
@pytest.mark.parametrize("flip", [False, True])
async def test_through_all_cut_reports_bidirectional_tool_normal(tmp_path, flip):
    path = tmp_path / f"through-{flip}.coalesce.json"
    results = await run_server(
        path,
        [
            (
                "sketch",
                rectangle_sketch(
                    request_id="base-sketch",
                    revision=0,
                    sketch_id="base_sketch",
                    entity_id="plate",
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
                    profile_ids=["plate"],
                    distance=10,
                ),
            ),
            (
                "sketch",
                rectangle_sketch(
                    request_id="tool-sketch",
                    revision=2,
                    sketch_id="tool_sketch",
                    entity_id="tool",
                    width=4,
                    height=4,
                    plane="XZ",
                    center=(0, 5),
                ),
            ),
            (
                "feature",
                cut(
                    request_id="through",
                    revision=3,
                    feature_id="through",
                    target="base",
                    tool="tool_sketch",
                    flip=flip,
                ),
            ),
        ],
    )

    assert results[-1]["direction"] == [0, -1, 0]


@pytest.mark.anyio
async def test_join_accepts_fillet_body_as_target(tmp_path):
    graph = Graph()
    graph.add(Sketch("base_sketch", entities=[Rectangle("plate", 20, 20)]))
    graph.add(Extrude("base", sketch="base_sketch", height=3))
    graph.add(
        Fillet(
            "rounded",
            target="base",
            edges=[Reference("base", "between(side(bottom),cap_end)")],
            radius=1,
        )
    )
    graph.add(
        Sketch(
            "boss_sketch",
            entities=[Rectangle("boss", 4, 4)],
            position=(0, 0, 3),
        )
    )
    path = tmp_path / "join-fillet.coalesce.json"
    DocumentStore(path).save(Document(graph, revision=4))

    results = await run_server(
        path,
        [
            (
                "feature",
                join_extrude(
                    request_id="boss",
                    revision=4,
                    feature_id="boss",
                    sketch_id="boss_sketch",
                    profile_ids=["boss"],
                    distance=2,
                    target="rounded",
                ),
            ),
            ("query", {"select": {"kind": "measurements"}}),
        ],
    )

    assert results[0]["revision"] == 5
    assert results[1]["solidCount"] == 1
    assert results[1]["volume"]["value"] > 20 * 20 * 3


@pytest.mark.anyio
async def test_join_rejects_existing_sketch_target_as_invalid_parameter(tmp_path):
    path = tmp_path / "join-sketch.coalesce.json"
    setup = await run_server(
        path,
        [
            (
                "sketch",
                rectangle_sketch(
                    request_id="base-sketch",
                    revision=0,
                    sketch_id="base_sketch",
                    entity_id="plate",
                    width=20,
                    height=20,
                ),
            ),
            (
                "sketch",
                rectangle_sketch(
                    request_id="boss-sketch",
                    revision=1,
                    sketch_id="boss_sketch",
                    entity_id="boss",
                    width=4,
                    height=4,
                ),
            ),
        ],
    )
    assert setup[-1]["revision"] == 2

    result = (
        await run_server(
            path,
            [
                (
                    "feature",
                    join_extrude(
                        request_id="boss",
                        revision=2,
                        feature_id="boss",
                        sketch_id="boss_sketch",
                        profile_ids=["boss"],
                        distance=2,
                        target="base_sketch",
                    ),
                )
            ],
        )
    )[0]

    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert result["revision"] == 2
