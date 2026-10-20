"""Exact board-substrate regeneration from a persisted KiCad mechanics snapshot."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from build123d import Edge, Face, Location, Shape, Wire
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeWire
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.gp import gp_Vec

from .graph import BoardImport, GraphError
from .provenance import candidate, derive_edge_candidates
from .references import BodySource, Candidate


@dataclass(frozen=True)
class PcbResult:
    shape: Shape
    candidates: list[Candidate]


def _point(value: list[float]) -> tuple[float, float, float]:
    return value[0], value[1], 0.0


def _wire(edges: list[Edge]) -> tuple[Wire, list[Edge]]:
    builder = BRepBuilderAPI_MakeWire()
    built = []
    for edge in edges:
        builder.Add(edge.wrapped)
        built.append(Edge(builder.Edge()))
    if not builder.IsDone():
        raise GraphError("INVALID_BOARD", "board boundary could not be joined into a wire")
    return Wire(builder.Wire()), built


def _loop_wire(loop: dict[str, Any]) -> tuple[Wire, list[tuple[str, Edge]]]:
    if not loop.get("closed"):
        raise GraphError(
            "INVALID_BOARD",
            "board outline contains an open loop",
            loop=loop.get("id"),
        )
    segments = loop.get("segments", [])
    if not segments:
        raise GraphError("INVALID_BOARD", "board outline loop has no segments", loop=loop.get("id"))
    if len(segments) == 1 and segments[0].get("kind") == "circle":
        segment = segments[0]
        center = segment["start"]
        radius = math.dist(segment["start"], segment["end"])
        if radius <= 0:
            raise GraphError("INVALID_BOARD", "board circle has no radius", loop=loop.get("id"))
        wire = Wire.make_circle(radius).moved(Location((center[0], center[1], 0)))
        return wire, [(segment["id"], wire.edges()[0])]

    edges = []
    ids = []
    for segment in segments:
        kind = segment.get("kind")
        if kind == "line":
            edge = Edge.make_line(_point(segment["start"]), _point(segment["end"]))
        elif kind == "arc" and segment.get("mid") is not None:
            edge = Edge.make_three_point_arc(
                _point(segment["start"]),
                _point(segment["mid"]),
                _point(segment["end"]),
            )
        else:
            raise GraphError(
                "UNSUPPORTED_BOARD_OUTLINE",
                f"board outline segment kind {kind!r} is not supported",
                loop=loop.get("id"),
                segment=segment.get("id"),
            )
        edges.append(edge)
        ids.append(segment["id"])
    wire, built = _wire(edges)
    return wire, list(zip(ids, built, strict=True))


def _drill_wire(hole: dict[str, Any]) -> tuple[Wire, list[Edge]]:
    drill = hole["drill"]
    width, height = drill["width"], drill["height"]
    if width <= 0 or height <= 0:
        raise GraphError("INVALID_BOARD", "board drill size must be positive", hole=hole["id"])
    x, y = hole["position"]
    if drill["shape"] == "circle":
        wire = Wire.make_circle(width / 2).moved(Location((x, y, 0)))
        return wire, wire.edges()
    if drill["shape"] != "oval":
        raise GraphError(
            "UNSUPPORTED_BOARD_DRILL",
            f"board drill shape {drill['shape']!r} is not supported",
            hole=hole["id"],
        )

    major = max(width, height)
    radius = min(width, height) / 2
    angle = math.radians(drill["rotation"] + (90 if height > width else 0))
    ux, uy = math.cos(angle), math.sin(angle)
    vx, vy = -uy, ux
    half_straight = (major - 2 * radius) / 2
    first = (x - half_straight * ux, y - half_straight * uy)
    second = (x + half_straight * ux, y + half_straight * uy)

    def at(center, along_u: float, along_v: float) -> tuple[float, float, float]:
        return (
            center[0] + along_u * ux + along_v * vx,
            center[1] + along_u * uy + along_v * vy,
            0,
        )

    edges = [
        Edge.make_line(at(first, 0, radius), at(second, 0, radius)),
        Edge.make_three_point_arc(
            at(second, 0, radius), at(second, radius, 0), at(second, 0, -radius)
        ),
        Edge.make_line(at(second, 0, -radius), at(first, 0, -radius)),
        Edge.make_three_point_arc(
            at(first, 0, -radius), at(first, -radius, 0), at(first, 0, radius)
        ),
    ]
    return _wire(edges)


def build_pcb(node: BoardImport) -> PcbResult:
    """Build one board solid and face provenance from a persisted import node."""
    mechanics = node.mechanics
    thickness = mechanics.get("thickness")
    outer_loops = mechanics.get("outline", [])
    if not isinstance(thickness, (int, float)) or not math.isfinite(thickness) or thickness <= 0:
        raise GraphError("INVALID_BOARD", "board thickness must be a positive finite value", feature=node.id)
    if len(outer_loops) != 1:
        raise GraphError(
            "INVALID_BOARD",
            "board must contain exactly one outer outline loop",
            feature=node.id,
            outerLoops=len(outer_loops),
        )

    outer, outer_edges = _loop_wire(outer_loops[0])
    inner_edges: list[tuple[str, str, Edge]] = []
    holes: list[Wire] = []
    for loop in mechanics.get("innerCutouts", []):
        wire, edges = _loop_wire(loop)
        holes.append(wire)
        inner_edges.extend(("cutout", segment_id, edge) for segment_id, edge in edges)
    for hole in mechanics.get("holes", []):
        wire, edges = _drill_wire(hole)
        holes.append(wire)
        inner_edges.extend(("hole", hole["id"], edge) for edge in edges)

    try:
        face = Face(outer, holes)
        prism = BRepPrimAPI_MakePrism(face.wrapped, gp_Vec(0, 0, thickness))
        prism.Build()
    except Exception as error:
        raise GraphError(
            "INVALID_BOARD",
            "board loops and drills do not define a valid substrate",
            feature=node.id,
        ) from error
    if not prism.IsDone():
        raise GraphError("INVALID_BOARD", "board substrate extrusion failed", feature=node.id)
    shape = Shape.cast(prism.Shape())
    if len(shape.solids()) != 1 or not BRepCheck_Analyzer(shape.wrapped).IsValid():
        raise GraphError(
            "INVALID_BOARD",
            "board loops and drills do not define one valid solid",
            feature=node.id,
        )

    body = BodySource(node.id, outer_loops[0]["id"])
    candidates = [
        candidate(node.id, "board_bottom", outer_loops[0]["id"], Shape.cast(prism.FirstShape()), body=body),
        candidate(node.id, "board_top", outer_loops[0]["id"], Shape.cast(prism.LastShape()), body=body),
    ]
    for role, source, edge in [
        *(("outline", segment_id, edge) for segment_id, edge in outer_edges),
        *inner_edges,
    ]:
        generated = list(prism.Generated(edge.wrapped))
        if len(generated) != 1:
            raise GraphError(
                "INVALID_BOARD",
                "board extrusion did not preserve boundary provenance",
                feature=node.id,
                role=role,
                source=source,
            )
        candidates.append(candidate(node.id, role, source, Shape.cast(generated[0]), body=body))
    return PcbResult(shape, [*candidates, *derive_edge_candidates(node.id, shape, candidates)])
