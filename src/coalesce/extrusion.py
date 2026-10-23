from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isclose

from build123d import Compound, Edge, Face, Location, Shape, Wire
from build123d import Plane as BuildPlane
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeWire, BRepBuilderAPI_NurbsConvert
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.gp import gp_Vec

from .constrained_profile import build_constrained_profile
from .graph import Circle, Extrude, GraphError, Polyline, Rectangle, Sketch, Spline, validate_sketch
from .profile_validation import build_outline_profile
from .provenance import candidate, derive_edge_candidates
from .references import BodySource, Candidate, SketchSegmentSource


@dataclass
class ExtrusionResult:
    shape: Shape
    candidates: list[Candidate]


_LINEAR_BOUNDARY_TOLERANCE = 1e-7
_AREA_RELATIVE_TOLERANCE = 1e-9
# Keep an absolute floor for floating-point noise without imposing a linear tolerance on areas.
_AREA_TOLERANCE_FLOOR = 1e-18


_ORIGIN_PLANES = {
    "XY": BuildPlane.XY,
    "XZ": BuildPlane.XZ,
    "YZ": BuildPlane.YZ,
}


def sketch_location(sketch: Sketch) -> Location:
    validate_sketch(sketch)
    frame = _ORIGIN_PLANES[sketch.plane.name]
    normal = frame.z_dir * sketch.plane.offset
    return (
        Location(sketch.position, sketch.rotation)
        * Location((normal.X, normal.Y, normal.Z))
        * frame.location
    )


class _ProfileRelation(Enum):
    DISJOINT = "disjoint"
    LEFT_IN_RIGHT = "left_in_right"
    RIGHT_IN_LEFT = "right_in_left"
    TOUCHING = "touching"
    OVERLAP = "overlap"


@dataclass
class _Profile:
    entity_id: str
    face: Face
    wire: Wire
    segments: dict[str, Edge]
    segment_topology: dict[str, tuple[str, ...]]
    parent: int | None = None
    depth: int = 0


def _rectangle_profile(entity: Rectangle, location: Location) -> tuple[Face, Wire, dict[str, Edge]]:
    x = entity.width / 2
    y = entity.height / 2
    points = [(-x, -y, 0), (x, -y, 0), (x, y, 0), (-x, y, 0)]
    names = ("bottom", "right", "top", "left")
    edges = [
        Edge.make_line(points[index], points[(index + 1) % 4]).moved(location) for index in range(4)
    ]
    builder = BRepBuilderAPI_MakeWire()
    constructed: dict[str, Edge] = {}
    for name, edge in zip(names, edges, strict=True):
        builder.Add(edge.wrapped)
        constructed[name] = Edge(builder.Edge())
    wire = Wire(builder.Wire())
    return Face(wire), wire, constructed


def _outline_profile(entity: Polyline | Spline, location: Location) -> _Profile:
    exact = build_outline_profile(
        "polyline" if isinstance(entity, Polyline) else "spline",
        entity.points,
        entity.point_ids,
        location,
        point_ids_explicit=entity.point_ids_explicit,
    )
    return _Profile(
        entity.id,
        exact.face,
        exact.wire,
        exact.segments,
        exact.segment_topology,
    )


def _profile(entity: Rectangle | Circle | Polyline | Spline, sketch_location: Location) -> _Profile:
    if isinstance(entity, (Polyline, Spline)):
        return _outline_profile(entity, sketch_location)
    entity_location = Location(
        (entity.position[0], entity.position[1], 0),
        (0, 0, entity.rotation),
    )
    location = sketch_location * entity_location
    if isinstance(entity, Rectangle):
        face, wire, segments = _rectangle_profile(entity, location)
        return _Profile(entity.id, face, wire, segments, {})
    if isinstance(entity, Circle):
        wire = Wire.make_circle(entity.radius).moved(location)
        (edge,) = wire.edges()
        return _Profile(entity.id, Face(wire), wire, {"circle": edge}, {})
    raise TypeError(f"unsupported sketch entity: {type(entity).__name__}")


def _intersection_area(left: Face, right: Face) -> float:
    intersection = left.intersect(right)
    return sum(face.area for face in intersection or [])


