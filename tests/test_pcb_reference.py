import math
import shutil
import time
from pathlib import Path

import pytest
from OCP.BRepCheck import BRepCheck_Analyzer

from coalesce.references import Cardinality, Reference, Status
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
    assert len({item.key.source for item in candidates if item.key.role == "cutout"}) == cutouts
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
    assert segment.status is Status.OK
    assert len(segment.candidates) == segment_count

    for hole in mechanics["holes"]:
        resolution = service._regen.resolve(
            Reference("controller_board", "hole", source=hole["id"])
        )
        assert resolution.status is Status.OK
        assert len(resolution.candidates) == 1


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
    assert failed["error"]["code"] == "IMPORT_FAILED"
    assert service.revision == 1
    assert store.current_revision == 1
    assert store.scene(1) == first
