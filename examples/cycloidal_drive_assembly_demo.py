"""Build, edit, inspect, restart, and export a mate-driven cycloidal drive through MCP."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from cycloidal_disc_demo import (
    PIN_RADIUS,
    DemoFailure,
    _run_process,
    cycloid_points,
    playwright_module,
    result_json,
    viewer_url,
)
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

GUIDE_URI = "coalesce://docs/examples/cycloidal-drive-assembly"
SCREENSHOT_HELPER = Path(__file__).with_name("cycloidal_drive_assembly_screenshots.mjs")
PATTERN_RADIUS = 30
EDITED_PATTERN_RADIUS = 31
PIN_COUNT = 8


def _transform(translation=(0, 0, 0), axis=(0, 0, 1), angle=0):
    return {
        "translation": {"value": list(translation), "unit": "mm"},
        "rotation": {"axis": list(axis), "angle": {"value": angle, "unit": "deg"}},
    }


def _occurrence(revision, occurrence_id, part_id, transform=None):
    return {
        "action": "create",
        "kind": "assembly_occurrence",
        "expectedRevision": revision,
        "requestId": f"place-{occurrence_id}",
        "occurrenceId": occurrence_id,
        "parameters": {"partId": part_id, "transform": transform or _transform()},
    }


def _body(feature, entity):
    return {"kind": "body_source", "feature": feature, "entity": entity}


def _cap_reference(feature, entity, role):
    return {
        "feature": feature,
        "role": role,
        "cardinality": "one",
        "source": {"kind": "tool_cap", "tool": feature, "role": role},
        "body": _body(feature, entity),
    }


def _sketch_side_reference(feature, sketch, entity, segment="circle"):
    return {
        "feature": feature,
        "role": f"side({segment})",
        "cardinality": "one",
        "source": {
            "kind": "sketch_segment",
            "sketch": sketch,
            "entity": entity,
            "segment": segment,
        },
        "body": _body(feature, entity),
    }


def _hole_reference(feature, name, body_feature, body_entity):
    return {
        "feature": feature,
        "role": "hole_wall",
        "cardinality": "one",
        "source": {
            "kind": "hole_surface",
            "feature": feature,
            "surface": "hole_wall",
            "name": name,
        },
        "body": _body(body_feature, body_entity),
    }


def _ground(revision, mate_id, occurrence_id):
    return {
        "action": "create",
        "kind": "assembly_mate",
        "expectedRevision": revision,
        "requestId": mate_id,
        "mateId": mate_id,
        "parameters": {"kind": "ground", "occurrenceId": occurrence_id},
    }


def _mate(revision, mate_id, kind, first_id, first_ref, second_id, second_ref):
    parameters = {
        "kind": kind,
        "first": {"occurrenceId": first_id, "reference": first_ref},
        "second": {"occurrenceId": second_id, "reference": second_ref},
        "orientation": "aligned" if kind == "concentric" else "opposed",
    }
    if kind == "planar":
        parameters["offset"] = {"value": 0, "unit": "mm"}
    return {
        "action": "create",
        "kind": "assembly_mate",
        "expectedRevision": revision,
        "requestId": mate_id,
        "mateId": mate_id,
        "parameters": parameters,
    }


def _requests():
    requests = [
        (
            "create disc sketch",
            "sketch",
            {
                "action": "create",
                "expectedRevision": 0,
                "requestId": "disc-sketch",
                "sketchId": "disc_profile",
                "plane": {"kind": "origin", "name": "XY"},
                "entities": [
                    {"id": "disc", "kind": "spline", "points": cycloid_points(), "closed": True}
                ],
            },
        ),
        (
            "extrude disc",
            "feature",
            {
                "action": "create",
                "expectedRevision": 1,
                "requestId": "disc-extrude",
                "featureId": "disc",
                "kind": "extrude",
                "parameters": {
                    "profile": {"sketchId": "disc_profile", "profileIds": ["disc"]},
                    "distance": {"value": 5, "unit": "mm"},
                    "operation": "new_body",
                    "direction": "normal",
                },
            },
        ),
        (
            "create disc bore",
            "feature",
            {
                "action": "create",
                "expectedRevision": 2,
                "requestId": "disc-bore",
                "featureId": "disc_bore",
                "kind": "hole",
                "parameters": {
                    "target": {"featureId": "disc"},
                    "diameter": {"value": 8, "unit": "mm"},
                    "positions": [{"name": "center", "point": [0, 0]}],
                },
            },
        ),
        (
            "create shaft sketch",
            "sketch",
            {
                "action": "create",
                "expectedRevision": 3,
                "requestId": "shaft-sketch",
                "sketchId": "shaft_profile",
                "plane": {"kind": "origin", "name": "XY"},
                "entities": [
                    {
                        "id": "shaft",
                        "kind": "polyline",
                        "closed": True,
                        "points": [[-10, 0], [10, 0], [10, 1.75], [6, 1.75], [6, 1.5], [-10, 1.5]],
                    }
                ],
            },
        ),
        (
            "revolve shaft",
            "feature",
            {
                "action": "create",
                "expectedRevision": 4,
                "requestId": "shaft-revolve",
                "featureId": "shaft",
                "kind": "revolve",
                "parameters": {
                    "profile": {"sketchId": "shaft_profile", "profileIds": ["shaft"]},
                    "axis": "x",
                    "angle": {"value": 360, "unit": "deg"},
                    "operation": "new_body",
                },
            },
        ),
        (
            "create pin sketch",
            "sketch",
            {
                "action": "create",
                "expectedRevision": 5,
                "requestId": "pin-sketch",
                "sketchId": "pin_profile",
                "plane": {"kind": "origin", "name": "XY"},
                "entities": [
                    {
                        "id": "pin",
                        "kind": "circle",
                        "center": [0, 0],
                        "radius": {"value": PIN_RADIUS, "unit": "mm"},
                    }
                ],
            },
        ),
        (
            "extrude pin",
            "feature",
            {
                "action": "create",
                "expectedRevision": 6,
                "requestId": "pin-extrude",
                "featureId": "pin",
                "kind": "extrude",
                "parameters": {
                    "profile": {"sketchId": "pin_profile", "profileIds": ["pin"]},
                    "distance": {"value": 8, "unit": "mm"},
                    "operation": "new_body",
                    "direction": "normal",
                },
            },
        ),
        (
            "create ring sketch",
            "sketch",
            {
                "action": "create",
                "expectedRevision": 7,
                "requestId": "ring-sketch",
                "sketchId": "ring_profile",
                "plane": {"kind": "origin", "name": "XY"},
                "entities": [
                    {
                        "id": "ring",
                        "kind": "circle",
                        "center": [0, 0],
                        "radius": {"value": 38, "unit": "mm"},
                    }
                ],
            },
        ),
        (
            "extrude ring",
            "feature",
            {
                "action": "create",
                "expectedRevision": 8,
                "requestId": "ring-extrude",
                "featureId": "ring",
                "kind": "extrude",
                "parameters": {
                    "profile": {"sketchId": "ring_profile", "profileIds": ["ring"]},
                    "distance": {"value": 2, "unit": "mm"},
                    "operation": "new_body",
                    "direction": "normal",
                },
            },
        ),
        (
            "create ring centre bore",
            "feature",
            {
                "action": "create",
                "expectedRevision": 9,
                "requestId": "ring-centre-bore",
                "featureId": "ring_center_bore",
                "kind": "hole",
                "parameters": {
                    "target": {"featureId": "ring"},
                    "diameter": {"value": 8, "unit": "mm"},
                    "positions": [{"name": "center", "point": [0, 0]}],
                },
            },
        ),
        (
            "create ring hole pattern",
            "feature",
            {
                "action": "create",
                "expectedRevision": 10,
                "requestId": "ring-holes",
                "featureId": "ring_holes",
                "kind": "hole",
                "parameters": {
                    "target": {"featureId": "ring_center_bore"},
                    "diameter": {"value": 2 * PIN_RADIUS, "unit": "mm"},
                    "pattern": {
                        "kind": "circular",
                        "center": [0, 0],
                        "radius": {"value": PATTERN_RADIUS, "unit": "mm"},
                        "count": PIN_COUNT,
                        "startAngle": {"value": 0, "unit": "deg"},
                        "namePrefix": "bolt",
                    },
                },
            },
        ),
    ]

    revision = len(requests)
    placements = [
        ("ring", "ring/ring", _transform((0, 0, -2))),
        ("disc", "disc/disc", _transform()),
        ("shaft", "shaft/shaft", _transform()),
        ("eccentric", "pin/pin", _transform((2, 0, 0))),
        *((f"ring-pin-{index}", "pin/pin", _transform()) for index in range(1, PIN_COUNT + 1)),
    ]
    for occurrence_id, part_id, transform in placements:
        requests.append(
            (f"place {occurrence_id}", "feature", _occurrence(revision, occurrence_id, part_id, transform))
        )
        revision += 1

    requests.extend(
        [
            ("ground ring", "feature", _ground(revision, "ring-ground", "ring")),
            ("ground eccentric", "feature", _ground(revision + 1, "eccentric-ground", "eccentric")),
        ]
    )
    revision += 2

    ring_bore = _hole_reference("ring_center_bore", "center", "ring", "ring")
    shaft_side = _sketch_side_reference("shaft", "shaft_profile", "shaft", "seg-p4-p5")
    requests.append(
        (
            "mate shaft to drive axis",
            "feature",
            _mate(revision, "shaft-axis", "concentric", "ring", ring_bore, "shaft", shaft_side),
        )
    )
    revision += 1

    pin_side = _sketch_side_reference("pin", "pin_profile", "pin")
    disc_bore = _hole_reference("disc_bore", "center", "disc", "disc")
    requests.append(
        (
            "mate disc to eccentric journal",
            "feature",
            _mate(
                revision,
                "disc-eccentric",
                "concentric",
                "eccentric",
                pin_side,
                "disc",
                disc_bore,
            ),
        )
    )
    revision += 1

    ring_seat = _cap_reference("ring", "ring", "cap_end")
    pin_seat = _cap_reference("pin", "pin", "cap_start")
    for index in range(1, PIN_COUNT + 1):
        occurrence_id = f"ring-pin-{index}"
        requests.extend(
            [
                (
                    f"mate {occurrence_id} to bolt{index}",
                    "feature",
                    _mate(
                        revision,
                        f"pin-{index}-axis",
                        "concentric",
                        "ring",
                        _hole_reference("ring_holes", f"bolt{index}", "ring", "ring"),
                        occurrence_id,
                        pin_side,
                    ),
                ),
                (
                    f"seat {occurrence_id}",
                    "feature",
                    _mate(
                        revision + 1,
                        f"pin-{index}-seat",
                        "planar",
                        "ring",
                        ring_seat,
                        occurrence_id,
                        pin_seat,
                    ),
                ),
            ]
        )
        revision += 2
    return requests


def _disc_pin_requirements():
    return [
        {
            "id": f"disc-pin-{index}-clearance",
            "kind": "assembly_interference",
            "occurrenceIds": ["disc", f"ring-pin-{index}"],
            "minimumClearance": {"value": 0, "unit": "mm"},
        }
        for index in range(1, PIN_COUNT + 1)
    ]


def _pattern_update(revision, request_id, **changes):
    return {
        "action": "update",
        "kind": "hole",
        "expectedRevision": revision,
        "requestId": request_id,
        "featureId": "ring_holes",
        "set": {"pattern": {"kind": "circular", **changes}},
    }


async def _screenshots(module: str, url: str, output: Path, revision: int) -> None:
    returncode, stdout, stderr = await _run_process(
        "node", str(SCREENSHOT_HELPER), module, url, str(output), str(revision), timeout=90
    )
    if returncode:
        raise DemoFailure(f"screenshot capture failed: {stderr.strip() or stdout.strip()}")


async def run_demo(output: Path, *, screenshots: bool) -> None:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    document = output / "cycloidal-drive-assembly.coalesce.json"
    step = output / "cycloidal-drive-assembly.step"
    transcript_path = output / "transcript.json"
    transcript = []

    def record(entry):
        transcript.append(entry)
        transcript_path.write_text(json.dumps(transcript, indent=2) + "\n")

    transcript_path.write_text("[]\n")
    with tempfile.TemporaryFile(mode="w+") as errlog:
        server = StdioServerParameters(
            command=shutil.which("coalesce") or "coalesce",
            args=["--document", str(document)],
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        async with stdio_client(server, errlog=errlog) as (read, write), ClientSession(
            read, write
        ) as session:
            await session.initialize()
            url = viewer_url(errlog)
            guide = await session.read_resource(GUIDE_URI)
            record(
                {
                    "method": "resources/read",
                    "uri": GUIDE_URI,
                    "revision": 0,
                    "response": [item.text for item in guide.contents],
                }
            )

            async def call(label, name, arguments, *, expect_ok=True):
                response = result_json(await session.call_tool(name, arguments))
                record(
                    {
                        "method": "tools/call",
                        "label": label,
                        "name": name,
                        "arguments": arguments,
                        "revision": response.get("revision"),
                        "response": response,
                    }
                )
                if response.get("ok") is not expect_ok:
                    code = response.get("error", {}).get("code", "UNKNOWN")
                    raise DemoFailure(f"{label} returned {code}")
                return response

            for label, name, request in _requests():
                await call(label, name, request)
            revision = len(_requests())
            await call("query initial assembly", "query", {"select": {"kind": "assembly"}})

            moved = await call(
                "move ring pattern radius",
                "feature",
                _pattern_update(
                    revision,
                    "move-ring-pattern",
                    radius={"value": EDITED_PATTERN_RADIUS, "unit": "mm"},
                ),
            )
            revision = moved["revision"]
            edited = await call("query edited assembly", "query", {"select": {"kind": "assembly"}})
            await call(
                "check edited pattern clearance",
                "check",
                {
                    "action": "evaluate",
                    "revision": revision,
                    "requirements": _disc_pin_requirements(),
                },
            )
            stale = await call(
                "reject stale ring member",
                "feature",
                _pattern_update(revision, "remove-bolt8", count=PIN_COUNT - 1),
                expect_ok=False,
            )
            if stale["error"]["code"] != "MATE_REFERENCE_STALE":
                raise DemoFailure("removing bolt8 did not fail with MATE_REFERENCE_STALE")
            unchanged = await call(
                "query after stale edit", "query", {"select": {"kind": "assembly"}}
            )
            if unchanged != edited:
                raise DemoFailure("stale pattern edit changed the assembly")

            restored = await call(
                "restore meshing radius",
                "feature",
                _pattern_update(
                    revision,
                    "restore-ring-pattern",
                    radius={"value": PATTERN_RADIUS, "unit": "mm"},
                ),
            )
            revision = restored["revision"]
            inspection = await call(
                "check assembled drive",
                "check",
                {
                    "action": "evaluate",
                    "revision": revision,
                    "requirements": [
                        {"id": "valid", "kind": "valid_brep"},
                        {
                            "id": "disc-shaft-clearance",
                            "kind": "assembly_interference",
                            "occurrenceIds": ["disc", "shaft"],
                            "minimumClearance": {"value": 0, "unit": "mm"},
                        },
                        *_disc_pin_requirements(),
                    ],
                },
            )
            if inspection["gate"] != "PASS":
                raise DemoFailure("assembled drive checks did not pass")
            assembly = await call("query assembly", "query", {"select": {"kind": "assembly"}})
            preflight = await call(
                "fresh-process assembly preflight",
                "export",
                {"action": "preflight", "mode": "assembly", "expectedRevision": revision},
            )
            xcaf = preflight["validation"]["xcaf"]
            if (xcaf["definitionCount"], xcaf["occurrenceCount"]) != (4, 12):
                raise DemoFailure("XCAF preflight did not preserve four definitions and 12 occurrences")
            await call(
                "publish assembly STEP",
                "export",
                {
                    "action": "publish",
                    "mode": "assembly",
                    "expectedRevision": revision,
                    "path": str(step),
                },
            )
            if screenshots:
                module = await playwright_module()
                if module is None:
                    print("screenshots skipped: no launchable Playwright Chromium was found")
                else:
                    await _screenshots(module, url, output, revision)

    restart_server = StdioServerParameters(
        command=shutil.which("coalesce") or "coalesce",
        args=["--document", str(document)],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    async with stdio_client(restart_server) as (read, write), ClientSession(
        read, write
    ) as session:
        await session.initialize()
        restarted = result_json(await session.call_tool("query", {"select": {"kind": "assembly"}}))
        record(
            {
                "method": "tools/call",
                "label": "restart assembly query",
                "name": "query",
                "arguments": {"select": {"kind": "assembly"}},
                "revision": restarted.get("revision"),
                "response": restarted,
            }
        )
        if restarted != assembly:
            raise DemoFailure("assembly changed after restart")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--screenshots", action="store_true")
    arguments = parser.parse_args()
    try:
        asyncio.run(run_demo(arguments.out, screenshots=arguments.screenshots))
    except (DemoFailure, OSError) as error:
        print(f"demo failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
