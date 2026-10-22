import copy
import json
import math
import shutil
from pathlib import Path

import pytest
from OCP.BRepCheck import BRepCheck_Analyzer
from test_mcp_vertical import call_server_tool, run_server

from coalesce.refresh_delta import format_summary, refresh_delta
from coalesce.service import DocumentService
from coalesce.viewer import ViewerSceneStore

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


def _scene_summary(store: ViewerSceneStore, revision: int) -> str:
    while store.current_revision != revision:
        version, _, _ = store.snapshot()
        assert store.wait_for_change(version, 10)
    return store.scene(revision)["change"]["summary"]


def _refresh_enclosure(
    tmp_path: Path, name: str, *, port: bool = False, stable_pad_ids: bool = False
):
    source = tmp_path / f"{name}.kicad_pcb"
    board_text = (FIXTURES / "features.kicad_pcb").read_text()
    if stable_pad_ids:
        board_text = board_text.replace(
            '(pad "" thru_hole circle (at 0 0) (drill 2.2)',
            '(pad "" thru_hole circle (at 0 0) (uuid "m2-pad") (drill 2.2)',
        )
    source.write_text(board_text)
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / f"{name}.coalesce.json", scene_store=store)
    assert service.import_board(import_request(source))["ok"]
    assert service.feature(base_request())["ok"]
    assert service.feature(standoffs_request(selection={"mode": "all_mounting"}))["ok"]
    if port:
        assert service.feature({
            "action": "create",
            "expectedRevision": 3,
            "requestId": "port",
            "featureId": "port",
            "kind": "port_cutout",
            "parameters": {
                "base": {"featureId": "base"},
                "footprint": "J1",
                "wall": "left",
                "clearance": quantity(0.5),
                "envelope": {
                    "source": "courtyard",
                    "height": quantity(2),
                    "bottomOffset": quantity(1),
                },
            },
        })["ok"]
    return service, source, store


def _refresh_variant(service: DocumentService, source: Path, revision: int) -> dict:
    return service.import_board({
        **import_request(source, revision=revision, request_id="refresh", action="refresh"),
        "path": str(source.resolve()),
    })


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
    assert _pilot_hole_ids(service) == {*ALL_MOUNTING_HOLES, added}
    assert refreshed["selectionChanges"] == [{
        "featureId": "mounts",
        "selector": "all_mounting",
        "holesAdded": [added],
        "holesRemoved": [],
    }]


def test_refresh_delta_reports_positions_distances_and_dependents(tmp_path):
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / "delta.coalesce.json", scene_store=store)
    assert service.import_board(import_request(FIXTURES / "features.kicad_pcb"))["ok"]
    assert service.feature(base_request())["ok"]
    assert service.feature(standoffs_request(selection={"mode": "all_mounting"}))["ok"]
    port = {
        "action": "create",
        "expectedRevision": 3,
        "requestId": "port",
        "featureId": "port",
        "kind": "port_cutout",
        "parameters": {
            "base": {"featureId": "base"},
            "footprint": "J1",
            "wall": "left",
            "clearance": quantity(0.5),
            "envelope": {"source": "courtyard", "height": quantity(2), "bottomOffset": quantity(1)},
        },
    }
    assert service.feature(port)["ok"] is True
    refreshed = service.import_board({
        **import_request(FIXTURES / "features-moved.kicad_pcb", revision=4, request_id="delta", action="refresh"),
        "path": str((FIXTURES / "features-moved.kicad_pcb").resolve()),
    })

    assert refreshed["ok"] is True
    delta = refreshed["boardChanges"]
    footprints = {item["reference"]: item for item in delta["footprints"]}
    assert footprints["J1"]["oldPosition"] == pytest.approx([1, 5], abs=1e-6)
    assert footprints["J1"]["newPosition"] == pytest.approx([3, 5], abs=1e-6)
    assert footprints["J1"]["distance"] == pytest.approx(2, abs=1e-6)
    assert len(delta["mountingHoles"]) == 3
    assert all(item["distance"] > 0 for item in delta["mountingHoles"])
    assert delta["dependentFeatures"] == ["standoffs", "port cutout"]


