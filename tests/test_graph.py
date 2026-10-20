import json
import subprocess
import sys
from dataclasses import asdict
from math import isclose

import pytest

from coalesce import (
    Cardinality,
    Cut,
    Extrude,
    Fillet,
    Graph,
    GraphError,
    Rectangle,
    Reference,
    Sketch,
    SketchSegmentSource,
    Status,
    regenerate,
)
from coalesce.serialization import loads


def base(height=10, *, position=(0, 0, 0), rotation=(0, 0, 0)):
    graph = Graph()
    graph.add(
        Sketch(
            "sk",
            entities=[Rectangle("profile", 10, 10)],
            position=position,
            rotation=rotation,
        )
    )
    graph.add(Extrude("base", sketch="sk", height=height))
    return graph


def snapshot(result):
    candidates = []
    for candidate in result.provenance["base"]:
        descriptor = candidate.descriptor
        candidates.append(
            {
                "key": json.loads(json.dumps(asdict(candidate.key))),
                "center": [round(value, 6) for value in descriptor["center"]],
                "measure": round(descriptor.get("area", descriptor.get("length", 0)), 6),
            }
        )
    resolution = result.resolve(Reference("base", "side(left)"))
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


def test_duplicate_sketch_entity_ids_fail_graph_validation_before_regen():
    graph = Graph()
    graph.add(
        Sketch(
            "sk",
            entities=[
                Rectangle("dup", 2, 2),
                Rectangle("dup", 2, 2, position=(4, 0)),
            ],
        )
    )
    graph.add(Extrude("parts", sketch="sk", height=3))

    with pytest.raises(GraphError) as caught:
        graph.validate()

    assert caught.value.error["code"] == "duplicate_entity_id"
    assert caught.value.error["sketch"] == "sk"
    assert caught.value.error["entity"] == "dup"



def test_case_1_cap_end_height_geometry():
    graph = base()
    first = regenerate(graph)
    ref = Reference("base", "cap_end")

    first_cap = first.resolve(ref)
    assert first_cap.status is Status.OK
    assert isclose(first_cap.candidates[0].entity.center().Z, 10)

    graph.nodes["base"].height = 20
    second_cap = regenerate(graph).resolve(ref)
    assert second_cap.status is Status.OK
    assert isclose(second_cap.candidates[0].entity.center().Z, 20)


def test_case_2_rectangle_wall_follows_width():
    graph = base()
    ref = Reference("base", "side(left)")

    first = regenerate(graph).resolve(ref)
    assert first.status is Status.OK
    assert isclose(first.candidates[0].entity.center().X, -5)

    graph.nodes["sk"].entities[0].width = 20
    second = regenerate(graph).resolve(ref)
    assert second.status is Status.OK
    assert isclose(second.candidates[0].entity.center().X, -10)


def test_case_8_delete_through_graph_api():
    graph = base()
    assert regenerate(graph).resolve(Reference("base", "cap_end")).status is Status.OK

    graph.delete("base")
    resolution = regenerate(graph).resolve(Reference("base", "cap_end"))

    assert resolution.status is Status.EMPTY
    assert resolution.error["feature"] == "base"
    assert resolution.error["reference"]["role"] == "cap_end"


def test_case_23_regeneration_is_deterministic_in_and_out_of_process():
    graph = base(position=(3, -2, 1), rotation=(0, 0, 30))
    serialized = graph.to_json()

    first = snapshot(regenerate(graph))
    second = snapshot(regenerate(graph))
    script = """
import json, sys
from dataclasses import asdict
from coalesce import Reference, regenerate
from coalesce.serialization import loads
result = regenerate(loads(sys.stdin.read()))
candidates = []
for candidate in result.provenance['base']:
    descriptor = candidate.descriptor
    candidates.append({
        'key': asdict(candidate.key),
        'center': [round(value, 6) for value in descriptor['center']],
        'measure': round(descriptor.get('area', descriptor.get('length', 0)), 6),
    })
resolution = result.resolve(Reference('base', 'side(left)'))
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

    assert first == second == json.loads(completed.stdout)
    assert regenerate(graph).resolve(Reference("base", "side(left)")).status is Status.OK


def test_anti_guessing_rotated_rectangle_uses_constructed_segment_history():
    graph = base(position=(7, -3, 2), rotation=(0, 0, 30))

    resolution = regenerate(graph).resolve(Reference("base", "side(left)"))

    assert resolution.status is Status.OK
    wall = resolution.candidates[0].entity
    assert isclose(wall.center().X, 7 - 5 * 3**0.5 / 2)
    assert isclose(wall.center().Y, -5.5)


def test_json_round_trip_preserves_typed_segment_source_and_body_scope():
    from coalesce import BodySource

    graph = base()
    source = SketchSegmentSource("sk", "profile", "left")
    body = BodySource("base", "profile")
    graph.add(
        Fillet(
            "round",
            target="base",
            edges=[Reference("base", "side(left)", source=source, body=body)],
        )
    )

    restored = loads(graph.to_json())

    reference = restored.nodes["round"].edges[0]
    assert reference.source == source
    assert reference.body == body


def test_json_round_trip_preserves_cut_depth_and_flip():
    graph = base()
    graph.add(Sketch("tool", entities=[Rectangle("pocket", 2, 2)]))
    graph.add(Cut("cut", target="base", tool="tool", depth=3, flip=True))

    restored = loads(graph.to_json())

    assert restored.nodes["cut"].depth == 3
    assert restored.nodes["cut"].flip is True


def test_json_round_trip_preserves_any_cardinality():
    graph = base()
    graph.add(
        Fillet(
            "round",
            target="base",
            edges=[Reference("base", "side(left)", Cardinality.ANY)],
        )
    )

    restored = loads(graph.to_json())

    assert restored.nodes["round"].edges[0].cardinality is Cardinality.ANY


def test_json_round_trip_regenerates_the_same_geometry():
    graph = base(position=(2, 4, 1), rotation=(10, 20, 30))
    original = regenerate(graph)

    clone = loads(graph.to_json())
    restored = regenerate(clone)

    assert isclose(restored.shapes["base"].volume, original.shapes["base"].volume)
    assert snapshot(restored) == snapshot(original)


def test_step_c_feature_fails_clearly_instead_of_guessing():
    graph = base()
    graph.add(Fillet("round", target="base"))

    with pytest.raises(NotImplementedError, match="Fillet regeneration requires step C"):
        regenerate(graph)
