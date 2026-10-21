from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from build123d import Edge, Face, Shape
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet

from .boolean import _deduplicate, _history_faces, _result_faces, _same
from .graph import GraphError
from .provenance import candidate, derive_edge_candidates, descriptor
from .references import Candidate, ElementKey

_LOGGER = logging.getLogger(__name__)


@dataclass
class FilletResult:
    shape: Shape
    candidates: list[Candidate]
    algorithm: BRepFilletAPI_MakeFillet
    consumed: dict[ElementKey, tuple[ElementKey, ...]]


def _blend_role(edge: ElementKey) -> str:
    return f"blend({edge!r})"


def _collect_history(
    feature: str,
    algorithm: BRepFilletAPI_MakeFillet,
    target_candidates: list[Candidate],
    output_faces: list[Face],
) -> tuple[list[Candidate], list[Candidate], dict[ElementKey, tuple[ElementKey, ...]]]:
    inherited: list[Candidate] = []
    for source in target_candidates:
        if not isinstance(source.entity, Face):
            continue
        for face in _history_faces(feature, algorithm, source, output_faces):
            inherited.append(
                Candidate(source.key, face, descriptor(face), source.merged_from)
            )

    generated: list[Candidate] = []
    consumed: dict[ElementKey, tuple[ElementKey, ...]] = {}
    target_edges = [
        source for source in target_candidates if isinstance(source.entity, Edge)
    ]
    for source in target_edges:
        history_faces = _result_faces(
            [
                *algorithm.Modified(source.entity.wrapped),
                *algorithm.Generated(source.entity.wrapped),
            ]
        )
        blend_faces = [face for face in history_faces if _same(face, output_faces)]
        blend_keys: list[ElementKey] = []
        for face in blend_faces:
            blend = candidate(
                feature,
                _blend_role(source.key),
                source.key,
                face,
                body=source.key.body,
            )
            generated.append(blend)
            blend_keys.append(blend.key)
        if algorithm.IsDeleted(source.entity.wrapped):
            consumed[source.key] = tuple(blend_keys)
    return inherited, generated, consumed


def fillet_shape(
    feature: str,
    target: Shape,
    target_candidates: list[Candidate],
    edges: list[Candidate],
    radius: float,
) -> FilletResult:
    edge_keys = [asdict(edge.key) for edge in edges]  # plain data: errors reach JSON responses
    if radius <= 0:
        raise GraphError(
            "invalid_fillet_radius",
            f"fillet {feature} radius must be positive",
            feature=feature,
            radius=radius,
            edges=edge_keys,
        )

    algorithm = BRepFilletAPI_MakeFillet(target.wrapped)
    try:
        for edge in edges:
            algorithm.Add(radius, edge.entity.wrapped)
        algorithm.Build()
        if not algorithm.IsDone():
            statuses = ", ".join(
                f"contour {contour} {algorithm.StripeStatus(contour).name}"
                for contour in map(algorithm.FaultyContour, range(1, algorithm.NbFaultyContours() + 1))
            )
            raise RuntimeError(f"BRepFilletAPI_MakeFillet not done: {statuses or 'no status'}")
        result = Shape.cast(algorithm.Shape())
    except Exception as error:
        _LOGGER.exception("fillet kernel operation failed for feature %s", feature)
        raise GraphError(
            "fillet_failed",
            f"fillet feature {feature} failed",
            feature=feature,
            radius=radius,
            edges=edge_keys,
            hint="reduce the fillet radius and retry, or find the largest radius that works",
            # the kernel's own words, for "Error details" only; the hint is the guidance
            kernelMessage=str(error),
        ) from error

    inherited, generated, consumed = _collect_history(
        feature, algorithm, target_candidates, result.faces()
    )
    result_faces = _deduplicate(feature, result, [*inherited, *generated])
    result_edges = derive_edge_candidates(feature, result, result_faces)
    return FilletResult(
        result,
        [*result_faces, *result_edges],
        algorithm,
        consumed,
    )
