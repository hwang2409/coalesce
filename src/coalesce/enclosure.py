"""Semantic enclosure geometry derived directly from persisted board mechanics."""

from __future__ import annotations

import math
from dataclasses import dataclass

from build123d import Align, Box, Compound, Face, Location, Shape, Wire
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.gp import gp_Vec

from .boolean import cut_shape, fuse_shape, join_contact_failures
from .graph import BoardImport, GraphError, PcbBase, PcbLid, PortCutout, Standoffs
from .pcb import board_loop_wire
from .provenance import candidate, derive_edge_candidates
from .references import BodySource, Candidate


@dataclass(frozen=True)
class EnclosureResult:
    shape: Shape
    candidates: list[Candidate]


@dataclass(frozen=True)
class PortCutoutResult(EnclosureResult):
    metadata: dict


def _offset_outward(wire: Wire, distance: float, feature: str, parameter: str) -> Wire:
    wires = []
    for signed_distance in (distance, -distance):
        try:
            offset = wire.offset_2d(signed_distance)
            wires.append(offset if isinstance(offset, Wire) else Wire([offset]))
        except RuntimeError:
            continue
    original_area = Face(wire).area
    outward = max(wires, key=lambda item: Face(item).area, default=None)
    if outward is None or Face(outward).area <= original_area:
        raise GraphError(
            "INVALID_PARAMETER",
            f"{feature} {parameter} cannot offset the board outline outward",
            feature=feature,
            parameter=parameter,
            value=distance,
        )
    return outward


def _offset_inward(wire: Wire, distance: float, feature: str, parameter: str) -> Wire:
    original_area = Face(wire).area
    wires = []
    for signed_distance in (distance, -distance):
        try:
            offset = wire.offset_2d(signed_distance)
            candidate_wire = offset if isinstance(offset, Wire) else Wire([offset])
            area = Face(candidate_wire).area
            if 0 < area < original_area:
                wires.append(candidate_wire)
        except RuntimeError:
            continue
    inward = min(wires, key=lambda item: Face(item).area, default=None)
    if inward is None:
        raise GraphError(
            "INVALID_PARAMETER",
            f"pcb_lid {feature} {parameter} leaves no space inside the walls",
            feature=feature,
            parameter=parameter,
            value=distance,
        )
    return inward


def _prism(
    feature: str,
    wire: Wire,
    z: float,
    height: float,
    body: BodySource,
    source: str,
    bottom_role: str,
    top_role: str,
    side_role: str,
) -> EnclosureResult:
    face = Face(wire).moved(Location((0, 0, z)))
    prism = BRepPrimAPI_MakePrism(face.wrapped, gp_Vec(0, 0, height))
    prism.Build()
    if not prism.IsDone():
        raise GraphError("BOOLEAN_FAILED", f"feature {feature} extrusion failed", feature=feature)
    shape = Shape.cast(prism.Shape())
    faces = [
        candidate(feature, bottom_role, source, Shape.cast(prism.FirstShape()), body=body),
        candidate(feature, top_role, source, Shape.cast(prism.LastShape()), body=body),
    ]
    for edge in face.edges():
        generated = [Shape.cast(item) for item in prism.Generated(edge.wrapped)]
        if len(generated) != 1:
            raise GraphError(
                "HISTORY_MISSING",
                f"feature {feature} extrusion lost wall history",
                feature=feature,
            )
        faces.append(candidate(feature, side_role, source, generated[0], body=body))
    return EnclosureResult(shape, [*faces, *derive_edge_candidates(feature, shape, faces)])


def _base_wires(node: PcbBase, board: BoardImport) -> tuple[str, Wire, Wire]:
    outlines = board.mechanics.get("outline", [])
    if len(outlines) != 1:
        raise GraphError(
            "INVALID_PARAMETER",
            f"pcb_base {node.id} requires one board outline loop",
            feature=node.id,
            parameter="board",
            board=node.board,
        )
    loop = outlines[0]
    board_wire, _ = board_loop_wire(loop)
    inner = _offset_outward(board_wire, node.clearance, node.id, "clearance")
    outer = _offset_outward(inner, node.wall_thickness, node.id, "wallThickness")
    return loop["id"], inner, outer


