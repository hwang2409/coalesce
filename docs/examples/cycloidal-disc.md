# seven-lobe cycloidal disc

This example makes a 7-lobe cycloidal-drive disc, extrudes it by 5 mm, adds six 3 mm bolt holes on a 12 mm bolt circle and an 8 mm center bore, and exports STEP.

## run the demo

From a clean checkout, run:

```bash
uv run --extra sketch python examples/cycloidal_disc_demo.py --out /tmp/cycloidal-disc-demo --screenshots
```

The output directory contains the revisioned `.coalesce.json` document, `transcript.json` with every public MCP resource and tool call, and the final `cycloidal-disc.step`. If a launchable Playwright Chromium is available, it also contains pre-edit and post-edit viewer screenshots at 1280×800 and 390×844. Without Playwright, the command prints a clear skip message and completes the CAD workflow.

### what you will see

The live viewer first shows the accepted seven-lobe disc with six bolt holes and a center bore. The demo then edits the bolt-circle radius and count in place. The final model has four repositioned bolt holes; `selectionChanges` in the transcript names the tracked moved hole and the two removed holes. The same transcript records the `valid_brep` check, fresh-process export preflight, and STEP publication.

## profile equations

Let:

- `N = 7` be the disc lobe count;
- `K = N + 1` be the mating ring-pin count;
- `R` be the pin-circle radius;
- `r` be the pin radius; and
- `e` be the eccentricity.

For parameter `t` from 0 to `2π`, define

```text
ψ(t) = atan2(sin((1-K)t), R/(eK) - cos((1-K)t))
x(t) = R cos(t) - r cos(t + ψ(t)) - e cos(Kt)
y(t) = -R sin(t) + r sin(t + ψ(t)) + e sin(Kt)
```

These are the standard pin-offset cycloidal-disc equations. The `atan2` form keeps the pressure-angle correction in the correct quadrant. The profile has `K - 1 = N` lobes.

## compute the points

The sketch coordinates are millimeters. Do not repeat the first point; `closed: true` supplies the closing span.

<!-- cycloid-python -->
```python
import math

N = 7
K = N + 1
R, r, e = 30.0, 4.0, 2.0
samples = N * 40
points = []
for index in range(samples):
    t = 2 * math.pi * index / samples
    psi = math.atan2(math.sin((1 - K) * t), R / (e * K) - math.cos((1 - K) * t))
    points.append([
        R * math.cos(t) - r * math.cos(t + psi) - e * math.cos(K * t),
        -R * math.sin(t) + r * math.sin(t + psi) + e * math.sin(K * t),
    ])
```

## exact mcp calls

Run the point snippet first, then send these calls in order. The final path can be any new absolute `.step` path.

<!-- cycloid-stdio -->
```python
calls = [
    ("sketch", {
        "action": "create",
        "expectedRevision": 0,
        "requestId": "cycloid-sketch",
        "sketchId": "cycloid_profile",
        "plane": {"kind": "origin", "name": "XY"},
        "entities": [{
            "id": "disc_outline",
            "kind": "spline",
            "points": points,
            "closed": True,
        }],
    }),
    ("feature", {
        "action": "create",
        "expectedRevision": 1,
        "requestId": "cycloid-extrude",
        "featureId": "cycloid_disc",
        "kind": "extrude",
        "parameters": {
            "profile": {"sketchId": "cycloid_profile", "profileIds": ["disc_outline"]},
            "distance": {"value": 5, "unit": "mm"},
            "operation": "new_body",
            "direction": "normal",
        },
    }),
    ("feature", {
        "action": "create",
        "expectedRevision": 2,
        "requestId": "bolt-holes",
        "featureId": "bolt_holes",
        "kind": "hole",
        "parameters": {
            "target": {"featureId": "cycloid_disc"},
            "diameter": {"value": 3, "unit": "mm"},
            "pattern": {
                "kind": "circular",
                "center": [0, 0],
                "radius": {"value": 12, "unit": "mm"},
                "count": 6,
                "startAngle": {"value": 0, "unit": "deg"},
                "namePrefix": "bolt",
            },
        },
    }),
    ("feature", {
        "action": "create",
        "expectedRevision": 3,
        "requestId": "center-bore",
        "featureId": "center_bore",
        "kind": "hole",
        "parameters": {
            "target": {"featureId": "bolt_holes"},
            "diameter": {"value": 8, "unit": "mm"},
            "position": [0, 0],
        },
    }),
    ("export", {
        "expectedRevision": 4,
        "partIds": ["center_bore"],
        "path": "/tmp/cycloidal-disc.step",
    }),
]
```

The spline is one periodic analytic B-spline edge. Its extruded side is exported as a B-spline surface. The six bolt centers are generated from the stored circular pattern, and the center bore is a separate feature, so the outline remains one simple outer loop.
