"""Persistent explicit assembly occurrences and rigid placement semantics."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .graph import GraphError
from .parts import TerminalPart
from .references import Reference, reference_from_dict

MAX_OCCURRENCES = 64
MAX_MATES = 128
MAX_OCCURRENCE_ID_LENGTH = 128
MAX_MATE_ID_LENGTH = 128


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


@dataclass(frozen=True)
class MateSide:
    occurrence_id: str
    reference: Reference


@dataclass(frozen=True)
class Mate:
    id: str
    kind: str
    occurrence_id: str | None = None
    first: MateSide | None = None
    second: MateSide | None = None
    orientation: str | None = None
    offset: float = 0.0


@dataclass
class Assembly:
    occurrences: dict[str, Occurrence] = field(default_factory=dict)
    mates: dict[str, Mate] = field(default_factory=dict)


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


_TRANSFORM_DECIMAL_PLACES = 12


def canonical_transform(transform: RigidTransform) -> RigidTransform:
    """Round below material tolerances so save/load and publication use one spelling."""
    rounded = lambda value: round(value, _TRANSFORM_DECIMAL_PLACES) or 0.0
    return RigidTransform(
        tuple(rounded(value) for value in transform.translation),  # type: ignore[arg-type]
        tuple(rounded(value) for value in transform.axis),  # type: ignore[arg-type]
        rounded(transform.angle_degrees),
    )


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
        raise AssemblyError(
            "INVALID_PARAMETER", "transform must contain translation and rotation"
        ) from error
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
        return canonical_transform(RigidTransform(vector, (0.0, 0.0, 1.0), 0.0))
    scale = max(abs(component) for component in axis)
    if scale == 0.0:
        raise AssemblyError("INVALID_PARAMETER", "rotation axis must be non-zero")
    scaled_axis = tuple(component / scale for component in axis)
    length = math.hypot(*scaled_axis)
    return canonical_transform(
        RigidTransform(vector, tuple(component / length for component in scaled_axis), angle)
    )


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
        ],
        "mates": [
            (
                {"id": mate.id, "kind": "ground", "occurrenceId": mate.occurrence_id}
                if mate.kind == "ground"
                else {
                    "id": mate.id,
                    "kind": mate.kind,
                    "first": {
                        "occurrenceId": mate.first.occurrence_id,
                        "reference": mate.first.reference.to_dict(),
                    },
                    "second": {
                        "occurrenceId": mate.second.occurrence_id,
                        "reference": mate.second.reference.to_dict(),
                    },
                    "orientation": mate.orientation,
                    **({"offset": mate.offset} if mate.kind == "planar" else {}),
                }
            )
            for mate in sorted(assembly.mates.values(), key=lambda item: item.id)
        ],
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
    if (
        not isinstance(occurrence_id, str)
        or not 1 <= len(occurrence_id) <= MAX_OCCURRENCE_ID_LENGTH
    ):
        raise _invalid_document("assembly occurrence id is invalid", field=f"{field}.id")
    if not isinstance(part_id, str) or not part_id:
        raise _invalid_document("assembly partId is invalid", field=f"{field}.partId")
    return Occurrence(occurrence_id, part_id, transform)


def _occurrences_from_dict(rows: Any) -> dict[str, Occurrence]:
    if not isinstance(rows, list):
        raise _invalid_document("assembly occurrences must be a list", field="assembly.occurrences")
    if len(rows) > MAX_OCCURRENCES:
        raise _invalid_document(
            "assembly exceeds the 64 occurrence limit", field="assembly.occurrences"
        )
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
    return occurrences


def _mate_side_from_dict(value: Any, field: str, occurrences: dict[str, Occurrence]) -> MateSide:
    if not isinstance(value, dict) or set(value) != {"occurrenceId", "reference"}:
        raise _invalid_document("assembly face mate side is malformed", field=field)
    occurrence_id = value["occurrenceId"]
    if occurrence_id not in occurrences:
        raise _invalid_document(
            f"assembly mate references unknown occurrence: {occurrence_id}",
            field=f"{field}.occurrenceId",
            occurrenceId=occurrence_id,
        )
    try:
        reference = reference_from_dict(value["reference"])
    except (KeyError, TypeError, ValueError) as error:
        raise _invalid_document(
            "assembly face mate reference is malformed", field=f"{field}.reference"
        ) from error
    if reference.cardinality.value != "one":
        raise _invalid_document(
            "assembly mate references must have cardinality one",
            field=f"{field}.reference.cardinality",
        )
    return MateSide(occurrence_id, reference)


def _ground_mate_from_dict(
    row: dict[str, Any], mate_id: str, field: str, occurrences: dict[str, Occurrence]
) -> Mate:
    if set(row) != {"id", "kind", "occurrenceId"}:
        raise _invalid_document("assembly ground mate has invalid fields", field=field)
    occurrence_id = row["occurrenceId"]
    if occurrence_id not in occurrences:
        raise _invalid_document(
            f"assembly mate references unknown occurrence: {occurrence_id}",
            field=f"{field}.occurrenceId",
            occurrenceId=occurrence_id,
        )
    return Mate(mate_id, "ground", occurrence_id)


def _face_mate_from_dict(
    row: dict[str, Any], mate_id: str, field: str, occurrences: dict[str, Occurrence]
) -> Mate:
    expected = {"id", "kind", "first", "second", "orientation"}
    if row.get("kind") == "planar":
        expected.add("offset")
    if set(row) != expected:
        raise _invalid_document(f"assembly {row.get('kind')} mate has invalid fields", field=field)
    if row["orientation"] not in {"aligned", "opposed"}:
        raise _invalid_document(
            "assembly face mate orientation is invalid", field=f"{field}.orientation"
        )
    offset = row.get("offset", 0.0)
    if type(offset) not in (int, float) or not math.isfinite(offset):
        raise _invalid_document("assembly planar mate offset is invalid", field=f"{field}.offset")
    first = _mate_side_from_dict(row["first"], f"{field}.first", occurrences)
    second = _mate_side_from_dict(row["second"], f"{field}.second", occurrences)
    if first.occurrence_id == second.occurrence_id:
        raise _invalid_document("assembly face mate must connect two occurrences", field=field)
    return Mate(
        mate_id,
        row["kind"],
        first=first,
        second=second,
        orientation=row["orientation"],
        offset=float(offset),
    )


def _mate_from_dict(row: Any, index: int, occurrences: dict[str, Occurrence]) -> Mate:
    field = f"assembly.mates.{index}"
    if not isinstance(row, dict):
        raise _invalid_document(f"assembly mate {index} has invalid fields", field=field)
    mate_id = row.get("id")
    if not isinstance(mate_id, str) or not 1 <= len(mate_id) <= MAX_MATE_ID_LENGTH:
        raise _invalid_document("assembly mate id is invalid", field=f"{field}.id")
    decoder = {
        "ground": _ground_mate_from_dict,
        "planar": _face_mate_from_dict,
        "concentric": _face_mate_from_dict,
    }.get(row.get("kind"))
    if decoder is None:
        raise _invalid_document("assembly mate kind is invalid", field=f"{field}.kind")
    return decoder(row, mate_id, field, occurrences)


def _mates_from_dict(rows: Any, occurrences: dict[str, Occurrence]) -> dict[str, Mate]:
    if not isinstance(rows, list):
        raise _invalid_document("assembly mates must be a list", field="assembly.mates")
    if len(rows) > MAX_MATES:
        raise _invalid_document("assembly exceeds the 128 mate limit", field="assembly.mates")
    mates: dict[str, Mate] = {}
    grounded: dict[str, str] = {}
    for index, row in enumerate(rows):
        mate = _mate_from_dict(row, index, occurrences)
        if mate.id in mates:
            raise _invalid_document(
                f"duplicate assembly mate id: {mate.id}",
                field=f"assembly.mates.{index}.id",
                mateId=mate.id,
            )
        if mate.kind == "ground" and mate.occurrence_id in grounded:
            raise _invalid_document(
                f"assembly occurrence {mate.occurrence_id} has more than one ground mate",
                field=f"assembly.mates.{index}.occurrenceId",
                mateIds=sorted([grounded[mate.occurrence_id], mate.id]),
                occurrenceId=mate.occurrence_id,
            )
        mates[mate.id] = mate
        if mate.kind == "ground":
            assert mate.occurrence_id is not None
            grounded[mate.occurrence_id] = mate.id
    return mates


def assembly_from_dict(value: Any, *, require_mates: bool = True) -> Assembly:
    """Load a persisted assembly while rejecting malformed or duplicate identities."""
    if value is None:
        return Assembly()
    expected_fields = {"occurrences", "mates"} if require_mates else {"occurrences"}
    if not isinstance(value, dict) or set(value) != expected_fields:
        description = "occurrences and mates" if require_mates else "only occurrences"
        raise _invalid_document(f"assembly must contain {description}", field="assembly")
    occurrences = _occurrences_from_dict(value["occurrences"])
    return Assembly(
        occurrences, _mates_from_dict(value["mates"], occurrences) if require_mates else {}
    )


def _reference_reason(part_id: str, part_ids: set[str]) -> str | None:
    if part_id in part_ids:
        return None
    return "SPLIT" if any(item.startswith(f"{part_id}#") for item in part_ids) else "EMPTY"


def _stale_references(
    assembly: Assembly, parts: list[TerminalPart]
) -> tuple[list[str], list[str], str] | None:
    part_ids = {part.part_id for part in parts}
    stale = [
        (item, reason)
        for item in assembly.occurrences.values()
        if (reason := _reference_reason(item.part_id, part_ids)) is not None
    ]
    if not stale:
        return None
    reasons = {reason for _, reason in stale}
    reason = "SPLIT" if "SPLIT" in reasons else "EMPTY"
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
        ResolvedOccurrence(
            item, by_id[item.part_id], locate_shape(by_id[item.part_id].shape, item.transform)
        )
        for item in sorted(assembly.occurrences.values(), key=lambda row: row.id)
    ]


def definition_shape(shape: Any) -> Any:
    """Return a copy in the local frame used by assembly placements."""
    from build123d import Location, Shape
    from OCP.TopLoc import TopLoc_Location

    local = shape.located(Location())
    return Shape.cast(local.wrapped.Located(TopLoc_Location()))


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
    return definition_shape(shape).located(Location(TopLoc_Location(trsf)))