def build_pcb_base(node: PcbBase, board: BoardImport) -> EnclosureResult:
    loop_id, inner, outer = _base_wires(node, board)
    bottom = -(node.floor_thickness + node.board_seat_height)
    top = board.mechanics["thickness"] + node.wall_height_above_board
    body = BodySource(node.id, loop_id)
    shell = _prism(
        node.id,
        outer,
        bottom,
        top - bottom,
        body,
        loop_id,
        "floor_bottom",
        "rim",
        "outer_wall",
    )
    cavity = _prism(
        node.id,
        inner,
        -node.board_seat_height,
        top + node.board_seat_height + 1,
        body,
        loop_id,
        "floor",
        "cavity_tool_top",
        "inner_wall",
    )
    result = cut_shape(
        node.id,
        shell.shape,
        shell.candidates,
        cavity.shape,
        cavity.candidates,
        kind="semantic",
    )
    return EnclosureResult(result.shape, result.candidates)


def build_pcb_lid(node: PcbLid, base: PcbBase, board: BoardImport) -> EnclosureResult:
    loop_id, inner, outer = _base_wires(base, board)
    rim_z = board.mechanics["thickness"] + base.wall_height_above_board
    body = BodySource(node.id, loop_id)
    slab = _prism(
        node.id,
        outer,
        rim_z,
        node.thickness,
        body,
        node.base,
        "underside",
        "top",
        "edge",
    )
    if node.lip_depth is None or node.lip_width is None:
        return slab

    clearance = rim_z - node.lip_depth - board.mechanics["thickness"]
    if clearance <= 1e-9:
        raise GraphError(
            "INVALID_PARAMETER",
            f"pcb_lid {node.id} lip bottom must stay above the board top",
            feature=node.id,
            parameter="lip.depth",
            measuredDistance=clearance,
            unit="mm",
            conflict="board_top",
        )
    lip_outer = _offset_inward(inner, node.fit_gap, node.id, "fitGap")
    lip_inner = _offset_inward(lip_outer, node.lip_width, node.id, "lip.width")
    lip_z = rim_z - node.lip_depth
    outer_prism = _prism(
        node.id,
        lip_outer,
        lip_z,
        node.lip_depth,
        body,
        node.base,
        "lip_bottom",
        "lip_join",
        "lip_outer",
    )
    inner_tool = _prism(
        node.id,
        lip_inner,
        lip_z - 1,
        node.lip_depth + 2,
        body,
        node.base,
        "lip_tool_bottom",
        "lip_tool_top",
        "lip_inner",
    )
    ring = cut_shape(
        node.id,
        outer_prism.shape,
        outer_prism.candidates,
        inner_tool.shape,
        inner_tool.candidates,
        kind="semantic",
    )
    joined = fuse_shape(node.id, slab.shape, slab.candidates, ring.shape, ring.candidates)
    return EnclosureResult(joined.shape, joined.candidates)


def _resolve_footprint(node: PortCutout, board: BoardImport) -> dict:
    footprints = board.mechanics.get("footprints", [])
    lookup = node.footprint_id or node.footprint
    by_id = [item for item in footprints if item["id"] == lookup]
    matches = by_id or [item for item in footprints if item["reference"] == lookup]
    if not matches:
        raise GraphError(
            "MISSING",
            f"port_cutout {node.id} footprint {node.footprint} is missing",
            feature=node.id,
            parameter="footprint",
            footprint=lookup,
            hint="restore the footprint or update the cutout to a stable footprint id",
        )
    if len(matches) != 1:
        raise GraphError(
            "AMBIGUOUS",
            f"port_cutout {node.id} footprint reference {node.footprint} is ambiguous",
            feature=node.id,
            parameter="footprint",
            footprint=node.footprint,
            candidates=sorted(item["id"] for item in matches),
            hint="use one of the listed stable footprint ids",
        )
    node.footprint_id = matches[0]["id"]
    return matches[0]


def _courtyard_bounds(node: PortCutout, footprint: dict) -> tuple[float, float, float, float]:
    courtyard = footprint.get("courtyard")
    if not courtyard:
        raise GraphError(
            "MISSING",
            f"port_cutout {node.id} footprint {footprint['id']} has no courtyard envelope",
            feature=node.id,
            parameter="envelope",
            footprint=footprint["id"],
            envelopeSource="courtyard",
            hint="provide an explicit envelope with width, height, and bottomOffset",
        )
    try:
        wire, _ = board_loop_wire(
            {
                "id": footprint["id"],
                "closed": True,
                "segments": [
                    {**segment, "id": segment.get("id") or f"courtyard-{index}"}
                    for index, segment in enumerate(courtyard)
                ],
            }
        )
        Face(wire)
    except (RuntimeError, ValueError) as error:
        raise GraphError(
            "INVALID_PARAMETER",
            f"port_cutout {node.id} courtyard is not a closed envelope",
            feature=node.id,
            parameter="envelope",
            footprint=footprint["id"],
            envelopeSource="courtyard",
        ) from error
    box = wire.bounding_box()
    return box.min.X, box.max.X, box.min.Y, box.max.Y


