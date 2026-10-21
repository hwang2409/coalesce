"""Stdio MCP adapter for the coalesce document module."""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

from jsonschema import ValidationError, validate
from jsonschema.exceptions import best_match, relevance
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .schemas import ACTION_SCHEMAS, FEATURE_ACTION_SCHEMAS, QUERY_FEATURE_SCHEMA, TOOL_SCHEMAS
from .service import DocumentService

_LOG = logging.getLogger(__name__)

_TOOL_DESCRIPTIONS = {
    "import": "Snapshot one explicit absolute local kicad_pcb path as a persisted BoardImport and exact reference substrate. Reads regular files up to 16 MiB, with no network or environment-variable model resolution. Use refresh to re-read the persisted path explicitly. Use delete to remove the board asset; it fails when dependents exist unless cascade is true.",
    "sketch": "Create, update, or delete rectangle and circle profiles on XY, XZ, or YZ origin planes with an optional normal offset. Supplied entities replace the full entity list. Use update to change a sketch in place. Use delete to remove it; delete fails when dependent features exist unless cascade is true.",
    "feature": "Create a normal new-body or target-join extrude, sketch cut, global +Z hole, semantic pcb_base, semantic standoffs, a separate semantic pcb_lid, or semantic port_cutout. pcb_base derives its cavity from the selected board outline; standoffs derive axes from stable board hole ids and join the base; pcb_lid derives its rim and optional locating lip from a pcb_base without consuming it; port_cutout resolves a stable footprint id or unique reference and cuts its declared opening through one planar base wall. For port_cutout, left/right are -X/+X and front/back are -Y/+Y. auto is allowed only for one unique nearest wall. A courtyard supplies only the wall-plane width; height and bottomOffset from board top remain explicit. An explicit envelope supplies width, height, and bottomOffset. No connector height or opening size is inferred. Plane normals are XY +Z, XZ -Y, YZ +X. For finite cuts, flip=false cuts along the negative sketch normal and flip=true along the positive sketch normal; through-all cuts are bidirectional, and flip has no effect for through-all cuts. Holes accept one position or 1..64 named positions; names are stable caller identities and default to h1..hn. Responses report the global frame, operation direction, and tool bbox. Enclosure geometry conflicts include computed `limits` when a numeric repair is available and `alternatives` with directly applicable, regenerated `set` values when another port wall or envelope position is proven safe. Connector envelope width and height are never changed by a remedy. Updates change numeric enclosure parameters, extrude distance, or cut and hole targets. Use update to preserve a feature identity and change its parameters. Use delete to remove a feature; delete fails with every transitive dependent unless cascade is true.",
    "query": "Read one feature, the graph, revision, exact mm metrics, inspect a PCB enclosure, or resolve a typed reference. Call select.kind enclosure before reporting enclosure dimensions.",
    "check": "Run exact validity checks against the current regenerated B-rep.",
    "export": "Write selected terminal manufacturing parts to STEP at an explicit absolute new path and current expectedRevision. Defaults to all manufacturing parts and never includes BoardImport reference geometry. It never overwrites. A fresh Python process validates STEP bytes in a private directory before direct publication. The final file is briefly visible while written; failed writes stay in place for manual recovery.",
}

