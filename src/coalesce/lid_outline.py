from __future__ import annotations

import math
from dataclasses import dataclass

from build123d import AngularDirection, Edge, Face, GeomType, Plane, Shape, Vector, Wire

from .graph import GraphError
from .lid_outline_compare import LidOutlineGeometry, LidOutlineSegment
from .references import Candidate


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
        return _geometry(segments)
    except (RuntimeError, ValueError) as error:
        raise GraphError(
            "AMBIGUOUS",
            f"lid outline source {feature} does not form one closed loop",
            feature=feature,
            parameter="outlineSource",
        ) from error


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


def _geometry(segments: tuple[LidOutlineSegment, ...]) -> LidOutlineGeometry:
    face = Face(Face(Wire([_edge(segment) for segment in segments])).outer_wire())
    box = face.bounding_box()
    return LidOutlineGeometry(
        segments,
        face.area,
        (box.min.X, box.min.Y, box.max.X, box.max.Y),
    )


def outline_geometry(face: Face) -> LidOutlineGeometry:
    segments = tuple(_segment(edge, "", "lid") for edge in face.outer_wire().edges())
    return _geometry(segments)
