"""Build one exact extrusion profile from solved constrained-sketch geometry."""

from __future__ import annotations

from dataclasses import dataclass
from math import cos, sin

from build123d import Edge, Face, Location, Wire
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeWire
from OCP.BRepCheck import BRepCheck_Analyzer

from .constrained_sketch import ConstrainedSketch
from .graph import GraphError
from .sketch_solver import SolvedArc, SolvedCircle, SolvedLine

_ENDPOINT_TOLERANCE = 1e-7
_AREA_TOLERANCE = 1e-9


@dataclass(frozen=True)
class ConstrainedProfile:
    id: str
    face: Face
    wire: Wire
    segments: dict[str, Edge]


@dataclass(frozen=True)
class _Curve:
    id: str
    solved: SolvedLine | SolvedArc

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


def _same_point(left: tuple[float, float], right: tuple[float, float]) -> bool:
    return (left[0] - right[0]) ** 2 + (left[1] - right[1]) ** 2 <= _ENDPOINT_TOLERANCE**2


def _vertex_index(vertices: list[tuple[float, float]], point: tuple[float, float]) -> int:
    for index, vertex in enumerate(vertices):
        if _same_point(vertex, point):
            return index
    vertices.append(point)
    return len(vertices) - 1


def _curve_vertices(curves: list[_Curve]) -> tuple[list[tuple[int, int]], list[list[int]]]:
    vertices: list[tuple[float, float]] = []
    endpoints: list[tuple[int, int]] = []
    incident: list[list[int]] = []
    for curve_index, curve in enumerate(curves):
        start = _vertex_index(vertices, curve.start)
        end = _vertex_index(vertices, curve.end)
        while len(incident) < len(vertices):
            incident.append([])
        endpoints.append((start, end))
        incident[start].append(curve_index)
        incident[end].append(curve_index)
    return endpoints, incident


def _ordered_loop(sketch_id: str, curves: list[_Curve]) -> list[_OrientedCurve]:
    endpoints, incident = _curve_vertices(curves)
    if any(len(curves_at_vertex) != 2 for curves_at_vertex in incident):
        _unsupported(sketch_id, "open")
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


def _wire_from_order(order: list[_OrientedCurve], location: Location) -> tuple[Wire, dict[str, Edge]]:
    builder = BRepBuilderAPI_MakeWire()
    segments: dict[str, Edge] = {}
    for item in order:
        builder.Add(_local_edge(item).moved(location).wrapped)
        segments[item.curve.id] = Edge(builder.Edge())
    return Wire(builder.Wire()), segments


def _checked_profile(sketch_id: str, wire: Wire, segments: dict[str, Edge]) -> ConstrainedProfile:
    face = Face(wire)
    if not BRepCheck_Analyzer(face.wrapped).IsValid() or face.area <= _AREA_TOLERANCE:
        _unsupported(sketch_id, "self_intersecting")
    return ConstrainedProfile(sketch_id, face, wire, segments)


def _circle_profile(sketch_id: str, circle: SolvedCircle, location: Location) -> ConstrainedProfile:
    local = Location((circle.center[0], circle.center[1], 0))
    wire = Wire.make_circle(circle.radius).moved(location * local)
    return _checked_profile(sketch_id, wire, {"circle": wire.edges()[0]})


def _profiles_are_nested(left: ConstrainedProfile, right: ConstrainedProfile) -> bool:
    intersection = left.face.intersect(right.face)
    overlap = sum(face.area for face in intersection or [])
    return abs(overlap - min(left.face.area, right.face.area)) <= _AREA_TOLERANCE


def build_constrained_profile(model: ConstrainedSketch, sketch_id: str, location: Location) -> ConstrainedProfile:
    """Return one exact solved loop, or a structured unsupported-profile error."""
    if model.solved is None:
        raise RuntimeError(f"constrained sketch {sketch_id} has no solved geometry")
    solved = model.solved.entities
    curves = sorted(
        (
            _Curve(entity_id, entity)
            for entity_id, entity in solved.items()
            if isinstance(entity, (SolvedLine, SolvedArc)) and not entity.construction
        ),
        key=lambda item: item.id,
    )
    circles = sorted(
        (
            entity
            for entity in solved.values()
            if isinstance(entity, SolvedCircle) and not entity.construction
        ),
        key=lambda item: (item.center, item.radius),
    )
    profiles: list[ConstrainedProfile] = []
    if curves:
        order = _ordered_loop(sketch_id, curves)
        wire, segments = _wire_from_order(order, location)
        profiles.append(_checked_profile(sketch_id, wire, segments))
    profiles.extend(_circle_profile(sketch_id, circle, location) for circle in circles)
    if not profiles:
        _unsupported(sketch_id, "open")
    if len(profiles) > 1:
        reason = "nested" if any(
            _profiles_are_nested(left, right)
            for index, left in enumerate(profiles)
            for right in profiles[index + 1 :]
        ) else "multiple"
        _unsupported(sketch_id, reason)
    return profiles[0]
