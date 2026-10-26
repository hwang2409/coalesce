import itertools
import json

import pytest
from build123d import Box, Compound, Cylinder, GeomType, Pos

from coalesce.assembly import (
    Assembly,
    AssemblyError,
    Mate,
    MateSide,
    Occurrence,
    ResolvedOccurrence,
    RigidTransform,
    assembly_from_dict,
    assembly_to_dict,
)
from coalesce.assembly_solver import _mate_faces, _residual, solve_assembly
from coalesce.parts import TerminalPart
from coalesce.references import BodySource, Candidate, ElementKey, Reference


def _fixture(occurrence_ids):
    shape = Cylinder(8, 10)
    cylinder = next(face for face in shape.faces() if face.geom_type is GeomType.CYLINDER)
    planes = sorted(
        (face for face in shape.faces() if face.geom_type is GeomType.PLANE),
        key=lambda face: face.center().Z,
    )
    body = BodySource("part", "body")
    part = TerminalPart("part/body", "part", "manufacturing", body, "part", shape)
    references = {
        "axis": Reference("part", "axis", source="axis", body=body),
        "bottom": Reference("part", "bottom", source="bottom", body=body),
        "top": Reference("part", "top", source="top", body=body),
    }
    candidates = [
        Candidate(ElementKey("part", "axis", source="axis", body=body), cylinder),
        Candidate(ElementKey("part", "bottom", source="bottom", body=body), planes[0]),
        Candidate(ElementKey("part", "top", source="top", body=body), planes[1]),
    ]
    occurrences = {
        occurrence_id: Occurrence(
            occurrence_id,
            part.part_id,
            RigidTransform(
                (17.0 * index, 0.0 if index == 0 else -11.0 * index, 3.0 * index),
                (0.267261241912, 0.534522483825, 0.801783725737),
                37.0 + 19.0 * index,
            ),
        )
        for index, occurrence_id in enumerate(occurrence_ids)
    }
    resolved = [ResolvedOccurrence(item, part, shape) for item in occurrences.values()]
    return occurrences, resolved, {"part": candidates}, references


def _mate(mate_id, kind, first, second, references, *, offset=0.0):
    first_role, second_role = ("axis", "axis") if kind == "concentric" else ("top", "bottom")
    return Mate(
        mate_id,
        kind,
        first=MateSide(first, references[first_role]),
        second=MateSide(second, references[second_role]),
        orientation="aligned" if kind == "concentric" else "opposed",
        offset=offset,
    )


def _chain_mates(references, occurrence_ids):
    mates = [Mate("ground-ring", "ground", occurrence_ids[0])]
    for index, (first, second) in enumerate(itertools.pairwise(occurrence_ids)):
        mates.extend(
            [
                _mate(f"{index}-axis", "concentric", first, second, references),
                _mate(f"{index}-seat", "planar", first, second, references),
            ]
        )
    return mates


def _solve(occurrences, resolved, provenance, mates):
    return solve_assembly(
        Assembly(occurrences, {mate.id: mate for mate in mates}), resolved, provenance
    )


def _transform_bytes(result):
    rows = assembly_to_dict(result.assembly)["occurrences"]
    return json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()


def test_grounded_mixed_ring_shaft_disc_chain_solves_every_mate_order_exactly():
    occurrences, resolved, provenance, references = _fixture(("ring", "shaft", "disc"))
    mates = _chain_mates(references, ("ring", "shaft", "disc"))
    ground, moving = mates[0], mates[1:]

    solved = [
        _solve(occurrences, resolved, provenance, [ground, *order])
        for order in itertools.permutations(moving)
    ]

    assert len({_transform_bytes(result) for result in solved}) == 1
    status = solved[0].status_by_occurrence
    assert {item: status[item]["remainingDof"] for item in status} == {
        "ring": 0,
        "shaft": 1,
        "disc": 1,
    }
    faces = _mate_faces(
        solved[0].assembly,
        {item.occurrence.id: item for item in resolved},
        provenance,
    )
    for mate in moving:
        linear, angular = _residual(mate, solved[0].assembly.occurrences, faces)
        assert linear <= 1e-7
        assert angular <= 1e-7


def test_grounded_mixed_chain_of_four_solves():
    ids = ("ring", "shaft", "disc", "carrier")
    occurrences, resolved, provenance, references = _fixture(ids)

    result = _solve(occurrences, resolved, provenance, _chain_mates(references, ids))

    assert [result.status_by_occurrence[item]["remainingDof"] for item in ids] == [0, 1, 1, 1]


