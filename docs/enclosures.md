# pcb enclosure screw closures

create `screw_closure` after the base, normal board standoffs, and lid. each of its 2–8 points has a stable caller-supplied name and an explicit xy position. place the full boss outside the exact pcb volume, inside the base floor, clear of walls, and clear of standoffs. invalid creation and refresh operations fail atomically and identify the point and its conflict.

```json
{
  "action": "create",
  "featureId": "closure",
  "kind": "screw_closure",
  "parameters": {
    "base": {"featureId": "base"},
    "lid": {"featureId": "lid"},
    "points": [
      {"name": "left", "point": [-2.5, 7]},
      {"name": "right", "point": [22.5, 7]}
    ],
    "basePilotDiameter": {"value": 1.6, "unit": "mm"},
    "bossDiameter": {"value": 4, "unit": "mm"},
    "lidClearanceDiameter": {"value": 2.4, "unit": "mm"}
  }
}
```

update `points`, `basePilotDiameter`, `bossDiameter`, or `lidClearanceDiameter` on the same feature id. the supplied points replace the complete named set. a removed or renamed point is deleted; coalesce never rebinds it by position or list index.

check the closure with an explicit exact residual-wall requirement:

```json
{
  "id": "closure-fit",
  "kind": "screw_closure",
  "minimumWall": {"value": 1, "unit": "mm"}
}
```

this requirement compares `minimumWall` with the measured minimum boss wall. it also requires all named pilot and clearance holes to be coaxial and requires zero base/lid interference. later features can target the original base or lid id; only that manufacturing body changes.
