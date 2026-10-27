"""Revisioned document operations shared by MCP transports."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import queue
import re
import threading
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, fields, replace
from enum import Enum
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from .assembly import (
    MAX_MATES,
    MAX_OCCURRENCES,
    Assembly,
    AssemblyError,
    Mate,
    MateSide,
    Occurrence,
    RigidTransform,
    normalize_transform,
    resolve_occurrences,
    transform_to_dict,
)
from .assembly_inspection import (
    MAX_INSPECTION_PAIRS,
    AssemblyInspectionError,
    inspect_assembly,
    inspection_deadline,
    inspection_pairs,
)
from .availability import SketchFailure, SolveAvailability, UnavailableFeature
from .board_import import BoardImportError, mounting_holes, read_kicad_board
from .constrained_sketch import ConstrainedSketch, ConstrainedSketchPayloadError, result_payload
from .constrained_sketch import from_payload as constrained_from_payload
from .document_store import MAX_RECEIPTS, MAX_REQUEST_HASHES, Document, DocumentStore
from .errors import ERROR_REGISTRY
from .export import (
    ABSOLUTE_TOLERANCE,
    RELATIVE_TOLERANCE,
    ExportFailure,
    _validate_part_for_step,
    export_assembly,
    export_parts,
    export_parts_separately,
    parts_are_ready,
    prepare_parts,
    public_parts,
    write_assembly_step_bytes,
)
from .fit_summary import fit_summary, part_facts
from .graph import (
    MAX_HOLE_COUNT,
    MAX_OUTLINE_POINTS,
    MIN_OUTLINE_POINTS,
    BoardImport,
    Chamfer,
    Circle,
    CircularHolePattern,
    Cut,
    Extrude,
    Feature,
    Fillet,
    GraphError,
    Hole,
    HolePosition,
    LinearHolePattern,
    PcbBase,
    PcbLid,
    Plane,
    Polyline,
    PortCutout,
    Rectangle,
    Revolve,
    ScrewClosure,
    ScrewPoint,
    Sketch,
    Spline,
    Standoffs,
    validate_sketch,
    validate_sketch_profiles,
)
from .parts import terminal_parts
from .references import (
    BodySource,
    Cardinality,
    HoleSurfaceSource,
    Reference,
    edge_role_catalog,
    face_role_catalog,
    reference_from_dict,
)
from .refresh_delta import bounded_summary, format_summary, refresh_delta
from .requirements import InvalidHoleIds, reduce_gate
from .requirements import evaluate as evaluate_requirements
from .schemas import FEATURE_KIND_SCHEMAS, SUPPORTED_REQUIREMENT_KINDS
from .serialization import dumps, loads
from .sketch_solver import (
    DEFAULT_SOLVE_TIMEOUT_SECONDS,
    Dimension,
    SketchSolverSession,
    diagnostic_geometry_details,
    solve_sketch,
)

_CONSTRAINED_SKETCH_SOLVE = ConstrainedSketch.solve

if TYPE_CHECKING:
    from .assembly_solver import AssemblySolveResult
    from .regen import RegenResult
    from .viewer import ScenePart, ViewerSceneStore

_LOG = logging.getLogger(__name__)
_ALLOWED_FEATURE_KINDS = list(FEATURE_KIND_SCHEMAS)


class _KernelLoaderInterface(Protocol):
    def start(self) -> None: ...

    def wait(self) -> None: ...


@dataclass(frozen=True)
class _KernelLoadResult:
    error: BaseException | None = None


class _KernelLoader:
    """Load the process-wide CAD kernel once and retain its result."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._result: _KernelLoadResult | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._load, name="coalesce-kernel-loader", daemon=True
            )
            self._thread.start()

    def wait(self) -> None:
        self.start()
        self._ready.wait()
        assert self._result is not None
        if self._result.error is not None:
            raise self._result.error

    def _load(self) -> None:
        global regen_module, RegenResult, BRepCheck_Analyzer, Compound
        try:
            from build123d import Compound as _Compound
            from OCP.BRepCheck import BRepCheck_Analyzer as _BRepCheck_Analyzer

            from . import regen as _regen_module
            from .regen import RegenResult as _RegenResult

            Compound = _Compound
            BRepCheck_Analyzer = _BRepCheck_Analyzer
            RegenResult = _RegenResult
            regen_module = _regen_module
            result = _KernelLoadResult()
        except Exception as error:  # noqa: BLE001 - preserve the original startup failure
            result = _KernelLoadResult(error=error)
        with self._lock:
            self._result = result
            self._ready.set()


_KERNEL_LOADER = _KernelLoader()


def regenerate(graph: Any, solver: Callable[[Any], Any] = solve_sketch) -> RegenResult:
    availability = _solve_constrained_sketches(graph, solver)
    if not availability.geometry_available:
        return regen_module.RegenResult(graph, {}, {}, [], availability=availability)
    result = regen_module.accept_regeneration(graph)
    result.availability = availability
    return result


_REGENERATE = regenerate


class RequestError(ValueError):
    def __init__(self, code: str, message: str, **details: Any):
        self.code = code
        self.details = details
        super().__init__(message)


