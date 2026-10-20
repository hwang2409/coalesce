"""Dependency-free reader for the mechanical part of KiCad PCB files."""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Error:
    kind: str
    message: str
    item_ids: list[str] = field(default_factory=list)
    endpoints: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class Segment:
    kind: str
    start: tuple[float, float]
    end: tuple[float, float]
    mid: tuple[float, float] | None = None
    id: str | None = None


@dataclass
class OutlineLoop:
    segments: list[Segment]
    closed: bool
    inner: bool = False

    @property
    def bbox(self) -> dict[str, float]:
        points = [point for segment in self.segments for point in _segment_extrema(segment)]
        return {
            "min_x": min(point[0] for point in points),
            "min_y": min(point[1] for point in points),
            "max_x": max(point[0] for point in points),
            "max_y": max(point[1] for point in points),
        }


@dataclass
class Drill:
    shape: str
    width: float
    height: float
    rotation: float


@dataclass
class Hole:
    position: tuple[float, float]
    drill: Drill
    plated: bool
    footprint_id: str | None


@dataclass
class Model:
    path: str
    offset: tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    rotate: tuple[float, float, float] = (0.0, 0.0, 0.0)
    unresolved_variables: bool = False


@dataclass
class Footprint:
    id: str
    reference: str
    value: str
    library_id: str
    position: tuple[float, float]
    rotation: float
    side: str
    courtyard: list[Segment] | None
    courtyard_availability: str
    models: list[Model]


@dataclass
class Board:
    thickness: float | None
    thickness_source: str
    outline: list[OutlineLoop]
    inner_cutouts: list[OutlineLoop]
    holes: list[Hole]
    footprints: list[Footprint]
    warnings: list[str]
    errors: list[Error] = field(default_factory=list)
    units: str = "mm"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


@dataclass(frozen=True)
class _FootprintTransform:
    position: tuple[float, float]
    rotation: float
    back: bool

    def point(self, point: tuple[float, float]) -> tuple[float, float]:
        x, y = point
        # KiCad flips footprints across their local X axis before board rotation:
        # https://gitlab.com/kicad/code/kicad/-/blob/master/pcbnew/footprint.cpp (FOOTPRINT::Flip).
        if self.back:
            y = -y
        angle = math.radians(self.rotation)
        return (
            self.position[0] + x * math.cos(angle) - y * math.sin(angle),
            self.position[1] + x * math.sin(angle) + y * math.cos(angle),
        )

    def angle(self, local: float) -> float:
        return (self.rotation + (-local if self.back else local)) % 360.0


_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|[^\s()]+|[()]')
_GRAPHICS = {
    "gr_line", "gr_arc", "gr_circle", "gr_rect", "gr_poly",
    "fp_line", "fp_arc", "fp_circle", "fp_rect", "fp_poly",
}
_UNSUPPORTED_GRAPHICS = {"gr_curve", "fp_curve"}
_ENDPOINT_GRID_MM = 1e-6  # KiCad's one-nanometre coordinate grid; applied after transforms.


def _expression(tokens: list[str], index: int) -> tuple[Any, int]:
    if index >= len(tokens):
        raise ValueError("truncated S-expression")
    if tokens[index] != "(":
        if tokens[index] == ")":
            raise ValueError("unexpected closing parenthesis")
        return _atom(tokens[index]), index + 1
    output: list[Any] = []
    index += 1
    while index < len(tokens) and tokens[index] != ")":
        value, index = _expression(tokens, index)
        output.append(value)
    if index >= len(tokens):
        raise ValueError("unterminated S-expression")
    return output, index + 1


def _parse(text: str) -> list[Any]:
    tokens: list[str] = []
    end = 0
    for match in _TOKEN.finditer(text):
        if text[end:match.start()].strip():
            raise ValueError(f"invalid input near offset {end}")
        tokens.append(match.group())
        end = match.end()
    if text[end:].strip() or not tokens:
        raise ValueError("empty or invalid S-expression")

    result, final = _expression(tokens, 0)
    if final != len(tokens):
        raise ValueError("trailing S-expression data")
    if not isinstance(result, list) or not result or result[0] != "kicad_pcb":
        raise ValueError("root must be kicad_pcb")
    return result


def _atom(value: str) -> str:
    if not value.startswith('"'):
        return value
    source = value[1:-1]
    output: list[str] = []
    index = 0
    while index < len(source):
        if source[index] == "\\" and index + 1 < len(source) and source[index + 1] in {'"', "\\"}:
            index += 1
        output.append(source[index])
        index += 1
    return "".join(output)


