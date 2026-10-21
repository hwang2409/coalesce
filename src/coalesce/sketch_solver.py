from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite, tau
from numbers import Real
from typing import Any, Literal


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


EntityType = Literal["point", "line", "arc", "circle"]


OperandTransform = Callable[[tuple[Any, ...], Any, Any], tuple[Any, ...]]


def _identity_operands(operands: tuple[Any, ...], _item: Any, _request: Any) -> tuple[Any, ...]:
    return operands


def _append_zero_angle(operands: tuple[Any, ...], _item: Any, _request: Any) -> tuple[Any, ...]:
    return (*operands, 0.0)


def _reverse_operands(operands: tuple[Any, ...], _item: Any, _request: Any) -> tuple[Any, ...]:
    return tuple(reversed(operands))


def _fixed_point_operands(operands: tuple[Any, ...], item: Any, request: Any) -> tuple[Any, ...]:
    point = next(point for point in request.points if point.id == item.entities[0])
    initial = request.initial.get(point.id, (point.x, point.y))
    return (*operands, *initial)


@dataclass(frozen=True)
class OperandSignature:
    """Ordered operand contract and its PlaneGCS adapter operation."""

    operands: tuple[EntityType, ...]
    roles: tuple[str, ...]
    operation: str
    value_unit: str | None = None
    transform: OperandTransform = _identity_operands


OPERAND_SIGNATURES: Mapping[DimensionKind | ConstraintKind, tuple[OperandSignature, ...]] = {
    DimensionKind.DISTANCE: (
        OperandSignature(
            ("point", "point"), ("first point", "second point"), "set_p2p_distance", "length"
        ),
        OperandSignature(("point", "line"), ("point", "line"), "set_p2l_distance", "length"),
    ),
    DimensionKind.RADIUS: (
        OperandSignature(("arc",), ("circular curve",), "set_arc_radius", "length"),
        OperandSignature(("circle",), ("circular curve",), "set_circle_radius", "length"),
    ),
    DimensionKind.DIAMETER: (
        OperandSignature(("arc",), ("circular curve",), "set_arc_diameter", "length"),
        OperandSignature(("circle",), ("circular curve",), "set_circle_diameter", "length"),
    ),
    DimensionKind.ANGLE: (
        OperandSignature(
            ("line", "line"),
            ("first directed line", "second directed line"),
            "set_l2l_angle",
            "radians",
        ),
    ),
    ConstraintKind.COINCIDENT: (
        OperandSignature(("point", "point"), ("first point", "second point"), "coincident"),
    ),
    ConstraintKind.HORIZONTAL: (OperandSignature(("line",), ("line",), "horizontal"),),
    ConstraintKind.VERTICAL: (OperandSignature(("line",), ("line",), "vertical"),),
    ConstraintKind.HORIZONTAL_POINTS: (
        OperandSignature(("point", "point"), ("first point", "second point"), "horizontal_points"),
    ),
    ConstraintKind.VERTICAL_POINTS: (
        OperandSignature(("point", "point"), ("first point", "second point"), "vertical_points"),
    ),
    ConstraintKind.PARALLEL: (
        OperandSignature(("line", "line"), ("first line", "second line"), "parallel"),
    ),
    ConstraintKind.PERPENDICULAR: (
        OperandSignature(("line", "line"), ("first line", "second line"), "perpendicular"),
    ),
    ConstraintKind.TANGENT: (
        OperandSignature(("line", "arc"), ("line", "arc"), "tangent_line_arc"),
        OperandSignature(("line", "circle"), ("line", "circle"), "tangent_line_circle"),
        OperandSignature(("arc", "arc"), ("first arc", "second arc"), "tangent_arc_arc"),
        OperandSignature(("circle", "arc"), ("circle", "arc"), "tangent_circle_arc"),
        OperandSignature(
            ("circle", "circle"),
            ("first circle", "second circle"),
            "tangent_circle_circle",
        ),
        OperandSignature(
            ("line", "arc", "point"),
            ("line", "arc", "shared tangent endpoint"),
            "set_angle_via_point",
            transform=_append_zero_angle,
        ),
    ),
    ConstraintKind.EQUAL: (
        OperandSignature(("line", "line"), ("first line", "second line"), "equal_length"),
        OperandSignature(("arc", "arc"), ("first arc", "second arc"), "equal_radius_aa"),
        OperandSignature(
            ("circle", "circle"), ("first circle", "second circle"), "equal_radius_cc"
        ),
        OperandSignature(("circle", "arc"), ("circle", "arc"), "equal_radius_ca"),
        OperandSignature(
            ("arc", "circle"),
            ("arc", "circle"),
            "equal_radius_ca",
            transform=_reverse_operands,
        ),
    ),
    ConstraintKind.SYMMETRIC: (
        OperandSignature(
            ("point", "point", "point"),
            ("first point", "second point", "center point"),
            "symmetric_point",
        ),
        OperandSignature(
            ("point", "point", "line"),
            ("first point", "second point", "center line"),
            "symmetric_line",
        ),
    ),
    ConstraintKind.POINT_ON: (
        OperandSignature(("point", "line"), ("point", "line"), "point_on_line"),
        OperandSignature(("point", "arc"), ("point", "arc"), "point_on_arc"),
        OperandSignature(("point", "circle"), ("point", "circle"), "point_on_circle"),
    ),
    ConstraintKind.FIX: (
        OperandSignature(("point",), ("point",), "fix_point", transform=_fixed_point_operands),
    ),
}


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
    """A counterclockwise arc from start to end, with angles in radians."""

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
    """A value applied to entities in the order and unit in OPERAND_SIGNATURES."""

    id: str
    kind: DimensionKind
    entities: tuple[str, ...]
    value: float
    driving: bool = True


