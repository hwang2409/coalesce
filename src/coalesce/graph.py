from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from math import isfinite
from typing import Any

from .references import (
    BodySource,
    ElementKey,
    HoleSurfaceSource,
    Reference,
    SketchSegmentSource,
    ToolCapSource,
)


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


MIN_OUTLINE_POINTS = 3
MAX_OUTLINE_POINTS = 2000


@dataclass
class Polyline:
    id: str
    points: tuple[tuple[float, float], ...]
    closed: bool = True


@dataclass
class Spline:
    id: str
    points: tuple[tuple[float, float], ...]
    closed: bool = True


PrimitiveEntity = Rectangle | Circle | Polyline | Spline


@dataclass
class PrimitiveSketchState:
    entities: list[PrimitiveEntity] = field(default_factory=list)


@dataclass
class ConstrainedSketchState:
    constrained: Any


@dataclass(init=False)
class Sketch:
    id: str
    plane: Plane
    state: PrimitiveSketchState | ConstrainedSketchState
    position: tuple[float, float, float]
    rotation: tuple[float, float, float]

    def __init__(
        self,
        id: str,
        plane: Plane | None = None,
        entities: list[PrimitiveEntity] | None = None,
        constrained: Any | None = None,
        position: tuple[float, float, float] = (0.0, 0.0, 0.0),
        rotation: tuple[float, float, float] = (0.0, 0.0, 0.0),
        state: PrimitiveSketchState | ConstrainedSketchState | None = None,
    ):
        supplied_variants = sum((bool(entities), constrained is not None, state is not None))
        if supplied_variants > 1:
            raise ValueError("a sketch must have exactly one state variant")
        self.id = id
        self.plane = plane or Plane()
        self.state = state or (
            ConstrainedSketchState(constrained)
            if constrained is not None
            else PrimitiveSketchState(entities or [])
        )
        self.position = position
        self.rotation = rotation

    @property
    def entities(self) -> list[PrimitiveEntity]:
        return self.state.entities if isinstance(self.state, PrimitiveSketchState) else []

    @property
    def constrained(self) -> Any | None:
        return self.state.constrained if isinstance(self.state, ConstrainedSketchState) else None

    def replace_entities(self, entities: list[PrimitiveEntity]) -> None:
        self.state = PrimitiveSketchState(entities)

    def replace_constrained(self, constrained: Any) -> None:
        self.state = ConstrainedSketchState(constrained)


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
class PcbBase(Feature):
    board: str = ""
    wall_thickness: float = 2
    floor_thickness: float = 2
    clearance: float = 1
    wall_height_above_board: float = 4
    board_seat_height: float = 1


@dataclass
class Standoffs(Feature):
    target: str = ""
    board: str = ""
    selection: str = "explicit"
    hole_ids: tuple[str, ...] = ()
    boss_diameter: float = 5
    pilot_diameter: float = 2


@dataclass
class PcbLid(Feature):
    base: str = ""
    thickness: float = 2
    fit_gap: float = 0.25
    lip_depth: float | None = None
    lip_width: float | None = None
    outline_source: str | None = None


@dataclass
class PortCutout(Feature):
    base: str = ""
    footprint: str = ""
    footprint_id: str | None = None
    wall: str = "auto"
    clearance: float = 0.5
    envelope_source: str = "courtyard"
    envelope_width: float | None = None
    envelope_height: float = 1
    bottom_offset: float = 0


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


@dataclass
class Chamfer(Feature):
    target: str = ""
    edges: list[Reference] = field(default_factory=list)
    distance: float = 1


class GraphError(ValueError):
    def __init__(self, code: str, message: str, **details: Any):
        self.error = {"code": code, "message": message, **details}
        super().__init__(message)


def _require_finite(sketch: Sketch, field: str, values: tuple[float, ...]) -> None:
    if not all(isinstance(value, (int, float)) and isfinite(value) for value in values):
        raise GraphError(
            "NON_FINITE_VALUE",
            f"sketch {sketch.id} {field} values must be finite",
            sketch=sketch.id,
            field=field,
        )


