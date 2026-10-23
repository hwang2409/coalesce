"""Command-line entry point for the coalesce stdio MCP server."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import webbrowser
from pathlib import Path

from .graph import GraphError
from .server import run_stdio
from .service import DocumentService, _graph_error_response


async def _run(document: Path, *, viewer_enabled: bool, open_browser: bool) -> None:
    if not viewer_enabled:
        service = await asyncio.to_thread(DocumentService, document, warmup=False)
        await run_stdio(service)
        return

    from .viewer import ViewerSceneStore, ViewerServer

    store = ViewerSceneStore()
    with ViewerServer(store) as viewer:
        # The document loads once, before the URL is shown, so a bad document never advertises it.
        service = await asyncio.to_thread(
            DocumentService,
            document,
            scene_store=store,
            viewer_url=viewer.url,
            warmup=False,
        )
        print(f"coalesce viewer: {viewer.url}", file=sys.stderr, flush=True)
        if open_browser:
            webbrowser.open(viewer.url)
        await run_stdio(service)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run coalesce as a stdio MCP server")
    parser.add_argument(
        "--document",
        type=Path,
        default=os.environ.get("COALESCE_DOCUMENT"),
        help="*.coalesce.json document path (or set COALESCE_DOCUMENT)",
    )
    parser.add_argument("--no-viewer", action="store_true", help="do not start the local viewer")
    parser.add_argument("--open", action="store_true", help="open the viewer in the default browser")
    arguments = parser.parse_args()
    if arguments.document is None:
        parser.error("--document or COALESCE_DOCUMENT is required")
    if arguments.no_viewer and arguments.open:
        parser.error("--open cannot be used with --no-viewer")
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(
            _run(
                arguments.document,
                viewer_enabled=not arguments.no_viewer,
                open_browser=arguments.open,
            )
        )
    except GraphError as error:
        try:
            revision = int(json.loads(arguments.document.read_text()).get("revision", 0))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            revision = 0
        failure = _graph_error_response(revision, None, "load", error)
        print(json.dumps(failure), file=sys.stderr, flush=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
