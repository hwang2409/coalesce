"""The viewer's fit check and part facts: measured on the committed enclosure, never a PASS without a measurement."""

import time

import pytest
from build123d import Pos
from test_mcp_assembly import occurrence_create
from test_mcp_cut_hole_circle import extrude, rectangle_sketch
from test_mcp_vertical import FEATURE, SKETCH
from test_pcb_enclosure import FIXTURES, base_request, import_request, quantity, standoffs_request
from test_pcb_lid import lid_request
from test_port_cutout import port_request
from test_viewer_browser import plate
from test_viewer_review_flow import module, open_driver  # noqa: F401 - module is a fixture

from coalesce import enclosure_inspection
from coalesce.service import DocumentService
from coalesce.viewer import ScenePart, ViewerSceneStore, ViewerServer

H1 = "hole-9ddb205ba1bc297f95347452"  # the NPTH mounting hole with reference H1
UNNAMED = ["hole-03e3c0594f5f1b105237c4c0", "hole-89c529cbad8407f9ccb81676"]  # REF** holes


def wait_for(store, revision):
    deadline = time.monotonic() + 15
    while store.current_revision != revision and time.monotonic() < deadline:
        time.sleep(0.02)
    scene = store.scene(revision)
    assert scene is not None, f"revision {revision} was not published"
    return scene


def enclosure(tmp_path, store, *, holes=(*UNNAMED, H1), models="declared", lid=True, lip=True, port=True):
    """Board, base, standoffs at ``holes``, a lid with a 0.4 mm lip, and a J1 port; returns the last revision."""
    service = DocumentService(tmp_path / "fit.coalesce.json", scene_store=store)
    assert service.import_board(import_request(FIXTURES / "features.kicad_pcb"))["ok"]
    # shortcut: the fixture declares no 3D models; set what a board with models reports
    service._document.graph.nodes["controller_board"].availability["models"] = models
    calls = [base_request(), standoffs_request(selection={"mode": "explicit", "holeIds": list(holes)})]
    if lid:
        # a shallow lip stays above the port opening
        calls.append(lid_request(revision=3, lip={"depth": quantity(0.4), "width": quantity(1)}) if lip
                     else lid_request(revision=3, include_lip=False))
    if port:
        calls.append(port_request(revision=len(calls) + 1))
    for call in calls:
        result = service.feature(call)
        assert result["ok"], result
    return service, wait_for(store, service.revision)


def rows(scene):
    [summary] = scene["fit"]
    assert summary["baseId"] == "base"
    return {row["id"]: row for row in summary["rows"]}


def part_ids(scene):
    return {part["label"]: part["partId"] for part in scene["parts"]}


def test_fit_rows_for_a_measured_enclosure(tmp_path):
    _, scene = enclosure(tmp_path, ViewerSceneStore())
    found = rows(scene)
    ids = part_ids(scene)

    assert list(found) == [
        "wall_thickness", "floor_thickness", "board_clearance", "standoffs",
        "lid_outline:lid", "lid_interference:lid", "lid_fit_gap:lid", "port:port",
    ]
    summary = {key: (row["status"], row["value"], row.get("reason")) for key, row in found.items()}
    assert summary == {
        # no threshold is stored with the document: the value, never an invented PASS
        "wall_thickness": ("MEASURED", "2 mm", "no requirement set"),
        "floor_thickness": ("MEASURED", "2 mm", "no requirement set"),
        "board_clearance": ("MEASURED", "1 mm min, all sides", "no requirement set"),
        "standoffs": ("PASS", "3 of 3 mounting holes", None),
        "lid_outline:lid": (
            "FAIL",
            "the lid follows mounts, but port was added to the base after it",
            "update the lid's outlineSource to port",
        ),
        "lid_interference:lid": ("PASS", "none", None),
        "lid_fit_gap:lid": ("MEASURED", "0.25 mm", "no requirement set"),
        "port:port": ("PASS", "J1: 5 × 3 mm on left wall, 0.5 mm clearance", None),
    }
    assert found["port:port"]["label"] == "Port J1 clears connector"
    assert {key: row["partId"] for key, row in found.items()} == {
        "wall_thickness": ids["base"],
        "floor_thickness": ids["base"],
        "board_clearance": ids["pcb reference"],
        "standoffs": ids["base"],
        "lid_outline:lid": ids["lid"],
        "lid_interference:lid": ids["lid"],
        "lid_fit_gap:lid": ids["lid"],
        "port:port": ids["base"],
    }
    # every row carries the measured evidence behind it
    assert found["standoffs"]["evidence"]["coveredHoleIds"] == [*UNNAMED, H1]
    assert found["wall_thickness"]["evidence"] == {"value": 2.0, "unit": "mm"}
    assert found["port:port"]["evidence"][0]["blocked"] is False


