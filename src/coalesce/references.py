from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from math import isclose, sqrt
from typing import Any


class Cardinality(str, Enum):
    ONE = "one"
    ONE_OR_MORE = "one_or_more"
    EXACTLY = "exactly"
    ANY = "any"


class Status(str, Enum):
    OK = "OK"
    SPLIT = "SPLIT"
    MERGED = "MERGED"
    AMBIGUOUS = "AMBIGUOUS"
    EMPTY = "EMPTY"
    ORDER = "ORDER"


@dataclass(frozen=True)
class BodySource:
    feature: str
    entity: str


@dataclass(frozen=True)
class SketchSegmentSource:
    sketch: str
    entity: str
    segment: str


@dataclass(frozen=True)
class ElementKey:
    feature: str
    role: str
    source: SketchSegmentSource | str | tuple[ElementKey, ...] | None = None
    body: BodySource | None = None


@dataclass
class Candidate:
    key: ElementKey
    entity: Any = field(default=None, repr=False, compare=False)
    descriptor: dict[str, Any] = field(default_factory=dict)
    merged_from: tuple[ElementKey, ...] = ()


@dataclass(frozen=True)
class Normal:
    direction: tuple[float, float, float]
    tolerance: float = 1e-6


@dataclass(frozen=True)
class Position:
    direction: tuple[float, float, float]
    value: float
    tolerance: float = 1e-6


@dataclass(frozen=True)
class MaxAlong:
    direction: tuple[float, float, float]
    tolerance: float = 1e-6


Filter = Normal | Position | MaxAlong


@dataclass
class Reference:
    feature: str
    role: str
    cardinality: Cardinality = Cardinality.ONE
    count: int | None = None
    source: SketchSegmentSource | str | None = None
    body: BodySource | None = None
    filters: list[Filter] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.cardinality is Cardinality.EXACTLY and (self.count is None or self.count < 0):
            raise ValueError("exactly cardinality requires a non-negative count")

    def to_dict(self) -> dict[str, Any]:
        filters = [
            {"kind": type(predicate).__name__, **asdict(predicate)}
            for predicate in self.filters
        ]
        return {
            "feature": self.feature,
            "role": self.role,
            "cardinality": self.cardinality.value,
            "count": self.count,
            "source": (
                {"kind": "sketch_segment", **asdict(self.source)}
                if isinstance(self.source, SketchSegmentSource)
                else self.source
            ),
            "body": (
                {"kind": "body_source", **asdict(self.body)}
                if self.body is not None
                else None
            ),
            "filters": filters,
        }


@dataclass
class Resolution:
    status: Status
    candidates: list[Candidate] = field(default_factory=list)
    error: dict[str, Any] | None = None


def _dot(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _unit(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = sqrt(_dot(vector, vector))
    if not length:
        raise ValueError("filter direction must not be zero")
    return tuple(value / length for value in vector)  # type: ignore[return-value]


def _candidate_error(candidate: Candidate) -> dict[str, Any]:
    return {
        "keys": [asdict(candidate.key), *(asdict(key) for key in candidate.merged_from)],
        "descriptor": candidate.descriptor,
    }


def _error(ref: Reference, status: Status, candidates: list[Candidate]) -> dict[str, Any]:
    suggestions = {
        Status.EMPTY: "check the feature id, role, source, and filters",
        Status.SPLIT: "use one_or_more or add a provenance-set filter",
        Status.MERGED: "accept the merged result or select a different provenance set",
        Status.AMBIGUOUS: "add a provenance-set filter or change the cardinality",
        Status.ORDER: "move the referenced feature before its consumer",
    }
    return {
        "feature": ref.feature,
        "reference": ref.to_dict(),
        "status": status.value,
        "candidates": [_candidate_error(candidate) for candidate in candidates],
        "suggestion": suggestions[status],
    }


@dataclass
class _FilterResult:
    candidates: list[Candidate]
    tied: bool = False


def _filter(candidates: list[Candidate], predicate: Filter) -> _FilterResult:
    direction = _unit(predicate.direction)
    if isinstance(predicate, Normal):
        return _FilterResult(
            [
                candidate
                for candidate in candidates
                if "normal" in candidate.descriptor
                and isclose(
                    _dot(_unit(tuple(candidate.descriptor["normal"])), direction),
                    1.0,
                    abs_tol=predicate.tolerance,
                )
            ]
        )

    positions = [
        (candidate, _dot(tuple(candidate.descriptor["center"]), direction))
        for candidate in candidates
        if "center" in candidate.descriptor
    ]
    if isinstance(predicate, Position):
        return _FilterResult(
            [
                candidate
                for candidate, position in positions
                if isclose(position, predicate.value, abs_tol=predicate.tolerance)
            ]
        )
    if not positions:
        return _FilterResult([])
    maximum = max(position for _, position in positions)
    selected = [
        candidate
        for candidate, position in positions
        if isclose(position, maximum, abs_tol=predicate.tolerance)
    ]
    return _FilterResult(selected, tied=len(selected) > 1)


def _select(ref: Reference, provenance: dict[str, list[Candidate]]) -> _FilterResult:
    candidates = [
        candidate
        for candidate in provenance.get(ref.feature, [])
        if candidate.key.role == ref.role
        and (ref.source is None or candidate.key.source == ref.source)
        and (ref.body is None or candidate.key.body == ref.body)
    ]
    tied = False
    for predicate in ref.filters:
        result = _filter(candidates, predicate)
        candidates = result.candidates
        tied = tied or result.tied
    return _FilterResult(candidates, tied)


def _classify(result: _FilterResult) -> Status:
    candidates = result.candidates
    if not candidates:
        return Status.EMPTY
    if result.tied:
        return Status.AMBIGUOUS
    if any(candidate.merged_from for candidate in candidates):
        return Status.MERGED
    if len(candidates) > 1 and len({candidate.key for candidate in candidates}) == 1:
        return Status.SPLIT
    return Status.OK


def _violates_cardinality(ref: Reference, count: int) -> bool:
    if ref.cardinality is Cardinality.ANY:
        return False
    if ref.cardinality is Cardinality.ONE:
        return count != 1
    if ref.cardinality is Cardinality.ONE_OR_MORE:
        return count < 1
    return count != ref.count


def resolve(
    ref: Reference,
    provenance: dict[str, list[Candidate]],
    *,
    ordered_features: list[str] | None = None,
    before_feature: str | None = None,
) -> Resolution:
    if (
        before_feature is not None
        and ordered_features is not None
        and ref.feature in ordered_features
        and before_feature in ordered_features
        and ordered_features.index(ref.feature) >= ordered_features.index(before_feature)
    ):
        return Resolution(Status.ORDER, error=_error(ref, Status.ORDER, []))

    filtered = _select(ref, provenance)
    candidates = filtered.candidates
    status = _classify(filtered)
    violates = _violates_cardinality(ref, len(candidates))
    if status is Status.OK and violates:
        status = Status.AMBIGUOUS
    error = _error(ref, status, candidates) if violates or filtered.tied else None
    return Resolution(status, candidates, error)
