"""Real-browser smoke test: headless Chromium via a locally installed Playwright.

Skips when Node or a launchable Playwright is not on this machine (for example in CI).
Set PLAYWRIGHT_MODULE to a playwright package's index.mjs to pick one explicitly, and
VIEWER_SCREENSHOTS to a directory to keep a screenshot of every step.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from build123d import Align, Box, Location

from coalesce import (
    Cardinality,
    Extrude,
    Fillet,
    Graph,
    GraphError,
    Rectangle,
    Reference,
    Sketch,
    regenerate,
)
from coalesce.assembly import RigidTransform
from coalesce.service import _graph_error_response
from coalesce.viewer import SceneOccurrence, ScenePart, ViewerSceneStore, ViewerServer

SMOKE = Path(__file__).with_name("viewer_smoke.mjs")
ASSEMBLY_SMOKE = Path(__file__).with_name("viewer_assembly_smoke.mjs")
# chromium logs every request to a stopped server, whatever sent it; no page code can avoid it
# a stopped server can refuse a new socket, reset one in flight, disconnect it before use,
# or abort the bounded authorization probe that decides when the outage becomes terminal
OUTAGE_ERRORS = (
    "net::ERR_ABORTED",
    "net::ERR_CONNECTION_REFUSED",
    "net::ERR_CONNECTION_RESET",
    "net::ERR_SOCKET_NOT_CONNECTED",
)


def plate(height: float) -> Box:
    return Box(60, 40, height, align=(Align.CENTER, Align.CENTER, Align.MIN))


def oversized_fillet_failure() -> dict:
    """What coalesce itself publishes for a 20 mm fillet on a 10 mm plate (no kernel wording)."""
    graph = Graph()
    graph.add(Sketch("plate_sketch", entities=[Rectangle("plate", 60, 40)]))
    graph.add(Extrude("plate_extrude", sketch="plate_sketch", height=10))
    edge = Reference("plate_extrude", "between(side(bottom),cap_end)", Cardinality.ONE)
    graph.add(Fillet("edge_fillet", target="plate_extrude", edges=[edge], radius=20))
    with pytest.raises(GraphError) as caught:
        regenerate(graph)
    error = _graph_error_response(2, "fillet", "feature", caught.value)["error"]
    return {key: error[key] for key in ("code", "message", "hint") if key in error}  # as publish_failure


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
    if os.environ.get("CI", "").lower() == "true":
        pytest.fail("CI requires a launchable Playwright Chromium")
    return None


@pytest.fixture(scope="module")
def module():
    found = playwright_module()
    if not found:
        pytest.skip("no Node with a launchable Playwright Chromium on this machine")
    return found


@pytest.mark.parametrize("viewport", [(1280, 800), (390, 844)])
def test_assembly_occurrences_and_pair_focus_in_a_real_browser(module, viewport, tmp_path):
    width, height = viewport
    shots = os.environ.get("VIEWER_SCREENSHOTS", str(tmp_path))
    store = ViewerSceneStore()
    shaft = ScenePart(
        "shaft/body",
        Location((7, -3, 2)) * Box(30, 4, 8),
        label="Shaft",
        terminal_feature="shaft-extrude",
    )
    pin = ScenePart("pin/body", Box(4, 4, 12), label="Pin", terminal_feature="pin-extrude")
    query_bom = {
        "rows": [
            {"partId": "pin/body", "label": "Pin", "count": 3, "occurrenceIds": ["pin-left", "pin-right", "pin-top"]},
            {"partId": "shaft/body", "label": "Shaft", "count": 2, "occurrenceIds": ["shaft", "shaft-skew"]},
        ],
        "excluded": [],
    }
    store.publish(
        1,
        [
            SceneOccurrence("shaft", shaft, RigidTransform((0, 0, 0), (0, 0, 1), 90)),
            SceneOccurrence(
                "shaft-skew",
                shaft,
                RigidTransform((20, 10, 3), (1 / 3**0.5,) * 3, 37),
            ),
            SceneOccurrence("pin-left", pin, RigidTransform((-8, 0, 0), (0, 0, 1), 0)),
            SceneOccurrence("pin-right", pin, RigidTransform((8, 0, 0), (0, 0, 1), 0)),
            SceneOccurrence("pin-top", pin, RigidTransform((0, 12, 0), (0, 0, 1), 0)),
        ],
        assembly_state={
            "bom": query_bom,
            "solveStatus": "UNDER_CONSTRAINED",
            "underConstrainedOccurrenceIds": ["shaft", "pin-left", "pin-right"],
            "conflicts": [],
            "occurrences": [
                {
                    "occurrenceId": occurrence_id,
                    "grounded": occurrence_id == "shaft-skew",
                    "remainingDof": 0 if occurrence_id == "shaft-skew" else 1,
                    "freeMotions": [] if occurrence_id == "shaft-skew" else ["axisSpin"],
                    "mateIds": ["pin-axis"] if occurrence_id.startswith("pin-") else [],
                }
                for occurrence_id in ("shaft", "shaft-skew", "pin-left", "pin-right", "pin-top")
            ],
            "mates": [
                {
                    "mateId": "pin-axis",
                    "kind": "concentric",
                    "occurrenceIds": ["shaft-skew", "pin-left"],
                    "orientation": "aligned",
                    "status": "satisfied",
                }
            ],
        },
        assembly_inspection=[
            {
                "occurrenceIds": pair,
                "partIds": ["shaft/body" if name.startswith("shaft") else "pin/body" for name in pair],
                "overlapVolume": {"value": 0, "unit": "mm^3"},
                "clearance": {"value": clearance, "unit": "mm"},
                "interferes": False,
                "revision": 1,
                "requirementId": requirement,
                "status": "PASS",
            }
            for pair, clearance, requirement in [
                (["shaft", "pin-right"], 0, "clearance-02"),
                (["pin-right", "shaft"], 0.0003, "clearance-reversed"),
                (["shaft", "shaft-skew"], 1, "clearance-03"),
                (["shaft", "pin-top"], 2, "clearance-04"),
                (["shaft-skew", "pin-left"], 3, "clearance-05"),
                (["shaft-skew", "pin-right"], 4, "clearance-06"),
                (["shaft-skew", "pin-top"], 5, "clearance-07"),
                (["pin-left", "pin-top"], 6, "clearance-08"),
                (["pin-right", "pin-top"], 7, "clearance-09"),
            ]
        ]
        + [
            {
                "occurrenceIds": ["shaft", "pin-left"],
                "partIds": ["shaft/body", "pin/body"],
                "overlapVolume": {"value": 0.0004, "unit": "mm^3"},
                "clearance": {"value": 0, "unit": "mm"},
                "interferes": True,
                "revision": 1,
                "requirementId": "assembly-clearance",
                "status": "FAIL",
                "_overlapShape": Box(1, 1, 1),
            },
            {
                "occurrenceIds": ["pin-left", "pin-right"],
                "partIds": ["pin/body", "pin/body"],
                "error": {"message": "clearance solver failed"},
                "revision": 1,
                "requirementId": "clearance-error",
                "status": "ERROR",
            },
            {
                "occurrenceIds": ["pin-right", "pin-left"],
                "partIds": ["pin/body", "pin/body"],
                "overlapVolume": {"value": 1, "unit": "mm^3"},
                "clearance": {"value": 0, "unit": "mm"},
                "interferes": True,
                "revision": 1,
                "requirementId": "clearance-fail",
                "status": "FAIL",
            },
        ],
    )
    expected_bounds = {part["name"]: part["bbox"] for part in store.scene(1)["parts"]}
    refresh_trigger = tmp_path / "refresh-ready"
    with ViewerServer(store) as server:
        process = subprocess.Popen(
            [
                "node",
                str(ASSEMBLY_SMOKE),
                module,
                server.url,
                str(width),
                str(height),
                shots,
                str(refresh_trigger),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 30
        while not refresh_trigger.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert refresh_trigger.exists(), process.communicate(timeout=5)[1]
        refreshed_rows = store.scene(1)["assemblyInspection"]
        refreshed_rows[-1] = {
            **refreshed_rows[-1],
            "requirementId": "refreshed-clearance",
        }
        store.publish_assembly_inspection(1, list(reversed(refreshed_rows)))
        stdout, stderr = process.communicate(timeout=30)
    assert process.returncode == 0, stderr
    data = json.loads(stdout)
    assert data["errors"] == []
    assert data["initial"]["solveStatus"] == "solved · free motion remains"
    assert data["initial"]["solveWarning"] == "shaft: spin · 3 pins: spin"
    assert data["initial"]["solveSummary"] == "1 grounded · 0 conflicts"
    assert data["initial"]["bomCount"] == f'{len(query_bom["rows"])} definitions'
    assert data["initial"]["bomRows"] == [
        f'{row["label"]} · {row["count"]}' for row in query_bom["rows"]
    ]
    assert data["initial"]["bomExpanded"] is True
    assert data["initial"]["bomRows"][0] == f'{query_bom["rows"][0]["label"]} · {query_bom["rows"][0]["count"]}'
    assert data["initial"]["mateRows"] == [
        "pin-axis · concentricshaft-skew ↔ pin-leftsatisfied · aligned"
    ]
    assert data["initial"]["occurrenceStates"] == [
        "explicit · 1 DOF",
        "grounded · 0 DOF",
        "mated · 1 DOF",
        "mated · 1 DOF",
        "mated · 1 DOF",
    ]
    assert data["initial"]["panelOverflowX"] is False
    assert data["mateFocused"]["showing"] == (
        "Showing shaft-skew and pin-left, other parts faded."
    )
    assert data["initial"]["solveLabelLines"] == 1
    assert data["initial"]["inspectionCount"] == "1 error, 1 fail, 8 pass"
    assert data["initial"]["inspectionStatuses"] == ["ERROR", "FAIL", *("PASS" for _ in range(6))]
    assert data["initial"]["inspectionReasons"][:3] == [
        "clearance-error · clearance-fail",
        "assembly-clearance",
        "clearance-02 · clearance-reversed",
    ]
    assert data["initial"]["inspectionLabels"][:3] == [
        "pin-left ↔ pin-right",
        "shaft ↔ pin-left",
        "shaft ↔ pin-right",
    ]
    assert data["initial"]["inspectionValues"][:3] == [
        "clearance solver failed",
        "< 0.001 mm³ overlap",
        "touching",
    ]
    assert data["initial"]["morePassing"] == "Show 2 more passing pairs"
    assert data["initial"]["disclosureExpanded"] == "false"
    assert data["expanded"]["disclosureExpanded"] == "true"
    assert data["expanded"]["morePassing"] == "Hide 2 passing pairs"
    assert data["expanded"]["activeConnected"] is True
    assert data["focusedPass"]["disclosureExpanded"] == "true"
    assert data["focusedPass"]["activeConnected"] is True
    assert data["focusedPass"]["activeKey"] is not None
    assert data["selectedCollapsed"]["disclosureExpanded"] == "false"
    assert data["selectedCollapsed"]["pairPressed"] == "true"
    assert data["selectedCollapsed"]["morePassing"] == "Show 1 more passing pairs"
    assert data["selectedCollapsed"]["activeConnected"] is True
    assert len(data["selectedCollapsed"]["inspectionStatuses"]) == 9
    assert data["refreshed"]["disclosureExpanded"] == "true"
    assert data["refreshed"]["pairPressed"] == "true"
    assert data["refreshed"]["activeConnected"] is True
    assert data["escaped"]["disclosureExpanded"] == "true"
    assert data["escaped"]["activeConnected"] is True
    assert data["collapsed"]["disclosureExpanded"] == "false"
    assert data["collapsed"]["activeConnected"] is True
    assert len(data["collapsed"]["inspectionStatuses"]) == 8
    assert '"value": 0.0003' in data["initial"]["rawInspection"]
    assert data["initial"]["partNames"] == ["shaft", "shaft-skew", "pin-left", "pin-right", "pin-top"]
    assert data["initial"]["partSizes"][:2] == ["30 × 4 × 8", "30 × 4 × 8"]
    assert data["hidden"]["visible"] == ["false", "true", "true", "true", "true"]
    for name, bounds in expected_bounds.items():
        for limit in ("min", "max"):
            assert data["hidden"]["worldBounds"][name][limit] == pytest.approx(
                bounds[limit], abs=store.TOLERANCE
            )
    assert data["errorFocused"]["selectionEvidence"] == "Inspection error"
    assert data["errorFocused"]["colors"][2] == data["errorFocused"]["colors"][3] == "#c77800"
    assert data["focused"]["pairPressed"] == "true"
    assert data["focused"]["memory"]["cachedMeshes"] == 3
    assert data["focused"]["showing"] == "Showing shaft and pin-left, other parts faded."
    assert "shaft and pin-left" in data["focused"]["description"]
    assert data["focused"]["selectionEvidence"] == "Exact overlap: < 0.001 mm³"
    assert data["focused"]["overlapCount"] == 2  # saturated volume plus its edges
    assert data["focused"]["colors"][0] == "#c62828"
    assert data["focused"]["colors"][2] == "#2367c9"
    assert data["focused"]["opacities"][0] == data["focused"]["opacities"][2] == 0.32
    pixels = data["focused"]["overlapPixels"]
    assert pixels["sampled"] > 0
    assert pixels["dominant"] / pixels["sampled"] > 0.02, pixels
    assert pixels["dominant"] > pixels["pairTint"], pixels
    projected = data["focused"]["focusProjected"]
    assert max(point[0] for point in projected) - min(point[0] for point in projected) > 0.5
    assert max(point[1] for point in projected) - min(point[1] for point in projected) > 0.5
    assert all(-1 <= coordinate <= 1 for point in projected for coordinate in point)
    assert data["escaped"]["overlapCount"] == 0
    assert data["escaped"]["colors"] == data["initial"]["colors"]
    assert data["touching"]["selectionEvidence"] == "Touching · no collision"
    assert data["touching"]["overlapCount"] == 0
    assert data["touching"]["colors"][0] == data["touching"]["colors"][3] == "#2f7a46"
    assert data["refreshed"]["pairPressed"] == "true"
    assert data["escaped"]["pairPressed"] == "false"
    assert data["escaped"]["description"].startswith("All parts shown.")
    assert "section view" in data["section"]["description"]
    assert any("Show shaft" in name for name in data["hidden"]["controls"])
    assert all(name for name in data["hidden"]["controls"])


def test_page_draws_and_follows_revisions_in_a_real_browser(module):
    store = ViewerSceneStore()
    shots = os.environ.get("VIEWER_SCREENSHOTS")
    restarted = None
    with ViewerServer(store) as server:
        driver = subprocess.Popen(
            ["node", str(SMOKE), module, server.url, *([shots] if shots else [])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )

        def step(name, outage=False):
            line = driver.stdout.readline()
            assert line.startswith(f"step {name} "), line
            result = json.loads(line.split(" ", 2)[2])
            # no console errors in any state, expired key and no access included
            errors = [e for e in result["errors"] if not (outage and any(o in e for o in OUTAGE_ERRORS))]
            assert errors == [], (name, errors)
            return result

        def next_step():
            driver.stdin.write("next\n")
            driver.stdin.flush()

        try:
            empty = step("empty")
            assert empty["label"] == "none" and empty["link"] == "Live"
            assert empty["disabled"] == [True] * 5  # nothing to aim the camera at yet
            assert empty["gizmo"] is False  # no orientation to show before a model
            store.publish(1, [ScenePart("plate", plate(6))])
            next_step()

            first = step("revision-1")
            assert first["label"] == "1"
            assert first["disabled"] == [False] * 5
            assert first["gizmo"] is True
            assert "#token" not in first["url"]  # the token leaves the address bar
            assert first["dark"] > 0.0003  # edge lines of the plate are on the canvas
            store.publish(2, [ScenePart("plate", plate(10))])
            next_step()

            second = step("revision-2")
            assert second["label"] == "2"
            assert second["top"] < first["top"]  # same camera, taller plate reaches higher
            assert (second["viewTop"], second["viewBottom"]) == (0, 800)  # no status: full height
            store.publish_failure(["edge_fillet"], oversized_fillet_failure())
            next_step()

            failed = step("failure")
            assert failed["label"] == "2"
            assert "edge_fillet" in failed["failure"]
            # coalesce guidance, never the kernel's own helper names
            assert "Ask your agent to reduce the fillet radius" in failed["failure"], failed["failure"]
            assert "max_fillet" not in failed["failure"]
            # the last good scene stays: same revision, parts, meshes and gpu geometries, drawn
            assert failed["scene"]["revision"] == 2
            identity = ("revision", "parts", "meshes", "geometries")
            assert [failed["scene"][k] for k in identity] == [second["scene"][k] for k in identity]
            assert failed["dark"] > 0.0003
            # the status lies over the canvas: the canvas and the model keep their place
            assert (failed["viewTop"], failed["viewBottom"]) == (0, 800)
            assert failed["pageTop"] == second["pageTop"], (failed["pageTop"], second["pageTop"])
            next_step()

            front = step("front")
            assert front["pressed"] == "true" and front["dark"] > 0.0003
            server.close()  # the page stays open on the last scene
            next_step()

            lost = step("disconnected", outage=True)
            assert lost["link"] == "Reconnecting"
            # one calm status: the connection is the news, the old failure one sentence of it
            assert lost["banner"] == (
                "Revision 2 may be out of date. Reconnecting to coalesce. The last edit, edge_fillet, failed."
            )
            assert lost["failure"] is None
            assert "grayscale(1)" in lost["filter"]
            assert (lost["viewTop"], lost["viewBottom"]) == (0, 800), lost
            assert lost["pageTop"] == lost["before"], (lost["pageTop"], lost["before"])
            # same port and key, as after a brief drop: the page must recover by itself
            restarted = ViewerServer(store, port=server.port)
            restarted._token = server._token  # test-only: a real restart would print a new key
            next_step()

            back = step("reconnected", outage=True)
            assert (back["link"], back["banner"], back["filter"]) == ("Live", None, "none")
            assert back["pageTop"] == back["before"], (back["pageTop"], back["before"])
            for revision in range(3, 53):  # back to back, so some loads lose the race
                store.publish(revision, [ScenePart("plate", plate(4 + revision % 7))])
            next_step()

            stress = step("stress")
            # 50 more revisions free what they replace: gpu counts match the one-part baseline
            assert stress["memory"] == second["memory"], (stress["memory"], second["memory"])
            # step() also checked that skipped revisions logged no console errors
            restarted.close()
            restarted = ViewerServer(store, port=server.port)  # a real restart: a new key
            next_step()

            ended = step("new-link", outage=True)  # a brief gap while the port changes hands
            assert ended["link"] == "Viewer ended"  # a new key: no waiting out the retry bound
            assert "open the new link it printed" in ended["status"]
            assert "stderr" in ended["status"]
            assert ended["eventRequestsAfter"] == 0  # the page stopped retrying the old key
            assert ended["gizmo"] is False
            assert "Revision 52 is the last one this page saw" in ended["status"]
            # nothing current to aim at: orbit and view buttons are off
            assert ended["camera"] is False and ended["disabled"] == [True] * 5
            # an expired key is an expected state, not a browser error (no 401s)
            next_step()

            bare = step("no-access")
            assert bare["link"] == "No access" and bare["gizmo"] is False
            assert bare["meta"] == (
                "Open the link printed as “coalesce viewer:” to see the current model"
            )
            assert "error output (stderr)" in bare["notice"]
            assert bare["revisionVisible"] is False
            assert bare["partsVisible"] is False
            next_step()
            assert driver.wait(timeout=30) == 0
        finally:
            driver.kill()
            if restarted:
                restarted.close()


UX = Path(__file__).with_name("viewer_ux_smoke.mjs")
LONG = "a_very_long_part_name_for_the_two_thousand_millimeter_test"


def bar(length: float) -> Box:
    return Box(0.5, 0.5, length, align=(Align.CENTER, Align.CENTER, Align.MIN))


def on_screen(result) -> bool:
    return all(-1 <= c <= 1 for corner in result["scene"]["projected"] for c in corner)


def scale_matches_grid(result) -> bool:
    # narrow layouts show a compact scale: the bar length only
    bar_mm, grid_mm = re.fullmatch(r"(\S+) mm(?:, grid (\S+) mm)?", result["scale"]).groups()
    return grid_mm in (None, bar_mm)


@pytest.mark.parametrize("viewport", [(1280, 800), (390, 844)])
def test_viewer_frames_fails_and_ends_honestly(module, viewport):
    store = ViewerSceneStore()
    shots = os.environ.get("VIEWER_SCREENSHOTS")
    server = ViewerServer(store)
    driver = subprocess.Popen(
        ["node", str(UX), module, server.url, *map(str, viewport), *([shots] if shots else [])],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )

    def step(name, outage=False):
        line = driver.stdout.readline()
        assert line.startswith(f"step {name} "), line
        result = json.loads(line.split(" ", 2)[2])
        # no console errors and no failed requests, the session request included
        errors = [e for e in result["errors"] if not (outage and any(o in e for o in OUTAGE_ERRORS))]
        assert errors == [], (name, errors)
        driver.stdin.write("next\n")
        driver.stdin.flush()
        return result

    try:
        empty = step("empty")
        assert empty["parts"] == "No parts yet"
        assert '- img "3D view of the current revision"' in empty["aria"]
        store.publish(1, [])  # a sketch alone: a revision with no solid parts

        solids = step("no-solids")
        assert "Revision 1 has no solid parts yet" in solids["notice"]
        assert solids["fitDisabled"] is True and solids["scale"] is None
        store.publish(2, [ScenePart("plate_extrude", plate(10))])

        first = step("plate")
        # the first solid is fitted even though an empty revision came first
        assert first["coverage"] > 0.02 and first["dark"] > 0.0003, first
        assert on_screen(first) and scale_matches_grid(first), first
        assert first["description"].startswith("Revision 2, iso view, fitted to the model. plate_extrude")
        store.publish_failure(
            ["plate_extrude"],
            {"code": "UNIT_MISMATCH", "message": "expected mm; received inch", "kernelMessage": "BRep_API: command not done"},
        )

        failed = step("failure")
        # the last good model stays drawn, at about the same size
        assert failed["coverage"] > first["coverage"] * 0.8 and failed["dark"] > 0.0003, failed
        assert failed["repair"].startswith("A value has the wrong unit (expected mm; received inch)")
        assert failed["codeVisible"] is False  # the stable code waits under "Error details"
        assert failed["kernelVisible"] is False and "BRep" not in failed["repair"]
        details = step("failure-details")
        assert details["codeVisible"] is True and details["kernelVisible"] is True
        assert details["kernel"] == "Kernel: BRep_API: command not done"
        store.publish(3, [ScenePart("plate_extrude", plate(10)), ScenePart(LONG, bar(2000))])

        large = step("large")
        # 0.5 mm and 2000 mm together: fitted, and scale bar and grid tell the same scale
        assert on_screen(large) and scale_matches_grid(large), large
        assert large["status"] is None and large["fit"] == "Fit"

        focused = step("long-name-focus")
        assert focused["name"] == {"text": LONG, "clipped": False}

        orbited = step("orbited")
        store.publish(4, [
            ScenePart("plate_extrude", plate(10)),
            ScenePart(LONG, bar(2000)),
            ScenePart("far_block", Box(100, 100, 100).moved(Location((3000, 0, 0)))),
        ])

        held = step("held")
        # the human's view stays; the page offers the fit instead of forcing it
        assert held["scene"]["view"] == orbited["scene"]["view"]
        assert held["fit"] == "Fit new model" and not on_screen(held)
        refit = step("refit")
        assert refit["fit"] == "Fit" and on_screen(refit) and scale_matches_grid(refit), refit
        server.close()  # for good: a restarted coalesce prints a new link on a new port

        retrying = step("reconnecting", outage=True)
        assert retrying["link"] == "Reconnecting"
        assert "Revision 4 may be out of date. Reconnecting to coalesce." == retrying["status"]

        ended = step("ended", outage=True)
        assert ended["link"] == "Viewer ended"
        assert ended["status"].startswith("Viewer ended. Revision 4 is the last one this page saw.")
        assert "cannot reconnect" in ended["status"] and "coalesce viewer:" in ended["status"]
        assert "select server" in ended["status"]
        assert ended["requestsAfter"] == []  # it stopped retrying
        assert ended["buttons"] == [True] * 5
        # the last model stays where it was, dimmed by css; the hidden axes gizmo was part of the refit count
        assert ended["scene"]["projected"] == refit["scene"]["projected"]
        assert ended["coverage"] > refit["coverage"] * 0.8
        assert driver.wait(timeout=30) == 0
    finally:
        driver.kill()
        server.close()
