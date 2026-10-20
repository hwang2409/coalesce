import math
import shutil
from pathlib import Path

import pytest
from OCP.BRepCheck import BRepCheck_Analyzer
from test_mcp_vertical import run_server

from coalesce.service import DocumentService

FIXTURES = Path(__file__).parent / "fixtures/kicad"
FEATURE_HOLES = [
    "hole-03e3c0594f5f1b105237c4c0",
    "hole-89c529cbad8407f9ccb81676",
]


def import_request(path: Path, *, revision=0, request_id="import-board", action="create"):
    request = {
        "action": action,
        "expectedRevision": revision,
        "requestId": request_id,
        "assetId": "controller_board",
        "format": "kicad_pcb",
    }
    if action == "create":
        request["path"] = str(path.resolve())
    return request


def quantity(value):
    return {"value": value, "unit": "mm"}


def base_request(*, revision=1, request_id="base", **changes):
    parameters = {
        "board": {"featureId": "controller_board"},
        "wallThickness": quantity(2),
        "floorThickness": quantity(2),
        "clearance": quantity(1),
        "wallHeightAboveBoard": quantity(4),
        "boardSeatHeight": quantity(1),
        **changes,
    }
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": request_id,
        "featureId": "base",
        "kind": "pcb_base",
        "parameters": parameters,
    }


def standoffs_request(*, revision=2, request_id="standoffs", **changes):
    parameters = {
        "target": {"featureId": "base"},
        "board": {"featureId": "controller_board"},
        "selection": {"mode": "explicit", "holeIds": FEATURE_HOLES},
        "bossDiameter": quantity(5),
        "pilotDiameter": quantity(1.6),
        **changes,
    }
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": request_id,
        "featureId": "mounts",
        "kind": "standoffs",
        "parameters": parameters,
    }


def _candidate(result, role, source=None):
    matches = [
        item
        for item in result.provenance.get("base", []) + result.provenance.get("mounts", [])
        if item.key.role == role and (source is None or item.key.source == source)
    ]
    assert matches
    return matches


@pytest.mark.parametrize(
    ("fixture", "outline_area", "outline_perimeter"),
    [
        ("features", 200, 60),
        ("rounded", 100 - (4 - math.pi), 32 + 2 * math.pi),
    ],
)
def test_pcb_base_offsets_real_outline_and_preserves_exact_dimensions(
    tmp_path, fixture, outline_area, outline_perimeter
):
    service = DocumentService(tmp_path / f"{fixture}.coalesce.json")
    assert service.import_board(import_request(FIXTURES / f"{fixture}.kicad_pcb"))["ok"]

    created = service.feature(base_request())

    assert created["ok"] is True
    base = service._regen.shapes["base"]
    assert BRepCheck_Analyzer(base.wrapped).IsValid()
    floor = _candidate(service._regen, "floor")[0].entity
    assert floor.area == pytest.approx(outline_area + outline_perimeter + math.pi, rel=1e-6)
    box = base.bounding_box()
    assert box.min.Z == pytest.approx(-3)
    assert box.max.Z == pytest.approx(
        service._regen.shapes["controller_board"].bounding_box().max.Z + 4
    )
    assert floor.bounding_box().min.Z == pytest.approx(-1)
    assert floor.bounding_box().max.Z == pytest.approx(-1)
    assert _candidate(service._regen, "inner_wall")
    assert _candidate(service._regen, "outer_wall")
    assert _candidate(service._regen, "rim")

    if fixture == "features":
        assert (box.min.X, box.max.X, box.min.Y, box.max.Y) == pytest.approx((-3, 23, -3, 13))
        floor_box = floor.bounding_box()
        assert floor_box.min.X - box.min.X == pytest.approx(2)
        assert box.max.X - floor_box.max.X == pytest.approx(2)
        assert 0 - floor_box.min.Z == pytest.approx(1)
        outer_area = 200 + 60 * 3 + math.pi * 3**2
        cavity_area = 200 + 60 * 1 + math.pi * 1**2
        expected = outer_area * 8.2 - cavity_area * 6.2
        assert base.volume == pytest.approx(expected, rel=1e-7)


