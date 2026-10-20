import json
import subprocess
import sys
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
    Rectangle,
    Reference,
    Sketch,
    Status,
    regenerate,
)
from coalesce.serialization import loads


def wall_graph() -> Graph:
    graph = Graph()
    graph.add(Sketch("plate_sketch", entities=[Rectangle("plate", 60, 40)]))
    graph.add(Extrude("plate", sketch="plate_sketch", height=3))
    graph.add(
        Sketch(
            "wall_sketch",
            entities=[Rectangle("outer", 60, 40), Rectangle("inner", 54, 34)],
            position=(0, 0, 3),
        )
    )
    graph.add(
        Extrude(
            "wall",
            sketch="wall_sketch",
            height=4,
            operation="join",
            target="plate",
        )
    )
    return graph


def summary(graph: Graph) -> dict[str, object]:
    result = regenerate(graph)
    shape = result.shapes["wall"]
    return {
        "volume": round(shape.volume, 9),
        "solids": len(shape.solids()),
        "keys": sorted(
            (
                candidate.key.feature,
                candidate.key.role,
                str(candidate.key.source),
                str(candidate.key.body),
                tuple(str(key) for key in candidate.merged_from),
            )
            for candidates in result.provenance.values()
            for candidate in candidates
        ),
    }


def test_wall_ring_join_is_one_target_body_with_history_roles() -> None:
    result = regenerate(wall_graph())
    shape = result.shapes["wall"]
    bounds = shape.bounding_box()

    assert len(shape.solids()) == 1
    assert isclose(shape.volume, 60 * 40 * 3 + (60 * 40 - 54 * 34) * 4)
    assert isclose(bounds.min.Z, 0)
    assert isclose(bounds.max.Z, 7)
    assert result.joined_material == {"wall": True}
    assert {
        candidate.key.body
        for candidates in result.provenance.values()
        for candidate in candidates
    } == {BodySource("plate", "plate")}
    assert result.resolve(Reference("wall", "cap_start")).status is Status.EMPTY

    plate_top = result.resolve(Reference("plate", "cap_end"))
    assert plate_top.status is Status.OK
    assert plate_top.error is None
    assert isclose(plate_top.candidates[0].entity.area, 54 * 34)

    merged_side = result.resolve(Reference("plate", "side(left)"))
    assert merged_side.status is Status.MERGED
    assert merged_side.error is None
    assert isclose(merged_side.candidates[0].entity.area, 40 * 7)
    wall_side = result.resolve(Reference("wall", "side(left)"))
    assert wall_side.status is Status.MERGED
    assert wall_side.candidates[0].entity.is_same(merged_side.candidates[0].entity)

    wall_top = result.resolve(
        Reference("wall", "cap_end", Cardinality.ONE_OR_MORE)
    )
    assert wall_top.status is Status.OK
    assert len(wall_top.candidates) == 1
    assert isclose(wall_top.candidates[0].entity.area, 60 * 40 - 54 * 34)


def test_cut_after_join_resolves_joined_wall_by_history() -> None:
    graph = wall_graph()
    graph.add(
        Sketch(
            "door_sketch",
            entities=[Rectangle("door", 10, 6)],
            position=(0, -20, 4),
            rotation=(90, 0, 0),
        )
    )
    graph.add(Cut("door", target="wall", tool="door_sketch", depth=3, flip=True))

    result = regenerate(graph)
    wall = result.resolve(
        Reference("wall", "side(bottom)", Cardinality.ONE_OR_MORE)
    )
    cut_floor = result.resolve(Reference("door", "pocket_floor"))

    assert wall.status is Status.MERGED
    assert len(wall.candidates) == 3
    assert cut_floor.status is Status.MERGED
    assert all(
        candidate.key.body == BodySource("plate", "plate")
        for candidate in [*wall.candidates, *cut_floor.candidates]
    )


def test_circle_boss_joins_plate() -> None:
    graph = Graph()
    graph.add(Sketch("plate_sketch", entities=[Rectangle("plate", 60, 40)]))
    graph.add(Extrude("plate", sketch="plate_sketch", height=3))
    graph.add(
        Sketch(
            "boss_sketch",
            entities=[Circle("boss_profile", 5)],
            position=(0, 0, 3),
        )
    )
    graph.add(
        Extrude(
            "boss",
            sketch="boss_sketch",
            height=4,
            operation="join",
            target="plate",
        )
    )

    result = regenerate(graph)
    shape = result.shapes["boss"]

    assert len(shape.solids()) == 1
    assert isclose(shape.volume, 60 * 40 * 3 + pi * 25 * 4, rel_tol=1e-9)
    assert result.resolve(Reference("boss", "side(circle)")).status is Status.OK
    assert result.resolve(Reference("boss", "cap_end")).status is Status.OK


def test_join_without_contact_is_structured_error() -> None:
    graph = Graph()
    graph.add(Sketch("plate_sketch", entities=[Rectangle("plate", 10, 10)]))
    graph.add(Extrude("plate", sketch="plate_sketch", height=3))
    graph.add(
        Sketch(
            "boss_sketch",
            entities=[Circle("boss_profile", 1)],
            position=(20, 0, 3),
        )
    )
    graph.add(
        Extrude(
            "boss",
            sketch="boss_sketch",
            height=4,
            operation="join",
            target="plate",
        )
    )

    with pytest.raises(GraphError) as caught:
        regenerate(graph)

    assert caught.value.error == {
        "code": "JOIN_NO_CONTACT",
        "message": "join extrude boss does not contact target plate",
        "feature": "boss",
        "target": "plate",
    }


def test_join_json_round_trip_preserves_operation_and_target() -> None:
    graph = wall_graph()

    payload = json.loads(graph.to_json())
    serialized_wall = next(node for node in payload["nodes"] if node["id"] == "wall")
    assert serialized_wall["operation"] == "join"
    assert serialized_wall["target"] == "plate"
    restored = loads(graph.to_json())
    wall = restored.nodes["wall"]

    assert wall.operation == "join"
    assert wall.target == "plate"
    assert summary(restored) == summary(graph)


def test_default_extrude_operation_remains_new_body() -> None:
    graph = Graph()
    graph.add(Sketch("sketch", entities=[Rectangle("profile", 2, 2)]))
    graph.add(Extrude("part", sketch="sketch", height=3))

    payload = json.loads(graph.to_json())
    serialized_part = next(node for node in payload["nodes"] if node["id"] == "part")
    assert "operation" not in serialized_part
    assert "target" not in serialized_part
    restored = loads(graph.to_json())

    assert restored.nodes["part"].operation == "new_body"
    assert restored.nodes["part"].target is None
    assert len(regenerate(restored).shapes["part"].solids()) == 1


def test_join_is_deterministic_across_processes(tmp_path) -> None:
    graph = wall_graph()
    path = tmp_path / "join.json"
    path.write_text(graph.to_json())
    script = """
import json
import sys
from coalesce.serialization import loads
from coalesce import regenerate

graph = loads(open(sys.argv[1]).read())
result = regenerate(graph)
shape = result.shapes[\"wall\"]
print(json.dumps({
    \"volume\": round(shape.volume, 9),
    \"solids\": len(shape.solids()),
    \"keys\": sorted((
        candidate.key.feature,
        candidate.key.role,
        str(candidate.key.source),
        str(candidate.key.body),
        tuple(str(key) for key in candidate.merged_from),
    ) for candidates in result.provenance.values() for candidate in candidates),
}, sort_keys=True))
"""

    first = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    second = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert json.loads(first) == json.loads(json.dumps(summary(graph)))
    assert first == second
