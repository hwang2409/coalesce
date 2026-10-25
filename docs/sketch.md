# constrained sketch solver

Coalesce keeps constrained-sketch solving behind `src/coalesce/sketch_solver.py`. The base package does not import or require a solver. Each `DocumentService` supervises one serialized solver child. It reuses that child for successful solves, kills and reaps it after a timeout or protocol failure, and starts a replacement only for the next solve. Server shutdown also kills and reaps the child.

The solve bound defaults to 1 second. Configure it from 0.5 through 120 seconds with `--sketch-solve-timeout SECONDS` or `COALESCE_SKETCH_SOLVE_TIMEOUT`. The configured value appears in the MCP server instructions.

## install

Install the optional solver extra:

```sh
pip install 'coalesce[sketch]'
```

PlaneGCS publishes a Python 3.13 Linux wheel. On macOS, install its build prerequisites once before pip builds it from source:

```sh
brew install eigen boost
pip install 'coalesce[sketch]'
```

## seam guarantees

Call `solve_sketch()` with coalesce-owned points, lines, arcs, circles, dimensions, constraints, initial values, and branch landmarks. Each item has a stable caller-supplied id. The result:

- returns solved geometry keyed by those ids;
- reports degrees of freedom and under-, fully-, over-, or redundantly constrained state;
- maps redundant and conflicting solver diagnostics back to stable constraint ids;
- rebuilds solver input in stable id order for deterministic results;
- rejects a branch flip or ambiguous landmark without returning partial geometry; and
- does not expose or persist PlaneGCS handles.

A successful numeric solve is not enough to accept geometry. Callers that edit a sketch must provide branch landmarks from the last accepted result. The seam fails closed if a landmark changes sign or becomes too close to zero.

### failed-solve diagnostic geometry

A redundant, conflicting, or non-converged edit can return the solver's finite last iterate as `error.diagnosticGeometry`. The object always contains `accepted: false`; its `entities` are keyed by the stable ids from that request. It is response-only diagnostic data, not CAD geometry. Coalesce never stores it in the document, publishes it as a viewer scene revision, uses it as a reference or check input, or exports it.

Coordinates are rounded to six decimal places. The response contains no more entities than the request and is limited to 64 KiB by `MAX_DIAGNOSTIC_GEOMETRY_BYTES`. If it exceeds that limit, `diagnosticGeometry` is absent and `diagnosticGeometryOmitted` reports `payload_too_large` and the byte limit. A branch flip, malformed input, missing solver dependency, solver crash or protocol failure, timeout, or non-finite last iterate has no `diagnosticGeometry`. Redundant and conflicting constraint ids remain in the normal solve diagnostics.

### geometry conventions

Line and angle direction follows each line's ordered `start` → `end` points. An `Arc` names its center, start point, and end point in that order. Arc angles use radians. The arc starts at `start_angle` and sweeps counterclockwise to `end_angle`; its sweep must be more than zero and less than `2π`. A positive angle dimension rotates counterclockwise from its first directed line to its second; a negative value rotates clockwise.

`initial` can override a point with `(x, y)`, an arc with `(radius, start_angle, end_angle)`, or a circle with `(radius,)`. Each tuple must have exactly that arity and contain only finite numbers. Lines do not take an `initial` tuple. A line's two effective initial endpoint coordinates must be distinct.

### ordered operands

The following table is the coalesce-owned contract. `length` uses the caller's sketch unit. Distances are unsigned. Operand order is significant unless both roles have the same name. The `OPERAND_SIGNATURES` table in `sketch_solver.py` is the machine-readable form used by validation and the PlaneGCS adapter.

| kind | ordered operands | value |
| --- | --- | --- |
| `distance` | `(point, point)` first point to second point; `(point, line)` perpendicular point-to-line distance | length |
| `radius` | `(arc)` or `(circle)` | positive length |
| `diameter` | `(arc)` or `(circle)` | positive length |
| `angle` | `(line, line)` first directed line to second directed line | signed radians; positive is counterclockwise |
| `coincident` | `(point, point)` | none |
| `horizontal` | `(line)` | none |
| `vertical` | `(line)` | none |
| `horizontal_points` | `(point, point)` | none |
| `vertical_points` | `(point, point)` | none |
| `parallel` | `(line, line)` | none |
| `perpendicular` | `(line, line)` | none |
| `tangent` | `(line, arc)`, `(line, circle)`, `(arc, arc)`, `(circle, arc)`, or `(circle, circle)` | none |
| `tangent` with an endpoint | `(line, arc, point)`; the point must be an endpoint shared by that line and arc | none |
| `equal` | `(line, line)`, `(arc, arc)`, `(circle, circle)`, `(circle, arc)`, or `(arc, circle)` | none |
| `symmetric` | `(point, point, point)` around a center point; `(point, point, line)` around a center line | none |
| `point_on` | `(point, line)`, `(point, arc)`, or `(point, circle)` | none |
| `fix` | `(point)` at its effective initial `(x, y)` | none |

