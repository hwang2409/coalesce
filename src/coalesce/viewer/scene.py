"""Committed geometry for the live viewer: tessellated named parts per revision."""

from __future__ import annotations

import hashlib
import itertools
import re
import sys
import threading
from array import array
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from build123d import Shape

# restrained engineering-review colors, assigned in part order when no color is given.
PALETTE = ("#8c9bab", "#a9a08e", "#7e9a8a", "#a08c9b", "#9aa7b8", "#b3a37c")
_COLOR = re.compile(r"#[0-9a-f]{6}")


@dataclass(frozen=True)
class ScenePart:
    name: str
    shape: Shape
    color: str | None = None
    visible: bool = True


def _bytes(kind: str, values: list) -> bytes:
    data = array(kind, values)
    if sys.byteorder == "big":
        data.byteswap()
    return data.tobytes()


def _tessellate(part: ScenePart, tolerance: float) -> tuple[bytes, dict, tuple, tuple]:
    """Binary mesh: float32 xyz vertices, uint32 triangle indices, float32 edge segment pairs."""
    vertices, triangles = part.shape.tessellate(tolerance, ViewerSceneStore.ANGULAR_TOLERANCE)
    if not triangles:
        raise ValueError(f"part {part.name!r} has no faces to show")
    edges = []
    for edge in part.shape.edges():
        points = edge.positions(deflection=tolerance)
        for a, b in itertools.pairwise(points):
            edges += [*a, *b]
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

    def publish(self, revision: int, parts: Iterable[ScenePart]) -> None:
        parts = list(parts)
        names = [p.name for p in parts]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate part names in {names}")
        for part in parts:
            if part.color is not None and not _COLOR.fullmatch(part.color):
                raise ValueError(f"part {part.name!r} color must be #rrggbb, got {part.color!r}")
        with self._changed:
            if self._revision is not None and revision <= self._revision:
                raise ValueError(f"revision {revision} is not after {self._revision}")

        # tessellate outside the lock: readers keep serving the last scene meanwhile.
        meshes, scene_parts, lows, highs = {}, [], [], []
        for index, part in enumerate(parts):
            data, counts, lo, hi = _tessellate(part, self.TOLERANCE)
            digest = hashlib.sha256(data).hexdigest()[:32]
            meshes[digest] = data
            exact = part.shape.bounding_box()
            scene_parts.append(
                {
                    "name": part.name,
                    "color": part.color or PALETTE[index % len(PALETTE)],
                    "visible": part.visible,
                    "size": [exact.size.X, exact.size.Y, exact.size.Z],
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
        }

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

    def publish_failure(self, feature_ids: Sequence[str], error: str) -> None:
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
