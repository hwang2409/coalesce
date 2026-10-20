import json

import pytest
from test_mcp_vertical import FEATURE, SKETCH, list_server_tools, run_server

from coalesce.server import TOOLS_GUIDE
from coalesce.service import DocumentService


@pytest.mark.anyio
async def test_tools_list_only_advertises_callable_feature_kinds_and_annotations(tmp_path):
    tools = {tool.name: tool for tool in await list_server_tools(tmp_path / "tools.coalesce.json")}

    assert tools["feature"].inputSchema["properties"]["kind"]["enum"] == ["extrude"]
    advertised = json.dumps([tool.model_dump(by_alias=True) for tool in tools.values()]).lower()
    assert all(kind not in advertised for kind in ("boolean", "hole", "fillet"))
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
    assert metrics["bbox"] == {
        "x": {"value": 60, "unit": "mm"},
        "y": {"value": 40, "unit": "mm"},
        "z": {"value": 10, "unit": "mm"},
        "min": {"x": -20, "y": 0, "z": 30, "unit": "mm"},
        "max": {"x": 40, "y": 40, "z": 40, "unit": "mm"},
        "center": {"x": 10, "y": 20, "z": 35, "unit": "mm"},
    }
    assert metrics["bodies"] == [
        {
            "featureId": "plate_extrude",
            "bodyIndex": 0,
            "bbox": metrics["bbox"],
            "volume": {"value": 24000, "unit": "mm^3"},
            "area": {"value": 6800, "unit": "mm^2"},
            "solidCount": 1,
        }
    ]


def test_feature_query_returns_only_requested_feature(tmp_path):
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


def test_tools_guide_states_placement_no_guess_and_harness_setup():
    assert "ask the user" in TOOLS_GUIDE.lower()
    assert "extrusion starts at the sketch plane" in TOOLS_GUIDE.lower()
    assert "default_tools_approval_mode = \"approve\"" in TOOLS_GUIDE
    assert '"mcpServers"' in TOOLS_GUIDE
