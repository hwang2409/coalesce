import importlib.util
import json
import subprocess
import textwrap
from pathlib import Path

import pytest
from mcp import McpError, types

from coalesce.server import create_server
from coalesce.service import DocumentService

GUIDES = {
    "coalesce://docs/vision": "docs/vision.md",
    "coalesce://docs/errors": "docs/errors.md",
    "coalesce://docs/sketch": "docs/sketch.md",
    "coalesce://docs/hole": "docs/hole.md",
    "coalesce://docs/enclosure": "docs/enclosures.md",
    "coalesce://docs/refresh": "docs/refresh.md",
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

    assert {*GUIDES, "coalesce://docs/index", "coalesce://session/capabilities"} <= uris

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
