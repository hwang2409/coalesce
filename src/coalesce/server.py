"""Stdio MCP adapter for the coalesce document module."""

from __future__ import annotations

import json
import logging
from typing import Any

from jsonschema import ValidationError, validate
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .schemas import TOOL_SCHEMAS
from .service import DocumentService

_LOG = logging.getLogger(__name__)

_TOOL_DESCRIPTIONS = {
    "sketch": "Create or update one rectangle sketch. Every mutation needs expectedRevision and requestId.",
    "feature": "Create or update an extrude. Boolean, hole, and fillet are reserved but not yet available.",
    "query": "Read the current graph, revision, exact mm metrics, or resolve a typed reference.",
    "check": "Run exact validity checks against the current regenerated B-rep.",
}

TOOLS_GUIDE = """# coalesce slice 1 tools

Start with `coalesce --document part.coalesce.json` or set `COALESCE_DOCUMENT`.
coalesce owns one revisioned document. Start with `query` using `select.kind: revision`.
Use `sketch` to create an XY rectangle, then `feature` to extrude it. Mutations need the
current `expectedRevision` and a unique `requestId`. Full replay receipts cover the last
100 mutations; compact request-id/hash tombstones reject changed reuse for the last 10,000.
All lengths are explicit mm quantities. Rectangle angles accept `deg` or `rad`.
Use `query` with `measurements` for bbox, area, volume, and solid count. Use `check` with
`valid_brep` before relying on the model. Domain failures have `ok: false` and a stable code.
"""

PLATE_EXAMPLE = """# 60 x 40 x 6 mm plate

1. `sketch`: create `plate_profile` at revision 0 with rectangle `outline`, width 60 mm,
   height 40 mm, center [0, 0], on the XY origin plane.
2. `feature`: create extrude `plate_extrude` at revision 1 with profile `outline`, distance
   6 mm, operation `new_body`, and direction `normal`.
3. `query`: select `measurements`; expect bbox 60 x 40 x 6 mm and volume 14400 mm^3.
4. `check`: evaluate a `valid_brep` requirement.
"""


def _tool_result(payload: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, indent=2))],
        structuredContent=payload,
        isError=not payload.get("ok", False),
    )


def _schema_for_action(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    action = arguments.get("action")
    return next(
        (
            branch
            for branch in schema.get("oneOf", [])
            if branch.get("properties", {}).get("action", {}).get("const") == action
        ),
        schema,
    )


def create_server(service: DocumentService) -> Server:
    server = Server(
        "coalesce",
        version="0.1.0",
        instructions="Read coalesce://docs/tools, then use query before mutations.",
    )

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=name,
                description=_TOOL_DESCRIPTIONS[name],
                inputSchema=schema,
            )
            for name, schema in TOOL_SCHEMAS.items()
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        if name not in TOOL_SCHEMAS:
            return _tool_result(
                {
                    "ok": False,
                    "revision": service.revision,
                    "requestId": arguments.get("requestId"),
                    "units": "mm",
                    "error": {
                        "code": "INVALID_PARAMETER",
                        "message": f"unknown tool: {name}",
                        "retryable": False,
                        "operation": name,
                    },
                }
            )
        try:
            validate(
                instance=arguments,
                schema=_schema_for_action(TOOL_SCHEMAS[name], arguments),
            )
        except ValidationError as error:
            details: dict[str, Any] = {}
            if name == "feature" and error.validator == "enum" and list(error.path) == ["kind"]:
                details["allowedKinds"] = ["extrude", "boolean", "hole", "fillet"]
            if error.validator == "additionalProperties":
                details["allowedFields"] = sorted(error.schema.get("properties", {}))
            return _tool_result(
                {
                    "ok": False,
                    "revision": service.revision,
                    "requestId": arguments.get("requestId"),
                    "units": "mm",
                    "error": {
                        "code": "INVALID_PARAMETER",
                        "message": error.message,
                        "retryable": False,
                        "operation": name,
                        **details,
                    },
                }
            )
        try:
            handler = getattr(service, name)
            return _tool_result(handler(arguments))
        except Exception:  # noqa: BLE001 - raw failures stay on stderr at the transport seam.
            _LOG.exception("unexpected failure during %s", name)
            return _tool_result(
                {
                    "ok": False,
                    "revision": service.revision,
                    "requestId": arguments.get("requestId"),
                    "units": "mm",
                    "error": {
                        "code": "KERNEL_OPERATION_FAILED",
                        "message": "the operation failed unexpectedly",
                        "retryable": False,
                        "operation": name,
                    },
                }
            )

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        return [
            types.Resource(
                name="coalesce tool guide",
                uri="coalesce://docs/tools",
                description="Short usage guide for the revisioned modeling tools.",
                mimeType="text/markdown",
            ),
            types.Resource(
                name="coalesce plate example",
                uri="coalesce://examples/plate",
                description="A complete rectangle and extrude workflow.",
                mimeType="text/markdown",
            ),
        ]

    @server.read_resource()
    async def read_resource(uri):
        value = str(uri)
        if value == "coalesce://docs/tools":
            return TOOLS_GUIDE
        if value == "coalesce://examples/plate":
            return PLATE_EXAMPLE
        raise ValueError("unknown coalesce resource")

    return server


async def run_stdio(service: DocumentService) -> None:
    server = create_server(service)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
