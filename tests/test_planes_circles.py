import json
import subprocess
import sys
from dataclasses import asdict
from math import isclose, pi

import pytest

from coalesce import (
    BodySource,
    Cardinality,
    Circle,
    Cut,
    Extrude,
    Graph,
    GraphError,
    Plane,
    Rectangle,
    Reference,
    Sketch,
    SketchSegmentSource,
    Status,
    regenerate,
)
from coalesce.serialization import loads


@pytest.mark.parametrize(
    ("plane", "expected_min", "expected_max"),
    [
        ("XY", (-2, -2, 3), (2, 2, 8)),
        ("XZ", (-2, -8, -2), (2, -3, 2)),
        ("YZ", (3, -2, -2), (8, 2, 2)),
    ],
)
def test_circle_extrudes_along_origin_plane_normal_with_exact_bounds(
    plane: str,
    expected_min: tuple[float, float, float],
    expected_max: tuple[float, float, float],
) -> None:
    graph = Graph()
    graph.add(
        Sketch(
            "circle_sketch",
            plane=Plane(plane, offset=3),
            entities=[Circle("circle", radius=2)],
        )
    )
    graph.add(Extrude("cylinder", sketch="circle_sketch", height=5))

    shape = regenerate(graph).shapes["cylinder"]
    bounds = shape.bounding_box()

    assert isclose(shape.volume, pi * 2**2 * 5, rel_tol=1e-9)
    assert tuple(bounds.min) == pytest.approx(expected_min)
    assert tuple(bounds.max) == pytest.approx(expected_max)


def test_circle_boss_is_a_separate_body_under_existing_new_body_rules() -> None:
    graph = Graph()
    graph.add(Sketch("plate_sketch", entities=[Rectangle("plate", 20, 20)]))
    graph.add(Extrude("plate", sketch="plate_sketch", height=2))
    graph.add(
        Sketch(
            "boss_sketch",
            plane=Plane("XY", offset=2),
            entities=[Circle("boss", radius=3)],
        )
    )
    graph.add(Extrude("boss", sketch="boss_sketch", height=4))

    result = regenerate(graph)

    assert set(result.shapes) == {"plate", "boss"}
    assert isclose(result.shapes["plate"].volume, 800)
    assert isclose(result.shapes["boss"].volume, pi * 3**2 * 4, rel_tol=1e-9)
    assert {
        candidate.key.body for candidate in result.provenance["boss"]
    } == {BodySource("boss", "boss")}


def test_xz_rectangle_cuts_through_box_wall_with_segment_roles() -> None:
    graph = Graph()
    graph.add(Sketch("box_sketch", entities=[Rectangle("box", 10, 10)]))
    graph.add(Extrude("box", sketch="box_sketch", height=10))
    graph.add(
        Sketch(
            "opening_sketch",
            plane=Plane("XZ", offset=-5),
            entities=[Rectangle("opening", 2, 4, position=(0, 5))],
        )
    )
    graph.add(Cut("opening", target="box", tool="opening_sketch"))

    result = regenerate(graph)

    assert isclose(result.shapes["opening"].volume, 920)
    for segment in ("bottom", "right", "top", "left"):
        wall = result.resolve(
            Reference(
                "opening",
                f"side({segment})",
                source=SketchSegmentSource("opening_sketch", "opening", segment),
                cardinality=Cardinality.ONE_OR_MORE,
            )
        )
        assert wall.status is Status.OK
        assert wall.candidates


def test_nested_circles_make_a_hollow_standoff() -> None:
    graph = Graph()
    graph.add(
        Sketch(
            "standoff_sketch",
            entities=[Circle("standoff", radius=4), Circle("hole", radius=2)],
        )
    )
    graph.add(Extrude("standoff", sketch="standoff_sketch", height=6))

    shape = regenerate(graph).shapes["standoff"]

    assert len(shape.solids()) == 1
    assert isclose(shape.volume, pi * (4**2 - 2**2) * 6, rel_tol=1e-9)


