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

    def _coherent_bodies(self, target: str) -> set[BodySource]:
        bodies = self.feature_bodies.get(target, set())
        shapes = {id(self.terminal_shapes[body]) for body in bodies}
        if not bodies or len(shapes) != 1:
            raise GraphError(
                "BODY_LINEAGE_DIVERGED",
                f"target {target} does not resolve to one body",
                target=target,
                bodySources=sorted(repr(body) for body in bodies),
                hint="target a feature that belongs to one body",
            )
        return bodies

    def terminal(self, target: str) -> str:
        bodies = self._coherent_bodies(target)
        terminals = {self.terminals[body] for body in bodies}
        if len(terminals) != 1:
            raise GraphError(
                "BODY_LINEAGE_DIVERGED",
                f"target {target} has diverged body terminals",
                target=target,
            )
        return next(iter(terminals))

    def shape(self, target: str) -> Any:
        bodies = self._coherent_bodies(target)
        return self.terminal_shapes[next(iter(bodies))]

    def candidates(self, target: str) -> list[Candidate]:
        return [
            candidate
            for body in sorted(self._coherent_bodies(target), key=repr)
            for candidate in self.terminal_states[body]
        ]

    def replace(self, target: str, feature: str, shape: Any, candidates: list[Candidate]) -> None:
        bodies = self._coherent_bodies(target)
        previous = self.terminal(target)
        previous_candidates = [
            candidate for body in bodies for candidate in self.terminal_states[body]
        ]
        self.predecessors[feature] = previous
        self.feature_bodies[feature] = bodies
        for body in bodies:
            self.body_predecessors[feature, body] = previous
            self.terminals[body] = feature
            self.terminal_shapes[body] = shape
            self.terminal_states[body] = [
                candidate for candidate in candidates if candidate.key.body == body
            ]
        self._refresh_superseded(previous)
        self._replace_provenance(previous_candidates, candidates)

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
        previous_candidates = [
            candidate for body in bodies for candidate in self.terminal_states[body]
        ]
        self.feature_bodies[feature] = bodies
        for body, (shape, candidates) in outputs.items():
            self.body_predecessors[feature, body] = previous[body]
            self.terminals[body] = feature
            self.terminal_shapes[body] = shape
            self.terminal_states[body] = candidates
        for predecessor in previous.values():
            self._refresh_superseded(predecessor)
        self._replace_provenance(
            previous_candidates,
            [candidate for _, candidates in outputs.values() for candidate in candidates],
        )

    def _refresh_superseded(self, feature: str) -> None:
        if feature not in self.terminals.values():
            self.superseded.add(feature)

    def _replace_provenance(self, previous: list[Candidate], current: list[Candidate]) -> None:
        groups: dict[str, list[Candidate]] = {}
        for item in current:
            groups.setdefault(item.key.feature, []).append(item)
        replaced_bodies = _body_sources(previous)
        affected = {item.key.feature for item in previous} | set(groups)
        for feature in affected:
            retained = [
                item
                for item in self.provenance.get(feature, [])
                if item.key.body not in replaced_bodies
            ]
            self.provenance[feature] = [*retained, *groups.get(feature, [])]
