"""One regeneration result for constrained-sketch solve availability."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SketchFailure:
    feature_id: str
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UnavailableFeature:
    feature_id: str
    depends_on: str


@dataclass(frozen=True)
class SolveAvailability:
    solved: tuple[str, ...] = ()
    failed: tuple[SketchFailure, ...] = ()
    unavailable: tuple[UnavailableFeature, ...] = ()

    @property
    def geometry_available(self) -> bool:
        return not self.failed

    def failure_details(self) -> dict[str, list[str]]:
        return {
            "failedFeatureIds": [failure.feature_id for failure in self.failed],
            "unavailableFeatureIds": [feature.feature_id for feature in self.unavailable],
        }
