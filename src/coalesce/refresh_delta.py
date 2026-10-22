"""Deterministic board refresh changes for MCP and viewer output."""

from __future__ import annotations

import math
from typing import Any


def _point(value: Any) -> list[float]:
    return [float(value[0]), float(value[1])]


def _items(before: list[dict], after: list[dict], *, reference: str = "reference") -> list[dict]:
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
            "change": "added" if was is None else "removed" if now is None else "moved",
        })
    return changes


def _dependents(graph: Any, board_id: str) -> list[str]:
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


def _changed(before: Any, after: Any, feature_id: str) -> bool:
    old = before.shapes.get(feature_id)
    new = after.shapes.get(feature_id)
    if old is None or new is None:
        return old is not new
    return not old.is_equal(new)


def _board_parts(board: Any) -> tuple[list[dict], list[dict]]:
    mechanics = board.mechanics if board is not None else {}
    holes = [hole for hole in mechanics.get("holes", []) if hole.get("mounting")]
    ids = {item["id"] for item in holes}
    references = {item.get("reference") for item in holes}
    if None in references:
        references.add("REF**")
    footprints = [
        item for item in mechanics.get("footprints", [])
        if item["id"] not in ids and item.get("reference") not in references
    ]
    return holes, footprints


def refresh_delta(
    before_graph: Any,
    after_graph: Any,
    before_result: Any,
    after_result: Any,
    board_id: str,
) -> dict[str, Any]:
    before_board = before_graph.nodes.get(board_id)
    after_board = after_graph.nodes.get(board_id)
    old_holes, old_footprints = _board_parts(before_board)
    new_holes, new_footprints = _board_parts(after_board)
    mounting_holes = _items(old_holes, new_holes)
    footprints = _items(old_footprints, new_footprints)
    dependent = [
        feature_id
        for feature_id in _dependents(after_graph, board_id)
        if feature_id in after_graph.nodes
        and feature_id in before_graph.nodes
        and _changed(before_result, after_result, feature_id)
        and after_graph.nodes[feature_id].__class__.__name__ in {"Standoffs", "PortCutout"}
    ]
    dependent = [
        {"Standoffs": "standoffs", "PortCutout": "port cutout"}[after_graph.nodes[feature_id].__class__.__name__]
        for feature_id in dependent
    ]
    return {
        "mountingHoles": mounting_holes,
        "footprints": footprints,
        "outlineChanged": (before_board.mechanics if before_board else {}).get("outline")
        != (after_board.mechanics if after_board else {}).get("outline"),
        "dependentFeatures": dependent,
    }


def format_summary(delta: dict[str, Any], board_id: str = "") -> str:
    labels = []
    for item in delta["footprints"]:
        if item["change"] == "moved" and item.get("reference"):
            labels.append(f"{item['reference']} moved {item['distance']:.1f} mm")
    for item in delta["mountingHoles"]:
        if item["change"] in {"added", "removed"}:
            labels.append(f"1 mounting hole {item['change']}")
    moved_holes = sum(item["change"] == "moved" for item in delta["mountingHoles"])
    if moved_holes:
        labels.append(f"{moved_holes} mounting hole{'s' if moved_holes != 1 else ''} moved")
    names = {"standoffs": "standoffs", "port cutout": "port cutout"}
    dependent = [names.get(item, item) for item in delta["dependentFeatures"]]
    if dependent:
        joined = dependent[0] if len(dependent) == 1 else " and ".join(dependent)
        labels.append(joined + " updated")
    return "refreshed board: " + (", ".join(labels) if labels else "source changed")
