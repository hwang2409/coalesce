import hashlib
import json
from pathlib import Path

import pytest
from test_mcp_vertical import run_server
from test_pcb_enclosure import FIXTURES, base_request, import_request, standoffs_request
from test_pcb_lid import lid_request

import coalesce.export as export_module
from coalesce.service import DocumentService


def _service(tmp_path: Path, *, include_lip=True) -> DocumentService:
    service = DocumentService(tmp_path / "handoff.coalesce.json")
    assert service.import_board(import_request(FIXTURES / "features.kicad_pcb"))["ok"]
    assert service.feature(base_request())["ok"]
    assert service.feature(standoffs_request())["ok"]
    assert service.feature(lid_request(include_lip=include_lip))["ok"]
    return service


def _preflight(**changes):
    return {
        "action": "preflight",
        "expectedRevision": 4,
        "base": "base",
        "requirements": [{"id": "valid", "kind": "valid_brep"}],
        **changes,
    }


def _separate(path: Path, **changes):
    return {
        "action": "publish",
        "mode": "separate",
        "expectedRevision": 4,
        "path": str(path),
        "base": "base",
        "requirements": [{"id": "valid", "kind": "valid_brep"}],
        **changes,
    }


def test_preflight_is_read_only_and_reports_each_part(tmp_path):
    service = _service(tmp_path)
    destination_parent = tmp_path / "delivery-parent"
    destination_parent.mkdir()
    before_tmp = sorted(tmp_path.iterdir())
    before_parent = sorted(destination_parent.iterdir())
    document_bytes = service.store.path.read_bytes()

    response = service.export(_preflight())

    assert response["ok"] is True
    assert response["ready"] is True
    assert response["requirements"]["gate"] == "PASS"
    assert response["unknowns"] == []
    assert [(part["label"], part["status"]) for part in response["parts"]] == [
        ("base", "passed"),
        ("lid", "passed"),
    ]
    assert response["partIds"] == [part["partId"] for part in response["parts"]]
    assert all(part["validation"]["freshProcess"] is True for part in response["parts"])
    assert sorted(tmp_path.iterdir()) == before_tmp
    assert sorted(destination_parent.iterdir()) == before_parent
    assert service.store.path.read_bytes() == document_bytes


