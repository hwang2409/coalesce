"""Packaged guide and session resources for the MCP adapter."""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

from mcp import McpError, types
from mcp.server.lowlevel.helper_types import ReadResourceContents

from .schemas import SUPPORTED_REQUIREMENT_KINDS, TOOL_SCHEMAS


@dataclass(frozen=True)
class Guide:
    name: str
    uri: str
    path: str
    description: str


GUIDES = (
    Guide("coalesce vision", "coalesce://docs/vision", "vision.md", "Product direction and rigor bar."),
    Guide("coalesce errors", "coalesce://docs/errors", "errors.md", "Stable error taxonomy and recovery guidance."),
    Guide("coalesce sketch guide", "coalesce://docs/sketch", "sketch.md", "Sketch entities, constraints, and dimensions."),
    Guide("coalesce hole guide", "coalesce://docs/hole", "hole.md", "Hole placement, patterns, and updates."),
    Guide("coalesce enclosure guide", "coalesce://docs/enclosure", "enclosures.md", "PCB enclosure workflow and checks."),
    Guide("coalesce refresh guide", "coalesce://docs/refresh", "refresh.md", "Board refresh behavior and repair guidance."),
    Guide(
        "cycloidal disc example",
        "coalesce://docs/examples/cycloidal-disc",
        "examples/cycloidal-disc.md",
        "Complete cycloidal disc modeling example.",
    ),
)

INDEX_URI = "coalesce://docs/index"
SESSION_URI = "coalesce://session/capabilities"


def package_version() -> str:
    return version("coalesce")


def listed_resources() -> list[types.Resource]:
    resources = [
        types.Resource(
            name="coalesce guide index",
            uri=INDEX_URI,
            description="Stable URIs for the packaged coalesce guides.",
            mimeType="application/json",
        ),
        types.Resource(
            name="coalesce session capabilities",
            uri=SESSION_URI,
            description="Version, runtime configuration, and supported modeling kinds.",
            mimeType="application/json",
        ),
    ]
    resources.extend(
        types.Resource(
            name=guide.name,
            uri=guide.uri,
            description=guide.description,
            mimeType="text/markdown",
        )
        for guide in GUIDES
    )
    return resources


def _guide_bytes(path: str) -> bytes:
    packaged = files("coalesce").joinpath("guides", path)
    if packaged.is_file():
        return packaged.read_bytes()
    return (Path(__file__).parents[2] / "docs" / path).read_bytes()


def read_resource(
    uri: str, *, sketch_solve_timeout: float, viewer_url: str | None
) -> list[ReadResourceContents]:
    guide = next((item for item in GUIDES if item.uri == uri), None)
    if guide is not None:
        content = _guide_bytes(guide.path).decode("utf-8")
        return [ReadResourceContents(content, "text/markdown")]
    if uri == INDEX_URI:
        content = json.dumps(
            {
                "guides": [
                    {"name": item.name, "uri": item.uri, "description": item.description}
                    for item in GUIDES
                ]
            },
            indent=2,
        )
        return [ReadResourceContents(content, "application/json")]
    if uri == SESSION_URI:
        content = json.dumps(
            {
                "version": package_version(),
                "sketchSolveTimeoutSeconds": sketch_solve_timeout,
                "optionalSolver": {
                    "name": "planegcs",
                    "available": importlib.util.find_spec("planegcs") is not None,
                },
                "viewerUrl": viewer_url,
                "featureKinds": TOOL_SCHEMAS["feature"]["properties"]["kind"]["enum"],
                "checkKinds": list(SUPPORTED_REQUIREMENT_KINDS),
            },
            indent=2,
        )
        return [ReadResourceContents(content, "application/json")]
    raise McpError(
        types.ErrorData(
            code=types.INVALID_PARAMS,
            message=f"unknown coalesce resource: {uri}",
            data={"code": "RESOURCE_NOT_FOUND", "uri": uri},
        )
    )
