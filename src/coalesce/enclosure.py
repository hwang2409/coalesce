"""Semantic enclosure geometry derived directly from persisted board mechanics."""

from __future__ import annotations

import math
from dataclasses import dataclass

from build123d import Compound, Face, Location, Shape, Wire
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.gp import gp_Vec

from .boolean import cut_shape, fuse_shape, join_contact_failures
from .graph import BoardImport, GraphError, PcbBase, PcbLid, Standoffs
from .pcb import board_loop_wire
from .provenance import candidate, derive_edge_candidates
from .references import BodySource, Candidate


@dataclass(frozen=True)
class EnclosureResult:
    shape: Shape
    candidates: list[Candidate]


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
