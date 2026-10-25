from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from time import monotonic
from typing import Any

from .assembly import ResolvedOccurrence

MAX_INSPECTION_OCCURRENCES = 16
MAX_INSPECTION_PAIRS = 120
INSPECTION_DEADLINE_SECONDS = 10.0
INTERFERENCE_VOLUME_TOLERANCE_MM3 = 1e-9


class AssemblyInspectionError(ValueError):
    def __init__(self, code: str, message: str, **details: Any):
        self.code = code
        self.details = details
        super().__init__(message)


@dataclass(frozen=True)
class _KernelSymbols:
    box: Any
    add_bounds: Any
    shape: Any
    common: Any
    analyzer: Any
    distance: Any | None


def inspection_deadline() -> float:
    return monotonic() + INSPECTION_DEADLINE_SECONDS


def inspection_pairs(
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


def _load_kernel_symbols(measure_clearance: bool) -> _KernelSymbols:
    from build123d import Shape
    from OCP.Bnd import Bnd_Box
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepCheck import BRepCheck_Analyzer

    if measure_clearance:
        from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    else:
        BRepExtrema_DistShapeShape = None
    return _KernelSymbols(
        Bnd_Box,
        BRepBndLib.AddOptimal_s,
        Shape,
        BRepAlgoAPI_Common,
        BRepCheck_Analyzer,
        BRepExtrema_DistShapeShape,
    )


def _bounding_box(shape: Any, symbols: _KernelSymbols | None = None) -> Any:
    symbols = symbols or _load_kernel_symbols(False)
    box = symbols.box()
    symbols.add_bounds(shape.wrapped, box, True, True)
    return box


def _boxes_overlap(first: Any, second: Any, symbols: _KernelSymbols | None = None) -> bool:
    symbols = symbols or _load_kernel_symbols(False)
    return not _bounding_box(first, symbols).IsOut(_bounding_box(second, symbols))


def _common_volume(first: Any, second: Any, symbols: _KernelSymbols | None = None) -> float:
    symbols = symbols or _load_kernel_symbols(False)
    operation = symbols.common(first.wrapped, second.wrapped)
    operation.Build()
    if not operation.IsDone():
        raise ValueError("exact common-volume operation did not complete")
    common = symbols.shape.cast(operation.Shape())
    if not symbols.analyzer(common.wrapped).IsValid():
        raise ValueError("exact common-volume operation returned an invalid shape")
    return sum(solid.volume for solid in common.solids())


def _minimum_clearance(first: Any, second: Any, symbols: _KernelSymbols | None = None) -> float:
    symbols = symbols or _load_kernel_symbols(True)
    operation = symbols.distance(first.wrapped, second.wrapped)
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


def _deadline_error(
    first: ResolvedOccurrence, second: ResolvedOccurrence, revision: int
) -> dict[str, Any]:
    row = _pair_error(
        first,
        second,
        revision,
        ValueError("assembly inspection deadline reached before this pair was checked"),
    )
    row["error"]["reason"] = "DEADLINE"
    return row


def _inspect_pair(
    first: ResolvedOccurrence,
    second: ResolvedOccurrence,
    revision: int,
    measure_clearance: bool,
    symbols: _KernelSymbols,
) -> dict[str, Any]:
    try:
        overlap = (
            _common_volume(first.shape, second.shape, symbols)
            if _boxes_overlap(first.shape, second.shape, symbols)
            else 0.0
        )
        overlap = overlap if overlap > INTERFERENCE_VOLUME_TOLERANCE_MM3 else 0.0
        interferes = overlap > 0.0
        clearance = (
            (0.0 if interferes else _minimum_clearance(first.shape, second.shape, symbols))
            if measure_clearance
            else None
        )
    except Exception as error:  # noqa: BLE001 - kernel errors become explicit measurement evidence.
        return _pair_error(first, second, revision, error)
    row = {
        "occurrenceIds": [first.occurrence.id, second.occurrence.id],
        "partIds": [first.occurrence.part_id, second.occurrence.part_id],
        "overlapVolume": {"value": overlap, "unit": "mm^3"},
        "interferes": interferes,
        "revision": revision,
    }
    if clearance is not None:
        row["clearance"] = {"value": clearance, "unit": "mm"}
    return row


def inspect_assembly(
    occurrences: list[ResolvedOccurrence],
    occurrence_ids: list[str] | None,
    revision: int,
    *,
    measure_clearance: bool = True,
    deadline: float | None = None,
) -> list[dict[str, Any]]:
    """Return stable exact pair evidence until the cooperative deadline."""
    pairs = inspection_pairs(occurrences, occurrence_ids)
    deadline = inspection_deadline() if deadline is None else deadline
    symbols = _load_kernel_symbols(measure_clearance)
    rows = []
    for index, (first, second) in enumerate(pairs):
        if monotonic() >= deadline:
            rows.extend(_deadline_error(left, right, revision) for left, right in pairs[index:])
            break
        rows.append(_inspect_pair(first, second, revision, measure_clearance, symbols))
    return rows
