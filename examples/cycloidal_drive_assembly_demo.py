"""Build, inspect, restart, and export a cycloidal drive assembly through public MCP."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
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


def _transform(translation=(0, 0, 0), axis=(0, 0, 1), angle=0):
    return {
        "translation": {"value": list(translation), "unit": "mm"},
        "rotation": {"axis": list(axis), "angle": {"value": angle, "unit": "deg"}},
    }


def _occurrence(revision, occurrence_id, part_id, transform):
    return {
        "action": "create",
        "kind": "assembly_occurrence",
        "expectedRevision": revision,
        "requestId": f"place-{occurrence_id}",
        "occurrenceId": occurrence_id,
        "parameters": {"partId": part_id, "transform": transform},
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
            "create center bore",
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
        ("place disc", "feature", _occurrence(7, "disc", "disc/disc", _transform((2, 0, 0)))),
        (
            "place shaft",
            "feature",
            _occurrence(8, "shaft", "shaft/shaft", _transform((0, 0, 0), (0, 1, 0), 90)),
        ),
    ]
    revision = 9
    for index in range(8):
        angle = 2 * math.pi * index / 8
        occurrence_id = f"ring-pin-{index + 1}"
        requests.append(
            (
                f"place {occurrence_id}",
                "feature",
                _occurrence(
                    revision,
                    occurrence_id,
                    "pin/pin",
                    _transform((30 * math.cos(angle), 30 * math.sin(angle), -1)),
                ),
            )
        )
        revision += 1
    return requests


async def _screenshots(module: str, url: str, output: Path, revision: int) -> None:
    returncode, stdout, stderr = await _run_process(
        "node", str(SCREENSHOT_HELPER), module, url, str(output), str(revision), timeout=90
    )
    if returncode:
        raise DemoFailure(f"screenshot capture failed: {stderr.strip() or stdout.strip()}")


async def run_demo(output: Path, *, screenshots: bool) -> None:
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
        async with (
            stdio_client(server, errlog=errlog) as (read, write),
            ClientSession(read, write) as session,
        ):
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

            async def call(label, name, arguments):
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
                if not response.get("ok"):
                    raise DemoFailure(
                        f"{label} failed: {response.get('error', {}).get('code', 'UNKNOWN')}"
                    )
                return response

            for label, name, request in _requests():
                await call(label, name, request)
            revision = 17
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
                        *(
                            {
                                "id": f"disc-pin-{index}-clearance",
                                "kind": "assembly_interference",
                                "occurrenceIds": ["disc", f"ring-pin-{index}"],
                                "minimumClearance": {"value": 0, "unit": "mm"},
                            }
                            for index in range(1, 9)
                        ),
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
            if preflight["validation"]["xcaf"]["occurrenceCount"] != 10:
                raise DemoFailure("XCAF preflight did not preserve ten occurrences")
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
    async with stdio_client(restart_server) as (read, write), ClientSession(read, write) as session:
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
