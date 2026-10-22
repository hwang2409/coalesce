"""Persisted constrained-sketch model and solver boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .sketch_solver import (
    Arc,
    BranchLandmark,
    Circle,
    Constraint,
    Dimension,
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


def from_payload(value: dict[str, Any]) -> ConstrainedSketch:
    def items(name: str, cls: Any) -> tuple[Any, ...]:
        parsed = []
        for item in value.get(name, []):
            item = dict(item)
            if cls is Dimension:
                from .sketch_solver import DimensionKind
                item["kind"] = DimensionKind(item["kind"])
            elif cls is Constraint:
                from .sketch_solver import ConstraintKind
                item["kind"] = ConstraintKind(item["kind"])
            parsed.append(cls(**item))
        return tuple(parsed)

    model = ConstrainedSketch(
        points=items("points", Point),
        lines=items("lines", Line),
        arcs=items("arcs", Arc),
        circles=items("circles", Circle),
        dimensions=items("dimensions", Dimension),
        constraints=items("constraints", Constraint),
        initial={key: tuple(raw) for key, raw in value.get("initial", {}).items()},
        landmarks=items("landmarks", BranchLandmark),
    )
    saved = value.get("solved")
    if isinstance(saved, dict):
        entities = {}
        types = {
            "SolvedPoint": SolvedPoint,
            "SolvedLine": SolvedLine,
            "SolvedArc": SolvedArc,
            "SolvedCircle": SolvedCircle,
        }
        for key, raw in saved.get("entities", {}).items():
            raw = dict(raw)
            cls = types[raw.pop("kind")]
            for field_name in ("start", "end", "center"):
                if field_name in raw:
                    raw[field_name] = tuple(raw[field_name])
            entities[key] = cls(**raw)
        model.solved = SolveResult(
            SolveStatus(saved["status"]),
            entities,
            saved.get("dof"),
            landmarks=saved.get("landmarks", {}),
        )
    return model


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
            "errors": [error.__dict__ for error in result.errors],
        },
    }
