"""The viewer's fit check and part facts: measured on the committed enclosure, never a PASS without a measurement."""

import time

import pytest
from test_mcp_cut_hole_circle import extrude, rectangle_sketch
from test_pcb_enclosure import FIXTURES, base_request, import_request, quantity, standoffs_request
from test_pcb_lid import lid_request
from test_port_cutout import port_request
from test_viewer_review_flow import module, open_driver  # noqa: F401 - module is a fixture

from coalesce import enclosure_inspection
from coalesce.service import DocumentService
from coalesce.viewer import ViewerSceneStore, ViewerServer

H1 = "hole-9ddb205ba1bc297f95347452"  # the NPTH mounting hole with reference H1
UNNAMED = ["hole-03e3c0594f5f1b105237c4c0", "hole-89c529cbad8407f9ccb81676"]  # REF** holes


def wait_for(store, revision):
    deadline = time.monotonic() + 15
    while store.current_revision != revision and time.monotonic() < deadline:
        time.sleep(0.02)
    scene = store.scene(revision)
    assert scene is not None, f"revision {revision} was not published"
    return scene


def enclosure(tmp_path, store, *, holes=(*UNNAMED, H1), models="declared", lid=True, port=True):
    """Board, base, standoffs at ``holes``, a lid with a 0.4 mm lip, and a J1 port; returns the last revision."""
    service = DocumentService(tmp_path / "fit.coalesce.json", scene_store=store)
    assert service.import_board(import_request(FIXTURES / "features.kicad_pcb"))["ok"]
    # shortcut: the fixture declares no 3D models; set what a board with models reports
    service._document.graph.nodes["controller_board"].availability["models"] = models
    calls = [base_request(), standoffs_request(selection={"mode": "explicit", "holeIds": list(holes)})]
    if lid:
        # a shallow lip stays above the port opening
        calls.append(lid_request(revision=3, lip={"depth": quantity(0.4), "width": quantity(1)}))
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
        "lid_interference:lid", "lid_fit_gap:lid", "port:port",
    ]
    summary = {key: (row["status"], row["value"], row.get("reason")) for key, row in found.items()}
    assert summary == {
        # no threshold is stored with the document: the value, never an invented PASS
        "wall_thickness": ("MEASURED", "2 mm", "no requirement set"),
        "floor_thickness": ("MEASURED", "2 mm", "no requirement set"),
        "board_clearance": ("MEASURED", "1 mm min, all sides", "no requirement set"),
        "standoffs": ("PASS", "3 of 3 mounting holes", None),
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
            ("Export", "reference only, not exported"),
            ("Mounting holes", "3: unnamed at (2, 3), unnamed at (18, 3), H1 at (10, 7) mm"),
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


@pytest.mark.parametrize("viewport", [(1280, 800), (390, 844)])
def test_fit_row_focuses_its_part_in_a_real_browser(module, viewport, tmp_path):  # noqa: F811
    store = ViewerSceneStore()
    server = ViewerServer(store)
    driver, step = open_driver(module, server.url, viewport)
    try:
        empty = step("fit-empty", wait={"ready": True}, do=[{"fit": True}])
        assert empty["actions"][0]["visible"] is False
        enclosure(tmp_path, store, holes=UNNAMED)
        shown = step("fit-shown", wait={"revision": 5}, do=[{"fit": True}])
        fit = shown["actions"][0]
        assert fit["visible"] is True
        assert [row["status"] for row in fit["rows"]] == [
            "FAIL", "Measured", "Measured", "Measured", "PASS", "Measured", "PASS",
        ]  # problems first
        assert fit["rows"][0]["text"] == "FAIL Standoffs 2 of 3 mounting holes no standoff at H1"
        assert fit["rows"][1]["text"] == "Measured Wall thickness 2 mm · no requirement set"
        assert fit["overflow"] == [], fit["overflow"]
        assert fit["evidenceOpen"] is False

        focused = step("fit-row-focused", do=[{"fitRow": 0}, {"fit": True}])
        state = focused["actions"][1]
        assert (state["focusedPart"], state["openPart"]) == ("base", "base")
        assert ["Standoffs", "3 × ⌀5 mm, pilot ⌀1.6 mm"] not in state["facts"]  # two holes, not three
        assert ["Standoffs", "2 × ⌀5 mm, pilot ⌀1.6 mm"] in state["facts"]
        assert state["overflow"] == [], state["overflow"]

        evidence = step("fit-evidence", do=[{"evidence": True}, {"fit": True}])
        opened = evidence["actions"][1]
        assert opened["evidenceOpen"] is True
        assert H1 in opened["evidence"]
        # focus moved on, the facts stay open: nothing below them jumped
        assert (opened["focusedPart"], opened["openPart"]) == (None, "base")
        assert ["Standoffs", "2 × ⌀5 mm, pilot ⌀1.6 mm"] in opened["facts"]
        driver.stdin.close()
        assert driver.wait(timeout=30) == 0
    finally:
        driver.kill()
        server.close()