def _outline_tolerance(points: tuple[tuple[float, float], ...]) -> float:
    extent = max(
        max(point[axis] for point in points) - min(point[axis] for point in points)
        for axis in (0, 1)
    )
    return max(extent * 1e-9, 1e-12)


def _cross(
    start: tuple[float, float], end: tuple[float, float], point: tuple[float, float]
) -> float:
    return (end[0] - start[0]) * (point[1] - start[1]) - (end[1] - start[1]) * (point[0] - start[0])


def _segments_intersect(
    left: tuple[tuple[float, float], tuple[float, float]],
    right: tuple[tuple[float, float], tuple[float, float]],
    tolerance: float,
) -> bool:
    a, b = left
    c, d = right
    products = (_cross(a, b, c) * _cross(a, b, d), _cross(c, d, a) * _cross(c, d, b))
    if products[0] > tolerance**2 or products[1] > tolerance**2:
        return False
    return not (
        max(a[0], b[0]) < min(c[0], d[0]) - tolerance
        or max(c[0], d[0]) < min(a[0], b[0]) - tolerance
        or max(a[1], b[1]) < min(c[1], d[1]) - tolerance
        or max(c[1], d[1]) < min(a[1], b[1]) - tolerance
    )


def _unsupported_outline(sketch: Sketch, entity: PrimitiveEntity, reason: str) -> None:
    raise GraphError(
        "SKETCH_PROFILE_UNSUPPORTED",
        f"sketch {sketch.id} entity {entity.id} has an unsupported {reason.replace('_', ' ')} profile",
        sketch=sketch.id,
        entity=entity.id,
        reason=reason,
    )


def _validate_outline(sketch: Sketch, entity: Polyline | Spline) -> None:
    points = entity.points
    tolerance = _outline_tolerance(points)
    if any(
        (left[0] - right[0]) ** 2 + (left[1] - right[1]) ** 2 <= tolerance**2
        for left, right in zip(points, (*points[1:], points[0]), strict=True)
    ):
        _unsupported_outline(sketch, entity, "degenerate")
    segments = list(zip(points, (*points[1:], points[0]), strict=True))
    count = len(segments)
    if any(
        _segments_intersect(segments[left], segments[right], tolerance)
        for left in range(count)
        for right in range(left + 1, count)
        if right not in {left + 1, count - 1 if left == 0 else -1}
    ):
        _unsupported_outline(sketch, entity, "self_intersecting")
    area_twice = sum(left[0] * right[1] - right[0] * left[1] for left, right in segments)
    if abs(area_twice) <= tolerance**2:
        _unsupported_outline(sketch, entity, "degenerate")