def test_consistent_cycle_passes_final_residual_gate():
    ids = ("a", "b", "c")
    occurrences, resolved, provenance, references = _fixture(ids)
    mates = [
        Mate("ground", "ground", "a"),
        _mate("ab", "planar", "a", "b", references, offset=10),
        _mate("bc", "planar", "b", "c", references, offset=10),
        _mate("ca", "planar", "a", "c", references, offset=30),
    ]

    result = _solve(occurrences, resolved, provenance, mates)

    assert result.redundant_mate_ids == ["bc"]


def test_inconsistent_cycle_reports_full_culprit_cycle():
    ids = ("a", "b", "c")
    occurrences, resolved, provenance, references = _fixture(ids)
    mates = [
        Mate("ground", "ground", "a"),
        _mate("ab", "planar", "a", "b", references, offset=10),
        _mate("bc", "planar", "b", "c", references, offset=10),
        _mate("ca", "planar", "a", "c", references, offset=40),
    ]

    with pytest.raises(AssemblyError) as error:
        _solve(occurrences, resolved, provenance, mates)

    assert error.value.code == "MATE_CONFLICT"
    assert error.value.details["mateIds"] == ["ab", "bc", "ca"]
    assert error.value.details["occurrenceIds"] == ["a", "b", "c"]


def _custom_fixture(shape, roles, transforms):
    body = BodySource("part", "body")
    part = TerminalPart("part/body", "part", "manufacturing", body, "part", shape)
    references = {
        role: Reference("part", role, source=role, body=body) for role in roles
    }
    provenance = {
        "part": [
            Candidate(ElementKey("part", role, source=role, body=body), face)
            for role, face in roles.items()
        ]
    }
    occurrences = {
        occurrence_id: Occurrence(occurrence_id, part.part_id, transform)
        for occurrence_id, transform in transforms.items()
    }
    resolved = [ResolvedOccurrence(item, part, shape) for item in occurrences.values()]
    return occurrences, resolved, provenance, references


def _planar_cycle(c_translation):
    shape = Box(10, 10, 10)
    roles = {}
    for face in shape.faces():
        normal = tuple(face.normal_at())
        axis = max(range(3), key=lambda index: abs(normal[index]))
        roles[f"{'xyz'[axis]}{'p' if normal[axis] > 0 else 'm'}"] = face
    identity = RigidTransform((0, 0, 0), (0, 0, 1), 0)
    occurrences, resolved, provenance, references = _custom_fixture(
        shape,
        roles,
        {
            "a": identity,
            "b": RigidTransform((0, 30, 5), (0, 0, 1), 0),
            "c": RigidTransform(c_translation, (0, 0, 1), 0),
        },
    )

    def planar(mate_id, first, first_role, second, second_role):
        return Mate(
            mate_id,
            "planar",
            first=MateSide(first, references[first_role]),
            second=MateSide(second, references[second_role]),
            orientation="opposed",
        )

    mates = [
        Mate("ground", "ground", "a"),
        planar("ab", "a", "zp", "b", "zm"),
        planar("ac", "a", "yp", "c", "ym"),
        planar("bc", "b", "xp", "c", "xm"),
    ]
    return occurrences, resolved, provenance, references, mates, planar


def test_planar_cycle_that_tree_seed_misses_is_unsupported_with_exact_cycle():
    occurrences, resolved, provenance, _, mates, _ = _planar_cycle((30, 5, 0))

    with pytest.raises(AssemblyError) as error:
        _solve(occurrences, resolved, provenance, mates)

    assert error.value.code == "MATE_SOLVE_UNSUPPORTED"
    assert error.value.details["mateIds"] == ["ab", "ac", "bc"]
    assert error.value.details["occurrenceIds"] == ["a", "b", "c"]


def test_planar_cycle_passes_when_tree_seed_satisfies_closer():
    occurrences, resolved, provenance, _, mates, _ = _planar_cycle((10, 5, 0))

    result = _solve(occurrences, resolved, provenance, mates)

    assert result.redundant_mate_ids == ["bc"]


