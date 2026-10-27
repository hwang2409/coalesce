import math
from pathlib import Path

import pytest
from build123d import import_step
from OCP.BRepCheck import BRepCheck_Analyzer
from test_revolve import PULLEY, polyline_sketch, revolve

import coalesce.revolution as revolution_module
from coalesce.references import Status, reference_from_dict, resolve
from coalesce.serialization import dumps
from coalesce.server import _call_tool
from coalesce.service import DocumentService, _scene_parts


def partial_revolve(*, angle, axis="x", revision=1, request_id="revolve", feature_id="part"):
    request = revolve(revision=revision, request_id=request_id, feature_id=feature_id)
    request["parameters"]["angle"] = {"value": angle, "unit": "deg"}
    request["parameters"]["axis"] = axis
    return request


def angle_update(angle, *, revision=2, request_id="angle-update"):
    return {
        "action": "update",
        "expectedRevision": revision,
        "requestId": request_id,
        "featureId": "part",
        "kind": "revolve",
        "set": {"angle": {"value": angle, "unit": "deg"}},
    }


@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("angle", [90, 180])
def test_partial_revolve_has_fractional_volume_and_exact_caps(tmp_path, axis, angle):
    service = DocumentService(tmp_path / f"{axis}-{angle}.json", warmup=False)
    points = PULLEY if axis == "x" else [[y, x] for x, y in PULLEY]
    assert service.sketch(polyline_sketch(points))["ok"]

    result = service.feature(partial_revolve(angle=angle, axis=axis))

    assert result["ok"] is True
    shape = service._regen.shapes["part"]
    full_volume = math.pi * (20 * (14**2 - 4**2) + 10 * (8**2 - 4**2))
    assert shape.volume == pytest.approx(full_volume * angle / 360, rel=1e-7)
    roles = service.query({"select": {"kind": "face_roles", "id": "part"}})["roles"]
    caps = [role for role in roles if role["role"] in {"cap_start", "cap_end"}]
    assert {role["role"] for role in caps} == {"cap_start", "cap_end"}
    assert all(role["faceCount"] == 1 for role in caps)


def test_partial_revolve_exports_one_valid_step_solid_after_reimport(tmp_path):
    target = tmp_path / "quarter.step"
    service = DocumentService(tmp_path / "quarter.json", warmup=False)
    assert service.sketch(polyline_sketch(PULLEY))["ok"]
    assert service.feature(partial_revolve(angle=90, axis="y"))["ok"]

    exported = service.export({"expectedRevision": 2, "path": str(target)})

    assert exported["ok"] is True
    assert exported["validation"]["reimport"]["solidCount"] == 1
    shape = import_step(target)
    assert len(shape.solids()) == 1
    assert BRepCheck_Analyzer(shape.wrapped).IsValid()


def test_partial_side_references_survive_source_and_angle_edits(tmp_path):
    service = DocumentService(tmp_path / "edits.json", warmup=False)
    original = [{"id": f"p{index}", "x": x, "y": y} for index, (x, y) in enumerate(PULLEY)]
    assert service.sketch(polyline_sketch(original))["ok"]
    assert service.feature(partial_revolve(angle=90))["ok"]
    before = service.query({"select": {"kind": "face_roles", "id": "part"}})["roles"]
    side_reference = next(role["exampleReference"] for role in before if role["role"] == "side(seg-p1-p2)")
    changed_points = [[0, 5], [30, 5], [30, 9], [20, 9], [20, 15], [0, 15]]
    changed = [
        {"id": f"p{index}", "x": x, "y": y}
        for index, (x, y) in enumerate(changed_points)
    ]

    assert service.sketch({
        "action": "update",
        "expectedRevision": 2,
        "requestId": "source-update",
        "sketchId": "profile",
        "set": {"entities": polyline_sketch(changed)["entities"]},
    })["ok"]
    assert resolve(reference_from_dict(side_reference), service._regen.provenance).status is Status.OK
    assert service.feature(angle_update(180, revision=3))["ok"]
    assert resolve(reference_from_dict(side_reference), service._regen.provenance).status is Status.OK


def test_cap_roles_appear_and_old_cap_reference_becomes_empty(tmp_path):
    service = DocumentService(tmp_path / "caps.json", warmup=False)
    assert service.sketch(polyline_sketch(PULLEY))["ok"]
    assert service.feature(revolve())["ok"]
    full_roles = service.query({"select": {"kind": "face_roles", "id": "part"}})["roles"]
    assert not {"cap_start", "cap_end"} & {role["role"] for role in full_roles}

    assert service.feature(angle_update(90))["ok"]
    partial_roles = service.query({"select": {"kind": "face_roles", "id": "part"}})["roles"]
    cap_reference = next(role["exampleReference"] for role in partial_roles if role["role"] == "cap_end")
    assert service.feature(angle_update(360, revision=3, request_id="full-again"))["ok"]

    resolution = resolve(reference_from_dict(cap_reference), service._regen.provenance)
    assert resolution.status is Status.EMPTY


@pytest.mark.parametrize("angle", [0, -1, 360.0001, float("inf"), float("nan")])
def test_invalid_partial_angles_fail_atomically(tmp_path, angle):
    supported = DocumentService(tmp_path / "supported-angle.json", warmup=False)
    assert supported.sketch(polyline_sketch(PULLEY))["ok"]
    assert supported.feature(partial_revolve(angle=90))["ok"]
    document = tmp_path / "invalid-angle.json"
    service = DocumentService(document, warmup=False)
    assert service.sketch(polyline_sketch(PULLEY))["ok"]
    before = document.read_bytes()

    result = service.feature(partial_revolve(angle=angle))

    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert result["revision"] == 1
    assert document.read_bytes() == before


