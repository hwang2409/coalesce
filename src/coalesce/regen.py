"""build123d regeneration and provenance capture."""

from .graph import Graph


def regenerate(graph: Graph) -> Graph:
    from build123d import Box

    graph.validate()
    for node in graph.ordered():
        if node.__class__.__name__ == "Sketch":
            continue
        if node.__class__.__name__ == "Extrude":
            shape = Box(1, 1, node.height)
            graph.shapes[node.id] = shape
            graph.provenance[node.id] = [
                _candidate(node.id, "cap_start"),
                _candidate(node.id, "cap_end"),
            ]
            sketch = graph.nodes[node.sketch]
            for entity in sketch.entities:
                graph.provenance[node.id].append(
                    _candidate(node.id, f"side({entity.id})", entity.id)
                )
        elif node.__class__.__name__ in ("Cut", "Hole"):
            graph.shapes[node.id] = graph.shapes[node.target]
            graph.provenance[node.id] = list(graph.provenance.get(node.target, []))
            if node.__class__.__name__ == "Hole":
                graph.provenance[node.id].append(_candidate(node.id, "hole_wall"))
        elif node.__class__.__name__ == "Fillet":
            graph.shapes[node.id] = graph.shapes[node.target]
            graph.provenance[node.id] = [_candidate(node.id, "blend", e.role) for e in node.edges]
    return graph


def _candidate(feature: str, role: str, source: str | None = None):
    from .references import Candidate, ElementKey

    return Candidate(ElementKey(feature, role, source))
