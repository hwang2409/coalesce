import hashlib
import json
import multiprocessing
import os
import shutil
import time
from pathlib import Path

import pytest
from test_mcp_cut_hole_circle import cut, hole, rectangle_sketch
from test_mcp_vertical import call_server_tool, run_server

import coalesce.board_import as board_import_module
from coalesce.service import DocumentService

FIXTURE = Path(__file__).parent / "fixtures/kicad/features.kicad_pcb"
MAX_KICAD_IMPORT_BYTES = 16 * 1024 * 1024


def import_request(path: Path | str, **changes):
    return {
        "action": "create",
        "expectedRevision": 0,
        "requestId": "import-1",
        "assetId": "controller_board",
        "format": "kicad_pcb",
        "path": str(path),
        **changes,
    }


def document_bytes(path: Path):
    return path.read_bytes() if path.exists() else None


def import_in_process(document: str, request: dict, ready, results) -> None:
    ready.send(None)
    ready.close()
    started = time.monotonic()
    result = DocumentService(Path(document)).import_board(request)
    results.put((result, time.monotonic() - started))


@pytest.mark.anyio
async def test_stdio_import_persists_stable_board_mechanics_across_restart(tmp_path):
    document = tmp_path / "board.coalesce.json"
    imported, queried = await run_server(
        document,
        [
            ("import", import_request(FIXTURE.resolve())),
            ("query", {"select": {"kind": "feature", "id": "controller_board"}}),
        ],
    )

    assert imported["ok"] is True
    assert imported["revision"] == 1
    assert imported["boardImport"] == queried["feature"]
    board = imported["boardImport"]
    assert board["type"] == "BoardImport"
    assert board["sourceDigest"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert board["parserVersion"] == "3"
    assert board["mechanics"]["thickness"] == 1.2
    assert board["mechanics"]["thicknessSource"] == "general.thickness"
    assert [item["id"] for item in board["mechanics"]["outline"]] == [
        "loop-1e9db439140237c1f6f93e94"
    ]
    assert board["mechanics"]["innerCutouts"] == []
    assert len({item["id"] for item in board["mechanics"]["holes"]}) == 3
    assert {
        (item["reference"], item["mounting"])
        for item in board["mechanics"]["holes"]
    } == {(None, True), ("H1", True)}
    assert {item["id"] for item in board["mechanics"]["footprints"]} == {
        "m2-id",
        "m3-id",
        "npth-id",
        "front-id",
        "center-id",
        "back-id",
    }
    assert board["availability"] == {
        "thickness": "available",
        "outline": "available",
        "innerCutouts": "available",
        "holes": "available",
        "footprints": "available",
        "courtyards": "partial",
        "models": "unresolved",
    }
    assert board["warnings"] == board["errors"] == []

    restarted = await run_server(
        document,
        [("query", {"select": {"kind": "feature", "id": "controller_board"}})],
    )
    assert restarted[0] == queried
    assert json.dumps(restarted[0]["feature"], sort_keys=True) == json.dumps(
        queried["feature"], sort_keys=True
    )


@pytest.mark.anyio
async def test_import_replay_uses_canonical_path_without_rereading_deleted_source(tmp_path):
    source_dir = tmp_path / "a"
    source_dir.mkdir()
    (source_dir / "sub").mkdir()
    source = source_dir / "board.kicad_pcb"
    shutil.copyfile(FIXTURE, source)
    alias = source_dir / "sub" / ".." / source.name
    document = tmp_path / "board.coalesce.json"
    service = DocumentService(document)

    created = await call_server_tool(service, "import", import_request(alias))
    assert created["boardImport"]["sourcePath"] == str(source.resolve())
    assert await call_server_tool(service, "import", import_request(source)) == created

    source.unlink()
    assert await call_server_tool(service, "import", import_request(source)) == created


@pytest.mark.anyio
async def test_import_replay_is_idempotent_and_reuse_or_stale_is_atomic(tmp_path):
    document = tmp_path / "board.coalesce.json"
    service = DocumentService(document)
    request = import_request(FIXTURE.resolve())

    created = await call_server_tool(service, "import", request)
    before = document.read_bytes()
    assert await call_server_tool(service, "import", request) == created
    assert document.read_bytes() == before

    reused = await call_server_tool(
        service, "import", {**request, "assetId": "other_board"}
    )
    assert reused["error"]["code"] == "REQUEST_ID_REUSED"
    assert document.read_bytes() == before

    stale = await call_server_tool(
        service,
        "import",
        import_request(FIXTURE.resolve(), requestId="import-2", expectedRevision=0),
    )
    assert stale["error"]["code"] == "REVISION_CONFLICT"
    assert document.read_bytes() == before


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="fifo paths require POSIX")
@pytest.mark.parametrize("source_kind", ["fifo", "device_symlink"])
def test_special_import_sources_fail_promptly_and_atomically(tmp_path, source_kind):
    if source_kind == "device_symlink" and not Path("/dev/zero").exists():
        pytest.skip("no character device is available")

    document = tmp_path / "board.coalesce.json"
    service = DocumentService(document)
    created = service.import_board(import_request(FIXTURE.resolve()))
    assert created["ok"] is True
    before = document.read_bytes()

    source = tmp_path / "special.kicad_pcb"
    if source_kind == "fifo":
        os.mkfifo(source)
    else:
        source.symlink_to("/dev/zero")
    request = import_request(
        source,
        assetId="special_board",
        expectedRevision=1,
        requestId=f"import-{source_kind}",
    )
    context = multiprocessing.get_context("spawn")
    ready_receiver, ready_sender = context.Pipe(duplex=False)
    results = context.Queue()
    process = context.Process(
        target=import_in_process,
        args=(str(document), request, ready_sender, results),
    )
    process_started = time.monotonic()
    process.start()
    ready_sender.close()
    if not ready_receiver.poll(30):
        process.terminate()
        process.join()
        pytest.fail("spawned import worker did not start within 30 seconds")
    ready_receiver.recv()
    ready_receiver.close()
    startup_seconds = time.monotonic() - process_started
    operation_started = time.monotonic()
    process.join(timeout=5)
    operation_seconds = time.monotonic() - operation_started
    if process.is_alive():
        process.terminate()
        process.join()
        pytest.fail(
            f"import did not complete within 5 seconds "
            f"(process startup {startup_seconds:.3f}s, operation {operation_seconds:.3f}s)"
        )

    result, child_operation_seconds = results.get(timeout=1)
    diagnostics = {
        "processStartupSeconds": startup_seconds,
        "parentOperationSeconds": operation_seconds,
        "childOperationSeconds": child_operation_seconds,
    }
    assert result["error"]["code"] == "IMPORT_PATH_NOT_FILE", diagnostics
    assert DocumentService(document).revision == 1, diagnostics
    assert document.read_bytes() == before, diagnostics


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("missing", "IMPORT_SOURCE_UNREADABLE"),
        ("relative", "IMPORT_PATH_NOT_ABSOLUTE"),
        ("directory", "IMPORT_PATH_NOT_FILE"),
        ("oversized", "IMPORT_TOO_LARGE"),
        ("malformed", "IMPORT_INVALID_MECHANICS"),
        ("open", "IMPORT_INVALID_MECHANICS"),
        ("duplicate", "IMPORT_INVALID_MECHANICS"),
    ],
)
async def test_invalid_import_sources_do_not_commit(tmp_path, case, code):
    document = tmp_path / "board.coalesce.json"
    service = DocumentService(document)
    source = tmp_path / "source.kicad_pcb"
    if case == "missing":
        source = tmp_path / "missing.kicad_pcb"
    elif case == "relative":
        source = Path("relative.kicad_pcb")
    elif case == "directory":
        source = tmp_path
    elif case == "oversized":
        source.write_bytes(b"x" * (MAX_KICAD_IMPORT_BYTES + 1))
    elif case == "malformed":
        source.write_text("(not_a_board)")
    elif case == "open":
        source.write_text(
            "(kicad_pcb "
            '(gr_line (start 0 0) (end 10 0) (layer "Edge.Cuts")) '
            '(gr_line (start 10 0) (end 10 10) (layer "Edge.Cuts")))'
        )
    else:
        source.write_text(
            "(kicad_pcb "
            '(gr_rect (start 0 0) (end 10 10) (layer "Edge.Cuts")) '
            '(footprint "A" (layer "F.Cu") (at 1 1) (uuid duplicate)) '
            '(footprint "B" (layer "F.Cu") (at 2 2) (uuid duplicate)))'
        )

    before = document_bytes(document)
    result = await call_server_tool(service, "import", import_request(source))

    assert result["ok"] is False
    assert result["error"]["code"] == code
    assert "hint" in result["error"]
    assert service.revision == 0
    assert document_bytes(document) == before


