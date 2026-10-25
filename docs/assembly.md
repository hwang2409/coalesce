# explicit assembly occurrences

## document format

files written by this version cannot be opened by earlier coalesce builds.

coalesce stores an assembly as caller-named occurrences outside the manufacturing feature graph. use the existing `feature` tool with `kind: "assembly_occurrence"` to create, update, or delete a placement. every mutation requires `expectedRevision` and `requestId`.

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

`maximumOverlapVolume` is optional and defaults to zero. common-solid volumes at or below the existing `1e-9 mm^3` kernel tolerance are reported as zero. `minimumClearance` is optional. both thresholds must be finite and non-negative and must use the units shown above. bounds only remove definite non-overlap candidates from the boolean stage. every non-interfering pair still gets an exact OCCT minimum-distance measurement. touching faces have zero overlap, zero clearance, and `interferes: false`. failed or invalid kernel measurements produce `ERROR`, never `PASS`.

on an apple m5 pro, 120 repeated box pairs took 0.108 seconds for touching common-volume checks, 0.311 seconds for overlapping common-volume checks, 0.211 seconds for touching clearance checks, and 0.114 seconds for disjoint clearance checks. 120 touching pairs running both common volume and clearance took 0.304 seconds. the v0 runtime budget is 2 seconds for one 120-pair requirement on enclosure-scale geometry, with 16 occurrences as the fixed cap. this is a performance budget, not a cancellation deadline; exact occt calls complete before the result returns.

viewer placement, mates, and assembly step export are not available yet.
