"""Revision-bound acceptance requirements for PCB enclosure inspections."""

from __future__ import annotations

from typing import Any


def _value(quantity: dict[str, Any]) -> float | None:
    return quantity.get("value") if quantity.get("unit") == "mm" else None


def _result(requirement: dict[str, Any], status: str, measured: Any, threshold: Any, involved: list[str], revision: int, reason: str | None = None) -> dict[str, Any]:
    value = {
        "id": requirement.get("id"),
        "kind": requirement.get("kind"),
        "status": status,
        "measured": measured,
        "threshold": threshold,
        "involved": involved,
        "revision": revision,
    }
    if reason:
        value["reason"] = reason
    return value


def _unknown(requirement: dict[str, Any], reason: str, involved: list[str], revision: int) -> dict[str, Any]:
    return _result(requirement, "UNKNOWN", {"status": "unknown", "reason": reason}, requirement.get("threshold"), involved, revision, reason)


def evaluate(requirements: list[dict[str, Any]], inspection: dict[str, Any], revision: int) -> dict[str, Any]:
    results = []
    base = inspection["base"]
    base_id = inspection["baseId"]
    for requirement in requirements:
        kind = requirement.get("kind")
        threshold = requirement.get("threshold", {})
        involved = [base_id]
        if kind in {"wall_thickness", "floor_thickness"}:
            key = "minimumWallThickness" if kind == "wall_thickness" else "floorThickness"
            measured = base[key]
            if measured.get("status") == "unknown":
                results.append(_unknown(requirement, measured["reason"], involved, revision))
                continue
            minimum = _value(threshold) if isinstance(threshold, dict) else threshold
            value = _value(measured)
            results.append(_result(requirement, "PASS" if value is not None and minimum is not None and value >= minimum else "FAIL", measured, threshold, involved, revision))
        elif kind in {"mounting_holes", "standoff_coverage"}:
            standoffs = inspection["standoffs"]
            involved += standoffs["uncoveredHoleIds"] + standoffs["coveredHoleIds"]
            missing = standoffs["uncoveredHoleIds"]
            measured = {"coveredHoleIds": standoffs["coveredHoleIds"], "uncoveredHoleIds": missing, "unit": "holes"}
            results.append(_result(requirement, "FAIL" if missing else "PASS", measured, {"all": "intact"}, involved, revision))
        elif kind == "lid_interference":
            lids = inspection["lids"]
            involved += [lid["featureId"] for lid in lids]
            if not lids:
                results.append(_unknown(requirement, "no lid exists for interference measurement", involved, revision))
            else:
                interference = [lid["interference"] for lid in lids]
                results.append(_result(requirement, "FAIL" if any(interference) else "PASS", {"interference": interference}, {"value": 0, "unit": "mm^3"}, involved, revision))
        elif kind in {"fit_gap", "lid_fit_gap"}:
            lids = inspection["lids"]
            for lid in lids:
                involved.append(lid["featureId"])
            if not lids or any(lid["fitGap"].get("status") == "unknown" for lid in lids):
                results.append(_unknown(requirement, "lid fit gap cannot be measured from final faces", involved, revision))
                continue
            low = _value(threshold.get("min", {}))
            high = _value(threshold.get("max", {}))
            measured = [lid["fitGap"] for lid in lids]
            ok = all(low is not None and high is not None and low <= _value(lid["fitGap"]["min"]) and _value(lid["fitGap"]["max"]) <= high for lid in lids)
            results.append(_result(requirement, "PASS" if ok else "FAIL", measured, threshold, involved, revision))
        elif kind in {"engagement_depth", "lid_engagement"}:
            lids = inspection["lids"]
            involved += [lid["featureId"] for lid in lids]
            minimum = _value(threshold)
            measured = [lid["lipDepth"] for lid in lids]
            if not lids or any(value is None for value in measured):
                results.append(_unknown(requirement, "lid engagement depth cannot be measured", involved, revision))
            else:
                results.append(_result(requirement, "PASS" if minimum is not None and all(_value(value) >= minimum for value in measured) else "FAIL", measured, threshold, involved, revision))
        elif kind in {"port_clearance", "connector_clearance"}:
            ports = inspection["ports"]
            involved += [port["featureId"] for port in ports] + [port["footprintId"] for port in ports]
            unknown = any(warning["code"] == "CONNECTOR_HEIGHT_UNRESOLVED" for warning in inspection["warnings"])
            if not ports or unknown:
                results.append(_unknown(requirement, "connector height or envelope is unknown", involved, revision))
            else:
                minimum = _value(threshold)
                measured = [port["clearance"] for port in ports]
                ok = minimum is not None and all(_value(port["clearance"]["min"]) >= minimum and not port["blocked"] for port in ports)
                results.append(_result(requirement, "PASS" if ok else "FAIL", measured, threshold, involved, revision))
        elif kind in {"clean_deliverables", "clean_manufacturing_bodies"}:
            extras = [warning for warning in inspection["warnings"] if warning["code"] == "OTHER_MANUFACTURING_BODY"]
            involved += [item["partId"] for item in inspection["terminalParts"]]
            results.append(_result(requirement, "FAIL" if extras else "PASS", {"extraBodies": extras, "partIds": involved[1:]}, {"extraBodies": 0}, involved, revision))
        else:
            results.append(_result(requirement, "ERROR", {"status": "error", "reason": "unsupported requirement kind"}, threshold, involved, revision, "unsupported requirement kind"))
    statuses = [item["status"] for item in results]
    gate = "FAIL" if "FAIL" in statuses else "UNKNOWN" if "UNKNOWN" in statuses else "ERROR" if "ERROR" in statuses else "PASS"
    return {"gate": gate, "results": results}
