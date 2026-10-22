"""Strict UX review 3 flow: an enclosure built through the MCP tool layer, shown in a real browser.

The browser test skips when Node or a launchable Playwright is not on this machine.
Set VIEWER_SCREENSHOTS to a directory to keep a screenshot of every step.
"""

import asyncio
import json
import math
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from build123d import Align, Box, GeomType, Pos
from OCP.BRepAdaptor import BRepAdaptor_Surface
from test_feature_delete import delete_feature
from test_mcp_cut_hole_circle import cut, extrude, rectangle_sketch
from test_mcp_join_holes_frames import join_extrude, patterned_hole
from test_mcp_kicad_import import FIXTURE as KICAD_FIXTURE
from test_mcp_kicad_import import import_request
from test_mcp_vertical import call_server_tool
from test_pcb_enclosure import FIXTURES as ENCLOSURE_FIXTURES
from test_pcb_enclosure import base_request, standoffs_request
from test_pcb_enclosure import import_request as enclosure_import
from test_pcb_lid import lid_request
from test_port_cutout import port_request
from test_viewer_browser import OUTAGE_ERRORS, oversized_fillet_failure, playwright_module

from coalesce.service import DocumentService
from coalesce.viewer import ScenePart, ViewerSceneStore, ViewerServer
from coalesce.viewer.scene import _oklab

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


def test_scene_names_the_change_and_the_features_of_each_part(tmp_path):
    target = "holes"
    lineages = [[*BASE, HOLES, PORT]]
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
    parts = scene["parts"]
    assert [part["features"] for part in parts] == lineages
    assert all(part["size"] == pytest.approx([60, 40, 25]) for part in parts)
    # a document opened from disk has no commit to describe
    reopened = ViewerSceneStore()
    DocumentService(tmp_path / "box.coalesce.json", scene_store=reopened)
    assert wait_for(reopened, 8)["change"] is None


def test_change_names_sketch_edits_and_board_import_and_refresh(tmp_path):
    source = tmp_path / "board.kicad_pcb"
    shutil.copyfile(KICAD_FIXTURE, source)
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / "box.coalesce.json", scene_store=store)
    sketch = rectangle_sketch(request_id="s", revision=0, sketch_id="plate_sketch", entity_id="plate",
                              width=60, height=40)

    def refresh(request_id):
        return {"action": "refresh", "requestId": request_id, "assetId": "controller_board", "format": "kicad_pcb"}

    two = [*sketch["entities"], {**sketch["entities"][0], "id": "tab", "center": [40, 0]}]
    mutations = [
        ("sketch", sketch, "added sketch plate_sketch (1 profile)"),
        ("sketch", {"action": "update", "requestId": "move", "sketchId": "plate_sketch",
                    "set": {"position": [5, 2.5, 0]}},
         "edited sketch plate_sketch position: (0, 0, 0) \u2192 (5, 2.5, 0) mm"),
        ("sketch", {"action": "update", "requestId": "tab", "sketchId": "plate_sketch",
                    "set": {"entities": two}},
         "edited sketch plate_sketch profiles: 1 \u2192 2"),
        ("import", import_request(source), "imported board board.kicad_pcb"),
        ("import", refresh("same"), "refreshed board controller_board, source unchanged"),
        ("import", refresh("changed"), "refreshed board: source changed"),
    ]
    summaries = []
    for revision, (tool, arguments, _) in enumerate(mutations, start=1):
        if revision == 6:
            source.write_bytes(source.read_bytes().replace(b"(thickness 1.2)", b"(thickness 1.6)"))
        result = call(service, tool, {**arguments, "expectedRevision": revision - 1})
        assert result.get("revision") == revision, result
        summaries.append(wait_for(store, revision)["change"]["summary"])
    assert summaries == [summary for *_, summary in mutations]


@pytest.fixture(scope="module")
def module():
    found = playwright_module()
    if not found:
        pytest.skip("no Node with a launchable Playwright Chromium on this machine")
    return found


