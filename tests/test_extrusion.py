from math import isclose

from coalesce.extrusion import extrude_sketch
from coalesce.graph import Circle, Extrude, Rectangle, Sketch


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


def test_every_sketch_entity_is_extruded():
    sketch = Sketch(
        "sk",
        entities=[
            Rectangle("box", 2, 2),
            Circle("round", 1),
        ],
    )

    result = extrude_sketch(Extrude("parts", sketch="sk", height=3), sketch)

    assert len(result.shape.solids()) == 2
    assert len(by_role(result, "cap_end")) == 2
    assert len(by_role(result, "side(round)")) == 1
