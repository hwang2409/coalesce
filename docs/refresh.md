# board refresh changes

A successful `import` refresh returns `boardChanges`. The object always contains:

- `mountingHoles`: changed mounting-hole records.
- `footprints`: changed non-mounting-footprint records.
- `outlineChanged`: `true` when the imported outer outline data changed.
- `dependentFeatures`: regenerated feature-kind names in deterministic order.

Each mounting-hole or footprint record has `id`, `reference`, `oldPosition`,
`newPosition`, `distance`, and `change`. A position is an `[x, y]` pair in mm.
An absent endpoint is `null`. `distance` is the endpoint distance in mm when both
positions exist; otherwise it is `null`. `change` is `added`, `removed`, `moved`,
or `changed`.

Changed board dimensions add these optional fields:

- `thickness: {before, after}` gives the old and new thickness in mm.
- `outlineBounds: {before: {width, height}, after: {width, height}}` gives the
  exact aggregate bounds in mm across all outer outline loops. Either value is
  `null` if that snapshot has no outer outline.

The refresh summary always starts with `refreshed board:`. It reports whole change
items and is at most 160 characters. When not all items fit, `+N more` gives the
exact number omitted.