@pytest.mark.anyio
async def test_hash_and_parse_use_one_captured_snapshot(tmp_path, monkeypatch):
    source = tmp_path / "source.kicad_pcb"
    original = FIXTURE.read_bytes()
    changed = original.replace(b"(thickness 1.2)", b"(thickness 1.6)")
    source.write_bytes(original)
    parse_snapshot = board_import_module.parse_kicad_pcb_bytes

    def change_source_after_read(raw):
        source.write_bytes(changed)
        return parse_snapshot(raw)

    monkeypatch.setattr(board_import_module, "parse_kicad_pcb_bytes", change_source_after_read)
    result = await call_server_tool(
        DocumentService(tmp_path / "board.coalesce.json"),
        "import",
        import_request(source),
    )

    assert result["boardImport"]["sourceDigest"] == hashlib.sha256(original).hexdigest()
    assert result["boardImport"]["mechanics"]["thickness"] == 1.2
    assert source.read_bytes() == changed


@pytest.mark.anyio
async def test_source_changes_are_inert_until_explicit_refresh(tmp_path):
    source = tmp_path / "source.kicad_pcb"
    shutil.copyfile(FIXTURE, source)
    document = tmp_path / "board.coalesce.json"
    service = DocumentService(document)

    created = await call_server_tool(service, "import", import_request(source))
    original = created["boardImport"]
    before_change = document.read_bytes()
    source.write_bytes(FIXTURE.read_bytes().replace(b"(thickness 1.2)", b"(thickness 1.6)"))

    unchanged = await call_server_tool(
        service, "query", {"select": {"kind": "feature", "id": "controller_board"}}
    )
    assert unchanged["feature"] == original
    assert document.read_bytes() == before_change

    refreshed = await call_server_tool(
        service,
        "import",
        {
            "action": "refresh",
            "expectedRevision": 1,
            "requestId": "refresh-1",
            "assetId": "controller_board",
            "format": "kicad_pcb",
        },
    )
    assert refreshed["revision"] == 2
    assert refreshed["previousRevision"] == 1
    assert refreshed["boardImport"]["sourceDigest"] != original["sourceDigest"]
    assert refreshed["boardImport"]["mechanics"]["thickness"] == 1.6


