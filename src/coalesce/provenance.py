from __future__ import annotations

from build123d import Face, Shape

from .references import BodySource, Candidate, ElementKey


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


def _adjacent_keys(edge: Shape, faces: list[Candidate]) -> list[ElementKey]:
    adjacent = [
        face
        for face in faces
        if any(edge.is_same(face_edge) for face_edge in face.entity.edges())
    ]
    return sorted(
        {face.key for face in adjacent},
        key=lambda key: (key.feature, key.role, str(key.source)),
    )


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
    vertical_roles = {
        "outer_wall": "outer_vertical",
        "inner_wall": "inner_vertical",
        "edge": "lid_vertical",
        "lip_outer": "lip_outer_vertical",
        "lip_inner": "lip_inner_vertical",
    }
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
            if keys[0].role == keys[1].role and keys[0].role in vertical_roles:
                result.append(
                    candidate(
                        edge_feature,
                        vertical_roles[keys[0].role],
                        source,
                        edge,
                        body=body,
                    )
                )
    return result