def open_driver(module, url, viewport):
    """Start the browser driver on ``url``; ``step`` sends one command and checks the page has no errors."""
    shots = os.environ.get("VIEWER_SCREENSHOTS")
    driver = subprocess.Popen(
        ["node", str(DRIVER), module, url, *map(str, viewport), *([shots] if shots else [])],
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

    return driver, step


@pytest.mark.parametrize("viewport", [(1280, 800), (390, 844)])
def test_review_flow_reads_clearly_in_a_real_browser(module, viewport, tmp_path, monkeypatch):
    narrow = viewport[0] < 720
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / "box.coalesce.json", scene_store=store)
    server = ViewerServer(store)
    restarted = None
    driver, step = open_driver(module, server.url, viewport)
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
        assert intersect["repair"].endswith("The last good model is still shown.")
        assert "greater than 0 mm" not in intersect["repair"]

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
        assert kernel["repair"].endswith("The last good model is still shown.")
        assert "greater than 0 mm" not in kernel["repair"]
        assert "BRep" not in kernel["status"]

        # Continue the terminal lineage; body forks are rejected by graph validation.
        slot = rectangle_sketch(request_id="slot-sketch", revision=revision, sketch_id="slot_sketch",
                                entity_id="slot", width=20, height=3, center=(0, -8))
        revision = commit("sketch", slot)
        revision = commit("feature", cut(request_id="slot", revision=revision, feature_id="slot_cut",
                                         target="portcut", tool="slot_sketch"))
        model = step("slot-top", wait={"revision": revision}, do=[{"click": "Top"}])
        assert len(model["parts"]) == 1
        step("slot-iso", do=[{"click": "Iso"}])
        names = ["plate_extrude"]
        # a real tap (touch on narrow) on a row shows its details and does not hide the part
        tapped = step("slot-tap", do=[{"blur": True}, {"tapPart": 0}])
        assert tapped["pressed"] == ["true"] and tapped["scene"]["visible"] == [True]
        assert tapped["details"][0] == "4 holes: nw, ne, sw, se. Built by plate_extrude \u203a wall \u203a holes \u203a portcut \u203a slot_cut"
        assert tapped["names"] == names
        # the swatch is the show/hide control.
        hidden = step("slot-hide", do=[{"toggle": 0}])
        assert hidden["pressed"] == ["false"] and hidden["scene"]["visible"] == [False]
        shown = step("slot-show", do=[{"toggle": 0}, {"blur": True}])
        assert shown["pressed"] == ["true"] and shown["scene"]["visible"] == [True]

        # keyboard only: tab from the page start to a part row, then to a view button
        keys = step("keyboard-part", do=[{"blur": True}, {"tabTo": names[0]}])
        assert keys["actions"][1]["visited"][-1] == names[0]
        assert keys["details"][0] is not None  # the focused part shows its detail, narrow too
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
        extremes = step(
            "tiny-and-2000",
            wait={"revision": revision},
            do=[{"click": "Iso"}, {"stallSession": True}],
        )
        assert all(-1 <= c <= 1 for corner in extremes["scene"]["projected"] for c in corner)
        assert "0.2 × 0.2 × 0.2" in extremes["description"] and "0.5 × 0.5 × 2000" in extremes["description"]

        # restart: a new viewer server has a new key; the old tab ends and offers one action
        server.close()
        restarted = ViewerServer(store)
        ended = step("ended", outage=True, wait={"status": "is the last one this page saw"})
        assert "coalesce viewer: http://127.0.0.1:<port>/#token=<key>" in ended["status"]
        copied = step("ended-copied", outage=True, do=[{"click": "Copy request for your agent", "until": "Copied. Paste it to your agent"}])
        assert copied["clipboard"].startswith("The coalesce viewer I had open has ended.")
        assert '{"select": {"kind": "server"}}' in copied["clipboard"]
        # a browser that refuses the clipboard gets the request as words instead
        refused = step("ended-copy-refused", outage=True, do=[
            {"rejectClipboard": True},
            {"click": "Copied. Paste it to your agent",
             "until": "Could not copy. Ask your agent to call query with select server"},
        ])
        assert "Could not copy. Ask your agent to call query with select server" in refused["status"]
        fresh = step("new-link", outage=True, do=[{"goto": restarted.url, "revision": revision}])
        assert fresh["link"] == "Live" and fresh["status"] is None
        driver.stdin.close()
        assert driver.wait(timeout=30) == 0
    finally:
        driver.kill()
        server.close()
        if restarted:
            restarted.close()


