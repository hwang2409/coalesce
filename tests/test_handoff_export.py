import hashlib
import json
from pathlib import Path

import pytest
from test_mcp_vertical import run_server
from test_pcb_enclosure import FIXTURES, base_request, import_request, standoffs_request
from test_pcb_lid import lid_request

import coalesce.export as export_module
import coalesce.service as service_module
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


def test_preflight_reports_failed_parts_and_is_not_ready(tmp_path, monkeypatch):
    service = _service(tmp_path)
    original = export_module._validated_part_bytes

    def fail_lid(parts, **kwargs):
        if parts[0].label == "lid":
            raise export_module.ExportFailure(
                "EXPORT_VALIDATION_FAILED", "injected", mismatchedMetric="volume"
            )
        return original(parts, **kwargs)

    monkeypatch.setattr(export_module, "_validated_part_bytes", fail_lid)
    response = service.export(_preflight())

    assert response["ok"] is True
    assert response["ready"] is False
    assert [(part["label"], part["status"]) for part in response["parts"]] == [
        ("base", "passed"),
        ("lid", "failed"),
    ]
    assert response["parts"][1]["validation"]["mismatchedMetric"] == "volume"


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
    first.mkdir()
    second.mkdir()
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


def test_existing_destination_entries_are_bounded_sorted_and_unchanged(tmp_path):
    service = _service(tmp_path)
    destination = tmp_path / "delivery"
    destination.mkdir()
    entries = [destination / f".{index:02}.txt" for index in reversed(range(21))]
    for entry in entries:
        entry.write_text("keep")

    response = service.export(_separate(destination))

    assert response["error"]["code"] == "EXPORT_PATH_EXISTS"
    assert response["error"]["entries"] == sorted(entry.name for entry in entries)[:20]
    assert response["error"]["truncated"] is True
    assert all(entry.read_text() == "keep" for entry in entries)
    assert not list(tmp_path.glob(".delivery.tmp-*"))


def _publish_test_files(destination):
    prepared = [
        {"file": "one.step", "_content": b"one"},
        {"file": "two.step", "_content": b"two"},
    ]
    manifest = b'{"parts":[{"file":"one.step"},{"file":"two.step"}]}\n'
    return export_module._publish_prepared_parts(destination, prepared, manifest)


def test_fault_after_staging_second_part_keeps_existing_directory_unchanged(tmp_path, monkeypatch):
    destination = tmp_path / "delivery"
    destination.mkdir()
    inode = destination.stat().st_ino
    original = export_module._write_private_file

    def fail_after_second(path, content):
        original(path, content)
        if path.name == "two.step":
            raise OSError("injected failure after second staged part")

    monkeypatch.setattr(export_module, "_write_private_file", fail_after_second)
    with pytest.raises(export_module.ExportFailure, match="could not be published"):
        _publish_test_files(destination)

    assert (destination.stat().st_ino, list(destination.iterdir())) == (inode, [])
    assert not list(tmp_path.glob(".delivery.tmp-*"))


@pytest.mark.parametrize("race", ["file", "symlink", "late"])
def test_target_change_after_preflight_blocks_atomic_publish(tmp_path, monkeypatch, race):
    destination = tmp_path / "delivery"
    destination.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()

    if race == "late":
        original = export_module._sync_directory

        def inject(path):
            (destination / "late.txt").write_text("keep")
            original(path)

        monkeypatch.setattr(export_module, "_sync_directory", inject)
    else:
        original = export_module.os.rename

        def inject(source, target):
            if race == "file":
                (destination / "race.txt").write_text("keep")
            else:
                destination.rmdir()
                destination.symlink_to(foreign, target_is_directory=True)
            return original(source, target)

        monkeypatch.setattr(export_module.os, "rename", inject)

    with pytest.raises(export_module.ExportFailure) as failure:
        _publish_test_files(destination)

    assert failure.value.code == "EXPORT_PATH_EXISTS"
    assert list(foreign.iterdir()) == []
    assert not list(tmp_path.glob(".delivery.tmp-*"))
    if race != "symlink":
        name = "race.txt" if race == "file" else "late.txt"
        assert failure.value.details["entries"] == [name]
        assert (destination / name).read_text() == "keep"


def test_manifest_is_never_visible_without_all_parts(tmp_path, monkeypatch):
    destination = tmp_path / "delivery"
    destination.mkdir()
    original = export_module.os.rename
    observations = []

    def observe(source, target):
        observations.append(set(destination.iterdir()))
        result = original(source, target)
        observations.append({path.name for path in destination.iterdir()})
        return result

    monkeypatch.setattr(export_module.os, "rename", observe)
    _publish_test_files(destination)

    assert observations == [set(), {"one.step", "two.step", "manifest.json"}]
    assert all("manifest.json" not in names or len(names) == 3 for names in observations)


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


