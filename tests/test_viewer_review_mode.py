"""Enclosure review modes in a real browser: lid off, exploded and one section plane.

These modes only move, hide or cut what the page draws; the scene and the document never change.
The browser tests skip when Node or a launchable Playwright is not on this machine (they run in CI).
Set VIEWER_SCREENSHOTS to a directory to keep a screenshot of every step.
"""

import copy
import math

import pytest
from build123d import Align, Axis, Box, Cylinder, Pos, fillet
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

        # what the cut draws is checked point by point in test_section_draws_the_cut_material_on_the_plane
        section = step("review-section", do=[{"click": "Section"}, {"settle": True}, {"review": True},
                                              {"capture": "section"}])
        state = review(section, 2)
        assert pressed(state) == ["Section"]
        drawn = section["scene"]
        assert drawn["positions"] == [[0, 0, 0]] * 3 and drawn["visible"] == [True] * 3
        assert drawn["cut"] == [True] * 3  # the board too: an uncut board would hide the standoffs
        assert drawn["projected"] == assembled["scene"]["projected"]  # the same parts, the same fit
        assert state["section"]["axis"] == "Y"
        assert state["section"]["value"] == pytest.approx(
            (state["section"]["min"] + state["section"]["max"]) / 2, abs=0.05)
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
        # the latest commit, in words, in the sidebar and in the canvas description
        line = "Added port cutout for J1: 5 \u00d7 3 mm on left wall"
        assert keys["change"] == line
        assert f"Latest change: a{line[1:]}." in keys["description"]

        # Escape in the section controls leaves the section view, says so, and keeps focus on the modes
        for control in ("#section-range", "#section-value"):
            name = control.removeprefix("#")
            focused = step(f"{name}-focused", do=[{"click": "Section"}, {"settle": True}, {"focus": control},
                                                  {"review": True}])
            assert review(focused, 3)["hint"] == "Esc returns to Assembled"
            # a screen reader hears it with the control, not only from the visible hint
            assert review(focused, 3)["described"] == "Escape returns to the Assembled view."
            escaped = step(f"{name}-escape", do=[{"key": "Escape"}, {"settle": True}, {"review": True},
                                                 {"diff": "assembled"}])
            state = review(escaped, 2)
            assert pressed(state) == ["Assembled"] and state["section"] is None and state["hint"] is None
            assert escaped["scene"]["cut"] == [False] * 3
            assert review(escaped, 3) == 0
            assert state["announced"] == "Section view ended; Assembled view restored."
            assert escaped["focused"] == "Assembled"

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
        # a new revision while cut, then the assembled view: the old cut faces were freed with their part
        store.publish(2, [ScenePart("plate", plate(6))])
        step("plate-section-2", wait={"revision": 2}, do=[{"settle": True}])
        back = step("plate-assembled", do=[{"click": "Assembled"}, {"settle": True}, {"review": True}])
        assert review(back, 2)["memory"] == state["memory"]
        driver.stdin.close()
        assert driver.wait(timeout=30) == 0
    finally:
        driver.kill()
        server.close()


BACKGROUND = "#e4e8eb"


def srgb(hex_color):
    return [int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]


