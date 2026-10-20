"""Revisioned document operations shared by MCP transports."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

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


def _quantity(value: Any, unit: str) -> float:
    if not isinstance(value, dict) or set(value) != {"value", "unit"}:
        raise RequestError("INVALID_PARAMETER", "quantity must contain value and unit")
    if value["unit"] != unit:
        raise RequestError(
            "UNIT_MISMATCH",
            f"expected {unit}; received {value['unit']}",
            expected=unit,
            actual=value["unit"],
        )
    if not isinstance(value["value"], (int, float)):
        raise RequestError("INVALID_PARAMETER", "quantity value must be a number")
    return float(value["value"])


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
        raise RequestError("INVALID_PARAMETER", "angle value must be a number")
    angle = float(value["value"])
    return math.degrees(angle) if value["unit"] == "rad" else angle


def _rectangles(payload: Any) -> list[Rectangle]:
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
                width=_quantity(entity.get("width"), "mm"),
                height=_quantity(entity.get("height"), "mm"),
                position=(float(center[0]), float(center[1])),
                rotation=_angle(rotation),
            )
        )
    return rectangles


def _canonical_request(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"value", "unit"} and value["unit"] in {"deg", "rad"}:
            return {"value": float(_angle(value)), "unit": "deg"}
        if set(value) == {"value", "unit"} and value["unit"] == "mm":
            return {"value": float(_quantity(value, "mm")), "unit": "mm"}
        return {key: _canonical_request(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical_request(item) for item in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return value


def _hash(payload: dict[str, Any]) -> str:
    normalized = _canonical_request(
        {key: value for key, value in payload.items() if key != "requestId"}
    )
    return hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
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


def _apply_sketch(graph: Any, payload: dict[str, Any]) -> str:
    action = payload.get("action")
    sketch_id = payload.get("sketchId")
    if action == "create":
        plane = payload.get("plane", {})
        if plane != {"kind": "origin", "name": "XY"}:
            raise RequestError("INVALID_PARAMETER", "slice 1 supports only the XY origin plane")
        graph.add(Sketch(sketch_id, entities=_rectangles(payload.get("entities"))))
    elif action == "update":
        node = graph.nodes.get(sketch_id)
        if not isinstance(node, Sketch):
            raise RequestError("UNKNOWN_ID", f"unknown sketch: {sketch_id}")
        changes = payload.get("set")
        if not isinstance(changes, dict) or not changes:
            raise RequestError("INVALID_PARAMETER", "update set must not be empty")
        if "position" in changes:
            node.position = tuple(float(value) for value in changes["position"])
        if "entities" in changes:
            node.entities = _rectangles(changes["entities"])
    else:
        raise RequestError("INVALID_PARAMETER", "action must be create or update")
    return sketch_id


def _apply_feature(graph: Any, payload: dict[str, Any]) -> str:
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
    feature_id = payload.get("featureId")
    action = payload.get("action")
    if action == "create":
        parameters = payload.get("parameters", {})
        profile = parameters.get("profile", {})
        if parameters.get("operation") != "new_body" or parameters.get("direction") != "normal":
            raise RequestError("INVALID_PARAMETER", "slice 1 supports normal new_body extrudes")
        sketch = graph.nodes.get(profile.get("sketchId"))
        if not isinstance(sketch, Sketch):
            raise RequestError("UNKNOWN_ID", "extrude profile sketch does not exist")
        if profile.get("profileIds") != [entity.id for entity in sketch.entities]:
            raise RequestError(
                "INVALID_PARAMETER",
                "profileIds must list all sketch profiles in graph order",
            )
        graph.add(
            Extrude(
                feature_id,
                sketch=sketch.id,
                height=_quantity(parameters.get("distance"), "mm"),
            )
        )
    elif action == "update":
        node = graph.nodes.get(feature_id)
        if not isinstance(node, Extrude):
            raise RequestError("UNKNOWN_ID", f"unknown extrude: {feature_id}")
        changes = payload.get("set")
        if not isinstance(changes, dict) or set(changes) != {"distance"}:
            raise RequestError("INVALID_PARAMETER", "extrude update requires set.distance")
        node.height = _quantity(changes["distance"], "mm")
    else:
        raise RequestError("INVALID_PARAMETER", "action must be create or update")
    return feature_id


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
        apply: Callable[[Any], str],
        response_fields: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = payload.get("requestId")
        with self._lock:
            try:
                if not isinstance(request_id, str) or not request_id:
                    raise RequestError("INVALID_PARAMETER", "requestId is required")
                body_hash = _hash(payload)
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
                expected = payload.get("expectedRevision")
                if expected != self._document.revision:
                    raise RequestError(
                        "REVISION_CONFLICT",
                        f"expected revision {expected}; current revision is {self._document.revision}",
                        expected=expected,
                        actual=self._document.revision,
                    )

                graph = loads(dumps(self._document.graph))
                changed_id = apply(graph)
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
                    return {**response, "durability": "unconfirmed"}
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
            lambda graph: _apply_sketch(graph, payload),
            {
                "sketchId": payload.get("sketchId"),
                "segmentIds": ["bottom", "right", "top", "left"],
            },
        )

    def feature(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._mutate(
            "feature",
            payload,
            lambda graph: _apply_feature(graph, payload),
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
