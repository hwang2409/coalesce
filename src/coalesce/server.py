"""Stdio MCP adapter for the coalesce document module."""

from __future__ import annotations

import json
import logging
import os
import sys
from functools import partial
from typing import Any

import anyio
from jsonschema import ValidationError, validate
from jsonschema.exceptions import best_match, relevance
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .resources import listed_resources, package_version
from .resources import read_resource as read_packaged_resource
from .schemas import (
    ACTION_SCHEMAS,
    FEATURE_ACTION_SCHEMAS,
    QUERY_FEATURE_SCHEMA,
    SUPPORTED_REQUIREMENT_KINDS,
    TOOL_SCHEMAS,
)
from .service import DocumentService

_LOG = logging.getLogger(__name__)

MAX_SKETCH_SET_JSON_BYTES = 256 * 1024

_TOOL_DESCRIPTIONS = {
    "import": "Snapshot one explicit absolute local kicad_pcb path as a persisted BoardImport and exact reference substrate. Reads regular files up to 16 MiB, with no network or environment-variable model resolution. Use refresh with an optional replacement absolute path to re-read the board explicitly. Use delete to remove the board asset; it fails when dependents exist unless cascade is true.",
    "sketch": "set.entities/set.constrained replace full definition; set.dimensionValues changes only named driving dimensions",
    "feature": "Create, update, or delete features, assembly occurrences, and mates. Discover faces with query face_roles. Planar mates use aligned or opposed normals and a signed offset; concentric mates use cylinder axes from named hole walls or revolve side segments. Mixed mate kinds must constrain the same moving pair; independent leaves around a grounded anchor are supported, but mixed kinds with different neighbors are not. See coalesce://docs/assembly. Ground fixes one named occurrence at its stored transform. Supports constant-radius fillet and symmetric one-distance chamfer; two-distance not supported. Holes use global +Z; plane normals are XY +Z, XZ -Y, YZ +X. Finite cuts use negative sketch normal for flip=false and positive sketch normal for flip=true; through-all is bidirectional. Routes: extrude/cut/fillet/chamfer -> coalesce://docs/tools; revolve -> coalesce://docs/revolve; hole/pattern -> coalesce://docs/hole; enclosure features -> coalesce://docs/enclosure; board refresh -> coalesce://docs/refresh.",
    "query": "Read one feature, the graph, revision, assembly, exact mm metrics, or typed references. Use select.kind edge_roles before choosing treatment edges and face_roles before authoring planar or concentric mates. See coalesce://docs/assembly for mate details. Call select.kind enclosure before reporting enclosure dimensions.",
    "check": "Run revision-bound exact requirements against regenerated OCCT geometry. Use enclosure requirement kinds for wall/floor thickness, board-edge clearance, mounting coverage, lid interference, fit, exact lid_outline_match, screw_closure alignment and residual wall, connector clearance, and clean manufacturing bodies. assembly_interference measures exact common-solid volume for every pair among 2 to 16 selected occurrences and minimum clearance when requested; omit occurrenceIds only when the assembly has at most 16 occurrences. One request is capped at 120 total assembly pairs and a 10-second cooperative deadline. board_clearance accepts side left, right, front, back, or all and a minimum mm threshold. Every result is PASS, FAIL, UNKNOWN, or ERROR with measured evidence, threshold, stable ids, and revision; report UNKNOWN items instead of claiming readiness. Port evidence reports the board footprint libraryId, reference, and value; do not relabel a footprint as USB unless that data says so.",
    "export": "Preflight or publish at the current expectedRevision. action=preflight is read-only and validates in a fresh Python process. Publishing defaults to the unchanged compound STEP mode. mode=separate atomically publishes one labeled STEP per manufacturing definition plus a deterministic manifest. mode=assembly writes a verified XCAF product tree for all manufacturing occurrences, preserves occurrence names and placements, and lists excluded reference occurrences; it does not accept partIds. Exports never overwrite.",
}

