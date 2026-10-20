from __future__ import annotations

from dataclasses import dataclass

from build123d import Face, Shape
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.TopAbs import TopAbs_FACE

from .graph import GraphError
from .provenance import candidate, derive_edge_candidates, descriptor
from .references import (
    BodySource,
    Candidate,
    HoleSurfaceSource,
    ToolCapSource,
)


@dataclass
class BooleanResult:
    shape: Shape
    candidates: list[Candidate]
    algorithm: BRepAlgoAPI_Cut
    removed_material: bool


@dataclass
class FuseResult:
    shape: Shape
    candidates: list[Candidate]
    algorithm: BRepAlgoAPI_Fuse
    joined_material: bool


def _same(shape: Shape, candidates: list[Shape]) -> bool:
    return any(shape.is_same(other) for other in candidates)


def _result_faces(shapes) -> list[Face]:
    return [Face(shape) for shape in shapes if shape.ShapeType() == TopAbs_FACE]


def _history_faces(
    feature: str,
    algorithm: BRepAlgoAPI_Cut | BRepAlgoAPI_Fuse,
    source: Candidate,
    output_faces: list[Face],
) -> list[Face]:
    history = [
        *_result_faces(algorithm.Modified(source.entity.wrapped)),
        *_result_faces(algorithm.Generated(source.entity.wrapped)),
    ]
    matches = [face for face in history if _same(face, output_faces)]
    if not matches and not algorithm.IsDeleted(source.entity.wrapped):
        matches = [face for face in output_faces if face.is_same(source.entity)]
    unique: list[Face] = []
    for face in matches:
        if not _same(face, unique):
            unique.append(face)
    if not unique and not algorithm.IsDeleted(source.entity.wrapped):
        raise GraphError(
            "history_missing",
            f"OCCT history is missing a result face for feature {feature}",
            feature=feature,
            source=source.key,
        )
    return unique


def _body_for_face(
    feature: str,
    face: Face,
    result: Shape,
    inherited: list[Candidate],
) -> BodySource:
    bodies: set[BodySource] = set()
    for solid in result.solids():
        if not _same(face, solid.faces()):
            continue
        bodies.update(
            inherited_face.key.body
            for inherited_face in inherited
            if inherited_face.key.body is not None
            and _same(inherited_face.entity, solid.faces())
        )
    if len(bodies) != 1:
        raise GraphError(
            "mixed_body_ancestry",
            f"feature {feature} result face does not have one target body",
            feature=feature,
            bodies=sorted((body.feature, body.entity) for body in bodies),
        )
    return bodies.pop()


def _tool_role(feature: str, tool: Candidate, kind: str) -> tuple[str, object]:
    if kind == "hole":
        role = "hole_wall" if tool.key.role.startswith("side(") else "hole_floor"
        return role, HoleSurfaceSource(feature, role)
    if tool.key.role.startswith("side("):
        return tool.key.role, tool.key.source
    return "pocket_floor", ToolCapSource(tool.key.feature, tool.key.role)


def _deduplicate(feature: str, result: Shape, faces: list[Candidate]) -> list[Candidate]:
    resolved: list[Candidate] = []
    for output in result.faces():
        sources = [item for item in faces if output.is_same(item.entity)]
        if not sources:
            raise GraphError(
                "history_missing",
                f"OCCT history is missing provenance for a face from feature {feature}",
                feature=feature,
            )
        keys = {item.key for item in sources}
        bodies = {key.body for key in keys}
        if len(bodies) != 1 or None in bodies:
            raise GraphError(
                "mixed_body_ancestry",
                f"feature {feature} face has mixed target-body ancestry",
                feature=feature,
                bodies=sorted(str(body) for body in bodies),
            )
        primary = min(sources, key=lambda item: str(item.key))
        merged_keys = {
            key
            for source in sources
            for key in (source.key, *source.merged_from)
        }
        primary.merged_from = tuple(
            sorted(merged_keys - {primary.key}, key=str)
        )
        resolved.append(primary)
    return resolved


