"""Deterministic board refresh changes for MCP and viewer output."""

from __future__ import annotations

import math
import re
from typing import Any

from .graph import BoardImport, PcbBase, PcbLid, PortCutout, Standoffs

LENGTH_TOLERANCE_MM = 1e-6


def _point(value: Any) -> list[float]:
    return [float(value[0]), float(value[1])]


def _position(item: dict | None) -> list[float] | None:
    return _point(item["position"]) if item and item.get("position") else None


def _item_change(
    item_id: str,
    was: dict | None,
    now: dict | None,
    reference: str,
    compared_fields: tuple[str, ...],
) -> dict | None:
    old_position, new_position = _position(was), _position(now)
    distance = None
    if old_position is not None and new_position is not None:
        distance = math.dist(old_position, new_position)
    if was is None or now is None:
        change = "added" if was is None else "removed"
    elif distance is not None and distance > LENGTH_TOLERANCE_MM:
        change = "moved"
    elif any(was.get(field) != now.get(field) for field in compared_fields):
        change = "changed"
    else:
        return None
    return {
        "id": item_id,
        "reference": (now or was).get(reference),
        "oldPosition": old_position,
        "newPosition": new_position,
        "distance": distance,
        "change": change,
    }


def _items(
    before: list[dict],
    after: list[dict],
    *,
    compared_fields: tuple[str, ...],
    reference: str = "reference",
) -> list[dict]:
    old = {item.get("id"): item for item in before}
    new = {item.get("id"): item for item in after}
    changes = (
        _item_change(
            item_id,
            old.get(item_id),
            new.get(item_id),
            reference,
            compared_fields,
        )
        for item_id in sorted(set(old) | set(new))
    )
    return [change for change in changes if change is not None]


def _dependents(graph: Any, board_id: str) -> list[str]:
    affected = {board_id}
    while additions := {
        node_id
        for node_id, node in graph.nodes.items()
        if node_id not in affected and set(graph._dependencies(node)) & affected
    }:
        affected.update(additions)
    return sorted(affected - {board_id})


def _board_parts(board: Any) -> tuple[list[dict], list[dict]]:
    mechanics = board.mechanics if board is not None else {}
    holes = [hole for hole in mechanics.get("holes", []) if hole.get("mounting")]
    ids = {item["id"] for item in holes}
    references = {item.get("reference") for item in holes}
    if None in references:
        references.add("REF**")
    footprints = [
        item
        for item in mechanics.get("footprints", [])
        if item["id"] not in ids and item.get("reference") not in references
    ]
    return holes, footprints


def _node_board(graph: Any, node: Any) -> BoardImport | None:
    board_id = getattr(node, "board", None)
    if isinstance(node, (PcbLid, PortCutout)):
        base = graph.nodes.get(node.base)
        board_id = base.board if isinstance(base, PcbBase) else None
    board = graph.nodes.get(board_id)
    return board if isinstance(board, BoardImport) else None


def _selected_hole_ids(graph: Any, node: Standoffs) -> set[str]:
    from .enclosure import selected_holes

    board = _node_board(graph, node)
    return {hole["id"] for hole in selected_holes(node, board)} if board else set()


def _standoffs_changed(
    before_graph: Any,
    after_graph: Any,
    feature_id: str,
    hole_changes: list[dict],
) -> bool:
    old_node = before_graph.nodes[feature_id]
    new_node = after_graph.nodes[feature_id]
    consumed = _selected_hole_ids(before_graph, old_node) | _selected_hole_ids(after_graph, new_node)
    return any(change["id"] in consumed for change in hole_changes)


def _footprint(graph: Any, node: PortCutout) -> dict | None:
    board = _node_board(graph, node)
    if board is None:
        return None
    footprint_id = node.footprint_id or node.footprint
    return next(
        (
            item
            for item in board.mechanics.get("footprints", [])
            if item["id"] == footprint_id or item.get("reference") == footprint_id
        ),
        None,
    )


def _port_cutout_changed(before_graph: Any, after_graph: Any, feature_id: str) -> bool:
    old_node = before_graph.nodes[feature_id]
    new_node = after_graph.nodes[feature_id]
    old = _footprint(before_graph, old_node)
    new = _footprint(after_graph, new_node)
    if old is None or new is None:
        return True
    distance = math.dist(_position(old), _position(new))
    if distance > LENGTH_TOLERANCE_MM or old.get("rotation") != new.get("rotation"):
        return True
    if new_node.envelope_source == "courtyard":
        return old.get("courtyard") != new.get("courtyard")
    return old_node.envelope_source != new_node.envelope_source


def _board_dimensions_changed(before_board: Any, after_board: Any) -> bool:
    old = before_board.mechanics if before_board else {}
    new = after_board.mechanics if after_board else {}
    return any(old.get(field) != new.get(field) for field in ("outline", "thickness"))


def _outline_bbox(board: Any) -> dict[str, float] | None:
    loops = (board.mechanics if board else {}).get("outline", [])
    if not loops:
        return None
    points = [
        point
        for segment in loops[0].get("segments", [])
        for point in (segment.get("start"), segment.get("mid"), segment.get("end"))
        if point is not None
    ]
    if not points:
        return None
    xs, ys = zip(*points)
    return {"width": max(xs) - min(xs), "height": max(ys) - min(ys)}


