# assembly occurrences and ground mates

## document format

format version 3 stores occurrences and mates. version 1 still loads without assembly state, and version 2 still loads its occurrence-only assembly. newer versions fail with `INVALID_DOCUMENT` and name the unsupported version. files written by this version cannot be opened by earlier coalesce builds.

coalesce stores an assembly as caller-named occurrences and mates outside the manufacturing feature graph. use the existing `feature` tool with `kind: "assembly_occurrence"` to create, update, or delete a placement. every mutation requires `expectedRevision` and `requestId`.

an occurrence references a canonical terminal `partId`. use `query` with `{"select":{"kind":"assembly"}}` to list `availableParts`; do not infer a part id from array order. more than one occurrence can reference the same part.

placements use a translation in mm and an axis-angle rotation in degrees or radians. coalesce normalizes the axis, reduces angles to `[-180, 180)`, and stores zero rotation as axis `[0,0,1]` and angle `0`. translation, axis, and angle values must be finite. a non-zero rotation requires a non-zero axis.

```json
{
  "action": "create",
  "kind": "assembly_occurrence",
  "expectedRevision": 2,
  "requestId": "place-plate-left",
  "occurrenceId": "plate-left",
  "parameters": {
    "partId": "plate_extrude/outline",
    "transform": {
      "translation": {"value": [30, 0, 0], "unit": "mm"},
      "rotation": {"axis": [0, 0, 1], "angle": {"value": 0, "unit": "deg"}}
    }
  }
}
```

graph edits fail atomically with `ASSEMBLY_REFERENCE_STALE` when they would remove or split a referenced part. delete affected occurrences in an earlier revision before deleting their parts. an assembly can contain at most 64 occurrences.

## ground mates and solve status

use `feature` with `kind: "assembly_mate"` to create, update, or delete a ground mate. a ground mate fixes one occurrence at its current explicit transform and leaves its transform compatible with the viewer, checks, interference, and export paths.

```json
{
  "action": "create",
  "kind": "assembly_mate",
  "expectedRevision": 3,
  "requestId": "ground-base",
  "mateId": "base-ground",
  "parameters": {"kind": "ground", "occurrenceId": "plate-left"}
}
```

update a ground mate with `"set":{"occurrenceId":"other-occurrence"}`. a mate id is caller-supplied and unique. one occurrence can have at most one ground mate; a second ground fails atomically with `MATE_CONFLICT`. an occurrence with a mate cannot be deleted until its mate is deleted.

an assembly query returns sorted `mates`, `solveStatus`, `underConstrainedOccurrenceIds`, and one status row per connected component. each occurrence reports `grounded`, `mateIds`, `remainingDof`, and `freeMotions`. a ground occurrence has zero remaining degrees of freedom. an unmated occurrence has six degrees of freedom and remains at its explicit stored transform. under-constraint is a successful state, not a failed solve.

## exact interference and clearance

use `check` with `kind: "assembly_interference"` to measure every selected occurrence pair. `occurrenceIds` is optional when the assembly has at most 16 occurrences. an explicit selection must contain 2 to 16 unique ids. this 16-occurrence cap bounds one requirement to 120 pairs; larger assemblies must use an explicit subset.

```json
{
  "action": "evaluate",
  "requirements": [
    {
      "id": "shaft-clearance",
      "kind": "assembly_interference",
      "occurrenceIds": ["shaft", "disc"],
      "maximumOverlapVolume": {"value": 0, "unit": "mm^3"},
      "minimumClearance": {"value": 0.2, "unit": "mm"}
    }
  ]
}
```

`maximumOverlapVolume` is optional and defaults to zero. common-solid volumes at or below the existing `1e-9 mm^3` kernel tolerance are reported as zero. `minimumClearance` is optional. both thresholds must be finite and non-negative and must use the units shown above. bounds only remove definite non-overlap candidates from the boolean stage. exact minimum distance is computed only when `minimumClearance` is present. touching faces have zero overlap and `interferes: false`; they also have zero clearance when clearance is requested. failed or invalid kernel measurements produce `ERROR`, never `PASS`.

one request can inspect at most 120 pairs across all assembly requirements. assembly inspection has a 10-second cooperative deadline. the deadline is checked between exact pair operations because an in-process OCCT call cannot be interrupted safely. one pair can therefore overrun the deadline by at most that pair's duration. pairs not started before the deadline report `INSPECTION_FAILED` with reason `DEADLINE`, and the requirement result is `ERROR`; a partial inspection never passes. geometry is resolved under the document lock, then exact pair work runs on that immutable snapshot without holding the lock. if the document changes during inspection, the request returns `REVISION_CONFLICT`.

on an apple m5 pro, 120 repeated box pairs took 0.108 seconds for touching common-volume checks, 0.311 seconds for overlapping common-volume checks, 0.211 seconds for touching clearance checks, and 0.114 seconds for disjoint clearance checks. 120 touching pairs running both common volume and clearance took 0.304 seconds.

## assembly step export

use `export` with `mode: "assembly"` to preserve the product tree, named occurrences, shared definitions, and rigid placements. assembly mode exports all manufacturing occurrences and rejects `partIds`; reference occurrences are returned in `excludedOccurrenceIds`. `action: "preflight"` accepts assembly mode without a path and verifies the same structure without publishing.

fresh-process validation uses xcaf labels and references, not only geometry reimport. publication stages a private sibling file and commits it with one exclusive rename, so validation and write failures publish nothing. compound mode and separate manufacturing manifests keep their existing behavior.
