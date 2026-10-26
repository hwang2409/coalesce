"""Closed-form planar assembly solving behind one mate-graph seam."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from build123d import Face, GeomType

from .assembly import (
    Assembly,
    AssemblyError,
    Mate,
    Occurrence,
    ResolvedOccurrence,
    RigidTransform,
    locate_shape,
)
from .references import Candidate, Status, face_role_catalog, resolve

_LINEAR_TOLERANCE = 1e-7
_ANGULAR_TOLERANCE = 1e-9


@dataclass(frozen=True)
class AssemblySolveResult:
    assembly: Assembly
    solve_status: str
    under_constrained_occurrence_ids: list[str]
    status_by_occurrence: dict[str, dict[str, Any]]
    components: list[dict[str, Any]]
    redundant_mate_ids: list[str]


class MateGraph:
    """Own connectivity, component ordering, and the one-ground rule."""

    def __init__(self, assembly: Assembly):
        self.assembly = assembly
        self.adjacent: dict[str, list[tuple[str, Mate]]] = {
            occurrence_id: [] for occurrence_id in assembly.occurrences
        }
        for mate in assembly.mates.values():
            if mate.kind != "planar":
                continue
            assert mate.first is not None and mate.second is not None
            first, second = mate.first.occurrence_id, mate.second.occurrence_id
            self.adjacent[first].append((second, mate))
            self.adjacent[second].append((first, mate))
        for edges in self.adjacent.values():
            edges.sort(key=lambda item: (item[1].id, item[0]))

    def components(self) -> list[tuple[str, ...]]:
        unseen = set(self.adjacent)
        result = []
        while unseen:
            start = min(unseen)
            stack = [start]
            component = []
            unseen.remove(start)
            while stack:
                current = stack.pop()
                component.append(current)
                for other, _ in self.adjacent[current]:
                    if other in unseen:
                        unseen.remove(other)
                        stack.append(other)
            result.append(tuple(sorted(component)))
        return result

    def grounds(self, component: tuple[str, ...]) -> list[Mate]:
        members = set(component)
        grounds = sorted(
            (
                mate
                for mate in self.assembly.mates.values()
                if mate.kind == "ground" and mate.occurrence_id in members
            ),
            key=lambda mate: mate.id,
        )
        if len(grounds) > 1:
            raise AssemblyError(
                "MATE_CONFLICT",
                f"assembly component cannot have more than one ground mate: {', '.join(component)}",
                mateIds=[mate.id for mate in grounds],
                occurrenceIds=list(component),
                hint="edit or remove one of the ground mates",
            )
        return grounds


def _dot(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _cross(left: tuple[float, float, float], right: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _scale(vector: tuple[float, float, float], factor: float) -> tuple[float, float, float]:
    return tuple(value * factor for value in vector)  # type: ignore[return-value]


def _add(left: tuple[float, float, float], right: tuple[float, float, float]) -> tuple[float, float, float]:
    return tuple(a + b for a, b in zip(left, right, strict=True))  # type: ignore[return-value]


def _unit(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(_dot(vector, vector))
    if length <= _ANGULAR_TOLERANCE:
        raise AssemblyError("MATE_GEOMETRY_UNSUPPORTED", "planar face has no stable normal")
    return _scale(vector, 1.0 / length)


def _quaternion(transform: RigidTransform) -> tuple[float, float, float, float]:
    half = math.radians(transform.angle_degrees) / 2
    scale = math.sin(half)
    return (math.cos(half), *(component * scale for component in transform.axis))


def _multiply(
    left: tuple[float, float, float, float], right: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    lw, lx, ly, lz = left
    rw, rx, ry, rz = right
    return (
        lw * rw - lx * rx - ly * ry - lz * rz,
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
    )


def _between(source: tuple[float, float, float], target: tuple[float, float, float]) -> tuple[float, float, float, float]:
    source, target = _unit(source), _unit(target)
    cosine = max(-1.0, min(1.0, _dot(source, target)))
    if cosine > 1.0 - _ANGULAR_TOLERANCE:
        return (1.0, 0.0, 0.0, 0.0)
    if cosine < -1.0 + _ANGULAR_TOLERANCE:
        axes = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        axis = _unit(_cross(source, min(axes, key=lambda item: abs(_dot(source, item)))))
        return (0.0, *axis)
    cross = _cross(source, target)
    quaternion = (1.0 + cosine, *cross)
    length = math.sqrt(sum(value * value for value in quaternion))
    return tuple(value / length for value in quaternion)  # type: ignore[return-value]


def _transform(quaternion: tuple[float, float, float, float], translation: tuple[float, float, float]) -> RigidTransform:
    length = math.sqrt(sum(value * value for value in quaternion))
    quaternion = tuple(value / length for value in quaternion)  # type: ignore[assignment]
    if quaternion[0] < 0:
        quaternion = tuple(-value for value in quaternion)  # type: ignore[assignment]
    w, x, y, z = quaternion
    vector_length = math.sqrt(x * x + y * y + z * z)
    if vector_length <= _ANGULAR_TOLERANCE:
        return RigidTransform(translation, (0.0, 0.0, 1.0), 0.0)
    axis = (x / vector_length, y / vector_length, z / vector_length)
    angle = math.degrees(2 * math.atan2(vector_length, max(-1.0, min(1.0, w))))
    if angle >= 180.0:
        angle -= 360.0
        axis = tuple(-value for value in axis)  # type: ignore[assignment]
    return RigidTransform(translation, axis, angle)


def _matching_face(candidate: Candidate, faces: list[Face]) -> Face | None:
    if not isinstance(candidate.entity, Face):
        return None
    matches = [face for face in faces if candidate.entity.is_same(face)]
    return matches[0] if len(matches) == 1 else None


def _oriented_candidate(candidate: Candidate, face: Face) -> Candidate:
    center = face.center()
    normal = face.normal_at()
    return Candidate(
        candidate.key,
        face,
        {
            "type": face.geom_type.name,
            "center": (center.X, center.Y, center.Z),
            "normal": (normal.X, normal.Y, normal.Z),
            "area": float(face.area),
        },
        candidate.merged_from,
    )


def _part_candidates(
    resolved: ResolvedOccurrence, provenance: dict[str, list[Candidate]]
) -> dict[str, list[Candidate]]:
    faces = resolved.part.shape.faces()
    selected = []
    candidates = (candidate for group in provenance.values() for candidate in group)
    for candidate in candidates:
        matching = _matching_face(candidate, faces)
        if (
            matching is not None
            and candidate.key.body == resolved.part.source_body
            and not any(matching.is_same(item.entity) for item in selected)
        ):
            selected.append(_oriented_candidate(candidate, matching))
    return {resolved.part.terminal_feature: selected}


def _resolved_face(
    mate: Mate,
    side_name: str,
    resolved: ResolvedOccurrence,
    provenance: dict[str, list[Candidate]],
) -> tuple[Face, Any]:
    side = getattr(mate, side_name)
    assert side is not None
    scoped = _part_candidates(resolved, provenance)
    resolution = resolve(side.reference, scoped)
    if resolution.status is not Status.OK or len(resolution.candidates) != 1:
        details = resolution.error or {
            "status": resolution.status.value,
            "reference": side.reference.to_dict(),
            "candidates": [],
            "availableRoles": face_role_catalog(scoped, side.reference.feature),
        }
        raise AssemblyError(
            "MATE_REFERENCE_STALE",
            f"assembly mate {mate.id} has a stale {side_name} face reference",
            mateId=mate.id,
            side=side_name,
            occurrenceId=side.occurrence_id,
            resolution=details,
        )
    face = resolution.candidates[0].entity
    if face.geom_type is not GeomType.PLANE or float(face.area) <= _LINEAR_TOLERANCE:
        raise AssemblyError(
            "MATE_GEOMETRY_UNSUPPORTED",
            f"assembly mate {mate.id} {side_name} face must be planar with non-zero area",
            mateId=mate.id,
            side=side_name,
            occurrenceId=side.occurrence_id,
            surfaceType=face.geom_type.name,
            area={"value": float(face.area), "unit": "mm^2"},
        )
    return face, resolved.part.shape


def _plane(
    resolved_face: tuple[Face, Any], transform: RigidTransform
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    face, part_shape = resolved_face
    located = face.moved(locate_shape(part_shape, transform).location)
    center = located.center()
    normal = located.normal_at()
    return (center.X, center.Y, center.Z), _unit((normal.X, normal.Y, normal.Z))


def _solve_planar(
    mate: Mate,
    moved_id: str,
    occurrences: dict[str, Occurrence],
    faces: dict[tuple[str, str], tuple[Face, Any]],
) -> Occurrence:
    assert mate.first is not None and mate.second is not None and mate.orientation is not None
    first_id, second_id = mate.first.occurrence_id, mate.second.occurrence_id
    first_transform = occurrences[first_id].transform
    second_transform = occurrences[second_id].transform
    first_center, first_normal = _plane(faces[(mate.id, "first")], first_transform)
    second_center, second_normal = _plane(faces[(mate.id, "second")], second_transform)
    moved = occurrences[moved_id]
    current_normal = first_normal if moved_id == first_id else second_normal
    if moved_id == second_id:
        target_normal = first_normal if mate.orientation == "aligned" else _scale(first_normal, -1)
    else:
        target_normal = second_normal if mate.orientation == "aligned" else _scale(second_normal, -1)
    rotated = _transform(_multiply(_between(current_normal, target_normal), _quaternion(moved.transform)), moved.transform.translation)
    trial = dict(occurrences)
    trial[moved_id] = replace(moved, transform=rotated)
    first_center, first_normal = _plane(faces[(mate.id, "first")], trial[first_id].transform)
    second_center, _ = _plane(faces[(mate.id, "second")], trial[second_id].transform)
    residual = mate.offset - _dot(
        tuple(b - a for a, b in zip(first_center, second_center, strict=True)), first_normal
    )
    translation = _add(rotated.translation, _scale(first_normal, residual if moved_id == second_id else -residual))
    return replace(moved, transform=_transform(_quaternion(rotated), translation))


def _residual(
    mate: Mate,
    occurrences: dict[str, Occurrence],
    faces: dict[tuple[str, str], tuple[Face, Any]],
) -> tuple[float, float]:
    assert mate.first is not None and mate.second is not None and mate.orientation is not None
    first_center, first_normal = _plane(faces[(mate.id, "first")], occurrences[mate.first.occurrence_id].transform)
    second_center, second_normal = _plane(faces[(mate.id, "second")], occurrences[mate.second.occurrence_id].transform)
    target_dot = 1.0 if mate.orientation == "aligned" else -1.0
    angular = abs(_dot(first_normal, second_normal) - target_dot)
    linear = abs(_dot(tuple(b - a for a, b in zip(first_center, second_center, strict=True)), first_normal) - mate.offset)
    return linear, angular


def _mate_faces(
    assembly: Assembly,
    resolved: dict[str, ResolvedOccurrence],
    provenance: dict[str, list[Candidate]],
) -> dict[tuple[str, str], tuple[Face, Any]]:
    faces = {}
    for mate in sorted(assembly.mates.values(), key=lambda item: item.id):
        if mate.kind == "planar":
            assert mate.first is not None and mate.second is not None
            faces[(mate.id, "first")] = _resolved_face(
                mate, "first", resolved[mate.first.occurrence_id], provenance
            )
            faces[(mate.id, "second")] = _resolved_face(
                mate, "second", resolved[mate.second.occurrence_id], provenance
            )
    return faces


def _apply_pending_mate(
    mate: Mate,
    known: set[str],
    occurrences: dict[str, Occurrence],
    faces: dict[tuple[str, str], tuple[Face, Any]],
    redundant: list[str],
) -> bool:
    assert mate.first is not None and mate.second is not None
    first, second = mate.first.occurrence_id, mate.second.occurrence_id
    if first in known and second not in known:
        occurrences[second] = _solve_planar(mate, second, occurrences, faces)
        known.add(second)
    elif second in known and first not in known:
        occurrences[first] = _solve_planar(mate, first, occurrences, faces)
        known.add(first)
    elif first in known and second in known:
        linear, angular = _residual(mate, occurrences, faces)
        if linear > _LINEAR_TOLERANCE or angular > _ANGULAR_TOLERANCE:
            raise AssemblyError(
                "MATE_CONFLICT",
                f"assembly mate {mate.id} conflicts with solved planar geometry",
                mateIds=[mate.id],
                occurrenceIds=sorted([first, second]),
                linearResidual={"value": linear, "unit": "mm"},
                angularResidual=angular,
            )
        redundant.append(mate.id)
    else:
        return False
    return True


def _solve_component(
    graph: MateGraph,
    component: tuple[str, ...],
    anchor: str,
    occurrences: dict[str, Occurrence],
    faces: dict[tuple[str, str], tuple[Face, Any]],
    redundant: list[str],
) -> None:
    known = {anchor}
    pending = {mate.id: mate for member in component for _, mate in graph.adjacent[member]}
    while pending:
        solved_id = next(
            (
                mate_id
                for mate_id, mate in sorted(pending.items())
                if _apply_pending_mate(mate, known, occurrences, faces, redundant)
            ),
            None,
        )
        if solved_id is None:
            raise AssemblyError(
                "MATE_SOLVE_UNSUPPORTED",
                "assembly mate graph could not be solved deterministically",
                mateIds=sorted(pending),
                occurrenceIds=list(component),
            )
        del pending[solved_id]


def _occurrence_status(
    graph: MateGraph,
    occurrence_id: str,
    grounds: list[Mate],
    anchor: str,
) -> dict[str, Any]:
    grounded = any(mate.occurrence_id == occurrence_id for mate in grounds)
    remaining = 0 if grounded else (6 if occurrence_id == anchor else 3)
    incident = [mate.id for _, mate in graph.adjacent[occurrence_id]]
    incident += [mate.id for mate in grounds if mate.occurrence_id == occurrence_id]
    motions = ["globalRigidMotion"] if occurrence_id == anchor else ["inPlaneX", "inPlaneY", "normalSpin"]
    return {
        "grounded": grounded,
        "remainingDof": remaining,
        "freeMotions": [] if grounded else motions,
        "mateIds": sorted(incident),
    }


def _component_status(
    graph: MateGraph,
    component: tuple[str, ...],
    grounds: list[Mate],
    anchor: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    grounded_id = grounds[0].occurrence_id if grounds else None
    status = {
        occurrence_id: _occurrence_status(graph, occurrence_id, grounds, anchor)
        for occurrence_id in component
    }
    component_mates = {mate.id for member in component for _, mate in graph.adjacent[member]}
    component_mates.update(mate.id for mate in grounds)
    remaining_dof = sum(row["remainingDof"] for row in status.values())
    return status, {
        "occurrenceIds": list(component),
        "mateIds": sorted(component_mates),
        "status": "FULLY_CONSTRAINED" if remaining_dof == 0 else "UNDER_CONSTRAINED",
        "remainingDof": remaining_dof,
        "grounded": grounded_id is not None,
    }


def solve_assembly(
    assembly: Assembly,
    resolved_occurrences: list[ResolvedOccurrence],
    provenance: dict[str, list[Candidate]],
) -> AssemblySolveResult:
    """Resolve mate faces, solve transforms, and report status from one shared mate graph."""
    graph = MateGraph(assembly)
    resolved = {item.occurrence.id: item for item in resolved_occurrences}
    faces = _mate_faces(assembly, resolved, provenance)
    occurrences = dict(assembly.occurrences)
    redundant: list[str] = []
    component_rows = []
    status: dict[str, dict[str, Any]] = {}
    for component in graph.components():
        grounds = graph.grounds(component)
        anchor = grounds[0].occurrence_id if grounds else component[0]
        assert anchor is not None
        _solve_component(graph, component, anchor, occurrences, faces, redundant)
        component_status, component_row = _component_status(graph, component, grounds, anchor)
        status.update(component_status)
        component_rows.append(component_row)
    under = sorted(item for item, row in status.items() if row["remainingDof"] > 0)
    return AssemblySolveResult(
        Assembly(occurrences, dict(assembly.mates)),
        "UNDER_CONSTRAINED" if under else "FULLY_CONSTRAINED",
        under,
        status,
        component_rows,
        sorted(redundant),
    )