def _finite_number(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise RequestError(
            "INVALID_PARAMETER", f"{field} must be a finite number", field=field
        ) from None
    if not math.isfinite(number):
        raise RequestError("INVALID_PARAMETER", f"{field} must be finite", field=field)
    return number


def _quantity(value: Any, unit: str, field: str) -> float:
    if not isinstance(value, dict) or set(value) != {"value", "unit"}:
        raise RequestError("INVALID_PARAMETER", "quantity must contain value and unit", field=field)
    if value["unit"] != unit:
        raise RequestError(
            "UNIT_MISMATCH",
            f"expected {unit}; received {value['unit']}",
            expected=unit,
            actual=value["unit"],
        )
    if not isinstance(value["value"], (int, float)):
        raise RequestError("INVALID_PARAMETER", "quantity value must be a number", field=field)
    return _finite_number(value["value"], field)


def _positive_quantity(value: Any, field: str) -> float:
    number = _quantity(value, "mm", field)
    if number <= 0:
        raise RequestError("INVALID_PARAMETER", f"{field} must be positive", field=field)
    return number


def _angle(value: Any, field: str = "rotation") -> float:
    if not isinstance(value, dict) or set(value) != {"value", "unit"}:
        raise RequestError("INVALID_PARAMETER", "angle must contain value and unit", field=field)
    if value["unit"] not in {"deg", "rad"}:
        raise RequestError(
            "UNIT_MISMATCH",
            f"expected deg or rad; received {value['unit']}",
            expected=["deg", "rad"],
            actual=value["unit"],
        )
    if not isinstance(value["value"], (int, float)):
        raise RequestError("INVALID_PARAMETER", "angle value must be a number", field=field)
    angle = _finite_number(value["value"], field)
    converted = math.degrees(angle) if value["unit"] == "rad" else angle
    return _finite_number(converted, field)


class Action(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    REFRESH = "refresh"
    DELETE = "delete"


class _Operation:
    action: Action
    expectedRevision: float
    requestId: str

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {field.name for field in fields(cls)}


@dataclass(frozen=True)
class BoardImportOperation(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    assetId: str
    format: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class ImportDelete(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    assetId: str
    cascade: bool = False


@dataclass(frozen=True)
class SketchDelete(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    sketchId: str
    cascade: bool = False


@dataclass(frozen=True)
class FeatureDelete(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    cascade: bool = False


@dataclass(frozen=True)
class AssemblyOccurrenceCreate(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    occurrenceId: str
    part_id: str
    transform: RigidTransform
    kind: ClassVar[str] = "assembly_occurrence"

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {"action", "expectedRevision", "requestId", "occurrenceId", "kind", "parameters"}


@dataclass(frozen=True)
class AssemblyOccurrenceUpdate(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    occurrenceId: str
    transform: RigidTransform
    kind: ClassVar[str] = "assembly_occurrence"

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {"action", "expectedRevision", "requestId", "occurrenceId", "kind", "set"}


@dataclass(frozen=True)
class AssemblyOccurrenceDelete(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    occurrenceId: str
    kind: ClassVar[str] = "assembly_occurrence"

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {"action", "expectedRevision", "requestId", "occurrenceId", "kind"}


@dataclass(frozen=True)
class AssemblyMateCreate(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    mateId: str
    occurrence_id: str | None = None
    first: MateSide | None = None
    second: MateSide | None = None
    orientation: str | None = None
    offset: float = 0.0
    mate_kind: str | None = None
    kind: ClassVar[str] = "assembly_mate"

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {"action", "expectedRevision", "requestId", "mateId", "kind", "parameters"}


@dataclass(frozen=True)
class AssemblyMateUpdate(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    mateId: str
    occurrence_id: str | None = None
    first: MateSide | None = None
    second: MateSide | None = None
    orientation: str | None = None
    offset: float = 0.0
    mate_kind: str | None = None
    kind: ClassVar[str] = "assembly_mate"

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {"action", "expectedRevision", "requestId", "mateId", "kind", "set"}


@dataclass(frozen=True)
class AssemblyMateDelete(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    mateId: str
    kind: ClassVar[str] = "assembly_mate"

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {"action", "expectedRevision", "requestId", "mateId", "kind"}


@dataclass(frozen=True)
class SketchPlane:
    kind: str
    name: str
    offset: float


@dataclass(frozen=True)
class SketchChanges:
    position: tuple[float, float, float] | None
    entities: tuple[Rectangle | Circle | Polyline | Spline, ...] | None
    constrained: ConstrainedSketch | None = None
    dimension_values: dict[str, float] | None = None


@dataclass(frozen=True)
class ExtrudeProfile:
    sketchId: str
    profileIds: tuple[str, ...] | None


@dataclass(frozen=True)
class ExtrudeParameters:
    profile: ExtrudeProfile
    distance: float
    operation: str
    target: FeatureTarget | None
    direction: str


@dataclass(frozen=True)
class RevolveParameters:
    profile: ExtrudeProfile
    axis: str
    operation: str


@dataclass(frozen=True)
class FeatureTarget:
    featureId: str


@dataclass(frozen=True)
class SketchTool:
    sketchId: str


@dataclass(frozen=True)
class CutParameters:
    target: FeatureTarget
    tool: SketchTool
    depth: float | None
    flip: bool


@dataclass(frozen=True)
class HoleParameters:
    target: FeatureTarget
    diameter: float
    depth: float | None
    positions: tuple[HolePosition, ...]
    pattern: CircularHolePattern | LinearHolePattern | None


@dataclass(frozen=True)
class EdgeTreatmentParameters:
    target: FeatureTarget
    edges: tuple[Reference, ...]
    size: float


@dataclass(frozen=True)
class PcbBaseParameters:
    board: FeatureTarget
    wallThickness: float
    floorThickness: float
    clearance: float
    wallHeightAboveBoard: float
    boardSeatHeight: float


@dataclass(frozen=True)
class StandoffSelection:
    mode: str
    holeIds: tuple[str, ...]


@dataclass(frozen=True)
class StandoffsParameters:
    target: FeatureTarget
    board: FeatureTarget
    selection: StandoffSelection
    bossDiameter: float
    pilotDiameter: float


@dataclass(frozen=True)
class LidLip:
    depth: float
    width: float


@dataclass(frozen=True)
class PcbLidParameters:
    base: FeatureTarget
    thickness: float
    fitGap: float
    lip: LidLip | None


@dataclass(frozen=True)
class PortEnvelope:
    source: str
    width: float | None
    height: float
    bottomOffset: float


@dataclass(frozen=True)
class PortCutoutParameters:
    base: FeatureTarget
    footprint: str
    wall: str
    clearance: float
    envelope: PortEnvelope


@dataclass(frozen=True)
class ScrewClosureParameters:
    base: FeatureTarget
    lid: FeatureTarget
    points: tuple[ScrewPoint, ...]
    basePilotDiameter: float
    bossDiameter: float
    lidClearanceDiameter: float


@dataclass(frozen=True)
class ExtrudeChanges:
    distance: float


@dataclass(frozen=True)
class PcbBaseChanges:
    wallThickness: float | None = None
    floorThickness: float | None = None
    clearance: float | None = None
    wallHeightAboveBoard: float | None = None
    boardSeatHeight: float | None = None


@dataclass(frozen=True)
class StandoffsChanges:
    bossDiameter: float | None = None
    pilotDiameter: float | None = None


@dataclass(frozen=True)
class PcbLidChanges:
    thickness: float | None = None
    fitGap: float | None = None
    lip: LidLip | None = None
    outlineSource: FeatureTarget | None = None


@dataclass(frozen=True)
class PortCutoutChanges:
    footprint: str | None = None
    wall: str | None = None
    clearance: float | None = None
    envelope: PortEnvelope | None = None


@dataclass(frozen=True)
class ScrewClosureChanges:
    points: tuple[ScrewPoint, ...] | None = None
    basePilotDiameter: float | None = None
    bossDiameter: float | None = None
    lidClearanceDiameter: float | None = None


@dataclass(frozen=True)
class TargetChanges:
    target: FeatureTarget


@dataclass(frozen=True)
class CircularHolePatternChanges:
    center: tuple[float, float] | None = None
    radius: float | None = None
    count: int | None = None
    start_angle: float | None = None
    name_prefix: str | None = None


@dataclass(frozen=True)
class LinearHolePatternChanges:
    start: tuple[float, float] | None = None
    direction: tuple[float, float] | None = None
    pitch: float | None = None
    count: int | None = None
    name_prefix: str | None = None


@dataclass(frozen=True)
class HoleChanges:
    target: FeatureTarget | None = None
    pattern: CircularHolePatternChanges | LinearHolePatternChanges | None = None


@dataclass(frozen=True)
class EdgeTreatmentChanges:
    size: float


@dataclass(frozen=True)
class SketchCreate(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    sketchId: str
    plane: SketchPlane
    entities: tuple[Rectangle | Circle | Polyline | Spline, ...]
    constrained: ConstrainedSketch | None = None


@dataclass(frozen=True)
class SketchUpdate(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    sketchId: str
    set: SketchChanges


class _FeatureCreate(_Operation):
    featureId: str
    parameters: Any

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {*super().wire_fields(), "kind"}


@dataclass(frozen=True)
class ExtrudeCreate(_FeatureCreate):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    parameters: ExtrudeParameters
    kind: ClassVar[str] = "extrude"


@dataclass(frozen=True)
class RevolveCreate(_FeatureCreate):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    parameters: RevolveParameters
    kind: ClassVar[str] = "revolve"


@dataclass(frozen=True)
class CutCreate(_FeatureCreate):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    parameters: CutParameters
    kind: ClassVar[str] = "cut"


@dataclass(frozen=True)
class HoleCreate(_FeatureCreate):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    parameters: HoleParameters
    kind: ClassVar[str] = "hole"


@dataclass(frozen=True)
class FilletCreate(_FeatureCreate):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    parameters: EdgeTreatmentParameters
    kind: ClassVar[str] = "fillet"


@dataclass(frozen=True)
class ChamferCreate(_FeatureCreate):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    parameters: EdgeTreatmentParameters
    kind: ClassVar[str] = "chamfer"


@dataclass(frozen=True)
class PcbBaseCreate(_FeatureCreate):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    parameters: PcbBaseParameters
    kind: ClassVar[str] = "pcb_base"


@dataclass(frozen=True)
class StandoffsCreate(_FeatureCreate):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    parameters: StandoffsParameters
    kind: ClassVar[str] = "standoffs"


@dataclass(frozen=True)
class PcbLidCreate(_FeatureCreate):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    parameters: PcbLidParameters
    kind: ClassVar[str] = "pcb_lid"


@dataclass(frozen=True)
class PortCutoutCreate(_FeatureCreate):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    parameters: PortCutoutParameters
    kind: ClassVar[str] = "port_cutout"


@dataclass(frozen=True)
class ScrewClosureCreate(_FeatureCreate):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    parameters: ScrewClosureParameters
    kind: ClassVar[str] = "screw_closure"


@dataclass(frozen=True)
class ExtrudeUpdate(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    set: ExtrudeChanges
    kind: ClassVar[str] = "extrude"

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {*super().wire_fields(), "kind"}


@dataclass(frozen=True)
class PcbBaseUpdate(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    set: PcbBaseChanges
    kind: ClassVar[str] = "pcb_base"

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {*super().wire_fields(), "kind"}


@dataclass(frozen=True)
class StandoffsUpdate(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    set: StandoffsChanges
    kind: ClassVar[str] = "standoffs"

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {*super().wire_fields(), "kind"}


@dataclass(frozen=True)
class PcbLidUpdate(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    set: PcbLidChanges
    kind: ClassVar[str] = "pcb_lid"

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {*super().wire_fields(), "kind"}


@dataclass(frozen=True)
class PortCutoutUpdate(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    set: PortCutoutChanges
    kind: ClassVar[str] = "port_cutout"

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {*super().wire_fields(), "kind"}


@dataclass(frozen=True)
class ScrewClosureUpdate(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    set: ScrewClosureChanges
    kind: ClassVar[str] = "screw_closure"

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {*super().wire_fields(), "kind"}


@dataclass(frozen=True)
class FilletUpdate(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    set: EdgeTreatmentChanges
    kind: ClassVar[str] = "fillet"

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {*super().wire_fields(), "kind"}


@dataclass(frozen=True)
class ChamferUpdate(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    set: EdgeTreatmentChanges
    kind: ClassVar[str] = "chamfer"

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {*super().wire_fields(), "kind"}


@dataclass(frozen=True)
class CutUpdate(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    set: TargetChanges
    kind: ClassVar[str] = "cut"

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {*super().wire_fields(), "kind"}


@dataclass(frozen=True)
class HoleUpdate(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    set: HoleChanges
    kind: ClassVar[str] = "hole"

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {*super().wire_fields(), "kind"}


DeleteOperation = ImportDelete | SketchDelete | FeatureDelete

MutationOperation = (
    AssemblyOccurrenceCreate
    | AssemblyOccurrenceUpdate
    | AssemblyOccurrenceDelete
    | AssemblyMateCreate
    | AssemblyMateUpdate
    | AssemblyMateDelete
    | BoardImportOperation
    | ImportDelete
    | SketchCreate
    | SketchUpdate
    | SketchDelete
    | FeatureDelete
    | ExtrudeCreate
    | RevolveCreate
    | CutCreate
    | HoleCreate
    | FilletCreate
    | ChamferCreate
    | PcbBaseCreate
    | StandoffsCreate
    | PcbLidCreate
    | PortCutoutCreate
    | ScrewClosureCreate
    | ScrewClosureUpdate
    | ExtrudeUpdate
    | PcbBaseUpdate
    | StandoffsUpdate
    | PcbLidUpdate
    | PortCutoutUpdate
    | FilletUpdate
    | ChamferUpdate
    | CutUpdate
    | HoleUpdate
)


def _parse_delete(
    payload: dict[str, Any],
    operation: type[ImportDelete | SketchDelete | FeatureDelete],
    id_field: str,
) -> ImportDelete | SketchDelete | FeatureDelete:
    return operation(
        action=Action(payload.get("action")),
        expectedRevision=_finite_number(payload.get("expectedRevision"), "expectedRevision"),
        requestId=payload.get("requestId"),
        **{id_field: payload.get(id_field)},
        cascade=payload.get("cascade", False),
    )


def _parse_import(payload: dict[str, Any]) -> BoardImportOperation | ImportDelete:
    action = Action(payload.get("action"))
    if action is Action.DELETE:
        return _parse_delete(payload, ImportDelete, "assetId")
    path = payload.get("path")
    if action is Action.CREATE and not isinstance(path, str):
        raise RequestError("INVALID_PARAMETER", "create import requires path")
    if isinstance(path, str) and Path(path).is_absolute():
        path = str(Path(path).resolve())
    return BoardImportOperation(
        action=action,
        expectedRevision=_finite_number(payload.get("expectedRevision"), "expectedRevision"),
        requestId=payload.get("requestId"),
        assetId=payload.get("assetId"),
        format=payload.get("format"),
        path=path,
    )


def _entity_id(entity: dict[str, Any], index: int) -> str:
    return entity.get("id", f"{entity['kind']}-{index + 1}")


def _outline_entity(entity: dict[str, Any], index: int) -> Polyline | Spline:
    kind = entity["kind"]
    if entity.get("closed") is not True:
        raise RequestError(
            "INVALID_PARAMETER",
            f"{kind} closed must be true; open outlines are not supported",
            field="closed",
        )
    raw_points = entity.get("points")
    if (
        not isinstance(raw_points, list)
        or not MIN_OUTLINE_POINTS <= len(raw_points) <= MAX_OUTLINE_POINTS
    ):
        raise RequestError(
            "INVALID_PARAMETER",
            f"{kind} points must contain {MIN_OUTLINE_POINTS} to {MAX_OUTLINE_POINTS} points",
            field="points",
        )
    points: list[tuple[float, float]] = []
    point_ids: list[str] = []
    point_ids_explicit = True
    for point_index, point in enumerate(raw_points):
        if isinstance(point, (list, tuple)) and len(point) == 2:
            point_id, x, y = f"p{point_index}", point[0], point[1]
            point_ids_explicit = False
        elif (
            isinstance(point, dict)
            and isinstance(point.get("id"), str)
            and point["id"]
            and "x" in point
            and "y" in point
        ):
            point_id, x, y = point["id"], point["x"], point["y"]
        else:
            raise RequestError(
                "INVALID_PARAMETER",
                "each outline point must be [x, y] or {id, x, y}",
                field="points",
            )
        if re.fullmatch(r"[A-Za-z0-9_]+", point_id) is None:
            raise RequestError(
                "INVALID_PARAMETER",
                "outline point ids may contain only ASCII letters, digits, and underscores",
                field=f"points[{point_index}].id",
            )
        point_ids.append(point_id)
        points.append((_finite_number(x, "points"), _finite_number(y, "points")))
    if len(set(point_ids)) != len(point_ids):
        raise RequestError("INVALID_PARAMETER", "outline point ids must be unique", field="points")
    entity_type = Polyline if kind == "polyline" else Spline
    return entity_type(
        _entity_id(entity, index),
        tuple(points),
        point_ids=tuple(point_ids),
        point_ids_explicit=point_ids_explicit,
        id_explicit="id" in entity,
    )


def _entities(payload: Any) -> tuple[Rectangle | Circle | Polyline | Spline, ...]:
    if not isinstance(payload, list) or not payload:
        raise RequestError("INVALID_PARAMETER", "entities must not be empty")
    parsed = []
    allowed = ["rectangle", "circle", "polyline", "spline"]
    for index, entity in enumerate(payload):
        if not isinstance(entity, dict) or entity.get("kind") not in allowed:
            raise RequestError(
                "INVALID_PARAMETER",
                "unknown sketch entity kind",
                allowedKinds=allowed,
            )
        if entity["kind"] in {"polyline", "spline"}:
            parsed.append(_outline_entity(entity, index))
            continue
        center = entity.get("center", [0, 0])
        common = {
            "id": entity["id"],
            "position": (
                _finite_number(center[0], "center"),
                _finite_number(center[1], "center"),
            ),
            "rotation": _angle(entity.get("rotation", {"value": 0, "unit": "deg"})),
        }
        if entity["kind"] == "rectangle":
            parsed.append(
                Rectangle(
                    **common,
                    width=_positive_quantity(entity.get("width"), "width"),
                    height=_positive_quantity(entity.get("height"), "height"),
                )
            )
        else:
            parsed.append(
                Circle(
                    **common,
                    radius=_positive_quantity(entity.get("radius"), "radius"),
                )
            )
    return tuple(parsed)


def _constrained_sketch(payload: Any) -> ConstrainedSketch:
    try:
        return constrained_from_payload(payload)
    except ConstrainedSketchPayloadError as error:
        raise RequestError("INVALID_PARAMETER", str(error), field=error.field) from error


def _sketch_common(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": Action(payload.get("action")),
        "expectedRevision": _finite_number(payload.get("expectedRevision"), "expectedRevision"),
        "requestId": payload.get("requestId"),
        "sketchId": payload.get("sketchId"),
    }


def _sketch_plane(payload: dict[str, Any]) -> SketchPlane:
    plane = payload.get("plane", {})
    return SketchPlane(
        plane.get("kind"),
        plane.get("name"),
        _quantity(plane.get("offset", {"value": 0, "unit": "mm"}), "mm", "offset"),
    )


def _parse_primitive_sketch_create(payload: dict[str, Any]) -> SketchCreate:
    return SketchCreate(
        **_sketch_common(payload),
        plane=_sketch_plane(payload),
        entities=_entities(payload.get("entities")),
    )


def _parse_constrained_sketch_create(payload: dict[str, Any]) -> SketchCreate:
    return SketchCreate(
        **_sketch_common(payload),
        plane=_sketch_plane(payload),
        entities=(),
        constrained=_constrained_sketch(payload.get("constrained")),
    )


def _parse_dimension_values(changes: dict[str, Any]) -> dict[str, float] | None:
    if "dimensionValues" not in changes:
        return None
    values = changes["dimensionValues"]
    if not isinstance(values, dict) or not values:
        raise RequestError(
            "INVALID_PARAMETER",
            "dimensionValues must be a JSON object with at least one entry",
            field="dimensionValues",
        )
    if any(isinstance(value, bool) for value in values.values()):
        raise RequestError(
            "INVALID_PARAMETER",
            "dimensionValues values must be finite numbers",
            field="dimensionValues",
        )
    return {
        dimension_id: _finite_number(value, f"dimensionValues.{dimension_id}")
        for dimension_id, value in values.items()
    }


def _parse_sketch_update(payload: dict[str, Any]) -> SketchUpdate:
    changes = payload.get("set")
    if not isinstance(changes, dict) or not changes:
        raise RequestError("INVALID_PARAMETER", "update set must not be empty")
    position = changes.get("position")
    return SketchUpdate(
        **_sketch_common(payload),
        set=SketchChanges(
            position=(
                tuple(_finite_number(value, "position") for value in position)
                if position is not None
                else None
            ),
            entities=_entities(changes["entities"]) if "entities" in changes else None,
            constrained=(
                _constrained_sketch(changes["constrained"]) if "constrained" in changes else None
            ),
            dimension_values=_parse_dimension_values(changes),
        ),
    )


def _parse_sketch(payload: dict[str, Any]) -> SketchCreate | SketchUpdate | SketchDelete:
    if payload.get("action") == "delete":
        return _parse_delete(payload, SketchDelete, "sketchId")
    if payload.get("action") == "update":
        return _parse_sketch_update(payload)
    if "constrained" in payload:
        return _parse_constrained_sketch_create(payload)
    return _parse_primitive_sketch_create(payload)


def _feature_common(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": Action(payload.get("action")),
        "expectedRevision": _finite_number(payload.get("expectedRevision"), "expectedRevision"),
        "requestId": payload.get("requestId"),
        "featureId": payload.get("featureId"),
    }


def _parse_extrude(
    payload: dict[str, Any], common: dict[str, Any]
) -> ExtrudeCreate | ExtrudeUpdate:
    if common["action"] is Action.UPDATE:
        changes = payload.get("set")
        if not isinstance(changes, dict) or set(changes) != {"distance"}:
            raise RequestError("INVALID_PARAMETER", "extrude update requires set.distance")
        return ExtrudeUpdate(
            **common,
            set=ExtrudeChanges(distance=_positive_quantity(changes["distance"], "distance")),
        )
    parameters = payload.get("parameters", {})
    profile = parameters.get("profile", {})
    return ExtrudeCreate(
        **common,
        parameters=ExtrudeParameters(
            profile=ExtrudeProfile(
                sketchId=profile.get("sketchId"),
                profileIds=(tuple(profile["profileIds"]) if "profileIds" in profile else None),
            ),
            distance=_positive_quantity(parameters.get("distance"), "distance"),
            operation=parameters.get("operation"),
            target=(
                FeatureTarget(parameters["target"].get("featureId"))
                if "target" in parameters
                else None
            ),
            direction=parameters.get("direction"),
        ),
    )


def _parse_revolve(payload: dict[str, Any], common: dict[str, Any]) -> RevolveCreate:
    if common["action"] is Action.UPDATE:
        raise RequestError("INVALID_PARAMETER", "revolve is updated through its source sketch")
    parameters = payload.get("parameters", {})
    profile = parameters.get("profile", {})
    angle = parameters.get("angle", {})
    if parameters.get("operation") != "new_body" or parameters.get("axis") not in {"x", "y"}:
        raise RequestError(
            "INVALID_PARAMETER", "revolve supports only new_body around local x or y"
        )
    if angle != {"value": 360, "unit": "deg"}:
        raise RequestError("INVALID_PARAMETER", "revolve angle must be exactly 360 deg")
    return RevolveCreate(
        **common,
        parameters=RevolveParameters(
            profile=ExtrudeProfile(
                sketchId=profile.get("sketchId"),
                profileIds=(tuple(profile["profileIds"]) if "profileIds" in profile else None),
            ),
            axis=parameters["axis"],
            operation=parameters["operation"],
        ),
    )


def _parse_target_update(
    payload: dict[str, Any], common: dict[str, Any], update: type[CutUpdate | HoleUpdate]
) -> CutUpdate | HoleUpdate:
    changes = payload.get("set")
    if not isinstance(changes, dict) or set(changes) != {"target"}:
        raise RequestError("INVALID_PARAMETER", f"{update.kind} update requires set.target")
    return update(
        **common,
        set=TargetChanges(target=FeatureTarget(changes["target"].get("featureId"))),
    )


def _parse_cut(payload: dict[str, Any], common: dict[str, Any]) -> CutCreate | CutUpdate:
    if common["action"] is Action.UPDATE:
        return _parse_target_update(payload, common, CutUpdate)
    parameters = payload.get("parameters", {})
    depth = parameters.get("depth", {})
    return CutCreate(
        **common,
        parameters=CutParameters(
            target=FeatureTarget(parameters.get("target", {}).get("featureId")),
            tool=SketchTool(parameters.get("tool", {}).get("sketchId")),
            depth=(
                None
                if depth.get("kind") == "through_all"
                else _positive_quantity(depth.get("distance"), "depth")
            ),
            flip=parameters.get("flip"),
        ),
    )


def _hole_positions(parameters: dict[str, Any]) -> tuple[HolePosition, ...]:
    raw_positions = parameters.get("positions")
    if raw_positions is None:
        raw_positions = [{"name": "h1", "point": parameters.get("position", [])}]
    positions = tuple(
        HolePosition(
            item.get("name", f"h{index}"),
            tuple(
                _finite_number(value, f"positions[{index - 1}].point") for value in item["point"]
            ),
        )
        for index, item in enumerate(raw_positions, start=1)
    )
    names = [position.name for position in positions]
    if len(names) != len(set(names)):
        raise RequestError("INVALID_PARAMETER", "hole position names must be unique")
    return positions


def _hole_point(value: Any, field: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise RequestError("INVALID_PARAMETER", f"{field} must contain two numbers", field=field)
    return tuple(_finite_number(item, f"{field}[{index}]") for index, item in enumerate(value))


def _hole_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_HOLE_COUNT:
        raise RequestError(
            "INVALID_PARAMETER",
            f"pattern.count must be an integer from 1 through {MAX_HOLE_COUNT}",
            field="pattern.count",
            maximum=MAX_HOLE_COUNT,
        )
    return value


def _name_prefix(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise RequestError(
            "INVALID_PARAMETER",
            "pattern.namePrefix must be a non-empty string",
            field="pattern.namePrefix",
        )
    return value


def _direction(value: Any) -> tuple[float, float]:
    x, y = _hole_point(value, "pattern.direction")
    length = math.hypot(x, y)
    if length == 0:
        raise RequestError(
            "INVALID_PARAMETER",
            "pattern.direction must be non-zero",
            field="pattern.direction",
        )
    return x / length, y / length


def _circular_hole_pattern(value: dict[str, Any]) -> CircularHolePattern:
    return CircularHolePattern(
        center=_hole_point(value.get("center"), "pattern.center"),
        radius=_positive_quantity(value.get("radius"), "pattern.radius"),
        count=_hole_count(value.get("count")),
        start_angle=_angle(value.get("startAngle"), "pattern.startAngle"),
        name_prefix=_name_prefix(value.get("namePrefix")),
    )


def _linear_hole_pattern(value: dict[str, Any]) -> LinearHolePattern:
    return LinearHolePattern(
        start=_hole_point(value.get("start"), "pattern.start"),
        direction=_direction(value.get("direction")),
        pitch=_positive_quantity(value.get("pitch"), "pattern.pitch"),
        count=_hole_count(value.get("count")),
        name_prefix=_name_prefix(value.get("namePrefix")),
    )


def _hole_pattern(value: Any) -> CircularHolePattern | LinearHolePattern:
    if not isinstance(value, dict):
        raise RequestError("INVALID_PARAMETER", "pattern must be an object", field="pattern")
    parsers = {"circular": _circular_hole_pattern, "linear": _linear_hole_pattern}
    if parser := parsers.get(value.get("kind")):
        return parser(value)
    raise RequestError(
        "INVALID_PARAMETER", "pattern.kind must be circular or linear", field="pattern.kind"
    )


def _circular_hole_pattern_changes(value: dict[str, Any]) -> CircularHolePatternChanges:
    return CircularHolePatternChanges(
        center=_hole_point(value["center"], "pattern.center") if "center" in value else None,
        radius=_positive_quantity(value["radius"], "pattern.radius") if "radius" in value else None,
        count=_hole_count(value["count"]) if "count" in value else None,
        start_angle=(
            _angle(value["startAngle"], "pattern.startAngle") if "startAngle" in value else None
        ),
        name_prefix=_name_prefix(value["namePrefix"]) if "namePrefix" in value else None,
    )


def _linear_hole_pattern_changes(value: dict[str, Any]) -> LinearHolePatternChanges:
    return LinearHolePatternChanges(
        start=_hole_point(value["start"], "pattern.start") if "start" in value else None,
        direction=_direction(value["direction"]) if "direction" in value else None,
        pitch=_positive_quantity(value["pitch"], "pattern.pitch") if "pitch" in value else None,
        count=_hole_count(value["count"]) if "count" in value else None,
        name_prefix=_name_prefix(value["namePrefix"]) if "namePrefix" in value else None,
    )


def _hole_pattern_changes(
    value: Any,
) -> CircularHolePatternChanges | LinearHolePatternChanges:
    if not isinstance(value, dict):
        raise RequestError("INVALID_PARAMETER", "pattern must be an object", field="pattern")
    parsers = {
        "circular": _circular_hole_pattern_changes,
        "linear": _linear_hole_pattern_changes,
    }
    if parser := parsers.get(value.get("kind")):
        return parser(value)
    raise RequestError(
        "INVALID_PARAMETER", "pattern.kind must be circular or linear", field="pattern.kind"
    )


def _parse_hole(payload: dict[str, Any], common: dict[str, Any]) -> HoleCreate | HoleUpdate:
    if common["action"] is Action.UPDATE:
        changes = payload.get("set", {})
        return HoleUpdate(
            **common,
            set=HoleChanges(
                target=(
                    FeatureTarget(changes["target"].get("featureId"))
                    if "target" in changes
                    else None
                ),
                pattern=(
                    _hole_pattern_changes(changes["pattern"]) if "pattern" in changes else None
                ),
            ),
        )
    parameters = payload.get("parameters", {})
    pattern = _hole_pattern(parameters["pattern"]) if "pattern" in parameters else None
    return HoleCreate(
        **common,
        parameters=HoleParameters(
            target=FeatureTarget(parameters.get("target", {}).get("featureId")),
            diameter=_positive_quantity(parameters.get("diameter"), "diameter"),
            depth=(
                _positive_quantity(parameters["depth"], "depth") if "depth" in parameters else None
            ),
            positions=() if pattern is not None else _hole_positions(parameters),
            pattern=pattern,
        ),
    )


def _parse_edge_treatment(
    payload: dict[str, Any],
    common: dict[str, Any],
) -> FilletCreate | ChamferCreate | FilletUpdate | ChamferUpdate:
    kind = payload["kind"]
    parameter = "radius" if kind == "fillet" else "distance"
    create = FilletCreate if kind == "fillet" else ChamferCreate
    update = FilletUpdate if kind == "fillet" else ChamferUpdate
    if common["action"] is Action.UPDATE:
        changes = payload.get("set")
        if not isinstance(changes, dict) or set(changes) != {parameter}:
            raise RequestError("INVALID_PARAMETER", f"{kind} update requires set.{parameter}")
        return update(
            **common,
            set=EdgeTreatmentChanges(size=_positive_quantity(changes[parameter], parameter)),
        )
    parameters = payload.get("parameters", {})
    return create(
        **common,
        parameters=EdgeTreatmentParameters(
            target=FeatureTarget(parameters.get("target", {}).get("featureId")),
            edges=tuple(_reference(edge) for edge in parameters.get("edges", [])),
            size=_positive_quantity(parameters.get(parameter), parameter),
        ),
    )


def _feature_quantity(value: Any, parameter: str, feature: str) -> float:
    try:
        return _positive_quantity(value, parameter)
    except RequestError as error:
        error.details.update(feature=feature, parameter=parameter)
        raise


def _parse_positive_changes(
    payload: dict[str, Any], allowed: set[str], kind: str, feature: str
) -> dict[str, float]:
    changes = payload.get("set")
    if not isinstance(changes, dict) or not changes or not set(changes) <= allowed:
        raise RequestError(
            "INVALID_PARAMETER",
            f"{kind} update requires one or more numeric parameters",
        )
    return {name: _feature_quantity(value, name, feature) for name, value in changes.items()}


def _parse_pcb_base(
    payload: dict[str, Any], common: dict[str, Any]
) -> PcbBaseCreate | PcbBaseUpdate:
    names = {
        "wallThickness",
        "floorThickness",
        "clearance",
        "wallHeightAboveBoard",
        "boardSeatHeight",
    }
    if common["action"] is Action.UPDATE:
        return PcbBaseUpdate(
            **common,
            set=PcbBaseChanges(
                **_parse_positive_changes(payload, names, "pcb_base", common["featureId"])
            ),
        )
    parameters = payload.get("parameters", {})
    return PcbBaseCreate(
        **common,
        parameters=PcbBaseParameters(
            board=FeatureTarget(parameters.get("board", {}).get("featureId")),
            **{
                name: _feature_quantity(parameters.get(name), name, common["featureId"])
                for name in names
            },
        ),
    )


def _standoff_selection(parameters: dict[str, Any], feature: str) -> StandoffSelection:
    selection = parameters.get("selection", {})
    mode = selection.get("mode")
    if mode == "explicit":
        hole_ids = selection.get("holeIds")
        if (
            not isinstance(hole_ids, list)
            or not hole_ids
            or not all(isinstance(item, str) and item for item in hole_ids)
        ):
            raise RequestError("INVALID_PARAMETER", "explicit selection requires holeIds")
        if len(hole_ids) != len(set(hole_ids)):
            raise RequestError("INVALID_PARAMETER", "selection holeIds must be unique")
        return StandoffSelection(mode, tuple(hole_ids))
    if mode == "all_mounting":
        if "minimumDiameter" in selection:
            raise RequestError(
                "INVALID_PARAMETER",
                "minimumDiameter was removed; all_mounting now selects every MountingHole:* footprint hole",
                feature=feature,
                parameter="selection.minimumDiameter",
            )
        return StandoffSelection(mode, ())
    raise RequestError("INVALID_PARAMETER", "unknown standoff selection mode")


def _parse_standoffs(
    payload: dict[str, Any], common: dict[str, Any]
) -> StandoffsCreate | StandoffsUpdate:
    names = {"bossDiameter", "pilotDiameter"}
    if common["action"] is Action.UPDATE:
        return StandoffsUpdate(
            **common,
            set=StandoffsChanges(
                **_parse_positive_changes(payload, names, "standoffs", common["featureId"])
            ),
        )
    parameters = payload.get("parameters", {})
    return StandoffsCreate(
        **common,
        parameters=StandoffsParameters(
            target=FeatureTarget(parameters.get("target", {}).get("featureId")),
            board=FeatureTarget(parameters.get("board", {}).get("featureId")),
            selection=_standoff_selection(parameters, common["featureId"]),
            bossDiameter=_feature_quantity(
                parameters.get("bossDiameter"), "bossDiameter", common["featureId"]
            ),
            pilotDiameter=_feature_quantity(
                parameters.get("pilotDiameter"), "pilotDiameter", common["featureId"]
            ),
        ),
    )


def _parse_lid_lip(value: Any, feature: str) -> LidLip:
    if not isinstance(value, dict) or set(value) != {"depth", "width"}:
        raise RequestError(
            "INVALID_PARAMETER",
            "pcb_lid lip requires depth and width",
            feature=feature,
            parameter="lip",
        )
    return LidLip(
        depth=_feature_quantity(value["depth"], "lip.depth", feature),
        width=_feature_quantity(value["width"], "lip.width", feature),
    )


def _parse_pcb_lid(payload: dict[str, Any], common: dict[str, Any]) -> PcbLidCreate | PcbLidUpdate:
    values = payload.get("set" if common["action"] is Action.UPDATE else "parameters", {})
    if common["action"] is Action.UPDATE:
        if (
            not isinstance(values, dict)
            or not values
            or not set(values)
            <= {
                "thickness",
                "fitGap",
                "lip",
                "outlineSource",
            }
        ):
            raise RequestError(
                "INVALID_PARAMETER",
                "pcb_lid update requires thickness, fitGap, lip, or outlineSource",
            )
        return PcbLidUpdate(
            **common,
            set=PcbLidChanges(
                thickness=(
                    _feature_quantity(values["thickness"], "thickness", common["featureId"])
                    if "thickness" in values
                    else None
                ),
                fitGap=(
                    _feature_quantity(values["fitGap"], "fitGap", common["featureId"])
                    if "fitGap" in values
                    else None
                ),
                lip=(
                    _parse_lid_lip(values["lip"], common["featureId"]) if "lip" in values else None
                ),
                outlineSource=(
                    FeatureTarget(values["outlineSource"].get("featureId"))
                    if isinstance(values.get("outlineSource"), dict)
                    else None
                ),
            ),
        )
    return PcbLidCreate(
        **common,
        parameters=PcbLidParameters(
            base=FeatureTarget(values.get("base", {}).get("featureId")),
            thickness=_feature_quantity(values.get("thickness"), "thickness", common["featureId"]),
            fitGap=_feature_quantity(values.get("fitGap"), "fitGap", common["featureId"]),
            lip=(_parse_lid_lip(values["lip"], common["featureId"]) if "lip" in values else None),
        ),
    )


def _parse_port_envelope(value: Any, feature: str) -> PortEnvelope:
    if not isinstance(value, dict):
        raise RequestError(
            "INVALID_PARAMETER",
            "port_cutout envelope must be an object",
            feature=feature,
            parameter="envelope",
        )
    source = value.get("source")
    required = {"source", "height", "bottomOffset"}
    allowed = required | ({"width"} if source == "explicit" else set())
    if source not in {"courtyard", "explicit"} or set(value) != allowed:
        raise RequestError(
            "INVALID_PARAMETER",
            "explicit port_cutout envelope requires width, height, and bottomOffset; courtyard requires height and bottomOffset",
            feature=feature,
            parameter="envelope",
        )
    try:
        bottom = _quantity(value["bottomOffset"], "mm", "envelope.bottomOffset")
    except RequestError as error:
        error.details.update(feature=feature, parameter="envelope.bottomOffset")
        raise
    return PortEnvelope(
        source=source,
        width=(
            _feature_quantity(value["width"], "envelope.width", feature)
            if source == "explicit"
            else None
        ),
        height=_feature_quantity(value["height"], "envelope.height", feature),
        bottomOffset=bottom,
    )


def _port_text(value: Any, parameter: str, feature: str) -> str:
    if not isinstance(value, str) or not value:
        raise RequestError(
            "INVALID_PARAMETER",
            f"port_cutout {parameter} must be a non-empty string",
            feature=feature,
            parameter=parameter,
        )
    return value


def _port_wall(value: Any, feature: str) -> str:
    wall = _port_text(value, "wall", feature)
    if wall not in {"auto", "left", "right", "front", "back"}:
        raise RequestError(
            "INVALID_PARAMETER",
            "port_cutout wall must be auto, left, right, front, or back",
            feature=feature,
            parameter="wall",
        )
    return wall


def _parse_port_cutout(
    payload: dict[str, Any], common: dict[str, Any]
) -> PortCutoutCreate | PortCutoutUpdate:
    feature = common["featureId"]
    values = payload.get("set" if common["action"] is Action.UPDATE else "parameters", {})
    if not isinstance(values, dict):
        raise RequestError("INVALID_PARAMETER", "port_cutout values must be an object")
    if common["action"] is Action.UPDATE:
        allowed = {"footprint", "wall", "clearance", "envelope"}
        if not values or not set(values) <= allowed:
            raise RequestError(
                "INVALID_PARAMETER",
                "port_cutout update requires footprint, wall, clearance, or envelope",
            )
        return PortCutoutUpdate(
            **common,
            set=PortCutoutChanges(
                footprint=(
                    _port_text(values["footprint"], "footprint", feature)
                    if "footprint" in values
                    else None
                ),
                wall=_port_wall(values["wall"], feature) if "wall" in values else None,
                clearance=(
                    _feature_quantity(values["clearance"], "clearance", feature)
                    if "clearance" in values
                    else None
                ),
                envelope=(
                    _parse_port_envelope(values["envelope"], feature)
                    if "envelope" in values
                    else None
                ),
            ),
        )
    return PortCutoutCreate(
        **common,
        parameters=PortCutoutParameters(
            base=FeatureTarget(values.get("base", {}).get("featureId")),
            footprint=_port_text(values.get("footprint"), "footprint", feature),
            wall=_port_wall(values.get("wall"), feature),
            clearance=_feature_quantity(values.get("clearance"), "clearance", feature),
            envelope=_parse_port_envelope(values.get("envelope"), feature),
        ),
    )


def _parse_screw_points(raw_points: Any, feature: str) -> tuple[ScrewPoint, ...]:
    if not isinstance(raw_points, list) or not 2 <= len(raw_points) <= 8:
        raise RequestError(
            "INVALID_PARAMETER",
            "screw_closure requires 2 to 8 named points",
            feature=feature,
            parameter="points",
        )
    points = []
    for item in raw_points:
        name = item.get("name") if isinstance(item, dict) else None
        point = item.get("point") if isinstance(item, dict) else None
        if not isinstance(name, str) or not name or not isinstance(point, list) or len(point) != 2:
            raise RequestError(
                "INVALID_PARAMETER",
                "each screw_closure point requires a name and xy point",
                feature=feature,
                parameter="points",
            )
        points.append(ScrewPoint(name, tuple(_finite_number(value, "point") for value in point)))
    if len({point.name for point in points}) != len(points):
        raise RequestError(
            "INVALID_PARAMETER",
            "screw_closure point names must be unique",
            feature=feature,
            parameter="points",
        )
    return tuple(points)


def _parse_screw_closure(
    payload: dict[str, Any], common: dict[str, Any]
) -> ScrewClosureCreate | ScrewClosureUpdate:
    feature = common["featureId"]
    update = common["action"] is Action.UPDATE
    values = payload.get("set" if update else "parameters", {})
    if not isinstance(values, dict):
        raise RequestError("INVALID_PARAMETER", "screw_closure values must be an object")
    if update:
        allowed = {"points", "basePilotDiameter", "bossDiameter", "lidClearanceDiameter"}
        if not values or not set(values) <= allowed:
            raise RequestError(
                "INVALID_PARAMETER",
                "screw_closure update requires points or a diameter",
                feature=feature,
            )
        return ScrewClosureUpdate(
            **common,
            set=ScrewClosureChanges(
                points=_parse_screw_points(values["points"], feature)
                if "points" in values
                else None,
                basePilotDiameter=(
                    _feature_quantity(values["basePilotDiameter"], "basePilotDiameter", feature)
                    if "basePilotDiameter" in values
                    else None
                ),
                bossDiameter=(
                    _feature_quantity(values["bossDiameter"], "bossDiameter", feature)
                    if "bossDiameter" in values
                    else None
                ),
                lidClearanceDiameter=(
                    _feature_quantity(
                        values["lidClearanceDiameter"], "lidClearanceDiameter", feature
                    )
                    if "lidClearanceDiameter" in values
                    else None
                ),
            ),
        )
    return ScrewClosureCreate(
        **common,
        parameters=ScrewClosureParameters(
            base=FeatureTarget(values.get("base", {}).get("featureId")),
            lid=FeatureTarget(values.get("lid", {}).get("featureId")),
            points=_parse_screw_points(values.get("points"), feature),
            basePilotDiameter=_feature_quantity(
                values.get("basePilotDiameter"), "basePilotDiameter", feature
            ),
            bossDiameter=_feature_quantity(values.get("bossDiameter"), "bossDiameter", feature),
            lidClearanceDiameter=_feature_quantity(
                values.get("lidClearanceDiameter"), "lidClearanceDiameter", feature
            ),
        ),
    )


def _parse_assembly_occurrence(
    payload: dict[str, Any],
) -> AssemblyOccurrenceCreate | AssemblyOccurrenceUpdate | AssemblyOccurrenceDelete:
    common = {
        "action": Action(payload.get("action")),
        "expectedRevision": _finite_number(payload.get("expectedRevision"), "expectedRevision"),
        "requestId": payload.get("requestId"),
        "occurrenceId": payload.get("occurrenceId"),
    }
    action = common["action"]
    if action is Action.DELETE:
        return AssemblyOccurrenceDelete(**common)
    raw_transform = (
        payload.get("parameters", {}).get("transform")
        if action is Action.CREATE
        else payload.get("set", {}).get("transform")
    )
    try:
        transform = normalize_transform(raw_transform)
    except AssemblyError as error:
        raise RequestError(error.code, str(error), **error.details) from error
    if action is Action.CREATE:
        return AssemblyOccurrenceCreate(
            **common,
            part_id=payload.get("parameters", {}).get("partId"),
            transform=transform,
        )
    return AssemblyOccurrenceUpdate(**common, transform=transform)


_MATE_REFERENCE_HINT = (
    "discover references with query select.kind face_roles; "
    "see coalesce://docs/assembly for planar mate request shapes"
)


def _parse_face_mate(values: dict[str, Any], mate_kind: str) -> dict[str, Any]:
    try:
        first = MateSide(values["first"]["occurrenceId"], _reference(values["first"]["reference"]))
        second = MateSide(
            values["second"]["occurrenceId"], _reference(values["second"]["reference"])
        )
        orientation = values["orientation"]
        offset = values.get("offset", {"value": 0, "unit": "mm"})
        if mate_kind == "planar" and offset["unit"] != "mm":
            raise RequestError("UNIT_MISMATCH", "planar mate offset unit must be mm")
        offset_value = _finite_number(offset["value"], "offset.value")
        if orientation not in {"aligned", "opposed"}:
            raise RequestError(
                "INVALID_PARAMETER", "planar mate orientation must be aligned or opposed"
            )
    except (KeyError, TypeError, ValueError) as error:
        raise RequestError(
            "INVALID_PARAMETER",
            f"{mate_kind} mate parameters are malformed",
            hint=_MATE_REFERENCE_HINT,
        ) from error
    if (
        first.reference.cardinality is not Cardinality.ONE
        or second.reference.cardinality is not Cardinality.ONE
    ):
        raise RequestError(
            "INVALID_PARAMETER",
            f"{mate_kind} mate references must have cardinality one",
            hint=_MATE_REFERENCE_HINT,
        )
    if first.occurrence_id == second.occurrence_id:
        raise RequestError("INVALID_PARAMETER", f"{mate_kind} mate must connect two occurrences")
    return {
        "first": first,
        "second": second,
        "orientation": orientation,
        "offset": offset_value,
        "mate_kind": mate_kind,
    }


def _parse_assembly_mate(
    payload: dict[str, Any],
) -> AssemblyMateCreate | AssemblyMateUpdate | AssemblyMateDelete:
    common = {
        "action": Action(payload.get("action")),
        "expectedRevision": _finite_number(payload.get("expectedRevision"), "expectedRevision"),
        "requestId": payload.get("requestId"),
        "mateId": payload.get("mateId"),
    }
    action = common["action"]
    if action is Action.DELETE:
        return AssemblyMateDelete(**common)
    values = payload.get("parameters", {}) if action is Action.CREATE else payload.get("set", {})
    mate_kind = (
        values.get("kind")
        if action is Action.CREATE
        else ("planar" if "offset" in values else ("concentric" if "first" in values else "ground"))
    )
    operation_type = AssemblyMateCreate if action is Action.CREATE else AssemblyMateUpdate
    if mate_kind == "ground":
        return operation_type(**common, occurrence_id=values.get("occurrenceId"))
    if mate_kind in {"planar", "concentric"}:
        return operation_type(**common, **_parse_face_mate(values, mate_kind))
    raise RequestError(
        "INVALID_PARAMETER", "assembly mate kind must be ground, planar, or concentric"
    )


def _parse_feature(
    payload: dict[str, Any],
) -> (
    ExtrudeCreate
    | RevolveCreate
    | CutCreate
    | HoleCreate
    | FilletCreate
    | ChamferCreate
    | PcbBaseCreate
    | StandoffsCreate
    | PcbLidCreate
    | PortCutoutCreate
    | ScrewClosureCreate
    | ScrewClosureUpdate
    | ExtrudeUpdate
    | PcbBaseUpdate
    | StandoffsUpdate
    | PcbLidUpdate
    | PortCutoutUpdate
    | FilletUpdate
    | ChamferUpdate
    | CutUpdate
    | HoleUpdate
    | FeatureDelete
):
    if payload.get("action") == "delete":
        return _parse_delete(payload, FeatureDelete, "featureId")
    kind = payload.get("kind")
    parser = {
        "extrude": _parse_extrude,
        "revolve": _parse_revolve,
        "cut": _parse_cut,
        "hole": _parse_hole,
        "fillet": _parse_edge_treatment,
        "chamfer": _parse_edge_treatment,
        "pcb_base": _parse_pcb_base,
        "standoffs": _parse_standoffs,
        "pcb_lid": _parse_pcb_lid,
        "port_cutout": _parse_port_cutout,
        "screw_closure": _parse_screw_closure,
    }.get(kind)
    if parser is None:
        raise RequestError(
            "INVALID_PARAMETER",
            f"unknown feature kind: {kind}",
            allowedKinds=_ALLOWED_FEATURE_KINDS,
        )
    return parser(payload, _feature_common(payload))


def _canonical(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float):
        return 0.0 if value == 0.0 else value
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _hash(operation: MutationOperation) -> str:
    intent = {"type": type(operation).__name__, **asdict(operation)}
    return hashlib.sha256(
        json.dumps(
            _canonical(intent), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _safe_error(
    revision: int,
    request_id: str | None,
    code: str,
    message: str,
    *,
    operation: str,
    **details: Any,
) -> dict[str, Any]:
    definition = ERROR_REGISTRY.get(code)
    if definition is None:
        raise ValueError(f"unregistered public error code: {code}")
    details = dict(details)
    if "feature" in details and "featureId" not in details:
        details["featureId"] = details.pop("feature")
    if terminal := details.get("terminalFeatureId"):
        details.setdefault("legalTargets", [{"featureId": terminal}])
    return {
        "ok": False,
        "revision": revision,
        "requestId": request_id,
        "units": "mm",
        "error": {
            "code": code,
            "message": message,
            "retryable": definition.retryable,
            "operation": operation,
            **details,
        },
    }


def _graph_error_response(
    revision: int,
    request_id: str | None,
    operation: str,
    error: GraphError,
) -> dict[str, Any]:
    internal_code = error.error["code"]
    registered = internal_code in ERROR_REGISTRY
    code = internal_code if registered else "KERNEL_OPERATION_FAILED"
    details = {key: value for key, value in error.error.items() if key not in {"code", "message"}}
    if code == "JOIN_NO_CONTACT":
        details["hint"] = "move or extend the listed profiles to contact the target"
    return _safe_error(
        revision,
        request_id,
        code,
        str(error) if registered else "the modeling operation could not produce valid geometry",
        operation=operation,
        **details,
    )


def _viewer_error(error: dict[str, Any]) -> dict[str, Any]:
    """The fields the viewer shows; kernelMessage goes only under "Error details"."""
    fields = (
        "code",
        "message",
        "hint",
        "kernelMessage",
        "failedFeatureIds",
        "unavailableFeatureIds",
        "diagnosticGeometry",
        "diagnosticGeometryOmitted",
    )
    viewer_error = {key: error[key] for key in fields if key in error}
    diagnostics = error.get("diagnostics", {}).get("diagnostics", {})
    for key in ("redundantConstraints", "conflictingConstraints"):
        constraint_ids = diagnostics.get(key, [])
        if constraint_ids:
            viewer_error[key] = [
                *constraint_ids[:5],
                *([f"+{len(constraint_ids) - 5} more"] if len(constraint_ids) > 5 else []),
            ]
    return viewer_error


def _availability_request_error(availability: SolveAvailability) -> RequestError:
    failure = availability.failed[0]
    return RequestError(
        failure.code,
        failure.message,
        **failure.details,
        **availability.failure_details(),
    )


def _reference(payload: dict[str, Any]) -> Reference:
    try:
        return reference_from_dict(payload)
    except ValueError as error:
        raise RequestError("INVALID_PARAMETER", str(error)) from error


def _camel_key(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.title() for part in tail)


def _wire_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {_camel_key(key): _wire_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_wire_value(item) for item in value]
    if isinstance(value, tuple):
        return [_wire_value(item) for item in value]
    return value


def _board_availability(board: Any) -> dict[str, str]:
    courtyards = [footprint.courtyard_availability for footprint in board.footprints]
    models = [model for footprint in board.footprints for model in footprint.models]
    return {
        "thickness": "available" if board.thickness is not None else "missing",
        "outline": "available",
        "innerCutouts": "available",
        "holes": "available",
        "footprints": "available",
        "courtyards": (
            "available"
            if courtyards and all(status == "present" for status in courtyards)
            else "partial"
            if any(status == "present" for status in courtyards)
            else "missing"
        ),
        "models": (
            "missing"
            if not models
            else "unresolved"
            if any(model.unresolved_variables for model in models)
            else "declared"
        ),
    }


def _apply_import(
    graph: Any,
    operation: BoardImportOperation | ImportDelete,
    committed: RegenResult | None,
) -> str | _DeleteResult:
    if isinstance(operation, ImportDelete):
        assert committed is not None
        return _apply_delete(graph, operation, committed)
    current = graph.nodes.get(operation.assetId)
    if operation.action is Action.REFRESH:
        if not isinstance(current, BoardImport):
            raise RequestError("UNKNOWN_ID", f"unknown board import: {operation.assetId}")
        expected_format = "kicad_pcb"
        if operation.format is not None and operation.format != expected_format:
            raise RequestError(
                "INVALID_PARAMETER",
                f"refresh format {operation.format!r} differs from asset format {expected_format!r}",
                expected=expected_format,
                actual=operation.format,
            )
        source_path = operation.path or current.source_path
    else:
        if operation.format != "kicad_pcb":
            raise RequestError("INVALID_PARAMETER", "only kicad_pcb imports are supported")
        if current is not None:
            raise RequestError("DUPLICATE_ID", f"duplicate id: {operation.assetId}")
        assert operation.path is not None
        source_path = operation.path
    resolved, board = read_kicad_board(source_path)
    snapshot = board.to_dict()
    for key in ("source_digest", "parser_version", "warnings", "errors"):
        snapshot.pop(key)
    imported = BoardImport(
        id=operation.assetId,
        source_path=str(resolved),
        source_digest=board.source_digest,
        parser_version=board.parser_version,
        mechanics=_wire_value(snapshot),
        availability=_board_availability(board),
        warnings=list(board.warnings),
        errors=[_wire_value(asdict(error)) for error in board.errors],
    )
    mounting_holes(imported)
    graph.nodes[operation.assetId] = imported
    return operation.assetId


@dataclass(frozen=True)
class _DeleteResult:
    target_id: str
    deleted: tuple[dict[str, str], ...]
    before_parts: tuple[dict[str, Any], ...]


def _part_state(result: RegenResult) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "id": part.part_id,
            "terminalFeatureId": part.terminal_feature,
            "volume": part.shape.volume,
            "area": part.shape.area,
        }
        for part in terminal_parts(result, role=None)
    )


def _node_kind(node: Any) -> str:
    return {
        "BoardImport": "board_import",
        "PcbBase": "pcb_base",
        "PcbLid": "pcb_lid",
        "PortCutout": "port_cutout",
        "ScrewClosure": "screw_closure",
    }.get(type(node).__name__, type(node).__name__.lower())


def _delete_target(operation: DeleteOperation) -> str:
    if isinstance(operation, ImportDelete):
        return operation.assetId
    if isinstance(operation, SketchDelete):
        return operation.sketchId
    return operation.featureId


def _delete_target_kind(operation: DeleteOperation) -> tuple[type, str]:
    if isinstance(operation, ImportDelete):
        return BoardImport, "board import"
    if isinstance(operation, SketchDelete):
        return Sketch, "sketch"
    return Feature, "feature"


def _apply_delete(graph: Any, operation: DeleteOperation, committed: RegenResult) -> _DeleteResult:
    target = _delete_target(operation)
    expected, noun = _delete_target_kind(operation)
    if not isinstance(graph.nodes.get(target), expected):
        raise RequestError("UNKNOWN_ID", f"unknown {noun}: {target}")

    ordered = graph.ordered()
    dependent_ids = {
        item.id for item in graph.transitive_dependents(target, committed.predecessors)
    }
    dependents = [
        {"id": item.id, "kind": _node_kind(item)} for item in ordered if item.id in dependent_ids
    ]
    if dependents and not operation.cascade:
        raise RequestError(
            "HAS_DEPENDENTS",
            f"{target} has dependents",
            dependents=dependents,
            hint="delete the listed dependents first or pass cascade: true",
        )

    deleted_ids = dependent_ids | {target}
    deleted = tuple(
        {"id": item.id, "kind": _node_kind(item)} for item in ordered if item.id in deleted_ids
    )
    before_parts = _part_state(committed)
    for item in deleted:
        graph.delete(item["id"])
    return _DeleteResult(target, deleted, before_parts)


def _applied_result(
    applied: str | _DeleteResult,
) -> tuple[str, list[str], _DeleteResult | None]:
    if isinstance(applied, _DeleteResult):
        return applied.target_id, [item["id"] for item in applied.deleted], applied
    return applied, [applied], None


def _deletion_result_fields(
    deletion: _DeleteResult | None, result: RegenResult
) -> tuple[dict[str, Any], dict[str, str] | None]:
    if deletion is None:
        return {}, None
    after_parts = {item["id"]: item for item in _part_state(result)}
    before_parts = {item["id"]: item for item in deletion.before_parts}
    removed_parts = [part_id for part_id in before_parts if part_id not in after_parts]
    changed_parts = [
        part_id
        for part_id in before_parts
        if part_id in after_parts and before_parts[part_id] != after_parts[part_id]
    ]
    target = next(item for item in deletion.deleted if item["id"] == deletion.target_id)
    summary = f"deleted {_named(target['kind'].replace('_', ' '), target['id'])}"
    dependents = [item["id"] for item in deletion.deleted if item is not target]
    if dependents:
        summary += (
            f" with {'dependent' if len(dependents) == 1 else 'dependents'} {', '.join(dependents)}"
        )
    fields = {
        "deleted": list(deletion.deleted),
        "summary": summary,
        "partsRemoved": removed_parts,
        "partsChanged": changed_parts,
    }
    return fields, {"featureId": deletion.target_id, "summary": summary}


def _sketch_rejection(result: Any, model: ConstrainedSketch) -> RequestError:
    messages = {
        "redundant": ("SKETCH_REDUNDANT", "constrained sketch was rejected: redundant"),
        "conflicting": ("SKETCH_CONFLICTING", "constrained sketch was rejected: conflicting"),
        "did_not_converge": ("SKETCH_DID_NOT_CONVERGE", "constrained sketch did not converge"),
        "branch_flip": ("SKETCH_BRANCH_FLIP", "constrained sketch changed branch"),
    }
    message = messages.get(
        result.status.value, ("INVALID_PARAMETER", "invalid constrained sketch input")
    )[1]
    entity_ids = {
        entity.id for entity in (*model.points, *model.lines, *model.arcs, *model.circles)
    }
    details = {
        "diagnostics": result_payload(model),
        **diagnostic_geometry_details(result, entity_ids),
    }
    if result.status.value == "invalid_input":
        return RequestError("INVALID_PARAMETER", message, **details)
    if result.status.value == "redundant":
        return RequestError("SKETCH_REDUNDANT", message, **details)
    if result.status.value == "conflicting":
        return RequestError("SKETCH_CONFLICTING", message, **details)
    if result.status.value == "did_not_converge":
        codes = {error.code for error in result.errors}
        if "SOLVE_TIMEOUT" in codes:
            return RequestError("SKETCH_SOLVE_TIMEOUT", message, **details)
        if "SOLVER_FAILED" in codes:
            return RequestError("SKETCH_SOLVER_FAILED", message, **details)
        return RequestError("SKETCH_DID_NOT_CONVERGE", message, **details)
    return RequestError("SKETCH_BRANCH_FLIP", message, **details)


def _accept_constrained_sketch(
    model: ConstrainedSketch, solver: Callable[[Any], Any] = solve_sketch
) -> None:
    try:
        result = (
            model.solve(solver) if type(model).solve is _CONSTRAINED_SKETCH_SOLVE else model.solve()
        )
    except ImportError as error:
        raise RequestError("SKETCH_SOLVER_UNAVAILABLE", str(error)) from error
    if result.status.value not in {"under_constrained", "fully_constrained"}:
        raise _sketch_rejection(result, model)


def _sketch_failures(
    graph: Any, solver: Callable[[Any], Any] = solve_sketch
) -> dict[str, RequestError | GraphError]:
    failures = {}
    for node in graph.nodes.values():
        if not isinstance(node, Sketch):
            continue
        try:
            if node.constrained is not None:
                _accept_constrained_sketch(node.constrained, solver)
            else:
                validate_sketch_profiles(node)
        except (RequestError, GraphError) as error:
            failures[node.id] = error
    return failures


def _sketch_failure(feature_id: str, error: RequestError | GraphError) -> SketchFailure:
    if isinstance(error, RequestError):
        return SketchFailure(feature_id, error.code, str(error), error.details)
    details = {key: value for key, value in error.error.items() if key not in {"code", "message"}}
    return SketchFailure(feature_id, error.error["code"], str(error), details)


def _solve_constrained_sketches(
    graph: Any, solver: Callable[[Any], Any] = solve_sketch
) -> SolveAvailability:
    failures = _sketch_failures(graph, solver)
    failed = tuple(_sketch_failure(feature_id, error) for feature_id, error in failures.items())
    unavailable = []
    reported = set(failures)
    for feature_id in failures:
        for dependent in graph.transitive_dependents(feature_id, {}):
            if dependent.id in reported:
                continue
            reported.add(dependent.id)
            unavailable.append(UnavailableFeature(dependent.id, feature_id))
    solved = tuple(
        node.id
        for node in graph.nodes.values()
        if isinstance(node, Sketch) and node.constrained is not None and node.id not in failures
    )
    return SolveAvailability(solved, failed, tuple(unavailable))


_MAX_VALID_DIMENSION_IDS = 50


def _updated_dimension(
    dimension_id: str,
    value: float,
    dimensions: dict[str, Dimension],
    driving_ids: list[str],
) -> Dimension:
    dimension = dimensions.get(dimension_id)
    if dimension is None:
        valid = driving_ids[:_MAX_VALID_DIMENSION_IDS]
        raise RequestError(
            "UNKNOWN_ID",
            f"unknown sketch dimension: {dimension_id}; valid driving dimension ids: {valid}",
            dimensionId=dimension_id,
            validDrivingDimensionIds=valid,
            validDrivingDimensionCount=len(driving_ids),
            validDrivingDimensionIdsTruncated=len(driving_ids) > len(valid),
        )
    if not dimension.driving:
        raise RequestError(
            "INVALID_PARAMETER",
            f"sketch dimension is reference-only: {dimension_id}",
            field=f"dimensionValues.{dimension_id}",
            dimensionId=dimension_id,
        )
    if dimension.kind.value in {"distance", "radius", "diameter"} and value <= 0:
        raise RequestError(
            "INVALID_PARAMETER",
            f"{dimension.kind.value} dimension must be positive: {dimension_id}",
            field=f"dimensionValues.{dimension_id}",
            dimensionId=dimension_id,
        )
    return replace(dimension, value=value)


def _replace_dimension_values(
    model: ConstrainedSketch, values: dict[str, float]
) -> tuple[Dimension, ...]:
    dimensions = {dimension.id: dimension for dimension in model.dimensions}
    driving_ids = sorted(dimension.id for dimension in model.dimensions if dimension.driving)
    for dimension_id, value in values.items():
        dimensions[dimension_id] = _updated_dimension(dimension_id, value, dimensions, driving_ids)
    return tuple(dimensions[dimension.id] for dimension in model.dimensions)


def _updated_circular_seeds(
    model: ConstrainedSketch, dimensions: tuple[Dimension, ...], updated_ids: set[str]
) -> ConstrainedSketch:
    circular_radii = {
        dimension.entities[0]: (
            dimension.value / 2 if dimension.kind.value == "diameter" else dimension.value
        )
        for dimension in dimensions
        if dimension.id in updated_ids
        and dimension.kind.value in {"radius", "diameter"}
        and len(dimension.entities) == 1
    }
    return replace(
        model,
        arcs=tuple(
            replace(arc, radius=circular_radii[arc.id]) if arc.id in circular_radii else arc
            for arc in model.arcs
        ),
        circles=tuple(
            replace(circle, radius=circular_radii[circle.id])
            if circle.id in circular_radii
            else circle
            for circle in model.circles
        ),
    )


def _updated_dimensions(model: ConstrainedSketch, values: dict[str, float]) -> ConstrainedSketch:
    dimensions = _replace_dimension_values(model, values)
    model = _updated_circular_seeds(model, dimensions, set(values))
    return replace(model, dimensions=dimensions, solved=None)


def _stored_generated_profile_ids(
    feature: Any, sketch_id: str, generated_ids: set[str]
) -> list[str]:
    if not isinstance(feature, (Extrude, Revolve)) or feature.sketch != sketch_id:
        return []
    return [profile_id for profile_id in feature.profile_ids or () if profile_id in generated_ids]


def _stale_generated_profile_references(
    graph: Any, sketch: Sketch, entities: tuple[Rectangle | Circle | Polyline | Spline, ...]
) -> None:
    if list(entities) == sketch.entities:
        return
    generated_ids = {
        entity.id for entity in sketch.entities if not getattr(entity, "id_explicit", True)
    }
    for feature in graph.nodes.values():
        stale_ids = _stored_generated_profile_ids(feature, sketch.id, generated_ids)
        if stale_ids:
            raise GraphError(
                "REFERENCE_FAILED",
                f"{feature.__class__.__name__.lower()} {feature.id} has stale generated profile ids "
                f"{stale_ids} for sketch {sketch.id}; give the outlines explicit ids to keep "
                "profile references across entity edits",
                feature=feature.id,
                sketch=sketch.id,
                staleProfileIds=stale_ids,
                resolution={"status": "EMPTY"},
                hint="give the outlines explicit ids to keep profile references across entity edits",
            )


def _apply_sketch_update(
    node: Sketch, changes: SketchChanges, solver: Callable[[Any], Any]
) -> None:
    if changes.position is not None:
        node.position = changes.position
    if changes.entities is not None:
        node.replace_entities(list(changes.entities))
    if changes.constrained is not None:
        _accept_constrained_sketch(changes.constrained, solver)
        node.replace_constrained(changes.constrained)
    if changes.dimension_values is None:
        return
    if node.constrained is None:
        raise RequestError(
            "INVALID_PARAMETER",
            "dimensionValues requires a constrained sketch",
            field="dimensionValues",
        )
    constrained = _updated_dimensions(node.constrained, changes.dimension_values)
    _accept_constrained_sketch(constrained, solver)
    node.replace_constrained(constrained)


def _apply_sketch(
    graph: Any,
    operation: SketchCreate | SketchUpdate | SketchDelete,
    committed: RegenResult | None,
    *,
    solver: Callable[[Any], Any] = solve_sketch,
) -> str | _DeleteResult:
    if isinstance(operation, SketchDelete):
        assert committed is not None
        return _apply_delete(graph, operation, committed)
    if isinstance(operation, SketchCreate):
        if operation.constrained is not None:
            _accept_constrained_sketch(operation.constrained, solver)
        if operation.plane.kind != "origin":
            raise RequestError("INVALID_PARAMETER", "only origin planes are supported")
        graph.add(
            Sketch(
                operation.sketchId,
                plane=Plane(operation.plane.name, operation.plane.offset),
                entities=list(operation.entities),
                constrained=operation.constrained,
            )
        )
    else:
        node = graph.nodes.get(operation.sketchId)
        if not isinstance(node, Sketch):
            raise RequestError("UNKNOWN_ID", f"unknown sketch: {operation.sketchId}")
        if operation.set.entities is not None:
            _stale_generated_profile_references(graph, node, operation.set.entities)
        _apply_sketch_update(node, operation.set, solver)
    node = graph.nodes[operation.sketchId]
    validate_sketch(node)
    validate_sketch_profiles(node)
    return operation.sketchId


def _validate_body_target(graph: Any, target: str | None, operation: str) -> None:
    if target not in graph.nodes:
        raise RequestError("UNKNOWN_ID", f"{operation} target feature does not exist")
    if not graph.produces_body(target):
        raise RequestError("INVALID_PARAMETER", f"{operation} target must produce a body")


def _sketch_profile_ids(sketch: Sketch) -> tuple[str, ...]:
    return (
        (sketch.id,)
        if sketch.constrained is not None
        else tuple(entity.id for entity in sketch.entities)
    )


MAX_EXPECTED_PROFILE_IDS = 20


def _profile_id_problems(
    received: tuple[str, ...], expected: tuple[str, ...]
) -> tuple[list[str], list[str]]:
    expected_set = set(expected)
    counts = Counter(received)
    unknown = list(
        dict.fromkeys(profile_id for profile_id in received if profile_id not in expected_set)
    )
    duplicates = [profile_id for profile_id, count in counts.items() if count > 1]
    return unknown, duplicates


def _invalid_profile_ids(
    sketch: Sketch,
    expected: tuple[str, ...],
    unknown: list[str],
    duplicates: list[str],
) -> RequestError:
    shown_unknown = unknown[:MAX_EXPECTED_PROFILE_IDS]
    shown_duplicates = duplicates[:MAX_EXPECTED_PROFILE_IDS]
    problems = []
    if shown_unknown:
        problems.append(f"unknown ids {json.dumps(shown_unknown, separators=(',', ':'))}")
    if shown_duplicates:
        problems.append(f"duplicate ids {json.dumps(shown_duplicates, separators=(',', ':'))}")
    shown = expected[:MAX_EXPECTED_PROFILE_IDS]
    return RequestError(
        "INVALID_PARAMETER",
        f"profileIds must be a unique subset of the profile ids for sketch '{sketch.id}'; "
        f"{'; '.join(problems)}. omit profileIds to use all closed loops in graph order.",
        expectedProfileIds=list(shown),
        expectedProfileIdsTruncated=len(expected) > len(shown),
        unknownProfileIds=shown_unknown,
        unknownProfileIdsTruncated=len(unknown) > len(shown_unknown),
        duplicateProfileIds=shown_duplicates,
        duplicateProfileIdsTruncated=len(duplicates) > len(shown_duplicates),
    )


def _resolved_profile_ids(profile: ExtrudeProfile, sketch: Sketch) -> tuple[str, ...]:
    expected = _sketch_profile_ids(sketch)
    if profile.profileIds is None:
        return expected
    unknown, duplicates = _profile_id_problems(profile.profileIds, expected)
    if unknown or duplicates:
        raise _invalid_profile_ids(sketch, expected, unknown, duplicates)
    selected = set(profile.profileIds)
    return tuple(profile_id for profile_id in expected if profile_id in selected)


def _apply_extrude(graph: Any, operation: ExtrudeCreate | ExtrudeUpdate) -> str:
    if isinstance(operation, ExtrudeUpdate):
        node = graph.nodes.get(operation.featureId)
        if not isinstance(node, Extrude):
            raise RequestError("UNKNOWN_ID", f"unknown extrude: {operation.featureId}")
        node.height = operation.set.distance
        return operation.featureId

    parameters = operation.parameters
    if parameters.operation not in {"new_body", "join"} or parameters.direction != "normal":
        raise RequestError(
            "INVALID_PARAMETER", "only normal new_body and join extrudes are supported"
        )
    target = parameters.target.featureId if parameters.target is not None else None
    if parameters.operation == "join":
        _validate_body_target(graph, target, "join")
    elif target is not None:
        raise RequestError("INVALID_PARAMETER", "new_body extrudes do not accept a target")
    sketch = graph.nodes.get(parameters.profile.sketchId)
    if not isinstance(sketch, Sketch):
        raise RequestError("UNKNOWN_ID", "extrude profile sketch does not exist")
    profile_ids = _resolved_profile_ids(parameters.profile, sketch)
    graph.add(
        Extrude(
            operation.featureId,
            sketch=sketch.id,
            height=parameters.distance,
            operation=parameters.operation,
            target=target,
            profile_ids=profile_ids,
        )
    )
    return operation.featureId


def _apply_revolve(graph: Any, operation: RevolveCreate) -> str:
    parameters = operation.parameters
    sketch = graph.nodes.get(parameters.profile.sketchId)
    if not isinstance(sketch, Sketch):
        raise RequestError("UNKNOWN_ID", "revolve profile sketch does not exist")
    profile_ids = _resolved_profile_ids(parameters.profile, sketch)
    if len(profile_ids) != 1:
        raise GraphError(
            "REVOLVE_MULTIPLE_PROFILES",
            f"revolve sketch {sketch.id} must select exactly one profile",
            sketch=sketch.id,
            profileCount=len(profile_ids),
        )
    graph.add(
        Revolve(
            operation.featureId,
            sketch=sketch.id,
            axis=parameters.axis,
            operation=parameters.operation,
            profile_ids=profile_ids,
        )
    )
    return operation.featureId


def _apply_target_update(
    graph: Any,
    operation: CutUpdate,
    feature_type: type[Cut],
) -> str:
    node = graph.nodes.get(operation.featureId)
    if not isinstance(node, feature_type):
        raise RequestError("UNKNOWN_ID", f"unknown {operation.kind}: {operation.featureId}")
    target = operation.set.target.featureId
    _validate_body_target(graph, target, operation.kind)
    node.target = target
    return operation.featureId


def _apply_edge_treatment(
    graph: Any,
    operation: FilletCreate | ChamferCreate | FilletUpdate | ChamferUpdate,
    committed: RegenResult | None = None,
) -> str:
    feature_type = Fillet if operation.kind == "fillet" else Chamfer
    parameter = "radius" if operation.kind == "fillet" else "distance"
    if isinstance(operation, (FilletUpdate, ChamferUpdate)):
        node = graph.nodes.get(operation.featureId)
        if not isinstance(node, feature_type):
            raise RequestError("UNKNOWN_ID", f"unknown {operation.kind}: {operation.featureId}")
        setattr(node, parameter, operation.set.size)
        return operation.featureId

    parameters = operation.parameters
    target = parameters.target.featureId
    _validate_body_target(graph, target, operation.kind)
    assert committed is not None
    edges = []
    for reference in parameters.edges:
        resolution = committed.resolve(reference, before_feature=operation.featureId)
        source = (
            resolution.candidates[0].key.source
            if reference.source is None and len(resolution.candidates) == 1
            else reference.source
        )
        edges.append(replace(reference, source=source))
    graph.add(
        feature_type(
            operation.featureId,
            target=target,
            edges=edges,
            **{parameter: parameters.size},
        )
    )
    return operation.featureId


def _apply_cut(graph: Any, operation: CutCreate | CutUpdate) -> str:
    if isinstance(operation, CutUpdate):
        return _apply_target_update(graph, operation, Cut)
    parameters = operation.parameters
    _validate_body_target(graph, parameters.target.featureId, "cut")
    if not isinstance(graph.nodes.get(parameters.tool.sketchId), Sketch):
        raise RequestError("UNKNOWN_ID", "cut tool sketch does not exist")
    graph.add(
        Cut(
            operation.featureId,
            target=parameters.target.featureId,
            tool=parameters.tool.sketchId,
            depth=parameters.depth,
            flip=parameters.flip,
        )
    )
    return operation.featureId


def _apply_hole_update(graph: Any, operation: HoleUpdate) -> str:
    node = graph.nodes.get(operation.featureId)
    if not isinstance(node, Hole):
        raise RequestError("UNKNOWN_ID", f"unknown hole: {operation.featureId}")
    if operation.set.target is not None:
        target = operation.set.target.featureId
        _validate_body_target(graph, target, "hole")
        node.target = target
    if operation.set.pattern is None:
        return operation.featureId
    changes = operation.set.pattern
    expected = (
        CircularHolePattern
        if isinstance(changes, CircularHolePatternChanges)
        else LinearHolePattern
    )
    if not isinstance(node.pattern, expected):
        raise RequestError(
            "INVALID_PARAMETER",
            "hole pattern update kind must match the stored pattern",
            feature=operation.featureId,
            field="pattern.kind",
        )
    node.pattern = replace(
        node.pattern,
        **{
            field.name: getattr(changes, field.name)
            for field in fields(changes)
            if getattr(changes, field.name) is not None
        },
    )
    return operation.featureId


def _apply_hole(graph: Any, operation: HoleCreate | HoleUpdate) -> str:
    if isinstance(operation, HoleUpdate):
        return _apply_hole_update(graph, operation)
    parameters = operation.parameters
    _validate_body_target(graph, parameters.target.featureId, "hole")
    graph.add(
        Hole(
            operation.featureId,
            target=parameters.target.featureId,
            diameter=parameters.diameter,
            depth=parameters.depth,
            positions=parameters.positions,
            pattern=parameters.pattern,
        )
    )
    return operation.featureId


def _require_board(graph: Any, board_id: str, feature: str) -> BoardImport:
    board = graph.nodes.get(board_id)
    if not isinstance(board, BoardImport):
        raise RequestError(
            "INVALID_PARAMETER",
            f"{feature} board must reference a board import",
            feature=feature,
            parameter="board",
        )
    return board


def _apply_pcb_base(graph: Any, operation: PcbBaseCreate | PcbBaseUpdate) -> str:
    if isinstance(operation, PcbBaseUpdate):
        node = graph.nodes.get(operation.featureId)
        if not isinstance(node, PcbBase):
            raise RequestError("UNKNOWN_ID", f"unknown pcb_base: {operation.featureId}")
        mapping = {
            "wallThickness": "wall_thickness",
            "floorThickness": "floor_thickness",
            "clearance": "clearance",
            "wallHeightAboveBoard": "wall_height_above_board",
            "boardSeatHeight": "board_seat_height",
        }
        for name, value in asdict(operation.set).items():
            if value is not None:
                setattr(node, mapping[name], value)
        return operation.featureId
    parameters = operation.parameters
    _require_board(graph, parameters.board.featureId, operation.featureId)
    graph.add(
        PcbBase(
            operation.featureId,
            board=parameters.board.featureId,
            wall_thickness=parameters.wallThickness,
            floor_thickness=parameters.floorThickness,
            clearance=parameters.clearance,
            wall_height_above_board=parameters.wallHeightAboveBoard,
            board_seat_height=parameters.boardSeatHeight,
        )
    )
    return operation.featureId


def _validate_standoff_diameters(feature: str, boss: float, pilot: float) -> None:
    if pilot >= boss:
        raise RequestError(
            "INVALID_PARAMETER",
            f"standoffs {feature} pilotDiameter must be smaller than bossDiameter",
            feature=feature,
            parameter="pilotDiameter",
        )


def _apply_standoffs(graph: Any, operation: StandoffsCreate | StandoffsUpdate) -> str:
    if isinstance(operation, StandoffsUpdate):
        node = graph.nodes.get(operation.featureId)
        if not isinstance(node, Standoffs):
            raise RequestError("UNKNOWN_ID", f"unknown standoffs: {operation.featureId}")
        changes = asdict(operation.set)
        boss = changes["bossDiameter"] or node.boss_diameter
        pilot = changes["pilotDiameter"] or node.pilot_diameter
        _validate_standoff_diameters(node.id, boss, pilot)
        node.boss_diameter = boss
        node.pilot_diameter = pilot
        return operation.featureId
    parameters = operation.parameters
    board = _require_board(graph, parameters.board.featureId, operation.featureId)
    target = graph.nodes.get(parameters.target.featureId)
    if not isinstance(target, PcbBase):
        raise RequestError(
            "INVALID_PARAMETER",
            "standoffs target must reference a pcb_base",
            feature=operation.featureId,
            parameter="target",
        )
    if target.board != board.id:
        raise RequestError(
            "INVALID_PARAMETER",
            "standoffs board must match the pcb_base board",
            feature=operation.featureId,
            parameter="board",
        )
    _validate_standoff_diameters(
        operation.featureId, parameters.bossDiameter, parameters.pilotDiameter
    )
    graph.add(
        Standoffs(
            operation.featureId,
            target=target.id,
            board=board.id,
            selection=parameters.selection.mode,
            hole_ids=parameters.selection.holeIds,
            boss_diameter=parameters.bossDiameter,
            pilot_diameter=parameters.pilotDiameter,
        )
    )
    return operation.featureId


def _terminal_base_source(committed: RegenResult, base: str) -> str:
    terminals = {
        terminal for body, terminal in committed.body_terminals.items() if body.feature == base
    }
    if len(terminals) != 1:
        raise RequestError(
            "BODY_LINEAGE_DIVERGED",
            f"pcb_base {base} does not have one terminal outline source",
            feature=base,
        )
    return next(iter(terminals))


def _set_lid_outline_source(node: PcbLid, requested: str | None, committed: RegenResult) -> None:
    source = _terminal_base_source(committed, node.base)
    if requested is not None and requested != source:
        raise RequestError(
            "INVALID_PARAMETER",
            "outlineSource must reference the current terminal base feature",
            feature=node.id,
            parameter="outlineSource",
            currentSource=node.outline_source,
            legalSource=source,
        )
    node.outline_source = source


def _apply_pcb_lid(
    graph: Any,
    operation: PcbLidCreate | PcbLidUpdate,
    committed: RegenResult,
) -> str:
    if isinstance(operation, PcbLidUpdate):
        node = graph.nodes.get(operation.featureId)
        if not isinstance(node, PcbLid):
            raise RequestError("UNKNOWN_ID", f"unknown pcb_lid: {operation.featureId}")
        if operation.set.thickness is not None:
            node.thickness = operation.set.thickness
        if operation.set.fitGap is not None:
            node.fit_gap = operation.set.fitGap
        if operation.set.lip is not None:
            node.lip_depth = operation.set.lip.depth
            node.lip_width = operation.set.lip.width
        if operation.set.outlineSource is not None:
            _set_lid_outline_source(node, operation.set.outlineSource.featureId, committed)
        return operation.featureId

    parameters = operation.parameters
    base = graph.nodes.get(parameters.base.featureId)
    if not isinstance(base, PcbBase):
        raise RequestError(
            "INVALID_PARAMETER",
            "pcb_lid base must reference a pcb_base",
            feature=operation.featureId,
            parameter="base",
        )
    node = PcbLid(
        operation.featureId,
        base=base.id,
        thickness=parameters.thickness,
        fit_gap=parameters.fitGap,
        lip_depth=parameters.lip.depth if parameters.lip else None,
        lip_width=parameters.lip.width if parameters.lip else None,
    )
    _set_lid_outline_source(node, None, committed)
    graph.add(node)
    return operation.featureId


def _update_port_cutout(graph: Any, operation: PortCutoutUpdate) -> str:
    node = graph.nodes.get(operation.featureId)
    if not isinstance(node, PortCutout):
        raise RequestError("UNKNOWN_ID", f"unknown port_cutout: {operation.featureId}")
    changes = operation.set
    if changes.footprint is not None:
        node.footprint = changes.footprint
        node.footprint_id = None
    if changes.wall is not None:
        node.wall = changes.wall
    if changes.clearance is not None:
        node.clearance = changes.clearance
    if changes.envelope is not None:
        node.envelope_source = changes.envelope.source
        node.envelope_width = changes.envelope.width
        node.envelope_height = changes.envelope.height
        node.bottom_offset = changes.envelope.bottomOffset
    return operation.featureId


def _create_port_cutout(graph: Any, operation: PortCutoutCreate) -> str:
    parameters = operation.parameters
    base = graph.nodes.get(parameters.base.featureId)
    if not isinstance(base, PcbBase):
        raise RequestError(
            "INVALID_PARAMETER",
            "port_cutout base must reference a pcb_base",
            feature=operation.featureId,
            parameter="base",
        )
    envelope = parameters.envelope
    graph.add(
        PortCutout(
            operation.featureId,
            base=base.id,
            footprint=parameters.footprint,
            wall=parameters.wall,
            clearance=parameters.clearance,
            envelope_source=envelope.source,
            envelope_width=envelope.width,
            envelope_height=envelope.height,
            bottom_offset=envelope.bottomOffset,
        )
    )
    return operation.featureId


def _apply_port_cutout(graph: Any, operation: PortCutoutCreate | PortCutoutUpdate) -> str:
    if isinstance(operation, PortCutoutUpdate):
        return _update_port_cutout(graph, operation)
    return _create_port_cutout(graph, operation)


def _validate_screw_closure_diameters(
    feature: str, pilot: float, boss: float, clearance: float
) -> None:
    if pilot >= boss:
        raise RequestError(
            "INVALID_PARAMETER",
            "basePilotDiameter must be smaller than bossDiameter",
            feature=feature,
            parameter="basePilotDiameter",
        )
    if clearance <= pilot:
        raise RequestError(
            "INVALID_PARAMETER",
            "lidClearanceDiameter must exceed basePilotDiameter",
            feature=feature,
            parameter="lidClearanceDiameter",
        )


def _apply_screw_closure(graph: Any, operation: ScrewClosureCreate | ScrewClosureUpdate) -> str:
    if isinstance(operation, ScrewClosureUpdate):
        node = graph.nodes.get(operation.featureId)
        if not isinstance(node, ScrewClosure):
            raise RequestError("UNKNOWN_ID", f"unknown screw_closure: {operation.featureId}")
        changes = operation.set
        pilot = changes.basePilotDiameter or node.base_pilot_diameter
        boss = changes.bossDiameter or node.boss_diameter
        clearance = changes.lidClearanceDiameter or node.lid_clearance_diameter
        _validate_screw_closure_diameters(operation.featureId, pilot, boss, clearance)
        if changes.points is not None:
            node.points = changes.points
        node.base_pilot_diameter = pilot
        node.boss_diameter = boss
        node.lid_clearance_diameter = clearance
        return operation.featureId

    parameters = operation.parameters
    base = graph.nodes.get(parameters.base.featureId)
    lid = graph.nodes.get(parameters.lid.featureId)
    if not isinstance(base, PcbBase):
        raise RequestError(
            "INVALID_PARAMETER",
            "screw_closure base must reference a pcb_base",
            feature=operation.featureId,
            parameter="base",
        )
    if not isinstance(lid, PcbLid) or lid.base != base.id:
        raise RequestError(
            "INVALID_PARAMETER",
            "screw_closure lid must reference a pcb_lid for its base",
            feature=operation.featureId,
            parameter="lid",
        )
    _validate_screw_closure_diameters(
        operation.featureId,
        parameters.basePilotDiameter,
        parameters.bossDiameter,
        parameters.lidClearanceDiameter,
    )
    graph.add(
        ScrewClosure(
            operation.featureId,
            base=base.id,
            lid=lid.id,
            points=parameters.points,
            base_pilot_diameter=parameters.basePilotDiameter,
            boss_diameter=parameters.bossDiameter,
            lid_clearance_diameter=parameters.lidClearanceDiameter,
        )
    )
    return operation.featureId


def _apply_feature(
    graph: Any, operation: MutationOperation, committed: RegenResult | None
) -> str | _DeleteResult:
    if isinstance(operation, FeatureDelete):
        assert committed is not None
        return _apply_delete(graph, operation, committed)
    applier = {
        ExtrudeCreate: _apply_extrude,
        ExtrudeUpdate: _apply_extrude,
        RevolveCreate: _apply_revolve,
        CutCreate: _apply_cut,
        CutUpdate: _apply_cut,
        HoleCreate: _apply_hole,
        HoleUpdate: _apply_hole,
        FilletCreate: _apply_edge_treatment,
        FilletUpdate: _apply_edge_treatment,
        ChamferCreate: _apply_edge_treatment,
        ChamferUpdate: _apply_edge_treatment,
        PcbBaseCreate: _apply_pcb_base,
        PcbBaseUpdate: _apply_pcb_base,
        StandoffsCreate: _apply_standoffs,
        StandoffsUpdate: _apply_standoffs,
        PcbLidCreate: _apply_pcb_lid,
        PcbLidUpdate: _apply_pcb_lid,
        PortCutoutCreate: _apply_port_cutout,
        PortCutoutUpdate: _apply_port_cutout,
        ScrewClosureCreate: _apply_screw_closure,
        ScrewClosureUpdate: _apply_screw_closure,
    }[type(operation)]
    if isinstance(
        operation,
        (FilletCreate, ChamferCreate, PcbLidCreate, PcbLidUpdate),
    ):
        assert committed is not None
        return applier(graph, operation, committed)
    return applier(graph, operation)


def _outline_point_ids(entity: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        point.get("id", f"p{index}") if isinstance(point, dict) else f"p{index}"
        for index, point in enumerate(entity["points"])
    )


def _entity_segments(entities: list[dict[str, Any]]) -> dict[str, list[str]]:
    segments = {
        "rectangle": lambda _entity: ["bottom", "right", "top", "left"],
        "circle": lambda _entity: ["circle"],
        "polyline": lambda entity: [
            f"seg-{start}-{end}"
            for start, end in zip(
                _outline_point_ids(entity),
                (*_outline_point_ids(entity)[1:], _outline_point_ids(entity)[0]),
                strict=True,
            )
        ],
        "spline": lambda _entity: ["spline"],
    }
    return {
        _entity_id(entity, index): segments[entity["kind"]](entity)
        for index, entity in enumerate(entities)
    }


def _kind(node: Any) -> str:
    """Human words for a graph node kind, for the viewer."""
    if isinstance(node, Extrude) and node.operation == "join":
        return "join extrude"
    if isinstance(node, Hole):
        return "hole feature"
    if isinstance(node, PcbBase):
        return "pcb base"
    if isinstance(node, Standoffs):
        return "standoffs"
    if isinstance(node, PcbLid):
        return "pcb lid"
    if isinstance(node, PortCutout):
        return "port cutout"
    if isinstance(node, ScrewClosure):
        return "screw closure"
    if isinstance(node, BoardImport):
        return "board import"
    return type(node).__name__.lower()


def _hole_names(node: Hole) -> list[str | None]:
    from .hole import hole_positions

    return [position.name for position in hole_positions(node)]


def _infer_update_kind(payload: dict[str, Any], node: Any) -> tuple[str, str | None]:
    return _node_kind(node), payload.get("kind")


def _resolve_export_part(
    requested_id: str, available: list[Any], by_id: dict[str, Any], result: RegenResult
) -> Any | None:
    if requested_id in by_id:
        return by_id[requested_id]
    matches = [
        part
        for part in available
        if requested_id
        in {feature["id"] for feature in _lineage(result, part.terminal_feature, part.source_body)}
    ]
    return matches[0] if len(matches) == 1 else None


def _lineage(
    result: RegenResult, feature_id: str, body: BodySource | None = None
) -> tuple[dict[str, Any], ...]:
    """The features that built a terminal part, oldest first."""
    chain = []
    while feature_id:
        node = result.graph.nodes[feature_id]
        entry = {"id": node.id, "kind": _kind(node)}
        if isinstance(node, Hole):
            entry["holes"] = _hole_names(node)
        chain.append(entry)
        feature_id = result.body_predecessors.get(
            (feature_id, body), result.predecessors.get(feature_id, "")
        )
    return tuple(reversed(chain))


def _mm(value: float | tuple[float, ...]) -> str:
    if isinstance(value, tuple):
        return "(" + ", ".join(_mm(coordinate) for coordinate in value) + ")"
    rounded = round(value, 6)
    return f"{0 if rounded == 0 else rounded:g}"


# the fields the update tools change, as the human reads them: a word, and old -> new values
_EDITED_FIELDS: dict[str, tuple[str, Callable[[Any, Any], str]]] = {
    "height": ("distance", lambda was, now: f"{_mm(was)} \u2192 {_mm(now)} mm"),
    "position": ("position", lambda was, now: f"{_mm(was)} \u2192 {_mm(now)} mm"),
    "entities": ("profiles", lambda was, now: f"{len(was)} \u2192 {len(now)}"),
    "wall_thickness": ("wall thickness", lambda was, now: f"{_mm(was)} \u2192 {_mm(now)} mm"),
    "floor_thickness": ("floor thickness", lambda was, now: f"{_mm(was)} \u2192 {_mm(now)} mm"),
    "clearance": ("clearance", lambda was, now: f"{_mm(was)} \u2192 {_mm(now)} mm"),
    "wall_height_above_board": (
        "wall height above board",
        lambda was, now: f"{_mm(was)} \u2192 {_mm(now)} mm",
    ),
    "board_seat_height": ("board seat height", lambda was, now: f"{_mm(was)} \u2192 {_mm(now)} mm"),
    "boss_diameter": ("boss diameter", lambda was, now: f"{_mm(was)} \u2192 {_mm(now)} mm"),
    "pilot_diameter": ("pilot diameter", lambda was, now: f"{_mm(was)} \u2192 {_mm(now)} mm"),
    "radius": ("radius", lambda was, now: f"{_mm(was)} \u2192 {_mm(now)} mm"),
    "distance": ("distance", lambda was, now: f"{_mm(was)} \u2192 {_mm(now)} mm"),
}


def _named(word: str, node_id: str) -> str:
    """ "pcb lid lid" says the id twice; keep the id only when it adds a word."""
    return word if set(node_id.split("_")) <= set(word.split()) else f"{word} {node_id}"


def _enclosure_added(node: Any, result: RegenResult) -> str | None:
    """What an enclosure feature made, in the numbers an engineer checks; None for other kinds."""
    summary = f"added {_named(_kind(node), node.id)}"
    if isinstance(node, PcbBase):
        return (
            f"{summary} for {node.board}: {_mm(node.wall_thickness)} mm walls, "
            f"{_mm(node.clearance)} mm board clearance"
        )
    if isinstance(node, Standoffs):
        from .enclosure import selected_holes

        count = len(selected_holes(node, result.graph.nodes[node.board]))
        return f"{summary} at {count} mounting {'hole' if count == 1 else 'holes'}"
    if isinstance(node, PcbLid):
        lip = f", {_mm(node.lip_depth)} mm deep lip" if node.lip_depth else ""
        return f"{summary}: {_mm(node.thickness)} mm thick, {_mm(node.fit_gap)} mm fit gap{lip}"
    if isinstance(node, ScrewClosure):
        return (
            f"{summary}: {len(node.points)} points, pilot {_mm(node.base_pilot_diameter)} mm, "
            f"boss {_mm(node.boss_diameter)} mm, clearance {_mm(node.lid_clearance_diameter)} mm"
        )
    if isinstance(node, PortCutout):
        port = result.metadata[node.id]
        width, height = (_mm(port["opening"][side]["value"]) for side in ("width", "height"))
        connector = port.get("reference") or port["footprintId"]
        return f"{summary} for {connector}: {width} \u00d7 {height} mm on {port['wall']} wall"
    return None


def _added(node: Any, result: RegenResult) -> str:
    if isinstance(node, BoardImport):
        return f"imported board {Path(node.source_path).name}"
    if enclosure := _enclosure_added(node, result):
        return enclosure
    summary = f"added {_named(_kind(node), node.id)}"
    target = getattr(node, "target", None)
    if target:
        summary += f" on {target}"
    if isinstance(node, Sketch):
        count = len(node.entities)
        summary += f" ({count} {'profile' if count == 1 else 'profiles'})"
    elif isinstance(node, Hole):
        count = len(_hole_names(node))
        summary += f" ({count} {'hole' if count == 1 else 'holes'})"
    elif isinstance(node, Extrude):
        summary += f" ({_mm(node.height)} mm)"
    return summary


def _pattern_edits(old: Any, new: Any) -> list[str]:
    if old == new:
        return []
    if old is None or new is None or type(old) is not type(new):
        before = "none" if old is None else type(old).__name__.removesuffix("HolePattern").lower()
        after = "none" if new is None else type(new).__name__.removesuffix("HolePattern").lower()
        return [f"pattern: {before} → {after}"]
    def plain(before: Any, after: Any) -> str:
        return f"{before} → {after}"

    def mm(before: Any, after: Any) -> str:
        return f"{_mm(before)} → {_mm(after)} mm"

    formats = {
        "center": ("center", mm),
        "radius": ("radius", mm),
        "count": ("count", plain),
        "start_angle": (
            "start angle",
            lambda before, after: f"{_mm(before)} → {_mm(after)} deg",
        ),
        "name_prefix": ("name prefix", plain),
        "start": ("start", mm),
        "direction": ("direction", lambda before, after: f"{_mm(before)} → {_mm(after)}"),
        "pitch": ("pitch", mm),
    }
    edits = []
    for field, (label, display) in formats.items():
        before, after = getattr(old, field, None), getattr(new, field, None)
        if before != after:
            edits.append(f"pattern {label}: {display(before, after)}")
    return edits


def _edited(old: Any, node: Any) -> str:
    if isinstance(node, ScrewClosure):
        return (
            f"updated screw closure: {len(node.points)} points, "
            f"pilot {_mm(node.base_pilot_diameter)} mm, boss {_mm(node.boss_diameter)} mm, "
            f"clearance {_mm(node.lid_clearance_diameter)} mm"
        )
    if isinstance(node, BoardImport):
        same = old.source_digest == node.source_digest
        return f"refreshed board {node.id}, source {'unchanged' if same else 'changed'}"
    edits = _pattern_edits(old.pattern, node.pattern) if isinstance(node, Hole) else []
    for name, (word, values) in _EDITED_FIELDS.items():
        was, now = getattr(old, name, None), getattr(node, name, None)
        if was != now:
            edits.append(f"{word}: {values(was, now)}")
    summary = f"edited {_kind(node)} {node.id}"
    return f"{summary} {', '.join(edits)}" if edits else f"{summary}, no value changed"


def _referenced_hole_members(graph: Any, feature_id: str) -> set[str]:
    return {
        source.name
        for node in graph.nodes.values()
        if isinstance(node, (Fillet, Chamfer))
        for reference in node.edges
        if isinstance((source := reference.source), HoleSurfaceSource)
        and source.feature == feature_id
    }


def _selection_state(graph: Any, node: Any) -> dict[str, Any] | None:
    if isinstance(node, Standoffs) and node.selection == "all_mounting":
        board = graph.nodes.get(node.board)
        if isinstance(board, BoardImport):
            return {
                "selector": "all_mounting",
                "positions": {hole["id"]: hole.get("position") for hole in mounting_holes(board)},
            }
    if isinstance(node, Hole) and node.pattern is not None:
        from .hole import hole_positions

        return {
            "selector": "pattern",
            "positions": {position.name: list(position.point) for position in hole_positions(node)},
            "referenced": _referenced_hole_members(graph, node.id),
        }
    return None


def _selection_snapshot(graph: Any) -> dict[str, dict[str, Any]]:
    return {
        node.id: state
        for node in graph.nodes.values()
        if (state := _selection_state(graph, node)) is not None
    }


def _moved_pattern_members(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    old, new = before["positions"], after["positions"]
    referenced = before["referenced"] | after["referenced"]
    return [
        {
            "name": name,
            "oldPosition": old[name],
            "newPosition": new[name],
        }
        for name in sorted(set(old) & set(new) & referenced)
        if math.dist(old[name], new[name]) > 1e-6
    ]


def _selection_changes(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    changes = []
    # creating or deleting a selector feature is not churn; only a selector that existed
    # before and after can gain, lose, or move holes.
    for feature_id in sorted(set(before) & set(after)):
        old = before[feature_id]["positions"]
        new = after[feature_id]["positions"]
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        moved = (
            _moved_pattern_members(before[feature_id], after[feature_id])
            if before[feature_id]["selector"] == "pattern"
            else []
        )
        if added or removed or moved:
            change = {
                "featureId": feature_id,
                "selector": before[feature_id]["selector"],
                "holesAdded": added,
                "holesRemoved": removed,
            }
            if moved:
                change["holesMoved"] = moved
            changes.append(change)
    return changes


def _pattern_selection_summary(changes: list[dict[str, Any]], prefix: str) -> str | None:
    pattern = next((change for change in changes if change["selector"] == "pattern"), None)
    if pattern is None:
        return None
    labels = [
        f"{member['name']} moved {_mm(tuple(member['oldPosition']))} → "
        f"{_mm(tuple(member['newPosition']))}"
        for member in pattern.get("holesMoved", [])
    ]
    for key, word in (("holesRemoved", "removed"), ("holesAdded", "added")):
        names = pattern[key]
        if names:
            subject = " and ".join(names) if len(names) <= 3 else f"{len(names)} members"
            labels.append(f"{subject} {word}")
    if not labels or len(f"{prefix}, +{len(labels)} more") > 160:
        # a very long feature id leaves no room; keep the plain change summary,
        # the full delta stays in selectionChanges
        return None
    return bounded_summary(f"{prefix}, ", labels)


def _is_board_refresh(operation: MutationOperation) -> bool:
    return isinstance(operation, BoardImportOperation) and operation.action is Action.REFRESH


def _ensure_refresh_geometry(
    operation: MutationOperation, ensure_geometry: Callable[[], None]
) -> None:
    if _is_board_refresh(operation):
        ensure_geometry()


def _refresh_fields(
    operation: MutationOperation,
    before: Any,
    after: Any,
    before_result: RegenResult | None,
    after_result: RegenResult,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if not _is_board_refresh(operation):
        return None, {}
    delta = refresh_delta(before, after, before_result, after_result, operation.assetId)
    return delta, {"boardChanges": delta}


def _change(
    before: Any,
    result: RegenResult,
    changed_id: str,
    before_result: RegenResult | None = None,
    delta: dict[str, Any] | None = None,
) -> dict[str, str]:
    """One line that tells the human what a commit did: added what, or which values changed."""
    node = result.graph.nodes[changed_id]
    old = before.nodes.get(changed_id)
    if (
        isinstance(node, BoardImport)
        and isinstance(old, BoardImport)
        and node.source_digest != old.source_digest
    ):
        delta = delta or refresh_delta(before, result.graph, before_result, result, changed_id)
        return {"featureId": changed_id, "summary": format_summary(delta, changed_id)}
    return {
        "featureId": changed_id,
        "summary": _added(node, result) if old is None else _edited(old, node),
    }


def _mutation_change(
    deletion_change: dict[str, str] | None,
    before: Any,
    result: RegenResult,
    changed_id: str,
    before_result: RegenResult | None,
    delta: dict[str, Any] | None,
) -> dict[str, str]:
    return deletion_change or _change(before, result, changed_id, before_result, delta)


def _edit_record(operation: MutationOperation) -> dict[str, Any] | None:
    feature_id = getattr(operation, "featureId", None)
    kind = getattr(operation, "kind", None)
    values = getattr(operation, "set", None) or getattr(operation, "parameters", None)
    if not isinstance(feature_id, str) or not isinstance(kind, str) or values is None:
        return None
    names = {
        "extrude": {"distance": "distance"},
        "hole": {"diameter": "diameter", "depth": "depth"},
        "fillet": {"size": "radius"},
        "chamfer": {"size": "distance"},
        "pcb_base": {
            "wallThickness": "wallThickness",
            "floorThickness": "floorThickness",
            "clearance": "clearance",
            "wallHeightAboveBoard": "wallHeightAboveBoard",
            "boardSeatHeight": "boardSeatHeight",
        },
        "standoffs": {"bossDiameter": "bossDiameter", "pilotDiameter": "pilotDiameter"},
        "pcb_lid": {"thickness": "thickness", "fitGap": "fitGap"},
        "port_cutout": {"clearance": "clearance"},
        "screw_closure": {
            "basePilotDiameter": "basePilotDiameter",
            "bossDiameter": "bossDiameter",
            "lidClearanceDiameter": "lidClearanceDiameter",
        },
    }.get(kind, {})
    fields = {
        public: {"value": value, "unit": "mm"}
        for internal, public in names.items()
        if isinstance((value := getattr(values, internal, None)), (int, float))
        and not isinstance(value, bool)
    }
    if not fields:
        return None
    from .viewer.scene import MAX_REPAIR_FIELDS

    return {
        "featureId": feature_id,
        "kind": kind,
        "fields": dict(list(fields.items())[:MAX_REPAIR_FIELDS]),
    }


def _step_validation_feature_ids(
    operation: MutationOperation, result: RegenResult, changed_id: str
) -> set[str]:
    if isinstance(operation, SketchUpdate):
        return {
            node.id
            for node in result.graph.nodes.values()
            if isinstance(node, Revolve) and node.sketch == operation.sketchId
        }
    if isinstance(
        operation,
        (RevolveCreate, FilletCreate, FilletUpdate, ChamferCreate, ChamferUpdate),
    ):
        return {changed_id}
    return set()


def _scene_parts(result: RegenResult, assembly: Assembly | None = None) -> Iterable[ScenePart]:
    from .viewer import SceneOccurrence, ScenePart

    # body creation order, not regen order: a later feature on one body must not move its row or color
    order = {feature: index for index, feature in enumerate(result.graph.nodes)}
    parts = terminal_parts(result, role=None)
    definitions = {}
    for part in sorted(parts, key=lambda p: (order[p.source_body.feature], p.part_id)):
        features = _lineage(result, part.terminal_feature, part.source_body)
        definitions[part.part_id] = ScenePart(
            part.part_id,
            part.shape,
            role=part.role,
            label=part.label,
            source_body=part.source_body,
            terminal_feature=part.terminal_feature,
            area=part.shape.area,
            volume=part.shape.volume,
            features=features,
            facts=part_facts(result, part.source_body.feature),
        )
    if not assembly or not assembly.occurrences:
        yield from definitions.values()
        return
    for occurrence in sorted(assembly.occurrences.values(), key=lambda item: item.id):
        yield SceneOccurrence(occurrence.id, definitions[occurrence.part_id], occurrence.transform)


def _viewer_assembly_state(result: RegenResult, assembly: Assembly) -> dict[str, Any] | None:
    if not assembly.mates:
        return None
    from .assembly_solver import solve_assembly

    solved = solve_assembly(
        assembly,
        resolve_occurrences(assembly, terminal_parts(result, role=None)),
        result.provenance,
    )
    return {
        "solveStatus": solved.solve_status,
        "underConstrainedOccurrenceIds": solved.under_constrained_occurrence_ids,
        "conflicts": [],
        "occurrences": [
            {"occurrenceId": occurrence_id, **solved.status_by_occurrence[occurrence_id]}
            for occurrence_id in sorted(assembly.occurrences)
        ],
        "mates": [
            {
                "mateId": mate.id,
                "kind": mate.kind,
                "occurrenceIds": (
                    [mate.occurrence_id]
                    if mate.kind == "ground"
                    else [mate.first.occurrence_id, mate.second.occurrence_id]
                ),
                "status": "satisfied",
                **({"orientation": mate.orientation} if mate.orientation else {}),
                **(
                    {"offset": {"value": mate.offset, "unit": "mm"}}
                    if mate.kind == "planar"
                    else {}
                ),
            }
            for mate in sorted(assembly.mates.values(), key=lambda item: item.id)
        ],
    }


class _ScenePublisher:
    """Moves committed scene work off the MCP response path, in commit order."""

    def __init__(self, store: ViewerSceneStore):
        self._store = store
        self._jobs: queue.Queue[tuple[str, tuple[Any, ...]]] = queue.Queue()
        threading.Thread(target=self._run, name="coalesce-viewer-publisher", daemon=True).start()

    def publish(
        self,
        revision: int,
        result: RegenResult,
        assembly: Assembly,
        before: Any = None,
        changed_id: str | None = None,
        change: dict[str, str] | None = None,
        accepted_edit: dict[str, Any] | None = None,
    ) -> None:
        """Describe the commit when supplied; disk startup publishes without a change."""
        self._jobs.put(
            ("publish", (revision, result, assembly, before, changed_id, change, accepted_edit))
        )

    def publish_assembly_inspection(self, revision: int, rows: list[dict[str, Any]]) -> None:
        self._jobs.put(("publish_assembly_inspection", (revision, rows)))

    def publish_failure(
        self,
        feature_ids: list[str],
        error: dict[str, str],
        attempted_edit: dict[str, Any] | None = None,
    ) -> None:
        self._jobs.put(("publish_failure", (feature_ids, error, attempted_edit)))

    def _run(self) -> None:
        while True:
            method, arguments = self._jobs.get()
            try:
                if method == "publish":
                    revision, result, assembly, before, changed_id, change, accepted_edit = (
                        arguments
                    )
                    if change is None and changed_id:
                        change = _change(before, result, changed_id)
                    self._store.publish(
                        revision,
                        _scene_parts(result, assembly),
                        change,
                        fit_summary(result, revision),
                        accepted_edit,
                        assembly_state=_viewer_assembly_state(result, assembly),
                    )
                elif method == "publish_assembly_inspection":
                    self._store.publish_assembly_inspection(*arguments)
                else:
                    self._store.publish_failure(*arguments)
            except Exception:  # noqa: BLE001 - viewer work must not affect document calls.
                _LOG.exception("viewer publish failed")


def _assembly_threshold(
    requirement: dict[str, Any], field: str, unit: str, default: float | None = None
) -> float | None:
    quantity = requirement.get(field)
    if quantity is None:
        return default
    if not isinstance(quantity, dict) or set(quantity) != {"value", "unit"}:
        raise AssemblyInspectionError(
            "INVALID_PARAMETER", f"{field} must contain only value and unit", field=field
        )
    if quantity["unit"] != unit:
        raise AssemblyInspectionError(
            "UNIT_MISMATCH", f"{field} must use {unit}", field=f"{field}.unit"
        )
    value = quantity["value"]
    if type(value) not in (int, float):
        raise AssemblyInspectionError(
            "INVALID_PARAMETER", f"{field} value must be a number", field=f"{field}.value"
        )
    if not math.isfinite(value):
        raise AssemblyInspectionError(
            "NON_FINITE_VALUE", f"{field} value must be finite", field=f"{field}.value"
        )
    if value < 0:
        raise AssemblyInspectionError(
            "INVALID_PARAMETER", f"{field} value must be non-negative", field=f"{field}.value"
        )
    return float(value)


def _validate_assembly_requirement(requirement: dict[str, Any]) -> None:
    _assembly_threshold(requirement, "maximumOverlapVolume", "mm^3", 0.0)
    _assembly_threshold(requirement, "minimumClearance", "mm")


def _valid_brep_result(regen: Any, requirement: dict[str, Any], revision: int) -> dict[str, Any]:
    parts = terminal_parts(regen)
    valid = bool(parts) and all(BRepCheck_Analyzer(part.shape.wrapped).IsValid() for part in parts)
    status = "PASS" if valid else "FAIL" if parts else "UNKNOWN"
    result = {
        "id": requirement.get("id"),
        "kind": "valid_brep",
        "status": status,
        "measured": {"value": valid, "unit": "boolean"},
        "threshold": {"value": True, "unit": "boolean"},
        "involved": [part.part_id for part in parts],
        "evidence": {"solidCount": len(parts)},
        "revision": revision,
    }
    if not parts:
        result["reason"] = "no manufacturing solids exist"
    return result


class DocumentService:
    """The only writer for one persisted, revisioned document."""

    def __init__(
        self,
        path: str | Path,
        *,
        scene_store: ViewerSceneStore | None = None,
        viewer_url: str | None = None,
        warmup: bool = True,
        kernel_loader: _KernelLoaderInterface | None = None,
        sketch_solve_timeout: float = DEFAULT_SOLVE_TIMEOUT_SECONDS,
    ):
        self.store = DocumentStore(path)
        self._lock = threading.RLock()
        self._document = self.store.load()
        self._regen = None
        self._graph_error: GraphError | None = None
        self._viewer_url = viewer_url
        self._publisher = _ScenePublisher(scene_store) if scene_store is not None else None
        self._kernel_loader = kernel_loader or _KERNEL_LOADER
        self._solver = SketchSolverSession(sketch_solve_timeout)
        self._warmup_started = False
        self._warmup_lock = threading.Lock()
        if warmup:
            self.start_kernel_warmup()

    def _regenerate(self, graph: Any) -> RegenResult:
        if regenerate is not _REGENERATE:
            return regenerate(graph)
        return regenerate(graph, self._solver.solve)

    def start_kernel_warmup(self) -> None:
        with self._warmup_lock:
            if self._warmup_started:
                return
            self._warmup_started = True
            threading.Thread(
                target=self._finish_startup, name="coalesce-kernel", daemon=True
            ).start()

    def _finish_startup(self) -> None:
        try:
            self._kernel_loader.start()
            self._kernel_loader.wait()
            with self._lock:
                if self._regen is not None or self._graph_error is not None:
                    return
                try:
                    result = self._regenerate(self._document.graph)
                except GraphError as error:
                    self._record_graph_error(error)
                    return
                try:
                    self._validate_assembly_references(result, loading=True)
                except GraphError as error:
                    self._record_graph_error(error)
                    return
                self._regen = result
                if self._document.revision > 0:
                    self._publish_regeneration(result)
        except Exception:  # noqa: BLE001 - startup errors must not kill the server
            _LOG.exception("kernel startup failed")

    def _publish_regeneration(self, result: RegenResult) -> None:
        if self._publisher is None:
            return
        availability = result.availability
        if availability.geometry_available:
            self._publisher.publish(self._document.revision, result, self._document.assembly)
            return
        error = _availability_request_error(availability)
        details = _safe_error(
            self._document.revision,
            None,
            error.code,
            str(error),
            operation="regeneration",
            **error.details,
        )["error"]
        self._publisher.publish_failure(
            list(availability.failure_details()["failedFeatureIds"]), _viewer_error(details)
        )

    def _record_graph_error(self, error: GraphError) -> None:
        self._graph_error = error
        if self._publisher is None:
            return
        failure = _graph_error_response(self._document.revision, None, "regeneration", error)
        details = failure["error"]
        feature_ids = [
            value
            for key in ("target", "consumer", "featureId")
            if isinstance((value := details.get(key)), str)
        ]
        self._publisher.publish_failure(
            feature_ids,
            _viewer_error(details),
        )

    def _ensure_geometry(self) -> None:
        self.start_kernel_warmup()
        self._kernel_loader.wait()
        with self._lock:
            if self._regen is not None:
                return
            if self._graph_error is not None:
                raise self._graph_error
            try:
                result = self._regenerate(self._document.graph)
            except GraphError as error:
                self._record_graph_error(error)
                raise
            self._validate_assembly_references(result, loading=True)
            self._regen = result
            if self._document.revision > 0:
                self._publish_regeneration(result)

    def _geometry_failure(self, operation: str) -> dict[str, Any] | None:
        try:
            self._ensure_geometry()
        except GraphError as error:
            return _graph_error_response(self._document.revision, None, operation, error)
        assert self._regen is not None
        if self._regen.availability.geometry_available:
            return None
        error = _availability_request_error(self._regen.availability)
        return self.error_response(operation, {}, error.code, str(error), **error.details)

    def close(self) -> None:
        self._solver.close()

    @property
    def sketch_solve_timeout(self) -> float:
        return self._solver.timeout

    @property
    def viewer_url(self) -> str | None:
        return self._viewer_url

    @property
    def revision(self) -> int:
        return self._document.revision

    def _validate_assembly_references(
        self,
        result: RegenResult,
        *,
        loading: bool = False,
        assembly: Assembly | None = None,
    ) -> AssemblySolveResult:
        from .assembly_solver import solve_assembly

        assembly = self._document.assembly if assembly is None else assembly
        try:
            resolved = resolve_occurrences(assembly, terminal_parts(result, role=None))
            solved = solve_assembly(assembly, resolved, result.provenance)
            if loading and solved.assembly.occurrences != assembly.occurrences:
                raise AssemblyError(
                    "INVALID_DOCUMENT",
                    "persisted assembly transforms disagree with regenerated mate solution",
                    occurrenceIds=sorted(assembly.occurrences),
                )
            return solved
        except AssemblyError as error:
            if loading:
                raise GraphError(
                    "INVALID_DOCUMENT",
                    str(error),
                    **error.details,
                ) from error
            raise RequestError(error.code, str(error), **error.details) from error

    def _resolved_assembly(self):
        assert self._regen is not None
        return resolve_occurrences(self._document.assembly, terminal_parts(self._regen, role=None))

    def _assert_effect(self, operation: MutationOperation, result: RegenResult) -> None:
        if not isinstance(operation, (CutCreate, HoleCreate)):
            return
        if result.removed_material.get(operation.featureId, False):
            return
        target = operation.parameters.target.featureId
        kind = "cut" if isinstance(operation, CutCreate) else "hole"
        hint = (
            "check flip, plane, and offset"
            if kind == "cut"
            else "check target, position, diameter, and depth"
        )
        details = {
            "featureId": operation.featureId,
            "target": target,
            "hint": hint,
        }
        message = f"{kind} {operation.featureId} removes no material from {target}"
        if isinstance(operation, CutCreate):
            raise RequestError("CUT_NO_EFFECT", message, **details)
        raise RequestError("HOLE_NO_EFFECT", message, **details)

    def _committed_regeneration(self, operation: MutationOperation) -> RegenResult | None:
        if not isinstance(
            operation,
            (
                ImportDelete,
                SketchDelete,
                FeatureDelete,
                FilletCreate,
                ChamferCreate,
                PcbLidCreate,
                PcbLidUpdate,
            ),
        ):
            return None
        self._ensure_geometry()
        assert self._regen is not None
        return self._regen

    @staticmethod
    def _validate_treatment_parts(
        operation: MutationOperation, result: RegenResult, changed_id: str
    ) -> None:
        validated_features = _step_validation_feature_ids(operation, result, changed_id)
        if not validated_features:
            return
        parts = [
            part
            for part in terminal_parts(result)
            if validated_features
            & {item["id"] for item in _lineage(result, part.terminal_feature, part.source_body)}
        ]
        for part in parts:
            try:
                _validate_part_for_step(part)
            except ExportFailure as error:
                feature_id = min(validated_features)
                feature_kind = getattr(operation, "kind", "revolve")
                raise RequestError(
                    "MANUFACTURING_VALIDATION_FAILED",
                    f"{feature_kind} {feature_id} cannot produce matching STEP geometry for {part.part_id}",
                    featureId=feature_id,
                    partId=part.part_id,
                    mismatchedMetric=error.details.get("mismatchedMetric", "reimport"),
                ) from error

    def error_response(
        self,
        operation: str,
        payload: dict[str, Any],
        code: str,
        message: str,
        **details: Any,
    ) -> dict[str, Any]:
        """Build the public error shape and add model context only when it is known."""
        with self._lock:
            failure = _safe_error(
                self._document.revision,
                payload.get("requestId"),
                code,
                message,
                operation=operation,
                **details,
            )
            return self._with_error_context(payload, failure)

    def _with_error_context(
        self, payload: dict[str, Any], failure: dict[str, Any]
    ) -> dict[str, Any]:
        error = failure["error"]
        mutation_ids = {
            field: value
            for field in ("featureId", "sketchId", "assetId", "occurrenceId")
            if isinstance(value := error.get(field) or payload.get(field), str)
        }
        error.update(mutation_ids)

        target = payload.get("parameters", {}).get("target", {})
        related = {
            value
            for value in (
                *mutation_ids.values(),
                error.get("target"),
                error.get("consumer"),
                target.get("featureId") if isinstance(target, dict) else None,
            )
            if isinstance(value, str)
        }
        if self._regen is not None and related:
            affected = [
                part.part_id
                for part in terminal_parts(self._regen)
                if related
                & {
                    item["id"]
                    for item in _lineage(self._regen, part.terminal_feature, part.source_body)
                }
            ]
            if affected:
                error["affectedParts"] = affected
        return failure

    def publish_failure(
        self,
        payload: dict[str, Any],
        result: dict[str, Any],
        attempted_edit: dict[str, Any] | None = None,
    ) -> None:
        """Show a failed sketch or feature call in the viewer; the last good scene stays."""
        if self._publisher is None:
            return
        feature_id = payload.get("featureId") or payload.get("sketchId") or payload.get("assetId")
        error = result["error"]
        viewer_error = _viewer_error(error)
        if "diagnosticGeometry" in viewer_error and isinstance(feature_id, str):
            viewer_error["diagnosticGeometry"] = {
                **viewer_error["diagnosticGeometry"],
                "frame": self._diagnostic_frame(payload, feature_id),
            }
        self._publisher.publish_failure(
            [feature_id] if isinstance(feature_id, str) else [],
            viewer_error,
            attempted_edit,
        )

    def _diagnostic_frame(self, payload: dict[str, Any], sketch_id: str) -> dict[str, list[float]]:
        if payload.get("action") == "create":
            plane = _sketch_plane(payload)
            sketch = Sketch(sketch_id, plane=Plane(plane.name, plane.offset))
        else:
            accepted = self._document.graph.nodes[sketch_id]
            position = payload.get("set", {}).get("position", accepted.position)
            sketch = Sketch(
                sketch_id,
                plane=accepted.plane,
                position=tuple(position),
                rotation=accepted.rotation,
            )
        return self._frame(sketch)

    def _mutation_request(
        self,
        payload: dict[str, Any],
        parse: Callable[[dict[str, Any]], MutationOperation],
    ) -> tuple[MutationOperation, str, dict[str, Any] | None]:
        request_id = payload.get("requestId")
        if not isinstance(request_id, str) or not request_id:
            raise RequestError("INVALID_PARAMETER", "requestId is required")
        parsed = parse(payload)
        body_hash = _hash(parsed)
        receipt = next(
            (item for item in self._document.receipts if item["requestId"] == request_id), None
        )
        if receipt is not None:
            if receipt["requestHash"] == body_hash:
                return parsed, body_hash, receipt["response"]
            raise RequestError(
                "REQUEST_ID_REUSED", "requestId was already used for a different request body"
            )
        request_hash = next(
            (
                item["requestHash"]
                for item in self._document.request_hashes
                if item["requestId"] == request_id
            ),
            None,
        )
        if request_hash is not None:
            message = (
                "requestId was already used for a different request body"
                if request_hash != body_hash
                else "requestId replay is outside the full receipt window"
            )
            raise RequestError("REQUEST_ID_REUSED", message)
        if parsed.expectedRevision != self._document.revision:
            raise RequestError(
                "REVISION_CONFLICT",
                f"expected revision {parsed.expectedRevision}; current revision is {self._document.revision}",
                expected=parsed.expectedRevision,
                actual=self._document.revision,
            )
        return parsed, body_hash, None

    def _publish_mutation(
        self,
        result: RegenResult,
        before: Any,
        changed_id: str,
        change: dict[str, str],
        accepted_edit: dict[str, Any] | None,
    ) -> None:
        if self._publisher is None:
            return
        if result.availability.geometry_available:
            self._publisher.publish(
                self._document.revision,
                result,
                self._document.assembly,
                before,
                changed_id,
                change,
                accepted_edit,
            )
        else:
            self._publish_regeneration(result)

    def _save_response(
        self,
        graph: Any,
        assembly: Assembly,
        response: dict[str, Any],
        body_hash: str,
    ) -> tuple[Document, bool]:
        receipt = {
            "requestId": response["requestId"],
            "requestHash": body_hash,
            "revision": response["revision"],
            "response": response,
        }
        document = Document(
            graph=graph,
            assembly=assembly,
            revision=response["revision"],
            receipts=[*self._document.receipts, receipt][-MAX_RECEIPTS:],
            request_hashes=[
                *self._document.request_hashes,
                {"requestId": response["requestId"], "requestHash": body_hash},
            ][-MAX_REQUEST_HASHES:],
        )
        return document, self.store.save(document)

    def _commit_mutation(
        self,
        payload: dict[str, Any],
        parsed: MutationOperation,
        body_hash: str,
        apply: Callable[[Any, MutationOperation, RegenResult | None], str | _DeleteResult],
        response_fields: Callable[[Any, RegenResult, str], dict[str, Any]],
        accepted_edit: dict[str, Any] | None,
    ) -> dict[str, Any]:
        self.start_kernel_warmup()
        self._kernel_loader.wait()
        _ensure_refresh_geometry(parsed, self._ensure_geometry)
        committed_result = self._regen
        previous_selections = _selection_snapshot(self._document.graph)
        graph = loads(dumps(self._document.graph))
        applied = apply(graph, parsed, self._committed_regeneration(parsed))
        changed_id, changed_ids, deletion = _applied_result(applied)
        result = self._regenerate(graph)
        if not result.availability.geometry_available and not isinstance(
            parsed, (SketchCreate, SketchUpdate, SketchDelete)
        ):
            raise _availability_request_error(result.availability)
        self._assert_effect(parsed, result)
        self._validate_treatment_parts(parsed, result, changed_id)
        solved = self._validate_assembly_references(result)
        deletion_fields, deletion_change = _deletion_result_fields(deletion, result)
        previous = self._document.revision
        before = self._document.graph
        delta, refresh_fields = _refresh_fields(parsed, before, graph, committed_result, result)
        change = _mutation_change(
            deletion_change, before, result, changed_id, committed_result, delta
        )
        selection_changes = _selection_changes(previous_selections, _selection_snapshot(graph))
        summary_prefix = change["summary"].removesuffix(", no value changed")
        if selection_summary := _pattern_selection_summary(selection_changes, summary_prefix):
            change["summary"] = selection_summary
        response = {
            "ok": True,
            "revision": previous + 1,
            "previousRevision": previous,
            "requestId": payload["requestId"],
            "units": "mm",
            "changedFeatureIds": changed_ids,
            "regeneration": {
                "status": "OK" if result.availability.geometry_available else "UNAVAILABLE",
                "valid": result.availability.geometry_available,
            },
            "warnings": [],
            "selectionChanges": selection_changes,
            **deletion_fields,
            **response_fields(graph, result, changed_id),
            **refresh_fields,
        }
        document, durable = self._save_response(graph, solved.assembly, response, body_hash)
        self._document = document
        self._regen = result
        self._graph_error = None
        self._publish_mutation(result, before, changed_id, change, accepted_edit)
        if not durable:
            _LOG.warning(
                "document rename committed but directory fsync failed; durability is unconfirmed"
            )
        return response

    def _mutation_failure(
        self,
        operation: str,
        payload: dict[str, Any],
        error: BaseException,
        attempted_edit: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if isinstance(error, RequestError):
            failure = self.error_response(
                operation, payload, error.code, str(error), **error.details
            )
        elif isinstance(error, BoardImportError):
            failure = self.error_response(
                operation,
                payload,
                error.code,
                str(error),
                hint=(
                    "use an explicit absolute path to a readable regular kicad_pcb file "
                    "of 16 MiB or less with one closed, valid board outline"
                ),
            )
        elif isinstance(error, GraphError):
            graph_failure = _graph_error_response(
                self._document.revision, payload.get("requestId"), operation, error
            )
            graph_details = dict(graph_failure["error"])
            code = graph_details.pop("code")
            message = graph_details.pop("message")
            graph_details.pop("retryable")
            graph_details.pop("operation")
            failure = self.error_response(operation, payload, code, message, **graph_details)
        else:
            _LOG.exception("kernel failure during %s", operation, exc_info=error)
            failure = self.error_response(
                operation,
                payload,
                "KERNEL_OPERATION_FAILED",
                "the modeling operation could not produce valid geometry",
            )
        self.publish_failure(payload, failure, attempted_edit)
        return failure

    def _mutate(
        self,
        operation: str,
        payload: dict[str, Any],
        parse: Callable[[dict[str, Any]], MutationOperation],
        apply: Callable[[Any, MutationOperation, RegenResult | None], str | _DeleteResult],
        response_fields: Callable[[Any, RegenResult, str], dict[str, Any]],
    ) -> dict[str, Any]:
        with self._lock:
            attempted_edit = None
            try:
                parsed, body_hash, replay = self._mutation_request(payload, parse)
                if replay is not None:
                    return replay
                attempted_edit = _edit_record(parsed)
                accepted_edit = attempted_edit if parsed.action == "update" else None
                return self._commit_mutation(
                    payload, parsed, body_hash, apply, response_fields, accepted_edit
                )
            except Exception as error:  # noqa: BLE001 - translate failures at this seam
                return self._mutation_failure(operation, payload, error, attempted_edit)

    @staticmethod
    def _frame(sketch: Sketch) -> dict[str, list[float]]:
        from build123d import Plane as BuildPlane

        from .extrusion import sketch_location

        plane = BuildPlane(sketch_location(sketch))

        def coordinates(vector: Any) -> list[float]:
            return [vector.X, vector.Y, vector.Z]

        return {
            "origin": coordinates(plane.origin),
            "normal": coordinates(plane.z_dir),
            "xAxis": coordinates(plane.x_dir),
            "yAxis": coordinates(plane.y_dir),
        }

    def _extrude_metadata(self, graph: Any, node: Extrude) -> dict[str, Any]:
        from .extrusion import extrude_sketch

        sketch = graph.nodes[node.sketch]
        tool = extrude_sketch(node, sketch).shape
        return {
            "frame": self._frame(sketch),
            "direction": self._frame(sketch)["normal"],
            "toolBBox": self._bbox([tool]),
        }

    def _cut_metadata(self, graph: Any, result: RegenResult, node: Cut) -> dict[str, Any]:
        from .cut import make_cut_tool

        sketch = graph.nodes[node.tool]
        tool = make_cut_tool(node, sketch, result.shapes[node.target]).shape
        normal = self._frame(sketch)["normal"]
        direction = (
            normal if node.depth is None else [value if node.flip else -value for value in normal]
        )
        return {
            "frame": self._frame(sketch),
            "direction": direction,
            "toolBBox": self._bbox([tool]),
        }

    def _hole_metadata(self, result: RegenResult, node: Hole) -> dict[str, Any]:
        from .hole import hole_positions, make_hole_tool

        sketch = Sketch("global_xy")
        tool = make_hole_tool(node, result.shapes[node.target]).shape
        return {
            "frame": self._frame(sketch),
            "direction": [0.0, 0.0, 1.0],
            "toolBBox": self._bbox([tool]),
            "positionNames": [position.name for position in hole_positions(node)],
        }

    def _node_metadata(self, graph: Any, result: RegenResult, node_id: str) -> dict[str, Any]:
        node = graph.nodes[node_id]
        if isinstance(node, Sketch):
            return {"frame": self._frame(node)}
        if isinstance(node, Extrude):
            return self._extrude_metadata(graph, node)
        if isinstance(node, Cut):
            return self._cut_metadata(graph, result, node)
        if isinstance(node, Hole):
            return self._hole_metadata(result, node)
        if isinstance(node, (Fillet, Chamfer)):
            generated_prefix = "blend(" if isinstance(node, Fillet) else "chamfer("
            return {
                "edgeTreatment": {
                    "kind": type(node).__name__.lower(),
                    "sources": [reference.to_dict() for reference in node.edges],
                    "generatedFaces": [
                        {
                            "role": candidate.key.role,
                            "source": _wire_value(asdict(candidate.key.source)),
                        }
                        for candidate in result.provenance[node.id]
                        if candidate.key.role.startswith(generated_prefix)
                    ],
                }
            }
        if isinstance(node, PortCutout):
            return {"portCutout": result.metadata[node.id]}
        return {}

    def import_board(self, payload: dict[str, Any]) -> dict[str, Any]:
        def response(graph: Any, result: RegenResult, changed_id: str) -> dict[str, Any]:
            if changed_id not in graph.nodes:
                return {"assetId": changed_id}
            node = next(
                item for item in json.loads(dumps(graph))["nodes"] if item["id"] == changed_id
            )
            return {"assetId": changed_id, "boardImport": node}

        return self._mutate(
            "import",
            payload,
            _parse_import,
            _apply_import,
            response,
        )

    def sketch(self, payload: dict[str, Any]) -> dict[str, Any]:
        entities = payload.get("entities") or payload.get("set", {}).get("entities")

        def response(graph: Any, result: RegenResult, changed_id: str) -> dict[str, Any]:
            fields = {"sketchId": changed_id}
            if changed_id in graph.nodes:
                fields.update(self._node_metadata(graph, result, changed_id))
                node = graph.nodes[changed_id]
                if isinstance(node, Sketch) and node.constrained is not None:
                    fields["solve"] = result_payload(node.constrained)
            if entities:
                fields["entitySegments"] = _entity_segments(entities)
            return fields

        return self._mutate(
            "sketch",
            payload,
            _parse_sketch,
            partial(_apply_sketch, solver=self._solver.solve),
            response,
        )

    def _resolve_update_kind(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Fill an update's kind from the stored feature; a different given kind is an error."""
        node = self._document.graph.nodes.get(payload.get("featureId"))
        if not isinstance(node, Feature):
            noun = payload.get("kind") or "feature"
            raise RequestError(
                "UNKNOWN_ID",
                f"unknown {noun}: {payload.get('featureId')}",
                featureId=payload.get("featureId"),
            )
        expected_kind, actual_kind = _infer_update_kind(payload, node)
        if actual_kind is not None and actual_kind != expected_kind:
            raise RequestError(
                "INVALID_PARAMETER",
                f"feature {node.id} is {expected_kind}, not {actual_kind}",
                featureId=node.id,
                expectedKind=expected_kind,
                actualKind=actual_kind,
            )
        return {**payload, "kind": expected_kind}

    @staticmethod
    def _ensure_occurrence_has_no_mates(assembly: Assembly, occurrence_id: str) -> None:
        dependent_mates = sorted(
            mate.id
            for mate in assembly.mates.values()
            if mate.occurrence_id == occurrence_id
            or (mate.first is not None and mate.first.occurrence_id == occurrence_id)
            or (mate.second is not None and mate.second.occurrence_id == occurrence_id)
        )
        if dependent_mates:
            raise RequestError(
                "HAS_DEPENDENTS",
                f"assembly occurrence has dependent mates: {occurrence_id}",
                occurrenceId=occurrence_id,
                mateIds=dependent_mates,
            )

    def _updated_assembly(
        self,
        parsed: AssemblyOccurrenceCreate | AssemblyOccurrenceUpdate | AssemblyOccurrenceDelete,
    ) -> tuple[Assembly, Occurrence]:
        assembly = Assembly(
            dict(self._document.assembly.occurrences), dict(self._document.assembly.mates)
        )
        current = assembly.occurrences.get(parsed.occurrenceId)
        if not isinstance(parsed, AssemblyOccurrenceCreate):
            if current is None:
                raise RequestError(
                    "UNKNOWN_ID",
                    f"unknown assembly occurrence: {parsed.occurrenceId}",
                    occurrenceId=parsed.occurrenceId,
                )
            if isinstance(parsed, AssemblyOccurrenceDelete):
                self._ensure_occurrence_has_no_mates(assembly, current.id)
                del assembly.occurrences[current.id]
                return assembly, current
            updated = Occurrence(current.id, current.part_id, parsed.transform)
            assembly.occurrences[current.id] = updated
            return assembly, updated
        if current is not None:
            raise RequestError(
                "DUPLICATE_ID",
                f"assembly occurrence already exists: {parsed.occurrenceId}",
                occurrenceId=parsed.occurrenceId,
            )
        if len(assembly.occurrences) >= MAX_OCCURRENCES:
            raise RequestError(
                "ASSEMBLY_OCCURRENCE_LIMIT",
                "an assembly can contain at most 64 occurrences",
                limit=MAX_OCCURRENCES,
            )
        assert self._regen is not None
        available = {part.part_id for part in terminal_parts(self._regen, role=None)}
        if parsed.part_id not in available:
            raise RequestError(
                "UNKNOWN_ID",
                f"unknown terminal part: {parsed.part_id}",
                partId=parsed.part_id,
            )
        created = Occurrence(parsed.occurrenceId, parsed.part_id, parsed.transform)
        assembly.occurrences[created.id] = created
        return assembly, created

    def _updated_mates(
        self, parsed: AssemblyMateCreate | AssemblyMateUpdate | AssemblyMateDelete
    ) -> tuple[Assembly, Mate]:
        assembly = Assembly(
            dict(self._document.assembly.occurrences), dict(self._document.assembly.mates)
        )
        current = assembly.mates.get(parsed.mateId)
        if not isinstance(parsed, AssemblyMateCreate):
            if current is None:
                raise RequestError(
                    "UNKNOWN_ID", f"unknown assembly mate: {parsed.mateId}", mateId=parsed.mateId
                )
            if isinstance(parsed, AssemblyMateDelete):
                del assembly.mates[current.id]
                return assembly, current
        elif current is not None:
            raise RequestError(
                "DUPLICATE_ID",
                f"assembly mate already exists: {parsed.mateId}",
                mateId=parsed.mateId,
            )
        elif len(assembly.mates) >= MAX_MATES:
            raise RequestError(
                "ASSEMBLY_MATE_LIMIT", "an assembly can contain at most 128 mates", limit=MAX_MATES
            )
        assert isinstance(parsed, (AssemblyMateCreate, AssemblyMateUpdate))
        occurrence_ids = (
            [parsed.occurrence_id]
            if parsed.occurrence_id is not None
            else [parsed.first.occurrence_id, parsed.second.occurrence_id]
        )
        unknown = [item for item in occurrence_ids if item not in assembly.occurrences]
        if unknown:
            raise RequestError(
                "UNKNOWN_ID",
                f"unknown assembly occurrence: {unknown[0]}",
                occurrenceId=unknown[0],
            )
        updated = (
            Mate(parsed.mateId, "ground", parsed.occurrence_id)
            if parsed.occurrence_id is not None
            else Mate(
                parsed.mateId,
                parsed.mate_kind,
                first=parsed.first,
                second=parsed.second,
                orientation=parsed.orientation,
                offset=parsed.offset,
            )
        )
        assembly.mates[updated.id] = updated
        return assembly, updated

    def _mate_response(self, payload: dict[str, Any], mate: Mate) -> dict[str, Any]:
        return {
            "ok": True,
            "revision": self._document.revision + 1,
            "previousRevision": self._document.revision,
            "requestId": payload["requestId"],
            "units": "mm",
            "mateId": mate.id,
            "kind": mate.kind,
            **({"occurrenceId": mate.occurrence_id} if mate.kind == "ground" else {}),
            "affectedMateIds": [mate.id],
            "changedOccurrences": [],
            "warnings": [],
        }

    def _occurrence_response(
        self, payload: dict[str, Any], occurrence: Occurrence
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "revision": self._document.revision + 1,
            "previousRevision": self._document.revision,
            "requestId": payload["requestId"],
            "units": "mm",
            "occurrenceId": occurrence.id,
            "partId": occurrence.part_id,
            "transform": transform_to_dict(occurrence.transform),
            "warnings": [],
        }

    @staticmethod
    def _mated_transform_warning(
        occurrence: Occurrence, status: dict[str, Any], mates: dict[str, Mate]
    ) -> dict[str, Any]:
        mate_kinds = {mates[mate_id].kind for mate_id in status["mateIds"]}
        constrained = []
        if "planar" in mate_kinds:
            constrained.extend(["normalAlignment", "normalOffset"])
        if "concentric" in mate_kinds:
            constrained.extend(["axisAlignment", "radialOffset"])
        return {
            "code": "MATED_TRANSFORM_OVERRIDDEN",
            "occurrenceId": occurrence.id,
            "constrainedCoordinates": constrained,
            "freeMotions": status["freeMotions"],
        }

    def _mutate_occurrence(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            try:
                parsed, body_hash, replay = self._mutation_request(
                    payload, _parse_assembly_occurrence
                )
                if replay is not None:
                    return replay
                assert isinstance(
                    parsed,
                    (AssemblyOccurrenceCreate, AssemblyOccurrenceUpdate, AssemblyOccurrenceDelete),
                )
                self._ensure_geometry()
                assembly, occurrence = self._updated_assembly(parsed)
                assert self._regen is not None
                solved = self._validate_assembly_references(self._regen, assembly=assembly)
                effective = solved.assembly.occurrences.get(occurrence.id, occurrence)
                response = self._occurrence_response(payload, effective)
                status = solved.status_by_occurrence.get(occurrence.id, {})
                if (
                    isinstance(parsed, AssemblyOccurrenceUpdate)
                    and occurrence.transform != effective.transform
                    and status.get("mateIds")
                    and not status.get("grounded")
                ):
                    response["warnings"] = [
                        self._mated_transform_warning(
                            occurrence, status, solved.assembly.mates
                        )
                    ]
                document, durable = self._save_response(
                    self._document.graph, solved.assembly, response, body_hash
                )
                self._document = document
                assert self._regen is not None
                self._publish_regeneration(self._regen)
                if not durable:
                    _LOG.warning(
                        "document rename committed but directory fsync failed; durability is unconfirmed"
                    )
                return response
            except Exception as error:  # noqa: BLE001 - translate failures at this seam
                return self._mutation_failure("feature", payload, error, None)

    def _mutate_mate(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            try:
                parsed, body_hash, replay = self._mutation_request(payload, _parse_assembly_mate)
                if replay is not None:
                    return replay
                assert isinstance(
                    parsed, (AssemblyMateCreate, AssemblyMateUpdate, AssemblyMateDelete)
                )
                self._ensure_geometry()
                assembly, mate = self._updated_mates(parsed)
                assert self._regen is not None
                solved = self._validate_assembly_references(self._regen, assembly=assembly)
                response = self._mate_response(payload, mate)
                response["changedOccurrences"] = [
                    occurrence_id
                    for occurrence_id in sorted(assembly.occurrences)
                    if assembly.occurrences[occurrence_id].transform
                    != solved.assembly.occurrences[occurrence_id].transform
                ]
                document, durable = self._save_response(
                    self._document.graph, solved.assembly, response, body_hash
                )
                self._document = document
                assert self._regen is not None
                self._publish_regeneration(self._regen)
                if not durable:
                    _LOG.warning(
                        "document rename committed but directory fsync failed; durability is unconfirmed"
                    )
                return response
            except Exception as error:  # noqa: BLE001 - translate failures at this seam
                return self._mutation_failure("feature", payload, error, None)

    def feature(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("kind") == "assembly_occurrence":
            return self._mutate_occurrence(payload)
        if payload.get("kind") == "assembly_mate":
            return self._mutate_mate(payload)

        def parse(candidate: dict[str, Any]) -> MutationOperation:
            if candidate.get("action") == "update":
                candidate = self._resolve_update_kind(candidate)
            return _parse_feature(candidate)

        return self._mutate(
            "feature",
            payload,
            parse,
            _apply_feature,
            lambda graph, result, changed_id: {
                "featureId": changed_id,
                **(
                    self._node_metadata(graph, result, changed_id)
                    if changed_id in graph.nodes
                    else {}
                ),
            },
        )

    def _check_export_revision(self, expected_revision: int) -> None:
        if expected_revision == self._document.revision:
            return
        raise RequestError(
            "STALE_REVISION",
            f"expected revision {expected_revision}; current revision is {self._document.revision}",
            expected=expected_revision,
            actual=self._document.revision,
            hint="query the current revision and review the changed model before exporting",
        )

    def _select_export_parts(self, requested: list[str] | None) -> list[Any]:
        available = [
            part
            for part in terminal_parts(self._regen)
            if not isinstance(self._document.graph.nodes.get(part.source_body.feature), BoardImport)
        ]
        if requested is None:
            selected = available
            unknown = []
        else:
            by_id = {part.part_id: part for part in available}
            selected = []
            unknown = []
            for requested_id in requested:
                match = _resolve_export_part(requested_id, available, by_id, self._regen)
                if match is not None:
                    selected.append(match)
                else:
                    unknown.append(requested_id)
        if unknown:
            raise RequestError(
                "UNKNOWN_ID",
                "each requested id must name a partId or resolve to exactly one terminal manufacturing part",
                unknownPartIds=unknown,
                validParts=[
                    {"partId": part.part_id, "featureId": part.terminal_feature}
                    for part in available
                ],
                hint="use one of the listed partIds when a feature built multiple parts",
            )
        if not selected:
            raise RequestError(
                "UNKNOWN_ID",
                "the document has no terminal manufacturing parts to export",
                hint="create manufacturing geometry before exporting",
            )
        return selected

    @staticmethod
    def _check_assembly_export_request(payload: dict[str, Any]) -> None:
        if payload.get("mode") != "assembly":
            return
        if "partIds" in payload:
            raise RequestError(
                "INVALID_PARAMETER",
                "assembly export includes all manufacturing occurrences and does not accept partIds",
                invalidFields=["partIds"],
            )

    @staticmethod
    def _check_export_request(payload: dict[str, Any]) -> None:
        action = payload.get("action", "publish")
        mode = payload.get("mode", "compound")
        DocumentService._check_assembly_export_request(payload)
        if action == "preflight":
            forbidden = [field for field in ("path",) if field in payload]
            if mode != "assembly" and "mode" in payload:
                forbidden.append("mode")
            if forbidden:
                raise RequestError(
                    "INVALID_PARAMETER",
                    "export preflight accepts mode only when mode is assembly and never accepts path",
                    invalidFields=forbidden,
                )
            return
        if "path" not in payload:
            raise RequestError(
                "INVALID_PARAMETER",
                "export publication requires path",
                requiredFields=["path"],
            )
        if mode == "compound":
            forbidden = [field for field in ("base", "requirements") if field in payload]
            if forbidden:
                raise RequestError(
                    "INVALID_PARAMETER",
                    "compound export does not accept handoff requirements",
                    invalidFields=forbidden,
                )

    def _handoff_requirements(self, payload: dict[str, Any]) -> dict[str, Any]:
        checked = self.check(
            {
                "action": "evaluate",
                "revision": self._document.revision,
                **({"base": payload["base"]} if "base" in payload else {}),
                **({"requirements": payload["requirements"]} if "requirements" in payload else {}),
            }
        )
        if not checked.get("ok"):
            error = checked["error"]
            raise RequestError(
                error["code"],
                error["message"],
                **{
                    key: value
                    for key, value in error.items()
                    if key not in {"code", "message", "operation", "retryable", "revision"}
                },
            )
        return {"gate": checked["gate"], "results": checked["results"]}

    def _handoff_sources(self) -> list[dict[str, str]]:
        return sorted(
            [
                {"assetId": node.id, "sha256": node.source_digest}
                for node in self._document.graph.nodes.values()
                if isinstance(node, BoardImport)
            ],
            key=lambda item: item["assetId"],
        )

    def _handoff_result(
        self, prepared: list[dict[str, Any]], payload: dict[str, Any]
    ) -> dict[str, Any]:
        parts = public_parts(prepared)
        requirements = self._handoff_requirements(payload)
        unknowns = [item for item in requirements["results"] if item["status"] == "UNKNOWN"]
        return {
            "parts": parts,
            "requirements": requirements,
            "unknowns": unknowns,
            "ready": parts_are_ready(prepared) and requirements["gate"] == "PASS",
        }

    def _export_preflight(self, selected: list[Any], payload: dict[str, Any]) -> dict[str, Any]:
        result = self._handoff_result(prepare_parts(selected), payload)
        return {
            "ok": True,
            "revision": self._document.revision,
            "units": "mm",
            "partIds": [part.part_id for part in selected],
            **result,
        }

    def _export_separate(self, selected: list[Any], payload: dict[str, Any]) -> dict[str, Any]:
        prepared = prepare_parts(selected)
        result = self._handoff_result(prepared, payload)
        manifest = {
            "revision": self._document.revision,
            "units": "mm",
            "sources": self._handoff_sources(),
            **result,
        }
        fields = export_parts_separately(Path(payload["path"]), prepared, manifest)
        return {"ok": True, "revision": self._document.revision, "units": "mm", **result, **fields}

    def _assembly_export(self, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._regen is not None
        resolved = resolve_occurrences(
            self._document.assembly, terminal_parts(self._regen, role=None)
        )
        included = [item for item in resolved if item.part.role == "manufacturing"]
        excluded = sorted(item.occurrence.id for item in resolved if item.part.role == "reference")
        if not included:
            raise RequestError(
                "ASSEMBLY_EXPORT_EMPTY",
                "assembly export requires at least one manufacturing occurrence",
                excludedOccurrenceIds=excluded,
            )
        root_name = self.store.path.name.removesuffix(".coalesce.json") or "assembly"
        if payload.get("action", "publish") == "preflight":
            content, digest, evidence = write_assembly_step_bytes(root_name, included)
            fields = {
                "partIds": sorted({item.part.part_id for item in included}),
                "occurrenceIds": sorted(item.occurrence.id for item in included),
                "sha256": digest,
                "byteCount": len(content),
                "tolerance": {"relative": RELATIVE_TOLERANCE, "absolute": ABSOLUTE_TOLERANCE},
                "validation": {
                    "geometry": {"status": "passed", "freshProcess": True},
                    "xcaf": evidence,
                },
            }
        else:
            fields = export_assembly(Path(payload["path"]), root_name, included)
        return {
            "ok": True,
            "revision": self._document.revision,
            "units": "mm",
            "excludedOccurrenceIds": excluded,
            **fields,
        }

    def export(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            selected: list[Any] = []
            try:
                if failure := self._geometry_failure("export"):
                    return failure
                self._check_export_revision(payload["expectedRevision"])
                self._check_export_request(payload)
                if payload.get("mode", "compound") == "assembly":
                    return self._assembly_export(payload)
                selected = self._select_export_parts(payload.get("partIds"))
                if payload.get("action", "publish") == "preflight":
                    return self._export_preflight(selected, payload)
                if payload.get("mode", "compound") == "separate":
                    return self._export_separate(selected, payload)
                fields = export_parts(Path(payload["path"]), selected)
                return {"ok": True, "revision": self._document.revision, "units": "mm", **fields}
            except ExportFailure as error:
                return self.error_response(
                    "export",
                    payload,
                    error.code,
                    error.message,
                    **({"affectedParts": [part.part_id for part in selected]} if selected else {}),
                    **error.details,
                )
            except RequestError as error:
                return self.error_response(
                    "export",
                    payload,
                    error.code,
                    str(error),
                    **error.details,
                )
            except Exception:  # noqa: BLE001 - filesystem failures are translated at this seam.
                _LOG.exception("unexpected failure during export")
                return self.error_response(
                    "export",
                    payload,
                    "EXPORT_WRITE_FAILED",
                    "STEP export could not be completed",
                    **({"affectedParts": [part.part_id for part in selected]} if selected else {}),
                    hint="check the target directory and retry with a new path",
                )

    def _assembly_solve_payload(self, assembly: Assembly) -> AssemblySolveResult:
        from .assembly_solver import solve_assembly

        assert self._regen is not None
        resolved = resolve_occurrences(assembly, terminal_parts(self._regen, role=None))
        return solve_assembly(assembly, resolved, self._regen.provenance)

    def _assembly_query(self, base: dict[str, Any]) -> dict[str, Any]:
        assert self._regen is not None
        parts = terminal_parts(self._regen, role=None)
        try:
            resolved = resolve_occurrences(self._document.assembly, parts)
        except AssemblyError as error:
            return _safe_error(
                self._document.revision,
                None,
                error.code,
                str(error),
                operation="query",
                **error.details,
            )
        solve = self._assembly_solve_payload(self._document.assembly)
        occurrences = [
            {
                "occurrenceId": item.occurrence.id,
                "partId": item.part.part_id,
                "terminalFeatureId": item.part.terminal_feature,
                "role": item.part.role,
                "transform": transform_to_dict(item.occurrence.transform),
                "bbox": self._bbox([item.shape]),
                "volume": {"value": item.part.shape.volume, "unit": "mm^3"},
                "area": {"value": item.part.shape.area, "unit": "mm^2"},
                "referenceStatus": "OK",
                **solve.status_by_occurrence[item.occurrence.id],
            }
            for item in resolved
        ]
        available = [
            {
                "partId": part.part_id,
                "terminalFeatureId": part.terminal_feature,
                "label": part.label,
                "role": part.role,
            }
            for part in sorted(parts, key=lambda item: item.part_id)
        ]
        mates = [
            (
                {"mateId": mate.id, "kind": "ground", "occurrenceId": mate.occurrence_id}
                if mate.kind == "ground"
                else {
                    "mateId": mate.id,
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
                    **(
                        {"offset": {"value": mate.offset, "unit": "mm"}}
                        if mate.kind == "planar"
                        else {}
                    ),
                }
            )
            for mate in sorted(self._document.assembly.mates.values(), key=lambda item: item.id)
        ]
        return {
            **base,
            "solveStatus": solve.solve_status,
            "underConstrainedOccurrenceIds": solve.under_constrained_occurrence_ids,
            "redundantMateIds": solve.redundant_mate_ids,
            "components": solve.components,
            "mates": mates,
            "occurrences": occurrences,
            "availableParts": available,
        }

    def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            kind = payload.get("select", {}).get("kind")
            graph_query = kind in {"document", "graph", "timeline"}
            if (
                not graph_query
                and kind
                in {
                    "measurements",
                    "feature",
                    "reference",
                    "edge_roles",
                    "face_roles",
                    "enclosure",
                    "assembly",
                }
                and (failure := self._geometry_failure("query"))
            ):
                return failure
            requested_revision = payload.get("revision", self._document.revision)
            if requested_revision != self._document.revision:
                return _safe_error(
                    self._document.revision,
                    None,
                    "UNKNOWN_ID",
                    "only the current revision is retained",
                    operation="query",
                )
            select = payload.get("select", {})
            base = {"ok": True, "revision": self._document.revision, "units": "mm"}
            if graph_query:
                self._ensure_geometry()
                assert self._regen is not None
                graph = json.loads(dumps(self._document.graph))
                graph.update(self._unavailable_sketch_output(self._regen.availability))
                return {**base, "graph": graph}
            if kind == "revision":
                return base
            if kind == "server":
                return {**base, "viewerUrl": self._viewer_url}
            if kind == "measurements":
                return {**base, **self._metrics(select.get("role", "manufacturing"))}
            if kind == "assembly":
                return self._assembly_query(base)
            if kind == "feature":
                return self._query_feature(select, base)
            if kind == "enclosure":
                return self._query_enclosure(select, base)
            if kind == "reference":
                return self._query_reference(select, base)
            if kind == "edge_roles":
                return self._query_edge_roles(select, base)
            if kind == "face_roles":
                return self._query_face_roles(select, base)
            return _safe_error(
                self._document.revision,
                None,
                "INVALID_PARAMETER",
                f"unknown query kind: {kind}",
                operation="query",
            )

    def _unavailable_sketch_output(
        self, availability: SolveAvailability
    ) -> dict[str, list[dict[str, Any]]]:
        errors = [
            _safe_error(
                self._document.revision,
                None,
                failure.code,
                failure.message,
                operation="query",
                featureId=failure.feature_id,
                **failure.details,
            )["error"]
            for failure in availability.failed
        ]
        unavailable = [
            {
                "featureId": feature.feature_id,
                "dependsOn": feature.depends_on,
                "error": _safe_error(
                    self._document.revision,
                    None,
                    "REFERENCE_FAILED",
                    f"{feature.feature_id} depends on unavailable sketch {feature.depends_on}",
                    operation="query",
                    featureId=feature.feature_id,
                    target=feature.depends_on,
                )["error"],
            }
            for feature in availability.unavailable
        ]
        return {"errors": errors, "unavailable": unavailable}

    def _query_enclosure(self, select: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
        base_id = select.get("base")
        if not isinstance(self._document.graph.nodes.get(base_id), PcbBase):
            return _safe_error(
                self._document.revision,
                None,
                "UNKNOWN_ID",
                f"unknown pcb_base: {base_id}",
                operation="query.enclosure",
            )
        try:
            from .enclosure_inspection import inspect_enclosure

            return {
                **base,
                **inspect_enclosure(self._document.graph, self._regen, base_id),
            }
        except (TypeError, ValueError):
            _LOG.exception("enclosure inspection failed")
            return _safe_error(
                self._document.revision,
                None,
                "KERNEL_OPERATION_FAILED",
                "the enclosure could not be inspected",
                operation="query.enclosure",
            )

    def _query_feature(self, select: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
        if "id" not in select:
            return _safe_error(
                self._document.revision,
                None,
                "INVALID_PARAMETER",
                "missing required field 'id' (field: select)",
                operation="query.feature",
            )
        feature_id = select["id"]
        nodes = json.loads(dumps(self._document.graph))["nodes"]
        feature = next((node for node in nodes if node["id"] == feature_id), None)
        if feature is None:
            return _safe_error(
                self._document.revision,
                None,
                "UNKNOWN_ID",
                f"unknown feature: {feature_id}",
                operation="query.feature",
            )
        return {
            **base,
            "feature": feature,
            **self._node_metadata(self._document.graph, self._regen, feature_id),
        }

    def _query_edge_roles(self, select: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
        feature = select.get("id")
        if feature not in self._document.graph.nodes:
            return _safe_error(
                self._document.revision,
                None,
                "UNKNOWN_ID",
                f"unknown feature: {feature}",
                operation="query.edge_roles",
            )
        return {
            **base,
            "featureId": feature,
            "roles": edge_role_catalog(self._regen.provenance, feature),
        }

    def _query_face_roles(self, select: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
        feature = select.get("id")
        if feature not in self._document.graph.nodes:
            return _safe_error(
                self._document.revision,
                None,
                "UNKNOWN_ID",
                f"unknown feature: {feature}",
                operation="query.face_roles",
            )
        return {
            **base,
            "featureId": feature,
            "roles": face_role_catalog(self._regen.provenance, feature),
        }

    def _query_reference(self, select: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
        try:
            reference = _reference(select.get("reference", {}))
            resolution = self._regen.resolve(reference)
            keys = [asdict(candidate.key) for candidate in resolution.candidates]
            fingerprints = [
                hashlib.sha256(
                    json.dumps(
                        {"key": key, "descriptor": candidate.descriptor},
                        sort_keys=True,
                        default=str,
                    ).encode()
                ).hexdigest()
                for key, candidate in zip(keys, resolution.candidates, strict=True)
            ]
            result = {
                **base,
                "status": resolution.status.value,
                "keys": keys,
                "fingerprints": fingerprints,
                "candidates": [candidate.descriptor for candidate in resolution.candidates],
                "error": resolution.error,
            }
            result["resolution"] = {
                key: result[key]
                for key in ("status", "keys", "fingerprints", "candidates", "error")
            }
            return result
        except (KeyError, TypeError, ValueError, RequestError):
            return _safe_error(
                self._document.revision,
                None,
                "INVALID_PARAMETER",
                "reference is not valid",
                operation="query.reference",
            )

    @staticmethod
    def _bbox(shapes: list[Any]) -> dict[str, Any]:
        if shapes:
            box = Compound(children=shapes).bounding_box()
            minimum = tuple(getattr(box.min, axis) for axis in "XYZ")
            maximum = tuple(getattr(box.max, axis) for axis in "XYZ")
        else:
            minimum = maximum = (0.0, 0.0, 0.0)
        size = tuple(high - low for low, high in zip(minimum, maximum, strict=True))
        center = tuple((low + high) / 2 for low, high in zip(minimum, maximum, strict=True))

        def point(values: tuple[float, float, float]) -> dict[str, Any]:
            return {**dict(zip(("x", "y", "z"), values, strict=True)), "unit": "mm"}

        return {
            **{
                axis: {"value": value, "unit": "mm"}
                for axis, value in zip(("x", "y", "z"), size, strict=True)
            },
            "min": point(minimum),
            "max": point(maximum),
            "center": point(center),
        }

    def _metrics(self, role: str = "manufacturing") -> dict[str, Any]:
        parts = terminal_parts(self._regen, role=role)
        solids = [part.shape for part in parts]
        catalog = [
            {
                "partId": part.part_id,
                "label": part.label,
                "role": part.role,
                "sourceBody": asdict(part.source_body),
                "terminalFeatureId": part.terminal_feature,
                "featureId": part.terminal_feature,
                "bbox": self._bbox([part.shape]),
                "volume": {"value": part.shape.volume, "unit": "mm^3"},
                "area": {"value": part.shape.area, "unit": "mm^2"},
                "solidCount": 1,
            }
            for part in parts
        ]
        return {
            "role": role,
            "bbox": self._bbox(solids),
            "volume": {"value": sum(part.shape.volume for part in parts), "unit": "mm^3"},
            "area": {"value": sum(part.shape.area for part in parts), "unit": "mm^2"},
            "solidCount": len(parts),
            "parts": catalog,
            "bodies": catalog,
        }

    def _prepare_assembly_requirements(
        self, requirements: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[Any], dict[str, Any] | None]:
        selected = [item for item in requirements if item.get("kind") == "assembly_interference"]
        if not selected:
            return [], [], None
        try:
            for requirement in selected:
                _validate_assembly_requirement(requirement)
            resolved = resolve_occurrences(
                self._document.assembly, terminal_parts(self._regen, role=None)
            )
            resolved = [replace(item, shape=copy.deepcopy(item.shape)) for item in resolved]
            pair_count = sum(
                len(inspection_pairs(resolved, item.get("occurrenceIds"))) for item in selected
            )
            if pair_count > MAX_INSPECTION_PAIRS:
                raise AssemblyInspectionError(
                    "INVALID_PARAMETER",
                    "assembly interference checks at most 120 total pairs per request",
                    totalPairCount=pair_count,
                    maximumPairCount=MAX_INSPECTION_PAIRS,
                )
            return selected, resolved, None
        except (AssemblyError, AssemblyInspectionError) as error:
            return (
                [],
                [],
                _safe_error(
                    self._document.revision,
                    None,
                    error.code,
                    str(error),
                    operation="check",
                    **error.details,
                ),
            )

    @staticmethod
    def _evaluate_assembly_requirement(
        requirement: dict[str, Any], resolved: list[Any], revision: int, deadline: float
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        inspection = inspect_assembly(
            resolved,
            requirement.get("occurrenceIds"),
            revision,
            measure_clearance="minimumClearance" in requirement,
            deadline=deadline,
            capture_overlap_shapes=True,
        )
        overlap_shapes = {
            tuple(row["occurrenceIds"]): row.pop("_overlapShape")
            for row in inspection
            if "_overlapShape" in row
        }
        [result] = evaluate_requirements([requirement], inspection, revision)["results"]
        failed = result.get("measured", {}).get("failedPairs", [])
        rows = []
        for row in inspection:
            status = "ERROR" if "error" in row else "FAIL" if row in failed else "PASS"
            shape = overlap_shapes.get(tuple(row["occurrenceIds"]))
            rows.append({
                **row,
                "requirementId": requirement["id"],
                "status": status,
                **({"_overlapShape": shape} if shape is not None else {}),
            })
        return result, rows

    @staticmethod
    def _evaluate_assembly_requirements(
        requirements: list[dict[str, Any]], resolved: list[Any], revision: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        deadline = inspection_deadline()
        evaluated = [
            DocumentService._evaluate_assembly_requirement(
                requirement, resolved, revision, deadline
            )
            for requirement in requirements
        ]
        return [result for result, _ in evaluated], [row for _, rows in evaluated for row in rows]

    def _evaluate_enclosure_requirements(
        self, payload: dict[str, Any], requirements: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        enclosure_requirements = [
            item
            for item in requirements
            if item.get("kind") not in {"valid_brep", "assembly_interference"}
        ]
        if not enclosure_requirements:
            return [], None
        try:
            from .enclosure_inspection import IncompleteEvidence, inspect_enclosure

            inspection = inspect_enclosure(
                self._document.graph, self._regen, payload.get("base", "base")
            )
            return (
                evaluate_requirements(enclosure_requirements, inspection, self._document.revision)[
                    "results"
                ],
                None,
            )
        except InvalidHoleIds as error:
            return [], _safe_error(
                self._document.revision,
                None,
                "INVALID_PARAMETER",
                str(error),
                operation="check",
                invalidHoleIds=error.invalid_hole_ids,
                validHoles=error.valid_holes,
            )
        except IncompleteEvidence as error:
            return [], _safe_error(
                self._document.revision,
                None,
                "INSPECTION_FAILED",
                "enclosure inspection could not be completed",
                operation="check",
                reason="enclosure geometry evidence is incomplete",
                feature=error.feature,
                role=error.role,
                faceCount=error.count,
            )
        except (KeyError, TypeError, RuntimeError) as error:
            return [], _safe_error(
                self._document.revision,
                None,
                "INSPECTION_FAILED",
                "enclosure inspection could not be completed",
                operation="check",
                kernelMessage=str(error),
            )

    def _finish_check_locked(
        self,
        payload: dict[str, Any],
        requirements: list[dict[str, Any]],
        results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        enclosure_results, failure = self._evaluate_enclosure_requirements(payload, requirements)
        if failure is not None:
            return failure
        results.extend(enclosure_results)
        results.extend(
            _valid_brep_result(self._regen, item, self._document.revision)
            for item in requirements
            if item.get("kind") == "valid_brep"
        )
        return {
            "ok": True,
            "revision": self._document.revision,
            "units": "mm",
            "gate": reduce_gate(results),
            "results": results,
        }

    def check(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if failure := self._geometry_failure("check"):
                return failure
            if payload.get("action") != "evaluate":
                return _safe_error(
                    self._document.revision,
                    None,
                    "INVALID_PARAMETER",
                    "only check action evaluate is supported",
                    operation="check",
                )
            if payload.get("revision", self._document.revision) != self._document.revision:
                return _safe_error(
                    self._document.revision,
                    None,
                    "REVISION_CONFLICT",
                    "requested revision is not current",
                    operation="check",
                )
            if "requirements" in payload and not payload["requirements"]:
                return _safe_error(
                    self._document.revision,
                    None,
                    "INVALID_PARAMETER",
                    "requirements must contain at least one item",
                    operation="check",
                )
            requirements = payload.get("requirements", [{"id": "valid", "kind": "valid_brep"}])
            supported_requirement_kinds = set(SUPPORTED_REQUIREMENT_KINDS)
            unknown_kinds = [
                item.get("kind")
                for item in requirements
                if item.get("kind") not in supported_requirement_kinds
            ]
            if unknown_kinds:
                return _safe_error(
                    self._document.revision,
                    None,
                    "INVALID_PARAMETER",
                    f"unknown requirement kind: {unknown_kinds[0]}",
                    operation="check",
                    kind=unknown_kinds[0],
                    supportedKinds=sorted(supported_requirement_kinds),
                )
            selected, resolved, failure = self._prepare_assembly_requirements(requirements)
            if failure is not None:
                return failure
            if not selected:
                return self._finish_check_locked(payload, requirements, [])
            revision = self._document.revision

        results, inspection_rows = self._evaluate_assembly_requirements(
            selected, resolved, revision
        )

        with self._lock:
            if self._document.revision != revision:
                return _safe_error(
                    self._document.revision,
                    None,
                    "REVISION_CONFLICT",
                    "document changed during assembly inspection",
                    operation="check",
                    requestedRevision=revision,
                )
            if self._publisher is not None:
                self._publisher.publish_assembly_inspection(revision, inspection_rows)
            return self._finish_check_locked(payload, requirements, results)