def validate_sketch(sketch: Sketch) -> None:
    if sketch.plane.name not in {"XY", "XZ", "YZ"}:
        raise GraphError(
            "UNKNOWN_PLANE",
            f"unknown origin plane: {sketch.plane.name}",
            sketch=sketch.id,
            plane=sketch.plane.name,
            allowed=["XY", "XZ", "YZ"],
        )
    _require_finite(sketch, "plane.offset", (sketch.plane.offset,))
    _require_finite(sketch, "position", sketch.position)
    _require_finite(sketch, "rotation", sketch.rotation)
    for entity in sketch.entities:
        if isinstance(entity, (Polyline, Spline)):
            _require_finite(
                sketch,
                f"entity.{entity.id}.points",
                tuple(coordinate for point in entity.points for coordinate in point),
            )
            _validate_outline(sketch, entity)
            continue
        _require_finite(sketch, f"entity.{entity.id}.position", entity.position)
        _require_finite(sketch, f"entity.{entity.id}.rotation", (entity.rotation,))
        if isinstance(entity, Circle):
            _require_finite(sketch, f"entity.{entity.id}.radius", (entity.radius,))
            if entity.radius <= 0:
                raise GraphError(
                    "INVALID_CIRCLE_RADIUS",
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
            raise GraphError("DUPLICATE_ID", f"duplicate id: {node.id}")
        self.nodes[node.id] = node
        return node

    def delete(self, feature: str) -> None:
        if feature not in self.nodes:
            raise GraphError("UNKNOWN_ID", f"unknown id: {feature}")
        del self.nodes[feature]

    def produces_body(self, feature: str) -> bool:
        return isinstance(self.nodes.get(feature), Feature)

    @staticmethod
    def _typed_reference_dependencies(value: Any):
        if isinstance(value, (Reference, ElementKey, BodySource)):
            yield value.feature
        elif isinstance(value, SketchSegmentSource):
            yield value.sketch
        elif isinstance(value, ToolCapSource):
            yield value.tool
        elif isinstance(value, HoleSurfaceSource):
            yield value.feature

        if is_dataclass(value):
            for item in fields(value):
                yield from Graph._typed_reference_dependencies(getattr(value, item.name))
        elif isinstance(value, dict):
            for item in value.values():
                yield from Graph._typed_reference_dependencies(item)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                yield from Graph._typed_reference_dependencies(item)

    @staticmethod
    def _dependencies(node: Any) -> list[str]:
        dependencies = list(getattr(node, "depends_on", []))
        for dependency in (
            *(
                getattr(node, dependency_field, None)
                for dependency_field in (
                    "sketch",
                    "target",
                    "tool",
                    "board",
                    "base",
                    "outline_source",
                )
            ),
            *Graph._typed_reference_dependencies(node),
        ):
            if dependency and dependency not in dependencies:
                dependencies.append(dependency)
        return dependencies

    def transitive_dependents(
        self, target: str, lineage_predecessors: Mapping[str, str]
    ) -> list[Any]:
        ordered = self.ordered()
        dependencies = {node.id: set(self._dependencies(node)) for node in ordered}
        for successor, predecessor in lineage_predecessors.items():
            dependencies[successor].add(predecessor)

        selected = {target}
        while additions := {
            node.id
            for node in ordered
            if node.id not in selected and dependencies[node.id] & selected
        }:
            selected.update(additions)
        return [node for node in ordered if node.id in selected and node.id != target]

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
                "DUPLICATE_ENTITY_ID",
                f"duplicate sketch entity id: {duplicate}",
                sketch=node.id,
                entity=duplicate,
            )

    @staticmethod
    def _body_target(node: Any) -> str | None:
        if isinstance(node, (Cut, Hole, Fillet, Chamfer)):
            return node.target
        if isinstance(node, Extrude) and node.operation == "join":
            return node.target
        return None

    def _validate_dependencies(self) -> None:
        for node in self.nodes.values():
            for dependency in self._dependencies(node):
                if dependency not in self.nodes:
                    raise GraphError(
                        "UNKNOWN_ID",
                        f"unknown id: {dependency}",
                        feature=node.id,
                    )

        visiting: set[str] = set()
        done: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise GraphError("CYCLE", "feature graph contains a cycle", feature=key)
            if key in done:
                return
            visiting.add(key)
            for dependency in self._dependencies(self.nodes[key]):
                visit(dependency)
            visiting.remove(key)
            done.add(key)

        for key in self.nodes:
            visit(key)

    def _validate_body_targets(self) -> None:
        consumers: dict[str, str] = {}
        conflict: tuple[Any, str, str] | None = None
        for node in self.nodes.values():
            target = self._body_target(node)
            if target is None:
                continue
            if target in consumers:
                conflict = conflict or (node, target, consumers[target])
            else:
                consumers[target] = node.id

        if conflict is None:
            return
        node, target, consumer = conflict
        terminal = consumer
        while terminal in consumers:
            terminal = consumers[terminal]
        raise GraphError(
            "TARGET_ALREADY_CONSUMED",
            f"target {target} is already consumed by {consumer}",
            feature=node.id,
            target=target,
            consumer=consumer,
            terminalFeatureId=terminal,
            hint=f"target {terminal} to apply this after {consumer}",
        )

    def validate(self) -> None:
        for node in self.nodes.values():
            self._validate_node(node)
        self._validate_dependencies()
        self._validate_body_targets()

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
