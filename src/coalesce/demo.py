"""Build, edit, inspect, restart, and export a mate-driven cycloidal drive through MCP."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

PIN_RADIUS = 4


class DemoFailure(RuntimeError):
    pass


def cycloid_points():
    import math
    lobes, pin_circle_radius, pin_radius, eccentricity = 7, 30.0, 4.0, 2.0
    samples = lobes * 40
    ring_pins = lobes + 1
    return [[
        pin_circle_radius * math.cos(a) - pin_radius * math.cos(a + math.atan2(math.sin((1-ring_pins)*a), pin_circle_radius/(eccentricity*ring_pins)-math.cos((1-ring_pins)*a))) - eccentricity * math.cos(ring_pins*a),
        -pin_circle_radius * math.sin(a) + pin_radius * math.sin(a + math.atan2(math.sin((1-ring_pins)*a), pin_circle_radius/(eccentricity*ring_pins)-math.cos((1-ring_pins)*a))) + eccentricity * math.sin(ring_pins*a),
    ] for i in range(samples) for a in [2 * math.pi * i / samples]]


def result_json(result):
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    return structured if structured is not None else json.loads(result.content[0].text)


def viewer_url(errlog):
    errlog.flush(); errlog.seek(0)
    for line in errlog:
        if "coalesce viewer:" in line:
            return line.split("coalesce viewer:", 1)[1].strip()
    return ""


async def _run_process(*command, timeout):
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except BaseException:
        process.terminate(); await process.wait(); raise
    return process.returncode, stdout.decode(), stderr.decode()


async def playwright_module():
    return None
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

GUIDE_URI = "coalesce://docs/examples/cycloidal-drive-assembly"
SCREENSHOT_HELPER = Path(__file__).with_name("cycloidal_drive_assembly_screenshots.mjs")
PATTERN_RADIUS = 30
EDITED_PATTERN_RADIUS = 31
PIN_COUNT = 8


def _assert_bom_matches_xcaf(assembly, xcaf):
    bom = assembly["bom"]
    if len(bom["rows"]) != xcaf["definitionCount"] or sum(
        row["count"] for row in bom["rows"]
    ) != xcaf["occurrenceCount"]:
        raise DemoFailure("XCAF preflight counts do not agree with the assembly BOM")
    pin_row = next((row for row in bom["rows"] if row["partId"] == "pin/pin"), None)
    if len(bom["rows"]) != 4 or pin_row is None or pin_row["count"] != 8:
        raise DemoFailure("assembly BOM did not preserve four definitions and eight pins")


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


def _sketch_side_reference(
    feature, sketch, entity, segment="circle", *, body_feature=None, body_entity=None
):
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
        "body": _body(body_feature or feature, body_entity or entity),
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


def _mate(
    revision,
    mate_id,
    kind,
    first_id,
    first_ref,
    second_id,
    second_ref,
    *,
    orientation=None,
    offset=0,
):
    parameters = {
        "kind": kind,
        "first": {"occurrenceId": first_id, "reference": first_ref},
        "second": {"occurrenceId": second_id, "reference": second_ref},
        "orientation": orientation or ("aligned" if kind == "concentric" else "opposed"),
    }
    if kind == "planar":
        parameters["offset"] = {"value": offset, "unit": "mm"}
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
            "create shaft shoulder sketch",
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
                        "kind": "circle",
                        "center": [0, 0],
                        "radius": {"value": 6, "unit": "mm"},
                    }
                ],
            },
        ),
        (
            "extrude shaft shoulder",
            "feature",
            {
                "action": "create",
                "expectedRevision": 4,
                "requestId": "shaft-extrude",
                "featureId": "shaft",
                "kind": "extrude",
                "parameters": {
                    "profile": {"sketchId": "shaft_profile", "profileIds": ["shaft"]},
                    "distance": {"value": 1, "unit": "mm"},
                    "operation": "new_body",
                    "direction": "normal",
                },
            },
        ),
        (
            "create eccentric journal sketch",
            "sketch",
            {
                "action": "create",
                "expectedRevision": 5,
                "requestId": "journal-sketch",
                "sketchId": "shaft_journal_profile",
                "plane": {"kind": "origin", "name": "XY"},
                "entities": [
                    {
                        "id": "journal",
                        "kind": "circle",
                        "center": [2, 0],
                        "radius": {"value": 4, "unit": "mm"},
                    }
                ],
            },
        ),
        (
            "join eccentric journal to shaft",
            "feature",
            {
                "action": "create",
                "expectedRevision": 6,
                "requestId": "journal-join",
                "featureId": "shaft_journal",
                "kind": "extrude",
                "parameters": {
                    "profile": {
                        "sketchId": "shaft_journal_profile",
                        "profileIds": ["journal"],
                    },
                    "distance": {"value": 6, "unit": "mm"},
                    "operation": "join",
                    "target": {"featureId": "shaft"},
                    "direction": "normal",
                },
            },
        ),
        (
            "create pin sketch",
            "sketch",
            {
                "action": "create",
                "expectedRevision": 7,
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
                "expectedRevision": 8,
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
                "expectedRevision": 9,
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
                "expectedRevision": 10,
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
                "expectedRevision": 11,
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
                "expectedRevision": 12,
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
        *((f"ring-pin-{index}", "pin/pin", _transform()) for index in range(1, PIN_COUNT + 1)),
    ]
    for occurrence_id, part_id, transform in placements:
        requests.append(
            (f"place {occurrence_id}", "feature", _occurrence(revision, occurrence_id, part_id, transform))
        )
        revision += 1

    requests.append(("ground ring", "feature", _ground(revision, "ring-ground", "ring")))
    revision += 1

    ring_bore = _hole_reference("ring_center_bore", "center", "ring", "ring")
    shaft_side = _sketch_side_reference("shaft", "shaft_profile", "shaft")
    requests.append(
        (
            "mate shaft to drive axis",
            "feature",
            _mate(revision, "shaft-axis", "concentric", "ring", ring_bore, "shaft", shaft_side),
        )
    )
    revision += 1

    ring_seat = _cap_reference("ring", "ring", "cap_end")
    shaft_shoulder = _cap_reference("shaft", "shaft", "cap_end")
    requests.append(
        (
            "seat shaft on ring",
            "feature",
            _mate(
                revision,
                "shaft-seat",
                "planar",
                "ring",
                ring_seat,
                "shaft",
                shaft_shoulder,
                orientation="aligned",
                offset=1,
            ),
        )
    )
    revision += 1

    pin_side = _sketch_side_reference("pin", "pin_profile", "pin")
    journal_side = _sketch_side_reference(
        "shaft_journal",
        "shaft_journal_profile",
        "journal",
        body_feature="shaft",
        body_entity="shaft",
    )
    disc_bore = _hole_reference("disc_bore", "center", "disc", "disc")
    requests.append(
        (
            "mate disc to eccentric journal",
            "feature",
            _mate(
                revision,
                "disc-eccentric",
                "concentric",
                "shaft",
                journal_side,
                "disc",
                disc_bore,
            ),
        )
    )
    revision += 1

    disc_start = _cap_reference("disc", "disc", "cap_start")
    requests.append(
        (
            "seat disc on shaft shoulder",
            "feature",
            _mate(
                revision,
                "disc-seat",
                "planar",
                "shaft",
                shaft_shoulder,
                "disc",
                disc_start,
            ),
        )
    )
    revision += 1

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


def _manufacturing_occurrence_ids():
    return ["disc", "ring", *(f"ring-pin-{index}" for index in range(1, PIN_COUNT + 1)), "shaft"]


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


async def run_demo(output: Path, *, screenshots: bool = False, pace: float = 0, open_browser: bool = False) -> None:
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
            command=sys.executable,
            args=["-m", "coalesce", "--document", str(document), "--open"] if open_browser else ["-m", "coalesce", "--document", str(document)],
            env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONPATH": os.pathsep.join(filter(None, [str(Path(__file__).parent.parent), os.environ.get("PYTHONPATH")]))},
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

            async def beat(label):
                print(label, flush=True)
                if pace:
                    await asyncio.sleep(pace)

            await beat("beat: build")
            for label, name, request in _requests():
                await call(label, name, request)
            revision = len(_requests())
            await call("query initial assembly", "query", {"select": {"kind": "assembly"}})

            await beat("beat: edit/lost-contact failure")
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

            await beat("beat: restore + interference pass")
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
                            "id": "whole-drive-interference",
                            "kind": "assembly_interference",
                            "occurrenceIds": _manufacturing_occurrence_ids(),
                            "minimumClearance": {"value": 0, "unit": "mm"},
                        },
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
            _assert_bom_matches_xcaf(assembly, xcaf)
            if (xcaf["definitionCount"], xcaf["occurrenceCount"]) != (4, 11):
                raise DemoFailure("XCAF preflight did not preserve four definitions and 11 occurrences")
            await beat("beat: STEP publish")
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

    print(f"viewer URL: {url or 'not available'}")
    print(f"definitions: {xcaf["definitionCount"]}; occurrences: {xcaf["occurrenceCount"]}; overlap: PASS; STEP: {step}")
    print("mate solve: static, not simulated motion")

    restart_server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "coalesce", "--document", str(document)],
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONPATH": os.pathsep.join(filter(None, [str(Path(__file__).parent.parent), os.environ.get("PYTHONPATH")]))},
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
    parser = argparse.ArgumentParser(description="run the installed cycloidal drive demo")
    parser.add_argument("--output", "--out", dest="output", type=Path)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--pace", type=float, default=0, metavar="SECONDS")
    parser.add_argument("--screenshots", action="store_true")
    arguments = parser.parse_args()
    output = arguments.output or Path(tempfile.mkdtemp(prefix="coalesce-demo-"))
    if arguments.pace < 0:
        parser.error("--pace must be non-negative")
    try:
        asyncio.run(run_demo(output, screenshots=arguments.screenshots, pace=arguments.pace, open_browser=arguments.open))
    except (DemoFailure, OSError, FileExistsError) as error:
        print(f"demo failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
