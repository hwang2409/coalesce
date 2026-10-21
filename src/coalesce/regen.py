"""Regenerate graph geometry and provenance as a run result value."""

from __future__ import annotations

import copy
import math
from dataclasses import asdict, dataclass, field, replace

from build123d import Edge, Shape
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepCheck import BRepCheck_Analyzer

from .boolean import FuseResult, cut_shape, fuse_shape, join_contact_failures
from .cut import make_cut_tool
from .edge_treatment import chamfer_shape, fillet_shape
from .enclosure import (
    build_pcb_base,
    build_pcb_lid,
    build_port_cutout,
    build_port_opening,
    build_standoffs,
)
from .extrusion import extrude_sketch
from .graph import (
    BoardImport,
    Chamfer,
    Cut,
    Extrude,
    Fillet,
    Graph,
    GraphError,
    Hole,
    PcbBase,
    PcbLid,
    PortCutout,
    Sketch,
    Standoffs,
)
from .hole import make_hole_tool
from .lineage import BodyLineage
from .pcb import build_pcb
from .references import (
    Candidate,
    ElementKey,
    Reference,
    Resolution,
    Status,
    edge_role_catalog,
    reference_matches_key,
    resolve,
)


@dataclass
class RegenResult:
    graph: Graph
    shapes: dict[str, Shape]
    provenance: dict[str, list[Candidate]]
    ordered_features: list[str]
    consumed: dict[ElementKey, tuple[ElementKey, ...]] = field(default_factory=dict)
    removed_material: dict[str, bool] = field(default_factory=dict)
    joined_material: dict[str, bool] = field(default_factory=dict)
    metadata: dict[str, dict] = field(default_factory=dict)
    opening_tools: dict[str, Shape] = field(default_factory=dict)
    superseded: set[str] = field(default_factory=set)
    predecessors: dict[str, str] = field(default_factory=dict)

    def resolve(self, ref: Reference, *, before_feature: str | None = None) -> Resolution:
        resolution = resolve(
            ref,
            self.provenance,
            ordered_features=self.ordered_features,
            before_feature=before_feature,
        )
        if resolution.status is not Status.EMPTY or resolution.error is None:
            return resolution
        suggestion = _consumed_edge_suggestion(ref, self.consumed)
        if suggestion is not None:
            resolution.error["suggestion"] = suggestion
        return resolution


def _consumed_edge_suggestion(
    ref: Reference,
    consumed_edges: dict[ElementKey, tuple[ElementKey, ...]],
) -> str | None:
    replacements = [
        replacement
        for consumed, generated in consumed_edges.items()
        if reference_matches_key(ref, consumed)
        for replacement in generated
    ]
    if not replacements:
        return None
    names = ", ".join(
        f"{replacement.feature}.{replacement.role}" for replacement in replacements
    )
    return f"use generated edge-treatment face {names}"


_REMEDY_STEP = 0.01
_PARAMETER_FIELDS = {
    "bossDiameter": "boss_diameter",
    "clearance": "clearance",
    "envelope.bottomOffset": "bottom_offset",
    "fitGap": "fit_gap",
    "lip.depth": "lip_depth",
    "lip.width": "lip_width",
}
# These intervals are monotonic: smaller bosses and shallower lips stay inside their bounds.
_PROVABLE_RANGES = {"bossDiameter", "lip.depth"}


def _round_minimum(value: float) -> float:
    # Move inward by one 0.01 mm step when the computed bound is exact.
    return round(math.ceil((value + 1e-9) / _REMEDY_STEP) * _REMEDY_STEP, 2)


def _round_maximum(value: float) -> float:
    return round(math.floor((value - 1e-9) / _REMEDY_STEP) * _REMEDY_STEP, 2)


def _quantity_value(value: dict) -> float:
    return float(value["value"])


def _alternative_updates(node: object, values: dict) -> dict[str, object] | None:
    updates: dict[str, object] = {}
    if "wall" in values:
        updates["wall"] = values["wall"]
    envelope = values.get("envelope")
    if envelope is None:
        return updates
    if not isinstance(node, PortCutout):
        return None
    width = _quantity_value(envelope["width"]) if "width" in envelope else None
    height = _quantity_value(envelope["height"])
    if width != node.envelope_width or height != node.envelope_height:
        return None
    updates.update(
        envelope_source=envelope["source"],
        envelope_width=width,
        envelope_height=height,
        bottom_offset=_quantity_value(envelope["bottomOffset"]),
    )
    return updates