def cut_color(hex_color):
    """The viewer's cut face: the part color mixed 45% toward white in linear light, shown in sRGB."""
    def linear(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    def encoded(c):
        return round(255 * (12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055))

    return [encoded(linear(c) + (1 - linear(c)) * 0.45) for c in srgb(hex_color)]


def distance(a, b):
    return max(abs(x - y) for x, y in zip(a, b, strict=True))


def section_front(step, name, position):
    """Section along Y at ``position``, seen from Front: straight at the cut, the kept half behind it."""
    shown = step(name, do=[{"click": "Section"}, {"fill": {"label": "Section position in mm", "value": position}},
                           {"click": "Front"}, {"settle": True}, {"review": True}])
    state = review(shown, 4)["section"]
    assert (state["axis"], state["value"]) == ("Y", position)
    return shown


# the Y = 3 plane through the standoffs, in the enclosure from base_request and standoffs_request:
# board 20 x 10 x 1.2 mm at z 0..1.2 (holes at x = 2 and 18), 1 mm clearance, 2 mm walls at x -3..-1
# and 21..23, floor z -3..-1, standoff bosses 5 mm across around 1.6 mm pilots from the floor to the board
CUT_Y = STANDOFF_Y
BASE_POINTS = {"wall": (-1.4, 3.5), "far wall": (22, 3.5), "floor": (10, -2), "standoff": (3.65, -0.5)}
BOARD_POINTS = {"board": (10, 0.9)}
# open on the plane: the ray goes on through the cavity to the far inner wall's shaded face
CAVITY_POINTS = {"cavity": (10, 3.5), "beside wall": (-0.6, 3.5), "above board": (10, 1.6), "pilot": (2, -0.5)}
OUTSIDE_POINTS = {"left of the box": (-6, 2), "above the box": (10, 8)}
# rows across the cut faces, finer than a pixel: edges behind the plane (the seams of the board's NPTH
# hole, the floor's inner corners at x = -1 and 21) must not draw over the cut face
BOARD_ROW = [(3.5 + 0.02 * i, 0.9) for i in range(626)]  # x 3.5..16, through x = 10.75
FLOOR_ROW = [(-2.8 + 0.02 * i, -2) for i in range(1281)]  # x -2.8..22.8


def on_plane(xz):
    return (xz[0], CUT_Y, xz[1])


@pytest.mark.parametrize("viewport", [(1280, 800), (390, 844)])
def test_section_draws_the_cut_material_on_the_plane(module, viewport, tmp_path):  # noqa: F811
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / "box.coalesce.json", scene_store=store)
    server = ViewerServer(store)
    driver, step = open_driver(module, server.url, viewport)
    try:
        step("empty", wait={"ready": True})
        for tool, arguments in [
            ("import", import_request(FIXTURES / "features.kicad_pcb")),
            ("feature", base_request()),
            ("feature", standoffs_request()),
        ]:
            result = call(service, tool, arguments)
            assert result.get("ok"), result
        scene = wait_for(store, result["revision"])
        colors = {part["label"]: part["color"] for part in scene["parts"]}
        base, board = service._regen.shapes["mounts"], service._regen.shapes["controller_board"]
        # the points are chosen from the geometry above; the kernel confirms each one
        assert all(base.is_inside(on_plane(p)) for p in BASE_POINTS.values())
        assert all(board.is_inside(on_plane(p)) for p in BOARD_POINTS.values())
        for p in [*CAVITY_POINTS.values(), *OUTSIDE_POINTS.values(), *BOARD_POINTS.values()]:
            assert not base.is_inside(on_plane(p)), p
        for p in [*CAVITY_POINTS.values(), *OUTSIDE_POINTS.values(), *BASE_POINTS.values()]:
            assert not board.is_inside(on_plane(p)), p
        assert all(board.is_inside(on_plane(p)) for p in BOARD_ROW)
        assert all(base.is_inside(on_plane(p)) for p in FLOOR_ROW)

        step("review", wait={"revision": result["revision"]}, do=[{"settle": True}])
        named = {**BASE_POINTS, **BOARD_POINTS, **CAVITY_POINTS, **OUTSIDE_POINTS}
        shown = section_front(step, "section-front-r3", CUT_Y)
        measured = step("section-pixels", do=[{"pixels": [on_plane(p) for p in named.values()]}])["actions"][0]
        rgb = {name: point["rgb"] for name, point in zip(named, measured, strict=True)}
        print(viewport, {name: (rgb[name], point["pixel"]) for name, point in zip(named, measured, strict=True)})
        base_cut, board_cut = cut_color(colors["base"]), cut_color(colors["pcb reference"])
        for name in BASE_POINTS:
            assert distance(rgb[name], base_cut) <= 3, (name, rgb[name], base_cut)
        for name in BOARD_POINTS:
            assert distance(rgb[name], board_cut) <= 3, (name, rgb[name], board_cut)
        for name in CAVITY_POINTS:
            # the far wall's lit face, neither a cut face nor the background
            assert distance(rgb[name], base_cut) > 12 and distance(rgb[name], board_cut) > 12, (name, rgb[name])
            assert distance(rgb[name], srgb(BACKGROUND)) > 12, (name, rgb[name])
        for name in OUTSIDE_POINTS:
            assert distance(rgb[name], srgb(BACKGROUND)) <= 3, (name, rgb[name])
        off = {}
        for name, row, color in [("board", BOARD_ROW, board_cut), ("floor", FLOOR_ROW, base_cut)]:
            sampled = step("section-rows", do=[{"pixels": [on_plane(p) for p in row]}])["actions"][0]
            off[name] = [(round(p[0], 2), s["pixel"], s["rgb"]) for p, s in zip(row, sampled, strict=True)
                         if distance(s["rgb"], color) > 3]
        assert off == {"board": [], "floor": []}, off
        assert shown["scene"]["cut"] == [True, True]
        step("section-iso-r3", do=[{"click": "Iso"}, {"settle": True}])
        driver.stdin.close()
        assert driver.wait(timeout=30) == 0
    finally:
        driver.kill()
        server.close()


