import shutil

import pytest
from build123d import import_step
from test_mcp_cut_hole_circle import extrude, rectangle_sketch
from test_mcp_vertical import run_server
from test_pcb_enclosure import FIXTURES, base_request, import_request, standoffs_request
from test_pcb_lid import lid_request


def _fillet(
    revision=2, *, role="outer_vertical", cardinality="exactly", count=4, radius=3
):
    reference = {
        "feature": "base",
        "role": role,
        "cardinality": cardinality,
    }
    if cardinality == "exactly":
        reference["count"] = count
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": "round-base",
        "featureId": "base_round",
        "kind": "fillet",
        "parameters": {
            "target": {"featureId": "base"},
            "edges": [reference],
            "radius": {"value": radius, "unit": "mm"},
        },
    }


@pytest.mark.anyio
async def test_outer_vertical_role_is_discoverable_and_fillet_survives_board_refresh(tmp_path):
    source = tmp_path / "board.kicad_pcb"
    shutil.copyfile(FIXTURES / "features.kicad_pcb", source)
    document = tmp_path / "rounded-base.coalesce.json"
    step = tmp_path / "rounded-base.step"

    created = await run_server(
        document,
        [
            ("import", import_request(source)),
            ("feature", base_request()),
            ("query", {"select": {"kind": "edge_roles", "id": "base"}}),
            ("feature", _fillet()),
            ("query", {"select": {"kind": "enclosure", "base": "base"}}),
        ],
    )

    roles = {item["role"]: item for item in created[2]["roles"]}
    assert roles["outer_vertical"]["edgeCount"] == 4
    assert roles["outer_vertical"]["orientation"] == "vertical"
    assert roles["outer_vertical"]["exampleReference"] == {
        "feature": "base",
        "role": "outer_vertical",
        "cardinality": "exactly",
        "count": 4,
    }
    assert created[3]["ok"] is True
    assert created[4]["base"]["minimumWallThickness"] == {"value": 2.0, "unit": "mm"}

    source.write_text(source.read_text().replace("(end 20 10)", "(end 30 15)"))
    refreshed = await run_server(
        document,
        [
            (
                "import",
                import_request(
                    source,
                    revision=3,
                    request_id="resize-board",
                    action="refresh",
                ),
            ),
            ("query", {"select": {"kind": "feature", "id": "base_round"}}),
            ("query", {"select": {"kind": "enclosure", "base": "base"}}),
            ("export", {"expectedRevision": 4, "path": str(step)}),
        ],
    )

    assert refreshed[0]["ok"] is True
    assert refreshed[1]["feature"]["edges"][0]["role"] == "outer_vertical"
    assert refreshed[2]["base"]["minimumWallThickness"] == {"value": 2.0, "unit": "mm"}
    assert refreshed[3]["ok"] is True
    reloaded = import_step(step)
    assert len(reloaded.solids()) == 1
    assert reloaded.is_valid


@pytest.mark.anyio
async def test_polygonized_arc_fillet_rejects_without_changing_last_good_revision(tmp_path):
    fixture = FIXTURES / "polygon32.kicad_pcb"
    document = tmp_path / "polygon32.coalesce.json"
    previous_step = tmp_path / "previous.step"

    created = await run_server(
        document,
        [
            ("import", import_request(fixture)),
            ("feature", base_request()),
        ],
    )
    assert created[-1]["revision"] == 2
    document_bytes = document.read_bytes()

    rejected, graph, exported = await run_server(
        document,
        [
            ("feature", _fillet(count=32, radius=0.2)),
            ("query", {"select": {"kind": "graph"}}),
            ("export", {"expectedRevision": 2, "path": str(previous_step)}),
        ],
    )

    assert rejected["ok"] is False
    assert rejected["revision"] == 2
    assert rejected["error"]["code"] == "MANUFACTURING_VALIDATION_FAILED"
    assert rejected["error"]["featureId"] == "base_round"
    assert rejected["error"]["partId"].startswith("base/")
    assert rejected["error"]["mismatchedMetric"]
    assert "path" not in rejected["error"].get("hint", "").lower()
    assert graph["revision"] == 2
    assert document.read_bytes() == document_bytes
    assert exported["ok"] is True
    assert previous_step.is_file()
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "polygon32.coalesce.json",
        "previous.step",
    ]


@pytest.mark.anyio
async def test_mixed_outline_roles_exclude_tangent_seams_and_fillet_exports(tmp_path):
    fixture = FIXTURES / "mixed-corners.kicad_pcb"
    document = tmp_path / "mixed.coalesce.json"
    step = tmp_path / "mixed.step"

    results = await run_server(
        document,
        [
            ("import", import_request(fixture)),
            ("feature", base_request()),
            ("query", {"select": {"kind": "edge_roles", "id": "base"}}),
            ("feature", _fillet(count=2, radius=1)),
            ("export", {"expectedRevision": 3, "path": str(step)}),
        ],
    )

    roles = {item["role"]: item for item in results[2]["roles"]}
    assert roles["outer_vertical"]["edgeCount"] == 2
    assert roles["inner_vertical"]["edgeCount"] == 2
    assert results[3]["ok"] is True
    assert results[4]["ok"] is True
    reloaded = import_step(step)
    assert len(reloaded.solids()) == 1
    assert reloaded.is_valid

    lid_results = await run_server(
        tmp_path / "mixed-lid.coalesce.json",
        [
            ("import", import_request(fixture)),
            ("feature", base_request()),
            ("feature", lid_request(revision=2, include_lip=False)),
            ("query", {"select": {"kind": "edge_roles", "id": "lid"}}),
        ],
    )
    lid_roles = {item["role"]: item for item in lid_results[3]["roles"]}
    assert lid_roles["lid_vertical"]["edgeCount"] == 2


