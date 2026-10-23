"""Build one exact extrusion profile from solved constrained-sketch geometry."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import cos, sin, sqrt

from build123d import Edge, Face, Location, Wire
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeWire

from .constrained_sketch import ConstrainedSketch
from .graph import GraphError
from .profile_validation import InvalidProfile, checked_face, profile_tolerance, same_point
from .sketch_solver import SolvedArc, SolvedCircle, SolvedLine


@dataclass(frozen=True)
class ConstrainedProfile:
    id: str
    face: Face
    wire: Wire
    segments: dict[str, Edge]
    segment_topology: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class _Curve:
    id: str
    solved: SolvedLine | SolvedArc
    topology: tuple[str, ...]

    @property
    def start(self) -> tuple[float, float]:
        return self.solved.start

    @property
    def end(self) -> tuple[float, float]:
        return self.solved.end


@dataclass(frozen=True)
class _OrientedCurve:
    curve: _Curve
    forward: bool


def _unsupported(sketch_id: str, reason: str, loops: list[str] | None = None) -> None:
    details: dict[str, object] = {"sketch": sketch_id, "reason": reason}
    if loops is not None:
        details["loops"] = sorted(loops)
    raise GraphError(
        "SKETCH_PROFILE_UNSUPPORTED",
        f"constrained sketch {sketch_id} has unsupported {reason.replace('_', ' ')} loops",
        **details,
    )


def _vertex_index(
    vertices: list[tuple[float, float]], point: tuple[float, float], tolerance: float
) -> int:
    for index, vertex in enumerate(vertices):
        if same_point(vertex, point, tolerance):
            return index
    vertices.append(point)
    return len(vertices) - 1


def _curve_vertices(
    curves: list[_Curve], tolerance: float
) -> tuple[list[tuple[int, int]], list[list[int]]]:
    vertices: list[tuple[float, float]] = []
    endpoints: list[tuple[int, int]] = []
    incident: list[list[int]] = []
    for curve_index, curve in enumerate(curves):
        start = _vertex_index(vertices, curve.start, tolerance)
        end = _vertex_index(vertices, curve.end, tolerance)
        while len(incident) < len(vertices):
            incident.append([])
        endpoints.append((start, end))
        incident[start].append(curve_index)
        incident[end].append(curve_index)
    return endpoints, incident


def _ordered_loop(
    sketch_id: str, curves: list[_Curve], tolerance: float
) -> list[_OrientedCurve]:
    endpoints, incident = _curve_vertices(curves, tolerance)
    start_curve = min(range(len(curves)), key=lambda index: curves[index].id)
    start_vertex, current_vertex = endpoints[start_curve]
    ordered = [_OrientedCurve(curves[start_curve], True)]
    used = {start_curve}
    while current_vertex != start_vertex:
        choices = sorted(
            (index for index in incident[current_vertex] if index not in used),
            key=lambda index: curves[index].id,
        )
        if not choices:
            _unsupported(sketch_id, "open")
        index = choices[0]
        first, second = endpoints[index]
        forward = first == current_vertex
        current_vertex = second if forward else first
        ordered.append(_OrientedCurve(curves[index], forward))
        used.add(index)
    if len(used) != len(curves):
        _unsupported(sketch_id, "multiple")
    return ordered


def _curve_components(
    sketch_id: str, curves: list[_Curve], tolerance: float
) -> list[list[_Curve]]:
    endpoints, incident = _curve_vertices(curves, tolerance)
    if any(len(curves_at_vertex) != 2 for curves_at_vertex in incident):
        _unsupported(sketch_id, "open")
    remaining = set(range(len(curves)))
    components: list[list[_Curve]] = []
    while remaining:
        pending = [min(remaining, key=lambda index: curves[index].id)]
        component: set[int] = set()
        while pending:
            index = pending.pop()
            if index in component:
                continue
            component.add(index)
            for vertex in endpoints[index]:
                pending.extend(incident[vertex])
        remaining -= component
        components.append([curves[index] for index in sorted(component)])
    return components


def _local_edge(item: _OrientedCurve) -> Edge:
    solved = item.curve.solved
    if isinstance(solved, SolvedLine):
        edge = Edge.make_line((*solved.start, 0), (*solved.end, 0))
    else:
        middle_angle = (solved.start_angle + solved.end_angle) / 2
        middle = (
            solved.center[0] + solved.radius * cos(middle_angle),
            solved.center[1] + solved.radius * sin(middle_angle),
            0,
        )
        edge = Edge.make_three_point_arc((*solved.start, 0), middle, (*solved.end, 0))
    return edge if item.forward else edge.reversed()


def _wire_from_order(
    order: list[_OrientedCurve], location: Location
) -> tuple[Wire, dict[str, Edge], dict[str, tuple[str, ...]]]:
    builder = BRepBuilderAPI_MakeWire()
    segments: dict[str, Edge] = {}
    topology: dict[str, tuple[str, ...]] = {}
    for item in order:
        builder.Add(_local_edge(item).moved(location).wrapped)
        segments[item.curve.id] = Edge(builder.Edge())
        topology[item.curve.id] = item.curve.topology
    return Wire(builder.Wire()), segments, topology


def _checked_profile(
    sketch_id: str,
    wire: Wire,
    segments: dict[str, Edge],
    topology: dict[str, tuple[str, ...]],
    tolerance: float,
) -> ConstrainedProfile:
    try:
        face = checked_face(wire, tolerance)
    except InvalidProfile as error:
        _unsupported(sketch_id, error.reason)
    return ConstrainedProfile(sketch_id, face, wire, segments, topology)


def _circle_profile(
    sketch_id: str,
    circle_id: str,
    circle: SolvedCircle,
    topology: tuple[str, ...],
    location: Location,
    tolerance: float,
) -> ConstrainedProfile:
    local = Location((circle.center[0], circle.center[1], 0))
    wire = Wire.make_circle(circle.radius).moved(location * local)
    return _checked_profile(
        sketch_id, wire, {circle_id: wire.edges()[0]}, {circle_id: topology}, tolerance
    )


def _profile_relation(
    left: ConstrainedProfile, right: ConstrainedProfile, tolerance: float
) -> str:
    intersection = left.face.intersect(right.face)
    overlap = sum(face.area for face in intersection or [])
    area_tolerance = max(
        tolerance**2,
        tolerance * sqrt(min(left.face.area, right.face.area)),
    )
    left_inside = abs(overlap - left.face.area) <= area_tolerance
    right_inside = abs(overlap - right.face.area) <= area_tolerance
    boundaries_touch = left.wire.distance_to(right.wire) <= tolerance
    if left_inside != right_inside:
        if boundaries_touch:
            return "touching"
        return "left_in_right" if left_inside else "right_in_left"
    if left_inside and right_inside:
        return "ambiguous_containment"
    if overlap <= area_tolerance:
        return "touching" if boundaries_touch else "disjoint"
    return "crossing"


def _solved_curves(
    solved: Mapping[str, object], topology: Mapping[str, tuple[str, ...]]
) -> list[_Curve]:
    return sorted(
        (
            _Curve(entity_id, entity, topology[entity_id])
            for entity_id, entity in solved.items()
            if isinstance(entity, (SolvedLine, SolvedArc)) and not entity.construction
        ),
        key=lambda item: item.id,
    )


def _solved_circles(solved: Mapping[str, object]) -> list[tuple[str, SolvedCircle]]:
    return sorted(
        (
            (entity_id, entity)
            for entity_id, entity in solved.items()
            if isinstance(entity, SolvedCircle) and not entity.construction
        ),
        key=lambda item: item[0],
    )


def _loop_name(profile: ConstrainedProfile) -> str:
    return ",".join(sorted(profile.segments))


def _containment(
    sketch_id: str, profiles: list[ConstrainedProfile], tolerance: float
) -> list[list[int]]:
    containers: list[list[int]] = [[] for _ in profiles]
    rejected = {"touching", "crossing", "ambiguous_containment"}
    for left_index, left in enumerate(profiles):
        for right_index in range(left_index + 1, len(profiles)):
            right = profiles[right_index]
            relation = _profile_relation(left, right, tolerance)
            if relation in rejected:
                _unsupported(sketch_id, relation, [_loop_name(left), _loop_name(right)])
            if relation == "left_in_right":
                containers[left_index].append(right_index)
            elif relation == "right_in_left":
                containers[right_index].append(left_index)
    return containers


def _profile_hierarchy(
    sketch_id: str,
    profiles: list[ConstrainedProfile],
    containers: list[list[int]],
) -> tuple[int, list[ConstrainedProfile]]:
    outer_indices = [index for index, items in enumerate(containers) if not items]
    if len(outer_indices) != 1:
        _unsupported(
            sketch_id,
            "multiple_outer",
            [_loop_name(profiles[index]) for index in outer_indices],
        )
    if any(len(items) > 1 for items in containers):
        _unsupported(sketch_id, "island", [_loop_name(profile) for profile in profiles])

    outer_index = outer_indices[0]
    holes = [
        profile
        for index, profile in enumerate(profiles)
        if index != outer_index and containers[index] == [outer_index]
    ]
    if len(holes) != len(profiles) - 1:
        _unsupported(
            sketch_id,
            "ambiguous_containment",
            [_loop_name(profile) for profile in profiles],
        )
    return outer_index, holes


def _combine_profiles(
    sketch_id: str, profiles: list[ConstrainedProfile], tolerance: float
) -> ConstrainedProfile:
    containers = _containment(sketch_id, profiles, tolerance)
    outer_index, holes = _profile_hierarchy(sketch_id, profiles, containers)
    outer = profiles[outer_index]
    try:
        face = checked_face(outer.wire, tolerance, [hole.wire for hole in holes])
    except InvalidProfile as error:
        _unsupported(sketch_id, error.reason, [_loop_name(profile) for profile in profiles])
    segments = {item: edge for profile in profiles for item, edge in profile.segments.items()}
    topology = {
        item: value
        for profile in profiles
        for item, value in profile.segment_topology.items()
    }
    return ConstrainedProfile(sketch_id, face, outer.wire, segments, topology)


def _topology(model: ConstrainedSketch) -> dict[str, tuple[str, ...]]:
    return {
        **{line.id: (line.start, line.end) for line in model.lines},
        **{arc.id: (arc.center, arc.start, arc.end) for arc in model.arcs},
        **{circle.id: (circle.center,) for circle in model.circles},
    }


def _profile_points(solved: Mapping[str, object]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for entity in solved.values():
        if isinstance(entity, (SolvedLine, SolvedArc)) and not entity.construction:
            points.extend((entity.start, entity.end))
        elif isinstance(entity, SolvedCircle) and not entity.construction:
            points.extend(
                (
                    (entity.center[0] - entity.radius, entity.center[1] - entity.radius),
                    (entity.center[0] + entity.radius, entity.center[1] + entity.radius),
                )
            )
    return points


def _validate_geometry(
    sketch_id: str,
    curves: list[_Curve],
    circles: list[tuple[str, SolvedCircle]],
    tolerance: float,
) -> None:
    for curve in curves:
        if same_point(curve.start, curve.end, tolerance):
            _unsupported(sketch_id, "degenerate")
        if isinstance(curve.solved, SolvedArc) and curve.solved.radius <= tolerance:
            _unsupported(sketch_id, "degenerate")
    if any(circle.radius <= tolerance for _, circle in circles):
        _unsupported(sketch_id, "degenerate")


def build_constrained_profile(model: ConstrainedSketch, sketch_id: str, location: Location) -> ConstrainedProfile:
    """Return one exact solved loop, or a structured unsupported-profile error.

    Degenerate means a solved line or arc collapsed within OCCT's scale-aware precision.
    """
    if model.solved is None:
        raise RuntimeError(f"constrained sketch {sketch_id} has no solved geometry")
    solved = model.solved.entities
    topology = _topology(model)
    curves = _solved_curves(solved, topology)
    circles = _solved_circles(solved)
    tolerance = profile_tolerance(_profile_points(solved))
    _validate_geometry(sketch_id, curves, circles, tolerance)
    profiles: list[ConstrainedProfile] = []
    for component in _curve_components(sketch_id, curves, tolerance) if curves else []:
        order = _ordered_loop(sketch_id, component, tolerance)
        wire, segments, segment_topology = _wire_from_order(order, location)
        profiles.append(
            _checked_profile(sketch_id, wire, segments, segment_topology, tolerance)
        )
    profiles.extend(
        _circle_profile(
            sketch_id, circle_id, circle, topology[circle_id], location, tolerance
        )
        for circle_id, circle in circles
    )
    if not profiles:
        _unsupported(sketch_id, "open")
    if len(profiles) == 1:
        return profiles[0]
    return _combine_profiles(sketch_id, profiles, tolerance)
