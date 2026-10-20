"""Regenerate graph geometry and provenance as a run result value."""

from __future__ import annotations

from dataclasses import dataclass

from build123d import Shape

from .boolean import cut_shape
from .cut import make_cut_tool
from .extrusion import extrude_sketch
from .graph import Cut, Extrude, Fillet, Graph, GraphError, Hole, Sketch
from .hole import make_hole_tool
from .references import Candidate, Reference, Resolution, resolve


@dataclass
class RegenResult:
    graph: Graph
    shapes: dict[str, Shape]
    provenance: dict[str, list[Candidate]]
    ordered_features: list[str]

    def resolve(self, ref: Reference, *, before_feature: str | None = None) -> Resolution:
        return resolve(
            ref,
            self.provenance,
            ordered_features=self.ordered_features,
            before_feature=before_feature,
        )


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
            raise NotImplementedError("Fillet regeneration requires step C")

    return RegenResult(graph, shapes, provenance, [node.id for node in ordered])
