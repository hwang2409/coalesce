import json
from pathlib import Path

import pytest

from coalesce.importers.kicad import parse_kicad_pcb

FIXTURE = Path(__file__).parent / "fixtures/kicad/rounded.kicad_pcb"
FIXTURES = FIXTURE.parent


def test_rounded_board_and_typed_entities():
    board = parse_kicad_pcb(FIXTURE)
    assert board.thickness == 1.6
    assert board.thickness_source == "general.thickness"
    assert len(board.outline) == 1
    assert board.outline[0].bbox == {"min_x": 0.0, "min_y": 0.0, "max_x": 10.0, "max_y": 10.0}
    assert len(board.outline[0].segments) == 8
    hole = board.holes[0]
    assert hole.position == (2.0, 3.0) and hole.diameter == 3.2 and not hole.plated
    connector = board.footprints[1]
    assert connector.id == "conn-id"
    assert connector.position == (5.0, 5.0) and connector.rotation == 90.0 and connector.side == "B"
    assert connector.courtyard_availability == "present"
    assert connector.models[0].unresolved_variables
    assert connector.models[0].offset == (1.0, 2.0, 3.0)
    assert board.footprints[0].reference == "REF**"


def test_json_round_trip_shape_is_serializable():
    board = parse_kicad_pcb(FIXTURE)
    decoded = json.loads(board.to_json())
    assert decoded["units"] == "mm"
    assert decoded["footprints"][1]["side"] == "B"


def test_open_outline_is_structured_error(tmp_path):
    source = FIXTURE.read_text().replace('(gr_arc (start 1 10)', '(gr_arc (start 2 10)')
    path = tmp_path / "open.kicad_pcb"
    path.write_text(source)
    board = parse_kicad_pcb(path)
    assert board.errors and board.errors[0].kind == "open_outline"
    assert board.errors[0].endpoints


def test_nested_cutout_roles_and_bboxes():
    path = FIXTURES / "cutout.kicad_pcb"
    source = path.read_text()
    for value in ("(start 0 0) (end 20 0)", "(start 5 2) (end 15 2)"):
        assert value in source
    board = parse_kicad_pcb(path)
    assert len(board.outline) == 1 and not board.outline[0].inner
    assert board.outline[0].bbox == {"min_x": 0.0, "min_y": 0.0, "max_x": 20.0, "max_y": 10.0}
    assert len(board.inner_cutouts) == 1 and board.inner_cutouts[0].inner
    assert board.inner_cutouts[0].bbox == {"min_x": 5.0, "min_y": 2.0, "max_x": 15.0, "max_y": 8.0}


def test_rectangle_and_line_chamfered_outlines():
    rect = parse_kicad_pcb(FIXTURES / "features.kicad_pcb")
    source = (FIXTURES / "features.kicad_pcb").read_text()
    assert '(gr_rect (start 0 0) (end 20 10)' in source
    assert len(rect.outline) == 1 and len(rect.outline[0].segments) == 4
    assert rect.outline[0].bbox == {"min_x": 0.0, "min_y": 0.0, "max_x": 20.0, "max_y": 10.0}
    chamfer = parse_kicad_pcb(FIXTURES / "chamfer.kicad_pcb")
    assert len(chamfer.outline) == 1 and len(chamfer.outline[0].segments) == 8
    assert chamfer.outline[0].bbox == {"min_x": 0.0, "min_y": 0.0, "max_x": 10.0, "max_y": 10.0}


def test_mounting_holes_refs_models_sides_and_courtyard():
    path = FIXTURES / "features.kicad_pcb"
    source = path.read_text()
    for value in ("(at 2 3)", "(drill 2.2)", "(at 18 3)", "(drill 3.2)", "(at 10 7)", "(drill 1.5)"):
        assert value in source
    board = parse_kicad_pcb(path)
    assert [(h.position, h.diameter, h.plated) for h in board.holes] == [
        ((2.0, 3.0), 2.2, True), ((18.0, 3.0), 3.2, True), ((10.0, 7.0), 1.5, False)]
    assert [f.reference for f in board.footprints[:2]] == ["REF**", "REF**"]
    assert board.footprints[0].id != board.footprints[1].id
    front, back = board.footprints[3:]
    assert (front.rotation, front.side) == (45.0, "F")
    assert (back.rotation, back.side) == (270.0, "B")
    assert front.courtyard_availability == back.courtyard_availability == "missing"
    assert all(m.unresolved_variables for f in (front, back) for m in f.models)


def test_circle_outline():
    path = FIXTURES / "circle.kicad_pcb"
    assert '(gr_circle (center 5 5) (end 8 5)' in path.read_text()
    board = parse_kicad_pcb(path)
    assert len(board.outline) == 1 and board.outline[0].segments[0].kind == "circle"
    assert board.outline[0].bbox == {"min_x": 2.0, "min_y": 2.0, "max_x": 8.0, "max_y": 8.0}


def test_public_projects_have_explicit_license_disposition():
    pytest.skip("upstream SL2.1A-Type-C-Hub has no LICENSE file; source numbers are recorded in README-sources.md")


def test_public_m5_source_license_is_mit_and_numbers_are_recorded():
    pytest.skip("M5_Hardware is MIT, but its full board is not redistributed; verified numbers are recorded in README-sources.md")
