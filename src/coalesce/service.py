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

from .document_store import MAX_RECEIPTS, MAX_REQUEST_HASHES, Document, DocumentStore
from .graph import (
    Circle,
    Cut,
    Extrude,
    GraphError,
    Hole,
    HolePosition,
    Plane,
    Rectangle,
    Sketch,
)
from .references import (
    BodySource,
    Cardinality,
    HoleSurfaceSource,
    MaxAlong,
    Normal,
    Position,
    Reference,
    SketchSegmentSource,
)
from .serialization import dumps, loads

if TYPE_CHECKING:
    from .regen import RegenResult
    from .viewer import ScenePart, ViewerSceneStore

_LOG = logging.getLogger(__name__)
_ALLOWED_FEATURE_KINDS = ["extrude", "cut", "hole"]


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
        global regenerate, RegenResult, BRepCheck_Analyzer, Compound
        try:
            from build123d import Compound as _Compound
            from OCP.BRepCheck import BRepCheck_Analyzer as _BRepCheck_Analyzer

            from .regen import RegenResult as _RegenResult
            from .regen import regenerate as _regenerate

            Compound = _Compound
            BRepCheck_Analyzer = _BRepCheck_Analyzer
            RegenResult = _RegenResult
            regenerate = _regenerate
            result = _KernelLoadResult()
        except Exception as error:  # noqa: BLE001 - preserve the original startup failure
            result = _KernelLoadResult(error=error)
        with self._lock:
            self._result = result
            self._ready.set()


_KERNEL_LOADER = _KernelLoader()


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


class _Operation:
    action: Action
    expectedRevision: float
    requestId: str

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {field.name for field in fields(cls)}


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
class ExtrudeChanges:
    distance: float


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


MutationOperation = (
    SketchCreate | SketchUpdate | ExtrudeCreate | CutCreate | HoleCreate | ExtrudeUpdate
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


def _parse_sketch(payload: dict[str, Any]) -> SketchCreate | SketchUpdate:
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


def _parse_cut(payload: dict[str, Any], common: dict[str, Any]) -> CutCreate:
    if common["action"] is Action.UPDATE:
        raise RequestError("INVALID_PARAMETER", "only extrude features can be updated")
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


def _parse_hole(payload: dict[str, Any], common: dict[str, Any]) -> HoleCreate:
    if common["action"] is Action.UPDATE:
        raise RequestError("INVALID_PARAMETER", "only extrude features can be updated")
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


def _parse_feature(payload: dict[str, Any]) -> ExtrudeCreate | CutCreate | HoleCreate | ExtrudeUpdate:
    kind = payload.get("kind")
    parser = {"extrude": _parse_extrude, "cut": _parse_cut, "hole": _parse_hole}.get(kind)
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
    retryable: bool = False,
    **details: Any,
) -> dict[str, Any]:
    return {
        "ok": False,
        "revision": revision,
        "requestId": request_id,
        "units": "mm",
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
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
    code = error.error["code"].upper()
    known = {
        "UNKNOWN_ID",
        "DUPLICATE_ID",
        "CYCLE",
        "ORDER",
        "EMPTY",
        "SPLIT",
        "MERGED",
        "AMBIGUOUS",
        "JOIN_NO_CONTACT",
        "JOIN_NON_MANIFOLD",
        "HOLES_INTERSECT",
        "HOLE_NO_EFFECT",
    }
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
        code if code in known else "KERNEL_OPERATION_FAILED",
        str(error) if code in known else "the modeling operation could not produce valid geometry",
        operation=operation,
        **details,
    )


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


def _apply_sketch(graph: Any, operation: SketchCreate | SketchUpdate) -> str:
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


def _validate_join_target(graph: Any, target: str | None) -> None:
    if target not in graph.nodes:
        raise RequestError("UNKNOWN_ID", "join target feature does not exist")
    if not graph.produces_body(target):
        raise RequestError("INVALID_PARAMETER", "join target must produce a body")


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
        _validate_join_target(graph, target)
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


