import math
import shutil
import time
from pathlib import Path

import pytest
from OCP.BRepCheck import BRepCheck_Analyzer

import coalesce.service as service_module
from coalesce.graph import BoardImport, GraphError
from coalesce.pcb import build_pcb
from coalesce.references import Cardinality, Reference, Status, resolve
from coalesce.service import DocumentService
from coalesce.viewer import ViewerSceneStore

FIXTURES = Path(__file__).parent / "fixtures/kicad"


def import_request(path: Path, **changes):
    return {
        "action": "create",
        "expectedRevision": 0,
        "requestId": "import-board",
        "assetId": "controller_board",
        "format": "kicad_pcb",
        "path": str(path.resolve()),
        **changes,
    }


def expected_area(name: str) -> float:
    return {
        "features": 200 - math.pi * (1.1**2 + 1.6**2 + 0.75**2),
        "rounded": 100 - (4 - math.pi) - math.pi * (1.6**2 + 0.5**2),
        "cutout": 200 - 60,
        "circle": math.pi * 3**2,
    }[name]


@pytest.mark.parametrize(
    ("name", "bbox", "thickness", "cutouts", "holes"),
    [
        ("features", (0, 0, 20, 10), 1.2, 0, 3),
        ("rounded", (0, 0, 10, 10), 1.6, 0, 2),
        ("cutout", (0, 0, 20, 10), 1.6, 1, 0),
        ("circle", (2, 2, 8, 8), 2.0, 0, 0),
    ],
)
def test_imported_board_regenerates_exact_valid_reference_geometry(
    tmp_path, name, bbox, thickness, cutouts, holes
):
    service = DocumentService(tmp_path / f"{name}.coalesce.json")
    response = service.import_board(import_request(FIXTURES / f"{name}.kicad_pcb"))

    assert response["ok"] is True
    shape = service._regen.shapes["controller_board"]
    assert BRepCheck_Analyzer(shape.wrapped).IsValid()
    box = shape.bounding_box()
    assert (box.min.X, box.min.Y, box.max.X, box.max.Y) == pytest.approx(bbox, abs=1e-6)
    assert box.size.Z == pytest.approx(thickness)
    assert shape.volume == pytest.approx(expected_area(name) * thickness, rel=1e-6)

    candidates = service._regen.provenance["controller_board"]
    top = next(item.entity for item in candidates if item.key.role == "board_top")
    assert len(top.wires()) - holes - 1 == cutouts
    assert len({item.key.source for item in candidates if item.key.role == "hole"}) == holes


@pytest.mark.parametrize("name", ["features", "rounded", "cutout", "circle"])
def test_board_source_ids_resolve_to_provenance_faces(tmp_path, name):
    service = DocumentService(tmp_path / f"{name}.coalesce.json")
    imported = service.import_board(import_request(FIXTURES / f"{name}.kicad_pcb"))
    mechanics = imported["boardImport"]["mechanics"]
    outer = mechanics["outline"][0]

    loop = service._regen.resolve(
        Reference("controller_board", "board_top", source=outer["id"])
    )
    assert loop.status is Status.OK
    assert len(loop.candidates) == 1

    segment_id = outer["segments"][0]["id"]
    segment_count = sum(item["id"] == segment_id for item in outer["segments"])
    segment = service._regen.resolve(
        Reference(
            "controller_board",
            "outline",
            Cardinality.EXACTLY,
            count=segment_count,
            source=segment_id,
        )
    )
    assert segment.status in {Status.OK, Status.SPLIT}
    assert len(segment.candidates) == segment_count

    for cutout in mechanics["innerCutouts"]:
        for item in cutout["segments"]:
            resolution = service._regen.resolve(
                Reference("controller_board", "cutout", source=item["id"])
            )
            assert resolution.status is Status.OK
            assert len(resolution.candidates) == 1

    for hole in mechanics["holes"]:
        resolution = service._regen.resolve(
            Reference("controller_board", "hole", source=hole["id"])
        )
        assert resolution.status is Status.OK
        assert len(resolution.candidates) == 1


def _rectangle_loop(ident, x1, y1, x2, y2):
    return {
        "id": ident,
        "closed": True,
        "segments": [
            {"id": f"{ident}-a", "kind": "line", "start": [x1, y1], "end": [x2, y1]},
            {"id": f"{ident}-b", "kind": "line", "start": [x2, y1], "end": [x2, y2]},
            {"id": f"{ident}-c", "kind": "line", "start": [x2, y2], "end": [x1, y2]},
            {"id": f"{ident}-d", "kind": "line", "start": [x1, y2], "end": [x1, y1]},
        ],
    }


