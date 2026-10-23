"""What the viewer shows a human about an enclosure: fit rows measured on the as-built solids, and part facts.

Fit rows use the ``check`` requirement rules (requirements.py). The document stores no thresholds, so a
measurement that only a stated threshold could judge shows its value and "no requirement set", never PASS.
"""

from __future__ import annotations

import logging
from typing import Any

from .graph import BoardImport, PcbBase, PcbLid, PortCutout, ScrewClosure, Standoffs
from .requirements import evaluate

_LOG = logging.getLogger(__name__)
NO_REQUIREMENT = "no requirement set"


def _mm(value: float) -> str:
    return f"{round(value, 3):g}"


def _row(
    row_id: str,
    label: str,
    status: str,
    value: str,
    part_id: str | None,
    evidence: Any,
    reason: str | None = None,
) -> dict[str, Any]:
    row = {"id": row_id, "label": label, "status": status, "value": value}
    if reason:
        row["reason"] = reason
    return {**row, "partId": part_id, "evidence": evidence}


def _rule(requirement: dict[str, Any], inspection: dict[str, Any], revision: int) -> dict[str, Any]:
    return evaluate([requirement], inspection, revision)["results"][0]


def _measured(
    row_id: str, label: str, measured: dict[str, Any], value: str, part_id: str
) -> dict[str, Any]:
    """A value with no stated threshold; UNKNOWN when the inspection could not measure it."""
    if measured.get("status") == "unknown":
        return _row(row_id, label, "UNKNOWN", "not measured", part_id, measured, measured["reason"])
    return _row(row_id, label, "MEASURED", value, part_id, measured, NO_REQUIREMENT)


def _holes(board: BoardImport) -> dict[str, tuple[str | None, str, str]]:
    """Mounting hole id -> (board reference or None, footprint name, "x, y" position in mm).

    The footprint name stands in for a missing reference: "M2 mounting hole" for MountingHole:M2.
    """
    from .enclosure_inspection import mounting_holes

    library = {
        footprint["id"]: footprint["libraryId"]
        for footprint in board.mechanics.get("footprints", [])
    }
    return {
        hole["id"]: (
            hole["reference"],
            f"{library[hole['footprintId']].removeprefix('MountingHole:')} mounting hole",
            ", ".join(_mm(c) for c in hole["position"]),
        )
        for hole in mounting_holes(board)
    }


def _thickness_rows(inspection: dict[str, Any]) -> list[dict[str, Any]]:
    base = inspection["base"]
    return [
        _measured(row_id, label, base[key], f"{_mm(base[key].get('value', 0))} mm", base["partId"])
        for row_id, label, key in (
            ("wall_thickness", "Wall thickness", "minimumWallThickness"),
            ("floor_thickness", "Floor thickness", "floorThickness"),
        )
    ]


def _board_row(inspection: dict[str, Any], board_part: str | None) -> dict[str, Any]:
    cavity = inspection["cavity"]
    clearance = cavity["boardClearance"]
    evidence = {"boardClearance": clearance, "boardInterference": cavity["boardInterference"]}
    label = "Board clearance"
    if cavity["boardInterference"]:
        volume = cavity["boardInterference"]["volume"]["value"]
        return _row(
            "board_clearance",
            label,
            "FAIL",
            "board overlaps base",
            board_part,
            evidence,
            f"{_mm(volume)} mm³ of the board is inside the base",
        )
    unknown = next((item for item in clearance.values() if item.get("status") == "unknown"), None)
    if unknown:
        return _row(
            "board_clearance",
            label,
            "UNKNOWN",
            "not measured",
            board_part,
            evidence,
            unknown["reason"],
        )
    values = {side: _mm(item["value"]) for side, item in clearance.items()}
    least = _mm(min(item["value"] for item in clearance.values()))
    sides = (
        "all sides"
        if set(values.values()) == {least}
        else ", ".join(f"{side} {value}" for side, value in values.items())
    )
    return _row(
        "board_clearance",
        label,
        "MEASURED",
        f"{least} mm min, {sides}",
        board_part,
        evidence,
        NO_REQUIREMENT,
    )


def _standoff_row(inspection: dict[str, Any], board: BoardImport, revision: int) -> dict[str, Any]:
    result = _rule({"id": "standoffs", "kind": "mounting_holes"}, inspection, revision)
    measured = result["measured"]
    total = len(inspection["standoffs"]["holeIds"])
    if result["status"] == "UNKNOWN":
        return _row(
            "standoffs",
            "Standoffs",
            "UNKNOWN",
            "not measured",
            inspection["base"]["partId"],
            measured,
            result["reason"],
        )
    holes = _holes(board)
    missing = [
        reference or f"{name} at ({position}) mm"
        for reference, name, position in (
            holes[hole_id] for hole_id in measured["uncoveredHoleIds"]
        )
    ]
    covered = len(measured["coveredHoleIds"])
    reason = f"no standoff at {', '.join(missing)}" if missing else None
    return _row(
        "standoffs",
        "Standoffs",
        result["status"],
        f"{covered} of {total} mounting holes",
        inspection["base"]["partId"],
        measured,
        reason,
    )


