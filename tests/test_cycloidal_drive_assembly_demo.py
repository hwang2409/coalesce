import json
import subprocess
import sys
import time
from pathlib import Path

DEMO = Path(__file__).parents[1] / "examples/cycloidal_drive_assembly_demo.py"


def test_cycloidal_drive_assembly_demo(tmp_path):
    output = tmp_path / "drive"
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, str(DEMO), "--out", str(output)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert completed.returncode == 0, completed.stderr
    assert elapsed < 60
    assert sorted(path.name for path in output.iterdir()) == [
        "cycloidal-drive-assembly.coalesce.json",
        "cycloidal-drive-assembly.step",
        "transcript.json",
    ]
    transcript = json.loads((output / "transcript.json").read_text())
    assembly = next(item for item in transcript if item.get("label") == "query assembly")
    restarted = next(item for item in transcript if item.get("label") == "restart assembly query")
    assert restarted["response"] == assembly["response"]
    occurrences = assembly["response"]["occurrences"]
    assert {item["occurrenceId"] for item in occurrences} == {
        "disc",
        "shaft",
        *(f"ring-pin-{index}" for index in range(1, 9)),
    }
    assert {
        item["partId"] for item in occurrences if item["occurrenceId"].startswith("ring-pin-")
    } == {"pin/pin"}
    checked = next(item for item in transcript if item.get("label") == "check assembled drive")
    assert checked["response"]["gate"] == "PASS"
    preflight = next(
        item for item in transcript if item.get("label") == "fresh-process assembly preflight"
    )
    xcaf = preflight["response"]["validation"]["xcaf"]
    assert (xcaf["rootCount"], xcaf["definitionCount"], xcaf["occurrenceCount"]) == (1, 3, 10)
    assert (
        len(
            {
                item["definitionLabel"]
                for item in xcaf["occurrences"]
                if item["occurrenceId"].startswith("ring-pin-")
            }
        )
        == 1
    )