def _board_node(*, holes=(), cutouts=(), outline=None):
    return BoardImport(
        "board",
        "/board.kicad_pcb",
        "digest",
        "2",
        {
            "thickness": 1.6,
            "outline": outline or [_rectangle_loop("outer", 0, 0, 20, 10)],
            "innerCutouts": list(cutouts),
            "holes": list(holes),
        },
        {},
    )


def _circle_hole(ident, x, y, diameter=2):
    return {
        "id": ident,
        "position": [x, y],
        "drill": {"shape": "circle", "width": diameter, "height": diameter, "rotation": 0},
    }


def test_oval_drill_uses_kicad_clockwise_rotation():
    node = _board_node(
        holes=[
            {
                "id": "slot",
                "position": [10, 5],
                "drill": {"shape": "oval", "width": 4, "height": 2, "rotation": 30},
            }
        ]
    )

    result = build_pcb(node)

    slot_area = 2 * 2 + math.pi * 1**2
    assert result.shape.volume == pytest.approx((200 - slot_area) * 1.6)
    slot_faces = [item for item in result.candidates if item.key.role == "hole"]
    assert len(slot_faces) == 4
    assert {item.key.source for item in slot_faces} == {"slot"}
    cap_centers = sorted(
        (item.entity.center().X, item.entity.center().Y)
        for item in slot_faces
        if item.entity.area > 4
    )
    assert cap_centers[0][1] > 5
    assert cap_centers[1][1] < 5


def test_drill_inside_cutout_is_a_no_op_with_empty_provenance():
    result = build_pcb(
        _board_node(
            cutouts=[_rectangle_loop("cutout", 6, 3, 14, 7)],
            holes=[_circle_hole("inside", 10, 5)],
        )
    )

    assert result.shape.volume == pytest.approx((200 - 32) * 1.6)
    resolution = resolve(Reference("board", "hole", source="inside"), {"board": result.candidates})
    assert resolution.status is Status.EMPTY
    assert resolution.candidates == []


def test_drill_overlapping_cutout_edge_merges_into_void():
    result = build_pcb(
        _board_node(
            cutouts=[_rectangle_loop("cutout", 6, 3, 14, 7)],
            holes=[_circle_hole("overlap", 6, 5)],
        )
    )

    assert result.shape.volume == pytest.approx((200 - 32 - math.pi / 2) * 1.6)
    hole_faces = [item for item in result.candidates if item.key.source == "overlap"]
    assert hole_faces


def test_drill_outside_substrate_is_a_no_op_with_empty_provenance():
    result = build_pcb(_board_node(holes=[_circle_hole("outside", 25, 5)]))

    assert result.shape.volume == pytest.approx(200 * 1.6)
    resolution = resolve(Reference("board", "hole", source="outside"), {"board": result.candidates})
    assert resolution.status is Status.EMPTY
    assert resolution.candidates == []


def test_nested_outline_builds_island_inside_cutout():
    result = build_pcb(
        _board_node(
            outline=[
                _rectangle_loop("outer", 0, 0, 20, 10),
                _rectangle_loop("island", 8, 4, 12, 6),
            ],
            cutouts=[_rectangle_loop("cutout", 6, 3, 14, 7)],
        )
    )

    assert len(result.shape.solids()) == 2
    assert result.shape.volume == pytest.approx((200 - 32 + 8) * 1.6)
    top_sources = {
        item.key.source for item in result.candidates if item.key.role == "board_top"
    }
    assert top_sources == {"outer", "island"}


def test_unsupported_outline_fails_with_structured_board_error():
    node = BoardImport(
        "board",
        "/board.kicad_pcb",
        "digest",
        "2",
        {
            "thickness": 1.6,
            "outline": [
                {
                    "id": "outer",
                    "closed": True,
                    "segments": [
                        {"id": "curve", "kind": "bezier", "start": [0, 0], "end": [0, 0]}
                    ],
                }
            ],
            "innerCutouts": [],
            "holes": [],
        },
        {},
    )

    with pytest.raises(GraphError) as error:
        build_pcb(node)
    assert error.value.error["code"] == "UNSUPPORTED_BOARD_OUTLINE"
    assert error.value.error["segment"] == "curve"


def test_reference_role_is_excluded_by_default_and_queryable_after_restart(tmp_path):
    document = tmp_path / "board.coalesce.json"
    service = DocumentService(document)
    service.import_board(import_request(FIXTURES / "features.kicad_pcb"))

    default = service.query({"select": {"kind": "measurements"}})
    reference = service.query({"select": {"kind": "measurements", "role": "reference"}})
    assert default["role"] == "manufacturing"
    assert default["solidCount"] == 0
    assert default["parts"] == []
    assert reference["role"] == "reference"
    assert reference["solidCount"] == 1
    assert reference["parts"][0]["role"] == "reference"
    assert reference["parts"][0]["label"] == "pcb reference"

    restarted = DocumentService(document).query(
        {"select": {"kind": "measurements", "role": "reference"}}
    )
    assert restarted == reference


