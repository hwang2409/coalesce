"""Semantic enclosure geometry derived directly from persisted board mechanics."""

from __future__ import annotations

import math
from dataclasses import dataclass

from build123d import Align, Box, Compound, Cylinder, Face, GeomType, Location, Shape, Vertex, Wire
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeOffset
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.GeomAbs import GeomAbs_JoinType
from OCP.gp import gp_Vec

from .board_import import mounting_holes
from .boolean import cut_shape, fuse_shape, join_contact_failures
from .graph import (
    BoardImport,
    GraphError,
    Hole,
    HolePosition,
    PcbBase,
    PcbLid,
    PortCutout,
    ScrewClosure,
    Standoffs,
)
from .hole import make_hole_tool
from .lid_outline import OutlineProfile
from .pcb import board_loop_wire
from .provenance import candidate, derive_edge_candidates
from .references import BodySource, Candidate, ElementKey


@dataclass(frozen=True)
class EnclosureResult:
    shape: Shape
    candidates: list[Candidate]


@dataclass(frozen=True)
class PortCutoutResult(EnclosureResult):
    metadata: dict
    opening_tool: Shape


@dataclass(frozen=True)
class ScrewClosureResult:
    base: EnclosureResult
    lid: EnclosureResult


_ProfileWire = OutlineProfile


_REMEDY_STEP = 0.01


def _safe_minimum(value: float) -> float:
    # Keep remedies inside the valid range instead of on a kernel contact boundary.
    return math.ceil((value + 1e-9) / _REMEDY_STEP) * _REMEDY_STEP


def _safe_maximum(value: float) -> float:
    return math.floor((value - 1e-9) / _REMEDY_STEP) * _REMEDY_STEP


def _limits(
    parameter: str,
    current: float,
    *,
    minimum: float,
    maximum: float,
    suggested: float | None = None,
) -> dict:
    safe_minimum = _safe_minimum(minimum)
    safe_maximum = _safe_maximum(maximum)
    proposal = maximum if suggested is None else suggested
    safe_suggested = min(safe_maximum, max(safe_minimum, round(proposal, 2)))
    return {
        "parameter": parameter,
        "current": round(current, 2),
        "minimum": round(safe_minimum, 2),
        "maximum": round(safe_maximum, 2),
        "suggested": round(safe_suggested, 2),
        "unit": "mm",
    }


def _offset_profile(
    profile: _ProfileWire,
    distance: float,
    join: GeomAbs_JoinType = GeomAbs_JoinType.GeomAbs_Arc,
) -> _ProfileWire | None:
    singleton_source = profile.edges[0][0] if len(profile.edges) == 1 else None
    if singleton_source is not None:
        source, edge = profile.edges[0]
        edges = [edge.trim(0.0, 0.5), edge.trim(0.5, 1.0)]
        profile = _ProfileWire(Wire(edges), [(source, item) for item in edges])
    operation = BRepOffsetAPI_MakeOffset()
    operation.Init(join)
    operation.AddWire(profile.wire.wrapped)
    operation.Perform(distance)
    if not operation.IsDone():
        return None
    shape = Shape.cast(operation.Shape())
    wires = shape.wires()
    if len(wires) != 1:
        return None
    wire = wires[0]
    generated: list[tuple[str, Shape]] = []
    for source, edge in profile.edges:
        generated.extend((source, Shape.cast(item)) for item in operation.Generated(edge.wrapped))
    for vertex in profile.wire.vertices():
        adjacent = sorted(
            {
                source
                for source, edge in profile.edges
                if any(vertex.is_same(item) for item in edge.vertices())
            }
        )
        if len(adjacent) == 2:
            source = f"between({adjacent[0]},{adjacent[1]})"
            generated.extend(
                (source, Shape.cast(item)) for item in operation.Generated(vertex.wrapped)
            )
    edges = (
        [(singleton_source, edge) for edge in wire.edges()]
        if singleton_source is not None
        else [
            (source, output)
            for source, output in generated
            if any(output.is_same(edge) for edge in wire.edges())
        ]
    )
    if len(edges) != len(wire.edges()):
        return None
    return _ProfileWire(wire, edges)


