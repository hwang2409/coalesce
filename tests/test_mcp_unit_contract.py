import copy
from pathlib import Path

import pytest
from test_mcp_vertical import FEATURE
from test_revolve import PULLEY, polyline_sketch, revolve

from coalesce.server import _call_tool
from coalesce.service import DocumentService

BAD_UNIT = "inch"


def _quantity(value=1, unit=BAD_UNIT):
    return {"value": value, "unit": unit}


def _reference():
    return {"feature": "part", "role": "cap_end"}


def _revolve_create():
    request = revolve()
    request["parameters"]["angle"] = _quantity(90)
    return request


def _revolve_update():
    return {
        "action": "update",
        "expectedRevision": 2,
        "requestId": "bad-revolve-update-unit",
        "featureId": "part",
        "kind": "revolve",
        "set": {"angle": _quantity(90)},
    }


def _extrude():
    request = copy.deepcopy(FEATURE)
    request["parameters"]["distance"] = _quantity(6)
    return request


def _hole():
    return {
        "action": "create",
        "expectedRevision": 0,
        "requestId": "bad-hole-unit",
        "featureId": "hole",
        "kind": "hole",
        "parameters": {
            "target": {"featureId": "part"},
            "diameter": _quantity(4),
            "position": [0, 0],
        },
    }


def _occurrence():
    return {
        "action": "create",
        "kind": "assembly_occurrence",
        "expectedRevision": 0,
        "requestId": "bad-occurrence-unit",
        "occurrenceId": "copy",
        "parameters": {
            "partId": "part/body",
            "transform": {
                "translation": _quantity([0, 0, 0]),
                "rotation": {"axis": [0, 0, 1], "angle": _quantity(0, "deg")},
            },
        },
    }


def _occurrence_rotation():
    request = _occurrence()
    request["requestId"] = "bad-rotation-unit"
    request["parameters"]["transform"]["translation"]["unit"] = "mm"
    request["parameters"]["transform"]["rotation"]["angle"]["unit"] = BAD_UNIT
    return request


def _planar_mate():
    return {
        "action": "create",
        "kind": "assembly_mate",
        "expectedRevision": 0,
        "requestId": "bad-mate-unit",
        "mateId": "flush",
        "parameters": {
            "kind": "planar",
            "first": {"occurrenceId": "a", "reference": _reference()},
            "second": {"occurrenceId": "b", "reference": _reference()},
            "orientation": "aligned",
            "offset": _quantity(),
        },
    }


def _wall_check():
    return {
        "action": "evaluate",
        "requirements": [
            {"id": "wall", "kind": "wall_thickness", "threshold": _quantity(2)}
        ],
    }


def _overlap_check():
    return {
        "action": "evaluate",
        "requirements": [
            {
                "id": "overlap",
                "kind": "assembly_interference",
                "maximumOverlapVolume": _quantity(0),
            }
        ],
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "payload", "field", "expected"),
    [
        ("feature", _revolve_create(), "parameters.angle.unit", ["deg", "rad"]),
        ("feature", _revolve_update(), "set.angle.unit", ["deg", "rad"]),
        ("feature", _extrude(), "parameters.distance.unit", "mm"),
        ("feature", _hole(), "parameters.diameter.unit", "mm"),
        (
            "feature",
            _occurrence(),
            "parameters.transform.translation.unit",
            "mm",
        ),
        (
            "feature",
            _occurrence_rotation(),
            "parameters.transform.rotation.angle.unit",
            ["deg", "rad"],
        ),
        ("feature", _planar_mate(), "parameters.offset.unit", "mm"),
        ("check", _wall_check(), "requirements[0].threshold.unit", "mm"),
        (
            "check",
            _overlap_check(),
            "requirements[0].maximumOverlapVolume.unit",
            "mm^3",
        ),
    ],
)
async def test_schema_unit_errors_are_public_unit_mismatches(
    tmp_path, name, payload, field, expected
):
    identity = payload.get("requestId", field)
    service = DocumentService(tmp_path / f"{identity}.json", warmup=False)

    result = (await _call_tool(service, name, payload)).structuredContent

    assert result["error"] == {
        **result["error"],
        "code": "UNIT_MISMATCH",
        "field": field,
        "expected": expected,
        "actual": BAD_UNIT,
    }


@pytest.mark.anyio
async def test_mcp_revolve_create_defaults_omitted_angle_to_legacy_identity(tmp_path):
    fixture = Path(__file__).parent / "fixtures/revolve/legacy-full-revolve.coalesce.json"
    document = tmp_path / "legacy.coalesce.json"
    document.write_bytes(fixture.read_bytes())
    service = DocumentService(document, warmup=False)
    request = revolve(
        request_id="eval-create-shaft-revolve", feature_id="shaft", sketch_id="profile"
    )
    request["parameters"]["profile"]["profileIds"] = ["outline"]
    del request["parameters"]["angle"]
    stored = next(
        receipt["response"]
        for receipt in service._document.receipts
        if receipt["requestId"] == request["requestId"]
    )

    result = (await _call_tool(service, "feature", request)).structuredContent

    assert result == stored


@pytest.mark.anyio
async def test_mcp_revolve_create_accepts_advertised_angle_unit(tmp_path):
    service = DocumentService(tmp_path / "good-unit.json", warmup=False)
    assert service.sketch(polyline_sketch(PULLEY))["ok"]
    request = revolve()
    request["parameters"]["angle"] = {"value": 3.141592653589793, "unit": "rad"}

    result = (await _call_tool(service, "feature", request)).structuredContent

    assert result["ok"] is True
