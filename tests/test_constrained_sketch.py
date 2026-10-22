from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def rectangle_model(*, width: float = 30.0, fixed_corner: bool = True) -> dict:
    points = [
        {"id": "p0", "x": 1.0, "y": 2.0},
        {"id": "p1", "x": 31.2, "y": 2.1},
        {"id": "p2", "x": 31.0, "y": 2.2},
        {"id": "p3", "x": 31.1, "y": 22.0},
        {"id": "p4", "x": 30.9, "y": 22.1},
        {"id": "p5", "x": 1.1, "y": 21.9},
        {"id": "p6", "x": 0.9, "y": 22.0},
        {"id": "p7", "x": 1.0, "y": 2.1},
    ]
    lines = [
        {"id": f"line{i}", "start": f"p{2 * i}", "end": f"p{2 * i + 1}"}
        for i in range(4)
    ]
    constraints = [
        {
            "id": f"join{i}",
            "kind": "coincident",
            "entities": [f"p{2 * i + 1}", f"p{(2 * (i + 1)) % 8}"],
        }
        for i in range(4)
    ]
    constraints.extend(
        {"id": f"axis{i}", "kind": kind, "entities": [f"line{i}"]}
        for i, kind in enumerate(("horizontal", "vertical", "horizontal", "vertical"))
    )
    if fixed_corner:
        constraints.append({"id": "origin", "kind": "fix", "entities": ["p0"]})
    return {
        "kind": "constrained",
        "points": points,
        "lines": lines,
        "arcs": [],
        "circles": [],
        "dimensions": [
            {"id": "width", "kind": "distance", "entities": ["p0", "p1"], "value": width},
            {"id": "height", "kind": "distance", "entities": ["p2", "p3"], "value": 20.0},
        ],
        "constraints": constraints,
        "initial": {"p1": [31.2, 2.1]},
        "landmarks": [
            {
                "id": "upper-side",
                "base_start": "p0",
                "base_end": "p1",
                "point": "p3",
                "expected_sign": 1,
                "constraint_ids": ["width", "height"],
            }
        ],
    }


def fixed_pair_model(distance: float) -> dict:
    return {
        "kind": "constrained",
        "points": [{"id": "p", "x": 0, "y": 0}, {"id": "q", "x": 5, "y": 0}],
        "lines": [{"id": "edge", "start": "p", "end": "q"}],
        "dimensions": [
            {"id": f"distance-{distance:g}", "kind": "distance", "entities": ["p", "q"], "value": distance}
        ],
        "constraints": [
            {"id": "fix-p", "kind": "fix", "entities": ["p"]},
            {"id": "fix-q", "kind": "fix", "entities": ["q"]},
        ],
    }


def branch_flip_model() -> dict:
    return {
        "kind": "constrained",
        "points": [
            {"id": "a", "x": 0, "y": 0},
            {"id": "b", "x": 6, "y": 0},
            {"id": "apex", "x": 3, "y": -4},
        ],
        "lines": [
            {"id": "base", "start": "a", "end": "b"},
            {"id": "left", "start": "a", "end": "apex"},
            {"id": "right", "start": "b", "end": "apex"},
        ],
        "dimensions": [
            {"id": "left-length", "kind": "distance", "entities": ["a", "apex"], "value": 6},
            {"id": "right-length", "kind": "distance", "entities": ["b", "apex"], "value": 6},
        ],
        "constraints": [
            {"id": "fix-a", "kind": "fix", "entities": ["a"]},
            {"id": "fix-b", "kind": "fix", "entities": ["b"]},
        ],
        "landmarks": [
            {
                "id": "apex-side",
                "base_start": "a",
                "base_end": "b",
                "point": "apex",
                "expected_sign": 1,
                "constraint_ids": ["left-length", "right-length"],
            }
        ],
    }


def create_request(model: dict, *, request_id: str = "create") -> dict:
    return {
        "action": "create",
        "expectedRevision": 0,
        "requestId": request_id,
        "sketchId": "profile",
        "plane": {"kind": "origin", "name": "XY"},
        "constrained": model,
    }


def update_request(model: dict, revision: int, request_id: str) -> dict:
    return {
        "action": "update",
        "expectedRevision": revision,
        "requestId": request_id,
        "sketchId": "profile",
        "set": {"constrained": model},
    }


def primitive_request(*, revision: int, request_id: str) -> dict:
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": request_id,
        "sketchId": "primitive",
        "plane": {"kind": "origin", "name": "XY"},
        "entities": [
            {
                "id": "outline",
                "kind": "rectangle",
                "width": {"value": 10, "unit": "mm"},
                "height": {"value": 8, "unit": "mm"},
            }
        ],
    }


def result_json(result) -> dict:
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


async def run_server(path: Path, calls: list[tuple[str, dict]], *, env: dict | None = None) -> list[dict]:
    server = StdioServerParameters(
        command=shutil.which("coalesce") or "coalesce",
        args=["--document", str(path)],
        env={**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})},
    )
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        return [result_json(await session.call_tool(name, arguments)) for name, arguments in calls]


def graph_sketch(query: dict) -> dict:
    return next(node for node in query["graph"]["nodes"] if node["id"] == "profile")