def _profile_relation(left: _Profile, right: _Profile) -> _ProfileRelation:
    overlap = _intersection_area(left.face, right.face)
    area_tolerance = max(
        min(left.face.area, right.face.area) * _AREA_RELATIVE_TOLERANCE,
        _AREA_TOLERANCE_FLOOR,
    )
    left_inside = isclose(
        overlap,
        left.face.area,
        rel_tol=_AREA_RELATIVE_TOLERANCE,
        abs_tol=area_tolerance,
    )
    right_inside = isclose(
        overlap,
        right.face.area,
        rel_tol=_AREA_RELATIVE_TOLERANCE,
        abs_tol=area_tolerance,
    )
    boundaries_touch = left.wire.distance_to(right.wire) <= _LINEAR_BOUNDARY_TOLERANCE
    if left_inside != right_inside:
        if boundaries_touch:
            return _ProfileRelation.TOUCHING
        return _ProfileRelation.LEFT_IN_RIGHT if left_inside else _ProfileRelation.RIGHT_IN_LEFT
    if isclose(overlap, 0.0, abs_tol=area_tolerance):
        return _ProfileRelation.TOUCHING if boundaries_touch else _ProfileRelation.DISJOINT
    return _ProfileRelation.OVERLAP


def _pair_error(
    sketch: Sketch, left: _Profile, right: _Profile, relation: _ProfileRelation
) -> None:
    if relation not in (_ProfileRelation.TOUCHING, _ProfileRelation.OVERLAP):
        return
    details = {
        "sketch": sketch.id,
        "entities": sorted([left.entity_id, right.entity_id]),
    }
    if relation is _ProfileRelation.TOUCHING:
        raise GraphError("TOUCHING_PROFILES", "sketch profile boundaries touch", **details)
    raise GraphError(
        "OVERLAPPING_PROFILES",
        "sketch profiles overlap without nesting",
        **details,
    )


def _organize_profiles(sketch: Sketch, location: Location) -> list[_Profile]:
    profiles = [_profile(entity, location) for entity in sketch.entities]
    containers: list[list[int]] = [[] for _ in profiles]
    for left_index, left in enumerate(profiles):
        for right_index in range(left_index + 1, len(profiles)):
            right = profiles[right_index]
            relation = _profile_relation(left, right)
            _pair_error(sketch, left, right, relation)
            if relation is _ProfileRelation.LEFT_IN_RIGHT:
                containers[left_index].append(right_index)
            elif relation is _ProfileRelation.RIGHT_IN_LEFT:
                containers[right_index].append(left_index)

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
    converter = (
        BRepBuilderAPI_NurbsConvert(prism.Shape())
        if any("spline" in profile.segments for profile in profiles)
        else None
    )

    def final_shape(shape) -> Shape:
        return (
            Shape.cast(converter.ModifiedShape(shape))
            if converter is not None
            else Shape.cast(shape)
        )

    outer = profiles[0]
    body = BodySource(node.id, outer.entity_id)
    faces = [
        candidate(
            node.id,
            "cap_start",
            outer.entity_id,
            final_shape(prism.FirstShape()),
            body=body,
        ),
        candidate(
            node.id,
            "cap_end",
            outer.entity_id,
            final_shape(prism.LastShape()),
            body=body,
        ),
    ]
    for profile in profiles:
        for segment, edge in profile.segments.items():
            generated = list(prism.Generated(edge.wrapped))
            if len(generated) != 1:
                raise RuntimeError(
                    f"extrude {node.id} generated {len(generated)} faces from segment {segment}"
                )
            source = SketchSegmentSource(
                sketch.id,
                profile.entity_id,
                segment,
                profile.segment_topology.get(segment, ()),
            )
            faces.append(
                candidate(
                    node.id,
                    f"side({segment})",
                    source,
                    final_shape(generated[0]),
                    body=body,
                )
            )
    return final_shape(prism.Shape()), faces


def extrude_sketch(node: Extrude, sketch: Sketch) -> ExtrusionResult:
    if not sketch.entities and sketch.constrained is None:
        raise ValueError(f"sketch {sketch.id} has no entities")

    location = sketch_location(sketch)
    direction = BuildPlane(location).z_dir * node.height
    if sketch.constrained is not None:
        exact = build_constrained_profile(sketch.constrained, sketch.id, location)
        profiles = [
            _Profile(
                exact.id,
                exact.face,
                exact.wire,
                exact.segments,
                exact.segment_topology,
            )
        ]
        regions = [(exact.face, profiles)]
    else:
        profiles = _organize_profiles(sketch, location)
        regions = _regions(profiles)
    shapes: list[Shape] = []
    faces: list[Candidate] = []
    for region, region_profiles in regions:
        shape, region_faces = _extrude_region(node, sketch, region, region_profiles, direction)
        shapes.append(shape)
        faces.extend(region_faces)

    shape = shapes[0] if len(shapes) == 1 else Compound(shapes)
    return ExtrusionResult(shape, [*faces, *derive_edge_candidates(node.id, shape, faces)])
