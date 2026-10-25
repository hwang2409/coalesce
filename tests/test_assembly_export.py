import hashlib

import pytest
from build123d import Box, Cylinder

from coalesce import export as export_module
from coalesce.assembly import Occurrence, ResolvedOccurrence, RigidTransform, locate_shape
from coalesce.export import (
    ExportFailure,
    export_assembly,
    verify_assembly_step_fresh,
    write_assembly_step_bytes,
)
from coalesce.parts import TerminalPart
from coalesce.references import BodySource


def _resolved():
    definitions = {
        "disc/body": TerminalPart(
            "disc/body",
            "disc",
            "manufacturing",
            BodySource("disc", "body"),
            "disc",
            Box(8, 6, 2).solids()[0],
        ),
        "shaft/body": TerminalPart(
            "shaft/body",
            "shaft",
            "manufacturing",
            BodySource("shaft", "body"),
            "shaft",
            Box(2, 3, 7).solids()[0],
        ),
        "pin/body": TerminalPart(
            "pin/body",
            "pin",
            "manufacturing",
            BodySource("pin", "body"),
            "pin",
            Cylinder(1, 4).solids()[0],
        ),
    }
    rows = [
        ("disc", "disc/body", RigidTransform((2, 0, 0), (0, 0, 1), 0)),
        ("shaft", "shaft/body", RigidTransform((0, 0, 0), (0, 1, 0), 90)),
    ]
    rows.extend(
        (f"ring-pin-{index}", "pin/body", RigidTransform((index * 4, 10, 0), (0, 0, 1), 0))
        for index in range(1, 9)
    )
    return [
        ResolvedOccurrence(
            Occurrence(name, part_id, transform),
            definitions[part_id],
            locate_shape(definitions[part_id].shape, transform),
        )
        for name, part_id, transform in rows
    ]


def test_xcaf_assembly_round_trip_preserves_tree_references_names_and_placements(tmp_path):
    content, digest, evidence = write_assembly_step_bytes("cycloidal-drive-assembly", _resolved())
    path = tmp_path / "drive.step"
    path.write_bytes(content)

    fresh = verify_assembly_step_fresh(path)
    assert digest == hashlib.sha256(content).hexdigest()
    assert fresh == evidence
    assert fresh["rootCount"] == 1
    assert fresh["rootName"] == "cycloidal-drive-assembly"
    assert fresh["definitionCount"] == 3
    assert fresh["occurrenceCount"] == 10
    assert {item["occurrenceId"] for item in fresh["occurrences"]} == {
        "disc",
        "shaft",
        *(f"ring-pin-{index}" for index in range(1, 9)),
    }
    pins = [item for item in fresh["occurrences"] if item["occurrenceId"].startswith("ring-pin-")]
    assert {item["definitionLabel"] for item in pins} == {"pin/body"}
    shaft = next(item for item in fresh["occurrences"] if item["occurrenceId"] == "shaft")
    assert shaft["transform"]["translation"] == pytest.approx([0, 0, 0], abs=1e-7)
    assert shaft["transform"]["angleDegrees"] == pytest.approx(90, abs=1e-7)
    assert shaft["transform"]["axis"] == pytest.approx([0, 1, 0], abs=1e-7)
    assert fresh["geometry"]["solidCount"] == 10
    assert fresh["geometry"]["allValid"] is True


def test_assembly_publish_parent_fsync_failure_returns_durability_warning(tmp_path, monkeypatch):
    destination = tmp_path / "drive.step"

    def fail(_path):
        raise OSError("injected parent fsync failure")

    monkeypatch.setattr(export_module, "_sync_directory", fail)
    published, warnings = export_module._publish_atomic_bytes(destination, b"assembly")

    assert published == destination
    assert destination.read_bytes() == b"assembly"
    assert warnings == [
        {
            "code": "EXPORT_DURABILITY_UNCONFIRMED",
            "message": "file is published, but its durability on disk is not confirmed",
        }
    ]
    assert list(tmp_path.iterdir()) == [destination]


def test_assembly_export_validation_failure_publishes_nothing(tmp_path, monkeypatch):
    destination = tmp_path / "drive.step"

    def fail(_path):
        raise ExportFailure("EXPORT_VALIDATION_FAILED", "injected")

    monkeypatch.setattr("coalesce.export.verify_assembly_step_fresh", fail)
    with pytest.raises(ExportFailure, match="injected"):
        export_assembly(destination, "drive", _resolved())
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []
