from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path


def _result(check, status, **extra):
    return {"kind": check["kind"], "status": status, **extra}


def _quantity(value):
    return value.get("value") if isinstance(value, dict) else value


class ArtifactContext:
    """Measure one run's saved document and exact STEP export directory once."""

    def __init__(self, document: Path, steps: Path, before_state: Path | None = None):
        self.document = document
        self.before_state = before_state
        artifacts = sorted(steps.iterdir()) if steps.is_dir() else []
        self.step_paths = [
            path
            for path in artifacts
            if path.is_file() and path.suffix.lower() in {".step", ".stp"}
        ]
        self.unexpected_artifacts = [path.name for path in artifacts if path not in self.step_paths]
        self._step_metrics = None
        self._step_shapes = None
        self._service = None
        self._inspection = None
        self._measurements = None
        self._document_parts = None
        self._document_payload = None

    @property
    def step_metrics(self):
        if self._step_metrics is None:
            from coalesce.export import _read_step_metrics

            self._step_metrics = []
            for path in self.step_paths:
                try:
                    self._step_metrics.append({"path": str(path), **_read_step_metrics(path)})
                except Exception as error:  # noqa: BLE001 - failed evidence is recorded
                    self._step_metrics.append({"path": str(path), "error": str(error)})
        return self._step_metrics

    @property
    def step_shapes(self):
        if self._step_shapes is None:
            from build123d import import_step

            self._step_shapes = [import_step(path) for path in self.step_paths]
        return self._step_shapes

    @property
    def service(self):
        if self._service is None:
            from coalesce.service import DocumentService

            self._service = DocumentService(self.document, warmup=False)
            self._service._ensure_geometry()
        return self._service

    @property
    def inspection(self):
        if self._inspection is None:
            graph = self.service.query({"select": {"kind": "graph"}})
            if graph.get("ok") is not True:
                raise ValueError(f"saved document graph query failed: {graph}")
            base = next(
                (node["id"] for node in graph["graph"]["nodes"] if node["type"] == "PcbBase"),
                None,
            )
            if base is None:
                raise ValueError("saved document has no pcb base")
            self._inspection = self.service.query({"select": {"kind": "enclosure", "base": base}})
            if self._inspection.get("ok") is not True:
                raise ValueError(f"saved document enclosure query failed: {self._inspection}")
        return self._inspection

    @property
    def measurements(self):
        if self._measurements is None:
            self._measurements = self.service.query({"select": {"kind": "measurements"}})
            if self._measurements.get("ok") is not True:
                raise ValueError(
                    f"saved document measurements query failed: {self._measurements}"
                )
        return self._measurements

    @property
    def document_parts(self):
        if self._document_parts is None:
            part_ids = [part["partId"] for part in self.measurements["parts"]]
            with tempfile.TemporaryDirectory(prefix="coalesce-eval-") as temporary:
                directory = Path(temporary)
                self._document_parts = {}
                for index, part_id in enumerate(part_ids):
                    path = directory / f"reference-{index}.step"
                    exported = self.service.export({
                        "expectedRevision": self.measurements["revision"],
                        "path": str(path),
                        "partIds": [part_id],
                    })
                    if exported.get("ok") is not True:
                        raise ValueError(f"reference export failed: {exported}")
                    self._document_parts[part_id] = _single_solid_metrics(path)
        return self._document_parts

    @property
    def document_payload(self):
        if self._document_payload is None:
            self._document_payload = json.loads(self.document.read_text())
        return self._document_payload


def _parts_result(expected, context):
    if len(context.step_metrics) != expected:
        return {
            "kind": "parts",
            "status": "fail",
            "reason": f"expected exactly {expected} STEP file(s), found {len(context.step_metrics)}",
        }
    counts = [metric.get("solidCount") for metric in context.step_metrics]
    if counts != [1] * expected:
        return {
            "kind": "parts",
            "status": "fail",
            "reason": f"expected exactly one solid in each STEP file, found {counts}",
        }
    return {"kind": "parts", "status": "pass", "actual": expected}


def _step_validity(check, context):
    metrics = context.step_metrics
    valid = bool(metrics) and all(metric.get("solidCount", 0) > 0 for metric in metrics)
    return _result(check, "pass" if valid else "fail", evidence=metrics)


