from __future__ import annotations


def plate_model(radius: float) -> dict:
    points = [
        {"id": "outer-p0", "x": 0, "y": 0},
        {"id": "outer-p1", "x": 40, "y": 0},
        {"id": "outer-p2", "x": 40, "y": 30},
        {"id": "outer-p3", "x": 0, "y": 30},
        {"id": "opening-center", "x": 20, "y": 15},
    ]
    return {
        "kind": "constrained",
        "points": points,
        "lines": [
            {"id": f"outer-edge{index}", "start": f"outer-p{index}", "end": f"outer-p{(index + 1) % 4}"}
            for index in range(4)
        ],
        "circles": [{"id": "opening", "center": "opening-center", "radius": radius}],
        "dimensions": [
            {"id": "opening-radius", "kind": "radius", "entities": ["opening"], "value": radius}
        ],
        "constraints": [
            *[
                {"id": f"fix-outer-p{index}", "kind": "fix", "entities": [f"outer-p{index}"]}
                for index in range(4)
            ],
            {"id": "fix-opening-center", "kind": "fix", "entities": ["opening-center"]},
        ],
    }


def sketch_create(model: dict) -> tuple[str, dict]:
    return (
        "sketch",
        {
            "action": "create",
            "expectedRevision": 0,
            "requestId": "golden-profile",
            "sketchId": "profile",
            "plane": {"kind": "origin", "name": "XY"},
            "constrained": model,
        },
    )


def sketch_update(model: dict, request_id: str) -> tuple[str, dict]:
    return (
        "sketch",
        {
            "action": "update",
            "expectedRevision": 2,
            "requestId": request_id,
            "sketchId": "profile",
            "set": {"constrained": model},
        },
    )


def extrude() -> tuple[str, dict]:
    return (
        "feature",
        {
            "action": "create",
            "expectedRevision": 1,
            "requestId": "golden-plate",
            "featureId": "plate",
            "kind": "extrude",
            "parameters": {
                "profile": {"sketchId": "profile", "profileIds": ["profile"]},
                "distance": {"value": 5, "unit": "mm"},
                "operation": "new_body",
                "direction": "normal",
            },
        },
    )