@pytest.mark.parametrize(
    ("points", "code"),
    [
        ([[0, -2], [10, -2], [10, 6], [0, 6]], "REVOLVE_AXIS_CROSSING"),
        ([[0, 0], [10, 2], [10, 6], [0, 6]], "REVOLVE_AXIS_CONTACT"),
    ],
)
def test_partial_revolve_axis_failures_are_atomic(tmp_path, points, code):
    document = tmp_path / f"{code}.json"
    service = DocumentService(document, warmup=False)
    assert service.sketch(polyline_sketch(points))["ok"]
    before = document.read_bytes()

    result = service.feature(partial_revolve(angle=90))

    assert result["error"]["code"] == code
    assert result["revision"] == 1
    assert document.read_bytes() == before


def test_partial_revolve_rejects_multiple_profiles_atomically(tmp_path):
    document = tmp_path / "profiles.json"
    service = DocumentService(document, warmup=False)
    request = polyline_sketch(PULLEY)
    request["entities"].append({
        "id": "second",
        "kind": "circle",
        "center": [40, 10],
        "radius": {"value": 2, "unit": "mm"},
    })
    assert service.sketch(request)["ok"]
    before = document.read_bytes()
    operation = partial_revolve(angle=90)
    del operation["parameters"]["profile"]["profileIds"]

    result = service.feature(operation)

    assert result["error"]["code"] == "REVOLVE_MULTIPLE_PROFILES"
    assert result["revision"] == 1
    assert document.read_bytes() == before


def test_ambiguous_partial_cap_history_fails_atomically(tmp_path, monkeypatch):
    class AmbiguousCaps:
        def __init__(self, *args, **kwargs):
            self.real = real(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.real, name)

        def FirstShape(self):
            return self.real.LastShape()

    real = revolution_module.BRepPrimAPI_MakeRevol
    monkeypatch.setattr(revolution_module, "BRepPrimAPI_MakeRevol", AmbiguousCaps)
    document = tmp_path / "ambiguous.json"
    service = DocumentService(document, warmup=False)
    assert service.sketch(polyline_sketch(PULLEY))["ok"]
    before = document.read_bytes()

    result = service.feature(partial_revolve(angle=90))

    assert result["error"]["code"] == "HISTORY_MISSING"
    assert result["revision"] == 1
    assert document.read_bytes() == before


def test_legacy_full_revolve_request_replays_and_saves_byte_identically(tmp_path):
    fixture = Path(__file__).parent / "fixtures/revolve/legacy-full-revolve.coalesce.json"
    document = tmp_path / "legacy.coalesce.json"
    document.write_bytes(fixture.read_bytes())
    before = document.read_bytes()
    service = DocumentService(document, warmup=False)
    request = {
        "action": "create",
        "expectedRevision": 1,
        "requestId": "eval-create-shaft-revolve",
        "featureId": "shaft",
        "kind": "revolve",
        "parameters": {
            "profile": {"sketchId": "profile", "profileIds": ["outline"]},
            "axis": "x",
            "angle": {"value": 360, "unit": "deg"},
            "operation": "new_body",
        },
    }
    stored = next(
        receipt["response"]
        for receipt in service._document.receipts
        if receipt["requestId"] == request["requestId"]
    )

    assert service.feature(request) == stored
    request["parameters"]["angle"] = {"value": 2 * math.pi, "unit": "rad"}
    assert service.feature(request) == stored
    assert service.store.save(service._document) is True
    assert document.read_bytes() == before


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["create", "update"])
async def test_mcp_revolve_wrong_angle_unit_reports_unit_mismatch(tmp_path, action):
    service = DocumentService(tmp_path / f"wrong-angle-{action}.json", warmup=False)
    assert service.sketch(polyline_sketch(PULLEY))["ok"]
    if action == "create":
        request = partial_revolve(angle=90)
        request["parameters"]["angle"]["unit"] = "mm"
    else:
        assert service.feature(partial_revolve(angle=90))["ok"]
        request = angle_update(180)
        request["set"]["angle"]["unit"] = "mm"

    result = (await _call_tool(service, "feature", request)).structuredContent

    assert result["error"]["code"] == "UNIT_MISMATCH"
    assert result["error"]["expected"] == ["deg", "rad"]
    assert result["error"]["actual"] == "mm"


def test_partial_angle_is_serialized_and_shown_in_viewer_facts(tmp_path):
    document = tmp_path / "facts.json"
    service = DocumentService(document, warmup=False)
    assert service.sketch(polyline_sketch(PULLEY))["ok"]
    assert service.feature(partial_revolve(angle=90))["ok"]

    facts = service.query({"select": {"kind": "graph"}})
    part = next(node for node in facts["graph"]["nodes"] if node["id"] == "part")
    assert part["angle"] == 90
    assert service._regen.graph.nodes["part"].angle == 90
    restarted = DocumentService(document, warmup=False)
    assert restarted._document.graph.nodes["part"].angle == 90
    assert restarted.query({"select": {"kind": "measurements"}})["ok"]
    [scene_part] = _scene_parts(restarted._regen)
    assert scene_part.facts[0]["value"].startswith("90° about the X axis")

    full = DocumentService(tmp_path / "full.json", warmup=False)
    assert full.sketch(polyline_sketch(PULLEY, request_id="full-sketch"))["ok"]
    assert full.feature(revolve(request_id="full-revolve"))["ok"]
    assert '"angle"' not in dumps(full._document.graph)
