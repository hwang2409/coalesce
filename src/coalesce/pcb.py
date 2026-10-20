"""Exact board-substrate regeneration from a persisted KiCad mechanics snapshot."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from build123d import Compound, Edge, Face, Location, Shape, Wire
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeWire
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.gp import gp_Vec

from .graph import BoardImport, GraphError
from .kicad_angles import kicad_angle
from .provenance import candidate, derive_edge_candidates
from .references import BodySource, Candidate


@dataclass(frozen=True)
class PcbResult:
    shape: Shape
    candidates: list[Candidate]


@dataclass
class _PlanarRegion:
    face: Face
    boundaries: list[tuple[str, str, Edge]]
    source: str


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
    angle = math.radians(kicad_angle(drill["rotation"]) + (90 if height > width else 0))
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


def _surviving_edges(
    operation: BRepAlgoAPI_Cut,
    source: Edge,
    output: list[Edge],
) -> list[Edge]:
    history = [
        Edge(item)
        for item in [
            *operation.Modified(source.wrapped),
            *operation.Generated(source.wrapped),
        ]
    ]
    if not history and not operation.IsDeleted(source.wrapped):
        history = [source]
    return [edge for edge in output if any(edge.is_same(item) for item in history)]


def _subtract_region(
    region: Face,
    boundaries: list[tuple[str, str, Edge]],
    wire: Wire,
    sources: list[tuple[str, str, Edge]],
) -> tuple[Face, list[tuple[str, str, Edge]]]:
    operation = BRepAlgoAPI_Cut(region.wrapped, Face(wire).wrapped)
    operation.Build()
    result = Shape.cast(operation.Shape())
    faces = result.faces()
    if not operation.IsDone() or len(faces) != 1:
        raise ValueError("subtraction did not produce one planar region")
    output = faces[0].edges()
    surviving = [
        (role, source, edge)
        for role, source, previous in [*boundaries, *sources]
        for edge in _surviving_edges(operation, previous, output)
    ]
    return faces[0], surviving


def _contains_face(container: Face, contained: Face) -> bool:
    common = BRepAlgoAPI_Common(container.wrapped, contained.wrapped)
    common.Build()
    if not common.IsDone():
        return False
    area = sum(face.area for face in Shape.cast(common.Shape()).faces())
    return math.isclose(area, contained.area, rel_tol=1e-9, abs_tol=1e-9)


_Subtraction = tuple[Wire, list[tuple[str, str, Edge]]]


def _cutout_subtractions(
    node: BoardImport,
    regions: list[_PlanarRegion],
) -> dict[str, list[_Subtraction]]:
    assigned: dict[str, list[_Subtraction]] = {region.source: [] for region in regions}
    for loop in node.mechanics.get("innerCutouts", []):
        wire, edges = _loop_wire(loop)
        cutout = Face(wire)
        parents = [region for region in regions if _contains_face(region.face, cutout)]
        if not parents:
            raise GraphError(
                "INVALID_BOARD",
                "board cutout is not inside an outer outline",
                feature=node.id,
                loop=loop["id"],
            )
        parent = min(parents, key=lambda region: region.face.area)
        assigned[parent.source].append(
            (wire, [("cutout", source, edge) for source, edge in edges])
        )
    return assigned


def _drill_subtractions(node: BoardImport) -> list[_Subtraction]:
    drills = []
    for hole in node.mechanics.get("holes", []):
        wire, edges = _drill_wire(hole)
        drills.append((wire, [("hole", hole["id"], edge) for edge in edges]))
    return drills


def _apply_subtractions(
    node: BoardImport,
    regions: list[_PlanarRegion],
    assigned: dict[str, list[_Subtraction]],
    drills: list[_Subtraction],
) -> None:
    try:
        for region in regions:
            for wire, sources in [*assigned[region.source], *drills]:
                region.face, region.boundaries = _subtract_region(
                    region.face, region.boundaries, wire, sources
                )
    except Exception as error:
        raise GraphError(
            "INVALID_BOARD",
            "board loops and drills do not define a valid substrate",
            feature=node.id,
        ) from error


def _planar_regions(node: BoardImport) -> list[_PlanarRegion]:
    regions: list[_PlanarRegion] = []
    for loop in node.mechanics.get("outline", []):
        wire, edges = _loop_wire(loop)
        regions.append(
            _PlanarRegion(
                Face(wire),
                [("outline", source, edge) for source, edge in edges],
                loop["id"],
            )
        )
    if not regions:
        raise GraphError("INVALID_BOARD", "board has no outer outline loop", feature=node.id)
    _apply_subtractions(
        node,
        regions,
        _cutout_subtractions(node, regions),
        _drill_subtractions(node),
    )
    return regions


def _validate_face_provenance(node: BoardImport, shape: Shape, faces: list[Candidate]) -> None:
    if any(
        sum(output.is_same(item.entity) for item in faces) != 1
        for output in shape.faces()
    ):
        raise GraphError(
            "INVALID_BOARD",
            "board extrusion did not preserve complete face provenance",
            feature=node.id,
        )


def _extrude_region(
    node: BoardImport,
    region: _PlanarRegion,
    thickness: float,
) -> tuple[Shape, list[Candidate]]:
    prism = BRepPrimAPI_MakePrism(region.face.wrapped, gp_Vec(0, 0, thickness))
    prism.Build()
    if not prism.IsDone():
        raise ValueError("board substrate extrusion failed")
    shape = Shape.cast(prism.Shape())
    body = BodySource(node.id, region.source)
    candidates = [
        candidate(node.id, "board_bottom", region.source, Shape.cast(prism.FirstShape()), body=body),
        candidate(node.id, "board_top", region.source, Shape.cast(prism.LastShape()), body=body),
    ]
    for role, source, edge in region.boundaries:
        generated = list(prism.Generated(edge.wrapped))
        if len(generated) != 1:
            raise ValueError(f"extrusion lost {role} provenance for {source}")
        candidates.append(candidate(node.id, role, source, Shape.cast(generated[0]), body=body))
    return shape, candidates


def build_pcb(node: BoardImport) -> PcbResult:
    """Build board solids; subtraction outside existing material is a no-op."""
    thickness = node.mechanics.get("thickness")
    if not isinstance(thickness, (int, float)) or not math.isfinite(thickness) or thickness <= 0:
        raise GraphError("INVALID_BOARD", "board thickness must be a positive finite value", feature=node.id)

    try:
        built = [_extrude_region(node, region, thickness) for region in _planar_regions(node)]
    except GraphError:
        raise
    except Exception as error:
        raise GraphError(
            "INVALID_BOARD",
            "board loops and drills do not define a valid substrate",
            feature=node.id,
        ) from error
    shapes = [shape for shape, _ in built]
    shape = shapes[0] if len(shapes) == 1 else Compound(children=shapes)
    candidates = [item for _, region_candidates in built for item in region_candidates]
    if not BRepCheck_Analyzer(shape.wrapped).IsValid():
        raise GraphError(
            "INVALID_BOARD",
            "board loops and drills do not define valid solids",
            feature=node.id,
        )
    _validate_face_provenance(node, shape, candidates)
    return PcbResult(shape, [*candidates, *derive_edge_candidates(node.id, shape, candidates)])
