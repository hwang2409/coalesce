import math
import shutil
import time

import anyio
import pytest
from build123d import Face, GeomType, Shape
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepCheck import BRepCheck_Analyzer
from test_mcp_cut_hole_circle import cut, rectangle_sketch
from test_mcp_vertical import run_server
from test_pcb_enclosure import (
    FIXTURES,
    base_request,
    import_request,
    quantity,
    standoffs_request,
)

from coalesce.export import verify_step_fresh
from coalesce.service import DocumentService
from coalesce.viewer import ViewerSceneStore


def lid_request(*, revision=3, request_id="lid", include_lip=True, **changes):
    parameters = {
        "base": {"featureId": "base"},
        "thickness": quantity(2),
        "fitGap": quantity(0.25),
        **changes,
    }
    if include_lip:
        parameters.setdefault(
            "lip", {"depth": quantity(3), "width": quantity(1)}
        )
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": request_id,
        "featureId": "lid",
        "kind": "pcb_lid",
        "parameters": parameters,
    }


def _faces(service, feature, role):
    return [
        item.entity
        for item in service._regen.provenance[feature]
        if item.key.role == role and isinstance(item.entity, Face)
    ]


def _common_volume(first, second):
    common = BRepAlgoAPI_Common(first.wrapped, second.wrapped)
    common.Build()
    assert common.IsDone()
    return Shape.cast(common.Shape()).volume


def _enclosure_service(tmp_path, *, standoffs=True):
    service = DocumentService(tmp_path / "enclosure.coalesce.json")
    service.import_board(import_request(FIXTURES / "features.kicad_pcb"))
    service.feature(base_request())
    if standoffs:
        service.feature(standoffs_request())
    return service


def test_lid_exactly_follows_base_and_has_exact_fit(tmp_path):
    service = _enclosure_service(tmp_path)

    created = service.feature(lid_request())

    assert created["ok"] is True
    base = service._regen.shapes["mounts"]
    lid = service._regen.shapes["lid"]
    assert BRepCheck_Analyzer(lid.wrapped).IsValid()
    [top] = _faces(service, "lid", "top")
    undersides = _faces(service, "lid", "underside")
    inner_walls = _faces(service, "base", "inner_wall")
    lip_outer = _faces(service, "lid", "lip_outer")
    assert top.area == pytest.approx(200 + 60 * 3 + math.pi * 3**2, rel=1e-7)
    assert all(
        min(face.distance_to(wall) for wall in inner_walls)
        == pytest.approx(0.25, abs=1e-6)
        for face in lip_outer
    )
    assert any(face.geom_type is GeomType.CYLINDER for face in lip_outer)
    assert min(
        underside.distance_to(rim)
        for underside in undersides
        for rim in _faces(service, "base", "rim")
    ) == pytest.approx(0, abs=1e-7)
    assert _common_volume(base, lid) == pytest.approx(0, abs=1e-7)
    lip_bottom = min(face.bounding_box().min.Z for face in lip_outer)
    board_top = service._regen.shapes["controller_board"].bounding_box().max.Z
    assert lip_bottom - board_top == pytest.approx(1)
    assert {
        item.key.source
        for item in service._regen.provenance["lid"]
        if isinstance(item.entity, Face)
    } == {"base"}
    assert _faces(service, "lid", "lip_inner")
    assert _faces(service, "lid", "edge")

    outer_area = 200 + 60 * 3 + math.pi * 3**2
    lip_outer_area = 200 + 60 * 0.75 + math.pi * 0.75**2
    lip_inner_area = (20 - 0.5) * (10 - 0.5)
    assert lid.volume == pytest.approx(
        outer_area * 2 + (lip_outer_area - lip_inner_area) * 3,
        rel=1e-7,
    )
    base_volume = outer_area * 8.2 - (200 + 60 + math.pi) * 6.2
    standoff_volume = 2 * math.pi * (2.5**2 - 0.8**2)
    assert base.volume == pytest.approx(base_volume + standoff_volume, rel=1e-7)


def test_lid_without_lip_is_a_valid_separate_slab(tmp_path):
    service = _enclosure_service(tmp_path, standoffs=False)

    assert service.feature(lid_request(revision=2, include_lip=False))["ok"] is True

    lid = service._regen.shapes["lid"]
    assert BRepCheck_Analyzer(lid.wrapped).IsValid()
    assert lid.volume == pytest.approx((200 + 180 + math.pi * 9) * 2, rel=1e-7)
    assert not _faces(service, "lid", "lip_outer")


