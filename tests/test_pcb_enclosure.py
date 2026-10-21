import json
import math
import shutil
from pathlib import Path

import pytest
from OCP.BRepCheck import BRepCheck_Analyzer
from test_mcp_vertical import call_server_tool, run_server

from coalesce.service import DocumentService

FIXTURES = Path(__file__).parent / "fixtures/kicad"
FEATURE_HOLES = [
    "hole-03e3c0594f5f1b105237c4c0",
    "hole-89c529cbad8407f9ccb81676",
]
ALL_MOUNTING_HOLES = [
    *FEATURE_HOLES,
    "hole-9ddb205ba1bc297f95347452",
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
        outer_area = 26 * 16
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

    base_volume = (26 * 16) * 8.2 - (
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


def test_all_mounting_selects_every_mounting_hole_including_npth(tmp_path):
    service = DocumentService(tmp_path / "all.coalesce.json")
    service.import_board(import_request(FIXTURES / "features.kicad_pcb"))
    service.feature(base_request())
    request = standoffs_request(selection={"mode": "all_mounting"})

    assert service.feature(request)["ok"] is True
    assert {
        item.key.source
        for item in service._regen.provenance["mounts"]
        if item.key.role == "pilot_wall"
    } == set(ALL_MOUNTING_HOLES)


def test_all_mounting_standoffs_pass_mounting_requirement(tmp_path):
    service = DocumentService(tmp_path / "all-check.coalesce.json")
    service.import_board(import_request(FIXTURES / "features.kicad_pcb"))
    service.feature(base_request())
    created = service.feature(
        standoffs_request(selection={"mode": "all_mounting"})
    )

    checked = service.check(
        {
            "action": "evaluate",
            "base": "base",
            "requirements": [{"id": "mounts", "kind": "mounting_holes"}],
        }
    )

    assert created["ok"] is True
    assert checked["results"][0]["status"] == "PASS"
    assert checked["results"][0]["measured"]["coveredHoleIds"] == ALL_MOUNTING_HOLES


@pytest.mark.anyio
async def test_removed_minimum_diameter_explains_all_mounting_replacement(tmp_path):
    service = DocumentService(tmp_path / "minimum.coalesce.json")
    service.import_board(import_request(FIXTURES / "features.kicad_pcb"))
    service.feature(base_request())

    result = await call_server_tool(
        service,
        "feature",
        standoffs_request(
            selection={"mode": "all_mounting", "minimumDiameter": quantity(2)}
        ),
    )

    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert "minimumDiameter was removed" in result["error"]["message"]
    assert "MountingHole:*" in result["error"]["message"]


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
    assert failed["error"]["featureId"] == "mounts"
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
    assert failed["error"]["featureId"] == "base"
    assert failed["error"]["parameter"] == "clearance"
    assert service.revision == 1
    assert document.read_bytes() == before


def _remove_mounting_footprint(source: Path, library_id: str) -> None:
    text = source.read_text()
    start = text.index(f' (footprint "{library_id}"')
    end = text.index(" (footprint ", start + 1)
    source.write_text(text[:start] + text[end:])


def _add_mounting_footprint(source: Path) -> None:
    text = source.read_text()
    footprint = ''' (footprint "MountingHole:M2" (layer "F.Cu") (uuid "added-id") (at 10 1.5)
  (property "Reference" "H2") (property "Value" "M2")
  (pad "" thru_hole circle (at 0 0) (drill 2.2) (layers "*.Cu" "*.Mask")))
'''
    source.write_text(text.replace(' (footprint "Connector:Front"', footprint + ' (footprint "Connector:Front"'))


def _dynamic_enclosure(tmp_path: Path, name: str = "dynamic") -> tuple[DocumentService, Path, Path]:
    source = tmp_path / f"{name}.kicad_pcb"
    shutil.copyfile(FIXTURES / "features.kicad_pcb", source)
    document = tmp_path / f"{name}.coalesce.json"
    service = DocumentService(document)
    assert service.import_board(import_request(source))["ok"]
    assert service.feature(base_request())["ok"]
    assert service.feature(
        standoffs_request(selection={"mode": "all_mounting"})
    )["ok"]
    return service, source, document


def _pilot_hole_ids(service: DocumentService) -> set[str]:
    return {
        item.key.source
        for item in service._regen.provenance["mounts"]
        if item.key.role == "pilot_wall"
    }


def test_refresh_can_replace_source_path_and_keep_import_ids(tmp_path):
    document = tmp_path / "refresh-path.coalesce.json"
    service = DocumentService(document)
    service.import_board(import_request(FIXTURES / "features.kicad_pcb"))
    before = service._document.graph.nodes["controller_board"]

    moved = service.import_board({
        **import_request(
            FIXTURES / "features-moved.kicad_pcb",
            revision=1,
            request_id="refresh-new-path",
            action="refresh",
        ),
        "path": str((FIXTURES / "features-moved.kicad_pcb").resolve()),
    })

    assert moved["ok"] is True
    after = service._document.graph.nodes["controller_board"]
    assert service.revision == 2
    assert after.source_path.endswith("features-moved.kicad_pcb")
    assert {item["id"] for item in before.mechanics["holes"]} == {
        item["id"] for item in after.mechanics["holes"]
    }
    assert before.source_digest != after.source_digest


def test_refresh_removes_dynamic_mounting_hole_and_reports_selection_change(tmp_path):
    service, source, _ = _dynamic_enclosure(tmp_path, "remove")
    removed = ALL_MOUNTING_HOLES[0]
    _remove_mounting_footprint(source, "MountingHole:M2")

    refreshed = service.import_board(
        import_request(source, revision=3, request_id="refresh-remove", action="refresh")
    )

    assert refreshed["ok"] is True
    assert _pilot_hole_ids(service) == set(ALL_MOUNTING_HOLES) - {removed}
    assert refreshed["selectionChanges"] == [{
        "featureId": "mounts",
        "selector": "all_mounting",
        "holesAdded": [],
        "holesRemoved": [removed],
    }]


def test_refresh_adds_dynamic_mounting_hole_and_reports_selection_change(tmp_path):
    service, source, _ = _dynamic_enclosure(tmp_path, "add")
    _add_mounting_footprint(source)

    refreshed = service.import_board(
        import_request(source, revision=3, request_id="refresh-add", action="refresh")
    )

    assert refreshed["ok"] is True
    added = next(
        hole["id"]
        for hole in service._document.graph.nodes["controller_board"].mechanics["holes"]
        if hole.get("reference") == "H2"
    )
    assert refreshed["ok"] is True
    assert _pilot_hole_ids(service) == {*ALL_MOUNTING_HOLES, added}
    assert refreshed["selectionChanges"] == [{
        "featureId": "mounts",
        "selector": "all_mounting",
        "holesAdded": [added],
        "holesRemoved": [],
    }]


def test_refresh_moved_board_with_same_ids_has_no_selection_changes(tmp_path):
    service, _, _ = _dynamic_enclosure(tmp_path, "move")

    refreshed = service.import_board({
        **import_request(
            FIXTURES / "features-moved.kicad_pcb",
            revision=3,
            request_id="refresh-move",
            action="refresh",
        ),
        "path": str((FIXTURES / "features-moved.kicad_pcb").resolve()),
    })

    assert refreshed["ok"] is True
    assert refreshed["selectionChanges"] == []
    assert _pilot_hole_ids(service) == set(ALL_MOUNTING_HOLES)


def test_unrelated_upstream_feature_update_has_no_selection_changes(tmp_path):
    service, _, _ = _dynamic_enclosure(tmp_path, "unrelated")

    updated = service.feature({
        "action": "update",
        "expectedRevision": 3,
        "requestId": "base-update",
        "featureId": "base",
        "kind": "pcb_base",
        "set": {"wallHeightAboveBoard": quantity(5)},
    })

    assert updated["ok"] is True
    assert updated["selectionChanges"] == []


def test_old_document_without_selection_snapshots_reports_first_refresh(tmp_path):
    service, source, document = _dynamic_enclosure(tmp_path, "old")
    payload = json.loads(document.read_text())
    payload.pop("selectionHistory", None)
    document.write_text(json.dumps(payload))
    service = DocumentService(document)
    removed = ALL_MOUNTING_HOLES[0]
    _remove_mounting_footprint(source, "MountingHole:M2")

    refreshed = service.import_board(
        import_request(source, revision=3, request_id="refresh-old", action="refresh")
    )

    assert refreshed["selectionChanges"][0]["holesRemoved"] == [removed]


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
    assert failed["error"]["featureId"] == "mounts"
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
    created = service.feature(standoffs_request())
    assert created["selectionChanges"] == []

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
    assert missing["error"]["featureId"] == "mounts"
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
