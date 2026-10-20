from __future__ import annotations

from dataclasses import asdict
from typing import Any

from build123d import Align, Box, Compound, Face, Location, Shape
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common

from .graph import BoardImport, PcbBase, PcbLid, PortCutout, Standoffs
from .parts import TerminalPart, terminal_parts
from .pcb import board_loop_wire

_TOLERANCE = 1e-7


def _quantity(value: float) -> dict[str, Any]:
    return {"value": round(value, 12), "unit": "mm"}


def _point(values: tuple[float, float, float]) -> dict[str, Any]:
    return {**dict(zip("xyz", (round(value, 12) for value in values), strict=True)), "unit": "mm"}


def _bbox(shape: Shape) -> dict[str, Any]:
    box = shape.bounding_box()
    return {
        "min": _point((box.min.X, box.min.Y, box.min.Z)),
        "max": _point((box.max.X, box.max.Y, box.max.Z)),
    }


def _size(shape: Shape) -> dict[str, Any]:
    box = shape.bounding_box()
    return {
        axis: _quantity(high - low)
        for axis, low, high in zip(
            "xyz",
            (box.min.X, box.min.Y, box.min.Z),
            (box.max.X, box.max.Y, box.max.Z),
            strict=True,
        )
    }


def _faces(result: Any, feature: str, role: str) -> list[Face]:
    return [
        candidate.entity
        for candidate in result.provenance.get(feature, [])
        if candidate.key.role == role and isinstance(candidate.entity, Face)
    ]


def _positive_distance(first: Shape, others: list[Shape]) -> float:
    distances = [first.distance_to(other) for other in others]
    positive = [distance for distance in distances if distance > _TOLERANCE]
    return min(positive or distances)


def _part_for(parts: list[TerminalPart], source_feature: str) -> TerminalPart:
    matches = [part for part in parts if part.source_body.feature == source_feature]
    if len(matches) != 1:
        raise ValueError(f"feature {source_feature} does not resolve to one terminal part")
    return matches[0]


def _mounting_holes(board: BoardImport) -> list[dict[str, Any]]:
    mounting_footprints = {
        footprint["id"]
        for footprint in board.mechanics.get("footprints", [])
        if footprint.get("libraryId", "").startswith("MountingHole:")
    }
    return [
        hole
        for hole in board.mechanics.get("holes", [])
        if hole.get("footprintId") in mounting_footprints
    ]


def _standoffs(graph: Any, result: Any, base: PcbBase, board_shape: Shape) -> dict[str, Any]:
    nodes = [
        node for node in graph.ordered() if isinstance(node, Standoffs) and node.target == base.id
    ]
    items = []
    covered = []
    board_bottom = board_shape.bounding_box().min.Z
    for node in nodes:
        sources = sorted(
            {
                candidate.key.source
                for candidate in result.provenance.get(node.id, [])
                if candidate.key.role == "standoff_side"
            }
        )
        for source in sources:
            [boss] = [
                candidate.entity
                for candidate in result.provenance[node.id]
                if candidate.key.role == "standoff_side" and candidate.key.source == source
            ]
            [pilot] = [
                candidate.entity
                for candidate in result.provenance[node.id]
                if candidate.key.role == "pilot_wall" and candidate.key.source == source
            ]
            box = boss.bounding_box()
            items.append(
                {
                    "holeId": source,
                    "axis": _point(
                        ((box.min.X + box.max.X) / 2, (box.min.Y + box.max.Y) / 2, box.min.Z)
                    ),
                    "bossDiameter": _quantity(boss.radius * 2),
                    "pilotDiameter": _quantity(pilot.radius * 2),
                    "topZ": _quantity(box.max.Z),
                    "boardUndersideZ": _quantity(board_bottom),
                }
            )
            covered.append(source)
    holes = _mounting_holes(graph.nodes[base.board])
    hole_ids = [hole["id"] for hole in holes]
    return {
        "count": len(items),
        "mountingHoleCount": len(holes),
        "coveredHoleIds": [hole_id for hole_id in hole_ids if hole_id in covered],
        "uncoveredHoleIds": [hole_id for hole_id in hole_ids if hole_id not in covered],
        "items": items,
    }


