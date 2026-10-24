import json
import math
from pathlib import Path

import pytest
from build123d import GeomType, import_step
from OCP.BRepCheck import BRepCheck_Analyzer
from test_mcp_vertical import run_server

import coalesce.revolution as revolution_module
from coalesce.service import DocumentService


def polyline_sketch(points, *, revision=0, request_id="sketch", sketch_id="profile"):
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": request_id,
        "sketchId": sketch_id,
        "plane": {"kind": "origin", "name": "XY"},
        "entities": [
            {"id": "outline", "kind": "polyline", "points": points, "closed": True}
        ],
    }


def revolve(*, revision=1, request_id="revolve", feature_id="part", sketch_id="profile"):
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": request_id,
        "featureId": feature_id,
        "kind": "revolve",
        "parameters": {
            "profile": {"sketchId": sketch_id, "profileIds": ["outline"]},
            "axis": "x",
            "angle": {"value": 360, "unit": "deg"},
            "operation": "new_body",
        },
    }


SHAFT = [[0, 0], [30, 0], [30, 5], [20, 5], [20, 10], [0, 10]]
PULLEY = [[0, 4], [30, 4], [30, 8], [20, 8], [20, 14], [0, 14]]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "points", "volume", "radii"),
    [
        ("shaft", SHAFT, math.pi * (20 * 10**2 + 10 * 5**2), [5, 10]),
        ("pulley", PULLEY, math.pi * (20 * (14**2 - 4**2) + 10 * (8**2 - 4**2)), [4, 8, 14]),
    ],
)
async def test_real_stdio_revolve_exports_exact_single_solid(tmp_path, name, points, volume, radii):
    document = tmp_path / f"{name}.coalesce.json"
    target = tmp_path / f"{name}.step"
    results = await run_server(
        document,
        [
            ("sketch", polyline_sketch(points)),
            ("feature", revolve(feature_id=name)),
            ("query", {"select": {"kind": "edge_roles", "id": name}}),
            ("export", {"expectedRevision": 2, "path": str(target)}),
        ],
    )

    assert all(result["ok"] is True for result in results)
    assert any(role["role"].startswith("between(side(") for role in results[2]["roles"])
    assert results[3]["validation"]["reimport"]["solidCount"] == 1
    shape = import_step(target)
    assert len(shape.solids()) == 1
    assert BRepCheck_Analyzer(shape.wrapped).IsValid()
    assert shape.volume == pytest.approx(volume, rel=1e-7)
    bounds = shape.bounding_box()
    assert (bounds.size.X, bounds.size.Y, bounds.size.Z) == pytest.approx((30, 2 * max(radii), 2 * max(radii)))
    cylinders = [face for face in shape.faces() if face.geom_type is GeomType.CYLINDER]
    assert sorted(face.radius for face in cylinders) == pytest.approx(radii)
    assert sum(face.geom_type is GeomType.PLANE for face in shape.faces()) == 3


@pytest.mark.anyio
async def test_roles_survive_sketch_update_restart_and_drive_fillet(tmp_path):
    document = tmp_path / "updated.coalesce.json"
    changed = [[0, 0], [30, 0], [30, 6], [20, 6], [20, 11], [0, 11]]
    role = "between(side(seg-p2-p3),side(seg-p3-p4))"
    first = await run_server(
        document,
        [
            ("sketch", polyline_sketch(SHAFT)),
            ("feature", revolve(feature_id="shaft")),
            (
                "sketch",
                {
                    "action": "update",
                    "expectedRevision": 2,
                    "requestId": "diameter-update",
                    "sketchId": "profile",
                    "set": {"entities": polyline_sketch(changed)["entities"]},
                },
            ),
        ],
    )
    assert first[-1]["ok"] is True
    restarted = await run_server(
        document,
        [
            ("query", {"select": {"kind": "edge_roles", "id": "shaft"}}),
            (
                "feature",
                {
                    "action": "create",
                    "expectedRevision": 3,
                    "requestId": "step-fillet",
                    "featureId": "rounded_shaft",
                    "kind": "fillet",
                    "parameters": {
                        "target": {"featureId": "shaft"},
                        "edges": [{"feature": "shaft", "role": role, "cardinality": "one"}],
                        "radius": {"value": 0.5, "unit": "mm"},
                    },
                },
            ),
        ],
    )
    assert role in {item["role"] for item in restarted[0]["roles"]}
    assert restarted[1]["ok"] is True
    assert restarted[1]["revision"] == 4


def test_fixed_constrained_profile_revolves_with_line_ids(tmp_path):
    points = [
        {"id": f"p{index}", "x": x, "y": y}
        for index, (x, y) in enumerate(SHAFT)
    ]
    lines = [
        {"id": f"edge{index}", "start": f"p{index}", "end": f"p{(index + 1) % len(points)}"}
        for index in range(len(points))
    ]
    constraints = [
        {"id": f"fix-p{index}", "kind": "fix", "entities": [f"p{index}"]}
        for index in range(len(points))
    ]
    service = DocumentService(tmp_path / "constrained.coalesce.json", warmup=False)
    result = service.sketch(
        {
            "action": "create",
            "expectedRevision": 0,
            "requestId": "constrained-profile",
            "sketchId": "profile",
            "plane": {"kind": "origin", "name": "XY"},
            "constrained": {
                "kind": "constrained",
                "points": points,
                "lines": lines,
                "constraints": constraints,
            },
        }
    )
    assert result["ok"] is True
    request = revolve()
    request["parameters"]["profile"]["profileIds"] = ["profile"]
    assert service.feature(request)["ok"] is True
    roles = service.query({"select": {"kind": "edge_roles", "id": "part"}})["roles"]
    assert any("side(edge" in item["role"] for item in roles)


