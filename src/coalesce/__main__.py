"""Command-line entry point for the coalesce stdio MCP server."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from .server import run_stdio
from .service import DocumentService


def main() -> None:
    parser = argparse.ArgumentParser(description="Run coalesce as a stdio MCP server")
    parser.add_argument(
        "--document",
        type=Path,
        default=os.environ.get("COALESCE_DOCUMENT"),
        help="*.coalesce.json document path (or set COALESCE_DOCUMENT)",
    )
    arguments = parser.parse_args()
    if arguments.document is None:
        parser.error("--document or COALESCE_DOCUMENT is required")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_stdio(DocumentService(arguments.document)))


if __name__ == "__main__":
    main()
