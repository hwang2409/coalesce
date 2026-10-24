import json
import subprocess
import sys
from pathlib import Path

import pytest

from evals.checks import check_task
from evals.run import build_before_state
from evals.tasks import load_tasks


FINAL_POINTS = [
    {"id": "axis_left", "x": 0, "y": 0},
    {"id": "axis_right", "x": 30, "y": 0},
    {"id": "small_end", "x": 30, "y": 8},
    {"id": "shoulder_inner", "x": 20, "y": 8},
    {"id": "shoulder_outer", "x": 20, "y": 10},
    {"id": "large_end", "x": 0, "y": 10},
]


def _task():
    return next(task for task in load_tasks(Path("evals/tasks")) if task["id"] == "revolve-shaft-edit")


def _artifact(tmp_path):
    from coalesce.service import DocumentService

    document = tmp_path / "model.coalesce.json"
    before = tmp_path / "before-state.json"
    steps = tmp_path / "steps"
    steps.mkdir()
    build_before_state("revolve-shaft-edit", document, tmp_path, before)
    service = DocumentService(document, warmup=False)
    updated = service.sketch({
        "action": "update",
        "expectedRevision": 3,
        "requestId": "test-diameter-update",
        "sketchId": "profile",
        "set": {
            "entities": [{
                "id": "outline",
                "kind": "polyline",
                "points": FINAL_POINTS,
                "closed": True,
            }]
        },
    })
    assert updated["ok"] is True
    measured = service.query({"select": {"kind": "measurements"}})
    assert measured["ok"] is True
    [part] = measured["parts"]
    exported = service.export({
        "expectedRevision": updated["revision"],
        "partIds": [part["partId"]],
        "path": str(steps / "shaft.step"),
    })
    assert exported["ok"] is True
    return document, before, steps


def _statuses(document, before, steps):
    return check_task(_task(), document, steps, before)


def test_revolve_shaft_golden_runs_through_stdio(tmp_path):
    out = tmp_path / "golden"
    result = subprocess.run(
        [sys.executable, "-m", "evals.golden.revolve_shaft_edit", str(out)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    results = json.loads((out / "golden-results.json").read_text())
    assert results and all(result["status"] == "pass" for result in results)


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong shoulder radius",
        "rebuilt revolve",
        "rebuilt fillet",
        "removed fillet",
        "wrong cylinder radius",
        "extra solid",
    ],
)
def test_revolve_shaft_mutations_fail_closed(tmp_path, mutation):
    from build123d import Box, export_step

    document, before, steps = _artifact(tmp_path)
    payload = json.loads(document.read_text())
    nodes = payload["graph"]["nodes"]
    if mutation == "wrong shoulder radius":
        next(node for node in nodes if node["id"] == "shoulder_fillet")["radius"] = 0.75
        document.write_text(json.dumps(payload))
    elif mutation == "rebuilt revolve":
        next(node for node in nodes if node["id"] == "shaft")["id"] = "rebuilt_shaft"
        document.write_text(json.dumps(payload))
    elif mutation == "rebuilt fillet":
        next(node for node in nodes if node["id"] == "shoulder_fillet")["id"] = "rebuilt_fillet"
        document.write_text(json.dumps(payload))
    elif mutation == "removed fillet":
        payload["graph"]["nodes"] = [node for node in nodes if node["id"] != "shoulder_fillet"]
        document.write_text(json.dumps(payload))
    elif mutation == "wrong cylinder radius":
        export_step(Box(30, 14, 14), steps / "shaft.step")
    elif mutation == "extra solid":
        export_step(Box(1, 1, 1), steps / "extra.step")

    results = _statuses(document, before, steps)
    assert results and any(result["status"] != "pass" for result in results)