def _wall_choice(
    node: PortCutout,
    footprint: dict,
    footprint_bounds: tuple[float, float, float, float] | None,
    inner: Wire,
) -> str:
    if node.wall != "auto":
        return node.wall
    inner_box = inner.bounding_box()
    if footprint_bounds is None:
        x0 = x1 = footprint["position"][0]
        y0 = y1 = footprint["position"][1]
    else:
        x0, x1, y0, y1 = footprint_bounds
    distances = {
        "left": x0 - inner_box.min.X,
        "right": inner_box.max.X - x1,
        "front": y0 - inner_box.min.Y,
        "back": inner_box.max.Y - y1,
    }
    nearest = min(distances.values())
    candidates = sorted(
        wall for wall, distance in distances.items() if math.isclose(distance, nearest, abs_tol=1e-7)
    )
    if len(candidates) != 1:
        raise GraphError(
            "AMBIGUOUS",
            f"port_cutout {node.id} footprint has no unique nearest wall",
            feature=node.id,
            parameter="wall",
            footprint=footprint["id"],
            candidates=candidates,
            hint="set wall explicitly to left, right, front, or back",
        )
    return candidates[0]


def _opening_tool(
    node: PortCutout,
    footprint: dict,
    wall: str,
    footprint_bounds: tuple[float, float, float, float] | None,
    inner: Wire,
    outer: Wire,
    board_top: float,
    floor_top: float,
    target_candidates: list[Candidate],
) -> tuple[Shape, list[Candidate], dict]:
    if footprint_bounds is None:
        center_x, center_y = footprint["position"]
        width = node.envelope_width
        assert width is not None
    else:
        x0, x1, y0, y1 = footprint_bounds
        center_x, center_y = (x0 + x1) / 2, (y0 + y1) / 2
        width = y1 - y0 if wall in {"left", "right"} else x1 - x0
    opening_width = width + 2 * node.clearance
    opening_height = node.envelope_height + 2 * node.clearance
    envelope_bottom = board_top + node.bottom_offset
    bottom = envelope_bottom - node.clearance
    top = envelope_bottom + node.envelope_height + node.clearance
    if bottom < floor_top and envelope_bottom >= floor_top:
        raise GraphError(
            "INVALID_PARAMETER",
            f"port_cutout {node.id} clearance would cut the base floor",
            feature=node.id,
            parameter="clearance",
            measuredBottom=bottom,
            floorTop=floor_top,
            unit="mm",
            hint="raise bottomOffset or reduce clearance",
        )

    inner_box = inner.bounding_box()
    outer_box = outer.bounding_box()
    margin = 1.0
    if wall in {"left", "right"}:
        tangent_min = center_y - opening_width / 2
        tangent_max = center_y + opening_width / 2
        wall_coordinate = inner_box.min.X if wall == "left" else inner_box.max.X
        if wall == "left":
            minimum_x, maximum_x = outer_box.min.X - margin, inner_box.min.X + margin
        else:
            minimum_x, maximum_x = inner_box.max.X - margin, outer_box.max.X + margin
        minimum_y, maximum_y = tangent_min, tangent_max
        wall_faces = [
            item.entity
            for item in target_candidates
            if item.key.role == "inner_wall"
            and isinstance(item.entity, Face)
            and math.isclose(item.entity.bounding_box().min.X, wall_coordinate, abs_tol=1e-7)
            and math.isclose(item.entity.bounding_box().max.X, wall_coordinate, abs_tol=1e-7)
        ]
        spans = [(face.bounding_box().min.Y, face.bounding_box().max.Y) for face in wall_faces]
    else:
        tangent_min = center_x - opening_width / 2
        tangent_max = center_x + opening_width / 2
        if wall == "front":
            minimum_y, maximum_y = outer_box.min.Y - margin, inner_box.min.Y + margin
        else:
            minimum_y, maximum_y = inner_box.max.Y - margin, outer_box.max.Y + margin
        minimum_x, maximum_x = tangent_min, tangent_max
        wall_coordinate = inner_box.min.Y if wall == "front" else inner_box.max.Y
        wall_faces = [
            item.entity
            for item in target_candidates
            if item.key.role == "inner_wall"
            and isinstance(item.entity, Face)
            and math.isclose(item.entity.bounding_box().min.Y, wall_coordinate, abs_tol=1e-7)
            and math.isclose(item.entity.bounding_box().max.Y, wall_coordinate, abs_tol=1e-7)
        ]
        spans = [(face.bounding_box().min.X, face.bounding_box().max.X) for face in wall_faces]
    if len(spans) != 1:
        raise GraphError(
            "INVALID_PARAMETER",
            f"port_cutout {node.id} wall {wall} is not one planar base wall",
            feature=node.id,
            parameter="wall",
            wall=wall,
        )
    straight_min, straight_max = spans[0]
    if tangent_min < straight_min - 1e-7 or tangent_max > straight_max + 1e-7:
        raise GraphError(
            "INVALID_PARAMETER",
            f"port_cutout {node.id} opening reaches an adjacent or curved wall",
            feature=node.id,
            parameter="envelope",
            wall=wall,
            openingSpan=[tangent_min, tangent_max],
            wallSpan=[straight_min, straight_max],
        )

    tool = Box(
        maximum_x - minimum_x,
        maximum_y - minimum_y,
        top - bottom,
        align=(Align.MIN, Align.MIN, Align.MIN),
    ).moved(Location((minimum_x, minimum_y, bottom)))
    tool_body = BodySource(node.id, footprint["id"])
    candidates = []
    for face in tool.faces():
        box = face.bounding_box()
        if math.isclose(box.min.Z, bottom, abs_tol=1e-7) and math.isclose(box.max.Z, bottom, abs_tol=1e-7):
            role = "port_bottom"
        elif math.isclose(box.min.Z, top, abs_tol=1e-7) and math.isclose(box.max.Z, top, abs_tol=1e-7):
            role = "port_top"
        else:
            role = "port_side"
        candidates.append(candidate(node.id, role, footprint["id"], face, body=tool_body))
    metadata = {
        "footprintId": footprint["id"],
        "reference": footprint["reference"],
        "wall": wall,
        "envelopeSource": node.envelope_source,
        "opening": {
            "width": {"value": round(opening_width, 12), "unit": "mm"},
            "height": {"value": round(opening_height, 12), "unit": "mm"},
            "bottom": {"value": round(bottom, 12), "unit": "mm"},
            "top": {"value": round(top, 12), "unit": "mm"},
        },
        "cutsBelowBoardTop": bottom < board_top,
    }
    if envelope_bottom < floor_top:
        metadata["cutsFloor"] = True
    return tool, candidates, metadata


