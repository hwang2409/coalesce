from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from typing import Any


class DimensionKind(StrEnum):
    DISTANCE = "distance"
    RADIUS = "radius"
    DIAMETER = "diameter"
    ANGLE = "angle"


class ConstraintKind(StrEnum):
    COINCIDENT = "coincident"
    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"
    HORIZONTAL_POINTS = "horizontal_points"
    VERTICAL_POINTS = "vertical_points"
    PARALLEL = "parallel"
    PERPENDICULAR = "perpendicular"
    TANGENT = "tangent"
    EQUAL = "equal"
    SYMMETRIC = "symmetric"
    POINT_ON = "point_on"
    FIX = "fix"


@dataclass(frozen=True)
class Point:
    id: str
    x: float
    y: float
    construction: bool = False


@dataclass(frozen=True)
class Line:
    id: str
    start: str
    end: str
    construction: bool = False


@dataclass(frozen=True)
class Arc:
    id: str
    center: str
    start: str
    end: str
    radius: float
    start_angle: float
    end_angle: float
    construction: bool = False


@dataclass(frozen=True)
class Circle:
    id: str
    center: str
    radius: float
    construction: bool = False


@dataclass(frozen=True)
class Dimension:
    id: str
    kind: DimensionKind
    entities: tuple[str, ...]
    value: float
    driving: bool = True


@dataclass(frozen=True)
class Constraint:
    id: str
    kind: ConstraintKind
    entities: tuple[str, ...]
    driving: bool = True