def test_board_reference_cannot_be_used_as_a_consumable_body_target(tmp_path):
    service = DocumentService(tmp_path / "board.coalesce.json")
    service.import_board(import_request(FIXTURES / "features.kicad_pcb"))

    result = service.feature(
        {
            "action": "create",
            "expectedRevision": 1,
            "requestId": "board-hole",
            "featureId": "board_hole",
            "kind": "hole",
            "parameters": {
                "target": {"featureId": "controller_board"},
                "diameter": {"value": 3, "unit": "mm"},
                "position": [0, 0],
            },
        }
    )

    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert result["error"]["message"] == "hole target must produce a body"
    assert service.revision == 1


def test_invalid_refresh_is_atomic_and_keeps_last_good_reference_scene(tmp_path):
    source = tmp_path / "board.kicad_pcb"
    shutil.copyfile(FIXTURES / "features.kicad_pcb", source)
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / "board.coalesce.json", scene_store=store)
    assert service.import_board(import_request(source))["ok"] is True
    deadline = time.monotonic() + 5
    while store.current_revision != 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    first = store.scene(1)

    source.write_text(
        '(kicad_pcb (gr_line (start 0 0) (end 10 0) (layer "Edge.Cuts")))'
    )
    failed = service.import_board(
        {
            "action": "refresh",
            "expectedRevision": 1,
            "requestId": "refresh-bad",
            "assetId": "controller_board",
            "format": "kicad_pcb",
        }
    )

    assert failed["ok"] is False
    assert failed["error"]["code"] == "IMPORT_INVALID_MECHANICS"
    assert service.revision == 1
    assert store.current_revision == 1
    assert store.scene(1) == first


def _add_plate(service):
    service.sketch(
        {
            "action": "create",
            "expectedRevision": 1,
            "requestId": "plate-sketch",
            "sketchId": "plate_profile",
            "plane": {"kind": "origin", "name": "XY"},
            "entities": [
                {
                    "id": "outline",
                    "kind": "rectangle",
                    "center": [0, 0],
                    "width": {"value": 60, "unit": "mm"},
                    "height": {"value": 40, "unit": "mm"},
                    "rotation": {"value": 0, "unit": "deg"},
                }
            ],
        }
    )
    service.feature(
        {
            "action": "create",
            "expectedRevision": 2,
            "requestId": "plate-feature",
            "featureId": "plate",
            "kind": "extrude",
            "parameters": {
                "profile": {"sketchId": "plate_profile", "profileIds": ["outline"]},
                "distance": {"value": 6, "unit": "mm"},
                "operation": "new_body",
                "direction": "normal",
            },
        }
    )


def test_valid_brep_excludes_pcb_reference_shapes(tmp_path, monkeypatch):
    service = DocumentService(tmp_path / "board.coalesce.json")
    service.import_board(import_request(FIXTURES / "features.kicad_pcb"))
    checked = []
    real_analyzer = service_module.BRepCheck_Analyzer

    class RecordingAnalyzer:
        def __init__(self, shape):
            checked.append(shape)
            self._analyzer = real_analyzer(shape)

        def IsValid(self):
            return self._analyzer.IsValid()

    monkeypatch.setattr(service_module, "BRepCheck_Analyzer", RecordingAnalyzer)
    result = service.check({"action": "evaluate"})

    assert result["results"][0]["status"] == "UNKNOWN"
    assert result["results"][0]["evidence"] == {"solidCount": 0}
    assert checked == []

    _add_plate(service)
    checked.clear()
    result = service.check({"action": "evaluate"})

    assert result["results"][0]["status"] == "PASS"
    assert result["results"][0]["evidence"] == {"solidCount": 1}
    assert len(checked) == 1


def test_valid_brep_fails_for_invalid_manufacturing_shape(tmp_path, monkeypatch):
    service = DocumentService(tmp_path / "board.coalesce.json")
    service.import_board(import_request(FIXTURES / "features.kicad_pcb"))
    _add_plate(service)

    class InvalidAnalyzer:
        def __init__(self, shape):
            pass

        def IsValid(self):
            return False

    monkeypatch.setattr(service_module, "BRepCheck_Analyzer", InvalidAnalyzer)
    result = service.check({"action": "evaluate"})

    assert result["results"][0]["status"] == "FAIL"
    assert result["results"][0]["evidence"] == {"solidCount": 1}