def screen_box(state):
    """The model's box on the page, in css px: its projected corners placed on the canvas."""
    view = state["view"]
    xs = [view["left"] + (x + 1) / 2 * view["width"] for x, _ in state["scene"]["projected"]]
    ys = [view["top"] + (1 - y) / 2 * view["height"] for _, y in state["scene"]["projected"]]
    return [min(xs), min(ys), max(xs), max(ys)]


@pytest.mark.parametrize("viewport", [(1280, 800), (390, 844)])
def test_status_never_moves_or_resizes_the_model(module, viewport):
    """Good, failed, reconnecting and ended: the canvas and the model stay where the human left them."""
    store = ViewerSceneStore()
    server = ViewerServer(store)
    restarted = None
    driver, step = open_driver(module, server.url, viewport)
    try:
        step("empty", wait={"ready": True})
        store.publish(1, [ScenePart("plate", Box(60, 40, 10, align=(Align.CENTER, Align.CENTER, Align.MIN)))])
        good = step("status-good", wait={"revision": 1})
        store.publish_failure(["edge_fillet"], oversized_fillet_failure())
        failed = step("status-failed", wait={"failure": "FILLET_FAILED"})
        assert "max_fillet" not in failed["status"] and "reduce the fillet radius" in failed["status"]
        server.close()
        # wait for the status itself: the link label says "Reconnecting" a moment before the status merges
        lost = step("status-reconnecting", outage=True, wait={"status": "may be out of date"}, do=[{"settle": True}])
        # one calm status, not two stacked messages
        assert lost["repair"] is None
        assert lost["status"] == (
            "Revision 1 may be out of date. Reconnecting to coalesce. The last edit, edge_fillet, failed."
        )
        restarted = ViewerServer(store, port=server.port)  # a new key on the same port: the page ends
        ended = step("status-ended", outage=True, wait={"status": "is the last one this page saw"}, do=[{"settle": True}])
        assert "The last edit, edge_fillet, failed." in ended["status"] and ended["repair"] is None
        for state in (failed, lost, ended):
            assert state["view"] == good["view"], state["status"]
            assert screen_box(state) == screen_box(good), state["status"]
        driver.stdin.close()
        assert driver.wait(timeout=30) == 0
    finally:
        driver.kill()
        server.close()
        if restarted:
            restarted.close()


@pytest.mark.parametrize("viewport", [(1280, 800), (390, 844)])
def test_unrelated_part_in_front_is_not_pushed_behind(module, viewport):
    """Depth priority is only for parts with the same box (a fork); a later part 0.3 mm above an
    earlier plate still covers it, however many parts come between them in the list."""
    store = ViewerSceneStore()
    server = ViewerServer(store)
    driver, step = open_driver(module, server.url, viewport)
    plate = Box(60, 40, 3, align=(Align.CENTER, Align.CENTER, Align.MIN))
    # small parts inside the plate: hidden from view, they only move the cover down the list
    inside = [ScenePart(f"inner_{x}_{y}", Pos(x * 5, y * 5, 1) * Box(1, 1, 1)) for x in range(-5, 6) for y in range(-3, 4)]
    cover = Pos(0, 0, 3.3) * Box(40, 20, 1, align=(Align.CENTER, Align.CENTER, Align.MIN))
    try:
        step("empty", wait={"ready": True})
        store.publish(1, [ScenePart("plate", plate, color="#c03030"), *inside,
                          ScenePart("cover", cover, color="#3050d0")])
        for view in ("Iso", "Top"):
            seen = step(f"cover-{view.lower()}", wait={"revision": 1}, do=[{"click": view}],
                        flat={"fx": [0.45, 0.55], "fy": [0.45, 0.55]})
            red, _, blue = seen["flat"]["median"]
            assert blue > red, (view, seen["flat"])  # the cover's blue, not the plate's red
        driver.stdin.close()
        assert driver.wait(timeout=30) == 0
    finally:
        driver.kill()
        server.close()