def _offset_outward(
    profile: _ProfileWire,
    distance: float,
    feature: str,
    parameter: str,
    *,
    join: GeomAbs_JoinType = GeomAbs_JoinType.GeomAbs_Arc,
) -> _ProfileWire:
    offsets = [
        offset
        for signed_distance in (distance, -distance)
        if (offset := _offset_profile(profile, signed_distance, join)) is not None
    ]
    original_area = Face(profile.wire).area
    outward = max(offsets, key=lambda item: Face(item.wire).area, default=None)
    if outward is None or Face(outward.wire).area <= original_area:
        raise GraphError(
            "INVALID_PARAMETER",
            f"{feature} {parameter} cannot offset the board outline outward",
            feature=feature,
            parameter=parameter,
            value=distance,
        )
    return outward


def _inward_wire(profile: _ProfileWire, distance: float) -> _ProfileWire | None:
    original_area = Face(profile.wire).area
    offsets = [
        offset
        for signed_distance in (distance, -distance)
        if (offset := _offset_profile(profile, signed_distance)) is not None
        and 0 < Face(offset.wire).area < original_area
    ]
    return min(offsets, key=lambda item: Face(item.wire).area, default=None)


def _maximum_inward_offset(profile: _ProfileWire, failed_distance: float) -> float:
    low, high = 0.0, failed_distance
    for _ in range(40):
        middle = (low + high) / 2
        if _inward_wire(profile, middle) is None:
            high = middle
        else:
            low = middle
    return low


def _offset_inward(
    profile: _ProfileWire, distance: float, feature: str, parameter: str
) -> _ProfileWire:
    inward = _inward_wire(profile, distance)
    if inward is None:
        maximum = _maximum_inward_offset(profile, distance)
        details = {}
        if maximum > 0:
            details["limits"] = _limits(
                parameter,
                distance,
                minimum=0,
                maximum=maximum,
                suggested=maximum / 2,
            )
        raise GraphError(
            "INVALID_PARAMETER",
            f"pcb_lid {feature} {parameter} leaves no space inside the walls",
            feature=feature,
            parameter=parameter,
            value=distance,
            **details,
        )
    return inward


def _prism(
    feature: str,
    profile: _ProfileWire,
    z: float,
    height: float,
    body: BodySource,
    source: str,
    bottom_role: str,
    top_role: str,
    side_role: str,
) -> EnclosureResult:
    location = Location((0, 0, z))
    face = Face(profile.wire).moved(location)
    prism = BRepPrimAPI_MakePrism(face.wrapped, gp_Vec(0, 0, height))
    prism.Build()
    if not prism.IsDone():
        raise GraphError("BOOLEAN_FAILED", f"feature {feature} extrusion failed", feature=feature)
    shape = Shape.cast(prism.Shape())
    faces = [
        candidate(feature, bottom_role, source, Shape.cast(prism.FirstShape()), body=body),
        candidate(feature, top_role, source, Shape.cast(prism.LastShape()), body=body),
    ]
    for source_id, source_edge in profile.edges:
        edge = source_edge.moved(location)
        generated = [Shape.cast(item) for item in prism.Generated(edge.wrapped)]
        if len(generated) != 1:
            raise GraphError(
                "HISTORY_MISSING",
                f"feature {feature} extrusion lost wall history",
                feature=feature,
            )
        faces.append(candidate(feature, side_role, source_id, generated[0], body=body))
    return EnclosureResult(shape, [*faces, *derive_edge_candidates(feature, shape, faces)])


def _base_wires(node: PcbBase, board: BoardImport) -> tuple[str, _ProfileWire, _ProfileWire]:
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
    board_wire, board_edges = board_loop_wire(loop)
    profile = _ProfileWire(board_wire, [(source, edge) for source, edge in board_edges])
    inner = _offset_outward(profile, node.clearance, node.id, "clearance")
    outer = _offset_outward(
        profile,
        node.clearance + node.wall_thickness,
        node.id,
        "wallThickness",
        join=(
            GeomAbs_JoinType.GeomAbs_Intersection
            if all(edge.geom_type is GeomType.LINE for _, edge in profile.edges)
            else GeomAbs_JoinType.GeomAbs_Arc
        ),
    )
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


