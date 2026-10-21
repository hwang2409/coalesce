from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from build123d import Edge, Face, Shape
from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer, BRepFilletAPI_MakeFillet

from .boolean import _deduplicate, _history_faces, _result_faces, _same
from .graph import GraphError
from .provenance import candidate, derive_edge_candidates, descriptor
from .references import Candidate, ElementKey

_LOGGER = logging.getLogger(__name__)


class _EdgeAlgorithm(Protocol):
    def Add(self, size: float, edge: Any) -> None: ...
    def Build(self) -> None: ...
    def IsDone(self) -> bool: ...
    def Shape(self) -> Any: ...
    def Modified(self, shape: Any) -> Any: ...
    def Generated(self, shape: Any) -> Any: ...
    def IsDeleted(self, shape: Any) -> bool: ...


@dataclass
class EdgeTreatmentResult:
    shape: Shape
    candidates: list[Candidate]
    algorithm: _EdgeAlgorithm
    consumed: dict[ElementKey, tuple[ElementKey, ...]]


def _generated_role(kind: str, edge: ElementKey) -> str:
    return f"{kind}({edge!r})"


def _collect_history(
    feature: str,
    kind: str,
    algorithm: _EdgeAlgorithm,
    target_candidates: list[Candidate],
    output_faces: list[Face],
) -> tuple[list[Candidate], list[Candidate], dict[ElementKey, tuple[ElementKey, ...]]]:
    inherited: list[Candidate] = []
    for source in target_candidates:
        if not isinstance(source.entity, Face):
            continue
        for face in _history_faces(feature, algorithm, source, output_faces):
            inherited.append(Candidate(source.key, face, descriptor(face), source.merged_from))

    generated: list[Candidate] = []
    consumed: dict[ElementKey, tuple[ElementKey, ...]] = {}
    target_edges = [source for source in target_candidates if isinstance(source.entity, Edge)]
    for source in target_edges:
        history_faces = _result_faces(
            [
                *algorithm.Modified(source.entity.wrapped),
                *algorithm.Generated(source.entity.wrapped),
            ]
        )
        generated_faces = [face for face in history_faces if _same(face, output_faces)]
        generated_keys: list[ElementKey] = []
        for face in generated_faces:
            generated_candidate = candidate(
                feature,
                _generated_role(kind, source.key),
                source.key,
                face,
                body=source.key.body,
            )
            generated.append(generated_candidate)
            generated_keys.append(generated_candidate.key)
        if algorithm.IsDeleted(source.entity.wrapped):
            consumed[source.key] = tuple(generated_keys)
    return inherited, generated, consumed


def edge_treatment_shape(
    feature: str,
    target: Shape,
    target_candidates: list[Candidate],
    edges: list[Candidate],
    size: float,
    *,
    kind: str,
) -> EdgeTreatmentResult:
    edge_keys = [asdict(edge.key) for edge in edges]
    parameter = "radius" if kind == "blend" else "distance"
    public_kind = "fillet" if kind == "blend" else "chamfer"
    invalid_code, failed_code = {
        "fillet": ("INVALID_FILLET_RADIUS", "FILLET_FAILED"),
        "chamfer": ("INVALID_CHAMFER_DISTANCE", "CHAMFER_FAILED"),
    }[public_kind]
    if size <= 0:
        raise GraphError(
            invalid_code,
            f"{public_kind} {feature} {parameter} must be positive",
            feature=feature,
            **{parameter: size},
            edges=edge_keys,
        )

    algorithm: _EdgeAlgorithm
    if public_kind == "fillet":
        algorithm = BRepFilletAPI_MakeFillet(target.wrapped)
    else:
        algorithm = BRepFilletAPI_MakeChamfer(target.wrapped)
    try:
        for edge in edges:
            algorithm.Add(size, edge.entity.wrapped)
        algorithm.Build()
        if not algorithm.IsDone():
            if isinstance(algorithm, BRepFilletAPI_MakeFillet):
                statuses = ", ".join(
                    f"contour {contour} {algorithm.StripeStatus(contour).name}"
                    for contour in map(
                        algorithm.FaultyContour,
                        range(1, algorithm.NbFaultyContours() + 1),
                    )
                )
                raise RuntimeError(
                    f"BRepFilletAPI_MakeFillet not done: {statuses or 'no status'}"
                )
            raise RuntimeError("BRepFilletAPI_MakeChamfer did not complete")
        result = Shape.cast(algorithm.Shape())
    except Exception as error:
        _LOGGER.exception("%s kernel operation failed for feature %s", public_kind, feature)
        details = {
            "feature": feature,
            parameter: size,
            "edges": edge_keys,
            "safeRange": "unknown",
            "hint": (
                "reduce the fillet radius and retry, or find the largest radius that works"
                if public_kind == "fillet"
                else "reduce the chamfer distance and retry"
            ),
            "kernelMessage": str(error),
        }
        raise GraphError(
            failed_code, f"{public_kind} feature {feature} failed", **details
        ) from error

    inherited, generated, consumed = _collect_history(
        feature, kind, algorithm, target_candidates, result.faces()
    )
    missing_history = [asdict(edge.key) for edge in edges if not consumed.get(edge.key)]
    if missing_history:
        details = {
            "feature": feature,
            parameter: size,
            "edges": missing_history,
            "safeRange": "unknown",
            "hint": "change the selected edges or reduce the treatment size",
        }
        message = f"{public_kind} feature {feature} did not return complete edge history"
        raise GraphError(failed_code, message, **details)
    result_faces = _deduplicate(feature, result, [*inherited, *generated])
    result_edges = derive_edge_candidates(feature, result, result_faces)
    return EdgeTreatmentResult(
        result,
        [*result_faces, *result_edges],
        algorithm,
        consumed,
    )


def fillet_shape(
    feature: str,
    target: Shape,
    target_candidates: list[Candidate],
    edges: list[Candidate],
    radius: float,
) -> EdgeTreatmentResult:
    return edge_treatment_shape(feature, target, target_candidates, edges, radius, kind="blend")


def chamfer_shape(
    feature: str,
    target: Shape,
    target_candidates: list[Candidate],
    edges: list[Candidate],
    distance: float,
) -> EdgeTreatmentResult:
    return edge_treatment_shape(feature, target, target_candidates, edges, distance, kind="chamfer")
