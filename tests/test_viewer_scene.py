import itertools
import math
import struct

import pytest
from build123d import Align, Box, Cylinder, Pos

from coalesce.assembly import RigidTransform
from coalesce.viewer import SceneOccurrence, ScenePart, ViewerSceneStore
from coalesce.viewer.scene import REFERENCE_COLOR, _oklab

PLATE_ALIGN = (Align.CENTER, Align.CENTER, Align.MIN)


def plate(height: float = 6) -> Box:
    return Box(60, 40, height, align=PLATE_ALIGN)


def decode(store: ViewerSceneStore, part: dict):
    mesh = part["mesh"]
    data = store.mesh(mesh["id"])
    v, t, e = mesh["vertexCount"], mesh["triangleCount"], mesh["edgeVertexCount"]
    assert len(data) == 4 * (3 * v + 3 * t + 3 * e)
    floats = struct.unpack_from(f"<{3 * v}f", data, 0)
    tris = struct.unpack_from(f"<{3 * t}I", data, 12 * v)
    points = [floats[i : i + 3] for i in range(0, len(floats), 3)]
    return points, [tris[i : i + 3] for i in range(0, len(tris), 3)]


def signed_volume(points, triangles) -> float:
    total = 0.0
    for a, b, c in triangles:
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = points[a], points[b], points[c]
        total += ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)
    return total / 6


def test_scene_bbox_uses_exact_shape_bounds_for_camera_fit():
    store = ViewerSceneStore()
    shape = plate()
    store.publish(1, [ScenePart("plate", shape)])

    scene = store.scene(1)
    exact = shape.bounding_box()
    tol = ViewerSceneStore.TOLERANCE
    assert scene["bbox"]["min"] == pytest.approx(tuple(exact.min), abs=tol)
    assert scene["bbox"]["max"] == pytest.approx(tuple(exact.max), abs=tol)
    assert scene["bbox"]["max"] == pytest.approx((30, 20, 6), abs=tol)
    [part] = scene["parts"]
    assert part["size"] == pytest.approx([60, 40, 6], abs=1e-6)

    points, triangles = decode(store, part)
    # a closed, outward-wound mesh: its volume equals the exact solid volume.
    assert signed_volume(points, triangles) == pytest.approx(shape.volume, rel=1e-6)


@pytest.mark.parametrize(
    ("transform"),
    [
        RigidTransform((11, -5, 3), (0, 0, 1), 90),
        RigidTransform((11, -5, 3), (1, 1, 1), 37),
    ],
)
def test_off_origin_definition_mesh_and_payload_match_located_shape(transform):
    store = ViewerSceneStore()
    shape = Pos(7, -3, 2) * Box(8, 3, 5)
    store.publish(1, [SceneOccurrence("placed", ScenePart("part", shape), transform)])

    scene = store.scene(1)
    [part] = scene["parts"]
    exact = __import__("coalesce.assembly", fromlist=["locate_shape"]).locate_shape(
        shape, transform
    ).bounding_box()
    expected = {"min": list(exact.min), "max": list(exact.max)}
    for limit in ("min", "max"):
        assert scene["bbox"][limit] == pytest.approx(expected[limit], abs=store.TOLERANCE)
        assert part["bbox"][limit] == pytest.approx(expected[limit], abs=store.TOLERANCE)

    points, _ = decode(store, part)
    axis_length = math.sqrt(sum(value * value for value in transform.axis))
    ux, uy, uz = (value / axis_length for value in transform.axis)
    angle = math.radians(transform.angle_degrees)
    cosine, sine = math.cos(angle), math.sin(angle)
    world = []
    for x, y, z in points:
        dot = ux * x + uy * y + uz * z
        cross = (uy * z - uz * y, uz * x - ux * z, ux * y - uy * x)
        world.append(
            tuple(
                coordinate * cosine
                + cross[i] * sine
                + (ux, uy, uz)[i] * dot * (1 - cosine)
                + transform.translation[i]
                for i, coordinate in enumerate((x, y, z))
            )
        )
    rendered = {
        "min": [min(point[i] for point in world) for i in range(3)],
        "max": [max(point[i] for point in world) for i in range(3)],
    }
    for limit in ("min", "max"):
        assert rendered[limit] == pytest.approx(expected[limit], abs=store.TOLERANCE)