def _children(node: list[Any], name: str) -> list[list[Any]]:
    return [child for child in node[1:] if isinstance(child, list) and child and child[0] == name]


def _child(node: list[Any], name: str) -> list[Any] | None:
    return next(iter(_children(node, name)), None)


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid number: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"non-finite number: {value!r}")
    return number


def _point(node: list[Any], name: str) -> tuple[float, float] | None:
    value = _child(node, name)
    if value is None:
        return None
    if len(value) < 3:
        raise ValueError(f"{name} requires x and y")
    return _number(value[1]), _number(value[2])


def _uuid(node: list[Any]) -> str | None:
    value = _child(node, "uuid") or _child(node, "tstamp")
    return str(value[1]) if value and len(value) > 1 else None


def _property(footprint: list[Any], key: str, legacy: str) -> str:
    current = next((item for item in _children(footprint, "property") if len(item) > 2 and item[1] == key), None)
    if current:
        return str(current[2])
    old = next((item for item in _children(footprint, "fp_text") if len(item) > 2 and item[1] == legacy), None)
    return str(old[2]) if old else ""


def _model_transform(node: list[Any], name: str, default: tuple[float, float, float]) -> tuple[float, float, float]:
    value = _child(node, name)
    if value is None:
        return default
    parts = value[1:]
    if parts and isinstance(parts[0], list):
        parts = parts[0][1:]
    if len(parts) < 3:
        raise ValueError(f"model {name} requires three values")
    return _number(parts[0]), _number(parts[1]), _number(parts[2])


def _layer_names(node: list[Any]) -> list[str]:
    values: list[str] = []
    for name in ("layer", "layers"):
        for layer in _children(node, name):
            values.extend(str(value) for value in layer[1:] if not isinstance(value, list))
    return values


def _on_layer(node: list[Any], name: str) -> bool:
    return name in _layer_names(node)


def _outline_item(item: list[Any]) -> list[Segment]:
    kind = str(item[0])
    ident = _uuid(item)
    if kind in {"gr_poly", "fp_poly"}:
        pts = _child(item, "pts")
        if pts is None:
            raise ValueError(f"{kind} requires pts")
        points = [(_number(point[1]), _number(point[2])) for point in _children(pts, "xy") if len(point) >= 3]
        if len(points) < 3 or len(points) != len(_children(pts, "xy")):
            raise ValueError(f"{kind}.pts requires at least three xy points")
        return [Segment("line", point, points[(index + 1) % len(points)], id=ident)
                for index, point in enumerate(points)]
    if kind in {"gr_rect", "fp_rect"}:
        start, end = _point(item, "start"), _point(item, "end")
        if start is None or end is None:
            raise ValueError(f"{kind} requires start and end")
        points = [start, (end[0], start[1]), end, (start[0], end[1])]
        return [Segment("line", points[index], points[(index + 1) % 4], id=ident) for index in range(4)]
    if kind in {"gr_line", "fp_line"}:
        start, end = _point(item, "start"), _point(item, "end")
        if start is None or end is None:
            raise ValueError(f"{kind} requires start and end")
        return [Segment("line", start, end, id=ident)]
    if kind in {"gr_arc", "fp_arc"}:
        start, mid, end = _point(item, "start"), _point(item, "mid"), _point(item, "end")
        if start is None or mid is None or end is None:
            raise ValueError(f"{kind} requires start, mid, and end")
        segment = Segment("arc", start, end, mid, ident)
        _arc_geometry(segment)
        return [segment]
    if kind in {"gr_circle", "fp_circle"}:
        center, end = _point(item, "center"), _point(item, "end")
        if center is None or end is None:
            raise ValueError(f"{kind} requires center and end")
        return [Segment("circle", center, end, id=ident)]
    return []


def _transform_segment(segment: Segment, transform: _FootprintTransform) -> Segment:
    return Segment(
        segment.kind,
        transform.point(segment.start),
        transform.point(segment.end),
        transform.point(segment.mid) if segment.mid else None,
        segment.id,
    )


