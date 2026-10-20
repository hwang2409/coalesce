"""Regenerate graph geometry and provenance as a run result value."""

from __future__ import annotations

from dataclasses import dataclass, field

from build123d import Edge, Shape

from .boolean import FuseResult, cut_shape, fuse_shape, join_contact_failures
from .cut import make_cut_tool
from .enclosure import build_pcb_base, build_standoffs
from .extrusion import extrude_sketch
from .fillet import fillet_shape
from .graph import (
    BoardImport,
    Cut,
    Extrude,
    Fillet,
    Graph,
    GraphError,
    Hole,
    PcbBase,
    Sketch,
    Standoffs,
)
from .hole import make_hole_tool
from .pcb import build_pcb
from .references import (
    Candidate,
    ElementKey,
    Reference,
    Resolution,
    Status,
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
    return f"use generated blend face {names}"


def _replace_provenance(
    provenance: dict[str, list[Candidate]],
    previous: list[Candidate],
    current: list[Candidate],
) -> None:
    groups: dict[str, list[Candidate]] = {}
    for item in current:
        groups.setdefault(item.key.feature, []).append(item)
    affected = {item.key.feature for item in previous} | set(groups)
    for feature in affected:
        provenance[feature] = groups.get(feature, [])


def _resolve_fillet_edges(
    fillet: Fillet,
    provenance: dict[str, list[Candidate]],
    ordered_features: list[str],
) -> list[Candidate]:
    if not fillet.edges:
        raise GraphError(
            "empty_fillet_edges",
            f"fillet {fillet.id} requires at least one edge reference",
            feature=fillet.id,
        )
    selected: list[Candidate] = []
    for reference in fillet.edges:
        resolution = resolve(
            reference,
            provenance,
            ordered_features=ordered_features,
            before_feature=fillet.id,
        )
        if resolution.error is not None:
            raise GraphError(
                "reference_failed",
                f"fillet {fillet.id} edge reference did not resolve",
                feature=fillet.id,
                resolution=resolution.error,
            )
        selected.extend(resolution.candidates)
    if any(not isinstance(item.entity, Edge) for item in selected):
        raise GraphError(
            "invalid_fillet_reference",
            f"fillet {fillet.id} reference must resolve to edges",
            feature=fillet.id,
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
) -> FuseResult:
    if node.target is None:
        raise GraphError(
            "join_target_required",
            f"join extrude {node.id} requires a target",
            feature=node.id,
        )
    prism = extrude_sketch(node, graph.nodes[node.sketch])
    unjoined_entities, non_manifold_entities = join_contact_failures(
        node.id,
        shapes[node.target],
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
        shapes[node.target],
        states[node.target],
        prism.shape,
        prism.candidates,
    )


def _regenerate_enclosure(
    node: PcbBase | Standoffs,
    graph: Graph,
    shapes: dict[str, Shape],
    states: dict[str, list[Candidate]],
    provenance: dict[str, list[Candidate]],
    joined_material: dict[str, bool],
) -> None:
    board_node = graph.nodes[node.board]
    if isinstance(node, PcbBase):
        if not isinstance(board_node, BoardImport):
            raise GraphError(
                "INVALID_PARAMETER",
                f"pcb_base {node.id} board must reference a board import",
                feature=node.id,
                parameter="board",
            )
        base = build_pcb_base(node, board_node)
        shapes[node.id] = base.shape
        states[node.id] = base.candidates
        provenance[node.id] = base.candidates
        return

    base_node = graph.nodes[node.target]
    if not isinstance(board_node, BoardImport) or not isinstance(base_node, PcbBase):
        raise GraphError(
            "INVALID_PARAMETER",
            f"standoffs {node.id} requires a pcb_base target and board import",
            feature=node.id,
            parameter="target",
        )
    mounts = build_standoffs(
        node,
        base_node,
        board_node,
        shapes[node.target],
        states[node.target],
    )
    shapes[node.id] = mounts.shape
    states[node.id] = mounts.candidates
    joined_material[node.id] = True
    _replace_provenance(provenance, states[node.target], mounts.candidates)


def regenerate(graph: Graph) -> RegenResult:
    ordered = graph.ordered()
    shapes: dict[str, Shape] = {}
    provenance: dict[str, list[Candidate]] = {}
    states: dict[str, list[Candidate]] = {}
    consumed: dict[ElementKey, tuple[ElementKey, ...]] = {}
    removed_material: dict[str, bool] = {}
    joined_material: dict[str, bool] = {}
    ordered_features = [node.id for node in ordered]

    for node in ordered:
        if isinstance(node, Sketch):
            continue
        if isinstance(node, BoardImport):
            board = build_pcb(node)
            shapes[node.id] = board.shape
            states[node.id] = board.candidates
            provenance[node.id] = board.candidates
        elif isinstance(node, (PcbBase, Standoffs)):
            _regenerate_enclosure(
                node,
                graph,
                shapes,
                states,
                provenance,
                joined_material,
            )
        elif isinstance(node, Extrude):
            if node.operation == "new_body":
                prism = extrude_sketch(node, graph.nodes[node.sketch])
                shapes[node.id] = prism.shape
                states[node.id] = prism.candidates
                provenance[node.id] = prism.candidates
                continue
            if node.operation != "join":
                raise GraphError(
                    "invalid_extrude_operation",
                    f"extrude {node.id} has unknown operation {node.operation}",
                    feature=node.id,
                    operation=node.operation,
                )
            result = _join_extrude(node, graph, shapes, states)
            joined_material[node.id] = result.joined_material
            shapes[node.id] = result.shape
            states[node.id] = result.candidates
            assert node.target is not None
            _replace_provenance(provenance, states[node.target], result.candidates)
        elif isinstance(node, Cut):
            tool_sketch = graph.nodes[node.tool]
            if not isinstance(tool_sketch, Sketch):
                raise GraphError(
                    "invalid_cut_tool",
                    f"cut {node.id} tool must reference a sketch",
                    feature=node.id,
                    tool=node.tool,
                )
            tool = make_cut_tool(node, tool_sketch, shapes[node.target])
            result = cut_shape(
                node.id,
                shapes[node.target],
                states[node.target],
                tool.shape,
                tool.candidates,
            )
            shapes[node.id] = result.shape
            states[node.id] = result.candidates
            removed_material[node.id] = result.removed_material
            _replace_provenance(provenance, states[node.target], result.candidates)
        elif isinstance(node, Hole):
            tool = make_hole_tool(node, shapes[node.target])
            result = cut_shape(
                node.id,
                shapes[node.target],
                states[node.target],
                tool.shape,
                tool.candidates,
                kind="hole",
            )
            shapes[node.id] = result.shape
            states[node.id] = result.candidates
            removed_material[node.id] = result.removed_material
            _replace_provenance(provenance, states[node.target], result.candidates)
        elif isinstance(node, Fillet):
            result = fillet_shape(
                node.id,
                shapes[node.target],
                states[node.target],
                _resolve_fillet_edges(node, provenance, ordered_features),
                node.radius,
            )
            shapes[node.id] = result.shape
            states[node.id] = result.candidates
            consumed.update(result.consumed)
            _replace_provenance(provenance, states[node.target], result.candidates)

    return RegenResult(
        graph,
        shapes,
        provenance,
        ordered_features,
        consumed,
        removed_material,
        joined_material,
    )
