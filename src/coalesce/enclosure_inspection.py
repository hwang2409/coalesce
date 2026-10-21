from __future__ import annotations

from dataclasses import asdict
from typing import Any

from build123d import Align, Box, Compound, Cylinder, Face, Location, Shape
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.Standard import Standard_Failure

from .board_import import mounting_holes
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


def _overlaps(first: Face, second: Face, axes: str) -> bool:
    first_box = first.bounding_box()
    second_box = second.bounding_box()
    return all(
        min(getattr(first_box.max, axis), getattr(second_box.max, axis))
        - max(getattr(first_box.min, axis), getattr(second_box.min, axis))
        > _TOLERANCE
        for axis in axes
    )


def _planar_coordinate(face: Face, axis: str) -> float | None:
    box = face.bounding_box()
    low = getattr(box.min, axis)
    high = getattr(box.max, axis)
    return low if high - low <= _TOLERANCE else None


def _same_face(face: Face, candidates: list[Face]) -> bool:
    return any(face.is_same(candidate) for candidate in candidates)


def _adjacent(first: Face, second: Face) -> bool:
    return any(edge.is_same(other) for edge in first.edges() for other in second.edges())


def _face_components(faces: list[Face]) -> list[list[Face]]:
    remaining = list(faces)
    components = []
    while remaining:
        component = [remaining.pop()]
        for face in component:
            connected = [candidate for candidate in remaining if _adjacent(face, candidate)]
            component.extend(connected)
            remaining = [candidate for candidate in remaining if candidate not in connected]
        components.append(component)
    return components


def _touches_any(component: list[Face], candidates: list[Face]) -> bool:
    return any(_adjacent(face, candidate) for face in component for candidate in candidates)


def _classify_component(
    component: list[Face], groups: dict[str, list[Face]], measured: list[str]
) -> None:
    touched = {group for group in measured if _touches_any(component, groups[group])}
    sides = {group.rsplit("_", 1)[1] for group in touched}
    if len(sides) != 1:
        groups["unclassified"].extend(component)
        return
    for group in touched:
        groups[group].extend(component)


def _neutral_boundary_faces(result: Any, base: PcbBase) -> list[Face]:
    neutral = _faces(result, base.id, "rim")
    ports = [
        node
        for node in result.graph.ordered()
        if isinstance(node, PortCutout) and node.base == base.id
    ]
    for port in ports:
        for role in ("port_bottom", "port_side", "port_top"):
            neutral.extend(_faces(result, port.id, role))
    return neutral


def _classify_boundary_faces(result: Any, base: PcbBase, shape: Shape) -> dict[str, list[Face]]:
    roles = {
        "inner_wall": "wall_inside",
        "outer_wall": "wall_outside",
        "floor": "floor_inside",
        "floor_bottom": "floor_outside",
    }
    groups = {group: _faces(result, base.id, role) for role, group in roles.items()}
    groups["unclassified"] = []
    seeds = [face for faces in groups.values() for face in faces]
    neutral = _neutral_boundary_faces(result, base)
    unclassified = [face for face in shape.faces() if not _same_face(face, [*seeds, *neutral])]
    for component in _face_components(unclassified):
        _classify_component(component, groups, list(roles.values()))
    return groups


def _minimum_distance(first: list[Face], second: list[Face]) -> float:
    if not first or not second:
        raise ValueError("final boundary face set is empty")
    extrema = BRepExtrema_DistShapeShape(
        Compound(children=first).wrapped,
        Compound(children=second).wrapped,
    )
    extrema.Perform()
    if not extrema.IsDone():
        raise ValueError("OCCT distance measurement did not complete")
    return extrema.Value()


def _thickness(groups: dict[str, list[Face]], kind: str) -> dict[str, Any]:
    try:
        if groups["unclassified"]:
            raise ValueError("final boundary faces could not be classified")
        return _quantity(_minimum_distance(groups[f"{kind}_inside"], groups[f"{kind}_outside"]))
    except (RuntimeError, Standard_Failure, ValueError) as error:
        return {"status": "unknown", "reason": str(error) or type(error).__name__}


def _as_built_thicknesses(
    result: Any, base: PcbBase, shape: Shape
) -> tuple[dict[str, Any], dict[str, Any]]:
    groups = _classify_boundary_faces(result, base, shape)
    return _thickness(groups, "wall"), _thickness(groups, "floor")


def _part_for(parts: list[TerminalPart], source_feature: str) -> TerminalPart:
    matches = [part for part in parts if part.source_body.feature == source_feature]
    if len(matches) != 1:
        raise ValueError(f"feature {source_feature} does not resolve to one terminal part")
    return matches[0]


