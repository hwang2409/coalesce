from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

from .checks import check_task, identity_snapshot
from .tasks import select_tasks


def _quantity(value):
    return {"value": value, "unit": "mm"}


def _require_ok(result):
    if result.get("ok") is not True:
        raise RuntimeError(f"before-state build failed: {result}")


def _build_pcb_change(document, fixture_dir):
    from coalesce.service import DocumentService

    service = DocumentService(document, warmup=False)
    _require_ok(service.import_board({
        "action": "create",
        "expectedRevision": 0,
        "requestId": "eval-import-board",
        "assetId": "board",
        "format": "kicad_pcb",
        "path": str((fixture_dir / "features.kicad_pcb").resolve()),
    }))
    _require_ok(service.feature({
        "action": "create",
        "expectedRevision": 1,
        "requestId": "eval-create-base",
        "featureId": "base",
        "kind": "pcb_base",
        "parameters": {
            "board": {"featureId": "board"},
            "wallThickness": _quantity(2),
            "floorThickness": _quantity(2),
            "clearance": _quantity(1),
            "wallHeightAboveBoard": _quantity(4),
            "boardSeatHeight": _quantity(1),
        },
    }))
    graph = service.query({"select": {"kind": "graph"}})
    _require_ok(graph)
    board = next(node for node in graph["graph"]["nodes"] if node["id"] == "board")
    mounting_footprints = {
        item["id"]
        for item in board["mechanics"]["footprints"]
        if item["libraryId"].startswith("MountingHole:")
    }
    mounting_holes = [
        item["id"]
        for item in board["mechanics"]["holes"]
        if item["footprintId"] in mounting_footprints
    ]
    _require_ok(service.feature({
        "action": "create",
        "expectedRevision": 2,
        "requestId": "eval-create-standoffs",
        "featureId": "mounts",
        "kind": "standoffs",
        "parameters": {
            "target": {"featureId": "base"},
            "board": {"featureId": "board"},
            "selection": {"mode": "explicit", "holeIds": mounting_holes},
            "bossDiameter": _quantity(5),
            "pilotDiameter": _quantity(1),
        },
    }))
    _require_ok(service.feature({
        "action": "create",
        "expectedRevision": 3,
        "requestId": "eval-create-lid",
        "featureId": "lid",
        "kind": "pcb_lid",
        "parameters": {
            "base": {"featureId": "base"},
            "thickness": _quantity(2),
            "fitGap": _quantity(0.25),
        },
    }))
    _require_ok(service.feature({
        "action": "create",
        "expectedRevision": 4,
        "requestId": "eval-create-port",
        "featureId": "port",
        "kind": "port_cutout",
        "parameters": {
            "base": {"featureId": "base"},
            "footprint": "J1",
            "wall": "left",
            "clearance": _quantity(0.5),
            "envelope": {
                "source": "courtyard",
                "height": _quantity(1),
                "bottomOffset": _quantity(0),
            },
        },
    }))
    inspected = service.query({"select": {"kind": "enclosure", "base": "base"}})
    port = next(item for item in inspected["ports"] if item["featureId"] == "port")
    wall = inspected["base"]["minimumWallThickness"]["value"]
    if not 1.9 <= wall <= 2.1:
        raise RuntimeError(f"before-state wall is not 2 mm: {wall}")
    return {
        "revision": service.revision,
        "wallThickness": wall,
        **identity_snapshot(inspected, port["featureId"]),
    }


_BEFORE_STATE_BUILDERS = {"pcb-change": _build_pcb_change}


def build_before_state(name, document, fixture_dir, facts_path):
    if document.exists():
        raise FileExistsError(f"runner document already exists: {document}")
    try:
        builder = _BEFORE_STATE_BUILDERS[name]
    except KeyError as error:
        raise ValueError(f"unknown before-state builder: {name}") from error
    facts = builder(document, fixture_dir)
    facts_path.write_text(json.dumps(facts, indent=2))
    return facts


def _json_events(transcript):
    for line in transcript.splitlines():
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _claude_message_counts(event):
    message = event.get("message", {})
    content = message.get("content", []) if isinstance(message, dict) else []
    tools = sum(item.get("type") == "tool_use" for item in content if isinstance(item, dict))
    failures = sum(
        item.get("type") == "tool_result" and item.get("is_error", False)
        for item in content
        if isinstance(item, dict)
    )
    return tools, failures