@pytest.mark.anyio
@pytest.mark.parametrize("feature_kind", ["cut", "hole"])
@pytest.mark.parametrize("target_kind", ["absent", "board_import", "sketch"])
async def test_subtractive_features_validate_body_targets(
    tmp_path, feature_kind, target_kind
):
    document = tmp_path / f"{feature_kind}-{target_kind}.coalesce.json"
    service = DocumentService(document)
    revision = 0
    if target_kind == "board_import":
        imported = await call_server_tool(service, "import", import_request(FIXTURE.resolve()))
        assert imported["ok"] is True
        target = "controller_board"
        revision = 1
    else:
        target = "target_sketch" if target_kind == "sketch" else "missing"

    sketch = rectangle_sketch(
        request_id="tool-sketch",
        revision=revision,
        sketch_id="target_sketch" if target_kind == "sketch" else "tool_sketch",
        entity_id="profile",
        width=2,
        height=2,
    )
    created = await call_server_tool(service, "sketch", sketch)
    assert created["ok"] is True
    revision += 1
    operation = (
        cut(
            request_id="invalid-cut",
            revision=revision,
            feature_id="invalid_cut",
            target=target,
            tool=sketch["sketchId"],
        )
        if feature_kind == "cut"
        else hole(
            request_id="invalid-hole",
            revision=revision,
            feature_id="invalid_hole",
            target=target,
            position=(0, 0),
        )
    )
    before = document.read_bytes()

    result = await call_server_tool(service, "feature", operation)

    assert result["error"]["code"] == (
        "UNKNOWN_ID" if target_kind == "absent" else "INVALID_PARAMETER"
    )
    assert service.revision == revision
    assert document.read_bytes() == before


@pytest.mark.anyio
async def test_unreadable_source_does_not_commit(tmp_path):
    if os.name == "nt":
        pytest.skip("mode bits do not make a file unreadable on Windows")
    source = tmp_path / "source.kicad_pcb"
    shutil.copyfile(FIXTURE, source)
    source.chmod(0)
    document = tmp_path / "board.coalesce.json"
    try:
        result = await call_server_tool(
            DocumentService(document), "import", import_request(source)
        )
    finally:
        source.chmod(0o600)

    assert result["ok"] is False
    assert result["error"]["code"] == "IMPORT_SOURCE_UNREADABLE"
    assert document_bytes(document) is None