def test_missing_standoff_fails_and_names_the_hole(tmp_path):
    _, scene = enclosure(tmp_path, ViewerSceneStore(), holes=UNNAMED)
    standoffs = rows(scene)["standoffs"]

    assert standoffs["status"] == "FAIL"
    assert standoffs["value"] == "2 of 3 mounting holes"
    assert standoffs["reason"] == "no standoff at H1"
    assert standoffs["evidence"]["uncoveredHoleIds"] == [H1]


def test_missing_standoff_without_a_reference_names_its_footprint_and_place(tmp_path):
    _, scene = enclosure(tmp_path, ViewerSceneStore(), holes=(H1,))
    # REF** holes: the footprint name and the position, never "unnamed"
    assert rows(scene)["standoffs"]["reason"] == (
        "no standoff at M2 mounting hole at (2, 3) mm, M3 mounting hole at (18, 3) mm")


def test_unknown_rows_say_why(tmp_path):
    # the fixture's connector height is unresolved, and there is no lid to measure
    _, scene = enclosure(tmp_path, ViewerSceneStore(), models="unresolved", lid=False)
    found = rows(scene)

    assert (found["lid"]["status"], found["lid"]["reason"]) == ("UNKNOWN", "no lid exists for this measurement")
    assert (found["port:port"]["status"], found["port:port"]["reason"]) == (
        "UNKNOWN", "connector height or envelope is unknown",
    )
    assert not any(row["status"] == "PASS" for row in found.values() if row["id"] in {"lid", "port:port"})


def test_failed_inspection_is_one_unknown_row_and_the_scene_still_publishes(tmp_path, monkeypatch):
    def broken(*_):
        raise ValueError("terminal base has no aperture for port")

    monkeypatch.setattr(enclosure_inspection, "inspect_enclosure", broken)
    _, scene = enclosure(tmp_path, ViewerSceneStore())

    assert len(scene["parts"]) == 3
    assert list(rows(scene).values()) == [
        {
            "id": "inspection",
            "label": "Enclosure inspection",
            "status": "UNKNOWN",
            "value": "not measured",
            "reason": "the enclosure could not be inspected: terminal base has no aperture for port",
            "partId": None,
            "evidence": None,
        }
    ]


def test_document_without_enclosure_has_no_fit_rows(tmp_path):
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / "plate.coalesce.json", scene_store=store)
    service.sketch(rectangle_sketch(request_id="s", revision=0, sketch_id="plate_sketch", entity_id="plate",
                                    width=60, height=40))
    service.feature(extrude(request_id="e", revision=1, feature_id="plate_extrude", sketch_id="plate_sketch",
                            profile_ids=["plate"], distance=3))
    assert wait_for(store, 2)["fit"] == []
    # a board alone is no enclosure yet
    board_store = ViewerSceneStore()
    board = DocumentService(tmp_path / "board.coalesce.json", scene_store=board_store)
    assert board.import_board(import_request(FIXTURES / "features.kicad_pcb"))["ok"]
    assert wait_for(board_store, 1)["fit"] == []


def test_part_facts_for_board_base_and_lid(tmp_path):
    _, scene = enclosure(tmp_path, ViewerSceneStore())
    facts = {part["label"]: [(fact["label"], fact["value"]) for fact in part["facts"]] for part in scene["parts"]}

    assert facts == {
        "pcb reference": [
            ("Mounting holes", "3: M2 mounting hole at (2, 3), M3 mounting hole at (18, 3), H1 at (10, 7) mm"),
        ],
        "base": [
            ("Wall", "2 mm"),
            ("Floor", "2 mm"),
            ("Board clearance", "1 mm"),
            ("Standoffs", "3 × ⌀5 mm, pilot ⌀1.6 mm"),
            ("Port J1", "5 × 3 mm, left wall"),
        ],
        "lid": [("Thickness", "2 mm"), ("Fit gap", "0.25 mm"), ("Lip", "0.4 mm deep, 1 mm wide")],
    }


