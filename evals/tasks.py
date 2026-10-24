from __future__ import annotations

import json
from pathlib import Path

REQUIRED = {"id", "tier", "prompt", "fixtures", "checks"}
EXPECTED_GEOMETRY = {
    "bbox": "size",
    "hole_positions": "positions",
    "part_hole_positions": "holes",
}


def _validate_expected_geometry(path, check):
    field = EXPECTED_GEOMETRY[check["kind"]]
    if not check.get(field):
        raise ValueError(f"invalid {field} in {path}: expected geometry must be non-empty")


def _validate_hole_positions(path, check):
    if not check.get("diameter"):
        raise ValueError(f"invalid diameter in {path}: expected geometry must be non-empty")


def _validate_part_hole_positions(path, check):
    if not check.get("feature") or any(
        not hole.get("position") or not hole.get("diameter") for hole in check["holes"]
    ):
        raise ValueError(f"invalid part hole geometry in {path}")


def _validate_document_state(path, check):
    if "features" not in check:
        raise ValueError(f"invalid document state in {path}")


def _validate_port_opening(path, check):
    if not all(check.get(field) for field in ("reference", "wall")):
        raise ValueError(f"invalid port identity in {path}")


def _validate_runner_feature_edit(path, check):
    if not check.get("feature") or not check.get("before"):
        raise ValueError(f"invalid runner feature edit in {path}")


_CHECK_VALIDATORS = {
    "bbox": (_validate_expected_geometry,),
    "hole_positions": (_validate_expected_geometry, _validate_hole_positions),
    "part_hole_positions": (_validate_expected_geometry, _validate_part_hole_positions),
    "document_state": (_validate_document_state,),
    "port_opening_clears_connector": (_validate_port_opening,),
    "runner_feature_edit": (_validate_runner_feature_edit,),
}


def _validate_checks(path: Path, checks: object) -> None:
    if not isinstance(checks, list):
        raise TypeError(f"invalid checks in {path}")
    if not checks:
        raise ValueError(f"invalid checks in {path}: checks must be non-empty")
    for check in checks:
        for validate in _CHECK_VALIDATORS.get(check.get("kind"), ()):
            validate(path, check)


def load_tasks(directory: Path) -> list[dict]:
    tasks = []
    for path in sorted(directory.glob("*.json")):
        task = json.loads(path.read_text())
        missing = REQUIRED - task.keys()
        if missing or task["tier"] not in {"easy", "medium", "hard"}:
            raise ValueError(f"invalid task {path}: missing {sorted(missing)}")
        if "parts" in task and (
            not isinstance(task["parts"], int) or isinstance(task["parts"], bool) or task["parts"] < 1
        ):
            raise ValueError(f"invalid parts in {path}: expected a positive integer")
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
