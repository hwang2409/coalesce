"""Acceptance requirements use regenerated geometry and stay bound to a revision."""

from pathlib import Path

import pytest
from jsonschema import ValidationError, validate
from test_mcp_vertical import run_server
from test_pcb_enclosure import (
    FIXTURES,
    base_request,
    import_request,
    standoffs_request,
)
from test_pcb_lid import lid_request
from test_port_cutout import port_request

from coalesce.schemas import CHECK_SCHEMA
from coalesce.server import TOOLS_GUIDE
from coalesce.service import DocumentService


def quantity(value):
    return {"value": value, "unit": "mm"}


def _service(tmp_path: Path, *, holes):
    service = DocumentService(tmp_path / "requirements.coalesce.json")
    assert service.import_board(import_request(FIXTURES / "features.kicad_pcb"))["ok"]
    assert service.feature(base_request())["ok"]
    assert service.feature(standoffs_request(selection={"mode": "explicit", "holeIds": holes}))["ok"]
    return service


def test_connector_warning_follows_declared_model_availability(tmp_path):
    service = _service(
        tmp_path,
        holes=["hole-03e3c0594f5f1b105237c4c0", "hole-89c529cbad8407f9ccb81676"],
    )
    assert service.feature(port_request(revision=3))["ok"]
    board = service._document.graph.nodes["controller_board"]

    board.availability["models"] = "declared"
    declared = service.query({"select": {"kind": "enclosure", "base": "base"}})
    assert not any(item["code"] == "CONNECTOR_HEIGHT_UNRESOLVED" for item in declared["warnings"])

    board.availability["models"] = "missing"
    missing = service.query({"select": {"kind": "enclosure", "base": "base"}})
    assert any(item["code"] == "CONNECTOR_HEIGHT_UNRESOLVED" for item in missing["warnings"])


def test_missing_mount_is_a_fail_with_the_hole_id(tmp_path):
    service = _service(tmp_path, holes=["hole-03e3c0594f5f1b105237c4c0"])
    response = service.check({"action": "evaluate", "base": "base", "requirements": [{"id": "mounts", "kind": "mounting_holes"}]})
    assert response["gate"] == "FAIL"
    result = response["results"][0]
    assert result["status"] == "FAIL"
    assert "hole-89c529cbad8407f9ccb81676" in result["measured"]["uncoveredHoleIds"]
    assert result["revision"] == service.revision


@pytest.mark.anyio
async def test_stdio_returns_pass_and_unknown_gates(tmp_path):
    document = tmp_path / "stdio-requirements.coalesce.json"
    calls = [
        ("import", import_request(FIXTURES / "features.kicad_pcb")),
        ("feature", base_request()),
        ("feature", standoffs_request()),
        ("feature", lid_request(revision=3, include_lip=False)),
        (
            "check",
            {
                "action": "evaluate",
                "base": "base",
                "requirements": [
                    {"id": "wall", "kind": "wall_thickness", "threshold": quantity(2)},
                    {"id": "fit", "kind": "fit_gap", "threshold": {"min": quantity(0.1), "max": quantity(0.5)}},
                ],
            },
        ),
    ]
    *_, response = await run_server(document, calls)
    assert response["gate"] == "UNKNOWN"
    assert response["results"][0]["status"] == "PASS"
    assert response["results"][1]["status"] == "UNKNOWN"
    assert response["results"][1]["reason"]


def test_explicit_empty_requirements_rejected(tmp_path):
    service = DocumentService(tmp_path / "empty.coalesce.json")
    response = service.check({"action": "evaluate", "requirements": []})
    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_PARAMETER"


def test_empty_document_is_not_valid_brep(tmp_path):
    service = DocumentService(tmp_path / "empty-brep.coalesce.json")
    response = service.check({"action": "evaluate", "requirements": [{"id": "valid", "kind": "valid_brep"}]})
    assert response["gate"] in {"UNKNOWN", "ERROR"}
    assert response["results"][0]["status"] in {"UNKNOWN", "ERROR"}


def test_hole_reference_is_rejected_as_hole_id(tmp_path):
    service = _service(tmp_path, holes=["hole-03e3c0594f5f1b105237c4c0"])
    response = service.check({"action": "evaluate", "base": "base", "requirements": [{"id": "mounts", "kind": "mounting_holes", "holeIds": ["H1"]}]})
    assert response["error"]["code"] == "INVALID_PARAMETER"
    assert response["error"]["invalidHoleIds"] == ["H1"]
    assert {item["id"] for item in response["error"]["validHoles"]} == {
        "hole-03e3c0594f5f1b105237c4c0",
        "hole-89c529cbad8407f9ccb81676",
        "hole-9ddb205ba1bc297f95347452",
    }
    assert any(item.get("reference") == "H1" for item in response["error"]["validHoles"])


def test_unknown_hole_id_is_rejected(tmp_path):
    service = _service(tmp_path, holes=["hole-03e3c0594f5f1b105237c4c0"])
    response = service.check({"action": "evaluate", "base": "base", "requirements": [{"id": "mounts", "kind": "mounting_holes", "holeIds": ["does-not-exist"]}]})
    assert response["error"]["code"] == "INVALID_PARAMETER"
    assert response["error"]["invalidHoleIds"] == ["does-not-exist"]
    assert len(response["error"]["validHoles"]) == 3