def build_pcb_lid(
    node: PcbLid,
    base: PcbBase,
    board: BoardImport,
    outline: OutlineProfile | None = None,
) -> EnclosureResult:
    loop_id, inner, board_outer = _base_wires(base, board)
    outer = outline or board_outer
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
        maximum = base.wall_height_above_board
        raise GraphError(
            "INVALID_PARAMETER",
            f"pcb_lid {node.id} lip bottom must stay above the board top",
            feature=node.id,
            parameter="lip.depth",
            measuredDistance=clearance,
            unit="mm",
            conflict="board_top",
            limits=_limits(
                "lip.depth",
                node.lip_depth,
                minimum=0,
                maximum=maximum,
                suggested=maximum / 2,
            ),
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
    # the stored id first; if the board replaced that id, the original reference again
    matches = (
        [item for item in footprints if item["id"] == node.footprint_id]
        or [item for item in footprints if item["id"] == node.footprint]
        or [item for item in footprints if item["reference"] == node.footprint]
    )
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
        wall
        for wall, distance in distances.items()
        if math.isclose(distance, nearest, abs_tol=1e-7)
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


def _wall_span(
    target_candidates: list[Candidate], axis: str, coordinate: float
) -> tuple[float, float] | None:
    tangent_axis = "Y" if axis == "X" else "X"
    spans = []
    for item in target_candidates:
        if item.key.role != "inner_wall" or not isinstance(item.entity, Face):
            continue
        box = item.entity.bounding_box()
        if math.isclose(getattr(box.min, axis), coordinate, abs_tol=1e-7) and math.isclose(
            getattr(box.max, axis), coordinate, abs_tol=1e-7
        ):
            spans.append((getattr(box.min, tangent_axis), getattr(box.max, tangent_axis)))
    return spans[0] if len(spans) == 1 else None


def _opening_xy_bounds(
    wall: str,
    center_x: float,
    center_y: float,
    opening_width: float,
    inner: Wire,
    outer: Wire,
    target_candidates: list[Candidate],
) -> tuple[tuple[float, float, float, float], tuple[float, float] | None]:
    inner_box = inner.bounding_box()
    outer_box = outer.bounding_box()
    tangent_center = center_y if wall in {"left", "right"} else center_x
    tangent_min = tangent_center - opening_width / 2
    tangent_max = tangent_center + opening_width / 2
    margin = 1.0
    if wall == "left":
        bounds = (outer_box.min.X - margin, inner_box.min.X + margin, tangent_min, tangent_max)
        span = _wall_span(target_candidates, "X", inner_box.min.X)
    elif wall == "right":
        bounds = (inner_box.max.X - margin, outer_box.max.X + margin, tangent_min, tangent_max)
        span = _wall_span(target_candidates, "X", inner_box.max.X)
    elif wall == "front":
        bounds = (tangent_min, tangent_max, outer_box.min.Y - margin, inner_box.min.Y + margin)
        span = _wall_span(target_candidates, "Y", inner_box.min.Y)
    else:
        bounds = (tangent_min, tangent_max, inner_box.max.Y - margin, outer_box.max.Y + margin)
        span = _wall_span(target_candidates, "Y", inner_box.max.Y)
    return bounds, span


@dataclass(frozen=True)
class _OpeningBounds:
    minimum_x: float
    maximum_x: float
    minimum_y: float
    maximum_y: float
    bottom: float
    top: float
    width: float
    height: float
    cuts_floor: bool


def _opening_bounds(
    node: PortCutout,
    footprint: dict,
    wall: str,
    footprint_bounds: tuple[float, float, float, float] | None,
    inner: Wire,
    outer: Wire,
    board_top: float,
    floor_top: float,
    target_candidates: list[Candidate],
) -> _OpeningBounds:
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

    bounds, wall_span = _opening_xy_bounds(
        wall,
        center_x,
        center_y,
        opening_width,
        inner.wire,
        outer.wire,
        target_candidates,
    )
    minimum_x, maximum_x, minimum_y, maximum_y = bounds
    tangent_min = minimum_y if wall in {"left", "right"} else minimum_x
    tangent_max = maximum_y if wall in {"left", "right"} else maximum_x
    if wall_span is None:
        raise GraphError(
            "INVALID_PARAMETER",
            f"port_cutout {node.id} wall {wall} is not one planar base wall",
            feature=node.id,
            parameter="wall",
            wall=wall,
        )
    straight_min, straight_max = wall_span
    envelope_width = opening_width - 2 * node.clearance
    center = (tangent_min + tangent_max) / 2
    envelope_min = center - envelope_width / 2
    envelope_max = center + envelope_width / 2
    maximums = [
        envelope_min - straight_min,
        straight_max - envelope_max,
        envelope_bottom - floor_top,
    ]
    rim_heights = [
        item.entity.bounding_box().min.Z for item in target_candidates if item.key.role == "rim"
    ]
    if rim_heights:
        maximums.append(min(rim_heights) - envelope_bottom - node.envelope_height)
    maximum = min(maximums)
    limit_details = (
        {
            "limits": _limits(
                "clearance",
                node.clearance,
                minimum=0,
                maximum=maximum,
                suggested=maximum / 2,
            )
        }
        if maximum > 0
        else {}
    )
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
            **limit_details,
        )
    if tangent_min < straight_min - 1e-7 or tangent_max > straight_max + 1e-7:
        raise GraphError(
            "INVALID_PARAMETER",
            f"port_cutout {node.id} opening reaches an adjacent or curved wall",
            feature=node.id,
            parameter="envelope",
            wall=wall,
            openingSpan=[tangent_min, tangent_max],
            wallSpan=[straight_min, straight_max],
            **limit_details,
        )
    return _OpeningBounds(
        minimum_x,
        maximum_x,
        minimum_y,
        maximum_y,
        bottom,
        top,
        opening_width,
        opening_height,
        envelope_bottom < floor_top,
    )


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
    bounds = _opening_bounds(
        node,
        footprint,
        wall,
        footprint_bounds,
        inner,
        outer,
        board_top,
        floor_top,
        target_candidates,
    )
    tool = Box(
        bounds.maximum_x - bounds.minimum_x,
        bounds.maximum_y - bounds.minimum_y,
        bounds.top - bounds.bottom,
        align=(Align.MIN, Align.MIN, Align.MIN),
    ).moved(Location((bounds.minimum_x, bounds.minimum_y, bounds.bottom)))
    tool_body = BodySource(node.id, footprint["id"])
    candidates = []
    for face in tool.faces():
        box = face.bounding_box()
        if math.isclose(box.min.Z, bounds.bottom, abs_tol=1e-7) and math.isclose(
            box.max.Z, bounds.bottom, abs_tol=1e-7
        ):
            role = "port_bottom"
        elif math.isclose(box.min.Z, bounds.top, abs_tol=1e-7) and math.isclose(
            box.max.Z, bounds.top, abs_tol=1e-7
        ):
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
            "width": {"value": round(bounds.width, 12), "unit": "mm"},
            "height": {"value": round(bounds.height, 12), "unit": "mm"},
            "bottom": {"value": round(bounds.bottom, 12), "unit": "mm"},
            "top": {"value": round(bounds.top, 12), "unit": "mm"},
        },
        "cutsBelowBoardTop": bounds.bottom < board_top,
    }
    if bounds.cuts_floor:
        metadata["cutsFloor"] = True
    return tool, candidates, metadata


