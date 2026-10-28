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
from .sketch_solver import (
    DEFAULT_SOLVE_TIMEOUT_SECONDS,
    MAX_SOLVE_TIMEOUT_SECONDS,
    MIN_SOLVE_TIMEOUT_SECONDS,
)


class _DocumentLoadFailure(Exception):
    def __init__(self, revision: int, error: GraphError):
        self.revision = revision
        self.error = error


def _revision(document: Path) -> int:
    try:
        return int(json.loads(document.read_text()).get("revision", 0))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return 0


def _load_service(document: Path, **kwargs) -> DocumentService:
    try:
        return DocumentService(document, warmup=False, **kwargs)
    except json.JSONDecodeError as error:
        failure = GraphError(
            "INVALID_DOCUMENT",
            "document contains invalid JSON",
            reason="json_syntax",
            line=error.lineno,
            column=error.colno,
        )
    except IsADirectoryError:
        failure = GraphError(
            "INVALID_DOCUMENT", "document path is a directory", reason="is_directory"
        )
    except PermissionError:
        failure = GraphError(
            "INVALID_DOCUMENT",
            "document permission was denied",
            reason="permission_denied",
        )
    except OSError:
        failure = GraphError(
            "INVALID_DOCUMENT", "document could not be read", reason="not_readable"
        )
    except GraphError as error:
        raise _DocumentLoadFailure(_revision(document), error) from None
    raise _DocumentLoadFailure(0, failure) from None


def _viewer_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer between 0 and 65535") from error
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be an integer between 0 and 65535")
    return port


def _sketch_solve_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number of seconds") from error
    if not MIN_SOLVE_TIMEOUT_SECONDS <= timeout <= MAX_SOLVE_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError(
            f"must be between {MIN_SOLVE_TIMEOUT_SECONDS:g} and "
            f"{MAX_SOLVE_TIMEOUT_SECONDS:g} seconds"
        )
    return timeout


async def _run(
    document: Path,
    *,
    viewer_enabled: bool,
    open_browser: bool,
    sketch_solve_timeout: float,
    viewer_port: int,
) -> None:
    if not viewer_enabled:
        service = await asyncio.to_thread(
            _load_service, document, sketch_solve_timeout=sketch_solve_timeout
        )
        await run_stdio(service)
        return

    from .viewer import ViewerSceneStore, ViewerServer

    store = ViewerSceneStore()
    with ViewerServer(store, port=viewer_port) as viewer:
        # Load persisted structure off the event loop; exact geometry starts in the background.
        service = await asyncio.to_thread(
            _load_service,
            document,
            scene_store=store,
            viewer_url=viewer.url,
            sketch_solve_timeout=sketch_solve_timeout,
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
    parser.add_argument(
        "--viewer-port",
        type=_viewer_port,
        default=0,
        help="local viewer port (0 chooses a free port)",
    )
    parser.add_argument(
        "--sketch-solve-timeout",
        type=_sketch_solve_timeout,
        default=os.environ.get(
            "COALESCE_SKETCH_SOLVE_TIMEOUT", str(DEFAULT_SOLVE_TIMEOUT_SECONDS)
        ),
        metavar="SECONDS",
        help=(
            "constrained sketch solve timeout in seconds, "
            f"{MIN_SOLVE_TIMEOUT_SECONDS:g}..{MAX_SOLVE_TIMEOUT_SECONDS:g} "
            "(or set COALESCE_SKETCH_SOLVE_TIMEOUT)"
        ),
    )
    parser.add_argument("--open", action="store_true", help="open the viewer in the default browser")
    arguments = parser.parse_args()
    if arguments.document is None:
        parser.error("--document or COALESCE_DOCUMENT is required")
    if arguments.no_viewer and arguments.open:
        parser.error("--open cannot be used with --no-viewer")
    logging.basicConfig(level=logging.INFO)
    try:
        if sys.platform == "win32":
            # OCCT can deadlock when its first import happens on the background warmup thread.
            __import__("build123d")
        asyncio.run(
            _run(
                arguments.document,
                viewer_enabled=not arguments.no_viewer,
                open_browser=arguments.open,
                sketch_solve_timeout=arguments.sketch_solve_timeout,
                viewer_port=arguments.viewer_port,
            )
        )
    except _DocumentLoadFailure as error:
        failure = _graph_error_response(error.revision, None, "load", error.error)
        print(json.dumps(failure), file=sys.stderr, flush=True)
        sys.exit(2)
    except ValueError as error:
        print(f"coalesce: {error}", file=sys.stderr, flush=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