def test_identical_refresh_has_empty_delta(tmp_path):
    service = DocumentService(tmp_path / "identical.coalesce.json")
    service.import_board(import_request(FIXTURES / "features.kicad_pcb"))
    refreshed = service.import_board(import_request(FIXTURES / "features.kicad_pcb", revision=1, request_id="same", action="refresh"))
    assert refreshed["boardChanges"] == {
        "mountingHoles": [], "footprints": [], "outlineChanged": False, "dependentFeatures": []
    }


def test_refresh_j1_only_reports_port_cutout(tmp_path):
    service, source, store = _refresh_enclosure(tmp_path, "j1-only", port=True)
    source.write_text(source.read_text().replace("(at 1 5)", "(at 1 6)"))

    refreshed = _refresh_variant(service, source, revision=4)

    assert refreshed["boardChanges"] == {
        "mountingHoles": [],
        "footprints": [{
            "id": "front-id",
            "reference": "J1",
            "oldPosition": [1.0, 5.0],
            "newPosition": [1.0, 6.0],
            "distance": 1.0,
            "change": "moved",
        }],
        "outlineChanged": False,
        "dependentFeatures": ["port cutout"],
    }
    assert _scene_summary(store, 5) == "refreshed board: J1 moved 1.0 mm, port cutout updated"


def test_refresh_removed_anonymous_hole_reports_standoffs(tmp_path):
    service, source, store = _refresh_enclosure(tmp_path, "removed-hole")
    _remove_mounting_footprint(source, "MountingHole:M2")

    refreshed = _refresh_variant(service, source, revision=3)

    assert refreshed["boardChanges"] == {
        "mountingHoles": [{
            "id": FEATURE_HOLES[0],
            "reference": None,
            "oldPosition": [2.0, 3.0],
            "newPosition": None,
            "distance": None,
            "change": "removed",
        }],
        "footprints": [],
        "outlineChanged": False,
        "dependentFeatures": ["standoffs"],
    }
    assert _scene_summary(store, 4) == "refreshed board: 1 mounting hole removed, standoffs updated"


def test_refresh_added_mounting_hole_reports_standoffs(tmp_path):
    service, source, store = _refresh_enclosure(tmp_path, "added-hole")
    _add_mounting_footprint(source)

    refreshed = _refresh_variant(service, source, revision=3)

    assert refreshed["boardChanges"] == {
        "mountingHoles": [{
            "id": "hole-db8e1452adbd1eeb02fb49c6",
            "reference": "H2",
            "oldPosition": None,
            "newPosition": [10.0, 1.5],
            "distance": None,
            "change": "added",
        }],
        "footprints": [],
        "outlineChanged": False,
        "dependentFeatures": ["standoffs"],
    }
    assert _scene_summary(store, 4) == "refreshed board: H2 added at (10.0, 1.5), standoffs updated"


def test_refresh_float_noise_below_tolerance_is_not_a_move(tmp_path):
    service, source, store = _refresh_enclosure(tmp_path, "noise")
    source.write_text(source.read_text().replace("(at 2 3)", "(at 2.0000004 3)"))

    refreshed = _refresh_variant(service, source, revision=3)

    assert refreshed["boardChanges"] == {
        "mountingHoles": [],
        "footprints": [],
        "outlineChanged": False,
        "dependentFeatures": [],
    }
    assert _scene_summary(store, 4) == "refreshed board: source changed"


def test_refresh_symmetric_hole_move_reports_standoffs(tmp_path):
    service, source, store = _refresh_enclosure(tmp_path, "symmetric")
    changed = source.read_text().replace("(at 2 3)", "(at 3 3)")
    source.write_text(changed.replace("(at 18 3)", "(at 17 3)"))

    refreshed = _refresh_variant(service, source, revision=3)

    assert refreshed["boardChanges"]["dependentFeatures"] == ["standoffs"]
    assert _scene_summary(store, 4) == "refreshed board: 2 mounting holes moved, standoffs updated"