def _claude_metadata(transcript):
    metadata = {"tool_calls": 0, "failed_tool_calls": 0, "transcript_parsed": False}
    final = ""
    for event in _json_events(transcript):
        tools, failures = _claude_message_counts(event)
        if event.get("type") == "assistant":
            metadata["tool_calls"] += tools
        if event.get("type") == "user":
            metadata["failed_tool_calls"] += failures
        if event.get("type") == "result":
            metadata["transcript_parsed"] = True
            final = event.get("result", "")
            if event.get("is_error"):
                metadata["error"] = final or event.get("subtype") or "unknown harness error"
            if event.get("total_cost_usd") is not None:
                metadata["cost_usd"] = event["total_cost_usd"]
    return metadata, final


def _zeta_metadata(transcript):
    metadata = {"tool_calls": 0, "failed_tool_calls": 0, "transcript_parsed": False}
    final = ""
    for event in _json_events(transcript):
        event_type = event.get("type")
        metadata["tool_calls"] += event_type == "tool_call"
        metadata["failed_tool_calls"] += event_type == "tool_result" and event.get("is_error", False)
        if event_type == "message" and event.get("role") == "assistant":
            metadata["transcript_parsed"] = True
            final = event.get("text", "")
        cost = event.get("cost_usd") or (event.get("usage") or {}).get("cost_usd")
        if cost is not None:
            metadata["cost_usd"] = cost
    return metadata, final


def claude_harness(prompt, run_dir, *, model, coalesce_path, before_state=None, **_):
    if before_state:
        build_before_state(
            before_state,
            run_dir / "model.coalesce.json",
            run_dir / "fixtures",
            run_dir / "before-state.json",
        )
    config = run_dir / "mcp.json"
    config.write_text(json.dumps({
        "mcpServers": {
            "coalesce": {
                "command": "uv",
                "args": [
                    "run", "--project", str(coalesce_path), "coalesce", "--document",
                    str(run_dir / "model.coalesce.json"),
                ],
            }
        }
    }, indent=2))
    (run_dir / "steps").mkdir(exist_ok=True)
    fixture_paths = sorted((run_dir / "fixtures").glob("*"))
    fixture_text = ", ".join(str(path) for path in fixture_paths) or "none"
    instruction = (
        prompt
        + f"\nThe task fixtures are at these exact paths: {fixture_text}."
        + f" Use the coalesce MCP document at {run_dir / 'model.coalesce.json'}"
        + f" and export every STEP file directly under {run_dir / 'steps'};"
        + " do not write artifacts elsewhere."
    )
    command = [
        "claude", "-p", instruction, "--model", model, "--mcp-config", str(config),
        "--strict-mcp-config", "--setting-sources", "", "--tools", "",
        "--output-format", "stream-json", "--verbose", "--allowedTools", "mcp__coalesce__*",
    ]
    started = time.monotonic()
    result = subprocess.run(
        command, cwd=run_dir, capture_output=True, text=True, check=False
    )
    (run_dir / "transcript.jsonl").write_text(result.stdout)
    metadata, final = _claude_metadata(result.stdout)
    (run_dir / "transcript.txt").write_text(final + ("\n" + result.stderr if result.stderr else ""))
    return {"wall_time": time.monotonic() - started, **metadata, "returncode": result.returncode}


def _stage_zeta_fixtures(run_dir, zeta_project):
    sources = sorted((run_dir / "fixtures").iterdir())
    targets = [zeta_project / f"coalesce-eval-{source.name}" for source in sources]
    existing = next((target for target in targets if target.exists()), None)
    if existing:
        raise FileExistsError(f"zeta project already contains {existing.name}")
    for source, target in zip(sources, targets, strict=True):
        shutil.copy2(source, target)
    return targets


def zeta_harness(prompt, run_dir, *, model, zeta_project, before_state=None, **_):
    """Run in a trusted project while owning every staged input and output path."""
    fixed = zeta_project / "probe.coalesce.json"
    fixed_out = zeta_project / "out"
    if fixed.exists() or fixed_out.exists():
        raise FileExistsError("zeta project already contains probe.coalesce.json or out")
    staged = _stage_zeta_fixtures(run_dir, zeta_project)
    if before_state:
        build_before_state(
            before_state, fixed, run_dir / "fixtures", run_dir / "before-state.json"
        )
    fixed_out.mkdir()
    fixture_text = ", ".join(str(path) for path in staged) or "none"
    instruction = (
        prompt
        + f"\nThe task fixtures are at these exact paths: {fixture_text}."
        + f" Use the coalesce MCP document at {fixed} and export every STEP file directly under"
        + f" {fixed_out}; do not write artifacts elsewhere."
    )
    started = time.monotonic()
    try:
        result = subprocess.run([
            "zeta", "-p", instruction, "--tools", "coalesce__*", "--require-tools",
            "--no-session", "--yolo", "--format", "json", "--max-turns", "80",
            "--model", model,
        ], cwd=zeta_project, capture_output=True, text=True, check=False)
    finally:
        for path in staged:
            path.unlink()
    (run_dir / "transcript.json").write_text(result.stdout)
    metadata, final = _zeta_metadata(result.stdout)
    (run_dir / "transcript.txt").write_text(final + ("\n" + result.stderr if result.stderr else ""))
    if fixed.exists():
        shutil.move(fixed, run_dir / "model.coalesce.json")
    shutil.move(fixed_out, run_dir / "steps")
    return {"wall_time": time.monotonic() - started, **metadata, "returncode": result.returncode}


