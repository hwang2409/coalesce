"""Committed geometry for the live viewer: tessellated named parts per revision."""

from __future__ import annotations

import copy
import hashlib
import itertools
import re
import sys
import threading
from array import array
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from build123d import Shape

    from coalesce.references import BodySource

# restrained engineering-review colors, assigned in part order when no color is given.
PALETTE = ("#8c9bab", "#a9a08e", "#7e9a8a", "#a08c9b", "#9aa7b8", "#b3a37c")
_COLOR = re.compile(r"#[0-9a-f]{6}")


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
        mesh = BRep_Tool.Triangulation_s(TopoDS.Face(faces_of.FindFromIndex(index).First()), location)
        polygon = BRep_Tool.PolygonOnTriangulation_s(TopoDS.Edge(faces_of.FindKey(index)), mesh, location)
        if polygon is None:  # a degenerated edge, such as a cone apex, has no length to draw
            continue
        move = location.Transformation()
        points = [mesh.Node(polygon.Node(i)).Transformed(move) for i in range(1, polygon.NbNodes() + 1)]
        for a, b in itertools.pairwise(points):
            edges += [a.X(), a.Y(), a.Z(), b.X(), b.Y(), b.Z()]
    flat = [c for v in vertices for c in v]
    data = (
        _bytes("f", flat)
        + _bytes("I", [i for t in triangles for i in t])
        + _bytes("f", edges)
    )
    lo = tuple(min(flat[i::3]) for i in range(3))
    hi = tuple(max(flat[i::3]) for i in range(3))
    counts = {
        "vertexCount": len(vertices),
        "triangleCount": len(triangles),
        "edgeVertexCount": len(edges) // 3,
    }
    return data, counts, lo, hi


def _build_scene(
    revision: int, parts: Iterable[ScenePart], tolerance: float, change: dict | None
) -> tuple[dict, dict[str, bytes]]:
    """Validate and tessellate parts into (scene JSON, mesh bytes by content digest)."""
    parts = list(parts)
    names = [p.name for p in parts]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate part names in {names}")
    for part in parts:
        if part.role not in {"manufacturing", "reference"}:
            raise ValueError(f"part {part.name!r} has unknown role {part.role!r}")
        if part.color is not None and not _COLOR.fullmatch(part.color):
            raise ValueError(f"part {part.name!r} color must be #rrggbb, got {part.color!r}")
    meshes, scene_parts, lows, highs = {}, [], [], []
    for index, part in enumerate(parts):
        data, counts, lo, hi = _tessellate(part, tolerance)
        digest = hashlib.sha256(data).hexdigest()[:32]
        meshes[digest] = data
        exact = part.shape.bounding_box()
        scene_parts.append(
            {
                "name": part.name,
                "partId": part.name,
                "label": part.label or part.name,
                "role": part.role,
                "sourceBody": asdict(part.source_body) if part.source_body else None,
                "terminalFeatureId": part.terminal_feature,
                "area": {"value": part.area if part.area is not None else part.shape.area, "unit": "mm^2"},
                "volume": {"value": part.volume if part.volume is not None else part.shape.volume, "unit": "mm^3"},
                "color": part.color or PALETTE[index % len(PALETTE)],
                "visible": part.visible,
                "size": [exact.size.X, exact.size.Y, exact.size.Z],
                "features": list(part.features),
                "mesh": {"id": digest, "url": f"/api/mesh/{digest}", **counts},
            }
        )
        lows.append(lo)
        highs.append(hi)
    scene = {
        "revision": revision,
        "units": "mm",
        "bbox": {
            "min": [min(c) for c in zip(*lows)] if lows else [0, 0, 0],
            "max": [max(c) for c in zip(*highs)] if highs else [0, 0, 0],
        },
        "parts": scene_parts,
        "change": change,
    }
    return scene, meshes


class ViewerSceneStore:
    """Holds the current and previous committed revision as tessellated named parts.

    Writers call ``publish``/``publish_failure``; readers (the viewer server) take
    ``snapshot`` and block in ``wait_for_change``. Writers never wait on readers.
    """

    # shortcut: fixed 0.05 mm chordal tolerance suits enclosure-scale parts; scale it
    # with part size when metre-scale models arrive.
    TOLERANCE = 0.05
    ANGULAR_TOLERANCE = 0.2

    def __init__(self) -> None:
        self._changed = threading.Condition()
        self._version = 0
        self._scenes: dict[int, dict] = {}
        self._meshes: dict[str, bytes] = {}
        self._revision: int | None = None
        self._failure: dict | None = None

    def publish(self, revision: int, parts: Iterable[ScenePart], change: dict | None = None) -> None:
        """``change`` says what the commit did, for the human: {"featureId", "summary"}."""
        with self._changed:
            if self._revision is not None and revision <= self._revision:
                raise ValueError(f"revision {revision} is not after {self._revision}")
        # tessellate outside the lock: readers keep serving the last scene meanwhile.
        scene, meshes = _build_scene(revision, parts, self.TOLERANCE, change)

        with self._changed:
            if self._revision is not None and revision <= self._revision:
                raise ValueError(f"revision {revision} is not after {self._revision}")
            self._scenes = {r: s for r, s in self._scenes.items() if r == self._revision}
            self._scenes[revision] = scene
            live = {p["mesh"]["id"] for s in self._scenes.values() for p in s["parts"]}
            self._meshes = {d: b for d, b in {**self._meshes, **meshes}.items() if d in live}
            self._revision = revision
            self._failure = None
            self._version += 1
            self._changed.notify_all()

    def publish_failure(self, feature_ids: Sequence[str], error: dict[str, str]) -> None:
        """Mark a failed edit attempt; the last good scene stays current."""
        with self._changed:
            self._failure = {
                "revision": self._revision,
                "featureIds": list(feature_ids),
                "error": error,
            }
            self._version += 1
            self._changed.notify_all()

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