def _lid_outline_row(
    lid: dict[str, Any], inspection: dict[str, Any], revision: int
) -> dict[str, Any] | None:
    result = _rule(
        {"id": "outline", "kind": "lid_outline_match"},
        {**inspection, "lids": [lid]},
        revision,
    )
    if result["status"] != "FAIL":
        return None
    measured = lid["outline"]
    source, legal_source = measured.get("source"), measured["legalSource"]
    value = (
        f"the lid follows {source}, but {legal_source} was added to the base after it"
        if source is not None and source != legal_source
        else result["reason"]
    )
    return _row(
        f"lid_outline:{lid['featureId']}",
        "Lid outline",
        "FAIL",
        value,
        lid["partId"],
        measured,
        f"update the lid's outlineSource to {legal_source}",
    )


def _lid_fit_row(lid: dict[str, Any], node: PcbLid) -> dict[str, Any]:
    if node.lip_depth is None or node.lip_width is None:
        return _row(
            f"lid_fit_gap:{lid['featureId']}",
            "Lid fit gap",
            "N/A",
            "the lid has no lip",
            lid["partId"],
            None,
        )
    gap = lid["fitGap"]
    low, high = (_mm(gap[end]["value"]) for end in ("min", "max")) if "min" in gap else ("", "")
    value = f"{low} mm" if low == high else f"{low}–{high} mm"
    return _measured(f"lid_fit_gap:{lid['featureId']}", "Lid fit gap", gap, value, lid["partId"])


def _one_lid_rows(
    lid: dict[str, Any], inspection: dict[str, Any], node: PcbLid, revision: int
) -> list[dict[str, Any]]:
    result = _rule(
        {"id": "lid", "kind": "lid_interference"},
        {**inspection, "lids": [lid]},
        revision,
    )
    overlap = lid["interference"]
    value = "none" if overlap is None else f"{_mm(overlap['volume']['value'])} mm³"
    reason = "lid and base overlap" if result["status"] == "FAIL" else result.get("reason")
    interference = _row(
        f"lid_interference:{lid['featureId']}",
        "Lid interference",
        result["status"],
        value,
        lid["partId"],
        result["measured"],
        reason,
    )
    return [
        *([outline] if (outline := _lid_outline_row(lid, inspection, revision)) else []),
        interference,
        _lid_fit_row(lid, node),
    ]


def _lid_rows(inspection: dict[str, Any], graph: Any, revision: int) -> list[dict[str, Any]]:
    if not inspection["lids"]:
        result = _rule({"id": "lid", "kind": "lid_interference"}, inspection, revision)
        return [
            _row(
                "lid",
                "Lid",
                "UNKNOWN",
                "not measured",
                inspection["base"]["partId"],
                result["measured"],
                result["reason"],
            )
        ]
    return [
        row
        for lid in inspection["lids"]
        for row in _one_lid_rows(lid, inspection, graph.nodes[lid["featureId"]], revision)
    ]


def _port_rows(inspection: dict[str, Any], revision: int) -> list[dict[str, Any]]:
    rows = []
    for port in inspection["ports"]:
        # clears the connector: the same rule as check port_clearance, at 0 mm
        requirement = {
            "id": "port",
            "kind": "port_clearance",
            "threshold": {"value": 0, "unit": "mm"},
        }
        result = _rule(requirement, {**inspection, "ports": [port]}, revision)
        opening = port["opening"]
        width, height = (
            opening[f"{axis}Max"]["value"] - opening[f"{axis}Min"]["value"] for axis in "uv"
        )
        clearance = port["clearance"]["min"]["value"]
        name = port["reference"] or port["footprintId"]
        value = f"{name}: {_mm(width)} × {_mm(height)} mm on {port['wall']} wall, {_mm(clearance)} mm clearance"
        reason = result.get("reason")
        if result["status"] == "FAIL":
            reason = (
                "the opening is blocked"
                if port["blocked"]
                else (f"the opening is {_mm(-clearance)} mm smaller than the connector")
            )
        rows.append(
            _row(
                f"port:{port['featureId']}",
                f"Port {name} clears connector",
                result["status"],
                value,
                inspection["base"]["partId"],
                [port],
                reason,
            )
        )
    return rows