def test_axis_crossing_multiple_profiles_and_kernel_failure_are_atomic(tmp_path, monkeypatch):
    document = tmp_path / "atomic.coalesce.json"
    service = DocumentService(document, warmup=False)
    assert service.sketch(polyline_sketch([[0, -2], [10, -2], [10, 6], [0, 6]]))["ok"]
    before = document.read_bytes()
    crossing = service.feature(revolve())
    assert crossing["error"]["code"] == "REVOLVE_AXIS_CROSSING"
    assert crossing["revision"] == 1 and document.read_bytes() == before

    multiple_document = tmp_path / "multiple.coalesce.json"
    multiple = DocumentService(multiple_document, warmup=False)
    request = polyline_sketch(PULLEY)
    request["entities"].append(
        {"id": "second", "kind": "circle", "center": [40, 10], "radius": {"value": 2, "unit": "mm"}}
    )
    assert multiple.sketch(request)["ok"]
    before = multiple_document.read_bytes()
    result = multiple.feature(revolve())
    assert result["error"]["code"] == "REVOLVE_MULTIPLE_PROFILES"
    assert result["revision"] == 1 and multiple_document.read_bytes() == before

    kernel_document = tmp_path / "kernel.coalesce.json"
    kernel = DocumentService(kernel_document, warmup=False)
    assert kernel.sketch(polyline_sketch(PULLEY))["ok"]
    before = kernel_document.read_bytes()
    monkeypatch.setattr(
        revolution_module,
        "BRepPrimAPI_MakeRevol",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected")),
    )
    failed = kernel.feature(revolve())
    assert failed["error"]["code"] == "REVOLVE_FAILED"
    assert failed["revision"] == 1 and kernel_document.read_bytes() == before


def test_step_round_trip_failure_is_atomic(tmp_path, monkeypatch):
    import coalesce.service as service_module
    from coalesce.export import ExportFailure

    document = tmp_path / "roundtrip.coalesce.json"
    service = DocumentService(document, warmup=False)
    assert service.sketch(polyline_sketch(PULLEY))["ok"]
    before = document.read_bytes()

    def fail_round_trip(_part):
        raise ExportFailure("EXPORT_VALIDATION_FAILED", "injected reimport mismatch", mismatchedMetric="volume")

    monkeypatch.setattr(service_module, "_validate_part_for_step", fail_round_trip)
    result = service.feature(revolve())
    assert result["error"]["code"] == "MANUFACTURING_VALIDATION_FAILED"
    assert result["error"]["mismatchedMetric"] == "volume"
    assert result["revision"] == 1 and document.read_bytes() == before


def test_stale_revision_is_atomic(tmp_path):
    document = tmp_path / "stale.coalesce.json"
    service = DocumentService(document, warmup=False)
    assert service.sketch(polyline_sketch(PULLEY))["ok"]
    before = document.read_bytes()
    result = service.feature(revolve(revision=0))
    assert result["error"]["code"] == "REVISION_CONFLICT"
    assert result["revision"] == 1 and document.read_bytes() == before


def test_axis_contact_validation_is_required(tmp_path, monkeypatch):
    request = polyline_sketch([[0, 0], [10, 2], [10, 6], [0, 6]])
    rejected = DocumentService(tmp_path / "rejected.json", warmup=False)
    assert rejected.sketch(request)["ok"]
    assert rejected.feature(revolve())["error"]["code"] == "REVOLVE_AXIS_CONTACT"

    monkeypatch.setattr(revolution_module, "_validate_axis", lambda *_args: set())
    mutated = DocumentService(tmp_path / "mutated.json", warmup=False)
    request["requestId"] = "mutated-sketch"
    assert mutated.sketch(request)["ok"]
    assert mutated.feature(revolve(request_id="mutated-revolve"))["ok"] is True


def test_generated_history_mapping_is_required(tmp_path, monkeypatch):
    class NoHistory:
        def __init__(self, *args, **kwargs):
            self.real = revolution_module.BRepPrimAPI_MakeRevol_REAL(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.real, name)

        def Generated(self, _edge):
            return []

    real = revolution_module.BRepPrimAPI_MakeRevol
    monkeypatch.setattr(revolution_module, "BRepPrimAPI_MakeRevol_REAL", real, raising=False)
    monkeypatch.setattr(revolution_module, "BRepPrimAPI_MakeRevol", NoHistory)
    document = tmp_path / "history.coalesce.json"
    service = DocumentService(document, warmup=False)
    assert service.sketch(polyline_sketch(PULLEY))["ok"]
    before = document.read_bytes()
    result = service.feature(revolve())
    assert result["error"]["code"] == "HISTORY_MISSING"
    assert result["revision"] == 1 and document.read_bytes() == before


def test_revolve_guide_example_is_json_and_registered():
    guide = Path("docs/revolve.md").read_text()
    payload = json.loads(guide.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert payload["kind"] == "revolve"
    assert payload["parameters"]["angle"] == {"value": 360, "unit": "deg"}