def circle_centers(shape, radius):
    """Exact centers of the cylinder faces of one radius, from the kernel shape."""
    faces = [face for face in shape.faces() if face.geom_type == GeomType.CYLINDER and abs(face.radius - radius) < 1e-6]
    assert faces, radius
    axes = [BRepAdaptor_Surface(face.wrapped).Cylinder().Location() for face in faces]
    return {(round(axis.X(), 6), round(axis.Y(), 6)) for axis in axes}


@pytest.mark.parametrize("viewport", [(1280, 800), (390, 844)])
def test_enclosure_rounds_and_part_rows_in_a_real_browser(module, viewport, tmp_path):
    """An MCP-built enclosure: round features draw round, and the part rows line up."""
    narrow = viewport[0] < 720
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / "box.coalesce.json", scene_store=store)
    server = ViewerServer(store)
    driver, step = open_driver(module, server.url, viewport)
    try:
        step("empty", wait={"ready": True})
        for tool, arguments in [
            ("import", enclosure_import(ENCLOSURE_FIXTURES / "features.kicad_pcb")),
            ("feature", base_request()),
            ("feature", standoffs_request()),
        ]:
            result = call(service, tool, arguments)
            assert result.get("ok"), result
        shown = step("enclosure", wait={"revision": 3})
        assert [name.strip() for name in shown["names"]] == ["pcb reference", "base"]

        board, base = store.scene(3)["parts"]
        # 5 mm bosses, their 1.6 mm pilots, and the board's 1.5 mm drill: each drawn circle has
        # at least 48 corners at every height, so it reads round at normal zoom
        rounds = [(base, service._regen.shapes["mounts"], radius) for radius in (2.5, 0.8)]
        rounds.append((board, service._regen.shapes["controller_board"], 0.75))
        for part, shape, radius in rounds:
            for center in circle_centers(shape, radius):
                [counted] = step(f"circle-{radius}", do=[{"circle": {"mesh": part["mesh"], "center": center, "radius": radius}}])["actions"]
                assert counted["corners"] and min(counted["corners"]) >= 48, (radius, center, counted)

        for row in shown["rows"]:
            toggle, part, swatch, name = row["toggle"], row["part"], row["swatch"], row["name"]
            # the show/hide target fills its row: same top and bottom as the part button, 24 px+
            assert row["row"]["top"] <= toggle["top"] and toggle["bottom"] <= row["row"]["bottom"], row
            assert (toggle["top"], toggle["bottom"]) == (part["top"], part["bottom"]), row
            assert toggle["width"] >= 24 and toggle["height"] >= 24, row
            # the swatch sits on the name line, centered within a pixel
            middle = (swatch["top"] + swatch["bottom"]) / 2
            assert abs(middle - (name["top"] + name["bottom"]) / 2) <= 1, row
            if narrow:
                # the details hint sits just after the name, on its line, in the muted ink (not --faint)
                assert row["hint"]["left"] >= name["right"] and row["hint"]["left"] - name["right"] <= 8, row
                assert name["top"] <= (row["hint"]["top"] + row["hint"]["bottom"]) / 2 <= name["bottom"], row
                assert row["hintColor"] == "rgb(93, 104, 115)", row
            else:
                assert row["hint"] is None  # wide rows always show their details and facts
        # narrow rows say whether their facts are open; wide rows have nothing to open
        closed, first, second = (["false", "false"], ["true", "false"], ["false", "true"]) if narrow else ([None] * 2,) * 3
        assert shown["expanded"] == closed
        opened = step("row-open", do=[{"tapPart": 0}])
        assert opened["expanded"] == first
        # the facts stay open when focus moves on, so nothing below them jumps; the next part's replace them
        kept = step("row-kept", do=[{"blur": True}])
        assert kept["expanded"] == first
        moved = step("row-moved", do=[{"tapPart": 1}])
        assert moved["expanded"] == second

        # keyboard: the swatch control shows a visible focus ring
        keys = step("swatch-focus", do=[{"tabTo": "pcb reference", "press": "Shift+Tab"}])
        assert keys["focus"] == {"className": "visibility", "outline": "solid 2px"}, keys["focus"]
        driver.stdin.close()
        assert driver.wait(timeout=30) == 0
    finally:
        driver.kill()
        server.close()


