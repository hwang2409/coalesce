import struct

import pytest
from build123d import Align, Box, Cylinder

from coalesce.viewer import ScenePart, ViewerSceneStore

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


def test_scene_bbox_from_mesh_matches_exact_plate_bbox():
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
