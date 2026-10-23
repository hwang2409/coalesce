# holes and hole patterns

A `hole` cuts along global +Z. Omit `depth` for a through hole. Supply exactly one of `position`, `positions`, or `pattern`.

Explicit positions keep caller-provided names. A pattern stores its geometric intent in the typed feature graph and expands through the same named-position kernel path. Pattern member names are always `<namePrefix>1` through `<namePrefix>N`, with a maximum count of 64.

## circular pattern

```json
{
  "action": "create",
  "expectedRevision": 2,
  "requestId": "bolt-holes",
  "featureId": "bolt_holes",
  "kind": "hole",
  "parameters": {
    "target": {"featureId": "disc"},
    "diameter": {"value": 3, "unit": "mm"},
    "pattern": {
      "kind": "circular",
      "center": [0, 0],
      "radius": {"value": 12, "unit": "mm"},
      "count": 6,
      "startAngle": {"value": 0, "unit": "deg"},
      "namePrefix": "bolt"
    }
  }
}
```

Angles increase counterclockwise from +X. `startAngle` accepts `deg` or `rad`.

## linear pattern

```json
{
  "action": "create",
  "expectedRevision": 2,
  "requestId": "row-holes",
  "featureId": "row_holes",
  "kind": "hole",
  "parameters": {
    "target": {"featureId": "plate"},
    "diameter": {"value": 3, "unit": "mm"},
    "pattern": {
      "kind": "linear",
      "start": [-12, -8],
      "direction": [3, 4],
      "pitch": {"value": 8, "unit": "mm"},
      "count": 4,
      "namePrefix": "row"
    }
  }
}
```

`direction` must be non-zero. Coalesce normalizes and stores it, so pitch is the exact center-to-center distance.

## update and references

Update pattern intent without resending generated coordinates. The update kind must match the stored pattern:

```json
{
  "action": "update",
  "expectedRevision": 3,
  "requestId": "resize-bolt-circle",
  "featureId": "bolt_holes",
  "kind": "hole",
  "set": {
    "pattern": {
      "kind": "circular",
      "radius": {"value": 10, "unit": "mm"},
      "count": 4
    }
  }
}
```

Pattern member identity is its ordinal name. A surviving `<namePrefix>N` reference follows that member when circular count, radius, center, or start angle changes move it, or when linear pitch, start, or direction changes move it. In particular, changing a circular count re-spaces the surviving members and moves downstream fillets, chamfers, and cuts with their named members. A smaller count removes trailing members, so a reference to removed `bolt6` resolves `EMPTY`. Changing `namePrefix` makes every old reference stale. Coalesce never falls back to a generated index or the nearest shape. Use explicit named positions when a reference must stay at a fixed place.

Pattern creation and update are atomic. Coalesce rejects overlapping members, members that remove no material, and members that break out of a body edge. Member-specific errors include `names` or `pairs`.
