"""Revision-bound acceptance requirements for PCB enclosure inspections."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class InvalidHoleIds(ValueError):
    """A mounting requirement selected values outside the stable hole-id namespace."""

    def __init__(self, invalid_hole_ids: list[str], valid_holes: list[dict[str, str]]) -> None:
        super().__init__(f"unknown mounting hole ids: {', '.join(invalid_hole_ids)}")
        self.invalid_hole_ids = invalid_hole_ids
        self.valid_holes = valid_holes


def _value(quantity: dict[str, Any]) -> float | None:
    return (
        quantity.get("value")
        if isinstance(quantity, dict) and quantity.get("unit") == "mm"
        else None
    )


def _result(
    *,
    requirement: dict[str, Any],
    status: str,
    measured: Any,
    threshold: Any,
    involved: list[str],
    revision: int,
    reason: str | None = None,
) -> dict[str, Any]:
    result = {
        "id": requirement.get("id"),
        "kind": requirement.get("kind"),
        "status": status,
        "measured": measured,
        "threshold": threshold,
        "involved": involved,
        "revision": revision,
    }
    if reason:
        result["reason"] = reason
    return result


def _unknown(
    requirement: dict[str, Any],
    reason: str,
    involved: list[str],
    revision: int,
    measured: Any = None,
) -> dict[str, Any]:
    evidence = measured if measured is not None else {"status": "unknown", "reason": reason}
    return _result(
        requirement=requirement,
        status="UNKNOWN",
        measured=evidence,
        threshold=requirement.get("threshold"),
        involved=involved,
        revision=revision,
        reason=reason,
    )


def _finish(
    requirement: dict[str, Any],
    measured: Any,
    threshold: Any,
    involved: list[str],
    revision: int,
    status: str,
    reason: str | None = None,
) -> dict[str, Any]:
    if measured is None or measured == []:
        return _unknown(
            requirement, "required inspection evidence is missing", involved, revision
        )
    if isinstance(measured, dict) and "value" in measured and measured.get("unit") != "mm":
        return _unknown(
            requirement, "measurement is not in millimetres", involved, revision, measured
        )
    if isinstance(measured, dict) and measured.get("status") == "unknown":
        return _unknown(
            requirement,
            measured.get("reason", "inspection evidence is unknown"),
            involved,
            revision,
            measured,
        )
    return _result(
        requirement=requirement,
        status=status,
        measured=measured,
        threshold=threshold,
        involved=involved,
        revision=revision,
        reason=reason,
    )


def _threshold_unknown(
    requirement: dict[str, Any], involved: list[str], revision: int, measured: Any
) -> dict[str, Any]:
    return _unknown(
        requirement,
        "threshold must use millimetres",
        involved,
        revision,
        measured,
    )


def _board_clearance(
    requirement: dict[str, Any], inspection: dict[str, Any], revision: int
) -> dict[str, Any]:
    side = requirement.get("side", "all")
    clearance = inspection.get("cavity", {}).get("boardClearance", {})
    involved = [inspection["board"]["assetId"], inspection["base"]["partId"]]
    if side == "all":
        measured = list(clearance.values())
        values = [_value(item) for item in measured]
        value = min(values) if values and all(item is not None for item in values) else None
        measured = {"value": value, "unit": "mm"} if value is not None else {
            "status": "unknown", "reason": "one or more board sides could not be measured"
        }
    else:
        measured = clearance.get(side)
    minimum = _value(requirement.get("threshold", {}))
    if minimum is None:
        return _threshold_unknown(requirement, involved, revision, measured)
    value = _value(measured) if isinstance(measured, dict) else None
    return _finish(
        requirement, measured, requirement.get("threshold"), involved, revision,
        "PASS" if value is not None and value >= minimum else "FAIL",
    )


def _thickness(
    requirement: dict[str, Any], inspection: dict[str, Any], revision: int
) -> dict[str, Any]:
    key = (
        "minimumWallThickness"
        if requirement["kind"] == "wall_thickness"
        else "floorThickness"
    )
    measured = inspection["base"].get(key)
    involved = [inspection["base"]["partId"]]
    minimum = _value(requirement.get("threshold", {}))
    if minimum is None:
        return _threshold_unknown(requirement, involved, revision, measured)
    value = _value(measured) if isinstance(measured, dict) else None
    return _finish(
        requirement,
        measured,
        requirement.get("threshold"),
        involved,
        revision,
        "PASS" if value is not None and value >= minimum else "FAIL",
    )


def _validate_hole_ids(
    selected: list[str] | None, all_ids: list[str], references: dict[str, str]
) -> None:
    invalid = [hole_id for hole_id in selected or [] if hole_id not in all_ids]
    if not invalid:
        return
    valid = [
        {"id": hole_id, **({"reference": references[hole_id]} if hole_id in references else {})}
        for hole_id in all_ids
    ]
    raise InvalidHoleIds(invalid, valid)


def _mounting_evidence(
    required: list[str], all_ids: list[str], covered_ids: list[str], references: dict[str, str]
) -> dict[str, Any]:
    covered = [hole_id for hole_id in covered_ids if hole_id in required]
    missing = [hole_id for hole_id in required if hole_id not in covered]
    not_required = [hole_id for hole_id in all_ids if hole_id not in required]
    return {
        "coveredHoleIds": covered,
        "uncoveredHoleIds": missing,
        "uncoveredHoleReferences": [references[item] for item in missing if item in references],
        "notRequiredHoleIds": not_required,
        "notRequiredReferences": [
            references[item] for item in not_required if item in references
        ],
        "unit": "holes",
    }


def _mounting(
    requirement: dict[str, Any], inspection: dict[str, Any], revision: int
) -> dict[str, Any]:
    standoffs = inspection["standoffs"]
    all_ids = standoffs.get("holeIds", [])
    if not all_ids:
        return _unknown(
            requirement,
            "no mounting-hole evidence exists",
            [inspection["base"]["partId"]],
            revision,
        )
    selected = requirement.get("holeIds")
    references = standoffs.get("references", {})
    _validate_hole_ids(selected, all_ids, references)
    required = all_ids if selected is None else selected
    measured = _mounting_evidence(
        required, all_ids, standoffs.get("coveredHoleIds", []), references
    )
    missing = measured["uncoveredHoleIds"]
    involved = [inspection["base"]["partId"]]
    return _finish(
        requirement,
        measured,
        {"all": "intact"},
        involved,
        revision,
        "FAIL" if missing else "PASS",
    )


def _lid_parts(inspection: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    lids = inspection["lids"]
    return lids, [inspection["base"]["partId"], *[lid["partId"] for lid in lids]]


def _outline_results(lids: list[dict[str, Any]]) -> tuple[list[bool], str | None]:
    from .lid_outline import compare_lid_outlines

    comparisons = [
        compare_lid_outlines(**lid["_outlineComparison"])
        for lid in lids
        if "_outlineComparison" in lid
    ]
    if len(comparisons) == len(lids):
        matches = [comparison.matches for comparison in comparisons]
        reason = next((item.reason for item in comparisons if not item.matches), None)
        return matches, reason
    outlines = [lid["outline"] for lid in lids]
    return (
        [item["matches"] for item in outlines],
        next((item.get("reason") for item in outlines if not item["matches"]), None),
    )


def _lid_outline_match(
    requirement: dict[str, Any], inspection: dict[str, Any], revision: int
) -> dict[str, Any]:
    lids, involved = _lid_parts(inspection)
    if not lids:
        return _unknown(requirement, "no lid exists for this measurement", involved, revision)
    measured = [lid["outline"] for lid in lids]
    matches, reason = _outline_results(lids)
    return _finish(
        requirement,
        measured[0] if len(measured) == 1 else measured,
        {
            "areaTolerance": {"value": 1e-6, "unit": "mm^2"},
            "bboxTolerance": {"value": 1e-6, "unit": "mm"},
            "arcRadiusTolerance": {"value": 1e-6, "unit": "mm"},
            "orderedSegmentTopology": "exact",
        },
        involved,
        revision,
        "PASS" if all(matches) else "FAIL",
        reason,
    )


def _lid_interference(
    requirement: dict[str, Any], inspection: dict[str, Any], revision: int
) -> dict[str, Any]:
    lids, involved = _lid_parts(inspection)
    if not lids:
        return _unknown(requirement, "no lid exists for this measurement", involved, revision)
    measured = [lid["interference"] for lid in lids]
    return _finish(
        requirement,
        measured,
        {"value": 0, "unit": "mm^3"},
        involved,
        revision,
        "FAIL" if any(measured) else "PASS",
    )


def _fit_gap(
    requirement: dict[str, Any], inspection: dict[str, Any], revision: int
) -> dict[str, Any]:
    lids, involved = _lid_parts(inspection)
    if not lids:
        return _unknown(requirement, "no lid exists for this measurement", involved, revision)
    measured = [lid["fitGap"] for lid in lids]
    if any(item.get("status") == "unknown" for item in measured):
        return _unknown(
            requirement,
            "lid fit gap cannot be measured from final faces",
            involved,
            revision,
            measured,
        )
    threshold = requirement.get("threshold", {})
    low, high = _value(threshold.get("min", {})), _value(threshold.get("max", {}))
    if low is None or high is None:
        return _threshold_unknown(requirement, involved, revision, measured)
    valid = all(
        _value(item.get("min", {})) is not None and _value(item.get("max", {})) is not None
        for item in measured
    )
    passes = valid and all(
        _value(item["min"]) >= low and _value(item["max"]) <= high for item in measured
    )
    return _finish(
        requirement,
        measured,
        threshold,
        involved,
        revision,
        "PASS" if passes else "FAIL",
    )


def _engagement(
    requirement: dict[str, Any], inspection: dict[str, Any], revision: int
) -> dict[str, Any]:
    lids, involved = _lid_parts(inspection)
    if not lids:
        return _unknown(requirement, "no lid exists for this measurement", involved, revision)
    measured = [lid["lipDepth"] for lid in lids]
    minimum = _value(requirement.get("threshold", {}))
    if minimum is None:
        return _threshold_unknown(requirement, involved, revision, measured)
    passes = all(_value(item) is not None and _value(item) >= minimum for item in measured)
    return _finish(
        requirement,
        measured,
        requirement.get("threshold"),
        involved,
        revision,
        "PASS" if passes else "FAIL",
    )


def _ports(
    requirement: dict[str, Any], inspection: dict[str, Any], revision: int
) -> dict[str, Any]:
    ports = inspection["ports"]
    involved = [inspection["base"]["partId"]]
    if not ports or any(
        warning["code"] == "CONNECTOR_HEIGHT_UNRESOLVED"
        for warning in inspection["warnings"]
    ):
        return _unknown(
            requirement, "connector height or envelope is unknown", involved, revision
        )
    measured = [
        {
            key: port[key]
            for key in (
                "featureId",
                "footprintId",
                "libraryId",
                "reference",
                "value",
                "clearance",
                "blocked",
            )
        }
        for port in ports
    ]
    minimum = _value(requirement.get("threshold", {}))
    if minimum is None:
        return _threshold_unknown(requirement, involved, revision, measured)
    passes = all(
        _value(port["clearance"]["min"]) is not None
        and _value(port["clearance"]["min"]) >= minimum
        and not port["blocked"]
        for port in ports
    )
    return _finish(
        requirement,
        measured,
        requirement.get("threshold"),
        involved,
        revision,
        "PASS" if passes else "FAIL",
    )


def _clean(
    requirement: dict[str, Any], inspection: dict[str, Any], revision: int
) -> dict[str, Any]:
    parts = [item["partId"] for item in inspection["terminalParts"]]
    extras = [
        warning
        for warning in inspection["warnings"]
        if warning["code"] == "OTHER_MANUFACTURING_BODY"
    ]
    return _finish(
        requirement,
        {"extraBodies": extras, "partIds": parts},
        {"extraBodies": 0},
        [inspection["base"]["partId"], *parts],
        revision,
        "FAIL" if extras else "PASS",
    )


REQUIREMENT_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "wall_thickness": _thickness,
    "board_clearance": _board_clearance,
    "floor_thickness": _thickness,
    "mounting_holes": _mounting,
    "standoff_coverage": _mounting,
    "lid_interference": _lid_interference,
    "lid_outline_match": _lid_outline_match,
    "fit_gap": _fit_gap,
    "lid_fit_gap": _fit_gap,
    "engagement_depth": _engagement,
    "lid_engagement": _engagement,
    "port_clearance": _ports,
    "connector_clearance": _ports,
    "clean_deliverables": _clean,
    "clean_manufacturing_bodies": _clean,
}
SUPPORTED_REQUIREMENT_KINDS = tuple(sorted(REQUIREMENT_HANDLERS))


def reduce_gate(results: list[dict[str, Any]]) -> str:
    statuses = {item["status"] for item in results}
    if "FAIL" in statuses:
        return "FAIL"
    if "UNKNOWN" in statuses:
        return "UNKNOWN"
    if results and statuses == {"PASS"}:
        return "PASS"
    return "ERROR"


def evaluate(
    requirements: list[dict[str, Any]], inspection: dict[str, Any], revision: int
) -> dict[str, Any]:
    unknown = [
        item.get("kind")
        for item in requirements
        if item.get("kind") not in REQUIREMENT_HANDLERS
    ]
    if unknown:
        raise ValueError(
            "unknown requirement kind: "
            f"{unknown[0]}; supported kinds: {', '.join(SUPPORTED_REQUIREMENT_KINDS)}"
        )
    results = [
        REQUIREMENT_HANDLERS[item["kind"]](item, inspection, revision)
        for item in requirements
    ]
    return {"gate": reduce_gate(results), "results": results}