def _effective_update_key(
    node: object, feature: str, updates: dict[str, object]
) -> tuple[str, tuple[tuple[str, object], ...]]:
    return feature, tuple(
        sorted(
            (name, value)
            for name, value in updates.items()
            if getattr(node, name) != value
        )
    )


def _remedy_succeeds(
    graph: Graph,
    feature: str,
    updates: dict[str, object],
    cache: dict[tuple[str, tuple[tuple[str, object], ...]], bool],
) -> bool:
    """Run one unchanged remedy through the same acceptance as a real mutation."""
    node = graph.nodes.get(feature)
    if node is None:
        return False
    key = _effective_update_key(node, feature, updates)
    if key in cache:
        return cache[key]
    candidate = copy.deepcopy(graph)
    try:
        candidate.nodes[feature] = replace(candidate.nodes[feature], **updates)
        accept_regeneration(candidate, verify_remedies=False)
    except (GraphError, TypeError, ValueError):
        cache[key] = False
    else:
        cache[key] = True
    return cache[key]


def _range_values(limits: dict) -> list[float]:
    minimum = limits["minimum"]
    maximum = limits["maximum"]
    return [minimum + (maximum - minimum) * step / 6 for step in range(7)]


def _verified_limits(
    graph: Graph,
    feature: str,
    limits: dict,
    cache: dict[tuple[str, tuple[tuple[str, object], ...]], bool],
    base_updates: dict[str, object] | None = None,
) -> dict | None:
    parameter = limits.get("parameter")
    field = _PARAMETER_FIELDS.get(parameter)
    if field is None:
        return None
    base_updates = base_updates or {}
    verified = {
        name: limits[name]
        for name in ("parameter", "current", "unit")
        if name in limits
    }
    suggested = limits.get("suggested")
    if suggested is not None and _remedy_succeeds(
        graph, feature, {**base_updates, field: suggested}, cache
    ):
        verified["suggested"] = suggested
    has_range = "minimum" in limits and "maximum" in limits
    if parameter in _PROVABLE_RANGES and has_range and all(
        _remedy_succeeds(graph, feature, {**base_updates, field: value}, cache)
        for value in _range_values(limits)
    ):
        verified["minimum"] = limits["minimum"]
        verified["maximum"] = limits["maximum"]
    return verified if "suggested" in verified or "minimum" in verified else None


def _verified_alternative(
    graph: Graph,
    feature: str,
    alternative: dict,
    cache: dict[tuple[str, tuple[tuple[str, object], ...]], bool],
) -> dict | None:
    updates = _alternative_updates(
        graph.nodes.get(feature), alternative.get("set", {})
    )
    if updates is None or not _remedy_succeeds(graph, feature, updates, cache):
        return None
    verified = dict(alternative)
    if limits := alternative.get("limits"):
        safe_limits = _verified_limits(graph, feature, limits, cache, updates)
        if safe_limits is None:
            verified.pop("limits", None)
        else:
            verified["limits"] = safe_limits
    return verified


def _verify_error_remedies(graph: Graph, error: GraphError) -> None:
    details = error.error
    feature = details.get("feature")
    if not isinstance(feature, str):
        details.pop("limits", None)
        details.pop("alternatives", None)
        return
    cache: dict[tuple[str, tuple[tuple[str, object], ...]], bool] = {}
    if limits := details.get("limits"):
        verified = _verified_limits(graph, feature, limits, cache)
        if verified is None:
            details.pop("limits")
        else:
            details["limits"] = verified
    alternatives = [
        verified
        for alternative in details.get("alternatives", [])
        if (verified := _verified_alternative(graph, feature, alternative, cache)) is not None
    ]
    if alternatives:
        details["alternatives"] = alternatives
    else:
        details.pop("alternatives", None)