Invalid ids, references, initial tuples, geometry, or operand signatures return `INVALID_INPUT`. The error names the responsible entity or constraint id before the PlaneGCS adapter builds solver objects.

## public constrained sketch form

The existing `sketch` tool accepts one mutually exclusive `constrained` form. It stores
stable solver-owned ids and the accepted solved result; coordinates are never solve-and-discard.
The shape is `{kind: "constrained", points, lines, arcs, circles, dimensions, constraints,
initial, landmarks}`. `query` returns `solve.status`, `solve.dof`, solved entities,
diagnostics, and landmark values. For example, a fixed 10 mm horizontal line is:

```json
{"action":"create","sketchId":"bracket","expectedRevision":0,"requestId":"r1",
 "plane":{"kind":"origin","name":"XY"},
 "constrained":{"kind":"constrained",
  "points":[{"id":"a","x":0,"y":0},{"id":"b","x":10,"y":0}],
  "lines":[{"id":"edge","start":"a","end":"b"}],
  "dimensions":[{"id":"length","kind":"distance","entities":["a","b"],"value":10}],
  "constraints":[{"id":"fix-a","kind":"fix","entities":["a"]},{"id":"horizontal","kind":"horizontal","entities":["edge"]}]}}
```

## design intent and edits

A solve status describes geometric freedom, not edit quality. Fixing every point can return
`fully_constrained` with zero degrees of freedom, but it leaves no editable driving dimensions.
Fix only the anchors that define placement. Give dimensions stable ids for sizes that will change,
such as `opening-radius`. Update only those driving values by id:

```json
{"action":"update","expectedRevision":2,"requestId":"resize-opening","sketchId":"profile","set":{"dimensionValues":{"opening-radius":4}}}
```

The update preserves every entity, constraint, and dimension id, then solves and regenerates all
downstream features atomically. Values must be finite; lengths, radii, and diameters must be
positive. Unknown ids return a bounded, sorted list of valid driving dimension ids, and reference
dimensions cannot be updated. A failed solve leaves the revision and document unchanged.
For compatibility, a string-valued `set` that contains a JSON object is decoded up to a fixed
256 KiB UTF-8 limit; larger, malformed, or excessively nested values are rejected without a write.

Full replacement through `set.constrained` remains accepted. It replaces the constrained definition.
Resend the same point, entity,
dimension, and constraint ids with the changed value.

For an outer loop with directly contained inner loops, send all loops in one constrained sketch.
To extrude every loop, omit `profileIds`; inner loops become holes. The extrusion stores the
resolved `[<sketch id>]` value so later sketch edits cannot silently change its profile identity.
Explicit `[<sketch id>]` payloads remain valid. To cut it, set `tool` to
`{"sketchId": "<sketch id>"}`.

Query the current revision before the first mutation. Each successful mutation returns the next
revision; use it as the following call's `expectedRevision` and use a new `requestId`. Keep the
same `sketchId` when editing a sketch so downstream features regenerate from that identity.

## primitive closed outlines

Primitive sketches support two free-form closed outline kinds in addition to rectangles and circles:

- `polyline` joins 3 to 2000 points with straight segments. Each role names its endpoint point ids, such as `seg-p0-p1`, including the closing segment.
- `spline` creates one periodic interpolating B-spline through 3 to 2000 points. It stays one analytic edge with the single role `spline`; it has no segment roles and is not polygonized.

A point can be `[x, y]` or `{"id": "corner", "x": x, "y": y}`. References follow point ids. Array points get default ids `p0`, `p1`, and so on by list position, so reordering or rotating that list moves the ids to different coordinates. Give points explicit ids if you will reorder them.

Coordinates are finite millimeter values. Set `closed` to `true`; open outlines are not supported. Consecutive duplicate points, near-zero-area outlines, and exact wire or face self-intersections are rejected atomically before the sketch is accepted. For primitive outlines, inner holes are separate cut or hole features; for constrained sketches, use the single-sketch route above. The entity `id` is optional; when omitted, it defaults to the kind and one-based entity position, such as `spline-1`. These default ids are positional. If an extrusion or revolution stores one, any later entity-list change is rejected atomically with `REFERENCE_FAILED`; give outlines explicit ids to keep profile references across entity edits. Explicit ids continue to follow their entities across reorder and remove-and-append edits.

This example creates and extrudes a triangular polyline:

```json
{"action":"create","expectedRevision":0,"requestId":"triangle-sketch","sketchId":"triangle_profile","plane":{"kind":"origin","name":"XY"},"entities":[{"id":"triangle","kind":"polyline","points":[[0,0],[30,0],[0,20]],"closed":true}]}
```

