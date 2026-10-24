"""Build one full-revolution body with profile-segment provenance."""

from __future__ import annotations

from dataclasses import dataclass
from math import pi

from build123d import Edge, Face, GeomType, Location, Shape
from build123d import Plane as BuildPlane
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepPrimAPI import BRepPrimAPI_MakeRevol
from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt

from .constrained_profile import build_constrained_profile
from .extrusion import _profile, sketch_location
from .graph import GraphError, PrimitiveEntity, Revolve, Sketch
from .provenance import candidate, derive_edge_candidates
from .references import BodySource, Candidate, SketchSegmentSource

_AXIS_TOLERANCE = 1e-7


@dataclass(frozen=True)
class RevolutionResult:
    shape: Shape
    candidates: list[Candidate]
    metadata: dict[str, list[float]]


@dataclass(frozen=True)
class _RevolutionProfile:
    entity_id: str
    face: Face
    segments: dict[str, Edge]
    topology: dict[str, tuple[str, ...]]


def _selected_profile(node: Revolve, sketch: Sketch) -> PrimitiveEntity | None:
    if sketch.constrained is not None:
        if node.profile_ids not in (None, (sketch.id,)):
            raise GraphError(
                "INVALID_PARAMETER",
                f"revolve {node.id} refers to a missing constrained profile",
                feature=node.id,
                sketch=sketch.id,
                expectedProfileIds=[sketch.id],
            )
        return None
    selected_ids = node.profile_ids or tuple(entity.id for entity in sketch.entities)
    entities = [entity for entity in sketch.entities if entity.id in selected_ids]
    if len(entities) != 1 or len(selected_ids) != 1:
        raise GraphError(
            "REVOLVE_MULTIPLE_PROFILES",
            f"revolve sketch {sketch.id} must contain exactly one selected profile",
            feature=node.id,
            sketch=sketch.id,
            profileCount=len(entities),
        )
    return entities[0]


def _profile_for_revolution(
    sketch: Sketch, location: Location, entity: PrimitiveEntity | None
) -> _RevolutionProfile:
    if entity is not None:
        exact = _profile(entity, location)
        return _RevolutionProfile(exact.entity_id, exact.face, exact.segments, exact.segment_topology)
    assert sketch.constrained is not None
    exact = build_constrained_profile(sketch.constrained, sketch.id, location)
    if len(exact.face.wires()) != 1:
        raise GraphError(
            "REVOLVE_MULTIPLE_PROFILES",
            f"revolve sketch {sketch.id} must contain one outer profile and no inner loops",
            sketch=sketch.id,
        )
    return _RevolutionProfile(exact.id, exact.face, exact.segments, exact.segment_topology)


def _ordinate(point, axis: str) -> float:
    return float(point.Y if axis == "x" else point.X)


def _edge_lies_on_axis(edge: Edge, axis: str) -> bool:
    return edge.geom_type is GeomType.LINE and all(
        abs(_ordinate(vertex.center(), axis)) <= _AXIS_TOLERANCE for vertex in edge.vertices()
    )


def _validate_axis(
    profile: _RevolutionProfile, axis: str, sketch_id: str, feature_id: str
) -> set[str]:
    bounds = profile.face.bounding_box()
    minimum, maximum = (
        (float(bounds.min.Y), float(bounds.max.Y))
        if axis == "x"
        else (float(bounds.min.X), float(bounds.max.X))
    )
    if minimum < -_AXIS_TOLERANCE and maximum > _AXIS_TOLERANCE:
        raise GraphError(
            "REVOLVE_AXIS_CROSSING",
            f"revolve profile {profile.entity_id} crosses the local {axis} axis",
            feature=feature_id,
            sketchId=sketch_id,
            profile=profile.entity_id,
            axis=axis,
        )
    touches = minimum <= _AXIS_TOLERANCE and maximum >= -_AXIS_TOLERANCE
    axis_segments = {
        name for name, edge in profile.segments.items() if _edge_lies_on_axis(edge, axis)
    }
    if touches and not axis_segments:
        raise GraphError(
            "REVOLVE_AXIS_CONTACT",
            f"revolve profile {profile.entity_id} may touch the local {axis} axis only along a straight segment",
            feature=feature_id,
            sketchId=sketch_id,
            profile=profile.entity_id,
            axis=axis,
        )
    return axis_segments


