"""Revisioned document operations shared by MCP transports."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import queue
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, fields
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from .board_import import BoardImportError, mounting_holes, read_kicad_board
from .document_store import MAX_RECEIPTS, MAX_REQUEST_HASHES, Document, DocumentStore
from .errors import ERROR_REGISTRY
from .export import (
    ExportFailure,
    _validate_part_for_step,
    export_parts,
    export_parts_separately,
    parts_are_ready,
    prepare_parts,
    public_parts,
)
from .fit_summary import fit_summary, part_facts
from .graph import (
    BoardImport,
    Chamfer,
    Circle,
    Cut,
    Extrude,
    Feature,
    Fillet,
    GraphError,
    Hole,
    HolePosition,
    PcbBase,
    PcbLid,
    Plane,
    PortCutout,
    Rectangle,
    Sketch,
    Standoffs,
)
from .parts import terminal_parts
from .references import (
    BodySource,
    Cardinality,
    HoleSurfaceSource,
    MaxAlong,
    Normal,
    Position,
    Reference,
    SketchSegmentSource,
    edge_role_catalog,
)
from .requirements import InvalidHoleIds, reduce_gate
from .requirements import evaluate as evaluate_requirements
from .schemas import SUPPORTED_REQUIREMENT_KINDS
from .serialization import dumps, loads

if TYPE_CHECKING:
    from .regen import RegenResult
    from .viewer import ScenePart, ViewerSceneStore

_LOG = logging.getLogger(__name__)
_ALLOWED_FEATURE_KINDS = [
    "extrude",
    "cut",
    "hole",
    "fillet",
    "chamfer",
    "pcb_base",
    "standoffs",
    "pcb_lid",
    "port_cutout",
]


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


def regenerate(graph: Any) -> RegenResult:
    return regen_module.accept_regeneration(graph)


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
        raise RequestError(
            "INVALID_PARAMETER", "quantity value must be a number", field=field
        )
    return _finite_number(value["value"], field)


def _positive_quantity(value: Any, field: str) -> float:
    number = _quantity(value, "mm", field)
    if number <= 0:
        raise RequestError("INVALID_PARAMETER", f"{field} must be positive", field=field)
    return number


def _angle(value: Any) -> float:
    if not isinstance(value, dict) or set(value) != {"value", "unit"}:
        raise RequestError("INVALID_PARAMETER", "angle must contain value and unit")
    if value["unit"] not in {"deg", "rad"}:
        raise RequestError(
            "UNIT_MISMATCH",
            f"expected deg or rad; received {value['unit']}",
            expected=["deg", "rad"],
            actual=value["unit"],
        )
    if not isinstance(value["value"], (int, float)):
        raise RequestError("INVALID_PARAMETER", "angle value must be a number", field="rotation")
    angle = _finite_number(value["value"], "rotation")
    converted = math.degrees(angle) if value["unit"] == "rad" else angle
    return _finite_number(converted, "rotation")


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
class SketchPlane:
    kind: str
    name: str
    offset: float


@dataclass(frozen=True)
class SketchChanges:
    position: tuple[float, float, float] | None
    entities: tuple[Rectangle | Circle, ...] | None


@dataclass(frozen=True)
class ExtrudeProfile:
    sketchId: str
    profileIds: tuple[str, ...]


@dataclass(frozen=True)
class ExtrudeParameters:
    profile: ExtrudeProfile
    distance: float
    operation: str
    target: FeatureTarget | None
    direction: str


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


@dataclass(frozen=True)
class PortCutoutChanges:
    footprint: str | None = None
    wall: str | None = None
    clearance: float | None = None
    envelope: PortEnvelope | None = None


@dataclass(frozen=True)
class TargetChanges:
    target: FeatureTarget


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
    entities: tuple[Rectangle | Circle, ...]


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
    set: TargetChanges
    kind: ClassVar[str] = "hole"

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {*super().wire_fields(), "kind"}


DeleteOperation = ImportDelete | SketchDelete | FeatureDelete

MutationOperation = (
    BoardImportOperation
    | ImportDelete
    | SketchCreate
    | SketchUpdate
    | SketchDelete
    | FeatureDelete
    | ExtrudeCreate
    | CutCreate
    | HoleCreate
    | FilletCreate
    | ChamferCreate
    | PcbBaseCreate
    | StandoffsCreate
    | PcbLidCreate
    | PortCutoutCreate
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


def _entities(payload: Any) -> tuple[Rectangle | Circle, ...]:
    if not isinstance(payload, list) or not payload:
        raise RequestError("INVALID_PARAMETER", "entities must not be empty")
    parsed = []
    for entity in payload:
        if not isinstance(entity, dict) or entity.get("kind") not in {"rectangle", "circle"}:
            raise RequestError(
                "INVALID_PARAMETER",
                "unknown sketch entity kind",
                allowedKinds=["rectangle", "circle"],
            )
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


def _parse_sketch(payload: dict[str, Any]) -> SketchCreate | SketchUpdate | SketchDelete:
    if payload.get("action") == "delete":
        return _parse_delete(payload, SketchDelete, "sketchId")
    common = {
        "action": Action(payload.get("action")),
        "expectedRevision": _finite_number(payload.get("expectedRevision"), "expectedRevision"),
        "requestId": payload.get("requestId"),
        "sketchId": payload.get("sketchId"),
    }
    if common["action"] is Action.CREATE:
        plane = payload.get("plane", {})
        return SketchCreate(
            **common,
            plane=SketchPlane(
                plane.get("kind"),
                plane.get("name"),
                _quantity(plane.get("offset", {"value": 0, "unit": "mm"}), "mm", "offset"),
            ),
            entities=_entities(payload.get("entities")),
        )
    changes = payload.get("set")
    if not isinstance(changes, dict) or not changes:
        raise RequestError("INVALID_PARAMETER", "update set must not be empty")
    position = changes.get("position")
    return SketchUpdate(
        **common,
        set=SketchChanges(
            position=(
                tuple(_finite_number(value, "position") for value in position)
                if position is not None
                else None
            ),
            entities=_entities(changes["entities"]) if "entities" in changes else None,
        ),
    )


def _feature_common(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": Action(payload.get("action")),
        "expectedRevision": _finite_number(payload.get("expectedRevision"), "expectedRevision"),
        "requestId": payload.get("requestId"),
        "featureId": payload.get("featureId"),
    }


def _parse_extrude(payload: dict[str, Any], common: dict[str, Any]) -> ExtrudeCreate | ExtrudeUpdate:
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
                profileIds=tuple(profile.get("profileIds", [])),
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
                _finite_number(value, f"positions[{index - 1}].point")
                for value in item["point"]
            ),
        )
        for index, item in enumerate(raw_positions, start=1)
    )
    names = [position.name for position in positions]
    if len(names) != len(set(names)):
        raise RequestError("INVALID_PARAMETER", "hole position names must be unique")
    return positions


def _parse_hole(payload: dict[str, Any], common: dict[str, Any]) -> HoleCreate | HoleUpdate:
    if common["action"] is Action.UPDATE:
        return _parse_target_update(payload, common, HoleUpdate)
    parameters = payload.get("parameters", {})
    return HoleCreate(
        **common,
        parameters=HoleParameters(
            target=FeatureTarget(parameters.get("target", {}).get("featureId")),
            diameter=_positive_quantity(parameters.get("diameter"), "diameter"),
            depth=(
                _positive_quantity(parameters["depth"], "depth")
                if "depth" in parameters
                else None
            ),
            positions=_hole_positions(parameters),
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
            raise RequestError(
                "INVALID_PARAMETER", f"{kind} update requires set.{parameter}"
            )
        return update(
            **common,
            set=EdgeTreatmentChanges(
                size=_positive_quantity(changes[parameter], parameter)
            ),
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
    return {
        name: _feature_quantity(value, name, feature)
        for name, value in changes.items()
    }


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
            set=PcbBaseChanges(**_parse_positive_changes(payload, names, "pcb_base", common["featureId"])),
        )
    parameters = payload.get("parameters", {})
    return PcbBaseCreate(
        **common,
        parameters=PcbBaseParameters(
            board=FeatureTarget(parameters.get("board", {}).get("featureId")),
            **{
                name: _feature_quantity(
                    parameters.get(name), name, common["featureId"]
                )
                for name in names
            },
        ),
    )


def _standoff_selection(parameters: dict[str, Any], feature: str) -> StandoffSelection:
    selection = parameters.get("selection", {})
    mode = selection.get("mode")
    if mode == "explicit":
        hole_ids = selection.get("holeIds")
        if not isinstance(hole_ids, list) or not hole_ids or not all(
            isinstance(item, str) and item for item in hole_ids
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


def _parse_pcb_lid(
    payload: dict[str, Any], common: dict[str, Any]
) -> PcbLidCreate | PcbLidUpdate:
    values = payload.get("set" if common["action"] is Action.UPDATE else "parameters", {})
    if common["action"] is Action.UPDATE:
        if not isinstance(values, dict) or not values or not set(values) <= {
            "thickness",
            "fitGap",
            "lip",
        }:
            raise RequestError(
                "INVALID_PARAMETER",
                "pcb_lid update requires thickness, fitGap, or lip",
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
                    _parse_lid_lip(values["lip"], common["featureId"])
                    if "lip" in values
                    else None
                ),
            ),
        )
    return PcbLidCreate(
        **common,
        parameters=PcbLidParameters(
            base=FeatureTarget(values.get("base", {}).get("featureId")),
            thickness=_feature_quantity(
                values.get("thickness"), "thickness", common["featureId"]
            ),
            fitGap=_feature_quantity(
                values.get("fitGap"), "fitGap", common["featureId"]
            ),
            lip=(
                _parse_lid_lip(values["lip"], common["featureId"])
                if "lip" in values
                else None
            ),
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


def _parse_feature(
    payload: dict[str, Any],
) -> (
    ExtrudeCreate
    | CutCreate
    | HoleCreate
    | FilletCreate
    | ChamferCreate
    | PcbBaseCreate
    | StandoffsCreate
    | PcbLidCreate
    | PortCutoutCreate
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
        "cut": _parse_cut,
        "hole": _parse_hole,
        "fillet": _parse_edge_treatment,
        "chamfer": _parse_edge_treatment,
        "pcb_base": _parse_pcb_base,
        "standoffs": _parse_standoffs,
        "pcb_lid": _parse_pcb_lid,
        "port_cutout": _parse_port_cutout,
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
    details = {
        key: value
        for key, value in error.error.items()
        if key not in {"code", "message"}
    }
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


def _viewer_error(error: dict[str, Any]) -> dict[str, str]:
    """The fields the viewer shows; kernelMessage goes only under "Error details"."""
    return {key: error[key] for key in ("code", "message", "hint", "kernelMessage") if key in error}


def _reference(payload: dict[str, Any]) -> Reference:
    source = payload.get("source")
    if isinstance(source, dict) and source.get("kind") == "sketch_segment":
        source = SketchSegmentSource(
            source["sketch"], source["entity"], source["segment"]
        )
    elif isinstance(source, dict) and source.get("kind") == "hole_surface":
        source = HoleSurfaceSource(
            source["feature"], source["surface"], source["name"]
        )
    body = payload.get("body")
    if isinstance(body, dict) and body.get("kind") == "body_source":
        body = BodySource(body["feature"], body["entity"])
    filters = []
    for item in payload.get("filters", []):
        kind = item.get("kind")
        cls = {"Normal": Normal, "Position": Position, "MaxAlong": MaxAlong}.get(kind)
        if cls is None:
            raise RequestError("INVALID_PARAMETER", f"unknown reference filter: {kind}")
        values = {key: value for key, value in item.items() if key != "kind"}
        values["direction"] = tuple(values["direction"])
        filters.append(cls(**values))
    return Reference(
        feature=payload["feature"],
        role=payload["role"],
        cardinality=Cardinality(payload.get("cardinality", "one")),
        count=payload.get("count"),
        source=source,
        body=body,
        filters=filters,
    )


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


def _apply_delete(
    graph: Any, operation: DeleteOperation, committed: RegenResult
) -> _DeleteResult:
    target = _delete_target(operation)
    expected, noun = _delete_target_kind(operation)
    if not isinstance(graph.nodes.get(target), expected):
        raise RequestError("UNKNOWN_ID", f"unknown {noun}: {target}")

    ordered = graph.ordered()
    dependent_ids = {
        item.id for item in graph.transitive_dependents(target, committed.predecessors)
    }
    dependents = [
        {"id": item.id, "kind": _node_kind(item)}
        for item in ordered
        if item.id in dependent_ids
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
        summary += f" with {'dependent' if len(dependents) == 1 else 'dependents'} {', '.join(dependents)}"
    fields = {
        "deleted": list(deletion.deleted),
        "summary": summary,
        "partsRemoved": removed_parts,
        "partsChanged": changed_parts,
    }
    return fields, {"featureId": deletion.target_id, "summary": summary}


def _apply_sketch(
    graph: Any,
    operation: SketchCreate | SketchUpdate | SketchDelete,
    committed: RegenResult | None,
) -> str | _DeleteResult:
    if isinstance(operation, SketchDelete):
        assert committed is not None
        return _apply_delete(graph, operation, committed)
    if isinstance(operation, SketchCreate):
        if operation.plane.kind != "origin":
            raise RequestError("INVALID_PARAMETER", "only origin planes are supported")
        graph.add(
            Sketch(
                operation.sketchId,
                plane=Plane(operation.plane.name, operation.plane.offset),
                entities=list(operation.entities),
            )
        )
    else:
        node = graph.nodes.get(operation.sketchId)
        if not isinstance(node, Sketch):
            raise RequestError("UNKNOWN_ID", f"unknown sketch: {operation.sketchId}")
        if operation.set.position is not None:
            node.position = operation.set.position
        if operation.set.entities is not None:
            node.entities = list(operation.set.entities)
    return operation.sketchId


def _validate_body_target(graph: Any, target: str | None, operation: str) -> None:
    if target not in graph.nodes:
        raise RequestError("UNKNOWN_ID", f"{operation} target feature does not exist")
    if not graph.produces_body(target):
        raise RequestError("INVALID_PARAMETER", f"{operation} target must produce a body")


def _apply_extrude(graph: Any, operation: ExtrudeCreate | ExtrudeUpdate) -> str:
    if isinstance(operation, ExtrudeUpdate):
        node = graph.nodes.get(operation.featureId)
        if not isinstance(node, Extrude):
            raise RequestError("UNKNOWN_ID", f"unknown extrude: {operation.featureId}")
        node.height = operation.set.distance
        return operation.featureId

    parameters = operation.parameters
    if parameters.operation not in {"new_body", "join"} or parameters.direction != "normal":
        raise RequestError("INVALID_PARAMETER", "only normal new_body and join extrudes are supported")
    target = parameters.target.featureId if parameters.target is not None else None
    if parameters.operation == "join":
        _validate_body_target(graph, target, "join")
    elif target is not None:
        raise RequestError("INVALID_PARAMETER", "new_body extrudes do not accept a target")
    sketch = graph.nodes.get(parameters.profile.sketchId)
    if not isinstance(sketch, Sketch):
        raise RequestError("UNKNOWN_ID", "extrude profile sketch does not exist")
    if list(parameters.profile.profileIds) != [entity.id for entity in sketch.entities]:
        raise RequestError(
            "INVALID_PARAMETER",
            "profileIds must list all sketch profiles in graph order",
        )
    graph.add(
        Extrude(
            operation.featureId,
            sketch=sketch.id,
            height=parameters.distance,
            operation=parameters.operation,
            target=target,
        )
    )
    return operation.featureId


def _apply_target_update(
    graph: Any,
    operation: CutUpdate | HoleUpdate,
    feature_type: type[Cut | Hole],
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
) -> str:
    feature_type = Fillet if operation.kind == "fillet" else Chamfer
    parameter = "radius" if operation.kind == "fillet" else "distance"
    if isinstance(operation, (FilletUpdate, ChamferUpdate)):
        node = graph.nodes.get(operation.featureId)
        if not isinstance(node, feature_type):
            raise RequestError(
                "UNKNOWN_ID", f"unknown {operation.kind}: {operation.featureId}"
            )
        setattr(node, parameter, operation.set.size)
        return operation.featureId

    parameters = operation.parameters
    target = parameters.target.featureId
    _validate_body_target(graph, target, operation.kind)
    graph.add(
        feature_type(
            operation.featureId,
            target=target,
            edges=list(parameters.edges),
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


def _apply_hole(graph: Any, operation: HoleCreate | HoleUpdate) -> str:
    if isinstance(operation, HoleUpdate):
        return _apply_target_update(graph, operation, Hole)
    parameters = operation.parameters
    _validate_body_target(graph, parameters.target.featureId, "hole")
    graph.add(
        Hole(
            operation.featureId,
            target=parameters.target.featureId,
            diameter=parameters.diameter,
            depth=parameters.depth,
            positions=parameters.positions,
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


def _apply_standoffs(
    graph: Any, operation: StandoffsCreate | StandoffsUpdate
) -> str:
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


def _apply_pcb_lid(graph: Any, operation: PcbLidCreate | PcbLidUpdate) -> str:
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
    graph.add(
        PcbLid(
            operation.featureId,
            base=base.id,
            thickness=parameters.thickness,
            fit_gap=parameters.fitGap,
            lip_depth=parameters.lip.depth if parameters.lip else None,
            lip_width=parameters.lip.width if parameters.lip else None,
        )
    )
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


def _apply_port_cutout(
    graph: Any, operation: PortCutoutCreate | PortCutoutUpdate
) -> str:
    if isinstance(operation, PortCutoutUpdate):
        return _update_port_cutout(graph, operation)
    return _create_port_cutout(graph, operation)


def _apply_feature(
    graph: Any, operation: MutationOperation, committed: RegenResult | None
) -> str | _DeleteResult:
    if isinstance(operation, FeatureDelete):
        assert committed is not None
        return _apply_delete(graph, operation, committed)
    applier = {
        ExtrudeCreate: _apply_extrude,
        ExtrudeUpdate: _apply_extrude,
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
    }[type(operation)]
    return applier(graph, operation)


def _entity_segments(entities: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        entity["id"]: (
            ["bottom", "right", "top", "left"]
            if entity["kind"] == "rectangle"
            else ["circle"]
        )
        for entity in entities
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
    if isinstance(node, BoardImport):
        return "board import"
    return type(node).__name__.lower()


def _hole_names(node: Hole) -> list[str | None]:
    return [position.name for position in node.positions] or [None]


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
        if requested_id in {
            feature["id"] for feature in _lineage(result, part.terminal_feature)
        }
    ]
    return matches[0] if len(matches) == 1 else None


def _lineage(result: RegenResult, feature_id: str) -> tuple[dict[str, Any], ...]:
    """The features that built a terminal part, oldest first."""
    chain = []
    while feature_id:
        node = result.graph.nodes[feature_id]
        entry = {"id": node.id, "kind": _kind(node)}
        if isinstance(node, Hole):
            entry["holes"] = _hole_names(node)
        chain.append(entry)
        feature_id = result.predecessors.get(feature_id, "")
    return tuple(reversed(chain))


def _mm(value: float | tuple[float, ...]) -> str:
    if isinstance(value, tuple):
        return "(" + ", ".join(f"{c:g}" for c in value) + ")"
    return f"{value:g}"


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
    """"pcb lid lid" says the id twice; keep the id only when it adds a word."""
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


def _point(value: Any) -> list[float]:
    return [float(value[0]), float(value[1])]


def _delta_items(before: list[dict], after: list[dict], *, reference: str = "reference") -> list[dict]:
    old = {item.get("id"): item for item in before}
    new = {item.get("id"): item for item in after}
    changes = []
    for item_id in sorted(set(old) | set(new)):
        was, now = old.get(item_id), new.get(item_id)
        old_position = _point(was["position"]) if was and was.get("position") else None
        new_position = _point(now["position"]) if now and now.get("position") else None
        distance = None
        if old_position is not None and new_position is not None:
            distance = math.hypot(new_position[0] - old_position[0], new_position[1] - old_position[1])
            if distance <= 1e-6:
                continue
        changes.append({
            "id": item_id,
            "reference": (now or was).get(reference),
            "oldPosition": old_position,
            "newPosition": new_position,
            "distance": distance,
        })
    return changes


def _dependent_features(graph: Any, board_id: str) -> list[str]:
    affected = {board_id}
    changed = True
    while changed:
        changed = False
        for node_id, node in graph.nodes.items():
            refs = {getattr(node, name, None) for name in ("board", "base", "target")}
            if node_id not in affected and refs & affected:
                affected.add(node_id)
                changed = True
    return sorted(affected - {board_id})


def _board_changes(before: Any, after: Any) -> dict[str, Any]:
    old = before.mechanics if isinstance(before, BoardImport) else {}
    new = after.mechanics if isinstance(after, BoardImport) else {}
    old_holes = [hole for hole in old.get("holes", []) if hole.get("mounting")]
    new_holes = [hole for hole in new.get("holes", []) if hole.get("mounting")]
    mounting_ids = {item["id"] for item in old_holes + new_holes}
    mounting_references = {item.get("reference") for item in old_holes + new_holes}
    old_footprints = {
        item["id"]: item
        for item in old.get("footprints", [])
        if item["id"] not in mounting_ids and item.get("reference") not in mounting_references
    }
    new_footprints = {
        item["id"]: item
        for item in new.get("footprints", [])
        if item["id"] not in mounting_ids and item.get("reference") not in mounting_references
    }
    return {
        "mountingHoles": _delta_items(old_holes, new_holes, reference="reference"),
        "footprints": _delta_items(list(old_footprints.values()), list(new_footprints.values())),
        "outlineChanged": old.get("outline") != new.get("outline"),
    }


def _edited(old: Any, node: Any) -> str:
    if isinstance(node, BoardImport):
        same = old.source_digest == node.source_digest
        return f"refreshed board {node.id}, source {'unchanged' if same else 'changed'}"
    edits = []
    for name, (word, values) in _EDITED_FIELDS.items():
        was, now = getattr(old, name, None), getattr(node, name, None)
        if was != now:
            edits.append(f"{word}: {values(was, now)}")
    summary = f"edited {_kind(node)} {node.id}"
    return f"{summary} {', '.join(edits)}" if edits else f"{summary}, no value changed"


def _selection_snapshot(graph: Any) -> dict[str, list[str]]:
    snapshot = {}
    for node in graph.nodes.values():
        if isinstance(node, Standoffs) and node.selection == "all_mounting":
            board = graph.nodes.get(node.board)
            if isinstance(board, BoardImport):
                snapshot[node.id] = sorted(hole["id"] for hole in mounting_holes(board))
    return snapshot


def _selection_changes(
    before: dict[str, list[str]], after: dict[str, list[str]]
) -> list[dict[str, Any]]:
    changes = []
    # creating or deleting a selector feature is not churn; only a selector that existed
    # before and after can gain or lose holes.
    for feature_id in sorted(set(before) & set(after)):
        added = sorted(set(after[feature_id]) - set(before[feature_id]))
        removed = sorted(set(before[feature_id]) - set(after[feature_id]))
        if added or removed:
            changes.append({
                "featureId": feature_id,
                "selector": "all_mounting",
                "holesAdded": added,
                "holesRemoved": removed,
            })
    return changes


def _change(before: Any, result: RegenResult, changed_id: str) -> dict[str, str]:
    """One line that tells the human what a commit did: added what, or which values changed."""
    node = result.graph.nodes[changed_id]
    old = before.nodes.get(changed_id)
    if isinstance(node, BoardImport) and isinstance(old, BoardImport) and node.source_digest != old.source_digest:
        delta = _board_changes(old, node)
        moved = [item for item in delta["footprints"] if item["distance"] is not None]
        labels = [f"{item['reference']} moved {item['distance']:.1f} mm" for item in moved if item.get("reference")]
        holes = sum(item["distance"] is not None for item in delta["mountingHoles"])
        if holes:
            labels.append(f"{holes} mounting hole{'s' if holes != 1 else ''} moved")
        dependent = [
            node_id for node_id in _dependent_features(result.graph, changed_id)
            if _kind(result.graph.nodes[node_id]) in {"standoffs", "port cutout"}
        ]
        if dependent:
            names = {
                node_id: _kind(result.graph.nodes[node_id])
                for node_id in dependent
            }
            dependent_labels = [names[node_id] for node_id in dependent]
            joined = (
                dependent_labels[0]
                if len(dependent_labels) == 1
                else " and ".join(dependent_labels)
                if len(dependent_labels) == 2
                else ", ".join(dependent_labels[:-1]) + " and " + dependent_labels[-1]
            )
            labels.append(joined + " updated")
        return {"featureId": changed_id, "summary": "refreshed board: " + (", ".join(labels) if labels else "source changed")}
    return {"featureId": changed_id, "summary": _added(node, result) if old is None else _edited(old, node)}


def _scene_parts(result: RegenResult) -> Iterable[ScenePart]:
    from .viewer import ScenePart

    # body creation order, not regen order: a later feature on one body must not move its row or color
    order = {feature: index for index, feature in enumerate(result.graph.nodes)}
    parts = terminal_parts(result, role=None)
    for part in sorted(parts, key=lambda p: (order[p.source_body.feature], p.part_id)):
        yield ScenePart(
            part.part_id,
            part.shape,
            role=part.role,
            label=part.label,
            source_body=part.source_body,
            terminal_feature=part.terminal_feature,
            area=part.shape.area,
            volume=part.shape.volume,
            features=_lineage(result, part.terminal_feature),
            facts=part_facts(result, part.source_body.feature),
        )


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
        before: Any = None,
        changed_id: str | None = None,
        change: dict[str, str] | None = None,
    ) -> None:
        """Describe the commit when supplied; disk startup publishes without a change."""
        self._jobs.put(("publish", (revision, result, before, changed_id, change)))

    def publish_failure(self, feature_ids: list[str], error: dict[str, str]) -> None:
        self._jobs.put(("publish_failure", (feature_ids, error)))

    def _run(self) -> None:
        while True:
            method, arguments = self._jobs.get()
            try:
                if method == "publish":
                    revision, result, before, changed_id, change = arguments
                    if change is None and changed_id:
                        change = _change(before, result, changed_id)
                    self._store.publish(revision, _scene_parts(result), change, fit_summary(result, revision))
                else:
                    self._store.publish_failure(*arguments)
            except Exception:  # noqa: BLE001 - viewer work must not affect document calls.
                _LOG.exception("viewer publish failed")


def _valid_brep_result(regen: Any, requirement: dict[str, Any], revision: int) -> dict[str, Any]:
    parts = terminal_parts(regen)
    valid = bool(parts) and all(
        BRepCheck_Analyzer(part.shape.wrapped).IsValid() for part in parts
    )
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
    ):
        self.store = DocumentStore(path)
        self._lock = threading.RLock()
        self._document = self.store.load()
        self._regen = None
        self._graph_error: GraphError | None = None
        self._viewer_url = viewer_url
        self._publisher = _ScenePublisher(scene_store) if scene_store is not None else None
        self._kernel_loader = kernel_loader or _KERNEL_LOADER
        self._warmup_started = False
        self._warmup_lock = threading.Lock()
        if warmup:
            self.start_kernel_warmup()

    def start_kernel_warmup(self) -> None:
        with self._warmup_lock:
            if self._warmup_started:
                return
            self._warmup_started = True
            threading.Thread(target=self._finish_startup, name="coalesce-kernel", daemon=True).start()

    def _finish_startup(self) -> None:
        try:
            self._kernel_loader.start()
            self._kernel_loader.wait()
            with self._lock:
                if self._regen is not None or self._graph_error is not None:
                    return
                try:
                    result = regenerate(self._document.graph)
                except GraphError as error:
                    self._record_graph_error(error)
                    return
                self._regen = result
                if self._publisher is not None and self._document.revision > 0:
                    self._publisher.publish(self._document.revision, result)
        except Exception:  # noqa: BLE001 - startup errors must not kill the server
            _LOG.exception("kernel startup failed")

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
                result = regenerate(self._document.graph)
            except GraphError as error:
                self._record_graph_error(error)
                raise
            self._regen = result
            if self._publisher is not None and self._document.revision > 0:
                self._publisher.publish(self._document.revision, result)

    def _geometry_failure(self, operation: str) -> dict[str, Any] | None:
        try:
            self._ensure_geometry()
        except GraphError as error:
            return _graph_error_response(self._document.revision, None, operation, error)
        return None

    @property
    def revision(self) -> int:
        return self._document.revision

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
        if not isinstance(operation, (ImportDelete, SketchDelete, FeatureDelete)):
            return None
        self._ensure_geometry()
        assert self._regen is not None
        return self._regen

    @staticmethod
    def _validate_treatment_parts(
        operation: MutationOperation, result: RegenResult, changed_id: str
    ) -> None:
        if not isinstance(operation, (FilletCreate, FilletUpdate, ChamferCreate, ChamferUpdate)):
            return
        parts = [
            part
            for part in terminal_parts(result)
            if changed_id in {item["id"] for item in _lineage(result, part.terminal_feature)}
        ]
        for part in parts:
            try:
                _validate_part_for_step(part)
            except ExportFailure as error:
                raise RequestError(
                    "MANUFACTURING_VALIDATION_FAILED",
                    f"{operation.kind} {changed_id} cannot produce matching STEP geometry for {part.part_id}",
                    featureId=changed_id,
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
            for field in ("featureId", "sketchId", "assetId")
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
                & {item["id"] for item in _lineage(self._regen, part.terminal_feature)}
            ]
            if affected:
                error["affectedParts"] = affected
        return failure

    def publish_failure(self, payload: dict[str, Any], result: dict[str, Any]) -> None:
        """Show a failed sketch or feature call in the viewer; the last good scene stays."""
        if self._publisher is None:
            return
        feature_id = payload.get("featureId") or payload.get("sketchId") or payload.get("assetId")
        error = result["error"]
        self._publisher.publish_failure(
            [feature_id] if isinstance(feature_id, str) else [],
            _viewer_error(error),
        )

    def _mutate(
        self,
        operation: str,
        payload: dict[str, Any],
        parse: Callable[[dict[str, Any]], MutationOperation],
        apply: Callable[
            [Any, MutationOperation, RegenResult | None], str | _DeleteResult
        ],
        response_fields: Callable[[Any, RegenResult, str], dict[str, Any]],
    ) -> dict[str, Any]:
        request_id = payload.get("requestId")
        with self._lock:
            try:
                if not isinstance(request_id, str) or not request_id:
                    raise RequestError("INVALID_PARAMETER", "requestId is required")
                parsed = parse(payload)
                body_hash = _hash(parsed)
                receipt = next(
                    (
                        item
                        for item in self._document.receipts
                        if item["requestId"] == request_id
                    ),
                    None,
                )
                if receipt is not None:
                    if receipt["requestHash"] == body_hash:
                        return receipt["response"]
                    raise RequestError(
                        "REQUEST_ID_REUSED",
                        "requestId was already used for a different request body",
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
                expected = parsed.expectedRevision
                if expected != self._document.revision:
                    raise RequestError(
                        "REVISION_CONFLICT",
                        f"expected revision {expected}; current revision is {self._document.revision}",
                        expected=expected,
                        actual=self._document.revision,
                    )

                self.start_kernel_warmup()
                self._kernel_loader.wait()
                previous_selections = _selection_snapshot(self._document.graph)
                graph = loads(dumps(self._document.graph))
                applied = apply(graph, parsed, self._committed_regeneration(parsed))
                changed_id, changed_ids, deletion = _applied_result(applied)
                result = regenerate(graph)
                self._assert_effect(parsed, result)
                self._validate_treatment_parts(parsed, result, changed_id)
                deletion_fields, change = _deletion_result_fields(deletion, result)
                previous = self._document.revision
                response = {
                    "ok": True,
                    "revision": previous + 1,
                    "previousRevision": previous,
                    "requestId": request_id,
                    "units": "mm",
                    "changedFeatureIds": changed_ids,
                    "regeneration": {"status": "OK", "valid": True},
                    "warnings": [],
                    "selectionChanges": _selection_changes(
                        previous_selections, _selection_snapshot(graph)
                    ),
                    **deletion_fields,
                    **response_fields(graph, result, changed_id),
                }
                if isinstance(parsed, BoardImportOperation) and parsed.action is Action.REFRESH:
                    response["boardChanges"] = _board_changes(
                        self._document.graph.nodes.get(parsed.assetId), graph.nodes.get(parsed.assetId)
                    )
                    if any(response["boardChanges"][key] for key in ("mountingHoles", "footprints")) or response["boardChanges"]["outlineChanged"]:
                        response["boardChanges"]["dependentFeatures"] = _dependent_features(graph, parsed.assetId)
                    else:
                        response["boardChanges"]["dependentFeatures"] = []
                receipt = {
                    "requestId": request_id,
                    "requestHash": body_hash,
                    "revision": previous + 1,
                    "response": response,
                }
                document = Document(
                    graph=graph,
                    revision=previous + 1,
                    receipts=[*self._document.receipts, receipt][-MAX_RECEIPTS:],
                    request_hashes=[
                        *self._document.request_hashes,
                        {"requestId": request_id, "requestHash": body_hash},
                    ][-MAX_REQUEST_HASHES:],
                )
                durable = self.store.save(document)
                before = self._document.graph
                self._document = document
                self._regen = result
                self._graph_error = None
                if self._publisher is not None:
                    self._publisher.publish(document.revision, result, before, changed_id, change)
                if not durable:
                    _LOG.warning(
                        "document rename committed but directory fsync failed; durability is unconfirmed"
                    )
                return response
            except RequestError as error:
                failure = self.error_response(
                    operation,
                    payload,
                    error.code,
                    str(error),
                    **error.details,
                )
                self.publish_failure(payload, failure)
                return failure
            except BoardImportError as error:
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
                self.publish_failure(payload, failure)
                return failure
            except GraphError as error:
                graph_failure = _graph_error_response(
                    self._document.revision, request_id, operation, error
                )
                graph_details = dict(graph_failure["error"])
                code = graph_details.pop("code")
                message = graph_details.pop("message")
                graph_details.pop("retryable")
                graph_details.pop("operation")
                failure = self.error_response(
                    operation,
                    payload,
                    code,
                    message,
                    **graph_details,
                )
                self.publish_failure(payload, failure)
                return failure
            except Exception:  # noqa: BLE001 - translate all kernel failures at this seam.
                _LOG.exception("kernel failure during %s", operation)
                failure = self.error_response(
                    operation,
                    payload,
                    "KERNEL_OPERATION_FAILED",
                    "the modeling operation could not produce valid geometry",
                )
                self.publish_failure(payload, failure)
                return failure

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

    def _cut_metadata(
        self, graph: Any, result: RegenResult, node: Cut
    ) -> dict[str, Any]:
        from .cut import make_cut_tool

        sketch = graph.nodes[node.tool]
        tool = make_cut_tool(node, sketch, result.shapes[node.target]).shape
        normal = self._frame(sketch)["normal"]
        direction = (
            normal
            if node.depth is None
            else [value if node.flip else -value for value in normal]
        )
        return {
            "frame": self._frame(sketch),
            "direction": direction,
            "toolBBox": self._bbox([tool]),
        }

    def _hole_metadata(self, result: RegenResult, node: Hole) -> dict[str, Any]:
        from .hole import make_hole_tool

        sketch = Sketch("global_xy")
        tool = make_hole_tool(node, result.shapes[node.target]).shape
        return {
            "frame": self._frame(sketch),
            "direction": [0.0, 0.0, 1.0],
            "toolBBox": self._bbox([tool]),
            "positionNames": [
                position.name
                for position in (node.positions or (HolePosition(None, node.position),))
            ],
        }

    def _node_metadata(
        self, graph: Any, result: RegenResult, node_id: str
    ) -> dict[str, Any]:
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
                item
                for item in json.loads(dumps(graph))["nodes"]
                if item["id"] == changed_id
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
            if entities:
                fields["entitySegments"] = _entity_segments(entities)
            return fields

        return self._mutate(
            "sketch",
            payload,
            _parse_sketch,
            _apply_sketch,
            response,
        )

    def _resolve_update_kind(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Fill an update's kind from the stored feature; a different given kind is an error."""
        with self._lock:
            node = self._document.graph.nodes.get(payload.get("featureId"))
            revision = self.revision
        request_id = payload.get("requestId")
        if not isinstance(node, Feature):
            noun = payload.get("kind") or "feature"
            return payload, _safe_error(
                revision, request_id, "UNKNOWN_ID",
                f"unknown {noun}: {payload.get('featureId')}", operation="feature",
                featureId=payload.get("featureId"),
            )
        expected_kind, actual_kind = _infer_update_kind(payload, node)
        if actual_kind is not None and actual_kind != expected_kind:
            return payload, _safe_error(
                revision, request_id, "INVALID_PARAMETER",
                f"feature {node.id} is {expected_kind}, not {actual_kind}",
                operation="feature", featureId=node.id,
                expectedKind=expected_kind, actualKind=actual_kind,
            )
        return {**payload, "kind": expected_kind}, None

    def feature(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("action") == "update":
            payload, failure = self._resolve_update_kind(payload)
            if failure is not None:
                failure = self._with_error_context(payload, failure)
                self.publish_failure(payload, failure)
                return failure
        return self._mutate(
            "feature",
            payload,
            _parse_feature,
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
            f"expected revision {expected_revision}; current revision is "
            f"{self._document.revision}",
            expected=expected_revision,
            actual=self._document.revision,
            hint="query the current revision and review the changed model before exporting",
        )

    def _select_export_parts(self, requested: list[str] | None) -> list[Any]:
        available = [
            part
            for part in terminal_parts(self._regen)
            if not isinstance(
                self._document.graph.nodes.get(part.source_body.feature), BoardImport
            )
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
    def _check_export_request(payload: dict[str, Any]) -> None:
        action = payload.get("action", "publish")
        mode = payload.get("mode", "compound")
        if action == "preflight":
            forbidden = [field for field in ("path", "mode") if field in payload]
            if forbidden:
                raise RequestError(
                    "INVALID_PARAMETER",
                    "export preflight does not accept path or mode",
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
                **(
                    {"requirements": payload["requirements"]}
                    if "requirements" in payload
                    else {}
                ),
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

    def export(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_geometry()
        with self._lock:
            selected: list[Any] = []
            try:
                self._check_export_revision(payload["expectedRevision"])
                self._check_export_request(payload)
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

    def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = payload.get("select", {}).get("kind")
        if kind in {"measurements", "feature", "reference", "edge_roles", "enclosure"} and (
            failure := self._geometry_failure("query")
        ):
            return failure
        with self._lock:
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
            if kind in {"document", "graph", "timeline"}:
                return {**base, "graph": json.loads(dumps(self._document.graph))}
            if kind == "revision":
                return base
            if kind == "server":
                return {**base, "viewerUrl": self._viewer_url}
            if kind == "measurements":
                return {**base, **self._metrics(select.get("role", "manufacturing"))}
            if kind == "feature":
                return self._query_feature(select, base)
            if kind == "enclosure":
                return self._query_enclosure(select, base)
            if kind == "reference":
                return self._query_reference(select, base)
            if kind == "edge_roles":
                return self._query_edge_roles(select, base)
            return _safe_error(
                self._document.revision,
                None,
                "INVALID_PARAMETER",
                f"unknown query kind: {kind}",
                operation="query",
            )

    def _query_enclosure(
        self, select: dict[str, Any], base: dict[str, Any]
    ) -> dict[str, Any]:
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

    def _query_feature(
        self, select: dict[str, Any], base: dict[str, Any]
    ) -> dict[str, Any]:
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

    def _query_edge_roles(
        self, select: dict[str, Any], base: dict[str, Any]
    ) -> dict[str, Any]:
        feature = select.get("id")
        if feature not in self._document.graph.nodes:
            return _safe_error(
                self._document.revision,
                None,
                "UNKNOWN_ID",
                f"unknown feature: {feature}",
                operation="query.edge_roles",
            )
        return {**base, "featureId": feature, "roles": edge_role_catalog(self._regen.provenance, feature)}

    def _query_reference(
        self, select: dict[str, Any], base: dict[str, Any]
    ) -> dict[str, Any]:
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
                key: result[key] for key in ("status", "keys", "fingerprints", "candidates", "error")
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

    def check(self, payload: dict[str, Any]) -> dict[str, Any]:
        if failure := self._geometry_failure("check"):
            return failure
        with self._lock:
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
            requirements = payload.get(
                "requirements", [{"id": "valid", "kind": "valid_brep"}]
            )
            supported_requirement_kinds = set(SUPPORTED_REQUIREMENT_KINDS)
            unknown_kinds = [
                item.get("kind") for item in requirements
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
            results = []
            enclosure_requirements = [
                item for item in requirements if item.get("kind") != "valid_brep"
            ]
            if enclosure_requirements:
                try:
                    from .enclosure_inspection import inspect_enclosure

                    inspection = inspect_enclosure(
                        self._document.graph, self._regen, payload.get("base", "base")
                    )
                    results.extend(
                        evaluate_requirements(
                            enclosure_requirements, inspection, self._document.revision
                        )["results"]
                    )
                except InvalidHoleIds as error:
                    return _safe_error(
                        self._document.revision,
                        None,
                        "INVALID_PARAMETER",
                        str(error),
                        operation="check",
                        invalidHoleIds=error.invalid_hole_ids,
                        validHoles=error.valid_holes,
                    )
                except (KeyError, TypeError, ValueError, RuntimeError) as error:
                    return _safe_error(
                        self._document.revision,
                        None,
                        "INSPECTION_FAILED",
                        "enclosure inspection could not be completed",
                        operation="check",
                        kernelMessage=str(error),
                    )
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
