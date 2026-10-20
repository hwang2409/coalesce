import json
import math
from pathlib import Path
import random

import pytest

from coalesce.importers.kicad import parse_kicad_pcb

FIXTURES = Path(__file__).parent / "fixtures/kicad"
ROUNDED = FIXTURES / "rounded.kicad_pcb"


def _parse_text(tmp_path, text):
    path = tmp_path / "board.kicad_pcb"
    path.write_text(text, encoding="utf-8")
    return parse_kicad_pcb(path)


def _board(*items):
    return "(kicad_pcb (version 20240108) (generator test)\n" + "\n".join(items) + "\n)"


def _line(a, b, layer="Edge.Cuts"):
    return f'(gr_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (layer "{layer}"))'


def _polygon(points):
    return [_line(a, b) for a, b in zip(points, points[1:] + points[:1], strict=True)]


def test_rounded_board_and_json_shape():
    board = parse_kicad_pcb(ROUNDED)
    assert board.thickness == 1.6
    assert board.thickness_source == "general.thickness"
    assert len(board.outline) == 1
    assert board.outline[0].bbox == {"min_x": 0.0, "min_y": 0.0, "max_x": 10.0, "max_y": 10.0}
    assert len(board.outline[0].segments) == 8
    hole = board.holes[0]
    assert hole.position == (2.0, 3.0)
    assert (hole.drill.shape, hole.drill.width, hole.drill.height) == ("circle", 3.2, 3.2)
    assert not hole.plated
    connector = board.footprints[1]
    assert (connector.position, connector.rotation, connector.side) == ((5.0, 5.0), 90.0, "B")
    assert connector.models[0].offset == (1.0, 2.0, 3.0)
    assert connector.models[0].unresolved_variables
    decoded = json.loads(board.to_json())
    assert decoded["units"] == "mm"
    assert decoded["holes"][0]["drill"]["shape"] == "circle"


def test_root_and_footprint_edges_accept_layer_and_layers_with_shared_transforms(tmp_path):
    front = """(footprint "Front" (layer "F.Cu") (at 10 20 90)
      (fp_line (start 0 0) (end 2 0) (layers "Edge.Cuts"))
      (fp_line (start 2 0) (end 0 0) (layers "Edge.Cuts")))"""
    back = """(footprint "Back" (layer "B.Cu") (at 30 40 90)
      (fp_line (start 0 0) (end 2 0) (layer "Edge.Cuts"))
      (fp_line (start 2 0) (end 0 0) (layer "Edge.Cuts")))"""
    root_layers = '(gr_line (start 0 0) (end 1 0) (layers "Edge.Cuts"))'
    board = _parse_text(tmp_path, _board(root_layers, _line((1, 0), (0, 0)), front, back))
    segments = [segment for loop in board.outline + board.inner_cutouts for segment in loop.segments]
    endpoints = {(segment.start, segment.end) for segment in segments}
    assert ((10.0, 20.0), (10.0, 22.0)) in endpoints
    # KiCad back footprints mirror local Y, then apply the footprint rotation and translation.
    assert ((30.0, 40.0), (30.0, 42.0)) in endpoints


def test_shared_transform_applies_to_holes_and_courtyards_on_both_sides(tmp_path):
    def footprint(name, layer, x):
        return f"""(footprint "{name}" (layer "{layer}") (at {x} 20 90)
          (fp_line (start 1 2) (end 3 2) (layer "{'B' if layer == 'B.Cu' else 'F'}.CrtYd"))
          (pad "1" thru_hole oval (at 1 2 30) (size 3 2) (drill oval 1 2)))"""

    board = _parse_text(tmp_path, _board(*_polygon([(0, 0), (50, 0), (50, 50), (0, 50)]),
                                                footprint("Front", "F.Cu", 10),
                                                footprint("Back", "B.Cu", 30)))
    front, back = board.footprints
    assert (front.courtyard[0].start, front.courtyard[0].end) == ((8.0, 21.0), (8.0, 23.0))
    assert (back.courtyard[0].start, back.courtyard[0].end) == ((32.0, 21.0), (32.0, 23.0))
    assert [hole.position for hole in board.holes] == [(8.0, 21.0), (32.0, 21.0)]