def _lids(
    graph: Any, result: Any, parts: list[TerminalPart], base: PcbBase
) -> list[dict[str, Any]]:
    inner_walls = _faces(result, base.id, "inner_wall")
    inspected = []
    for node in graph.ordered():
        if not isinstance(node, PcbLid) or node.base != base.id:
            continue
        part = _part_for(parts, node.id)
        undersides = _faces(result, node.id, "underside")
        lips = _faces(result, node.id, "lip_outer")
        gaps = [_positive_distance(lip, inner_walls) for lip in lips]
        lip_box = Compound(children=lips).bounding_box() if lips else None
        inspected.append(
            {
                "featureId": node.id,
                "partId": part.part_id,
                "outerBBox": _bbox(part.shape),
                "seatingZ": _quantity(min(face.bounding_box().min.Z for face in undersides)),
                "fitGap": (
                    {"min": _quantity(min(gaps)), "max": _quantity(max(gaps))} if gaps else None
                ),
                "lipDepth": (
                    _quantity(lip_box.max.Z - lip_box.min.Z) if lip_box is not None else None
                ),
            }
        )
    return inspected


def _opening_rectangle(tool: Shape, wall: str) -> dict[str, Any]:
    box = tool.bounding_box()
    if wall in {"left", "right"}:
        axes = ["y", "z"]
        values = (box.min.Y, box.max.Y, box.min.Z, box.max.Z)
    else:
        axes = ["x", "z"]
        values = (box.min.X, box.max.X, box.min.Z, box.max.Z)
    return {
        "axes": axes,
        **{
            key: _quantity(value)
            for key, value in zip(("uMin", "uMax", "vMin", "vMax"), values, strict=True)
        },
    }


def _port_clearances(
    node: PortCutout, board: BoardImport, board_shape: Shape, tool: Shape, wall: str
) -> list[float]:
    metadata_box = tool.bounding_box()
    footprint = next(
        footprint
        for footprint in board.mechanics.get("footprints", [])
        if footprint["id"] == node.footprint_id
    )
    if node.envelope_source == "courtyard":
        wire, _ = board_loop_wire(
            {"id": footprint["id"], "closed": True, "segments": footprint["courtyard"]}
        )
        envelope = wire.bounding_box()
        tangent_min, tangent_max = (
            (envelope.min.Y, envelope.max.Y)
            if wall in {"left", "right"}
            else (envelope.min.X, envelope.max.X)
        )
    else:
        center_x, center_y = footprint["position"]
        assert node.envelope_width is not None
        half_width = node.envelope_width / 2
        tangent_min, tangent_max = (
            (center_y - half_width, center_y + half_width)
            if wall in {"left", "right"}
            else (center_x - half_width, center_x + half_width)
        )
    envelope_bottom = board_shape.bounding_box().max.Z + node.bottom_offset
    if wall in {"left", "right"}:
        envelope_shape = Box(
            metadata_box.max.X - metadata_box.min.X,
            tangent_max - tangent_min,
            node.envelope_height,
            align=(Align.MIN, Align.MIN, Align.MIN),
        ).moved(Location((metadata_box.min.X, tangent_min, envelope_bottom)))
        opening_min, opening_max = metadata_box.min.Y, metadata_box.max.Y
        envelope_min, envelope_max = (
            envelope_shape.bounding_box().min.Y,
            envelope_shape.bounding_box().max.Y,
        )
    else:
        envelope_shape = Box(
            tangent_max - tangent_min,
            metadata_box.max.Y - metadata_box.min.Y,
            node.envelope_height,
            align=(Align.MIN, Align.MIN, Align.MIN),
        ).moved(Location((tangent_min, metadata_box.min.Y, envelope_bottom)))
        opening_min, opening_max = metadata_box.min.X, metadata_box.max.X
        envelope_min, envelope_max = (
            envelope_shape.bounding_box().min.X,
            envelope_shape.bounding_box().max.X,
        )
    envelope_box = envelope_shape.bounding_box()
    return [
        envelope_min - opening_min,
        opening_max - envelope_max,
        envelope_box.min.Z - metadata_box.min.Z,
        metadata_box.max.Z - envelope_box.max.Z,
    ]


def _common_volume(first: Shape, second: Shape) -> float:
    operation = BRepAlgoAPI_Common(first.wrapped, second.wrapped)
    operation.Build()
    if not operation.IsDone():
        raise ValueError("port blockage measurement failed")
    return Shape.cast(operation.Shape()).volume


