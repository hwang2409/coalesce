import time
from pathlib import Path

import pytest
from build123d import Face, GeomType
from OCP.BRepCheck import BRepCheck_Analyzer
from test_mcp_cut_hole_circle import hole
from test_mcp_vertical import run_server
from test_pcb_enclosure import FIXTURES, base_request, import_request, quantity, standoffs_request
from test_pcb_lid import lid_request

from coalesce.service import DocumentService

POINTS = [
    {"name": "left", "point": [-2.5, 7]},
    {"name": "right", "point": [22.5, 7]},
    {"name": "front", "point": [5, -2.5]},
    {"name": "rear", "point": [15, 12.5]},
]


def closure_request(*, revision=4, points=POINTS):
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": "close-enclosure",
        "featureId": "closure",
        "kind": "screw_closure",
        "parameters": {
            "base": {"featureId": "base"},
            "lid": {"featureId": "lid"},
            "points": points,
            "basePilotDiameter": quantity(1.6),
            "bossDiameter": quantity(4),
            "lidClearanceDiameter": quantity(2.4),
        },
    }


def enclosure(tmp_path: Path, *, mounts=False) -> DocumentService:
    service = DocumentService(tmp_path / "closure.coalesce.json")
    assert service.import_board(import_request(FIXTURES / "features.kicad_pcb"))["ok"]
    assert service.feature(base_request(clearance=quantity(5)))["ok"]
    if mounts:
        assert service.feature(standoffs_request())["ok"]
    assert service.feature(lid_request(revision=3 if mounts else 2, include_lip=False))["ok"]
    return service


def _wait_for_scene(store, revision):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (scene := store.scene(revision)) is not None:
            return scene
        time.sleep(0.02)
    raise AssertionError("viewer publication timed out")


def _closure_faces(service, role):
    return [
        item.entity
        for item in service._regen.provenance["closure"]
        if item.key.role == role and isinstance(item.entity, Face)
    ]


def test_screw_closure_builds_two_exact_parts_persists_and_checks(tmp_path):
    document = tmp_path / "closure.coalesce.json"
    service = enclosure(tmp_path, mounts=True)

    response = service.feature(closure_request(revision=4))

    assert response["ok"] is True
    measured = service.query({"select": {"kind": "measurements"}})
    assert measured["solidCount"] == 2
    assert len(measured["parts"]) == 2
    from coalesce.parts import terminal_parts

    manufacturing = terminal_parts(service._regen, role="manufacturing")
    assert len(manufacturing) == 2
    assert all(len(part.shape.solids()) == 1 for part in manufacturing)
    assert all(BRepCheck_Analyzer(part.shape.wrapped).IsValid() for part in manufacturing)

    pilots = _closure_faces(service, "screw_pilot_wall")
    clearances = _closure_faces(service, "screw_clearance_wall")
    assert len(pilots) == len(clearances) == 4
    assert {face.geom_type for face in [*pilots, *clearances]} == {GeomType.CYLINDER}
    assert sorted(face.radius for face in pilots) == pytest.approx([0.8] * 4)
    assert sorted(face.radius for face in clearances) == pytest.approx([1.2] * 4)

    def axes(faces):
        return sorted(
            (
                (face.bounding_box().min.X + face.bounding_box().max.X) / 2,
                (face.bounding_box().min.Y + face.bounding_box().max.Y) / 2,
            )
            for face in faces
        )

    assert axes(pilots) == pytest.approx(axes(clearances))

    checked = service.check(
        {
            "action": "evaluate",
            "base": "base",
            "requirements": [
                {"id": "closure-exact", "kind": "screw_closure", "minimumWall": quantity(1)}
            ],
        }
    )
    assert checked["gate"] == "PASS"
    evidence = checked["results"][0]["measured"]
    assert evidence["count"] == 4
    assert evidence["names"] == [point["name"] for point in POINTS]
    assert evidence["coaxial"] is True
    assert evidence["minimumResidualWall"]["value"] == pytest.approx(1.2)
    assert evidence["interference"]["value"] == pytest.approx(0)
    assert checked["results"][0]["threshold"]["minimumWall"] == quantity(1)

    restarted = DocumentService(document)
    assert (
        restarted.check(
            {
                "action": "evaluate",
                "base": "base",
                "requirements": [
                    {"id": "closure-exact", "kind": "screw_closure", "minimumWall": quantity(1)}
                ],
            }
        )["gate"]
        == "PASS"
    )
    assert sorted(
        item.key.source
        for item in restarted._regen.provenance["closure"]
        if item.key.role == "screw_pilot_wall"
    ) == sorted(point["name"] for point in POINTS)


