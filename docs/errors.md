# public errors

coalesce error codes are stable `UPPER_SNAKE` values. an agent can branch on `error.code`; it does not need to parse `error.message`.

## error shape

every failed tool call returns the last unchanged document `revision` and this shape:

```json
{
  "ok": false,
  "revision": 4,
  "requestId": "edit-12",
  "units": "mm",
  "error": {
    "code": "REVISION_CONFLICT",
    "message": "expected revision 3; current revision is 4",
    "operation": "feature",
    "featureId": "bosses",
    "retryable": true,
    "affectedParts": ["base/outline"],
    "expected": 3,
    "actual": 4
  }
}
```

`featureId` and `affectedParts` are present only when coalesce can identify them. `retryable` is `true` only for a revision conflict. retrying any other failure without changing the request will not help.

`limits`, `alternatives`, and `legalTargets` are present only when the normal edit and regeneration path proved them. a limit is not a general safe range unless it includes `minimum` and `maximum`. `suggested` is one verified value. each item in `alternatives` is a verified edit. each item in `legalTargets` names a target proved by the current lineage.

raw kernel diagnostics never appear in `message`. when available, they appear as `kernelMessage` in the structured error details. agents must not parse or show that field as recovery advice.

## families

| family | meaning | first recovery step |
|---|---|---|
| request | the request, id, unit, document, or revision is invalid | correct the named field or read the current revision |
| dependency | the feature order, dependency, or body lineage is invalid | use `legalTargets` when present; otherwise query the graph |
| reference | a typed reference or occt history contract failed | query the reference and select an unambiguous provenance set |
| geometry | the requested exact geometry cannot be made or is invalid | change the named geometric input; use only verified remedies |
| import | the board source or imported mechanics is invalid | correct the source according to the code and import again |
| enclosure | a semantic enclosure contract failed | apply a verified limit or alternative when present |
| export | step selection, validation, or publication failed | correct the selection/path or inspect the unchanged model |

## codes

