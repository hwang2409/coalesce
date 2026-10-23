from pathlib import Path

import pytest
from build123d import Face, GeomType
from OCP.BRepCheck import BRepCheck_Analyzer
from test_mcp_vertical import run_server
from test_pcb_enclosure import FIXTURES, base_request, import_request, quantity, standoffs_request
from test_pcb_lid import lid_request

from coalesce.service import DocumentService

POINTS = [
    {"name": "front-left", "point": [7, 3]},
    {"name": "front-right", "point": [13, 3]},
    {"name": "rear-left", "point": [7, 8]},
    {"name": "rear-right", "point": [13, 8]},
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
    assert service.feature(base_request())["ok"]
    if mounts:
        assert service.feature(standoffs_request())["ok"]
    assert service.feature(lid_request(revision=3 if mounts else 2, include_lip=False))["ok"]
    return service


def _closure_faces(service, role):
    return [
        item.entity
        for item in service._regen.provenance["closure"]
        if item.key.role == role and isinstance(item.entity, Face)
    ]


def test_screw_closure_builds_two_exact_parts_persists_and_checks(tmp_path):
    document = tmp_path / "closure.coalesce.json"
    service = enclosure(tmp_path)

    response = service.feature(closure_request(revision=3))

    assert response["ok"] is True
    measured = service.query({"select": {"kind": "measurements"}})
    assert measured["solidCount"] == 2
    assert len(measured["parts"]) == 2
    from coalesce.parts import terminal_parts

    assert all(
        BRepCheck_Analyzer(part.shape.wrapped).IsValid()
        for part in terminal_parts(service._regen)
    )

    pilots = _closure_faces(service, "screw_pilot_wall")
    clearances = _closure_faces(service, "screw_clearance_wall")
    assert len(pilots) == len(clearances) == 4
    assert {face.geom_type for face in [*pilots, *clearances]} == {GeomType.CYLINDER}
    assert sorted(face.radius for face in pilots) == pytest.approx([0.8] * 4)
    assert sorted(face.radius for face in clearances) == pytest.approx([1.2] * 4)
    def axes(faces):
        return sorted(
            ((face.bounding_box().min.X + face.bounding_box().max.X) / 2,
             (face.bounding_box().min.Y + face.bounding_box().max.Y) / 2)
            for face in faces
        )

    assert axes(pilots) == pytest.approx(axes(clearances))

    checked = service.check({
        "action": "evaluate",
        "base": "base",
        "requirements": [{"id": "closure-exact", "kind": "screw_closure"}],
    })
    assert checked["gate"] == "PASS"
    evidence = checked["results"][0]["measured"]
    assert evidence["count"] == 4
    assert evidence["names"] == [point["name"] for point in POINTS]
    assert evidence["coaxial"] is True
    assert evidence["minimumResidualWall"]["value"] == pytest.approx(1.2)
    assert evidence["interference"]["value"] == pytest.approx(0)

    restarted = DocumentService(document)
    assert restarted.check({
        "action": "evaluate", "base": "base",
        "requirements": [{"id": "closure-exact", "kind": "screw_closure"}],
    })["gate"] == "PASS"
    assert [item.key.source for item in restarted._regen.provenance["closure"] if item.key.role == "screw_pilot_wall"] == [point["name"] for point in POINTS]


@pytest.mark.parametrize(
    ("points", "point_name"),
    [
        ([*POINTS[:3], {"name": "bad", "point": [100, 100]}], "bad"),
        ([*POINTS[:3], {"name": "rear-right", "point": [13, 6]}], "rear-right"),
    ],
)
def test_invalid_screw_points_fail_atomically_with_point_name(tmp_path, points, point_name):
    service = enclosure(tmp_path)
    response = service.feature(closure_request(revision=3, points=points))
    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_PARAMETER"
    assert response["error"]["point"] == point_name
    assert service.query({"select": {"kind": "revision"}})["revision"] == 3


@pytest.mark.anyio
async def test_real_stdio_screw_closure_refresh_and_fresh_process_preflight(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1] / "src"))
    document = tmp_path / "stdio-closure.coalesce.json"
    imported, base, lid, closure, checked, refreshed, rechecked, preflight = await run_server(
        document,
        [
            ("import", import_request(FIXTURES / "features.kicad_pcb")),
            ("feature", base_request()),
            ("feature", lid_request(revision=2, include_lip=False)),
            ("feature", closure_request(revision=3)),
            ("check", {
                "action": "evaluate", "base": "base",
                "requirements": [{"id": "closure", "kind": "screw_closure"}],
            }),
            ("import", import_request(
                FIXTURES / "features-moved.kicad_pcb",
                revision=4,
                request_id="refresh-board",
                action="refresh",
            )),
            ("check", {
                "action": "evaluate", "base": "base",
                "requirements": [{"id": "closure", "kind": "screw_closure"}],
            }),
            ("export", {
                "action": "preflight",
                "expectedRevision": 5,
                "base": "base",
                "requirements": [
                    {"id": "valid", "kind": "valid_brep"},
                    {"id": "closure", "kind": "screw_closure"},
                ],
            }),
        ],
    )
    assert [imported["revision"], base["revision"], lid["revision"], closure["revision"]] == [1, 2, 3, 4]
    assert checked["ok"], checked
    assert rechecked["ok"], rechecked
    assert checked["gate"] == rechecked["gate"] == "PASS"
    assert refreshed["revision"] == 5
    assert rechecked["results"][0]["measured"]["names"] == [
        point["name"] for point in POINTS
    ]
    assert preflight["ready"] is True
    assert len(preflight["parts"]) == 2
    assert all(part["validation"]["freshProcess"] is True for part in preflight["parts"])


def test_screw_requirement_fails_noncoaxial_evidence():
    from coalesce.requirements import evaluate

    inspection = {
        "base": {"partId": "base/outline"},
        "lids": [{"partId": "lid/outline"}],
        "screwClosures": [{
            "count": 4,
            "names": ["a", "b", "c", "d"],
            "coaxial": False,
            "minimumResidualWall": quantity(1.2),
            "interference": {"value": 0, "unit": "mm^3"},
        }],
    }
    result = evaluate(
        [{"id": "closure", "kind": "screw_closure"}], inspection, revision=5
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
