from __future__ import annotations

from itertools import combinations
from typing import Any

from build123d import Shape
from OCP.Bnd import Bnd_Box
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepExtrema import BRepExtrema_DistShapeShape

from .assembly import ResolvedOccurrence

MAX_INSPECTION_OCCURRENCES = 16
MAX_INSPECTION_PAIRS = 120
INTERFERENCE_VOLUME_TOLERANCE_MM3 = 1e-9


class AssemblyInspectionError(ValueError):
    def __init__(self, code: str, message: str, **details: Any):
        self.code = code
        self.details = details
        super().__init__(message)


def _candidate_pairs(
    occurrences: list[ResolvedOccurrence], occurrence_ids: list[str] | None
) -> list[tuple[ResolvedOccurrence, ResolvedOccurrence]]:
    by_id = {item.occurrence.id: item for item in occurrences}
    selected_ids = sorted(by_id) if occurrence_ids is None else occurrence_ids
    if len(selected_ids) != len(set(selected_ids)):
        raise AssemblyInspectionError(
            "INVALID_PARAMETER", "occurrenceIds must not contain duplicates"
        )
    unknown = sorted(set(selected_ids) - set(by_id))
    if unknown:
        raise AssemblyInspectionError(
            "UNKNOWN_ID",
            f"unknown assembly occurrence: {unknown[0]}",
            occurrenceIds=unknown,
            validOccurrenceIds=sorted(by_id),
        )
    if len(selected_ids) < 2:
        raise AssemblyInspectionError(
            "INVALID_PARAMETER", "assembly interference requires at least two occurrences"
        )
    if len(selected_ids) > MAX_INSPECTION_OCCURRENCES:
        raise AssemblyInspectionError(
            "INVALID_PARAMETER",
            "assembly interference checks at most 16 selected occurrences (120 pairs)",
            selectedOccurrenceCount=len(selected_ids),
            maximumOccurrenceCount=MAX_INSPECTION_OCCURRENCES,
            maximumPairCount=MAX_INSPECTION_PAIRS,
            hint="provide occurrenceIds with at most 16 occurrence ids",
        )
    selected = [by_id[item] for item in sorted(selected_ids)]
    return list(combinations(selected, 2))


def _bounding_box(shape: Any) -> Bnd_Box:
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape.wrapped, box, True, True)
    return box


def _boxes_overlap(first: Any, second: Any) -> bool:
    return not _bounding_box(first).IsOut(_bounding_box(second))


def _common_volume(first: Any, second: Any) -> float:
    operation = BRepAlgoAPI_Common(first.wrapped, second.wrapped)
    operation.Build()
    if not operation.IsDone():
        raise ValueError("exact common-volume operation did not complete")
    common = Shape.cast(operation.Shape())
    if not BRepCheck_Analyzer(common.wrapped).IsValid():
        raise ValueError("exact common-volume operation returned an invalid shape")
    return sum(solid.volume for solid in common.solids())


def _minimum_clearance(first: Any, second: Any) -> float:
    operation = BRepExtrema_DistShapeShape(first.wrapped, second.wrapped)
    operation.Perform()
    if not operation.IsDone():
        raise ValueError("exact minimum-clearance operation did not complete")
    return operation.Value()


def _pair_error(
    first: ResolvedOccurrence,
    second: ResolvedOccurrence,
    revision: int,
    error: BaseException,
) -> dict[str, Any]:
    return {
        "occurrenceIds": [first.occurrence.id, second.occurrence.id],
        "partIds": [first.occurrence.part_id, second.occurrence.part_id],
        "error": {
            "code": "INSPECTION_FAILED",
            "message": str(error) or type(error).__name__,
        },
        "revision": revision,
    }


def _inspect_pair(
    first: ResolvedOccurrence, second: ResolvedOccurrence, revision: int
) -> dict[str, Any]:
    try:
        overlap = (
            _common_volume(first.shape, second.shape)
            if _boxes_overlap(first.shape, second.shape)
            else 0.0
        )
        overlap = overlap if overlap > INTERFERENCE_VOLUME_TOLERANCE_MM3 else 0.0
        interferes = overlap > 0.0
        clearance = 0.0 if interferes else _minimum_clearance(first.shape, second.shape)
    except Exception as error:  # noqa: BLE001 - kernel errors become explicit measurement evidence.
        return _pair_error(first, second, revision, error)
    return {
        "occurrenceIds": [first.occurrence.id, second.occurrence.id],
        "partIds": [first.occurrence.part_id, second.occurrence.part_id],
        "overlapVolume": {"value": overlap, "unit": "mm^3"},
        "clearance": {"value": clearance, "unit": "mm"},
        "interferes": interferes,
        "revision": revision,
    }


def inspect_assembly(
    occurrences: list[ResolvedOccurrence],
    occurrence_ids: list[str] | None,
    revision: int,
) -> list[dict[str, Any]]:
    """Return stable exact pair evidence for one bounded occurrence selection."""
    return [_inspect_pair(first, second, revision) for first, second in _candidate_pairs(occurrences, occurrence_ids)]
