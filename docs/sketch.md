# constrained sketch solver

Coalesce keeps constrained-sketch solving behind `src/coalesce/sketch_solver.py`. The base package does not import or require a solver.

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

## primitive closed outlines

Primitive sketches support two free-form closed outline kinds in addition to rectangles and circles:

- `polyline` joins 3 to 2000 points with straight segments. Each role names its endpoint point ids, such as `seg-p0-p1`, including the closing segment.
- `spline` creates one periodic interpolating B-spline through 3 to 2000 points. It stays one analytic edge with the single role `spline`; it has no segment roles and is not polygonized.

A point can be `[x, y]` or `{"id": "corner", "x": x, "y": y}`. References follow point ids. Array points get default ids `p0`, `p1`, and so on by list position, so reordering or rotating that list moves the ids to different coordinates. Give points explicit ids if you will reorder them.

Coordinates are finite millimeter values. Set `closed` to `true`; open outlines are not supported. Consecutive duplicate points, near-zero-area outlines, and exact wire or face self-intersections are rejected atomically before the sketch is accepted. Inner holes are separate cut or hole features. The entity `id` is optional; when omitted, it defaults to the kind and one-based entity position, such as `spline-1`.

This example creates and extrudes a triangular polyline:

```json
{"action":"create","expectedRevision":0,"requestId":"triangle-sketch","sketchId":"triangle_profile","plane":{"kind":"origin","name":"XY"},"entities":[{"id":"triangle","kind":"polyline","points":[[0,0],[30,0],[0,20]],"closed":true}]}
```

```json
{"action":"create","expectedRevision":1,"requestId":"triangle-extrude","featureId":"triangle","kind":"extrude","parameters":{"profile":{"sketchId":"triangle_profile","profileIds":["triangle"]},"distance":{"value":5,"unit":"mm"},"operation":"new_body","direction":"normal"}}
```

A reference bound to a polyline segment or spline remains stable when coordinates change and the point count stays the same. A point-count change makes the old reference resolve as `EMPTY`; coalesce does not silently bind it to a different edge.

See [the seven-lobe cycloidal disc example](examples/cycloidal-disc.md) for a complete spline, extrusion, center bore, and STEP export workflow.

## exact closed profile extrusion

A constrained sketch can supply one closed non-construction profile to the existing
`extrude` and `cut` features. Use the sketch id as the single `profileIds` value.
Lines, arcs, and circles remain analytic; construction geometry is ignored. Open,
degenerate, self-intersecting, nested, and multiple loops are rejected with
`SKETCH_PROFILE_UNSUPPORTED` and a structured `reason`. A profile is `degenerate`
when a solved line or arc collapses within the scale-aware OCCT precision floor.

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