def test_refresh_drill_change_reports_hole_and_standoffs(tmp_path):
    service, source, store = _refresh_enclosure(tmp_path, "drill", stable_pad_ids=True)
    changed_id = next(
        hole["id"]
        for hole in service._document.graph.nodes["controller_board"].mechanics["holes"]
        if hole["drill"]["width"] == 2.2
    )
    source.write_text(source.read_text().replace("(drill 2.2)", "(drill oval 2.4 2.2)"))

    refreshed = _refresh_variant(service, source, revision=3)

    delta = refreshed["boardChanges"]
    assert [(item["id"], item["change"]) for item in delta["mountingHoles"]] == [
        (changed_id, "changed")
    ]
    assert delta["dependentFeatures"] == ["standoffs"]
    assert _scene_summary(store, 4) == "refreshed board: 1 mounting hole changed, standoffs updated"


def test_refresh_courtyard_change_reports_footprint_and_port_cutout(tmp_path):
    service, source, store = _refresh_enclosure(tmp_path, "courtyard", port=True)
    source.write_text(
        source.read_text().replace(
            '(fp_rect (start -1 -2) (end 1 2)',
            '(fp_rect (start -1 -2) (end 1.5 2)',
        )
    )

    refreshed = _refresh_variant(service, source, revision=4)

    delta = refreshed["boardChanges"]
    assert [(item["id"], item["change"]) for item in delta["footprints"]] == [
        ("front-id", "changed")
    ]
    assert delta["dependentFeatures"] == ["port cutout"]
    assert _scene_summary(store, 5) == "refreshed board: port cutout updated"


def test_refresh_unrelated_footprint_move_reports_no_dependent(tmp_path):
    service, source, store = _refresh_enclosure(tmp_path, "unrelated", port=True)
    before_graph = copy.deepcopy(service._document.graph)
    source.write_text(source.read_text().replace("(at 10 5)", "(at 11 5)"))

    refreshed = _refresh_variant(service, source, revision=4)

    assert refreshed["boardChanges"]["dependentFeatures"] == []
    assert _scene_summary(store, 5) == "refreshed board: J3 moved 1.0 mm"

    class GeometryMustNotBeRead:
        @property
        def shapes(self):
            raise AssertionError("dependent geometry must not be inspected")

    delta = refresh_delta(
        before_graph,
        service._document.graph,
        GeometryMustNotBeRead(),
        GeometryMustNotBeRead(),
        "controller_board",
    )
    assert delta["dependentFeatures"] == []


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
    delta = refreshed["boardChanges"]
    assert delta["outlineChanged"] is False
    assert {item["id"] for item in delta["mountingHoles"]} == set(ALL_MOUNTING_HOLES)
    assert all(item["distance"] > 0 for item in delta["mountingHoles"])
    assert delta["dependentFeatures"] == ["standoffs"]
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


def test_creating_or_deleting_all_mounting_standoffs_is_not_a_selection_change(tmp_path):
    service = DocumentService(tmp_path / "selection.coalesce.json")
    service.import_board(import_request(FIXTURES / "features.kicad_pcb"))
    service.feature(base_request())

    created = service.feature(standoffs_request(selection={"mode": "all_mounting"}))
    assert created["ok"] is True
    assert created["selectionChanges"] == []

    deleted = service.feature(
        {
            "action": "delete",
            "expectedRevision": 3,
            "requestId": "drop-mounts",
            "featureId": "mounts",
        }
    )
    assert deleted["ok"] is True
    assert deleted["selectionChanges"] == []


def _round_base(service: DocumentService, revision: int) -> None:
    assert service.feature({
        "action": "create",
        "expectedRevision": revision,
        "requestId": "round-base",
        "featureId": "base_round",
        "kind": "fillet",
        "parameters": {
            "target": {"featureId": "base"},
            "edges": [{"feature": "base", "role": "outer_vertical", "cardinality": "exactly", "count": 4}],
            "radius": quantity(1),
        },
    })["ok"]


def test_refresh_j1_only_does_not_report_an_unaffected_fillet(tmp_path):
    service, source, store = _refresh_enclosure(tmp_path, "j1-fillet", port=True)
    _round_base(service, revision=4)
    source.write_text(source.read_text().replace("(at 1 5)", "(at 1 6)"))

    refreshed = _refresh_variant(service, source, revision=5)

    assert refreshed["boardChanges"]["dependentFeatures"] == ["port cutout"]
    assert _scene_summary(store, 6) == "refreshed board: J1 moved 1.0 mm, port cutout updated"


