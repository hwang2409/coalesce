from __future__ import annotations

from build123d import Face, Shape

from .references import Candidate, ElementKey


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
) -> Candidate:
    return Candidate(ElementKey(feature, role, source), entity, descriptor(entity))


def _between_role(roles: list[str]) -> str:
    ordered = sorted(roles, key=lambda role: (role.startswith("cap_"), role))
    return f"between({ordered[0]},{ordered[1]})"


def derive_edge_candidates(
    feature: str,
    shape: Shape,
    faces: list[Candidate],
) -> list[Candidate]:
    result: list[Candidate] = []
    for edge in shape.edges():
        adjacent = [
            face
            for face in faces
            if any(edge.is_same(face_edge) for face_edge in face.entity.edges())
        ]
        keys = sorted(
            {face.key for face in adjacent},
            key=lambda key: (key.feature, key.role, str(key.source)),
        )
        if len(keys) == 2:
            result.append(
                candidate(
                    feature,
                    _between_role([key.role for key in keys]),
                    tuple(keys),
                    edge,
                )
            )
    return result
