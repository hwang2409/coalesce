"""Regenerate graph geometry and provenance as a run result value."""

from __future__ import annotations

from dataclasses import dataclass

from build123d import Shape

from .extrusion import extrude_sketch
from .graph import Cut, Extrude, Fillet, Graph, Hole, Sketch
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


def regenerate(graph: Graph) -> RegenResult:
    ordered = graph.ordered()
    shapes: dict[str, Shape] = {}
    provenance: dict[str, list[Candidate]] = {}

    for node in ordered:
        if isinstance(node, Sketch):
            continue
        if isinstance(node, Extrude):
            result = extrude_sketch(node, graph.nodes[node.sketch])
            shapes[node.id] = result.shape
            provenance[node.id] = result.candidates
        elif isinstance(node, (Cut, Hole)):
            raise NotImplementedError(f"{type(node).__name__} regeneration requires step B")
        elif isinstance(node, Fillet):
            raise NotImplementedError("Fillet regeneration requires step C")

    return RegenResult(graph, shapes, provenance, [node.id for node in ordered])
