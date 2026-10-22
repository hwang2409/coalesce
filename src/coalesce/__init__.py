"""A typed, history-aware feature graph for build123d."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import (
        Chamfer,
        Circle,
        Cut,
        Extrude,
        Fillet,
        Graph,
        GraphError,
        Hole,
        Parameter,
        PathSegment,
        PcbBase,
        PcbLid,
        Plane,
        Rectangle,
        Sketch,
        SketchPath,
        Standoffs,
    )
    from .references import (
        BodySource,
        Cardinality,
        HoleSurfaceSource,
        MaxAlong,
        Normal,
        Position,
        Reference,
        Resolution,
        SketchSegmentSource,
        Status,
        ToolCapSource,
    )
    from .regen import RegenResult, regenerate

__all__ = [
    "BodySource", "Cardinality", "Chamfer", "Circle", "Cut", "Extrude", "Fillet", "Graph",
    "GraphError", "Hole", "HoleSurfaceSource", "MaxAlong", "Normal", "Parameter",
    "PathSegment", "PcbBase", "PcbLid", "Plane", "Position", "Rectangle", "Reference",
    "RegenResult", "Resolution", "Sketch", "SketchPath", "SketchSegmentSource", "Standoffs", "Status", "ToolCapSource", "regenerate",
]

_GRAPH_NAMES = {
    "Chamfer", "Circle", "Cut", "Extrude", "Fillet", "Graph", "GraphError", "Hole", "Parameter",
    "PathSegment", "PcbBase", "PcbLid", "Plane", "Rectangle", "Sketch", "SketchPath", "Standoffs",
}
_REFERENCE_NAMES = {
    "BodySource", "Cardinality", "HoleSurfaceSource", "MaxAlong", "Normal", "Position",
    "Reference", "Resolution", "SketchSegmentSource", "Status", "ToolCapSource",
}


def __getattr__(name: str):
    if name in _GRAPH_NAMES:
        value = getattr(import_module(".graph", __name__), name)
    elif name in _REFERENCE_NAMES:
        value = getattr(import_module(".references", __name__), name)
    elif name == "RegenResult" or name == "regenerate":
        value = getattr(import_module(".regen", __name__), name)
    else:
        raise AttributeError(name)
    globals()[name] = value
    return value
