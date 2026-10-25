"""Persistent explicit assembly occurrences and rigid placement semantics."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .graph import GraphError
from .parts import TerminalPart

MAX_OCCURRENCES = 64
MAX_OCCURRENCE_ID_LENGTH = 128


class AssemblyError(ValueError):
    def __init__(self, code: str, message: str, **details: Any):
        self.code = code
        self.details = details
        super().__init__(message)


@dataclass(frozen=True)
class RigidTransform:
    translation: tuple[float, float, float]
    axis: tuple[float, float, float]
    angle_degrees: float


@dataclass(frozen=True)
class Occurrence:
    id: str
    part_id: str
    transform: RigidTransform


@dataclass
class Assembly:
    occurrences: dict[str, Occurrence] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedOccurrence:
    occurrence: Occurrence
    part: TerminalPart
    shape: Any


def _finite_vector(value: Any, field_name: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise AssemblyError("INVALID_PARAMETER", f"{field_name} must contain exactly three numbers")
    if not all(type(item) in (int, float) and math.isfinite(item) for item in value):
        raise AssemblyError("NON_FINITE_VALUE", f"{field_name} values must be finite")
    return tuple(float(item) for item in value)


def normalize_transform(value: Any) -> RigidTransform:
    """Validate and canonicalize one wire-format rigid transform."""
    try:
        translation = value["translation"]
        rotation = value["rotation"]
        if translation["unit"] != "mm":
            raise AssemblyError("UNIT_MISMATCH", "assembly translation unit must be mm")
        vector = _finite_vector(translation["value"], "translation")
        axis = _finite_vector(rotation["axis"], "rotation axis")
        angle_value = rotation["angle"]["value"]
        angle_unit = rotation["angle"]["unit"]
    except AssemblyError:
        raise
    except (KeyError, TypeError) as error:
        raise AssemblyError("INVALID_PARAMETER", "transform must contain translation and rotation") from error
    if type(angle_value) not in (int, float) or not math.isfinite(angle_value):
        raise AssemblyError("NON_FINITE_VALUE", "rotation angle must be finite")
    if angle_unit not in {"deg", "rad"}:
        raise AssemblyError("UNIT_MISMATCH", "assembly rotation unit must be deg or rad")
    angle = math.degrees(angle_value) if angle_unit == "rad" else float(angle_value)
    if not math.isfinite(angle):
        raise AssemblyError("NON_FINITE_VALUE", "rotation angle must be finite in degrees")
    angle %= 360.0
    if angle >= 180.0:
        angle -= 360.0
    if angle == 0.0:
        return RigidTransform(vector, (0.0, 0.0, 1.0), 0.0)
    scale = max(abs(component) for component in axis)
    if scale == 0.0:
        raise AssemblyError("INVALID_PARAMETER", "rotation axis must be non-zero")
    scaled_axis = tuple(component / scale for component in axis)
    length = math.hypot(*scaled_axis)
    return RigidTransform(vector, tuple(component / length for component in scaled_axis), angle)


def transform_to_dict(transform: RigidTransform) -> dict[str, Any]:
    return {
        "translation": {"value": list(transform.translation), "unit": "mm"},
        "rotation": {
            "axis": list(transform.axis),
            "angle": {"value": transform.angle_degrees, "unit": "deg"},
        },
    }


def assembly_to_dict(assembly: Assembly) -> dict[str, Any]:
    return {
        "occurrences": [
            {
                "id": occurrence.id,
                "partId": occurrence.part_id,
                "transform": {
                    "translation": list(occurrence.transform.translation),
                    "rotation": {
                        "axis": list(occurrence.transform.axis),
                        "angleDegrees": occurrence.transform.angle_degrees,
                    },
                },
            }
            for occurrence in sorted(assembly.occurrences.values(), key=lambda item: item.id)
        ]
    }


def _invalid_document(message: str, **details: Any) -> GraphError:
    return GraphError("INVALID_DOCUMENT", message, **details)


def _occurrence_from_dict(row: Any, index: int) -> Occurrence:
    field = f"assembly.occurrences.{index}"
    if not isinstance(row, dict) or set(row) != {"id", "partId", "transform"}:
        raise _invalid_document(f"assembly occurrence {index} has invalid fields", field=field)
    try:
        raw = row["transform"]
        rotation = raw["rotation"]
        if set(raw) != {"translation", "rotation"} or set(rotation) != {
            "axis",
            "angleDegrees",
        }:
            raise KeyError("unexpected transform field")
        transform = normalize_transform(
            {
                "translation": {"value": raw["translation"], "unit": "mm"},
                "rotation": {
                    "axis": rotation["axis"],
                    "angle": {"value": rotation["angleDegrees"], "unit": "deg"},
                },
            }
        )
    except (KeyError, TypeError, AssemblyError) as error:
        raise _invalid_document(f"assembly occurrence {index} is malformed", field=field) from error
    occurrence_id = row["id"]
    part_id = row["partId"]
    if not isinstance(occurrence_id, str) or not 1 <= len(occurrence_id) <= MAX_OCCURRENCE_ID_LENGTH:
        raise _invalid_document("assembly occurrence id is invalid", field=f"{field}.id")
    if not isinstance(part_id, str) or not part_id:
        raise _invalid_document("assembly partId is invalid", field=f"{field}.partId")
    return Occurrence(occurrence_id, part_id, transform)


def assembly_from_dict(value: Any) -> Assembly:
    """Load a persisted assembly while rejecting malformed or duplicate identities."""
    if value is None:
        return Assembly()
    if not isinstance(value, dict) or set(value) != {"occurrences"}:
        raise _invalid_document("assembly must contain only occurrences", field="assembly")
    rows = value["occurrences"]
    if not isinstance(rows, list):
        raise _invalid_document("assembly occurrences must be a list", field="assembly.occurrences")
    if len(rows) > MAX_OCCURRENCES:
        raise _invalid_document("assembly exceeds the 64 occurrence limit", field="assembly.occurrences")
    occurrences: dict[str, Occurrence] = {}
    for index, row in enumerate(rows):
        occurrence = _occurrence_from_dict(row, index)
        if occurrence.id in occurrences:
            raise _invalid_document(
                f"duplicate assembly occurrence id: {occurrence.id}",
                field=f"assembly.occurrences.{index}.id",
                occurrenceId=occurrence.id,
            )
        occurrences[occurrence.id] = occurrence
    return Assembly(occurrences)


def _reference_reason(part_id: str, by_id: dict[str, list[TerminalPart]]) -> str | None:
    if len(by_id.get(part_id, [])) == 1:
        return None
    if len(by_id.get(part_id, [])) > 1:
        return "AMBIGUOUS"
    return "SPLIT" if any(item.startswith(f"{part_id}#") for item in by_id) else "EMPTY"


def _stale_references(
    assembly: Assembly, parts: list[TerminalPart]
) -> tuple[list[str], list[str], str] | None:
    by_id: dict[str, list[TerminalPart]] = {}
    for part in parts:
        by_id.setdefault(part.part_id, []).append(part)
    stale = [
        (item, reason)
        for item in assembly.occurrences.values()
        if (reason := _reference_reason(item.part_id, by_id)) is not None
    ]
    if not stale:
        return None
    reasons = {reason for _, reason in stale}
    reason = "AMBIGUOUS" if "AMBIGUOUS" in reasons else "SPLIT" if "SPLIT" in reasons else "EMPTY"
    return (
        sorted(item.id for item, _ in stale),
        sorted({item.part_id for item, _ in stale}),
        reason,
    )


def resolve_occurrences(assembly: Assembly, parts: list[TerminalPart]) -> list[ResolvedOccurrence]:
    stale = _stale_references(assembly, parts)
    if stale is not None:
        occurrence_ids, part_ids, reason = stale
        raise AssemblyError(
            "ASSEMBLY_REFERENCE_STALE",
            f"assembly occurrences {', '.join(occurrence_ids)} reference unavailable parts",
            occurrenceIds=occurrence_ids,
            partIds=part_ids,
            reason=reason,
        )
    by_id = {part.part_id: part for part in parts}
    return [
        ResolvedOccurrence(item, by_id[item.part_id], locate_shape(by_id[item.part_id].shape, item.transform))
        for item in sorted(assembly.occurrences.values(), key=lambda row: row.id)
    ]


def locate_shape(shape: Any, transform: RigidTransform) -> Any:
    """Return a located copy without changing the shared part definition."""
    from build123d import Location
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
    from OCP.TopLoc import TopLoc_Location

    trsf = gp_Trsf()
    trsf.SetRotation(
        gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(*transform.axis)),
        math.radians(transform.angle_degrees),
    )
    trsf.SetTranslationPart(gp_Vec(*transform.translation))
    return shape.located(Location(TopLoc_Location(trsf)))