def _standoff_probe(node: Standoffs, hole: dict[str, Any], bottom: float, top: float) -> Shape:
    x, y = hole["position"]
    height = top - bottom
    boss = Cylinder(
        node.boss_diameter / 2,
        height,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    ).moved(Location((x, y, bottom)))
    pilot = Cylinder(
        node.pilot_diameter / 2,
        height,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    ).moved(Location((x, y, bottom)))
    return boss - pilot


def _classify_standoff(expected: Shape, remaining: Shape) -> str:
    if remaining.volume <= _TOLERANCE:
        return "missing"
    if abs(expected.volume - remaining.volume) <= max(_TOLERANCE, expected.volume * 1e-7):
        return "intact"
    return "partial"


def _measure_standoff(
    node: Standoffs, hole: dict[str, Any], base_shape: Shape, bottom: float, top: float
) -> dict[str, Any]:
    expected = _standoff_probe(node, hole, bottom, top)
    remaining = _common_shape(expected, base_shape)
    classification = _classify_standoff(expected, remaining)
    x, y = hole["position"]
    return {
        "holeId": hole["id"],
        "axis": _point((x, y, bottom)),
        "classification": classification,
        "measured": {
            "remainingVolume": {**_quantity(remaining.volume), "unit": "mm^3"},
            "topZ": _quantity(remaining.bounding_box().max.Z) if remaining.volume > _TOLERANCE else None,
        },
        "designed": {
            "bossDiameter": _quantity(node.boss_diameter),
            "pilotDiameter": _quantity(node.pilot_diameter),
            "topZ": _quantity(top),
        },
        "boardUndersideZ": _quantity(top),
    }


def _standoff_items(
    nodes: list[Standoffs],
    holes_by_id: dict[str, dict[str, Any]],
    base_shape: Shape,
    floor_top: float,
    board_bottom: float,
) -> list[dict[str, Any]]:
    return [
        _measure_standoff(node, holes_by_id[source], base_shape, floor_top, board_bottom)
        for node in nodes
        for source in (
            node.hole_ids if node.selection == "explicit" else tuple(holes_by_id)
        )
        if source in holes_by_id
    ]


def _standoffs(
    graph: Any, result: Any, base: PcbBase, board_shape: Shape, base_shape: Shape
) -> dict[str, Any]:
    board = graph.nodes[base.board]
    holes = mounting_holes(board)
    holes_by_id = {hole["id"]: hole for hole in holes}
    board_bottom = board_shape.bounding_box().min.Z
    floor_top = min(face.bounding_box().min.Z for face in _faces(result, base.id, "floor"))
    nodes = [
        node for node in graph.ordered() if isinstance(node, Standoffs) and node.target == base.id
    ]
    items = _standoff_items(nodes, holes_by_id, base_shape, floor_top, board_bottom)
    intact = {item["holeId"] for item in items if item["classification"] == "intact"}
    hole_ids = [hole["id"] for hole in holes]
    return {
        "count": len(items),
        "mountingHoleCount": len(holes),
        "holeIds": hole_ids,
        "references": {hole["id"]: hole.get("reference") for hole in holes if hole.get("reference")},
        "coveredHoleIds": [hole_id for hole_id in hole_ids if hole_id in intact],
        "uncoveredHoleIds": [hole_id for hole_id in hole_ids if hole_id not in intact],
        "items": items,
    }


def _parallel_gap(reference: Face, shape: Shape) -> float | None:
    axes = [axis for axis in "XYZ" if _planar_coordinate(reference, axis) is not None]
    if len(axes) != 1:
        return None
    axis = axes[0]
    tangent_axes = "YZ" if axis == "X" else "XZ" if axis == "Y" else "XY"
    candidates = [
        face
        for face in shape.faces()
        if _planar_coordinate(face, axis) is not None and _overlaps(reference, face, tangent_axes)
    ]
    return _positive_distance(reference, candidates)


def _interference(first: Shape, second: Shape, parts: list[str]) -> dict[str, Any] | None:
    volume = _common_volume(first, second)
    if volume <= _TOLERANCE:
        return None
    return {"volume": {"value": round(volume, 12), "unit": "mm^3"}, "parts": parts}


def _directional_clearance(
    board_shape: Shape, base_shape: Shape, axis: str, coordinate: float, outward: int
) -> float:
    tangent_axes = "YZ" if axis == "X" else "XZ"
    board_faces = [
        face
        for face in board_shape.faces()
        if (value := _planar_coordinate(face, axis)) is not None
        and abs(value - coordinate) <= _TOLERANCE
    ]
    distances = [
        (candidate_coordinate - coordinate) * outward
        for board_face in board_faces
        for candidate in base_shape.faces()
        if (candidate_coordinate := _planar_coordinate(candidate, axis)) is not None
        and _overlaps(board_face, candidate, tangent_axes)
        and (candidate_coordinate - coordinate) * outward >= -_TOLERANCE
    ]
    if not distances:
        raise ValueError("terminal base has no wall beside board")
    return max(0.0, min(distances))