def _arc_geometry(segment: Segment) -> tuple[tuple[float, float], float, float, float, bool]:
    if segment.mid is None:
        raise ValueError("arc requires a midpoint")
    (x1, y1), (x2, y2), (x3, y3) = segment.start, segment.mid, segment.end
    determinant = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if determinant == 0:
        raise ValueError("arc points are collinear")
    q1, q2, q3 = x1 * x1 + y1 * y1, x2 * x2 + y2 * y2, x3 * x3 + y3 * y3
    center = (
        (q1 * (y2 - y3) + q2 * (y3 - y1) + q3 * (y1 - y2)) / determinant,
        (q1 * (x3 - x2) + q2 * (x1 - x3) + q3 * (x2 - x1)) / determinant,
    )
    angles = [math.atan2(y - center[1], x - center[0]) for x, y in (segment.start, segment.mid, segment.end)]
    ccw = (angles[1] - angles[0]) % math.tau <= (angles[2] - angles[0]) % math.tau
    return center, math.hypot(x1 - center[0], y1 - center[1]), angles[0], angles[2], ccw


def _angle_on_arc(angle: float, start: float, end: float, ccw: bool) -> bool:
    if ccw:
        return (angle - start) % math.tau <= (end - start) % math.tau
    return (start - angle) % math.tau <= (start - end) % math.tau


def _segment_extrema(segment: Segment) -> list[tuple[float, float]]:
    if segment.kind == "circle":
        radius = math.dist(segment.start, segment.end)
        x, y = segment.start
        return [(x - radius, y), (x + radius, y), (x, y - radius), (x, y + radius)]
    if segment.kind != "arc":
        return [segment.start, segment.end]
    center, radius, start, end, ccw = _arc_geometry(segment)
    points = [segment.start, segment.end]
    for angle in (0.0, math.pi / 2, math.pi, 3 * math.pi / 2):
        if _angle_on_arc(angle, start, end, ccw):
            points.append((center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle)))
    return points


def _endpoint_key(point: tuple[float, float]) -> tuple[int, int]:
    return round(point[0] / _ENDPOINT_GRID_MM), round(point[1] / _ENDPOINT_GRID_MM)


def _chain(segments: list[Segment]) -> list[OutlineLoop]:
    """Chain on KiCad's 1 nm grid so transform roundoff cannot split an endpoint."""
    loops: list[OutlineLoop] = []
    remaining = segments[:]
    while remaining:
        first = remaining.pop(0)
        if first.kind == "circle":
            loops.append(OutlineLoop([first], True))
            continue
        chain = [first]
        while remaining and _endpoint_key(chain[-1].end) != _endpoint_key(first.start):
            end_key = _endpoint_key(chain[-1].end)
            match = next((item for item in remaining
                          if _endpoint_key(item.start) == end_key or _endpoint_key(item.end) == end_key), None)
            if match is None:
                break
            remaining.remove(match)
            if _endpoint_key(match.end) == end_key:
                match = Segment(match.kind, match.end, match.start, match.mid, match.id)
            chain.append(match)
        loops.append(OutlineLoop(chain, _endpoint_key(chain[-1].end) == _endpoint_key(first.start)))
    return loops


def _line_winding(segment: Segment, point: tuple[float, float]) -> float:
    y1, y2 = segment.start[1], segment.end[1]
    if y1 == y2 or not min(y1, y2) <= point[1] <= max(y1, y2):
        return 0.0
    ratio = (point[1] - y1) / (y2 - y1)
    crossing_x = segment.start[0] + ratio * (segment.end[0] - segment.start[0])
    if crossing_x <= point[0]:
        return 0.0
    weight = 0.5 if ratio in {0.0, 1.0} else 1.0
    return math.copysign(weight, y2 - y1)


def _arc_winding(segment: Segment, point: tuple[float, float]) -> float:
    center, radius, start, end, ccw = _arc_geometry(segment)
    relative_y = (point[1] - center[1]) / radius
    if not -1.0 <= relative_y <= 1.0:
        return 0.0
    first = math.asin(relative_y)
    candidates = (first, math.pi - first)
    winding = 0.0
    direction = 1.0 if ccw else -1.0
    for angle in candidates:
        angle %= math.tau
        if not _angle_on_arc(angle, start, end, ccw):
            continue
        crossing_x = center[0] + radius * math.cos(angle)
        derivative = direction * radius * math.cos(angle)
        if crossing_x <= point[0] or math.isclose(derivative, 0.0, abs_tol=1e-12):
            continue
        endpoint = math.isclose((angle - start) % math.tau, 0.0, abs_tol=1e-12) or math.isclose(
            (angle - end) % math.tau, 0.0, abs_tol=1e-12
        )
        winding += math.copysign(0.5 if endpoint else 1.0, derivative)
    return winding


