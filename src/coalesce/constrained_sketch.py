"""Constrained-sketch payload, persistence, and bounded solver interface."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from math import isfinite
from typing import Any

from .sketch_solver import (
    Arc,
    BranchLandmark,
    Circle,
    Constraint,
    ConstraintKind,
    Dimension,
    DimensionKind,
    Line,
    Point,
    SolvedArc,
    SolvedCircle,
    SolvedLine,
    SolvedPoint,
    SolveRequest,
    SolveResult,
    SolveStatus,
    solve_sketch,
)

_ID = {"type": "string", "minLength": 1}
_FINITE_NUMBER = {"type": "number"}
_ENTITY_IDS = {"type": "array", "items": _ID, "minItems": 1}


def _object(properties: dict[str, Any], required: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


POINT_SCHEMA = _object(
    {"id": _ID, "x": _FINITE_NUMBER, "y": _FINITE_NUMBER, "construction": {"type": "boolean"}},
    ("id", "x", "y"),
)
LINE_SCHEMA = _object(
    {"id": _ID, "start": _ID, "end": _ID, "construction": {"type": "boolean"}},
    ("id", "start", "end"),
)
ARC_SCHEMA = _object(
    {
        "id": _ID,
        "center": _ID,
        "start": _ID,
        "end": _ID,
        "radius": _FINITE_NUMBER,
        "start_angle": _FINITE_NUMBER,
        "end_angle": _FINITE_NUMBER,
        "construction": {"type": "boolean"},
    },
    ("id", "center", "start", "end", "radius", "start_angle", "end_angle"),
)
CIRCLE_SCHEMA = _object(
    {"id": _ID, "center": _ID, "radius": _FINITE_NUMBER, "construction": {"type": "boolean"}},
    ("id", "center", "radius"),
)
DIMENSION_SCHEMA = _object(
    {
        "id": _ID,
        "kind": {"enum": [kind.value for kind in DimensionKind]},
        "entities": _ENTITY_IDS,
        "value": _FINITE_NUMBER,
        "driving": {"type": "boolean"},
    },
    ("id", "kind", "entities", "value"),
)
CONSTRAINT_SCHEMA = _object(
    {
        "id": _ID,
        "kind": {"enum": [kind.value for kind in ConstraintKind]},
        "entities": _ENTITY_IDS,
        "driving": {"type": "boolean"},
    },
    ("id", "kind", "entities"),
)
LANDMARK_SCHEMA = _object(
    {
        "id": _ID,
        "base_start": _ID,
        "base_end": _ID,
        "point": _ID,
        "expected_sign": {"enum": [-1, 1]},
        "constraint_ids": {"type": "array", "items": _ID},
    },
    ("id", "base_start", "base_end", "point", "expected_sign"),
)
CONSTRAINED_SKETCH_SCHEMA = _object(
    {
        "kind": {"const": "constrained"},
        "points": {"type": "array", "items": POINT_SCHEMA},
        "lines": {"type": "array", "items": LINE_SCHEMA},
        "arcs": {"type": "array", "items": ARC_SCHEMA},
        "circles": {"type": "array", "items": CIRCLE_SCHEMA},
        "dimensions": {"type": "array", "items": DIMENSION_SCHEMA},
        "constraints": {"type": "array", "items": CONSTRAINT_SCHEMA},
        "initial": {
            "type": "object",
            "additionalProperties": {
                "type": "array",
                "items": _FINITE_NUMBER,
                "minItems": 1,
                "maxItems": 3,
            },
        },
        "landmarks": {"type": "array", "items": LANDMARK_SCHEMA},
    },
    ("kind",),
)


class ConstrainedSketchPayloadError(ValueError):
    def __init__(self, field: str, message: str):
        self.field = field
        super().__init__(f"{field} {message}")


@dataclass
class ConstrainedSketch:
    points: tuple[Point, ...] = ()
    lines: tuple[Line, ...] = ()
    arcs: tuple[Arc, ...] = ()
    circles: tuple[Circle, ...] = ()
    dimensions: tuple[Dimension, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    initial: dict[str, tuple[float, ...]] = field(default_factory=dict)
    landmarks: tuple[BranchLandmark, ...] = ()
    solved: SolveResult | None = None

    def request(self) -> SolveRequest:
        return SolveRequest(
            self.points,
            self.lines,
            self.arcs,
            self.circles,
            self.dimensions,
            self.constraints,
            self.initial,
            self.landmarks,
        )

    def solve(self) -> SolveResult:
        result = solve_sketch(self.request())
        self.solved = result
        return result


_PUBLIC_SOLVE_ERROR_CODES = {
    "SOLVE_TIMEOUT": "SKETCH_SOLVE_TIMEOUT",
    "SOLVER_FAILED": "SKETCH_SOLVER_FAILED",
}
_PUBLIC_SOLVE_STATUS_CODES = {
    SolveStatus.INVALID_INPUT: "INVALID_PARAMETER",
    SolveStatus.REDUNDANT: "SKETCH_REDUNDANT",
    SolveStatus.CONFLICTING: "SKETCH_CONFLICTING",
    SolveStatus.DID_NOT_CONVERGE: "SKETCH_DID_NOT_CONVERGE",
    SolveStatus.BRANCH_FLIP: "SKETCH_BRANCH_FLIP",
}

_ITEM_TYPES = {
    "points": (Point, None),
    "lines": (Line, None),
    "arcs": (Arc, None),
    "circles": (Circle, None),
    "dimensions": (Dimension, DimensionKind),
    "constraints": (Constraint, ConstraintKind),
    "landmarks": (BranchLandmark, None),
}
_REQUIRED_FIELDS = {
    "points": ("id", "x", "y"),
    "lines": ("id", "start", "end"),
    "arcs": ("id", "center", "start", "end", "radius", "start_angle", "end_angle"),
    "circles": ("id", "center", "radius"),
    "dimensions": ("id", "kind", "entities", "value"),
    "constraints": ("id", "kind", "entities"),
    "landmarks": ("id", "base_start", "base_end", "point", "expected_sign"),
}
_NUMERIC_FIELDS = {
    "points": ("x", "y"),
    "arcs": ("radius", "start_angle", "end_angle"),
    "circles": ("radius",),
    "dimensions": ("value",),
}
_REFERENCE_FIELDS = {
    "lines": ("start", "end"),
    "arcs": ("center", "start", "end"),
    "circles": ("center",),
    "landmarks": ("base_start", "base_end", "point"),
}


def _payload_error(field: str, message: str) -> None:
    raise ConstrainedSketchPayloadError(field, message)


def _required_item_fields(item: dict[str, Any], name: str, path: str) -> None:
    for required in _REQUIRED_FIELDS[name]:
        if required not in item:
            _payload_error(f"{path}.{required}", "is required")


def _item_numbers(item: dict[str, Any], name: str, path: str) -> None:
    for field_name in _NUMERIC_FIELDS.get(name, ()):
        number = item[field_name]
        if not isinstance(number, (int, float)) or not isfinite(number):
            _payload_error(f"{path}.{field_name}", "must be a finite number")


def _item_sequences(item: dict[str, Any], path: str) -> None:
    for field_name in ("entities", "constraint_ids"):
        if field_name not in item:
            continue
        if not isinstance(item[field_name], list) or not all(
            isinstance(entry, str) and entry for entry in item[field_name]
        ):
            _payload_error(f"{path}.{field_name}", "must contain non-empty ids")
        item[field_name] = tuple(item[field_name])


def _parse_item(raw: Any, name: str, index: int) -> Any:
    path = f"constrained.{name}[{index}]"
    if not isinstance(raw, dict):
        _payload_error(path, "must be an object")
    item = dict(raw)
    _required_item_fields(item, name, path)
    if not isinstance(item["id"], str) or not item["id"]:
        _payload_error(f"{path}.id", "must be a non-empty string")
    cls, enum = _ITEM_TYPES[name]
    if enum is not None:
        try:
            item["kind"] = enum(item["kind"])
        except (TypeError, ValueError):
            _payload_error(f"{path}.kind", "has an unknown value")
    _item_numbers(item, name, path)
    _item_sequences(item, path)
    try:
        return cls(**item)
    except TypeError as error:
        _payload_error(path, f"has invalid fields: {error}")


def _parse_items(value: dict[str, Any], name: str) -> tuple[Any, ...]:
    raw_items = value.get(name, [])
    if not isinstance(raw_items, list):
        _payload_error(f"constrained.{name}", "must be an array")
    return tuple(_parse_item(raw, name, index) for index, raw in enumerate(raw_items))


def _validate_point_references(model: ConstrainedSketch, point_ids: set[str]) -> None:
    for name, fields in _REFERENCE_FIELDS.items():
        for index, item in enumerate(getattr(model, name)):
            for field_name in fields:
                if getattr(item, field_name) not in point_ids:
                    _payload_error(
                        f"constrained.{name}[{index}].{field_name}", "references an unknown point id"
                    )


def _validate_operand_references(model: ConstrainedSketch, entity_ids: set[str]) -> None:
    for name in ("dimensions", "constraints"):
        for index, item in enumerate(getattr(model, name)):
            if any(entity_id not in entity_ids for entity_id in item.entities):
                _payload_error(
                    f"constrained.{name}[{index}].entities", "references an unknown entity id"
                )


def _validate_landmark_references(model: ConstrainedSketch, constraint_ids: set[str]) -> None:
    for index, landmark in enumerate(model.landmarks):
        if any(item not in constraint_ids for item in landmark.constraint_ids):
            _payload_error(
                f"constrained.landmarks[{index}].constraint_ids",
                "references an unknown constraint id",
            )


def _validate_references(model: ConstrainedSketch) -> None:
    point_ids = {point.id for point in model.points}
    entity_ids = point_ids | {item.id for item in (*model.lines, *model.arcs, *model.circles)}
    constraint_ids = {item.id for item in (*model.dimensions, *model.constraints)}
    _validate_point_references(model, point_ids)
    _validate_operand_references(model, entity_ids)
    _validate_landmark_references(model, constraint_ids)


def from_payload(value: dict[str, Any]) -> ConstrainedSketch:
    if not isinstance(value, dict) or value.get("kind") != "constrained":
        _payload_error("constrained.kind", "must equal constrained")
    items = {name: _parse_items(value, name) for name in _ITEM_TYPES}
    raw_initial = value.get("initial", {})
    if not isinstance(raw_initial, dict):
        _payload_error("constrained.initial", "must be an object")
    initial = {}
    for entity_id, raw in raw_initial.items():
        field_path = f"constrained.initial.{entity_id}"
        if not isinstance(raw, list) or not raw or not all(
            isinstance(number, (int, float)) and isfinite(number) for number in raw
        ):
            _payload_error(field_path, "must contain finite numbers")
        initial[entity_id] = tuple(raw)
    model = ConstrainedSketch(initial=initial, **items)
    _validate_references(model)
    saved = value.get("solved")
    if isinstance(saved, dict):
        model.solved = _result_from_payload(saved)
    return model


def _result_from_payload(saved: dict[str, Any]) -> SolveResult:
    entities = {}
    types = {
        "SolvedPoint": SolvedPoint,
        "SolvedLine": SolvedLine,
        "SolvedArc": SolvedArc,
        "SolvedCircle": SolvedCircle,
    }
    for key, value in saved.get("entities", {}).items():
        raw = dict(value)
        cls = types[raw.pop("kind")]
        for field_name in ("start", "end", "center"):
            if field_name in raw:
                raw[field_name] = tuple(raw[field_name])
        entities[key] = cls(**raw)
    return SolveResult(
        SolveStatus(saved["status"]),
        entities,
        saved.get("dof"),
        landmarks=saved.get("landmarks", {}),
    )


def to_payload(model: ConstrainedSketch) -> dict[str, Any]:
    payload = {
        "kind": "constrained",
        **{
            name: [
                {
                    key: value.value if isinstance(value, StrEnum) else value
                    for key, value in asdict(item).items()
                }
                for item in getattr(model, name)
            ]
            for name in _ITEM_TYPES
        },
        "initial": model.initial,
    }
    if model.solved is not None:
        payload["solved"] = result_payload(model)
    return payload


def result_payload(model: ConstrainedSketch) -> dict[str, Any]:
    result = model.solved
    if result is None:
        return {}
    return {
        "status": result.status.value,
        "dof": result.degrees_of_freedom,
        "entities": {
            key: {"kind": type(entity).__name__, **entity.__dict__}
            for key, entity in result.entities.items()
        },
        "landmarks": dict(result.landmarks),
        "diagnostics": {
            "iterationsOrDetail": dict(result.iterations_or_detail),
            "redundantConstraints": list(result.redundant_constraints),
            "conflictingConstraints": list(result.conflicting_constraints),
            "errors": [
                dict(
                    error.__dict__,
                    code=_PUBLIC_SOLVE_ERROR_CODES.get(
                        error.code, _PUBLIC_SOLVE_STATUS_CODES[result.status]
                    ),
                )
                for error in result.errors
            ],
        },
    }