def _copy_fixtures(task, run_dir):
    fixtures = run_dir / "fixtures"
    fixtures.mkdir()
    for fixture in task["fixtures"]:
        source = Path("evals/fixtures") / fixture
        if not source.is_file():
            raise FileNotFoundError(f"missing task fixture: {source}")
        shutil.copy2(source, fixtures / source.name)


def _harness_failure(metadata):
    for field in ("error", "exception"):
        if field in metadata:
            return f"harness {field}: {metadata[field]}"
    if "returncode" not in metadata:
        return "harness return code is missing"
    if metadata["returncode"] != 0:
        return f"harness exited with return code {metadata['returncode']}"
    if metadata.get("transcript_parsed") is not True:
        return "harness transcript was not parsed"
    return None


def _run_result(task, model, run_dir, metadata):
    failure = _harness_failure(metadata)
    if failure:
        return {
            "task": task["id"],
            "model": model,
            "checks": [],
            "overall": False,
            "reason": failure,
            **metadata,
        }
    results = check_task(
        task,
        run_dir / "model.coalesce.json",
        run_dir / "steps",
        run_dir / "before-state.json",
    )
    return {
        "task": task["id"],
        "model": model,
        "checks": results,
        "overall": bool(results) and all(result["status"] == "pass" for result in results),
        **metadata,
    }


def _run_task(task, model, out, harness, kwargs):
    run_dir = out / task["id"] / model
    if run_dir.exists():
        raise FileExistsError(f"run directory already exists: {run_dir}; choose a new output path")
    run_dir.mkdir(parents=True)
    (run_dir / "prompt.txt").write_text(task["prompt"])
    _copy_fixtures(task, run_dir)
    started = time.monotonic()
    try:
        metadata = harness(
            task["prompt"],
            run_dir,
            model=model,
            before_state=task.get("beforeState"),
            **kwargs,
        )
    except Exception as error:  # noqa: BLE001 - harness failures are scorecard evidence
        metadata = {"error": str(error)}
    metadata.setdefault("wall_time", time.monotonic() - started)
    row = _run_result(task, model, run_dir, metadata)
    (run_dir / "check-results.json").write_text(json.dumps(row, indent=2))
    return row


def _write_scorecard(out, rows):
    lines = [
        "# scorecard", "",
        "| task | model | overall | tool calls | failed tool calls | wall time |",
        "|---|---|---|---:|---:|---:|",
    ]
    for row in rows:
        overall = "pass" if row["overall"] else "fail/unknown"
        lines.append(
            f"| {row['task']} | {row['model']} | {overall} |"
            f" {row.get('tool_calls', 0)} | {row.get('failed_tool_calls', 0)} |"
            f" {row.get('wall_time', 0):.1f}s |"
        )
    (out / "scorecard.md").write_text("\n".join(lines) + "\n")


def run_tasks(task_dir, out, selectors, models, harness, **kwargs):
    out.mkdir(parents=True, exist_ok=True)
    tasks = [task for selector in selectors for task in select_tasks(task_dir, selector)]
    rows = [
        _run_task(task, model, out, harness, kwargs)
        for task in tasks
        for model in models
    ]
    (out / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    _write_scorecard(out, rows)
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default="all")
    parser.add_argument("--model", action="append", required=True)
    parser.add_argument("--harness", choices=["claude", "zeta"], required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--coalesce-path", type=Path, default=Path.cwd())
    parser.add_argument("--zeta-project", type=Path)
    args = parser.parse_args()
    harness = claude_harness if args.harness == "claude" else zeta_harness
    options = (
        {"coalesce_path": args.coalesce_path}
        if args.harness == "claude"
        else {"zeta_project": args.zeta_project}
    )
    if args.harness == "zeta" and not args.zeta_project:
        parser.error("--zeta-project is required for the zeta harness")
    run_tasks(Path("evals/tasks"), args.out, [args.task], args.model, harness, **options)


if __name__ == "__main__":
    main()
