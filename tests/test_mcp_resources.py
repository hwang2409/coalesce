import asyncio
import importlib.util
import json
import subprocess
import textwrap
import threading
import time
from pathlib import Path

import pytest
from jsonschema import validate
from mcp import McpError, types

from coalesce import resources, schemas
from coalesce.server import TOOLS_GUIDE, create_server
from coalesce.service import DocumentService

GUIDES = {
    "coalesce://docs/vision": "docs/vision.md",
    "coalesce://docs/errors": "docs/errors.md",
    "coalesce://docs/sketch": "docs/sketch.md",
    "coalesce://docs/hole": "docs/hole.md",
    "coalesce://docs/enclosure": "docs/enclosures.md",
    "coalesce://docs/refresh": "docs/refresh.md",
    "coalesce://docs/revolve": "docs/revolve.md",
    "coalesce://docs/examples/cycloidal-disc": "docs/examples/cycloidal-disc.md",
}


@pytest.mark.anyio
async def test_resource_catalog_reads_canonical_guides_and_session(tmp_path):
    service = DocumentService(
        tmp_path / "resources.coalesce.json", warmup=False, sketch_solve_timeout=2.75
    )
    server = create_server(service)
    listed = await server.request_handlers[types.ListResourcesRequest](types.ListResourcesRequest())
    resources = listed.root.resources
    uris = {str(resource.uri) for resource in resources}

    assert {
        *GUIDES,
        "coalesce://docs/index",
        "coalesce://docs/tools",
        "coalesce://examples/plate",
        "coalesce://session/capabilities",
    } == uris

    read = server.request_handlers[types.ReadResourceRequest]
    for uri, path in GUIDES.items():
        result = await read(types.ReadResourceRequest(params=types.ReadResourceRequestParams(uri=uri)))
        assert result.root.contents[0].text.encode() == Path(path).read_bytes()

    session_result = await read(
        types.ReadResourceRequest(
            params=types.ReadResourceRequestParams(uri="coalesce://session/capabilities")
        )
    )
    session = json.loads(session_result.root.contents[0].text)
    assert session["version"] == "0.1.0"
    assert session["sketchSolveTimeoutSeconds"] == 2.75
    assert session["viewerUrl"] is None
    assert session["optionalSolver"] == {
        "name": "planegcs",
        "available": importlib.util.find_spec("planegcs") is not None,
    }
    assert "extrude" in session["featureKinds"]
    assert "valid_brep" in session["checkKinds"]

    index_result = await read(
        types.ReadResourceRequest(
            params=types.ReadResourceRequestParams(uri="coalesce://docs/index")
        )
    )
    index = json.loads(index_result.root.contents[0].text)
    assert {item["uri"] for item in index["resources"]} == uris
    assert "coalesce://" in index["uriScheme"]
    assert "coalesce://docs/tools" in TOOLS_GUIDE

    with pytest.raises(McpError) as raised:
        await read(
            types.ReadResourceRequest(
                params=types.ReadResourceRequestParams(uri="coalesce://docs/unknown")
            )
        )
    assert raised.value.error.code == types.INVALID_PARAMS
    assert raised.value.error.data == {
        "code": "RESOURCE_NOT_FOUND",
        "uri": "coalesce://docs/unknown",
    }


@pytest.mark.anyio
async def test_resource_read_does_not_block_event_loop_on_document_lock(tmp_path):
    service = DocumentService(tmp_path / "locked.coalesce.json", warmup=False)
    server = create_server(service)
    read = server.request_handlers[types.ReadResourceRequest]
    lock_held = threading.Event()

    def hold_lock():
        with service._lock:
            lock_held.set()
            time.sleep(0.4)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert lock_held.wait(timeout=1)

    ticks = []
    started = time.monotonic()

    async def heartbeat():
        for _ in range(5):
            await asyncio.sleep(0.05)
            ticks.append(time.monotonic() - started)

    await asyncio.gather(
        heartbeat(),
        read(
            types.ReadResourceRequest(
                params=types.ReadResourceRequestParams(uri="coalesce://session/capabilities")
            )
        ),
    )
    assert thread.is_alive()
    thread.join()
    assert len(ticks) == 5
    assert ticks[0] < 0.2