def build_port_cutout(
    node: PortCutout,
    base: PcbBase,
    board: BoardImport,
    target_shape: Shape,
    target_candidates: list[Candidate],
) -> PortCutoutResult:
    footprint = _resolve_footprint(node, board)
    footprint_bounds = (
        _courtyard_bounds(node, footprint)
        if node.envelope_source == "courtyard"
        else None
    )
    _, inner, outer = _base_wires(base, board)
    wall = _wall_choice(node, footprint, footprint_bounds, inner)
    board_top = board.mechanics["thickness"]
    tool, tool_candidates, metadata = _opening_tool(
        node,
        footprint,
        wall,
        footprint_bounds,
        inner,
        outer,
        board_top,
        -base.board_seat_height,
        target_candidates,
    )
    for item in target_candidates:
        if item.key.role.startswith(("standoff_", "pilot_")) and item.entity.distance_to(tool) <= 1e-7:
            raise GraphError(
                "INVALID_PARAMETER",
                f"port_cutout {node.id} opening intersects a standoff",
                feature=node.id,
                parameter="envelope",
                footprint=footprint["id"],
                conflict=item.key.source,
            )
    result = cut_shape(
        node.id,
        target_shape,
        target_candidates,
        tool,
        tool_candidates,
        kind="semantic",
    )
    if not result.removed_material:
        raise GraphError(
            "INVALID_PARAMETER",
            f"port_cutout {node.id} does not intersect wall {wall}",
            feature=node.id,
            parameter="wall",
            wall=wall,
            footprint=footprint["id"],
        )
    return PortCutoutResult(result.shape, result.candidates, metadata)