def _circle_winding(segment: Segment, point: tuple[float, float]) -> float:
    center, edge = segment.start, segment.end
    radius = math.dist(center, edge)
    relative_y = (point[1] - center[1]) / radius
    if not -1.0 < relative_y < 1.0:
        return 0.0
    crossing_x = center[0] + radius * math.sqrt(1.0 - relative_y * relative_y)
    return 1.0 if crossing_x > point[0] else 0.0


def _contains(loop: OutlineLoop, point: tuple[float, float]) -> bool:
    winding = 0.0
    for segment in loop.segments:
        if segment.kind == "arc":
            winding += _arc_winding(segment, point)
        elif segment.kind == "circle":
            winding += _circle_winding(segment, point)
        else:
            winding += _line_winding(segment, point)
    return abs(winding) > 0.5


def _classify(loops: list[OutlineLoop]) -> tuple[list[OutlineLoop], list[OutlineLoop]]:
    closed = [loop for loop in loops if loop.closed]
    outer: list[OutlineLoop] = []
    inner: list[OutlineLoop] = []
    for loop in closed:
        point = loop.segments[0].start
        depth = sum(_contains(other, point) for other in closed if other is not loop)
        classified = OutlineLoop(loop.segments, True, depth % 2 == 1)
        (inner if classified.inner else outer).append(classified)
    return outer, inner


def _thickness(root: list[Any]) -> tuple[float | None, str]:
    general = _child(root, "general")
    value = _child(general, "thickness") if general else None
    if value:
        if len(value) < 2:
            raise ValueError("general thickness requires a value")
        return _number(value[1]), "general.thickness"
    setup = _child(root, "setup")
    stackup = _child(setup, "stackup") if setup else None
    value = _child(stackup, "thickness") if stackup else None
    if value:
        return _number(value[1]), "setup.stackup.thickness"
    if stackup:
        dielectric = next((layer for layer in _children(stackup, "layer") if len(layer) > 1 and "dielectric" in str(layer[1]).lower()), None)
        value = _child(dielectric, "thickness") if dielectric else None
        if value:
            return _number(value[1]), "setup.stackup.dielectric"
    return None, "unavailable"


def _position(node: list[Any], path: str) -> tuple[tuple[float, float], float]:
    at = _child(node, "at")
    if at is None:
        return (0.0, 0.0), 0.0
    if len(at) < 3:
        raise ValueError(f"{path}: requires x and y")
    try:
        return (_number(at[1]), _number(at[2])), _number(at[3]) if len(at) > 3 else 0.0
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def _footprint_transform(node: list[Any], path: str) -> _FootprintTransform:
    position, rotation = _position(node, f"{path}.at")
    layer = _child(node, "layer")
    back = bool(layer and len(layer) > 1 and str(layer[1]).startswith("B."))
    return _FootprintTransform(position, rotation, back)


def _graphics(node: list[Any], layer: str, transform: _FootprintTransform | None = None) -> list[Segment]:
    output: list[Segment] = []
    for item in node[1:]:
        if not isinstance(item, list) or not item or item[0] not in _GRAPHICS or not _on_layer(item, layer):
            continue
        segments = _outline_item(item)
        output.extend(_transform_segment(segment, transform) for segment in segments) if transform else output.extend(segments)
    return output


def _courtyard(node: list[Any], transform: _FootprintTransform) -> list[Segment]:
    output: list[Segment] = []
    for item in node[1:]:
        if not isinstance(item, list) or not item or item[0] not in _GRAPHICS:
            continue
        if not any(layer.endswith("CrtYd") for layer in _layer_names(item)):
            continue
        output.extend(_transform_segment(segment, transform) for segment in _outline_item(item))
    return output


def _models(node: list[Any]) -> list[Model]:
    models: list[Model] = []
    for item in _children(node, "model"):
        if len(item) < 2:
            raise ValueError("model requires a path")
        path = str(item[1])
        models.append(Model(path, _model_transform(item, "offset", (0.0, 0.0, 0.0)),
                            _model_transform(item, "scale", (1.0, 1.0, 1.0)),
                            _model_transform(item, "rotate", (0.0, 0.0, 0.0)), "${" in path))
    return models


