"""Strict UX review 3 flow: an enclosure built through the MCP tool layer, shown in a real browser.

The browser test skips when Node or a launchable Playwright is not on this machine.
Set VIEWER_SCREENSHOTS to a directory to keep a screenshot of every step.
"""

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from test_mcp_cut_hole_circle import cut, extrude, rectangle_sketch
from test_mcp_join_holes_frames import join_extrude, patterned_hole
from test_mcp_vertical import call_server_tool
from test_viewer_browser import OUTAGE_ERRORS, playwright_module

from coalesce.service import DocumentService
from coalesce.viewer import ViewerSceneStore, ViewerServer

DRIVER = Path(__file__).with_name("viewer_review_smoke.mjs")
NAMES = ["nw", "ne", "sw", "se"]
POSITIONS = [
    {"name": name, "point": point}
    for name, point in zip(NAMES, ([-25, 15], [25, 15], [-25, -15], [25, -15]), strict=True)
]


def mm(value):
    return {"value": value, "unit": "mm"}


def wall_sketch(revision):
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": "wall-sketch",
        "sketchId": "wall_sketch",
        "plane": {"kind": "origin", "name": "XY", "offset": mm(3)},
        "entities": [
            {"id": "outer", "kind": "rectangle", "width": mm(60), "height": mm(40)},
            {"id": "inner", "kind": "rectangle", "width": mm(54), "height": mm(34)},
        ],
    }


def enclosure(port_target):
    """Plate, joined wall ring, four named holes, an XZ port cut, then a thicker plate.

    The review's model cut the port into ``wall``, which ``holes`` already consumed: a fork
    that leaves two coincident bodies. ``port_target="holes"`` is the single-body model.
    """
    return [
        ("sketch", rectangle_sketch(request_id="plate-sketch", revision=0, sketch_id="plate_sketch",
                                    entity_id="plate", width=60, height=40)),
        ("feature", extrude(request_id="plate", revision=1, feature_id="plate_extrude",
                            sketch_id="plate_sketch", profile_ids=["plate"], distance=3)),
        ("sketch", wall_sketch(2)),
        ("feature", join_extrude(request_id="wall", revision=3, feature_id="wall", sketch_id="wall_sketch",
                                 profile_ids=["outer", "inner"], distance=22, target="plate_extrude")),
        ("feature", patterned_hole(request_id="m3", revision=4, feature_id="holes", target="wall",
                                   positions=POSITIONS)),
        ("sketch", rectangle_sketch(request_id="port-sketch", revision=5, sketch_id="port_sketch",
                                    entity_id="usb_c", width=9, height=3.5, plane="XZ", center=(0, 10))),
        ("feature", cut(request_id="portcut", revision=6, feature_id="portcut", target=port_target,
                        tool="port_sketch")),
        ("feature", {"action": "update", "expectedRevision": 7, "requestId": "thick",
                     "featureId": "plate_extrude", "kind": "extrude", "set": {"distance": mm(5)}}),
    ]


CHANGES = [
    "added sketch plate_sketch (1 profile)",
    "added extrude plate_extrude (3 mm)",
    "added sketch wall_sketch (2 profiles)",
    "added join extrude wall on plate_extrude (22 mm)",
    "added hole feature holes on wall (4 holes)",
    "added sketch port_sketch (1 profile)",
    "added cut portcut on {target}",
    "edited extrude plate_extrude distance: 3 \u2192 5 mm",
]
BASE = [{"id": "plate_extrude", "kind": "extrude"}, {"id": "wall", "kind": "join extrude"}]
HOLES = {"id": "holes", "kind": "hole feature", "holes": NAMES}
PORT = {"id": "portcut", "kind": "cut"}


def call(service, tool, arguments):
    return asyncio.run(call_server_tool(service, tool, arguments))


def wait_for(store, revision):
    deadline = time.monotonic() + 10
    while store.current_revision != revision and time.monotonic() < deadline:
        time.sleep(0.02)
    return store.scene(revision)