def test_lid_forks_base_without_consuming_it(tmp_path):
    service = _enclosure_service(tmp_path, standoffs=False)
    assert service.feature(lid_request(revision=2))["ok"] is True
    assert service.sketch(
        rectangle_sketch(
            request_id="wall-cut-sketch",
            revision=3,
            sketch_id="wall_cut_sketch",
            entity_id="slot",
            width=4,
            height=4,
            center=(-2, 0),
        )
    )["ok"] is True

    cut_result = service.feature(
        cut(
            request_id="wall-cut",
            revision=4,
            feature_id="wall_cut",
            target="base",
            tool="wall_cut_sketch",
        )
    )

    assert cut_result["ok"] is True
    assert service._regen.removed_material["wall_cut"] is True
    measured = service.query({"select": {"kind": "measurements"}})
    assert {part["terminalFeatureId"] for part in measured["parts"]} == {
        "wall_cut",
        "lid",
    }


@pytest.mark.parametrize(
    ("changes", "parameter", "distance"),
    [
        ({"fitGap": quantity(0)}, "fitGap", None),
        (
            {"lip": {"depth": quantity(3), "width": quantity(20)}},
            "lip.width",
            None,
        ),
        (
            {"lip": {"depth": quantity(4), "width": quantity(1)}},
            "lip.depth",
            0,
        ),
    ],
)
def test_invalid_lid_parameters_are_structured_and_atomic(
    tmp_path, changes, parameter, distance
):
    service = _enclosure_service(tmp_path)
    before = service.store.path.read_bytes()

    failed = service.feature(lid_request(**changes))

    assert failed["ok"] is False
    assert failed["error"]["code"] == "INVALID_PARAMETER"
    assert failed["error"]["featureId"] == "lid"
    assert failed["error"]["parameter"] == parameter
    if distance is not None:
        assert failed["error"]["measuredDistance"] == pytest.approx(distance)
        assert failed["error"]["unit"] == "mm"
    assert service.revision == 3
    assert service.store.path.read_bytes() == before


def test_restart_refresh_and_base_update_regenerate_lid(tmp_path):
    source = tmp_path / "board.kicad_pcb"
    shutil.copyfile(FIXTURES / "features.kicad_pcb", source)
    document = tmp_path / "refresh.coalesce.json"
    service = DocumentService(document)
    service.import_board(import_request(source))
    service.feature(base_request())
    service.feature(lid_request(revision=2))
    original = service._regen.shapes["lid"].bounding_box()

    restarted = DocumentService(document)
    restarted.query({"select": {"kind": "graph"}})
    assert restarted._regen.shapes["lid"].volume == pytest.approx(
        service._regen.shapes["lid"].volume
    )
    assert [
        node["type"]
        for node in restarted.query({"select": {"kind": "graph"}})["graph"]["nodes"]
    ] == ["BoardImport", "PcbBase", "PcbLid"]

    source.write_text(source.read_text().replace("(end 20 10)", "(end 22 10)"))
    assert service.import_board(
        import_request(source, revision=3, request_id="refresh-board", action="refresh")
    )["ok"] is True
    refreshed = service._regen.shapes["lid"].bounding_box()
    assert refreshed.max.X - original.max.X == pytest.approx(2)

    assert service.feature(
        {
            "action": "update",
            "expectedRevision": 4,
            "requestId": "higher-base",
            "featureId": "base",
            "kind": "pcb_base",
            "set": {"wallHeightAboveBoard": quantity(6)},
        }
    )["ok"] is True
    assert service._regen.shapes["lid"].bounding_box().min.Z - refreshed.min.Z == pytest.approx(
        2
    )


@pytest.mark.anyio
async def test_mcp_export_and_viewer_publish_two_manufacturing_parts(tmp_path):
    document = tmp_path / "lid.coalesce.json"
    target = tmp_path / "lid.step"
    results = await run_server(
        document,
        [
            ("import", import_request(FIXTURES / "features.kicad_pcb")),
            ("feature", base_request()),
            ("feature", standoffs_request()),
            ("feature", lid_request()),
            (
                "check",
                {
                    "action": "evaluate",
                    "requirements": [{"id": "valid", "kind": "valid_brep"}],
                },
            ),
            ("query", {"select": {"kind": "measurements"}}),
            ("export", {"expectedRevision": 4, "path": str(target)}),
        ],
    )
    checked, measured, exported = results[-3:]
    assert checked["results"][0]["status"] == "PASS"
    assert checked["results"][0]["evidence"]["solidCount"] == 2
    assert len(measured["parts"]) == 2
    assert {part["terminalFeatureId"] for part in measured["parts"]} == {
        "mounts",
        "lid",
    }
    assert exported["ok"] is True
    assert exported["validation"]["reimport"]["partCount"] == 2
    assert exported["validation"]["reimport"]["solidCount"] == 2
    assert verify_step_fresh(target)["partCount"] == 2

    store = ViewerSceneStore()
    service = DocumentService(document, scene_store=store)
    deadline = time.monotonic() + 5
    scene = store.scene(service.revision)
    while scene is None and time.monotonic() < deadline:
        await anyio.sleep(0.02)
        scene = store.scene(service.revision)
    assert scene is not None
    manufacturing = [part for part in scene["parts"] if part["role"] == "manufacturing"]
    assert len(manufacturing) == 2
    assert {part["terminalFeatureId"] for part in manufacturing} == {"mounts", "lid"}