@dataclass(frozen=True)
class Constraint:
    """A relation over entities in the ordered roles in OPERAND_SIGNATURES."""

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
    try:
        validation_error = _validate(request)
    except (TypeError, ValueError) as exc:
        validation_error = SketchSolveError("INVALID_INPUT", str(exc), recoverable=True)
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
        line_handles[line.id] = sketch.add_line(point_handles[line.start], point_handles[line.end])
    for arc in sorted(request.arcs, key=lambda item: item.id):
        initial = request.initial.get(arc.id, (arc.radius, arc.start_angle, arc.end_angle))
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


def _validate(request: SolveRequest) -> SketchSolveError | None:
    error = _validate_structure(request)
    if error:
        return error

    error = _validate_ids(request)
    if error:
        return error

    entity_types = _entity_types(request)
    error = _validate_initial_values(request, entity_types)
    if error:
        return error

    error = _validate_geometry(request)
    if error:
        return error

    for item in (*request.dimensions, *request.constraints):
        if not _match_signature(item.kind, item.entities, entity_types):
            kind = item.kind.value if isinstance(item.kind, StrEnum) else str(item.kind)
            return SketchSolveError(
                "INVALID_OPERANDS",
                f"{kind} {item.id!r} has unsupported ordered operands {item.entities!r}",
                constraint_ids=(item.id,),
            )

    for dimension in request.dimensions:
        if not _finite((dimension.value,)):
            return SketchSolveError(
                "NON_FINITE_VALUE",
                f"dimension {dimension.id!r} must have a finite value",
                constraint_ids=(dimension.id,),
            )
        if (
            dimension.kind in (DimensionKind.RADIUS, DimensionKind.DIAMETER)
            and dimension.value <= 0
        ):
            return SketchSolveError(
                "INVALID_DIMENSION",
                f"dimension {dimension.id!r} must be positive",
                constraint_ids=(dimension.id,),
            )

    error = _validate_tangent_endpoints(request)
    if error:
        return error
    return _validate_landmarks(request)


def _validate_structure(request: SolveRequest) -> SketchSolveError | None:
    if not isinstance(request, SolveRequest):
        return SketchSolveError("INVALID_REQUEST", "request must be a SolveRequest")
    for validator in (
        _validate_containers,
        _validate_references,
        _validate_operand_fields,
        _validate_numeric_fields,
    ):
        error = validator(request)
        if error:
            return error
    return None


