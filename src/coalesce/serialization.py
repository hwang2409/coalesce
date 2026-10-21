import json
from dataclasses import asdict
from typing import Any

from .graph import (
    BoardImport,
    Chamfer,
    Circle,
    Cut,
    Extrude,
    Fillet,
    Graph,
    GraphError,
    Hole,
    HolePosition,
    PcbBase,
    PcbLid,
    Plane,
    PortCutout,
    Rectangle,
    Sketch,
    Standoffs,
)
from .references import (
    BodySource,
    Cardinality,
    MaxAlong,
    Normal,
    Position,
    Reference,
    _source_from_dict,
)

_TYPES = {
    cls.__name__: cls
    for cls in (
        BoardImport,
        Sketch,
        Rectangle,
        Circle,
        Extrude,
        PcbBase,
        PcbLid,
        PortCutout,
        Standoffs,
        Cut,
        Hole,
        Fillet,
        Chamfer,
    )
}
_FILTERS = {cls.__name__: cls for cls in (Normal, Position, MaxAlong)}


def _reference(payload: dict[str, Any]) -> Reference:
    value = dict(payload)
    value["cardinality"] = Cardinality(value.get("cardinality", Cardinality.ONE))
    value["source"] = _source_from_dict(value.get("source"))
    body = value.get("body")
    if isinstance(body, dict) and body.get("kind") == "body_source":
        value["body"] = BodySource(
            **{key: field for key, field in body.items() if key != "kind"}
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


def _sketch(payload: dict[str, Any]) -> dict[str, Any]:
    payload["plane"] = Plane(**payload.get("plane", {}))
    payload["position"] = tuple(payload.get("position", (0, 0, 0)))
    payload["rotation"] = tuple(payload.get("rotation", (0, 0, 0)))
    entities = []
    for raw_entity in payload.get("entities", []):
        entity = dict(raw_entity)
        entity_type = _TYPES.get(entity.pop("type", None))
        if entity_type not in (Rectangle, Circle):
            raise ValueError("sketch entity must be a Rectangle or Circle")
        entity["position"] = tuple(entity.get("position", (0, 0)))
        entities.append(entity_type(**entity))
    payload["entities"] = entities
    return payload


def _node_payload(node: Any) -> dict[str, Any]:
    payload = asdict(node)
    if isinstance(node, BoardImport):
        return {
            "type": "BoardImport",
            "id": node.id,
            "format": "kicad_pcb",
            "sourcePath": node.source_path,
            "sourceDigest": node.source_digest,
            "parserVersion": node.parser_version,
            "mechanics": node.mechanics,
            "availability": node.availability,
            "warnings": node.warnings,
            "errors": node.errors,
        }
    if isinstance(node, Sketch):
        payload["entities"] = [
            {"type": entity.__class__.__name__, **asdict(entity)}
            for entity in node.entities
        ]
    elif isinstance(node, Extrude) and node.operation == "new_body":
        payload.pop("operation")
        if node.target is None:
            payload.pop("target")
    elif isinstance(node, Hole):
        if node.positions:
            payload.pop("position")
        else:
            payload.pop("positions")
    elif isinstance(node, (Fillet, Chamfer)):
        payload["edges"] = [reference.to_dict() for reference in node.edges]
    return {"type": node.__class__.__name__, **payload}


def dumps(graph: Graph) -> str:
    return json.dumps({"nodes": [_node_payload(node) for node in graph.nodes.values()]})


def loads(value: str) -> Graph:
    data = json.loads(value)
    graph = Graph()
    for raw_payload in data.get("nodes", []):
        payload = dict(raw_payload)
        kind = payload.pop("type", None)
        cls = _TYPES.get(kind)
        if cls is None:
            raise ValueError(f"unknown node type: {kind}")
        if cls is BoardImport:
            if payload.pop("format", None) != "kicad_pcb":
                raise ValueError("BoardImport format must be kicad_pcb")
            payload["source_path"] = payload.pop("sourcePath")
            payload["source_digest"] = payload.pop("sourceDigest")
            payload["parser_version"] = payload.pop("parserVersion")
        elif cls is Sketch:
            payload = _sketch(payload)
        elif cls is Standoffs:
            if "minimum_diameter" in payload or "minimumDiameter" in payload:
                raise GraphError(
                    "INVALID_DOCUMENT",
                    "minimumDiameter was removed; use selection mode all_mounting, which selects every MountingHole:* footprint hole.",
                    feature=payload.get("id"),
                    field="minimumDiameter",
                )
            payload["hole_ids"] = tuple(payload.get("hole_ids", ()))
        elif cls is Hole:
            payload["position"] = tuple(payload.get("position", (0, 0)))
            payload["positions"] = tuple(
                HolePosition(item["name"], tuple(item["point"]))
                for item in payload.get("positions", [])
            )
        elif cls in (Fillet, Chamfer):
            payload["edges"] = [_reference(item) for item in payload.get("edges", [])]
        graph.add(cls(**payload))
    return graph
