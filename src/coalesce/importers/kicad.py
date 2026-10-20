"""Small, dependency-free reader for the mechanical part of KiCad PCB files."""
from __future__ import annotations

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
        points = [p for s in self.segments for p in (s.start, s.end, s.mid) if p]
        return {"min_x": min(p[0] for p in points), "min_y": min(p[1] for p in points),
                "max_x": max(p[0] for p in points), "max_y": max(p[1] for p in points)}


@dataclass
class Hole:
    position: tuple[float, float]
    diameter: float
    plated: bool
    footprint_id: str | None
    kind: str = "drill"


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
        import json
        return json.dumps(self.to_dict(), sort_keys=True)


_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|[^\s()]+|[()]')


def _parse(text: str) -> list[Any]:
    tokens = _TOKEN.findall(text)
    def expr(i: int) -> tuple[Any, int]:
        if tokens[i] != "(": return _atom(tokens[i]), i + 1
        out: list[Any] = []; i += 1
        while i < len(tokens) and tokens[i] != ")":
            value, i = expr(i); out.append(value)
        if i == len(tokens): raise ValueError("unterminated S-expression")
        return out, i + 1
    result, end = expr(0)
    if end != len(tokens): raise ValueError("trailing S-expression data")
    return result


def _atom(value: str) -> str:
    if value.startswith('"'):
        return bytes(value[1:-1], "utf8").decode("unicode_escape")
    return value


def _children(node: list[Any], name: str) -> list[list[Any]]:
    return [x for x in node[1:] if isinstance(x, list) and x and x[0] == name]


def _child(node: list[Any], name: str) -> list[Any] | None:
    values = _children(node, name)
    return values[0] if values else None


def _num(value: Any) -> float:
    return float(value)


def _point(node: list[Any], name: str) -> tuple[float, float] | None:
    x = _child(node, name)
    return (_num(x[1]), _num(x[2])) if x else None


def _uuid(node: list[Any]) -> str | None:
    x = _child(node, "uuid") or _child(node, "tstamp")
    return str(x[1]) if x else None


def _property(fp: list[Any], key: str, legacy: str = "") -> str:
    p = next((x for x in _children(fp, "property") if len(x) > 2 and x[1] == key), None)
    if p: return str(p[2])
    for x in _children(fp, "fp_text"):
        if len(x) > 2 and x[1] == legacy:
            return str(x[2])
    return ""


def _transform(node: list[Any], name: str, default: tuple[float, float, float]) -> tuple[float, float, float]:
    x = _child(node, name)
    if not x:
        return default
    values = x[1:]
    if values and isinstance(values[0], list):
        values = values[0][1:]
    return tuple(_num(v) for v in values[:3])  # type: ignore[return-value]


def _outline_item(item: list[Any]) -> Segment | None:
    kind = str(item[0]); ident = _uuid(item)
    if kind in ("gr_line", "fp_line"):
        a, b = _point(item, "start"), _point(item, "end")
        return Segment("line", a, b, id=ident) if a and b else None
    if kind in ("gr_arc", "fp_arc"):
        a, b, m = _point(item, "start"), _point(item, "end"), _point(item, "mid")
        # KiCad 6 uses start/mid/end; old files may use center/angle.
        return Segment("arc", a, b, m, ident) if a and b else None
    if kind in ("gr_circle", "fp_circle"):
        c, e = _point(item, "center"), _point(item, "end")
        return Segment("circle", c, e, id=ident) if c and e else None
    return None


def _chain(items: list[Segment], warnings: list[str]) -> list[OutlineLoop]:
    loops: list[OutlineLoop] = []; remaining = items[:]; tol = 1e-5
    while remaining:
        first = remaining.pop(0); chain = [first]; end = first.end
        while remaining and any(abs(end[i] - first.start[i]) > tol for i in (0, 1)):
            found = next((s for s in remaining if sum((s.start[i] - end[i]) ** 2 for i in (0, 1)) <= tol ** 2), None)
            reverse = False
            if not found:
                found = next((s for s in remaining if sum((s.end[i] - end[i]) ** 2 for i in (0, 1)) <= tol ** 2), None)
                reverse = found is not None
            if not found: break
            remaining.remove(found)
            if reverse: found = Segment(found.kind, found.end, found.start, found.mid, found.id)
            chain.append(found); end = found.end
        closed = sum((end[i] - first.start[i]) ** 2 for i in (0, 1)) <= tol ** 2
        if not closed:
            warnings.append("open Edge.Cuts loop")
        loops.append(OutlineLoop(chain, closed))
    return loops