def _validate_containers(request: SolveRequest) -> SketchSolveError | None:
    collections = (
        ("points", request.points, Point),
        ("lines", request.lines, Line),
        ("arcs", request.arcs, Arc),
        ("circles", request.circles, Circle),
        ("dimensions", request.dimensions, Dimension),
        ("constraints", request.constraints, Constraint),
        ("landmarks", request.landmarks, BranchLandmark),
    )
    for name, items, expected_type in collections:
        if not _is_sequence(items) or not all(isinstance(item, expected_type) for item in items):
            return SketchSolveError(
                "INVALID_CONTAINER", f"{name} must be a sequence of {expected_type.__name__} values"
            )
    if not isinstance(request.initial, Mapping):
        return SketchSolveError("INVALID_INITIAL", "initial must be a mapping")
    return None


def _validate_references(request: SolveRequest) -> SketchSolveError | None:
    entities = (*request.points, *request.lines, *request.arcs, *request.circles)
    identified = (*entities, *request.dimensions, *request.constraints, *request.landmarks)
    if any(not isinstance(item.id, str) or not item.id for item in identified):
        return SketchSolveError("INVALID_ID", "all sketch ids must be non-empty strings")

    referenced = (
        *((line.id, (line.start, line.end)) for line in request.lines),
        *((arc.id, (arc.center, arc.start, arc.end)) for arc in request.arcs),
        *((circle.id, (circle.center,)) for circle in request.circles),
        *(
            (landmark.id, (landmark.base_start, landmark.base_end, landmark.point))
            for landmark in request.landmarks
        ),
    )
    for entity_id, references in referenced:
        if any(not isinstance(reference, str) or not reference for reference in references):
            return _entity_error(
                "INVALID_REFERENCE", entity_id, "references must be non-empty strings"
            )
    return None


def _validate_operand_fields(request: SolveRequest) -> SketchSolveError | None:
    for item in (*request.dimensions, *request.constraints):
        expected_kind = DimensionKind if isinstance(item, Dimension) else ConstraintKind
        if not isinstance(item.kind, expected_kind):
            return SketchSolveError(
                "INVALID_KIND",
                f"constraint {item.id!r} has an invalid kind",
                constraint_ids=(item.id,),
            )
        if not _is_sequence(item.entities) or not all(
            isinstance(entity_id, str) and entity_id for entity_id in item.entities
        ):
            return SketchSolveError(
                "INVALID_OPERANDS",
                f"constraint {item.id!r} operands must be a sequence of non-empty strings",
                constraint_ids=(item.id,),
            )

    for landmark in request.landmarks:
        if not _is_sequence(landmark.constraint_ids) or not all(
            isinstance(constraint_id, str) and constraint_id
            for constraint_id in landmark.constraint_ids
        ):
            return _entity_error(
                "INVALID_LANDMARK", landmark.id, "constraint ids must be non-empty strings"
            )
    return None


def _validate_numeric_fields(request: SolveRequest) -> SketchSolveError | None:
    entity_values = (
        *((point.id, (point.x, point.y)) for point in request.points),
        *((arc.id, (arc.radius, arc.start_angle, arc.end_angle)) for arc in request.arcs),
        *((circle.id, (circle.radius,)) for circle in request.circles),
    )
    for entity_id, values in entity_values:
        if not _finite(values):
            return _entity_error(
                "NON_FINITE_VALUE", entity_id, "numbers must be finite real numbers"
            )
    for dimension in request.dimensions:
        if not _finite((dimension.value,)):
            return SketchSolveError(
                "NON_FINITE_VALUE",
                f"dimension {dimension.id!r} must have a finite real value",
                constraint_ids=(dimension.id,),
            )

    for entity_id, values in request.initial.items():
        if not isinstance(entity_id, str) or not entity_id:
            return SketchSolveError("INVALID_INITIAL", "initial ids must be non-empty strings")
        if not _is_sequence(values) or not _finite(values):
            return SketchSolveError(
                "INVALID_INITIAL",
                f"initial value {entity_id!r} must be a sequence of finite real numbers",
                entity_ids=(entity_id,),
            )
    return None


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes)


