"""Build, edit, validate, and export a cycloidal disc through public MCP."""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import math
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

LOBES = 7
PIN_CIRCLE_RADIUS = 30.0
PIN_RADIUS = 4.0
ECCENTRICITY = 2.0
PROFILE_SAMPLES = LOBES * 40
INITIAL_BOLT_RADIUS = 12.0
INITIAL_BOLT_COUNT = 6
EDITED_BOLT_COUNT = 4
GUIDE_URI = "coalesce://docs/examples/cycloidal-disc"
SCREENSHOT_HELPER = Path(__file__).with_name("cycloidal_disc_screenshots.mjs")


class DemoFailure(RuntimeError):
    pass


def cycloid_points() -> list[list[float]]:
    ring_pins = LOBES + 1
    points = []
    for index in range(PROFILE_SAMPLES):
        angle = 2 * math.pi * index / PROFILE_SAMPLES
        correction = math.atan2(
            math.sin((1 - ring_pins) * angle),
            PIN_CIRCLE_RADIUS / (ECCENTRICITY * ring_pins)
            - math.cos((1 - ring_pins) * angle),
        )
        points.append(
            [
                PIN_CIRCLE_RADIUS * math.cos(angle)
                - PIN_RADIUS * math.cos(angle + correction)
                - ECCENTRICITY * math.cos(ring_pins * angle),
                -PIN_CIRCLE_RADIUS * math.sin(angle)
                + PIN_RADIUS * math.sin(angle + correction)
                + ECCENTRICITY * math.sin(ring_pins * angle),
            ]
        )
    return points


