# one-sided revolve

Revolve creates one new solid by rotating one closed sketch profile through a positive angle up to 360 degrees from the sketch-local zero plane around the local x or y axis.

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
    "angle": {"value": 90, "unit": "deg"},
    "operation": "new_body"
  }
}
```

## supported scope

- use exactly one primitive profile or one solved constrained-sketch outer loop. omit `profileIds` to select it; an explicit one-id list remains valid.
- inner loops and multiple profiles are rejected. offset one outer profile from the axis to make a bore.
- the profile must stay on one side of the axis. a complete straight boundary segment within `1e-7 mm` of the axis counts as lying on the axis, generates no face, and has no selectable role. a near-axis segment farther than `1e-7 mm` from the axis keeps its side face.
- point-only axis contact, curved axis contact, and axis crossing are rejected.
- only `new_body`, angles greater than 0 and at most 360 degrees, and local `x` or `y` axes are supported. angles use the existing `deg` or `rad` quantity convention and rotate one-sided in the positive direction.
- update only the angle in place with `{"action":"update","kind":"revolve","set":{"angle":{"value":180,"unit":"deg"}},...}`. update the source sketch separately to reshape the body.
- acceptance includes one-solid validation and a STEP write/reimport comparison. any failure leaves the document bytes and revision unchanged.

join, symmetric or midplane rotation, arbitrary axes, thin revolve, and multiple profiles are not supported.

## stable roles

each non-axis profile segment maps through OCCT generation history to `side(<segment-id>)`. a partial revolution also maps OCCT `FirstShape` and `LastShape` history to exactly one `cap_start` and one `cap_end`. a 360-degree revolution has no angular cap roles. changing 360 degrees to a partial angle creates those roles; changing back to 360 degrees makes saved cap references resolve as `EMPTY`. coalesce never infers a cap by face index or normal.

result edges between generated faces have roles such as `between(side(step),side(small_od))`. periodic seam edges are not selectable because they do not lie between two distinct provenance faces.

use `query` with `{"select":{"kind":"face_roles","id":"pulley"}}` to inspect cap and side faces. use `query` with `{"select":{"kind":"edge_roles","id":"pulley"}}` before a fillet or chamfer. source-sketch and angle edits preserve surviving segment-side identity.