def test_lid_without_a_lip_has_no_fit_gap_to_measure(tmp_path):
    _, scene = enclosure(tmp_path, ViewerSceneStore(), lip=False)
    gap = rows(scene)["lid_fit_gap:lid"]
    lid = next(part for part in scene["parts"] if part["label"] == "lid")

    # not applicable: neither a gate result nor an unknown measurement
    assert (gap["status"], gap["value"], gap.get("reason"), gap["evidence"]) == (
        "N/A", "the lid has no lip", None, None)
    assert gap["partId"] == lid["partId"]
    assert [(fact["label"], fact["value"]) for fact in lid["facts"]] == [("Thickness", "2 mm"), ("Lip", "none")]


def test_assembly_check_reuses_measured_pair_rows_in_the_scene(tmp_path):
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / "assembly.coalesce.json", scene_store=store, warmup=False)
    assert service.sketch(SKETCH)["ok"]
    assert service.feature(FEATURE)["ok"]
    assert service.feature(occurrence_create(2, "place-a", "a", 0))["ok"]
    assert service.feature(occurrence_create(3, "place-b", "b", 61))["ok"]
    wait_for(store, 4)

    response = service.check(
        {
            "action": "evaluate",
            "requirements": [
                {
                    "id": "clearance",
                    "kind": "assembly_interference",
                    "minimumClearance": {"value": 2, "unit": "mm"},
                }
            ],
        }
    )
    deadline = time.monotonic() + 5
    while not store.scene(4)["assemblyInspection"] and time.monotonic() < deadline:
        time.sleep(0.01)

    [row] = store.scene(4)["assemblyInspection"]
    measured = response["results"][0]["measured"]["pairs"][0]
    assert {key: row[key] for key in measured} == measured
    assert row["occurrenceIds"] == ["a", "b"]
    assert row["requirementId"] == "clearance"
    assert row["status"] == "FAIL"