def _screw_closure_row(
    inspection: dict[str, Any], graph: Any, revision: int
) -> dict[str, Any] | None:
    closures = [node for node in graph.ordered() if isinstance(node, ScrewClosure)]
    if not closures:
        return None
    node = closures[0]
    minimum = (node.boss_diameter - node.base_pilot_diameter) / 2
    result = _rule(
        {
            "id": "screw_closure",
            "kind": "screw_closure",
            "minimumWall": {"value": minimum, "unit": "mm"},
        },
        inspection,
        revision,
    )
    measured = result["measured"]
    wall = measured.get("minimumResidualWall", {}).get("value")
    interference = measured.get("interference", {}).get("value")
    value = (
        f"{measured.get('count', 0)} points, {_mm(wall)} mm minimum wall, "
        f"{'coaxial' if measured.get('coaxial') else 'not coaxial'}, "
        f"{'no interference' if interference is not None and interference <= 1e-7 else 'interference'}"
    )
    return _row(
        "screw_closure",
        "Screw closure",
        result["status"],
        value,
        inspection["base"]["partId"],
        measured,
        result.get("reason"),
    )


def _rows(result: Any, base: PcbBase, revision: int) -> list[dict[str, Any]]:
    from . import enclosure_inspection
    from .parts import terminal_parts

    inspection = enclosure_inspection.inspect_enclosure(result.graph, result, base.id)
    board_part = next(
        (
            part.part_id
            for part in terminal_parts(result, role="reference")
            if part.source_body.feature == base.board
        ),
        None,
    )
    return [
        *_thickness_rows(inspection),
        _board_row(inspection, board_part),
        _standoff_row(inspection, result.graph.nodes[base.board], revision),
        *_lid_rows(inspection, result.graph, revision),
        *([closure] if (closure := _screw_closure_row(inspection, result.graph, revision)) else []),
        *_port_rows(inspection, revision),
    ]


def fit_summary(result: Any, revision: int) -> list[dict[str, Any]]:
    """One fit check per pcb base in the regenerated document; [] when there is no enclosure."""
    summaries = []
    for base in (node for node in result.graph.ordered() if isinstance(node, PcbBase)):
        try:
            rows = _rows(result, base, revision)
        except Exception as error:  # noqa: BLE001 - a failed inspection is an UNKNOWN row, never a lost scene
            _LOG.exception("enclosure inspection for the viewer failed")
            rows = [
                _row(
                    "inspection",
                    "Enclosure inspection",
                    "UNKNOWN",
                    "not measured",
                    None,
                    None,
                    f"the enclosure could not be inspected: {error}",
                )
            ]
        summaries.append({"baseId": base.id, "rows": rows})
    return summaries


def _fact(label: str, value: str) -> dict[str, Any]:
    return {"label": label, "value": value}


def _length(label: str, mm: float) -> dict[str, Any]:
    """A length fact; ``mm`` keeps the number for the viewer, which stops section ends past the walls."""
    return {**_fact(label, f"{_mm(mm)} mm"), "mm": mm}


def _base_facts(result: Any, base: PcbBase) -> list[dict[str, Any]]:
    from .enclosure import selected_holes

    facts = [
        _length("Wall", base.wall_thickness),
        _length("Floor", base.floor_thickness),
        _fact("Board clearance", f"{_mm(base.clearance)} mm"),
    ]
    for node in result.graph.ordered():
        if isinstance(node, Standoffs) and node.target == base.id:
            count = len(selected_holes(node, result.graph.nodes[node.board]))
            facts.append(
                _fact(
                    "Standoffs",
                    f"{count} × ⌀{_mm(node.boss_diameter)} mm, "
                    f"pilot ⌀{_mm(node.pilot_diameter)} mm",
                )
            )
        elif isinstance(node, PortCutout) and node.base == base.id:
            port = result.metadata[node.id]
            width, height = (_mm(port["opening"][side]["value"]) for side in ("width", "height"))
            facts.append(
                _fact(
                    f"Port {port.get('reference') or port['footprintId']}",
                    f"{width} × {height} mm, {port['wall']} wall",
                )
            )
    return facts


def part_facts(result: Any, source_feature: str) -> tuple[dict[str, Any], ...]:
    """What the committed features say about the part whose body ``source_feature`` made."""
    node = result.graph.nodes[source_feature]
    if isinstance(node, BoardImport):
        holes = [
            f"{reference or name} at ({position})"
            for reference, name, position in _holes(node).values()
        ]
        # the row's role line already says "Reference, not exported" on every width
        return (
            _fact("Mounting holes", f"{len(holes)}: {', '.join(holes)} mm" if holes else "none"),
        )
    if isinstance(node, PcbBase):
        return tuple(_base_facts(result, node))
    if isinstance(node, PcbLid):
        if node.lip_depth is None or node.lip_width is None:
            # the fit gap only spaces a lip from the walls; a slab lid has none to report
            return (_length("Thickness", node.thickness), _fact("Lip", "none"))
        return (
            _length("Thickness", node.thickness),
            _fact("Fit gap", f"{_mm(node.fit_gap)} mm"),
            _fact("Lip", f"{_mm(node.lip_depth)} mm deep, {_mm(node.lip_width)} mm wide"),
        )
    return ()