def test_typed_oval_drills_keep_dimensions_orientation_and_plating(tmp_path):
    footprint = """(footprint "Drills" (layer "B.Cu") (at 10 10 90)
      (pad "1" thru_hole oval (at 1 2 30) (size 3 2) (drill oval 1 2))
      (pad "2" np_thru_hole circle (at 0 0 15) (size 3 3) (drill 1.5)))"""
    board = _parse_text(tmp_path, _board(*_polygon([(0, 0), (20, 0), (20, 20), (0, 20)]), footprint))
    slot, round_hole = board.holes
    assert (slot.drill.shape, slot.drill.width, slot.drill.height, slot.drill.rotation) == ("oval", 1.0, 2.0, 60.0)
    assert slot.plated
    assert (round_hole.drill.shape, round_hole.drill.width, round_hole.drill.height) == ("circle", 1.5, 1.5)
    assert not round_hole.plated


def test_arc_bbox_includes_exact_cardinal_extrema(tmp_path):
    arc = """(gr_arc (start 0.984807753 0.173648178) (mid 0.766044443 0.642787610)
      (end -0.173648178 0.984807753) (layer "Edge.Cuts"))"""
    closing = _line((-0.173648178, 0.984807753), (0.984807753, 0.173648178))
    board = _parse_text(tmp_path, _board(arc, closing))
    assert board.outline[0].bbox["max_y"] == pytest.approx(1.0)


def test_loop_classification_uses_point_containment_and_even_odd_depth(tmp_path):
    concave = [(0, 0), (10, 0), (10, 3), (3, 3), (3, 7), (10, 7), (10, 10), (0, 10)]
    in_bbox_but_outside = [(5, 4), (6, 4), (6, 6), (5, 6)]
    outer = [(20, 0), (40, 0), (40, 20), (20, 20)]
    hole = [(24, 4), (36, 4), (36, 16), (24, 16)]
    island = [(28, 8), (32, 8), (32, 12), (28, 12)]
    items = [item for points in (concave, in_bbox_but_outside, outer, hole, island) for item in _polygon(points)]
    board = _parse_text(tmp_path, _board(*items))
    assert len(board.outline) == 4
    assert len(board.inner_cutouts) == 1
    assert board.inner_cutouts[0].bbox["min_x"] == 24.0


def test_any_nonzero_closing_gap_is_open_outline(tmp_path):
    points = [(0, 0), (10, 0), (10, 10), (0, 10)]
    items = _polygon(points)[:-1] + [_line((0, 10), (0.000001, 0))]
    board = _parse_text(tmp_path, _board(*items))
    assert [error.kind for error in board.errors] == ["open_outline"]
    assert board.errors[0].endpoints == [(0.000001, 0.0), (0.0, 0.0)]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "(",
        "(kicad_pcb (general (thickness nope)))",
        "(kicad_pcb (gr_arc (start 0 0) (mid 1 0) (end 2 0) (layer Edge.Cuts)))",
        "(not_a_board)",
    ],
)
def test_all_malformed_input_returns_structured_parse_error(tmp_path, text):
    board = _parse_text(tmp_path, text)
    assert board.errors and board.errors[0].kind == "parse"
    assert board.outline == []


def test_utf8_and_kicad_escapes_are_preserved_with_kicad6_text_fallback():
    board = parse_kicad_pcb(FIXTURES / "kicad6_legacy_text.kicad_pcb")
    footprint = board.footprints[0]
    assert footprint.reference == 'Café "quoted"'
    assert footprint.value == "Legacy\\Path"