@pytest.mark.parametrize("viewport", [(1280, 800), (390, 844)])
def test_fit_row_focuses_its_part_in_a_real_browser(module, viewport, tmp_path):  # noqa: F811
    store = ViewerSceneStore()
    server = ViewerServer(store)
    driver, step = open_driver(module, server.url, viewport)
    try:
        empty = step("fit-empty", wait={"ready": True}, do=[{"fit": True}])
        assert empty["actions"][0]["visible"] is False
        # the review's model: the connector height is unresolved, so the port check is UNKNOWN
        enclosure(tmp_path, store, holes=UNNAMED, models="unresolved")
        shown = step("fit-shown", wait={"revision": 5}, do=[{"fit": True}])
        fit = shown["actions"][0]
        assert fit["visible"] is True
        assert [row["status"] for row in fit["rows"]] == [
            "FAIL", "FAIL", "UNKNOWN", "MEASURED ONLY", "MEASURED ONLY", "MEASURED ONLY", "PASS",
            "MEASURED ONLY",
        ]  # problems first
        assert fit["rows"][0]["text"] == "FAIL Standoffs 2 of 3 mounting holes no standoff at H1"
        assert fit["rows"][3]["text"] == "MEASURED ONLY Wall thickness 2 mm · no requirement set"
        # a measured value is no gate result, and the panel says so once
        assert fit["count"] == "2 fail, 1 unknown, 1 pass, 4 measured only"
        assert fit["note"] == "Measured only values have no threshold and do not pass the fit gate."
        # the heading's title keeps one line, and no badge runs into its label
        assert fit["titleLines"] == 1
        assert fit["countLines"] == [1, 1, 1, 1], fit["count"]  # a count never wraps away from its status
        # the "scroll for more" bar sits below the scrolled rows, not over the last one
        assert fit["moreTop"] is None or fit["scrollBottom"] <= fit["moreTop"] + 0.5, fit
        assert min(row["gap"] for row in fit["rows"]) >= 4, fit["rows"]
        assert len({row["labelLeft"] for row in fit["rows"]}) == 1, fit["rows"]  # one label column
        assert fit["overflow"] == [], fit["overflow"]
        assert fit["evidenceOpen"] is False
        if viewport[0] < 720:
            # the panel ends in a cue below the row it cuts, so more checks are not a surprise
            assert fit["more"] == "Scroll for more results and parts"

        view = shown["scene"]["view"]
        focused = step("fit-row-focused", do=[{"fitRow": 0}, {"settle": True}, {"fit": True}])
        state = focused["actions"][2]
        assert (state["focusedPart"], state["openPart"]) == ("base", "base")
        assert ["Standoffs", "3 × ⌀5 mm, pilot ⌀1.6 mm"] not in state["facts"]  # two holes, not three
        assert ["Standoffs", "2 × ⌀5 mm, pilot ⌀1.6 mm"] in state["facts"]
        assert state["overflow"] == [], state["overflow"]
        # the canvas shows the row's part, the others faded, from the same camera
        assert state["rows"][0]["pressed"] == "true"
        drawn = focused["scene"]
        assert dict(zip(drawn["parts"], drawn["faded"], strict=True)) == {
            name: name != next(part["name"] for part in store.scene(5)["parts"] if part["label"] == "base")
            for name in drawn["parts"]
        }
        assert drawn["view"] == view
        assert "Showing base, other parts faded." in focused["description"]
        cleared = step("fit-row-cleared", do=[{"fitRow": 0}, {"settle": True}, {"fit": True}])
        assert cleared["actions"][2]["rows"][0]["pressed"] == "false"
        assert cleared["scene"]["faded"] == [False] * 3
        assert cleared["scene"]["view"] == view
        # Escape shows all parts again, puts focus back on the row it started from, and says so
        step("fit-row-again", do=[{"fitRow": 0}, {"settle": True}])
        escaped = step("fit-row-escape", do=[{"key": "Escape"}, {"settle": True}, {"fit": True}])
        assert escaped["actions"][2]["rows"][0]["pressed"] == "false"
        assert escaped["scene"]["faded"] == [False] * 3
        assert escaped["focused"].startswith("FAILStandoffs")
        assert escaped["description"].startswith("All parts shown. Revision 5,")
        # a section and a shown part: Escape in the section controls ends the section first, the next shows all
        sectioned = step(
            "fit-row-section",
            do=[{"fitRow": 0}, {"click": "Section"}, {"settle": True}, {"fit": True}, {"focus": "#section-range"}],
        )
        if viewport[0] < 720:
            fit_state = sectioned["actions"][3]
            selected_box = fit_state["rows"][0]["box"]
            assert fit_state["countVisible"] is True
            assert fit_state["panelBox"]["top"] <= selected_box["top"]
            assert selected_box["bottom"] <= fit_state["panelBox"]["bottom"]
        first = step("fit-row-section-escape", do=[{"key": "Escape"}, {"settle": True}])
        assert first["focused"] == "Assembled"
        assert first["scene"]["cut"] == [False] * 3 and first["scene"]["faded"].count(False) == 1
        second = step("fit-row-section-escape-again", do=[{"key": "Escape"}, {"settle": True}])
        assert second["scene"]["faded"] == [False] * 3
        assert second["focused"].startswith("FAILStandoffs")

        evidence = step("fit-evidence", do=[{"evidence": True}, {"fit": True}])
        opened = evidence["actions"][1]
        assert opened["evidenceOpen"] is True
        # one line per row in words, with the part it was measured on; ids only in the raw record
        assert opened["evidence"][0] == "Standoffs on base: 2 of 3 mounting holes, fail"
        assert "Wall thickness on base: 2 mm, measured only" in opened["evidence"]
        assert len(opened["evidence"]) == 8
        assert not any("hole-" in line or "{" in line for line in opened["evidence"])
        assert (opened["rawOpen"], opened["raw"]) == (False, None)
        # focus moved on, the facts stay open: nothing below them jumped
        assert (opened["focusedPart"], opened["openPart"]) == (None, "base")
        assert ["Standoffs", "2 × ⌀5 mm, pilot ⌀1.6 mm"] in opened["facts"]
        raw = step("fit-evidence-raw", do=[{"rawEvidence": True}, {"fit": True}])["actions"][1]
        assert raw["rawOpen"] is True and H1 in raw["raw"]
        driver.stdin.close()
        assert driver.wait(timeout=30) == 0
    finally:
        driver.kill()
        server.close()