def test_reference_never_appears_in_id_field(tmp_path):
    service = _service(tmp_path, holes=["hole-03e3c0594f5f1b105237c4c0"])
    response = service.check({"action": "evaluate", "base": "base", "requirements": [{"id": "mounts", "kind": "mounting_holes"}]})

    def id_values(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith("Ids") and isinstance(item, list):
                    yield from item
                yield from id_values(item)
        elif isinstance(value, list):
            for item in value:
                yield from id_values(item)

    assert "H1" not in set(id_values(response))
    assert "REF**" not in set(id_values(response))


def test_standoffs_on_all_mounting_holes_pass(tmp_path):
    holes = [
        "hole-03e3c0594f5f1b105237c4c0",
        "hole-89c529cbad8407f9ccb81676",
        "hole-9ddb205ba1bc297f95347452",
    ]
    service = _service(tmp_path, holes=holes)
    result = service.check({"action": "evaluate", "base": "base", "requirements": [{"id": "mounts", "kind": "mounting_holes"}]})["results"][0]
    assert result["status"] == "PASS"
    assert result["measured"]["coveredHoleIds"] == holes
    assert result["measured"]["uncoveredHoleIds"] == []


def test_mount_subset_reports_waived_hole(tmp_path):
    required = ["hole-03e3c0594f5f1b105237c4c0", "hole-89c529cbad8407f9ccb81676"]
    service = _service(tmp_path, holes=required)
    result = service.check({"action": "evaluate", "base": "base", "requirements": [{"id": "mounts", "kind": "mounting_holes", "holeIds": required}]})["results"][0]
    assert result["status"] == "PASS"
    assert result["measured"]["notRequiredHoleIds"] == ["hole-9ddb205ba1bc297f95347452"]
    assert result["measured"]["notRequiredReferences"] == ["H1"]


def test_no_mounting_holes_is_unknown():
    from coalesce.requirements import evaluate
    inspection = {"baseId": "base", "base": {"partId": "part"}, "standoffs": {"holeIds": [], "coveredHoleIds": [], "uncoveredHoleIds": []}}
    result = evaluate([{"id": "mounts", "kind": "mounting_holes"}], inspection, 1)["results"][0]
    assert result["status"] == "UNKNOWN"


def test_non_mm_measurement_is_unknown():
    from coalesce.requirements import evaluate
    inspection = {"baseId": "base", "base": {"partId": "part", "minimumWallThickness": {"value": 2, "unit": "inch"}, "floorThickness": {"value": 2, "unit": "mm"}}}
    result = evaluate([{"id": "wall", "kind": "wall_thickness", "threshold": quantity(1)}], inspection, 1)["results"][0]
    assert result["status"] == "UNKNOWN"


def test_inch_wall_threshold_is_unknown():
    from coalesce.requirements import evaluate
    inspection = {"base": {"partId": "part", "minimumWallThickness": quantity(2)}}
    result = evaluate([{"id": "wall", "kind": "wall_thickness", "threshold": {"value": 1, "unit": "inch"}}], inspection, 1)["results"][0]
    assert result["status"] == "UNKNOWN"
    assert "threshold" in result["reason"]


def test_inch_floor_threshold_is_unknown():
    from coalesce.requirements import evaluate
    inspection = {"base": {"partId": "part", "floorThickness": quantity(2)}}
    result = evaluate([{"id": "floor", "kind": "floor_thickness", "threshold": {"value": 1, "unit": "inch"}}], inspection, 1)["results"][0]
    assert result["status"] == "UNKNOWN"
    assert "threshold" in result["reason"]


def test_inch_fit_gap_threshold_is_unknown():
    from coalesce.requirements import evaluate
    inspection = {
        "base": {"partId": "base-part"},
        "lids": [{"partId": "lid-part", "fitGap": {"min": quantity(0.2), "max": quantity(0.2)}}],
    }
    threshold = {"min": {"value": 0.1, "unit": "inch"}, "max": quantity(0.5)}
    result = evaluate([{"id": "fit", "kind": "fit_gap", "threshold": threshold}], inspection, 1)["results"][0]
    assert result["status"] == "UNKNOWN"
    assert "threshold" in result["reason"]


def test_check_schema_rejects_empty_requirements():
    with pytest.raises(ValidationError):
        validate({"action": "evaluate", "requirements": []}, CHECK_SCHEMA)


def test_tools_guide_does_not_claim_zero_solids_pass():
    guide = " ".join(TOOLS_GUIDE.split()).lower()
    assert "`pass` with `solidcount: 0`" not in guide


def test_port_evidence_keeps_board_identity_and_part_id(tmp_path):
    service = _service(tmp_path, holes=["hole-03e3c0594f5f1b105237c4c0"])
    assert service.feature(port_request(revision=3))['ok']
    service._document.graph.nodes["controller_board"].availability["models"] = "declared"
    result = service.check({"action": "evaluate", "base": "base", "requirements": [{"id": "port", "kind": "port_clearance", "threshold": quantity(0)}]})["results"][0]
    assert result["involved"] == [service.query({"select": {"kind": "enclosure", "base": "base"}})["base"]["partId"]]
    assert {"reference", "value", "libraryId", "clearance", "blocked"} <= set(result["measured"][0])


def test_clean_base_passes_measured_requirements(tmp_path):
    service = _service(
        tmp_path,
        holes=["hole-03e3c0594f5f1b105237c4c0", "hole-89c529cbad8407f9ccb81676", "hole-9ddb205ba1bc297f95347452"],
    )
    response = service.check(
        {
            "action": "evaluate",
            "base": "base",
            "requirements": [
                {"id": "wall", "kind": "wall_thickness", "threshold": quantity(2)},
                {"id": "floor", "kind": "floor_thickness", "threshold": quantity(2)},
                {"id": "mounts", "kind": "mounting_holes", "holeIds": ["hole-03e3c0594f5f1b105237c4c0", "hole-89c529cbad8407f9ccb81676"]},
                {"id": "clean", "kind": "clean_deliverables"},
            ],
        }
    )
    assert response["gate"] == "PASS"
    assert all(result["status"] == "PASS" for result in response["results"])
    assert all(result["revision"] == service.revision for result in response["results"])


def test_inspection_failure_keeps_exception_text_out_of_message(tmp_path, monkeypatch):
    import coalesce.enclosure_inspection

    service = _service(tmp_path, holes=["hole-03e3c0594f5f1b105237c4c0"])

    def broken(*_args, **_kwargs):
        raise RuntimeError("BRep_API: command not done")

    monkeypatch.setattr(coalesce.enclosure_inspection, "inspect_enclosure", broken)
    response = service.check(
        {"action": "evaluate", "base": "base", "requirements": [{"id": "mounts", "kind": "mounting_holes"}]}
    )

    error = response["error"]
    assert error["code"] == "INSPECTION_FAILED"
    assert "BRep_API" not in error["message"]
    assert error["kernelMessage"] == "BRep_API: command not done"
