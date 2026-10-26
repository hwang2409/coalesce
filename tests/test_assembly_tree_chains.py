import itertools
import json

import pytest
from build123d import Cylinder, GeomType

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


def test_coupled_cycle_that_needs_free_dof_is_unsupported():
    ids = ("a", "b", "c")
    occurrences, resolved, provenance, references = _fixture(ids)
    mates = [
        Mate("ground", "ground", "a"),
        _mate("ab-axis", "concentric", "a", "b", references),
        _mate("bc-seat", "planar", "b", "c", references),
        _mate("ca-axis", "concentric", "c", "a", references),
    ]

    with pytest.raises(AssemblyError) as error:
        _solve(occurrences, resolved, provenance, mates)

    assert error.value.code == "MATE_SOLVE_UNSUPPORTED"
    assert error.value.details["mateIds"] == ["ab-axis", "bc-seat", "ca-axis"]
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