def test_failing_screw_point_is_named_first_in_browser_evidence(module):  # noqa: F811
    store = ViewerSceneStore()
    server = ViewerServer(store)
    driver, step = open_driver(module, server.url, (1280, 800))
    evidence = {
        "points": [
            {"name": "front-left", "residualWall": {"value": 1.2}, "coaxial": True, "status": "PASS"},
            {"name": "rear-right", "residualWall": {"value": 0.8}, "coaxial": False, "status": "FAIL"},
        ]
    }
    fit = [{"baseId": "base", "rows": [{
        "id": "screw_closure", "label": "Screw closure", "status": "FAIL",
        "value": "2 points, 0.8 mm minimum wall, not coaxial, no interference",
        "partId": "base", "evidence": evidence,
    }]}]
    try:
        step("empty", wait={"ready": True})
        store.publish(1, [ScenePart("base", plate(6))], fit=fit)
        shown = step("screw-points", wait={"revision": 1}, do=[{"evidence": True}, {"fit": True}])
        points = [line for line in shown["actions"][1]["evidence"] if line.startswith(("front-left", "rear-right"))]
        assert points == [
            "rear-right: wall 0.8 mm, not coaxial — fail",
            "front-left: wall 1.2 mm, coaxial — pass",
        ]
        driver.stdin.close()
        assert driver.wait(timeout=30) == 0
    finally:
        driver.kill()
        server.close()


def gap_check(part_id):
    row = {"id": "gap", "label": "Fit gap", "status": "FAIL", "value": "0 mm", "reason": "too tight",
           "partId": part_id, "evidence": {}}
    return [{"baseId": "base", "rows": [row]}]


def test_shown_part_ends_only_by_its_own_escape_or_a_revision_that_drops_it(module, tmp_path):  # noqa: F811
    store = ViewerSceneStore()
    server = ViewerServer(store)
    driver, step = open_driver(module, server.url, (1280, 800))
    lid = ScenePart("lid", Pos(0, 0, 10) * plate(2))
    try:
        step("empty", wait={"ready": True})
        store.publish(1, [ScenePart("base", plate(6)), lid], fit=gap_check("lid"))
        step("shown", wait={"revision": 1}, do=[{"fitRow": 0}, {"settle": True}])
        # a native disclosure does not use Escape, so Escape on its summary shows all parts
        summary = step("summary-escape", do=[{"focus": ".fit-evidence > summary"}, {"key": "Escape"}, {"settle": True}])
        assert summary["scene"]["faded"] == [False, False]
        # Escape on the page shows all parts too
        step("shown-for-body", do=[{"fitRow": 0}, {"settle": True}])
        cleared = step("body-escape", do=[{"blur": True}, {"key": "Escape"}, {"settle": True}])
        assert cleared["scene"]["faded"] == [False, False]

        # a new revision keeps the part but drops its fit row: all parts again, focus stays on the part, and it says so
        step("shown-again", do=[{"fitRow": 0}, {"settle": True}])
        store.publish(2, [ScenePart("base", plate(6)), lid])
        dropped = step("row-dropped", wait={"revision": 2}, do=[{"settle": True}])
        assert dropped["scene"]["faded"] == [False, False]
        assert dropped["focused"] == "lid"
        assert dropped["description"].startswith("All parts shown. Revision 2,")

        # focus elsewhere stays where it is when a revision drops the shown part
        store.publish(3, [ScenePart("base", plate(6)), lid], fit=gap_check("lid"))
        step("shown-3", wait={"revision": 3}, do=[{"fitRow": 0}, {"settle": True}, {"focus": "[data-mode='section']"}])
        store.publish(4, [ScenePart("base", plate(6))], fit=gap_check(None))
        gone = step("part-removed", wait={"revision": 4}, do=[{"settle": True}])
        assert gone["scene"]["faded"] == [False]
        assert gone["focused"] == "Section"
        assert gone["description"].startswith("All parts shown. Revision 4,")
        driver.stdin.close()
        assert driver.wait(timeout=30) == 0
    finally:
        driver.kill()
        server.close()
