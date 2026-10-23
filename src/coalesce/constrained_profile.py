"""Build one exact extrusion profile from solved constrained-sketch geometry."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import cos, sin

from build123d import Edge, Face, Location, Wire
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeWire
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.Precision import Precision

from .constrained_sketch import ConstrainedSketch
from .graph import GraphError
from .sketch_solver import SolvedArc, SolvedCircle, SolvedLine

_RELATIVE_TOLERANCE = 1e-9


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


def _unsupported(sketch_id: str, reason: str) -> None:
    raise GraphError(
        "SKETCH_PROFILE_UNSUPPORTED",
        f"constrained sketch {sketch_id} has an unsupported {reason.replace('_', ' ')} profile",
        sketch=sketch_id,
        reason=reason,
    )


def _same_point(
    left: tuple[float, float], right: tuple[float, float], tolerance: float
) -> bool:
    return (left[0] - right[0]) ** 2 + (left[1] - right[1]) ** 2 <= tolerance**2


def _vertex_index(
    vertices: list[tuple[float, float]], point: tuple[float, float], tolerance: float
) -> int:
    for index, vertex in enumerate(vertices):
        if _same_point(vertex, point, tolerance):
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
    face = Face(wire)
    if not BRepCheck_Analyzer(face.wrapped).IsValid():
        _unsupported(sketch_id, "self_intersecting")
    if face.area <= tolerance**2:
        _unsupported(sketch_id, "degenerate")
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


def _profiles_are_nested(
    left: ConstrainedProfile, right: ConstrainedProfile, area_tolerance: float
) -> bool:
    intersection = left.face.intersect(right.face)
    overlap = sum(face.area for face in intersection or [])
    return abs(overlap - min(left.face.area, right.face.area)) <= area_tolerance


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


def _multiple_profile_reason(
    profiles: list[ConstrainedProfile], area_tolerance: float
) -> str:
    nested = any(
        _profiles_are_nested(left, right, area_tolerance)
        for index, left in enumerate(profiles)
        for right in profiles[index + 1 :]
    )
    return "nested" if nested else "multiple"


def _topology(model: ConstrainedSketch) -> dict[str, tuple[str, ...]]:
    return {
        **{line.id: (line.start, line.end) for line in model.lines},
        **{arc.id: (arc.center, arc.start, arc.end) for arc in model.arcs},
        **{circle.id: (circle.center,) for circle in model.circles},
    }


def _profile_tolerance(solved: Mapping[str, object]) -> float:
    points = [
        point
        for entity in solved.values()
        if isinstance(entity, (SolvedLine, SolvedArc)) and not entity.construction
        for point in (entity.start, entity.end)
    ]
    points.extend(
        (circle.center[0] - circle.radius, circle.center[1] - circle.radius)
        for circle in solved.values()
        if isinstance(circle, SolvedCircle) and not circle.construction
    )
    points.extend(
        (circle.center[0] + circle.radius, circle.center[1] + circle.radius)
        for circle in solved.values()
        if isinstance(circle, SolvedCircle) and not circle.construction
    )
    extent = max(
        max(point[axis] for point in points) - min(point[axis] for point in points)
        for axis in (0, 1)
    ) if points else 0.0
    return max(extent * _RELATIVE_TOLERANCE, Precision.Confusion_s())


def _validate_geometry(
    sketch_id: str,
    curves: list[_Curve],
    circles: list[tuple[str, SolvedCircle]],
    tolerance: float,
) -> None:
    for curve in curves:
        if _same_point(curve.start, curve.end, tolerance):
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
    tolerance = _profile_tolerance(solved)
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
    if len(profiles) > 1:
        _unsupported(sketch_id, _multiple_profile_reason(profiles, tolerance**2))
    return profiles[0]