def far(a, b):
    """Two colours (#rrggbb or [r, g, b]) a reader tells apart at a glance: 120+ summed channel difference."""
    rgb = [[int(c[i:i + 2], 16) for i in (1, 3, 5)] if isinstance(c, str) else c for c in (a, b)]
    return sum(abs(x - y) for x, y in zip(*rgb, strict=True)) >= 120


@pytest.mark.parametrize("viewport", [(1280, 800), (390, 844)])
def test_board_reference_reads_apart_from_the_enclosure_with_the_lid_hidden(module, viewport, tmp_path):
    """The imported board is the reference: its own colour and a row label, so it shows inside the box."""
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / "box.coalesce.json", scene_store=store)
    server = ViewerServer(store)
    driver, step = open_driver(module, server.url, viewport)
    try:
        step("empty", wait={"ready": True})
        for tool, arguments in [
            ("import", enclosure_import(ENCLOSURE_FIXTURES / "features.kicad_pcb")),
            ("feature", base_request()),
            ("feature", standoffs_request()),
            ("feature", lid_request(revision=3)),
        ]:
            result = call(service, tool, arguments)
            assert result.get("ok"), result
        shown = step("enclosure-lid", wait={"revision": 4})
        parts = store.scene(4)["parts"]
        lid = [p["label"] for p in parts].index("lid")
        step("lid-hidden-iso", do=[{"toggle": lid}, {"click": "Iso"}])
        top = step("lid-hidden-top", do=[{"click": "Top"}], flat={"fx": [0.45, 0.55], "fy": [0.45, 0.55]})
        rim = step("lid-hidden-rim", flat={"fx": [0.005, 0.04], "fy": [0.45, 0.55]})

        assert [p["role"] for p in parts] == ["reference", "manufacturing", "manufacturing"]
        colors = [p["color"] for p in parts]
        # board, lid and base: no two the same colour, and the board far from both manufactured parts
        assert len(set(colors)) == 3, colors
        assert all(far(colors[0], other) for other in colors[1:]), colors
        for part, row in zip(parts, shown["rows"], strict=True):
            red, green, blue = (int(part["color"][i:i + 2], 16) for i in (1, 3, 5))
            assert row["swatchColor"] == f"rgb({red}, {green}, {blue})", row
            assert row["role"] == ("Reference, not exported" if part["role"] == "reference" else None), row
        assert shown["rows"][0]["roleColor"] == "rgb(93, 104, 115)"  # --mute, 5.3:1 on the panel (WCAG AA)
        # seen from above with the lid hidden, the board inside reads apart from the base's rim
        assert far(top["flat"]["median"], rim["flat"]["median"]), (top["flat"], rim["flat"])
        driver.stdin.close()
        assert driver.wait(timeout=30) == 0
    finally:
        driver.kill()
        server.close()


