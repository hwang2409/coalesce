# explicit assembly occurrences

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

graph edits fail atomically with `ASSEMBLY_REFERENCE_STALE` when they would remove, split, or make ambiguous a referenced part. delete affected occurrences in an earlier revision before deleting their parts. an assembly can contain at most 64 occurrences.

this slice persists and queries explicit placements. viewer placement, interference checks, mates, and assembly step export are not available yet.
