import hashlib
import os
import subprocess
from pathlib import Path

import pytest
from test_mcp_cut_hole_circle import cut, extrude, rectangle_sketch
from test_mcp_join_holes_frames import join_extrude, patterned_hole
from test_mcp_vertical import call_server_tool, list_server_tools, run_server

import coalesce.export as export_module
from coalesce.server import TOOLS_GUIDE
from coalesce.service import DocumentService


def enclosure_calls():
    return [
        (
            "sketch",
            rectangle_sketch(
                request_id="plate-sketch",
                revision=0,
                sketch_id="plate_sketch",
                entity_id="plate",
                width=60,
                height=40,
            ),
        ),
        (
            "feature",
            extrude(
                request_id="plate",
                revision=1,
                feature_id="plate",
                sketch_id="plate_sketch",
                profile_ids=["plate"],
                distance=3,
            ),
        ),
        (
            "sketch",
            {
                "action": "create",
                "expectedRevision": 2,
                "requestId": "wall-sketch",
                "sketchId": "wall_sketch",
                "plane": {
                    "kind": "origin",
                    "name": "XY",
                    "offset": {"value": 3, "unit": "mm"},
                },
                "entities": [
                    {
                        "id": "outer",
                        "kind": "rectangle",
                        "width": {"value": 60, "unit": "mm"},
                        "height": {"value": 40, "unit": "mm"},
                    },
                    {
                        "id": "inner",
                        "kind": "rectangle",
                        "width": {"value": 54, "unit": "mm"},
                        "height": {"value": 34, "unit": "mm"},
                    },
                ],
            },
        ),
        (
            "feature",
            join_extrude(
                request_id="wall",
                revision=3,
                feature_id="wall",
                sketch_id="wall_sketch",
                profile_ids=["outer", "inner"],
                distance=7,
                target="plate",
            ),
        ),
        (
            "feature",
            patterned_hole(
                request_id="holes",
                revision=4,
                feature_id="mounting_holes",
                target="wall",
                positions=[
                    {"name": "nw", "point": [-25, 15]},
                    {"name": "ne", "point": [25, 15]},
                    {"name": "sw", "point": [-25, -15]},
                    {"name": "se", "point": [25, -15]},
                ],
            ),
        ),
        (
            "sketch",
            rectangle_sketch(
                request_id="lid-sketch",
                revision=5,
                sketch_id="lid_sketch",
                entity_id="lid",
                width=58,
                height=38,
                offset=12,
            ),
        ),
        (
            "feature",
            extrude(
                request_id="lid",
                revision=6,
                feature_id="lid",
                sketch_id="lid_sketch",
                profile_ids=["lid"],
                distance=2,
            ),
        ),
    ]


def export_request(path, part_ids=None, revision=7):
    request = {"expectedRevision": revision, "path": str(path)}
    if part_ids is not None:
        request["partIds"] = part_ids
    return request


@pytest.mark.anyio
async def test_step_export_success_is_stateless(tmp_path):
    document = tmp_path / "enclosure.coalesce.json"
    target = tmp_path / "enclosure.step"
    await run_server(document, enclosure_calls())
    document_bytes = document.read_bytes()

    exported = (await run_server(document, [("export", export_request(target))]))[0]

    content = target.read_bytes()
    assert exported["ok"] is True
    assert exported["revision"] == 7
    assert exported["partIds"] == ["plate/plate", "lid/lid"]
    assert exported["path"] == str(target)
    assert exported["sha256"] == hashlib.sha256(content).hexdigest()
    assert exported["byteCount"] == len(content)
    assert exported["validation"]["geometry"] == {
        "status": "passed",
        "freshProcess": True,
    }
    assert exported["validation"]["reimport"]["partCount"] == 2
    assert exported["validation"]["reimport"]["solidCount"] == 2
    assert document.read_bytes() == document_bytes
    assert not list(tmp_path.glob("*.exports.jsonl"))