@pytest.mark.anyio
async def test_fully_constrained_create_reload_and_query_matches_dimensions(tmp_path):
    path = tmp_path / "full.coalesce.json"
    created = (await run_server(path, [("sketch", create_request(rectangle_model()))]))[0]

    assert created["ok"] is True
    assert created["solve"]["status"] == "fully_constrained"
    assert created["solve"]["dof"] == 0
    points = created["solve"]["entities"]
    assert abs((points["p1"]["x"] - points["p0"]["x"]) - 30.0) <= 1e-9
    assert abs((points["p3"]["y"] - points["p2"]["y"]) - 20.0) <= 1e-9

    reloaded = (await run_server(path, [("query", {"select": {"kind": "graph"}})]))[0]
    solved = graph_sketch(reloaded)["constrained"]["solved"]
    assert reloaded["revision"] == 1
    assert solved["status"] == "fully_constrained"
    assert solved["dof"] == 0
    assert solved["diagnostics"] == {
        "iterationsOrDetail": {},
        "redundantConstraints": [],
        "conflictingConstraints": [],
        "errors": [],
    }
    assert abs((solved["entities"]["p1"]["x"] - solved["entities"]["p0"]["x"]) - 30.0) <= 1e-9
    assert abs((solved["entities"]["p3"]["y"] - solved["entities"]["p2"]["y"]) - 20.0) <= 1e-9


@pytest.mark.anyio
async def test_primitive_and_constrained_forms_are_mutually_exclusive(tmp_path):
    request = create_request(rectangle_model())
    request["entities"] = primitive_request(revision=0, request_id="primitive")["entities"]

    result = (await run_server(tmp_path / "exclusive.coalesce.json", [("sketch", request)]))[0]

    assert result["ok"] is False
    assert result["revision"] == 0
    assert result["error"]["code"] == "INVALID_PARAMETER"


@pytest.mark.anyio
async def test_under_constrained_sketch_is_accepted_and_reported(tmp_path):
    result = (
        await run_server(
            tmp_path / "under.coalesce.json",
            [("sketch", create_request(rectangle_model(fixed_corner=False)))],
        )
    )[0]

    assert result["ok"] is True
    assert result["revision"] == 1
    assert result["solve"]["status"] == "under_constrained"
    assert result["solve"]["dof"] == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("model", "code"),
    [
        (fixed_pair_model(5), "SKETCH_REDUNDANT"),
        (fixed_pair_model(10), "SKETCH_CONFLICTING"),
        (branch_flip_model(), "SKETCH_BRANCH_FLIP"),
    ],
)
async def test_rejected_updates_leave_revision_and_file_unchanged(tmp_path, model, code):
    path = tmp_path / f"{code}.coalesce.json"
    accepted = (await run_server(path, [("sketch", create_request(rectangle_model()))]))[0]
    saved = path.read_bytes()

    rejected = (
        await run_server(path, [("sketch", update_request(model, 1, f"reject-{code}"))])
    )[0]

    assert accepted["revision"] == 1
    assert rejected["ok"] is False
    assert rejected["revision"] == 1
    assert rejected["error"]["code"] == code
    assert path.read_bytes() == saved


@pytest.mark.anyio
async def test_missing_solver_extra_is_structured_and_primitive_still_works(tmp_path):
    blocker = tmp_path / "without-planegcs"
    blocker.mkdir()
    (blocker / "planegcs.py").write_text("raise ImportError('blocked optional dependency')\n")
    path = tmp_path / "base-install.coalesce.json"
    env = {"PYTHONPATH": str(blocker)}

    constrained, primitive = await run_server(
        path,
        [
            ("sketch", create_request(rectangle_model())),
            ("sketch", primitive_request(revision=0, request_id="primitive")),
        ],
        env=env,
    )

    assert constrained["ok"] is False
    assert constrained["revision"] == 0
    assert constrained["error"]["code"] == "SKETCH_SOLVER_UNAVAILABLE"
    assert "coalesce[sketch]" in constrained["error"]["message"]
    assert primitive["ok"] is True
    assert primitive["revision"] == 1


@pytest.mark.anyio
async def test_save_reload_preserves_constraint_model_and_landmarks_exactly(tmp_path):
    path = tmp_path / "roundtrip.coalesce.json"
    model = rectangle_model()
    created = await run_server(
        path,
        [
            ("sketch", create_request(model)),
            ("query", {"select": {"kind": "graph"}}),
        ],
    )
    reloaded = (await run_server(path, [("query", {"select": {"kind": "graph"}})]))[0]
    before = graph_sketch(created[1])["constrained"]
    after = graph_sketch(reloaded)["constrained"]

    assert after == before
    assert after["kind"] == "constrained"
    assert after["initial"] == model["initial"]
    assert after["landmarks"] == model["landmarks"]
    assert after["solved"]["landmarks"] == {"upper-side": pytest.approx(600.0, abs=1e-9)}


@pytest.mark.anyio
async def test_update_and_reload_resolve_instead_of_trusting_cached_output(tmp_path):
    path = tmp_path / "resolve.coalesce.json"
    created, updated = await run_server(
        path,
        [
            ("sketch", create_request(rectangle_model())),
            ("sketch", update_request(rectangle_model(width=42.0), 1, "resize")),
        ],
    )
    assert created["solve"]["entities"]["p1"]["x"] == pytest.approx(31.0, abs=1e-9)
    assert updated["solve"]["entities"]["p1"]["x"] == pytest.approx(43.0, abs=1e-9)

    document = json.loads(path.read_text())
    constrained = document["graph"]["nodes"][0]["constrained"]
    constrained["solved"]["entities"]["p1"]["x"] = -999.0
    constrained["solved"]["dof"] = 999
    path.write_text(json.dumps(document))

    reloaded = (await run_server(path, [("query", {"select": {"kind": "graph"}})]))[0]
    solved = graph_sketch(reloaded)["constrained"]["solved"]
    assert solved["dof"] == 0
    assert solved["entities"]["p1"]["x"] == pytest.approx(43.0, abs=1e-9)
    assert solved["entities"]["p1"]["x"] != -999.0
