from __future__ import annotations

SHOULDER_ROLE = (
    "between(side(seg-shoulder_inner-shoulder_outer),"
    "side(seg-small_end-shoulder_inner))"
)


def shaft_points(small_radius: float) -> list[dict]:
    return [
        {"id": "axis_left", "x": 0, "y": 0},
        {"id": "axis_right", "x": 30, "y": 0},
        {"id": "small_end", "x": 30, "y": small_radius},
        {"id": "shoulder_inner", "x": 20, "y": small_radius},
        {"id": "shoulder_outer", "x": 20, "y": 10},
        {"id": "large_end", "x": 0, "y": 10},
    ]


def profile_entities(small_radius: float) -> list[dict]:
    return [{
        "id": "outline",
        "kind": "polyline",
        "points": shaft_points(small_radius),
        "closed": True,
    }]


def profile_update(request_id: str = "golden-diameter-update") -> tuple[str, dict]:
    return (
        "sketch",
        {
            "action": "update",
            "expectedRevision": 3,
            "requestId": request_id,
            "sketchId": "profile",
            "set": {"entities": profile_entities(8)},
        },
    )


def stage_shaft(service) -> None:
    requests = [
        (
            service.sketch,
            {
                "action": "create",
                "expectedRevision": 0,
                "requestId": "eval-create-shaft-profile",
                "sketchId": "profile",
                "plane": {"kind": "origin", "name": "XY"},
                "entities": profile_entities(6),
            },
        ),
        (
            service.feature,
            {
                "action": "create",
                "expectedRevision": 1,
                "requestId": "eval-create-shaft-revolve",
                "featureId": "shaft",
                "kind": "revolve",
                "parameters": {
                    "profile": {"sketchId": "profile", "profileIds": ["outline"]},
                    "axis": "x",
                    "angle": {"value": 360, "unit": "deg"},
                    "operation": "new_body",
                },
            },
        ),
        (
            service.feature,
            {
                "action": "create",
                "expectedRevision": 2,
                "requestId": "eval-create-shoulder-fillet",
                "featureId": "shoulder_fillet",
                "kind": "fillet",
                "parameters": {
                    "target": {"featureId": "shaft"},
                    "edges": [{
                        "feature": "shaft",
                        "role": SHOULDER_ROLE,
                        "cardinality": "one",
                    }],
                    "radius": {"value": 0.5, "unit": "mm"},
                },
            },
        ),
    ]
    for apply, request in requests:
        result = apply(request)
        if result.get("ok") is not True:
            raise RuntimeError(f"before-state build failed: {result}")