def _resolve_edge_treatment_edges(
    node: Fillet | Chamfer,
    provenance: dict[str, list[Candidate]],
    ordered_features: list[str],
) -> list[Candidate]:
    kind = type(node).__name__.lower()
    if not node.edges:
        raise GraphError(
            "EMPTY_EDGE_SELECTION",
            f"{kind} {node.id} requires at least one edge reference",
            feature=node.id,
        )
    selected: list[Candidate] = []
    resolutions: list[dict] = []
    for reference in node.edges:
        resolution = resolve(
            reference,
            provenance,
            ordered_features=ordered_features,
            before_feature=node.id,
        )
        resolution_details = resolution.error or {
            "feature": reference.feature,
            "reference": reference.to_dict(),
            "status": resolution.status.value,
            "candidates": [],
            "availableRoles": edge_role_catalog(provenance, reference.feature),
        }
        resolutions.append(resolution_details)
        if resolution.error is not None:
            raise GraphError(
                "REFERENCE_FAILED",
                f"{kind} {node.id} edge reference did not resolve",
                feature=node.id,
                selectedEdges=[asdict(candidate.key) for candidate in selected],
                resolution=resolution.error,
            )
        selected.extend(resolution.candidates)
    if not selected:
        raise GraphError(
            "EMPTY_EDGE_SELECTION",
            f"{kind} {node.id} edge references selected no edges",
            feature=node.id,
            selectedEdges=[],
            resolution=resolutions[0] if len(resolutions) == 1 else resolutions,
        )
    if any(not isinstance(item.entity, Edge) for item in selected):
        raise GraphError(
            "INVALID_EDGE_REFERENCE",
            f"{kind} {node.id} reference must resolve to edges",
            feature=node.id,
        )
    unique: list[Candidate] = []
    for item in selected:
        if not any(item.entity.is_same(other.entity) for other in unique):
            unique.append(item)
    return unique


def _join_extrude(
    node: Extrude,
    graph: Graph,
    shapes: dict[str, Shape],
    states: dict[str, list[Candidate]],
    target: str,
) -> FuseResult:
    if node.target is None:
        raise GraphError(
            "JOIN_TARGET_REQUIRED",
            f"join extrude {node.id} requires a target",
            feature=node.id,
        )
    prism = extrude_sketch(node, graph.nodes[node.sketch])
    unjoined_entities, non_manifold_entities = join_contact_failures(
        node.id,
        shapes[target],
        prism.shape,
        prism.candidates,
    )
    if unjoined_entities:
        raise GraphError(
            "JOIN_NO_CONTACT",
            f"join extrude {node.id} has profiles that do not contact target {node.target}",
            feature=node.id,
            target=node.target,
            entities=unjoined_entities,
        )
    if non_manifold_entities:
        entity = non_manifold_entities[0]
        raise GraphError(
            "JOIN_NON_MANIFOLD",
            f"join extrude {node.id} profile {entity} only touches target {node.target} along an edge or point",
            feature=node.id,
            target=node.target,
            entity=entity,
            hint="extend the profile so faces overlap or touch",
        )
    return fuse_shape(
        node.id,
        shapes[target],
        states[target],
        prism.shape,
        prism.candidates,
    )