def _feature_changed(
    before_graph: Any,
    after_graph: Any,
    feature_id: str,
    hole_changes: list[dict],
    board_dimensions_changed: bool,
    changed: set[str],
) -> bool:
    node = after_graph.nodes[feature_id]
    # every feature changes when a feature it consumes changed
    if set(after_graph._dependencies(node)) & changed:
        return True
    if isinstance(node, Standoffs):
        return _standoffs_changed(before_graph, after_graph, feature_id, hole_changes)
    if isinstance(node, PortCutout):
        # the cutout also reads the board thickness directly
        return board_dimensions_changed or _port_cutout_changed(before_graph, after_graph, feature_id)
    if isinstance(node, (PcbBase, PcbLid)):
        return board_dimensions_changed
    return False


def _kind_label(node: Any) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", " ", node.__class__.__name__).lower()


def refresh_delta(
    before_graph: Any,
    after_graph: Any,
    _before_result: Any,
    _after_result: Any,
    board_id: str,
) -> dict[str, Any]:
    before_board = before_graph.nodes.get(board_id)
    after_board = after_graph.nodes.get(board_id)
    old_holes, old_footprints = _board_parts(before_board)
    new_holes, new_footprints = _board_parts(after_board)
    mounting_holes = _items(old_holes, new_holes, compared_fields=("drill",))
    footprints = _items(
        old_footprints,
        new_footprints,
        compared_fields=("rotation", "courtyard", "courtyard_availability"),
    )
    dimensions_changed = _board_dimensions_changed(before_board, after_board)
    affected = set(_dependents(after_graph, board_id))
    changed: set[str] = set()
    # inputs before consumers, so a consumer sees every changed input
    for node in after_graph.ordered():
        if node.id in affected and node.id in before_graph.nodes and _feature_changed(
            before_graph, after_graph, node.id, mounting_holes, dimensions_changed, changed
        ):
            changed.add(node.id)
    dependent = [_kind_label(after_graph.nodes[feature_id]) for feature_id in sorted(changed)]
    old_mechanics = before_board.mechanics if before_board else {}
    new_mechanics = after_board.mechanics if after_board else {}
    delta = {
        "mountingHoles": mounting_holes,
        "footprints": footprints,
        "outlineChanged": old_mechanics.get("outline") != new_mechanics.get("outline"),
        "dependentFeatures": dependent,
    }
    if old_mechanics.get("thickness") != new_mechanics.get("thickness"):
        delta["thickness"] = {"before": old_mechanics.get("thickness"), "after": new_mechanics.get("thickness")}
    if delta["outlineChanged"]:
        delta["outline"] = {"before": _outline_bbox(before_board), "after": _outline_bbox(after_board)}
    return delta


def _footprint_labels(footprints: list[dict]) -> list[str]:
    return [
        f"{item['reference']} moved {item['distance']:.1f} mm"
        for item in footprints
        if item["change"] == "moved" and item.get("reference")
    ]


def _named_hole_label(item: dict) -> str:
    label = f"{item['reference']} {item['change']}"
    if item["change"] == "added":
        x, y = item["newPosition"]
        label += f" at ({x:.1f}, {y:.1f})"
    return label


def _hole_labels(holes: list[dict]) -> list[str]:
    labels = []
    for change in ("added", "removed"):
        named = [item for item in holes if item["change"] == change and item.get("reference")]
        labels.extend(_named_hole_label(item) for item in named)
        unnamed = sum(item["change"] == change and not item.get("reference") for item in holes)
        if unnamed:
            labels.append(f"{unnamed} mounting hole{'s' if unnamed != 1 else ''} {change}")
    for change in ("changed", "moved"):
        count = sum(item["change"] == change for item in holes)
        if count:
            labels.append(f"{count} mounting hole{'s' if count != 1 else ''} {change}")
    return labels


def _dependent_label(dependents: list[str]) -> list[str]:
    if not dependents:
        return []
    joined = dependents[0] if len(dependents) == 1 else ", ".join(dependents[:-1]) + " and " + dependents[-1]
    return [joined + " updated"]


def _dimension_labels(delta: dict[str, Any]) -> list[str]:
    labels = []
    if thickness := delta.get("thickness"):
        labels.append(f"board thickness {thickness['before']:.1f} → {thickness['after']:.1f} mm")
    if delta.get("outlineChanged"):
        outline = delta.get("outline") or {}
        before, after = outline.get("before"), outline.get("after")
        labels.append(
            f"outline {before['width']:.1f} × {before['height']:.1f} → "
            f"{after['width']:.1f} × {after['height']:.1f} mm"
            if before and after else "board outline changed"
        )
    return labels


def format_summary(delta: dict[str, Any], board_id: str = "") -> str:
    labels = [
        *_dimension_labels(delta),
        *_footprint_labels(delta["footprints"]),
        *_hole_labels(delta["mountingHoles"]),
        *_dependent_label(delta["dependentFeatures"]),
    ]
    if not labels:
        return "refreshed board: source changed"
    summary = ", ".join(labels)
    if len(summary) > 160:
        shown, remaining = [], len(labels)
        for label in labels:
            candidate = ", ".join([*shown, label, f"+{remaining - len(shown) - 1} more"])
            if len(candidate) > 160:
                break
            shown.append(label)
        summary = ", ".join([*shown, f"+{remaining - len(shown)} more"])
    return "refreshed board: " + summary