def test_part_colours_stay_with_their_body_across_downstream_features_and_restart(tmp_path):
    """Adding a hole to the first body moves it last in regen order; neither part may change colour."""
    store = ViewerSceneStore()
    path = tmp_path / "two.coalesce.json"
    service = DocumentService(path, scene_store=store)
    for tool, arguments in [
        ("sketch", rectangle_sketch(request_id="sa", revision=0, sketch_id="A", entity_id="a",
                                    width=20, height=20, center=(-20, 0))),
        ("feature", extrude(request_id="ea", revision=1, feature_id="ea", sketch_id="A",
                            profile_ids=["a"], distance=5)),
        ("sketch", rectangle_sketch(request_id="sb", revision=2, sketch_id="B", entity_id="b",
                                    width=20, height=20, center=(20, 0))),
        ("feature", extrude(request_id="eb", revision=3, feature_id="eb", sketch_id="B",
                            profile_ids=["b"], distance=5)),
    ]:
        assert call(service, tool, arguments).get("ok"), arguments
    before = {p["partId"]: p["color"] for p in wait_for(store, 4)["parts"]}
    assert len(set(before.values())) == 2, before

    hole_a = patterned_hole(request_id="ha", revision=4, feature_id="ha", target="ea",
                            positions=[{"name": "h", "point": [-20, 0]}])
    assert call(service, "feature", hole_a).get("ok")
    assert {p["partId"]: p["color"] for p in wait_for(store, 5)["parts"]} == before

    reopened = ViewerSceneStore()
    DocumentService(path, scene_store=reopened)
    assert {p["partId"]: p["color"] for p in wait_for(reopened, 5)["parts"]} == before


def test_splitting_one_body_keeps_other_body_colours_and_shades_the_pieces(tmp_path):
    """A cut through the first body makes two pieces; the second body must keep its colour."""
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / "split.coalesce.json", scene_store=store)
    for tool, arguments in [
        ("sketch", rectangle_sketch(request_id="sa", revision=0, sketch_id="A", entity_id="a",
                                    width=20, height=20, center=(-20, 0))),
        ("feature", extrude(request_id="ea", revision=1, feature_id="ea", sketch_id="A",
                            profile_ids=["a"], distance=5)),
        ("sketch", rectangle_sketch(request_id="sb", revision=2, sketch_id="B", entity_id="b",
                                    width=20, height=20, center=(20, 0))),
        ("feature", extrude(request_id="eb", revision=3, feature_id="eb", sketch_id="B",
                            profile_ids=["b"], distance=5)),
    ]:
        assert call(service, tool, arguments).get("ok"), arguments
    before = {p["partId"]: p["color"] for p in wait_for(store, 4)["parts"]}

    for tool, arguments in [
        ("sketch", rectangle_sketch(request_id="ss", revision=4, sketch_id="S", entity_id="s",
                                    width=2, height=30, center=(-20, 0))),
        ("feature", cut(request_id="cs", revision=5, feature_id="cs", target="ea", tool="S")),
    ]:
        assert call(service, tool, arguments).get("ok"), arguments
    after = {p["partId"]: p["color"] for p in wait_for(store, 6)["parts"]}
    pieces = [color for part, color in after.items() if part.startswith("ea/a#")]

    assert after["eb/b"] == before["eb/b"]
    assert len(pieces) == 2, after
    base = _oklab(before["ea/a"])
    shades = [_oklab(color) for color in pieces]
    # distinct from each other, but the same hue and chroma as the body: only lightness moves
    assert math.dist(*shades) >= 0.05, pieces
    for shade in shades:
        assert math.dist(shade[1:], base[1:]) < 0.015, (pieces, before["ea/a"])


