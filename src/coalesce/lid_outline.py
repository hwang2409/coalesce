from __future__ import annotations

from dataclasses import dataclass

from build123d import Edge, Face, GeomType, Shape, Vector, Wire

from .graph import GraphError, LidOutlineGeometry, LidOutlineSegment
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
    return LidOutlineSegment(
        kind="line" if edge.geom_type is GeomType.LINE else "arc",
        start=point(0),
        midpoint=point(0.5),
        end=point(1),
        source=source,
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


def _edge(segment: LidOutlineSegment) -> Edge:
    start, midpoint, end = (
        Vector(point[0], point[1], 0) for point in (segment.start, segment.midpoint, segment.end)
    )
    if segment.kind == "line":
        return Edge.make_line(start, end)
    return Edge.make_three_point_arc(start, midpoint, end)


def outline_profile(geometry: LidOutlineGeometry) -> OutlineProfile:
    edges = [(segment.source, _edge(segment)) for segment in geometry.segments]
    return OutlineProfile(Wire([edge for _, edge in edges]), edges)


def outline_difference(first: LidOutlineGeometry, second: LidOutlineGeometry) -> dict[str, float]:
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
    return {
        "area": abs(first_face.area - second_face.area),
        "bbox": bbox_difference,
    }
