import json
from dataclasses import asdict
from typing import Any

from .constrained_sketch import from_payload as constrained_from_payload
from .constrained_sketch import to_payload as constrained_to_payload
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
        value["body"] = BodySource(**{key: field for key, field in body.items() if key != "kind"})
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
    constrained = payload.get("constrained")
    if constrained is not None:
        payload.pop("entities", None)
        payload["constrained"] = constrained_from_payload(constrained)
        return payload
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


def _board_payload(node: BoardImport, payload: dict[str, Any]) -> dict[str, Any]:
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


def _sketch_payload(node: Sketch, _payload: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "id": node.id,
        "plane": asdict(node.plane),
        "position": node.position,
        "rotation": node.rotation,
    }
    if node.constrained is not None:
        payload["constrained"] = constrained_to_payload(node.constrained)
    else:
        payload["entities"] = [
            {"type": entity.__class__.__name__, **asdict(entity)} for entity in node.entities
        ]
    return payload


def _extrude_payload(node: Extrude, payload: dict[str, Any]) -> dict[str, Any]:
    if node.operation == "new_body":
        payload.pop("operation")
        if node.target is None:
            payload.pop("target")
    return payload


def _lid_payload(node: PcbLid, payload: dict[str, Any]) -> dict[str, Any]:
    payload.pop("outline_source")
    if node.outline_source is not None:
        payload["outlineSource"] = {"featureId": node.outline_source}
    return payload


def _hole_payload(node: Hole, payload: dict[str, Any]) -> dict[str, Any]:
    payload.pop("position" if node.positions else "positions")
    return payload


def _treatment_payload(node: Any, payload: dict[str, Any]) -> dict[str, Any]:
    payload["edges"] = [reference.to_dict() for reference in node.edges]
    return payload


_SERIALIZERS = {
    BoardImport: _board_payload,
    Sketch: _sketch_payload,
    Extrude: _extrude_payload,
    PcbLid: _lid_payload,
    Hole: _hole_payload,
    Fillet: _treatment_payload,
    Chamfer: _treatment_payload,
}


def _node_payload(node: Any) -> dict[str, Any]:
    payload = asdict(node)
    serializer = _SERIALIZERS.get(type(node))
    if serializer is not None:
        payload = serializer(node, payload)
    return (
        payload if isinstance(node, BoardImport) else {"type": node.__class__.__name__, **payload}
    )


def dumps(graph: Graph) -> str:
    return json.dumps({"nodes": [_node_payload(node) for node in graph.nodes.values()]})


def _load_board(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.pop("format", None) != "kicad_pcb":
        raise ValueError("BoardImport format must be kicad_pcb")
    payload["source_path"] = payload.pop("sourcePath")
    payload["source_digest"] = payload.pop("sourceDigest")
    payload["parser_version"] = payload.pop("parserVersion")
    return payload


def _load_standoffs(payload: dict[str, Any]) -> dict[str, Any]:
    if "minimum_diameter" in payload or "minimumDiameter" in payload:
        raise GraphError(
            "INVALID_DOCUMENT",
            "minimumDiameter was removed; use selection mode all_mounting, which selects every MountingHole:* footprint hole.",
            feature=payload.get("id"),
            field="minimumDiameter",
        )
    payload["hole_ids"] = tuple(payload.get("hole_ids", ()))
    return payload


def _load_lid(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload.pop("outlineSource", None)
    payload["outline_source"] = source.get("featureId") if source else None
    return payload


def _load_hole(payload: dict[str, Any]) -> dict[str, Any]:
    payload["position"] = tuple(payload.get("position", (0, 0)))
    payload["positions"] = tuple(
        HolePosition(item["name"], tuple(item["point"])) for item in payload.get("positions", [])
    )
    return payload


def _load_treatment(payload: dict[str, Any]) -> dict[str, Any]:
    payload["edges"] = [_reference(item) for item in payload.get("edges", [])]
    return payload


_LOADERS = {
    BoardImport: _load_board,
    Sketch: _sketch,
    Standoffs: _load_standoffs,
    PcbLid: _load_lid,
    Hole: _load_hole,
    Fillet: _load_treatment,
    Chamfer: _load_treatment,
}


def loads(value: str) -> Graph:
    data = json.loads(value)
    graph = Graph()
    for raw_payload in data.get("nodes", []):
        payload = dict(raw_payload)
        kind = payload.pop("type", None)
        cls = _TYPES.get(kind)
        if cls is None:
            raise ValueError(f"unknown node type: {kind}")
        loader = _LOADERS.get(cls)
        graph.add(cls(**(loader(payload) if loader is not None else payload)))
    return graph