@pytest.mark.anyio
async def test_real_stdio_separate_export_writes_two_parts_and_deterministic_manifest(tmp_path):
    document = tmp_path / "stdio-handoff.coalesce.json"
    first = tmp_path / "delivery-one"
    second = tmp_path / "delivery-two"
    calls = [
        ("import", import_request(FIXTURES / "features.kicad_pcb")),
        ("feature", base_request()),
        ("feature", standoffs_request()),
        ("feature", lid_request()),
        ("export", _separate(first)),
        ("export", _separate(second)),
    ]

    *_, first_response, second_response = await run_server(document, calls)

    assert first_response["ok"] is True
    assert first_response["ready"] is True
    assert second_response["ok"] is True
    assert sorted(path.name for path in first.iterdir()) == ["base.step", "lid.step", "manifest.json"]
    manifest = json.loads((first / "manifest.json").read_bytes())
    assert (first / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()
    assert manifest["revision"] == 4
    assert manifest["units"] == "mm"
    assert manifest["requirements"]["gate"] == "PASS"
    assert manifest["unknowns"] == []
    assert manifest["sources"] == [
        {
            "assetId": "controller_board",
            "sha256": hashlib.sha256((FIXTURES / "features.kicad_pcb").read_bytes()).hexdigest(),
        }
    ]
    assert [(part["label"], part["file"]) for part in manifest["parts"]] == [
        ("base", "base.step"),
        ("lid", "lid.step"),
    ]
    assert first_response["partIds"] == [part["partId"] for part in manifest["parts"]]
    for part in manifest["parts"]:
        content = (first / part["file"]).read_bytes()
        assert part["sha256"] == hashlib.sha256(content).hexdigest()
        assert part["validation"]["label"] == part["label"]
        assert part["validation"]["status"] == "passed"
    assert first_response["manifestSha256"] == hashlib.sha256(
        (first / "manifest.json").read_bytes()
    ).hexdigest()


def test_reference_board_is_not_a_separate_export_part(tmp_path):
    service = _service(tmp_path)
    destination = tmp_path / "delivery"

    response = service.export(_separate(destination))
    manifest = json.loads((destination / "manifest.json").read_bytes())

    assert response["partIds"] == [part["partId"] for part in manifest["parts"]]
    assert all(part["partId"] != "controller_board/board" for part in manifest["parts"])
    assert len(list(destination.glob("*.step"))) == 2


def test_unknown_requirement_is_recorded_and_never_ready(tmp_path):
    service = _service(tmp_path, include_lip=False)
    destination = tmp_path / "unknown-delivery"
    request = _separate(
        destination,
        requirements=[{
            "id": "fit",
            "kind": "fit_gap",
            "threshold": {
                "min": {"value": 0.1, "unit": "mm"},
                "max": {"value": 0.5, "unit": "mm"},
            },
        }]
    )

    response = service.export(request)

    assert response["ok"] is True
    assert response["ready"] is False
    assert response["requirements"]["gate"] == "UNKNOWN"
    assert response["unknowns"] == [response["requirements"]["results"][0]]
    manifest = json.loads((destination / "manifest.json").read_bytes())
    assert manifest["ready"] is False
    assert manifest["requirements"] == response["requirements"]
    assert manifest["unknowns"] == response["unknowns"]


def test_exclusive_directory_rename_never_replaces_an_existing_directory(tmp_path):
    source = tmp_path / "private"
    destination = tmp_path / "delivery"
    source.mkdir()
    destination.mkdir()
    (source / "new.txt").write_text("new")
    (destination / "keep.txt").write_text("keep")

    with pytest.raises(FileExistsError):
        export_module._rename_new_directory(source, destination)

    assert (source / "new.txt").read_text() == "new"
    assert (destination / "keep.txt").read_text() == "keep"


def test_existing_destination_is_structured_error_and_unchanged(tmp_path):
    service = _service(tmp_path)
    destination = tmp_path / "delivery"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("keep")
    before = sorted(tmp_path.iterdir())

    response = service.export(_separate(destination))

    assert response["error"]["code"] == "EXPORT_PATH_EXISTS"
    assert sentinel.read_text() == "keep"
    assert sorted(tmp_path.iterdir()) == before
    assert not list(tmp_path.glob(".delivery.tmp-*"))


def test_validation_failure_publishes_nothing_and_leaves_no_sibling_temp(tmp_path, monkeypatch):
    service = _service(tmp_path)
    destination = tmp_path / "delivery"

    def fail(_parts, **_kwargs):
        raise export_module.ExportFailure("EXPORT_VALIDATION_FAILED", "injected")

    monkeypatch.setattr(export_module, "_validated_part_bytes", fail)
    response = service.export(_separate(destination))

    assert response["error"]["code"] == "EXPORT_VALIDATION_FAILED"
    assert not destination.exists()
    assert not list(tmp_path.glob(".delivery.tmp-*"))


def test_destination_created_at_rename_is_structured_and_empty(tmp_path, monkeypatch):
    service = _service(tmp_path)
    destination = tmp_path / "delivery"
    original = export_module._rename_new_directory

    def race(source, target):
        target.mkdir()
        original(source, target)

    monkeypatch.setattr(export_module, "_rename_new_directory", race)
    response = service.export(_separate(destination))

    assert response["error"]["code"] == "EXPORT_PATH_EXISTS"
    assert list(destination.iterdir()) == []
    assert not list(tmp_path.glob(".delivery.tmp-*"))


def test_staging_directory_fsync_failure_publishes_nothing(tmp_path, monkeypatch):
    service = _service(tmp_path)
    destination = tmp_path / "delivery"
    original = export_module.os.fsync
    calls = 0

    def fail_staging(descriptor):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("injected staging fsync failure")
        return original(descriptor)

    monkeypatch.setattr(export_module.os, "fsync", fail_staging)
    response = service.export(_separate(destination))

    assert response["error"]["code"] == "EXPORT_WRITE_FAILED"
    assert not destination.exists()
    assert not list(tmp_path.glob(".delivery.tmp-*"))


def test_rename_failure_publishes_nothing(tmp_path, monkeypatch):
    service = _service(tmp_path)
    destination = tmp_path / "delivery"
    def fail_rename(*_args):
        raise OSError("injected rename failure")

    monkeypatch.setattr(export_module, "_rename_new_directory", fail_rename)
    response = service.export(_separate(destination))

    assert response["error"]["code"] == "EXPORT_WRITE_FAILED"
    assert not destination.exists()
    assert not list(tmp_path.glob(".delivery.tmp-*"))


def test_write_failure_publishes_nothing_and_leaves_no_sibling_temp(tmp_path, monkeypatch):
    service = _service(tmp_path)
    destination = tmp_path / "delivery"
    writes = 0
    temporary_parents = []
    real_write = export_module._write_private_file

    def fail_second(path, content):
        nonlocal writes
        writes += 1
        temporary_parents.append(path.parent)
        if writes == 2:
            raise OSError("injected write failure")
        return real_write(path, content)

    monkeypatch.setattr(export_module, "_write_private_file", fail_second)
    response = service.export(_separate(destination))

    assert response["error"]["code"] == "EXPORT_WRITE_FAILED"
    assert all(temporary.parent == destination.parent for temporary in temporary_parents)
    assert not destination.exists()
    assert not list(tmp_path.glob(".delivery.tmp-*"))
