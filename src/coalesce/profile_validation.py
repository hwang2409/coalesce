"""Build and validate exact sketch profiles at one shared seam."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise

from build123d import Edge, Face, Location, Wire
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeWire
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.Precision import Precision
from OCP.Standard import Standard_Failure
from OCP.StdFail import StdFail_NotDone

_RELATIVE_TOLERANCE = 1e-9


class InvalidProfile(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ExactProfile:
    face: Face
    wire: Wire
    segments: dict[str, Edge]
    segment_topology: dict[str, tuple[str, ...]]


def profile_tolerance(points: list[tuple[float, float]]) -> float:
    if not points:
        return Precision.Confusion_s()
    extent = max(
        max(point[axis] for point in points) - min(point[axis] for point in points)
        for axis in (0, 1)
    )
    return max(extent * _RELATIVE_TOLERANCE, Precision.Confusion_s())


def same_point(
    left: tuple[float, float], right: tuple[float, float], tolerance: float
) -> bool:
    return (left[0] - right[0]) ** 2 + (left[1] - right[1]) ** 2 <= tolerance**2


def checked_face(
    wire: Wire, tolerance: float, inner_wires: Iterable[Wire] = ()
) -> Face:
    try:
        face = Face(wire, list(inner_wires))
    except (Standard_Failure, StdFail_NotDone, ValueError) as error:
        raise InvalidProfile("self_intersecting") from error
    if not BRepCheck_Analyzer(wire.wrapped).IsValid() or not BRepCheck_Analyzer(
        face.wrapped
    ).IsValid():
        raise InvalidProfile("self_intersecting")
    if abs(face.area) <= tolerance**2:
        raise InvalidProfile("degenerate")
    return face


def build_outline_profile(
    kind: str,
    points: tuple[tuple[float, float], ...],
    point_ids: tuple[str, ...],
    location: Location,
    *,
    point_ids_explicit: bool = True,
) -> ExactProfile:
    tolerance = profile_tolerance(list(points))
    closed_points = (*points, points[0])
    if any(same_point(left, right, tolerance) for left, right in pairwise(closed_points)):
        raise InvalidProfile("degenerate")
    origin, direction = points[:2]
    if all(
        abs(
            (direction[0] - origin[0]) * (point[1] - origin[1])
            - (direction[1] - origin[1]) * (point[0] - origin[0])
        )
        <= tolerance**2
        for point in points[2:]
    ):
        raise InvalidProfile("degenerate")

    try:
        if kind == "spline":
            edge = Edge.make_spline([(*point, 0) for point in points], periodic=True).moved(
                location
            )
            wire = Wire(edge)
            return ExactProfile(
                checked_face(wire, tolerance), wire, {"spline": edge}, {"spline": ()}
            )

        builder = BRepBuilderAPI_MakeWire()
        segments: dict[str, Edge] = {}
        topology: dict[str, tuple[str, ...]] = {}
        closed_ids = (*point_ids, point_ids[0])
        implicit_topology = (
            "__implicit__",
            *(f"{float(x).hex()}:{float(y).hex()}" for x, y in points),
        )
        for (start, end), (start_id, end_id) in zip(
            pairwise(closed_points), pairwise(closed_ids), strict=True
        ):
            builder.Add(Edge.make_line((*start, 0), (*end, 0)).moved(location).wrapped)
            role = f"seg-{start_id}-{end_id}"
            if role in segments:
                raise InvalidProfile("duplicate_segment_role")
            segments[role] = Edge(builder.Edge())
            topology[role] = (
                (start_id, end_id) if point_ids_explicit else implicit_topology
            )
        wire = Wire(builder.Wire())
        return ExactProfile(checked_face(wire, tolerance), wire, segments, topology)
    except InvalidProfile:
        raise
    except (Standard_Failure, StdFail_NotDone, ValueError) as error:
        raise InvalidProfile("self_intersecting") from error
