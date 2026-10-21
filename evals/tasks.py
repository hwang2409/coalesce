from __future__ import annotations

import json
from pathlib import Path

REQUIRED = {"id", "tier", "prompt", "fixtures", "checks"}
def load_tasks(directory: Path) -> list[dict]:
    tasks = []
    for path in sorted(directory.glob("*.json")):
        task = json.loads(path.read_text())
        missing = REQUIRED - task.keys()
        if missing or task["tier"] not in {"easy", "medium", "hard"}:
            raise ValueError(f"invalid task {path}: missing {sorted(missing)}")
        if not isinstance(task["checks"], list):
            raise TypeError(f"invalid checks in {path}")
        tasks.append(task)
    if len({task["id"] for task in tasks}) != len(tasks):
        raise ValueError("duplicate task id")
    return tasks

def select_tasks(directory: Path, selector: str) -> list[dict]:
    tasks = load_tasks(directory)
    return [task for task in tasks if selector == "all" or task["id"] == selector or task["tier"] == selector]
