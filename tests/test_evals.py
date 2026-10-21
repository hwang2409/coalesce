import json
from pathlib import Path

import pytest

from evals.checks import check_task
from evals.run import _claude_metadata, _zeta_metadata
from evals.tasks import load_tasks

REVISION = {"coalesce_path": Path.cwd(), "coalesce_revision": "0" * 40, "coalesce_dirty": False}


@pytest.fixture
def validated(monkeypatch):
    import evals.run

    monkeypatch.setattr(evals.run, "_preflight", lambda *_a, **_k: REVISION)
    return REVISION


def _document(path):
    path.write_text(json.dumps({"revision": 0, "graph": {"nodes": []}}))
    return path


def _inspection(monkeypatch, inspected):
    import coalesce.service

    class FakeService:
        calls = 0

        def __init__(self, *_args, **_kwargs):
            pass

        def _ensure_geometry(self):
            FakeService.calls += 1

        def query(self, query):
            if query["select"]["kind"] == "graph":
                return {
                    "ok": True,
                    "graph": {"nodes": [{"type": "PcbBase", "id": "base"}]},
                }
            return {"ok": True, **inspected}

    monkeypatch.setattr(coalesce.service, "DocumentService", FakeService)
    return FakeService


def _enclosure_check(tmp_path, monkeypatch, inspected, check):
    service = _inspection(monkeypatch, inspected)
    steps = tmp_path / "steps"
    steps.mkdir()
    results = check_task({"checks": [check]}, _document(tmp_path / "model.coalesce.json"), steps)
    return results[0], service


def test_run_preflight_allows_output_inside_clean_checkout(tmp_path, monkeypatch):
    from evals.run import run_tasks

    monkeypatch.setattr("evals.run._git_state", lambda _path: ("revision-1", False))
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    (task_dir / "one.json").write_text(json.dumps({
        "id": "one", "tier": "easy", "prompt": "x", "fixtures": [],
        "checks": [{"kind": "document_value", "path": "revision", "equals": 0}],
    }))

    def fake(_prompt, run_dir, **_kwargs):
        _document(run_dir / "model.coalesce.json")
        (run_dir / "steps").mkdir()
        return {"returncode": 0, "transcript_parsed": True}

    rows = run_tasks(task_dir, tmp_path / "eval-output", ["one"], ["fake"], fake,
                     coalesce_path=tmp_path)
    assert rows[0]["coalesce_revision"] == "revision-1"


def test_run_preflight_rejects_dirty_checkout_before_output(tmp_path, monkeypatch):
    from evals.run import run_tasks

    monkeypatch.setattr("evals.run._git_state", lambda _path: ("revision-1", True))
    with pytest.raises(RuntimeError, match="coalesce checkout.*dirty"):
        run_tasks(tmp_path / "tasks", tmp_path / "eval-output", ["one"], ["fake"], lambda **_: {},
                  coalesce_path=tmp_path)
    assert not (tmp_path / "eval-output").exists()


def test_zeta_preflight_rejects_revision_mismatch_before_output(tmp_path, monkeypatch):
    import evals.run
    from evals.run import run_tasks, zeta_harness

    project = _zeta_project(tmp_path, tmp_path / "server")
    monkeypatch.setattr(evals.run, "_git_state", lambda path: ("under-test" if path == tmp_path else "stale", False))
    with pytest.raises(RuntimeError, match="does not match revision under test"):
        run_tasks(tmp_path / "tasks", tmp_path / "eval-output", ["one"], ["m"], zeta_harness,
                  coalesce_path=tmp_path, zeta_project=project)
    assert not (tmp_path / "eval-output").exists()


def test_root_only_step_does_not_count(tmp_path, validated):
    from build123d import Box, export_step

    from evals.run import run_tasks

    def fake(_prompt, run_dir, **_kwargs):
        _document(run_dir / "model.coalesce.json")
        export_step(Box(60, 40, 6), run_dir / "stray.step")
        return {"returncode": 0, "transcript_parsed": True}

    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    (task_dir / "root.json").write_text(json.dumps({
        "id": "root", "tier": "easy", "prompt": "x", "fixtures": [],
        "checks": [{"kind": "step_validity"}],
    }))
    rows = run_tasks(task_dir, tmp_path / "out", ["root"], ["fake"], fake, coalesce_path=Path.cwd())
    assert rows[0]["checks"][0]["status"] == "fail"
    assert rows[0]["overall"] is False