@pytest.mark.parametrize(
    ("point", "conflict"),
    [
        ({"name": "off-floor", "point": [100, 100]}, "floor"),
        ({"name": "through-wall", "point": [-4, 5]}, "wall"),
    ],
)
def test_invalid_screw_points_fail_atomically_with_point_name(tmp_path, point, conflict):
    service = enclosure(tmp_path)
    response = service.feature(closure_request(revision=3, points=[*POINTS[:3], point]))
    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_PARAMETER"
    assert response["error"]["point"] == point["name"]
    assert response["error"]["conflict"] == conflict
    assert service.query({"select": {"kind": "revision"}})["revision"] == 3


def test_overlapping_screw_points_fail_atomically(tmp_path):
    service = enclosure(tmp_path)
    point = {"name": "overlap", "point": [22.5, 8]}
    response = service.feature(closure_request(revision=3, points=[*POINTS[:3], point]))
    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_PARAMETER"
    assert response["error"]["point"] == "overlap"
    assert response["error"]["conflict"] == "right"
    assert service.query({"select": {"kind": "revision"}})["revision"] == 3


@pytest.mark.anyio
async def test_real_stdio_screw_closure_refresh_and_fresh_process_preflight(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1] / "src"))
    document = tmp_path / "stdio-closure.coalesce.json"
    (
        imported,
        base,
        mounts,
        lid,
        closure,
        checked,
        refreshed,
        rechecked,
        preflight,
    ) = await run_server(
        document,
        [
            ("import", import_request(FIXTURES / "features.kicad_pcb")),
            ("feature", base_request(clearance=quantity(5))),
            ("feature", standoffs_request()),
            ("feature", lid_request(revision=3, include_lip=False)),
            ("feature", closure_request(revision=4)),
            (
                "check",
                {
                    "action": "evaluate",
                    "base": "base",
                    "requirements": [
                        {"id": "closure", "kind": "screw_closure", "minimumWall": quantity(1)}
                    ],
                },
            ),
            (
                "import",
                import_request(
                    FIXTURES / "features-moved.kicad_pcb",
                    revision=5,
                    request_id="refresh-board",
                    action="refresh",
                ),
            ),
            (
                "check",
                {
                    "action": "evaluate",
                    "base": "base",
                    "requirements": [
                        {"id": "closure", "kind": "screw_closure", "minimumWall": quantity(1)}
                    ],
                },
            ),
            (
                "export",
                {
                    "action": "preflight",
                    "expectedRevision": 6,
                    "base": "base",
                    "requirements": [
                        {"id": "valid", "kind": "valid_brep"},
                        {"id": "closure", "kind": "screw_closure", "minimumWall": quantity(1)},
                    ],
                },
            ),
        ],
    )
    assert [
        imported["revision"],
        base["revision"],
        mounts["revision"],
        lid["revision"],
        closure["revision"],
    ] == [1, 2, 3, 4, 5]
    assert checked["ok"], checked
    assert rechecked["ok"], rechecked
    assert checked["gate"] == rechecked["gate"] == "PASS"
    assert refreshed["revision"] == 6
    assert rechecked["results"][0]["measured"]["names"] == [point["name"] for point in POINTS]
    assert preflight["ready"] is True
    assert len(preflight["parts"]) == 2
    assert all(part["validation"]["freshProcess"] is True for part in preflight["parts"])