def _optional_clearance(
    board_shape: Shape, base_shape: Shape, axis: str, coordinate: float, outward: int
) -> float | None:
    try:
        return _directional_clearance(board_shape, base_shape, axis, coordinate, outward)
    except ValueError:
        return None


def _board_fit(
    board_id: str, board_shape: Shape, base_part: TerminalPart
) -> tuple[dict[str, float | None], dict[str, float | None], Any]:
    box = board_shape.bounding_box()
    clearances = {
        "left": _optional_clearance(board_shape, base_part.shape, "X", box.min.X, -1),
        "right": _optional_clearance(board_shape, base_part.shape, "X", box.max.X, 1),
        "front": _optional_clearance(board_shape, base_part.shape, "Y", box.min.Y, -1),
        "back": _optional_clearance(board_shape, base_part.shape, "Y", box.max.Y, 1),
    }
    interference = _interference(
        board_shape, base_part.shape, [board_id, base_part.part_id]
    )
    reported = (
        dict.fromkeys(("left", "right", "front", "back"), 0.0)
        if interference is not None
        else clearances
    )
    return clearances, reported, interference


def _clearance_quantity(value: float | None) -> dict[str, Any]:
    if value is None:
        return {"status": "unknown", "reason": "no directional wall face could be measured"}
    return _quantity(value)


def _inner_size(board_size: float, first: float | None, second: float | None) -> dict[str, Any]:
    if first is None or second is None:
        return {"status": "unknown", "reason": "board clearances could not be measured"}
    return _quantity(board_size + first + second)


def _lid_measurement(
    result: Any, parts: list[TerminalPart], node: PcbLid, base_part: TerminalPart
) -> dict[str, Any]:
    part = _part_for(parts, node.id)
    undersides = _faces(result, node.id, "underside")
    lips = _faces(result, node.id, "lip_outer")
    interference = _interference(base_part.shape, part.shape, [base_part.part_id, part.part_id])
    gaps = [gap for lip in lips if (gap := _parallel_gap(lip, base_part.shape)) is not None]
    if interference is not None:
        gaps = [0.0]
    lip_box = Compound(children=lips).bounding_box() if lips else None
    return {
        "featureId": node.id,
        "partId": part.part_id,
        "outerBBox": _bbox(part.shape),
        "seatingZ": _quantity(min(face.bounding_box().min.Z for face in undersides)),
        "fitGap": (
            {"min": _quantity(min(gaps)), "max": _quantity(max(gaps))}
            if gaps
            else {"status": "unknown", "reason": "no parallel lip and base faces could be measured"}
        ),
        "interference": interference,
        "lipDepth": _quantity(lip_box.max.Z - lip_box.min.Z) if lip_box is not None else None,
    }


def _lids(
    graph: Any, result: Any, parts: list[TerminalPart], base: PcbBase, base_part: TerminalPart
) -> list[dict[str, Any]]:
    return [
        _lid_measurement(result, parts, node, base_part)
        for node in graph.ordered()
        if isinstance(node, PcbLid) and node.base == base.id
    ]


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


def _wall_slab(base_shape: Shape, inner_walls: list[Face], wall: str) -> Shape:
    outer = base_shape.bounding_box()
    inner = Compound(children=inner_walls).bounding_box()
    if wall == "left":
        size = (inner.min.X - outer.min.X, inner.max.Y - inner.min.Y, inner.max.Z - inner.min.Z)
        origin = (outer.min.X, inner.min.Y, inner.min.Z)
    elif wall == "right":
        size = (outer.max.X - inner.max.X, inner.max.Y - inner.min.Y, inner.max.Z - inner.min.Z)
        origin = (inner.max.X, inner.min.Y, inner.min.Z)
    elif wall == "front":
        size = (inner.max.X - inner.min.X, inner.min.Y - outer.min.Y, inner.max.Z - inner.min.Z)
        origin = (inner.min.X, outer.min.Y, inner.min.Z)
    else:
        size = (inner.max.X - inner.min.X, outer.max.Y - inner.max.Y, inner.max.Z - inner.min.Z)
        origin = (inner.min.X, inner.max.Y, inner.min.Z)
    return Box(*size, align=(Align.MIN, Align.MIN, Align.MIN)).moved(Location(origin))