def _concentric_cycle(c_angle):
    shape = Compound(children=[Cylinder(2, 10), Pos(10, 0, 0) * Cylinder(2, 10)])
    cylinders = sorted(
        (face for face in shape.faces() if face.geom_type is GeomType.CYLINDER),
        key=lambda face: face.center().X,
    )
    roles = {"axis0": cylinders[0], "axis1": cylinders[1]}
    occurrences, resolved, provenance, references = _custom_fixture(
        shape,
        roles,
        {
            "a": RigidTransform((0, 0, 0), (0, 0, 1), 0),
            "b": RigidTransform((0, 0, 0), (0, 0, 1), 0),
            "c": RigidTransform((0, 0, 0), (0, 0, 1), c_angle),
        },
    )

    def concentric(mate_id, first, first_role, second, second_role):
        return Mate(
            mate_id,
            "concentric",
            first=MateSide(first, references[first_role]),
            second=MateSide(second, references[second_role]),
            orientation="aligned",
        )

    mates = [
        Mate("ground", "ground", "a"),
        concentric("ab", "a", "axis0", "b", "axis0"),
        concentric("ac", "a", "axis0", "c", "axis0"),
        concentric("bc", "b", "axis1", "c", "axis1"),
    ]
    return occurrences, resolved, provenance, mates


def test_concentric_cycle_that_tree_seed_misses_is_unsupported_with_exact_cycle():
    occurrences, resolved, provenance, mates = _concentric_cycle(90)

    with pytest.raises(AssemblyError) as error:
        _solve(occurrences, resolved, provenance, mates)

    assert error.value.code == "MATE_SOLVE_UNSUPPORTED"
    assert error.value.details["mateIds"] == ["ab", "ac", "bc"]
    assert error.value.details["occurrenceIds"] == ["a", "b", "c"]


def test_concentric_cycle_passes_when_tree_seed_satisfies_closer():
    occurrences, resolved, provenance, mates = _concentric_cycle(0)

    result = _solve(occurrences, resolved, provenance, mates)

    assert result.redundant_mate_ids == ["bc"]


@pytest.mark.parametrize("grounded", ["a", "b"])
def test_consistent_planar_cycle_with_concentric_leaf_solves_for_either_ground(grounded):
    occurrences, resolved, provenance, references = _fixture(("a", "b", "c", "d"))
    cycle_mates = [
        Mate("ground", "ground", grounded),
        _mate("ab", "planar", "a", "b", references, offset=10),
        _mate("bc", "planar", "b", "c", references, offset=10),
        _mate("ca", "planar", "a", "c", references, offset=30),
        _mate("bd", "concentric", "b", "d", references),
    ]

    result = _solve(occurrences, resolved, provenance, cycle_mates)

    assert set(result.redundant_mate_ids) <= {"ab", "bc", "ca"}
    assert len(result.redundant_mate_ids) == 1


def test_fully_constrained_inconsistent_cycle_is_conflict_with_exact_cycle():
    occurrences, resolved, provenance, _, _, planar = _planar_cycle((0, 0, 0))
    mates = [Mate("ground", "ground", "a")]
    for pair, first, second in (("ab", "a", "b"), ("ac", "a", "c")):
        mates.extend(
            [
                planar(f"{pair}-x", first, "xp", second, "xm"),
                planar(f"{pair}-y", first, "yp", second, "ym"),
                planar(f"{pair}-z", first, "zp", second, "zm"),
            ]
        )
    mates.append(planar("bc-conflict", "b", "xp", "c", "xp"))

    with pytest.raises(AssemblyError) as error:
        _solve(occurrences, resolved, provenance, mates)

    assert error.value.code == "MATE_CONFLICT"
    assert error.value.details["mateIds"] == [
        "ab-x",
        "ab-y",
        "ab-z",
        "ac-x",
        "ac-y",
        "ac-z",
        "bc-conflict",
    ]
    assert error.value.details["occurrenceIds"] == ["a", "b", "c"]


def test_mixed_chain_restart_is_byte_stable_with_odd_rotations():
    ids = ("ring", "shaft", "disc")
    occurrences, resolved, provenance, references = _fixture(ids)
    first = _solve(occurrences, resolved, provenance, _chain_mates(references, ids))
    encoded = json.dumps(assembly_to_dict(first.assembly), sort_keys=True, separators=(",", ":"))
    restarted = assembly_from_dict(json.loads(encoded))
    restarted_resolved = [
        ResolvedOccurrence(restarted.occurrences[item.occurrence.id], item.part, item.shape)
        for item in resolved
    ]

    second = solve_assembly(restarted, restarted_resolved, provenance)

    assert json.dumps(
        assembly_to_dict(second.assembly), sort_keys=True, separators=(",", ":")
    ) == encoded