```json
{"action":"create","expectedRevision":1,"requestId":"triangle-extrude","featureId":"triangle","kind":"extrude","parameters":{"profile":{"sketchId":"triangle_profile"},"distance":{"value":5,"unit":"mm"},"operation":"new_body","direction":"normal"}}
```

How a downstream reference (for example a fillet on a profile edge) behaves when you update the outline points:

- polyline with explicit point ids: a segment reference follows its two endpoint ids. Moving, reordering, or rotating the points keeps it on the same segment. If an endpoint id is removed or replaced, the reference resolves as `EMPTY`.
- polyline with default ids (array points): segment identity is positional. While any downstream reference exists, every change to the point list (coordinates, order, or count) is rejected atomically with `REFERENCE_FAILED` (`EMPTY`) and the hint to give the points explicit ids. With no downstream references, you can edit the points freely.
- spline: the single `spline` role stays bound across any point edit, including a point-count change.

Documents saved before point ids were recorded load as default-id polylines, which is the conservative rule. coalesce never silently binds a reference to a different edge.

See [the seven-lobe cycloidal disc example](examples/cycloidal-disc.md) for a complete spline, extrusion, center bore, and STEP export workflow.

## exact closed profile extrusion

A constrained sketch can supply one outer closed non-construction loop and directly
contained inner loops to the existing `extrude` and `cut` features. For `extrude`, omit
`profileIds` to select all loops, or explicitly use the sketch id as its single value. For `cut`,
set `tool` to `{"sketchId": "<sketch id>"}`. Inner loops become holes. Lines, arcs, and circles
remain analytic; construction geometry is ignored. Loop containment is derived from
the exact solved faces, not entity order. Open, degenerate, self-intersecting,
touching, crossing, ambiguous, multiple-outer, and three-level island profiles are
rejected with `SKETCH_PROFILE_UNSUPPORTED`. A profile can contain at most 64 loops;
this bounds exact pairwise containment checks to 2,016 while allowing detailed parts.
The `too_many_loops` reason includes the `limit` and actual `count`. Other structured
reasons use `loops` to name the involved segment or circle ids. A profile is
`degenerate` when a solved line or arc collapses within the scale-aware OCCT precision
floor. Segment references from outer and inner loops use the same typed sketch-segment
provenance and can drive downstream treatments.

This complete line-and-arc bracket example is sent unchanged through the real stdio
server by the acceptance test:

<!-- bracket-stdio -->
```json
[
  {
    "tool": "sketch",
    "arguments": {
      "action": "create",
      "expectedRevision": 0,
      "requestId": "bracket-sketch",
      "sketchId": "bracket_profile",
      "plane": {"kind": "origin", "name": "XY"},
      "constrained": {
        "kind": "constrained",
        "points": [
          {"id": "origin", "x": 0, "y": 0},
          {"id": "tangent", "x": 0, "y": 20},
          {"id": "end", "x": -20, "y": 0},
          {"id": "center", "x": -20, "y": 20, "construction": true}
        ],
        "lines": [
          {"id": "side", "start": "origin", "end": "tangent"},
          {"id": "base", "start": "origin", "end": "end"}
        ],
        "arcs": [
          {
            "id": "corner",
            "center": "center",
            "start": "tangent",
            "end": "end",
            "radius": 20,
            "start_angle": 0,
            "end_angle": 4.71238898038469
          }
        ],
        "dimensions": [
          {"id": "side-length", "kind": "distance", "entities": ["origin", "tangent"], "value": 20},
          {"id": "corner-radius", "kind": "radius", "entities": ["corner"], "value": 20}
        ],
        "constraints": [
          {"id": "fix-origin", "kind": "fix", "entities": ["origin"]},
          {"id": "base-horizontal", "kind": "horizontal", "entities": ["base"]},
          {"id": "equal-sides", "kind": "equal", "entities": ["side", "base"]},
          {"id": "side-tangent", "kind": "tangent", "entities": ["side", "corner", "tangent"]}
        ]
      }
    }
  },
  {
    "tool": "feature",
    "arguments": {
      "action": "create",
      "expectedRevision": 1,
      "requestId": "bracket-extrude",
      "featureId": "bracket",
      "kind": "extrude",
      "parameters": {
        "profile": {"sketchId": "bracket_profile", "profileIds": ["bracket_profile"]},
        "distance": {"value": 5, "unit": "mm"},
        "operation": "new_body",
        "direction": "normal"
      }
    }
  },
  {"tool": "query", "arguments": {"select": {"kind": "measurements"}}}
]
```

## replace the solver

PlaneGCS is a separate optional dependency. To use a different compatible solver, replace only the private implementation in `src/coalesce/sketch_solver.py` while keeping its coalesce-owned request and result types stable. No other coalesce module imports PlaneGCS.