# each section axis, and the camera that looks straight at its cut face from the removed side
AXIS_VIEWS = {"X": "Right", "Y": "Front", "Z": "Top"}


def plane_grid(axis, value, bbox):
    """Points 1 mm apart on the section plane, across the model's box."""
    i = "XYZ".index(axis)
    u, v = (j for j in range(3) if j != i)
    for a in range(math.floor(bbox["min"][u]), math.ceil(bbox["max"][u]) + 1):
        for b in range(math.floor(bbox["min"][v]), math.ceil(bbox["max"][v]) + 1):
            point = [0.0, 0.0, 0.0]
            point[i], point[u], point[v] = value, a, b
            yield tuple(point), u, v


def deep_inside(shape, point, u, v, margin=0.3):
    """Inside the solid, and so are the points ``margin`` away in the plane: clear of outline edges."""
    def moved(axis, step):
        p = list(point)
        p[axis] += step
        return tuple(p)

    return all(shape.is_inside(p) for p in [point, *(moved(a, s) for a in (u, v) for s in (margin, -margin))])


@pytest.mark.parametrize("viewport", [(1280, 800)])
def test_section_at_each_end_of_the_slider_draws_a_solid_cut_face(module, viewport, tmp_path):  # noqa: F811
    """At the slider ends the plane stays inside the material: one flat cut colour, no stipple of a coplanar face."""
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / "box.coalesce.json", scene_store=store)
    server = ViewerServer(store)
    driver, step = open_driver(module, server.url, viewport)
    try:
        step("empty", wait={"ready": True})
        for tool, arguments in [
            ("import", import_request(FIXTURES / "features.kicad_pcb")),
            ("feature", base_request()),
            ("feature", standoffs_request()),
        ]:
            result = call(service, tool, arguments)
            assert result.get("ok"), result
        scene = wait_for(store, result["revision"])
        base_cut = cut_color(next(part["color"] for part in scene["parts"] if part["label"] == "base"))
        base, board = service._regen.shapes["mounts"], service._regen.shapes["controller_board"]
        step("review", wait={"revision": result["revision"]}, do=[{"click": "Section"}, {"settle": True}])
        off, inset = {}, {}
        for axis, view in AXIS_VIEWS.items():
            for end, typed in (("max", 999), ("min", -999)):
                shown = step(f"section-{axis}-{end}", do=[
                    {"click": axis}, {"fill": {"label": "Section position in mm", "value": typed}},
                    {"click": view}, {"settle": True}, {"review": True}])
                state = review(shown, 4)["section"]
                value = state[end]
                assert state["value"] == value
                extent = scene["bbox"][end]["XYZ".index(axis)]
                inset[f"{axis} {end}"] = round(abs(value - extent), 2)
                points = [p for p, u, v in plane_grid(axis, value, scene["bbox"])
                          if deep_inside(base, p, u, v) and not board.is_inside(p)]
                assert len(points) > 20, (axis, end, len(points))
                sampled = step(f"section-{axis}-{end}-pixels", do=[{"pixels": points}])["actions"][0]
                off[f"{axis} {end}"] = [(p, s["rgb"]) for p, s in zip(points, sampled, strict=True)
                                        if distance(s["rgb"], base_cut) > 3]
        assert all(not bad for bad in off.values()), {key: (len(bad), bad[:5]) for key, bad in off.items() if bad}
        # one 0.1 mm step past the 2 mm walls and floor; the open top keeps one step inside the wall tops
        assert inset == {"X max": 2.1, "X min": 2.1, "Y max": 2.1, "Y min": 2.1, "Z max": 0.1, "Z min": 2.1}, inset
        driver.stdin.close()
        assert driver.wait(timeout=30) == 0
    finally:
        driver.kill()
        server.close()