TOOLS_GUIDE = """# coalesce tools

Start with `coalesce --document part.coalesce.json` or set `COALESCE_DOCUMENT`.
coalesce owns one revisioned document. Start with `query` using `select.kind: revision`.
Use `import` create with an explicit absolute local `kicad_pcb` path to persist a
`BoardImport` snapshot. The source must be a readable regular file of 16 MiB or less.
The import stores the resolved path, SHA-256 digest, parser version, stable mechanics ids,
availability, warnings, and errors. It regenerates the exact board substrate as reference
geometry from line, arc, circle, cutout, and drill data. It does not access the network or
resolve environment variables in model paths. Later source edits do not affect the document; use
`import` refresh to read the persisted path and commit a new snapshot explicitly.
Use `sketch` for rectangles or circles on XY, XZ, or YZ. Local axes are XY=(+X,+Y), XZ=(+X,+Z), YZ=(+Y,+Z). Plane offset moves along its normal.
Plane normals are XY +Z, XZ -Y, YZ +X.
Extrusion starts at the sketch plane and extends along its normal; it is not centered.
Use operation `join` with an explicit target feature to add material to one contacting body.
A join, cut, hole, or fillet must target the current terminal feature of its body lineage.
Use `feature` update with `set.target.featureId` to retarget an existing cut or hole.
For finite cuts, `flip=false` cuts along the negative sketch normal and `flip=true` cuts
along the positive sketch normal. Through-all cuts are bidirectional, and flip has no effect for through-all cuts.
Holes use absolute XY positions and global +Z only. Omitted hole depth means through-all.
A hole accepts one `position` or 1..64 `positions`; each position can have a stable,
caller-chosen `name`, defaulting to `h1`..`hn` in request order. Use that name in a
`hole_surface` reference. Sketch and feature responses report the global frame; feature
responses also report the direction vector and exact tool/cutter bbox.

For a PCB enclosure, create `pcb_base` with a BoardImport id, wall and floor thickness,
board-edge clearance, wall height above the board top, and board seat height. The inner
cavity is the board outline offset outward by the clearance; arcs remain arcs. The board
stays at its imported Z position. The floor top is `-boardSeatHeight`, the floor bottom is
lower by `floorThickness`, and the wall top is the board top plus
`wallHeightAboveBoard`. Then create `standoffs` with that base as target and the same board.
Use `selection.mode: explicit` with stable `holeIds`, or `all_mounting` with
`minimumDiameter`; all plated and non-plated drills whose smaller drill dimension meets the
minimum are selected. Boss height is derived from the base seat height, so the board bottom
sits on the boss tops. `pilotDiameter` must be smaller than `bossDiameter`, and every boss
must fit fully on the cavity floor without crossing a wall. Board refresh recomputes the
base and standoff positions; a missing explicit hole id fails the refresh atomically.

Create `pcb_lid` with `base` set to the `pcb_base` id, positive `thickness` and
`fitGap` quantities, and an optional `lip` containing positive `depth` and `width`.
The lid is a separate terminal manufacturing part. Its underside sits on the base rim and
its outer outline follows the base outer outline. The lip outer faces follow the base inner
wall inward by the radial `fitGap`; the lip extends another `width` inward, and outline arcs
remain arcs. The lip bottom must stay above both the board top and any standoff top. A lid
reads but does not consume the base, so later features can still target the current base
lineage. Base edits and board refresh regenerate the lid. Updates accept `thickness`,
`fitGap`, or a complete replacement `lip`. Provenance exposes `top`, `underside`,
`lip_outer`, `lip_inner`, and `edge`, linked to the base id.

Create `port_cutout` after standoffs with `base` set to the original `pcb_base` id and
`footprint` set to a stable footprint id or a reference that resolves once. `wall` is one of
`left` (-X), `right` (+X), `front` (-Y), `back` (+Y), or `auto`; auto fails unless one wall
is uniquely nearest to the footprint envelope. A courtyard envelope derives only its
wall-plane width. Both courtyard and explicit envelopes require an explicit `height` and
board-top-relative `bottomOffset`; explicit also requires `width`. Positive `clearance` is
added around all opening edges. The response reports the resolved footprint id, wall,
envelope source, exact opening, and whether it reaches below the board top. Missing
footprints or courtyards and ambiguous references or walls fail atomically.

Short enclosure sequence: `import` the board, create `pcb_base`, create `standoffs`, create
`pcb_lid`, create `port_cutout`, run `check` with `valid_brep`, then `export` the two terminal manufacturing parts.
Export excludes the board reference geometry.

Ask the user before changing geometry when a dimension is vague, such as "a bit wider".
Mutations need the current `expectedRevision` and a unique `requestId`; exact replay is
idempotent. Use update when the same sketch or feature should remain in the timeline with new
parameters. Use delete to remove a sketch, feature, or imported board. Delete without cascade
fails with every transitive dependent in graph order; delete those first or pass `cascade: true`
to remove the target and all dependents in one revision. Supplied update entities replace the
full list; omitted entities stay unchanged.
All lengths are explicit mm quantities. Rectangle angles accept `deg` or `rad`.
Use `query` with `enclosure` and a `base` pcb_base id before reporting enclosure dimensions.
It compactly measures the board, base walls and cavity, standoff coverage, lids, ports,
assembled size, terminal parts, and orphan warnings from regenerated OCCT geometry.
Use `query` with `measurements` for bbox placement, area, volume, and terminal bodies. It
returns manufacturing parts by default; set `select.role: reference` to measure imported PCB
reference geometry separately. Reference parts remain visible in the viewer but are excluded
from manufacturing totals. Use `select.kind: feature` with `id` for one node. Use `check`
with `valid_brep` before relying on the model. It checks terminal manufacturing parts only and
excludes BoardImport reference geometry; `PASS` with `solidCount: 0` means no manufacturing
geometry was checked. Use `export` with the current `expectedRevision` and an explicit absolute new
`path`; export is stateless and does not accept `requestId`. It defaults to all terminal
manufacturing parts; select stable `partIds` from measurements when needed. BoardImport
reference geometry is never included. STEP export never overwrites, renames, truncates, or
removes a destination. It validates bytes in a private directory. A fresh Python process
reimports them and compares part and solid counts, bounding boxes, per-part and
aggregate volume and area, and cylinder radii and axes. The private directory is removed
after success, validation failure, or timeout. It then creates the final path directly and
writes the validated in-memory bytes. The final file is briefly visible while it is written.
Parent directories resolve normally; only the final path component must be new and is protected
by `O_EXCL | O_NOFOLLOW`. Success returns `path`, `sha256`, `byteCount`, and `validation` evidence.
An existing path, including a symlink, returns `EXPORT_PATH_EXISTS` and is untouched. A failure after path
creation returns `EXPORT_WRITE_FAILED` with `path`, `expectedSha256`, and `expectedBytes`:
the file at path may be incomplete; coalesce leaves it in place; delete it yourself or export to a new path.
There is no automatic retry or replay. Geometry and STEP
product-label preservation are reported separately. Domain failures have `ok: false` and a
stable code.

## Claude Code

```json
{"mcpServers":{"coalesce":{"type":"stdio","command":"coalesce","args":["--document","part.coalesce.json"]}}}
```

## Codex

```toml
[mcp_servers.coalesce]
command = "coalesce"
args = ["--document", "part.coalesce.json"]
default_tools_approval_mode = "approve"
```
"""

