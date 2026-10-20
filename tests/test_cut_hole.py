from math import isclose, pi

import pytest

from coalesce import (
    BodySource,
    Cardinality,
    Cut,
    Extrude,
    Graph,
    GraphError,
    Hole,
    HoleSurfaceSource,
    MaxAlong,
    Rectangle,
    Reference,
    Sketch,
    SketchSegmentSource,
    Status,
    regenerate,
)


def add_extrude(
    graph: Graph,
    feature: str,
    entity: str,
    width: float,
    height: float,
    length: float,
    *,
    position: tuple[float, float, float] = (0, 0, 0),
) -> None:
    sketch = f"{feature}_sketch"
    graph.add(Sketch(sketch, entities=[Rectangle(entity, width, height)], position=position))
    graph.add(Extrude(feature, sketch=sketch, height=length))


def base_graph() -> Graph:
    graph = Graph()
    add_extrude(graph, "base", "plate", 10, 10, 10)
    return graph


def add_tool_sketch(
    graph: Graph,
    sketch: str,
    entity: str,
    width: float,
    height: float,
    *,
    position: tuple[float, float, float],
) -> None:
    graph.add(Sketch(sketch, entities=[Rectangle(entity, width, height)], position=position))


def add_slot(graph: Graph, *, z: float = 10) -> None:
    add_tool_sketch(graph, "slot_tool", "slot", 2, 20, position=(0, 0, z))
    graph.add(Cut("slot", target="base", tool="slot_tool"))


def test_case_4_hole_diameter_change_preserves_wall_cap_edge() -> None:
    graph = base_graph()
    graph.add(Hole("mount", target="base", diameter=2, position=(2, 0)))
    edge_ref = Reference("mount", "between(hole_wall,cap_end)")

    first = regenerate(graph).resolve(edge_ref)
    assert first.status is Status.OK
    assert isclose(first.candidates[0].entity.radius, 1)

    graph.nodes["mount"].diameter = 4
    second = regenerate(graph).resolve(edge_ref)
    assert second.status is Status.OK
    assert isclose(second.candidates[0].entity.radius, 2)


def test_case_9_slot_split_is_explicit_and_preserves_total_top_area() -> None:
    graph = base_graph()
    add_slot(graph)

    result = regenerate(graph)
    one = result.resolve(Reference("base", "cap_end"))
    many = result.resolve(Reference("base", "cap_end", Cardinality.ONE_OR_MORE))

    assert one.status is Status.SPLIT
    assert one.error is not None
    assert len(one.error["candidates"]) == 2
    assert many.status is Status.SPLIT
    assert many.error is None
    assert len(many.candidates) == 2
    assert isclose(sum(candidate.entity.area for candidate in many.candidates), 80)
    wall = result.resolve(Reference("slot", "side(left)"))
    assert wall.status is Status.OK
    assert wall.candidates[0].key.source == SketchSegmentSource(
        "slot_tool", "slot", "left"
    )


def test_case_17_maximum_filter_reports_equal_height_body_tie() -> None:
    graph = Graph()
    graph.add(
        Sketch(
            "parts_sketch",
            entities=[
                Rectangle("left", 4, 4, position=(-4, 0)),
                Rectangle("right", 4, 4, position=(4, 0)),
            ],
        )
    )
    graph.add(Extrude("parts", sketch="parts_sketch", height=10))
    add_tool_sketch(
        graph, "notch_tool", "notch", 1, 1, position=(-4, 0, 4)
    )
    graph.add(Cut("notch", target="parts", tool="notch_tool", depth=2, flip=True))

    resolution = regenerate(graph).resolve(
        Reference(
            "parts",
            "cap_end",
            Cardinality.ONE_OR_MORE,
            filters=[MaxAlong((0, 0, 1))],
        )
    )

    assert resolution.status is Status.AMBIGUOUS
    assert resolution.error is not None
    assert len(resolution.error["candidates"]) == 2


def test_case_7_modified_top_face_survives_upstream_of_hole() -> None:
    graph = base_graph()
    add_tool_sketch(
        graph, "pocket_tool", "pocket", 2, 2, position=(-2, 0, 10)
    )
    graph.add(Cut("pocket", target="base", tool="pocket_tool", depth=2))
    graph.add(Hole("mount", target="pocket", diameter=2, position=(2, 0)))

    result = regenerate(graph)
    top = result.resolve(Reference("base", "cap_end"))
    hole_edge = result.resolve(Reference("mount", "between(hole_wall,cap_end)"))

    assert top.status is Status.OK
    assert isclose(top.candidates[0].entity.area, 96 - pi)
    assert result.resolve(Reference("pocket", "pocket_floor")).status is Status.OK
    assert hole_edge.status is Status.OK
    assert isclose(hole_edge.candidates[0].entity.radius, 1)


def test_case_10_top_face_returns_to_ok_when_slot_stops_short() -> None:
    graph = base_graph()
    add_slot(graph)
    ref = Reference("base", "cap_end")

    assert regenerate(graph).resolve(ref).status is Status.SPLIT

    graph.nodes["slot_tool"].position = (0, 0, 8)
    graph.nodes["slot"].depth = 1
    graph.nodes["slot"].flip = True
    restored = regenerate(graph).resolve(ref)
    assert restored.status is Status.OK
    assert isclose(restored.candidates[0].entity.area, 100)