def _validate_ids(request: SolveRequest) -> SketchSolveError | None:
    entities = (*request.points, *request.lines, *request.arcs, *request.circles)
    constraints = (*request.dimensions, *request.constraints)
    identified = (*entities, *constraints, *request.landmarks)
    for item in identified:
        if not item.id:
            return SketchSolveError(
                "INVALID_ID", "all sketch ids must be non-empty", entity_ids=(item.id,)
            )
    ids = [item.id for item in identified]
    duplicate = next((item_id for item_id in ids if ids.count(item_id) > 1), None)
    if duplicate is None:
        return None
    return SketchSolveError(
        "INVALID_ID",
        f"sketch id {duplicate!r} is duplicated",
        entity_ids=(duplicate,) if any(item.id == duplicate for item in entities) else (),
        constraint_ids=(duplicate,) if any(item.id == duplicate for item in constraints) else (),
    )


def _entity_types(request: SolveRequest) -> dict[str, EntityType]:
    return {
        **{item.id: "point" for item in request.points},
        **{item.id: "line" for item in request.lines},
        **{item.id: "arc" for item in request.arcs},
        **{item.id: "circle" for item in request.circles},
    }


def _validate_initial_values(
    request: SolveRequest, entity_types: Mapping[str, EntityType]
) -> SketchSolveError | None:
    arities = {"point": 2, "arc": 3, "circle": 1}
    for entity_id, values in request.initial.items():
        entity_type = entity_types.get(entity_id)
        if entity_type not in arities:
            return SketchSolveError(
                "INVALID_INITIAL",
                f"initial value {entity_id!r} does not name a point, arc, or circle",
                entity_ids=(entity_id,),
            )
        if len(values) != arities[entity_type] or not _finite(values):
            return SketchSolveError(
                "INVALID_INITIAL",
                f"initial value {entity_id!r} must contain {arities[entity_type]} finite numbers",
                entity_ids=(entity_id,),
            )
    return None


def _validate_geometry(request: SolveRequest) -> SketchSolveError | None:
    points = {point.id: point for point in request.points}
    validators = (
        (_validate_point, request.points),
        (_validate_line, request.lines),
        (_validate_arc, request.arcs),
        (_validate_circle, request.circles),
    )
    for validator, entities in validators:
        for entity in entities:
            error = validator(entity, points, request.initial)
            if error:
                return error
    return None


def _validate_point(
    point: Point, _points: Mapping[str, Point], _initial: Mapping[str, Sequence[Real]]
) -> SketchSolveError | None:
    if not _finite((point.x, point.y)):
        return _entity_error("NON_FINITE_VALUE", point.id, "point coordinates must be finite")
    return None


def _validate_line(
    line: Line, points: Mapping[str, Point], initial: Mapping[str, Sequence[Real]]
) -> SketchSolveError | None:
    if line.start not in points or line.end not in points:
        return _entity_error("UNKNOWN_POINT", line.id, "line references an unknown point")
    start = initial.get(line.start, (points[line.start].x, points[line.start].y))
    end = initial.get(line.end, (points[line.end].x, points[line.end].y))
    if line.start == line.end or start == end:
        return _entity_error("DEGENERATE_LINE", line.id, "line endpoints must be distinct")
    return None


def _validate_arc(
    arc: Arc, points: Mapping[str, Point], initial: Mapping[str, Sequence[Real]]
) -> SketchSolveError | None:
    if not {arc.center, arc.start, arc.end} <= points.keys():
        return _entity_error("UNKNOWN_POINT", arc.id, "arc references an unknown point")
    values = initial.get(arc.id, (arc.radius, arc.start_angle, arc.end_angle))
    if not _valid_arc_values(values):
        return _entity_error(
            "INVALID_ARC",
            arc.id,
            "arc radius must be positive and its CCW sweep must be between 0 and 2π",
        )
    return None


def _validate_circle(
    circle: Circle, points: Mapping[str, Point], initial: Mapping[str, Sequence[Real]]
) -> SketchSolveError | None:
    if circle.center not in points:
        return _entity_error("UNKNOWN_POINT", circle.id, "circle references an unknown point")
    radius = initial.get(circle.id, (circle.radius,))[0]
    if radius <= 0:
        return _entity_error("INVALID_RADIUS", circle.id, "circle radius must be positive")
    return None


def _finite(values: Sequence[object]) -> bool:
    return all(_finite_number(value) for value in values)