def _bbox_check(check, context):
    if not context.step_metrics:
        return _result(check, "fail", reason="steps directory is missing or empty")
    if "bbox" not in context.step_metrics[0]:
        return _result(check, "unknown", evidence=context.step_metrics[0])
    box = context.step_metrics[0]["bbox"]
    tolerance = check.get("tolerance", 0.1)
    if "min" in check and "max" in check:
        passed = all(
            abs(measured - expected) <= tolerance
            for actual, expected_values in ((box["min"], check["min"]), (box["max"], check["max"]))
            for measured, expected in zip(actual, expected_values, strict=True)
        )
        return _result(check, "pass" if passed else "fail", actual=box)
    actual = [
        maximum - minimum
        for minimum, maximum in zip(box["min"], box["max"], strict=True)
    ]
    passed = all(
        abs(measured - expected) <= tolerance
        for measured, expected in zip(actual, check["size"], strict=True)
    )
    return _result(check, "pass" if passed else "fail", actual=actual)


def _no_overlapping_parts(check, context):
    if len(context.step_shapes) < 2:
        return _result(check, "fail", reason="at least two STEP parts are required")
    overlap = any(
        _intersection_volume(first, second) > 1e-7
        for index, first in enumerate(context.step_shapes)
        for second in context.step_shapes[index + 1 :]
    )
    return _result(check, "fail" if overlap else "pass")


def _intersection_volume(first, second):
    intersection = first.intersect(second)
    if intersection is None:
        return 0.0
    shapes = list(intersection) if not hasattr(intersection, "solids") else [intersection]
    return sum(solid.volume for shape in shapes for solid in shape.solids())


