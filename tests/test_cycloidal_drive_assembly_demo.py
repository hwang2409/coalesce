import ast
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coalesce.export import verify_assembly_step_fresh

DEMO = Path(__file__).parents[1] / "examples/cycloidal_drive_assembly_demo.py"
MAX_DISC_PIN_CLEARANCE_MM = 1e-6


def _entry(transcript, label):
    return next(item for item in transcript if item.get("label") == label)


def _assert_functional_interference_checks(checked):
    results = checked["response"]["results"]
    disc_pin_results = [item for item in results if item["id"].startswith("disc-pin-")]
    disc_pin_rows = [item["measured"]["pairs"][0] for item in disc_pin_results]

    assert checked["response"]["gate"] == "PASS"
    whole_drive = next(item for item in results if item["id"] == "whole-drive-interference")
    all_pairs = whole_drive["measured"]["pairs"]
    assert len(all_pairs) == 55
    assert all(row["overlapVolume"]["value"] == 0 for row in all_pairs)
    expected_contacts = {
        frozenset(("disc", "shaft")),
        frozenset(("ring", "shaft")),
        *(frozenset(("disc", f"ring-pin-{index}")) for index in range(1, 9)),
        *(frozenset(("ring", f"ring-pin-{index}")) for index in range(1, 9)),
    }
    actual_contacts = {
        frozenset(row["occurrenceIds"])
        for row in all_pairs
        if row["clearance"]["value"] <= MAX_DISC_PIN_CLEARANCE_MM
    }
    assert actual_contacts == expected_contacts
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
    assert (xcaf["rootCount"], xcaf["definitionCount"], xcaf["occurrenceCount"]) == (1, 4, 11)
    pin_definitions = {
        item["definitionLabel"]
        for item in xcaf["occurrences"]
        if item["occurrenceId"].startswith("ring-pin-")
    }
    assert len(pin_definitions) == 1