@pytest.mark.anyio
async def test_step_export_excludes_pcb_reference_parts_like_default_measurements(tmp_path):
    document = tmp_path / "pcb-enclosure.coalesce.json"
    target = tmp_path / "plate.step"
    fixture = Path(__file__).parent / "fixtures/kicad/features.kicad_pcb"
    calls = [
        (
            "import",
            {
                "action": "create",
                "expectedRevision": 0,
                "requestId": "import-board",
                "assetId": "controller_board",
                "format": "kicad_pcb",
                "path": str(fixture.resolve()),
            },
        ),
        (
            "sketch",
            rectangle_sketch(
                request_id="plate-sketch",
                revision=1,
                sketch_id="plate_sketch",
                entity_id="plate",
                width=60,
                height=40,
            ),
        ),
        (
            "feature",
            extrude(
                request_id="plate",
                revision=2,
                feature_id="plate",
                sketch_id="plate_sketch",
                profile_ids=["plate"],
                distance=3,
            ),
        ),
        ("query", {"select": {"kind": "measurements"}}),
        ("export", export_request(target, revision=3)),
    ]

    *_, measured, exported = await run_server(document, calls)

    assert [part["partId"] for part in measured["parts"]] == ["plate/plate"]
    assert exported["ok"] is True
    assert exported["partIds"] == ["plate/plate"]
    assert exported["validation"]["reimport"]["partCount"] == 1
    assert exported["validation"]["reimport"]["solidCount"] == 1


