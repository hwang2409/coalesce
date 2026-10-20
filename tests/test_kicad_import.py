import json
from pathlib import Path

from coalesce.importers.kicad import parse_kicad_pcb

FIXTURE = Path(__file__).parent / "fixtures/kicad/rounded.kicad_pcb"


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


def test_public_projects_when_available():
    paths = [
        Path("/tmp/coalesce-pcb-hub/USB 2.0 Hub.kicad_pcb"),
        Path("/tmp/coalesce-pcb-m5/KiCad/Projects/Module_ExtPort_For_Core2/Module_ExtPort_For_Core2.kicad_pcb"),
    ]
    for path in paths:
        if path.exists():
            board = parse_kicad_pcb(path)
            assert board.thickness == 1.6
            assert board.outline and all(loop.closed for loop in board.outline)
