import json
from pathlib import Path

import pytest
from build123d import Face, GeomType
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepCheck import BRepCheck_Analyzer

from coalesce.export import verify_step_fresh
from coalesce.service import DocumentService
from test_pcb_enclosure import FIXTURES, base_request, import_request, quantity, standoffs_request
from test_pcb_lid import lid_request


POINTS = [
    {"name": "front-left", "point": [5, 5]},
    {"name": "front-right", "point": [15, 5]},
    {"name": "rear-left", "point": [5, 8]},
    {"name": "rear-right", "point": [15, 8]},
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
    parts = service.query({"select": {"kind": "parts"}})["parts"]
    assert len(parts) == 3  # PCB reference plus separate base and lid manufacturing parts.
    manufacturing = [part for part in parts if part["role"] == "manufacturing"]
    assert len(manufacturing) == 2
    assert all(part["solidCount"] == 1 for part in manufacturing)
    assert all(BRepCheck_Analyzer(part.shape.wrapped).IsValid() for part in __import__("coalesce.parts", fromlist=["terminal_parts"]).terminal_parts(service._regen))

    pilots = _closure_faces(service, "screw_pilot_wall")
    clearances = _closure_faces(service, "screw_clearance_wall")
    assert len(pilots) == len(clearances) == 4
    assert {face.geom_type for face in [*pilots, *clearances]} == {GeomType.CYLINDER}
    assert sorted(face.radius for face in pilots) == pytest.approx([0.8] * 4)
    assert sorted(face.radius for face in clearances) == pytest.approx([1.2] * 4)
    assert sorted((face.center().X, face.center().Y) for face in pilots) == pytest.approx(
        sorted((face.center().X, face.center().Y) for face in clearances)
    )

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
        ([*POINTS[:3], {"name": "rear-right", "point": [15.5, 8]}], "rear-right"),
    ],
)
def test_invalid_screw_points_fail_atomically_with_point_name(tmp_path, points, point_name):
    service = enclosure(tmp_path)
    response = service.feature(closure_request(revision=3, points=points))
    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_PARAMETER"
    assert response["error"]["point"] == point_name
    assert service.query({"select": {"kind": "revision"}})["revision"] == 3


def test_screw_closure_rejects_boss_that_hits_standoff(tmp_path):
    service = enclosure(tmp_path, mounts=True)
    points = [{"name": "hits-mount", "point": [2, 3]}, *POINTS[:3]]
    response = service.feature(closure_request(revision=4, points=points))
    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_PARAMETER"
    assert response["error"]["point"] == "hits-mount"
    assert response["error"]["conflict"] == "standoff"
    assert service.query({"select": {"kind": "revision"}})["revision"] == 4
