"""Acceptance requirements use regenerated geometry and stay bound to a revision."""

from pathlib import Path

import pytest
from test_mcp_vertical import run_server
from test_pcb_enclosure import (
    FIXTURES,
    base_request,
    import_request,
    standoffs_request,
)
from test_pcb_lid import lid_request
from test_port_cutout import port_request

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


def test_clean_base_passes_measured_requirements(tmp_path):
    service = _service(
        tmp_path,
        holes=["hole-03e3c0594f5f1b105237c4c0", "hole-89c529cbad8407f9ccb81676"],
    )
    response = service.check(
        {
            "action": "evaluate",
            "base": "base",
            "requirements": [
                {"id": "wall", "kind": "wall_thickness", "threshold": quantity(2)},
                {"id": "floor", "kind": "floor_thickness", "threshold": quantity(2)},
                {"id": "mounts", "kind": "mounting_holes"},
                {"id": "clean", "kind": "clean_deliverables"},
            ],
        }
    )
    assert response["gate"] == "PASS"
    assert all(result["status"] == "PASS" for result in response["results"])
    assert all(result["revision"] == service.revision for result in response["results"])
