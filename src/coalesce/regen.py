"""regenerate the graph with real build123d solids and occt topology."""
from __future__ import annotations

from math import isclose

from build123d import Circle as BDCircle
from build123d import Cylinder, extrude
from build123d import Rectangle as BDRectangle

from .graph import Circle, Cut, Extrude, Fillet, Graph, Hole, Rectangle, Sketch
from .references import Candidate, ElementKey


def _key(feature: str, role: str, source: str | None = None, entity=None) -> Candidate:
    return Candidate(ElementKey(feature, role, source), entity)


def _faces(shape):
    return list(shape.faces())


def _normal(face):
    n = face.normal_at()
    return (round(n.X), round(n.Y), round(n.Z))


def _record_extrude(node: Extrude, sketch: Sketch, shape) -> list[Candidate]:
    out = []
    for face in _faces(shape):
        n = _normal(face)
        if n == (0, 0, -1): out.append(_key(node.id, "cap_start", entity=face))
        elif n == (0, 0, 1): out.append(_key(node.id, "cap_end", entity=face))
        else:
            role = None
            for entity in sketch.entities:
                if isinstance(entity, Rectangle):
                    # Rectangle segment names describe the outward wall direction.
                    wanted = {"left": (-1, 0), "right": (1, 0), "bottom": (0, -1), "top": (0, 1)}.get(entity.id)
                    if wanted and (n[0], n[1]) == wanted: role = f"side({entity.id})"
                elif isinstance(entity, Circle): role = f"side({entity.id})"
            if role: out.append(_key(node.id, role, role[5:-1], face))
    # Edge keys are derived from the two adjacent role keys, not from edge indexes.
    for edge in shape.edges():
        adjacent = [f for f in _faces(shape) if any(edge.is_same(e) for e in f.edges())]
        roles = {c.key.role for c in out if c.entity in adjacent}
        if "cap_end" in roles:
            for role in roles - {"cap_end"}:
                out.append(_key(node.id, f"edge({role},cap_end)", entity=edge))
                out.append(_key(node.id, f"between({role},cap_end)", entity=edge))
    return out


def _inherit(node_id, target_id, target_shape, result):
    """Use OCCT's resulting faces, inheriting keys for unchanged/modified support faces."""
    old = _CURRENT.provenance.get(target_id, [])
    out = []
    for face in _faces(result):
        n = _normal(face); c = face.center()
        for candidate in old:
            entity = candidate.entity
            if entity is None or not hasattr(entity, "normal_at") or not hasattr(entity, "center"): continue
            if _normal(entity) == n and (isclose(c.Z, entity.center().Z, abs_tol=1e-5) or n[2] != 0 or (abs(c.X-entity.center().X) < 1e-5 and abs(c.Y-entity.center().Y) < 1e-5)):
                out.append(_key(node_id, candidate.key.role, candidate.key.source, face)); break
    return out


def _sketch_face(sketch: Sketch):
    if not sketch.entities: raise ValueError(f"sketch {sketch.id} has no entities")
    entity = sketch.entities[0]
    if isinstance(entity, Rectangle): return BDRectangle(entity.width, entity.height)
    if isinstance(entity, Circle): return BDCircle(entity.radius)
    raise TypeError(entity)


def regenerate(graph: Graph) -> Graph:
    global _CURRENT
    graph.validate(); _CURRENT = graph
    graph.shapes.clear(); graph.provenance.clear()
    for node in graph.ordered():
        if isinstance(node, Sketch): continue
        if isinstance(node, Extrude):
            sketch = graph.nodes[node.sketch]
            shape = extrude(_sketch_face(sketch), node.height)
            graph.shapes[node.id] = shape
            graph.provenance[node.id] = _record_extrude(node, sketch, shape)
        elif isinstance(node, Cut):
            target = graph.shapes[node.target]; tool_node = graph.nodes[node.tool]
            tool = extrude(_sketch_face(tool_node), target.bounding_box().max.Z - target.bounding_box().min.Z + 2)
            result = target.cut(tool)
            graph.shapes[node.id] = result
            graph.provenance[node.id] = _inherit(node.id, node.target, target, result)
        elif isinstance(node, Hole):
            target = graph.shapes[node.target]; bb = target.bounding_box()
            depth = node.depth if node.depth is not None else bb.size.Z + 2
            tool = Cylinder(node.diameter / 2, depth, align=None).translate((node.position[0], node.position[1], bb.min.Z - 1))
            result = target.cut(tool)
            graph.shapes[node.id] = result
            graph.provenance[node.id] = _inherit(node.id, node.target, target, result)
            for face in _faces(result):
                if face.geom_type.name == "CYLINDER": graph.provenance[node.id].append(_key(node.id, "hole_wall", entity=face))
        elif isinstance(node, Fillet):
            target = graph.shapes[node.target]
            edges = []
            for ref in node.edges:
                resolution = graph.resolve(ref)
                for candidate in resolution.candidates:
                    if candidate.entity is None: continue
                    if hasattr(candidate.entity, "edges"):
                        edges.extend(candidate.entity.edges())
                    else:
                        edges.append(candidate.entity)
            result = target.fillet(node.radius, edges)
            graph.shapes[node.id] = result
            graph.provenance[node.id] = _inherit(node.id, node.target, target, result)
            for face in _faces(result):
                if face.geom_type.name != "PLANE": graph.provenance[node.id].append(_key(node.id, "blend", entity=face))
    return graph


_CURRENT: Graph
