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
    canonical_transform,
    locate_shape,
)
from .references import Candidate, SketchSegmentSource, Status, face_role_catalog, resolve

_LINEAR_TOLERANCE = 1e-7
_ANGULAR_TOLERANCE = 1e-7


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
            if mate.kind == "ground":
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


def _cross(
    left: tuple[float, float, float], right: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _scale(vector: tuple[float, float, float], factor: float) -> tuple[float, float, float]:
    return tuple(value * factor for value in vector)  # type: ignore[return-value]


def _add(
    left: tuple[float, float, float], right: tuple[float, float, float]
) -> tuple[float, float, float]:
    return tuple(a + b for a, b in zip(left, right, strict=True))  # type: ignore[return-value]


def _unit(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(_dot(vector, vector))
    if length <= _ANGULAR_TOLERANCE:
        raise AssemblyError("MATE_GEOMETRY_UNSUPPORTED", "mate geometry has no stable direction")
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


def _angle_between(
    source: tuple[float, float, float], target: tuple[float, float, float]
) -> float:
    source, target = _unit(source), _unit(target)
    cross = _cross(source, target)
    return math.atan2(math.sqrt(_dot(cross, cross)), max(-1.0, min(1.0, _dot(source, target))))


def _between(
    source: tuple[float, float, float], target: tuple[float, float, float]
) -> tuple[float, float, float, float]:
    source, target = _unit(source), _unit(target)
    cosine = max(-1.0, min(1.0, _dot(source, target)))
    angle = _angle_between(source, target)
    if angle <= _ANGULAR_TOLERANCE:
        return (1.0, 0.0, 0.0, 0.0)
    if math.pi - angle <= _ANGULAR_TOLERANCE:
        axes = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        axis = _unit(_cross(source, min(axes, key=lambda item: abs(_dot(source, item)))))
        return (0.0, *axis)
    cross = _cross(source, target)
    quaternion = (1.0 + cosine, *cross)
    length = math.sqrt(sum(value * value for value in quaternion))
    return tuple(value / length for value in quaternion)  # type: ignore[return-value]


def _transform(
    quaternion: tuple[float, float, float, float], translation: tuple[float, float, float]
) -> RigidTransform:
    length = math.sqrt(sum(value * value for value in quaternion))
    quaternion = tuple(value / length for value in quaternion)  # type: ignore[assignment]
    if quaternion[0] < 0:
        quaternion = tuple(-value for value in quaternion)  # type: ignore[assignment]
    w, x, y, z = quaternion
    vector_length = math.sqrt(x * x + y * y + z * z)
    if vector_length <= _ANGULAR_TOLERANCE:
        return canonical_transform(RigidTransform(translation, (0.0, 0.0, 1.0), 0.0))
    axis = (x / vector_length, y / vector_length, z / vector_length)
    angle = math.degrees(2 * math.atan2(vector_length, max(-1.0, min(1.0, w))))
    if angle >= 180.0:
        angle -= 360.0
        axis = tuple(-value for value in axis)  # type: ignore[assignment]
    return canonical_transform(RigidTransform(translation, axis, angle))


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


def _identity_segment_candidates(
    reference: Any, scoped: dict[str, list[Candidate]]
) -> list[Candidate]:
    source = reference.source
    if not isinstance(source, SketchSegmentSource):
        return []
    return [
        candidate
        for candidate in scoped.get(reference.feature, [])
        if candidate.key.role == reference.role
        and isinstance(candidate.key.source, SketchSegmentSource)
        and (
            candidate.key.source.sketch,
            candidate.key.source.entity,
            candidate.key.source.segment,
        )
        == (source.sketch, source.entity, source.segment)
        and candidate.key.body == reference.body
    ]


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
    candidates = resolution.candidates
    if resolution.status is not Status.OK:
        candidates = _identity_segment_candidates(side.reference, scoped)
    if len(candidates) != 1:
        details = {
            **(
                resolution.error
                or {
                    "status": resolution.status.value,
                    "reference": side.reference.to_dict(),
                    "candidates": [],
                }
            ),
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
    face = candidates[0].entity
    expected = GeomType.PLANE if mate.kind == "planar" else GeomType.CYLINDER
    if face.geom_type is not expected or float(face.area) <= _LINEAR_TOLERANCE:
        raise AssemblyError(
            "MATE_GEOMETRY_UNSUPPORTED",
            f"assembly mate {mate.id} {side_name} face must be {expected.name.lower()} with non-zero area",
            mateId=mate.id,
            side=side_name,
            occurrenceId=side.occurrence_id,
            surfaceType=face.geom_type.name,
            area={"value": float(face.area), "unit": "mm^2"},
            availableRoles=face_role_catalog(scoped, side.reference.feature),
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


def _cylinder_axis(
    resolved_face: tuple[Face, Any], transform: RigidTransform
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    face, part_shape = resolved_face
    located = face.moved(locate_shape(part_shape, transform).location)
    axis = located.axis_of_rotation
    return tuple(axis.position), _unit(tuple(axis.direction))


def _solve_concentric(
    mate: Mate,
    moved_id: str,
    occurrences: dict[str, Occurrence],
    faces: dict[tuple[str, str], tuple[Face, Any]],
) -> Occurrence:
    assert mate.first is not None and mate.second is not None and mate.orientation is not None
    linear, angular = _concentric_residual(mate, occurrences, faces)
    if linear <= _LINEAR_TOLERANCE and angular <= _ANGULAR_TOLERANCE:
        return occurrences[moved_id]
    first_id, second_id = mate.first.occurrence_id, mate.second.occurrence_id
    moved_side = "first" if moved_id == first_id else "second"
    fixed_side = "second" if moved_side == "first" else "first"
    fixed_id = second_id if moved_side == "first" else first_id
    fixed_point, fixed_axis = _cylinder_axis(
        faces[(mate.id, fixed_side)], occurrences[fixed_id].transform
    )
    _, moved_axis = _cylinder_axis(faces[(mate.id, moved_side)], occurrences[moved_id].transform)
    target_axis = fixed_axis if mate.orientation == "aligned" else _scale(fixed_axis, -1)
    moved = occurrences[moved_id]
    rotated = _transform(
        _multiply(_between(moved_axis, target_axis), _quaternion(moved.transform)),
        moved.transform.translation,
    )
    moved_point, _ = _cylinder_axis(faces[(mate.id, moved_side)], rotated)
    delta = tuple(a - b for a, b in zip(fixed_point, moved_point, strict=True))
    radial = tuple(
        value - fixed_axis[index] * _dot(delta, fixed_axis) for index, value in enumerate(delta)
    )
    return replace(
        moved, transform=_transform(_quaternion(rotated), _add(rotated.translation, radial))
    )


def _solve_planar(
    mate: Mate,
    moved_id: str,
    occurrences: dict[str, Occurrence],
    faces: dict[tuple[str, str], tuple[Face, Any]],
) -> Occurrence:
    assert mate.first is not None and mate.second is not None and mate.orientation is not None
    first_id, second_id = mate.first.occurrence_id, mate.second.occurrence_id
    linear, angular = _planar_residual(mate, occurrences, faces)
    if linear <= _LINEAR_TOLERANCE and angular <= _ANGULAR_TOLERANCE:
        return occurrences[moved_id]
    first_transform = occurrences[first_id].transform
    second_transform = occurrences[second_id].transform
    first_center, first_normal = _plane(faces[(mate.id, "first")], first_transform)
    second_center, second_normal = _plane(faces[(mate.id, "second")], second_transform)
    moved = occurrences[moved_id]
    current_normal = first_normal if moved_id == first_id else second_normal
    if moved_id == second_id:
        target_normal = first_normal if mate.orientation == "aligned" else _scale(first_normal, -1)
    else:
        target_normal = (
            second_normal if mate.orientation == "aligned" else _scale(second_normal, -1)
        )
    rotated = _transform(
        _multiply(_between(current_normal, target_normal), _quaternion(moved.transform)),
        moved.transform.translation,
    )
    trial = dict(occurrences)
    trial[moved_id] = replace(moved, transform=rotated)
    first_center, first_normal = _plane(faces[(mate.id, "first")], trial[first_id].transform)
    second_center, _ = _plane(faces[(mate.id, "second")], trial[second_id].transform)
    residual = mate.offset - _dot(
        tuple(b - a for a, b in zip(first_center, second_center, strict=True)), first_normal
    )
    translation = _add(
        rotated.translation, _scale(first_normal, residual if moved_id == second_id else -residual)
    )
    return replace(moved, transform=_transform(_quaternion(rotated), translation))


def _planar_residual(
    mate: Mate,
    occurrences: dict[str, Occurrence],
    faces: dict[tuple[str, str], tuple[Face, Any]],
) -> tuple[float, float]:
    assert mate.first is not None and mate.second is not None and mate.orientation is not None
    first_center, first_normal = _plane(
        faces[(mate.id, "first")], occurrences[mate.first.occurrence_id].transform
    )
    second_center, second_normal = _plane(
        faces[(mate.id, "second")], occurrences[mate.second.occurrence_id].transform
    )
    target_normal = first_normal if mate.orientation == "aligned" else _scale(first_normal, -1)
    angular = _angle_between(target_normal, second_normal)
    linear = abs(
        _dot(tuple(b - a for a, b in zip(first_center, second_center, strict=True)), first_normal)
        - mate.offset
    )
    return linear, angular


def _concentric_residual(
    mate: Mate,
    occurrences: dict[str, Occurrence],
    faces: dict[tuple[str, str], tuple[Face, Any]],
) -> tuple[float, float]:
    assert mate.first is not None and mate.second is not None and mate.orientation is not None
    first_point, first_axis = _cylinder_axis(
        faces[(mate.id, "first")], occurrences[mate.first.occurrence_id].transform
    )
    second_point, second_axis = _cylinder_axis(
        faces[(mate.id, "second")], occurrences[mate.second.occurrence_id].transform
    )
    delta = tuple(b - a for a, b in zip(first_point, second_point, strict=True))
    cross = _cross(delta, first_axis)
    linear = math.sqrt(_dot(cross, cross))
    target_axis = first_axis if mate.orientation == "aligned" else _scale(first_axis, -1)
    angular = _angle_between(target_axis, second_axis)
    return linear, angular


def _residual(
    mate: Mate,
    occurrences: dict[str, Occurrence],
    faces: dict[tuple[str, str], tuple[Face, Any]],
) -> tuple[float, float]:
    return (
        _planar_residual(mate, occurrences, faces)
        if mate.kind == "planar"
        else _concentric_residual(mate, occurrences, faces)
    )


def _solve_mate(
    mate: Mate,
    moved_id: str,
    occurrences: dict[str, Occurrence],
    faces: dict[tuple[str, str], tuple[Face, Any]],
) -> Occurrence:
    return (
        _solve_planar(mate, moved_id, occurrences, faces)
        if mate.kind == "planar"
        else _solve_concentric(mate, moved_id, occurrences, faces)
    )


def _mate_faces(
    assembly: Assembly,
    resolved: dict[str, ResolvedOccurrence],
    provenance: dict[str, list[Candidate]],
) -> dict[tuple[str, str], tuple[Face, Any]]:
    faces = {}
    for mate in sorted(assembly.mates.values(), key=lambda item: item.id):
        if mate.kind in {"planar", "concentric"}:
            assert mate.first is not None and mate.second is not None
            faces[(mate.id, "first")] = _resolved_face(
                mate, "first", resolved[mate.first.occurrence_id], provenance
            )
            faces[(mate.id, "second")] = _resolved_face(
                mate, "second", resolved[mate.second.occurrence_id], provenance
            )
    return faces


def _mate_side_for_occurrence(mate: Mate, occurrence_id: str) -> str:
    assert mate.first is not None and mate.second is not None
    return "first" if mate.first.occurrence_id == occurrence_id else "second"


def _has_independent_seat(
    occurrence_id: str,
    concentric: list[Mate],
    planar: list[Mate],
    occurrence: Occurrence,
    faces: dict[tuple[str, str], tuple[Face, Any]],
) -> bool:
    axes = [
        _cylinder_axis(
            faces[(mate.id, _mate_side_for_occurrence(mate, occurrence_id))],
            occurrence.transform,
        )[1]
        for mate in concentric
    ]
    normals = [
        _plane(
            faces[(mate.id, _mate_side_for_occurrence(mate, occurrence_id))],
            occurrence.transform,
        )[1]
        for mate in planar
    ]
    return any(
        min(_angle_between(axis, normal), _angle_between(axis, _scale(normal, -1)))
        <= _ANGULAR_TOLERANCE
        for axis in axes
        for normal in normals
    )


def _classify_occurrence_restrictions(
    graph: MateGraph,
    occurrence_id: str,
    occurrence: Occurrence,
    faces: dict[tuple[str, str], tuple[Face, Any]],
) -> None:
    incident = [mate for _, mate in graph.adjacent[occurrence_id]]
    concentric = [mate for mate in incident if mate.kind == "concentric"]
    planar = [mate for mate in incident if mate.kind == "planar"]
    if not concentric or not planar or _has_independent_seat(
        occurrence_id, concentric, planar, occurrence, faces
    ):
        return
    raise AssemblyError(
        "MATE_CONFLICT",
        "a planar seat must be perpendicular to its concentric axis",
        mateIds=sorted(mate.id for mate in incident),
        occurrenceIds=sorted(
            {occurrence_id, *(other for other, _ in graph.adjacent[occurrence_id])}
        ),
        hint="select a seating plane whose normal is parallel to the concentric axis",
    )


def _classify_restrictions(
    graph: MateGraph,
    component: tuple[str, ...],
    grounded_ids: set[str],
    occurrences: dict[str, Occurrence],
    faces: dict[tuple[str, str], tuple[Face, Any]],
) -> None:
    for occurrence_id in component:
        if occurrence_id not in grounded_ids:
            _classify_occurrence_restrictions(
                graph, occurrence_id, occurrences[occurrence_id], faces
            )


def _reachable_without(
    graph: MateGraph, start: str, target: str, blocked: str
) -> bool:
    pending = [start]
    seen = {blocked, start}
    while pending:
        current = pending.pop()
        if current == target:
            return True
        for other, _ in graph.adjacent[current]:
            if other not in seen:
                seen.add(other)
                pending.append(other)
    return False


def _cyclic_occurrence_ids(graph: MateGraph, component: tuple[str, ...]) -> set[str]:
    cyclic = set()
    for occurrence_id in component:
        neighbors = sorted({other for other, _ in graph.adjacent[occurrence_id]})
        if any(
            _reachable_without(graph, first, second, occurrence_id)
            for index, first in enumerate(neighbors)
            for second in neighbors[index + 1 :]
        ):
            cyclic.add(occurrence_id)
    return cyclic


def _edge_is_cyclic(graph: MateGraph, first: str, second: str) -> bool:
    pending = [first]
    seen = {first}
    while pending:
        current = pending.pop()
        for other, _ in graph.adjacent[current]:
            if {current, other} == {first, second} or other in seen:
                continue
            if other == second:
                return True
            seen.add(other)
            pending.append(other)
    return False


def _cycle_mate_ids(graph: MateGraph, component: tuple[str, ...]) -> list[str]:
    return sorted(
        {
            mate.id
            for first in component
            for second, mate in graph.adjacent[first]
            if first < second and _edge_is_cyclic(graph, first, second)
        }
    )


def _reject_unsupported_coupling(
    graph: MateGraph, component: tuple[str, ...], grounded_ids: set[str]
) -> None:
    cyclic = _cyclic_occurrence_ids(graph, component)
    for occurrence_id in component:
        edges = graph.adjacent[occurrence_id]
        if (
            occurrence_id not in grounded_ids
            and occurrence_id in cyclic
            and len({other for other, _ in edges}) > 1
            and len({mate.kind for _, mate in edges}) > 1
        ):
            raise AssemblyError(
                "MATE_SOLVE_UNSUPPORTED",
                "a cyclic moving occurrence cannot use mixed mate kinds with different neighbors",
                mateIds=_cycle_mate_ids(graph, component),
                occurrenceIds=sorted(cyclic),
            )


def _tree_pairs(graph: MateGraph, anchor: str) -> list[tuple[str, str]]:
    pairs = []
    pending = [anchor]
    seen = {anchor}
    while pending:
        parent = pending.pop(0)
        for child in sorted({other for other, _ in graph.adjacent[parent]}):
            if child not in seen:
                seen.add(child)
                pending.append(child)
                pairs.append((parent, child))
    return pairs


def _pair_mates(graph: MateGraph, first: str, second: str) -> list[Mate]:
    return sorted(
        {mate.id: mate for other, mate in graph.adjacent[first] if other == second}.values(),
        key=lambda mate: ({"concentric": 0, "planar": 1}[mate.kind], mate.id),
    )


def _apply_tree_pair(
    graph: MateGraph,
    parent: str,
    child: str,
    provenance: dict[str, set[str]],
    occurrences: dict[str, Occurrence],
    faces: dict[tuple[str, str], tuple[Face, Any]],
    redundant: list[str],
) -> None:
    pair_mates = _pair_mates(graph, parent, child)
    applied_kinds = set()
    for mate in pair_mates:
        linear, angular = _residual(mate, occurrences, faces)
        if mate.kind in applied_kinds and max(linear, angular) <= _LINEAR_TOLERANCE:
            redundant.append(mate.id)
        occurrences[child] = _solve_mate(mate, child, occurrences, faces)
        applied_kinds.add(mate.kind)
    provenance[child] = provenance[parent] | {mate.id for mate in pair_mates}


def _solve_component(
    graph: MateGraph,
    component: tuple[str, ...],
    anchor: str,
    grounded_ids: set[str],
    occurrences: dict[str, Occurrence],
    faces: dict[tuple[str, str], tuple[Face, Any]],
    redundant: list[str],
) -> dict[str, set[str]]:
    _reject_unsupported_coupling(graph, component, grounded_ids)
    _classify_restrictions(graph, component, grounded_ids, occurrences, faces)
    provenance = {anchor: set()}
    tree_pairs = _tree_pairs(graph, anchor)
    for parent, child in tree_pairs:
        _apply_tree_pair(
            graph, parent, child, provenance, occurrences, faces, redundant
        )
    tree_edges = {frozenset(pair) for pair in tree_pairs}
    cycles = {}
    for first in component:
        for second, mate in graph.adjacent[first]:
            if first < second and frozenset((first, second)) not in tree_edges:
                cycles[mate.id] = provenance[first] ^ provenance[second] | {mate.id}
    return cycles


def _failed_residuals(
    mates: dict[str, Mate],
    occurrences: dict[str, Occurrence],
    faces: dict[tuple[str, str], tuple[Face, Any]],
) -> list[tuple[Mate, float, float]]:
    failed = []
    for mate in sorted(mates.values(), key=lambda item: item.id):
        linear, angular = _residual(mate, occurrences, faces)
        if linear > _LINEAR_TOLERANCE or angular > _ANGULAR_TOLERANCE:
            failed.append((mate, linear, angular))
    return failed


def _residual_culprit_ids(mate: Mate, mates: dict[str, Mate]) -> set[str]:
    assert mate.first is not None and mate.second is not None
    pair = {mate.first.occurrence_id, mate.second.occurrence_id}
    return {
        item.id
        for item in mates.values()
        if item.kind == mate.kind
        and item.first is not None
        and item.second is not None
        and {item.first.occurrence_id, item.second.occurrence_id} == pair
    }


def _validate_component_residuals(
    graph: MateGraph,
    component: tuple[str, ...],
    occurrences: dict[str, Occurrence],
    faces: dict[tuple[str, str], tuple[Face, Any]],
    cycles: dict[str, set[str]],
) -> None:
    mates = {
        mate.id: mate
        for occurrence_id in component
        for _, mate in graph.adjacent[occurrence_id]
    }
    failed = _failed_residuals(mates, occurrences, faces)
    if not failed:
        return
    mate, linear, angular = failed[0]
    culprit_ids = cycles.get(mate.id, _residual_culprit_ids(mate, mates))
    occurrence_ids = {
        side.occurrence_id
        for mate_id in culprit_ids
        for side in (
            graph.assembly.mates[mate_id].first,
            graph.assembly.mates[mate_id].second,
        )
        if side is not None
    }
    raise AssemblyError(
        "MATE_CONFLICT",
        f"assembly mate {mate.id} exceeds the solved residual tolerance",
        mateIds=sorted(culprit_ids),
        occurrenceIds=sorted(occurrence_ids),
        linearResidual={"value": linear, "unit": "mm"},
        angularResidual={"value": math.degrees(angular), "unit": "deg"},
    )


def _restriction_count(independent: list[Mate]) -> int:
    kinds = {mate.kind for mate in independent}
    if "concentric" in kinds:
        return 5 if "planar" in kinds else 4
    return 3 if "planar" in kinds else 0


def _occurrence_status(
    graph: MateGraph,
    occurrence_id: str,
    grounds: list[Mate],
    anchor: str,
    redundant: set[str],
) -> dict[str, Any]:
    grounded = any(mate.occurrence_id == occurrence_id for mate in grounds)
    connected = [mate for _, mate in graph.adjacent[occurrence_id]]
    independent = [mate for mate in connected if mate.id not in redundant]
    restrictions = _restriction_count(independent)
    if grounded:
        remaining, motions = 0, []
    elif occurrence_id == anchor:
        remaining, motions = 6, ["globalRigidMotion"]
    elif restrictions == 5:
        remaining, motions = 1, ["axisSpin"]
    elif restrictions == 4:
        remaining, motions = 2, ["axisTranslation", "axisSpin"]
    else:
        remaining, motions = 3, ["inPlaneX", "inPlaneY", "normalSpin"]
    incident = [mate.id for mate in connected]
    incident += [mate.id for mate in grounds if mate.occurrence_id == occurrence_id]
    return {
        "grounded": grounded,
        "remainingDof": remaining,
        "freeMotions": motions,
        "mateIds": sorted(incident),
    }


def _component_status(
    graph: MateGraph,
    component: tuple[str, ...],
    grounds: list[Mate],
    anchor: str,
    redundant: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    grounded_id = grounds[0].occurrence_id if grounds else None
    status = {
        occurrence_id: _occurrence_status(graph, occurrence_id, grounds, anchor, redundant)
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


def _solve_one_component(
    graph: MateGraph,
    component: tuple[str, ...],
    occurrences: dict[str, Occurrence],
    faces: dict[tuple[str, str], tuple[Face, Any]],
    redundant: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    grounds = graph.grounds(component)
    anchor = grounds[0].occurrence_id if grounds else component[0]
    assert anchor is not None
    grounded_ids = {mate.occurrence_id for mate in grounds if mate.occurrence_id is not None}
    cycles = _solve_component(
        graph, component, anchor, grounded_ids, occurrences, faces, redundant
    )
    _validate_component_residuals(graph, component, occurrences, faces, cycles)
    for mate_id, cycle_ids in cycles.items():
        other_kinds = {
            graph.assembly.mates[item].kind for item in cycle_ids - {mate_id}
        }
        if graph.assembly.mates[mate_id].kind in other_kinds:
            redundant.append(mate_id)
    return _component_status(graph, component, grounds, anchor, set(redundant))


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
        component_status, component_row = _solve_one_component(
            graph, component, occurrences, faces, redundant
        )
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