def test_through_m3_clearance_holes_pass(tmp_path):
    from build123d import Align, Box, Cylinder, Pos, export_step

    steps = tmp_path / "steps"
    steps.mkdir()
    plate = Box(60, 40, 6, align=(Align.MIN, Align.MIN, Align.MIN))
    for x, y in ((5, 5), (55, 5), (5, 35), (55, 35)):
        plate -= Pos(x, y, 0) * Cylinder(
            1.7, 6, align=(Align.CENTER, Align.CENTER, Align.MIN)
        )
    export_step(plate, steps / "plate.step")
    check = {"kind": "hole_positions", "positions": [[5, 5], [55, 5], [5, 35], [55, 35]], "diameter": [3.2, 3.6], "tolerance": .2}
    assert check_task({"checks": [check]}, _document(tmp_path / "model.coalesce.json"), steps)[0]["status"] == "pass"


def test_posts_at_hole_positions_fail(tmp_path):
    from build123d import Align, Box, Cylinder, Pos, export_step

    steps = tmp_path / "steps"
    steps.mkdir()
    plate = Box(60, 40, 6, align=(Align.MIN, Align.MIN, Align.MIN))
    for x, y in ((5, 5), (55, 5), (5, 35), (55, 35)):
        plate += Pos(x, y, 6) * Cylinder(1.7, 3)
    export_step(plate, steps / "posts.step")
    check = {"kind": "hole_positions", "positions": [[5, 5], [55, 5], [5, 35], [55, 35]], "diameter": [3.2, 3.6], "tolerance": .2}
    assert check_task({"checks": [check]}, _document(tmp_path / "model.coalesce.json"), steps)[0]["status"] == "fail"


def _plate_with_holes(offset=(0, 0, 0)):
    from build123d import Align, Box, Cylinder, Pos

    plate = Pos(*offset) * Box(60, 40, 6, align=(Align.MIN, Align.MIN, Align.MIN))
    for x, y in ((5, 5), (55, 5), (5, 35), (55, 35)):
        plate -= Pos(x + offset[0], y + offset[1], offset[2]) * Cylinder(
            1.7, 6, align=(Align.CENTER, Align.CENTER, Align.MIN)
        )
    return plate


def _plate_holes_task():
    return next(task for task in load_tasks(Path("evals/tasks")) if task["id"] == "plate-holes")


def test_plate_and_separate_hole_tubes_fail_single_part_rule(tmp_path):
    from build123d import Align, Box, Cylinder, Pos, export_step

    steps = tmp_path / "steps"
    steps.mkdir()
    export_step(Box(60, 40, 6, align=(Align.MIN, Align.MIN, Align.MIN)), steps / "plate.step")
    for index, (x, y) in enumerate(((5, 5), (55, 5), (5, 35), (55, 35))):
        tube = Pos(x, y, 0) * (
            Cylinder(2.2, 6, align=(Align.CENTER, Align.CENTER, Align.MIN))
            - Cylinder(1.7, 6, align=(Align.CENTER, Align.CENTER, Align.MIN))
        )
        export_step(tube, steps / f"tube-{index}.step")
    results = check_task(
        _plate_holes_task(), _document(tmp_path / "model.coalesce.json"), steps
    )
    assert results[0]["status"] == "fail"
    assert "exactly 1 STEP file" in results[0]["reason"]


def test_plate_offset_from_stated_frame_fails(tmp_path):
    from build123d import export_step

    steps = tmp_path / "steps"
    steps.mkdir()
    export_step(_plate_with_holes((-1, -1, -3)), steps / "plate.step")
    results = check_task(
        _plate_holes_task(), _document(tmp_path / "model.coalesce.json"), steps
    )
    bbox = next(result for result in results if result["kind"] == "bbox")
    assert bbox["status"] == "fail"


def test_correct_single_plate_in_stated_frame_passes(tmp_path):
    from build123d import export_step

    steps = tmp_path / "steps"
    steps.mkdir()
    export_step(_plate_with_holes(), steps / "plate.step")
    results = check_task(
        _plate_holes_task(), _document(tmp_path / "model.coalesce.json"), steps
    )
    assert results[0]["kind"] == "parts"
    assert all(result["status"] == "pass" for result in results)


