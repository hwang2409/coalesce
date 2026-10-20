"""Real-browser smoke test: headless Chromium via a locally installed Playwright.

Skips when Node or a launchable Playwright is not on this machine (for example in CI).
Set PLAYWRIGHT_MODULE to a playwright package's index.mjs to pick one explicitly, and
VIEWER_SCREENSHOTS to a directory to keep a screenshot of every step.
"""

import glob
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from build123d import Align, Box, fillet

from coalesce.viewer import ScenePart, ViewerSceneStore, ViewerServer

SMOKE = Path(__file__).with_name("viewer_smoke.mjs")


def plate(height: float) -> Box:
    return Box(60, 40, height, align=(Align.CENTER, Align.CENTER, Align.MIN))


def playwright_module() -> str | None:
    node = shutil.which("node")
    if not node:
        return None
    candidates = [os.environ.get("PLAYWRIGHT_MODULE")] + sorted(
        glob.glob(os.path.expanduser("~/.npm/_npx/*/node_modules/playwright/index.mjs"))
    )
    probe = "const m = await import(process.argv[1]); await (await m.chromium.launch()).close();"
    for candidate in filter(None, candidates):
        ok = subprocess.run(
            [node, "--input-type=module", "-e", probe, candidate],
            capture_output=True, timeout=60, check=False,
        )
        if ok.returncode == 0:
            return candidate
    return None


@pytest.fixture(scope="module")
def module():
    found = playwright_module()
    if not found:
        pytest.skip("no Node with a launchable Playwright Chromium on this machine")
    return found


def test_page_draws_and_follows_revisions_in_a_real_browser(module):
    store = ViewerSceneStore()
    shots = os.environ.get("VIEWER_SCREENSHOTS")
    with ViewerServer(store) as server:
        driver = subprocess.Popen(
            ["node", str(SMOKE), module, server.url, *([shots] if shots else [])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )

        def step(name):
            line = driver.stdout.readline()
            assert line.startswith(f"step {name} "), line
            return json.loads(line.split(" ", 2)[2])

        def next_step():
            driver.stdin.write("next\n")
            driver.stdin.flush()

        try:
            empty = step("empty")
            assert empty["label"] == "none" and empty["link"] == "Live"
            store.publish(1, [ScenePart("plate", plate(6))])
            next_step()

            first = step("revision-1")
            assert first["label"] == "1"
            assert "#token" not in first["url"]  # the token leaves the address bar
            assert first["dark"] > 0.0003  # edge lines of the plate are on the canvas
            store.publish(2, [ScenePart("plate", plate(10))])
            next_step()

            second = step("revision-2")
            assert second["label"] == "2"
            assert second["top"] < first["top"]  # same camera, taller plate reaches higher
            try:
                fillet(plate(10).edges(), radius=20)
            except ValueError as error:
                store.publish_failure(["edge_fillet"], str(error))
            next_step()

            failed = step("failure")
            assert failed["label"] == "2"
            assert "edge_fillet" in failed["failure"] and "fillet" in failed["failure"]
            assert failed["dark"] == pytest.approx(second["dark"])  # last good scene stays
            next_step()

            front = step("front")
            assert front["pressed"] == "true" and front["dark"] > 0.0003
            assert front["errors"] == []
            for revision in range(3, 53):  # back to back, so some loads lose the race
                store.publish(revision, [ScenePart("plate", plate(4 + revision % 7))])
            next_step()

            stress = step("stress")
            # 50 more revisions free what they replace: gpu counts match the one-part baseline
            assert stress["memory"] == second["memory"], (stress["memory"], second["memory"])
            # the store keeps two revisions, so skipped ones may 404; the page then catches up
            dropped = "the server responded with a status of 404"
            assert [e for e in stress["errors"] if dropped not in e] == []
            next_step()
            assert driver.wait(timeout=30) == 0
        finally:
            driver.kill()