@pytest.mark.parametrize(
    ("target", "lineages"),
    [("holes", [[*BASE, HOLES, PORT]]), ("wall", [[*BASE, HOLES], [*BASE, PORT]])],
)
def test_scene_names_the_change_and_the_features_of_each_part(tmp_path, target, lineages):
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / "box.coalesce.json", scene_store=store)
    changes = []
    for tool, arguments in enclosure(target):
        revision = call(service, tool, arguments)["revision"]
        scene = wait_for(store, revision)
        changes.append(scene["change"])

    assert changes == [
        {"featureId": arguments.get("featureId") or arguments["sketchId"], "summary": text.format(target=target)}
        for (_, arguments), text in zip(enclosure(target), CHANGES, strict=True)
    ]
    # the fork leaves two coincident 60 x 40 x 25 bodies; the features tell them apart
    parts = scene["parts"]
    assert [part["features"] for part in parts] == lineages
    assert all(part["size"] == pytest.approx([60, 40, 25]) for part in parts)
    # a document opened from disk has no commit to describe
    reopened = ViewerSceneStore()
    DocumentService(tmp_path / "box.coalesce.json", scene_store=reopened)
    assert wait_for(reopened, 8)["change"] is None


@pytest.fixture(scope="module")
def module():
    found = playwright_module()
    if not found:
        pytest.skip("no Node with a launchable Playwright Chromium on this machine")
    return found