def test_plate_with_fifth_qualifying_hole_fails(tmp_path):
    from build123d import Align, Cylinder, Pos, export_step

    steps = tmp_path / "steps"
    steps.mkdir()
    plate = _plate_with_holes()
    plate -= Pos(30, 20, 0) * Cylinder(
        1.7, 6, align=(Align.CENTER, Align.CENTER, Align.MIN)
    )
    export_step(plate, steps / "plate.step")
    results = check_task(
        _plate_holes_task(), _document(tmp_path / "model.coalesce.json"), steps
    )
    holes = next(result for result in results if result["kind"] == "hole_positions")
    assert holes["status"] == "fail"
    assert holes["unmatchedDetected"] == [
        {"position": [30.0, 20.0], "diameter": pytest.approx(3.4)}
    ]
    assert holes["unmatchedExpected"] == []


def test_plate_with_fifth_out_of_range_hole_fails(tmp_path):
    from build123d import Align, Cylinder, Pos, export_step

    steps = tmp_path / "steps"
    steps.mkdir()
    plate = _plate_with_holes()
    plate -= Pos(30, 20, 0) * Cylinder(
        1.5, 6, align=(Align.CENTER, Align.CENTER, Align.MIN)
    )
    export_step(plate, steps / "plate.step")
    results = check_task(
        _plate_holes_task(), _document(tmp_path / "model.coalesce.json"), steps
    )
    holes = next(result for result in results if result["kind"] == "hole_positions")
    assert holes["status"] == "fail"
    assert holes["unmatchedDetected"] == [
        {"position": [30.0, 20.0], "diameter": pytest.approx(3.0)}
    ]
    assert holes["unmatchedExpected"] == []


@pytest.mark.parametrize(
    "extra",
    ["blind_hole", "blind_pocket", "boss"],
)
def test_plate_with_any_unexpected_geometry_fails(tmp_path, extra):
    from build123d import Align, Box, Cylinder, Pos, export_step

    steps = tmp_path / "steps"
    steps.mkdir()
    plate = _plate_with_holes()
    if extra == "blind_hole":
        plate -= Pos(30, 20, 3) * Cylinder(1.5, 3, align=(Align.CENTER, Align.CENTER, Align.MIN))
    elif extra == "blind_pocket":
        plate -= Pos(30, 20, 4) * Box(6, 4, 2, align=(Align.CENTER, Align.CENTER, Align.MIN))
    else:
        plate += Pos(30, 20, 6) * Box(4, 4, 2, align=(Align.CENTER, Align.CENTER, Align.MIN))
    export_step(plate, steps / "plate.step")
    results = check_task(
        _plate_holes_task(), _document(tmp_path / "model.coalesce.json"), steps
    )
    holes = next(result for result in results if result["kind"] == "hole_positions")
    assert holes["status"] == "fail", holes


@pytest.mark.parametrize("suffix", [".stp", ".STEP"])
def test_extra_step_suffix_fails(tmp_path, suffix):
    from build123d import Box, export_step

    steps = tmp_path / "steps"
    steps.mkdir()
    export_step(_plate_with_holes(), steps / "plate.step")
    export_step(Box(1, 1, 1), steps / f"unexpected{suffix}")
    results = check_task(
        _plate_holes_task(), _document(tmp_path / "model.coalesce.json"), steps
    )
    parts = next(result for result in results if result["kind"] == "parts")
    assert parts["status"] == "fail"
    assert parts["reason"] == "expected exactly 1 STEP file(s), found 2"


def test_non_step_file_in_steps_fails(tmp_path):
    from build123d import export_step

    steps = tmp_path / "steps"
    steps.mkdir()
    export_step(_plate_with_holes(), steps / "plate.step")
    (steps / "notes.txt").write_text("not geometry")
    results = check_task(
        _plate_holes_task(), _document(tmp_path / "model.coalesce.json"), steps
    )
    inventory = next(result for result in results if result["kind"] == "artifacts")
    assert inventory == {
        "kind": "artifacts",
        "status": "fail",
        "unexpected": ["notes.txt"],
    }


def test_blocked_expected_port_fails(tmp_path, monkeypatch):
    port = {"reference": "J1", "wall": "left", "blocked": True, "clearance": {"min": {"value": .5}}}
    result, _ = _enclosure_check(tmp_path, monkeypatch, {"ports": [port]}, {"kind": "port_opening_clears_connector", "reference": "J1", "wall": "left"})
    assert result["status"] == "fail"


def test_empty_ports_fail(tmp_path, monkeypatch):
    result, _ = _enclosure_check(tmp_path, monkeypatch, {"ports": []}, {"kind": "port_opening_clears_connector", "reference": "J1", "wall": "left"})
    assert result["status"] == "fail"


