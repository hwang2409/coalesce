"""Committed geometry for the live viewer: tessellated named parts per revision."""

from __future__ import annotations

import copy
import functools
import hashlib
import itertools
import math
import re
import sys
import threading
from array import array
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from build123d import Shape

    from coalesce.assembly import RigidTransform
    from coalesce.references import BodySource

# restrained engineering-review colors for manufactured parts, assigned in part order when no color
# is given; no greens, so a part never looks like the board.
PALETTE = ("#8c9bab", "#a9a08e", "#a08c9b", "#9aa7b8", "#b3a37c")
# a reference part (the imported board) is solder-mask green, so it reads apart from the box it sits in
REFERENCE_COLOR = "#2f7a46"
_COLOR = re.compile(r"#[0-9a-f]{6}")
_LMS_TO_LAB = (
    (0.2104542553, 0.7936177850, -0.0040720468),
    (1.9779984951, -2.4285922050, 0.4505937099),
    (0.0259040371, 0.7827717662, -0.8086757660),
)
_LAB_TO_LMS = (
    (1, 0.3963377774, 0.2158037573),
    (1, -0.1055613458, -0.0638541728),
    (1, -0.0894841775, -1.2914855480),
)
_LMS_TO_RGB = (
    (4.0767416621, -3.3077115913, 0.2309699292),
    (-1.2684380046, 2.6097574011, -0.3413193965),
    (-0.0041960863, -0.7034186147, 1.7076147010),
)
# generated colors keep at least this OKLab distance (about three just-noticeable differences) from every
# earlier color and the reference; the fixed palette's closest pair is 0.031.
_MIN_DISTANCE = 0.03
_CANDIDATE_LIGHTNESS = (0.70, 0.62, 0.78)
_CANDIDATES = 360
_SPLIT_STEP = 0.07
_SPLIT_LIGHTNESS = (0.45, 0.95)
# (hue turn in radians, added chroma) for each family of shades, the body's own hue and chroma first
_SPLIT_VARIANTS = ((0, 0), (math.radians(45), 0.02), (math.radians(-45), 0.02), (0, 0.05))
# shortcut: split shades keep apart from the first 12 body colors only; check more once models have more bodies.
_SPLIT_GUARD = 12
MAX_REPAIR_FEATURES = 32
MAX_REPAIR_FIELDS = 8
MAX_OVERLAP_MESH_BYTES = 2 * 1024 * 1024
MAX_TOTAL_OVERLAP_MESH_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class ScenePart:
    name: str
    shape: Shape
    color: str | None = None
    visible: bool = True
    role: str = "manufacturing"
    # short part-list text; name stays the stable identity.
    label: str | None = None
    source_body: BodySource | None = None
    terminal_feature: str | None = None
    area: float | None = None
    volume: float | None = None
    # the features that built this part, oldest first: [{"id", "kind", "holes"?}]
    features: tuple[dict, ...] = ()
    # what the committed features say about this part, for the human: [{"label", "value"}]
    facts: tuple[dict, ...] = ()


@dataclass(frozen=True)
class SceneOccurrence:
    occurrence_id: str
    definition: ScenePart
    transform: RigidTransform


def _bytes(kind: str, values: list) -> bytes:
    data = array(kind, values)
    if sys.byteorder == "big":
        data.byteswap()
    return data.tobytes()