def _finite_number(value: object) -> bool:
    if not isinstance(value, Real) or isinstance(value, bool):
        return False
    try:
        return isfinite(float(value))
    except OverflowError:
        return False


def _valid_arc_values(values: tuple[float, ...]) -> bool:
    radius, start_angle, end_angle = values
    sweep = end_angle - start_angle
    return _finite(values) and radius > 0 and 0 < sweep < tau


def _entity_error(code: str, entity_id: str, reason: str) -> SketchSolveError:
    return SketchSolveError(code, f"entity {entity_id!r}: {reason}", entity_ids=(entity_id,))


def _match_signature(
    kind: DimensionKind | ConstraintKind,
    entity_ids: tuple[str, ...],
    entity_types: Mapping[str, EntityType],
) -> OperandSignature | None:
    operand_types = tuple(entity_types.get(entity_id) for entity_id in entity_ids)
    return next(
        (
            signature
            for signature in OPERAND_SIGNATURES.get(kind, ())
            if signature.operands == operand_types
        ),
        None,
    )


def _validate_tangent_endpoints(request: SolveRequest) -> SketchSolveError | None:
    lines = {line.id: line for line in request.lines}
    arcs = {arc.id: arc for arc in request.arcs}
    for constraint in request.constraints:
        if constraint.kind is not ConstraintKind.TANGENT or len(constraint.entities) != 3:
            continue
        line_id, arc_id, point_id = constraint.entities
        line = lines.get(line_id)
        arc = arcs.get(arc_id)
        if line and arc and point_id in (line.start, line.end) and point_id in (arc.start, arc.end):
            continue
        return SketchSolveError(
            "INVALID_TANGENT_ENDPOINT",
            f"tangent {constraint.id!r} must name the shared line and arc endpoint",
            constraint_ids=(constraint.id,),
        )
    return None


def _validate_landmarks(request: SolveRequest) -> SketchSolveError | None:
    point_ids = {point.id for point in request.points}
    constraint_ids = {item.id for item in (*request.dimensions, *request.constraints)}
    for landmark in request.landmarks:
        if (
            landmark.expected_sign not in (-1, 1)
            or not {
                landmark.base_start,
                landmark.base_end,
                landmark.point,
            }
            <= point_ids
        ):
            return SketchSolveError(
                "INVALID_LANDMARK",
                f"landmark {landmark.id!r} is invalid",
                entity_ids=(landmark.id,),
            )
        if not set(landmark.constraint_ids) <= constraint_ids:
            return SketchSolveError(
                "UNKNOWN_CONSTRAINT",
                f"landmark {landmark.id!r} references an unknown constraint",
                entity_ids=(landmark.id,),
            )
    return None


def _add_dimension(sketch: Any, dimension: Dimension, handles: tuple[dict[str, Any], ...]) -> Any:
    signature, operands = _dispatch_operands(dimension, handles)
    operands = signature.transform(operands, dimension, None)
    return getattr(sketch, signature.operation)(
        *operands, dimension.value, driving=dimension.driving
    )


def _add_constraint(
    sketch: Any,
    constraint: Constraint,
    handles: tuple[dict[str, Any], ...],
    request: SolveRequest,
) -> Any:
    signature, operands = _dispatch_operands(constraint, handles)
    operands = signature.transform(operands, constraint, request)
    return getattr(sketch, signature.operation)(*operands, driving=constraint.driving)


def _dispatch_operands(
    item: Dimension | Constraint, handles: tuple[dict[str, Any], ...]
) -> tuple[OperandSignature, tuple[Any, ...]]:
    entity_types = {
        entity_id: entity_type
        for collection, entity_type in zip(handles, ("point", "line", "arc", "circle"), strict=True)
        for entity_id in collection
    }
    signature = _match_signature(item.kind, item.entities, entity_types)
    assert signature is not None
    typed_handles = dict(zip(("point", "line", "arc", "circle"), handles, strict=True))
    operands = tuple(
        typed_handles[entity_type][entity_id]
        for entity_type, entity_id in zip(signature.operands, item.entities, strict=True)
    )
    return signature, operands


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
        values[landmark.id] = (b.x - a.x) * (point.y - a.y) - (b.y - a.y) * (point.x - a.x)
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
