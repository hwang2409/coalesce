# full revolution

`revolve` creates one new solid by rotating one closed sketch profile through 360 degrees. the axis is the sketch-local `x` or `y` axis through the sketch origin.

```json
{
  "action": "create",
  "expectedRevision": 1,
  "requestId": "revolve-pulley-1",
  "featureId": "pulley",
  "kind": "revolve",
  "parameters": {
    "profile": {"sketchId": "pulley_profile"},
    "axis": "x",
    "angle": {"value": 360, "unit": "deg"},
    "operation": "new_body"
  }
}
```

## supported scope

- use exactly one primitive profile or one solved constrained-sketch outer loop. omit `profileIds` to select it; an explicit one-id list remains valid.
- inner loops and multiple profiles are rejected. offset one outer profile from the axis to make a bore.
- the profile must stay on one side of the axis. a complete straight boundary segment within `1e-7 mm` of the axis counts as lying on the axis, generates no face, and has no selectable role. a near-axis segment farther than `1e-7 mm` from the axis keeps its side face.
- point-only axis contact, curved axis contact, and axis crossing are rejected.
- only `new_body`, 360 degrees, and local `x` or `y` are supported.
- acceptance includes one-solid validation and a STEP write/reimport comparison. any failure leaves the document bytes and revision unchanged.

## stable roles

each non-axis profile segment maps through OCCT generation history to `side(<segment-id>)`. there are no angular cap roles for a full revolution. result edges between two generated faces have roles such as `between(side(step),side(small_od))`. periodic seam edges are not selectable because they do not lie between two distinct provenance faces.

use `query` with `{"select":{"kind":"edge_roles","id":"pulley"}}` before a fillet or chamfer. update the source sketch to change the revolved body while preserving the revolve feature and segment identity.
