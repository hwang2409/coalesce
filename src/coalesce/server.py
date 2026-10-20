"""Stdio MCP adapter for the coalesce document module."""

from __future__ import annotations

import json
import logging
from typing import Any

from jsonschema import ValidationError, validate
from jsonschema.exceptions import best_match, relevance
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .schemas import ACTION_SCHEMAS, TOOL_SCHEMAS
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
Use `query` with `measurements` for bbox, area, volume, and solid count. `query` with
`server` returns the full viewer link, including its fragment access token, so the agent can
show the link to the user. Use `check` with `valid_brep` before relying on the model.
Domain failures have `ok: false` and a stable code.
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


_TYPE_NAMES = {
    "array": "an array",
    "object": "an object",
    "string": "a string",
    "number": "a number",
    "integer": "an integer",
    "boolean": "a boolean",
    "null": "null",
}


def _quoted(values) -> str:
    return ", ".join(repr(value) for value in values)


def schema_error_message(error: ValidationError) -> str:
    """Stable coalesce wording for a schema failure; never the jsonschema library text."""
    path = "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path
    ).lstrip(".")
    last = error.absolute_path[-1] if error.absolute_path else None
    subject = last if isinstance(last, str) else "value"
    kind, expected, instance = error.validator, error.validator_value, error.instance
    if kind == "type":
        types_ = [expected] if isinstance(expected, str) else expected
        text = f"{subject} must be " + " or ".join(_TYPE_NAMES.get(t, t) for t in types_)
    elif kind == "additionalProperties" and isinstance(instance, dict):
        unknown = sorted(set(instance) - set(error.schema.get("properties", {})))
        text = f"unknown field{'s' if len(unknown) > 1 else ''} {_quoted(unknown)}"
    elif kind == "required" and isinstance(instance, dict):
        missing = next(name for name in expected if name not in instance)
        text = f"missing required field {missing!r}"
    elif kind == "minimum":
        text = f"{subject} must be at least {expected}"
    elif kind == "enum":
        text = f"{subject} must be one of {_quoted(expected)}"
    elif kind == "const":
        text = f"{subject} must be {expected!r}"
    else:
        text = "invalid value"
    return f"{text} (field: {path})" if path else text


def _is_kind_union(error: ValidationError) -> bool:
    return (
        error.validator in ("oneOf", "anyOf")
        and isinstance(error.instance, dict)
        and "kind" in error.instance
    )


def _branch_error(error: ValidationError) -> ValidationError:
    """Report a union failure from the branch the supplied `kind` selects, not a wrong branch."""
    union = error
    while union.parent is not None:
        union = union.parent
    if not _is_kind_union(union):
        return error
    while _is_kind_union(union):
        kinds = [branch.get("properties", {}).get("kind", {}) for branch in union.validator_value]
        allowed = [
            value for k in kinds for value in ([k["const"]] if "const" in k else k.get("enum", []))
        ]
        index = next(
            (
                i
                for i, k in enumerate(kinds)
                if k and union.instance["kind"] in (k.get("const"), *k.get("enum", ()))
            ),
            None,
        )
        if index is None:
            return ValidationError(
                "kind",
                validator="enum" if len(allowed) > 1 else "const",
                validator_value=allowed if len(allowed) > 1 else allowed[0],
                instance=union.instance["kind"],
                path=[*union.absolute_path, "kind"],
            )
        branch = [e for e in union.context if e.relative_schema_path[0] == index]
        if not branch:
            return error
        union = max(branch, key=relevance)
    return best_match([union])


def _schema_for_action(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return ACTION_SCHEMAS.get(name, {}).get(arguments.get("action"), TOOL_SCHEMAS[name])


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
                schema=_schema_for_action(name, arguments),
            )
        except ValidationError as raised:
            error = _branch_error(raised)
            details: dict[str, Any] = {}
            if name == "feature" and error.validator == "enum" and list(error.path) == ["kind"]:
                details["allowedKinds"] = ["extrude", "boolean", "hole", "fillet"]
            if error.validator == "additionalProperties":
                details["allowedFields"] = sorted(error.schema.get("properties", {}))
            failure = {
                "ok": False,
                "revision": service.revision,
                "requestId": arguments.get("requestId"),
                "units": "mm",
                "error": {
                    "code": "INVALID_PARAMETER",
                    "message": schema_error_message(error),
                    "retryable": False,
                    "operation": name,
                    **details,
                },
            }
            if name in ("sketch", "feature"):
                service.publish_failure(arguments, failure)
            return _tool_result(failure)
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
