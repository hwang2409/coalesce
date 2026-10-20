"""STEP export with fresh-process geometric validation and atomic no-overwrite publish."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RELATIVE_TOLERANCE = 1e-7
ABSOLUTE_TOLERANCE = 1e-7


@dataclass(frozen=True)
class ExportFailure(Exception):
    code: str
    message: str
    details: dict[str, Any]

    def __init__(self, code: str, message: str, **details: Any):
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "details", details)
        Exception.__init__(self, message)


def _point(vector: Any) -> list[float]:
    return [float(getattr(vector, axis)) for axis in "XYZ"]


def _bbox(shapes: list[Any]) -> dict[str, list[float]]:
    from build123d import Compound

    box = Compound(children=shapes).bounding_box()
    return {"min": _point(box.min), "max": _point(box.max)}


def _cylinders(shape: Any) -> list[dict[str, Any]]:
    from build123d import GeomType

    cylinders = []
    for face in shape.faces():
        if face.geom_type is not GeomType.CYLINDER:
            continue
        axis = face.axis_of_rotation
        direction = _point(axis.direction)
        first = next((value for value in direction if abs(value) > 1e-12), 0.0)
        if first < 0:
            direction = [-value for value in direction]
        position = _point(axis.position)
        along = sum(a * b for a, b in zip(position, direction, strict=True))
        line_position = [
            value - along * axis_value
            for value, axis_value in zip(position, direction, strict=True)
        ]
        cylinders.append(
            {
                "radius": float(face.radius),
                "axis": {"position": line_position, "direction": direction},
            }
        )
    return sorted(
        cylinders,
        key=lambda item: (
            item["radius"],
            *item["axis"]["position"],
            *item["axis"]["direction"],
        ),
    )


def _part_metrics(shape: Any, label: str) -> dict[str, Any]:
    solids = list(shape.solids())
    return {
        "label": label,
        "solidCount": len(solids),
        "bbox": _bbox(solids),
        "volume": float(sum(solid.volume for solid in solids)),
        "area": float(sum(solid.area for solid in solids)),
        "cylinders": [item for solid in solids for item in _cylinders(solid)],
    }


def _metrics(shapes: list[Any], labels: list[str]) -> dict[str, Any]:
    parts = [_part_metrics(shape, label) for shape, label in zip(shapes, labels, strict=True)]
    solids = [solid for shape in shapes for solid in shape.solids()]
    return {
        "partCount": len(parts),
        "solidCount": len(solids),
        "bbox": _bbox(solids),
        "parts": parts,
        "volume": float(sum(part["volume"] for part in parts)),
        "area": float(sum(part["area"] for part in parts)),
    }


def _read_step_metrics(path: Path) -> dict[str, Any]:
    from build123d import import_step

    imported = import_step(path)
    shapes = list(imported.children) if imported.children else [imported]
    return _metrics(shapes, [shape.label for shape in shapes])


def verify_step_fresh(path: Path) -> dict[str, Any]:
    """Reimport one STEP in a new interpreter and return only its measured evidence."""
    completed = subprocess.run(
        [sys.executable, "-m", "coalesce.export", "--verify", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise ExportFailure(
            "EXPORT_VALIDATION_FAILED",
            "the STEP file could not be reimported in a fresh Python process",
            hint="inspect the model and retry export to a different new path",
            verifierError=completed.stderr.strip()[-2000:],
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ExportFailure(
            "EXPORT_VALIDATION_FAILED",
            "the fresh-process STEP verifier returned invalid evidence",
            hint="inspect the model and retry export to a different new path",
        ) from error


def _same(expected: Any, actual: Any) -> bool:
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return math.isclose(
            float(expected),
            float(actual),
            rel_tol=RELATIVE_TOLERANCE,
            abs_tol=ABSOLUTE_TOLERANCE,
        )
    if isinstance(expected, dict) and isinstance(actual, dict):
        return expected.keys() == actual.keys() and all(
            _same(expected[key], actual[key]) for key in expected
        )
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(
            _same(left, right) for left, right in zip(expected, actual, strict=True)
        )
    return expected == actual


def _geometry(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        **{key: evidence[key] for key in ("partCount", "solidCount", "bbox", "volume", "area")},
        "parts": [
            {key: part[key] for key in ("solidCount", "bbox", "volume", "area", "cylinders")}
            for part in evidence["parts"]
        ],
    }


def _validate(expected: dict[str, Any], actual: dict[str, Any]) -> None:
    try:
        matches = _same(_geometry(expected), _geometry(actual))
    except (KeyError, TypeError, ValueError):
        matches = False
    if not matches:
        raise ExportFailure(
            "EXPORT_VALIDATION_FAILED",
            "fresh-process STEP geometry does not match the source parts",
            hint="inspect the model and retry export to a different new path",
            tolerance={"relative": RELATIVE_TOLERANCE, "absolute": ABSOLUTE_TOLERANCE},
            expected=expected,
            actual=actual,
        )


def export_parts(path: Path, parts: list[Any]) -> dict[str, Any]:
    """Write, validate, and atomically publish selected terminal parts to a new path."""
    from build123d import Compound, Solid, export_step

    if not path.is_absolute():
        raise ExportFailure(
            "EXPORT_WRITE_FAILED",
            "export path must be absolute",
            hint="provide an explicit absolute path whose parent directory already exists",
        )
    if os.path.lexists(path):
        raise ExportFailure(
            "EXPORT_PATH_EXISTS",
            "export path already exists; STEP export never overwrites",
            hint="choose a new path",
        )
    parent = path.parent
    if not parent.is_dir() or parent.stat().st_mode & 0o222 == 0:
        raise ExportFailure(
            "EXPORT_WRITE_FAILED",
            "export parent directory does not exist or is not writable",
            hint="create a writable parent directory and retry with a new path",
        )

    shapes = [Solid(part.shape.wrapped, label=part.label) for part in parts]
    expected = _metrics(shapes, [part.label for part in parts])
    temporary: Path | None = None
    published = False
    try:
        with tempfile.NamedTemporaryFile(
            dir=parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
        export_step(Compound(children=shapes, label="coalesce_export"), temporary)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        actual = verify_step_fresh(temporary)
        _validate(expected, actual)
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise ExportFailure(
                "EXPORT_PATH_EXISTS",
                "export path already exists; STEP export never overwrites",
                hint="choose a new path",
            ) from error
        published = True
        temporary.unlink()
        temporary = None
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        labels_expected = [part.label for part in parts]
        labels_actual = [part.get("label", "") for part in actual["parts"]]
        for part_id, evidence in zip(
            [part.part_id for part in parts], actual["parts"], strict=True
        ):
            evidence["partId"] = part_id
        return {
            "path": str(path),
            "partIds": [part.part_id for part in parts],
            "sha256": digest,
            "tolerance": {
                "relative": RELATIVE_TOLERANCE,
                "absolute": ABSOLUTE_TOLERANCE,
            },
            "validation": {
                "geometry": {"status": "passed", "freshProcess": True},
                "labels": {
                    "status": "preserved" if labels_actual == labels_expected else "not preserved",
                    "expected": labels_expected,
                    "actual": labels_actual,
                },
                "reimport": actual,
            },
        }
    except ExportFailure:
        if published:
            path.unlink(missing_ok=True)
        raise
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        if published:
            path.unlink(missing_ok=True)
        raise ExportFailure(
            "EXPORT_WRITE_FAILED",
            "STEP export could not be written",
            hint="check that the absolute parent directory is writable and retry with a new path",
        ) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] != "--verify":
        raise SystemExit("usage: python -m coalesce.export --verify PATH")
    print(json.dumps(_read_step_metrics(Path(sys.argv[2])), separators=(",", ":")))


if __name__ == "__main__":
    _main()