def test_circle_side_reference_survives_radius_change_without_seam_keys() -> None:
    graph = Graph()
    graph.add(Sketch("sketch", entities=[Circle("circle", radius=2)]))
    graph.add(Extrude("cylinder", sketch="sketch", height=5))
    reference = Reference(
        "cylinder",
        "side(circle)",
        source=SketchSegmentSource("sketch", "circle", "circle"),
    )

    first = regenerate(graph)
    graph.nodes["sketch"].entities[0].radius = 3
    second = regenerate(graph)

    assert first.resolve(reference).status is Status.OK
    assert second.resolve(reference).status is Status.OK
    assert isclose(first.resolve(reference).candidates[0].entity.radius, 2)
    assert isclose(second.resolve(reference).candidates[0].entity.radius, 3)
    assert not any(
        candidate.key.role == "between(side(circle),side(circle))"
        for candidate in second.provenance["cylinder"]
    )


def test_plane_offset_and_circle_round_trip_through_json() -> None:
    graph = Graph()
    graph.add(
        Sketch(
            "sketch",
            plane=Plane("YZ", offset=7.5),
            entities=[Circle("circle", radius=2.5, position=(1, -2))],
        )
    )
    graph.add(Extrude("cylinder", sketch="sketch", height=4))

    restored = loads(graph.to_json())

    assert restored.nodes["sketch"] == graph.nodes["sketch"]
    assert restored.to_json() == graph.to_json()


@pytest.mark.parametrize(
    ("sketch", "code"),
    [
        (Sketch("sketch", plane=Plane("ZX"), entities=[Circle("circle", 2)]), "unknown_plane"),
        (
            Sketch(
                "sketch",
                plane=Plane("XY", offset=float("inf")),
                entities=[Circle("circle", 2)],
            ),
            "non_finite_value",
        ),
        (Sketch("sketch", entities=[Circle("circle", 0)]), "invalid_circle_radius"),
        (Sketch("sketch", entities=[Circle("circle", float("nan"))]), "non_finite_value"),
        (
            Sketch("sketch", entities=[Circle("circle", 2, position=(float("inf"), 0))]),
            "non_finite_value",
        ),
    ],
)
def test_invalid_plane_and_circle_values_are_structured_errors(
    sketch: Sketch, code: str
) -> None:
    graph = Graph()
    graph.add(sketch)
    graph.add(Extrude("feature", sketch="sketch", height=1))

    with pytest.raises(GraphError) as caught:
        regenerate(graph)

    assert caught.value.error["code"] == code
    assert caught.value.error["sketch"] == "sketch"


def _circle_snapshot(graph: Graph) -> dict[str, object]:
    result = regenerate(graph)
    candidates = [
        {
            "key": json.loads(json.dumps(asdict(candidate.key))),
            "center": [round(value, 9) for value in candidate.descriptor["center"]],
            "measure": round(
                candidate.descriptor.get("area", candidate.descriptor.get("length", 0)),
                9,
            ),
        }
        for candidate in result.provenance["cylinder"]
    ]
    resolution = result.resolve(Reference("cylinder", "side(circle)"))
    return {
        "candidates": candidates,
        "resolution": {
            "status": resolution.status.value,
            "keys": [
                json.loads(json.dumps(asdict(candidate.key)))
                for candidate in resolution.candidates
            ],
        },
    }


def test_circle_on_xz_is_deterministic_across_processes() -> None:
    graph = Graph()
    graph.add(
        Sketch(
            "sketch",
            plane=Plane("XZ", offset=3),
            entities=[Circle("circle", radius=2, position=(1, -2))],
        )
    )
    graph.add(Extrude("cylinder", sketch="sketch", height=4))
    serialized = graph.to_json()
    expected = _circle_snapshot(graph)
    script = """
import json, sys
from dataclasses import asdict
from coalesce import Reference, regenerate
from coalesce.serialization import loads
result = regenerate(loads(sys.stdin.read()))
candidates = []
for candidate in result.provenance['cylinder']:
    descriptor = candidate.descriptor
    candidates.append({
        'key': asdict(candidate.key),
        'center': [round(value, 9) for value in descriptor['center']],
        'measure': round(descriptor.get('area', descriptor.get('length', 0)), 9),
    })
resolution = result.resolve(Reference('cylinder', 'side(circle)'))
print(json.dumps({
    'candidates': candidates,
    'resolution': {
        'status': resolution.status.value,
        'keys': [asdict(candidate.key) for candidate in resolution.candidates],
    },
}))
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        input=serialized,
        text=True,
        capture_output=True,
        check=True,
    )

    assert expected == json.loads(completed.stdout)
