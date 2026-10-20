"""Regenerate graph geometry and provenance as a run result value."""

from __future__ import annotations

from dataclasses import dataclass, field

from build123d import Edge, Shape

from .boolean import cut_shape
from .cut import make_cut_tool
from .extrusion import extrude_sketch
from .fillet import fillet_shape
from .graph import Cut, Extrude, Fillet, Graph, GraphError, Hole, Sketch
from .hole import make_hole_tool
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

    def resolve(self, ref: Reference, *, before_feature: str | None = None) -> Resolution:
        resolution = resolve(
            ref,
            self.provenance,
            ordered_features=self.ordered_features,
            before_feature=before_feature,
        )
        if resolution.status is not Status.EMPTY or resolution.error is None:
            return resolution
        replacements = [
            replacement
            for consumed, generated in self.consumed.items()
            if reference_matches_key(ref, consumed)
            for replacement in generated
        ]
        if replacements:
            names = ", ".join(
                f"{replacement.feature}.{replacement.role}"
                for replacement in replacements
            )
            resolution.error["suggestion"] = f"use generated blend face {names}"
        return resolution


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


def regenerate(graph: Graph) -> RegenResult:
    ordered = graph.ordered()
    shapes: dict[str, Shape] = {}
    provenance: dict[str, list[Candidate]] = {}
    states: dict[str, list[Candidate]] = {}
    consumed: dict[ElementKey, tuple[ElementKey, ...]] = {}

    for node in ordered:
        if isinstance(node, Sketch):
            continue
        if isinstance(node, Extrude):
            result = extrude_sketch(node, graph.nodes[node.sketch])
            shapes[node.id] = result.shape
            states[node.id] = result.candidates
            provenance[node.id] = result.candidates
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
            _replace_provenance(provenance, states[node.target], result.candidates)
        elif isinstance(node, Fillet):
            if not node.edges:
                raise GraphError(
                    "empty_fillet_edges",
                    f"fillet {node.id} requires at least one edge reference",
                    feature=node.id,
                )
            selected: list[Candidate] = []
            for reference in node.edges:
                resolution = resolve(
                    reference,
                    provenance,
                    ordered_features=[item.id for item in ordered],
                    before_feature=node.id,
                )
                if resolution.error is not None:
                    raise GraphError(
                        "reference_failed",
                        f"fillet {node.id} edge reference did not resolve",
                        feature=node.id,
                        resolution=resolution.error,
                    )
                selected.extend(resolution.candidates)
            if any(not isinstance(item.entity, Edge) for item in selected):
                raise GraphError(
                    "invalid_fillet_reference",
                    f"fillet {node.id} reference must resolve to edges",
                    feature=node.id,
                )
            unique = []
            for item in selected:
                if not any(item.entity.is_same(other.entity) for other in unique):
                    unique.append(item)
            result = fillet_shape(
                node.id,
                shapes[node.target],
                states[node.target],
                unique,
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
        [node.id for node in ordered],
        consumed,
    )