def _drill(node: list[Any], transform: _FootprintTransform) -> Drill:
    values = [value for value in node[1:] if not isinstance(value, list)]
    shape = "oval" if values and values[0] == "oval" else "circle"
    dimensions = values[1:] if shape == "oval" else values
    required = 2 if shape == "oval" else 1
    if len(dimensions) < required:
        raise ValueError(f"{shape} drill requires {required} dimension(s)")
    width = _number(dimensions[0])
    height = _number(dimensions[1]) if shape == "oval" else width
    return Drill(shape, width, height, transform.angle(0.0))


def _holes(node: list[Any], footprint_id: str, transform: _FootprintTransform, path: str) -> list[Hole]:
    holes: list[Hole] = []
    for index, pad in enumerate(_children(node, "pad")):
        drill_node = _child(pad, "drill")
        if drill_node is None:
            continue
        local, local_angle = _position(pad, f"{path}.pad[{index}].at")
        drill = _drill(drill_node, transform)
        drill.rotation = transform.angle(local_angle)
        offset = _point(drill_node, "offset") or (0.0, 0.0)
        angle = math.radians(local_angle)
        rotated_offset = (offset[0] * math.cos(angle) - offset[1] * math.sin(angle),
                          offset[0] * math.sin(angle) + offset[1] * math.cos(angle))
        hole_position = (local[0] + rotated_offset[0], local[1] + rotated_offset[1])
        pad_type = str(pad[2]) if len(pad) > 2 else ""
        holes.append(Hole(transform.point(hole_position), drill, pad_type == "thru_hole", footprint_id))
    return holes


def _extract_footprints(root: list[Any]) -> tuple[list[Footprint], list[Hole], list[Segment]]:
    footprints: list[Footprint] = []
    holes: list[Hole] = []
    edges: list[Segment] = []
    for index, node in enumerate(_children(root, "footprint")):
        path = f"footprint[{index}]"
        if len(node) < 2:
            raise ValueError(f"{path}: requires a library id")
        transform = _footprint_transform(node, path)
        footprint_id = _uuid(node) or f"footprint-{len(footprints)}"
        courtyard = _courtyard(node, transform)
        footprints.append(Footprint(
            footprint_id, _property(node, "Reference", "reference"), _property(node, "Value", "value"),
            str(node[1]), transform.position, transform.rotation, "B" if transform.back else "F",
            courtyard or None, "present" if courtyard else "missing", _models(node),
        ))
        holes.extend(_holes(node, footprint_id, transform, path))
        edges.extend(_graphics(node, "Edge.Cuts", transform))
    return footprints, holes, edges


def _open_errors(loops: list[OutlineLoop]) -> tuple[list[str], list[Error]]:
    open_loops = [loop for loop in loops if not loop.closed]
    warnings = ["open Edge.Cuts loop" for _ in open_loops]
    errors = [Error("open_outline", "Edge.Cuts loop is open", [segment.id for segment in loop.segments if segment.id],
                    [loop.segments[-1].end, loop.segments[0].start]) for loop in open_loops]
    return warnings, errors


def _unsupported_geometry(root: list[Any]) -> list[Error]:
    errors: list[Error] = []
    scopes = [(root, "board")]
    scopes.extend((footprint, f"footprint[{index}]") for index, footprint in enumerate(_children(root, "footprint")))
    for node, path in scopes:
        for index, item in enumerate(node[1:]):
            if (not isinstance(item, list) or not item or item[0] not in _UNSUPPORTED_GRAPHICS
                    or not _on_layer(item, "Edge.Cuts")):
                continue
            item_id = _uuid(item) or f"{path}.{item[0]}[{index}]"
            errors.append(Error("unsupported_geometry", f"unsupported Edge.Cuts geometry: {item[0]}", [item_id]))
    return errors


def _parse_board(root: list[Any]) -> Board:
    thickness, thickness_source = _thickness(root)
    footprints, holes, footprint_edges = _extract_footprints(root)
    loops = _chain(_graphics(root, "Edge.Cuts") + footprint_edges)
    outline, inner = _classify(loops)
    warnings, errors = _open_errors(loops)
    errors.extend(_unsupported_geometry(root))
    if not outline and not errors:
        errors.append(Error("missing_outline", "no recognized closed Edge.Cuts outline"))
    return Board(thickness, thickness_source, outline, inner, holes, footprints, warnings, errors)


def parse_kicad_pcb(path: str | Path) -> Board:
    """Parse board mechanics and return structured errors for all invalid input."""
    try:
        return _parse_board(_parse(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, IndexError, RecursionError) as exc:
        message = str(exc) or exc.__class__.__name__
        return Board(None, "unavailable", [], [], [], [], [message], [Error("parse", message)])