def test_standoffs_follow_hole_ids_and_join_with_exact_pilots(tmp_path):
    document = tmp_path / "enclosure.coalesce.json"
    service = DocumentService(document)
    service.import_board(import_request(FIXTURES / "features.kicad_pcb"))
    service.feature(base_request())

    created = service.feature(standoffs_request())

    assert created["ok"] is True
    shape = service._regen.shapes["mounts"]
    assert BRepCheck_Analyzer(shape.wrapped).IsValid()
    assert len(shape.solids()) == 1
    assert service._regen.joined_material["mounts"] is True
    pilots = [_candidate(service._regen, "pilot_wall", hole)[0].entity for hole in FEATURE_HOLES]
    axes = [
        (
            (face.bounding_box().min.X + face.bounding_box().max.X) / 2,
            (face.bounding_box().min.Y + face.bounding_box().max.Y) / 2,
        )
        for face in pilots
    ]
    assert sorted(axes) == pytest.approx([(2, 3), (18, 3)], abs=1e-6)
    assert [face.radius for face in pilots] == pytest.approx([0.8, 0.8])
    for hole in FEATURE_HOLES:
        assert _candidate(service._regen, "standoff_side", hole)
        assert _candidate(service._regen, "standoff_top", hole)

    base_volume = (200 + 60 * 3 + math.pi * 3**2) * 8.2 - (
        200 + 60 + math.pi
    ) * 6.2
    standoff_volume = 2 * math.pi * (2.5**2 - 0.8**2) * 1
    assert shape.volume == pytest.approx(base_volume + standoff_volume, rel=1e-7)

    restarted = DocumentService(document)
    graph = restarted.query({"select": {"kind": "graph"}})["graph"]
    restarted_shape = restarted._regen.shapes["mounts"]
    assert restarted_shape.volume == pytest.approx(shape.volume)
    assert [node["type"] for node in graph["nodes"]] == [
        "BoardImport",
        "PcbBase",
        "Standoffs",
    ]


def test_all_mounting_selects_all_drills_at_or_above_minimum_dimension(tmp_path):
    service = DocumentService(tmp_path / "all.coalesce.json")
    service.import_board(import_request(FIXTURES / "features.kicad_pcb"))
    service.feature(base_request())
    request = standoffs_request(
        selection={"mode": "all_mounting", "minimumDiameter": quantity(2)}
    )

    assert service.feature(request)["ok"] is True
    assert {
        item.key.source
        for item in service._regen.provenance["mounts"]
        if item.key.role == "pilot_wall"
    } == set(FEATURE_HOLES)


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        (standoffs_request(pilotDiameter=quantity(5)), "pilotDiameter"),
        (standoffs_request(bossDiameter=quantity(7)), "bossDiameter"),
    ],
)
def test_invalid_standoff_parameters_fail_atomically(tmp_path, payload, field):
    document = tmp_path / "invalid.coalesce.json"
    service = DocumentService(document)
    service.import_board(import_request(FIXTURES / "features.kicad_pcb"))
    service.feature(base_request())
    before = document.read_bytes()

    failed = service.feature(payload)

    assert failed["ok"] is False
    assert failed["error"]["code"] == "INVALID_PARAMETER"
    assert failed["error"]["feature"] == "mounts"
    assert failed["error"]["parameter"] == field
    assert service.revision == 2
    assert document.read_bytes() == before


def test_invalid_base_clearance_names_feature_and_parameter_and_is_atomic(tmp_path):
    document = tmp_path / "invalid-base.coalesce.json"
    service = DocumentService(document)
    service.import_board(import_request(FIXTURES / "features.kicad_pcb"))
    before = document.read_bytes()

    failed = service.feature(base_request(clearance=quantity(0)))

    assert failed["ok"] is False
    assert failed["error"]["code"] == "INVALID_PARAMETER"
    assert failed["error"]["feature"] == "base"
    assert failed["error"]["parameter"] == "clearance"
    assert service.revision == 1
    assert document.read_bytes() == before


def test_refresh_that_moves_selected_hole_off_floor_fails_atomically(tmp_path):
    source = tmp_path / "board.kicad_pcb"
    shutil.copyfile(FIXTURES / "features.kicad_pcb", source)
    document = tmp_path / "off-floor.coalesce.json"
    service = DocumentService(document)
    service.import_board(import_request(source))
    service.feature(base_request())
    service.feature(standoffs_request())
    source.write_text(
        source.read_text().replace('(uuid "m2-id") (at 2 3)', '(uuid "m2-id") (at 30 20)')
    )
    before = document.read_bytes()

    failed = service.import_board(
        import_request(source, revision=3, request_id="refresh-off-floor", action="refresh")
    )

    assert failed["ok"] is False
    assert failed["error"]["code"] == "INVALID_PARAMETER"
    assert failed["error"]["feature"] == "mounts"
    assert failed["error"]["parameter"] == "bossDiameter"
    assert service.revision == 3
    assert document.read_bytes() == before


