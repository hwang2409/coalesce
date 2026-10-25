import json
import subprocess
import sys
import time
from pathlib import Path

DEMO = Path(__file__).parents[1] / "examples/cycloidal_drive_assembly_demo.py"
MAX_DISC_PIN_CLEARANCE_MM = 1e-6


def _entry(transcript, label):
    return next(item for item in transcript if item.get("label") == label)


def _assert_functional_interference_checks(checked):
    results = checked["response"]["results"]
    disc_pin_results = [item for item in results if item["id"].startswith("disc-pin-")]
    disc_pin_rows = [item["measured"]["pairs"][0] for item in disc_pin_results]

    assert checked["response"]["gate"] == "PASS"
    assert (
        next(item for item in results if item["id"] == "disc-shaft-clearance")["status"] == "PASS"
    )
    assert {tuple(row["occurrenceIds"]) for row in disc_pin_rows} == {
        ("disc", f"ring-pin-{index}") for index in range(1, 9)
    }
    assert all(item["status"] == "PASS" for item in disc_pin_results)
    assert all(row["overlapVolume"]["value"] == 0 for row in disc_pin_rows)
    assert max(row["clearance"]["value"] for row in disc_pin_rows) <= MAX_DISC_PIN_CLEARANCE_MM


def _assert_xcaf_tree(preflight):
    xcaf = preflight["response"]["validation"]["xcaf"]
    assert (xcaf["rootCount"], xcaf["definitionCount"], xcaf["occurrenceCount"]) == (1, 3, 10)
    pin_definitions = {
        item["definitionLabel"]
        for item in xcaf["occurrences"]
        if item["occurrenceId"].startswith("ring-pin-")
    }
    assert len(pin_definitions) == 1


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
    assembly = _entry(transcript, "query assembly")
    restarted = _entry(transcript, "restart assembly query")
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
    _assert_functional_interference_checks(_entry(transcript, "check assembled drive"))
    _assert_xcaf_tree(_entry(transcript, "fresh-process assembly preflight"))
