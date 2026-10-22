from __future__ import annotations

import math
from dataclasses import dataclass

from build123d import AngularDirection, Edge, Face, GeomType, Plane, Shape, Vector, Wire

from .graph import GraphError
from .references import Candidate


@dataclass(frozen=True)
class LidOutlineSegment:
    kind: str
    start: tuple[float, float, float]
    midpoint: tuple[float, float, float]
    end: tuple[float, float, float]
    source: str
    center: tuple[float, float, float] | None = None
    radius: float | None = None


@dataclass(frozen=True)
class LidOutlineGeometry:
    segments: tuple[LidOutlineSegment, ...]


@dataclass(frozen=True)
class LidOutlineComparison:
    matches: bool
    reason: str | None
    area_difference: float
    bbox_difference: float


@dataclass(frozen=True)
class OutlineProfile:
    wire: Wire
    edges: list[tuple[str, Shape]]


def _source(edge: Edge, candidates: list[Candidate], feature: str) -> str:
    matches = [
        item
        for item in candidates
        if isinstance(item.entity, Edge) and item.entity.is_same(edge)
    ]
    if not matches:
        raise GraphError(
            "EMPTY",
            f"lid outline source {feature} has an edge without provenance",
            feature=feature,
            parameter="outlineSource",
        )
    allowed = [
        item
        for item in matches
        if "rim" in item.key.role
        and ("outer_wall" in item.key.role or "blend(" in item.key.role)
    ]
    if not allowed:
        raise GraphError(
            "SPLIT",
            f"lid outline source {feature} does not have one intact outer wall loop",
            feature=feature,
            parameter="outlineSource",
            candidates=[item.key.role for item in matches],
        )
    if len(allowed) != 1:
        raise GraphError(
            "AMBIGUOUS",
            f"lid outline source {feature} has ambiguous outer wall provenance",
            feature=feature,
            parameter="outlineSource",
            candidates=[item.key.role for item in matches],
        )
    key = allowed[0].key
    return f"{key.feature}:{key.role}:{key.source!r}"


def _segment(edge: Edge, source: str, feature: str) -> LidOutlineSegment:
    if edge.geom_type not in {GeomType.LINE, GeomType.CIRCLE}:
        raise GraphError(
            "AMBIGUOUS",
            f"lid outline source {feature} contains unsupported analytic geometry",
            feature=feature,
            parameter="outlineSource",
            geometry=edge.geom_type.name,
        )
    def point(position: float) -> tuple[float, float, float]:
        value = edge.position_at(position)
        return (value.X, value.Y, value.Z)

    circular = edge.geom_type is GeomType.CIRCLE
    center = edge.arc_center if circular else None
    return LidOutlineSegment(
        kind="arc" if circular else "line",
        start=point(0),
        midpoint=point(0.5),
        end=point(1),
        source=source,
        center=(center.X, center.Y, center.Z) if center is not None else None,
        radius=edge.radius if circular else None,
    )


def capture_lid_outline(feature: str, candidates: list[Candidate]) -> LidOutlineGeometry:
    rims = [
        item.entity
        for item in candidates
        if item.key.role == "rim" and isinstance(item.entity, Face)
    ]
    if not rims:
        raise GraphError(
            "EMPTY",
            f"lid outline source {feature} has no rim face",
            feature=feature,
            parameter="outlineSource",
        )
    if len(rims) != 1:
        raise GraphError(
            "SPLIT",
            f"lid outline source {feature} has a split rim face",
            feature=feature,
            parameter="outlineSource",
            count=len(rims),
        )
    rim = rims[0]
    box = rim.bounding_box()
    if rim.geom_type is not GeomType.PLANE or box.max.Z - box.min.Z > 1e-7:
        raise GraphError(
            "AMBIGUOUS",
            f"lid outline source {feature} rim is not planar at the lid plane",
            feature=feature,
            parameter="outlineSource",
        )
    edges = rim.outer_wire().edges()
    segments = tuple(_segment(edge, _source(edge, candidates, feature), feature) for edge in edges)
    try:
        Face(Wire([_edge(segment) for segment in segments]))
    except (RuntimeError, ValueError) as error:
        raise GraphError(
            "AMBIGUOUS",
            f"lid outline source {feature} does not form one closed loop",
            feature=feature,
            parameter="outlineSource",
        ) from error
    return LidOutlineGeometry(segments)