class _EnclosureRegenerator:
    def __init__(
        self,
        graph: Graph,
        shapes: dict[str, Shape],
        states: dict[str, list[Candidate]],
        provenance: dict[str, list[Candidate]],
        removed_material: dict[str, bool],
        joined_material: dict[str, bool],
        metadata: dict[str, dict],
        lineage: BodyLineage,
    ) -> None:
        self.graph = graph
        self.shapes = shapes
        self.states = states
        self.provenance = provenance
        self.removed_material = removed_material
        self.joined_material = joined_material
        self.metadata = metadata
        self.lineage = lineage
        self.opening_tools: dict[str, Shape] = {}

    def _base_context(self, base_id: str, feature: str) -> tuple[PcbBase, BoardImport]:
        base = self.graph.nodes.get(base_id)
        if not isinstance(base, PcbBase):
            raise GraphError(
                "INVALID_PARAMETER",
                f"{feature} base must reference a pcb_base",
                feature=feature,
                parameter="base",
            )
        board = self.graph.nodes.get(base.board)
        if not isinstance(board, BoardImport):
            raise GraphError(
                "INVALID_PARAMETER",
                f"{feature} base must reference a valid board import",
                feature=feature,
                parameter="base",
            )
        return base, board

    def _regenerate_pcb_base(self, node: PcbBase) -> None:
        board = self.graph.nodes.get(node.board)
        if not isinstance(board, BoardImport):
            raise GraphError(
                "INVALID_PARAMETER",
                f"pcb_base {node.id} board must reference a board import",
                feature=node.id,
                parameter="board",
            )
        base = build_pcb_base(node, board)
        self.shapes[node.id] = base.shape
        self.states[node.id] = base.candidates
        self.provenance[node.id] = base.candidates
        self.lineage.create(node.id, base.candidates)

    def _regenerate_pcb_lid(self, node: PcbLid) -> None:
        base, board = self._base_context(node.base, node.id)
        lid = build_pcb_lid(node, base, board)
        self.shapes[node.id] = lid.shape
        self.states[node.id] = lid.candidates
        self.provenance[node.id] = lid.candidates
        self.lineage.create(node.id, lid.candidates)

    def _regenerate_port_cutout(self, node: PortCutout) -> None:
        base, board = self._base_context(node.base, node.id)
        terminal = self.lineage.terminal(node.base)
        try:
            opening = build_port_cutout(
                node,
                base,
                board,
                self.shapes[terminal],
                self.states[terminal],
            )
        except GraphError as error:
            alternatives = self._safe_wall_alternatives(node)
            if alternatives:
                error.error["alternatives"] = alternatives
            raise
        self.shapes[node.id] = opening.shape
        self.states[node.id] = opening.candidates
        self.removed_material[node.id] = True
        self.metadata[node.id] = opening.metadata
        self.opening_tools[node.id] = opening.opening_tool
        self.lineage.replace(node.base, node.id, opening.candidates)

    def _regenerate_standoffs(self, node: Standoffs) -> None:
        base, board = self._base_context(node.target, node.id)
        if node.board != board.id:
            raise GraphError(
                "INVALID_PARAMETER",
                f"standoffs {node.id} board must match its pcb_base board",
                feature=node.id,
                parameter="board",
            )
        terminal = self.lineage.terminal(node.target)
        mounts = build_standoffs(
            node,
            base,
            board,
            self.shapes[terminal],
            self.states[terminal],
        )
        self.shapes[node.id] = mounts.shape
        self.states[node.id] = mounts.candidates
        self.joined_material[node.id] = True
        self.lineage.replace(node.target, node.id, mounts.candidates)

    def apply(self, node: PcbBase | PcbLid | PortCutout | Standoffs) -> None:
        if isinstance(node, PcbBase):
            self._regenerate_pcb_base(node)
        elif isinstance(node, PcbLid):
            self._regenerate_pcb_lid(node)
        elif isinstance(node, PortCutout):
            self._regenerate_port_cutout(node)
        else:
            self._regenerate_standoffs(node)

    def _standoff_conflict(self, port: PortCutout, tool: Shape) -> str | None:
        terminal = self.lineage.terminal(port.base)
        return next(
            (
                item.key.source
                for item in self.states[terminal]
                if item.key.role.startswith(("standoff_", "pilot_"))
                and item.entity.distance_to(tool) <= 1e-7
            ),
            None,
        )

    def _lid_overlap(self, lid: PcbLid, tool: Shape) -> Shape | None:
        common = BRepAlgoAPI_Common(tool.wrapped, self.shapes[lid.id].wrapped)
        common.Build()
        if not common.IsDone():
            return None
        overlap = Shape.cast(common.Shape())
        return overlap if sum(solid.volume for solid in overlap.solids()) > 1e-9 else None

    def _safe_wall_alternatives(self, port: PortCutout) -> list[dict]:
        base, board = self._base_context(port.base, port.id)
        terminal = self.lineage.terminal(port.base)
        lids = [
            node
            for node in self.graph.nodes.values()
            if isinstance(node, PcbLid) and node.base == port.base and node.id in self.shapes
        ]
        alternatives = []
        for wall in ("left", "right", "front", "back"):
            if wall == port.wall:
                continue
            candidate_port = replace(port, wall=wall)
            try:
                tool, _, _ = build_port_opening(
                    candidate_port, base, board, self.states[terminal]
                )
            except GraphError:
                continue
            if self._standoff_conflict(port, tool) is not None:
                continue
            if any(self._lid_overlap(lid, tool) is not None for lid in lids):
                continue
            alternatives.append({"set": {"wall": wall}})
        return alternatives

    def _base_lids(self, port: PortCutout) -> list[PcbLid]:
        return [
            node
            for node in self.graph.nodes.values()
            if isinstance(node, PcbLid) and node.base == port.base and node.id in self.shapes
        ]

    def _standoff_tops(self, port: PortCutout) -> list[float]:
        terminal = self.lineage.terminal(port.base)
        return [
            item.entity.bounding_box().max.Z
            for item in self.states[terminal]
            if item.key.role.startswith(("standoff_", "pilot_"))
        ]

    def _envelope_payload(self, port: PortCutout) -> dict:
        envelope = {
            "source": port.envelope_source,
            "height": {"value": round(port.envelope_height, 12), "unit": "mm"},
            "bottomOffset": {"value": round(port.bottom_offset, 12), "unit": "mm"},
        }
        if port.envelope_width is not None:
            envelope["width"] = {"value": round(port.envelope_width, 12), "unit": "mm"}
        return envelope

    def _safe_standoff_envelope_alternative(self, port: PortCutout) -> dict | None:
        base, board = self._base_context(port.base, port.id)
        terminal = self.lineage.terminal(port.base)
        tops = self._standoff_tops(port)
        if not tops:
            return None
        board_top = board.mechanics["thickness"]
        minimum = max(tops) + port.clearance - board_top
        maximum = base.wall_height_above_board - port.envelope_height - port.clearance
        lids = self._base_lids(port)
        if lids:
            lid_top = min(self.shapes[lid.id].bounding_box().min.Z for lid in lids)
            maximum = min(
                maximum,
                lid_top
                - board_top
                - port.envelope_height
                - port.clearance,
            )
        minimum = _round_minimum(minimum)
        maximum = _round_maximum(maximum)
        if minimum > maximum:
            return None
        candidate_port = replace(port, bottom_offset=minimum)
        try:
            tool, _, _ = build_port_opening(
                candidate_port, base, board, self.states[terminal]
            )
        except GraphError:
            return None
        if self._standoff_conflict(port, tool) is not None or any(
            self._lid_overlap(lid, tool) is not None for lid in lids
        ):
            return None
        return {
            "set": {"envelope": self._envelope_payload(candidate_port)},
            "limits": {
                "parameter": "envelope.bottomOffset",
                "current": round(port.bottom_offset, 2),
                "minimum": minimum,
                "maximum": maximum,
                "suggested": minimum,
                "unit": "mm",
            },
        }

    def _validate_standoff_clearance(self, port: PortCutout, tool: Shape) -> None:
        conflict = self._standoff_conflict(port, tool)
        if conflict is not None:
            alternatives = self._safe_wall_alternatives(port)
            envelope_alternative = self._safe_standoff_envelope_alternative(port)
            if envelope_alternative is not None:
                alternatives.append(envelope_alternative)
            details = {"alternatives": alternatives} if alternatives else {}
            raise GraphError(
                "INVALID_PARAMETER",
                f"port_cutout {port.id} opening intersects a standoff",
                feature=port.id,
                parameter="envelope",
                footprint=port.footprint_id,
                conflict=conflict,
                **details,
            )

    def _validate_lid_clearance(self, port: PortCutout, lid: PcbLid, tool: Shape) -> None:
        overlap = self._lid_overlap(lid, tool)
        if overlap is None:
            return
        common = BRepAlgoAPI_Common(tool.wrapped, self.shapes[lid.id].wrapped)
        common.Build()
        if not common.IsDone():
            raise GraphError(
                "BOOLEAN_FAILED",
                f"port_cutout {port.id} clearance check against lid {lid.id} failed",
                feature=port.id,
                port=port.id,
                lid=lid.id,
            )
        volume = sum(solid.volume for solid in overlap.solids())
        bounds = overlap.bounding_box()
        alternatives = self._safe_wall_alternatives(port)
        details = {"alternatives": alternatives} if alternatives else {}
        raise GraphError(
            "PORT_BLOCKED_BY_LID",
            f"port_cutout {port.id} opening is blocked by pcb_lid {lid.id}",
            feature=port.id,
            port=port.id,
            lid=lid.id,
            overlapVolume=volume,
            overlapBounds={
                "min": [bounds.min.X, bounds.min.Y, bounds.min.Z],
                "max": [bounds.max.X, bounds.max.Y, bounds.max.Z],
                "unit": "mm",
            },
            hints=[
                "lower the opening height",
                "lower bottomOffset",
                "reduce the lid lip depth or change the lid",
            ],
            **details,
        )

    def validate(self) -> None:
        lids = [node for node in self.graph.nodes.values() if isinstance(node, PcbLid)]
        for node in self.graph.nodes.values():
            if not isinstance(node, PortCutout):
                continue
            tool = self.opening_tools[node.id]
            self._validate_standoff_clearance(node, tool)
            for lid in lids:
                if lid.base == node.base:
                    self._validate_lid_clearance(node, lid, tool)


