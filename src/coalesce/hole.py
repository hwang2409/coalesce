from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import cos, hypot, isclose, radians, sin, tau

from build123d import Compound, Face, Location, Shape, Wire
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.BRepTools import BRepTools
from OCP.gp import gp_Vec
from OCP.TopoDS import TopoDS

from .boolean import _has_solid_common
from .graph import (
    MAX_HOLE_COUNT,
    CircularHolePattern,
    GraphError,
    Hole,
    HolePosition,
    LinearHolePattern,
)
from .provenance import candidate
from .references import BodySource, Candidate, HoleSurfaceSource


@dataclass
class HoleTool:
    shape: Shape
    candidates: list[Candidate]
    algorithms: tuple[BRepPrimAPI_MakePrism, ...]


@dataclass
class _NamedCutter:
    name: str
    position: HolePosition
    shape: Shape
    candidates: list[Candidate]
    algorithm: BRepPrimAPI_MakePrism


def _build_cutter(
    node: Hole,
    position: HolePosition,
    name: str,
    start: float,
    height: float,
) -> _NamedCutter:
    wire = Wire.make_circle(node.diameter / 2).moved(
        Location((position.point[0], position.point[1], start))
    )
    (edge,) = wire.edges()
    prism = BRepPrimAPI_MakePrism(Face(wire).wrapped, gp_Vec(0, 0, height))
    shape = Shape.cast(prism.Shape())
    body = BodySource(node.id, position.name or "tool")
    faces = [
        candidate(
            node.id,
            "cap_start",
            HoleSurfaceSource(node.id, "floor", position.name),
            Shape.cast(prism.FirstShape()),
            body=body,
        ),
        candidate(
            node.id,
            "cap_end",
            HoleSurfaceSource(node.id, "opening", position.name),
            Shape.cast(prism.LastShape()),
            body=body,
        ),
    ]
    generated = list(prism.Generated(edge.wrapped))
    if len(generated) != 1:
        raise GraphError(
            "HISTORY_MISSING",
            f"hole {node.id} position {position.name} generated {len(generated)} walls",
            feature=node.id,
            position=position.name,
        )
    faces.append(
        candidate(
            node.id,
            "side(hole)",
            HoleSurfaceSource(node.id, "wall", position.name),
            Shape.cast(generated[0]),
            body=body,
        )
    )
    return _NamedCutter(name, position, shape, faces, prism)


def hole_positions(node: Hole) -> tuple[HolePosition, ...]:
    if isinstance(node.pattern, CircularHolePattern):
        step = 360 / node.pattern.count
        return tuple(
            HolePosition(
                f"{node.pattern.name_prefix}{index + 1}",
                (
                    node.pattern.center[0]
                    + node.pattern.radius * cos(radians(node.pattern.start_angle + index * step)),
                    node.pattern.center[1]
                    + node.pattern.radius * sin(radians(node.pattern.start_angle + index * step)),
                ),
            )
            for index in range(node.pattern.count)
        )
    if isinstance(node.pattern, LinearHolePattern):
        return tuple(
            HolePosition(
                f"{node.pattern.name_prefix}{index + 1}",
                (
                    node.pattern.start[0] + index * node.pattern.pitch * node.pattern.direction[0],
                    node.pattern.start[1] + index * node.pattern.pitch * node.pattern.direction[1],
                ),
            )
            for index in range(node.pattern.count)
        )
    return node.positions or (HolePosition(None, node.position),)


def _intersecting_pairs(cutters: list[_NamedCutter], diameter: float) -> list[list[str]]:
    return [
        [first.name, second.name]
        for first, second in combinations(cutters, 2)
        if hypot(
            first.position.point[0] - second.position.point[0],
            first.position.point[1] - second.position.point[1],
        )
        <= diameter
    ]