def _make_revolution(node: Revolve, profile: _RevolutionProfile, location: Location):
    plane = BuildPlane(location)
    origin = plane.origin
    direction = plane.x_dir if node.axis == "x" else plane.y_dir
    try:
        maker = BRepPrimAPI_MakeRevol(
            profile.face.wrapped,
            gp_Ax1(
                gp_Pnt(origin.X, origin.Y, origin.Z),
                gp_Dir(direction.X, direction.Y, direction.Z),
            ),
            2 * pi,
            True,
        )
        maker.Build()
        if not maker.IsDone():
            raise RuntimeError("OCCT did not complete the revolution")
        return maker, Shape.cast(maker.Shape())
    except Exception as error:
        raise GraphError(
            "REVOLVE_FAILED",
            f"revolve {node.id} failed in the CAD kernel",
            feature=node.id,
        ) from error


def _validate_result(node: Revolve, shape: Shape) -> None:
    solid_count = len(shape.solids())
    if solid_count != 1:
        raise GraphError(
            "REVOLVE_MULTIPLE_SOLIDS",
            f"revolve {node.id} must produce exactly one solid",
            feature=node.id,
            solidCount=solid_count,
        )
    if not BRepCheck_Analyzer(shape.wrapped).IsValid():
        raise GraphError(
            "INVALID_BREP", f"revolve {node.id} produced invalid geometry", feature=node.id
        )


def _generated_face(node: Revolve, maker, segment: str, edge: Edge, on_axis: bool) -> Face | None:
    generated = [Shape.cast(item) for item in maker.Generated(edge.wrapped)]
    if on_axis:
        if generated:
            raise GraphError(
                "HISTORY_MISSING",
                f"revolve {node.id} axis segment {segment} unexpectedly generated geometry",
                feature=node.id,
                segment=segment,
            )
        return None
    if len(generated) != 1 or not isinstance(generated[0], Face):
        raise GraphError(
            "HISTORY_MISSING",
            f"revolve {node.id} did not map segment {segment} to exactly one face",
            feature=node.id,
            segment=segment,
            generatedCount=len(generated),
        )
    return generated[0]


def _profile_diameters(profile: _RevolutionProfile, axis: str) -> dict[str, list[float]]:
    orientation = 1 if profile.face.normal_at().Z > 0 else -1
    dimensions: dict[str, list[float]] = {"bores": [], "outerDiameters": []}
    for edge in profile.segments.values():
        if edge.geom_type is not GeomType.LINE:
            continue
        vertices = edge.vertices()
        ordinates = [_ordinate(vertex.center(), axis) for vertex in vertices]
        if max(ordinates) - min(ordinates) > _AXIS_TOLERANCE:
            continue
        ordinate = ordinates[0]
        if abs(ordinate) <= _AXIS_TOLERANCE:
            continue
        tangent = edge.tangent_at(0)
        material_direction = orientation * (tangent.X if axis == "x" else -tangent.Y)
        kind = "bores" if ordinate * material_direction > 0 else "outerDiameters"
        dimensions[kind].append(2 * abs(ordinate))  # exact; rounded only for display
    return {kind: sorted(values) for kind, values in dimensions.items()}


def _face_candidates(node: Revolve, sketch: Sketch, profile: _RevolutionProfile, maker, axis_segments: set[str]) -> list[Candidate]:
    body = BodySource(node.id, profile.entity_id)
    faces = []
    for segment, edge in profile.segments.items():
        generated = _generated_face(node, maker, segment, edge, segment in axis_segments)
        if generated is None:
            continue
        source = SketchSegmentSource(
            sketch.id,
            profile.entity_id,
            segment,
            profile.topology.get(segment, ()),
        )
        faces.append(candidate(node.id, f"side({segment})", source, generated, body=body))
    return faces


def revolve_sketch(node: Revolve, sketch: Sketch) -> RevolutionResult:
    if node.operation != "new_body" or node.axis not in {"x", "y"}:
        raise GraphError(
            "INVALID_REVOLVE",
            f"revolve {node.id} supports only a full new_body around local x or y",
            feature=node.id,
            operation=node.operation,
            axis=node.axis,
        )
    entity = _selected_profile(node, sketch)
    local = _profile_for_revolution(sketch, Location(), entity)
    axis_segments = _validate_axis(local, node.axis, sketch.id, node.id)
    location = sketch_location(sketch)
    profile = _profile_for_revolution(sketch, location, entity)
    maker, shape = _make_revolution(node, profile, location)
    _validate_result(node, shape)
    faces = _face_candidates(node, sketch, profile, maker, axis_segments)
    candidates = [*faces, *derive_edge_candidates(node.id, shape, faces)]
    return RevolutionResult(shape, candidates, _profile_diameters(local, node.axis))
