from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .graph import GraphError
from .references import BodySource, Candidate


def _body_sources(candidates: list[Candidate]) -> set[BodySource]:
    return {candidate.key.body for candidate in candidates if candidate.key.body is not None}


@dataclass
class BodyLineage:
    """Owns each body's current terminal shape and feature predecessor chain."""

    states: dict[str, list[Candidate]]
    provenance: dict[str, list[Candidate]]
    terminals: dict[BodySource, str] = field(default_factory=dict)
    terminal_shapes: dict[BodySource, Any] = field(default_factory=dict)
    terminal_states: dict[BodySource, list[Candidate]] = field(default_factory=dict)
    feature_bodies: dict[str, set[BodySource]] = field(default_factory=dict)
    predecessors: dict[str, str] = field(default_factory=dict)
    body_predecessors: dict[tuple[str, BodySource], str] = field(default_factory=dict)
    superseded: set[str] = field(default_factory=set)

    def create(self, feature: str, shape: Any, candidates: list[Candidate]) -> None:
        bodies = _body_sources(candidates)
        self.feature_bodies[feature] = bodies
        for body in bodies:
            self.terminals[body] = feature
            self.terminal_shapes[body] = shape
            self.terminal_states[body] = [
                candidate for candidate in candidates if candidate.key.body == body
            ]

    def body(self, target: str) -> BodySource:
        bodies = self.feature_bodies.get(target, set())
        if len(bodies) != 1:
            raise GraphError(
                "BODY_LINEAGE_DIVERGED",
                f"target {target} does not resolve to one body",
                target=target,
                bodySources=sorted(repr(body) for body in bodies),
                hint="target a feature that belongs to one body",
            )
        return next(iter(bodies))

    def terminal(self, target: str) -> str:
        return self.terminals[self.body(target)]

    def shape(self, target: str) -> Any:
        return self.terminal_shapes[self.body(target)]

    def candidates(self, target: str) -> list[Candidate]:
        return self.terminal_states[self.body(target)]

    def replace(self, target: str, feature: str, shape: Any, candidates: list[Candidate]) -> None:
        body = self.body(target)
        previous = self.terminals[body]
        self.predecessors[feature] = previous
        self.body_predecessors[feature, body] = previous
        self.feature_bodies[feature] = {body}
        self.terminals[body] = feature
        self.terminal_shapes[body] = shape
        self.terminal_states[body] = [
            candidate for candidate in candidates if candidate.key.body == body
        ]
        self._refresh_superseded(previous)
        self._replace_provenance(self.states[previous], candidates)

    def replace_many(
        self,
        targets: tuple[str, ...],
        feature: str,
        outputs: dict[BodySource, tuple[Any, list[Candidate]]],
    ) -> None:
        bodies = {self.body(target) for target in targets}
        if bodies != set(outputs):
            raise GraphError(
                "BODY_LINEAGE_DIVERGED",
                f"feature {feature} outputs do not match its target bodies",
                feature=feature,
            )
        previous = {body: self.terminals[body] for body in bodies}
        self.feature_bodies[feature] = bodies
        for body, (shape, candidates) in outputs.items():
            self.body_predecessors[feature, body] = previous[body]
            self.terminals[body] = feature
            self.terminal_shapes[body] = shape
            self.terminal_states[body] = candidates
        for predecessor in previous.values():
            self._refresh_superseded(predecessor)
        self._replace_provenance(
            [candidate for terminal in previous.values() for candidate in self.states[terminal]],
            [candidate for _, candidates in outputs.values() for candidate in candidates],
        )

    def _refresh_superseded(self, feature: str) -> None:
        if feature not in self.terminals.values():
            self.superseded.add(feature)

    def _replace_provenance(self, previous: list[Candidate], current: list[Candidate]) -> None:
        groups: dict[str, list[Candidate]] = {}
        for item in current:
            groups.setdefault(item.key.feature, []).append(item)
        affected = {item.key.feature for item in previous} | set(groups)
        for feature in affected:
            self.provenance[feature] = groups.get(feature, [])