@pytest.mark.parametrize("viewport", [(1280, 800)])
def test_section_at_each_end_of_the_slider_still_cuts_through_the_cavity(module, viewport, tmp_path):  # noqa: F811
    """Every slider position is useful: at each end the plane is past the outer wall, so it opens the cavity."""
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / "box.coalesce.json", scene_store=store)
    server = ViewerServer(store)
    driver, step = open_driver(module, server.url, viewport)
    try:
        step("empty", wait={"ready": True})
        revision = enclosure(service)
        scene = wait_for(store, revision)
        cuts = [cut_color(part["color"]) for part in scene["parts"]]
        bbox = scene["bbox"]
        step("review", wait={"revision": revision}, do=[{"click": "Section"}, {"settle": True}])
        found = {}
        for axis, view in AXIS_VIEWS.items():
            for end, typed in (("mid", None), ("max", 999), ("min", -999)):
                fill = [] if typed is None else [{"fill": {"label": "Section position in mm", "value": typed}}]
                shown = step(f"section-{axis}-{end}", do=[{"click": axis}, *fill, {"click": view}, {"settle": True},
                                                         {"review": True}, {"caps": cuts}])
                value = review(shown, len(fill) + 3)["section"]["value"]
                caps = review(shown, len(fill) + 4)
                # the middle of the model across the plane: open cavity in this enclosure (the kernel agrees)
                middle = [(bbox["min"][i] + bbox["max"][i]) / 2 for i in range(3)]
                middle["XYZ".index(axis)] = value
                solid = any(shape.is_inside(tuple(middle)) for shape in service._regen.shapes.values())
                pixel = step(f"section-{axis}-{end}-middle", do=[{"pixels": [middle]}])["actions"][0][0]["rgb"]
                found[f"{axis} {end}"] = {"value": value, "area": sum(caps["areas"]), "regions": sum(caps["regions"]),
                                          "middle solid": solid, "middle cut": min(distance(pixel, cut) for cut in cuts) <= 3}
        print(viewport, found)
        for key in (f"{axis} {end}" for axis in AXIS_VIEWS for end in ("min", "max")):
            shown, mid = found[key], found[f"{key[0]} mid"]["area"]
            # the end cut loses only the board and standoffs the middle cut crosses (0.88 of it or more here);
            # a sliver at a rounded edge or a wall's rim would be far less than half
            assert shown["area"] >= 0.5 * mid, (key, shown, mid)
            assert shown["regions"] > 1, (key, shown)
            # a slab of the outer wall passes both checks above (it is larger than the middle cut); this one
            # rejects it: the middle of the end cut is open cavity, not cut material
            assert not shown["middle solid"] and not shown["middle cut"], (key, shown)
        driver.stdin.close()
        assert driver.wait(timeout=30) == 0
    finally:
        driver.kill()
        server.close()


