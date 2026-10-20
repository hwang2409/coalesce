from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import hypot

from build123d import Compound, Face, Location, Shape, Wire
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.gp import gp_Vec

from .graph import GraphError, Hole, HolePosition
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
            "history_missing",
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


def _has_effect(target: Shape, cutter: Shape) -> bool:
    operation = BRepAlgoAPI_Cut(target.wrapped, cutter.wrapped)
    operation.Build()
    if not operation.IsDone():
        raise GraphError("boolean_failed", "hole cutter contact check failed")
    return any(
        operation.IsDeleted(face.wrapped) or len(operation.Modified(face.wrapped)) > 0
        for face in target.faces()
    ) or any(
        len(operation.Modified(face.wrapped)) > 0
        or len(operation.Generated(face.wrapped)) > 0
        for face in cutter.faces()
    )


def _validate_cutters(node: Hole, target: Shape, cutters: list[_NamedCutter]) -> None:
    pairs = _intersecting_pairs(cutters, node.diameter)
    if pairs:
        raise GraphError(
            "holes_intersect",
            f"hole {node.id} has intersecting positions",
            feature=node.id,
            pairs=pairs,
        )
    missing = [cutter.name for cutter in cutters if not _has_effect(target, cutter.shape)]
    if missing and len(missing) != len(cutters):
        raise GraphError(
            "hole_no_effect",
            f"hole {node.id} has positions that remove no material",
            feature=node.id,
            target=node.target,
            names=missing,
            hint="check target, position, diameter, and depth",
        )


def make_hole_tool(node: Hole, target: Shape) -> HoleTool:
    if node.diameter <= 0:
        raise GraphError(
            "invalid_hole",
            f"hole {node.id} diameter must be positive",
            feature=node.id,
        )
    if node.depth is not None and node.depth <= 0:
        raise GraphError(
            "invalid_hole",
            f"hole {node.id} depth must be positive",
            feature=node.id,
        )

    bounds = target.bounding_box()
    margin = max(bounds.size.Z, node.diameter, 1.0)
    if node.depth is None:
        start = bounds.min.Z - margin
        height = bounds.size.Z + 2 * margin
    else:
        start = bounds.max.Z - node.depth
        height = node.depth + margin

    positions = node.positions or (HolePosition(None, node.position),)
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
