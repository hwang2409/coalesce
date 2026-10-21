"""Revision-bound acceptance requirements for PCB enclosure inspections."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def _value(quantity: dict[str, Any]) -> float | None:
    return quantity.get("value") if isinstance(quantity, dict) and quantity.get("unit") == "mm" else None


def _result(*, requirement: dict[str, Any], status: str, measured: Any, threshold: Any, involved: list[str], revision: int, reason: str | None = None) -> dict[str, Any]:
    result = {"id": requirement.get("id"), "kind": requirement.get("kind"), "status": status, "measured": measured, "threshold": threshold, "involved": involved, "revision": revision}
    if reason:
        result["reason"] = reason
    return result


def _unknown(requirement: dict[str, Any], reason: str, involved: list[str], revision: int, measured: Any = None) -> dict[str, Any]:
    evidence = measured if measured is not None else {"status": "unknown", "reason": reason}
    return _result(requirement=requirement, status="UNKNOWN", measured=evidence, threshold=requirement.get("threshold"), involved=involved, revision=revision, reason=reason)


def _finish(requirement: dict[str, Any], measured: Any, threshold: Any, involved: list[str], revision: int, status: str, reason: str | None = None) -> dict[str, Any]:
    if measured is None or measured == []:
        return _unknown(requirement, "required inspection evidence is missing", involved, revision)
    if isinstance(measured, dict) and "value" in measured and measured.get("unit") != "mm":
        return _unknown(requirement, "measurement is not in millimetres", involved, revision, measured)
    if isinstance(measured, dict) and measured.get("status") == "unknown":
        return _unknown(requirement, measured.get("reason", "inspection evidence is unknown"), involved, revision, measured)
    return _result(requirement=requirement, status=status, measured=measured, threshold=threshold, involved=involved, revision=revision, reason=reason)


def _thickness(requirement: dict[str, Any], inspection: dict[str, Any], revision: int) -> dict[str, Any]:
    key = "minimumWallThickness" if requirement["kind"] == "wall_thickness" else "floorThickness"
    measured = inspection["base"].get(key)
    minimum = _value(requirement.get("threshold", {}))
    value = _value(measured) if isinstance(measured, dict) else None
    status = "PASS" if value is not None and minimum is not None and value >= minimum else "FAIL"
    return _finish(requirement, measured, requirement.get("threshold"), [inspection["base"]["partId"]], revision, status)


def _mounting(requirement: dict[str, Any], inspection: dict[str, Any], revision: int) -> dict[str, Any]:
    standoffs = inspection["standoffs"]
    all_ids = standoffs.get("holeIds", standoffs.get("coveredHoleIds", []) + standoffs.get("uncoveredHoleIds", []))
    selected = requirement.get("holeIds")
    required = all_ids if selected is None else [hole_id for hole_id in all_ids if hole_id in selected]
    covered = [hole_id for hole_id in standoffs.get("coveredHoleIds", []) if hole_id in required]
    missing = [hole_id for hole_id in required if hole_id not in covered]
    not_required = [hole_id for hole_id in all_ids if hole_id not in required]
    measured = {"coveredHoleIds": covered, "uncoveredHoleIds": missing, "notRequired": not_required, "unit": "holes"}
    if not all_ids:
        return _unknown(requirement, "no mounting-hole evidence exists", [inspection["base"]["partId"]], revision)
    references = standoffs.get("references", {})
    measured["uncoveredHoleReferences"] = [references[hole_id] for hole_id in missing if hole_id in references]
    measured["notRequiredReferences"] = [references[hole_id] for hole_id in not_required if hole_id in references]
    measured["uncoveredHoleIds"].extend(measured["uncoveredHoleReferences"])
    measured["notRequired"].extend(measured["notRequiredReferences"])
    involved = [inspection["base"]["partId"]]
    return _finish(requirement, measured, {"all": "intact"}, involved, revision, "FAIL" if missing else "PASS")


def _lids(requirement: dict[str, Any], inspection: dict[str, Any], revision: int) -> dict[str, Any]:
    lids = inspection["lids"]
    involved = [inspection["base"]["partId"], *[lid["partId"] for lid in lids]]
    if not lids:
        return _unknown(requirement, "no lid exists for this measurement", involved, revision)
    kind = requirement["kind"]
    if kind == "lid_interference":
        measured = [lid["interference"] for lid in lids]
        status = "FAIL" if any(measured) else "PASS"
        threshold = {"value": 0, "unit": "mm^3"}
    elif kind in {"fit_gap", "lid_fit_gap"}:
        measured = [lid["fitGap"] for lid in lids]
        if any(item.get("status") == "unknown" for item in measured):
            return _unknown(requirement, "lid fit gap cannot be measured from final faces", involved, revision, measured)
        low, high = requirement.get("threshold", {}).get("min"), requirement.get("threshold", {}).get("max")
        valid = all(isinstance(x, dict) and _value(x.get("min")) is not None and _value(x.get("max")) is not None for x in measured)
        status = "PASS" if valid and _value(low) is not None and _value(high) is not None and all(_value(x["min"]) >= _value(low) and _value(x["max"]) <= _value(high) for x in measured) else "FAIL"
        threshold = requirement.get("threshold")
    else:
        measured = [lid["lipDepth"] for lid in lids]
        minimum = _value(requirement.get("threshold", {}))
        status = "PASS" if minimum is not None and all(_value(x) is not None and _value(x) >= minimum for x in measured) else "FAIL"
        threshold = requirement.get("threshold")
    return _finish(requirement, measured, threshold, involved, revision, status)


def _ports(requirement: dict[str, Any], inspection: dict[str, Any], revision: int) -> dict[str, Any]:
    ports = inspection["ports"]
    involved = [inspection["base"]["partId"]]
    if not ports:
        return _unknown(requirement, "connector height or envelope is unknown", involved, revision)
    if any(warning["code"] == "CONNECTOR_HEIGHT_UNRESOLVED" for warning in inspection["warnings"]):
        return _unknown(requirement, "connector height or envelope is unknown", involved, revision)
    measured = [{key: port[key] for key in ("featureId", "footprintId", "libraryId", "reference", "value", "clearance", "blocked")} for port in ports]
    minimum = _value(requirement.get("threshold"))
    status = "PASS" if minimum is not None and all(_value(port["clearance"]["min"]) >= minimum and not port["blocked"] for port in ports) else "FAIL"
    return _finish(requirement, measured, requirement.get("threshold"), involved, revision, status)


def _clean(requirement: dict[str, Any], inspection: dict[str, Any], revision: int) -> dict[str, Any]:
    parts = [item["partId"] for item in inspection["terminalParts"]]
    extras = [warning for warning in inspection["warnings"] if warning["code"] == "OTHER_MANUFACTURING_BODY"]
    return _finish(requirement, {"extraBodies": extras, "partIds": parts}, {"extraBodies": 0}, [inspection["base"]["partId"], *parts], revision, "FAIL" if extras else "PASS")


def evaluate(requirements: list[dict[str, Any]], inspection: dict[str, Any], revision: int) -> dict[str, Any]:
    handlers: dict[str, Callable[..., dict[str, Any]]] = {
        "wall_thickness": _thickness, "floor_thickness": _thickness, "mounting_holes": _mounting, "standoff_coverage": _mounting,
        "lid_interference": _lids, "fit_gap": _lids, "lid_fit_gap": _lids, "engagement_depth": _lids, "lid_engagement": _lids,
        "port_clearance": _ports, "connector_clearance": _ports, "clean_deliverables": _clean, "clean_manufacturing_bodies": _clean,
    }
    results = [handlers[item["kind"]](item, inspection, revision) for item in requirements]
    statuses = [item["status"] for item in results]
    gate = "PASS" if statuses and all(status == "PASS" for status in statuses) else "FAIL" if "FAIL" in statuses else "UNKNOWN" if "UNKNOWN" in statuses else "ERROR"
    return {"gate": gate, "results": results}