@pytest.mark.parametrize(("lids", "expected"), [
    ([{"fitGap": {"min": {"value": .2}, "max": {"value": .4}}, "interference": None}], "pass"),
    ([], "fail"),
    ([{"fitGap": {"min": {"value": .2}, "max": {"value": 1.2}}, "interference": None}], "fail"),
    ([{"fitGap": {"min": {"value": 0}, "max": {"value": 0}}, "interference": {"volume": {"value": 1}}}], "fail"),
])
def test_lid_fit_gap_range(lids, expected, tmp_path, monkeypatch):
    result, _ = _enclosure_check(tmp_path, monkeypatch, {"lids": lids}, {"kind": "lid_fit_gap", "minimum": 0, "maximum": 1})
    assert result["status"] == expected


def test_missing_standoff_fails(tmp_path, monkeypatch):
    inspected = {"standoffs": {"mountingHoleCount": 3, "coveredHoleIds": ["m2-id", "m3-id"], "uncoveredHoleIds": ["npth-id"]}}
    result, _ = _enclosure_check(tmp_path, monkeypatch, inspected, {"kind": "standoffs_at_mounting_holes"})
    assert result["status"] == "fail"


def test_unrelated_parts_do_not_satisfy_base_and_lid_roles(tmp_path, monkeypatch):
    inspected = {"base": {"partId": "base"}, "lids": [], "terminalParts": [{"partId": "x"}, {"partId": "y"}]}
    result, _ = _enclosure_check(tmp_path, monkeypatch, inspected, {"kind": "enclosure_parts"})
    assert result["status"] == "fail"


def test_base_and_lid_part_roles_pass_after_base_edit(tmp_path, monkeypatch):
    inspected = {
        "base": {"partId": "part:base"},
        "lids": [{"featureId": "lid", "partId": "part:lid"}],
        "terminalParts": [
            {"partId": "part:base", "featureId": "j1_cutout"},
            {"partId": "part:lid", "featureId": "lid"},
        ],
    }
    result, _ = _enclosure_check(tmp_path, monkeypatch, inspected, {"kind": "enclosure_parts"})
    assert result["status"] == "pass"


def test_extra_terminal_part_fails(tmp_path, monkeypatch):
    inspected = {
        "base": {"partId": "part:base"},
        "lids": [{"featureId": "lid", "partId": "part:lid"}],
        "terminalParts": [
            {"partId": "part:base"},
            {"partId": "part:lid"},
            {"partId": "part:unexpected"},
        ],
    }
    result, _ = _enclosure_check(tmp_path, monkeypatch, inspected, {"kind": "enclosure_parts"})
    assert result["status"] == "fail"


def test_reference_resolution_uses_regeneration_not_warnings(tmp_path, monkeypatch):
    result, service = _enclosure_check(tmp_path, monkeypatch, {"warnings": [{"code": "CONNECTOR_HEIGHT_UNRESOLVED"}]}, {"kind": "references_resolve"})
    assert result["status"] == "pass"
    assert service.calls == 1


def _staged_pcb_change(tmp_path):
    from evals.run import build_before_state

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    fixture = fixtures / "features.kicad_pcb"
    fixture.write_bytes(Path("evals/fixtures/features.kicad_pcb").read_bytes())
    document = tmp_path / "model.coalesce.json"
    facts = tmp_path / "before-state.json"
    build_before_state("pcb-change", document, fixtures, facts)
    task = next(task for task in load_tasks(Path("evals/tasks")) if task["id"] == "pcb-change")
    task = {
        key: value
        for key, value in task.items()
        if key != "parts"
    }
    task["checks"] = [
        check
        for check in task["checks"]
        if check["kind"] not in {"no_overlapping_parts", "step_validity"}
    ]
    return task, document, facts


def _change_wall(document, feature_id="base"):
    from coalesce.service import DocumentService

    service = DocumentService(document, warmup=False)
    result = service.feature({
        "action": "update",
        "expectedRevision": service.revision,
        "requestId": "change-wall-to-3-mm",
        "featureId": feature_id,
        "kind": "pcb_base",
        "set": {"wallThickness": {"value": 3, "unit": "mm"}},
    })
    assert result["ok"] is True


def test_pcb_change_unchanged_before_state_fails(tmp_path):
    task, document, facts = _staged_pcb_change(tmp_path)
    results = check_task(task, document, tmp_path / "steps", facts)
    wall = next(result for result in results if result["kind"] == "wall_thickness")
    edit = next(result for result in results if result["kind"] == "edit_survival")
    assert wall["status"] == "fail"
    assert wall["actual"] == pytest.approx(2)
    assert edit["status"] == "fail"
    assert "document revision did not advance" in edit["reason"]


