from __future__ import annotations

from dataclasses import dataclass
from math import isclose

from build123d import Compound, Edge, Face, Location, Shape, Wire
from build123d import Plane as BuildPlane
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeWire
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.gp import gp_Vec

from .graph import Circle, Extrude, GraphError, Rectangle, Sketch
from .provenance import candidate, derive_edge_candidates
from .references import Candidate, SketchSegmentSource


@dataclass
class ExtrusionResult:
    shape: Shape
    candidates: list[Candidate]


@dataclass
class _Profile:
    entity: Rectangle | Circle
    face: Face
    wire: Wire
    segments: dict[str, Edge]
    parent: int | None = None
    depth: int = 0


def _rectangle_profile(entity: Rectangle, location: Location) -> tuple[Face, Wire, dict[str, Edge]]:
    x = entity.width / 2
    y = entity.height / 2
    points = [(-x, -y, 0), (x, -y, 0), (x, y, 0), (-x, y, 0)]
    names = ("bottom", "right", "top", "left")
    edges = [
        Edge.make_line(points[index], points[(index + 1) % 4]).moved(location)
        for index in range(4)
    ]
    builder = BRepBuilderAPI_MakeWire()
    constructed: dict[str, Edge] = {}
    for name, edge in zip(names, edges, strict=True):
        builder.Add(edge.wrapped)
        constructed[name] = Edge(builder.Edge())
    wire = Wire(builder.Wire())
    return Face(wire), wire, constructed


def _profile(entity: Rectangle | Circle, sketch_location: Location) -> _Profile:
    entity_location = Location(
        (entity.position[0], entity.position[1], 0),
        (0, 0, entity.rotation),
    )
    location = sketch_location * entity_location
    if isinstance(entity, Rectangle):
        face, wire, segments = _rectangle_profile(entity, location)
        return _Profile(entity, face, wire, segments)
    if isinstance(entity, Circle):
        wire = Wire.make_circle(entity.radius).moved(location)
        (edge,) = wire.edges()
        return _Profile(entity, Face(wire), wire, {entity.id: edge})
    raise TypeError(f"unsupported sketch entity: {type(entity).__name__}")


def _intersection_area(left: Face, right: Face) -> float:
    intersection = left.intersect(right)
    return sum(face.area for face in intersection or [])


def _organize_profiles(sketch: Sketch, location: Location) -> list[_Profile]:
    profiles = [_profile(entity, location) for entity in sketch.entities]
    containers: list[list[int]] = [[] for _ in profiles]
    for left_index, left in enumerate(profiles):
        for right_index in range(left_index + 1, len(profiles)):
            right = profiles[right_index]
            overlap = _intersection_area(left.face, right.face)
            if isclose(overlap, 0.0, abs_tol=1e-7):
                continue
            left_inside = isclose(overlap, left.face.area, rel_tol=1e-9, abs_tol=1e-7)
            right_inside = isclose(overlap, right.face.area, rel_tol=1e-9, abs_tol=1e-7)
            if left_inside != right_inside:
                inner, outer = (left_index, right_index) if left_inside else (right_index, left_index)
                containers[inner].append(outer)
                continue
            raise GraphError(
                "overlapping_profiles",
                "sketch profiles overlap without nesting",
                sketch=sketch.id,
                entities=sorted([left.entity.id, right.entity.id]),
            )

    for index, profile in enumerate(profiles):
        profile.depth = len(containers[index])
        if containers[index]:
            profile.parent = min(containers[index], key=lambda item: profiles[item].face.area)
    return profiles


def _regions(profiles: list[_Profile]) -> list[tuple[Face, list[_Profile]]]:
    regions: list[tuple[Face, list[_Profile]]] = []
    for index, outer in enumerate(profiles):
        if outer.depth % 2:
            continue
        holes = [
            profile
            for profile in profiles
            if profile.parent == index and profile.depth == outer.depth + 1
        ]
        regions.append((Face(outer.wire, [hole.wire for hole in holes]), [outer, *holes]))
    return regions


def _extrude_region(
    node: Extrude,
    sketch: Sketch,
    face: Face,
    profiles: list[_Profile],
    direction,
) -> tuple[Shape, list[Candidate]]:
    prism = BRepPrimAPI_MakePrism(
        face.wrapped,
        gp_Vec(direction.X, direction.Y, direction.Z),
    )
    outer = profiles[0].entity
    faces = [
        candidate(node.id, "cap_start", outer.id, Shape.cast(prism.FirstShape())),
        candidate(node.id, "cap_end", outer.id, Shape.cast(prism.LastShape())),
    ]
    for profile in profiles:
        for segment, edge in profile.segments.items():
            generated = list(prism.Generated(edge.wrapped))
            if len(generated) != 1:
                raise RuntimeError(
                    f"extrude {node.id} generated {len(generated)} faces from segment {segment}"
                )
            source = SketchSegmentSource(sketch.id, profile.entity.id, segment)
            faces.append(
                candidate(node.id, f"side({segment})", source, Shape.cast(generated[0]))
            )
    return Shape.cast(prism.Shape()), faces


def extrude_sketch(node: Extrude, sketch: Sketch) -> ExtrusionResult:
    if not sketch.entities:
        raise ValueError(f"sketch {sketch.id} has no entities")

    location = Location(sketch.position, sketch.rotation)
    direction = BuildPlane(location).z_dir * node.height
    profiles = _organize_profiles(sketch, location)
    shapes: list[Shape] = []
    faces: list[Candidate] = []
    for region, region_profiles in _regions(profiles):
        shape, region_faces = _extrude_region(node, sketch, region, region_profiles, direction)
        shapes.append(shape)
        faces.extend(region_faces)

    shape = shapes[0] if len(shapes) == 1 else Compound(shapes)
    return ExtrusionResult(shape, [*faces, *derive_edge_candidates(node.id, shape, faces)])