def _apply_extrude(
    node: Extrude,
    graph: Graph,
    shapes: dict[str, Shape],
    states: dict[str, list[Candidate]],
    provenance: dict[str, list[Candidate]],
    joined_material: dict[str, bool],
    lineage: BodyLineage,
) -> None:
    if node.operation == "new_body":
        prism = extrude_sketch(node, graph.nodes[node.sketch])
        shapes[node.id] = prism.shape
        states[node.id] = prism.candidates
        provenance[node.id] = prism.candidates
        lineage.create(node.id, prism.candidates)
        return
    if node.operation != "join":
        raise GraphError(
            "INVALID_EXTRUDE_OPERATION",
            f"extrude {node.id} has unknown operation {node.operation}",
            feature=node.id,
            operation=node.operation,
        )
    assert node.target is not None
    target = lineage.terminal(node.target)
    result = _join_extrude(node, graph, shapes, states, target)
    joined_material[node.id] = result.joined_material
    shapes[node.id] = result.shape
    states[node.id] = result.candidates
    lineage.replace(node.target, node.id, result.candidates)


def _apply_cut(
    node: Cut,
    graph: Graph,
    shapes: dict[str, Shape],
    states: dict[str, list[Candidate]],
    removed_material: dict[str, bool],
    lineage: BodyLineage,
) -> None:
    tool_sketch = graph.nodes[node.tool]
    if not isinstance(tool_sketch, Sketch):
        raise GraphError(
            "INVALID_CUT_TOOL",
            f"cut {node.id} tool must reference a sketch",
            feature=node.id,
            tool=node.tool,
        )
    target = lineage.terminal(node.target)
    tool = make_cut_tool(node, tool_sketch, shapes[target])
    result = cut_shape(
        node.id,
        shapes[target],
        states[target],
        tool.shape,
        tool.candidates,
    )
    shapes[node.id] = result.shape
    states[node.id] = result.candidates
    removed_material[node.id] = result.removed_material
    lineage.replace(node.target, node.id, result.candidates)


