from __future__ import annotations

from dataclasses import dataclass

from build123d import Face, Location, Shape, Wire
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.gp import gp_Vec

from .graph import GraphError, Hole
from .provenance import candidate
from .references import BodySource, Candidate, HoleSurfaceSource


@dataclass
class HoleTool:
    shape: Shape
    candidates: list[Candidate]
    algorithm: BRepPrimAPI_MakePrism


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

    wire = Wire.make_circle(node.diameter / 2).moved(
        Location((node.position[0], node.position[1], start))
    )
    (edge,) = wire.edges()
    prism = BRepPrimAPI_MakePrism(Face(wire).wrapped, gp_Vec(0, 0, height))
    body = BodySource(node.id, "tool")
    faces = [
        candidate(
            node.id,
            "cap_start",
            HoleSurfaceSource(node.id, "floor"),
            Shape.cast(prism.FirstShape()),
            body=body,
        ),
        candidate(
            node.id,
            "cap_end",
            HoleSurfaceSource(node.id, "opening"),
            Shape.cast(prism.LastShape()),
            body=body,
        ),
    ]
    generated = list(prism.Generated(edge.wrapped))
    if len(generated) != 1:
        raise GraphError(
            "history_missing",
            f"hole {node.id} generated {len(generated)} walls",
            feature=node.id,
        )
    faces.append(
        candidate(
            node.id,
            "side(hole)",
            HoleSurfaceSource(node.id, "wall"),
            Shape.cast(generated[0]),
            body=body,
        )
    )
    return HoleTool(Shape.cast(prism.Shape()), faces, prism)
