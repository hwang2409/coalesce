from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .references import Candidate, Cardinality, Reference, Resolution, Status


@dataclass
class Parameter:
    name: str
    value: float


@dataclass
class Plane:
    name: str = "XY"


@dataclass
class Rectangle:
    id: str
    width: float
    height: float


@dataclass
class Circle:
    id: str
    radius: float


@dataclass
class Sketch:
    id: str
    plane: Plane = field(default_factory=Plane)
    entities: list[Rectangle | Circle] = field(default_factory=list)


@dataclass
class Feature:
    id: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Extrude(Feature):
    sketch: str = ""
    height: float = 1


@dataclass
class Cut(Feature):
    target: str = ""
    tool: str = ""


@dataclass
class Hole(Feature):
    target: str = ""
    diameter: float = 1
    depth: float | None = None


@dataclass
class Fillet(Feature):
    target: str = ""
    edges: list[Reference] = field(default_factory=list)
    radius: float = 1


class GraphError(ValueError):
    def __init__(self, code: str, message: str, **details: Any):
        self.error = {"code": code, "message": message, **details}
        super().__init__(message)


class Graph:
    def __init__(self):
        self.nodes: dict[str, Any] = {}
        self.provenance: dict[str, list[Candidate]] = {}
        self.shapes: dict[str, Any] = {}

    def add(self, node: Any) -> Any:
        if node.id in self.nodes:
            raise GraphError("duplicate_id", f"duplicate id: {node.id}")
        self.nodes[node.id] = node
        return node

    def validate(self) -> None:
        for node in self.nodes.values():
            refs = list(getattr(node, "depends_on", []))
            for name in ("sketch", "target", "tool"):
                value = getattr(node, name, None)
                if value:
                    refs.append(value)
            for ref in refs:
                if ref not in self.nodes:
                    raise GraphError("unknown_id", f"unknown id: {ref}", feature=node.id)
        visiting: set[str] = set()
        done: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise GraphError("cycle", "feature graph contains a cycle", feature=key)
            if key in done:
                return
            visiting.add(key)
            node = self.nodes[key]
            for dep in getattr(node, "depends_on", []) + [
                getattr(node, x) for x in ("sketch", "target", "tool") if getattr(node, x, None)
            ]:
                visit(dep)
            visiting.remove(key)
            done.add(key)

        for key in self.nodes:
            visit(key)

    def ordered(self) -> list[Any]:
        self.validate()
        result: list[Any] = []
        seen: set[str] = set()

        def add(key: str) -> None:
            if key in seen:
                return
            node = self.nodes[key]
            for dep in getattr(node, "depends_on", []) + [
                getattr(node, x) for x in ("sketch", "target", "tool") if getattr(node, x, None)
            ]:
                add(dep)
            seen.add(key)
            result.append(node)

        for key in self.nodes:
            add(key)
        return result

    def resolve(self, ref: Reference) -> Resolution:
        if ref.feature not in self.nodes and ref.feature not in self.provenance:
            return Resolution(
                Status.EMPTY,
                error={
                    "ref": ref.to_dict(),
                    "feature": ref.feature,
                    "suggestion": "restore the referenced feature",
                },
            )
        candidates = self.provenance.get(ref.feature, [])
        if ref.source:
            candidates = [c for c in candidates if c.key.source == ref.source]
        candidates = [c for c in candidates if c.key.role == ref.role]
        if not candidates:
            return Resolution(
                Status.EMPTY,
                error={
                    "ref": ref.to_dict(),
                    "feature": ref.feature,
                    "suggestion": "check the feature id and role",
                },
            )
        if ref.cardinality == Cardinality.ONE and len(candidates) != 1:
            status = Status.SPLIT if len(candidates) > 1 else Status.EMPTY
            return Resolution(
                status,
                candidates,
                {"ref": ref.to_dict(), "candidates": [asdict(x) for x in candidates]},
            )
        if ref.cardinality == Cardinality.EXACTLY and len(candidates) != ref.count:
            return Resolution(
                Status.AMBIGUOUS,
                candidates,
                {"ref": ref.to_dict(), "candidates": [asdict(x) for x in candidates]},
            )
        return Resolution(Status.OK, candidates)

    def to_json(self) -> str:
        from .serialization import dumps

        return dumps(self)