TOOLS_GUIDE = """# coalesce tools

Start with `coalesce --document part.coalesce.json` or set `COALESCE_DOCUMENT`.
Set `--sketch-solve-timeout SECONDS` or `COALESCE_SKETCH_SOLVE_TIMEOUT` to configure the
bounded constrained-sketch solver (default 1 second; accepted range 0.5 through 120 seconds).
coalesce owns one revisioned document. Start with `query` using `select.kind: revision`.
Failures use the stable UPPER_SNAKE taxonomy in `docs/errors.md`. Every failure includes
`code`, `message`, `operation`, `retryable`, and the unchanged `revision`; it includes
`featureId` and `affectedParts` when known. Retry only revision conflicts. Use
`limits`, `alternatives`, or `legalTargets` only when present: the real edit path proved
those remedies. Never parse `message` or `kernelMessage` to choose a recovery.

## resources

List resources to discover the complete catalog. Read `coalesce://docs/index` for stable
URIs, `coalesce://session/capabilities` for runtime support, and
`coalesce://docs/tools` for this guide. Resource reads do not change the document.

Use `import` create with an explicit absolute local `kicad_pcb` path to persist a
`BoardImport` snapshot. The source must be a readable regular file of 16 MiB or less.
The import stores the resolved path, SHA-256 digest, parser version, stable mechanics ids,
availability, warnings, and errors. It regenerates the exact board substrate as reference
geometry from line, arc, circle, cutout, and drill data. It does not access the network or
resolve environment variables in model paths. Later source edits do not affect the document; use
`import` refresh to read the persisted path, or supply an optional replacement absolute path, and
commit a new snapshot explicitly. Refresh `format` is optional and inherits `kicad_pcb`; if supplied, it
must match. Example: `{"action":"refresh","assetId":"board","expectedRevision":2,"requestId":"refresh-1"}`
returns a deterministic `boardChanges` delta. `mountingHoles` and `footprints` contain `{id, reference, oldPosition, newPosition, distance, change}`; positions are `[x,y]` mm, distance is mm, reference is null when the footprint has none, absent endpoints are null, and change is added, removed, moved, or changed. `outlineChanged` is boolean. `dependentFeatures` lists regenerated feature-kind names. Optional `thickness {before, after}` values are mm. Optional `outlineBounds {before: {width, height}, after: {width, height}}` values are mm and cover all outer loops. An identical source gives the summary `refreshed board <id>, source unchanged`.
`all_mounting` standoff selections follow the refreshed board and
report added or removed holes in `selectionChanges`; explicit hole ids stay fixed and the refresh
fails without writing when any selected id is missing.
Use `sketch` for rectangles or circles on XY, XZ, or YZ. Local axes are XY=(+X,+Y), XZ=(+X,+Z), YZ=(+Y,+Z). Plane offset moves along its normal.
Plane normals are XY +Z, XZ -Y, YZ +X.
Extrusion starts at the sketch plane and extends along its normal; it is not centered.
Use operation `join` with an explicit target feature to add material to one contacting body.
A join, cut, hole, fillet, or chamfer must target the current terminal feature of its body lineage.
Use `feature` update with `set.target.featureId` to retarget an existing cut or hole.
For finite cuts, `flip=false` cuts along the negative sketch normal and `flip=true` cuts
along the positive sketch normal. Through-all cuts are bidirectional, and flip has no effect for through-all cuts.
Holes use absolute XY positions and global +Z only. Omitted hole depth means through-all.
A hole accepts exactly one `position`, 1..64 `positions`, or one `pattern`. Explicit
positions can have stable caller-chosen names, defaulting to `h1`..`hn`. A circular pattern
stores `center`, positive `radius`, `count`, `startAngle`, and `namePrefix`. A linear pattern
stores `start`, normalized non-zero `direction`, positive `pitch`, `count`, and `namePrefix`.
Pattern members keep ordinal `<namePrefix>N` identity. Update pattern fields with `set.pattern`
and the stored pattern `kind`; generated coordinates are not inputs. Circular count, radius,
center, or start-angle edits and linear pitch, start, or direction edits move surviving named
members and their downstream treatments. A circular count edit re-spaces survivors. A smaller
count makes trailing references resolve EMPTY; a name-prefix edit makes every old reference
stale. There is no index or nearest-shape fallback. Use explicit named positions for fixed
places and a member name in a `hole_surface` reference. Sketch and feature responses report the global frame; feature
responses also report the direction vector and exact tool/cutter bbox.
Create `fillet` or `chamfer` with a terminal body target and one or more typed edge
references. Each reference has an explicit cardinality; split, merged, ambiguous, empty,
or out-of-order references fail the whole edit. Fillet accepts one positive `radius`
quantity. Chamfer accepts one positive symmetric `distance` quantity; two-distance
chamfers are not supported. Both operations are atomic and report source references plus
generated face roles. Kernel failures report every selected edge key and a safe range only
when the real edit path can prove it; otherwise `safeRange` is `unknown`.
Use `query` with `select.kind: edge_roles` and `id` to list each selectable role, exact
edge count, orientation, length, and an example exact-cardinality reference. Empty or
failed references include the same `availableRoles` catalog. A reference query always
includes `resolution`, including when `cardinality` is `any`. For a stale sketch-segment
reference, `resolution.sketchSegment` has string `sketch`, `entity`, and `segment` ids,
a string array `storedTopology`, a string-array-or-null `currentTopology`, and boolean
`segmentExists`. Re-select the face on the updated profile; if the segment still exists,
keeping its original endpoint ids can also recover the reference.

For a PCB enclosure, create `pcb_base` with a BoardImport id, wall and floor thickness,
board-edge clearance, wall height above the board top, and board seat height. The inner
cavity is the board outline offset outward by the clearance; arcs remain arcs. Base walls
expose `outer_vertical` and `inner_vertical` for sharp corners only, excluding tangent
line/arc seams. Lid slabs expose `lid_vertical` with the same sharp-corner rule. The board
stays at its imported Z position. The floor top is `-boardSeatHeight`, the floor bottom is
lower by `floorThickness`, and the wall top is the board top plus
`wallHeightAboveBoard`. Then create `standoffs` with that base as target and the same board.
Use `selection.mode: explicit` with stable `holeIds`, or `all_mounting` to select every
hole in a `MountingHole:*` footprint, plated or non-plated. The import response marks every
hole with `mounting: true|false` and its footprint `reference`. Boss height is derived from
the base seat height, so the board bottom
sits on the boss tops. `pilotDiameter` must be smaller than `bossDiameter`, and every boss
must fit fully on the cavity floor without crossing a wall. Board refresh recomputes the base and standoff positions. `all_mounting` follows the current board, so new mounting holes gain standoffs and removed holes lose them. Explicit `holeIds` stay fixed and fail closed if an id disappears. A missing explicit hole id fails the refresh atomically.

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

Create `screw_closure` after its base and lid with 2..8 unique named XY points,
positive basePilotDiameter, bossDiameter, and lidClearanceDiameter quantities. The pilot must
be smaller than the boss and the lid clearance must exceed the pilot. It joins full-height
bosses to the base, cuts pilot holes in those bosses, and cuts coaxial through-holes in the
separate lid. Points are explicit: no auto-placement. Off-floor, wall-crossing, board-intersecting, overlapping,
and standoff-conflicting points fail atomically and name the point. Updates replace the complete
named point set or change any diameter; removed or renamed points are deleted, never rebound.
Check requirement kind `screw_closure` requires an explicit positive `minimumWall` quantity and
compares it with the exact measured residual boss wall while also proving count, coaxial axes,
and zero base/lid interference.

Short enclosure sequence: `import` the board, create `pcb_base`, create `standoffs`, create
`pcb_lid`, optionally create `port_cutout`, create `screw_closure`, run `check` with
`valid_brep` and `screw_closure`, then `export` the two terminal manufacturing parts.
Export excludes the board reference geometry.

Use `query` with `select.kind: assembly` to list canonical terminal `availableParts`, solve
status, and per-occurrence degrees of freedom. Create named placements with
`kind: assembly_occurrence`. Use `select.kind: face_roles` to discover exact face references,
then create a planar mate with aligned or opposed normals and a signed offset, or a concentric mate from named hole-wall or revolve-segment cylinder faces. Use a ground mate
to fix one occurrence. See `coalesce://docs/assembly` for request shapes and update behavior.

Ask the user before changing geometry when a dimension is vague, such as "a bit wider".
Mutations need the current `expectedRevision` and a unique `requestId`; exact replay is
idempotent. Use update when the same sketch or feature should remain in the timeline with new
parameters. `kind` is optional on feature update; when supplied, it must match the existing
feature. Use delete to remove a sketch, feature, or imported board. Delete without cascade
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
from manufacturing totals. Use `select.kind: feature` with `id` for one node. Use `check` with `valid_brep` before relying on the model, or declare enclosure requirements for measured acceptance. It checks terminal manufacturing parts only and
excludes BoardImport reference geometry. Zero manufacturing solids return `UNKNOWN` with
`solidCount: 0` and block readiness. Use `export` action `preflight` with the current
`expectedRevision` to validate each selected part without publishing. Supply `base` and explicit
`requirements` to include enclosure acceptance statuses. The response lists every `UNKNOWN`
result and sets `ready: false` unless every requirement passes. Preflight writes no user-visible
files. Set `mode: assembly` during preflight to validate the XCAF product tree for all manufacturing
occurrences; assembly mode rejects `partIds` and lists reference occurrences as excluded. Publishing
is stateless and does not accept `requestId`; it requires an explicit absolute new `path` and defaults
to all terminal manufacturing parts. Select stable `partIds` from measurements when needed. A feature id is
accepted only when it resolves to exactly one terminal manufacturing part; otherwise
`UNKNOWN_ID` lists valid part ids and the feature that built each. BoardImport
reference geometry is never included. The default compound STEP mode never overwrites, renames,
truncates, or removes a destination. It validates bytes in a private directory. A fresh Python process
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
product-label preservation are reported separately. With `mode: separate`, export validates each
part independently and stages every label-derived `.step` file plus deterministic `manifest.json`
in a private sibling directory. Separate STEP export requires a POSIX system (macOS or Linux).
One rename publishes the complete set at a new path or replaces an existing empty real directory;
if an entry appears first, publication fails with `EXPORT_PATH_EXISTS` and a bounded, sorted entry
list. A file, symlink, or non-empty directory is never replaced or followed. Replacing an empty
directory gives the export a new inode, mode, and owner; a process whose working directory was
inside the old directory keeps that old, now-unlinked directory. Validation or a write failure
before the rename publishes nothing. A parent-directory sync failure after the rename returns
success with `EXPORT_DURABILITY_UNCONFIRMED`: the files are published, but their durability on disk
is not confirmed. The manifest records revision,
mm units, selected part ids, labels, files,
SHA-256 hashes, imported-board source digests, requirement results, and unknowns. Separate export
also sets `ready: false` for `UNKNOWN` or failed requirements. Domain failures have `ok: false` and a
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
4. `check`: evaluate `valid_brep` or revision-bound enclosure requirements. An enclosure gate is PASS only when every declared requirement passes; UNKNOWN blocks readiness and must be reported to the user.
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


def _quantity_shape_hint(error: ValidationError) -> str:
    schema = error.schema
    if (
        isinstance(schema, dict)
        and schema.get("type") == "object"
        and set(schema.get("properties", {})) >= {"value", "unit"}
    ):
        return '; expected shape: {"value": 2, "unit": "mm"}'
    return ""


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
        if unknown == ["minimumDiameter"]:
            text = "minimumDiameter was removed; all_mounting now selects every MountingHole:* footprint hole"
        else:
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
    text += _quantity_shape_hint(error)
    return f"{text} (field: {path})" if path else text


def _kind_discriminator(branch: Any) -> tuple[Any, ...]:
    if not isinstance(branch, dict):
        return ()
    properties = branch.get("properties")
    if not isinstance(properties, dict):
        return ()
    kind = properties.get("kind")
    if not isinstance(kind, dict):
        return ()
    if "const" in kind:
        return (kind["const"],)
    values = kind.get("enum")
    return tuple(values) if isinstance(values, list) else ()


def _is_kind_union(error: ValidationError) -> bool:
    return (
        error.validator in ("oneOf", "anyOf")
        and isinstance(error.instance, dict)
        and "kind" in error.instance
        and any(_kind_discriminator(branch) for branch in error.validator_value)
    )


def _matching_kind_union(error: ValidationError) -> ValidationError | None:
    if _is_kind_union(error):
        return error
    return next(
        (match for child in error.context if (match := _matching_kind_union(child)) is not None),
        None,
    )


def _kind_discriminators(union: ValidationError) -> list[tuple[int, tuple[Any, ...]]]:
    pairs = [
        (index, _kind_discriminator(branch)) for index, branch in enumerate(union.validator_value)
    ]
    return [(index, values) for index, values in pairs if values]


def _invalid_kind_error(union: ValidationError, allowed: list[Any]) -> ValidationError:
    many = len(allowed) > 1
    return ValidationError(
        "kind",
        validator="enum" if many else "const",
        validator_value=allowed if many else allowed[0],
        instance=union.instance["kind"],
        path=[*union.absolute_path, "kind"],
    )


def _selected_branch_error(union: ValidationError, index: int) -> ValidationError:
    branch = [error for error in union.context if list(error.relative_schema_path)[:1] == [index]]
    return max(branch, key=relevance) if branch else union


def _kind_union_branch(union: ValidationError) -> ValidationError:
    discriminators = _kind_discriminators(union)
    if not discriminators:
        return union
    supplied = union.instance["kind"]
    for index, values in discriminators:
        if supplied in values:
            return _selected_branch_error(union, index)
    return _invalid_kind_error(union, [value for _, values in discriminators for value in values])


def _branch_error(error: ValidationError) -> ValidationError:
    """Report a union failure from the branch the supplied `kind` selects, not a wrong branch."""
    root = error
    while root.parent is not None:
        root = root.parent
    union = _matching_kind_union(root)
    if union is None:
        return error
    while _is_kind_union(union):
        selected = _kind_union_branch(union)
        if selected is union:
            return error
        union = selected
    return best_match([union])


def _prefer_unknown_field(error: ValidationError) -> ValidationError:
    if error.validator != "required" or not isinstance(error.schema, dict):
        return error
    properties = error.schema.get("properties", {})
    unknown = (
        sorted(set(error.instance) - set(properties)) if isinstance(error.instance, dict) else []
    )
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


def _schema_error_field(error: ValidationError) -> str:
    field = "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path
    ).lstrip(".")
    if error.validator == "required" and isinstance(error.instance, dict):
        missing = next(name for name in error.validator_value if name not in error.instance)
        return f"{field}.{missing}" if field else missing
    return field


def _schema_error_details(
    name: str, arguments: dict[str, Any], error: ValidationError
) -> dict[str, Any]:
    details: dict[str, Any] = {"field": _schema_error_field(error)}
    if name == "feature" and error.validator == "enum" and list(error.path) == ["kind"]:
        details["allowedKinds"] = TOOL_SCHEMAS["feature"]["properties"]["kind"]["enum"]
    if name == "check" and error.validator == "enum":
        requirement = next(
            (
                item
                for item in arguments.get("requirements", [])
                if item.get("kind") not in SUPPORTED_REQUIREMENT_KINDS
            ),
            None,
        )
        if requirement is not None:
            details.update(
                kind=requirement.get("kind"),
                supportedKinds=list(SUPPORTED_REQUIREMENT_KINDS),
            )
    if error.validator == "additionalProperties":
        allowed = set(error.schema.get("properties", {}))
        if name == "sketch":
            allowed.discard("constrained")
        details["allowedFields"] = sorted(allowed)
    return details


def _schema_for_action(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "feature":
        key = (
            ("delete", None)
            if arguments.get("action") == "delete"
            and arguments.get("kind") not in {"assembly_occurrence", "assembly_mate"}
            else (arguments.get("action"), arguments.get("kind"))
        )
        return FEATURE_ACTION_SCHEMAS.get(key, TOOL_SCHEMAS[name])
    if name == "query" and arguments.get("select", {}).get("kind") == "feature":
        return QUERY_FEATURE_SCHEMA
    return ACTION_SCHEMAS.get(name, {}).get(arguments.get("action"), TOOL_SCHEMAS[name])


def _stateful_error(
    service: DocumentService,
    name: str,
    arguments: dict[str, Any],
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    *,
    publish: bool = False,
) -> dict[str, Any]:
    failure = service.error_response(name, arguments, code, message, **(details or {}))
    if publish:
        service.publish_failure(arguments, failure)
    return failure


def _decode_sketch_set(arguments: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if arguments.get("action") != "update" or not isinstance(arguments.get("set"), str):
        return arguments, False
    encoded = arguments["set"]
    if len(encoded) > MAX_SKETCH_SET_JSON_BYTES:
        return arguments, False
    try:
        if len(encoded.encode("utf-8")) > MAX_SKETCH_SET_JSON_BYTES:
            return arguments, False
        decoded = json.loads(encoded)
    except (json.JSONDecodeError, RecursionError, UnicodeEncodeError):
        return arguments, False
    if not isinstance(decoded, dict):
        return arguments, False
    return {**arguments, "set": decoded}, True


async def _schema_failure(
    service: DocumentService, name: str, arguments: dict[str, Any], raised: ValidationError
) -> types.CallToolResult:
    error = _branch_error(raised)
    if name == "feature" and arguments.get("action") == "update":
        error = _prefer_unknown_field(error)
    details = _schema_error_details(name, arguments, error)
    message = schema_error_message(error)
    if (
        name == "sketch"
        and arguments.get("action") == "update"
        and isinstance(arguments.get("set"), str)
    ):
        message = (
            'set must be a JSON object; valid forms are "set": {"entities": [...]}, '
            '"set": {"constrained": {...}}, "set": {"dimensionValues": {...}}, or '
            '"set": {"position": [...]} without surrounding quotes (field: set)'
        )
    failure = await anyio.to_thread.run_sync(
        partial(
            _stateful_error,
            service,
            name,
            arguments,
            "INVALID_PARAMETER",
            message,
            details,
            publish=name in ("sketch", "feature"),
        )
    )
    return _tool_result(failure)


async def _call_tool(
    service: DocumentService, name: str, arguments: dict[str, Any]
) -> types.CallToolResult:
    if name not in TOOL_SCHEMAS:
        failure = await anyio.to_thread.run_sync(
            partial(
                _stateful_error,
                service,
                name,
                arguments,
                "INVALID_PARAMETER",
                f"unknown tool: {name}",
            )
        )
        return _tool_result(failure)
    decoded_string = False
    if name == "sketch":
        arguments, decoded_string = _decode_sketch_set(arguments)
    try:
        validate(instance=arguments, schema=_schema_for_action(name, arguments))
    except ValidationError as raised:
        return await _schema_failure(service, name, arguments, raised)
    try:
        handler = service.import_board if name == "import" else getattr(service, name)
        result = await anyio.to_thread.run_sync(handler, arguments)
        if decoded_string and result.get("ok"):
            result = {
                **result,
                "warnings": [
                    *result.get("warnings", []),
                    {
                        "code": "NESTED_JSON_STRING_DECODED",
                        "message": "set was decoded from a JSON string; send it as an object",
                    },
                ],
            }
        return _tool_result(result)
    except Exception:  # noqa: BLE001 - raw failures stay on stderr at the transport seam.
        _LOG.exception("unexpected failure during %s", name)
        failure = await anyio.to_thread.run_sync(
            partial(
                _stateful_error,
                service,
                name,
                arguments,
                "KERNEL_OPERATION_FAILED",
                "the operation failed unexpectedly",
            )
        )
        return _tool_result(failure)


def _read_resource(service: DocumentService, uri: str):
    if uri == "coalesce://docs/tools":
        return TOOLS_GUIDE
    if uri == "coalesce://examples/plate":
        return PLATE_EXAMPLE
    return read_packaged_resource(
        uri,
        sketch_solve_timeout=service.sketch_solve_timeout,
        viewer_url=service.viewer_url,
    )


def create_server(service: DocumentService) -> Server:
    server = Server(
        "coalesce",
        version=package_version(),
        instructions=(
            "Read coalesce://docs/tools and query the current revision before mutations. "
            "Ask before guessing vague dimensions. Sketch centers use plane-local axes; "
            "extrusions start at the sketch plane and follow its normal. Holes are global +Z. "
            f"The constrained-sketch solve timeout is {service.sketch_solve_timeout:g} seconds."
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
        return await _call_tool(service, name, arguments)

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        return listed_resources()

    @server.read_resource()
    async def read_resource(uri):
        return await anyio.to_thread.run_sync(_read_resource, service, str(uri))

    return server


async def run_stdio(service: DocumentService) -> None:
    server = create_server(service)
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(service.close)
