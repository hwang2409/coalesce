from math import isclose, pi

from coalesce import (
    BodySource,
    Cardinality,
    Cut,
    Extrude,
    Graph,
    Hole,
    MaxAlong,
    HoleSurfaceSource,
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


def add_slot(graph: Graph, *, z: float = 8, depth: float = 4) -> None:
    add_extrude(graph, "slot_tool", "slot", 2, 20, depth, position=(0, 0, z))
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
        "slot_tool_sketch", "slot", "left"
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
    add_extrude(graph, "notch_tool", "notch", 1, 1, 2, position=(-4, 0, 4))
    graph.add(Cut("notch", target="parts", tool="notch_tool"))

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
    add_extrude(graph, "pocket_tool", "pocket", 2, 2, 4, position=(-2, 0, 8))
    graph.add(Cut("pocket", target="base", tool="pocket_tool"))
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

    graph.nodes["slot_tool"].height = 1
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
    ref = Reference("parts", "cap_end", body=body_b)
    original = before.resolve(ref)
    add_extrude(graph, "tool", "cutter", 2, 2, 4, position=(-5, 0, 3))
    graph.add(Cut("cut", target="parts", tool="tool"))

    updated = regenerate(graph).resolve(ref)

    assert original.status is updated.status is Status.OK
    assert updated.candidates[0].key == original.candidates[0].key
    assert updated.candidates[0].descriptor == original.candidates[0].descriptor


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