def _tessellate(part: ScenePart, tolerance: float) -> tuple[bytes, dict, tuple, tuple]:
    """Binary mesh: float32 xyz vertices, uint32 triangle indices, float32 edge segment pairs."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepTools import BRepTools
    from OCP.collections import (
        IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher as FacesOfEdge,
    )
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    shape = copy.deepcopy(part.shape)
    if shape.wrapped is None:
        raise ValueError(f"part {part.name!r} is empty")
    BRepTools.Clean_s(shape.wrapped)
    for face in shape.faces():
        BRepMesh_IncrementalMesh(
            face.wrapped,
            tolerance,
            False,
            ViewerSceneStore.ANGULAR_TOLERANCE,
            False,
        )
    vertices, triangles = shape.tessellate(tolerance, ViewerSceneStore.ANGULAR_TOLERANCE)
    if not triangles:
        raise ValueError(f"part {part.name!r} has no faces to show")
    # edge lines are the face meshes' own boundary polygons, so lines and faces meet exactly.
    faces_of = FacesOfEdge()
    TopExp.MapShapesAndAncestors_s(shape.wrapped, TopAbs_EDGE, TopAbs_FACE, faces_of)
    edges = []
    for index in range(1, faces_of.Extent() + 1):
        location = TopLoc_Location()
        mesh = BRep_Tool.Triangulation_s(
            TopoDS.Face(faces_of.FindFromIndex(index).First()), location
        )
        polygon = BRep_Tool.PolygonOnTriangulation_s(
            TopoDS.Edge(faces_of.FindKey(index)), mesh, location
        )
        if polygon is None:  # a degenerated edge, such as a cone apex, has no length to draw
            continue
        move = location.Transformation()
        points = [
            mesh.Node(polygon.Node(i)).Transformed(move) for i in range(1, polygon.NbNodes() + 1)
        ]
        for a, b in itertools.pairwise(points):
            edges += [a.X(), a.Y(), a.Z(), b.X(), b.Y(), b.Z()]
    flat = [c for v in vertices for c in v]
    data = _bytes("f", flat) + _bytes("I", [i for t in triangles for i in t]) + _bytes("f", edges)
    lo = tuple(min(flat[i::3]) for i in range(3))
    hi = tuple(max(flat[i::3]) for i in range(3))
    counts = {
        "vertexCount": len(vertices),
        "triangleCount": len(triangles),
        "edgeVertexCount": len(edges) // 3,
    }
    return data, counts, lo, hi


def _oklab(color: str) -> tuple[float, float, float]:
    """#rrggbb to OKLab (L, a, b), where euclidean distance tracks seen difference."""
    r, g, b = (
        c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        for c in (int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    )
    lms = (
        (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3),
        (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3),
        (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3),
    )
    return tuple(sum(k * c for k, c in zip(row, lms, strict=True)) for row in _LMS_TO_LAB)


def _hex(lab: Sequence[float]) -> str:
    lms = [sum(k * c for k, c in zip(row, lab, strict=True)) ** 3 for row in _LAB_TO_LMS]
    rgb = (sum(k * c for k, c in zip(row, lms, strict=True)) for row in _LMS_TO_RGB)
    rgb = (min(max(c, 0), 1) for c in rgb)
    rgb = (12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055 for c in rgb)
    return "#" + "".join(f"{round(c * 255):02x}" for c in rgb)


def _candidate(index: int) -> str:
    """Muted OKLab hues a golden-ratio step apart in 220..450 degrees (never green or teal), three lightnesses."""
    hue = math.radians(220 + (index * 0.6180339887) % 1 * 230)
    return _hex((_CANDIDATE_LIGHTNESS[index % 3], 0.05 * math.cos(hue), 0.05 * math.sin(hue)))


@functools.cache
def _manufactured_colors(count: int) -> tuple[str, ...]:
    """The palette, then each next color the first candidate at least ``_MIN_DISTANCE`` from every earlier one.

    Greedy in order, so the first n colors never change as count grows.
    """
    colors = list(PALETTE[:count])
    return (
        *colors,
        *_pick(
            map(_candidate, range(_CANDIDATES)), [REFERENCE_COLOR, *colors], count - len(colors)
        ),
    )


def _pick(candidates: Iterable[str], taken: Sequence[str], count: int) -> list[str]:
    """``count`` candidates in order, each the first at least ``_MIN_DISTANCE`` from ``taken`` and earlier picks."""
    candidates = [(c, _oklab(c)) for c in candidates]
    taken = [_oklab(c) for c in taken]
    picked = []
    while len(picked) < count:
        gaps = [min(math.dist(lab, t) for t in taken) for _, lab in candidates]
        # the first candidate far enough; once the space is full, the farthest one
        pick = next(
            (i for i, gap in enumerate(gaps) if gap >= _MIN_DISTANCE), gaps.index(max(gaps))
        )
        color, lab = candidates[pick]
        picked.append(color)
        taken.append(lab)
    return picked


@functools.cache
def _shades(color: str, count: int) -> tuple[str, ...]:
    """The pieces of a split body: its color, then shades apart from the first body colors and each other.

    Lighter or darker in steps inside the sRGB gamut; once lightness runs out, the same steps with the hue
    turned. Greedy, so a piece's shade never changes as the count grows.
    """
    lightness, a, b = _oklab(color)
    chroma, hue = math.hypot(a, b), math.atan2(b, a)
    candidates = []
    for turn, more in _SPLIT_VARIANTS:
        if (
            turn and 90 < math.degrees(hue + turn) % 360 < 220
        ):  # never turn toward green or teal, the board's hues
            continue
        ab = (chroma + more) * math.cos(hue + turn), (chroma + more) * math.sin(hue + turn)
        for step in range(1, 8):
            for sign in (1, -1):
                lab = (lightness + sign * step * _SPLIT_STEP, *ab)
                shade = _hex(lab)
                # out of gamut clips toward white or black, where many shades would end up the same
                if (
                    _SPLIT_LIGHTNESS[0] <= lab[0] <= _SPLIT_LIGHTNESS[1]
                    and math.dist(_oklab(shade), lab) < 0.005
                ):
                    candidates.append(shade)
    guard = [REFERENCE_COLOR, *_manufactured_colors(_SPLIT_GUARD), color]
    return (color, *_pick(candidates, guard, count - 1))


def _part_colors(parts: Sequence[ScenePart]) -> list[str]:
    """Given color, reference green, or the color of the part's source body.

    A body's color comes from its place in body creation order (callers list parts that way), so later
    features and splits of other bodies never move it. Pieces of a split body are shades of its color.
    """
    bodies: dict[object, list[str]] = {}
    for part in parts:
        if part.color is None and part.role != "reference":
            bodies.setdefault(part.source_body or part.name, []).append(part.name)
    base = dict(zip(bodies, _manufactured_colors(len(bodies)), strict=True))
    colors = []
    for part in parts:
        if part.color or part.role == "reference":
            colors.append(part.color or REFERENCE_COLOR)
            continue
        body = part.source_body or part.name
        pieces = sorted(bodies[body])
        colors.append(_shades(base[body], len(pieces))[pieces.index(part.name)])
    return colors


def _item_part(item: ScenePart | SceneOccurrence) -> ScenePart:
    return item.definition if isinstance(item, SceneOccurrence) else item


def _item_name(item: ScenePart | SceneOccurrence) -> str:
    return item.occurrence_id if isinstance(item, SceneOccurrence) else item.name


def _definitions(items: list[ScenePart | SceneOccurrence]) -> list[ScenePart]:
    by_name = {}
    for item in items:
        part = _item_part(item)
        if isinstance(item, SceneOccurrence):
            from coalesce.assembly import definition_shape

            part = replace(part, shape=definition_shape(part.shape))
            by_name[part.name] = part
        else:
            by_name.setdefault(part.name, part)
    parts = list(by_name.values())
    for part in parts:
        if part.role not in {"manufacturing", "reference"}:
            raise ValueError(f"part {part.name!r} has unknown role {part.role!r}")
        if part.color is not None and not _COLOR.fullmatch(part.color):
            raise ValueError(f"part {part.name!r} color must be #rrggbb, got {part.color!r}")
    return parts


def _definition_assets(parts: list[ScenePart], tolerance: float) -> tuple[dict, dict]:
    assets, meshes = {}, {}
    for part, color in zip(parts, _part_colors(parts), strict=True):
        data, counts, _, _ = _tessellate(part, tolerance)
        digest = hashlib.sha256(data).hexdigest()[:32]
        assets[part.name] = color, digest, counts
        meshes[digest] = data
    return assets, meshes


def _number(value: float, places: int = 3) -> str:
    return f"{round(value, places) + 0.0:g}"


def _rotation_text(transform: RigidTransform) -> str:
    if round(transform.angle_degrees, 3) == 0:
        return "none"
    principal = {
        (1.0, 0.0, 0.0): "X",
        (-1.0, 0.0, 0.0): "-X",
        (0.0, 1.0, 0.0): "Y",
        (0.0, -1.0, 0.0): "-Y",
        (0.0, 0.0, 1.0): "Z",
        (0.0, 0.0, -1.0): "-Z",
    }.get(transform.axis)
    axis = (
        principal
        or "(" + ", ".join(f"{round(value, 2) + 0.0:.2f}" for value in transform.axis) + ")"
    )
    return f"{_number(transform.angle_degrees)}° about {axis}"


def _occurrence_facts(occurrence: SceneOccurrence) -> list[dict]:
    from coalesce.assembly import definition_shape

    part, value = occurrence.definition, occurrence.transform
    size = definition_shape(part.shape).bounding_box().size
    area = part.area if part.area is not None else part.shape.area
    volume = part.volume if part.volume is not None else part.shape.volume
    return [
        {"label": "Translation", "value": ", ".join(_number(n) for n in value.translation) + " mm"},
        {"label": "Rotation", "value": _rotation_text(value)},
        {"label": "Terminal feature", "value": part.terminal_feature or "none"},
        {"label": "Definition size", "value": " × ".join(_number(n) for n in size) + " mm"},
        {"label": "Area", "value": f"{_number(area)} mm²"},
        {"label": "Volume", "value": f"{_number(volume)} mm³"},
        *part.facts,
    ]


def _scene_row(item: ScenePart | SceneOccurrence, assets: dict) -> tuple[dict, tuple, tuple]:
    from coalesce.assembly import definition_shape, locate_shape

    part = _item_part(item)
    occurrence = item if isinstance(item, SceneOccurrence) else None
    exact = (
        locate_shape(part.shape, occurrence.transform).bounding_box()
        if occurrence
        else part.shape.bounding_box()
    )
    display_bounds = definition_shape(part.shape).bounding_box() if occurrence else exact
    color, digest, counts = assets[part.name]
    area = part.area if part.area is not None else part.shape.area
    volume = part.volume if part.volume is not None else part.shape.volume
    row = {
        "name": _item_name(item),
        "partId": part.name,
        "label": _item_name(item) if occurrence else part.label or part.name,
        "role": part.role,
        "sourceBody": asdict(part.source_body) if part.source_body else None,
        "terminalFeatureId": part.terminal_feature,
        "area": {"value": area, "unit": "mm^2"},
        "volume": {"value": volume, "unit": "mm^3"},
        "color": color,
        "visible": part.visible,
        "size": [display_bounds.size.X, display_bounds.size.Y, display_bounds.size.Z],
        "features": list(part.features),
        "facts": _occurrence_facts(occurrence) if occurrence else list(part.facts),
        "mesh": {"id": digest, "url": f"/api/mesh/{digest}", **counts},
    }
    if occurrence:
        value = occurrence.transform
        row.update(
            occurrenceId=occurrence.occurrence_id,
            definitionLabel=part.label or part.name,
            transform={
                "translation": list(value.translation),
                "rotation": {"axis": list(value.axis), "angleDegrees": value.angle_degrees},
            },
            bbox={"min": list(exact.min), "max": list(exact.max)},
        )
    return row, tuple(exact.min), tuple(exact.max)


def _scene_bounds(lows: list[tuple], highs: list[tuple]) -> dict:
    if not lows:
        return {"min": [0, 0, 0], "max": [0, 0, 0]}
    return {"min": [min(c) for c in zip(*lows)], "max": [max(c) for c in zip(*highs)]}


def _inspection_assets(
    rows: list[dict], tolerance: float, maximum_bytes: int, total_bytes: int
) -> tuple[list[dict], dict[str, bytes]]:
    published = [{key: value for key, value in row.items() if key != "_overlapShape"} for row in rows]
    meshes, used, exhausted = {}, 0, False
    # Pair identity order makes the same limited set visible even if requirement rows are reordered.
    ordered = sorted(enumerate(rows), key=lambda item: tuple(sorted(item[1]["occurrenceIds"])))
    for index, source in ordered:
        shape = source.get("_overlapShape")
        if shape is None:
            continue
        if exhausted:
            published[index]["overlapMeshOmitted"] = "publication overlap budget exhausted"
            continue
        data, counts, _, _ = _tessellate(ScenePart(f"overlap-{index}", shape), tolerance)
        if len(data) > maximum_bytes:
            published[index]["overlapMeshOmitted"] = "mesh exceeds the viewer size limit"
            continue
        if used + len(data) > total_bytes:
            published[index]["overlapMeshOmitted"] = "publication overlap budget exhausted"
            exhausted = True
            continue
        digest = hashlib.sha256(data).hexdigest()[:32]
        published[index]["overlapMesh"] = {
            "id": digest,
            "url": f"/api/mesh/{digest}",
            **counts,
            "byteCount": len(data),
        }
        meshes[digest] = data
        used += len(data)
    return published, meshes


def _scene_mesh_ids(scene: dict) -> set[str]:
    return {part["mesh"]["id"] for part in scene["parts"]} | {
        row["overlapMesh"]["id"]
        for row in scene.get("assemblyInspection", [])
        if "overlapMesh" in row
    }


def _build_scene(
    revision: int,
    items: Iterable[ScenePart | SceneOccurrence],
    tolerance: float,
    change: dict | None,
    fit: list[dict],
    assembly_inspection: list[dict],
    assembly_state: dict | None,
) -> tuple[dict, dict[str, bytes]]:
    """Validate and tessellate definitions once into scene JSON and mesh bytes."""
    items = list(items)
    names = [_item_name(item) for item in items]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate part names in {names}")
    definitions = _definitions(items)
    assets, meshes = _definition_assets(definitions, tolerance)
    assembly_inspection, inspection_meshes = _inspection_assets(
        assembly_inspection,
        tolerance,
        MAX_OVERLAP_MESH_BYTES,
        MAX_TOTAL_OVERLAP_MESH_BYTES,
    )
    meshes.update(inspection_meshes)
    rows = [_scene_row(item, assets) for item in items]
    scene = {
        "revision": revision,
        "units": "mm",
        "bbox": _scene_bounds([low for _, low, _ in rows], [high for _, _, high in rows]),
        "parts": [row for row, _, _ in rows],
        "change": change,
        "fit": fit,
    }
    if any(isinstance(item, SceneOccurrence) for item in items):
        from OCP.Precision import Precision

        scene["assemblyInspection"] = assembly_inspection
        scene["assemblyLinearTolerance"] = Precision.Confusion_s()
        scene["assemblyState"] = copy.deepcopy(assembly_state)
    return scene, meshes


def _bounded_edit(edit: dict | None) -> dict | None:
    if (
        not edit
        or not isinstance(edit.get("featureId"), str)
        or not isinstance(edit.get("kind"), str)
    ):
        return None
    fields = edit.get("fields")
    if not isinstance(fields, dict) or not fields:
        return None
    return {
        "featureId": edit["featureId"],
        "kind": edit["kind"],
        "fields": dict(list(fields.items())[:MAX_REPAIR_FIELDS]),
    }


def _repair(attempted: dict | None, accepted: dict) -> dict | None:
    if attempted is None or attempted["kind"] != accepted["kind"]:
        return None
    fields = {
        name: {"attempted": value, "accepted": accepted["fields"][name]}
        for name, value in attempted["fields"].items()
        if name in accepted["fields"]
    }
    if not fields:
        return None
    return {
        "featureId": accepted["featureId"],
        "kind": accepted["kind"],
        "fields": fields,
    }


class ViewerSceneStore:
    """Holds the current and previous committed revision as tessellated named parts.

    Writers call ``publish``/``publish_failure``; readers (the viewer server) take
    ``snapshot`` and block in ``wait_for_change``. Writers never wait on readers.
    """

    # shortcut: fixed 0.05 mm chordal tolerance suits enclosure-scale parts; scale it
    # with part size when metre-scale models arrive.
    TOLERANCE = 0.05
    ANGULAR_TOLERANCE = 0.2
    MAX_OVERLAP_MESH_BYTES = MAX_OVERLAP_MESH_BYTES
    MAX_TOTAL_OVERLAP_MESH_BYTES = MAX_TOTAL_OVERLAP_MESH_BYTES

    def __init__(self) -> None:
        self._changed = threading.Condition()
        self._version = 0
        self._scenes: dict[int, dict] = {}
        self._meshes: dict[str, bytes] = {}
        self._revision: int | None = None
        self._failure: dict | None = None
        self._pending_edits: OrderedDict[str, dict] = OrderedDict()
        self._repair_history: OrderedDict[str, dict] = OrderedDict()

    def publish(
        self,
        revision: int,
        parts: Iterable[ScenePart | SceneOccurrence],
        change: dict | None = None,
        fit: list[dict] | None = None,
        accepted_edit: dict | None = None,
        assembly_inspection: list[dict] | None = None,
        assembly_state: dict | None = None,
    ) -> None:
        """``change`` says what the commit did, for the human: {"featureId", "summary"}.

        ``fit`` is the enclosure fit check per pcb base: [{"baseId", "rows"}].
        """
        with self._changed:
            if self._revision is not None and revision <= self._revision:
                raise ValueError(f"revision {revision} is not after {self._revision}")
        # tessellate outside the lock: readers keep serving the last scene meanwhile.
        scene, meshes = _build_scene(
            revision,
            parts,
            self.TOLERANCE,
            change,
            fit or [],
            assembly_inspection or [],
            assembly_state,
        )

        with self._changed:
            if self._revision is not None and revision <= self._revision:
                raise ValueError(f"revision {revision} is not after {self._revision}")
            self._accept_edit(change, accepted_edit)
            scene["repairHistory"] = list(self._repair_history.values())
            self._scenes = {r: s for r, s in self._scenes.items() if r == self._revision}
            self._scenes[revision] = scene
            live = {mesh for scene in self._scenes.values() for mesh in _scene_mesh_ids(scene)}
            self._meshes = {d: b for d, b in {**self._meshes, **meshes}.items() if d in live}
            self._revision = revision
            self._failure = None
            self._version += 1
            self._changed.notify_all()

    def publish_assembly_inspection(self, revision: int, rows: list[dict]) -> None:
        """Attach already-measured pair rows to the matching committed scene."""
        with self._changed:
            if revision not in self._scenes:
                return
        published, meshes = _inspection_assets(
            rows,
            self.TOLERANCE,
            self.MAX_OVERLAP_MESH_BYTES,
            self.MAX_TOTAL_OVERLAP_MESH_BYTES,
        )
        with self._changed:
            scene = self._scenes.get(revision)
            if scene is not None:
                scene["assemblyInspection"] = published
                live = {
                    mesh
                    for stored in self._scenes.values()
                    for mesh in _scene_mesh_ids(stored)
                }
                self._meshes = {
                    digest: data
                    for digest, data in {**self._meshes, **meshes}.items()
                    if digest in live
                }
                self._version += 1
                self._changed.notify_all()

    def publish_failure(
        self, feature_ids: Sequence[str], error: dict[str, str], attempted_edit: dict | None = None
    ) -> None:
        """Mark a failed edit attempt; the last good scene stays current."""
        with self._changed:
            attempted_edit = self._track_attempt(attempted_edit)
            display_error = dict(error)
            transient = {
                key: display_error.pop(key)
                for key in ("diagnosticGeometry", "diagnosticGeometryOmitted")
                if key in display_error
            }
            self._failure = {
                "revision": self._revision,
                "featureIds": list(feature_ids),
                "error": display_error,
                **transient,
                **({"attemptedEdit": attempted_edit} if attempted_edit else {}),
            }
            self._version += 1
            self._changed.notify_all()

    def _track_attempt(self, edit: dict | None) -> dict | None:
        edit = _bounded_edit(edit)
        if edit is None:
            return None
        feature_id = edit["featureId"]
        self._pending_edits.pop(feature_id, None)
        self._pending_edits[feature_id] = edit
        while len(self._pending_edits) > MAX_REPAIR_FEATURES:
            self._pending_edits.popitem(last=False)
        return edit

    def _accept_edit(self, change: dict | None, edit: dict | None) -> None:
        feature_id = change.get("featureId") if change else None
        if not isinstance(feature_id, str):
            return
        # A same-feature commit consumes the attempt; only overlapping update fields make a repair.
        attempted = self._pending_edits.pop(feature_id, None)
        edit = _bounded_edit(edit)
        repair = _repair(attempted, edit) if edit and edit["featureId"] == feature_id else None
        if repair is None:
            return
        self._repair_history.pop(feature_id, None)
        self._repair_history[feature_id] = repair
        while len(self._repair_history) > MAX_REPAIR_FEATURES:
            self._repair_history.popitem(last=False)

    @property
    def current_revision(self) -> int | None:
        with self._changed:
            return self._revision

    def scene(self, revision: int) -> dict | None:
        with self._changed:
            return self._scenes.get(revision)

    def mesh(self, digest: str) -> bytes | None:
        with self._changed:
            return self._meshes.get(digest)

    def snapshot(self) -> tuple[int, int | None, dict | None]:
        """(version, current revision, outstanding failure or None)."""
        with self._changed:
            return self._version, self._revision, self._failure

    def wait_for_change(self, version: int, timeout: float) -> bool:
        with self._changed:
            return self._changed.wait_for(lambda: self._version != version, timeout)
