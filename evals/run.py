from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

from .checks import check_task
from .tasks import select_tasks


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
    metadata = {"tool_calls": 0, "failed_tool_calls": 0}
    final = ""
    for event in _json_events(transcript):
        tools, failures = _claude_message_counts(event)
        if event.get("type") == "assistant":
            metadata["tool_calls"] += tools
        if event.get("type") == "user":
            metadata["failed_tool_calls"] += failures
        if event.get("type") == "result":
            final = event.get("result", "")
            if event.get("total_cost_usd") is not None:
                metadata["cost_usd"] = event["total_cost_usd"]
    return metadata, final


def _zeta_metadata(transcript):
    metadata = {"tool_calls": 0, "failed_tool_calls": 0}
    final = ""
    for event in _json_events(transcript):
        event_type = event.get("type")
        metadata["tool_calls"] += event_type == "tool_call"
        metadata["failed_tool_calls"] += event_type == "tool_result" and event.get("is_error", False)
        if event_type == "message" and event.get("role") == "assistant":
            final = event.get("text", "")
        cost = event.get("cost_usd") or (event.get("usage") or {}).get("cost_usd")
        if cost is not None:
            metadata["cost_usd"] = cost
    return metadata, final


def claude_harness(prompt, run_dir, *, model, coalesce_path, **_):
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
    instruction = (
        prompt
        + f"\nUse the coalesce MCP document at {run_dir / 'model.coalesce.json'}"
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


def zeta_harness(prompt, run_dir, *, model, zeta_project, **_):
    """Run in a trusted project while owning every staged input and output path."""
    fixed = zeta_project / "probe.coalesce.json"
    fixed_out = zeta_project / "out"
    if fixed.exists() or fixed_out.exists():
        raise FileExistsError("zeta project already contains probe.coalesce.json or out")
    staged = _stage_zeta_fixtures(run_dir, zeta_project)
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


def _run_task(task, model, out, harness, kwargs):
    run_dir = out / task["id"] / model
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.txt").write_text(task["prompt"])
    _copy_fixtures(task, run_dir)
    started = time.monotonic()
    try:
        metadata = harness(task["prompt"], run_dir, model=model, **kwargs)
    except Exception as error:  # noqa: BLE001 - harness failures are scorecard evidence
        metadata = {"error": str(error)}
    metadata.setdefault("wall_time", time.monotonic() - started)
    results = check_task(task, run_dir / "model.coalesce.json", run_dir / "steps")
    row = {
        "task": task["id"],
        "model": model,
        "checks": results,
        "overall": bool(results) and all(result["status"] == "pass" for result in results),
        **metadata,
    }
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