def test_refresh_moves_standoff_and_removed_hole_is_missing_and_atomic(tmp_path):
    source = tmp_path / "board.kicad_pcb"
    shutil.copyfile(FIXTURES / "features.kicad_pcb", source)
    document = tmp_path / "refresh.coalesce.json"
    service = DocumentService(document)
    service.import_board(import_request(source))
    service.feature(base_request())
    service.feature(standoffs_request())

    source.write_text(source.read_text().replace('(uuid "m2-id") (at 2 3)', '(uuid "m2-id") (at 4 4)'))
    moved = service.import_board(
        import_request(source, revision=3, request_id="refresh-moved", action="refresh")
    )
    assert moved["ok"] is True
    pilot = _candidate(service._regen, "pilot_wall", FEATURE_HOLES[0])[0].entity
    pilot_box = pilot.bounding_box()
    assert (
        (pilot_box.min.X + pilot_box.max.X) / 2,
        (pilot_box.min.Y + pilot_box.max.Y) / 2,
    ) == pytest.approx((4, 4), abs=1e-6)

    text = source.read_text()
    start = text.index(' (footprint "MountingHole:M2"')
    end = text.index(' (footprint "MountingHole:M3"')
    source.write_text(text[:start] + text[end:])
    before = document.read_bytes()
    missing = service.import_board(
        import_request(source, revision=4, request_id="refresh-removed", action="refresh")
    )
    assert missing["ok"] is False
    assert missing["error"]["code"] == "MISSING"
    assert missing["error"]["feature"] == "mounts"
    assert missing["error"]["hole"] == FEATURE_HOLES[0]
    assert service.revision == 4
    assert document.read_bytes() == before


@pytest.mark.anyio
async def test_real_stdio_semantic_enclosure_sequence_exports_without_board(tmp_path):
    document = tmp_path / "stdio.coalesce.json"
    target = tmp_path / "enclosure.step"
    imported, base, mounts, checked, measured, exported = await run_server(
        document,
        [
            ("import", import_request(FIXTURES / "features.kicad_pcb")),
            ("feature", base_request()),
            ("feature", standoffs_request()),
            (
                "check",
                {
                    "action": "evaluate",
                    "requirements": [{"id": "valid", "kind": "valid_brep"}],
                },
            ),
            ("query", {"select": {"kind": "measurements"}}),
            (
                "export",
                {"expectedRevision": 3, "path": str(target)},
            ),
        ],
    )

    assert [imported["revision"], base["revision"], mounts["revision"]] == [1, 2, 3]
    assert checked["results"][0]["status"] == "PASS"
    assert checked["results"][0]["measured"] == {"value": True, "unit": "boolean"}
    assert checked["results"][0]["evidence"]["solidCount"] == 1
    assert measured["role"] == "manufacturing"
    assert measured["solidCount"] == 1
    assert measured["parts"][0]["partId"].startswith("base/")
    assert exported["ok"] is True
    assert exported["partIds"] == [measured["parts"][0]["partId"]]
    assert exported["validation"]["reimport"]["partCount"] == 1
    assert exported["validation"]["reimport"]["solidCount"] == 1
    assert target.stat().st_size == exported["byteCount"]


def test_numeric_updates_regenerate_semantic_features(tmp_path):
    service = DocumentService(tmp_path / "update.coalesce.json")
    service.import_board(import_request(FIXTURES / "features.kicad_pcb"))
    service.feature(base_request())
    service.feature(standoffs_request())

    base = service.feature(
        {
            "action": "update",
            "expectedRevision": 3,
            "requestId": "base-update",
            "featureId": "base",
            "kind": "pcb_base",
            "set": {"wallHeightAboveBoard": quantity(6)},
        }
    )
    mounts = service.feature(
        {
            "action": "update",
            "expectedRevision": 4,
            "requestId": "mounts-update",
            "featureId": "mounts",
            "kind": "standoffs",
            "set": {"pilotDiameter": quantity(2)},
        }
    )

    assert base["ok"] is mounts["ok"] is True
    assert service._regen.shapes["mounts"].bounding_box().max.Z == pytest.approx(7.2)
    pilots = [
        item.entity
        for item in service._regen.provenance["mounts"]
        if item.key.role == "pilot_wall"
    ]
    assert [face.radius for face in pilots] == pytest.approx([1, 1])
