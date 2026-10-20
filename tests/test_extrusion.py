from math import isclose, pi

import pytest
from build123d import Face
from OCP.BRepCheck import BRepCheck_Analyzer

from coalesce.extrusion import extrude_sketch
from coalesce.graph import Circle, Extrude, GraphError, Rectangle, Sketch


def by_role(result, role):
    return [candidate for candidate in result.candidates if candidate.key.role == role]


def test_prism_history_assigns_caps_and_rectangle_segments():
    sketch = Sketch("sk", entities=[Rectangle("profile", 10, 6)])

    result = extrude_sketch(Extrude("base", sketch="sk", height=4), sketch)

    assert isclose(result.shape.volume, 240)
    assert {candidate.key.role for candidate in result.candidates} >= {
        "cap_start",
        "cap_end",
        "side(bottom)",
        "side(right)",
        "side(top)",
        "side(left)",
        "between(side(bottom),cap_end)",
    }
    assert isclose(by_role(result, "cap_end")[0].entity.center().Z, 4)


def test_rotated_off_origin_rectangle_keeps_constructed_left_segment():
    sketch = Sketch(
        "sk",
        entities=[Rectangle("profile", 10, 6)],
        position=(7, -3, 2),
        rotation=(0, 0, 30),
    )

    result = extrude_sketch(Extrude("base", sketch="sk", height=4), sketch)
    wall = by_role(result, "side(left)")[0].entity

    assert isclose(wall.center().X, 7 - 5 * 3**0.5 / 2)
    assert isclose(wall.center().Y, -5.5)


def test_constructed_segment_handles_do_not_depend_on_face_traversal(monkeypatch):
    original_edges = Face.edges

    def reversed_edges(face):
        return type(original_edges(face))(reversed(original_edges(face)))

    monkeypatch.setattr(Face, "edges", reversed_edges)
    sketch = Sketch("sk", entities=[Rectangle("profile", 10, 6)])

    result = extrude_sketch(Extrude("base", sketch="sk", height=4), sketch)

    assert isclose(by_role(result, "side(left)")[0].entity.center().X, -5)


def test_nested_profile_is_a_hole_in_one_solid():
    sketch = Sketch(
        "sk",
        entities=[Rectangle("box", 10, 10), Circle("hole", 2)],
    )

    result = extrude_sketch(Extrude("part", sketch="sk", height=3), sketch)

    assert len(result.shape.solids()) == 1
    assert isclose(result.shape.volume, (100 - pi * 4) * 3, rel_tol=1e-7)
    assert BRepCheck_Analyzer(result.shape.wrapped).IsValid()
    assert len(by_role(result, "side(hole)")) == 1


def test_disjoint_profiles_are_separate_solids_of_one_extrusion():
    sketch = Sketch(
        "sk",
        entities=[
            Rectangle("a", 2, 2),
            Rectangle("b", 2, 2, position=(4, 0)),
        ],
    )

    result = extrude_sketch(Extrude("parts", sketch="sk", height=3), sketch)

    assert len(result.shape.solids()) == 2
    assert isclose(result.shape.volume, 24)


def test_overlapping_non_nested_profiles_are_a_structured_error():
    sketch = Sketch(
        "sk",
        entities=[Rectangle("wide", 10, 4), Rectangle("tall", 4, 10)],
    )

    with pytest.raises(GraphError) as caught:
        extrude_sketch(Extrude("part", sketch="sk", height=3), sketch)

    assert caught.value.error["code"] == "overlapping_profiles"
    assert set(caught.value.error["entities"]) == {"wide", "tall"}


def test_internally_tangent_profile_is_a_structured_error():
    sketch = Sketch(
        "sk",
        entities=[Rectangle("box", 10, 10), Circle("hole", 5)],
    )

    with pytest.raises(GraphError) as caught:
        extrude_sketch(Extrude("part", sketch="sk", height=3), sketch)

    assert caught.value.error["code"] == "touching_profiles"
    assert set(caught.value.error["entities"]) == {"box", "hole"}


@pytest.mark.parametrize(
    "second",
    [
        Rectangle("corner", 2, 2, position=(2, 2)),
        Rectangle("edge", 2, 2, position=(2, 0)),
    ],
    ids=["external-tangency", "shared-edge"],
)
def test_touching_disjoint_profiles_are_a_structured_error(second):
    sketch = Sketch("sk", entities=[Rectangle("first", 2, 2), second])

    with pytest.raises(GraphError) as caught:
        extrude_sketch(Extrude("parts", sketch="sk", height=3), sketch)

    assert caught.value.error["code"] == "touching_profiles"
    assert set(caught.value.error["entities"]) == {"first", second.id}


def test_gap_above_tolerance_creates_valid_disjoint_solids():
    sketch = Sketch(
        "sk",
        entities=[
            Rectangle("a", 2, 2),
            Rectangle("b", 2, 2, position=(2.00001, 0)),
        ],
    )

    result = extrude_sketch(Extrude("parts", sketch="sk", height=3), sketch)

    assert len(result.shape.solids()) == 2
    assert all(BRepCheck_Analyzer(solid.wrapped).IsValid() for solid in result.shape.solids())


def test_disjoint_profiles_have_distinct_body_identity_and_scope():
    from coalesce.references import BodySource, Reference, Status, resolve

    sketch = Sketch(
        "sk",
        entities=[Rectangle("a", 2, 2), Rectangle("b", 2, 2, position=(4, 0))],
    )

    result = extrude_sketch(Extrude("parts", sketch="sk", height=3), sketch)
    bodies = {candidate.key.body for candidate in result.candidates}

    assert bodies == {BodySource("parts", "a"), BodySource("parts", "b")}
    selected = resolve(
        Reference("parts", "cap_end", body=BodySource("parts", "b")),
        {"parts": result.candidates},
    )
    assert selected.status is Status.OK
    assert len(selected.candidates) == 1
    assert isclose(selected.candidates[0].entity.center().X, 4)


def test_segment_sources_include_sketch_entity_and_segment_ids():
    from coalesce.references import Reference, SketchSegmentSource, Status, resolve

    sketch = Sketch(
        "sk",
        entities=[Rectangle("a", 2, 2), Rectangle("b", 2, 2, position=(4, 0))],
    )

    result = extrude_sketch(Extrude("parts", sketch="sk", height=3), sketch)
    left_sources = {candidate.key.source for candidate in by_role(result, "side(left)")}

    assert left_sources == {
        SketchSegmentSource("sk", "a", "left"),
        SketchSegmentSource("sk", "b", "left"),
    }
    selected = resolve(
        Reference(
            "parts",
            "side(left)",
            source=SketchSegmentSource("sk", "b", "left"),
        ),
        {"parts": result.candidates},
    )
    assert selected.status is Status.OK
    assert isclose(selected.candidates[0].entity.center().X, 3)
