import hashlib
import json
import multiprocessing
import os
import stat
import subprocess

import pytest
from test_mcp_cut_hole_circle import extrude, rectangle_sketch
from test_mcp_join_holes_frames import join_extrude, patterned_hole
from test_mcp_vertical import call_server_tool, list_server_tools, run_server

import coalesce.export as export_module
from coalesce.document_store import ExportJournal
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


def export_request(path, request_id="export-all", part_ids=None, revision=7):
    request = {
        "expectedRevision": revision,
        "requestId": request_id,
        "path": str(path),
    }
    if part_ids is not None:
        request["partIds"] = part_ids
    return request


def append_export_receipts(document, prefix):
    journal = ExportJournal(document)
    for index in range(50):
        journal.append(
            {
                "requestId": f"{prefix}-{index}",
                "requestHash": f"hash-{prefix}-{index}",
                "revision": 0,
                "response": {"ok": True},
            }
        )


def validation_timeout_worker(temporary_directory):
    from build123d import Box

    shape = Box(1, 1, 1)
    expected = export_module._metrics([shape], ["box"])

    def make_temporary_directory():
        temporary_directory.mkdir(mode=0o700)
        return str(temporary_directory)

    def time_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("STEP verifier", 120)

    export_module.tempfile.mkdtemp = make_temporary_directory
    export_module.subprocess.run = time_out
    try:
        export_module._validated_step_bytes([shape], expected)
    except export_module.ExportFailure as error:
        assert error.code == "EXPORT_VALIDATION_FAILED"
    else:
        raise AssertionError("validation timeout unexpectedly succeeded")


@pytest.mark.anyio
async def test_step_export_all_selected_restart_and_replay(tmp_path):
    document = tmp_path / "enclosure.coalesce.json"
    first_path = tmp_path / "enclosure.step"
    await run_server(document, enclosure_calls())
    document_bytes = document.read_bytes()
    exported = (
        await run_server(document, [("export", export_request(first_path))])
    )[0]

    assert document.read_bytes() == document_bytes

    assert exported["ok"] is True
    assert exported["revision"] == 7
    assert exported["partIds"] == ["plate/plate", "lid/lid"]
    assert exported["path"] == str(first_path)
    assert len(exported["sha256"]) == 64
    assert hashlib.sha256(first_path.read_bytes()).hexdigest() == exported["sha256"]
    assert exported["tolerance"] == {"relative": 1e-07, "absolute": 1e-07}
    assert exported["validation"]["geometry"]["status"] == "passed"
    assert exported["validation"]["geometry"]["freshProcess"] is True
    assert exported["validation"]["reimport"]["partCount"] == 2
    assert exported["validation"]["reimport"]["solidCount"] == 2
    assert len(exported["validation"]["reimport"]["parts"][0]["cylinders"]) == 4
    assert exported["validation"]["labels"] == {
        "status": "preserved",
        "expected": ["plate", "lid"],
        "actual": ["plate", "lid"],
    }

    selected_path = tmp_path / "plate.step"
    selected = (
        await run_server(
            document,
            [
                (
                    "export",
                    export_request(
                        selected_path,
                        request_id="export-plate",
                        part_ids=["plate/plate"],
                    ),
                )
            ],
        )
    )[0]
    assert selected["partIds"] == ["plate/plate"]
    assert selected["validation"]["reimport"]["partCount"] == 1
    assert selected["validation"]["reimport"]["solidCount"] == 1
    assert document.read_bytes() == document_bytes

    second_path = tmp_path / "enclosure-restarted.step"
    restarted = (
        await run_server(
            document,
            [("export", export_request(second_path, request_id="export-restarted"))],
        )
    )[0]
    assert restarted["validation"] == exported["validation"]
    assert document.read_bytes() == document_bytes

    replay = (
        await run_server(document, [("export", export_request(first_path))])
    )[0]
    assert replay == exported
    assert document.read_bytes() == document_bytes

    journal = document.with_name(f"{document.name}.exports.jsonl")
    with journal.open("ab") as stream:
        stream.write(b'{"requestId":"torn')
    with pytest.warns(RuntimeWarning, match="torn export journal line"):
        restarted_service = DocumentService(document)
    replay_after_torn_line = await call_server_tool(
        restarted_service, "export", export_request(first_path)
    )
    assert replay_after_torn_line == exported
    assert document.read_bytes() == document_bytes


