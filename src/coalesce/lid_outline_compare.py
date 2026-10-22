from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LidOutlineSegment:
    kind: str
    start: tuple[float, float, float]
    midpoint: tuple[float, float, float]
    end: tuple[float, float, float]
    source: str
    center: tuple[float, float, float] | None = None
    radius: float | None = None


@dataclass(frozen=True)
class LidOutlineGeometry:
    segments: tuple[LidOutlineSegment, ...]
    area: float
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class LidOutlineComparison:
    matches: bool
    reason: str | None
    area_difference: float
    bbox_difference: float


def _outline_differences(
    first: LidOutlineGeometry | None, second: LidOutlineGeometry | None
) -> tuple[float, float]:
    if first is None or second is None:
        return float("inf"), float("inf")
    bbox_difference = max(
        abs(left - right) for left, right in zip(first.bbox, second.bbox, strict=True)
    )
    return abs(first.area - second.area), bbox_difference


def _topology_difference(
    first: LidOutlineGeometry, second: LidOutlineGeometry
) -> str | None:
    if len(first.segments) != len(second.segments):
        return f"segment count differs: {len(first.segments)} != {len(second.segments)}"
    for left, right in zip(first.segments, second.segments, strict=True):
        if left.kind not in {"line", "arc"} or right.kind not in {"line", "arc"}:
            return "segment has unsupported type"
        if left.kind != right.kind:
            return "segment kind differs"
        if left.kind == "arc" and (
            left.radius is None
            or right.radius is None
            or abs(left.radius - right.radius) > 1e-6
        ):
            return "arc radius differs"
    return None


def _comparison(
    matches: bool,
    reason: str | None,
    first: LidOutlineGeometry | None,
    second: LidOutlineGeometry | None,
) -> LidOutlineComparison:
    area, bbox = _outline_differences(first, second)
    return LidOutlineComparison(matches, reason, area, bbox)


def compare_lid_outlines(
    outline_source: str | None,
    current_source: str,
    source: LidOutlineGeometry | None,
    current: LidOutlineGeometry | None,
    *,
    legacy_stale: bool = False,
) -> LidOutlineComparison:
    if outline_source is None and not legacy_stale:
        return LidOutlineComparison(True, None, 0.0, 0.0)
    if outline_source is not None and outline_source != current_source:
        reason = f"outline source is stale: {outline_source} != {current_source}"
        return _comparison(False, reason, source, current)
    if source is None:
        return _comparison(False, "outline source does not resolve", source, current)
    if current is None:
        return _comparison(False, "current outline source does not resolve", source, current)
    if difference := _topology_difference(source, current):
        return _comparison(False, difference, source, current)
    area, bbox = _outline_differences(source, current)
    if area > 1e-6:
        return LidOutlineComparison(False, "outline area differs", area, bbox)
    if bbox > 1e-6:
        return LidOutlineComparison(False, "outline bounding box differs", area, bbox)
    return LidOutlineComparison(True, None, area, bbox)
