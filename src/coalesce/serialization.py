import json
from dataclasses import asdict

from .graph import Circle, Cut, Extrude, Fillet, Graph, Hole, Plane, Rectangle, Sketch
from .references import Reference

_TYPES = {c.__name__: c for c in (Sketch, Rectangle, Circle, Extrude, Cut, Hole, Fillet)}


def dumps(graph: Graph) -> str:
    return json.dumps(
        {"nodes": [{"type": n.__class__.__name__, **asdict(n)} for n in graph.nodes.values()]}
    )


def loads(value: str) -> Graph:
    data = json.loads(value)
    graph = Graph()
    for payload in data.get("nodes", []):
        kind = payload.pop("type", None)
        cls = _TYPES.get(kind)
        if cls is None:
            raise ValueError(f"unknown node type: {kind}")
        if cls is Sketch:
            payload["plane"] = Plane(**payload.get("plane", {}))
            payload["entities"] = [
                _TYPES[e.pop("type", "Rectangle")](**e) for e in payload.get("entities", [])
            ]
        if cls is Fillet:
            payload["edges"] = [Reference(**e) for e in payload.get("edges", [])]
        graph.add(cls(**payload))
    return graph