def test_required_m5stack_extract_has_independent_mechanical_assertions():
    path = FIXTURES / "m5stack_module_extport_trimmed.kicad_pcb"
    source = path.read_text(encoding="utf-8")
    assert "Copyright (c) 2021 M5Stack" in (FIXTURES / "LICENSE-M5Stack.txt").read_text()
    assert '(footprint "MountingHole:MountingHole_2.2mm_M2"' in source
    assert '(footprint "akita:SMDIP-6_W9.53mm_narrow" (layer "B.Cu")' in source
    board = parse_kicad_pcb(path)
    assert board.thickness == 1.6
    assert board.outline[0].bbox == {"min_x": 0.0, "min_y": 0.0, "max_x": 50.0, "max_y": 50.0}
    assert len(board.outline[0].segments) == 10
    front, back, plated = board.footprints
    assert front.courtyard[0].start == (7.0, 3.0)
    assert front.courtyard[0].end == (9.45, 3.0)
    # Back local (6.05, -4.1) mirrors to (6.05, 4.1), rotates -90°, then translates (14, 17).
    assert back.courtyard[0].start == pytest.approx((18.1, 10.95))
    assert back.courtyard[0].end == pytest.approx((9.9, 10.95))
    assert back.models[0].path.endswith("SMDIP-6_W9.53mm.wrl")
    assert back.models[0].unresolved_variables
    assert (board.holes[0].position, board.holes[0].drill.width, board.holes[0].plated) == ((7.0, 3.0), 2.2, False)
    # Back local (-3, 0) mirrors unchanged, rotates -90°, then translates (6.9, 33).
    assert board.holes[1].position == pytest.approx((6.9, 36.0))
    assert (board.holes[1].drill.width, board.holes[1].plated) == (0.8, True)
    assert plated.reference == "CN5"


def test_existing_outline_forms_and_cutout_regressions():
    rect = parse_kicad_pcb(FIXTURES / "features.kicad_pcb")
    assert rect.outline[0].bbox == {"min_x": 0.0, "min_y": 0.0, "max_x": 20.0, "max_y": 10.0}
    circle = parse_kicad_pcb(FIXTURES / "circle.kicad_pcb")
    assert circle.outline[0].bbox == {"min_x": 2.0, "min_y": 2.0, "max_x": 8.0, "max_y": 8.0}
    cutout = parse_kicad_pcb(FIXTURES / "cutout.kicad_pcb")
    assert len(cutout.outline) == len(cutout.inner_cutouts) == 1


def test_polygon_edges_are_supported_and_curves_are_explicitly_rejected(tmp_path):
    polygon = """(gr_poly (pts (xy 0 0) (xy 10 0) (xy 10 5) (xy 0 5))
      (layer "Edge.Cuts"))"""
    board = _parse_text(tmp_path, _board(polygon))
    assert len(board.outline) == 1
    assert len(board.outline[0].segments) == 4
    assert board.errors == []

    curve = """(gr_curve (pts (xy 0 0) (xy 1 0) (xy 1 1) (xy 0 1))
      (layer "Edge.Cuts") (uuid curve-id))"""
    board = _parse_text(tmp_path, _board(curve))
    error = next(error for error in board.errors if error.kind == "unsupported_geometry")
    assert error.item_ids == ["curve-id"]
    assert "gr_curve" in error.message


def test_board_without_a_recognized_closed_outline_is_an_error(tmp_path):
    board = _parse_text(tmp_path, _board('(gr_text "not an outline" (at 0 0) (layer "F.SilkS"))'))
    assert board.outline == []
    assert any(error.kind == "missing_outline" for error in board.errors)


def test_transformed_endpoints_use_one_nanometre_canonical_keys(tmp_path):
    footprint = """(footprint "Quarter" (layer "F.Cu") (at 0 0 90)
      (fp_line (start 0 0) (end 1 0) (layer "Edge.Cuts")))"""
    closed = _parse_text(tmp_path, _board(footprint,
                                           _line((0, 1), (-1, 1)),
                                           _line((-1, 1), (-1, 0)),
                                           _line((-1, 0), (0, 0))))
    assert len(closed.outline) == 1
    assert closed.errors == []

    gap = _parse_text(tmp_path, _board(footprint,
                                        _line((0.001, 1), (-1, 1)),
                                        _line((-1, 1), (-1, 0)),
                                        _line((-1, 0), (0, 0))))
    assert [error.kind for error in gap.errors] == ["open_outline"]
    assert gap.errors[0].endpoints[0] == pytest.approx((0.0, 1.0))
    assert gap.errors[0].endpoints[1] == (0.001, 1.0)