def _removed_material(
    algorithm: BRepAlgoAPI_Cut,
    target: Shape,
    target_candidates: list[Candidate],
    tool: Shape,
    tool_candidates: list[Candidate],
    output_faces: list[Face],
) -> bool:
    common = BRepAlgoAPI_Common(target.wrapped, tool.wrapped)
    common.Build()
    if not common.IsDone() or not Shape.cast(common.Shape()).solids():
        return False

    tool_face_appears = any(
        _same(face, output_faces)
        for source in tool_candidates
        if isinstance(source.entity, Face)
        for face in _result_faces(
            [
                *algorithm.Modified(source.entity.wrapped),
                *algorithm.Generated(source.entity.wrapped),
            ]
        )
    )
    target_changed = any(
        algorithm.IsDeleted(source.entity.wrapped)
        or bool(algorithm.Modified(source.entity.wrapped))
        for source in target_candidates
        if isinstance(source.entity, Face)
    )
    return tool_face_appears or target_changed


def cut_shape(
    feature: str,
    target: Shape,
    target_candidates: list[Candidate],
    tool: Shape,
    tool_candidates: list[Candidate],
    *,
    kind: str = "cut",
) -> BooleanResult:
    algorithm = BRepAlgoAPI_Cut(target.wrapped, tool.wrapped)
    algorithm.Build()
    if not algorithm.IsDone():
        raise GraphError("boolean_failed", f"cut feature {feature} failed", feature=feature)

    result = Shape.cast(algorithm.Shape())
    output_faces = result.faces()
    inherited: list[Candidate] = []
    for source in target_candidates:
        if not isinstance(source.entity, Face):
            continue
        for face in _history_faces(feature, algorithm, source, output_faces):
            inherited.append(Candidate(source.key, face, descriptor(face), source.merged_from))

    generated: list[Candidate] = []
    for source in tool_candidates:
        if not isinstance(source.entity, Face):
            continue
        role, key_source = _tool_role(feature, source, kind)
        for face in _history_faces(feature, algorithm, source, output_faces):
            generated.append(
                candidate(
                    feature,
                    role,
                    key_source,
                    face,
                    body=_body_for_face(feature, face, result, inherited),
                )
            )

    result_faces = _deduplicate(feature, result, [*inherited, *generated])
    edges = derive_edge_candidates(feature, result, result_faces)
    removed_material = _removed_material(
        algorithm,
        target,
        target_candidates,
        tool,
        tool_candidates,
        output_faces,
    )
    return BooleanResult(result, [*result_faces, *edges], algorithm, removed_material)


def _join_body(feature: str, target_candidates: list[Candidate]) -> BodySource:
    bodies = {candidate.key.body for candidate in target_candidates}
    if len(bodies) != 1 or None in bodies:
        raise GraphError(
            "join_target_body",
            f"join extrude {feature} target must contain exactly one body",
            feature=feature,
            bodies=sorted(str(body) for body in bodies),
        )
    body = bodies.pop()
    assert body is not None
    return body


def _joined_material(
    algorithm: BRepAlgoAPI_Fuse,
    result: Shape,
    target: Shape,
    target_candidates: list[Candidate],
    tool: Shape,
) -> bool:
    fewer_solids = len(result.solids()) < len(target.solids()) + len(tool.solids())
    target_changed = any(
        algorithm.IsDeleted(source.entity.wrapped)
        or len(algorithm.Modified(source.entity.wrapped)) > 0
        for source in target_candidates
        if isinstance(source.entity, Face)
    )
    return fewer_solids or target_changed


def fuse_shape(
    feature: str,
    target: Shape,
    target_candidates: list[Candidate],
    tool: Shape,
    tool_candidates: list[Candidate],
) -> FuseResult:
    body = _join_body(feature, target_candidates)
    algorithm = BRepAlgoAPI_Fuse(target.wrapped, tool.wrapped)
    algorithm.Build()
    if not algorithm.IsDone():
        raise GraphError("boolean_failed", f"join extrude {feature} failed", feature=feature)
    algorithm.SimplifyResult()

    result = Shape.cast(algorithm.Shape())
    output_faces = result.faces()
    mapped: list[Candidate] = []
    for source in target_candidates:
        if not isinstance(source.entity, Face):
            continue
        for face in _history_faces(feature, algorithm, source, output_faces):
            mapped.append(Candidate(source.key, face, descriptor(face), source.merged_from))
    for source in tool_candidates:
        if not isinstance(source.entity, Face):
            continue
        for face in _history_faces(feature, algorithm, source, output_faces):
            key = source.key
            mapped.append(
                candidate(
                    key.feature,
                    key.role,
                    key.source,
                    face,
                    body=body,
                )
            )

    result_faces = _deduplicate(feature, result, mapped)
    edges = derive_edge_candidates(feature, result, result_faces)
    return FuseResult(
        result,
        [*result_faces, *edges],
        algorithm,
        _joined_material(algorithm, result, target, target_candidates, tool),
    )
