from __future__ import annotations

import json
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
        self.step_paths = sorted(steps.glob("*.step")) if steps.is_dir() else []
        self._step_metrics = None
        self._step_shapes = None
        self._service = None
        self._inspection = None

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
            from coalesce.graph import PcbBase

            graph = self.service._document.graph
            base = next((node.id for node in graph.ordered() if isinstance(node, PcbBase)), None)
            if base is None:
                raise ValueError("saved document has no pcb base")
            self._inspection = self.service.query({"select": {"kind": "enclosure", "base": base}})
            if self._inspection.get("ok") is False:
                raise ValueError(str(self._inspection))
        return self._inspection


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


def _is_through_hole(cylinder, shape, z_min, z_max, diameter_range):
    from build123d import Align, Cylinder, Pos

    axis = cylinder["axis"]
    diameter = cylinder["radius"] * 2
    if abs(abs(axis["direction"][2]) - 1) > 1e-6:
        return False
    if not diameter_range[0] <= diameter <= diameter_range[1]:
        return False
    x, y = axis["position"][:2]
    probe = Pos(x, y, z_min) * Cylinder(
        min(cylinder["radius"] / 4, 0.2),
        z_max - z_min,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    return _intersection_volume(shape, probe) <= 1e-7


def _metric_hole_points(metric, shape, diameter_range):
    if "bbox" not in metric:
        return []
    z_min, z_max = metric["bbox"]["min"][2], metric["bbox"]["max"][2]
    cylinders = [cylinder for part in metric["parts"] for cylinder in part["cylinders"]]
    return [
        tuple(cylinder["axis"]["position"][:2])
        for cylinder in cylinders
        if _is_through_hole(cylinder, shape, z_min, z_max, diameter_range)
    ]


def _hole_positions(check, context):
    if len(context.step_paths) != 1:
        return _result(
            check,
            "fail",
            reason=f"hole evidence requires exactly one STEP file, found {len(context.step_paths)}",
        )
    points = _metric_hole_points(
        context.step_metrics[0], context.step_shapes[0], check["diameter"]
    )
    tolerance = check.get("tolerance", 0.1)
    passed = all(
        any(abs(x - expected_x) <= tolerance and abs(y - expected_y) <= tolerance for x, y in points)
        for expected_x, expected_y in check["positions"]
    )
    return _result(check, "pass" if passed else "fail", actual=points)


def _step_check(check, context):
    handlers = {
        "step_validity": _step_validity,
        "bbox": _bbox_check,
        "no_overlapping_parts": _no_overlapping_parts,
        "hole_positions": _hole_positions,
    }
    handler = handlers.get(check["kind"])
    return handler(check, context) if handler else None


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
    lid_ids = {lid.get("partId") for lid in inspected.get("lids", [])}
    passed = base_id in terminal_ids and bool(lid_ids) and lid_ids <= terminal_ids
    actual = {"base": base_id, "lids": sorted(lid_ids - {None})}
    return _result(check, "pass" if passed else "fail", actual=actual)


def _standoffs(check, inspected):
    evidence = inspected.get("standoffs") or {}
    count = evidence.get("mountingHoleCount", 0)
    covered = evidence.get("coveredHoleIds", [])
    passed = count > 0 and len(covered) == count and not evidence.get("uncoveredHoleIds")
    return _result(check, "pass" if passed else "fail", actual=evidence)


def _edit_survival(check, context, inspected):
    if context.before_state is None or not context.before_state.is_file():
        return _result(check, "fail", reason="runner before-state facts are missing")
    before = json.loads(context.before_state.read_text())
    if context.service.revision <= before["revision"]:
        return _result(check, "fail", reason="document revision did not advance from before-state")
    base_id = inspected.get("base", {}).get("partId")
    if base_id != before["basePartId"]:
        return _result(
            check,
            "fail",
            reason=f"base partId changed: {before['basePartId']} -> {base_id}",
        )
    lids = {lid.get("featureId"): lid.get("partId") for lid in inspected.get("lids", [])}
    if lids != before["lidPartIds"]:
        return _result(check, "fail", reason="lid feature or partId changed", actual=lids)
    ports = [
        port
        for port in inspected.get("ports", [])
        if port.get("featureId") == before["port"]["featureId"]
    ]
    if len(ports) != 1:
        return _result(check, "fail", reason="saved port feature id no longer resolves")
    port = ports[0]
    expected_port = before["port"]
    if any(port.get(field) != expected_port[field] for field in ("reference", "footprintId", "wall")):
        return _result(check, "fail", reason="saved port reference changed", actual=port)
    if port.get("blocked") is not False:
        return _result(check, "fail", reason="saved port no longer clears J1", actual=port)
    part_ids = sorted(
        part.get("partId") for part in inspected.get("terminalParts", []) if part.get("partId")
    )
    if part_ids != before["manufacturingPartIds"]:
        return _result(
            check,
            "fail",
            reason="manufacturing bodies were added, removed, or orphaned",
            actual=part_ids,
        )
    orphaned = [
        warning
        for warning in inspected.get("warnings", [])
        if warning.get("code") == "OTHER_MANUFACTURING_BODY"
    ]
    if orphaned:
        return _result(check, "fail", reason="orphan manufacturing body", actual=orphaned)
    return _result(
        check,
        "pass",
        actual={"basePartId": base_id, "lidPartIds": lids, "port": port},
    )


def _enclosure_check(check, context):
    kind = check["kind"]
    if kind == "references_resolve":
        _ = context.service
        return _result(check, "pass")
    inspected = context.inspection
    if kind == "wall_thickness":
        return _range_result(check, inspected.get("base", {}).get("minimumWallThickness"))
    if kind == "board_clearance":
        clearances = inspected.get("cavity", {}).get("boardClearance", {}).values()
        values = [_quantity(value) for value in clearances]
        return _range_result(check, min(values) if values and None not in values else None)
    if kind == "port_opening_clears_connector":
        return _port_clearance(check, inspected)
    if kind == "lid_fit_gap":
        return _lid_fit(check, inspected)
    if kind == "enclosure_parts":
        return _enclosure_parts(check, inspected)
    if kind == "standoffs_at_mounting_holes":
        return _standoffs(check, inspected)
    if kind == "edit_survival":
        return _edit_survival(check, context, inspected)
    return None


def check_task(
    task: dict, document: Path, steps: Path, before_state: Path | None = None
) -> list[dict]:
    context = ArtifactContext(document, steps, before_state)
    output = []
    if "parts" in task:
        output.append(_parts_result(task["parts"], context))
    for check in task["checks"]:
        try:
            result = _step_check(check, context) or _enclosure_check(check, context)
            output.append(result or _result(check, "unknown", reason="unsupported check"))
        except Exception as error:  # noqa: BLE001 - a failed measurement is unknown
            output.append(_result(check, "unknown", reason=str(error)))
    return output