def _ports(
    graph: Any,
    result: Any,
    base: PcbBase,
    board: BoardImport,
    board_shape: Shape,
    assembled: Shape,
) -> list[dict[str, Any]]:
    inspected = []
    for node in graph.ordered():
        if not isinstance(node, PortCutout) or node.base != base.id:
            continue
        metadata = result.metadata[node.id]
        wall = metadata["wall"]
        tool = result.opening_tools[node.id]
        clearances = _port_clearances(node, board, board_shape, tool, wall)
        inspected.append(
            {
                "featureId": node.id,
                "footprintId": metadata["footprintId"],
                "reference": metadata["reference"],
                "wall": wall,
                "opening": _opening_rectangle(tool, wall),
                "clearance": {
                    "min": _quantity(min(clearances)),
                    "max": _quantity(max(clearances)),
                },
                "blocked": _common_volume(tool, assembled) > _TOLERANCE,
            }
        )
    return inspected


def inspect_enclosure(graph: Any, result: Any, base_id: str) -> dict[str, Any]:
    base = graph.nodes.get(base_id)
    if not isinstance(base, PcbBase):
        raise TypeError(f"unknown pcb_base: {base_id}")
    board = graph.nodes[base.board]
    if not isinstance(board, BoardImport):
        raise TypeError(f"pcb_base {base_id} has no board")

    parts = terminal_parts(result, role="manufacturing")
    base_part = _part_for(parts, base.id)
    lid_nodes = [
        node for node in graph.ordered() if isinstance(node, PcbLid) and node.base == base.id
    ]
    enclosure_parts = [base_part, *[_part_for(parts, node.id) for node in lid_nodes]]
    assembled = Compound(children=[part.shape for part in enclosure_parts])
    board_shape = result.shapes[board.id]
    board_box = board_shape.bounding_box()
    inner_walls = _faces(result, base.id, "inner_wall")
    outer_walls = _faces(result, base.id, "outer_wall")
    floor_faces = _faces(result, base.id, "floor")
    floor_bottoms = _faces(result, base.id, "floor_bottom")
    inner_box = Compound(children=inner_walls).bounding_box()

    selected_ids = {part.part_id for part in enclosure_parts}
    same_board_sources = {
        node.id for node in graph.ordered() if isinstance(node, PcbBase) and node.board == board.id
    }
    same_board_sources.update(
        node.id
        for node in graph.ordered()
        if isinstance(node, PcbLid)
        and isinstance(graph.nodes.get(node.base), PcbBase)
        and graph.nodes[node.base].board == board.id
    )
    warnings = [
        {
            "code": "OTHER_MANUFACTURING_BODY",
            "featureId": part.terminal_feature,
            "partId": part.part_id,
        }
        for part in parts
        if part.part_id not in selected_ids and part.source_body.feature in same_board_sources
    ]
    ports = _ports(graph, result, base, board, board_shape, assembled)
    if ports and board.availability.get("models") != "available":
        warnings.append(
            {
                "code": "CONNECTOR_HEIGHT_UNRESOLVED",
                "footprintIds": [port["footprintId"] for port in ports],
            }
        )

    return {
        "baseId": base.id,
        "board": {
            "assetId": board.id,
            "outlineBBox": _bbox(board_shape),
            "topZ": _quantity(board_box.max.Z),
        },
        "base": {
            "partId": base_part.part_id,
            "outerBBox": _bbox(base_part.shape),
            "outerSize": _size(base_part.shape),
            "minimumWallThickness": _quantity(
                min(_positive_distance(inner, outer_walls) for inner in inner_walls)
            ),
            "floorThickness": _quantity(
                min(floor.distance_to(bottom) for floor in floor_faces for bottom in floor_bottoms)
            ),
        },
        "cavity": {
            "innerSize": {
                "x": _quantity(inner_box.max.X - inner_box.min.X),
                "y": _quantity(inner_box.max.Y - inner_box.min.Y),
            },
            "boardClearance": {
                "left": _quantity(board_box.min.X - inner_box.min.X),
                "right": _quantity(inner_box.max.X - board_box.max.X),
                "front": _quantity(board_box.min.Y - inner_box.min.Y),
                "back": _quantity(inner_box.max.Y - board_box.max.Y),
            },
        },
        "standoffs": _standoffs(graph, result, base, board_shape),
        "lids": _lids(graph, result, parts, base),
        "ports": ports,
        "assembled": {"bbox": _bbox(assembled), "size": _size(assembled)},
        "terminalParts": [
            {
                "partId": part.part_id,
                "featureId": part.terminal_feature,
                "sourceBody": asdict(part.source_body),
            }
            for part in parts
        ],
        "warnings": warnings,
    }
