"""Stable terminal solids shared by metrics and the viewer."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .graph import BoardImport
from .references import BodySource, Candidate


class AmbiguousTerminalPartsError(ValueError):
    def __init__(self, part_ids: list[str]):
        self.part_ids = part_ids
        super().__init__("terminal part provenance is ambiguous")


@dataclass(frozen=True)
class TerminalPart:
    part_id: str
    label: str
    role: str
    source_body: BodySource
    terminal_feature: str
    shape: Any

    @property
    def bbox(self) -> dict[str, tuple[float, float, float]]:
        box = self.shape.bounding_box()
        return {
            "min": (box.min.X, box.min.Y, box.min.Z),
            "max": (box.max.X, box.max.Y, box.max.Z),
        }


def _terminal_shapes(
    result: Any,
) -> list[tuple[str, Any, set[BodySource], list[Candidate]]]:
    groups: dict[int, tuple[str, Any, set[BodySource], list[Candidate]]] = {}
    for body, shape in result.body_terminal_shapes.items():
        group = groups.setdefault(id(shape), (result.body_terminals[body], shape, set(), []))
        group[2].add(body)
        group[3].extend(result.body_terminal_states[body])
    return list(groups.values())


def _solid_body(solid: Any, owned: list[Candidate]) -> tuple[BodySource, tuple[str, ...]]:
    faces = [face.wrapped for face in solid.faces()]
    candidates = [
        candidate
        for candidate in owned
        if any(candidate.entity.wrapped.IsSame(face) for face in faces)
    ]
    bodies = {candidate.key.body for candidate in candidates}
    if len(bodies) != 1 or None in bodies:
        raise ValueError("terminal solid has ambiguous body provenance")
    body = next(iter(bodies))
    assert body is not None
    history = tuple(sorted(repr(candidate.key) for candidate in candidates))
    return body, history


def _terminal_part(
    result: Any,
    feature: str,
    solid: Any,
    body: BodySource,
    history: tuple[str, ...],
    split: bool,
) -> TerminalPart:
    role = (
        "reference"
        if isinstance(result.graph.nodes[body.feature], BoardImport)
        else "manufacturing"
    )
    part_id = f"{body.feature}/{body.entity}"
    if split:
        suffix = hashlib.sha256("\n".join(history).encode()).hexdigest()[:12]
        part_id = f"{part_id}#{suffix}"
    label = "pcb reference" if role == "reference" else body.feature
    return TerminalPart(part_id, label, role, body, feature, solid)


def _ambiguous_source_ids(parts: list[TerminalPart]) -> list[str]:
    counts = Counter(part.part_id for part in parts)
    duplicate_ids = {part_id for part_id, count in counts.items() if count > 1}
    return sorted(
        {
            f"{part.source_body.feature}/{part.source_body.entity}"
            for part in parts
            if part.part_id in duplicate_ids
        }
    )


def _select_unique_parts(parts: list[TerminalPart], role: str | None) -> list[TerminalPart]:
    selected = parts if role is None else [part for part in parts if part.role == role]
    if ambiguous_source_ids := _ambiguous_source_ids(selected):
        raise AmbiguousTerminalPartsError(ambiguous_source_ids)
    return selected


def terminal_parts(result: Any, role: str | None = "manufacturing") -> list[TerminalPart]:
    owned = [
        candidate
        for candidates in result.provenance.values()
        for candidate in candidates
        if candidate.key.body is not None and candidate.entity is not None
    ]
    solids: list[tuple[str, Any, BodySource, tuple[str, ...]]] = []
    for feature, shape, expected_bodies, terminal_state in _terminal_shapes(result):
        for solid in shape.solids():
            body, history = _solid_body(solid, [*owned, *terminal_state])
            if body not in expected_bodies:
                raise ValueError("terminal solid does not match its body lineage")
            solids.append((feature, solid, body, history))

    counts: dict[BodySource, int] = {}
    for _, _, body, _ in solids:
        counts[body] = counts.get(body, 0) + 1
    parts = [
        _terminal_part(result, feature, solid, body, history, counts[body] > 1)
        for feature, solid, body, history in solids
    ]
    return _select_unique_parts(parts, role)