def test_one_time_lid_validation_failure_blocks_separate_export(tmp_path, monkeypatch):
    service = _service(tmp_path)
    destination = tmp_path / "delivery"
    original = export_module._validated_part_bytes
    failed = False

    def fail_lid_once(parts, **kwargs):
        nonlocal failed
        if parts[0].label == "lid" and not failed:
            failed = True
            raise export_module.ExportFailure(
                "EXPORT_VALIDATION_FAILED", "injected", mismatchedMetric="volume"
            )
        return original(parts, **kwargs)

    monkeypatch.setattr(export_module, "_validated_part_bytes", fail_lid_once)
    response = service.export(_separate(destination))

    assert response["ok"] is False
    assert response["error"]["code"] == "EXPORT_VALIDATION_FAILED"
    failed_parts = [part for part in response["error"]["parts"] if part["status"] == "failed"]
    assert [part["label"] for part in failed_parts] == ["lid"]
    assert response["error"]["failedParts"] == [failed_parts[0]["partId"]]
    assert not destination.exists()
    assert not list(tmp_path.glob(".delivery.tmp-*"))


@pytest.mark.parametrize("action", ["preflight", "separate"])
def test_handoff_validates_each_part_once(tmp_path, monkeypatch, action):
    service = _service(tmp_path)
    original = export_module._validated_part_bytes
    calls = []

    def count(parts, **kwargs):
        calls.append(parts[0].label)
        return original(parts, **kwargs)

    monkeypatch.setattr(export_module, "_validated_part_bytes", count)
    request = _preflight() if action == "preflight" else _separate(tmp_path / "delivery")

    assert service.export(request)["ok"] is True
    assert calls == ["base", "lid"]


def test_manifest_part_statuses_and_ready_match_response(tmp_path, monkeypatch):
    service = _service(tmp_path, include_lip=False)
    destination = tmp_path / "delivery"
    original = export_module._validated_part_bytes
    calls = []

    def count(parts, **kwargs):
        calls.append(parts[0].label)
        return original(parts, **kwargs)

    monkeypatch.setattr(export_module, "_validated_part_bytes", count)
    response = service.export(
        _separate(
            destination,
            requirements=[{
                "id": "fit",
                "kind": "fit_gap",
                "threshold": {
                    "min": {"value": 0.1, "unit": "mm"},
                    "max": {"value": 0.5, "unit": "mm"},
                },
            }],
        )
    )
    manifest = json.loads((destination / "manifest.json").read_bytes())

    assert manifest["ready"] == response["ready"]
    assert [part["status"] for part in manifest["parts"]] == [
        part["status"] for part in response["parts"]
    ]
    assert calls == ["base", "lid"]


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


@pytest.mark.parametrize("existing", [False, True])
def test_parent_fsync_failure_after_rename_returns_success_with_warning(
    tmp_path, monkeypatch, existing
):
    destination = tmp_path / "delivery"
    if existing:
        destination.mkdir()
    prepared = [
        {"partId": "one", "status": "passed", "file": "one.step", "_content": b"one"},
        {"partId": "two", "status": "passed", "file": "two.step", "_content": b"two"},
    ]
    original = export_module._sync_directory

    def fail_parent_sync(path):
        if path == destination.parent:
            raise OSError("injected parent fsync failure")
        original(path)

    monkeypatch.setattr(export_module, "_sync_directory", fail_parent_sync)
    monkeypatch.setattr(service_module, "prepare_parts", lambda _selected: prepared)
    service = object.__new__(DocumentService)
    service._document = type("Document", (), {"revision": 4})()
    service._handoff_result = lambda _prepared, _payload: {
        "parts": export_module.public_parts(prepared),
        "requirements": {"gate": "PASS", "results": []},
        "unknowns": [],
        "ready": True,
    }
    service._handoff_sources = list
    result = service._export_separate([], {"path": str(destination)})

    assert result["ok"] is True
    assert result["warnings"] == [{
        "code": "EXPORT_DURABILITY_UNCONFIRMED",
        "message": "files are published, but their durability on disk is not confirmed",
    }]
    assert sorted(path.name for path in destination.iterdir()) == [
        "manifest.json",
        "one.step",
        "two.step",
    ]
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
