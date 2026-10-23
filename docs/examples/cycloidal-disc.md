# seven-lobe cycloidal disc

This example makes a 7-lobe cycloidal-drive disc, extrudes it by 5 mm, adds an 8 mm center bore, and exports STEP.

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
        "requestId": "center-bore",
        "featureId": "center_bore",
        "kind": "hole",
        "parameters": {
            "target": {"featureId": "cycloid_disc"},
            "diameter": {"value": 8, "unit": "mm"},
            "position": [0, 0],
        },
    }),
    ("export", {
        "expectedRevision": 3,
        "partIds": ["center_bore"],
        "path": "/tmp/cycloidal-disc.step",
    }),
]
```

The spline is one periodic analytic B-spline edge. Its extruded side is exported as a B-spline surface. The center bore is a separate feature, so the outline remains one simple outer loop.