@pytest.mark.anyio
async def test_export_rejects_existing_stale_unknown_and_bad_parent(tmp_path):
    document = tmp_path / "errors.coalesce.json"
    await run_server(document, enclosure_calls())
    document_bytes = document.read_bytes()
    existing = tmp_path / "existing.step"
    existing.write_bytes(b"keep me")
    missing_parent = tmp_path / "missing" / "part.step"

    results = await run_server(
        document,
        [
            ("export", export_request(existing, request_id="exists")),
            (
                "export",
                export_request(tmp_path / "stale.step", request_id="stale", revision=6),
            ),
            (
                "export",
                export_request(
                    tmp_path / "unknown.step",
                    request_id="unknown",
                    part_ids=["missing/part"],
                ),
            ),
            ("export", export_request(missing_parent, request_id="missing-parent")),
        ],
    )

    assert [result["error"]["code"] for result in results] == [
        "EXPORT_PATH_EXISTS",
        "STALE_REVISION",
        "UNKNOWN_ID",
        "EXPORT_WRITE_FAILED",
    ]
    assert existing.read_bytes() == b"keep me"
    assert not (tmp_path / "stale.step").exists()
    assert not (tmp_path / "unknown.step").exists()
    assert not missing_parent.exists()
    assert document.read_bytes() == document_bytes


@pytest.mark.anyio
async def test_export_rejects_unwritable_parent(tmp_path):
    document = tmp_path / "unwritable.coalesce.json"
    await run_server(document, enclosure_calls())
    document_bytes = document.read_bytes()
    parent = tmp_path / "locked"
    parent.mkdir()
    parent.chmod(0o500)
    target = parent / "part.step"
    try:
        result = (
            await run_server(
                document,
                [("export", export_request(target, request_id="unwritable"))],
            )
        )[0]
    finally:
        parent.chmod(0o700)

    assert result["error"]["code"] == "EXPORT_WRITE_FAILED"
    assert not target.exists()
    assert document.read_bytes() == document_bytes


@pytest.mark.anyio
async def test_validation_mismatch_removes_temporary_and_final_files(tmp_path, monkeypatch):
    document = tmp_path / "mismatch.coalesce.json"
    await run_server(document, enclosure_calls())
    document_bytes = document.read_bytes()
    target = tmp_path / "mismatch.step"
    service = DocumentService(document)

    def mismatched(_path, **_kwargs):
        return {
            "partCount": 0,
            "solidCount": 0,
            "bbox": {"min": [0, 0, 0], "max": [0, 0, 0]},
            "parts": [],
            "volume": 0,
            "area": 0,
        }

    monkeypatch.setattr(export_module, "verify_step_fresh", mismatched)
    result = await call_server_tool(
        service,
        "export",
        export_request(target, request_id="mismatch"),
    )

    assert result["error"]["code"] == "EXPORT_VALIDATION_FAILED"
    assert not target.exists()
    assert not list(tmp_path.glob(".mismatch.step.*.tmp"))
    assert document.read_bytes() == document_bytes


@pytest.mark.anyio
async def test_destination_created_after_validation_is_preserved(tmp_path, monkeypatch):
    document = tmp_path / "appeared.coalesce.json"
    await run_server(document, enclosure_calls())
    target = tmp_path / "appeared.step"
    foreign = b"created by another process"
    original_verify = export_module.verify_step_fresh

    def create_destination_after_validation(path, **kwargs):
        evidence = original_verify(path, **kwargs)
        target.write_bytes(foreign)
        return evidence

    monkeypatch.setattr(export_module, "verify_step_fresh", create_destination_after_validation)
    result = await call_server_tool(
        DocumentService(document),
        "export",
        export_request(target, request_id="appeared"),
    )

    assert result["error"]["code"] == "EXPORT_PATH_EXISTS"
    assert target.read_bytes() == foreign


@pytest.mark.anyio
async def test_replacement_before_receipt_is_preserved(tmp_path, monkeypatch):
    document = tmp_path / "replaced.coalesce.json"
    await run_server(document, enclosure_calls())
    target = tmp_path / "replaced.step"
    foreign = b"replacement from another process"
    service = DocumentService(document)
    original_append = service._export_journal.append

    def replace_then_fail(receipt):
        if receipt.get("kind") == "pending":
            return original_append(receipt)
        target.unlink()
        target.write_bytes(foreign)
        raise OSError("injected receipt failure after replacement")

    monkeypatch.setattr(service._export_journal, "append", replace_then_fail)
    result = await call_server_tool(
        service,
        "export",
        export_request(target, request_id="replaced"),
    )

    assert result["error"]["code"] == "EXPORT_RECEIPT_FAILED"
    assert result["error"]["path"] == str(target)
    assert len(result["error"]["expectedSha256"]) == 64
    assert result["error"]["expectedBytes"] > 0
    assert "same requestId" in result["error"]["hint"]
    assert target.read_bytes() == foreign