def test_screw_requirement_fails_noncoaxial_evidence():
    from coalesce.requirements import evaluate

    inspection = {
        "base": {"partId": "base/outline"},
        "lids": [{"partId": "lid/outline"}],
        "screwClosures": [
            {
                "count": 4,
                "names": ["a", "b", "c", "d"],
                "coaxial": False,
                "minimumResidualWall": quantity(1.2),
                "interference": {"value": 0, "unit": "mm^3"},
            }
        ],
    }
    result = evaluate(
        [{"id": "closure", "kind": "screw_closure", "minimumWall": quantity(1)}],
        inspection,
        revision=5,
    )
    assert result["gate"] == "FAIL"


def test_screw_closure_rejects_boss_that_hits_standoff(tmp_path):
    service = enclosure(tmp_path, mounts=True)
    points = [{"name": "hits-mount", "point": [2, 3]}, *POINTS[:3]]
    response = service.feature(closure_request(revision=4, points=points))
    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_PARAMETER"
    assert response["error"]["point"] == "hits-mount"
    assert response["error"]["conflict"] == "standoff"
    assert service.query({"select": {"kind": "revision"}})["revision"] == 4


def test_screw_closure_rejects_board_collision_atomically(tmp_path):
    service = enclosure(tmp_path, mounts=True)
    response = service.feature(
        closure_request(
            revision=4,
            points=[{"name": "hits-board", "point": [10, 5]}, *POINTS[:3]],
        )
    )
    assert response["ok"] is False
    assert response["error"]["point"] == "hits-board"
    assert response["error"]["conflict"] == "board"
    assert service.query({"select": {"kind": "revision"}})["revision"] == 4


def test_refresh_that_moves_board_under_boss_fails_atomically(tmp_path):
    source = tmp_path / "moving.kicad_pcb"
    source.write_text((FIXTURES / "features.kicad_pcb").read_text())
    service = DocumentService(tmp_path / "moving.coalesce.json")
    assert service.import_board(import_request(source))["ok"]
    assert service.feature(base_request(clearance=quantity(5)))["ok"]
    assert service.feature(standoffs_request())["ok"]
    assert service.feature(lid_request(revision=3, include_lip=False))["ok"]
    assert service.feature(closure_request(revision=4))["ok"]
    source.write_text(
        source.read_text().replace(
            "(gr_rect (start 0 0) (end 20 10)",
            "(gr_rect (start -4 0) (end 20 10)",
        )
    )

    response = service.import_board(
        import_request(source, revision=5, request_id="move-board", action="refresh")
    )

    assert response["ok"] is False
    assert response["error"]["point"] in {"left"}
    assert response["error"]["conflict"] == "board"
    assert service.query({"select": {"kind": "revision"}})["revision"] == 5


def test_later_holes_replace_only_the_requested_closure_body(tmp_path):
    document = tmp_path / "closure.coalesce.json"
    service = enclosure(tmp_path, mounts=True)
    assert service.feature(closure_request(revision=4))["ok"]
    from coalesce.parts import terminal_parts

    def volumes(current):
        if current._regen is None:
            current.query({"select": {"kind": "revision"}})
        return {
            part.source_body.feature: part.shape.volume for part in terminal_parts(current._regen)
        }

    before = volumes(service)
    assert service.feature(
        hole(
            request_id="base-hole",
            revision=5,
            feature_id="base-hole",
            target="base",
            position=(10, 5),
            diameter=1,
        )
    )["ok"]
    after_base = volumes(service)
    removed = pytest.approx(3.141592653589793 / 2, abs=1e-6)
    assert before["base"] - after_base["base"] == removed
    assert after_base["lid"] == pytest.approx(before["lid"], abs=1e-7)

    assert service.feature(
        hole(
            request_id="lid-hole",
            revision=6,
            feature_id="lid-hole",
            target="lid",
            position=(10, 5),
            diameter=1,
        )
    )["ok"]
    after_lid = volumes(service)
    assert after_lid["base"] == pytest.approx(after_base["base"], abs=1e-7)
    assert after_base["lid"] - after_lid["lid"] == removed

    restarted = DocumentService(document)
    assert volumes(restarted) == pytest.approx(after_lid, abs=1e-7)
    refreshed = restarted.import_board(
        import_request(
            FIXTURES / "features.kicad_pcb",
            revision=7,
            request_id="refresh-after-holes",
            action="refresh",
        )
    )
    assert refreshed["ok"] is True
    assert volumes(restarted) == pytest.approx(after_lid, abs=1e-7)