def test_pcb_change_rebuilt_with_new_ids_fails(tmp_path):
    task, document, facts = _staged_pcb_change(tmp_path)
    replacements = {
        "base": "replacement-base",
        "mounts": "replacement-mounts",
        "lid": "replacement-lid",
        "port": "replacement-port",
    }

    def replace_ids(value):
        if isinstance(value, dict):
            return {key: replace_ids(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace_ids(item) for item in value]
        return replacements.get(value, value)

    document.write_text(json.dumps(replace_ids(json.loads(document.read_text()))))
    _change_wall(document, "replacement-base")
    results = check_task(task, document, tmp_path / "steps", facts)
    edit = next(result for result in results if result["kind"] == "edit_survival")
    assert edit["status"] == "fail"
    assert edit["reason"] == "saved part or reference identity changed"
    assert edit["actual"]["basePartId"].startswith("replacement-base/")


def _pcb_change_with_document_parts(tmp_path):
    task, document, facts = _staged_pcb_change(tmp_path)
    task["documentParts"] = True
    _change_wall(document)
    return task, document, facts


def _export_document_parts(document, steps):
    from coalesce.service import DocumentService

    service = DocumentService(document, warmup=False)
    measured = service.query({"select": {"kind": "measurements"}})
    assert measured["ok"] is True
    steps.mkdir()
    for index, part in enumerate(measured["parts"]):
        result = service.export({
            "expectedRevision": service.revision,
            "path": str((steps / f"part-{index}.step").resolve()),
            "partIds": [part["partId"]],
        })
        assert result["ok"] is True


def test_pcb_change_correct_in_place_edit_passes(tmp_path):
    task, document, facts = _pcb_change_with_document_parts(tmp_path)
    _export_document_parts(document, tmp_path / "steps")
    results = check_task(task, document, tmp_path / "steps", facts)
    assert all(result["status"] == "pass" for result in results), results


def test_pcb_document_with_unrelated_cube_steps_fails(tmp_path):
    from build123d import Box, Pos, export_step

    task, document, facts = _pcb_change_with_document_parts(tmp_path)
    steps = tmp_path / "steps"
    steps.mkdir()
    export_step(Box(1, 1, 1), steps / "wrong-a.step")
    export_step(Pos(10, 0, 0) * Box(1, 1, 1), steps / "wrong-b.step")
    results = check_task(task, document, steps, facts)
    binding = next(result for result in results if result["kind"] == "document_parts")
    assert binding["status"] == "fail"
    assert len(binding["unmappedSteps"]) == 2
    assert len(binding["unmatchedParts"]) == 2


def test_pcb_stale_step_exported_before_edit_fails(tmp_path):
    task, document, facts = _staged_pcb_change(tmp_path)
    task["documentParts"] = True
    steps = tmp_path / "steps"
    _export_document_parts(document, steps)
    _change_wall(document)
    results = check_task(task, document, steps, facts)
    binding = next(result for result in results if result["kind"] == "document_parts")
    assert binding["status"] == "fail"
    assert binding["unmappedSteps"]
    assert binding["unmatchedParts"]


def test_pcb_duplicate_step_for_one_document_part_fails(tmp_path):
    import shutil

    task, document, facts = _pcb_change_with_document_parts(tmp_path)
    steps = tmp_path / "steps"
    _export_document_parts(document, steps)
    shutil.copy2(steps / "part-0.step", steps / "duplicate.step")
    results = check_task(task, document, steps, facts)
    binding = next(result for result in results if result["kind"] == "document_parts")
    assert binding["status"] == "fail"
    assert len(binding["duplicateParts"]) == 1


def test_one_enclosure_inspection_per_run(tmp_path, monkeypatch):
    inspected = {"base": {"minimumWallThickness": {"value": 2}}, "warnings": []}
    service = _inspection(monkeypatch, inspected)
    steps = tmp_path / "steps"
    steps.mkdir()
    checks = [{"kind": "wall_thickness", "minimum": 1}, {"kind": "references_resolve"}]
    assert [item["status"] for item in check_task({"checks": checks}, _document(tmp_path / "model.coalesce.json"), steps)] == ["pass", "pass"]
    assert service.calls == 1


def test_empty_checks_rejected(tmp_path):
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    (task_dir / "empty.json").write_text(json.dumps({"id": "empty", "tier": "easy", "prompt": "x", "fixtures": [], "checks": []}))
    with pytest.raises(ValueError, match="non-empty"):
        load_tasks(task_dir)


def test_empty_expected_hole_geometry_rejected(tmp_path):
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    (task_dir / "empty.json").write_text(json.dumps({"id": "empty", "tier": "easy", "prompt": "x", "fixtures": [], "checks": [{"kind": "hole_positions", "positions": []}]}))
    with pytest.raises(ValueError, match="positions"):
        load_tasks(task_dir)


def test_plate_cut_has_no_irrelevant_overlap_check():
    task = next(task for task in load_tasks(Path("evals/tasks")) if task["id"] == "plate-cut")
    assert "no_overlapping_parts" not in {check["kind"] for check in task["checks"]}


def test_claude_stream_transcript_metadata():
    lines = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__coalesce__create"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__coalesce__bad"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True}]}},
        {"type": "result", "result": "done", "total_cost_usd": 0.0123},
    ]
    metadata, final = _claude_metadata("\n".join(json.dumps(line) for line in lines))
    assert metadata == {
        "tool_calls": 2,
        "failed_tool_calls": 1,
        "transcript_parsed": True,
        "cost_usd": 0.0123,
    }
    assert final == "done"


