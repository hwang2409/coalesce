from __future__ import annotations

import json
from pathlib import Path

REQUIRED = {"id", "tier", "prompt", "fixtures", "checks"}
EXPECTED_GEOMETRY = {"bbox": "size", "hole_positions": "positions"}


def _validate_checks(path: Path, checks: object) -> None:
    if not isinstance(checks, list):
        raise TypeError(f"invalid checks in {path}")
    if not checks:
        raise ValueError(f"invalid checks in {path}: checks must be non-empty")
    for check in checks:
        field = EXPECTED_GEOMETRY.get(check.get("kind"))
        if field and not check.get(field):
            raise ValueError(f"invalid {field} in {path}: expected geometry must be non-empty")
        if check.get("kind") == "hole_positions" and not check.get("diameter"):
            raise ValueError(f"invalid diameter in {path}: expected geometry must be non-empty")
        if check.get("kind") == "port_opening_clears_connector" and not all(
            check.get(field) for field in ("reference", "wall")
        ):
            raise ValueError(f"invalid port identity in {path}")


def load_tasks(directory: Path) -> list[dict]:
    tasks = []
    for path in sorted(directory.glob("*.json")):
        task = json.loads(path.read_text())
        missing = REQUIRED - task.keys()
        if missing or task["tier"] not in {"easy", "medium", "hard"}:
            raise ValueError(f"invalid task {path}: missing {sorted(missing)}")
        _validate_checks(path, task["checks"])
        tasks.append(task)
    if len({task["id"] for task in tasks}) != len(tasks):
        raise ValueError("duplicate task id")
    return tasks


def select_tasks(directory: Path, selector: str) -> list[dict]:
    tasks = load_tasks(directory)
    return [
        task
        for task in tasks
        if selector == "all" or task["id"] == selector or task["tier"] == selector
    ]
