import math

import pytest

from coalesce.document_store import Document, DocumentStore
from coalesce.graph import Extrude, Graph, Hole, Rectangle, Sketch
from coalesce.service import DocumentService
from coalesce.viewer import ViewerSceneStore
from test_mcp_cut_hole_circle import cut, extrude, hole, rectangle_sketch
from test_mcp_vertical import run_server


def plate_setup():
    return [
        ("sketch", rectangle_sketch(request_id="s", revision=0, sketch_id="plate_sketch", entity_id="plate", width=60, height=40)),
        ("feature", extrude(request_id="p", revision=1, feature_id="plate", sketch_id="plate_sketch", profile_ids=["plate"], distance=3)),
    ]


@pytest.mark.anyio
async def test_terminal_catalog_keeps_plate_identity_through_join_and_holes_and_scene(tmp_path):
    path = tmp_path / "catalog.coalesce.json"
    calls = plate_setup() + [
        ("sketch", {"action": "create", "expectedRevision": 2, "requestId": "wall-sketch", "sketchId": "wall_sketch", "plane": {"kind": "origin", "name": "XY", "offset": {"value": 3, "unit": "mm"}}, "entities": [{"id": "outer", "kind": "rectangle", "width": {"value": 60, "unit": "mm"}, "height": {"value": 40, "unit": "mm"}}, {"id": "inner", "kind": "rectangle", "width": {"value": 54, "unit": "mm"}, "height": {"value": 34, "unit": "mm"}}]}),
        ("feature", {"action": "create", "expectedRevision": 3, "requestId": "wall", "featureId": "wall", "kind": "extrude", "parameters": {"profile": {"sketchId": "wall_sketch", "profileIds": ["outer", "inner"]}, "distance": {"value": 4, "unit": "mm"}, "operation": "join", "target": {"featureId": "plate"}, "direction": "normal"}}),
        ("feature", hole(request_id="hole-a", revision=4, feature_id="hole_a", target="wall", position=(-20, 10))),
        ("feature", hole(request_id="hole-b", revision=5, feature_id="hole_b", target="hole_a", position=(20, 10))),
        ("query", {"select": {"kind": "measurements"}}),
    ]
    results = await run_server(path, calls)
    part = results[-1]["parts"][0]
    assert part["partId"] == "plate/plate"
    assert part["label"] == "plate"
    assert part["terminalFeatureId"] == "hole_b"
    assert "hole" not in part["partId"]

    store = ViewerSceneStore()
    service = DocumentService(path, scene_store=store)
    deadline = __import__("time").monotonic() + 5
    while store.current_revision != service.revision and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.02)
    scene = store.scene(service.revision)
    assert scene["parts"][0]["partId"] == part["partId"]
    assert scene["parts"][0]["label"] == part["label"]


@pytest.mark.anyio
async def test_separate_lid_is_a_second_stable_part(tmp_path):
    path = tmp_path / "lid.coalesce.json"
    calls = plate_setup() + [
        ("sketch", rectangle_sketch(request_id="lid-s", revision=2, sketch_id="lid_sketch", entity_id="lid", width=58, height=38, offset=0)),
        ("feature", extrude(request_id="lid", revision=3, feature_id="lid", sketch_id="lid_sketch", profile_ids=["lid"], distance=2)),
        ("query", {"select": {"kind": "measurements"}}),
    ]
    result = (await run_server(path, calls))[-1]
    assert len(result["parts"]) == 2
    assert len({part["partId"] for part in result["parts"]}) == 2


@pytest.mark.anyio
async def test_catalog_is_identical_after_restart(tmp_path):
    path = tmp_path / "restart.coalesce.json"
    await run_server(path, plate_setup())
    first = (await run_server(path, [("query", {"select": {"kind": "measurements"}})]))[0]
    second = (await run_server(path, [("query", {"select": {"kind": "measurements"}})]))[0]
    assert second == first


@pytest.mark.anyio
async def test_parameter_edit_keeps_part_id_and_changes_metrics(tmp_path):
    path = tmp_path / "edit.coalesce.json"
    await run_server(path, plate_setup())
    before = (await run_server(path, [("query", {"select": {"kind": "measurements"}})]))[0]
    edited = (await run_server(path, [("feature", {"action": "update", "expectedRevision": 2, "requestId": "thicker", "featureId": "plate", "kind": "extrude", "set": {"distance": {"value": 5, "unit": "mm"}}}), ("query", {"select": {"kind": "measurements"}})]))[-1]
    assert edited["parts"][0]["partId"] == before["parts"][0]["partId"]
    assert edited["volume"] != before["volume"]
    assert edited["bbox"] != before["bbox"]


@pytest.mark.anyio
async def test_split_parts_have_stable_suffixes_after_restart_and_unrelated_edit(tmp_path):
    path = tmp_path / "split.coalesce.json"
    calls = plate_setup() + [
        ("sketch", rectangle_sketch(request_id="cut-s", revision=2, sketch_id="cut_sketch", entity_id="slot", width=10, height=50)),
        ("feature", cut(request_id="cut", revision=3, feature_id="split", target="plate", tool="cut_sketch")),
        ("query", {"select": {"kind": "measurements"}}),
    ]
    first = (await run_server(path, calls))[-1]
    ids = sorted(part["partId"] for part in first["parts"])
    assert len(ids) == 2
    assert all("#" in part_id for part_id in ids)
    restarted = (await run_server(path, [("query", {"select": {"kind": "measurements"}})]))[0]
    assert sorted(part["partId"] for part in restarted["parts"]) == ids


@pytest.mark.anyio
async def test_catalog_aggregates_terminal_parts_and_excludes_consumed_intermediates(tmp_path):
    path = tmp_path / "aggregate.coalesce.json"
    results = await run_server(path, plate_setup() + [
        ("feature", hole(request_id="hole", revision=2, feature_id="hole", target="plate", position=(0, 0))),
        ("query", {"select": {"kind": "measurements"}}),
    ])
    metrics = results[-1]
    assert metrics["solidCount"] == len(metrics["parts"]) == 1
    assert metrics["volume"]["value"] == pytest.approx(sum(part["volume"]["value"] for part in metrics["parts"]))
    assert metrics["area"]["value"] == pytest.approx(sum(part["area"]["value"] for part in metrics["parts"]))
    assert all(part["terminalFeatureId"] != "plate" for part in metrics["parts"])