@pytest.mark.anyio
async def test_tool_list_serializes_valid_design_intent_examples(tmp_path):
    server = create_server(DocumentService(tmp_path / "recipes.coalesce.json", warmup=False))
    result = await server.request_handlers[types.ListToolsRequest](types.ListToolsRequest())
    snapshot = json.loads(result.model_dump_json(by_alias=True, exclude_none=True))
    tools = {tool["name"]: tool for tool in snapshot["tools"]}

    for name in ("sketch", "feature"):
        schema = tools[name]["inputSchema"]
        assert schema["examples"]
        for example in schema["examples"]:
            validate(example, schema)

    sketch_example = tools["sketch"]["inputSchema"]["examples"][0]
    feature_example = tools["feature"]["inputSchema"]["examples"][0]
    constrained = sketch_example["constrained"]
    assert sum(item["kind"] == "fix" for item in constrained["constraints"]) == 1
    assert {item["id"] for item in constrained["dimensions"]} >= {
        "width",
        "height",
        "opening-radius",
    }
    assert feature_example["parameters"]["profile"] == {
        "sketchId": "profile",
        "profileIds": ["profile"],
    }

    call = server.request_handlers[types.CallToolRequest]
    for name, example in (("sketch", sketch_example), ("feature", feature_example)):
        result = await call(
            types.CallToolRequest(
                params=types.CallToolRequestParams(name=name, arguments=example)
            )
        )
        assert result.root.isError is False

    serialized = json.dumps(snapshot).lower()
    assert "fixing every point" in serialized
    assert "opening-radius" in serialized
    assert "outer plus inner loops as holes" in serialized
    assert "chain each returned revision" in serialized
    assert "coalesce://docs/hole" in serialized


def test_packaged_guides_match_inline_design_intent_recipes():
    [sketch] = resources.read_resource(
        "coalesce://docs/sketch", sketch_solve_timeout=1.0, viewer_url=None
    )
    [hole] = resources.read_resource(
        "coalesce://docs/hole", sketch_solve_timeout=1.0, viewer_url=None
    )

    assert "fully_constrained` with zero degrees of freedom" in sketch.content
    assert "no editable driving dimensions" in sketch.content
    assert "same point, entity,\ndimension, and constraint ids" in sketch.content
    assert "sketch id as the only `profileIds` value" in sketch.content
    assert "keep the existing `featureId`" in hole.content
    assert "chain the returned revision" in hole.content


def test_missing_packaged_guide_returns_registered_resource_error(monkeypatch):
    def missing_guide(_path):
        raise FileNotFoundError("removed from scratch install")

    monkeypatch.setattr(resources, "_guide_bytes", missing_guide)

    with pytest.raises(McpError) as raised:
        resources.read_resource(
            "coalesce://docs/vision", sketch_solve_timeout=1.0, viewer_url=None
        )

    assert raised.value.error.data == {
        "code": "RESOURCE_UNAVAILABLE",
        "uri": "coalesce://docs/vision",
    }


def test_session_capabilities_follow_dispatch_registries(monkeypatch):
    monkeypatch.setitem(schemas.FEATURE_ACTION_SCHEMAS, ("create", "probe_feature"), {})

    [result] = resources.read_resource(
        "coalesce://session/capabilities", sketch_solve_timeout=1.0, viewer_url=None
    )
    capabilities = json.loads(result.content)

    assert "probe_feature" in capabilities["featureKinds"]
    assert capabilities["checkKinds"] == list(schemas.SUPPORTED_REQUIREMENT_KINDS)
    assert schemas.CHECK_REQUIREMENT["properties"]["kind"]["enum"] == list(
        schemas.SUPPORTED_REQUIREMENT_KINDS
    )


def test_built_wheel_serves_resources_over_stdio_outside_repo(tmp_path):
    root = Path(__file__).parents[1]
    wheel_dir = tmp_path / "wheel"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)], cwd=root, check=True
    )
    wheel = next(wheel_dir.glob("coalesce-*.whl"))
    venv = tmp_path / "venv"
    subprocess.run(["uv", "venv", "--python", "3.13", str(venv)], check=True)
    python = venv / "bin" / "python"
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(wheel)], check=True
    )

    outside = tmp_path / "outside"
    outside.mkdir()
    script = textwrap.dedent(
        f"""
        import anyio
        import json
        from mcp import ClientSession, McpError, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def main():
            params = StdioServerParameters(
                command={str(venv / "bin" / "coalesce")!r},
                args=["--document", "part.coalesce.json", "--no-viewer",
                      "--sketch-solve-timeout", "3.25"],
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    resources = await session.list_resources()
                    values = {{}}
                    for resource in resources.resources:
                        result = await session.read_resource(resource.uri)
                        values[str(resource.uri)] = result.contents[0].text
                    try:
                        await session.read_resource("coalesce://docs/unknown")
                    except McpError as error:
                        unknown = error.error.model_dump()
                    else:
                        raise AssertionError("unknown resource succeeded")
                    print(json.dumps({{"values": values, "unknown": unknown}}))

        anyio.run(main)
        """
    )
    completed = subprocess.run(
        [str(python), "-c", script], cwd=outside, text=True, capture_output=True, check=True
    )
    result = json.loads(completed.stdout)
    values = result["values"]
    assert set(GUIDES) <= set(values)
    for uri, path in GUIDES.items():
        assert values[uri].encode() == (root / path).read_bytes()
    session = json.loads(values["coalesce://session/capabilities"])
    assert session["sketchSolveTimeoutSeconds"] == 3.25
    assert session["viewerUrl"] is None
    assert session["optionalSolver"] == {"name": "planegcs", "available": False}
    assert result["unknown"]["code"] == types.INVALID_PARAMS
    assert result["unknown"]["data"]["code"] == "RESOURCE_NOT_FOUND"