def test_drill_offsets_follow_pad_rotation_and_back_mirroring(tmp_path):
    def footprint(name, layer, position, rotation):
        return f"""(footprint "{name}" (layer "{layer}") (at {position[0]} {position[1]} {rotation})
          (pad "1" np_thru_hole circle (at 3 4 30) (size 2 2)
            (drill 1 (offset 1 2))))"""

    board = _parse_text(tmp_path, _board(*_polygon([(0, 0), (50, 0), (50, 50), (0, 50)]),
                                                footprint("Front", "F.Cu", (10, 20), 90),
                                                footprint("Back", "B.Cu", (30, 40), 90)))
    # Offset (1, 2) rotates 30 degrees around the pad, then the footprint transform is applied.
    offset_x = 1 * 3**0.5 / 2 - 2 * 0.5
    offset_y = 1 * 0.5 + 2 * 3**0.5 / 2
    assert board.holes[0].position == pytest.approx((10 - (4 + offset_y), 20 + 3 + offset_x))
    assert board.holes[1].position == pytest.approx((30 + (4 + offset_y), 40 + 3 + offset_x))


def test_arc_containment_uses_the_true_curve_not_ten_degree_chords(tmp_path):
    upper = """(gr_arc (start 10 0) (mid 0 10) (end -10 0) (layer "Edge.Cuts"))"""
    lower = """(gr_arc (start -10 0) (mid 0 -10) (end 10 0) (layer "Edge.Cuts"))"""
    triangle = [(9.98 * math.cos(math.radians(angle)), 9.98 * math.sin(math.radians(angle)))
                for angle in (4.8, 5.2)]
    triangle.append((9.97 * math.cos(math.radians(5)), 9.97 * math.sin(math.radians(5))))
    board = _parse_text(tmp_path, _board(upper, lower, *_polygon(triangle)))
    assert len(board.outline) == 1
    assert len(board.inner_cutouts) == 1


@pytest.mark.parametrize(
    ("footprint", "path"),
    [
        ('(footprint "Bad" (layer "F.Cu") (at 10))', "footprint[0].at"),
        ('(footprint "Bad" (layer "F.Cu") (at nope 2))', "footprint[0].at"),
        ('(footprint "Bad" (layer "F.Cu") (pad "1" np_thru_hole circle (at 4) (drill 1)))',
         "footprint[0].pad[0].at"),
    ],
)
def test_present_malformed_position_reports_its_node_path(tmp_path, footprint, path):
    board = _parse_text(tmp_path, _board(*_polygon([(0, 0), (10, 0), (10, 10), (0, 10)]), footprint))
    assert board.errors[0].kind == "parse"
    assert path in board.errors[0].message


def test_half_nanometre_uses_kicad_rounding_and_does_not_close(tmp_path):
    board = _parse_text(tmp_path, _board(
        _line((0, 0), (0.000001, 0)),
        _line((0.0000005, 0), (0, 0)),
    ))
    assert [error.kind for error in board.errors] == ["open_outline"]


def test_non_cardinal_footprint_rotation_uses_integer_nanometres(tmp_path):
    footprint = """(footprint "Rotated" (layer "F.Cu") (at 0 0 45)
      (fp_line (start 0 0) (end 0.000001 0) (layer "Edge.Cuts")))"""
    board = _parse_text(tmp_path, _board(
        footprint,
        _line((0.000001, 0.000001), (0, 0.000001)),
        _line((0, 0.000001), (0, 0)),
    ))
    assert board.errors == []
    assert board.outline[0].segments[0].end == (0.000001, 0.000001)


def test_arc_seam_y_uses_half_open_vertex_ownership(tmp_path):
    upper = """(gr_arc (start 9.945218953683 1.045284632677)
      (mid -1.045284632677 9.945218953683) (end -9.945218953683 -1.045284632677)
      (layer "Edge.Cuts"))"""
    lower = """(gr_arc (start -9.945218953683 -1.045284632677)
      (mid 1.045284632677 -9.945218953683) (end 9.945218953683 1.045284632677)
      (layer "Edge.Cuts"))"""
    triangle = [(0, 1.045284632677), (0.1, 1.145284632677), (-0.1, 1.145284632677)]
    board = _parse_text(tmp_path, _board(upper, lower, *_polygon(triangle)))
    assert len(board.outline) == 1
    assert len(board.inner_cutouts) == 1


@pytest.mark.parametrize(
    "invalid",
    [
        _line((1, 1), (1, 1)),
        '(gr_arc (start 1 0) (mid 0 1) (end 1 0) (layer "Edge.Cuts"))',
        '(gr_circle (center 1 1) (end 1 1) (layer "Edge.Cuts"))',
    ],
)
def test_zero_length_outline_geometry_is_a_structured_parse_error(tmp_path, invalid):
    board = _parse_text(tmp_path, _board(invalid))
    assert [error.kind for error in board.errors] == ["parse"]
    assert board.outline == []


