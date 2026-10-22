"""Deterministic board refresh changes for MCP and viewer output."""

from __future__ import annotations

import math
from typing import Any

LENGTH_TOLERANCE_MM = 1e-6
AREA_TOLERANCE_MM2 = 1e-6
VOLUME_TOLERANCE_MM3 = 1e-6


def _point(value: Any) -> list[float]:
    return [float(value[0]), float(value[1])]


def _position(item: dict | None) -> list[float] | None:
    return _point(item["position"]) if item and item.get("position") else None


def _item_change(item_id: str, was: dict | None, now: dict | None, reference: str) -> dict | None:
    old_position, new_position = _position(was), _position(now)
    distance = None
    if old_position is not None and new_position is not None:
        distance = math.dist(old_position, new_position)
        if distance <= LENGTH_TOLERANCE_MM:
            return None
    return {
        "id": item_id,
        "reference": (now or was).get(reference),
        "oldPosition": old_position,
        "newPosition": new_position,
        "distance": distance,
        "change": "added" if was is None else "removed" if now is None else "moved",
    }


def _items(before: list[dict], after: list[dict], *, reference: str = "reference") -> list[dict]:
    old = {item.get("id"): item for item in before}
    new = {item.get("id"): item for item in after}
    changes = (
        _item_change(item_id, old.get(item_id), new.get(item_id), reference)
        for item_id in sorted(set(old) | set(new))
    )
    return [change for change in changes if change is not None]


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


def _coordinates(vector: Any) -> tuple[float, float, float]:
    return (float(vector.X), float(vector.Y), float(vector.Z))


def _shape_fingerprint(shape: Any) -> dict[str, Any]:
    bounds = shape.bounding_box()
    return {
        "volume": (float(shape.volume),),
        "area": (float(shape.area),),
        "bounds": (*_coordinates(bounds.min), *_coordinates(bounds.max)),
        "center": _coordinates(shape.center()),
        "counts": (len(shape.solids()), len(shape.faces())),
    }


def _values_equal(before: tuple[float, ...], after: tuple[float, ...], tolerance: float) -> bool:
    return all(math.isclose(old, new, rel_tol=0, abs_tol=tolerance) for old, new in zip(before, after))


def _changed(before: Any, after: Any, feature_id: str) -> bool:
    old = before.shapes.get(feature_id)
    new = after.shapes.get(feature_id)
    if old is None or new is None:
        return old is not new
    old_fingerprint, new_fingerprint = _shape_fingerprint(old), _shape_fingerprint(new)
    return (
        old_fingerprint["counts"] != new_fingerprint["counts"]
        or not _values_equal(old_fingerprint["volume"], new_fingerprint["volume"], VOLUME_TOLERANCE_MM3)
        or not _values_equal(old_fingerprint["area"], new_fingerprint["area"], AREA_TOLERANCE_MM2)
        or not _values_equal(old_fingerprint["bounds"], new_fingerprint["bounds"], LENGTH_TOLERANCE_MM)
        or not _values_equal(old_fingerprint["center"], new_fingerprint["center"], LENGTH_TOLERANCE_MM)
    )


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


def _footprint_labels(footprints: list[dict]) -> list[str]:
    return [
        f"{item['reference']} moved {item['distance']:.1f} mm"
        for item in footprints
        if item["change"] == "moved" and item.get("reference")
    ]


def _hole_labels(holes: list[dict]) -> list[str]:
    labels = [
        f"1 mounting hole {item['change']}"
        for item in holes
        if item["change"] in {"added", "removed"}
    ]
    moved = sum(item["change"] == "moved" for item in holes)
    if moved:
        labels.append(f"{moved} mounting hole{'s' if moved != 1 else ''} moved")
    return labels


def _dependent_label(dependents: list[str]) -> list[str]:
    if not dependents:
        return []
    joined = dependents[0] if len(dependents) == 1 else " and ".join(dependents)
    return [joined + " updated"]


def format_summary(delta: dict[str, Any], board_id: str = "") -> str:
    labels = [
        *_footprint_labels(delta["footprints"]),
        *_hole_labels(delta["mountingHoles"]),
        *_dependent_label(delta["dependentFeatures"]),
    ]
    return "refreshed board: " + (", ".join(labels) if labels else "source changed")