def build_port_opening(
    node: PortCutout,
    base: PcbBase,
    board: BoardImport,
    target_candidates: list[Candidate],
) -> tuple[Shape, list[Candidate], dict]:
    footprint = _resolve_footprint(node, board)
    footprint_bounds = (
        _courtyard_bounds(node, footprint) if node.envelope_source == "courtyard" else None
    )
    _, inner, outer = _base_wires(base, board)
    wall = _wall_choice(node, footprint, footprint_bounds, inner.wire)
    return _opening_tool(
        node,
        footprint,
        wall,
        footprint_bounds,
        inner,
        outer,
        board.mechanics["thickness"],
        -base.board_seat_height,
        target_candidates,
    )


def build_port_cutout(
    node: PortCutout,
    base: PcbBase,
    board: BoardImport,
    target_shape: Shape,
    target_candidates: list[Candidate],
) -> PortCutoutResult:
    tool, tool_candidates, metadata = build_port_opening(node, base, board, target_candidates)
    wall = metadata["wall"]
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
            footprint=metadata["footprintId"],
        )
    return PortCutoutResult(result.shape, result.candidates, metadata, tool)


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
    return mounting_holes(board)


def _common_area(first: Face, second: Face) -> float:
    common = BRepAlgoAPI_Common(first.wrapped, second.wrapped)
    common.Build()
    if not common.IsDone():
        return 0
    return sum(face.area for face in Shape.cast(common.Shape()).faces())