def test_zeta_json_transcript_metadata_from_real_shape():
    transcript = """{"type":"tool_call","name":"coalesce__create"}
{"type":"tool_result","is_error":false}
{"type":"tool_call","name":"coalesce__export"}
{"type":"tool_result","is_error":true}
{"type":"usage","usage":{"output_tokens":12}}
{"type":"message","role":"assistant","text":"done"}"""
    metadata, final = _zeta_metadata(transcript)
    assert metadata == {"tool_calls": 2, "failed_tool_calls": 1, "transcript_parsed": True}
    assert final == "done"


def test_zeta_fixture_staging_refuses_existing_path(tmp_path, monkeypatch):
    import evals.run
    from evals.run import zeta_harness

    monkeypatch.setattr(evals.run, "_git_state", lambda _path: ("same", False))

    run_dir = tmp_path / "run"
    fixtures = run_dir / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "features.kicad_pcb").write_text("fixture")
    project = tmp_path / "project"
    (project / ".zeta").mkdir(parents=True)
    (project / ".zeta" / "mcp.json").write_text(json.dumps({
        "servers": {"coalesce": {"args": ["--directory", str(Path.cwd())]}}
    }))
    (project / "coalesce-eval-features.kicad_pcb").write_text("do not replace")
    with pytest.raises(FileExistsError, match="coalesce-eval-features.kicad_pcb"):
        zeta_harness("prompt", run_dir, model="claude-haiku-4-5", zeta_project=project,
                     revision=REVISION)
    assert (project / "coalesce-eval-features.kicad_pcb").read_text() == "do not replace"


def _zeta_project(tmp_path, directory):
    project = tmp_path / "project"
    (project / ".zeta").mkdir(parents=True)
    config = json.loads(Path("tests/fixtures/mcp.json").read_text())
    config["servers"]["coalesce"]["args"][config["servers"]["coalesce"]["args"].index("--directory") + 1] = str(directory)
    (project / ".zeta" / "mcp.json").write_text(json.dumps(config))
    return project


def test_zeta_relative_server_directory_resolves_from_project(tmp_path):
    from evals.run import _zeta_server_directory

    project = _zeta_project(tmp_path, Path("relative-server"))
    assert _zeta_server_directory(project) == (project / "relative-server").resolve()


def test_zeta_dirty_server_fails_before_staging(tmp_path, monkeypatch):
    import evals.run
    from evals.run import zeta_harness

    calls = []
    monkeypatch.setattr(evals.run, "_git_state", lambda path: ("same", path != Path("source")))
    def fake_run(*args, **kwargs):
        calls.append(args[0])
        return type("Result", (), {"stdout": " M dirty-file", "stderr": "", "returncode": 0})()
    monkeypatch.setattr(evals.run.subprocess, "run", fake_run)
    run_dir = tmp_path / "run"
    (run_dir / "fixtures").mkdir(parents=True)
    project = _zeta_project(tmp_path, Path("server"))
    with pytest.raises(RuntimeError, match="coalesce server checkout.*dirty-file.*commit or clean"):
        evals.run._preflight(zeta_harness, Path("source"), project)
    assert not list((tmp_path / "project").glob("coalesce-eval-*"))
    assert calls == [["git", "-C", str(tmp_path / "project" / "server"), "status", "--porcelain"]]