def _apply_hole(
    node: Hole,
    shapes: dict[str, Shape],
    states: dict[str, list[Candidate]],
    removed_material: dict[str, bool],
    lineage: BodyLineage,
) -> None:
    target = lineage.terminal(node.target)
    tool = make_hole_tool(node, shapes[target])
    result = cut_shape(
        node.id,
        shapes[target],
        states[target],
        tool.shape,
        tool.candidates,
        kind="hole",
    )
    shapes[node.id] = result.shape
    states[node.id] = result.candidates
    removed_material[node.id] = result.removed_material
    lineage.replace(node.target, node.id, result.candidates)


def _apply_fillet(
    node: Fillet,
    shapes: dict[str, Shape],
    states: dict[str, list[Candidate]],
    provenance: dict[str, list[Candidate]],
    ordered_features: list[str],
    consumed: dict[ElementKey, tuple[ElementKey, ...]],
    lineage: BodyLineage,
) -> None:
    target = lineage.terminal(node.target)
    result = fillet_shape(
        node.id,
        shapes[target],
        states[target],
        _resolve_edge_treatment_edges(node, provenance, ordered_features),
        node.radius,
    )
    shapes[node.id] = result.shape
    states[node.id] = result.candidates
    consumed.update(result.consumed)
    lineage.replace(node.target, node.id, result.candidates)