def test_refresh_unrelated_footprint_does_not_report_an_unaffected_fillet(tmp_path):
    service, source, store = _refresh_enclosure(tmp_path, "unrelated-fillet", port=True)
    _round_base(service, revision=4)
    source.write_text(source.read_text().replace("(at 10 5)", "(at 11 5)"))

    refreshed = _refresh_variant(service, source, revision=5)

    assert refreshed["boardChanges"]["dependentFeatures"] == []
    assert _scene_summary(store, 6) == "refreshed board: J3 moved 1.0 mm"


def test_refresh_summary_counts_hole_changes_and_lists_dependents():
    delta = {
        "footprints": [],
        "mountingHoles": [{"change": "added"}, {"change": "added"}, {"change": "removed"}],
        "dependentFeatures": ["standoffs", "port cutout", "fillet"],
    }
    assert format_summary(delta) == (
        "refreshed board: 2 mounting holes added, 1 mounting hole removed, "
        "standoffs, port cutout and fillet updated"
    )


def test_refresh_outline_change_reports_exact_bbox(tmp_path):
    service, source, store = _refresh_enclosure(tmp_path, "outline")
    source.write_text(source.read_text().replace("(end 20 10)", "(end 22 10)"))

    refreshed = _refresh_variant(service, source, revision=3)

    assert refreshed["boardChanges"]["outline"] == {
        "before": {"width": 20.0, "height": 10.0},
        "after": {"width": 22.0, "height": 10.0},
    }
    assert _scene_summary(store, 4) == "refreshed board: outline 20.0 × 10.0 → 22.0 × 10.0 mm, pcb base and standoffs updated"


def test_refresh_summary_names_labeled_removed_hole():
    delta = {
        "footprints": [],
        "mountingHoles": [{"change": "removed", "reference": "H1", "oldPosition": [2.0, 3.0]}],
        "dependentFeatures": [],
    }
    assert format_summary(delta) == "refreshed board: H1 removed"


def test_refresh_summary_truncates_named_holes():
    delta = {
        "footprints": [],
        "mountingHoles": [
            {"change": "added", "reference": f"M{i}", "newPosition": [float(i), 2.0]}
            for i in range(10)
        ],
        "dependentFeatures": [],
    }
    summary = format_summary(delta)
    assert "+N more" not in summary
    assert "+4 more" in summary
    assert len(summary) <= 160 + len("refreshed board: ")


def test_refresh_thickness_change_reports_every_consumer(tmp_path):
    service, source, store = _refresh_enclosure(tmp_path, "thickness", port=True)
    source.write_text(source.read_text().replace("(thickness 1.2)", "(thickness 1.6)"))

    refreshed = _refresh_variant(service, source, revision=4)

    assert refreshed["ok"]
    assert refreshed["boardChanges"]["thickness"] == {"before": 1.2, "after": 1.6}
    assert set(refreshed["boardChanges"]["dependentFeatures"]) == {"pcb base", "standoffs", "port cutout"}
    assert _scene_summary(store, 5) == "refreshed board: board thickness 1.2 → 1.6 mm, pcb base, standoffs and port cutout updated"


def test_refresh_port_follows_its_reference_after_the_footprint_id_changes(tmp_path):
    service, source, _ = _refresh_enclosure(tmp_path, "new-id", port=True)
    source.write_text(
        source.read_text().replace('(uuid "front-id") (at 1 5)', '(uuid "front-id-new") (at 1 6)')
    )

    refreshed = _refresh_variant(service, source, revision=4)

    assert refreshed["ok"], refreshed
    assert "port cutout" in refreshed["boardChanges"]["dependentFeatures"]
    assert service._document.graph.nodes["port"].footprint_id == "front-id-new"


def test_refresh_fails_closed_when_the_port_footprint_is_removed(tmp_path):
    service, source, _ = _refresh_enclosure(tmp_path, "removed", port=True)
    source.write_text(
        source.read_text()
        .replace('(uuid "front-id")', '(uuid "other-id")')
        .replace('(property "Reference" "J1")', '(property "Reference" "J9")')
    )

    refreshed = _refresh_variant(service, source, revision=4)

    assert not refreshed["ok"]
    assert refreshed["error"]["code"] == "MISSING"
    assert service._document.revision == 4
