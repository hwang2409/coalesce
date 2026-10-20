from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .references import Reference


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
    position: tuple[float, float] = (0.0, 0.0)
    rotation: float = 0.0


@dataclass
class Circle:
    id: str
    radius: float
    position: tuple[float, float] = (0.0, 0.0)
    rotation: float = 0.0


@dataclass
class Sketch:
    id: str
    plane: Plane = field(default_factory=Plane)
    entities: list[Rectangle | Circle] = field(default_factory=list)
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)


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
    depth: float | None = None
    flip: bool = False


@dataclass
class Hole(Feature):
    target: str = ""
    diameter: float = 1
    depth: float | None = None
    position: tuple[float, float] = (0.0, 0.0)


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
    def __init__(self) -> None:
        self.nodes: dict[str, Any] = {}

    def add(self, node: Any) -> Any:
        if node.id in self.nodes:
            raise GraphError("duplicate_id", f"duplicate id: {node.id}")
        self.nodes[node.id] = node
        return node

    def delete(self, feature: str) -> None:
        if feature not in self.nodes:
            raise GraphError("unknown_id", f"unknown id: {feature}")
        del self.nodes[feature]

    @staticmethod
    def _dependencies(node: Any) -> list[str]:
        dependencies = list(getattr(node, "depends_on", []))
        dependencies.extend(
            getattr(node, field)
            for field in ("sketch", "target", "tool")
            if getattr(node, field, None)
        )
        return dependencies

    @staticmethod
    def _validate_node(node: Any) -> None:
        if not isinstance(node, Sketch):
            return
        entity_ids = [entity.id for entity in node.entities]
        duplicate = next(
            (entity_id for entity_id in entity_ids if entity_ids.count(entity_id) > 1),
            None,
        )
        if duplicate is not None:
            raise GraphError(
                "duplicate_entity_id",
                f"duplicate sketch entity id: {duplicate}",
                sketch=node.id,
                entity=duplicate,
            )

    def _validate_dependencies(self) -> None:
        for node in self.nodes.values():
            for dependency in self._dependencies(node):
                if dependency not in self.nodes:
                    raise GraphError(
                        "unknown_id",
                        f"unknown id: {dependency}",
                        feature=node.id,
                    )

        visiting: set[str] = set()
        done: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise GraphError("cycle", "feature graph contains a cycle", feature=key)
            if key in done:
                return
            visiting.add(key)
            for dependency in self._dependencies(self.nodes[key]):
                visit(dependency)
            visiting.remove(key)
            done.add(key)

        for key in self.nodes:
            visit(key)

    def validate(self) -> None:
        for node in self.nodes.values():
            self._validate_node(node)
        self._validate_dependencies()

    def ordered(self) -> list[Any]:
        self.validate()
        result: list[Any] = []
        seen: set[str] = set()

        def add(key: str) -> None:
            if key in seen:
                return
            node = self.nodes[key]
            for dependency in self._dependencies(node):
                add(dependency)
            seen.add(key)
            result.append(node)

        for key in self.nodes:
            add(key)
        return result

    def to_json(self) -> str:
        from .serialization import dumps

        return dumps(self)