@dataclass(frozen=True)
class BranchLandmark:
    id: str
    base_start: str
    base_end: str
    point: str
    expected_sign: int
    constraint_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SolveRequest:
    points: tuple[Point, ...] = ()
    lines: tuple[Line, ...] = ()
    arcs: tuple[Arc, ...] = ()
    circles: tuple[Circle, ...] = ()
    dimensions: tuple[Dimension, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    initial: Mapping[str, tuple[float, ...]] = field(default_factory=dict)
    landmarks: tuple[BranchLandmark, ...] = ()


class SolveStatus(StrEnum):
    UNDER_CONSTRAINED = "under_constrained"
    FULLY_CONSTRAINED = "fully_constrained"
    REDUNDANT = "redundant"
    CONFLICTING = "conflicting"
    DID_NOT_CONVERGE = "did_not_converge"
    BRANCH_FLIP = "branch_flip"
    INVALID_INPUT = "invalid_input"


@dataclass(frozen=True)
class SolvedPoint:
    x: float
    y: float
    construction: bool = False


@dataclass(frozen=True)
class SolvedLine:
    start: tuple[float, float]
    end: tuple[float, float]
    construction: bool = False


@dataclass(frozen=True)
class SolvedArc:
    center: tuple[float, float]
    start: tuple[float, float]
    end: tuple[float, float]
    radius: float
    start_angle: float
    end_angle: float
    construction: bool = False


@dataclass(frozen=True)
class SolvedCircle:
    center: tuple[float, float]
    radius: float
    construction: bool = False


SolvedEntity = SolvedPoint | SolvedLine | SolvedArc | SolvedCircle


@dataclass(frozen=True)
class SketchSolveError:
    code: str
    message: str
    entity_ids: tuple[str, ...] = ()
    constraint_ids: tuple[str, ...] = ()
    recoverable: bool = True
    details: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SolveResult:
    status: SolveStatus
    entities: Mapping[str, SolvedEntity] = field(default_factory=dict)
    degrees_of_freedom: int | None = None
    redundant_constraints: tuple[str, ...] = ()
    conflicting_constraints: tuple[str, ...] = ()
    landmarks: Mapping[str, float] = field(default_factory=dict)
    iterations_or_detail: Mapping[str, object] = field(default_factory=dict)
    errors: tuple[SketchSolveError, ...] = ()


def solve_sketch(request: SolveRequest) -> SolveResult:
    """Solve one sketch without exposing PlaneGCS handles or mutating the request."""
    validation_error = _validate(request)
    if validation_error:
        return _error_result(SolveStatus.INVALID_INPUT, validation_error)

    try:
        from planegcs import Sketch
        from planegcs import SolveStatus as PlaneSolveStatus
    except ImportError as exc:
        message = "install the sketch solver with: pip install 'coalesce[sketch]'"
        import sys

        if sys.platform == "darwin":
            message += "; on macOS, run `brew install eigen boost` first"
        raise ImportError(message) from exc

    try:
        sketch = Sketch()
        point_handles: dict[str, Any] = {}
        line_handles: dict[str, Any] = {}
        arc_handles: dict[str, Any] = {}
        circle_handles: dict[str, Any] = {}
        tag_to_constraint: dict[int, str] = {}

        for point in sorted(request.points, key=lambda item: item.id):
            initial = request.initial.get(point.id, (point.x, point.y))
            point_handles[point.id] = sketch.add_point(initial[0], initial[1])
        for line in sorted(request.lines, key=lambda item: item.id):
            line_handles[line.id] = sketch.add_line(
                point_handles[line.start], point_handles[line.end]
            )
        for arc in sorted(request.arcs, key=lambda item: item.id):
            initial = request.initial.get(
                arc.id, (arc.radius, arc.start_angle, arc.end_angle)
            )
            arc_handles[arc.id] = sketch.add_arc_cse(
                point_handles[arc.center],
                point_handles[arc.start],
                point_handles[arc.end],
                initial[0],
                initial[1],
                initial[2],
            )
        for circle in sorted(request.circles, key=lambda item: item.id):
            radius = request.initial.get(circle.id, (circle.radius,))[0]
            circle_handles[circle.id] = sketch.add_circle(
                point_handles[circle.center], sketch.add_param(radius)
            )

        handles = (point_handles, line_handles, arc_handles, circle_handles)
        for dimension in sorted(request.dimensions, key=lambda item: item.id):
            tags = _add_dimension(sketch, dimension, handles)
            _remember_tags(tag_to_constraint, tags, dimension.id)
        for constraint in sorted(request.constraints, key=lambda item: item.id):
            tags = _add_constraint(sketch, constraint, handles, request)
            _remember_tags(tag_to_constraint, tags, constraint.id)

        diagnosis = sketch.diagnose()
        redundant = _diagnostic_ids(
            (*diagnosis.redundant, *diagnosis.partially_redundant), tag_to_constraint
        )
        conflicting = _diagnostic_ids(diagnosis.conflicting, tag_to_constraint)
        if conflicting:
            return SolveResult(
                SolveStatus.CONFLICTING,
                degrees_of_freedom=diagnosis.dof,
                redundant_constraints=redundant,
                conflicting_constraints=conflicting,
            )
        plane_status = sketch.solve()
        if plane_status not in (PlaneSolveStatus.Success, PlaneSolveStatus.Converged):
            return _error_result(
                SolveStatus.DID_NOT_CONVERGE,
                SketchSolveError(
                    "DID_NOT_CONVERGE",
                    "the sketch solver did not converge",
                    recoverable=True,
                    details={"solver_status": plane_status.name},
                ),
                dof=diagnosis.dof,
            )

        solved = _read_entities(
            sketch,
            request,
            point_handles,
            line_handles,
            arc_handles,
            circle_handles,
        )
        landmarks = _evaluate_landmarks(request, solved)
        branch_error = _branch_error(request, landmarks)
        if branch_error:
            return _error_result(
                SolveStatus.BRANCH_FLIP, branch_error, dof=diagnosis.dof, landmarks=landmarks
            )
        status = (
            SolveStatus.REDUNDANT
            if redundant
            else SolveStatus.FULLY_CONSTRAINED
            if diagnosis.dof == 0
            else SolveStatus.UNDER_CONSTRAINED
        )
        return SolveResult(
            status,
            solved,
            diagnosis.dof,
            redundant_constraints=redundant,
            landmarks=landmarks,
        )
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        return _error_result(
            SolveStatus.INVALID_INPUT,
            SketchSolveError("INVALID_INPUT", str(exc), recoverable=True),
        )


def _validate(request: SolveRequest) -> SketchSolveError | None:
    identified = (
        *request.points,
        *request.lines,
        *request.arcs,
        *request.circles,
        *request.dimensions,
        *request.constraints,
        *request.landmarks,
    )
    ids = [item.id for item in identified]
    if any(not item_id for item_id in ids) or len(ids) != len(set(ids)):
        return SketchSolveError("INVALID_ID", "all sketch ids must be non-empty and unique")

    entity_ids = {item.id for item in (*request.points, *request.lines, *request.arcs, *request.circles)}
    point_ids = {point.id for point in request.points}
    constraint_ids = {item.id for item in (*request.dimensions, *request.constraints)}
    numbers = [value for point in request.points for value in (point.x, point.y)]
    numbers.extend(
        value
        for arc in request.arcs
        for value in (arc.radius, arc.start_angle, arc.end_angle)
    )
    numbers.extend(circle.radius for circle in request.circles)
    numbers.extend(dimension.value for dimension in request.dimensions)
    numbers.extend(value for values in request.initial.values() for value in values)
    if not all(isfinite(value) for value in numbers):
        return SketchSolveError("NON_FINITE_VALUE", "sketch values must be finite")
    if any(item.radius <= 0 for item in (*request.arcs, *request.circles)):
        return SketchSolveError("INVALID_RADIUS", "arc and circle radii must be positive")
    if any(
        dimension.kind in (DimensionKind.RADIUS, DimensionKind.DIAMETER)
        and dimension.value <= 0
        for dimension in request.dimensions
    ):
        return SketchSolveError("INVALID_DIMENSION", "radius and diameter dimensions must be positive")

    for line in request.lines:
        if line.start not in point_ids or line.end not in point_ids:
            return SketchSolveError("UNKNOWN_POINT", f"line {line.id!r} references an unknown point")
    for curve in (*request.arcs, *request.circles):
        references = (curve.center,) if isinstance(curve, Circle) else (curve.center, curve.start, curve.end)
        if not set(references) <= point_ids:
            return SketchSolveError("UNKNOWN_POINT", f"curve {curve.id!r} references an unknown point")
    for item in (*request.dimensions, *request.constraints):
        if not set(item.entities) <= entity_ids:
            return SketchSolveError(
                "UNKNOWN_ENTITY",
                f"constraint {item.id!r} references an unknown entity",
                constraint_ids=(item.id,),
            )
    for landmark in request.landmarks:
        if landmark.expected_sign not in (-1, 1) or not {
            landmark.base_start,
            landmark.base_end,
            landmark.point,
        } <= point_ids:
            return SketchSolveError("INVALID_LANDMARK", f"landmark {landmark.id!r} is invalid")
        if not set(landmark.constraint_ids) <= constraint_ids:
            return SketchSolveError(
                "UNKNOWN_CONSTRAINT", f"landmark {landmark.id!r} references an unknown constraint"
            )
    return None


def _add_dimension(sketch: Any, dimension: Dimension, handles: tuple[dict[str, Any], ...]) -> Any:
    points, lines, arcs, circles = handles
    entities = dimension.entities
    driving = dimension.driving
    if dimension.kind is DimensionKind.DISTANCE and len(entities) == 2:
        first, second = entities
        if first in points and second in points:
            return sketch.set_p2p_distance(points[first], points[second], dimension.value, driving=driving)
        if first in points and second in lines:
            return sketch.set_p2l_distance(points[first], lines[second], dimension.value, driving=driving)
        if second in points and first in lines:
            return sketch.set_p2l_distance(points[second], lines[first], dimension.value, driving=driving)
    if dimension.kind is DimensionKind.RADIUS and len(entities) == 1:
        entity = entities[0]
        if entity in arcs:
            return sketch.set_arc_radius(arcs[entity], dimension.value, driving=driving)
        if entity in circles:
            return sketch.set_circle_radius(circles[entity], dimension.value, driving=driving)
    if dimension.kind is DimensionKind.DIAMETER and len(entities) == 1:
        entity = entities[0]
        if entity in arcs:
            return sketch.set_arc_diameter(arcs[entity], dimension.value, driving=driving)
        if entity in circles:
            return sketch.set_circle_diameter(circles[entity], dimension.value, driving=driving)
    if (
        dimension.kind is DimensionKind.ANGLE
        and len(entities) == 2
        and entities[0] in lines
        and entities[1] in lines
    ):
        return sketch.set_l2l_angle(
            lines[entities[0]], lines[entities[1]], dimension.value, driving=driving
        )
    raise ValueError(f"unsupported {dimension.kind.value} dimension {dimension.id!r}")


def _add_constraint(
    sketch: Any,
    constraint: Constraint,
    handles: tuple[dict[str, Any], ...],
    request: SolveRequest,
) -> Any:
    points, lines, arcs, circles = handles
    entities = constraint.entities
    driving = constraint.driving
    unary_line = {
        ConstraintKind.HORIZONTAL: sketch.horizontal,
        ConstraintKind.VERTICAL: sketch.vertical,
    }
    binary_line = {
        ConstraintKind.PARALLEL: sketch.parallel,
        ConstraintKind.PERPENDICULAR: sketch.perpendicular,
    }
    if constraint.kind is ConstraintKind.COINCIDENT and len(entities) == 2:
        return sketch.coincident(points[entities[0]], points[entities[1]], driving=driving)
    if constraint.kind in unary_line and len(entities) == 1:
        return unary_line[constraint.kind](lines[entities[0]], driving=driving)
    if constraint.kind in binary_line and len(entities) == 2:
        return binary_line[constraint.kind](lines[entities[0]], lines[entities[1]], driving=driving)
    if constraint.kind is ConstraintKind.HORIZONTAL_POINTS and len(entities) == 2:
        return sketch.horizontal_points(points[entities[0]], points[entities[1]], driving=driving)
    if constraint.kind is ConstraintKind.VERTICAL_POINTS and len(entities) == 2:
        return sketch.vertical_points(points[entities[0]], points[entities[1]], driving=driving)
    if constraint.kind is ConstraintKind.FIX and len(entities) == 1:
        point = next(point for point in request.points if point.id == entities[0])
        initial = request.initial.get(point.id, (point.x, point.y))
        return sketch.fix_point(points[point.id], initial[0], initial[1], driving=driving)
    if constraint.kind is ConstraintKind.EQUAL and len(entities) == 2:
        return _equal(sketch, entities, lines, arcs, circles, driving)
    if constraint.kind is ConstraintKind.TANGENT and len(entities) in (2, 3):
        if len(entities) == 3:
            first, second, point = entities
            if first in lines and second in arcs and point in points:
                return sketch.set_angle_via_point(
                    lines[first], arcs[second], points[point], 0.0, driving=driving
                )
            if second in lines and first in arcs and point in points:
                return sketch.set_angle_via_point(
                    lines[second], arcs[first], points[point], 0.0, driving=driving
                )
            raise ValueError("three-entity tangent constraints require a line, arc, and endpoint")
        return _tangent(sketch, entities, lines, arcs, circles, driving)
    if constraint.kind is ConstraintKind.SYMMETRIC and len(entities) == 3:
        first, second, center = entities
        if center in points:
            return sketch.symmetric_point(points[first], points[second], points[center], driving=driving)
        if center in lines:
            return sketch.symmetric_line(points[first], points[second], lines[center], driving=driving)
    if constraint.kind is ConstraintKind.POINT_ON and len(entities) == 2:
        point, curve = entities
        if curve in lines:
            return sketch.point_on_line(points[point], lines[curve], driving=driving)
        if curve in arcs:
            return sketch.point_on_arc(points[point], arcs[curve], driving=driving)
        if curve in circles:
            return sketch.point_on_circle(points[point], circles[curve], driving=driving)
    raise ValueError(f"unsupported {constraint.kind.value} constraint {constraint.id!r}")


def _equal(
    sketch: Any,
    entities: tuple[str, str],
    lines: dict[str, Any],
    arcs: dict[str, Any],
    circles: dict[str, Any],
    driving: bool,
) -> Any:
    first, second = entities
    if first in lines and second in lines:
        return sketch.equal_length(lines[first], lines[second], driving=driving)
    if first in arcs and second in arcs:
        return sketch.equal_radius_aa(arcs[first], arcs[second], driving=driving)
    if first in circles and second in circles:
        return sketch.equal_radius_cc(circles[first], circles[second], driving=driving)
    if first in circles and second in arcs:
        return sketch.equal_radius_ca(circles[first], arcs[second], driving=driving)
    if second in circles and first in arcs:
        return sketch.equal_radius_ca(circles[second], arcs[first], driving=driving)
    raise ValueError("equal constraints require two lines or two circular curves")


def _tangent(
    sketch: Any,
    entities: tuple[str, str],
    lines: dict[str, Any],
    arcs: dict[str, Any],
    circles: dict[str, Any],
    driving: bool,
) -> Any:
    first, second = entities
    typed = [(first, lines, "line"), (second, lines, "line")]
    line_id = next((item for item, collection, _ in typed if item in collection), None)
    arc_id = next((item for item in entities if item in arcs), None)
    circle_ids = [item for item in entities if item in circles]
    if line_id and arc_id:
        return sketch.tangent_line_arc(lines[line_id], arcs[arc_id], driving=driving)
    if line_id and circle_ids:
        return sketch.tangent_line_circle(lines[line_id], circles[circle_ids[0]], driving=driving)
    if first in arcs and second in arcs:
        return sketch.tangent_arc_arc(arcs[first], arcs[second], driving=driving)
    if arc_id and circle_ids:
        return sketch.tangent_circle_arc(circles[circle_ids[0]], arcs[arc_id], driving=driving)
    if len(circle_ids) == 2:
        return sketch.tangent_circle_circle(
            circles[circle_ids[0]], circles[circle_ids[1]], driving=driving
        )
    raise ValueError("tangent constraints require supported line, arc, or circle pairs")


def _remember_tags(mapping: dict[int, str], tags: Any, constraint_id: str) -> None:
    if not isinstance(tags, tuple):
        tags = (tags,)
    for tag in tags:
        mapping[int(tag)] = constraint_id


def _diagnostic_ids(tags: Any, mapping: Mapping[int, str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(mapping[int(tag)] for tag in tags if int(tag) in mapping))


def _read_entities(
    sketch: Any,
    request: SolveRequest,
    points: Mapping[str, Any],
    lines: Mapping[str, Any],
    arcs: Mapping[str, Any],
    circles: Mapping[str, Any],
) -> dict[str, SolvedEntity]:
    solved: dict[str, SolvedEntity] = {}
    for point in sorted(request.points, key=lambda item: item.id):
        x, y = sketch.get_point(points[point.id])
        solved[point.id] = SolvedPoint(x, y, point.construction)
    for line in sorted(request.lines, key=lambda item: item.id):
        info = sketch.get_line(lines[line.id])
        solved[line.id] = SolvedLine(info.p1, info.p2, line.construction)
    for arc in sorted(request.arcs, key=lambda item: item.id):
        info = sketch.get_arc(arcs[arc.id])
        solved[arc.id] = SolvedArc(
            info.center,
            info.start_point,
            info.end_point,
            info.radius,
            info.start_angle,
            info.end_angle,
            arc.construction,
        )
    for circle in sorted(request.circles, key=lambda item: item.id):
        info = sketch.get_circle(circles[circle.id])
        solved[circle.id] = SolvedCircle(info.center, info.radius, circle.construction)
    if not all(_solved_entity_is_finite(entity) for entity in solved.values()):
        raise RuntimeError("the sketch solver returned non-finite coordinates")
    return solved


def _solved_entity_is_finite(entity: SolvedEntity) -> bool:
    if isinstance(entity, SolvedPoint):
        values = (entity.x, entity.y)
    elif isinstance(entity, SolvedLine):
        values = (*entity.start, *entity.end)
    elif isinstance(entity, SolvedArc):
        values = (
            *entity.center,
            *entity.start,
            *entity.end,
            entity.radius,
            entity.start_angle,
            entity.end_angle,
        )
    else:
        values = (*entity.center, entity.radius)
    return all(isfinite(value) for value in values)


def _evaluate_landmarks(
    request: SolveRequest, entities: Mapping[str, SolvedEntity]
) -> dict[str, float]:
    values: dict[str, float] = {}
    for landmark in sorted(request.landmarks, key=lambda item: item.id):
        a = entities[landmark.base_start]
        b = entities[landmark.base_end]
        point = entities[landmark.point]
        assert isinstance(a, SolvedPoint) and isinstance(b, SolvedPoint)
        assert isinstance(point, SolvedPoint)
        values[landmark.id] = (b.x - a.x) * (point.y - a.y) - (b.y - a.y) * (
            point.x - a.x
        )
    return values


def _branch_error(
    request: SolveRequest, values: Mapping[str, float], tolerance: float = 1e-9
) -> SketchSolveError | None:
    for landmark in sorted(request.landmarks, key=lambda item: item.id):
        value = values[landmark.id]
        if abs(value) <= tolerance:
            return SketchSolveError(
                "AMBIGUOUS_BRANCH",
                f"branch landmark {landmark.id!r} is too close to zero",
                entity_ids=(landmark.base_start, landmark.base_end, landmark.point),
                constraint_ids=landmark.constraint_ids,
            )
        if (value > 0) != (landmark.expected_sign > 0):
            return SketchSolveError(
                "BRANCH_FLIP",
                f"solving changed branch landmark {landmark.id!r}",
                entity_ids=(landmark.base_start, landmark.base_end, landmark.point),
                constraint_ids=landmark.constraint_ids,
            )
    return None


def _error_result(
    status: SolveStatus,
    error: SketchSolveError,
    *,
    dof: int | None = None,
    landmarks: Mapping[str, float] | None = None,
) -> SolveResult:
    return SolveResult(
        status,
        degrees_of_freedom=dof,
        landmarks={} if landmarks is None else landmarks,
        errors=(error,),
    )