@pytest.mark.parametrize("viewport", [(1280, 800), (390, 844)])
def test_review_flow_reads_clearly_in_a_real_browser(module, viewport, tmp_path, monkeypatch):
    narrow = viewport[0] < 720
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / "box.coalesce.json", scene_store=store)
    server = ViewerServer(store)
    restarted = None
    shots = os.environ.get("VIEWER_SCREENSHOTS")
    driver = subprocess.Popen(
        ["node", str(DRIVER), module, server.url, *map(str, viewport), *([shots] if shots else [])],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )

    def step(name, outage=False, **command):
        driver.stdin.write(json.dumps({"name": name, **command}) + "\n")
        driver.stdin.flush()
        line = driver.stdout.readline()
        assert line.startswith(f"step {name} "), line
        result = json.loads(line.split(" ", 2)[2])
        # zero console errors and failed requests, apart from a stopped server's refusals
        errors = [e for e in result["errors"] if not (outage and any(o in e for o in OUTAGE_ERRORS))]
        assert errors == [], (name, errors)
        return result

    def commit(tool, arguments):
        result = call(service, tool, arguments)
        assert result.get("ok"), result
        return result["revision"]

    def fail(tool, arguments, code):
        result = call(service, tool, arguments)
        assert result["error"]["code"] == code, result
        return step(code.lower(), wait={"failure": code})

    try:
        step("empty", wait={"ready": True})
        flow = enclosure("holes")
        for tool, arguments in flow[:5]:
            revision = commit(tool, arguments)
        holes = step("holes", wait={"revision": revision})
        # the latest change sits apart from the revision number
        assert holes["change"] == "Added hole feature holes on wall (4 holes)"
        assert "Latest change: added hole feature holes on wall (4 holes)." in holes["description"]
        assert "4 holes: nw, ne, sw, se" in holes["description"]
        detail = "4 holes: nw, ne, sw, se. Built by plate_extrude \u203a wall \u203a holes"
        # narrow screens show the hole names only for the focused (tapped) part
        assert holes["details"] == [None if narrow else detail]
        focused = step("holes-focused", do=[{"focusPart": 0}])
        assert focused["details"] == [detail]
        step("blur", do=[{"blur": True}])

        for tool, arguments in flow[5:]:
            revision = commit(tool, arguments)
        thick = step("thickness", wait={"revision": revision})
        assert thick["change"] == "Edited extrude plate_extrude distance: 3 \u2192 5 mm"
        assert len(thick["parts"]) == 1
        # a compact scale stays on narrow screens; wide ones also name the grid
        assert thick["scale"] == ("20 mm" if narrow else "20 mm, grid 20 mm"), thick["scale"]

        overlap = patterned_hole(request_id="overlap", revision=revision, feature_id="pair", target="portcut",
                                 positions=[{"name": "a", "point": [0, 0]}, {"name": "b", "point": [1, 0]}])
        intersect = fail("feature", overlap, "HOLES_INTERSECT")
        assert intersect["revision"] == revision and intersect["change"] == thick["change"]
        assert intersect["scene"]["parts"] == thick["scene"]["parts"]  # the last good model stays

        floating = rectangle_sketch(request_id="lid-sketch", revision=revision, sketch_id="lid_sketch",
                                    entity_id="lid", width=10, height=10, offset=40)
        revision = commit("sketch", floating)
        step("lid-sketch", wait={"revision": revision})
        lid = join_extrude(request_id="lid", revision=revision, feature_id="lid", sketch_id="lid_sketch",
                           profile_ids=["lid"], distance=2, target="portcut")
        no_contact = fail("feature", lid, "JOIN_NO_CONTACT")
        assert no_contact["repair"].endswith("Ask your agent to move or extend the listed profiles to contact the target.")

        invalid = {**flow[-1][1], "expectedRevision": revision, "requestId": "colour", "colour": "red"}
        schema = fail("feature", invalid, "INVALID_PARAMETER")
        assert "unknown field 'colour'" in schema["repair"]

        def kernel_failure(graph):
            raise RuntimeError("BRep_API: command not done")

        monkeypatch.setattr("coalesce.service.regenerate", kernel_failure)
        thicker = {**flow[-1][1], "expectedRevision": revision, "requestId": "kernel"}
        kernel = fail("feature", thicker, "KERNEL_OPERATION_FAILED")
        monkeypatch.undo()
        assert kernel["repair"].startswith("These values do not make valid geometry")
        assert "BRep" not in kernel["status"]

        # the review's fork: a second cut into a body another cut already consumed
        slot = rectangle_sketch(request_id="slot-sketch", revision=revision, sketch_id="slot_sketch",
                                entity_id="slot", width=20, height=3, center=(0, -8))
        revision = commit("sketch", slot)
        revision = commit("feature", cut(request_id="slot", revision=revision, feature_id="slot_cut",
                                         target="holes", tool="slot_sketch"))
        fork = step("fork-top", wait={"revision": revision}, do=[{"click": "Top"}],
                    flat={"fx": [0.25, 0.75], "fy": [0.55, 0.75]})
        assert len(fork["parts"]) == 2
        # the floor of both bodies lies in one plane: it must read as one flat color, not
        # z-fighting triangles
        assert fork["flat"]["pixels"] > 1000, fork["flat"]
        assert fork["flat"]["off"] <= fork["flat"]["pixels"] * 0.002, fork["flat"]
        step("fork-iso", do=[{"click": "Iso"}])

        # keyboard only: tab from the page start to a part row, then to a view button
        keys = step("keyboard-part", do=[{"blur": True}, {"tabTo": "plate_extrude"}])
        assert keys["actions"][1]["visited"][-1] == "plate_extrude"
        assert all(d is not None for d in keys["details"][:1])  # the focused part shows its detail
        keys = step("keyboard-front", do=[{"tabTo": "Front", "press": "Enter"}])
        assert keys["actions"][0]["visited"][-1] == "Front"
        assert keys["description"].startswith(f"Revision {revision}, front view")

        tiny = rectangle_sketch(request_id="tiny-sketch", revision=revision, sketch_id="tiny_sketch",
                                entity_id="tiny", width=0.2, height=0.2, center=(100, 0))
        bar = rectangle_sketch(request_id="bar-sketch", revision=revision + 2, sketch_id="bar_sketch",
                               entity_id="bar", width=0.5, height=0.5, center=(-100, 0))
        revision = commit("sketch", tiny)
        revision = commit("feature", extrude(request_id="tiny", revision=revision, feature_id="tiny_extrude",
                                             sketch_id="tiny_sketch", profile_ids=["tiny"], distance=0.2))
        revision = commit("sketch", bar)
        revision = commit("feature", extrude(request_id="bar", revision=revision, feature_id="bar_extrude",
                                             sketch_id="bar_sketch", profile_ids=["bar"], distance=2000))
        extremes = step("tiny-and-2000", wait={"revision": revision}, do=[{"click": "Iso"}])
        assert all(-1 <= c <= 1 for corner in extremes["scene"]["projected"] for c in corner)
        assert "0.2 × 0.2 × 0.2" in extremes["description"] and "0.5 × 0.5 × 2000" in extremes["description"]

        # restart: a new viewer server has a new key; the old tab ends and offers one action
        server.close()
        restarted = ViewerServer(store)
        ended = step("ended", outage=True, wait={"text": "Viewer ended"})
        assert "coalesce viewer: http://127.0.0.1:<port>/#token=<key>" in ended["status"]
        copied = step("ended-copied", outage=True, do=[{"click": "Copy request for your agent", "until": "Copied. Paste it to your agent"}])
        assert copied["clipboard"].startswith("The coalesce viewer I had open has ended.")
        assert '{"select": {"kind": "server"}}' in copied["clipboard"]
        fresh = step("new-link", outage=True, do=[{"goto": restarted.url, "revision": revision}])
        assert fresh["link"] == "Live" and fresh["status"] is None
        driver.stdin.close()
        assert driver.wait(timeout=30) == 0
    finally:
        driver.kill()
        server.close()
        if restarted:
            restarted.close()
