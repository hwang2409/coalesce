import ast
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.request import urlopen

import pytest

from coalesce.demo import _contact_summary
from coalesce.export import verify_assembly_step_fresh

DEMO = Path(__file__).parents[1] / "examples/cycloidal_drive_assembly_demo.py"
PACKAGED_DEMO = Path(__file__).parents[1] / "src/coalesce/demo.py"
MAX_DISC_PIN_CLEARANCE_MM = 1e-6


def test_contact_summary_keeps_zero_clearance_pins_touching():
    inspection = {
        "results": [{
            "measured": {"pairs": [
                {"clearance": {"value": value}}
                for value in (1.0, 0.856, 0, 0.967, 0, 0.967, 0.890, 0.856)
            ]}
        }]
    }
    assert _contact_summary(inspection) == (6, 8, 0.856, 1.0)


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
    assert "all 55 part pairs checked: pass; 11 assembled occurrences in 4 reusable part definitions;" in completed.stdout
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
    bom = _entry(transcript, "query assembly")["response"]["bom"]
    assert [row["partId"] for row in bom["rows"]] == sorted(
        row["partId"] for row in bom["rows"]
    )
    assert len(bom["rows"]) == 4
    assert next(row for row in bom["rows"] if row["partId"] == "pin/pin")["count"] == 8
    xcaf = _entry(transcript, "fresh-process assembly preflight")["response"]["validation"]["xcaf"]
    assert len(bom["rows"]) == xcaf["definitionCount"]
    assert sum(row["count"] for row in bom["rows"]) == xcaf["occurrenceCount"]


def test_demo_default_output_path_is_new_and_publishes_artifacts(tmp_path):
    completed = subprocess.run(
        [sys.executable, "-m", "coalesce.demo"],
        cwd=tmp_path,
        env={**os.environ, "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    outputs = list(tmp_path.glob("coalesce-demo-*"))
    assert len(outputs) == 1
    assert sorted(path.name for path in outputs[0].iterdir()) == [
        "cycloidal-drive-assembly.coalesce.json",
        "cycloidal-drive-assembly.step",
        "transcript.json",
    ]


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
    assert process.stdout.readline().startswith("viewer URL: ")
    assert process.stdout.readline().strip().startswith("beat: build · revision 2")
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
    assert process.returncode == 0
    for pid in child_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def _start_line_reader(stream):
    lines = queue.Queue()

    def read_lines():
        for line in iter(stream.readline, ""):
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read_lines, daemon=True).start()
    return lines


def _readline_with_deadline(lines, timeout=30):
    try:
        line = lines.get(timeout=timeout)
    except queue.Empty as error:
        raise AssertionError("timed out waiting for demo output") from error
    assert line is not None, "demo exited before expected output"
    return line


def test_paced_demo_prints_live_viewer_and_reuses_port(tmp_path):
    process = subprocess.Popen(
        [sys.executable, "-m", "coalesce.demo", "--output", str(tmp_path / "paced"), "--pace", "0.01"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    output_lines = _start_line_reader(process.stdout)
    lines = []
    try:
        while True:
            line = _readline_with_deadline(output_lines)
            lines.append(line.strip())
            if line.startswith("viewer URL: "):
                first_url = line.split(": ", 1)[1]
                break
        assert urlopen(first_url, timeout=5).status == 200
        while True:
            line = _readline_with_deadline(output_lines)
            lines.append(line.strip())
            if line.startswith("viewer live at "):
                final_url = line.split(" at ", 1)[1].split(";", 1)[0]
                break
        assert urlopen(final_url, timeout=5).status == 200
        assert first_url.split(":")[2].split("/", 1)[0] == final_url.split(":")[2].split("/", 1)[0]
        beats = [line for line in lines if line.startswith("beat: ")]
        assert beats[0].startswith("beat: build · revision 2")
        assert beats[1].startswith("beat: edit/lost-contact failure · revision 46 · contacts lost: 6 of 8")
        assert beats[2].startswith("beat: reject broken edit · revision 46")
        assert beats[3].startswith("beat: restore + interference pass · revision 47 · all 55 pairs pass")
        assert beats[4].startswith("beat: STEP publish · revision 47")
        process.send_signal(signal.SIGINT)
        process.wait(timeout=30)
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    assert process.returncode == 0


def test_drive_demo_models_the_eccentric_journal_in_the_shaft_definition():
    source = PACKAGED_DEMO.read_text(encoding="utf-8")

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
    bom = assembly["response"]["bom"]
    assert [row["partId"] for row in bom["rows"]] == sorted(row["partId"] for row in bom["rows"])
    assert len(bom["rows"]) == 4
    assert next(row for row in bom["rows"] if row["partId"] == "pin/pin")["count"] == 8
    preflight = _entry(transcript, "fresh-process assembly preflight")["response"]
    xcaf = preflight["validation"]["xcaf"]
    assert len(bom["rows"]) == xcaf["definitionCount"]
    assert sum(row["count"] for row in bom["rows"]) == xcaf["occurrenceCount"]
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
