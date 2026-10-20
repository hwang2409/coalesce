"""Stable terminal solids shared by metrics and the viewer."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .graph import BoardImport
from .references import BodySource, Candidate


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


def _terminal_shapes(result: Any) -> list[tuple[str, Any]]:
    consumed = {
        node.target
        for node in result.graph.nodes.values()
        if getattr(node, "target", None) in result.shapes
    }
    return [
        (feature, shape)
        for feature, shape in result.shapes.items()
        if feature not in consumed
    ]


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


def terminal_parts(result: Any, role: str | None = "manufacturing") -> list[TerminalPart]:
    owned = [
        candidate
        for candidates in result.provenance.values()
        for candidate in candidates
        if candidate.key.body is not None and candidate.entity is not None
    ]
    solids: list[tuple[str, Any, BodySource, tuple[str, ...]]] = []
    for feature, shape in _terminal_shapes(result):
        for solid in shape.solids():
            body, history = _solid_body(solid, owned)
            solids.append((feature, solid, body, history))

    counts: dict[BodySource, int] = {}
    for _, _, body, _ in solids:
        counts[body] = counts.get(body, 0) + 1
    parts: list[TerminalPart] = []
    for feature, solid, body, history in solids:
        part_role = (
            "reference"
            if isinstance(result.graph.nodes[body.feature], BoardImport)
            else "manufacturing"
        )
        if role is not None and part_role != role:
            continue
        base = f"{body.feature}/{body.entity}"
        if counts[body] > 1:
            suffix = hashlib.sha256("\n".join(history).encode()).hexdigest()[:12]
            part_id = f"{base}#{suffix}"
        else:
            part_id = base
        label = "pcb reference" if part_role == "reference" else body.feature
        parts.append(TerminalPart(part_id, label, part_role, body, feature, solid))
    if len({part.part_id for part in parts}) != len(parts):
        raise ValueError("terminal part provenance is ambiguous")
    return parts
