from __future__ import annotations

from dataclasses import dataclass, field

from .graph import GraphError
from .references import BodySource, Candidate


def _body_sources(candidates: list[Candidate]) -> set[BodySource]:
    return {candidate.key.body for candidate in candidates if candidate.key.body is not None}


@dataclass
class BodyLineage:
    """Owns each body's current terminal and feature predecessor chain."""

    states: dict[str, list[Candidate]]
    provenance: dict[str, list[Candidate]]
    terminals: dict[BodySource, str] = field(default_factory=dict)
    feature_bodies: dict[str, set[BodySource]] = field(default_factory=dict)
    predecessors: dict[str, str] = field(default_factory=dict)
    superseded: set[str] = field(default_factory=set)

    def create(self, feature: str, candidates: list[Candidate]) -> None:
        bodies = _body_sources(candidates)
        self.feature_bodies[feature] = bodies
        for body in bodies:
            self.terminals[body] = feature

    def terminal(self, target: str) -> str:
        bodies = self.feature_bodies.get(target, set())
        terminals = {self.terminals[body] for body in bodies}
        if len(terminals) != 1:
            raise GraphError(
                "BODY_LINEAGE_DIVERGED",
                f"target {target} does not resolve to one current body terminal",
                target=target,
                terminalFeatureIds=sorted(terminals),
                hint="target a feature whose bodies share one terminal",
            )
        return next(iter(terminals))

    def replace(self, target: str, feature: str, candidates: list[Candidate]) -> None:
        previous = self.terminal(target)
        bodies = self.feature_bodies[target]
        self.predecessors[feature] = previous
        self.superseded.add(previous)
        self.feature_bodies[feature] = set(bodies)
        for body in bodies:
            self.terminals[body] = feature
        self._replace_provenance(self.states[previous], candidates)

    def replace_many(
        self, targets: tuple[str, ...], feature: str, candidates: list[Candidate]
    ) -> None:
        previous = [self.terminal(target) for target in targets]
        bodies = set().union(*(self.feature_bodies[target] for target in targets))
        self.feature_bodies[feature] = bodies
        self.predecessors[feature] = previous[0]
        self.superseded.update(previous)
        for body in bodies:
            self.terminals[body] = feature
        self._replace_provenance(
            [candidate for terminal in previous for candidate in self.states[terminal]],
            candidates,
        )

    def _replace_provenance(
        self, previous: list[Candidate], current: list[Candidate]
    ) -> None:
        groups: dict[str, list[Candidate]] = {}
        for item in current:
            groups.setdefault(item.key.feature, []).append(item)
        affected = {item.key.feature for item in previous} | set(groups)
        for feature in affected:
            self.provenance[feature] = groups.get(feature, [])