@pytest.mark.anyio
@pytest.mark.parametrize("fixture", ["rounded.kicad_pcb", "circle.kicad_pcb"])
async def test_smooth_outlines_report_no_sharp_outer_edges_and_fail_closed(
    tmp_path, fixture
):
    results = await run_server(
        tmp_path / f"{fixture}.coalesce.json",
        [
            ("import", import_request(FIXTURES / fixture)),
            ("feature", base_request()),
            ("query", {"select": {"kind": "edge_roles", "id": "base"}}),
            ("feature", _fillet()),
        ],
    )

    roles = {item["role"]: item for item in results[2]["roles"]}
    assert roles["outer_vertical"]["edgeCount"] == 0
    assert roles["inner_vertical"]["edgeCount"] == 0
    assert results[3]["ok"] is False
    assert results[3]["error"]["code"] == "REFERENCE_FAILED"
    assert results[3]["error"]["resolution"]["status"] == "EMPTY"
    available = results[3]["error"]["resolution"]["availableRoles"]
    assert {item["role"]: item["edgeCount"] for item in available}["outer_vertical"] == 0


@pytest.mark.anyio
async def test_lid_and_plain_extrude_expose_distinct_vertical_edges(tmp_path):
    lid_results = await run_server(
        tmp_path / "lid.coalesce.json",
        [
            ("import", import_request(FIXTURES / "features.kicad_pcb")),
            ("feature", base_request()),
            ("feature", lid_request(revision=2, include_lip=False)),
            ("query", {"select": {"kind": "edge_roles", "id": "lid"}}),
        ],
    )
    lid_roles = {item["role"]: item for item in lid_results[3]["roles"]}
    assert lid_roles["lid_vertical"]["edgeCount"] == 4

    extrude_results = await run_server(
        tmp_path / "extrude.coalesce.json",
        [
            (
                "sketch",
                rectangle_sketch(
                    request_id="sketch",
                    revision=0,
                    sketch_id="profile",
                    entity_id="outline",
                    width=20,
                    height=10,
                ),
            ),
            (
                "feature",
                extrude(
                    request_id="extrude",
                    revision=1,
                    feature_id="body",
                    sketch_id="profile",
                    profile_ids=["outline"],
                    distance=5,
                ),
            ),
            ("query", {"select": {"kind": "edge_roles", "id": "body"}}),
        ],
    )
    roles = {item["role"] for item in extrude_results[2]["roles"]}
    assert {
        "between(side(bottom),side(left))",
        "between(side(bottom),side(right))",
        "between(side(left),side(top))",
        "between(side(right),side(top))",
    } <= roles


@pytest.mark.anyio
async def test_empty_edge_reference_lists_roles_even_with_any_cardinality(tmp_path):
    results = await run_server(
        tmp_path / "empty.coalesce.json",
        [
            ("import", import_request(FIXTURES / "features.kicad_pcb")),
            ("feature", base_request()),
            ("feature", _fillet(role="guessed_role", cardinality="any")),
        ],
    )

    error = results[2]["error"]
    assert error["code"] == "EMPTY_EDGE_SELECTION"
    assert error["resolution"]["status"] == "EMPTY"
    assert "outer_vertical" in {item["role"] for item in error["resolution"]["availableRoles"]}


@pytest.mark.anyio
async def test_lid_chamfer_commits_and_exports(tmp_path):
    document = tmp_path / "lid-chamfer.coalesce.json"
    step = tmp_path / "lid-chamfer.step"
    chamfer = {
        "action": "create",
        "expectedRevision": 3,
        "requestId": "bevel-lid",
        "featureId": "lid_bevel",
        "kind": "chamfer",
        "parameters": {
            "target": {"featureId": "lid"},
            "edges": [
                {
                    "feature": "lid",
                    "role": "lid_vertical",
                    "cardinality": "exactly",
                    "count": 4,
                }
            ],
            "distance": {"value": 0.5, "unit": "mm"},
        },
    }

    results = await run_server(
        document,
        [
            ("import", import_request(FIXTURES / "features.kicad_pcb")),
            ("feature", base_request()),
            ("feature", lid_request(revision=2, include_lip=False)),
            ("feature", chamfer),
            ("export", {"expectedRevision": 4, "path": str(step)}),
        ],
    )

    assert results[3]["ok"] is True
    assert results[4]["ok"] is True


@pytest.mark.anyio
async def test_inner_floor_fillet_exports(tmp_path):
    document = tmp_path / "inner-floor.coalesce.json"
    step = tmp_path / "inner-floor.step"
    fillet = _fillet(role="between(floor,inner_wall)", cardinality="one_or_more")
    fillet["parameters"]["radius"] = {"value": 3, "unit": "mm"}

    results = await run_server(
        document,
        [
            ("import", import_request(FIXTURES / "features.kicad_pcb")),
            ("feature", base_request()),
            ("feature", fillet),
            ("feature", standoffs_request(revision=3, selection={"mode": "all_mounting"})),
            ("export", {"expectedRevision": 4, "path": str(step)}),
        ],
    )

    assert results[2]["ok"] is True
    assert results[3]["ok"] is True
    assert results[4]["ok"] is True
    reloaded = import_step(step)
    assert len(reloaded.solids()) == 1
    assert reloaded.is_valid
