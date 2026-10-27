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

from . import schemas


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
    Guide("coalesce revolve guide", "coalesce://docs/revolve", "revolve.md", "One-sided revolve workflow and provenance roles."),
    Guide("coalesce assembly guide", "coalesce://docs/assembly", "assembly.md", "Named occurrences and explicit rigid placements."),
    Guide(
        "cycloidal disc example",
        "coalesce://docs/examples/cycloidal-disc",
        "examples/cycloidal-disc.md",
        "Complete cycloidal disc modeling example.",
    ),
    Guide(
        "cycloidal drive assembly example",
        "coalesce://docs/examples/cycloidal-drive-assembly",
        "examples/cycloidal-drive-assembly.md",
        "Verified XCAF assembly export example.",
    ),
)

INDEX_URI = "coalesce://docs/index"
SESSION_URI = "coalesce://session/capabilities"

RESOURCE_CATALOG = (
    Guide("coalesce guide index", INDEX_URI, "", "Stable URIs for all coalesce resources."),
    Guide(
        "coalesce session capabilities",
        SESSION_URI,
        "",
        "Version, runtime configuration, and supported modeling kinds.",
    ),
    Guide(
        "coalesce tool guide",
        "coalesce://docs/tools",
        "",
        "Short usage guide for the revisioned modeling tools.",
    ),
    Guide(
        "coalesce plate example",
        "coalesce://examples/plate",
        "",
        "A complete rectangle and extrude workflow.",
    ),
    *GUIDES,
)


def package_version() -> str:
    return version("coalesce")


def listed_resources() -> list[types.Resource]:
    return [
        types.Resource(
            name=item.name,
            uri=item.uri,
            description=item.description,
            mimeType=(
                "application/json" if item.uri in {INDEX_URI, SESSION_URI} else "text/markdown"
            ),
        )
        for item in RESOURCE_CATALOG
    ]


def _guide_bytes(path: str) -> bytes:
    packaged = files("coalesce").joinpath("guides", path)
    if packaged.is_file():
        return packaged.read_bytes()
    return (Path(__file__).parents[2] / "docs" / path).read_bytes()


def _session_capabilities(
    *, sketch_solve_timeout: float, viewer_url: str | None
) -> dict[str, object]:
    return {
        "version": package_version(),
        "sketchSolveTimeoutSeconds": sketch_solve_timeout,
        "optionalSolver": {
            "name": "planegcs",
            "available": importlib.util.find_spec("planegcs") is not None,
        },
        "viewerUrl": viewer_url,
        "featureKinds": sorted(
            kind
            for action, kind in schemas.FEATURE_ACTION_SCHEMAS
            if action == "create" and kind is not None
        ),
        "checkKinds": list(schemas.SUPPORTED_REQUIREMENT_KINDS),
    }


def read_resource(
    uri: str, *, sketch_solve_timeout: float, viewer_url: str | None
) -> list[ReadResourceContents]:
    guide = next((item for item in GUIDES if item.uri == uri), None)
    if guide is not None:
        try:
            content = _guide_bytes(guide.path).decode("utf-8")
        except (OSError, UnicodeError):
            raise _resource_unavailable(uri) from None
        return [ReadResourceContents(content, "text/markdown")]
    if uri == INDEX_URI:
        content = json.dumps(
            {
                "uriScheme": (
                    "coalesce:// uses docs for guides, examples for complete workflows, "
                    "and session for runtime capabilities."
                ),
                "resources": [
                    {"name": item.name, "uri": item.uri, "description": item.description}
                    for item in RESOURCE_CATALOG
                ],
            },
            indent=2,
        )
        return [ReadResourceContents(content, "application/json")]
    if uri == SESSION_URI:
        content = json.dumps(
            _session_capabilities(
                sketch_solve_timeout=sketch_solve_timeout, viewer_url=viewer_url
            ),
            indent=2,
        )
        return [ReadResourceContents(content, "application/json")]
    raise _resource_not_found(uri)


def _resource_unavailable(uri: str) -> McpError:
    return McpError(
        types.ErrorData(
            code=types.INTERNAL_ERROR,
            message=f"coalesce packaged resource is unavailable: {uri}",
            data={"code": "RESOURCE_UNAVAILABLE", "uri": uri},
        )
    )


def _resource_not_found(uri: str) -> McpError:
    return McpError(
        types.ErrorData(
            code=types.INVALID_PARAMS,
            message=f"coalesce resource is not available: {uri}",
            data={"code": "RESOURCE_NOT_FOUND", "uri": uri},
        )
    )
