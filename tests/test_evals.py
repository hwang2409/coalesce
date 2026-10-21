import json
from pathlib import Path

from evals.checks import check_task
from evals.run import _claude_metadata
from evals.tasks import load_tasks


def test_real_artifact_shape_scores_from_run_root(tmp_path):
    from build123d import Align, Box, Cylinder, Pos, export_step

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "model.coalesce.json").write_text(json.dumps({"formatVersion": 1, "graph": {"nodes": []}}))
    plate = Box(60, 40, 6, align=(Align.MIN, Align.MIN, Align.MIN))
    for x, y in ((5, 5), (55, 5), (5, 35), (55, 35)):
        plate -= Pos(x, y, 0) * Cylinder(1.7, 6)
    export_step(plate, run_dir / "plate_60x40x6_m3_holes.step")
    task = {"checks": [
        {"kind": "bbox", "size": [60, 40, 6], "tolerance": .1},
        {"kind": "hole_positions", "positions": [[5, 5], [55, 5], [5, 35], [55, 35]], "tolerance": .2},
        {"kind": "step_validity"},
    ]}
    assert [result["status"] for result in check_task(task, run_dir / "model.coalesce.json", run_dir)] == ["pass"] * 3


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


def test_checks_pass_fail_unknown(tmp_path):
    from build123d import Box, export_step
    document = tmp_path / "model.coalesce.json"
    document.write_text(json.dumps({"revision": 0, "graph": {"nodes": []}}))
    steps = tmp_path / "steps"; steps.mkdir()
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
        (run_dir / "model.coalesce.json").write_text(
            json.dumps({"revision": 0, "graph": {"nodes": []}})
        )
        (run_dir / "steps").mkdir()
        return {"tool_calls": 1, "failed_tool_calls": 0}

    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    (task_dir / "one.json").write_text(json.dumps({
        "id": "one", "tier": "easy", "prompt": "make a thing", "fixtures": [],
        "checks": [{"kind": "document_value", "path": "missing", "equals": 1}],
    }))
    out = tmp_path / "out"
    run_tasks(task_dir, out, ["one"], ["fake"], fake)
    assert (out / "scorecard.md").exists()
    assert len((out / "results.jsonl").read_text().splitlines()) == 1