def _angle(point: tuple[float, float, float], center: tuple[float, float, float]) -> float:
    return math.degrees(math.atan2(point[1] - center[1], point[0] - center[0]))


def _edge(segment: LidOutlineSegment) -> Edge:
    start, midpoint, end = (
        Vector(point[0], point[1], 0) for point in (segment.start, segment.midpoint, segment.end)
    )
    if segment.kind == "line":
        return Edge.make_line(start, end)
    if segment.center is None or segment.radius is None:
        return Edge.make_three_point_arc(start, midpoint, end)
    start_angle = _angle(segment.start, segment.center)
    midpoint_angle = _angle(segment.midpoint, segment.center)
    end_angle = _angle(segment.end, segment.center)
    direction = (
        AngularDirection.COUNTER_CLOCKWISE
        if (midpoint_angle - start_angle) % 360 <= (end_angle - start_angle) % 360
        else AngularDirection.CLOCKWISE
    )
    return Edge.make_circle(
        segment.radius,
        Plane((segment.center[0], segment.center[1], 0)),
        start_angle,
        end_angle,
        direction,
    )


def outline_profile(geometry: LidOutlineGeometry) -> OutlineProfile:
    wire = Wire([_edge(segment) for segment in geometry.segments])
    face_wire = Face(wire).outer_wire()
    edges = list(zip((segment.source for segment in geometry.segments), face_wire.edges()))
    return OutlineProfile(face_wire, edges)


def outline_geometry(face: Face) -> LidOutlineGeometry:
    return LidOutlineGeometry(
        tuple(_segment(edge, "", "lid") for edge in face.outer_wire().edges())
    )


def _outline_differences(
    first: LidOutlineGeometry | None, second: LidOutlineGeometry | None
) -> tuple[float, float]:
    if first is None or second is None:
        return float("inf"), float("inf")
    first_face = Face(outline_profile(first).wire)
    second_face = Face(outline_profile(second).wire)
    first_box = first_face.bounding_box()
    second_box = second_face.bounding_box()
    bbox_difference = max(
        abs(left - right)
        for left, right in zip(
            (first_box.min.X, first_box.min.Y, first_box.max.X, first_box.max.Y),
            (second_box.min.X, second_box.min.Y, second_box.max.X, second_box.max.Y),
            strict=True,
        )
    )
    return abs(first_face.area - second_face.area), bbox_difference


def _topology_difference(
    first: LidOutlineGeometry, second: LidOutlineGeometry
) -> str | None:
    if len(first.segments) != len(second.segments):
        return f"segment count differs: {len(first.segments)} != {len(second.segments)}"
    for index, (left, right) in enumerate(zip(first.segments, second.segments, strict=True)):
        if left.kind not in {"line", "arc"} or right.kind not in {"line", "arc"}:
            return f"segment {index} has unsupported type"
        if left.kind != right.kind:
            return f"segment {index} type differs: {left.kind} != {right.kind}"
        if left.kind == "arc" and (
            left.radius is None
            or right.radius is None
            or abs(left.radius - right.radius) > 1e-6
        ):
            return f"segment {index} arc radius differs"
    return None


def _comparison(
    matches: bool,
    reason: str | None,
    first: LidOutlineGeometry | None,
    second: LidOutlineGeometry | None,
) -> LidOutlineComparison:
    area, bbox = _outline_differences(first, second)
    return LidOutlineComparison(matches, reason, area, bbox)


def compare_lid_outlines(
    outline_source: str | None,
    current_source: str,
    source: LidOutlineGeometry | None,
    current: LidOutlineGeometry | None,
    *,
    legacy_stale: bool = False,
) -> LidOutlineComparison:
    if outline_source is None and not legacy_stale:
        return LidOutlineComparison(True, None, 0.0, 0.0)
    if outline_source is not None and outline_source != current_source:
        reason = f"outline source is stale: {outline_source} != {current_source}"
        return _comparison(False, reason, source, current)
    if source is None:
        return _comparison(False, "outline source does not resolve", source, current)
    if current is None:
        return _comparison(False, "current outline source does not resolve", source, current)
    if difference := _topology_difference(source, current):
        return _comparison(False, difference, source, current)
    area, bbox = _outline_differences(source, current)
    if area > 1e-6:
        return LidOutlineComparison(False, "outline area differs", area, bbox)
    if bbox > 1e-6:
        return LidOutlineComparison(False, "outline bounding box differs", area, bbox)
    return LidOutlineComparison(True, None, area, bbox)