def _run_installed_demo(tmp_path, output):
    uv = shutil.which("uv")
    assert uv is not None
    wheel_dir = tmp_path / "wheel"
    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(wheel_dir)],
        cwd=Path(__file__).parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    venv = tmp_path / "venv"
    subprocess.run([uv, "venv", str(venv)], check=True, capture_output=True, text=True)
    python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    wheel = next(wheel_dir.glob("coalesce-*.whl"))
    subprocess.run(
        [uv, "pip", "install", "--python", str(python), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    return subprocess.run(
        [str(venv / ("Scripts/coalesce-demo.exe" if sys.platform == "win32" else "bin/coalesce-demo")), "--output", str(output)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_installed_demo_command_publishes_complete_artifacts(tmp_path):
    output = tmp_path / "installed-demo"
    completed = _run_installed_demo(tmp_path, output)

    assert completed.returncode == 0, completed.stderr
    assert "definitions: 4; occurrences: 11; overlap: PASS; STEP:" in completed.stdout
    assert "mate solve: static, not simulated motion" in completed.stdout
    assert sorted(path.name for path in output.iterdir()) == [
        "cycloidal-drive-assembly.coalesce.json",
        "cycloidal-drive-assembly.step",
        "transcript.json",
    ]
    assert verify_assembly_step_fresh(output / "cycloidal-drive-assembly.step")["rootCount"] == 1
    transcript = json.loads((output / "transcript.json").read_text())
    assert _entry(transcript, "restart assembly query")["response"] == _entry(
        transcript, "query assembly"
    )["response"]


def test_demo_rejects_existing_output_directory_without_changing_it(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "sentinel.txt"
    sentinel.write_text("keep")

    completed = subprocess.run(
        [sys.executable, "-m", "coalesce.demo", "--output", str(output)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "demo failed" in completed.stderr
    assert "File exists" in completed.stderr
    assert sorted(path.name for path in output.iterdir()) == ["sentinel.txt"]
    assert sentinel.read_text() == "keep"


def test_sigint_during_paced_demo_stops_server_child(tmp_path):
    if os.name == "nt":
        pytest.skip("server process-group checks are POSIX-only")
    output = tmp_path / "interrupted"
    process = subprocess.Popen(
        [sys.executable, "-m", "coalesce.demo", "--output", str(output), "--pace", "1"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "beat: build"
    child_pids = []
    for _ in range(100):
        listing = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, check=True)
        child_pids = [
            int(line.strip().split(None, 1)[0])
            for line in listing.stdout.splitlines()
            if str(output / "cycloidal-drive-assembly.coalesce.json") in line
            and int(line.strip().split(None, 1)[0]) != process.pid
        ]
        if child_pids:
            break
        time.sleep(0.05)
    assert child_pids
    process.send_signal(signal.SIGINT)
    process.communicate(timeout=30)
    assert process.returncode != 0
    for pid in child_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_paced_demo_prints_story_beats_in_order(tmp_path):
    completed = subprocess.run(
        [sys.executable, "-m", "coalesce.demo", "--output", str(tmp_path / "paced"), "--pace", "0.01"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert [
        line for line in completed.stdout.splitlines() if line.startswith("beat: ")
    ] == [
        "beat: build",
        "beat: edit/lost-contact failure",
        "beat: restore + interference pass",
        "beat: STEP publish",
    ]


def test_drive_demo_models_the_eccentric_journal_in_the_shaft_definition():
    source = DEMO.read_text(encoding="utf-8")

    assert '("eccentric", "pin/pin"' not in source
    assert '"operation": "join"' in source


def test_drive_demo_does_not_compute_pin_transforms_with_trigonometry():
    tree = ast.parse(DEMO.read_text(encoding="utf-8"))
    calls = {
        f"{node.func.value.id}.{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
    }
    assert calls.isdisjoint({"math.sin", "math.cos"})


def test_cycloidal_drive_assembly_demo(tmp_path):
    output = tmp_path / "drive"
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, str(DEMO), "--out", str(output)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert completed.returncode == 0, completed.stderr
    assert elapsed < 120
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
        "ring",
        "disc",
        "shaft",
        *(f"ring-pin-{index}" for index in range(1, 9)),
    }
    assert {
        item["partId"] for item in occurrences if item["occurrenceId"].startswith("ring-pin-")
    } == {"pin/pin"}
    assert assembly["response"]["solveStatus"] == "UNDER_CONSTRAINED"
    mates = assembly["response"]["mates"]
    assert len(mates) == 21
    pin_axes = [mate for mate in mates if mate["mateId"].endswith("-axis") and mate["mateId"].startswith("pin-")]
    assert {mate["first"]["reference"]["source"]["name"] for mate in pin_axes} == {
        f"bolt{index}" for index in range(1, 9)
    }
    shaft_axis = next(mate for mate in mates if mate["mateId"] == "shaft-axis")
    assert shaft_axis["first"]["reference"]["source"]["name"] == "center"
    by_id = {item["occurrenceId"]: item for item in occurrences}
    assert (by_id["ring"]["grounded"], by_id["ring"]["remainingDof"]) == (True, 0)
    assert by_id["shaft"]["remainingDof"] == 1
    assert by_id["disc"]["remainingDof"] == 1
    assert all(by_id[f"ring-pin-{index}"]["remainingDof"] == 1 for index in range(1, 9))
    assert by_id["shaft"]["transform"]["translation"]["value"] == [0.0, 0.0, 0.0]
    assert by_id["shaft"]["transform"]["rotation"]["angle"]["value"] == 0.0
    assert by_id["disc"]["transform"]["translation"]["value"] == [2.0, 0.0, 1.0]

    initial = _entry(transcript, "query initial assembly")["response"]
    edited = _entry(transcript, "query edited assembly")["response"]
    after_stale = _entry(transcript, "query after stale edit")["response"]
    initial_pins = {
        item["occurrenceId"]: item["transform"] for item in initial["occurrences"]
        if item["occurrenceId"].startswith("ring-pin-")
    }
    edited_pins = {
        item["occurrenceId"]: item["transform"] for item in edited["occurrences"]
        if item["occurrenceId"].startswith("ring-pin-")
    }
    assert initial_pins.keys() == edited_pins.keys()
    assert all(edited_pins[name] != initial_pins[name] for name in initial_pins)
    assert {mate["mateId"] for mate in initial["mates"]} == {
        mate["mateId"] for mate in edited["mates"]
    }
    assert after_stale == edited
    edited_rows = [
        result["measured"]["pairs"][0]
        for result in _entry(transcript, "check edited pattern clearance")["response"]["results"]
    ]
    assert all(row["overlapVolume"]["value"] == 0 for row in edited_rows)
    edited_clearances = [row["clearance"]["value"] for row in edited_rows]
    assert sum(clearance > MAX_DISC_PIN_CLEARANCE_MM for clearance in edited_clearances) == 6
    assert sum(clearance <= MAX_DISC_PIN_CLEARANCE_MM for clearance in edited_clearances) == 2
    stale = _entry(transcript, "reject stale ring member")["response"]
    assert stale["error"]["code"] == "MATE_REFERENCE_STALE"
    assert stale["error"]["mateId"] == "pin-8-axis"
    _assert_functional_interference_checks(_entry(transcript, "check assembled drive"))
    _assert_xcaf_tree(_entry(transcript, "fresh-process assembly preflight"))