def test_zeta_mismatched_server_revision_fails_closed(tmp_path, monkeypatch):
    import evals.run
    from evals.run import zeta_harness

    monkeypatch.setattr(evals.run, "_git_state", lambda path: ("under-test" if path == Path("source") else "stale", False))
    monkeypatch.setattr(evals.run.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("zeta was called"))
    run_dir = tmp_path / "run"
    (run_dir / "fixtures").mkdir(parents=True)
    project = _zeta_project(tmp_path, Path("server"))
    with pytest.raises(RuntimeError, match="git -C .* checkout --detach under-test"):
        evals.run._preflight(zeta_harness, Path("source"), project)
    assert not (run_dir / "transcript.json").exists()


def test_zeta_matching_server_revision_runs(tmp_path, monkeypatch):
    import evals.run
    from evals.run import zeta_harness

    monkeypatch.setattr(evals.run, "_git_state", lambda _path: ("same", False))
    def fake_run(*_args, **_kwargs):
        return type("Result", (), {"stdout": '{"type":"message","role":"assistant","text":"done"}', "stderr": "", "returncode": 0})()
    monkeypatch.setattr(evals.run.subprocess, "run", fake_run)
    run_dir = tmp_path / "run"
    (run_dir / "fixtures").mkdir(parents=True)
    project = _zeta_project(tmp_path, Path("server"))
    revision = evals.run._preflight(zeta_harness, Path("source"), project)
    result = zeta_harness("prompt", run_dir, model="m", zeta_project=project, revision=revision)
    assert result["coalesce_revision"] == "same"


@pytest.mark.parametrize("config", ["not json", {"servers": {"coalesce": {"args": []}}}])
def test_zeta_invalid_server_config_fails_closed(tmp_path, monkeypatch, config):
    import evals.run
    from evals.run import zeta_harness

    project = tmp_path / "project"
    (project / ".zeta").mkdir(parents=True)
    (project / ".zeta" / "mcp.json").write_text(config if isinstance(config, str) else json.dumps(config))
    monkeypatch.setattr(evals.run, "_git_state", lambda _path: ("same", False))
    with pytest.raises(RuntimeError, match="(parse.*config|config .*names no)"):
        evals.run._preflight(zeta_harness, Path("source"), project)


def test_claude_dirty_checkout_fails_closed(tmp_path, monkeypatch):
    import evals.run
    from evals.run import claude_harness

    monkeypatch.setattr(evals.run, "_git_state", lambda _path: ("revision", True))
    monkeypatch.setattr(evals.run.subprocess, "run", lambda *_args, **_kwargs: type("Result", (), {"stdout": " M dirty-file", "stderr": "", "returncode": 0})())
    (tmp_path / "fixtures").mkdir()
    with pytest.raises(RuntimeError, match="coalesce checkout.*dirty-file.*commit or clean"):
        evals.run._preflight(claude_harness, Path("source"))