def test_change_line_says_what_each_enclosure_feature_did(tmp_path):
    """One concise line per enclosure step, from data the commit already has; ids only when they add words."""
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / "box.coalesce.json", scene_store=store)
    top = {**lid_request(revision=5, request_id="top"), "featureId": "top_cover"}
    steps = [
        ("import", enclosure_import(ENCLOSURE_FIXTURES / "features.kicad_pcb"),
         "imported board features.kicad_pcb"),
        ("feature", base_request(wallHeightAboveBoard={"value": 8, "unit": "mm"}),
         "added pcb base for controller_board: 2 mm walls, 1 mm board clearance"),
        ("feature", standoffs_request(), "added standoffs mounts at 2 mounting holes"),
        ("feature", lid_request(revision=3), "added pcb lid: 2 mm thick, 0.25 mm fit gap, 3 mm deep lip"),
        ("feature", port_request(revision=4), "added port cutout for J1: 5 \u00d7 3 mm on left wall"),
        ("feature", top, "added pcb lid top_cover: 2 mm thick, 0.25 mm fit gap, 3 mm deep lip"),
        ("feature", delete_feature("port", 6, "delete-port"), "deleted port cutout"),
        ("feature", delete_feature("base", 7, "delete-base", cascade=True),
         "deleted pcb base with dependents mounts, lid, top_cover"),
    ]
    for revision, (tool, arguments, summary) in enumerate(steps, start=1):
        result = call(service, tool, arguments)
        assert result.get("revision") == revision, result
        assert wait_for(store, revision)["change"]["summary"] == summary


@pytest.mark.parametrize("viewport", [(1280, 800), (390, 844)])
def test_split_pieces_get_distinct_titles_and_the_revision_update_is_not_boxed(module, viewport, tmp_path):
    """Two pieces of one body share label and last feature: each row still names its own piece."""
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / "split.coalesce.json", scene_store=store)
    server = ViewerServer(store)
    driver, step = open_driver(module, server.url, viewport)
    try:
        step("empty", wait={"ready": True})
        for tool, arguments in [
            ("sketch", rectangle_sketch(request_id="sa", revision=0, sketch_id="A", entity_id="a",
                                        width=20, height=20, center=(-20, 0))),
            ("feature", extrude(request_id="ea", revision=1, feature_id="base", sketch_id="A",
                                profile_ids=["a"], distance=5)),
            ("sketch", rectangle_sketch(request_id="sb", revision=2, sketch_id="B", entity_id="b",
                                        width=20, height=20, center=(20, 0))),
            ("feature", extrude(request_id="eb", revision=3, feature_id="later", sketch_id="B",
                                profile_ids=["b"], distance=5)),
            ("sketch", rectangle_sketch(request_id="ss", revision=4, sketch_id="S", entity_id="s",
                                        width=2, height=30, center=(-20, 0))),
            ("feature", cut(request_id="cs", revision=5, feature_id="split", target="base", tool="S")),
        ]:
            assert call(service, tool, arguments).get("ok"), arguments
        shown = step("split", wait={"revision": 6}, do=[{"fresh": True}])
        parts = store.scene(6)["parts"]

        plain, fresh = shown["actions"][0]["plain"], shown["actions"][0]["fresh"]
        # the update signal changes only the text colour: no box, no padding, no layout shift
        for key in ("background", "padding", "margin", "box"):
            assert fresh[key] == plain[key], (key, plain, fresh)
        assert plain["background"] == "rgba(0, 0, 0, 0)" and plain["padding"] == "0px", plain
        assert fresh["color"] != plain["color"], (plain, fresh)
        # pieces numbered in sorted part id order, the same order that picks their shades
        pieces = sorted(p["partId"] for p in parts if p["label"] == "base")
        expected = {pid: f"base \u00b7 split, piece {pieces.index(pid) + 1}" for pid in pieces}
        assert shown["names"] == [expected.get(p["partId"], p["label"]) for p in parts], shown["names"]
        assert "later" in shown["names"]  # a title that does not collide is unchanged
        driver.stdin.close()
        assert driver.wait(timeout=30) == 0
    finally:
        driver.kill()
        server.close()