def _maximum_boss_diameter(cavity: Face, hole: dict, holes: list[dict]) -> float:
    x, y = hole["position"]
    center = Vertex(x, y, 0)
    wall_limit = 2 * min(edge.distance_to(center) for edge in cavity.edges())
    spacing_limit = min(
        (
            math.dist(hole["position"], other["position"])
            for other in holes
            if other["id"] != hole["id"]
        ),
        default=math.inf,
    )
    return min(wall_limit, spacing_limit)


def _boss_limits(node: Standoffs, cavity: Face, hole: dict, holes: list[dict]) -> dict | None:
    maximum = _maximum_boss_diameter(cavity, hole, holes)
    if maximum <= node.pilot_diameter:
        return None
    return _limits(
        "bossDiameter",
        node.boss_diameter,
        minimum=node.pilot_diameter,
        maximum=maximum,
        suggested=(node.pilot_diameter + maximum) / 2,
    )


def _validate_bosses(node: Standoffs, cavity: Face, holes: list[dict]) -> None:
    radius = node.boss_diameter / 2
    for hole in holes:
        x, y = hole["position"]
        disk = Face(Wire.make_circle(radius).moved(Location((x, y, 0))))
        if not cavity.is_inside((x, y, 0)):
            message = f"standoffs {node.id} hole {hole['id']} is off the base floor"
            limits = None
        elif math.isclose(_common_area(cavity, disk), disk.area, rel_tol=1e-9, abs_tol=1e-8):
            continue
        else:
            message = f"standoffs {node.id} boss at hole {hole['id']} crosses a wall"
            limits = _boss_limits(node, cavity, hole, holes)
        details = {"limits": limits} if limits is not None else {}
        raise GraphError(
            "INVALID_PARAMETER",
            message,
            feature=node.id,
            parameter="bossDiameter",
            hole=hole["id"],
            **details,
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
        profile = _ProfileWire(wire, [(hole["id"], wire.edges()[0])])
        built.append(
            _prism(
                node.id,
                profile,
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
    _validate_bosses(node, Face(inner.wire), holes)
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
    joined = fuse_shape(node.id, target_shape, target_candidates, bosses.shape, bosses.candidates)
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


def _closure_holes(node: ScrewClosure) -> list[dict]:
    return [{"id": item.name, "position": item.point} for item in node.points]


def _validate_closure_floor(node: ScrewClosure, cavity: Face) -> None:
    try:
        _validate_bosses(
            Standoffs(
                node.id,
                boss_diameter=node.boss_diameter,
                pilot_diameter=node.base_pilot_diameter,
            ),
            cavity,
            _closure_holes(node),
        )
    except GraphError as error:
        name = error.error.pop("hole", None)
        if name is not None:
            error.error["point"] = name
            screw = next(item for item in node.points if item.name == name)
            disk = Face(Wire.make_circle(node.boss_diameter / 2).moved(Location((*screw.point, 0))))
            crosses_wall = _common_area(disk, cavity) > 1e-8
            error.error["conflict"] = "wall" if crosses_wall else "floor"
            if crosses_wall:
                error.error["message"] = (
                    f"screw_closure {node.id} point {name} crosses the base wall"
                )
        raise


def _validate_closure_overlap(node: ScrewClosure) -> None:
    for index, first in enumerate(node.points):
        for second in node.points[index + 1 :]:
            if math.dist(first.point, second.point) < node.boss_diameter:
                raise GraphError(
                    "INVALID_PARAMETER",
                    f"screw_closure {node.id} point {second.name} overlaps {first.name}",
                    feature=node.id,
                    parameter="points",
                    point=second.name,
                    conflict=first.name,
                )


def _validate_closure_standoffs(node: ScrewClosure, target_candidates: list[Candidate]) -> None:
    for point in node.points:
        probe = Cylinder(
            node.boss_diameter / 2,
            1,
            align=(Align.CENTER, Align.CENTER, Align.MIN),
        ).moved(Location((*point.point, 0)))
        conflict = next(
            (
                item.key.source
                for item in target_candidates
                if item.key.role.startswith("standoff_") and item.entity.distance_to(probe) <= 1e-7
            ),
            None,
        )
        if conflict is not None:
            raise GraphError(
                "INVALID_PARAMETER",
                f"screw_closure {node.id} point {point.name} hits standoff {conflict}",
                feature=node.id,
                parameter="points",
                point=point.name,
                conflict="standoff",
                standoff=conflict,
            )


def _validate_closure_points(
    node: ScrewClosure,
    cavity: Face,
    target_candidates: list[Candidate],
) -> None:
    _validate_closure_floor(node, cavity)
    _validate_closure_overlap(node)
    _validate_closure_standoffs(node, target_candidates)


def build_screw_closure(
    node: ScrewClosure,
    base: PcbBase,
    board: BoardImport,
    board_shape: Shape,
    base_shape: Shape,
    base_candidates: list[Candidate],
    lid_shape: Shape,
    lid_candidates: list[Candidate],
) -> ScrewClosureResult:
    _, inner, _ = _base_wires(base, board)
    _validate_closure_points(node, Face(inner.wire), base_candidates)
    holes = _closure_holes(node)
    boss_node = Standoffs(
        node.id,
        boss_diameter=node.boss_diameter,
        pilot_diameter=node.base_pilot_diameter,
    )
    bottom = -base.board_seat_height
    top = board.mechanics["thickness"] + base.wall_height_above_board
    bosses = _cylinders(
        boss_node,
        holes,
        node.boss_diameter,
        bottom,
        top - bottom,
        ("screw_boss_bottom", "screw_boss_top", "screw_boss_side"),
    )
    unjoined, non_manifold = join_contact_failures(
        node.id, base_shape, bosses.shape, bosses.candidates
    )
    if unjoined or non_manifold:
        point = (unjoined or non_manifold)[0]
        raise GraphError(
            "INVALID_PARAMETER",
            f"screw_closure {node.id} point {point} does not join the base floor",
            feature=node.id,
            parameter="points",
            point=point,
        )
    joined = fuse_shape(node.id, base_shape, base_candidates, bosses.shape, bosses.candidates)
    pilots = _cylinders(
        boss_node,
        holes,
        node.base_pilot_diameter,
        bottom - 1,
        top - bottom + 2,
        ("screw_pilot_bottom", "screw_pilot_top", "screw_pilot_wall"),
    )
    cut_base = cut_shape(
        node.id, joined.shape, joined.candidates, pilots.shape, pilots.candidates, kind="semantic"
    )

    hole_node = Hole(
        node.id,
        target=node.lid,
        diameter=node.lid_clearance_diameter,
        positions=tuple(HolePosition(point.name, point.point) for point in node.points),
    )
    tool = make_hole_tool(hole_node, lid_shape)
    renamed = [
        Candidate(
            ElementKey(
                node.id,
                "screw_clearance_wall"
                if item.key.role.startswith("side(")
                else "screw_clearance_cap",
                item.key.source.name if hasattr(item.key.source, "name") else item.key.source,
                item.key.body,
            ),
            item.entity,
            item.descriptor,
            item.merged_from,
        )
        for item in tool.candidates
    ]
    cut_lid = cut_shape(node.id, lid_shape, lid_candidates, tool.shape, renamed, kind="semantic")
    return ScrewClosureResult(
        EnclosureResult(cut_base.shape, cut_base.candidates),
        EnclosureResult(cut_lid.shape, cut_lid.candidates),
    )
