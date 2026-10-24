import json
import subprocess
import sys
from pathlib import Path

from test_mcp_sketch_outlines import assert_cycloidal_disc_step

DEMO = Path(__file__).parents[1] / "examples/cycloidal_disc_demo.py"


def run_demo(output, *arguments):
    return subprocess.run(
        [sys.executable, str(DEMO), "--out", str(output), *arguments],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_cycloidal_disc_demo_smoke(tmp_path):
    output = tmp_path / "demo"

    completed = run_demo(output)

    assert completed.returncode == 0, completed.stderr
    assert sorted(path.name for path in output.iterdir()) == [
        "cycloidal-disc.coalesce.json",
        "cycloidal-disc.step",
        "transcript.json",
    ]
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


def test_cycloidal_disc_demo_failure_keeps_transcript(tmp_path):
    output = tmp_path / "failed-demo"

    completed = run_demo(output, "--pattern-radius", "100")

    assert completed.returncode != 0
    transcript = json.loads((output / "transcript.json").read_text())
    failed = transcript[-1]
    assert failed["label"] == "edit bolt pattern"
    assert failed["response"]["ok"] is False
    assert failed["revision"] == 5
