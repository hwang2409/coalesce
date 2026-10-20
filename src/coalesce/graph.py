from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any

from .references import Reference


@dataclass
class Parameter:
    name: str
    value: float


@dataclass
class Plane:
    """An origin plane matching build123d's frames.

    XY uses local x=+X, y=+Y, normal=+Z. XZ uses local x=+X, y=+Z,
    normal=-Y. YZ uses local x=+Y, y=+Z, normal=+X. ``offset`` moves
    the plane origin along its normal.
    """

    name: str = "XY"
    offset: float = 0.0


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
class BoardImport:
    id: str
    source_path: str
    source_digest: str
    parser_version: str
    mechanics: dict[str, Any]
    availability: dict[str, str]
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Feature:
    id: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Extrude(Feature):
    sketch: str = ""
    height: float = 1
    operation: str = "new_body"
    target: str | None = None


@dataclass
class Cut(Feature):
    target: str = ""
    tool: str = ""
    depth: float | None = None
    flip: bool = False


@dataclass(frozen=True)
class HolePosition:
    name: str | None
    point: tuple[float, float]


@dataclass
class Hole(Feature):
    target: str = ""
    diameter: float = 1
    depth: float | None = None
    position: tuple[float, float] = (0.0, 0.0)
    positions: tuple[HolePosition, ...] = ()


@dataclass
class Fillet(Feature):
    target: str = ""
    edges: list[Reference] = field(default_factory=list)
    radius: float = 1


class GraphError(ValueError):
    def __init__(self, code: str, message: str, **details: Any):
        self.error = {"code": code, "message": message, **details}
        super().__init__(message)


def _require_finite(sketch: Sketch, field: str, values: tuple[float, ...]) -> None:
    if not all(isinstance(value, (int, float)) and isfinite(value) for value in values):
        raise GraphError(
            "non_finite_value",
            f"sketch {sketch.id} {field} values must be finite",
            sketch=sketch.id,
            field=field,
        )


def validate_sketch(sketch: Sketch) -> None:
    if sketch.plane.name not in {"XY", "XZ", "YZ"}:
        raise GraphError(
            "unknown_plane",
            f"unknown origin plane: {sketch.plane.name}",
            sketch=sketch.id,
            plane=sketch.plane.name,
            allowed=["XY", "XZ", "YZ"],
        )
    _require_finite(sketch, "plane.offset", (sketch.plane.offset,))
    _require_finite(sketch, "position", sketch.position)
    _require_finite(sketch, "rotation", sketch.rotation)
    for entity in sketch.entities:
        _require_finite(sketch, f"entity.{entity.id}.position", entity.position)
        _require_finite(sketch, f"entity.{entity.id}.rotation", (entity.rotation,))
        if isinstance(entity, Circle):
            _require_finite(sketch, f"entity.{entity.id}.radius", (entity.radius,))
            if entity.radius <= 0:
                raise GraphError(
                    "invalid_circle_radius",
                    f"circle {entity.id} radius must be positive",
                    sketch=sketch.id,
                    entity=entity.id,
                    radius=entity.radius,
                )


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

    def produces_body(self, feature: str) -> bool:
        return isinstance(self.nodes.get(feature), Feature)

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
        validate_sketch(node)
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
