from __future__ import annotations

import math

from build123d import Face, Shape

from .references import VERTICAL_EDGE_ROLES, BodySource, Candidate, ElementKey


def descriptor(entity: Shape) -> dict[str, object]:
    center = entity.center()
    value: dict[str, object] = {
        "type": entity.geom_type.name,
        "center": (center.X, center.Y, center.Z),
    }
    if isinstance(entity, Face):
        try:
            normal = entity.normal_at()
        except ValueError:
            pass
        else:
            value["normal"] = (normal.X, normal.Y, normal.Z)
        value["area"] = entity.area
    else:
        value["length"] = entity.length
    return value


def candidate(
    feature: str,
    role: str,
    source: object,
    entity: Shape,
    *,
    body: BodySource,
) -> Candidate:
    return Candidate(ElementKey(feature, role, source, body), entity, descriptor(entity))


def _between_role(roles: list[str]) -> str:
    ordered = sorted(roles, key=lambda role: (role.startswith("cap_"), role))
    return f"between({ordered[0]},{ordered[1]})"


def _adjacent_faces(edge: Shape, faces: list[Candidate]) -> list[Candidate]:
    return [
        face
        for face in faces
        if any(edge.is_same(face_edge) for face_edge in face.entity.edges())
    ]


def _adjacent_keys(edge: Shape, faces: list[Candidate]) -> list[ElementKey]:
    return sorted(
        {face.key for face in _adjacent_faces(edge, faces)},
        key=lambda key: (key.feature, key.role, str(key.source)),
    )


def _is_sharp(edge: Shape, faces: list[Candidate], tolerance: float = 1e-6) -> bool:
    adjacent = _adjacent_faces(edge, faces)
    if len(adjacent) != 2:
        return False
    point = edge.center()
    normals = [face.entity.normal_at(point) for face in adjacent]
    dot = sum(
        getattr(normals[0], axis) * getattr(normals[1], axis) for axis in "XYZ"
    )
    return math.acos(max(-1.0, min(1.0, abs(dot)))) > tolerance


def _same_body(keys: list[ElementKey]) -> BodySource:
    bodies = {key.body for key in keys}
    if len(bodies) != 1 or None in bodies:
        raise RuntimeError("an edge must be bounded by faces from one body")
    body = bodies.pop()
    assert body is not None
    return body


def derive_edge_candidates(
    feature: str,
    shape: Shape,
    faces: list[Candidate],
) -> list[Candidate]:
    result: list[Candidate] = []
    for edge in shape.edges():
        keys = _adjacent_keys(edge, faces)
        if len(keys) == 2:
            edge_feature = (
                keys[0].feature if keys[0].feature == keys[1].feature else feature
            )
            source = tuple(keys)
            body = _same_body(keys)
            result.append(
                candidate(
                    edge_feature,
                    _between_role([key.role for key in keys]),
                    source,
                    edge,
                    body=body,
                )
            )
            if (
                keys[0].role == keys[1].role
                and keys[0].role in VERTICAL_EDGE_ROLES
                and _is_sharp(edge, faces)
            ):
                result.append(
                    candidate(
                        edge_feature,
                        VERTICAL_EDGE_ROLES[keys[0].role],
                        source,
                        edge,
                        body=body,
                    )
                )
    return result