@pytest.mark.anyio
async def test_assembly_export_uses_occurrences_and_lists_reference_exclusions(tmp_path):
    document = tmp_path / "assembly.coalesce.json"
    target = tmp_path / "assembly.step"
    fixture = Path(__file__).parent / "fixtures/kicad/features.kicad_pcb"

    def occurrence(revision, occurrence_id, part_id, x):
        return {
            "action": "create",
            "kind": "assembly_occurrence",
            "expectedRevision": revision,
            "requestId": f"place-{occurrence_id}",
            "occurrenceId": occurrence_id,
            "parameters": {
                "partId": part_id,
                "transform": {
                    "translation": {"value": [x, 0, 0], "unit": "mm"},
                    "rotation": {
                        "axis": [0, 1, 0],
                        "angle": {"value": 90 if x else 0, "unit": "deg"},
                    },
                },
            },
        }

    calls = [
        (
            "import",
            {
                "action": "create",
                "expectedRevision": 0,
                "requestId": "board",
                "assetId": "controller_board",
                "format": "kicad_pcb",
                "path": str(fixture.resolve()),
            },
        ),
        (
            "sketch",
            rectangle_sketch(
                request_id="plate-sketch",
                revision=1,
                sketch_id="plate_sketch",
                entity_id="plate",
                width=20,
                height=10,
            ),
        ),
        (
            "feature",
            extrude(
                request_id="plate",
                revision=2,
                feature_id="plate",
                sketch_id="plate_sketch",
                profile_ids=["plate"],
                distance=3,
            ),
        ),
        ("feature", occurrence(3, "plate-a", "plate/plate", 0)),
        ("feature", occurrence(4, "plate-b", "plate/plate", 30)),
        (
            "feature",
            occurrence(
                5,
                "board-reference",
                "controller_board/loop-1e9db439140237c1f6f93e94",
                0,
            ),
        ),
        ("export", {"action": "preflight", "mode": "assembly", "expectedRevision": 6}),
        (
            "export",
            {
                "action": "publish",
                "mode": "assembly",
                "expectedRevision": 6,
                "path": str(target),
            },
        ),
    ]
    *_, preflight, published = await run_server(document, calls)

    assert preflight["ok"] is True
    assert preflight["excludedOccurrenceIds"] == ["board-reference"]
    assert preflight["occurrenceIds"] == ["plate-a", "plate-b"]
    assert preflight["validation"]["xcaf"]["definitionCount"] == 1
    assert preflight["validation"]["xcaf"]["occurrenceCount"] == 2
    assert published["excludedOccurrenceIds"] == ["board-reference"]
    assert published["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()


@pytest.mark.anyio
async def test_step_export_accepts_terminal_feature_id(tmp_path):
    document = tmp_path / "feature-id.coalesce.json"
    target = tmp_path / "plate.step"
    await run_server(document, enclosure_calls())

    result = (
        await run_server(
            document,
            [("export", export_request(target, part_ids=["mounting_holes"]))],
        )
    )[0]

    assert result["ok"] is True
    assert result["partIds"] == ["plate/plate"]


@pytest.mark.anyio
async def test_step_export_ambiguous_feature_lists_valid_parts_and_builders(tmp_path):
    document = tmp_path / "split.coalesce.json"
    target = tmp_path / "split.step"
    calls = [
        (
            "sketch",
            rectangle_sketch(
                request_id="plate-sketch",
                revision=0,
                sketch_id="plate_sketch",
                entity_id="plate",
                width=60,
                height=40,
            ),
        ),
        (
            "feature",
            extrude(
                request_id="plate",
                revision=1,
                feature_id="plate",
                sketch_id="plate_sketch",
                profile_ids=["plate"],
                distance=3,
            ),
        ),
        (
            "sketch",
            rectangle_sketch(
                request_id="slot-sketch",
                revision=2,
                sketch_id="slot_sketch",
                entity_id="slot",
                width=10,
                height=50,
            ),
        ),
        (
            "feature",
            cut(
                request_id="split",
                revision=3,
                feature_id="split",
                target="plate",
                tool="slot_sketch",
            ),
        ),
    ]
    await run_server(document, calls)

    result = (
        await run_server(
            document,
            [("export", export_request(target, part_ids=["split"], revision=4))],
        )
    )[0]

    assert result["error"]["code"] == "UNKNOWN_ID"
    assert result["error"]["unknownPartIds"] == ["split"]
    assert len(result["error"]["validParts"]) == 2
    assert all(part["partId"].startswith("plate/plate#") for part in result["error"]["validParts"])
    assert {part["featureId"] for part in result["error"]["validParts"]} == {"split"}


@pytest.mark.anyio
async def test_step_export_selected_part(tmp_path):
    document = tmp_path / "selected.coalesce.json"
    target = tmp_path / "plate.step"
    await run_server(document, enclosure_calls())

    result = (
        await run_server(
            document,
            [("export", export_request(target, part_ids=["plate/plate"]))],
        )
    )[0]

    assert result["partIds"] == ["plate/plate"]
    assert result["validation"]["reimport"]["partCount"] == 1
    assert result["validation"]["reimport"]["solidCount"] == 1


@pytest.mark.anyio
async def test_export_through_symlinked_parent_reports_resolved_path(tmp_path):
    document = tmp_path / "symlink-parent.coalesce.json"
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    target = linked_parent / "export.step"
    await run_server(document, enclosure_calls())

    result = (await run_server(document, [("export", export_request(target))]))[0]

    assert result["ok"] is True
    assert result["path"] == str(real_parent / "export.step")
    assert (real_parent / "export.step").exists()


@pytest.mark.anyio
async def test_export_dangling_parent_is_structured_error(tmp_path):
    document = tmp_path / "dangling-parent.coalesce.json"
    dangling_parent = tmp_path / "dangling-parent"
    dangling_parent.symlink_to(tmp_path / "missing-parent", target_is_directory=True)
    target = dangling_parent / "export.step"
    await run_server(document, enclosure_calls())

    result = (await run_server(document, [("export", export_request(target))]))[0]

    assert result["error"]["code"] == "EXPORT_WRITE_FAILED"
    assert "parent directory does not exist or is not writable" in result["error"]["message"]
    assert not target.exists()


@pytest.mark.anyio
async def test_compound_export_existing_directory_explains_modes(tmp_path):
    from evals.golden.revolve_shaft_common import profile_entities, stage_shaft

    document = tmp_path / "shaft.coalesce.json"
    service = DocumentService(document)
    stage_shaft(service)
    assert service.sketch({
        "action": "update",
        "expectedRevision": 3,
        "requestId": "golden-diameter-update",
        "sketchId": "profile",
        "set": {"entities": profile_entities(8)},
    })["revision"] == 4
    destination = tmp_path / "out"
    destination.mkdir()

    result = await call_server_tool(service, "export", {
        "action": "publish",
        "expectedRevision": 4,
        "partIds": ["shaft"],
        "path": str(destination),
    })

    assert result["error"]["code"] == "EXPORT_PATH_EXISTS"
    assert result["error"]["message"] == (
        f'export path {destination} is a directory; use mode:"separate" to write one STEP per part '
        "into it, or provide a file path ending in .step for compound export"
    )
    assert list(destination.iterdir()) == []


@pytest.mark.anyio
async def test_export_refuses_existing_path_and_symlink_untouched(tmp_path):
    document = tmp_path / "existing.coalesce.json"
    await run_server(document, enclosure_calls())
    existing = tmp_path / "existing.step"
    existing.write_bytes(b"keep me")
    symlink_target = tmp_path / "foreign.step"
    symlink_target.write_bytes(b"foreign")
    symlink = tmp_path / "linked.step"
    symlink.symlink_to(symlink_target)

    results = await run_server(
        document,
        [
            ("export", export_request(existing)),
            ("export", export_request(symlink)),
        ],
    )

    assert [result["error"]["code"] for result in results] == [
        "EXPORT_PATH_EXISTS",
        "EXPORT_PATH_EXISTS",
    ]
    assert existing.read_bytes() == b"keep me"
    assert symlink.is_symlink()
    assert symlink_target.read_bytes() == b"foreign"


@pytest.mark.anyio
async def test_export_refuses_stale_revision(tmp_path):
    document = tmp_path / "stale.coalesce.json"
    target = tmp_path / "stale.step"
    await run_server(document, enclosure_calls())

    result = (
        await run_server(document, [("export", export_request(target, revision=6))])
    )[0]

    assert result["error"]["code"] == "STALE_REVISION"
    assert result["error"]["retryable"] is False
    assert not target.exists()


@pytest.mark.anyio
async def test_export_rejects_request_id(tmp_path):
    document = tmp_path / "request-id.coalesce.json"
    await run_server(document, enclosure_calls())

    result = (
        await run_server(
            document,
            [("export", {**export_request(tmp_path / "part.step"), "requestId": "old"})],
        )
    )[0]

    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert "unknown field 'requestId'" in result["error"]["message"]


@pytest.mark.anyio
async def test_validation_failure_removes_private_directory(tmp_path, monkeypatch):
    document = tmp_path / "validation.coalesce.json"
    target = tmp_path / "validation.step"
    private = tmp_path / "private-validation"
    await run_server(document, enclosure_calls())

    def make_temporary_directory():
        private.mkdir(mode=0o700)
        return str(private)

    def fail_validation(_path):
        raise export_module.ExportFailure(
            "EXPORT_VALIDATION_FAILED", "injected validation failure"
        )

    monkeypatch.setattr(export_module.tempfile, "mkdtemp", make_temporary_directory)
    monkeypatch.setattr(export_module, "verify_step_fresh", fail_validation)
    result = await call_server_tool(
        DocumentService(document), "export", export_request(target)
    )

    assert result["error"]["code"] == "EXPORT_VALIDATION_FAILED"
    assert not private.exists()
    assert not target.exists()


@pytest.mark.anyio
async def test_fresh_process_timeout_is_structured_and_removes_private_directory(
    tmp_path, monkeypatch
):
    document = tmp_path / "timeout.coalesce.json"
    target = tmp_path / "timeout.step"
    private = tmp_path / "private-validation"
    await run_server(document, enclosure_calls())

    def make_temporary_directory():
        private.mkdir(mode=0o700)
        return str(private)

    def time_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("STEP verifier", 120)

    monkeypatch.setattr(export_module.tempfile, "mkdtemp", make_temporary_directory)
    monkeypatch.setattr(export_module.subprocess, "run", time_out)
    result = await call_server_tool(
        DocumentService(document), "export", export_request(target)
    )

    assert result["error"]["code"] == "EXPORT_VALIDATION_FAILED"
    assert not private.exists()
    assert not target.exists()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["write", "file_fsync", "directory_fsync"])
