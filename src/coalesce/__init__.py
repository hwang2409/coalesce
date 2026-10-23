"""A typed, history-aware feature graph for build123d."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import (
        Chamfer,
        Circle,
        CircularHolePattern,
        Cut,
        Extrude,
        Fillet,
        Graph,
        GraphError,
        Hole,
        LinearHolePattern,
        Parameter,
        PcbBase,
        PcbLid,
        Plane,
        Rectangle,
        Sketch,
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
    "BodySource", "Cardinality", "Chamfer", "Circle", "CircularHolePattern", "Cut",
    "Extrude", "Fillet", "Graph", "GraphError", "Hole", "HoleSurfaceSource",
    "LinearHolePattern", "MaxAlong", "Normal", "Parameter",
    "PcbBase", "PcbLid", "Plane", "Position", "Rectangle", "Reference", "RegenResult", "Resolution",
    "Sketch", "SketchSegmentSource", "Standoffs", "Status", "ToolCapSource", "regenerate",
]

_GRAPH_NAMES = {
    "Chamfer", "Circle", "CircularHolePattern", "Cut", "Extrude", "Fillet", "Graph",
    "GraphError", "Hole", "LinearHolePattern", "Parameter",
    "PcbBase", "PcbLid", "Plane", "Rectangle", "Sketch", "Standoffs",
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
