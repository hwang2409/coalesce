from __future__ import annotations

from dataclasses import dataclass

from build123d import Compound, Edge, Face, Location, Plane as BuildPlane, Shape, Wire
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.gp import gp_Vec

from .graph import Circle, Extrude, Rectangle, Sketch
from .references import Candidate, ElementKey


@dataclass
class ExtrusionResult:
    shape: Shape
    candidates: list[Candidate]


def _descriptor(entity: Shape) -> dict[str, object]:
    center = entity.center()
    descriptor: dict[str, object] = {
        "type": entity.geom_type.name,
        "center": (center.X, center.Y, center.Z),
    }
    if isinstance(entity, Face):
        try:
            normal = entity.normal_at()
        except ValueError:
            pass
        else:
            descriptor["normal"] = (normal.X, normal.Y, normal.Z)
        descriptor["area"] = entity.area
    else:
        descriptor["length"] = entity.length
    return descriptor


def _candidate(feature: str, role: str, source: str | None, entity: Shape) -> Candidate:
    return Candidate(ElementKey(feature, role, source), entity, _descriptor(entity))


def _profile(entity: Rectangle | Circle, location: Location) -> tuple[Face, dict[str, Edge]]:
    if isinstance(entity, Rectangle):
        x = entity.width / 2
        y = entity.height / 2
        points = [(-x, -y, 0), (x, -y, 0), (x, y, 0), (-x, y, 0)]
        names = ("bottom", "right", "top", "left")
        constructed = [
            Edge.make_line(points[index], points[(index + 1) % 4]).moved(location)
            for index in range(4)
        ]
        face = Face(Wire(constructed))
        return face, dict(zip(names, face.edges(), strict=True))
    if isinstance(entity, Circle):
        face = Face(Wire.make_circle(entity.radius).moved(location))
        return face, {entity.id: face.edges()[0]}
    raise TypeError(f"unsupported sketch entity: {type(entity).__name__}")


def _between_role(roles: list[str]) -> str:
    ordered = sorted(roles, key=lambda role: (role.startswith("cap_"), role))
    return f"between({ordered[0]},{ordered[1]})"


def extrude_sketch(node: Extrude, sketch: Sketch) -> ExtrusionResult:
    if not sketch.entities:
        raise ValueError(f"sketch {sketch.id} has no entities")

    location = Location(sketch.position, sketch.rotation)
    direction = BuildPlane(location).z_dir * node.height
    shapes: list[Shape] = []
    faces: list[Candidate] = []

    for entity in sketch.entities:
        profile, segments = _profile(entity, location)
        prism = BRepPrimAPI_MakePrism(
            profile.wrapped,
            gp_Vec(direction.X, direction.Y, direction.Z),
        )
        shape = Shape.cast(prism.Shape())
        shapes.append(shape)
        faces.extend(
            (
                _candidate(node.id, "cap_start", entity.id, Shape.cast(prism.FirstShape())),
                _candidate(node.id, "cap_end", entity.id, Shape.cast(prism.LastShape())),
            )
        )
        for segment, edge in segments.items():
            generated = list(prism.Generated(edge.wrapped))
            if len(generated) != 1:
                raise RuntimeError(
                    f"extrude {node.id} generated {len(generated)} faces from segment {segment}"
                )
            faces.append(
                _candidate(
                    node.id,
                    f"side({segment})",
                    segment,
                    Shape.cast(generated[0]),
                )
            )

    shape = shapes[0] if len(shapes) == 1 else Compound(shapes)
    candidates = list(faces)
    for edge in shape.edges():
        adjacent = [
            candidate
            for candidate in faces
            if any(edge.is_same(face_edge) for face_edge in candidate.entity.edges())
        ]
        roles = sorted({candidate.key.role for candidate in adjacent})
        if len(roles) == 2:
            candidates.append(
                _candidate(node.id, _between_role(roles), None, edge)
            )
    return ExtrusionResult(shape, candidates)
