from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import product
from math import sqrt

from build123d import Plane as BuildPlane
from build123d import Shape

from .extrusion import extrude_sketch, sketch_location
from .graph import Cut, Extrude, GraphError, Sketch
from .references import Candidate


@dataclass
class CutTool:
    shape: Shape
    candidates: list[Candidate]


def _through_span(sketch: Sketch, target: Shape) -> float:
    bounds = target.bounding_box()
    location = sketch_location(sketch)
    normal = BuildPlane(location).z_dir
    origin = location.position
    projections = [
        abs(
            (x - origin.X) * normal.X
            + (y - origin.Y) * normal.Y
            + (z - origin.Z) * normal.Z
        )
        for x, y, z in product(
            (bounds.min.X, bounds.max.X),
            (bounds.min.Y, bounds.max.Y),
            (bounds.min.Z, bounds.max.Z),
        )
    ]
    diagonal = sqrt(bounds.size.X**2 + bounds.size.Y**2 + bounds.size.Z**2)
    return max(projections) + max(diagonal * 0.01, 1.0)


def make_cut_tool(node: Cut, sketch: Sketch, target: Shape) -> CutTool:
    if node.depth is not None and node.depth <= 0:
        raise GraphError(
            "invalid_cut_depth",
            f"cut {node.id} depth must be positive",
            feature=node.id,
            depth=node.depth,
        )

    tool_sketch = sketch
    if node.depth is None:
        span = _through_span(sketch, target)
        normal = BuildPlane(sketch_location(sketch)).z_dir
        tool_sketch = replace(
            sketch,
            position=(
                sketch.position[0] - normal.X * span,
                sketch.position[1] - normal.Y * span,
                sketch.position[2] - normal.Z * span,
            ),
        )
        height = 2 * span
    else:
        height = node.depth if node.flip else -node.depth

    result = extrude_sketch(
        Extrude(node.id, sketch=sketch.id, height=height),
        tool_sketch,
    )
    return CutTool(result.shape, result.candidates)
