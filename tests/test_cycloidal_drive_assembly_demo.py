import ast
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


def test_drive_demo_models_the_eccentric_journal_in_the_shaft_definition():
    source = DEMO.read_text()

    assert '("eccentric", "pin/pin"' not in source
    assert '"operation": "join"' in source


def test_drive_demo_does_not_compute_pin_transforms_with_trigonometry():
    tree = ast.parse(DEMO.read_text())
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