def _is_through_hole(cylinder, shape, z_min, z_max):
    from build123d import Align, Cylinder, Pos

    axis = cylinder["axis"]
    if abs(abs(axis["direction"][2]) - 1) > 1e-6:
        return False
    x, y = axis["position"][:2]
    probe = Pos(x, y, z_min) * Cylinder(
        min(cylinder["radius"] / 4, 0.2),
        z_max - z_min,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    return _intersection_volume(shape, probe) <= 1e-7


def _metric_holes(metric, shape):
    if "bbox" not in metric:
        return []
    z_min, z_max = metric["bbox"]["min"][2], metric["bbox"]["max"][2]
    cylinders = [cylinder for part in metric["parts"] for cylinder in part["cylinders"]]
    return [
        {
            "position": list(cylinder["axis"]["position"][:2]),
            "diameter": cylinder["radius"] * 2,
        }
        for cylinder in cylinders
        if _is_through_hole(cylinder, shape, z_min, z_max)
    ]


def _match_holes(expected, holes, tolerance):
    unmatched = list(holes)
    unmatched_expected = []
    matched = []
    for item in expected:
        expected_x, expected_y = item["position"]
        diameter = item["diameter"]
        candidates = [
            (index, math.hypot(x - expected_x, y - expected_y))
            for index, hole in enumerate(unmatched)
            for x, y in [hole["position"]]
            if diameter[0] <= hole["diameter"] <= diameter[1]
            and abs(x - expected_x) <= tolerance
            and abs(y - expected_y) <= tolerance
        ]
        if not candidates:
            unmatched_expected.append(item)
            continue
        index, _distance = min(candidates, key=lambda candidate: candidate[1])
        matched.append(unmatched.pop(index))
    return matched, unmatched, unmatched_expected


def _hole_positions(check, context):
    if len(context.step_paths) != 1:
        return _result(
            check,
            "fail",
            reason=f"hole evidence requires exactly one STEP file, found {len(context.step_paths)}",
        )
    holes = _metric_holes(context.step_metrics[0], context.step_shapes[0])
    tolerance = check.get("tolerance", 0.1)
    expected = [
        {"position": position, "diameter": check["diameter"]}
        for position in check["positions"]
    ]
    matched, unmatched, unmatched_expected = _match_holes(expected, holes, tolerance)
    volume, expected_volume = _plate_volumes(context.step_metrics[0], context.step_shapes[0], matched)
    # shortcut: aggregate volume, so a box minus its holes; an extra cavity, pocket, or boss changes it,
    # but equal-volume compensation can spoof it (see evals/README.md for the upgrade condition).
    exact = math.isclose(volume, expected_volume, rel_tol=1e-6, abs_tol=1e-3)
    return _result(
        check,
        "pass" if not unmatched and not unmatched_expected and exact else "fail",
        actual=holes,
        unmatchedDetected=unmatched,
        unmatchedExpected=unmatched_expected,
        volume=volume,
        expectedVolume=expected_volume,
    )


def _part_hole_positions(check, context):
    matching_parts = [
        part
        for part in context.measurements["parts"]
        if part.get("terminalFeatureId") == check["feature"]
    ]
    if len(matching_parts) != 1:
        return _result(
            check,
            "fail",
            reason=(
                f"feature {check['feature']!r} resolved to {len(matching_parts)} "
                "terminal parts; expected exactly one"
            ),
        )
    part_id = matching_parts[0]["partId"]
    reference = context.document_parts[part_id]
    candidates = []
    for path, metric, shape in zip(
        context.step_paths, context.step_metrics, context.step_shapes, strict=True
    ):
        solids = list(shape.solids())
        if len(solids) == 1 and _same_solid(_solid_metrics(solids[0]), reference):
            candidates.append((path, metric, shape))
    if len(candidates) != 1:
        return _result(
            check,
            "fail",
            reason=f"terminal part {part_id!r} matched {len(candidates)} STEP files; expected one",
        )
    _path, metric, shape = candidates[0]
    holes = _metric_holes(metric, shape)
    _matched, unmatched, unmatched_expected = _match_holes(
        check["holes"], holes, check.get("tolerance", 0.1)
    )
    return _result(
        check,
        "pass" if not unmatched and not unmatched_expected else "fail",
        actual=holes,
        unmatched=unmatched,
        unmatchedExpected=unmatched_expected,
    )


def _plate_volumes(metric, shape, matched):
    low, high = metric["bbox"]["min"], metric["bbox"]["max"]
    width, depth, height = (high[i] - low[i] for i in range(3))
    holes = sum(math.pi * (hole["diameter"] / 2) ** 2 * height for hole in matched)
    return float(shape.volume), width * depth * height - holes


def _solid_metrics(solid):
    from build123d import CenterOf

    box = solid.bounding_box()
    center = solid.center(CenterOf.MASS)
    return {
        "volume": float(solid.volume),
        "bbox": [
            float(value)
            for point in (box.min, box.max)
            for value in (point.X, point.Y, point.Z)
        ],
        "centerOfMass": [float(center.X), float(center.Y), float(center.Z)],
    }


def _single_solid_metrics(path):
    from build123d import import_step

    solids = list(import_step(path).solids())
    if len(solids) != 1:
        raise ValueError(f"reference export contains {len(solids)} solids")
    return _solid_metrics(solids[0])


def _same_solid(first, second):
    # shortcut: properties can match a different solid; use topology or shape distance when needed.
    return all(
        math.isclose(left, right, rel_tol=1e-7, abs_tol=1e-6)
        for field in ("volume", "bbox", "centerOfMass")
        for left, right in zip(
            first[field] if isinstance(first[field], list) else [first[field]],
            second[field] if isinstance(second[field], list) else [second[field]],
            strict=True,
        )
    )


def _step_solid_candidates(context, expected):
    output = []
    for path, shape in zip(context.step_paths, context.step_shapes, strict=True):
        for index, solid in enumerate(shape.solids()):
            metrics = _solid_metrics(solid)
            output.append((
                f"{path}#{index}",
                [
                    part_id
                    for part_id, reference in expected.items()
                    if _same_solid(metrics, reference)
                ],
            ))
    return output


def _document_parts(context):
    expected = context.document_parts
    candidates = _step_solid_candidates(context, expected)
    matches = {solid: parts[0] for solid, parts in candidates if len(parts) == 1}
    unmapped = [solid for solid, parts in candidates if not parts]
    ambiguous = [
        {"solid": solid, "partIds": parts} for solid, parts in candidates if len(parts) > 1
    ]
    counts = {
        part_id: sum(matched == part_id for matched in matches.values())
        for part_id in expected
    }
    unmatched = [part_id for part_id, count in counts.items() if count == 0]
    duplicates = [part_id for part_id, count in counts.items() if count > 1]
    failures = unmapped or ambiguous or unmatched or duplicates
    return {
        "kind": "document_parts",
        "status": "fail" if failures else "pass",
        "matches": matches,
        "unmappedSteps": unmapped,
        "ambiguousSteps": ambiguous,
        "unmatchedParts": unmatched,
        "duplicateParts": duplicates,
    }


def _step_check(check, context):
    handlers = {
        "step_validity": _step_validity,
        "bbox": _bbox_check,
        "no_overlapping_parts": _no_overlapping_parts,
        "hole_positions": _hole_positions,
        "part_hole_positions": _part_hole_positions,
        "cylinder_radii": _cylinder_radii,
        "cylinder_steps": _cylinder_steps,
    }
    handler = handlers.get(check["kind"])
    return handler(check, context) if handler else None


def _cylinder_radii(check, context):
    if len(context.step_shapes) != 1:
        return _result(
            check,
            "fail",
            reason=(
                "cylinder evidence requires exactly one STEP file, "
                f"found {len(context.step_shapes)}"
            ),
        )
    from build123d import GeomType

    actual = sorted(
        float(face.radius)
        for face in context.step_shapes[0].faces()
        if face.geom_type is GeomType.CYLINDER
    )
    expected = sorted(check["radii"])
    tolerance = check.get("tolerance", 1e-6)
    passed = len(actual) == len(expected) and all(
        math.isclose(measured, wanted, rel_tol=0, abs_tol=tolerance)
        for measured, wanted in zip(actual, expected, strict=True)
    )
    return _result(check, "pass" if passed else "fail", actual=actual)


def _cylinder_steps(check, context):
    if len(context.step_shapes) != 1:
        return _result(check, "fail", reason="cylinder step evidence requires exactly one STEP file")
    from build123d import GeomType

    actual = []
    for face in context.step_shapes[0].faces():
        if face.geom_type is not GeomType.CYLINDER:
            continue
        bounds = face.bounding_box()
        actual.append([float(face.radius), float(bounds.min.X), float(bounds.max.X)])
    tolerance = check.get("tolerance", 1e-6)
    expected = check["steps"]
    passed = len(actual) == len(expected) and all(
        any(
            all(abs(measured - wanted) <= tolerance for measured, wanted in zip(found, wanted_step, strict=True))
            for found in actual
        )
        for wanted_step in expected
    )
    return _result(check, "pass" if passed else "fail", actual=sorted(actual))


def _contains(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _contains(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            _contains(left, right) for left, right in zip(actual, expected, strict=True)
        )
    return actual == expected


def _inner_loop_model_reason(model):
    if not any(not item.get("construction") for item in model.get("lines", [])):
        return "inner-loop intent missing: one outer boundary and at least one inner opening loop are required"
    openings = model.get("circles", []) + model.get("arcs", [])
    if not any(not item.get("construction") for item in openings):
        return "inner-loop intent missing: one outer boundary and at least one inner opening loop are required"
    if not any(
        item.get("kind") == "radius" and item.get("driving", True)
        for item in model.get("dimensions", [])
    ):
        return "inner-loop intent missing: the opening radius must be an editable driving dimension"
    return None


def _inner_loop_intent(check, context):
    nodes = context.document_payload.get("graph", {}).get("nodes", [])
    constrained = [
        node for node in nodes
        if node.get("type") == "Sketch" and isinstance(node.get("constrained"), dict)
    ]
    extrusions = [node for node in nodes if node.get("type") == "Extrude"]
    if len(constrained) != 1 or len(extrusions) != 1:
        return _result(check, "fail", reason="inner-loop intent missing: one constrained sketch and one extrusion are required")
    sketch, extrusion = constrained[0], extrusions[0]
    if extrusion.get("sketch") != sketch.get("id"):
        return _result(check, "fail", reason="inner-loop intent missing: the extrusion profile must be the constrained sketch")
    if any(node.get("type") not in {"Sketch", "Extrude"} for node in nodes):
        return _result(check, "fail", reason="inner-loop intent missing: the opening must be a loop in the extrusion profile, not a separate cut")
    reason = _inner_loop_model_reason(sketch["constrained"])
    return _result(check, "fail", reason=reason) if reason else _result(check, "pass")


def _document_state(check, context):
    payload = context.document_payload
    nodes = {node.get("id"): node for node in payload.get("graph", {}).get("nodes", [])}
    expected_features = check.get("features", {})
    actual_features = {feature_id: nodes.get(feature_id) for feature_id in expected_features}
    actual_receipts = [
        receipt.get("response", {}).get("changedFeatureIds")
        for receipt in payload.get("receipts", [])
    ]
    # edit tasks leave revision and receipt history to runner_feature_edit
    actual_feature_ids = list(nodes)
    passed = (
        check.get("revision", payload.get("revision")) == payload.get("revision")
        and _contains(actual_features, expected_features)
        and check.get("featureIds", actual_feature_ids) == actual_feature_ids
        and check.get("receiptFeatureIds", actual_receipts) == actual_receipts
    )
    return _result(
        check,
        "pass" if passed else "fail",
        actual={
            "revision": payload.get("revision"),
            "features": actual_features,
            "featureIds": actual_feature_ids,
            "receiptFeatureIds": actual_receipts,
        },
    )


def _runner_feature_edit(check, context):
    """The agent changed the runner's own feature in place: one or more updates, no delete."""
    if context.before_state is None or not context.before_state.is_file():
        return _result(check, "fail", reason="runner before-state facts are missing")
    before = json.loads(context.before_state.read_text())
    payload = context.document_payload
    feature_id = check["feature"]
    nodes = {node.get("id"): node for node in payload.get("graph", {}).get("nodes", [])}
    responses = [receipt.get("response", {}) for receipt in payload.get("receipts", [])]
    receipts = [response.get("changedFeatureIds") for response in responses]
    before_receipts = before.get("receiptFeatureIds", [])
    edits = responses[len(before_receipts):]
    preserved = before.get("preservedFeatures", {})
    passed = (
        before.get("featureId") == feature_id
        and _contains(before.get("feature"), check["before"])
        and feature_id in nodes
        and all(nodes.get(item_id) == node for item_id, node in preserved.items())
        and receipts[: len(before_receipts)] == before_receipts
        and bool(edits)
        and all(
            edit.get("changedFeatureIds") == [feature_id] and "deleted" not in edit
            for edit in edits
        )
        and payload.get("revision") == before.get("revision", -1) + len(edits)
    )
    return _result(
        check,
        "pass" if passed else "fail",
        actual={
            "beforeFeatureId": before.get("featureId"),
            "preservedFeatureIds": sorted(preserved),
            "revision": payload.get("revision"),
            "receiptFeatureIds": receipts,
        },
    )


def _range_result(check, value):
    if value is None or (isinstance(value, dict) and value.get("status") == "unknown"):
        return _result(check, "unknown")
    measured = _quantity(value)
    passed = check.get("minimum", float("-inf")) <= measured <= check.get("maximum", float("inf"))
    return _result(check, "pass" if passed else "fail", actual=measured)


def _port_clearance(check, inspected):
    ports = [
        port
        for port in inspected.get("ports", [])
        if port.get("reference") == check["reference"] and port.get("wall") == check["wall"]
    ]
    clearances = [_quantity(port.get("clearance", {}).get("min")) for port in ports]
    passed = bool(ports) and all(
        port.get("blocked") is False and clearance is not None and clearance >= 0
        for port, clearance in zip(ports, clearances, strict=True)
    )
    return _result(check, "pass" if passed else "fail", actual=ports)


def _lid_fit(check, inspected):
    lids = inspected.get("lids", [])
    values = []
    for lid in lids:
        gap = lid.get("fitGap") or {}
        values.append((_quantity(gap.get("min")), _quantity(gap.get("max"))))
    passed = bool(values) and all(
        minimum is not None
        and maximum is not None
        and lid.get("interference") is None
        and check.get("minimum", float("-inf")) <= minimum
        and maximum <= check.get("maximum", float("inf"))
        for lid, (minimum, maximum) in zip(lids, values, strict=True)
    )
    return _result(check, "pass" if passed else "fail", actual=values)


def _enclosure_parts(check, inspected):
    terminal_ids = {part.get("partId") for part in inspected.get("terminalParts", [])}
    base_id = inspected.get("base", {}).get("partId")
    lid_ids = {lid.get("partId") for lid in inspected.get("lids", [])} - {None}
    expected_ids = {base_id, *lid_ids} - {None}
    passed = base_id is not None and len(lid_ids) == 1 and terminal_ids == expected_ids
    actual = {"base": base_id, "lids": sorted(lid_ids), "terminalParts": sorted(terminal_ids)}
    return _result(check, "pass" if passed else "fail", actual=actual)


def _standoffs(check, inspected):
    evidence = inspected.get("standoffs") or {}
    count = evidence.get("mountingHoleCount", 0)
    covered = evidence.get("coveredHoleIds", [])
    passed = count > 0 and len(covered) == count and not evidence.get("uncoveredHoleIds")
    return _result(check, "pass" if passed else "fail", actual=evidence)


def identity_snapshot(inspected, port_feature_id="port"):
    port = next(
        (item for item in inspected.get("ports", []) if item.get("featureId") == port_feature_id),
        None,
    )
    return {
        "basePartId": inspected.get("base", {}).get("partId"),
        "lidPartIds": {
            lid.get("featureId"): lid.get("partId") for lid in inspected.get("lids", [])
        },
        "port": (
            {field: port.get(field) for field in ("featureId", "reference", "footprintId", "wall")}
            if port
            else None
        ),
        "manufacturingPartIds": sorted(
            part.get("partId")
            for part in inspected.get("terminalParts", [])
            if part.get("partId")
        ),
    }


def _board_refresh(check, context):
    response = context.service.query({"select": {"kind": "feature", "id": check.get("board", "board")}})
    feature = response.get("feature") if response.get("ok") is True else None
    mechanics = feature.get("mechanics") if isinstance(feature, dict) else None
    if not isinstance(mechanics, dict):
        return _result(check, "fail", reason="saved board mechanics are missing")
    holes = {
        hole.get("footprintId"): hole.get("position")
        for hole in mechanics.get("holes", [])
        if hole.get("footprintId") in check.get("mountingHoles", {})
    }
    expected_holes = check.get("mountingHoles", {})
    footprints = {
        footprint.get("reference"): footprint
        for footprint in mechanics.get("footprints", [])
    }
    connector = footprints.get(check.get("connector", "J1"))
    actual_connector = (
        [connector.get("position"), connector.get("rotation")]
        if connector else None
    )
    expected_connector = [check.get("connectorPosition"), check.get("connectorRotation", 0)]
    passed = holes == expected_holes and actual_connector == expected_connector
    return _result(
        check,
        "pass" if passed else "fail",
        actual={"mountingHoles": holes, "connector": actual_connector},
    )


def _edit_survival(check, context, inspected):
    if context.before_state is None or not context.before_state.is_file():
        return _result(check, "fail", reason="runner before-state facts are missing")
    before = json.loads(context.before_state.read_text())
    if context.service.revision <= before["revision"]:
        return _result(check, "fail", reason="document revision did not advance from before-state")
    expected = {field: before[field] for field in identity_snapshot(inspected)}
    actual = identity_snapshot(inspected, before["port"]["featureId"])
    if actual != expected:
        return _result(check, "fail", reason="saved part or reference identity changed", actual=actual)
    port = next(
        item for item in inspected["ports"] if item.get("featureId") == before["port"]["featureId"]
    )
    if port.get("blocked") is not False:
        return _result(check, "fail", reason="saved port no longer clears J1", actual=port)
    return _result(check, "pass", actual=actual)


def _requirement_gate(check, context):
    response = context.service.check({
        "action": "evaluate",
        "base": check.get("base"),
        "requirements": check["requirements"],
    })
    results = response.get("results") if response.get("ok") is True else None
    passed = bool(results) and all(item.get("status") == "PASS" for item in results)
    return _result(check, "pass" if passed else "fail", actual=results)


# task targets name a semantic part; each maps to exactly one serialized graph type
TARGET_GRAPH_TYPES = {"pcb_base": "PcbBase", "pcb_lid": "PcbLid"}


def _resolve_treatment_targets(check, expected, nodes):
    targets = {}
    for item in expected:
        target = item["target"]
        graph_type = TARGET_GRAPH_TYPES.get(target)
        if graph_type is None:
            return None, _result(
                check, "fail", reason=f"unknown semantic target {target!r}", target=target
            )
        matches = [node.get("id") for node in nodes if node.get("type") == graph_type]
        if len(matches) != 1:
            count = "no" if not matches else str(len(matches))
            return None, _result(
                check,
                "fail",
                reason=f"semantic target {target!r} resolved to {count} features; expected exactly one",
                target=target,
                matches=matches,
            )
        targets[target] = matches[0]
    return targets, None


def _normalize_treatment(node):
    kind = node.get("type", "").lower()
    amount = _quantity(node.get("radius" if kind == "fillet" else "distance"))
    roles = tuple(sorted({
        edge.get("role") for edge in node.get("edges", [])
        if isinstance(edge, dict) and edge.get("role")
    }))
    return kind, node.get("target"), amount, roles


def _same_treatments(actual, wanted):
    return len(actual) == len(wanted) and all(
        left[:2] == right[:2]
        and math.isclose(left[2], right[2], rel_tol=1e-7, abs_tol=1e-6)
        and left[3] == right[3]
        for left, right in zip(sorted(actual), sorted(wanted))
    )


def _edge_treatment(check, context):
    graph = context.service.query({"select": {"kind": "graph"}})
    if graph.get("ok") is not True:
        return _result(check, "unknown", reason="graph query failed")
    expected = check.get("treatments", [check])
    if any(not item.get("edgeRole") for item in expected):
        return _result(check, "fail", reason="expected edge role is missing")
    nodes = graph.get("graph", {}).get("nodes", [])
    targets, failure = _resolve_treatment_targets(check, expected, nodes)
    if failure:
        return failure
    actual = [
        _normalize_treatment(node)
        for node in nodes
        if node.get("type", "").lower() in {"fillet", "chamfer"}
    ]
    wanted = [
        (item["feature"], targets[item["target"]], item["amount"], (item["edgeRole"],))
        for item in expected
    ]
    return _result(check, "pass" if _same_treatments(actual, wanted) else "fail", actual=actual)


def _references_resolve(check, context):
    _ = context.service
    return _result(check, "pass")


def _wall_thickness(check, _context, inspected):
    return _range_result(check, inspected.get("base", {}).get("minimumWallThickness"))


def _board_clearance(check, _context, inspected):
    clearances = inspected.get("cavity", {}).get("boardClearance", {}).values()
    values = [_quantity(value) for value in clearances]
    return _range_result(check, min(values) if values and None not in values else None)


def _with_inspection(handler):
    return lambda check, context: handler(check, context.inspection)


def _with_context_and_inspection(handler):
    return lambda check, context: handler(check, context, context.inspection)


_ENCLOSURE_CHECKS = {
    "document_state": _document_state,
    "inner_loop_intent": _inner_loop_intent,
    "runner_feature_edit": _runner_feature_edit,
    "references_resolve": _references_resolve,
    "edge_treatment": _edge_treatment,
    "requirement_gate": _requirement_gate,
    "board_refresh": _board_refresh,
    "wall_thickness": _with_context_and_inspection(_wall_thickness),
    "board_clearance": _with_context_and_inspection(_board_clearance),
    "port_opening_clears_connector": _with_inspection(_port_clearance),
    "lid_fit_gap": _with_inspection(_lid_fit),
    "enclosure_parts": _with_inspection(_enclosure_parts),
    "standoffs_at_mounting_holes": _with_inspection(_standoffs),
    "edit_survival": _with_context_and_inspection(_edit_survival),
}


def _enclosure_check(check, context):
    handler = _ENCLOSURE_CHECKS.get(check["kind"])
    return handler(check, context) if handler else None


def check_task(
    task: dict, document: Path, steps: Path, before_state: Path | None = None
) -> list[dict]:
    context = ArtifactContext(document, steps, before_state)
    output = []
    if context.unexpected_artifacts:
        output.append({
            "kind": "artifacts",
            "status": "fail",
            "unexpected": context.unexpected_artifacts,
        })
    if "parts" in task:
        output.append(_parts_result(task["parts"], context))
    if task.get("documentParts"):
        try:
            output.append(_document_parts(context))
        except Exception as error:  # noqa: BLE001 - failed evidence is recorded
            output.append({"kind": "document_parts", "status": "unknown", "reason": str(error)})
    for check in task["checks"]:
        try:
            result = _step_check(check, context) or _enclosure_check(check, context)
            output.append(result or _result(check, "unknown", reason="unsupported check"))
        except Exception as error:  # noqa: BLE001 - a failed measurement is unknown
            output.append(_result(check, "unknown", reason=str(error)))
    return output
