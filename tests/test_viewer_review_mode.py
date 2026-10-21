"""Enclosure review modes in a real browser: lid off, exploded and one section plane.

These modes only move, hide or cut what the page draws; the scene and the document never change.
The browser tests skip when Node or a launchable Playwright is not on this machine (they run in CI).
Set VIEWER_SCREENSHOTS to a directory to keep a screenshot of every step.
"""

import copy

import pytest
from test_feature_delete import delete_feature
from test_pcb_enclosure import FIXTURES, base_request, import_request, quantity, standoffs_request
from test_pcb_lid import lid_request
from test_port_cutout import port_request
from test_viewer_browser import plate
from test_viewer_review_flow import call, module, open_driver, wait_for  # noqa: F401  (module is a fixture)

from coalesce.service import DocumentService
from coalesce.viewer import ScenePart, ViewerSceneStore, ViewerServer

MODES = ["Assembled", "Lid off", "Exploded", "Section"]
STANDOFF_Y = 3  # the board's two mounting holes (and their standoffs) sit on this line


def enclosure(service):
    """Board reference, base with standoffs, a lid with a lip, and a port below the lip."""
    for tool, arguments in [
        ("import", import_request(FIXTURES / "features.kicad_pcb")),
        # walls tall enough that the port opening sits below the lid's lip
        ("feature", base_request(wallHeightAboveBoard=quantity(8))),
        ("feature", standoffs_request()),
        ("feature", lid_request(revision=3)),
        ("feature", port_request(revision=4)),
    ]:
        result = call(service, tool, arguments)
        assert result.get("ok"), result
    return result["revision"]


def review(result, index=0):
    return result["actions"][index]


def pressed(state):
    return [mode["name"] for mode in state["modes"] if mode["pressed"] == "true"]


def overlaps(a, b):
    return a["left"] < b["right"] and b["left"] < a["right"] and a["top"] < b["bottom"] and b["top"] < a["bottom"]


def assert_clear_layout(state):
    """The review controls sit inside the canvas and cover none of the other controls."""
    boxes = state["boxes"]
    canvas = boxes["#view"]
    for name in ("#modes", "#section-controls", "#review-hint"):
        mine = boxes[name]
        if mine is None:
            continue
        assert canvas["left"] <= mine["left"] and mine["right"] <= canvas["right"], (name, mine, canvas)
        assert canvas["top"] <= mine["top"] and mine["bottom"] <= canvas["bottom"], (name, mine, canvas)
        for other in (".views", "#scale", "#gizmo", "#status", ".panel"):
            if boxes[other] is not None:
                assert not overlaps(mine, boxes[other]), (name, other, mine, boxes[other])


