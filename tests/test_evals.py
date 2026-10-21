import json
from pathlib import Path

from evals.checks import check_task
from evals.tasks import load_tasks


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