def parse_kicad_pcb(path: str | Path) -> Board:
    """Parse board mechanics; unsupported drawing primitives are warnings, not guesses."""
    warnings: list[str] = []
    try: root = _parse(Path(path).read_text(encoding="utf8"))
    except (OSError, ValueError) as exc:
        return Board(None, "unavailable", [], [], [], [], [str(exc)], [Error("parse", str(exc))])
    setup = _child(root, "setup"); thickness = None; source = "unavailable"
    if setup:
        stack = _child(setup, "stackup")
        if stack:
            layer = next((x for x in _children(stack, "layer") if len(x) > 2 and "dielectric" in str(x[1]).lower()), None)
            thickness_node = _child(stack, "thickness")
            if thickness_node: thickness, source = _num(thickness_node[1]), "setup.stackup.thickness"
            elif layer and _child(layer, "thickness"): thickness, source = _num(_child(layer, "thickness")[1]), "setup.stackup.dielectric"
    general = _child(root, "general")
    if general and _child(general, "thickness"):
        thickness, source = _num(_child(general, "thickness")[1]), "general.thickness"
    edge_items = [_outline_item(x) for x in root[1:] if isinstance(x, list) and x and x[0] in ("gr_line", "gr_arc", "gr_circle") and _child(x, "layer") and _child(x, "layer")[1] == "Edge.Cuts"]
    outline = _chain([x for x in edge_items if x], warnings)
    holes: list[Hole] = []; footprints: list[Footprint] = []
    for fp in _children(root, "footprint"):
        fid = _uuid(fp) or f"footprint-{len(footprints)}"; at = _child(fp, "at")
        pos = (_num(at[1]), _num(at[2])) if at and len(at) > 2 else (0.0, 0.0)
        rotation = _num(at[3]) if at and len(at) > 3 else 0.0
        layer = _child(fp, "layer"); side = "B" if layer and str(layer[1]).startswith("B.") else "F"
        courtyard = [_outline_item(x) for x in fp[1:] if isinstance(x, list) and x and x[0] in ("fp_line", "fp_arc", "fp_circle") and _child(x, "layer") and str(_child(x, "layer")[1]).endswith("CrtYd")]
        courtyard = [x for x in courtyard if x]
        models = []
        for m in _children(fp, "model"):
            p = str(m[1]); models.append(Model(p, _transform(m, "offset", (0, 0, 0)), _transform(m, "scale", (1, 1, 1)), _transform(m, "rotate", (0, 0, 0)), "${" in p))
        footprints.append(Footprint(fid, _property(fp, "Reference", "fp_text"), _property(fp, "Value"), str(fp[1]), pos, rotation, side, courtyard or None, "present" if courtyard else "missing", models))
        for pad in _children(fp, "pad"):
            drill = _child(pad, "drill")
            if not drill: continue
            numbers = [v for v in drill[1:] if not isinstance(v, list)]
            try:
                d = _num(next(v for v in numbers if re.fullmatch(r"[-+]?\d*\.?\d+", str(v))))
            except StopIteration:
                warnings.append(f"unsupported drill in footprint {fid}")
                continue
            local = _child(pad, "at")
            lx, ly = (_num(local[1]), _num(local[2])) if local and len(local) > 2 else (0.0, 0.0)
            angle = math.radians(rotation)
            p = (pos[0] + lx * math.cos(angle) - ly * math.sin(angle), pos[1] + lx * math.sin(angle) + ly * math.cos(angle))
            shape = str(pad[2]) if len(pad) > 2 else ""
            holes.append(Hole(p, d, "np_thru_hole" not in pad, fid, shape))
    outer = [x for x in outline if x.closed]; inner: list[OutlineLoop] = []
    errors = [Error("open_outline", "Edge.Cuts loop is open", [s.id for s in x.segments if s.id], [x.segments[0].start, x.segments[-1].end]) for x in outline if not x.closed]
    return Board(thickness, source, outer, inner, holes, footprints, warnings, errors)