def _apply_cut(graph: Any, operation: CutCreate) -> str:
    parameters = operation.parameters
    if not isinstance(graph.nodes.get(parameters.target.featureId), (Extrude, Cut, Hole)):
        raise RequestError("UNKNOWN_ID", "cut target feature does not exist")
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


def _apply_hole(graph: Any, operation: HoleCreate) -> str:
    parameters = operation.parameters
    if not isinstance(graph.nodes.get(parameters.target.featureId), (Extrude, Cut, Hole)):
        raise RequestError("UNKNOWN_ID", "hole target feature does not exist")
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


def _apply_feature(
    graph: Any,
    operation: ExtrudeCreate | CutCreate | HoleCreate | ExtrudeUpdate,
) -> str:
    applier = {
        ExtrudeCreate: _apply_extrude,
        ExtrudeUpdate: _apply_extrude,
        CutCreate: _apply_cut,
        HoleCreate: _apply_hole,
    }[type(operation)]
    return applier(graph, operation)


def _terminal_shapes(result: RegenResult) -> list[tuple[str, Any]]:
    consumed = {
        node.target
        for node in result.graph.nodes.values()
        if getattr(node, "target", None) in result.shapes
    }
    return [(feature, shape) for feature, shape in result.shapes.items() if feature not in consumed]


def _body_name(solid: Any, owned: list[Any]) -> str:
    faces = [face.wrapped for face in solid.faces()]
    bodies = sorted(
        {
            candidate.key.body.entity
            for candidate in owned
            if any(candidate.entity.wrapped.IsSame(face) for face in faces)
        }
    )
    return "+".join(bodies) or "solid"


def _split_suffixes(names: list[str]) -> list[str]:
    # a cut can split one body into several solids; scene names must stay unique.
    return [
        f" ({names[: index + 1].count(name)})" if names.count(name) > 1 else ""
        for index, name in enumerate(names)
    ]


def _entity_segments(entities: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        entity["id"]: (
            ["bottom", "right", "top", "left"]
            if entity["kind"] == "rectangle"
            else ["circle"]
        )
        for entity in entities
    }


def _scene_parts(result: RegenResult) -> Iterable[ScenePart]:
    from .viewer import ScenePart

    owned = [
        candidate
        for candidates in result.provenance.values()
        for candidate in candidates
        if candidate.key.body is not None and candidate.entity is not None
    ]
    for feature, shape in _terminal_shapes(result):
        solids = shape.solids()
        bodies = [_body_name(solid, owned) for solid in solids]
        several = len(set(bodies)) > 1
        for body, suffix, solid in zip(bodies, _split_suffixes(bodies), solids, strict=True):
            label = f"{feature} · {body}{suffix}" if several else f"{feature}{suffix}"
            yield ScenePart(f"{feature}/{body}{suffix}", solid, label=label)