def test_occurrence_facts_use_position_and_readable_rotation_text():
    store = ViewerSceneStore()
    definition = ScenePart("part", Box(1, 1, 1))
    store.publish(
        1,
        [
            SceneOccurrence("none", definition, RigidTransform((-8, 0, 0), (0, 0, 1), 0)),
            SceneOccurrence("z", definition, RigidTransform((0, 0, 0), (0, 0, 1), 90)),
            SceneOccurrence(
                "skew",
                definition,
                RigidTransform((0, 0, 0), (1 / math.sqrt(3),) * 3, 37),
            ),
            SceneOccurrence("negzero", definition, RigidTransform((-0.0, 0.0, -0.0), (0, 0, 1), 0)),
            SceneOccurrence(
                "fraction", definition, RigidTransform((1.23456, 2.34567, 3.45678), (0, 0, 1), 12.34567)
            ),
        ],
    )

    facts = {
        row["name"]: {fact["label"]: fact["value"] for fact in row["facts"]}
        for row in store.scene(1)["parts"]
    }
    assert facts["none"]["Translation"] == "-8, 0, 0 mm"
    assert facts["none"]["Rotation"] == "none"
    assert facts["z"]["Rotation"] == "90° about Z"
    assert facts["skew"]["Rotation"] == "37° about (0.58, 0.58, 0.58)"
    assert facts["negzero"]["Translation"] == "0, 0, 0 mm"
    assert facts["fraction"]["Translation"] == "1.235, 2.346, 3.457 mm"
    assert facts["fraction"]["Rotation"] == "12.346° about Z"