@pytest.mark.parametrize("viewport", [(1280, 800)])
def test_section_of_a_model_with_no_enclosure_spans_its_full_extent(module, viewport):  # noqa: F811
    """No pcb base: each end keeps one 0.1 mm step inside the outer face, a through-hole sets no wall."""
    store = ViewerSceneStore()
    server = ViewerServer(store)
    driver, step = open_driver(module, server.url, viewport)
    models = {
        "holed plate": plate(6) - Pos(0, 0, 3) * Cylinder(5, 6),
        "plain plate": plate(6),
        "filleted plate": fillet(plate(6).edges().filter_by(Axis.Z), radius=5),
    }
    try:
        step("empty", wait={"ready": True})
        found = {}
        for revision, (name, shape) in enumerate(models.items(), start=1):
            store.publish(revision, [ScenePart("plate", shape)])
            shot = name.replace(" ", "-")  # a step name is one word
            step(shot, wait={"revision": revision}, do=[{"click": "Section"}, {"settle": True}])
            for axis in "XYZ":
                state = review(step(f"{shot}-{axis}", do=[{"click": axis}, {"review": True}]), 1)["section"]
                found[f"{name} {axis}"] = (state["min"], state["max"])
            step(f"{shot}-off", do=[{"click": "Assembled"}])
        full = {"X": (-29.9, 29.9), "Y": (-19.9, 19.9), "Z": (0.1, 5.9)}
        assert found == {f"{name} {axis}": full[axis] for name in models for axis in "XYZ"}, found
        driver.stdin.close()
        assert driver.wait(timeout=30) == 0
    finally:
        driver.kill()
        server.close()


@pytest.mark.parametrize("viewport", [(1280, 800), (390, 844)])
def test_section_of_overlapping_parts_shows_the_nearer_surface(module, viewport):  # noqa: F811
    """Two overlapping closed solids: a 20 mm cube A at the origin and cube B moved 10 mm along X and Y."""
    store = ViewerSceneStore()
    server = ViewerServer(store)
    driver, step = open_driver(module, server.url, viewport)
    try:
        step("empty", wait={"ready": True})
        cube = Box(20, 20, 20, align=(Align.MIN, Align.MIN, Align.MIN))
        store.publish(1, [ScenePart("a", cube), ScenePart("b", Pos(10, 10, 0) * cube)])
        colors = [part["color"] for part in wait_for(store, 1)["parts"]]
        step("overlap", wait={"revision": 1}, do=[{"settle": True}])
        shown = section_front(step, "overlap-section-front-r3", 5)
        # solids with no wall to cross: the range keeps one step inside the outer faces, full cross-sections
        assert (review(shown, 4)["section"]["min"], review(shown, 4)["section"]["max"]) == (0.1, 29.9)
        # on the Y = 5 plane: A's material at x 0..20, none of B's (B starts at y = 10)
        points = {"A only": (5, 5, 10), "A, B behind it": (15, 5, 10), "B behind the plane": (25, 5, 10)}
        measured = step("overlap-pixels", do=[{"pixels": list(points.values())}])["actions"][0]
        rgb = dict(zip(points, (point["rgb"] for point in measured), strict=True))
        print(viewport, rgb)
        assert distance(rgb["A only"], cut_color(colors[0])) <= 3
        assert distance(rgb["B behind the plane"], cut_color(colors[1])) > 12  # B's lit front face
        # B's front face at y = 10 is behind the plane: A's cut face covers it
        assert distance(rgb["A, B behind it"], cut_color(colors[0])) <= 3, rgb
        driver.stdin.close()
        assert driver.wait(timeout=30) == 0
    finally:
        driver.kill()
        server.close()
