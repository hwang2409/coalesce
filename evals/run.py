from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

from .checks import check_task
from .tasks import select_tasks


def _claude_metadata(transcript):
    tool_calls = failed_tool_calls = 0
    cost = None
    final = ""
    for line in transcript.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message", {})
        content = message.get("content", []) if isinstance(message, dict) else []
        if event.get("type") == "assistant":
            tool_calls += sum(item.get("type") == "tool_use" for item in content if isinstance(item, dict))
        if event.get("type") == "user":
            failed_tool_calls += sum(item.get("type") == "tool_result" and item.get("is_error", False)
                                     for item in content if isinstance(item, dict))
        if event.get("type") == "result":
            final = event.get("result", "")
            cost = event.get("total_cost_usd")
    metadata = {"tool_calls": tool_calls, "failed_tool_calls": failed_tool_calls}
    if cost is not None:
        metadata["cost_usd"] = cost
    return metadata, final


def claude_harness(prompt, run_dir, *, model, coalesce_path, **_):
    config = run_dir / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"coalesce": {"command": "uv", "args": ["run", "--project", str(coalesce_path), "coalesce", "--document", str(run_dir / "model.coalesce.json")]}}}, indent=2))
    started = time.monotonic()
    instruction = prompt + f"\nUse the coalesce MCP document at {run_dir / 'model.coalesce.json'} and export every STEP file under {run_dir / 'steps'}; do not write artifacts elsewhere."
    (run_dir / "steps").mkdir(exist_ok=True)
    result = subprocess.run(["claude", "-p", instruction, "--model", model, "--mcp-config", str(config),
                             "--strict-mcp-config", "--output-format", "stream-json", "--verbose",
                             "--allowedTools", "mcp__coalesce__*"], cwd=run_dir, capture_output=True,
                            text=True, check=False)
    (run_dir / "transcript.jsonl").write_text(result.stdout)
    metadata, final = _claude_metadata(result.stdout)
    (run_dir / "transcript.txt").write_text(final + ("\n" + result.stderr if result.stderr else ""))
    return {"wall_time": time.monotonic()-started, **metadata, "returncode": result.returncode}


def zeta_harness(prompt, run_dir, *, zeta_project, **_):
    """run against a pre-trusted project and move its fixed artifacts after completion."""
    fixed = zeta_project / "probe.coalesce.json"
    started = time.monotonic()
    instruction = prompt + f"\nUse the coalesce MCP document at {zeta_project / 'probe.coalesce.json'} and export every STEP file under {zeta_project / 'out'}; do not write artifacts elsewhere."
    result = subprocess.run(["zeta", "-p", instruction], cwd=zeta_project, capture_output=True, text=True, check=False)
    (run_dir / "transcript.txt").write_text(result.stdout + "\n" + result.stderr)
    if fixed.exists(): shutil.move(fixed, run_dir / "model.coalesce.json")
    fixed_out = zeta_project / "out"
    if fixed_out.exists(): shutil.move(fixed_out, run_dir / "steps")
    return {"wall_time": time.monotonic()-started, "tool_calls": result.stdout.count("tool"), "failed_tool_calls": result.stdout.lower().count("error"), "returncode": result.returncode}


def run_tasks(task_dir, out, selectors, models, harness, **kwargs):
    out.mkdir(parents=True, exist_ok=True); rows=[]
    tasks = [task for selector in selectors for task in select_tasks(task_dir, selector)]
    for task in tasks:
        for model in models:
            run_dir = out / task["id"] / model; run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "prompt.txt").write_text(task["prompt"])
            fixtures = run_dir / "fixtures"; fixtures.mkdir()
            for fixture in task["fixtures"]:
                source = Path("evals/fixtures") / fixture
                if source.exists(): shutil.copy2(source, fixtures / fixture)
            started = time.monotonic()
            try: metadata = harness(task["prompt"], run_dir, model=model, **kwargs)
            except Exception as error:  # noqa: BLE001 - harness failures are recorded
                metadata = {"error": str(error)}
            metadata.setdefault("wall_time", time.monotonic()-started)
            results = check_task(task, run_dir / "model.coalesce.json", run_dir)
            row = {"task": task["id"], "model": model, "checks": results, "overall": all(x["status"] == "pass" for x in results), **metadata}
            (run_dir / "check-results.json").write_text(json.dumps(row, indent=2))
            rows.append(row)
    (out / "results.jsonl").write_text("".join(json.dumps(row)+"\n" for row in rows))
    lines = ["# scorecard", "", "| task | model | overall | tool calls | failed tool calls | wall time |", "|---|---|---|---:|---:|---:|"]
    for row in rows: lines.append(f"| {row['task']} | {row['model']} | {'pass' if row['overall'] else 'fail/unknown'} | {row.get('tool_calls', 0)} | {row.get('failed_tool_calls', 0)} | {row.get('wall_time', 0):.1f}s |")
    (out / "scorecard.md").write_text("\n".join(lines)+"\n")
    return rows


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--task", default="all"); parser.add_argument("--model", action="append", required=True); parser.add_argument("--harness", choices=["claude", "zeta"], required=True); parser.add_argument("--out", type=Path, required=True); parser.add_argument("--coalesce-path", type=Path, default=Path.cwd()); parser.add_argument("--zeta-project", type=Path)
    args = parser.parse_args()
    harness = claude_harness if args.harness == "claude" else zeta_harness
    options = {"coalesce_path": args.coalesce_path} if args.harness == "claude" else {"zeta_project": args.zeta_project}
    if args.harness == "zeta" and not args.zeta_project:
        parser.error("--zeta-project is required for the zeta harness")
    run_tasks(Path("evals/tasks"), args.out, [args.task], args.model, harness, **options)
if __name__ == "__main__": main()