def test_repeated_occurrences_share_one_mesh_and_use_world_bounds(monkeypatch):
    store = ViewerSceneStore()
    definition = ScenePart("shaft/body", Box(30, 4, 8), label="Shaft")
    calls = 0
    original = __import__("coalesce.viewer.scene", fromlist=["_tessellate"])._tessellate

    def counted(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr("coalesce.viewer.scene._tessellate", counted)
    occurrences = [
        SceneOccurrence(
            f"shaft-{index + 1}",
            definition,
            RigidTransform((index * 40.0, 0.0, 0.0), (0.0, 0.0, 1.0), 90.0 if index == 7 else 0.0),
        )
        for index in range(8)
    ]

    store.publish(1, occurrences)

    scene = store.scene(1)
    assert calls == 1
    assert len(scene["parts"]) == 8
    assert len({part["mesh"]["id"] for part in scene["parts"]}) == 1
    assert scene["bbox"]["min"] == pytest.approx([-15, -15, -4])
    assert scene["bbox"]["max"] == pytest.approx([282, 15, 4])
    rotated = scene["parts"][-1]
    assert rotated["name"] == rotated["occurrenceId"] == "shaft-8"
    assert rotated["partId"] == "shaft/body"
    assert rotated["size"] == pytest.approx([4, 30, 8])
    assert rotated["transform"]["rotation"]["angleDegrees"] == 90


def test_assembly_inspection_refreshes_the_current_scene():
    store = ViewerSceneStore()
    store.publish(1, [ScenePart("body", Box(1, 1, 1))])
    version = store.snapshot()[0]

    store.publish_assembly_inspection(1, [{"occurrenceIds": ["a", "b"]}])

    refreshed, revision, _ = store.snapshot()
    assert refreshed == version + 1
    assert revision == 1
    assert store.scene(1)["assemblyInspection"] == [{"occurrenceIds": ["a", "b"]}]


def test_curved_part_mesh_stays_within_tolerance_and_has_brep_edges():
    store = ViewerSceneStore()
    shape = Cylinder(10, 5)
    store.publish(1, [ScenePart("pin", shape, color="#7e9a8a")])

    [part] = store.scene(1)["parts"]
    points, triangles = decode(store, part)
    tol = ViewerSceneStore.TOLERANCE
    assert signed_volume(points, triangles) == pytest.approx(shape.volume, rel=0.01)
    radii = [(x * x + y * y) ** 0.5 for x, y, _ in points]
    assert max(radii) == pytest.approx(10, abs=tol)
    assert part["color"] == "#7e9a8a"
    # three B-rep edges (two circles, one seam) sampled as segment pairs.
    assert part["mesh"]["edgeVertexCount"] % 2 == 0
    assert part["mesh"]["edgeVertexCount"] > 2 * 3


def test_publish_keeps_current_and_previous_revision_only():
    store = ViewerSceneStore()
    for revision, height in [(1, 6), (2, 10), (3, 12)]:
        store.publish(revision, [ScenePart("plate", plate(height))])

    assert store.current_revision == 3
    assert store.scene(1) is None
    assert store.scene(2)["bbox"]["max"][2] == pytest.approx(10)
    assert store.scene(3)["bbox"]["max"][2] == pytest.approx(12)
    assert store.mesh(store.scene(2)["parts"][0]["mesh"]["id"]) is not None


def test_publish_rejects_stale_revision_and_bad_parts():
    store = ViewerSceneStore()
    store.publish(2, [ScenePart("plate", plate())])

    with pytest.raises(ValueError, match="revision"):
        store.publish(2, [ScenePart("plate", plate(10))])
    with pytest.raises(ValueError, match="duplicate"):
        store.publish(3, [ScenePart("a", plate()), ScenePart("a", plate())])
    with pytest.raises(ValueError, match="color"):
        store.publish(3, [ScenePart("a", plate(), color="red")])
    assert store.current_revision == 2


def test_failure_keeps_last_good_scene_and_next_publish_clears_it():
    store = ViewerSceneStore()
    store.publish(1, [ScenePart("plate", plate())])
    good = store.scene(1)

    store.publish_failure(
        ["edge_fillet"],
        {"code": "INVALID_PARAMETER", "message": "fillet radius 20 mm is too large"},
    )

    _, revision, failure = store.snapshot()
    assert revision == 1
    assert store.scene(1) == good
    assert failure == {
        "revision": 1,
        "featureIds": ["edge_fillet"],
        "error": {
            "code": "INVALID_PARAMETER",
            "message": "fillet radius 20 mm is too large",
        },
    }

    store.publish(2, [ScenePart("plate", plate(10))])
    assert store.snapshot()[2] is None


def test_hidden_part_is_still_published_with_its_visibility():
    store = ViewerSceneStore()
    store.publish(1, [ScenePart("plate", plate()), ScenePart("ref", plate(1), visible=False)])

    parts = {p["name"]: p for p in store.scene(1)["parts"]}
    assert parts["plate"]["visible"] is True
    assert parts["ref"]["visible"] is False
    assert parts["plate"]["color"] != parts["ref"]["color"]


def test_edge_lines_follow_the_face_mesh_and_small_holes_look_round():
    store = ViewerSceneStore()
    # a 1.5 mm drill through a plate, and a 5 mm standoff on it
    shape = plate(2) - Cylinder(0.75, 10) + Pos(15, 0, 4.5) * Cylinder(2.5, 5)
    store.publish(1, [ScenePart("board", shape)])

    [part] = store.scene(1)["parts"]
    points, _ = decode(store, part)
    mesh = part["mesh"]
    data = store.mesh(mesh["id"])
    lines = struct.unpack_from(f"<{3 * mesh['edgeVertexCount']}f", data, 12 * mesh["vertexCount"] + 12 * mesh["triangleCount"])
    ends = {tuple(round(c, 4) for c in lines[i : i + 3]) for i in range(0, len(lines), 3)}
    # every edge-line point is a face-mesh vertex: lines and faces meet exactly
    assert ends <= {tuple(round(c, 4) for c in p) for p in points}
    for radius, center in ((0.75, (0, 0)), (2.5, (15, 0))):
        on_circle = {
            (x, y) for x, y, _ in ends if abs(((x - center[0]) ** 2 + (y - center[1]) ** 2) ** 0.5 - radius) < 1e-3
        }
        # a drawn circle has at least 48 corners: round, not a polygon, at normal zoom
        assert len(on_circle) >= 48, (radius, len(on_circle))


def test_seven_manufactured_parts_get_seven_colours_apart_from_the_reference():
    store = ViewerSceneStore()
    parts = [ScenePart(f"p{i}/body", plate().moved(Pos(70 * i, 0, 0))) for i in range(7)]
    store.publish(1, [ScenePart("board/pcb", plate(1), role="reference"), *parts])

    colors = [p["color"] for p in store.scene(1)["parts"]]
    assert colors[0] == REFERENCE_COLOR
    assert len(set(colors)) == 8, colors
    reference = [int(REFERENCE_COLOR[i:i + 2], 16) for i in (1, 3, 5)]
    for color in colors[1:]:
        rgb = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
        assert sum(abs(x - y) for x, y in zip(rgb, reference, strict=True)) >= 120, color


def test_first_twelve_manufactured_colours_are_perceptually_apart_from_each_other_and_the_reference():
    store = ViewerSceneStore()
    parts = [ScenePart(f"p{i}/body", plate().moved(Pos(70 * i, 0, 0))) for i in range(12)]
    store.publish(1, parts)

    colors = [p["color"] for p in store.scene(1)["parts"]]
    assert [round(c, 3) for c in _oklab("#ffffff")] == [1, 0, 0]
    # 0.03 in OKLab is about three just-noticeable differences; the fixed palette's closest pair is 0.031
    for a, b in itertools.combinations([*colors, REFERENCE_COLOR], 2):
        assert math.dist(_oklab(a), _oklab(b)) >= 0.03, (a, b)


def test_split_shades_stay_apart_from_every_body_colour_and_from_each_other():
    """Six bodies, each split into 1..8 pieces: no shade reads as another body or a sibling piece."""
    from coalesce.references import BodySource
    from coalesce.viewer.scene import _part_colors

    bases = _part_colors([ScenePart(f"b{i}/body", None) for i in range(6)])
    for pieces in range(1, 9):
        parts = [
            ScenePart(f"b{i}/body#{k}", None, source_body=BodySource(f"b{i}", "body"))
            for i in range(6)
            for k in range(pieces)
        ]
        colors = _part_colors(parts)
        assert len(set(colors)) == len(colors), (pieces, colors)
        for i in range(6):
            family = colors[i * pieces : (i + 1) * pieces]
            assert family[0] == bases[i]  # the first piece keeps the body colour
            # 0.03 in OKLab is about three just-noticeable differences (_MIN_DISTANCE)
            for shade in family[1:]:
                for other in [*bases, REFERENCE_COLOR, *(s for s in family if s != shade)]:
                    assert math.dist(_oklab(shade), _oklab(other)) >= 0.03, (pieces, i, shade, other)