def _edge_breakouts(target: Shape, cutters: list[_NamedCutter]) -> list[str]:
    breakouts = []
    for cutter in cutters:
        algorithm = BRepAlgoAPI_Cut(target.wrapped, cutter.shape.wrapped)
        algorithm.Build()
        wall = cutter.candidates[-1].entity
        wall_faces = [TopoDS.Face(shape) for shape in algorithm.Modified(wall.wrapped)]
        if wall_faces and any(
            not isclose(
                BRepTools.UVBounds_s(face)[1] - BRepTools.UVBounds_s(face)[0],
                tau,
                abs_tol=1e-7,
            )
            for face in wall_faces
        ):
            breakouts.append(cutter.name)
    return breakouts


def _validate_cutters(node: Hole, target: Shape, cutters: list[_NamedCutter]) -> None:
    pairs = _intersecting_pairs(cutters, node.diameter)
    if pairs:
        raise GraphError(
            "HOLES_INTERSECT",
            f"hole {node.id} has intersecting positions",
            feature=node.id,
            pairs=pairs,
        )
    missing = [
        cutter.name
        for cutter in cutters
        if not _has_solid_common(target, cutter.shape)
    ]
    if missing:
        raise GraphError(
            "HOLE_NO_EFFECT",
            f"hole {node.id} has positions that remove no material",
            feature=node.id,
            target=node.target,
            names=missing,
            hint="check target, position, diameter, and depth",
        )
    breakouts = _edge_breakouts(target, cutters) if node.pattern is not None else []
    if breakouts:
        raise GraphError(
            "INVALID_HOLE",
            f"hole {node.id} has positions that break out of a body edge",
            feature=node.id,
            target=node.target,
            names=breakouts,
            hint="move each named member fully inside the target body",
        )


def _validate_parameters(node: Hole) -> None:
    pattern = node.pattern
    if pattern is not None and not 1 <= pattern.count <= MAX_HOLE_COUNT:
        raise GraphError(
            "INVALID_HOLE",
            f"hole {node.id} pattern count must be from 1 through {MAX_HOLE_COUNT}",
            feature=node.id,
            field="pattern.count",
            maximum=MAX_HOLE_COUNT,
        )
    if pattern is not None and not pattern.name_prefix:
        raise GraphError(
            "INVALID_HOLE",
            f"hole {node.id} pattern name prefix must not be empty",
            feature=node.id,
            field="pattern.namePrefix",
        )
    if isinstance(pattern, CircularHolePattern) and pattern.radius <= 0:
        raise GraphError(
            "INVALID_HOLE",
            f"hole {node.id} pattern radius must be positive",
            feature=node.id,
            field="pattern.radius",
        )
    if isinstance(pattern, LinearHolePattern) and (
        pattern.pitch <= 0 or not isclose(hypot(*pattern.direction), 1, abs_tol=1e-12)
    ):
        raise GraphError(
            "INVALID_HOLE",
            f"hole {node.id} pattern pitch must be positive and direction must be normalized",
            feature=node.id,
            field="pattern",
        )
    if node.diameter <= 0:
        raise GraphError(
            "INVALID_HOLE",
            f"hole {node.id} diameter must be positive",
            feature=node.id,
        )
    if node.depth is not None and node.depth <= 0:
        raise GraphError(
            "INVALID_HOLE",
            f"hole {node.id} depth must be positive",
            feature=node.id,
        )


def _cutter_span(node: Hole, target: Shape) -> tuple[float, float]:
    bounds = target.bounding_box()
    margin = max(bounds.size.Z, node.diameter, 1.0)
    if node.depth is None:
        return bounds.min.Z - margin, bounds.size.Z + 2 * margin
    return bounds.max.Z - node.depth, node.depth + margin


def make_hole_tool(node: Hole, target: Shape) -> HoleTool:
    _validate_parameters(node)
    start, height = _cutter_span(node, target)
    positions = hole_positions(node)
    cutters = [
        _build_cutter(node, position, position.name or f"h{index}", start, height)
        for index, position in enumerate(positions, start=1)
    ]
    _validate_cutters(node, target, cutters)
    shapes = [cutter.shape for cutter in cutters]
    shape = shapes[0] if len(shapes) == 1 else Compound(children=shapes)
    return HoleTool(
        shape,
        [face for cutter in cutters for face in cutter.candidates],
        tuple(cutter.algorithm for cutter in cutters),
    )
