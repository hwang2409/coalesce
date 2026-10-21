import json
from pathlib import Path

import pytest

from evals.checks import check_task
from evals.run import _claude_metadata, _zeta_metadata
from evals.tasks import load_tasks


def _document(path):
    path.write_text(json.dumps({"revision": 0, "graph": {"nodes": []}}))
    return path


def _inspection(monkeypatch, inspected):
    import coalesce.service
    from coalesce.graph import PcbBase

    class Graph:
        def ordered(self):
            return [PcbBase(id="base")]

    class FakeService:
        calls = 0

        def __init__(self, *_args, **_kwargs):
            self._document = type("Document", (), {"graph": Graph()})()

        def _ensure_geometry(self):
            FakeService.calls += 1

        def query(self, _query):
            return inspected

    monkeypatch.setattr(coalesce.service, "DocumentService", FakeService)
    return FakeService


def _enclosure_check(tmp_path, monkeypatch, inspected, check):
    service = _inspection(monkeypatch, inspected)
    steps = tmp_path / "steps"
    steps.mkdir()
    results = check_task({"checks": [check]}, _document(tmp_path / "model.coalesce.json"), steps)
    return results[0], service


def test_root_only_step_does_not_count(tmp_path):
    from build123d import Box, export_step

    from evals.run import run_tasks

    def fake(_prompt, run_dir, **_kwargs):
        _document(run_dir / "model.coalesce.json")
        export_step(Box(60, 40, 6), run_dir / "stray.step")
        return {}

    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    (task_dir / "root.json").write_text(json.dumps({
        "id": "root", "tier": "easy", "prompt": "x", "fixtures": [],
        "checks": [{"kind": "step_validity"}],
    }))
    rows = run_tasks(task_dir, tmp_path / "out", ["root"], ["fake"], fake)
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
    inspected = {"baseId": "base", "lids": [], "terminalParts": [{"featureId": "x"}, {"featureId": "y"}]}
    result, _ = _enclosure_check(tmp_path, monkeypatch, inspected, {"kind": "enclosure_parts"})
    assert result["status"] == "fail"


def test_reference_resolution_uses_regeneration_not_warnings(tmp_path, monkeypatch):
    result, service = _enclosure_check(tmp_path, monkeypatch, {"warnings": [{"code": "CONNECTOR_HEIGHT_UNRESOLVED"}]}, {"kind": "references_resolve"})
    assert result["status"] == "pass"
    assert service.calls == 1


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
    assert metadata == {"tool_calls": 2, "failed_tool_calls": 1, "cost_usd": 0.0123}
    assert final == "done"


def test_zeta_json_transcript_metadata_from_real_shape():
    transcript = """{"type":"tool_call","name":"coalesce__create"}
{"type":"tool_result","is_error":false}
{"type":"tool_call","name":"coalesce__export"}
{"type":"tool_result","is_error":true}
{"type":"usage","usage":{"output_tokens":12}}
{"type":"message","role":"assistant","text":"done"}"""
    metadata, final = _zeta_metadata(transcript)
    assert metadata == {"tool_calls": 2, "failed_tool_calls": 1}
    assert final == "done"


def test_zeta_fixture_staging_refuses_existing_path(tmp_path):
    from evals.run import zeta_harness

    run_dir = tmp_path / "run"
    fixtures = run_dir / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "features.kicad_pcb").write_text("fixture")
    project = tmp_path / "project"
    project.mkdir()
    (project / "coalesce-eval-features.kicad_pcb").write_text("do not replace")
    with pytest.raises(FileExistsError, match="coalesce-eval-features.kicad_pcb"):
        zeta_harness("prompt", run_dir, model="claude-haiku-4-5", zeta_project=project)
    assert (project / "coalesce-eval-features.kicad_pcb").read_text() == "do not replace"


def test_claude_disables_builtin_tools_and_settings(tmp_path, monkeypatch):
    import evals.run
    from evals.run import claude_harness

    seen = {}

    def fake_run(command, **_kwargs):
        seen["command"] = command
        return type("Result", (), {"stdout": "", "stderr": "", "returncode": 0})()

    monkeypatch.setattr(evals.run.subprocess, "run", fake_run)
    claude_harness("prompt", tmp_path, model="claude-haiku-4-5", coalesce_path=Path.cwd())
    assert seen["command"][seen["command"].index("--tools") + 1] == ""
    assert seen["command"][seen["command"].index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in seen["command"]
    assert seen["command"][seen["command"].index("--allowedTools") + 1] == "mcp__coalesce__*"


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


def test_runner_fake_harness(tmp_path):
    from evals.run import run_tasks

    def fake(prompt, run_dir, **kwargs):
        _document(run_dir / "model.coalesce.json")
        (run_dir / "steps").mkdir()
        return {"tool_calls": 1, "failed_tool_calls": 0}

    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    (task_dir / "one.json").write_text(json.dumps({
        "id": "one", "tier": "easy", "prompt": "make a thing", "fixtures": [],
        "checks": [{"kind": "document_value", "path": "missing", "equals": 1}],
    }))
    out = tmp_path / "out"
    rows = run_tasks(task_dir, out, ["one"], ["fake"], fake)
    assert rows[0]["overall"] is False
    assert (out / "scorecard.md").exists()
    assert len((out / "results.jsonl").read_text().splitlines()) == 1