| code | family | when it happens | agent recovery |
|---|---|---|---|
| `INVALID_PARAMETER` | request | a field, action, or combination is not accepted | correct the named field and resend with a new request id |
| `UNIT_MISMATCH` | request | a quantity uses a unit not accepted by that field | convert and send an accepted unit |
| `DUPLICATE_ID` | request | a node id already exists | choose a new stable id |
| `DUPLICATE_ENTITY_ID` | request | one sketch repeats an entity id | give each sketch entity a unique id |
| `UNKNOWN_ID` | request | a requested node, part, or revision is not retained | query the graph or listed valid parts and use an existing id |
| `UNKNOWN_PLANE` | request | a sketch names an unsupported origin plane | use `XY`, `XZ`, or `YZ` |
| `NON_FINITE_VALUE` | request | a numeric value is NaN or infinite | send a finite number |
| `INVALID_CIRCLE_RADIUS` | request | a sketch circle radius is not positive | send a positive radius |
| `REQUEST_ID_REUSED` | request | a request id was used for a different body or its receipt expired | send the intended edit with a new request id |
| `REVISION_CONFLICT` | request | a mutation was based on an old revision | query revision, review the model, and retry against that revision |
| `STALE_REVISION` | request | export was requested from an old revision | query revision, review the model, and export that revision |
| `INVALID_DOCUMENT` | request | a saved document cannot be loaded under the current format | correct or migrate the named field before starting coalesce |
| `CYCLE` | dependency | the feature graph contains a dependency cycle | reorder or remove the cyclic dependency |
| `HAS_DEPENDENTS` | dependency | non-cascade delete would orphan dependent nodes | inspect the listed dependents, then delete them or use cascade deliberately |
| `TARGET_ALREADY_CONSUMED` | dependency | an edit targets a non-terminal body feature | use the proved `legalTargets` entry |
| `BODY_LINEAGE_DIVERGED` | dependency | one edit would combine divergent body lineages | target one terminal body lineage |
| `MIXED_BODY_ANCESTRY` | dependency | a result face does not have one source body | change the operation so each result has one body ancestry |
| `JOIN_TARGET_REQUIRED` | dependency | a join extrude has no target | provide one terminal target feature |
| `JOIN_TARGET_BODY` | dependency | join output cannot be assigned to the target body | change the profile or target so the join has one target body |
| `REFERENCE_FAILED` | reference | an edge reference resolves with a non-OK status | inspect `resolution.status` and query a corrected typed reference |
| `EMPTY_EDGE_SELECTION` | reference | a fillet or chamfer has no resolved edges | provide at least one edge reference that resolves |
| `INVALID_EDGE_REFERENCE` | reference | a treatment reference resolves to a non-edge | select an edge provenance role |
| `HISTORY_MISSING` | reference | occt did not provide required provenance history | change the operation; do not guess a replacement face |
| `BOOLEAN_FAILED` | geometry | an exact cut, fuse, or enclosure boolean did not complete | change the inputs or use a verified remedy when present |
| `INVALID_BREP` | geometry | regeneration produced an invalid b-rep | change the last geometric edit |
| `KERNEL_OPERATION_FAILED` | geometry | an unexpected or unmapped modeling failure occurred | inspect the structured context and change the edit; report reproducible cases |
| `TOUCHING_PROFILES` | geometry | two sketch profile boundaries only touch | separate them or make the intended overlap/nesting explicit |
| `OVERLAPPING_PROFILES` | geometry | profiles overlap without valid nesting | remove the overlap or make one profile fully nested |
| `INVALID_EXTRUDE_OPERATION` | geometry | an extrude operation is not supported | use `new_body` or a valid target join |
| `INVALID_CUT_DEPTH` | geometry | a finite cut depth is invalid | send a positive depth or use through-all |
| `INVALID_CUT_TOOL` | geometry | a cut tool sketch cannot make the requested cutter | correct the tool sketch and direction |
| `CUT_NO_EFFECT` | geometry | a cut removes no material | check target, plane, offset, depth, and flip |
| `HOLES_INTERSECT` | geometry | holes in one feature overlap | move holes apart or reduce diameter |
| `HOLE_NO_EFFECT` | geometry | a hole removes no material | check target, position, diameter, and depth |
| `INVALID_HOLE` | geometry | a hole cannot produce valid exact geometry | correct the named hole input |
| `JOIN_NO_CONTACT` | geometry | a join profile does not contact the target | move or extend the listed profile to contact the target |
| `JOIN_NON_MANIFOLD` | geometry | a join touches only along an edge or point | extend the profile until faces overlap or touch |
| `INVALID_FILLET_RADIUS` | geometry | a fillet radius is not positive | send a positive radius |
| `INVALID_CHAMFER_DISTANCE` | geometry | a chamfer distance is not positive | send a positive distance |
| `FILLET_FAILED` | geometry | occt cannot apply the selected fillet | change the edges or reduce radius; do not infer a safe range |
| `CHAMFER_FAILED` | geometry | occt cannot apply the selected chamfer | change the edges or reduce distance; do not infer a safe range |
| `IMPORT_PATH_NOT_ABSOLUTE` | import | a board path is relative | send an explicit absolute local path |
| `IMPORT_PATH_NOT_FILE` | import | a board path does not name a regular file | choose a readable regular `.kicad_pcb` file |
| `IMPORT_SOURCE_UNREADABLE` | import | the board source cannot be opened or read | correct permissions/path and import again |
| `IMPORT_TOO_LARGE` | import | the board source exceeds 16 MiB | choose or produce a source within the documented limit |
| `IMPORT_INVALID_MECHANICS` | import | the parser reports invalid mechanical data | correct the reported board mechanics and import again |
| `INVALID_BOARD` | import | imported board geometry is not valid for regeneration | correct the reported outline, thickness, or drill data |
| `UNSUPPORTED_BOARD_OUTLINE` | import | an outline primitive or topology is unsupported | simplify the outline to supported closed geometry |
| `UNSUPPORTED_BOARD_DRILL` | import | a drill shape is unsupported | use a supported circular or slotted drill |
| `MISSING` | import | required board or footprint mechanics are absent | provide the named mechanics or select a different subject |
| `AMBIGUOUS` | import | a board/footprint subject has more than one valid match | use a stable id that resolves once |
| `PORT_BLOCKED_BY_LID` | enclosure | a port opening conflicts with the lid or lip | apply a verified `alternative` or change the lid/opening |
| `INSPECTION_FAILED` | enclosure | enclosure inspection cannot evaluate the requested requirements; the raw diagnostic is in `kernelMessage` | check each requirement kind against the `check` tool schema; if they are valid, treat the requirements as unevaluated and report the failure |
| `EXPORT_PATH_EXISTS` | export | the target path already exists | choose a new path; coalesce never overwrites |
| `EXPORT_VALIDATION_FAILED` | export | fresh-process step reimport does not match the source | inspect the model and retry to a different new path |
| `EXPORT_WRITE_FAILED` | export | step bytes cannot be safely published | check the directory and use a new path; inspect any incomplete file |

## inspection warnings

a successful enclosure inspection can include `warnings`. a warning does not fail the request. agents can branch on its `code`.

| warning | meaning | agent action |
|---|---|---|
| `OTHER_MANUFACTURING_BODY` | a manufactured body that is not part of the enclosure exists; it is named by `featureId` and `partId`. | delete it, or confirm it is intended before export. |
| `CONNECTOR_HEIGHT_UNRESOLVED` | the board declares no 3D models, so connector heights for `footprintIds` are not measured. | treat port height clearance as unknown; ask for connector heights or add margin. |
| `UNCLASSIFIED_BOARD_HOLES` | board holes in `holeIds` are not mounting holes, so no standoff is expected for them. | check that these holes need no standoff. |
