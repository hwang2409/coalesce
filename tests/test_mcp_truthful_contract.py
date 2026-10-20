import json

import pytest
from test_mcp_vertical import FEATURE, SKETCH, list_server_tools, run_server

from coalesce.server import TOOLS_GUIDE
from coalesce.service import DocumentService


@pytest.mark.anyio
async def test_tools_list_only_advertises_callable_feature_kinds_and_annotations(tmp_path):
    tools = {tool.name: tool for tool in await list_server_tools(tmp_path / "tools.coalesce.json")}

    assert tools["feature"].inputSchema["properties"]["kind"]["enum"] == [
        "extrude",
        "cut",
        "hole",
        "pcb_base",
        "standoffs",
        "pcb_lid",
    ]
    advertised = json.dumps([tool.model_dump(by_alias=True) for tool in tools.values()]).lower()
    assert "fillet" not in advertised
    assert "boolean" not in tools["feature"].inputSchema["properties"]["kind"]["enum"]
    assert tools["query"].annotations.readOnlyHint is True
    assert tools["query"].annotations.destructiveHint is False
    assert tools["query"].annotations.idempotentHint is True
    assert tools["check"].annotations.readOnlyHint is True
    assert tools["check"].annotations.destructiveHint is False
    assert tools["check"].annotations.idempotentHint is True
    for name in ("sketch", "feature"):
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.destructiveHint is True
        assert tools[name].annotations.idempotentHint is True


@pytest.mark.anyio
async def test_translated_plate_reports_bbox_placement_and_terminal_body_metrics(tmp_path):
    path = tmp_path / "translated.coalesce.json"
    sketch = {**SKETCH, "entities": [{**SKETCH["entities"][0]}]}
    sketch["entities"][0]["width"] = {"value": 60, "unit": "mm"}
    sketch["entities"][0]["height"] = {"value": 40, "unit": "mm"}
    feature = {**FEATURE, "parameters": {**FEATURE["parameters"], "distance": {"value": 10, "unit": "mm"}}}
    results = await run_server(
        path,
        [
            ("sketch", sketch),
            (
                "sketch",
                {
                    "action": "update",
                    "expectedRevision": 1,
                    "requestId": "translate",
                    "sketchId": "plate_profile",
                    "set": {"position": [10, 20, 30]},
                },
            ),
            ("feature", {**feature, "expectedRevision": 2}),
            ("query", {"select": {"kind": "measurements"}}),
        ],
    )

    metrics = results[-1]
    assert metrics["role"] == "manufacturing"
    assert metrics["bbox"] == {
        "x": {"value": 60, "unit": "mm"},
        "y": {"value": 40, "unit": "mm"},
        "z": {"value": 10, "unit": "mm"},
        "min": {"x": -20, "y": 0, "z": 30, "unit": "mm"},
        "max": {"x": 40, "y": 40, "z": 40, "unit": "mm"},
        "center": {"x": 10, "y": 20, "z": 35, "unit": "mm"},
    }
    assert metrics["parts"] == [
        {
            "partId": "plate_extrude/outline",
            "label": "plate_extrude",
            "role": "manufacturing",
            "sourceBody": {"feature": "plate_extrude", "entity": "outline"},
            "terminalFeatureId": "plate_extrude",
            "featureId": "plate_extrude",
            "bbox": metrics["bbox"],
            "volume": {"value": 24000, "unit": "mm^3"},
            "area": {"value": 6800, "unit": "mm^2"},
            "solidCount": 1,
        }
    ]
    assert metrics["bodies"] == metrics["parts"]


def test_feature_query_returns_only_requested_feature_and_rejects_unknown_id(tmp_path):
    service = DocumentService(tmp_path / "feature.coalesce.json")
    service.sketch(SKETCH)
    service.feature(FEATURE)

    result = service.query({"select": {"kind": "feature", "id": "plate_extrude"}})

    assert result["feature"] == {
        "type": "Extrude",
        "id": "plate_extrude",
        "depends_on": [],
        "sketch": "plate_profile",
        "height": 6.0,
    }
    assert "graph" not in result

    unknown = service.query({"select": {"kind": "feature", "id": "missing"}})
    assert unknown["error"] == {
        "code": "UNKNOWN_ID",
        "message": "unknown feature: missing",
        "retryable": False,
        "operation": "query.feature",
    }


@pytest.mark.anyio
async def test_feature_query_requires_id(tmp_path):
    result = (
        await run_server(
            tmp_path / "missing-id.coalesce.json",
            [("query", {"select": {"kind": "feature"}})],
        )
    )[0]

    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert result["error"]["message"] == "missing required field 'id' (field: select)"


def test_tools_guide_states_placement_cut_direction_no_guess_and_harness_setup():
    guide = TOOLS_GUIDE.lower()
    assert "ask the user" in guide
    assert "extrusion starts at the sketch plane" in guide
    assert "xy +z, xz -y, yz +x" in guide
    assert "flip=false" in guide
    assert "negative sketch normal" in guide
    assert "flip=true" in guide
    assert "positive sketch normal" in guide
    assert "flip has no effect for through-all" in guide
    assert "pcb_base" in guide
    assert "stable `holeids`" in guide
    assert "import` the board, create `pcb_base`, create `standoffs`, create" in guide
    assert "`pcb_lid`, run `check`" in guide
    assert "default_tools_approval_mode = \"approve\"" in TOOLS_GUIDE
    assert '"mcpServers"' in TOOLS_GUIDE