def _actual_opening(base_shape: Shape, inner_walls: list[Face], tool: Shape, wall: str) -> Shape:
    void = _wall_slab(base_shape, inner_walls, wall) - base_shape
    matches = [solid for solid in void.solids() if solid.distance_to(tool) <= _TOLERANCE]
    if not matches:
        raise ValueError("terminal base has no aperture for port")
    return max(matches, key=lambda solid: solid.volume)


def _port_clearances(
    node: PortCutout, footprint: dict[str, Any], board_shape: Shape, opening: Shape, wall: str
) -> list[float]:
    metadata_box = opening.bounding_box()
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


def _common_shape(first: Shape, second: Shape) -> Shape:
    operation = BRepAlgoAPI_Common(first.wrapped, second.wrapped)
    operation.Build()
    if not operation.IsDone():
        raise ValueError("solid intersection measurement failed")
    return Shape.cast(operation.Shape())


def _common_volume(first: Shape, second: Shape) -> float:
    return _common_shape(first, second).volume


def _ports(
    graph: Any,
    result: Any,
    base: PcbBase,
    board: BoardImport,
    board_shape: Shape,
    base_shape: Shape,
    assembled: Shape,
) -> list[dict[str, Any]]:
    inspected = []
    inner_walls = _faces(result, base.id, "inner_wall")
    footprints = {item["id"]: item for item in board.mechanics.get("footprints", [])}
    for node in graph.ordered():
        if not isinstance(node, PortCutout) or node.base != base.id:
            continue
        metadata = result.metadata[node.id]
        wall = metadata["wall"]
        opening = _actual_opening(base_shape, inner_walls, result.opening_tools[node.id], wall)
        footprint = footprints[node.footprint_id]
        clearances = _port_clearances(node, footprint, board_shape, opening, wall)
        inspected.append(
            {
                "featureId": node.id,
                "footprintId": metadata["footprintId"],
                "reference": metadata["reference"],
                "libraryId": footprint.get("libraryId"),
                "value": footprint.get("value"),
                "wall": wall,
                "opening": _opening_rectangle(opening, wall),
                "clearance": {
                    "min": _quantity(min(clearances)),
                    "max": _quantity(max(clearances)),
                },
                "blocked": _common_volume(opening, assembled) > _TOLERANCE,
            }
        )
    return inspected


def _other_body_warnings(
    parts: list[TerminalPart], enclosure_parts: list[TerminalPart]
) -> list[dict[str, Any]]:
    selected_ids = {part.part_id for part in enclosure_parts}
    return [
        {
            "code": "OTHER_MANUFACTURING_BODY",
            "featureId": part.terminal_feature,
            "partId": part.part_id,
        }
        for part in parts
        if part.part_id not in selected_ids
    ]


def _unclassified_hole_ids(board: BoardImport) -> list[str]:
    mounting_ids = {hole["id"] for hole in mounting_holes(board)}
    return [
        hole["id"] for hole in board.mechanics.get("holes", []) if hole["id"] not in mounting_ids
    ]


def _warnings(
    graph: Any,
    board: BoardImport,
    parts: list[TerminalPart],
    enclosure_parts: list[TerminalPart],
    ports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    warnings = _other_body_warnings(parts, enclosure_parts)
    if ports and board.availability.get("models") != "declared":
        warnings.append(
            {
                "code": "CONNECTOR_HEIGHT_UNRESOLVED",
                "footprintIds": [port["footprintId"] for port in ports],
            }
        )
    unclassified_ids = _unclassified_hole_ids(board)
    if unclassified_ids:
        warnings.append({"code": "UNCLASSIFIED_BOARD_HOLES", "holeIds": unclassified_ids})
    return warnings


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
    wall_thickness, floor_thickness = _as_built_thicknesses(result, base, base_part.shape)
    wall_clearance, board_clearance, board_interference = _board_fit(
        board.id, board_shape, base_part
    )
    ports = _ports(graph, result, base, board, board_shape, base_part.shape, assembled)
    warnings = _warnings(graph, board, parts, enclosure_parts, ports)

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
            "minimumWallThickness": wall_thickness,
            "floorThickness": floor_thickness,
        },
        "cavity": {
            "innerSize": {
                "x": _inner_size(
                    board_box.max.X - board_box.min.X,
                    wall_clearance["left"],
                    wall_clearance["right"],
                ),
                "y": _inner_size(
                    board_box.max.Y - board_box.min.Y,
                    wall_clearance["front"],
                    wall_clearance["back"],
                ),
            },
            "boardClearance": {
                direction: _clearance_quantity(distance)
                for direction, distance in board_clearance.items()
            },
            "boardInterference": board_interference,
        },
        "standoffs": _standoffs(graph, result, base, board_shape, base_part.shape),
        "lids": _lids(graph, result, parts, base, base_part),
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
