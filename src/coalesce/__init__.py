"""A typed, history-aware feature graph for build123d."""

from importlib import import_module

__all__ = [
    "BodySource", "Cardinality", "Circle", "Cut", "Extrude", "Fillet", "Graph",
    "GraphError", "Hole", "HoleSurfaceSource", "MaxAlong", "Normal", "Parameter",
    "Plane", "Position", "Rectangle", "Reference", "RegenResult", "Resolution",
    "Sketch", "SketchSegmentSource", "Status", "ToolCapSource", "regenerate",
]

_GRAPH_NAMES = {
    "Circle", "Cut", "Extrude", "Fillet", "Graph", "GraphError", "Hole", "Parameter",
    "Plane", "Rectangle", "Sketch",
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
