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

## replace the solver

PlaneGCS is a separate optional dependency. To use a different compatible solver, replace only the private implementation in `src/coalesce/sketch_solver.py` while keeping its coalesce-owned request and result types stable. No other coalesce module imports PlaneGCS.