@pytest.mark.anyio
async def test_interrupted_short_write_preserves_replacement(tmp_path, monkeypatch):
    document = tmp_path / "short-write.coalesce.json"
    await run_server(document, enclosure_calls())
    target = tmp_path / "short-write.step"
    foreign = b"replacement from another process"
    original_write = os.write
    interrupted = False

    def interrupt_step_write(descriptor, data):
        nonlocal interrupted
        if not interrupted and bytes(data[:16]).startswith(b"ISO-10303"):
            interrupted = True
            written = original_write(descriptor, data[: len(data) // 2])
            target.unlink()
            target.write_bytes(foreign)
            return written
        if interrupted:
            raise OSError("injected write failure after a short write")
        return original_write(descriptor, data)

    monkeypatch.setattr(export_module.os, "write", interrupt_step_write)
    result = await call_server_tool(
        DocumentService(document),
        "export",
        export_request(target, request_id="short-write"),
    )

    assert result["error"]["code"] == "EXPORT_WRITE_FAILED"
    assert result["error"]["path"] == str(target)
    assert len(result["error"]["expectedSha256"]) == 64
    assert result["error"]["expectedBytes"] > 0
    assert target.read_bytes() == foreign


@pytest.mark.anyio
async def test_receipt_failure_leaves_final_and_retry_adopts_it(tmp_path, monkeypatch):
    document = tmp_path / "receipt-failure.coalesce.json"
    await run_server(document, enclosure_calls())
    target = tmp_path / "receipt-failure.step"
    service = DocumentService(document)
    original_append = service._export_journal.append

    def fail_append(receipt):
        if receipt.get("kind") == "pending":
            return original_append(receipt)
        raise OSError("injected receipt failure")

    monkeypatch.setattr(service._export_journal, "append", fail_append)
    failed = await call_server_tool(
        service,
        "export",
        export_request(target, request_id="receipt-failure"),
    )
    assert failed["error"]["code"] == "EXPORT_RECEIPT_FAILED"
    assert failed["error"]["path"] == str(target)
    assert failed["error"]["expectedBytes"] == target.stat().st_size
    assert failed["error"]["expectedSha256"] == hashlib.sha256(
        target.read_bytes()
    ).hexdigest()

    retried = await call_server_tool(
        DocumentService(document),
        "export",
        export_request(target, request_id="receipt-failure"),
    )
    assert retried["ok"] is True
    assert retried["adopted"] is True
    assert retried["sha256"] == failed["error"]["expectedSha256"]
    journal = document.with_name(f"{document.name}.exports.jsonl")
    records = [json.loads(line) for line in journal.read_text().splitlines()]
    assert len([record for record in records if "response" in record]) == 1
    assert records[0]["sha256"] == failed["error"]["expectedSha256"]


@pytest.mark.anyio
async def test_retry_rejects_changed_existing_file(tmp_path, monkeypatch):
    document = tmp_path / "changed-retry.coalesce.json"
    await run_server(document, enclosure_calls())
    target = tmp_path / "changed-retry.step"
    service = DocumentService(document)
    original_append = service._export_journal.append

    def fail_append(receipt):
        if receipt.get("kind") == "pending":
            return original_append(receipt)
        raise OSError("injected receipt failure")

    monkeypatch.setattr(service._export_journal, "append", fail_append)
    failed = await call_server_tool(
        service,
        "export",
        export_request(target, request_id="changed-retry"),
    )
    assert failed["error"]["code"] == "EXPORT_RECEIPT_FAILED"

    foreign = b"changed after failed export"
    target.write_bytes(foreign)
    monkeypatch.setattr(service._export_journal, "append", original_append)
    retried = await call_server_tool(
        service,
        "export",
        export_request(target, request_id="changed-retry"),
    )

    assert retried["error"]["code"] == "EXPORT_PATH_EXISTS"
    assert target.read_bytes() == foreign
    journal = document.with_name(f"{document.name}.exports.jsonl")
    records = [json.loads(line) for line in journal.read_text().splitlines()]
    assert all("response" not in record for record in records)


@pytest.mark.anyio
@pytest.mark.parametrize("failure_target", ["file", "directory"])
async def test_fsync_failure_leaves_final_for_retry(
    tmp_path, monkeypatch, failure_target
):
    document = tmp_path / f"{failure_target}-fsync.coalesce.json"
    await run_server(document, enclosure_calls())
    target = tmp_path / f"{failure_target}-fsync.step"
    original_fsync = export_module.os.fsync
    failed = False

    def fail_selected_fsync(descriptor):
        nonlocal failed
        mode = os.fstat(descriptor).st_mode
        selected = stat.S_ISREG(mode) if failure_target == "file" else stat.S_ISDIR(mode)
        if selected and target.exists() and not failed:
            failed = True
            raise OSError(f"injected {failure_target} fsync failure")
        return original_fsync(descriptor)

    monkeypatch.setattr(export_module.os, "fsync", fail_selected_fsync)
    result = await call_server_tool(
        DocumentService(document),
        "export",
        export_request(target, request_id=f"{failure_target}-fsync"),
    )

    assert result["error"]["code"] == "EXPORT_WRITE_FAILED"
    assert result["error"]["path"] == str(target)
    assert result["error"]["expectedBytes"] == target.stat().st_size
    assert result["error"]["expectedSha256"] == hashlib.sha256(
        target.read_bytes()
    ).hexdigest()
    assert "same requestId" in result["error"]["hint"]


@pytest.mark.anyio
async def test_malformed_complete_journal_line_blocks_only_export(tmp_path):
    document = tmp_path / "corrupt-journal.coalesce.json"
    await run_server(document, enclosure_calls())
    journal = document.with_name(f"{document.name}.exports.jsonl")
    valid = {
        "requestId": "old-export",
        "requestHash": "hash",
        "revision": 7,
        "response": {"ok": True},
    }
    journal.write_text(
        json.dumps(valid) + "\n" + "{malformed}\n" + json.dumps(valid) + "\n"
    )

    service = DocumentService(document)
    query = await call_server_tool(service, "query", {"select": {"kind": "measurements"}})
    result = await call_server_tool(
        service,
        "export",
        export_request(tmp_path / "blocked.step", request_id="blocked"),
    )

    assert query["ok"] is True
    assert result["error"]["code"] == "EXPORT_JOURNAL_CORRUPT"
    assert result["error"]["line"] == 2
    assert not (tmp_path / "blocked.step").exists()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "journal_contents",
    [
        None,
        "{}\n",
        '{"requestId":"old","requestHash":"hash","revision":"7","response":{}}\n',
        '{"requestId":"old","requestHash":"hash","revision":7,"response":{},"extra":true}\n',
    ],
    ids=["directory", "missing-fields", "wrong-type", "extra-field"],
)
async def test_invalid_journal_shape_blocks_only_export(tmp_path, journal_contents):
    document = tmp_path / "invalid-journal.coalesce.json"
    journal = document.with_name(f"{document.name}.exports.jsonl")
    if journal_contents is None:
        journal.mkdir()
    else:
        journal.write_text(journal_contents)

    service = DocumentService(document, warmup=False)
    query = await call_server_tool(service, "query", {"select": {"kind": "revision"}})
    result = await call_server_tool(
        service,
        "export",
        export_request(tmp_path / "blocked.step", request_id="blocked", revision=0),
    )

    assert query["ok"] is True
    assert result["error"]["code"] == "EXPORT_JOURNAL_CORRUPT"
    assert result["error"]["path"] == str(journal)
    assert not (tmp_path / "blocked.step").exists()


def test_private_validation_directory_removed_after_timeout_in_fresh_process(tmp_path):
    temporary_directory = tmp_path / "private-validation"
    process = multiprocessing.get_context("spawn").Process(
        target=validation_timeout_worker,
        args=(temporary_directory,),
    )
    process.start()
    process.join(timeout=30)

    assert process.exitcode == 0
    assert not temporary_directory.exists()


def test_export_journal_serializes_processes(tmp_path):
    document = tmp_path / "concurrent.coalesce.json"
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=append_export_receipts, args=(document, prefix))
        for prefix in ("left", "right")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    journal = document.with_name(f"{document.name}.exports.jsonl")
    lines = journal.read_bytes().splitlines()
    assert len(lines) == 100
    assert len({json.loads(line)["requestId"] for line in lines}) == 100


@pytest.mark.anyio
async def test_export_contract_describes_absolute_no_overwrite_fresh_validation(tmp_path):
    tools = {tool.name: tool for tool in await list_server_tools(tmp_path / "tools.json")}
    export = tools["export"]

    assert export.inputSchema["properties"]["path"]["type"] == "string"
    assert export.annotations.readOnlyHint is False
    assert export.annotations.destructiveHint is False
    assert export.annotations.idempotentHint is True
    description = export.description.lower()
    assert "absolute" in description
    assert "never overwrite" in description
    assert "fresh python process" in description
    assert "never overwrite" in TOOLS_GUIDE.lower()
    assert "fresh python process" in TOOLS_GUIDE.lower()
    assert "briefly visible" in TOOLS_GUIDE.lower()
    assert "retry" in TOOLS_GUIDE.lower()
    assert os.path.isabs(str(tmp_path))