@pytest.mark.parametrize("viewport", [(1280, 800), (390, 844)])
def test_review_modes_show_inside_the_enclosure_and_restore_it_exactly(module, viewport, tmp_path):  # noqa: F811
    store = ViewerSceneStore()
    path = tmp_path / "box.coalesce.json"
    service = DocumentService(path, scene_store=store)
    server = ViewerServer(store)
    driver, step = open_driver(module, server.url, viewport)
    try:
        step("empty", wait={"ready": True})
        revision = enclosure(service)
        scene = copy.deepcopy(wait_for(store, revision))
        document = path.read_bytes()
        labels = [part["label"] for part in scene["parts"]]
        assert labels == ["pcb reference", "base", "lid"]
        board, base, lid = (labels.index(name) for name in ("pcb reference", "base", "lid"))

        assembled = step("review-assembled", wait={"revision": revision},
                         do=[{"settle": True}, {"review": True}, {"capture": "assembled"}])
        state = review(assembled, 1)
        assert [mode["name"] for mode in state["modes"]] == MODES
        assert pressed(state) == ["Assembled"]
        assert [mode["disabled"] for mode in state["modes"]] == [False] * 4
        assert state["section"] is None
        drawn = assembled["scene"]
        assert drawn["mode"] == "assembled"
        assert drawn["positions"] == [[0, 0, 0]] * 3
        assert drawn["visible"] == [True] * 3
        assert drawn["cut"] == [False] * 3
        assert_clear_layout(state)
        state_memory = state["memory"]

        lid_off = step("review-lid-off", do=[{"click": "Lid off"}, {"settle": True}, {"review": True},
                                              {"diff": "assembled"}])
        assert pressed(review(lid_off, 2)) == ["Lid off"]
        assert lid_off["scene"]["visible"] == [i != lid for i in range(3)]
        assert lid_off["scene"]["positions"] == [[0, 0, 0]] * 3
        assert review(lid_off, 3) > 0.01  # the board and standoffs show where the lid was
        # the part list still says what the user chose: the lid is shown, only this view lifts it off
        assert lid_off["pressed"] == ["true"] * 3

        exploded = step("review-exploded", do=[{"click": "Exploded"}, {"settle": True}, {"review": True}])
        assert pressed(review(exploded, 2)) == ["Exploded"]
        drawn = exploded["scene"]
        assert drawn["visible"] == [True] * 3
        z = drawn["zRanges"]
        gap = 0.4 * (scene["bbox"]["max"][2] - scene["bbox"]["min"][2])
        # parts move along Z only, the base stays, and each layer clears the one below by one fixed gap
        assert all(x == 0 and y == 0 for x, y, _ in drawn["positions"]), drawn["positions"]
        assert drawn["positions"][base][2] == 0
        assert z[board][0] - z[base][1] == pytest.approx(gap, abs=1e-3)
        assert z[lid][0] - z[board][1] == pytest.approx(gap, abs=1e-3)
        assert all(-1 <= c <= 1 for corner in drawn["projected"] for c in corner)  # refit: all on screen

        section = step("review-section", do=[{"click": "Section"}, {"settle": True}, {"review": True},
                                              {"diff": "assembled"}, {"capture": "section"}])
        state = review(section, 2)
        assert pressed(state) == ["Section"]
        drawn = section["scene"]
        assert drawn["positions"] == [[0, 0, 0]] * 3 and drawn["visible"] == [True] * 3
        assert drawn["cut"] == [True] * 3  # the board too: an uncut board would hide the standoffs
        assert state["section"]["axis"] == "Y"
        assert state["section"]["value"] == pytest.approx(
            (state["section"]["min"] + state["section"]["max"]) / 2, abs=0.05)
        assert review(section, 3) > 0.01  # the cut shows walls, lip and standoffs
        assert_clear_layout(state)

        moved = step("review-section-moved", do=[
            {"fill": {"label": "Section position in mm", "value": STANDOFF_Y}}, {"settle": True},
            {"review": True}, {"diff": "section"}])
        assert review(moved, 2)["section"]["value"] == STANDOFF_Y
        assert review(moved, 3) > 0.005  # a new cut position draws a new cut
        axis = step("review-section-x", do=[{"click": "X"}, {"settle": True}, {"review": True},
                                             {"diff": "section"}])
        assert review(axis, 2)["section"]["axis"] == "X"
        assert review(axis, 3) > 0.005

        # keyboard only: tab to Assembled and press Enter
        keys = step("review-keyboard", do=[{"tabTo": "Assembled", "press": "Enter"}, {"settle": True},
                                           {"review": True}, {"diff": "assembled"}])
        assert keys["focus"]["outline"] == "solid 2px", keys["focus"]
        assert pressed(review(keys, 2)) == ["Assembled"]
        drawn = keys["scene"]
        assert drawn["positions"] == [[0, 0, 0]] * 3 and drawn["visible"] == [True] * 3
        assert drawn["cut"] == [False] * 3
        assert drawn["projected"] == assembled["scene"]["projected"]
        assert review(keys, 3) == 0  # the assembled view comes back pixel for pixel
        assert review(keys, 2)["memory"] == state_memory  # cut materials were freed
        # nothing reached the scene or the document
        assert store.scene(revision) == scene
        assert path.read_bytes() == document
        assert keys["revision"] == revision

        # a new revision keeps the chosen mode while it still applies
        step("review-exploded-again", do=[{"click": "Exploded"}, {"settle": True}])
        result = call(service, "feature", delete_feature("lid", revision, "delete-lid"))
        assert result.get("ok"), result
        no_lid = step("review-no-lid", wait={"revision": revision + 1}, do=[{"review": True}])
        state = review(no_lid)
        assert pressed(state) == ["Exploded"]
        assert [mode["disabled"] for mode in state["modes"]] == [False, True, False, False]
        assert state["modes"][1]["reason"] == "No lid in this model"
        z = no_lid["scene"]["zRanges"]
        assert z[0][0] > z[1][1]  # the board still floats above the base
        driver.stdin.close()
        assert driver.wait(timeout=30) == 0
    finally:
        driver.kill()
        server.close()


@pytest.mark.parametrize("viewport", [(1280, 800), (390, 844)])
def test_modes_that_do_not_apply_are_disabled_with_a_reason(module, viewport):  # noqa: F811
    store = ViewerSceneStore()
    server = ViewerServer(store)
    driver, step = open_driver(module, server.url, viewport)
    try:
        step("empty", wait={"ready": True})
        store.publish(1, [ScenePart("plate", plate(6))])
        shown = step("plate-modes", wait={"revision": 1}, do=[{"review": True}])
        state = review(shown)
        assert [mode["disabled"] for mode in state["modes"]] == [False, True, True, False]
        assert [mode["reason"] for mode in state["modes"][1:3]] == [
            "No lid in this model", "No lid or board to separate"]
        assert state["hint"] is None
        # choosing one says why, and keeps the assembled view
        tried = step("plate-lid-off", do=[{"force": "Lid off"}, {"review": True}])
        state = review(tried, 1)
        assert state["hint"] == "Lid off: no lid in this model"
        assert pressed(state) == ["Assembled"]
        assert tried["scene"]["visible"] == [True]
        assert_clear_layout(state)
        cut = step("plate-section", do=[{"click": "Section"}, {"review": True}])
        assert pressed(review(cut, 1)) == ["Section"]
        assert cut["scene"]["cut"] == [True]
        assert_clear_layout(review(cut, 1))
        driver.stdin.close()
        assert driver.wait(timeout=30) == 0
    finally:
        driver.kill()
        server.close()