def test_footprint_curve_courtyard_warns_explicitly(tmp_path):
    footprint = """(footprint "Curve" (layer "F.Cu")
      (fp_curve (pts (xy 0 0) (xy 1 0) (xy 1 1) (xy 0 1)) (layer "F.CrtYd")))"""
    board = _parse_text(tmp_path, _board(*_polygon([(0, 0), (2, 0), (2, 2), (0, 2)]), footprint))
    assert board.errors == []
    assert any("unsupported courtyard geometry: fp_curve" in warning for warning in board.warnings)


@pytest.mark.parametrize(
    "item",
    [
        "(general (thickness 0))",
        "(general (thickness -1))",
        '(footprint "Drill" (layer "F.Cu") (pad "1" np_thru_hole circle (drill 0)))',
        '(footprint "Drill" (layer "F.Cu") (pad "1" np_thru_hole oval (drill oval 1 -2)))',
    ],
)
def test_nonpositive_dimensions_are_structured_parse_errors(tmp_path, item):
    board = _parse_text(tmp_path, _board(*_polygon([(0, 0), (2, 0), (2, 2), (0, 2)]), item))
    assert [error.kind for error in board.errors] == ["parse"]


def test_seeded_random_nested_rotated_geometry(tmp_path):
    rng = random.Random(2409)

    def rotate(point, degrees):
        angle = math.radians(degrees)
        return (point[0] * math.cos(angle) - point[1] * math.sin(angle),
                point[0] * math.sin(angle) + point[1] * math.cos(angle))

    def rectangle(size, degrees):
        return [rotate(point, degrees) for point in [(-size, -size), (size, -size), (size, size), (-size, size)]]

    def arc_circle(radius, degrees):
        points = [rotate((radius, 0), degrees), rotate((0, radius), degrees),
                  rotate((-radius, 0), degrees), rotate((0, -radius), degrees)]
        return [
            f'(gr_arc (start {points[0][0]:.12f} {points[0][1]:.12f}) '
            f'(mid {points[1][0]:.12f} {points[1][1]:.12f}) '
            f'(end {points[2][0]:.12f} {points[2][1]:.12f}) (layer "Edge.Cuts"))',
            f'(gr_arc (start {points[2][0]:.12f} {points[2][1]:.12f}) '
            f'(mid {points[3][0]:.12f} {points[3][1]:.12f}) '
            f'(end {points[0][0]:.12f} {points[0][1]:.12f}) (layer "Edge.Cuts"))',
        ]

    for case in range(200):
        degrees = rng.uniform(-180, 180)
        outer_kind, inner_kind = rng.choice(["rectangle", "circle", "arcs"]), rng.choice(
            ["rectangle", "circle", "arcs"]
        )

        def shape(kind, size):
            if kind == "rectangle":
                return _polygon(rectangle(size, degrees))
            if kind == "circle":
                edge = rotate((size, 0), degrees)
                return [f'(gr_circle (center 0 0) (end {edge[0]:.12f} {edge[1]:.12f}) '
                        '(layer "Edge.Cuts"))']
            return arc_circle(size, degrees)

        board = _parse_text(tmp_path, _board(*shape(outer_kind, 10), *shape(inner_kind, 2)))
        assert board.errors == [], case
        assert (len(board.outline), len(board.inner_cutouts)) == (1, 1), case


def test_generated_usb_c_hub_fixture_records_public_mechanical_facts():
    board = parse_kicad_pcb(FIXTURES / "usb_c_hub_generated.kicad_pcb")
    assert board.thickness == 1.6
    assert board.errors == []
    assert len(board.outline) == 1
    assert len(board.outline[0].segments) == 8
    assert board.outline[0].bbox == pytest.approx(
        {"min_x": 53.086, "min_y": 80.772, "max_x": 110.49, "max_y": 108.712}
    )
    assert len(board.footprints) == len(board.holes) == 32
    assert board.holes[0].position == (106.68, 84.582)
    assert board.holes[1].position == (56.896, 104.902)
    assert board.holes[0].drill.width == board.holes[1].drill.width == 3.2