PLATE_EXAMPLE = """# 60 x 40 x 6 mm plate

1. `sketch`: create `plate_profile` at revision 0 with rectangle `outline`, width 60 mm,
   height 40 mm, center [0, 0], on the XY origin plane.
2. `feature`: create extrude `plate_extrude` at revision 1 with profile `outline`, distance
   6 mm, operation `new_body`, and direction `normal`.
3. `query`: select `measurements`; expect bbox min [-30, -20, 0], max [30, 20, 6],
   center [0, 0, 3] mm, and volume 14400 mm^3.
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


def _prefer_unknown_field(error: ValidationError) -> ValidationError:
    if error.validator != "required" or not isinstance(error.schema, dict):
        return error
    properties = error.schema.get("properties", {})
    unknown = sorted(set(error.instance) - set(properties)) if isinstance(error.instance, dict) else []
    if unknown:
        return ValidationError(
            "unexpected fields",
            validator="additionalProperties",
            validator_value=False,
            instance=error.instance,
            schema=error.schema,
            path=error.absolute_path,
            schema_path=error.absolute_schema_path,
        )
    return error


def _schema_for_action(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "feature":
        key = (
            ("delete", None)
            if arguments.get("action") == "delete"
            else (arguments.get("action"), arguments.get("kind"))
        )
        return FEATURE_ACTION_SCHEMAS.get(key, TOOL_SCHEMAS[name])
    if name == "query" and arguments.get("select", {}).get("kind") == "feature":
        return QUERY_FEATURE_SCHEMA
    return ACTION_SCHEMAS.get(name, {}).get(arguments.get("action"), TOOL_SCHEMAS[name])


def create_server(service: DocumentService) -> Server:
    server = Server(
        "coalesce",
        version="0.1.0",
        instructions=(
            "Read coalesce://docs/tools and query the current revision before mutations. "
            "Ask before guessing vague dimensions. Sketch centers use plane-local axes; "
            "extrusions start at the sketch plane and follow its normal. Holes are global +Z."
        ),
    )

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        if os.environ.get("COALESCE_DEBUG_MODULES"):
            print(f"build123d={'build123d' in sys.modules}", file=sys.stderr, flush=True)
        service.start_kernel_warmup()
        return [
            types.Tool(
                name=name,
                description=_TOOL_DESCRIPTIONS[name],
                inputSchema=schema,
                annotations=types.ToolAnnotations(
                    readOnlyHint=name in {"query", "check"},
                    destructiveHint=name in {"import", "sketch", "feature"},
                    idempotentHint=True,
                    openWorldHint=name in {"import", "export"},
                ),
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
            if name == "feature" and arguments.get("action") == "update":
                error = _prefer_unknown_field(error)
            details: dict[str, Any] = {}
            if name == "feature" and error.validator == "enum" and list(error.path) == ["kind"]:
                details["allowedKinds"] = TOOL_SCHEMAS["feature"]["properties"]["kind"]["enum"]
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
            handler = service.import_board if name == "import" else getattr(service, name)
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
