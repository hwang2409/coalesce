"""STEP export with fresh-process validation and no-overwrite publication."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
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
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "coalesce.export", "--verify", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ExportFailure(
            "EXPORT_VALIDATION_FAILED",
            "the fresh Python process could not validate the STEP file",
            hint="inspect the model and retry export to a different new path",
        ) from error
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


def _validated_step_bytes(
    shapes: list[Any], expected: dict[str, Any]
) -> tuple[bytes, str, dict[str, Any]]:
    from build123d import Compound, export_step

    temporary_directory = Path(tempfile.mkdtemp())
    temporary_path = temporary_directory / "export.step"
    try:
        export_step(Compound(children=shapes, label="coalesce_export"), temporary_path)
        actual = verify_step_fresh(temporary_path)
        _validate(expected, actual)
        content = temporary_path.read_bytes()
        return content, hashlib.sha256(content).hexdigest(), actual
    finally:
        shutil.rmtree(temporary_directory)


def _open_directory(path: Path) -> int:
    if not path.is_absolute():
        raise ExportFailure(
            "EXPORT_WRITE_FAILED",
            "export path must be absolute",
            hint="provide an explicit absolute path whose parent directory already exists",
        )
    parent = path.parent
    if not parent.is_dir() or parent.stat().st_mode & 0o222 == 0:
        raise ExportFailure(
            "EXPORT_WRITE_FAILED",
            "export parent directory does not exist or is not writable",
            hint="create a writable parent directory and retry with a new path",
        )
    return os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _remove_published(
    directory_fd: int, name: str, expected: os.stat_result
) -> bool:
    try:
        actual = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    if not _same_inode(actual, expected):
        return False
    os.unlink(name, dir_fd=directory_fd)
    os.fsync(directory_fd)
    return True


def _publication_failure(error: Exception, removed: bool) -> ExportFailure:
    if isinstance(error, ExportFailure):
        code = error.code
        message = error.message
        details = error.details
    else:
        code = "EXPORT_WRITE_FAILED"
        message = "STEP export could not be written"
        details = {
            "hint": "check that the absolute parent directory is writable and retry",
        }
    if not removed:
        message += "; the destination was replaced, so the foreign file was preserved"
        details = {**details, "foreignFilePreserved": True}
    return ExportFailure(code, message, **details)


def _publish_bytes(
    path: Path, content: bytes, record: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    directory_fd = _open_directory(path)
    descriptor: int | None = None
    published_stat: os.stat_result | None = None
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | os.O_CLOEXEC,
                0o644,
                dir_fd=directory_fd,
            )
        except FileExistsError as error:
            raise ExportFailure(
                "EXPORT_PATH_EXISTS",
                "export path already exists; STEP export never overwrites",
                hint="choose a new path",
            ) from error
        published_stat = os.fstat(descriptor)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("STEP export write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.fsync(directory_fd)
        return record()
    except Exception as error:
        removed = published_stat is None or _remove_published(
            directory_fd, path.name, published_stat
        )
        raise _publication_failure(error, removed) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def _export_fields(
    path: Path, parts: list[Any], digest: str, actual: dict[str, Any]
) -> dict[str, Any]:
    part_ids = [part.part_id for part in parts]
    labels_expected = [part.label for part in parts]
    labels_actual = [part.get("label", "") for part in actual["parts"]]
    for part_id, evidence in zip(part_ids, actual["parts"], strict=True):
        evidence["partId"] = part_id
    return {
        "path": str(path),
        "partIds": part_ids,
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


def export_parts(
    path: Path,
    parts: list[Any],
    record: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Validate bytes, publish without replacement, then durably record success."""
    from build123d import Solid

    shapes = [Solid(part.shape.wrapped, label=part.label) for part in parts]
    expected = _metrics(shapes, [part.label for part in parts])
    content, digest, actual = _validated_step_bytes(shapes, expected)
    fields = _export_fields(path, parts, digest, actual)
    return _publish_bytes(path, content, lambda: record(fields))


def _main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] != "--verify":
        raise SystemExit("usage: python -m coalesce.export --verify PATH")
    print(json.dumps(_read_step_metrics(Path(sys.argv[2])), separators=(",", ":")))


if __name__ == "__main__":
    _main()