def test_claude_disables_builtin_tools_and_settings(tmp_path, monkeypatch):
    import evals.run
    from evals.run import claude_harness

    seen = {}

    def fake_run(command, **_kwargs):
        seen["command"] = command
        return type("Result", (), {"stdout": "", "stderr": "", "returncode": 0})()

    monkeypatch.setattr(evals.run.subprocess, "run", fake_run)
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    fixture = fixtures / "features.kicad_pcb"
    fixture.write_text("fixture")
    claude_harness("prompt", tmp_path, model="claude-haiku-4-5", revision=REVISION)
    assert str(fixture) in seen["command"][2]
    assert seen["command"][seen["command"].index("--tools") + 1] == ""
    assert seen["command"][seen["command"].index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in seen["command"]
    assert seen["command"][seen["command"].index("--allowedTools") + 1] == "mcp__coalesce__*"


@pytest.mark.parametrize("harness", ["claude", "zeta"])
def test_harness_instruction_says_no_human_is_available(tmp_path, monkeypatch, harness):
    import evals.run
    from evals.run import HEADLESS, claude_harness, zeta_harness

    seen = {}

    def fake_run(command, **_kwargs):
        seen["command"] = command
        return type("Result", (), {"stdout": "", "stderr": "", "returncode": 0})()

    monkeypatch.setattr(evals.run.subprocess, "run", fake_run)
    run_dir = tmp_path / "run"
    (run_dir / "fixtures").mkdir(parents=True)
    if harness == "claude":
        claude_harness("prompt", run_dir, model="claude-haiku-4-5", revision=REVISION)
    else:
        project = tmp_path / "project"
        (project / ".zeta").mkdir(parents=True)
        (project / ".zeta" / "mcp.json").write_text(json.dumps({
            "servers": {"coalesce": {"args": ["--directory", str(Path.cwd())]}}
        }))
        zeta_harness("prompt", run_dir, model="claude-haiku-4-5", zeta_project=project,
                     revision=REVISION)
    assert seen["command"][2].count(HEADLESS) == 1


def test_checks_pass_fail_unknown(tmp_path):
    from build123d import Box, export_step

    document = _document(tmp_path / "model.coalesce.json")
    steps = tmp_path / "steps"
    steps.mkdir()
    export_step(Box(60, 40, 6), steps / "plate.step")
    task = {"id": "x", "checks": [
        {"kind": "bbox", "size": [60, 40, 6], "tolerance": .01},
        {"kind": "bbox", "size": [1, 2, 3], "tolerance": .01},
        {"kind": "document_value", "path": "missing", "equals": 1},
    ]}
    results = check_task(task, document, steps)
    assert [result["status"] for result in results] == ["pass", "fail", "unknown"]


def test_task_files_load_and_validate():
    tasks = load_tasks(Path("evals/tasks"))
    assert len(tasks) == 5
    assert {task["tier"] for task in tasks} == {"easy", "medium", "hard"}


def test_harness_nonzero_returncode_fails_with_good_artifacts(tmp_path, validated):
    from build123d import export_step

    from evals.run import run_tasks

    def failed_harness(_prompt, run_dir, **_kwargs):
        _document(run_dir / "model.coalesce.json")
        (run_dir / "steps").mkdir()
        export_step(
            _plate_with_holes(),
            run_dir / "steps" / "plate.step",
        )
        return {"returncode": 1, "transcript_parsed": True}

    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    (task_dir / "plate-holes.json").write_text(
        Path("evals/tasks/plate_holes.json").read_text()
    )
    rows = run_tasks(task_dir, tmp_path / "out", ["plate-holes"], ["fake"], failed_harness, coalesce_path=Path.cwd())
    assert rows[0]["overall"] is False
    assert rows[0]["reason"] == "harness exited with return code 1"


@pytest.mark.parametrize(("metadata", "reason"), [
    ({"error": "agent failed", "returncode": 0, "transcript_parsed": True},
     "harness error: agent failed"),
    ({"exception": "runner crashed", "returncode": 0, "transcript_parsed": True},
     "harness exception: runner crashed"),
    ({"returncode": 0, "transcript_parsed": False}, "harness transcript was not parsed"),
])
def test_harness_error_or_unparsed_transcript_prevents_scoring(tmp_path, metadata, reason):
    from evals.run import _run_result

    row = _run_result({"id": "x"}, "fake", tmp_path, metadata)
    assert row["overall"] is False
    assert row["checks"] == []
    assert row["reason"] == reason


def test_existing_run_directory_is_rejected_before_reading(tmp_path, validated):
    from evals.run import run_tasks

    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    (task_dir / "one.json").write_text(json.dumps({
        "id": "one", "tier": "easy", "prompt": "new", "fixtures": [],
        "checks": [{"kind": "step_validity"}],
    }))
    run_dir = tmp_path / "out" / "one" / "fake"
    run_dir.mkdir(parents=True)
    prompt = run_dir / "prompt.txt"
    prompt.write_text("existing")

    with pytest.raises(FileExistsError, match="run directory already exists"):
        run_tasks(task_dir, tmp_path / "out", ["one"], ["fake"], lambda *_a, **_k: {}, coalesce_path=Path.cwd())
    assert prompt.read_text() == "existing"


def test_runner_fake_harness(tmp_path, validated):
    from evals.run import run_tasks

    def fake(prompt, run_dir, **kwargs):
        _document(run_dir / "model.coalesce.json")
        (run_dir / "steps").mkdir()
        return {
            "tool_calls": 1,
            "failed_tool_calls": 0,
            "returncode": 0,
            "transcript_parsed": True,
        }

    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    (task_dir / "one.json").write_text(json.dumps({
        "id": "one", "tier": "easy", "prompt": "make a thing", "fixtures": [],
        "checks": [{"kind": "document_value", "path": "missing", "equals": 1}],
    }))
    out = tmp_path / "out"
    rows = run_tasks(task_dir, out, ["one"], ["fake"], fake, coalesce_path=Path.cwd())
    assert rows[0]["overall"] is False
    assert (out / "scorecard.md").exists()
    assert len((out / "results.jsonl").read_text().splitlines()) == 1