class _ScenePublisher:
    """Moves committed scene work off the MCP response path, in commit order."""

    def __init__(self, store: ViewerSceneStore):
        self._store = store
        self._jobs: queue.Queue[tuple[str, tuple[Any, ...]]] = queue.Queue()
        threading.Thread(target=self._run, name="coalesce-viewer-publisher", daemon=True).start()

    def publish(self, revision: int, result: RegenResult) -> None:
        self._jobs.put(("publish", (revision, result)))

    def publish_failure(self, feature_ids: list[str], error: dict[str, str]) -> None:
        self._jobs.put(("publish_failure", (feature_ids, error)))

    def _run(self) -> None:
        while True:
            method, arguments = self._jobs.get()
            try:
                if method == "publish":
                    revision, result = arguments
                    self._store.publish(revision, _scene_parts(result))
                else:
                    self._store.publish_failure(*arguments)
            except Exception:  # noqa: BLE001 - viewer work must not affect document calls.
                _LOG.exception("viewer publish failed")


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
                if self._regen is not None:
                    return
                result = regenerate(self._document.graph)
                self._assert_valid(result)
                self._regen = result
                if self._publisher is not None and self._document.revision > 0:
                    self._publisher.publish(self._document.revision, result)
        except Exception:  # noqa: BLE001 - startup errors must not kill the server
            _LOG.exception("kernel startup failed")

    def _ensure_geometry(self) -> None:
        self.start_kernel_warmup()
        self._kernel_loader.wait()
        with self._lock:
            if self._regen is None:
                result = regenerate(self._document.graph)
                self._assert_valid(result)
                self._regen = result

    @property
    def revision(self) -> int:
        return self._document.revision

    def _assert_valid(self, result: RegenResult) -> None:
        if any(not BRepCheck_Analyzer(shape.wrapped).IsValid() for shape in result.shapes.values()):
            raise RequestError("INVALID_BREP", "regeneration produced invalid geometry")

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
        raise RequestError(
            f"{kind.upper()}_NO_EFFECT",
            f"{kind} {operation.featureId} removes no material from {target}",
            featureId=operation.featureId,
            target=target,
            hint=hint,
        )

    def publish_failure(self, payload: dict[str, Any], result: dict[str, Any]) -> None:
        """Show a failed sketch or feature call in the viewer; the last good scene stays."""
        if self._publisher is None:
            return
        feature_id = payload.get("featureId") or payload.get("sketchId")
        error = result["error"]
        self._publisher.publish_failure(
            [feature_id] if isinstance(feature_id, str) else [],
            {k: error[k] for k in ("code", "message", "hint") if k in error},
        )

    def _mutate(
        self,
        operation: str,
        payload: dict[str, Any],
        parse: Callable[[dict[str, Any]], MutationOperation],
        apply: Callable[[Any, MutationOperation], str],
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

                graph = loads(dumps(self._document.graph))
                changed_id = apply(graph, parsed)
                result = regenerate(graph)
                self._assert_valid(result)
                self._assert_effect(parsed, result)
                previous = self._document.revision
                response = {
                    "ok": True,
                    "revision": previous + 1,
                    "previousRevision": previous,
                    "requestId": request_id,
                    "units": "mm",
                    "changedFeatureIds": [changed_id],
                    "regeneration": {"status": "OK", "valid": True},
                    "warnings": [],
                    **response_fields(graph, result, changed_id),
                }
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
                self._document = document
                self._regen = result
                if self._publisher is not None:
                    self._publisher.publish(document.revision, result)
                if not durable:
                    _LOG.warning(
                        "document rename committed but directory fsync failed; durability is unconfirmed"
                    )
                return response
            except RequestError as error:
                failure = _safe_error(
                    self._document.revision,
                    request_id,
                    error.code,
                    str(error),
                    operation=operation,
                    retryable=error.code == "REVISION_CONFLICT",
                    **error.details,
                )
                self.publish_failure(payload, failure)
                return failure
            except GraphError as error:
                failure = _graph_error_response(
                    self._document.revision, request_id, operation, error
                )
                self.publish_failure(payload, failure)
                return failure
            except Exception:  # noqa: BLE001 - translate all kernel failures at this seam.
                _LOG.exception("kernel failure during %s", operation)
                failure = _safe_error(
                    self._document.revision,
                    request_id,
                    "KERNEL_OPERATION_FAILED",
                    "the modeling operation could not produce valid geometry",
                    operation=operation,
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

    def _node_metadata(
        self, graph: Any, result: RegenResult, node_id: str
    ) -> dict[str, Any]:
        from .cut import make_cut_tool
        from .extrusion import extrude_sketch
        from .hole import make_hole_tool

        node = graph.nodes[node_id]
        if isinstance(node, Sketch):
            return {"frame": self._frame(node)}
        if isinstance(node, Extrude):
            sketch = graph.nodes[node.sketch]
            tool = extrude_sketch(node, sketch).shape
            direction = self._frame(sketch)["normal"]
        elif isinstance(node, Cut):
            sketch = graph.nodes[node.tool]
            tool = make_cut_tool(node, sketch, result.shapes[node.target]).shape
            normal = self._frame(sketch)["normal"]
            direction = (
                normal if node.depth is None else [value if node.flip else -value for value in normal]
            )
        elif isinstance(node, Hole):
            sketch = Sketch("global_xy")
            tool = make_hole_tool(node, result.shapes[node.target]).shape
            direction = [0.0, 0.0, 1.0]
        else:
            return {}
        metadata = {
            "frame": self._frame(sketch),
            "direction": direction,
            "toolBBox": self._bbox([tool]),
        }
        if isinstance(node, Hole):
            metadata["positionNames"] = [
                position.name
                for position in (node.positions or (HolePosition(None, node.position),))
            ]
        return metadata

    def sketch(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_geometry()
        entities = payload.get("entities") or payload.get("set", {}).get("entities")

        def response(graph: Any, result: RegenResult, changed_id: str) -> dict[str, Any]:
            fields = {"sketchId": changed_id, **self._node_metadata(graph, result, changed_id)}
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

    def feature(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_geometry()
        return self._mutate(
            "feature",
            payload,
            _parse_feature,
            _apply_feature,
            lambda graph, result, changed_id: {
                "featureId": changed_id,
                **self._node_metadata(graph, result, changed_id),
            },
        )

    def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = payload.get("select", {}).get("kind")
        if kind in {"measurements", "feature", "reference"}:
            self._ensure_geometry()
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
                return {**base, **self._metrics()}
            if kind == "feature":
                return self._query_feature(select, base)
            if kind == "reference":
                return self._query_reference(select, base)
            return _safe_error(
                self._document.revision,
                None,
                "INVALID_PARAMETER",
                f"unknown query kind: {kind}",
                operation="query",
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
            return {
                **base,
                "status": resolution.status.value,
                "keys": keys,
                "fingerprints": fingerprints,
                "candidates": [candidate.descriptor for candidate in resolution.candidates],
                "error": resolution.error,
            }
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

    def _metrics(self) -> dict[str, Any]:
        terminal = _terminal_shapes(self._regen)
        solids = [solid for _, shape in terminal for solid in shape.solids()]
        bodies = [
            {
                "featureId": feature,
                "bodyIndex": index,
                "bbox": self._bbox([solid]),
                "volume": {"value": solid.volume, "unit": "mm^3"},
                "area": {"value": solid.area, "unit": "mm^2"},
                "solidCount": 1,
            }
            for feature, shape in terminal
            for index, solid in enumerate(shape.solids())
        ]
        return {
            "bbox": self._bbox(solids),
            "volume": {"value": sum(solid.volume for solid in solids), "unit": "mm^3"},
            "area": {"value": sum(solid.area for solid in solids), "unit": "mm^2"},
            "solidCount": len(solids),
            "bodies": bodies,
        }

    def check(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_geometry()
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
            requirements = payload.get("requirements") or [{"id": "valid", "kind": "valid_brep"}]
            results = []
            for requirement in requirements:
                if requirement.get("kind") != "valid_brep":
                    results.append(
                        {
                            "id": requirement.get("id"),
                            "kind": requirement.get("kind"),
                            "status": "ERROR",
                            "evidence": "check kind is not available",
                        }
                    )
                    continue
                valid = all(
                    BRepCheck_Analyzer(shape.wrapped).IsValid()
                    for shape in self._regen.shapes.values()
                )
                results.append(
                    {
                        "id": requirement.get("id"),
                        "kind": "valid_brep",
                        "status": "PASS" if valid else "FAIL",
                        "measured": {"value": valid, "unit": "boolean"},
                        "evidence": {"solidCount": self._metrics()["solidCount"]},
                    }
                )
            return {
                "ok": True,
                "revision": self._document.revision,
                "units": "mm",
                "results": results,
            }
