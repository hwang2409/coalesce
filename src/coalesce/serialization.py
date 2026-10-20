import json
from dataclasses import asdict
from typing import Any

from .graph import Circle, Cut, Extrude, Fillet, Graph, Hole, Plane, Rectangle, Sketch
from .references import (
    Cardinality,
    MaxAlong,
    Normal,
    Position,
    Reference,
    SketchSegmentSource,
)

_TYPES = {cls.__name__: cls for cls in (Sketch, Rectangle, Circle, Extrude, Cut, Hole, Fillet)}
_FILTERS = {cls.__name__: cls for cls in (Normal, Position, MaxAlong)}


def _reference(payload: dict[str, Any]) -> Reference:
    value = dict(payload)
    value["cardinality"] = Cardinality(value.get("cardinality", Cardinality.ONE))
    source = value.get("source")
    if isinstance(source, dict) and source.get("kind") == "sketch_segment":
        value["source"] = SketchSegmentSource(
            **{key: field for key, field in source.items() if key != "kind"}
        )
    value["filters"] = [
        _FILTERS[item["kind"]](
            **{
                key: tuple(field) if key == "direction" else field
                for key, field in item.items()
                if key != "kind"
            }
        )
        for item in value.get("filters", [])
    ]
    return Reference(**value)


def dumps(graph: Graph) -> str:
    nodes = []
    for node in graph.nodes.values():
        payload = asdict(node)
        if isinstance(node, Sketch):
            payload["entities"] = [
                {"type": entity.__class__.__name__, **asdict(entity)}
                for entity in node.entities
            ]
        elif isinstance(node, Fillet):
            payload["edges"] = [reference.to_dict() for reference in node.edges]
        nodes.append({"type": node.__class__.__name__, **payload})
    return json.dumps({"nodes": nodes})


def loads(value: str) -> Graph:
    data = json.loads(value)
    graph = Graph()
    for raw_payload in data.get("nodes", []):
        payload = dict(raw_payload)
        kind = payload.pop("type", None)
        cls = _TYPES.get(kind)
        if cls is None:
            raise ValueError(f"unknown node type: {kind}")
        if cls is Sketch:
            payload["plane"] = Plane(**payload.get("plane", {}))
            payload["position"] = tuple(payload.get("position", (0, 0, 0)))
            payload["rotation"] = tuple(payload.get("rotation", (0, 0, 0)))
            entities = []
            for raw_entity in payload.get("entities", []):
                entity = dict(raw_entity)
                entity_type = _TYPES.get(entity.pop("type", None))
                if entity_type not in (Rectangle, Circle):
                    raise ValueError("sketch entity must be a Rectangle or Circle")
                entities.append(entity_type(**entity))
            payload["entities"] = entities
        elif cls is Fillet:
            payload["edges"] = [_reference(item) for item in payload.get("edges", [])]
        graph.add(cls(**payload))
    return graph
