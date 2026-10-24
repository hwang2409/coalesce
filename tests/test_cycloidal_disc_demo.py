import asyncio
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_mcp_sketch_outlines import assert_cycloidal_disc_step

from examples import cycloidal_disc_demo

DEMO = Path(__file__).parents[1] / "examples/cycloidal_disc_demo.py"


def run_demo(output, *arguments, env=None):
    return subprocess.run(
        [sys.executable, str(DEMO), "--out", str(output), *arguments],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )


def process_group_exists(pid):
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_cycloidal_disc_demo_smoke(tmp_path):
    output = tmp_path / "demo"

    arguments = ("--screenshots",) if os.environ.get("PLAYWRIGHT_MODULE") else ()
    completed = run_demo(output, *arguments)

    assert completed.returncode == 0, completed.stderr
    expected_files = [
        "cycloidal-disc.coalesce.json",
        "cycloidal-disc.step",
        "transcript.json",
    ]
    if arguments:
        expected_files += [
            "post-edit-1280x800.png",
            "post-edit-390x844.png",
            "pre-edit-1280x800.png",
            "pre-edit-390x844.png",
        ]
        browser_output = completed.stdout + completed.stderr
        assert " console: " not in browser_output
        assert " request: " not in browser_output
    assert sorted(path.name for path in output.iterdir()) == sorted(expected_files)
    transcript = json.loads((output / "transcript.json").read_text())
    mutations = [
        entry["revision"]
        for entry in transcript
        if entry.get("method") == "tools/call"
        and entry["name"] in {"sketch", "feature"}
    ]
    assert mutations == [1, 2, 3, 4, 5, 6]
    assert transcript[0]["method"] == "resources/read"
    assert transcript[0]["uri"] == "coalesce://docs/examples/cycloidal-disc"

    pattern_edit = next(entry for entry in transcript if entry.get("label") == "edit bolt pattern")
    changes = pattern_edit["response"]["selectionChanges"][0]
    assert [hole["name"] for hole in changes["holesMoved"]] == ["bolt2"]
    assert changes["holesRemoved"] == ["bolt5", "bolt6"]

    validity = next(entry for entry in transcript if entry.get("label") == "check valid brep")
    assert validity["response"]["gate"] == "PASS"
    preflight = next(entry for entry in transcript if entry.get("label") == "fresh-process preflight")
    assert preflight["response"]["ready"] is True
    assert_cycloidal_disc_step(output / "cycloidal-disc.step", 4)


def test_cancelled_screenshot_capture_stops_child(tmp_path, monkeypatch):
    assert inspect.iscoroutinefunction(cycloidal_disc_demo.capture_screenshots)
    helper = tmp_path / "slow_capture.mjs"
    helper.write_text("await new Promise(resolve => setTimeout(resolve, 60_000));\n")
    pid_file = tmp_path / "capture.pid"

    monkeypatch.setattr(cycloidal_disc_demo, "SCREENSHOT_HELPER", helper)
    monkeypatch.setenv("COALESCE_CYCLOID_CAPTURE_PID_FILE", str(pid_file))

    async def cancel_capture():
        task = asyncio.create_task(
            cycloidal_disc_demo.capture_screenshots("unused", "unused", tmp_path, "test", 0)
        )
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        assert pid_file.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_capture())
    assert not process_group_exists(int(pid_file.read_text()))


def test_browser_capture_failure_keeps_transcript_and_stops_children(tmp_path):
    module = os.environ.get("PLAYWRIGHT_MODULE")
    if not module:
        pytest.skip("PLAYWRIGHT_MODULE is required")
    output = tmp_path / "failed-capture"
    pid_file = tmp_path / "capture.pid"
    env = {
        **os.environ,
        "COALESCE_CYCLOID_VIEWER_URL": "http://127.0.0.1:1",
        "COALESCE_CYCLOID_CAPTURE_PID_FILE": str(pid_file),
    }

    completed = run_demo(output, "--screenshots", env=env)

    assert completed.returncode != 0
    assert "screenshot capture failed (pre-edit)" in completed.stderr
    transcript = json.loads((output / "transcript.json").read_text())
    assert transcript[-1]["label"] == "create bore"
    assert transcript[-1]["revision"] == 5
    assert not process_group_exists(int(pid_file.read_text()))


def test_cycloidal_disc_demo_failure_keeps_transcript(tmp_path):
    output = tmp_path / "failed-demo"

    completed = run_demo(output, "--pattern-radius", "100")

    assert completed.returncode != 0
    transcript = json.loads((output / "transcript.json").read_text())
    failed = transcript[-1]
    assert failed["label"] == "edit bolt pattern"
    assert failed["response"]["ok"] is False
    assert failed["revision"] == 5
