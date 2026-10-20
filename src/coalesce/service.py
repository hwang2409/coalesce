"""Revisioned document operations shared by MCP transports."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

from OCP.BRepCheck import BRepCheck_Analyzer

from .document_store import MAX_RECEIPTS, MAX_REQUEST_HASHES, Document, DocumentStore
from .graph import Extrude, GraphError, Rectangle, Sketch
from .references import (
    BodySource,
    Cardinality,
    MaxAlong,
    Normal,
    Position,
    Reference,
    SketchSegmentSource,
)
from .regen import RegenResult, regenerate
from .serialization import dumps, loads

_LOG = logging.getLogger(__name__)
_ALLOWED_FEATURE_KINDS = ["extrude", "boolean", "hole", "fillet"]


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


@dataclass(frozen=True)
class SketchChanges:
    position: tuple[float, float, float] | None
    entities: tuple[Rectangle, ...] | None


@dataclass(frozen=True)
class ExtrudeProfile:
    sketchId: str
    profileIds: tuple[str, ...]


@dataclass(frozen=True)
class ExtrudeParameters:
    profile: ExtrudeProfile
    distance: float
    operation: str
    direction: str


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
    entities: tuple[Rectangle, ...]


@dataclass(frozen=True)
class SketchUpdate(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    sketchId: str
    set: SketchChanges


@dataclass(frozen=True)
class ExtrudeCreate(_Operation):
    action: Action
    expectedRevision: float
    requestId: str
    featureId: str
    parameters: ExtrudeParameters
    kind: ClassVar[str] = "extrude"

    @classmethod
    def wire_fields(cls) -> set[str]:
        return {*super().wire_fields(), "kind"}


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


MutationOperation = SketchCreate | SketchUpdate | ExtrudeCreate | ExtrudeUpdate


def _rectangles(payload: Any) -> tuple[Rectangle, ...]:
    if not isinstance(payload, list) or not payload:
        raise RequestError("INVALID_PARAMETER", "entities must contain a rectangle")
    rectangles = []
    for entity in payload:
        if not isinstance(entity, dict) or entity.get("kind") != "rectangle":
            raise RequestError(
                "INVALID_PARAMETER",
                "slice 1 supports only rectangle sketch entities",
                allowedKinds=["rectangle"],
            )
        center = entity.get("center", [0, 0])
        rotation = entity.get("rotation", {"value": 0, "unit": "deg"})
        rectangles.append(
            Rectangle(
                id=entity["id"],
                width=_quantity(entity.get("width"), "mm", "width"),
                height=_quantity(entity.get("height"), "mm", "height"),
                position=(
                    _finite_number(center[0], "center"),
                    _finite_number(center[1], "center"),
                ),
                rotation=_angle(rotation),
            )
        )
    return tuple(rectangles)


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
            plane=SketchPlane(plane.get("kind"), plane.get("name")),
            entities=_rectangles(payload.get("entities")),
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
            entities=_rectangles(changes["entities"]) if "entities" in changes else None,
        ),
    )


def _parse_feature(payload: dict[str, Any]) -> ExtrudeCreate | ExtrudeUpdate:
    kind = payload.get("kind")
    if kind not in _ALLOWED_FEATURE_KINDS:
        raise RequestError(
            "INVALID_PARAMETER",
            f"unknown feature kind: {kind}",
            allowedKinds=_ALLOWED_FEATURE_KINDS,
        )
    if kind != "extrude":
        raise RequestError(
            "FEATURE_NOT_AVAILABLE",
            f"feature kind {kind} is not yet available",
            allowedKinds=_ALLOWED_FEATURE_KINDS,
        )
    common = {
        "action": Action(payload.get("action")),
        "expectedRevision": _finite_number(payload.get("expectedRevision"), "expectedRevision"),
        "requestId": payload.get("requestId"),
        "featureId": payload.get("featureId"),
    }
    if common["action"] is Action.CREATE:
        parameters = payload.get("parameters", {})
        profile = parameters.get("profile", {})
        return ExtrudeCreate(
            **common,
            parameters=ExtrudeParameters(
                profile=ExtrudeProfile(
                    sketchId=profile.get("sketchId"),
                    profileIds=tuple(profile.get("profileIds", [])),
                ),
                distance=_quantity(parameters.get("distance"), "mm", "distance"),
                operation=parameters.get("operation"),
                direction=parameters.get("direction"),
            ),
        )
    changes = payload.get("set")
    if not isinstance(changes, dict) or set(changes) != {"distance"}:
        raise RequestError("INVALID_PARAMETER", "extrude update requires set.distance")
    return ExtrudeUpdate(
        **common,
        set=ExtrudeChanges(distance=_quantity(changes["distance"], "mm", "distance")),
    )


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


def _reference(payload: dict[str, Any]) -> Reference:
    source = payload.get("source")
    if isinstance(source, dict) and source.get("kind") == "sketch_segment":
        source = SketchSegmentSource(
            source["sketch"], source["entity"], source["segment"]
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
        if operation.plane != SketchPlane("origin", "XY"):
            raise RequestError("INVALID_PARAMETER", "slice 1 supports only the XY origin plane")
        graph.add(Sketch(operation.sketchId, entities=list(operation.entities)))
    else:
        node = graph.nodes.get(operation.sketchId)
        if not isinstance(node, Sketch):
            raise RequestError("UNKNOWN_ID", f"unknown sketch: {operation.sketchId}")
        if operation.set.position is not None:
            node.position = operation.set.position
        if operation.set.entities is not None:
            node.entities = list(operation.set.entities)
    return operation.sketchId


def _apply_feature(graph: Any, operation: ExtrudeCreate | ExtrudeUpdate) -> str:
    if isinstance(operation, ExtrudeCreate):
        parameters = operation.parameters
        if parameters.operation != "new_body" or parameters.direction != "normal":
            raise RequestError("INVALID_PARAMETER", "slice 1 supports normal new_body extrudes")
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
            )
        )
    else:
        node = graph.nodes.get(operation.featureId)
        if not isinstance(node, Extrude):
            raise RequestError("UNKNOWN_ID", f"unknown extrude: {operation.featureId}")
        node.height = operation.set.distance
    return operation.featureId


class DocumentService:
    """The only writer for one persisted, revisioned document."""

    def __init__(self, path: str | Path):
        self.store = DocumentStore(path)
        self._lock = threading.RLock()
        self._document = self.store.load()
        self._regen = regenerate(self._document.graph)
        self._assert_valid(self._regen)

    @property
    def revision(self) -> int:
        return self._document.revision

    def _assert_valid(self, result: RegenResult) -> None:
        if any(not BRepCheck_Analyzer(shape.wrapped).IsValid() for shape in result.shapes.values()):
            raise RequestError("INVALID_BREP", "regeneration produced invalid geometry")

    def _mutate(
        self,
        operation: str,
        payload: dict[str, Any],
        parse: Callable[[dict[str, Any]], MutationOperation],
        apply: Callable[[Any, MutationOperation], str],
        response_fields: dict[str, Any],
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
                    **response_fields,
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
                if not durable:
                    _LOG.warning(
                        "document rename committed but directory fsync failed; durability is unconfirmed"
                    )
                return response
            except RequestError as error:
                return _safe_error(
                    self._document.revision,
                    request_id,
                    error.code,
                    str(error),
                    operation=operation,
                    retryable=error.code == "REVISION_CONFLICT",
                    **error.details,
                )
            except GraphError as error:
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
                }
                return _safe_error(
                    self._document.revision,
                    request_id,
                    code if code in known else "KERNEL_OPERATION_FAILED",
                    "the modeling operation could not produce valid geometry",
                    operation=operation,
                )
            except Exception:  # noqa: BLE001 - translate all kernel failures at this seam.
                _LOG.exception("kernel failure during %s", operation)
                return _safe_error(
                    self._document.revision,
                    request_id,
                    "KERNEL_OPERATION_FAILED",
                    "the modeling operation could not produce valid geometry",
                    operation=operation,
                )

    def sketch(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._mutate(
            "sketch",
            payload,
            _parse_sketch,
            _apply_sketch,
            {
                "sketchId": payload.get("sketchId"),
                "segmentIds": ["bottom", "right", "top", "left"],
            },
        )

    def feature(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._mutate(
            "feature",
            payload,
            _parse_feature,
            _apply_feature,
            {"featureId": payload.get("featureId")},
        )

    def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            requested_revision = payload.get("revision", self._document.revision)
            if requested_revision != self._document.revision:
                return _safe_error(
                    self._document.revision,
                    None,
                    "UNKNOWN_ID",
                    "only the current revision is retained in slice 1",
                    operation="query",
                )
            select = payload.get("select", {})
            kind = select.get("kind")
            base = {"ok": True, "revision": self._document.revision, "units": "mm"}
            if kind in {"document", "graph", "timeline"}:
                return {**base, "graph": json.loads(dumps(self._document.graph))}
            if kind == "revision":
                return base
            if kind == "measurements":
                return {**base, **self._metrics()}
            if kind == "reference":
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
            return _safe_error(
                self._document.revision,
                None,
                "INVALID_PARAMETER",
                f"unknown query kind: {kind}",
                operation="query",
            )

    def _metrics(self) -> dict[str, Any]:
        shapes = list(self._regen.shapes.values())
        if not shapes:
            dimensions = (0.0, 0.0, 0.0)
        else:
            boxes = [shape.bounding_box() for shape in shapes]
            dimensions = (
                max(box.max.X for box in boxes) - min(box.min.X for box in boxes),
                max(box.max.Y for box in boxes) - min(box.min.Y for box in boxes),
                max(box.max.Z for box in boxes) - min(box.min.Z for box in boxes),
            )
        return {
            "bbox": {
                axis: {"value": value, "unit": "mm"}
                for axis, value in zip(("x", "y", "z"), dimensions, strict=True)
            },
            "volume": {"value": sum(shape.volume for shape in shapes), "unit": "mm^3"},
            "area": {"value": sum(shape.area for shape in shapes), "unit": "mm^2"},
            "solidCount": sum(len(shape.solids()) for shape in shapes),
        }

    def check(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if payload.get("action") != "evaluate":
                return _safe_error(
                    self._document.revision,
                    None,
                    "INVALID_PARAMETER",
                    "slice 1 supports only check action evaluate",
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
                            "evidence": "check kind is not available in slice 1",
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