def selected_holes(node: Standoffs, board: BoardImport) -> list[dict]:
    holes = {hole["id"]: hole for hole in board.mechanics.get("holes", [])}
    if node.selection == "explicit":
        missing = next((hole_id for hole_id in node.hole_ids if hole_id not in holes), None)
        if missing is not None:
            raise GraphError(
                "MISSING",
                f"standoffs {node.id} board hole {missing} is missing",
                feature=node.id,
                parameter="selection.holeIds",
                hole=missing,
                board=node.board,
            )
        return [holes[hole_id] for hole_id in node.hole_ids]
    assert node.minimum_diameter is not None
    return [
        hole
        for hole in holes.values()
        if min(hole["drill"]["width"], hole["drill"]["height"])
        >= node.minimum_diameter
    ]


def _common_area(first: Face, second: Face) -> float:
    common = BRepAlgoAPI_Common(first.wrapped, second.wrapped)
    common.Build()
    if not common.IsDone():
        return 0
    return sum(face.area for face in Shape.cast(common.Shape()).faces())


def _validate_bosses(node: Standoffs, cavity: Face, holes: list[dict]) -> None:
    radius = node.boss_diameter / 2
    for hole in holes:
        x, y = hole["position"]
        disk = Face(Wire.make_circle(radius).moved(Location((x, y, 0))))
        if not cavity.is_inside((x, y, 0)):
            message = f"standoffs {node.id} hole {hole['id']} is off the base floor"
        elif math.isclose(_common_area(cavity, disk), disk.area, rel_tol=1e-9, abs_tol=1e-8):
            continue
        else:
            message = f"standoffs {node.id} boss at hole {hole['id']} crosses a wall"
        raise GraphError(
            "INVALID_PARAMETER",
            message,
            feature=node.id,
            parameter="bossDiameter",
            hole=hole["id"],
        )


def _cylinders(
    node: Standoffs,
    holes: list[dict],
    diameter: float,
    z: float,
    height: float,
    roles: tuple[str, str, str],
) -> EnclosureResult:
    built = []
    for hole in holes:
        x, y = hole["position"]
        wire = Wire.make_circle(diameter / 2).moved(Location((x, y, 0)))
        built.append(
            _prism(
                node.id,
                wire,
                z,
                height,
                BodySource(node.id, hole["id"]),
                hole["id"],
                roles[0],
                roles[1],
                roles[2],
            )
        )
    shape = built[0].shape if len(built) == 1 else Compound(children=[item.shape for item in built])
    return EnclosureResult(shape, [candidate for item in built for candidate in item.candidates])


def build_standoffs(
    node: Standoffs,
    base: PcbBase,
    board: BoardImport,
    target_shape: Shape,
    target_candidates: list[Candidate],
) -> EnclosureResult:
    holes = selected_holes(node, board)
    if not holes:
        raise GraphError(
            "MISSING",
            f"standoffs {node.id} selection contains no board holes",
            feature=node.id,
            parameter="selection",
            board=node.board,
        )
    _, inner, _ = _base_wires(base, board)
    _validate_bosses(node, Face(inner), holes)
    bosses = _cylinders(
        node,
        holes,
        node.boss_diameter,
        -base.board_seat_height,
        base.board_seat_height,
        ("standoff_bottom", "standoff_top", "standoff_side"),
    )
    unjoined, non_manifold = join_contact_failures(
        node.id, target_shape, bosses.shape, bosses.candidates
    )
    if unjoined or non_manifold:
        raise GraphError(
            "INVALID_PARAMETER",
            f"standoffs {node.id} bosses do not join the base floor",
            feature=node.id,
            parameter="bossDiameter",
            holes=unjoined or non_manifold,
        )
    joined = fuse_shape(
        node.id, target_shape, target_candidates, bosses.shape, bosses.candidates
    )
    pilots = _cylinders(
        node,
        holes,
        node.pilot_diameter,
        -base.board_seat_height,
        base.board_seat_height + 1,
        ("pilot_bottom", "pilot_top", "pilot_wall"),
    )
    cut = cut_shape(
        node.id,
        joined.shape,
        joined.candidates,
        pilots.shape,
        pilots.candidates,
        kind="semantic",
    )
    return EnclosureResult(cut.shape, cut.candidates)