def _apply_chamfer(
    node: Chamfer,
    shapes: dict[str, Shape],
    states: dict[str, list[Candidate]],
    provenance: dict[str, list[Candidate]],
    ordered_features: list[str],
    consumed: dict[ElementKey, tuple[ElementKey, ...]],
    lineage: BodyLineage,
) -> None:
    target = lineage.terminal(node.target)
    result = chamfer_shape(
        node.id,
        shapes[target],
        states[target],
        _resolve_edge_treatment_edges(node, provenance, ordered_features),
        node.distance,
    )
    shapes[node.id] = result.shape
    states[node.id] = result.candidates
    consumed.update(result.consumed)
    lineage.replace(node.target, node.id, result.candidates)


def _regenerate(graph: Graph) -> RegenResult:
    ordered = graph.ordered()
    shapes: dict[str, Shape] = {}
    provenance: dict[str, list[Candidate]] = {}
    states: dict[str, list[Candidate]] = {}
    consumed: dict[ElementKey, tuple[ElementKey, ...]] = {}
    removed_material: dict[str, bool] = {}
    joined_material: dict[str, bool] = {}
    metadata: dict[str, dict] = {}
    ordered_features = [node.id for node in ordered]
    lineage = BodyLineage(states, provenance)
    enclosure = _EnclosureRegenerator(
        graph,
        shapes,
        states,
        provenance,
        removed_material,
        joined_material,
        metadata,
        lineage,
    )

    for node in ordered:
        if isinstance(node, Sketch):
            continue
        if isinstance(node, BoardImport):
            board = build_pcb(node)
            shapes[node.id] = board.shape
            states[node.id] = board.candidates
            provenance[node.id] = board.candidates
            lineage.create(node.id, board.candidates)
        elif isinstance(node, (PcbBase, PcbLid, PortCutout, Standoffs)):
            enclosure.apply(node)
        elif isinstance(node, Extrude):
            _apply_extrude(
                node,
                graph,
                shapes,
                states,
                provenance,
                joined_material,
                lineage,
            )
        elif isinstance(node, Cut):
            _apply_cut(node, graph, shapes, states, removed_material, lineage)
        elif isinstance(node, Hole):
            _apply_hole(node, shapes, states, removed_material, lineage)
        elif isinstance(node, Fillet):
            _apply_fillet(
                node,
                shapes,
                states,
                provenance,
                ordered_features,
                consumed,
                lineage,
            )
        elif isinstance(node, Chamfer):
            _apply_chamfer(
                node,
                shapes,
                states,
                provenance,
                ordered_features,
                consumed,
                lineage,
            )

    enclosure.validate()
    return RegenResult(
        graph=graph,
        shapes=shapes,
        provenance=provenance,
        ordered_features=ordered_features,
        consumed=consumed,
        removed_material=removed_material,
        joined_material=joined_material,
        metadata=metadata,
        opening_tools=enclosure.opening_tools,
        superseded=lineage.superseded,
        predecessors=lineage.predecessors,
    )


def accept_regeneration(graph: Graph, *, verify_remedies: bool = True) -> RegenResult:
    """Regenerate and apply every validity check required before a commit."""
    try:
        result = _regenerate(graph)
        if any(
            not BRepCheck_Analyzer(shape.wrapped).IsValid()
            for shape in result.shapes.values()
        ):
            raise GraphError("INVALID_BREP", "regeneration produced invalid geometry")
        return result
    except GraphError as error:
        if verify_remedies:
            _verify_error_remedies(graph, error)
        raise


def regenerate(graph: Graph) -> RegenResult:
    return accept_regeneration(graph)