async def test_publication_failure_leaves_file_with_recovery_fields(
    tmp_path, monkeypatch, failure
):
    document = tmp_path / f"{failure}.coalesce.json"
    target = tmp_path / f"{failure}.step"
    await run_server(document, enclosure_calls())
    original_write = os.write
    original_fsync = os.fsync
    step_fd = None

    def fail_write(descriptor, data):
        nonlocal step_fd
        if bytes(data[:16]).startswith(b"ISO-10303"):
            step_fd = descriptor
            original_write(descriptor, data[: max(1, len(data) // 2)])
            raise OSError("injected write failure")
        return original_write(descriptor, data)

    def fail_fsync(descriptor):
        if failure == "file_fsync" and descriptor == step_fd:
            raise OSError("injected file fsync failure")
        if failure == "directory_fsync" and step_fd is not None and descriptor != step_fd:
            raise OSError("injected directory fsync failure")
        return original_fsync(descriptor)

    if failure == "write":
        monkeypatch.setattr(export_module.os, "write", fail_write)
    else:
        def remember_step_write(descriptor, data):
            nonlocal step_fd
            if bytes(data[:16]).startswith(b"ISO-10303"):
                step_fd = descriptor
            return original_write(descriptor, data)

        monkeypatch.setattr(export_module.os, "write", remember_step_write)
        monkeypatch.setattr(export_module.os, "fsync", fail_fsync)

    result = await call_server_tool(
        DocumentService(document), "export", export_request(target)
    )

    error = result["error"]
    content = target.read_bytes()
    assert error["code"] == "EXPORT_WRITE_FAILED"
    assert error["path"] == str(target)
    assert len(error["expectedSha256"]) == 64
    assert error["expectedBytes"] > 0
    assert error["message"].endswith(
        "the file at path may be incomplete; coalesce leaves it in place; "
        "delete it yourself or export to a new path."
    )
    assert content


@pytest.mark.anyio
async def test_non_export_tool_is_unaffected(tmp_path):
    document = tmp_path / "query.coalesce.json"
    await run_server(document, enclosure_calls())

    result = await call_server_tool(
        DocumentService(document), "query", {"select": {"kind": "revision"}}
    )

    assert result == {"ok": True, "revision": 7, "units": "mm"}


@pytest.mark.anyio
async def test_export_contract_documents_stateless_recovery(tmp_path):
    export_tool = next(
        tool for tool in await list_server_tools(tmp_path / "guide.coalesce.json")
        if tool.name == "export"
    )

    assert set(export_tool.inputSchema["required"]) == {"expectedRevision"}
    assert export_tool.inputSchema["properties"]["action"]["enum"] == [
        "preflight",
        "publish",
    ]
    assert export_tool.inputSchema["properties"]["mode"]["enum"] == [
        "compound",
        "separate",
        "assembly",
    ]
    assert "requestId" not in export_tool.inputSchema["properties"]
    for phrase in (
        "private directory",
        "fresh Python process",
        "briefly visible",
        "never overwrites",
        "path",
        "sha256",
        "byteCount",
        "validation",
        "EXPORT_PATH_EXISTS",
        "EXPORT_WRITE_FAILED",
        "expectedSha256",
        "expectedBytes",
        "delete it yourself or export to a new path",
    ):
        assert phrase in TOOLS_GUIDE