def test_screw_closure_update_keeps_identity_names_and_repair_history(tmp_path):
    from coalesce.viewer import ViewerSceneStore

    store = ViewerSceneStore()
    service = DocumentService(tmp_path / "update.coalesce.json", scene_store=store)
    assert service.import_board(import_request(FIXTURES / "features.kicad_pcb"))["ok"]
    assert service.feature(base_request(clearance=quantity(5)))["ok"]
    assert service.feature(standoffs_request())["ok"]
    assert service.feature(lid_request(revision=3, include_lip=False))["ok"]
    assert service.feature(closure_request(revision=4))["ok"]
    created_scene = _wait_for_scene(store, 5)
    assert created_scene["change"]["summary"] == (
        "added screw closure: 4 points, pilot 1.6 mm, boss 4 mm, clearance 2.4 mm"
    )
    closure_row = next(
        row for row in created_scene["fit"][0]["rows"] if row["id"] == "screw_closure"
    )
    assert closure_row["status"] == "PASS"
    assert closure_row["value"] == "4 points, 1.2 mm minimum wall, coaxial, no interference"
    failed = service.feature(
        {
            "action": "update",
            "expectedRevision": 5,
            "requestId": "bad-closure",
            "featureId": "closure",
            "kind": "screw_closure",
            "set": {"bossDiameter": quantity(1)},
        }
    )
    assert failed["ok"] is False
    updated_points = [
        {"name": "left", "point": [-2.8, 7]},
        {"name": "right", "point": [22.8, 7]},
        {"name": "front", "point": [5, -2.8]},
        {"name": "new-rear", "point": [15, 12.8]},
    ]
    response = service.feature(
        {
            "action": "update",
            "expectedRevision": 5,
            "requestId": "edit-closure",
            "featureId": "closure",
            "kind": "screw_closure",
            "set": {
                "points": updated_points,
                "basePilotDiameter": quantity(1.8),
                "bossDiameter": quantity(4.4),
                "lidClearanceDiameter": quantity(2.8),
            },
        }
    )
    assert response["ok"] is True
    node = service._regen.graph.nodes["closure"]
    assert node.id == "closure"
    assert [point.name for point in node.points] == [point["name"] for point in updated_points]
    scene = _wait_for_scene(store, 6)
    assert scene["change"]["summary"] == (
        "updated screw closure: 4 points, pilot 1.8 mm, boss 4.4 mm, clearance 2.8 mm"
    )
    assert scene["repairHistory"][0]["featureId"] == "closure"
    assert set(scene["repairHistory"][0]["fields"]) == {"bossDiameter"}
    sources = {
        item.key.source
        for item in service._regen.provenance["closure"]
        if item.key.role == "screw_pilot_wall"
    }
    assert "rear" not in sources
    assert "new-rear" in sources


def test_screw_requirement_uses_explicit_minimum_wall():
    from coalesce.requirements import evaluate

    inspection = {
        "base": {"partId": "base/outline"},
        "lids": [{"partId": "lid/outline"}],
        "screwClosures": [
            {
                "count": 2,
                "names": ["a", "b"],
                "coaxial": True,
                "minimumResidualWall": quantity(0.001),
                "interference": {"value": 0, "unit": "mm^3"},
            }
        ],
    }
    result = evaluate(
        [
            {
                "id": "closure",
                "kind": "screw_closure",
                "minimumWall": quantity(0.8),
            }
        ],
        inspection,
        revision=5,
    )
    assert result["gate"] == "FAIL"
    assert result["results"][0]["threshold"]["minimumWall"] == quantity(0.8)