def result_json(result) -> dict:
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    return json.loads(result.content[0].text)


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def _run_process(
    *command: str, timeout: float, pid_file: str | None = None
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    if pid_file:
        Path(pid_file).write_text(str(process.pid))
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except BaseException:
        await _stop_process(process)
        raise
    return process.returncode, stdout.decode(), stderr.decode()


async def playwright_module() -> str | None:
    if not shutil.which("node"):
        return None
    candidates = [os.environ.get("PLAYWRIGHT_MODULE")]
    candidates.extend(sorted(glob.glob(os.path.expanduser("~/.npm/_npx/*/node_modules/playwright/index.mjs"))))
    probe = "const m=await import(process.argv[1]);await(await m.chromium.launch()).close()"
    for candidate in filter(None, candidates):
        try:
            returncode, _, _ = await _run_process(
                "node", "--input-type=module", "-e", probe, candidate, timeout=60
            )
        except TimeoutError:
            continue
        if returncode == 0:
            return candidate
    return None


async def capture_screenshots(
    module: str, viewer_url: str, output: Path, stage: str, revision: int
) -> None:
    try:
        returncode, stdout, stderr = await _run_process(
            "node",
            str(SCREENSHOT_HELPER),
            module,
            viewer_url,
            str(output),
            stage,
            str(revision),
            timeout=90,
            pid_file=os.environ.get("COALESCE_CYCLOID_CAPTURE_PID_FILE"),
        )
    except TimeoutError as error:
        raise DemoFailure(f"screenshot capture timed out ({stage})") from error
    if returncode != 0:
        raise DemoFailure(f"screenshot capture failed ({stage}): {stderr.strip() or stdout.strip()}")


def viewer_url(errlog) -> str:
    errlog.flush()
    errlog.seek(0)
    for line in errlog.read().splitlines():
        if line.startswith("coalesce viewer: "):
            return line.removeprefix("coalesce viewer: ")
    raise DemoFailure("the server did not publish a viewer URL")


async def run_demo(output: Path, *, screenshots: bool, edited_bolt_radius: float) -> None:
    output.mkdir(parents=True, exist_ok=False)
    document = output / "cycloidal-disc.coalesce.json"
    step = output / "cycloidal-disc.step"
    transcript_path = output / "transcript.json"
    transcript: list[dict] = []
    transcript_path.write_text("[]\n")

    def record(entry: dict) -> None:
        transcript.append(entry)
        transcript_path.write_text(json.dumps(transcript, indent=2) + "\n")

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
            url = os.environ.get("COALESCE_CYCLOID_VIEWER_URL") or viewer_url(errlog)
            module = await playwright_module() if screenshots else None
            if screenshots and module is None:
                print("screenshots skipped: no launchable Playwright Chromium was found")

            resource = await session.read_resource(GUIDE_URI)
            record(
                {
                    "method": "resources/read",
                    "uri": GUIDE_URI,
                    "revision": 0,
                    "response": [content.text for content in resource.contents],
                }
            )

            async def call(label: str, name: str, arguments: dict) -> dict:
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
                    error = response.get("error", {})
                    raise DemoFailure(f"{label} failed: {error.get('code', 'UNKNOWN')}")
                return response

            await call(
                "create cycloid sketch",
                "sketch",
                {
                    "action": "create",
                    "expectedRevision": 0,
                    "requestId": "cycloid-sketch",
                    "sketchId": "cycloid_profile",
                    "plane": {"kind": "origin", "name": "XY"},
                    "entities": [
                        {
                            "id": "disc_outline",
                            "kind": "spline",
                            "points": cycloid_points(),
                            "closed": True,
                        }
                    ],
                },
            )
            await call(
                "extrude disc",
                "feature",
                {
                    "action": "create",
                    "expectedRevision": 1,
                    "requestId": "cycloid-extrude",
                    "featureId": "cycloid_disc",
                    "kind": "extrude",
                    "parameters": {
                        "profile": {
                            "sketchId": "cycloid_profile",
                            "profileIds": ["disc_outline"],
                        },
                        "distance": {"value": 5, "unit": "mm"},
                        "operation": "new_body",
                        "direction": "normal",
                    },
                },
            )
            await call(
                "create bolt pattern",
                "feature",
                {
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
                            "radius": {"value": INITIAL_BOLT_RADIUS, "unit": "mm"},
                            "count": INITIAL_BOLT_COUNT,
                            "startAngle": {"value": 0, "unit": "deg"},
                            "namePrefix": "bolt",
                        },
                    },
                },
            )
            await call(
                "round tracked bolt",
                "feature",
                {
                    "action": "create",
                    "expectedRevision": 3,
                    "requestId": "round-bolt",
                    "featureId": "rounded_bolt",
                    "kind": "fillet",
                    "parameters": {
                        "target": {"featureId": "bolt_holes"},
                        "edges": [
                            {
                                "feature": "bolt_holes",
                                "role": "between(hole_wall,cap_end)",
                                "cardinality": "one",
                                "filters": [{"kind": "MaxAlong", "direction": [1, 0, 0]}],
                                "source": {
                                    "kind": "hole_surface",
                                    "feature": "bolt_holes",
                                    "name": "bolt2",
                                    "surface": "hole_wall",
                                },
                            }
                        ],
                        "radius": {"value": 0.05, "unit": "mm"},
                    },
                },
            )
            await call(
                "create bore",
                "feature",
                {
                    "action": "create",
                    "expectedRevision": 4,
                    "requestId": "center-bore",
                    "featureId": "center_bore",
                    "kind": "hole",
                    "parameters": {
                        "target": {"featureId": "rounded_bolt"},
                        "diameter": {"value": 8, "unit": "mm"},
                        "position": [0, 0],
                    },
                },
            )
            if module:
                await capture_screenshots(module, url, output, "pre-edit", 5)

            await call(
                "edit bolt pattern",
                "feature",
                {
                    "action": "update",
                    "expectedRevision": 5,
                    "requestId": "edit-bolt-pattern",
                    "featureId": "bolt_holes",
                    "kind": "hole",
                    "set": {
                        "pattern": {
                            "kind": "circular",
                            "center": [0, 0],
                            "radius": {"value": edited_bolt_radius, "unit": "mm"},
                            "count": EDITED_BOLT_COUNT,
                            "startAngle": {"value": 0, "unit": "deg"},
                            "namePrefix": "bolt",
                        }
                    },
                },
            )
            if module:
                await capture_screenshots(module, url, output, "post-edit", 6)

            validity = await call(
                "check valid brep",
                "check",
                {
                    "action": "evaluate",
                    "revision": 6,
                    "requirements": [{"id": "valid", "kind": "valid_brep"}],
                },
            )
            if validity["gate"] != "PASS":
                raise DemoFailure("valid_brep did not pass")
            preflight = await call(
                "fresh-process preflight",
                "export",
                {
                    "action": "preflight",
                    "expectedRevision": 6,
                    "partIds": ["center_bore"],
                    "requirements": [{"id": "valid", "kind": "valid_brep"}],
                },
            )
            if not preflight["ready"]:
                raise DemoFailure("fresh-process export preflight did not pass")
            await call(
                "publish step",
                "export",
                {
                    "action": "publish",
                    "expectedRevision": 6,
                    "partIds": ["center_bore"],
                    "path": str(step),
                },
            )

    print(f"demo written to {output}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="new output directory")
    parser.add_argument(
        "--screenshots",
        action="store_true",
        help="capture pre-edit and post-edit viewer screenshots when Playwright is available",
    )
    parser.add_argument(
        "--pattern-radius",
        type=float,
        default=10.0,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def demo_error(error: BaseException) -> DemoFailure | None:
    if isinstance(error, DemoFailure):
        return error
    if isinstance(error, BaseExceptionGroup):
        return next(
            (failure for nested in error.exceptions if (failure := demo_error(nested)) is not None),
            None,
        )
    return None


def main() -> int:
    arguments = parse_args()
    try:
        asyncio.run(
            run_demo(
                arguments.out.expanduser().resolve(),
                screenshots=arguments.screenshots,
                edited_bolt_radius=arguments.pattern_radius,
            )
        )
    except FileExistsError as error:
        print(f"demo failed: output directory already exists: {error.filename}", file=sys.stderr)
        return 1
    except BaseException as error:
        if failure := demo_error(error):
            print(f"demo failed: {failure}", file=sys.stderr)
            return 1
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