def test_cut_preserves_unintersected_body_keys() -> None:
    graph = Graph()
    graph.add(
        Sketch(
            "parts_sketch",
            entities=[
                Rectangle("a", 4, 4, position=(-5, 0)),
                Rectangle("b", 4, 4, position=(5, 0)),
            ],
        )
    )
    graph.add(Extrude("parts", sketch="parts_sketch", height=5))
    before = regenerate(graph)
    body_b = BodySource("parts", "b")
    original = {
        candidate.key: candidate.descriptor
        for candidate in before.provenance["parts"]
        if candidate.key.body == body_b
    }
    add_tool_sketch(graph, "tool", "cutter", 2, 2, position=(-5, 0, 5))
    graph.add(Cut("cut", target="parts", tool="tool"))

    result = regenerate(graph)
    updated = {
        candidate.key: candidate.descriptor
        for candidate in result.provenance["parts"]
        if candidate.key.body == body_b
    }

    assert updated.keys() == original.keys()
    for key, descriptor in original.items():
        assert updated[key]["center"] == descriptor["center"]
        assert updated[key].get("area", updated[key].get("length")) == descriptor.get(
            "area", descriptor.get("length")
        )


def test_cut_crossing_two_bodies_assigns_generated_faces_to_each_body() -> None:
    graph = Graph()
    graph.add(
        Sketch(
            "parts_sketch",
            entities=[
                Rectangle("left", 4, 4, position=(-3, 0)),
                Rectangle("right", 4, 4, position=(3, 0)),
            ],
        )
    )
    graph.add(Extrude("parts", sketch="parts_sketch", height=5))
    add_tool_sketch(graph, "tool", "slot", 10, 2, position=(0, 0, 5))
    graph.add(Cut("cut", target="parts", tool="tool"))

    result = regenerate(graph)
    walls = result.resolve(
        Reference("cut", "side(bottom)", Cardinality.ONE_OR_MORE)
    )

    assert walls.status is Status.OK
    assert len(walls.candidates) == 2
    assert {candidate.key.body for candidate in walls.candidates} == {
        BodySource("parts", "left"),
        BodySource("parts", "right"),
    }
    assert all(isclose(candidate.entity.area, 20) for candidate in walls.candidates)


def test_cut_splitting_one_body_keeps_generated_faces_on_original_body() -> None:
    graph = base_graph()
    add_slot(graph)

    result = regenerate(graph)
    walls = result.resolve(
        Reference("slot", "side(left)", Cardinality.ONE_OR_MORE)
    )

    assert len(result.shapes["slot"].solids()) == 2
    assert walls.status is Status.OK
    assert walls.candidates
    assert all(
        candidate.key.body == BodySource("base", "plate")
        for candidate in walls.candidates
    )


def test_cut_through_all_cuts_both_sides_of_sketch_plane() -> None:
    graph = base_graph()
    add_tool_sketch(graph, "tool", "slot", 2, 2, position=(0, 0, 5))
    graph.add(Cut("slot", target="base", tool="tool", depth=None))

    shape = regenerate(graph).shapes["slot"]
    bounds = shape.bounding_box()

    assert isclose(shape.volume, 960)
    assert isclose(bounds.min.Z, 0)
    assert isclose(bounds.max.Z, 10)


@pytest.mark.parametrize(
    ("z", "flip", "floor_z"),
    [(10, False, 7), (0, True, 3)],
)
def test_cut_finite_depth_uses_sketch_normal_direction(
    z: float, flip: bool, floor_z: float
) -> None:
    graph = base_graph()
    add_tool_sketch(graph, "tool", "pocket", 2, 2, position=(0, 0, z))
    graph.add(Cut("pocket", target="base", tool="tool", depth=3, flip=flip))

    result = regenerate(graph)
    shape = result.shapes["pocket"]
    bounds = shape.bounding_box()
    floor = result.resolve(Reference("pocket", "pocket_floor"))

    assert isclose(shape.volume, 988)
    assert isclose(bounds.min.Z, 0)
    assert isclose(bounds.max.Z, 10)
    assert floor.status is Status.OK
    assert isclose(floor.candidates[0].entity.center().Z, floor_z)


@pytest.mark.parametrize("depth", [0, -1])
def test_cut_rejects_non_positive_depth_with_structured_error(depth: float) -> None:
    graph = base_graph()
    add_tool_sketch(graph, "tool", "pocket", 2, 2, position=(0, 0, 10))
    graph.add(Cut("pocket", target="base", tool="tool", depth=depth))

    with pytest.raises(GraphError) as caught:
        regenerate(graph)

    assert caught.value.error == {
        "code": "invalid_cut_depth",
        "message": "cut pocket depth must be positive",
        "feature": "pocket",
        "depth": depth,
    }


def test_blind_hole_has_history_derived_floor() -> None:
    graph = base_graph()
    graph.add(Hole("blind", target="base", diameter=2, depth=3, position=(0, 0)))

    result = regenerate(graph)

    wall = result.resolve(Reference("blind", "hole_wall"))
    assert wall.status is Status.OK
    assert wall.candidates[0].key.source == HoleSurfaceSource("blind", "hole_wall")
    floor = result.resolve(Reference("blind", "hole_floor"))
    assert floor.status is Status.OK
    assert isclose(floor.candidates[0].entity.center().Z, 7)
