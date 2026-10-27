"""STEP export with fresh-process validation and no-overwrite publication."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RELATIVE_TOLERANCE = 1e-7
ABSOLUTE_TOLERANCE = 1e-7


def _silence_occt_messages() -> None:
    from OCP.Message import Message

    messenger = Message.DefaultMessenger_s()
    for printer in list(messenger.Printers()):
        messenger.RemovePrinter(printer)


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
    from OCP.BRepAdaptor import BRepAdaptor_Surface

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
                "radius": float(BRepAdaptor_Surface(face.wrapped).Cylinder().Radius()),
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

    _silence_occt_messages()
    imported = import_step(path)
    shapes = list(imported.children) if imported.children else [imported]
    return _metrics(shapes, [shape.label for shape in shapes])


def verify_step_fresh(path: Path) -> dict[str, Any]:
    """Reimport one STEP in a new interpreter and return only its measured evidence."""
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "coalesce.export", "--verify", str(path)],
            check=False,
            stdin=subprocess.DEVNULL,
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


def _child(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _numbers_mismatch(expected: float, actual: float, path: str) -> str | None:
    matches = math.isclose(
        float(expected),
        float(actual),
        rel_tol=RELATIVE_TOLERANCE,
        abs_tol=ABSOLUTE_TOLERANCE,
    )
    return None if matches else path


def _mapping_mismatch(expected: dict, actual: dict, path: str) -> str | None:
    for key, value in expected.items():
        if key not in actual:
            return _child(path, key)
        if mismatch := _first_mismatch(value, actual[key], _child(path, key)):
            return mismatch
    extra = next((key for key in actual if key not in expected), None)
    return None if extra is None else _child(path, extra)


def _sequence_mismatch(expected: list, actual: list, path: str) -> str | None:
    if len(expected) != len(actual):
        return f"{path}.length"
    for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
        if mismatch := _first_mismatch(left, right, f"{path}[{index}]"):
            return mismatch
    return None


def _first_mismatch(expected: Any, actual: Any, path: str = "") -> str | None:
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return _numbers_mismatch(expected, actual, path)
    if isinstance(expected, dict) and isinstance(actual, dict):
        return _mapping_mismatch(expected, actual, path)
    if isinstance(expected, list) and isinstance(actual, list):
        return _sequence_mismatch(expected, actual, path)
    return None if expected == actual else path


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
        mismatch = _first_mismatch(_geometry(expected), _geometry(actual))
    except (KeyError, TypeError, ValueError):
        mismatch = "partCount"
    if mismatch is not None:
        raise ExportFailure(
            "EXPORT_VALIDATION_FAILED",
            "fresh-process STEP geometry does not match the source parts",
            hint="inspect the model and retry export to a different new path",
            mismatchedMetric=mismatch,
            tolerance={"relative": RELATIVE_TOLERANCE, "absolute": ABSOLUTE_TOLERANCE},
            expected=expected,
            actual=actual,
        )


def _validated_step_bytes(
    shapes: list[Any], expected: dict[str, Any], *, normalize: bool = False
) -> tuple[bytes, str, dict[str, Any]]:
    from build123d import Compound, export_step

    _silence_occt_messages()
    temporary_directory = Path(tempfile.mkdtemp())
    temporary_path = temporary_directory / "export.step"
    try:
        export_step(Compound(children=shapes, label="coalesce_export"), temporary_path)
        if normalize:
            temporary_path.write_bytes(_normalized_step_bytes(temporary_path.read_bytes()))
        actual = verify_step_fresh(temporary_path)
        _validate(expected, actual)
        content = temporary_path.read_bytes()
        return content, hashlib.sha256(content).hexdigest(), actual
    finally:
        shutil.rmtree(temporary_directory)


def _resolved_destination(path: Path) -> Path:
    if not path.is_absolute():
        raise ExportFailure(
            "EXPORT_WRITE_FAILED",
            "export path must be absolute",
            hint="provide an explicit absolute path whose parent directory already exists",
        )
    parent = Path(os.path.realpath(path.parent))
    if not parent.is_dir() or parent.stat().st_mode & 0o222 == 0:
        raise ExportFailure(
            "EXPORT_WRITE_FAILED",
            "export parent directory does not exist or is not writable",
            hint="create a writable parent directory and retry with a new path",
        )
    return parent / path.name


def _open_directory(path: Path) -> tuple[Path, int]:
    resolved_path = _resolved_destination(path)
    parent = resolved_path.parent
    try:
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as error:
        raise ExportFailure(
            "EXPORT_WRITE_FAILED",
            "export parent directory could not be opened",
            path=str(resolved_path),
            hint="check the target directory and retry with a new path",
        ) from error
    return resolved_path, directory_fd


def _publication_failure(
    path: Path, digest: str, byte_count: int
) -> ExportFailure:
    return ExportFailure(
        "EXPORT_WRITE_FAILED",
        "STEP export could not be written; the file at path may be incomplete; "
        "coalesce leaves it in place; delete it yourself or export to a new path.",
        path=str(path),
        expectedSha256=digest,
        expectedBytes=byte_count,
    )


def _existing_path_failure(path: Path) -> ExportFailure:
    if stat.S_ISDIR(path.lstat().st_mode):
        return ExportFailure(
            "EXPORT_PATH_EXISTS",
            f'export path {path} is a directory; use mode:"separate" to write one STEP per part '
            "into it, or provide a file path ending in .step for compound export",
            path=str(path),
            hint='use mode:"separate" for this directory or choose a new .step file path',
        )
    return ExportFailure(
        "EXPORT_PATH_EXISTS",
        "export path already exists; STEP export never overwrites",
        path=str(path),
        hint="choose a new path",
    )


def _publish_bytes(path: Path, content: bytes, digest: str) -> Path:
    path, directory_fd = _open_directory(path)
    descriptor: int | None = None
    created = False
    failure: Exception | None = None
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
            created = True
        except FileExistsError as error:
            raise _existing_path_failure(path) from error

        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("STEP export write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.fsync(directory_fd)
    except (ExportFailure, OSError) as error:
        failure = error

    for open_descriptor in (descriptor, directory_fd):
        if open_descriptor is None:
            continue
        try:
            os.close(open_descriptor)
        except OSError as error:
            failure = failure or error

    if failure is None:
        return path
    if isinstance(failure, ExportFailure):
        raise failure
    if created:
        raise _publication_failure(path, digest, len(content)) from failure
    raise failure


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


def _validated_part_bytes(
    parts: list[Any], *, normalize: bool = False
) -> tuple[bytes, str, dict[str, Any]]:
    from build123d import Solid

    shapes = [Solid(part.shape.wrapped, label=part.label) for part in parts]
    expected = _metrics(shapes, [part.label for part in parts])
    return _validated_step_bytes(shapes, expected, normalize=normalize)


def _validate_part_for_step(part: Any) -> None:
    _validated_part_bytes([part])


def _part_file_names(parts: list[Any]) -> list[str]:
    names: list[str] = []
    for part in parts:
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", part.label).strip("-.") or "part"
        name = f"{stem}.step"
        if name in names:
            suffix = hashlib.sha256(part.part_id.encode()).hexdigest()[:8]
            name = f"{stem}-{suffix}.step"
        names.append(name)
    return names


def _normalized_step_bytes(content: bytes) -> bytes:
    # OCCT emits wall-clock and process-counter metadata that would destabilize file hashes.
    text = content.decode("utf-8")
    text = re.sub(
        r"(?<=FILE_NAME\('coalesce_export',)'[^']*'",
        "'1970-01-01T00:00:00'",
        text,
        count=1,
    )
    occurrence = 0

    def normalize_occurrence(match: re.Match[str]) -> str:
        nonlocal occurrence
        occurrence += 1
        return f"{match.group(1)}'{occurrence}'{match.group(2)}"

    text = re.sub(
        r"(NEXT_ASSEMBLY_USAGE_OCCURRENCE\()'[^']*'([^;]*;)",
        normalize_occurrence,
        text,
    )
    return text.encode("utf-8")


def _prepare_part(part: Any, file_name: str) -> dict[str, Any]:
    content, digest, actual = _validated_part_bytes([part], normalize=True)
    actual_label = actual["parts"][0].get("label", "")
    if actual_label != part.label:
        raise ExportFailure(
            "EXPORT_VALIDATION_FAILED",
            "fresh-process STEP label does not match the source part",
            partId=part.part_id,
            expectedLabel=part.label,
            actualLabel=actual_label,
        )
    return {
        "partId": part.part_id,
        "label": part.label,
        "file": file_name,
        "sha256": digest,
        "status": "passed",
        "validation": {
            "status": "passed",
            "freshProcess": True,
            "label": actual_label,
            "reimport": actual,
        },
        "_content": content,
    }


def _failed_part(part: Any, file_name: str, error: ExportFailure) -> dict[str, Any]:
    validation = {
        "status": "failed",
        "freshProcess": True,
        "message": error.message,
        **error.details,
    }
    validation["code"] = error.code
    return {
        "partId": part.part_id,
        "label": part.label,
        "file": file_name,
        "status": "failed",
        "validation": validation,
        "_content": None,
    }


def prepare_parts(parts: list[Any]) -> list[dict[str, Any]]:
    """Prepare and validate every selected part exactly once without writing it."""
    prepared = []
    for part, file_name in zip(parts, _part_file_names(parts), strict=True):
        try:
            prepared.append(_prepare_part(part, file_name))
        except ExportFailure as error:
            prepared.append(_failed_part(part, file_name, error))
    return prepared


def public_parts(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in part.items() if not key.startswith("_")}
        for part in parts
    ]


def parts_are_ready(parts: list[dict[str, Any]]) -> bool:
    return all(part["status"] == "passed" for part in parts)


def _write_private_file(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _path_entries(path: Path) -> list[str]:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(mode):
        return [path.name]
    try:
        return sorted(entry.name for entry in path.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return [path.name]


def _destination_state(path: Path) -> tuple[Path, bool, list[str]]:
    resolved, directory_fd = _open_directory(path)
    os.close(directory_fd)
    try:
        mode = resolved.lstat().st_mode
    except FileNotFoundError:
        return resolved, False, []
    if not stat.S_ISDIR(mode):
        raise _existing_directory_failure(resolved, [resolved.name])
    return resolved, True, _path_entries(resolved)


def _existing_directory_failure(path: Path, entries: list[str]) -> ExportFailure:
    return ExportFailure(
        "EXPORT_PATH_EXISTS",
        "export path is not an empty real directory; choose an empty or new directory",
        path=str(path),
        entries=entries[:20],
        truncated=len(entries) > 20,
        hint="choose an empty or new directory",
    )


def _rename_new_directory(source: Path, destination: Path) -> None:
    if os.name == "nt":
        os.rename(source, destination)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if renamex := getattr(libc, "renamex_np", None):
        result = renamex(source_bytes, destination_bytes, 0x00000004)  # RENAME_EXCL
    elif renameat2 := getattr(libc, "renameat2", None):
        result = renameat2(-100, source_bytes, -100, destination_bytes, 1)  # RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, "exclusive directory rename is unavailable")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number), destination)
    raise OSError(error_number, os.strerror(error_number), destination)


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        raise OSError(errno.ENOTSUP, "directory fsync is unavailable on Windows")
    directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _publish_prepared_parts(
    path: Path, prepared: list[dict[str, Any]], manifest_bytes: bytes
) -> tuple[Path, list[dict[str, str]]]:
    """Publish prepared bytes with one atomic sibling-directory handoff."""
    destination, existed, entries = _destination_state(path)
    if entries:
        raise _existing_directory_failure(destination, entries)

    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent)
    )
    published = False
    try:
        for item in prepared:
            _write_private_file(temporary / item["file"], item["_content"])
        _write_private_file(temporary / "manifest.json", manifest_bytes)
        _sync_directory(temporary)
        try:
            if existed:
                os.rename(temporary, destination)
            else:
                _rename_new_directory(temporary, destination)
        except OSError as error:
            if error.errno in {errno.EEXIST, errno.ENOTDIR, errno.ENOTEMPTY}:
                raise _existing_directory_failure(
                    destination, _path_entries(destination)
                ) from error
            raise
        published = True
    except ExportFailure:
        raise
    except OSError as error:
        raise ExportFailure(
            "EXPORT_WRITE_FAILED",
            "separate-file export could not be published",
            path=str(destination),
            hint="check the target parent and retry with a new path",
        ) from error
    finally:
        if not published and temporary.exists():
            shutil.rmtree(temporary)

    warnings = []
    try:
        _sync_directory(destination.parent)
    except OSError:
        warnings.append({
            "code": "EXPORT_DURABILITY_UNCONFIRMED",
            "message": "files are published, but their durability on disk is not confirmed",
        })
    return destination, warnings


def export_parts_separately(
    path: Path, prepared: list[dict[str, Any]], manifest: dict[str, Any]
) -> dict[str, Any]:
    """Gate and publish one directory from records prepared exactly once."""
    failed_parts = [part["partId"] for part in prepared if part["status"] == "failed"]
    if failed_parts:
        raise ExportFailure(
            "EXPORT_VALIDATION_FAILED",
            "one or more parts failed fresh-process validation",
            failedParts=failed_parts,
            parts=public_parts(prepared),
        )
    manifest_bytes = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()
    # build every result field before the rename: no failure may follow the commit point
    part_ids = [part["partId"] for part in prepared]
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    destination, warnings = _publish_prepared_parts(path, prepared, manifest_bytes)
    result = {"path": str(destination), "partIds": part_ids, "manifestSha256": manifest_sha256}
    if warnings:
        result["warnings"] = warnings
    return result


def export_parts(path: Path, parts: list[Any]) -> dict[str, Any]:
    """Validate STEP bytes, then publish them once without replacement."""
    content, digest, actual = _validated_part_bytes(parts)
    if os.name == "nt":
        path, warnings = _publish_atomic_bytes(path, content)
    else:
        path, warnings = _publish_bytes(path, content, digest), []
    fields = _export_fields(path, parts, digest, actual)
    return {**fields, "byteCount": len(content), "warnings": warnings}


def _label_name(label: Any) -> str:
    from OCP.TDataStd import TDataStd_Name

    attribute = TDataStd_Name()
    if not label.FindAttribute(TDataStd_Name.GetID_s(), attribute):
        return ""
    return attribute.Get().ToExtString()


def _transform_evidence(location: Any) -> dict[str, Any]:
    from OCP.gp import gp_Vec

    transform = location.Transformation()
    translation = transform.TranslationPart()
    axis = gp_Vec()
    (angle,) = transform.GetRotation().GetVectorAndAngle(axis)
    if abs(angle) <= ABSOLUTE_TOLERANCE:
        axis_values = [0.0, 0.0, 1.0]
        angle = 0.0
    else:
        axis_values = [axis.X(), axis.Y(), axis.Z()]
    return {
        "translation": [translation.X(), translation.Y(), translation.Z()],
        "axis": axis_values,
        "angleDegrees": math.degrees(angle),
        "matrix": [transform.Value(row, column) for row in range(1, 4) for column in range(1, 5)],
    }


def _read_assembly_step(path: Path) -> dict[str, Any]:
    from build123d import Shape
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.collections import Sequence_TDF_Label
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDF import TDF_Label
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ShapeTool

    _silence_occt_messages()
    document = TDocStd_Document(TCollection_ExtendedString("XmlXCAF"))
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    if reader.ReadFile(str(path)) != IFSelect_ReturnStatus.IFSelect_RetDone or not reader.Transfer(
        document
    ):
        raise ValueError("STEP XCAF transfer failed")
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    roots = Sequence_TDF_Label()
    shape_tool.GetFreeShapes(roots)
    if roots.Size() != 1:
        return {"rootCount": roots.Size()}
    root = roots.Value(1)
    components = Sequence_TDF_Label()
    if not XCAFDoc_ShapeTool.GetComponents_s(root, components):
        raise ValueError("STEP root is not an assembly")

    occurrences = []
    definitions = []
    shapes = []
    valid = []
    for index in range(1, components.Size() + 1):
        component = components.Value(index)
        definition = TDF_Label()
        if not XCAFDoc_ShapeTool.GetReferredShape_s(component, definition):
            raise ValueError("STEP assembly component has no referred definition")
        if not any(definition.IsEqual(item) for item in definitions):
            definitions.append(definition)
        wrapped = XCAFDoc_ShapeTool.GetShape_s(component)
        shapes.append(Shape.cast(wrapped))
        valid.append(BRepCheck_Analyzer(wrapped).IsValid())
        occurrences.append(
            {
                "occurrenceId": _label_name(component),
                "definitionLabel": _label_name(definition),
                "transform": _transform_evidence(XCAFDoc_ShapeTool.GetLocation_s(component)),
            }
        )
    occurrences.sort(key=lambda item: item["occurrenceId"])
    geometry = _metrics(shapes, [item["occurrenceId"] for item in occurrences])
    geometry["allValid"] = all(valid)
    return {
        "rootCount": 1,
        "rootName": _label_name(root),
        "definitionCount": len(definitions),
        "occurrenceCount": len(occurrences),
        "occurrences": occurrences,
        "geometry": geometry,
    }


def verify_assembly_step_fresh(path: Path) -> dict[str, Any]:
    """Read an XCAF product tree in a new interpreter and return measured evidence."""
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "coalesce.export", "--verify-assembly", str(path)],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ExportFailure(
            "EXPORT_VALIDATION_FAILED",
            "the fresh Python process could not validate the STEP assembly",
        ) from error
    if completed.returncode != 0:
        raise ExportFailure(
            "EXPORT_VALIDATION_FAILED",
            "the STEP assembly could not be read through XCAF in a fresh Python process",
            verifierError=completed.stderr.strip()[-2000:],
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ExportFailure(
            "EXPORT_VALIDATION_FAILED",
            "the fresh-process XCAF verifier returned invalid evidence",
        ) from error


def _assembly_expectation(root_name: str, occurrences: list[Any]) -> dict[str, Any]:
    expected_rows = []
    for item in sorted(occurrences, key=lambda row: row.occurrence.id):
        expected_rows.append(
            {
                "occurrenceId": item.occurrence.id,
                "definitionLabel": item.part.part_id,
                "transform": _transform_evidence(item.shape.wrapped.Location()),
            }
        )
    geometry = _metrics(
        [item.shape for item in sorted(occurrences, key=lambda row: row.occurrence.id)],
        [item.occurrence.id for item in sorted(occurrences, key=lambda row: row.occurrence.id)],
    )
    geometry["allValid"] = True
    return {
        "rootCount": 1,
        "rootName": root_name,
        "definitionCount": len({item.part.part_id for item in occurrences}),
        "occurrenceCount": len(occurrences),
        "occurrences": expected_rows,
        "geometry": geometry,
    }


def write_assembly_step_bytes(
    root_name: str, occurrences: list[Any]
) -> tuple[bytes, str, dict[str, Any]]:
    """Build and fresh-process validate one XCAF assembly without publishing it."""
    from OCP.BRep import BRep_Builder
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopoDS import TopoDS_Compound
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    if not occurrences:
        raise ExportFailure(
            "ASSEMBLY_EXPORT_EMPTY", "assembly export has no manufacturing occurrences"
        )
    _silence_occt_messages()
    document = TDocStd_Document(TCollection_ExtendedString("XmlXCAF"))
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    shape_tool.SetAutoNaming_s(False)
    compound = TopoDS_Compound()
    BRep_Builder().MakeCompound(compound)
    root = shape_tool.AddShape(compound, True, False)
    TDataStd_Name.Set_s(root, TCollection_ExtendedString(root_name or "assembly"))
    from .assembly import definition_shape

    definitions = {}
    for item in sorted(occurrences, key=lambda row: row.part.part_id):
        if item.part.part_id not in definitions:
            definition = shape_tool.AddShape(
                definition_shape(item.part.shape).wrapped, False, False
            )
            TDataStd_Name.Set_s(definition, TCollection_ExtendedString(item.part.part_id))
            definitions[item.part.part_id] = definition
    for item in sorted(occurrences, key=lambda row: row.occurrence.id):
        component = shape_tool.AddComponent(
            root, definitions[item.part.part_id], item.shape.wrapped.Location()
        )
        TDataStd_Name.Set_s(component, TCollection_ExtendedString(item.occurrence.id))
    shape_tool.UpdateAssemblies()

    temporary_directory = Path(tempfile.mkdtemp())
    temporary_path = temporary_directory / "assembly.step"
    try:
        writer = STEPCAFControl_Writer()
        writer.SetNameMode(True)
        if (
            not writer.Transfer(document)
            or writer.Write(str(temporary_path)) != IFSelect_ReturnStatus.IFSelect_RetDone
        ):
            raise ExportFailure("EXPORT_VALIDATION_FAILED", "XCAF assembly STEP writing failed")
        content = _normalized_step_bytes(temporary_path.read_bytes())
        temporary_path.write_bytes(content)
        expected = _assembly_expectation(root_name or "assembly", occurrences)
        actual = verify_assembly_step_fresh(temporary_path)
        mismatch = _first_mismatch(expected, actual)
        if mismatch is not None:
            raise ExportFailure(
                "EXPORT_VALIDATION_FAILED",
                "fresh-process XCAF assembly evidence does not match the source assembly",
                mismatchedMetric=mismatch,
                expected=expected,
                actual=actual,
            )
        digest = hashlib.sha256(content).hexdigest()
        return content, digest, actual
    finally:
        shutil.rmtree(temporary_directory)


def _publish_atomic_bytes(path: Path, content: bytes) -> tuple[Path, list[dict[str, str]]]:
    destination = _resolved_destination(path)
    if destination.exists() or destination.is_symlink():
        raise _existing_path_failure(destination)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.tmp-", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    published = False
    try:
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _rename_new_directory(temporary, destination)
        published = True
    except FileExistsError as error:
        raise _existing_path_failure(destination) from error
    except OSError as error:
        raise ExportFailure(
            "EXPORT_WRITE_FAILED",
            "assembly STEP could not be published",
            path=str(destination),
            hint="check the target parent and retry with a new path",
        ) from error
    finally:
        if not published:
            temporary.unlink(missing_ok=True)

    warnings = []
    try:
        _sync_directory(destination.parent)
    except OSError:
        warnings.append(
            {
                "code": "EXPORT_DURABILITY_UNCONFIRMED",
                "message": "file is published, but its durability on disk is not confirmed",
            }
        )
    return destination, warnings


def export_assembly(path: Path, root_name: str, occurrences: list[Any]) -> dict[str, Any]:
    """Validate and atomically publish one XCAF assembly STEP."""
    content, digest, evidence = write_assembly_step_bytes(root_name, occurrences)
    destination, warnings = _publish_atomic_bytes(path, content)
    return {
        "path": str(destination),
        "warnings": warnings,
        "partIds": sorted({item.part.part_id for item in occurrences}),
        "occurrenceIds": sorted(item.occurrence.id for item in occurrences),
        "sha256": digest,
        "byteCount": len(content),
        "tolerance": {"relative": RELATIVE_TOLERANCE, "absolute": ABSOLUTE_TOLERANCE},
        "validation": {"geometry": {"status": "passed", "freshProcess": True}, "xcaf": evidence},
    }


def _main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in {"--verify", "--verify-assembly"}:
        raise SystemExit("usage: python -m coalesce.export --verify|--verify-assembly PATH")
    reader = _read_assembly_step if sys.argv[1] == "--verify-assembly" else _read_step_metrics
    print(json.dumps(reader(Path(sys.argv[2])), separators=(",", ":")))


if __name__ == "__main__":
    _main()
