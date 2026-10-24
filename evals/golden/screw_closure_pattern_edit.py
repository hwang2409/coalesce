from __future__ import annotations

import sys
from pathlib import Path

from .common import FIXTURES, finish_artifact, q


def feature(revision, feature_id, kind, parameters, request_id):
    return (
        "feature",
        {
            "action": "create",
            "expectedRevision": revision,
            "requestId": request_id,
            "featureId": feature_id,
            "kind": kind,
            "parameters": parameters,
        },
    )


def calls():
    source = FIXTURES / "features.kicad_pcb"
    points = [
        {"name": "left", "point": [-2.5, 7]},
        {"name": "right", "point": [22.5, 7]},
        {"name": "front", "point": [5, -2.5]},
        {"name": "rear", "point": [15, 12.5]},
    ]
    return [
        (
            "import",
            {
                "action": "create",
                "expectedRevision": 0,
                "requestId": "golden-import",
                "assetId": "board",
                "format": "kicad_pcb",
                "path": str(source.resolve()),
            },
        ),
        feature(
            1,
            "base",
            "pcb_base",
            {
                "board": {"featureId": "board"},
                "wallThickness": q(2),
                "floorThickness": q(2),
                "clearance": q(5),
                "wallHeightAboveBoard": q(4),
                "boardSeatHeight": q(1),
            },
            "golden-base",
        ),
        feature(
            2,
            "mounts",
            "standoffs",
            {
                "target": {"featureId": "base"},
                "board": {"featureId": "board"},
                "selection": {"mode": "all_mounting"},
                "bossDiameter": q(5),
                "pilotDiameter": q(1),
            },
            "golden-mounts",
        ),
        feature(
            3,
            "lid",
            "pcb_lid",
            {"base": {"featureId": "base"}, "thickness": q(2), "fitGap": q(0.25)},
            "golden-lid",
        ),
        feature(
            4,
            "closure",
            "screw_closure",
            {
                "base": {"featureId": "base"},
                "lid": {"featureId": "lid"},
                "points": points,
                "basePilotDiameter": q(1.6),
                "bossDiameter": q(4),
                "lidClearanceDiameter": q(2.4),
            },
            "golden-closure",
        ),
        feature(
            5,
            "lid_pattern",
            "hole",
            {
                "target": {"featureId": "lid"},
                "diameter": q(3),
                "pattern": {
                    "kind": "linear",
                    "start": [2, 2],
                    "direction": [1, 0],
                    "pitch": q(6),
                    "count": 3,
                    "namePrefix": "vent",
                },
            },
            "golden-pattern",
        ),
        (
            "feature",
            {
                "action": "update",
                "expectedRevision": 6,
                "requestId": "golden-pattern-edit",
                "featureId": "lid_pattern",
                "kind": "hole",
                "set": {"pattern": {"kind": "linear", "pitch": q(5), "count": 4}},
            },
        ),
    ]


if __name__ == "__main__":
    finish_artifact(Path(sys.argv[1]), "screw-closure-pattern-edit", calls())
